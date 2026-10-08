"""Giveaway service — entry recording, winner draw, and prize granting.

This is the single place that mutates giveaway state. Handlers and the
scheduler call into these helpers; they never touch the ORM tables directly.

Design notes:
  * One-entry-per-user is enforced by the DB unique index
    ``ix_giveaway_entry_uniq (giveaway_id, user_id)``. ``record_entry`` inserts
    optimistically and treats an IntegrityError as "already joined" — a true
    atomic guard that survives concurrent double-taps and callback replays.
  * Prize granting reuses the per-type logic from
    ``services.gspin_reward_service.apply_reward`` (coins / gems / quest_points /
    player, with the roster-cap + overflow fallback for player prizes).
  * ``draw_winners`` is idempotent via the ``winners_drawn_at`` guard so an
    overlapping scheduler tick (or a manual "Draw now" racing the sweeper) can
    never pay out twice.
"""

import logging
import random
import re
from datetime import datetime

from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# Currency prize types → the User column each credits.
_CURRENCY_TYPES = ("coins", "gems", "quest_points")

# Entry-count winner tiers for ``auto_winners`` giveaways: (min entries, winners).
# Checked top-down, so 31+ entries → 5 winners, 21-30 → 4, 10-20 → 3, else 1.
WINNER_TIERS = ((31, 5), (21, 4), (10, 3), (0, 1))
MAX_TIERED_WINNERS = WINNER_TIERS[0][1]


def tiered_winner_count(entries: int) -> int:
    """How many winners a tiered giveaway draws for ``entries`` eligible entries."""
    entries = int(entries or 0)
    for floor, winners in WINNER_TIERS:
        if entries >= floor:
            return winners
    return 1


def tier_table_text() -> str:
    """One-line description of the winner tiers for announcements."""
    return "1–9 entries → 1 winner · 10–20 → 3 · 21–30 → 4 · 31+ → 5"


def winner_slots(giveaway, eligible_count: int) -> int:
    """Winner slots for this giveaway given ``eligible_count`` eligible entries."""
    if getattr(giveaway, "auto_winners", False):
        return tiered_winner_count(eligible_count)
    return int(giveaway.num_winners or 1)


# ── Official-group presentation helpers ──────────────────────────────

def group_handle(cfg) -> str | None:
    """Return the Official Group public @handle for display (e.g. '@cmugames'),
    or None when only a private invite link / numeric id is configured.

    Prefers the admin-set ``branding_group_username``; otherwise parses a public
    ``t.me/<username>`` out of ``official_group_link`` (private ``t.me/+…`` invite
    links have no public handle, so they yield None)."""
    if not cfg:
        return None
    uname = (getattr(cfg, "branding_group_username", None) or "").strip().lstrip("@")
    if uname:
        return "@" + uname
    link = (getattr(cfg, "official_group_link", None) or "").strip()
    m = re.search(r"t\.me/([A-Za-z0-9_]{3,})$", link)
    if m:
        return "@" + m.group(1)
    return None


def group_join_url(cfg) -> str | None:
    """Return a clickable URL to join the Official Group, or None."""
    if not cfg:
        return None
    link = (getattr(cfg, "official_group_link", None) or "").strip()
    if link:
        return link
    uname = (getattr(cfg, "branding_group_username", None) or "").strip().lstrip("@")
    if uname:
        return "https://t.me/" + uname
    return None


# ── Entry ────────────────────────────────────────────────────────────

def record_entry(session, giveaway, user, telegram_id) -> str:
    """Record one user's entry. Returns 'joined', 'already', or 'error'.

    Relies on the unique (giveaway_id, user_id) index — a concurrent second
    insert raises IntegrityError, which we map to 'already'. Caller commits on
    'joined'. This is the ONLY supported way to create an entry.
    """
    from models import GiveawayEntry
    entry = GiveawayEntry(
        giveaway_id=giveaway.id,
        user_id=user.id,
        telegram_id=int(telegram_id),
        joined_at=datetime.utcnow(),
    )
    session.add(entry)
    try:
        session.flush()  # trigger the unique-index check now, not at commit
    except IntegrityError:
        session.rollback()
        return "already"
    except Exception:
        session.rollback()
        logger.exception("record_entry failed for giveaway %s user %s",
                         giveaway.id, user.id)
        return "error"
    return "joined"


def entry_count(session, giveaway_id) -> int:
    from models import GiveawayEntry
    return (session.query(GiveawayEntry)
            .filter(GiveawayEntry.giveaway_id == giveaway_id).count())


# ── Prize granting ───────────────────────────────────────────────────

