"""Challenge Draft (/cdraft) — the draft itself, as plain functions.

Two players, eleven slots, no squads and no admin. Each slot deals **two cards
that share a role and are within a whisker of each other on OVR**; the captain
on the clock takes one and the other goes to their opponent. After eleven slots
both captains hold eleven players, and the match runs as a normal Challenge
League fixture.

Why the slots carry a fixed role
--------------------------------
"The Playing XI rules apply in the draft" is enforced here by construction
rather than checked afterwards. Because both cards in a pair share that slot's
role, *both* squads finish on the same shape — 4 Batsmen, 1 Wicket Keeper,
2 All-rounders, 4 Bowlers — which is legal under both of the game's rulebooks:

  • ``xi_rules.validate_challenge_xi`` — 11 players, ≥1 keeper, ≥5 bowling
    options (this gives 6).
  • ``xi_rules.validate_roster_xi`` — 3-5 Batsmen, 3-5 Bowlers, 1-2 Keepers,
    1-3 All-rounders, and at two all-rounders the "3rd all-rounder must bowl
    worse than every pure Bowler" clause never fires.

It is the same 4/1/2/4 shape, for the same reasons, that the AI opponent's XI
builder uses (``services.bot_xi_builder.LPBOT_SHAPE``). A slot's role is
therefore never relaxed to fill it: if the catalogue cannot supply a pair for
some role, the draft refuses to start (``CdraftPoolError``) rather than deal a
squad that cannot field a legal XI.

The roles are *interleaved* down the rating ladder rather than blocked together.
A blocked template (four batsmen first, four bowlers last) would hand every
bowler in the game a rating ~10 OVR below every batsman, every time, for both
sides. Interleaving keeps the marquee-first feel of a real draft without
building that bias into the mode.

No Telegram, no SQLAlchemy, no session: the pool is read through
``services.player_cache`` (which already excludes Career cards — see
``player_cache._refresh``), so this module is a pure function of its inputs and
can be unit-tested on its own, exactly like ``services.xi_rules``.
"""

import json
import logging
import os
import random

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# The rulebook of a draft
# ════════════════════════════════════════════════════════════════════

ROLE_BATSMAN = "Batsman"
ROLE_KEEPER = "Wicket Keeper"
ROLE_ALLROUNDER = "All-rounder"
ROLE_BOWLER = "Bowler"

# 4 Batsmen + 1 Keeper + 2 All-rounders + 4 Bowlers, interleaved so no role
# sits entirely at one end of the rating ladder (see the module docstring).
SLOT_ROLES = (
    ROLE_BATSMAN,      # 1
    ROLE_BOWLER,       # 2
    ROLE_BATSMAN,      # 3
    ROLE_ALLROUNDER,   # 4
    ROLE_BOWLER,       # 5
    ROLE_BATSMAN,      # 6
    ROLE_KEEPER,       # 7
    ROLE_BOWLER,       # 8
    ROLE_BATSMAN,      # 9
    ROLE_ALLROUNDER,   # 10
    ROLE_BOWLER,       # 11
)

SLOT_COUNT = len(SLOT_ROLES)

# Snake order. Strict alternation would give the host every odd slot — six
# picks, with the advantage stacked on the best cards at the top of the ladder.
# The snake keeps the split at 6-5 but spreads it out, and hands the guest the
# last pick as the price of the host picking first.
PICK_ORDER = (
    "host", "target", "target", "host", "host", "target",
    "target", "host", "host", "target", "target",
)

SIDES = ("host", "target")


