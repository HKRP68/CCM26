"""Career Player — the one card that belongs to a single user (/cmucareer).

A career player is an ordinary ``players`` row, so it plays, scores, renders and
appears in an XI exactly like any other card. Three things make it different:

* **It is owned.** ``is_career`` + ``career_owner_user_id`` tie it to one user,
  and it must never be dealt from a shared pool (see
  :func:`services.player_service.not_career`), sold or traded.
* **It is grown, not pulled.** Every career player starts identical — all ten
  attributes at :data:`CAREER_START` — and the owner raises them one point at a
  time with gems. The three ratings the rest of the game reads
  (``bat_rating``, ``bowl_rating``, ``rating``) are *derived* from those
  attributes, so an upgrade immediately matters in the match engine, on the
  leaderboard and on the card, with no changes anywhere else.
* **It has its own face.** ``career_face`` names a card-template variant whose
  blank artwork already contains the player's face.
"""

import logging

from sqlalchemy.exc import IntegrityError

from models import Player, UserRoster

logger = logging.getLogger(__name__)


# ── Attributes ──────────────────────────────────────────────────────────────
# The five that feed Batting Power and the five that feed Bowling Specs. Order
# is the display order used by the bot and the Mini App.
BAT_ATTRS = ("technique", "power", "timing", "footwork", "composure")
BOWL_ATTRS = ("pace", "accuracy", "swing", "stamina", "variation")
ALL_ATTRS = BAT_ATTRS + BOWL_ATTRS

ATTR_LABELS = {
    "technique": "Technique", "power": "Power", "timing": "Timing",
    "footwork": "Footwork", "composure": "Composure",
    "pace": "Pace", "accuracy": "Accuracy", "swing": "Swing",
    "stamina": "Stamina", "variation": "Variation",
}

# Every attribute starts here, so a brand-new career player is exactly
# 78 Batting Power / 78 Bowling Specs / 78 OVR.
CAREER_START = 78
CAREER_MAX = 99

# Fixed identity of every career card.
CAREER_VERSION = "Career"
CAREER_CATEGORY = "All-rounder"

# Shown wherever a career card is refused for sale or trade.
CAREER_LOCKED_MESSAGE = (
    "🎖 Your Career Player is yours for good — it can't be sold or traded."
)


def attr_column(attr):
    """Return the ``players`` column name backing an attribute key."""
    return f"attr_{attr}"


def upgrade_cost(new_value):
    """Gems to raise an attribute *to* ``new_value`` — the value itself.

    78 → 79 costs 79 💎, 79 → 80 costs 80 💎, and so on.
    """
    return int(new_value)


def total_invested(player):
    """Gems spent on this career player so far, derived from its attributes.

    Every attribute starts at :data:`CAREER_START` and each step costs its new
    value, so the total spent on an attribute now at ``v`` is the sum of the
    integers from ``CAREER_START + 1`` to ``v``. Exact, and needs no stored
    counter to drift out of sync — the one caveat being that an attribute
    edited directly by an admin makes the figure notional.
    """
    base = CAREER_START * (CAREER_START + 1)
    total = 0
    for attr in ALL_ATTRS:
        value = int(getattr(player, attr_column(attr), CAREER_START) or CAREER_START)
        if value > CAREER_START:
            total += (value * (value + 1) - base) // 2
    return total


def cost_to_max(player):
    """Gems still needed to take every attribute to :data:`CAREER_MAX`."""
    base = CAREER_MAX * (CAREER_MAX + 1)
    total = 0
    for attr in ALL_ATTRS:
        value = int(getattr(player, attr_column(attr), CAREER_START) or CAREER_START)
        if value < CAREER_MAX:
            total += (base - value * (value + 1)) // 2
    return total


def _round_half_up(value):
    """Round .5 upwards.

    Python's built-in ``round`` is banker's rounding, so an average of 80.5
    would land on 80 while 81.5 landed on 82 — arbitrary-looking on a card the
    owner is paying to improve. Always rounding up keeps progress predictable.
    """
    return int(value + 0.5)


def _mean(player, attrs):
    values = [int(getattr(player, attr_column(a), CAREER_START) or CAREER_START)
              for a in attrs]
    return _round_half_up(sum(values) / len(values)) if values else CAREER_START