def _grant_prize(session, giveaway, user) -> str:
    """Grant the giveaway's prize to one user. Returns a human-readable detail
    string (e.g. "50,000 coins" or "Virat Kohli (91)"). Caller commits."""
    ptype = giveaway.prize_type

    if ptype in _CURRENCY_TYPES:
        amt = int(giveaway.prize_amount or 0)
        if ptype == "coins":
            user.total_coins = (user.total_coins or 0) + amt
            log_action, coins_change, gems_change = "giveaway_coins", amt, 0
            label = f"{amt:,} coins"
        elif ptype == "gems":
            user.total_gems = (user.total_gems or 0) + amt
            log_action, coins_change, gems_change = "giveaway_gems", 0, amt
            label = f"{amt:,} gems"
        else:  # quest_points
            user.quest_points = (user.quest_points or 0) + amt
            log_action, coins_change, gems_change = "giveaway_qp", 0, 0
            label = f"{amt:,} quest points"

        from services.activity_service import log_activity
        log_activity(session, user.id, log_action,
                     detail=f"Giveaway #{giveaway.id} '{giveaway.title}' — {label}",
                     coins_change=coins_change, gems_change=gems_change)
        return label

    if ptype == "player":
        return _grant_player(session, giveaway, user)

    logger.warning("Unknown giveaway prize_type %r on giveaway %s",
                   ptype, giveaway.id)
    return "(no prize)"


def _grant_player(session, giveaway, user) -> str:
    """Grant the giveaway's specific player card, mirroring the roster-cap +
    overflow handling in gspin_reward_service.apply_reward."""
    from models import Player, UserRoster
    from config import MAX_ROSTER

    player = (session.query(Player)
              .filter(Player.id == giveaway.prize_player_id).first())
    if not player:
        logger.warning("Giveaway %s prize player %s not found",
                      giveaway.id, giveaway.prize_player_id)
        return "(player unavailable)"
    if getattr(player, "is_career", False):
        # A career card belongs to exactly one user — never hand it out.
        logger.warning("Giveaway %s prize player %s is a career card — not granted",
                       giveaway.id, player.id)
        return "(player unavailable)"

    label = f"{player.name} ({player.rating})"

    if (user.roster_count or 0) < MAX_ROSTER:
        entry = UserRoster(
            user_id=user.id, player_id=player.id,
            order_position=(user.roster_count or 0) + 1,
            acquired_date=datetime.utcnow(),
        )
        session.add(entry)
        user.roster_count = (user.roster_count or 0) + 1
    else:
        # Roster full — park it as a pending overflow claim so the card is never
        # lost (same path GSpin/daily use). If parking fails, fall back to
        # crediting the card's sell value in coins so the winner is never told
        # they won a prize that wasn't actually granted.
        try:
            from services.overflow_service import record_overflow
            record_overflow(session, user, player, source="giveaway")
            label += " — roster full, held as pending claim"
        except Exception:
            logger.exception("giveaway overflow park failed for user %s", user.id)
            try:
                from config import get_sell_value
                coins = int(get_sell_value(player.rating) or 0)
            except Exception:
                coins = 0
            user.total_coins = (user.total_coins or 0) + coins
            label = f"{coins:,} coins (roster full — {player.name} converted)"
            from services.activity_service import log_activity
            log_activity(session, user.id, "giveaway_player_coins",
                         detail=f"Giveaway #{giveaway.id} '{giveaway.title}' — {label}",
                         coins_change=coins)
            return label

    from services.activity_service import log_activity
    log_activity(session, user.id, "giveaway_player",
                 detail=f"Giveaway #{giveaway.id} '{giveaway.title}' — {label}",
                 player_name=player.name, player_rating=player.rating)
    return label


# ── Winner draw ──────────────────────────────────────────────────────

def _seat_winners(eligible, slots, giveaway_id=None):
    """Pick ``slots`` winners from ``eligible`` [(entry, user), …].

    Admin-marked priority entries take the first seats, ordered by when they
    were marked (``priority_set_at``, then entry id so an un-timestamped legacy
    row still sorts deterministically). Whatever is left over is filled by
    ``random.sample`` from the unmarked entries, which is the original behaviour
    for a giveaway nobody has marked anyone in.
    """
    if slots <= 0:
        return []

    priority = [pair for pair in eligible if getattr(pair[0], "is_priority", False)]
    rest = [pair for pair in eligible if not getattr(pair[0], "is_priority", False)]
    priority.sort(key=lambda pair: (getattr(pair[0], "priority_set_at", None)
                                    or datetime.max,
                                    getattr(pair[0], "id", 0) or 0))

    if len(priority) > slots:
        logger.warning(
            "Giveaway %s has %d priority entries for %d winner slot(s) — "
            "seating the %d earliest-marked, the rest do not win",
            giveaway_id, len(priority), slots, slots)

    chosen = priority[:slots]
    remaining = slots - len(chosen)
    if remaining > 0 and rest:
        chosen += random.sample(rest, min(remaining, len(rest)))
    return chosen


