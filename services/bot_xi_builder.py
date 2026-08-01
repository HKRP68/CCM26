"""Automatic Playing XI generation for the AI opponent in /lpbot and /ciplbot.

Two builders, one per squad source, plus the inline-trait helper both the new
bot modes and /vsbot share:

  • ``build_letsplay_bot_xi`` — /lpbot. The bot has no roster of its own, so its
    XI is assembled from the master ``Player`` pool, strength-matched to the
    user's own fielded XI and legal under the personal-roster rules
    (``services.xi_rules.validate_roster_xi``).

  • ``build_challenge_bot_xi`` — /ciplbot. The bot's XI is picked out of one
    league team's ``ChallengePlayer`` squad, highest OVR first, legal under the
    Challenge League rules (``services.xi_rules.validate_challenge_xi``,
    including the league's overseas min/max).

Both return the XI in BATTING ORDER: keepers/batsmen/all-rounders first (by
batting rating, high → low), then the bowlers (also high → low). Selection is
"best available"; the ordering just keeps the card looking like a real line-up
instead of putting a 90-rated quick at number one.

No Telegram here — these are plain functions so they can be unit-tested.
"""

import logging
import random

from services import xi_rules

from services.player_service import not_career

logger = logging.getLogger(__name__)


# Roster-id space for auto-built /lpbot players. Negative so it can never
# collide with a real ``UserRoster.id``, and offset clear of the spaces
# handlers/vsbot.py already uses (``-btp.id`` and ``-(100000 + player.id)``).
LPBOT_RID_BASE = 200000

# The bot fields an XI of the same strength as the user's — a fair practice
# opponent, neither a pushover nor a wall.
LPBOT_RATING_DELTA = 0
LPBOT_RATING_BAND = 8

# "Bot team will have some of Traits": five of the eleven carry one, so the bot
# feels like a real trait-equipped opponent without out-gunning a normal roster.
LPBOT_TRAIT_LEVEL = 3
LPBOT_BAT_TRAITS = ("bat_anchor", "bat_finisher", "bat_power_hitter")
LPBOT_BOWL_TRAITS = ("bowl_wicket_hunter", "bowl_death")

# 4 + 1 + 2 + 4 = 11, and it satisfies every clause of validate_xi: 3-5 Batsmen,
# 3-5 Bowlers, 1-2 Wicket Keepers, 1-3 All-rounders. Staying at two all-rounders
# also keeps the "3rd all-rounder must bowl worse than every pure Bowler" rule
# out of play, so a legal XI never depends on the pool's rating spread.
LPBOT_SHAPE = (("Batsman", 4), ("Wicket Keeper", 1), ("All-rounder", 2), ("Bowler", 4))

# Batting-order blocks: top-order roles bat first, the specialist attack last.
_ORDER_BLOCK = {"Batsman": 0, "Wicket Keeper": 0, "All-rounder": 1, "Bowler": 2}


def trait_dict(effect_key, level):
    """Build the inline trait dict the engine reads, for a player with no
    ``PlayerTrait`` rows of their own (the bot owns no cards).

    Same shape as ``handlers.letsplay._roster_traits`` produces, so
    ``services.cipl_match._make_trait_hook`` and ``handlers.match._calc`` treat a
    bot's trait exactly like a user's. Returns None for an unknown effect key.
    """
    from services.trait_service import TRAIT_DEFINITIONS
    meta = next((t for t in TRAIT_DEFINITIONS if t["effect_key"] == effect_key), None)
    if not meta:
        return None
    return {
        "effect_key": effect_key,
        "level": level,
        "display_name": meta["name"],
        "emoji": meta["emoji"],
        "category": meta["category"],
    }


# ════════════════════════════════════════════════════════════════════
# /lpbot — auto-built from the master Player pool
# ════════════════════════════════════════════════════════════════════