def _env_int(name, default, lo, hi):
    """An int from the environment, clamped. A junk value falls back to the
    default rather than taking the whole mode down at import time."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("%s is not an integer; using %s", name, default)
        return default
    return max(lo, min(hi, value))


# The rating BAND a draft is dealt from. Every slot draws its own target at
# random from inside it, so no slot is pinned to either end and no two drafts
# have the same shape. Both ends are configurable so a group can run a draft at
# whatever strength its catalogue actually holds.
RATING_TOP = _env_int("CDRAFT_RATING_TOP", 88, 50, 100)
RATING_BOTTOM = _env_int("CDRAFT_RATING_BOTTOM", 78, 40, 100)

# How far apart the TWO CARDS IN ONE PAIR may be on OVR. At 1, a pair is either
# two equal cards or an 84 next to an 85 — close enough that neither captain can
# claim the slot was handed to them.
PAIR_SPREAD = _env_int("CDRAFT_PAIR_SPREAD", 1, 0, 5)

# Seconds a captain has to make one pick before the bot picks for them.
PICK_SECONDS = _env_int("CDRAFT_PICK_SECONDS", 60, 15, 600)

# Consecutive auto-picks by one side that mean they have walked away.
MAX_AUTO_STREAK = _env_int("CDRAFT_MAX_AUTO_PICKS", 3, 1, 11)

# How wide the search for a pair may grow before giving up. The ladder spans ten
# OVR by default, so this reaches the whole usable catalogue.
_MAX_WIDEN = 50


class CdraftPoolError(Exception):
    """The card catalogue cannot supply a legal pair for some slot.

    Raised rather than relaxing the slot's role, because a squad built without
    (say) a keeper could not field a legal XI — the one thing this mode
    guarantees.
    """


def slot_ratings(top=None, bottom=None, count=SLOT_COUNT, rng=None):
    """A target OVR for each slot, drawn at random from ``[bottom, top]``.

    The admin's two numbers bound a *band*, not the ends of a staircase: each
    slot draws independently, so slot 1 is not pinned to the ceiling, slot 11 is
    not pinned to the floor, and no two drafts come out the same shape. (This
    used to step evenly from top to bottom, which made every draft identical and
    made the highest rating in the band a certainty rather than a chance.)

    Fairness is untouched by this — the two cards in a slot are still the same
    role within the pair spread of each other, so whatever a slot draws, it
    draws for both captains at once.
    """
    top = RATING_TOP if top is None else top
    bottom = RATING_BOTTOM if bottom is None else bottom
    if bottom > top:
        bottom, top = top, bottom
    rng = rng or random
    return [rng.randint(bottom, top) for _ in range(max(0, count))]


# ════════════════════════════════════════════════════════════════════
# The card shim
# ════════════════════════════════════════════════════════════════════

class DraftCard:
    """A drafted catalogue card, wearing a ``ChallengePlayer``'s clothes.

    Everything downstream of the draft — the Playing XI picker, the XI
    rulebook in ``services.xi_rules``, ``services.cipl_match.cp_to_player_dict``
    — reads a squad member's ratings, role and handedness out of a
    ``details_json`` blob. Presenting the same surface here means the whole
    Challenge League flow runs on a drafted squad without a single throwaway
    database row.

    ``id`` is the real ``Player.id``, so the engine's ``roster_id`` and
    ``player_id`` both land on the master card and Player-of-the-Match and
    global player stats are recorded against it like any other match.
    """

    __slots__ = ("id", "name", "country", "details_json", "is_overseas",
                 "source_player_id", "sort_order")

    def __init__(self, data):
        self.id = int(data["id"])
        self.name = str(data.get("name") or "Player")
        self.country = data.get("country") or ""
        self.source_player_id = self.id
        self.sort_order = 0
        # No league means no home country, so nobody is overseas and the
        # overseas min/max the XI rules apply are left wide open (0/11).
        self.is_overseas = False
        self.details_json = json.dumps({
            "name": self.name,
            "category": data.get("category") or ROLE_BATSMAN,
            "rating": int(data.get("rating") or 0),
            "bat_rating": int(data.get("bat_rating") or 0),
            "bowl_rating": int(data.get("bowl_rating") or 0),
            "bat_hand": data.get("bat_hand") or "Right",
            "bowl_hand": data.get("bowl_hand") or "Right",
            "bowl_style": data.get("bowl_style") or "",
            "country": self.country,
            "source_player_id": self.id,
        })

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"<DraftCard {self.id} {self.name}>"


def pool_card(data):
    """The subset of a ``player_cache`` dict a draft needs, as a plain dict.

    Kept plain (and JSON-safe) so a whole draft can sit in ``bot_data`` and be
    logged or diffed; ``DraftCard`` shims are built from it on demand.
    """
    return {
        "id": int(data["id"]),
        "name": str(data.get("name") or "Player"),
        "country": data.get("country") or "",
        "category": data.get("category") or ROLE_BATSMAN,
        "rating": int(data.get("rating") or 0),
        "bat_rating": int(data.get("bat_rating") or 0),
        "bowl_rating": int(data.get("bowl_rating") or 0),
        "bat_hand": data.get("bat_hand") or "Right",
        "bowl_hand": data.get("bowl_hand") or "Right",
        "bowl_style": data.get("bowl_style") or "",
        # Carried so a drafted card can name its edition in the slot card. The
        # allowed-version rule is applied to the raw pool entry, before this.
        "version": data.get("version") or "",
    }


# ════════════════════════════════════════════════════════════════════
# Which versions may be drafted
# ════════════════════════════════════════════════════════════════════

# The labels an admin ticks are free text out of ``Player.version``, except for
# this one: "Base" is a *state*, not a label. A base card may carry the literal
# string, an empty one, or none at all — what actually marks it is having no
# parent card. Matching all three is what the admin panel's own Players filter
# does (see the version filter in admin.py), so the tick box means the same
# thing in both places.
BASE_LABEL = "Base"
_BASE_ALIASES = ("", "base")


def parse_allowed_versions(raw):
    """The stored ``cdraft_versions_json`` as a list of labels, or ``None``.

    ``None`` means *every* version is allowed, which is what an unset, empty or
    unreadable setting has to mean: the alternative — a draft that can never
    start — is a worse answer to a malformed row than simply not filtering.
    Accepts a JSON array or a comma-separated string, so a value typed straight
    into the database by hand still works.
    """
    if raw is None:
        return None
    if isinstance(raw, (list, tuple, set)):
        labels = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return None
        if text[0] in "[{":
            # Meant to be JSON. If it will not parse, fall back to "everything
            # allowed" rather than to a list of nonsense labels — a tick list
            # that matches no card is a mode that can never start, which is a
            # far worse answer to a corrupt value than not filtering.
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                logger.warning("cdraft: unreadable version list %r; "
                               "allowing every version", text)
                return None
        else:
            parsed = text.split(",")
        labels = parsed if isinstance(parsed, list) else [parsed]
    cleaned = []
    seen = set()
    for label in labels:
        name = str(label or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        cleaned.append(name)
    return cleaned or None


def dump_allowed_versions(labels):
    """Labels back to a ``cdraft_versions_json`` value. ``None`` = all allowed."""
    cleaned = parse_allowed_versions(labels)
    return json.dumps(cleaned) if cleaned else None


def is_base_card(card):
    """True when this catalogue entry is somebody's base card."""
    if card.get("parent_player_id"):
        return False
    return str(card.get("version") or "").strip().casefold() in _BASE_ALIASES