def draw_winners(session, giveaway, eligible_user_ids=None,
                 exclude_user_ids=None) -> list[dict]:
    """Draw winners, grant prizes, and mark the giveaway ended.

    Idempotent: if ``winners_drawn_at`` is already set, returns [] and does
    nothing (guards against double payout from overlapping ticks).

    Eligibility re-validation at draw time (anti-cheat):
      * Banned users (``User.is_banned``) are always dropped.
      * When ``eligible_user_ids`` is provided (the caller computes live Official
        GC membership via the bot), only those users survive — so anyone who
        left the GC after joining is excluded.

    Priority (guaranteed) winners:
      Entries an admin marked from the Participants tab (``is_priority``) are
      seated FIRST, in the order they were marked, and the remaining slots are
      filled at random from everyone else. So a marked participant wins as long
      as they are still eligible above — a priority flag is a reserved seat, not
      a bypass of the ban / GC checks, and it cannot conjure a prize for someone
      who left the group. If more entries are marked than there are slots, the
      earliest-marked ones win and the rest are logged as overflow.

    ``exclude_user_ids`` (recent automatic-giveaway winners) are passed over at
    random selection but still keep a seat an admin reserved for them.

    Tiered giveaways (``auto_winners``) size the draw from the number of
    eligible entries — see :func:`tiered_winner_count`.

    ``random.sample`` over the surviving DISTINCT entries guarantees no user
    wins two slots. If there are fewer eligible entries than ``num_winners``,
    every eligible entrant wins. Returns a list of winner dicts. Caller commits.

    Concurrency: the giveaway row is re-fetched ``with_for_update()`` and the
    ``winners_drawn_at`` guard is checked while holding that row lock, so two
    processes finalizing the same giveaway (e.g. during a rolling deploy) can't
    both pay out — the second blocks until the first commits, then sees the flag
    set and returns []. (On SQLite the lock is a no-op, but there only one
    process runs.)
    """
    from models import Giveaway, GiveawayEntry, User

    # Claim the giveaway under a row lock. Operate on the locked instance so the
    # flag set below is protected by the same lock the caller's commit releases.
    giveaway = (session.query(Giveaway)
                .filter(Giveaway.id == giveaway.id)
                .with_for_update()
                .first())
    if giveaway is None or giveaway.winners_drawn_at is not None:
        return []

    entries = (session.query(GiveawayEntry)
               .filter(GiveawayEntry.giveaway_id == giveaway.id)
               .all())

    # Re-validate eligibility.
    eligible = []
    for e in entries:
        user = session.query(User).filter(User.id == e.user_id).first()
        if not user or user.is_banned:
            continue
        if eligible_user_ids is not None and user.id not in eligible_user_ids:
            continue
        eligible.append((e, user))

    winners: list[dict] = []
    if eligible:
        slots = winner_slots(giveaway, len(eligible))
        if exclude_user_ids:
            eligible = [pair for pair in eligible
                        if getattr(pair[0], "is_priority", False)
                        or pair[1].id not in exclude_user_ids]
        n = min(slots, len(eligible))
        chosen = _seat_winners(eligible, n, giveaway.id)
        now = datetime.utcnow()
        for entry, user in chosen:
            detail = _grant_prize(session, giveaway, user)
            entry.is_winner = True
            entry.won_at = now
            entry.prize_detail = detail
            winners.append({
                "user_id": user.id,
                "telegram_id": entry.telegram_id,
                "username": user.username,
                "first_name": user.first_name,
                "prize_detail": detail,
            })

    giveaway.winners_drawn_at = datetime.utcnow()
    giveaway.status = "ended"
    return winners


# ── Text builders ────────────────────────────────────────────────────

def prize_label(giveaway) -> str:
    """Short prize description for announcements."""
    ptype = giveaway.prize_type
    if ptype == "coins":
        return f"🪙 {int(giveaway.prize_amount or 0):,} coins"
    if ptype == "gems":
        return f"💎 {int(giveaway.prize_amount or 0):,} gems"
    if ptype == "quest_points":
        return f"🎯 {int(giveaway.prize_amount or 0):,} quest points"
    if ptype == "player":
        # prize_label is embedded into Telegram HTML messages, so escape the
        # (admin/imported) player name — a stray & or < would 400 the send.
        name = _esc(giveaway.prize_player.name) if giveaway.prize_player else "a player card"
        rating = f" ({giveaway.prize_player.rating})" if giveaway.prize_player else ""
        return f"🃏 {name}{rating}"
    return "a prize"