def _target_rating(session, user_id):
    """Average rating of the XI the user actually fields.

    Tuned against the fielded top 11 rather than their best cards overall —
    otherwise benched stars would inflate the bot and it would tower over the
    team the user is really putting out.
    """
    try:
        from handlers.lineup import _get_ordered_roster
        fielded = _get_ordered_roster(session, user_id)[:11]
        ratings = [p.rating for _e, p in fielded if p and p.rating]
        if ratings:
            return sum(ratings) / len(ratings)
    except Exception:
        logger.exception("lpbot: reading the user's fielded XI failed (user %s)", user_id)
    try:
        from services.quick_match_service import get_user_team_rating
        return get_user_team_rating(session, user_id)
    except Exception:
        logger.exception("lpbot: team-rating fallback failed (user %s)", user_id)
        return 60.0


def _pick_category(session, category, need, seen, lo, hi):
    """Best ``need`` active players of ``category``, highest rating first.

    Tries the target band first and then the whole pool, skipping any player
    whose base id is already used (so two versions of the same cricketer can
    never both make the XI — the same rule ``validate_xi`` enforces for users).
    """
    from models import Player
    from services.sim_team import base_player_id

    out = []
    for banded in (True, False):
        if len(out) >= need:
            break
        q = (not_career(session.query(Player))
             .filter(Player.category == category, Player.is_active == True))  # noqa: E712
        if banded:
            q = q.filter(Player.rating.between(lo, hi))
        for p in q.order_by(Player.rating.desc()).limit(max(need * 6, 30)).all():
            if len(out) >= need:
                break
            bid = base_player_id(p)
            if bid in seen:
                continue
            seen.add(bid)
            out.append(p)
    return out


def _lpbot_player_dict(player, traits=None):
    return {
        "roster_id": -(LPBOT_RID_BASE + int(player.id)),
        "player_id": int(player.id),
        "name": player.name or "Player",
        "rating": int(player.rating or 50),
        "category": player.category or "Batsman",
        "bat_rating": int(player.bat_rating or 40),
        "bowl_rating": int(player.bowl_rating or 40),
        "bowl_style": player.bowl_style or "",
        "bowl_hand": player.bowl_hand or "Right",
        "bat_hand": player.bat_hand or "Right",
        # Carried so a bot XI is scored for Team Chemistry on its real
        # countries and editions rather than on missing fields.
        "country": getattr(player, "country", None),
        "version": getattr(player, "version", None),
        "is_bot_player": True,
        "traits": list(traits or []),
    }


def _batting_order(rows, category_of, bat_rating_of, rating_of):
    """Order a squad into a believable line-up: top-order roles first, then the
    specialist bowlers, each block sorted by batting rating (high → low)."""
    return sorted(
        rows,
        key=lambda r: (_ORDER_BLOCK.get(category_of(r), 1),
                       -(bat_rating_of(r) or 0),
                       -(rating_of(r) or 0),
                       str(getattr(r, "name", "") or "")))


def _assign_lpbot_traits(ordered):
    """Give five of the eleven one role-matched trait.

    The top three in the batting order get a batting trait and the two
    best-rated bowlers get a bowling one — the shape a human roster with a few
    trait cards tends to have.
    """
    bat_targets = [p for p in ordered
                   if p["category"] in ("Batsman", "Wicket Keeper", "All-rounder")][:3]
    bowl_targets = sorted(
        (p for p in ordered if p["category"] in ("Bowler", "All-rounder")),
        key=lambda p: p["bowl_rating"], reverse=True)
    picked_bat = {id(p) for p in bat_targets}
    bowl_targets = [p for p in bowl_targets if id(p) not in picked_bat][:2]

    for i, p in enumerate(bat_targets):
        t = trait_dict(LPBOT_BAT_TRAITS[i % len(LPBOT_BAT_TRAITS)], LPBOT_TRAIT_LEVEL)
        if t:
            p["traits"] = [t]
    for i, p in enumerate(bowl_targets):
        t = trait_dict(LPBOT_BOWL_TRAITS[i % len(LPBOT_BOWL_TRAITS)], LPBOT_TRAIT_LEVEL)
        if t:
            p["traits"] = [t]


class _XIShim:
    """Minimal stand-in for a ``Player`` row so an engine-dict XI can be run
    through ``services.xi_rules.validate_roster_xi`` without touching the DB."""

    __slots__ = ("category", "bat_rating", "bowl_rating", "rating", "name",
                 "id", "parent_player_id")

    def __init__(self, d):
        self.category = d.get("category") or "Batsman"
        self.bat_rating = d.get("bat_rating") or 0
        self.bowl_rating = d.get("bowl_rating") or 0
        self.rating = d.get("rating") or 0
        self.name = d.get("name") or "Player"
        self.id = d.get("player_id") or d.get("roster_id")
        self.parent_player_id = None