def recompute_ratings(player):
    """Derive Batting Power, Bowling Specs and OVR from the ten attributes.

    With every attribute at 78 this yields exactly 78/78/78, and each upgrade
    moves the discipline average — which is what the simulation engine and the
    card renderer already read.
    """
    player.bat_rating = _mean(player, BAT_ATTRS)
    player.bowl_rating = _mean(player, BOWL_ATTRS)
    player.rating = _round_half_up((player.bat_rating + player.bowl_rating) / 2)
    return player.rating


def projected_ratings(player, attr=None, steps=0):
    """Ratings this player *would* have after ``steps`` more points in ``attr``.

    Used to show "next OVR at …" without mutating anything.
    """
    values = {a: int(getattr(player, attr_column(a), CAREER_START) or CAREER_START)
              for a in ALL_ATTRS}
    if attr in values:
        values[attr] = min(CAREER_MAX, values[attr] + int(steps))
    bat = _round_half_up(sum(values[a] for a in BAT_ATTRS) / len(BAT_ATTRS))
    bowl = _round_half_up(sum(values[a] for a in BOWL_ATTRS) / len(BOWL_ATTRS))
    return {"bat_rating": bat, "bowl_rating": bowl,
            "rating": _round_half_up((bat + bowl) / 2)}


# ── Lookup ──────────────────────────────────────────────────────────────────

def is_career_player(player):
    """True when a ``Player`` row (or None) is somebody's career card."""
    return bool(player is not None and getattr(player, "is_career", False))


def get_career_player(session, user_id):
    """Return the user's career player, or ``None`` if they have not made one."""
    if not user_id:
        return None
    return (session.query(Player)
            .filter(Player.is_career.is_(True),
                    Player.career_owner_user_id == user_id)
            .first())


def career_roster_entry(session, user_id):
    """Return the ``UserRoster`` row holding the user's career player."""
    player = get_career_player(session, user_id)
    if not player:
        return None
    return (session.query(UserRoster)
            .filter(UserRoster.user_id == user_id,
                    UserRoster.player_id == player.id)
            .first())


def career_roster_ids(session, user_id):
    """Roster ids that hold a career card — cheap guard for sell/release paths."""
    entry = career_roster_entry(session, user_id)
    return {entry.id} if entry else set()


# ── Creation ────────────────────────────────────────────────────────────────

def _validated_face_slot(session, face_slot):
    """The slot as an int when a live face actually wears it, else ``None``.

    The wizard only ever offers faces that exist, are active and have artwork,
    but its answers are several button presses old by the time they arrive here
    — and nothing stops a caller passing a slot number that was never a face at
    all. An unknown slot would be stored happily and then render on face 1 (or
    the base card) with nothing to explain why, so it is refused instead.

    Deleted artwork is deliberately *not* checked: falling back to face 1 is the
    documented behaviour when an admin removes a design, and re-checking the
    filesystem here would fail a creation that renders perfectly well.
    """
    try:
        face_slot = int(face_slot)
    except (TypeError, ValueError):
        return None
    try:
        from services.career_face_service import list_faces
        live = {face.slot for face in list_faces(session, active_only=True)}
    except Exception:
        logger.exception("career face validation failed")
        return None
    return face_slot if face_slot in live else None