def version_allowed(card, versions):
    """Is this catalogue entry one of the ticked versions?

    ``versions is None`` allows everything. Otherwise a card matches on an exact
    (case-insensitive) label, or on being a base card when ``Base`` is ticked.
    """
    if not versions:
        return True
    wanted = {str(v).strip().casefold() for v in versions}
    if is_base_card(card):
        return BASE_LABEL.casefold() in wanted
    return str(card.get("version") or "").strip().casefold() in wanted


def filter_pool(pool, versions):
    """The catalogue entries a draft with these settings may deal."""
    if not versions:
        return list(pool)
    return [card for card in pool if version_allowed(card, versions)]


# How many DIFFERENT cricketers each role needs before a draft can be dealt:
# two per slot of that role, because both cards in a pair share the slot's role
# and no cricketer may appear twice in one draft.
ROLE_REQUIREMENTS = {
    role: SLOT_ROLES.count(role) * 2
    for role in (ROLE_BATSMAN, ROLE_KEEPER, ROLE_ALLROUNDER, ROLE_BOWLER)
}


def pool_feasibility(pool=None, versions=None):
    """``{role: (have, need)}`` — can these settings actually deal a draft?

    ``have`` counts DISTINCT cricketers by name, because that is what the deal
    de-duplicates on: five cards of one player are one usable pick, not five.
    Used by the admin page to warn at save time and by /cdraftset to print the
    pool, so an admin finds out before a group does.
    """
    if pool is None:
        from services import player_cache
        pool = player_cache.get_all_active()
    names = {role: set() for role in ROLE_REQUIREMENTS}
    for entry in filter_pool(pool, versions):
        try:
            rating = int(entry.get("rating") or 0)
        except (TypeError, ValueError):
            continue
        if rating <= 0:
            continue
        role = normalise_role(entry.get("category"))
        names[role].add(str(entry.get("name") or "").casefold())
    return {role: (len(names[role]), need)
            for role, need in ROLE_REQUIREMENTS.items()}


def feasibility_shortfalls(feasibility):
    """The roles that cannot fill their slots, worst first. Empty when fine."""
    short = [(role, have, need) for role, (have, need) in feasibility.items()
             if have < need]
    short.sort(key=lambda row: row[1] - row[2])
    return short