def validate_engine_xi(xi):
    """Run an engine-dict XI through the personal-roster Playing XI rules.

    Returns ``(ok, errors)`` from ``xi_rules.validate_roster_xi``, which expects
    ``(entry, player)`` pairs — the shim supplies the attributes it reads.
    """
    pairs = [(None, _XIShim(p)) for p in xi]
    return xi_rules.validate_roster_xi(pairs)


def build_letsplay_bot_xi(session, user_id):
    """Assemble the /lpbot opponent's Playing XI from the master Player pool.

    Strength-matched to the user's fielded XI, legal under ``validate_xi``,
    picked highest-rating-first inside each role, and returned in batting order
    with inline traits attached. Returns a list of engine player dicts (the same
    shape ``handlers.letsplay._xi_to_engine`` produces for a human side).
    """
    from models import Player
    from services.sim_team import append_distinct_base_players, base_player_id

    target = int(round(max(50, min(99, _target_rating(session, user_id)
                                   + LPBOT_RATING_DELTA))))
    lo = max(40, target - LPBOT_RATING_BAND)
    hi = min(100, target + LPBOT_RATING_BAND)

    rows, seen = [], set()
    for category, need in LPBOT_SHAPE:
        rows.extend(_pick_category(session, category, need, seen, lo, hi))

    # The pool was thin in some role — top up with the best players left so the
    # bot always fields eleven, even if the composition drifts off the ideal shape.
    if len(rows) < 11:
        near = (not_career(session.query(Player))
                .filter(Player.is_active == True,  # noqa: E712
                        Player.rating.between(lo, hi))
                .order_by(Player.rating.desc()).limit(64).all())
        append_distinct_base_players(rows, near)
    if len(rows) < 11:
        full = (not_career(session.query(Player))
                .filter(Player.is_active == True)  # noqa: E712
                .order_by(Player.rating.desc()).limit(128).all())
        append_distinct_base_players(rows, full)
    if not rows:
        logger.error("lpbot: no active players available to build a bot XI")
        return []

    rows = rows[:11]
    ordered_rows = _batting_order(
        rows,
        category_of=lambda p: p.category or "Batsman",
        bat_rating_of=lambda p: p.bat_rating or 0,
        rating_of=lambda p: p.rating or 0)
    xi = [_lpbot_player_dict(p) for p in ordered_rows]
    _assign_lpbot_traits(xi)

    ok, errs = validate_engine_xi(xi)
    if not ok:
        # Not fatal: the bot still fields eleven distinct players, it just isn't
        # the textbook composition. Worth a log — it means the player pool can't
        # currently supply one of the roles.
        logger.warning("lpbot: auto XI is off-composition (%s)", "; ".join(errs))
    # Guard the de-dup invariant explicitly — two versions of one cricketer in
    # the same XI would double a player's traits and read as a bug on the card.
    if len({base_player_id(p) for p in ordered_rows}) != len(ordered_rows):
        logger.warning("lpbot: auto XI contains duplicate base players")
    return xi


def bot_xi_summary(xi):
    """``(team_ovr, top-3 star names)`` for an engine-dict XI — used on cards."""
    if not xi:
        return 0, ""
    total = sum(int(p.get("rating") or 0) for p in xi)
    stars = sorted(xi, key=lambda p: int(p.get("rating") or 0), reverse=True)[:3]
    return total, ", ".join(str(p.get("name") or "Player") for p in stars)


# ════════════════════════════════════════════════════════════════════
# /ciplbot — best legal XI out of a league team's squad
# ════════════════════════════════════════════════════════════════════

_cp_rating = xi_rules.challenge_numeric_rating
_cp_bat_rating = xi_rules.challenge_bat_rating