def create_career_player(session, user, *, name, country, bat_hand, bowl_hand,
                         bowl_style, face_slot):
    """Create the user's career player and add it to their roster.

    Caller commits. Returns ``{"ok": True, "player": Player}`` or an error dict.
    Everything is re-checked here, inside the caller's transaction, because the
    wizard collected its answers several button presses ago: the name may have
    been taken since, the chosen face may have been deleted or deactivated by an
    admin, and the roster may have filled up. The database's
    ``uq_players_career_owner`` partial index is the backstop for the one check
    that a re-read cannot win on its own — two concurrent taps.
    """
    from config import MAX_ROSTER
    from services.career_name_service import name_is_taken

    if get_career_player(session, user.id):
        return {"ok": False, "error": "exists",
                "message": "You already have a Career Player."}

    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "no_name",
                "message": "Could not generate a name. Please try again."}
    if name_is_taken(session, name):
        return {"ok": False, "error": "name_taken",
                "message": "That name was just taken — pick a different letter."}

    face_slot = _validated_face_slot(session, face_slot)
    if face_slot is None:
        return {"ok": False, "error": "bad_face",
                "message": ("That face is no longer available. "
                            "Run /cmucareer again to pick another.")}

    # Count the roster rows rather than trusting ``user.roster_count``: the
    # counter is what we are about to increment, and a career card that slipped
    # past the cap could never be released to get back under it.
    held = (session.query(UserRoster)
            .filter(UserRoster.user_id == user.id).count())
    if held >= MAX_ROSTER:
        return {"ok": False, "error": "roster_full",
                "message": (f"Your roster is full ({MAX_ROSTER}). Release a "
                            f"player first, then run /cmucareer again.")}

    player = Player(
        name=name,
        version=CAREER_VERSION,
        rating=CAREER_START,
        category=CAREER_CATEGORY,
        country=country,
        bat_hand=bat_hand,
        bowl_hand=bowl_hand,
        bowl_style=bowl_style,
        bat_rating=CAREER_START,
        bowl_rating=CAREER_START,
        is_active=True,
        is_career=True,
        career_owner_user_id=user.id,
        career_face=f"career_{face_slot}",
        # Career cards are personal: never tradable, never buyable by name.
        non_tradable=True,
        restricted_from_buypl=True,
    )
    for attr in ALL_ATTRS:
        setattr(player, attr_column(attr), CAREER_START)
    recompute_ratings(player)
    try:
        # A SAVEPOINT, not the whole transaction: a losing race must undo this
        # one insert and nothing else. session.rollback() here would discard
        # whatever the caller had already buffered — the freshly created User
        # on the /debut-then-/cmucareer path, for one — and leave them with a
        # polite error message and no account.
        with session.begin_nested():
            session.add(player)
            session.flush()
    except IntegrityError:
        # A concurrent request beat us between the checks above and this flush.
        # Two indexes can fire: uq_players_career_owner (they created their own
        # card) or uq_players_name_version (somebody else took the name). Ask
        # which it was rather than guessing — the two need different advice.
        logger.info("concurrent career creation for user %s hit a unique index",
                    user.id)
        if get_career_player(session, user.id):
            return {"ok": False, "error": "exists",
                    "message": "You already have a Career Player."}
        return {"ok": False, "error": "name_taken",
                "message": "That name was just taken — pick a different letter."}

    session.add(UserRoster(user_id=user.id, player_id=player.id,
                           order_position=held + 1))
    user.roster_count = held + 1

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "cmucareer",
                     f"Created Career Player {name} ({country})",
                     player_name=name, player_rating=player.rating)
    except Exception:
        logger.exception("career creation activity log failed")

    return {"ok": True, "player": player}


# ── Upgrading ───────────────────────────────────────────────────────────────

def _invalidate_card(player):
    """Drop every cached render of this card so the new numbers show at once.

    The stored ``gen_card_file_id`` is cleared on the ORM object here, inside
    the caller's transaction, and only the in-memory caches are dropped —
    ``invalidate_card_cache`` would open a second session to UPDATE this very
    row and block on the lock the caller is still holding.
    """
    player.gen_card_file_id = None
    try:
        from services.card_generator import drop_cached_cards
        drop_cached_cards(player.id)
    except Exception:
        logger.exception("career card cache invalidation failed")


def affordable_steps(player, attr, gems):
    """How many points of ``attr`` ``gems`` buys, respecting the 99 cap."""
    value = int(getattr(player, attr_column(attr), CAREER_START) or CAREER_START)
    spent = steps = 0
    while value < CAREER_MAX and spent + upgrade_cost(value + 1) <= gems:
        value += 1
        spent += upgrade_cost(value)
        steps += 1
    return steps, spent