def winners_line(giveaway) -> str:
    """'👥 3 winners', or the tier table for a tiered giveaway."""
    if getattr(giveaway, "auto_winners", False):
        return f"👥 More entries = more winners!\n<i>{tier_table_text()}</i>"
    winners_word = "winner" if (giveaway.num_winners or 1) == 1 else "winners"
    return f"👥 {giveaway.num_winners} {winners_word}"


def announcement_text(giveaway) -> str:
    """The message posted to every chat when the giveaway starts."""
    end_ist = _to_ist(giveaway.end_time)
    lines = [
        "🎉 <b>GIVEAWAY IS LIVE!</b> 🎉",
        "",
        f"<b>{_esc(giveaway.title)}</b>",
        "",
        f"🏆 Prize: <b>{prize_label(giveaway)}</b>",
        winners_line(giveaway),
        f"⏰ Ends: <b>{end_ist:%d %b %Y, %I:%M %p} IST</b>",
        "",
        "Tap the button below to enter.",
        "⚠️ You must be a member of the Official GC to participate.",
    ]
    return "\n".join(lines)


def winners_text(giveaway, winners: list[dict]) -> str:
    """The results message posted in the Official GC."""
    if not winners:
        return (f"🎉 <b>{_esc(giveaway.title)}</b>\n\n"
                "😔 The giveaway ended with no eligible participants. "
                "No winners this time!")
    lines = [
        f"🎉 <b>{_esc(giveaway.title)}</b> — RESULTS 🎉",
        "",
        f"🏆 Prize: <b>{prize_label(giveaway)}</b>",
        "",
        f"🥳 Congratulations to our {len(winners)} "
        f"{'winner' if len(winners) == 1 else 'winners'}:",
    ]
    for i, w in enumerate(winners, 1):
        who = _mention(w)
        lines.append(f"{i}. {who} — won <b>{_esc(w['prize_detail'])}</b>")
    lines.append("")
    lines.append("Prizes have been credited. 🎁")
    return "\n".join(lines)


def recent_auto_winner_ids(session, n=2, exclude_giveaway_id=None) -> set:
    """User ids that won any of the last ``n`` finished automatic giveaways."""
    from models import Giveaway, GiveawayEntry
    q = (session.query(Giveaway.id)
         .filter(Giveaway.is_auto.is_(True), Giveaway.winners_drawn_at.isnot(None)))
    if exclude_giveaway_id is not None:
        q = q.filter(Giveaway.id != exclude_giveaway_id)
    ids = [gid for (gid,) in q.order_by(Giveaway.winners_drawn_at.desc()).limit(n).all()]
    if not ids:
        return set()
    return {uid for (uid,) in (session.query(GiveawayEntry.user_id)
                               .filter(GiveawayEntry.giveaway_id.in_(ids),
                                       GiveawayEntry.is_winner.is_(True)).all())}


def status_line(giveaway, entries: int) -> str:
    """'14 entries → 3 winners' style progress line."""
    entries = int(entries or 0)
    word = "entry" if entries == 1 else "entries"
    if getattr(giveaway, "auto_winners", False):
        w = tiered_winner_count(entries)
        return f"{entries} {word} → {w} {'winner' if w == 1 else 'winners'}"
    return f"{entries} {word}"


def time_left_text(end_time, now=None) -> str:
    """Compact '2d 5h' / '3h 12m' / '9m' countdown."""
    secs = int(((end_time or datetime.utcnow()) - (now or datetime.utcnow())).total_seconds())
    if secs <= 0:
        return "ending now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{max(m, 1)}m"


def reminder_text(giveaway, entries: int) -> str:
    """The 'ending soon' nudge posted in the Official GC."""
    return "\n".join([
        "⏰ <b>GIVEAWAY ENDING SOON!</b>",
        "",
        f"<b>{_esc(giveaway.title)}</b>",
        f"🏆 Prize: <b>{prize_label(giveaway)}</b>",
        f"⌛ Ends in <b>{time_left_text(giveaway.end_time)}</b>",
        f"📊 {status_line(giveaway, entries)} so far",
        "",
        "Not in yet? Tap below to enter.",
    ])


# ── Small helpers ────────────────────────────────────────────────────

def _esc(text) -> str:
    """Minimal HTML escaping for Telegram parse_mode=HTML."""
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _mention(winner: dict) -> str:
    name = winner.get("username")
    if name:
        return f"@{_esc(name)}"
    display = winner.get("first_name") or str(winner.get("telegram_id"))
    return f'<a href="tg://user?id={winner["telegram_id"]}">{_esc(display)}</a>'


def _to_ist(dt: datetime) -> datetime:
    from datetime import timedelta
    return (dt or datetime.utcnow()) + timedelta(hours=5, minutes=30)