# ════════════════════════════════════════════════════════════════════
# Admin settings
# ════════════════════════════════════════════════════════════════════

# Bounds the stored settings are clamped to. Wide on purpose — they exist to
# stop a typo (a floor of 5, a ceiling of 900) producing a draft nobody can
# explain, not to second-guess a deliberate choice.
RATING_FLOOR_LIMIT = 40
RATING_CEILING_LIMIT = 100

# The pair spread has a hard ceiling rather than a wide one: it is the single
# mechanic that makes the two squads provably fair, and at a large value a slot
# genuinely can hand one captain the better card.
SPREAD_LIMIT_LOW = 0
SPREAD_LIMIT_HIGH = 5


def load_settings(config=None):
    """The admin's pool settings:
    ``{rating_min, rating_max, pair_spread, versions}``.

    Reads the single-row ``GameConfig`` the website and /cdraftset both write,
    and falls back to the environment defaults for anything unset — so an
    install that never opens the admin panel behaves exactly as it did before
    the settings existed.

    Repairs rather than rejects: a min above a max is swapped, out-of-range ends
    are clamped. A draft refusing to start because a number was typed backwards
    would be a worse failure than quietly dealing the range that was meant.
    """
    if config is None:
        try:
            from services.config_service import get_config
            config = get_config()
        except Exception:
            logger.exception("cdraft: could not read the pool settings; "
                             "falling back to the defaults")
            config = {}

    def _number(key, fallback):
        try:
            value = config.get(key)
        except AttributeError:
            value = None
        if value in (None, ""):
            return fallback
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    low = _number("cdraft_rating_min", RATING_BOTTOM)
    high = _number("cdraft_rating_max", RATING_TOP)
    if low > high:
        low, high = high, low
    low = max(RATING_FLOOR_LIMIT, min(RATING_CEILING_LIMIT, low))
    high = max(RATING_FLOOR_LIMIT, min(RATING_CEILING_LIMIT, high))
    spread = max(SPREAD_LIMIT_LOW, min(SPREAD_LIMIT_HIGH,
                                       _number("cdraft_pair_spread", PAIR_SPREAD)))
    try:
        versions = parse_allowed_versions(config.get("cdraft_versions_json"))
    except AttributeError:
        versions = None
    return {"rating_min": low, "rating_max": high, "pair_spread": spread,
            "versions": versions}


def settings_summary(settings):
    """One line naming the pool, for the lobby card and /cdraftset."""
    versions = settings.get("versions")
    label = ", ".join(versions) if versions else "all versions"
    return (f"{settings.get('rating_min', RATING_BOTTOM)}–"
            f"{settings.get('rating_max', RATING_TOP)} OVR · {label}")


# ════════════════════════════════════════════════════════════════════
# Dealing the slots
# ════════════════════════════════════════════════════════════════════

def normalise_role(value):
    """Fold a catalogue ``category`` onto one of the four draft roles."""
    low = str(value or "").strip().lower().replace("-", " ")
    if low in ("wk", "keeper", "wicketkeeper", "wicket keeper",
               "wicket keeper batter", "wicket keeper batsman"):
        return ROLE_KEEPER
    if low in ("all rounder", "allrounder", "all round", "alr"):
        return ROLE_ALLROUNDER
    if low in ("bowler", "bowl"):
        return ROLE_BOWLER
    return ROLE_BATSMAN


def _pool_by_role(pool=None, versions=None):
    """``{role: [card dict, …]}`` for the catalogue a draft may deal from.

    The version rule is applied here, on the raw pool entries, so no disallowed
    card ever reaches the dealing code at all.
    """
    if pool is None:
        from services import player_cache
        pool = player_cache.get_all_active()
    by_role = {ROLE_BATSMAN: [], ROLE_KEEPER: [],
               ROLE_ALLROUNDER: [], ROLE_BOWLER: []}
    for entry in filter_pool(pool, versions):
        try:
            card = pool_card(entry)
        except (KeyError, TypeError, ValueError):
            continue
        if card["rating"] <= 0:
            continue
        by_role[normalise_role(entry.get("category"))].append(card)
    return by_role