def upgrade_attribute(session, user, attr, steps=1):
    """Spend gems to raise one attribute. Caller commits.

    The user row is re-read ``FOR UPDATE`` before the balance is touched so two
    rapid taps in the Mini App cannot both pass the affordability check and
    overdraw the wallet.
    """
    from models import User

    attr = (attr or "").strip().lower()
    if attr not in ALL_ATTRS:
        return {"ok": False, "error": "bad_attr",
                "message": "Unknown attribute."}
    try:
        steps = int(steps)
    except (TypeError, ValueError):
        steps = 0
    if steps < 1:
        return {"ok": False, "error": "bad_steps",
                "message": "Choose at least one point to upgrade."}

    player = get_career_player(session, user.id)
    if not player:
        return {"ok": False, "error": "no_career",
                "message": "You don't have a Career Player yet. Use /cmucareer."}

    # Serialise concurrent upgrades on this wallet. SQLite has no row locks and
    # raises on FOR UPDATE, so fall back to a plain re-read there.
    locked = None
    try:
        locked = (session.query(User).filter(User.id == user.id)
                  .with_for_update().first())
    except Exception:
        locked = session.query(User).filter(User.id == user.id).first()
    wallet = locked or user

    current = int(getattr(player, attr_column(attr), CAREER_START) or CAREER_START)
    if current >= CAREER_MAX:
        return {"ok": False, "error": "maxed",
                "message": f"{ATTR_LABELS[attr]} is already at the {CAREER_MAX} cap."}

    target = min(CAREER_MAX, current + steps)
    cost = sum(upgrade_cost(v) for v in range(current + 1, target + 1))
    balance = int(wallet.total_gems or 0)
    if cost > balance:
        return {"ok": False, "error": "insufficient_gems", "cost": cost,
                "balance": balance,
                "message": (f"{ATTR_LABELS[attr]} {current} → {target} costs "
                            f"{cost:,} 💎, you have {balance:,} 💎.")}

    wallet.total_gems = balance - cost
    if locked is not None and locked is not user:
        user.total_gems = wallet.total_gems
    setattr(player, attr_column(attr), target)
    old_rating = player.rating
    recompute_ratings(player)
    _invalidate_card(player)
    session.flush()

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "career_upgrade",
                     f"{ATTR_LABELS[attr]} {current} → {target} "
                     f"({player.name}) for {cost} gems",
                     gems_change=-cost, player_name=player.name,
                     player_rating=player.rating)
    except Exception:
        logger.exception("career upgrade activity log failed")

    return {
        "ok": True, "attr": attr, "label": ATTR_LABELS[attr],
        "from": current, "to": target, "cost": cost,
        "balance": wallet.total_gems,
        "rating": player.rating, "rating_changed": player.rating != old_rating,
        "bat_rating": player.bat_rating, "bowl_rating": player.bowl_rating,
    }


# ── Presentation ────────────────────────────────────────────────────────────

def attribute_rows(player):
    """``[{key, label, value, next_cost, maxed, group}]`` in display order."""
    rows = []
    for attr in ALL_ATTRS:
        value = int(getattr(player, attr_column(attr), CAREER_START) or CAREER_START)
        maxed = value >= CAREER_MAX
        rows.append({
            "key": attr,
            "label": ATTR_LABELS[attr],
            "value": value,
            "next_cost": None if maxed else upgrade_cost(value + 1),
            "maxed": maxed,
            "group": "batting" if attr in BAT_ATTRS else "bowling",
        })
    return rows


def career_stats(session, user_id, player=None):
    """Return the career player's match record as a plain dict.

    Keys match the ``PlayerGameStats`` columns and computed properties, which is
    exactly the shape ``services.cmu_stats_card_service`` expects — so the stats
    cards render with no adapter beyond this function. A career player who has
    never played gets a zeroed dict rather than ``None``.
    """
    from models import PlayerGameStats

    player = player or get_career_player(session, user_id)
    if not player:
        return None

    row = (session.query(PlayerGameStats)
           .filter(PlayerGameStats.user_id == user_id,
                   PlayerGameStats.player_id == player.id)
           .first())
    fields = ("potm", "bat_inns", "runs", "fifties", "hundreds", "fours",
              "sixes", "balls_faced", "times_out", "ducks", "highest_score",
              "bowl_inns", "wickets_taken", "runs_conceded", "overs_bowled",
              "balls_bowled", "three_fers", "five_fers", "hattricks",
              "best_bowl_wickets", "best_bowl_runs")
    props = ("bat_avg", "bat_sr", "bowl_avg", "bowl_economy", "bowl_sr",
             "hs_str", "bbf_str")
    if not row:
        stats = {f: 0 for f in fields}
        stats.update({"bat_avg": 0.0, "bat_sr": 0.0, "bowl_avg": 0.0,
                      "bowl_economy": 0.0, "bowl_sr": 0.0,
                      "hs_str": "-", "bbf_str": "-", "played": False})
        return stats
    stats = {f: getattr(row, f, 0) or 0 for f in fields}
    stats.update({p: getattr(row, p) for p in props})
    stats["played"] = True
    return stats