def _counts(xi):
    """``(keepers, bowling options, overseas)`` in a candidate XI."""
    return (
        sum(1 for p in xi if xi_rules.challenge_is_wicket_keeper(p)),
        sum(1 for p in xi if xi_rules.challenge_is_bowling_option(p)),
        sum(1 for p in xi if xi_rules.challenge_is_overseas(p)),
    )


def _fix_overseas(xi, bench, min_overseas, max_overseas):
    """Swap players in/out until the overseas count sits inside the league limit.

    Each swap keeps the wicket-keeper and five-bowling-option minimums intact,
    and always trades the cheapest way — dropping the lowest-rated player of the
    surplus nationality for the highest-rated available replacement.
    """
    is_overseas = xi_rules.challenge_is_overseas

    def _swap_ok(out_player, in_player):
        trial = [p for p in xi if p is not out_player] + [in_player]
        keepers, bowling, _ = _counts(trial)
        return keepers >= 1 and bowling >= 5

    for _ in range(len(xi)):
        _keepers, _bowling, overseas = _counts(xi)
        if min_overseas <= overseas <= max_overseas:
            break
        # Too many overseas → drop the weakest overseas for the best local.
        # Too few → the reverse.
        drop_overseas = overseas > max_overseas
        outs = sorted((p for p in xi if is_overseas(p) == drop_overseas),
                      key=_cp_rating)
        ins = sorted((p for p in bench if is_overseas(p) != drop_overseas),
                     key=_cp_rating, reverse=True)
        for out_player in outs:
            match = next((p for p in ins if _swap_ok(out_player, p)), None)
            if match is None:
                continue
            xi[xi.index(out_player)] = match
            bench.remove(match)
            bench.append(out_player)
            break
        else:
            # No legal swap exists (e.g. the squad simply has too few locals) —
            # stop rather than spin. The validation below will report it.
            break
    return xi, bench


def build_challenge_bot_xi(players, min_overseas=0, max_overseas=11):
    """Pick the bot's Playing XI out of one league team's squad.

    ``players`` is that team's ``ChallengePlayer`` rows (see
    ``handlers.challenge._query_team_players``). Selection is highest OVR first
    under the Challenge League rules — exactly 11, at least one wicket-keeper, at
    least five bowling options, and the league's overseas min/max. Returns the
    chosen rows in batting order (selection order IS batting order for
    ``handlers.cipl_play.build_xi_from_draft``), or ``[]`` for a squad too small
    to field an XI.
    """
    squad = list(players or [])
    if len(squad) < 11:
        logger.warning("ciplbot: squad has %s players — cannot field an XI", len(squad))
        return []

    by_rating = sorted(squad, key=_cp_rating, reverse=True)
    xi, remaining = [], list(by_rating)

    def _take(pred):
        for p in list(remaining):
            if pred(p):
                remaining.remove(p)
                xi.append(p)
                return True
        return False

    # Mandatory slots first, best available each time: one keeper, then five
    # bowling options. Everything after that is simply the best 11 by rating.
    _take(xi_rules.challenge_is_wicket_keeper)
    while sum(1 for p in xi if xi_rules.challenge_is_bowling_option(p)) < 5 and len(xi) < 11:
        if not _take(xi_rules.challenge_is_bowling_option):
            break
    while len(xi) < 11 and remaining:
        xi.append(remaining.pop(0))

    xi, remaining = _fix_overseas(xi, remaining, min_overseas, max_overseas)

    ok, error = xi_rules.validate_challenge_xi(xi, min_overseas, max_overseas)
    if not ok:
        logger.warning("ciplbot: generated XI failed validation (%s) — "
                       "falling back to the top 11 by rating", error)
        xi = by_rating[:11]

    return _batting_order(
        xi,
        category_of=xi_rules.challenge_player_category,
        bat_rating_of=_cp_bat_rating,
        rating_of=_cp_rating)


def pick_bot_team(team_names, taken, squad_size_of):
    """Choose the bot's league team: a random one the user didn't take that has
    a squad big enough to field an XI. Falls back to any free team when no squad
    reaches eleven, so the flow surfaces a real error instead of stalling."""
    free = [t for t in (team_names or []) if t not in set(taken or ())]
    if not free:
        return None
    playable = [t for t in free if squad_size_of(t) >= 11]
    return random.choice(playable or free)
