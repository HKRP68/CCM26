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
from datetime import datetime

from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

# Currency prize types → the User column each credits.
_CURRENCY_TYPES = ("coins", "gems", "quest_points")


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

def draw_winners(session, giveaway, eligible_user_ids=None) -> list[dict]:
    """Draw winners, grant prizes, and mark the giveaway ended.

    Idempotent: if ``winners_drawn_at`` is already set, returns [] and does
    nothing (guards against double payout from overlapping ticks).

    Eligibility re-validation at draw time (anti-cheat):
      * Banned users (``User.is_banned``) are always dropped.
      * When ``eligible_user_ids`` is provided (the caller computes live Official
        GC membership via the bot), only those users survive — so anyone who
        left the GC after joining is excluded.

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
        n = min(int(giveaway.num_winners or 1), len(eligible))
        chosen = random.sample(eligible, n)
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


def announcement_text(giveaway) -> str:
    """The message posted to every chat when the giveaway starts."""
    winners_word = "winner" if (giveaway.num_winners or 1) == 1 else "winners"
    end_ist = _to_ist(giveaway.end_time)
    lines = [
        "🎉 <b>GIVEAWAY IS LIVE!</b> 🎉",
        "",
        f"<b>{_esc(giveaway.title)}</b>",
        "",
        f"🏆 Prize: <b>{prize_label(giveaway)}</b>",
        f"👥 {giveaway.num_winners} {winners_word}",
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