def _choose_pair(candidates, target, used_names, rng, spread=PAIR_SPREAD):
    """Two same-role cards within ``spread`` OVR of each other, nearest ``target``.

    Returns ``None`` when this pool cannot produce a pair. Duplicates are ruled
    out **by name**, not by id: the catalogue carries several versions of the
    same cricketer, and dealing two of them into one draft would put the same
    player on the field twice.
    """
    fresh = [c for c in candidates if c["name"].casefold() not in used_names]
    if len(fresh) < 2:
        return None
    # Nearest the target first, ties broken at random so the same slot doesn't
    # deal the same two cards in every draft.
    rng.shuffle(fresh)
    fresh.sort(key=lambda c: abs(c["rating"] - target))
    for i, first in enumerate(fresh):
        for second in fresh[i + 1:]:
            if first["name"].casefold() == second["name"].casefold():
                continue
            if abs(first["rating"] - second["rating"]) <= spread:
                pair = [first, second]
                rng.shuffle(pair)
                return pair
    return None


def _search_windows(target, floor, ceiling):
    """Rating windows to try for one slot, best first.

    The admin's floor and ceiling bound a *band*, not merely slots 11 and 1, so
    a slot whose exact target is empty looks elsewhere **inside that band**
    first — widening a point at a time, clamped at both ends — and only leaves
    the band once every rating in it has been tried. A thin rating therefore
    borrows from the rest of the pool the admin allowed, and breaks their range
    only when the whole of it has nothing for this role.
    """
    yield target, target
    for widen in range(1, max(ceiling - target, target - floor) + 1):
        yield max(target - widen, floor), min(target + widen, ceiling)
    # The allowed band is exhausted. Rather than fail, reach outside it —
    # nearest rating first. This is the "relax the rating, never the version"
    # rule: the tick list above still holds, only the numbers give.
    for widen in range(1, _MAX_WIDEN + 1):
        yield target - widen, target + widen


def build_slots(pool=None, seed=None, top=None, bottom=None, spread=PAIR_SPREAD,
                versions=None):
    """Deal the eleven slots.

    Each slot is ``{"role", "target_rating", "cards": [a, b]}`` where both cards
    carry that slot's role and sit within ``spread`` OVR of each other. No
    cricketer appears twice across the whole draft.

    ``versions`` is the admin's allowed-version tick list (``None`` = all).
    Unlike the rating band it is **absolute**: when a rating is thin the search
    widens, but it never reaches for a version that was not ticked. A role the
    allowed pool genuinely cannot supply raises ``CdraftPoolError`` rather than
    being swapped for another role, because that is what would let a squad end up
    without a keeper or short of bowling options.
    """
    rng = random.Random(seed)
    by_role = _pool_by_role(pool, versions)
    # The band comes from the SETTINGS, not from the targets drawn out of it:
    # the draws are random and unsorted, so the lowest and highest of them are
    # not the band's ends and using them would quietly shrink the search.
    floor = RATING_BOTTOM if bottom is None else bottom
    ceiling = RATING_TOP if top is None else top
    if floor > ceiling:
        floor, ceiling = ceiling, floor
    targets = slot_ratings(ceiling, floor, rng=rng)
    used_names = set()
    slots = []

    for index, role in enumerate(SLOT_ROLES):
        target = targets[index]
        candidates = by_role.get(role) or []
        pair = None
        for lo, hi in _search_windows(target, floor, ceiling):
            window = [c for c in candidates if lo <= c["rating"] <= hi]
            pair = _choose_pair(window, target, used_names, rng, spread)
            if pair:
                break
        if not pair:
            allowed = (", ".join(versions) if versions else "all versions")
            raise CdraftPoolError(
                f"The card pool has no pair of {role}s for slot {index + 1} "
                f"(needs two different players within {spread} OVR of each "
                f"other). Allowed versions: {allowed}."
            )
        for card in pair:
            used_names.add(card["name"].casefold())
        slots.append({
            "role": role,
            "target_rating": target,
            "cards": pair,
            "picked_id": None,
            "auto": False,
        })
    return slots


# ════════════════════════════════════════════════════════════════════
# Draft state
# ════════════════════════════════════════════════════════════════════

def new_state(slots):
    """A fresh draft over ``slots``. Plain data — safe to park in ``bot_data``."""
    return {
        "slots": slots,
        "index": 0,
        "order": list(PICK_ORDER),
        "squads": {"host": [], "target": []},
        "auto_streak": {"host": 0, "target": 0},
    }


def current_side(state):
    """Whose pick it is, or ``None`` once every slot is filled."""
    index = state.get("index", 0)
    order = state.get("order") or list(PICK_ORDER)
    if index >= len(order):
        return None
    return order[index]


def is_complete(state):
    return state.get("index", 0) >= len(state.get("slots") or ())


def current_slot(state):
    """The slot on the clock, or ``None`` when the draft is done."""
    if is_complete(state):
        return None
    return state["slots"][state["index"]]


def other_side(side):
    return "target" if side == "host" else "host"


def picks_remaining(state, side):
    """How many more picks ``side`` gets to make."""
    order = state.get("order") or list(PICK_ORDER)
    return sum(1 for s in order[state.get("index", 0):] if s == side)


def auto_pick_id(slot):
    """The card a walked-away captain is given: the stronger of the two.

    Ties on OVR — the common case, since a pair is dealt within a point of
    itself — are broken on the rating the slot's role is actually for, so an
    auto-picked bowler is the better bowler rather than the better batter.
    """
    role = slot.get("role")
    key = ("bowl_rating" if role in (ROLE_BOWLER, ROLE_ALLROUNDER)
           else "bat_rating")
    best = max(slot["cards"],
               key=lambda c: (c["rating"], c[key], -c["id"]))
    return int(best["id"])


def apply_pick(state, slot_index, side, chosen_id, auto=False):
    """Record one pick: ``chosen_id`` to ``side``, the other card to the opponent.

    Returns ``(chosen, other)`` as card dicts. Raises ``ValueError`` when the
    pick does not belong to the slot on the clock — the callback layer relies on
    this rather than trusting a button that may be several slots stale.
    """
    if is_complete(state):
        raise ValueError("This draft is already complete.")
    if int(slot_index) != int(state.get("index", 0)):
        raise ValueError("That slot has already been picked.")
    if side != current_side(state):
        raise ValueError("It is not your pick.")

    slot = state["slots"][int(slot_index)]
    chosen_id = int(chosen_id)
    chosen = next((c for c in slot["cards"] if int(c["id"]) == chosen_id), None)
    if chosen is None:
        raise ValueError("That player is not in this slot.")
    other = next(c for c in slot["cards"] if int(c["id"]) != chosen_id)

    slot["picked_id"] = chosen_id
    slot["auto"] = bool(auto)
    state["squads"][side].append(chosen)
    state["squads"][other_side(side)].append(other)

    streaks = state.setdefault("auto_streak", {"host": 0, "target": 0})
    streaks[side] = streaks.get(side, 0) + 1 if auto else 0
    state["index"] = int(state.get("index", 0)) + 1
    return chosen, other


def walked_away(state):
    """The side that has let ``MAX_AUTO_STREAK`` picks lapse in a row, if any."""
    for side in SIDES:
        if (state.get("auto_streak") or {}).get(side, 0) >= MAX_AUTO_STREAK:
            return side
    return None


# ════════════════════════════════════════════════════════════════════
# Reading a finished draft
# ════════════════════════════════════════════════════════════════════

def squad_cards(state, side):
    """A side's squad as ``DraftCard`` shims, in draft order.

    This is what ``handlers.challenge._query_team_players`` hands to the Playing
    XI picker in place of a league team's ``ChallengePlayer`` rows.
    """
    return [DraftCard(card) for card in (state.get("squads") or {}).get(side, [])]


def squad_in_batting_order(state, side):
    """A side's player ids in a sensible batting order.

    Keepers and batsmen first, then the all-rounders, then the specialist
    bowlers, each block by batting rating — the same ordering the AI opponent's
    XI uses, so a drafted line-up reads like any other.
    """
    # The same block order the AI opponent's line-up is built from; imported
    # rather than restated so both modes stay in step if it is ever retuned.
    from services.bot_xi_builder import _ORDER_BLOCK

    cards = (state.get("squads") or {}).get(side, [])
    ordered = sorted(
        cards,
        key=lambda c: (_ORDER_BLOCK.get(normalise_role(c.get("category")), 1),
                       -(c.get("bat_rating") or 0),
                       -(c.get("rating") or 0),
                       str(c.get("name") or "")))
    return [int(c["id"]) for c in ordered]


def squad_shape(state, side):
    """``{role: count}`` for a side — what the XI rules are checked against."""
    shape = {ROLE_BATSMAN: 0, ROLE_KEEPER: 0, ROLE_ALLROUNDER: 0, ROLE_BOWLER: 0}
    for card in (state.get("squads") or {}).get(side, []):
        shape[normalise_role(card.get("category"))] += 1
    return shape


def squad_strength(state, side):
    """A side's total OVR — the one-line answer to 'who drafted better?'."""
    return sum(int(c.get("rating") or 0)
               for c in (state.get("squads") or {}).get(side, []))
