"""Team Chemistry — how well a Playing XI's countries and card editions fit.

Chemistry is scored out of 100 and splits into two independent halves:

  • COUNTRY CHEMISTRY (0-80) — national blocks inside the XI.
  • SPECIAL CHEMISTRY  (0-20) — the spread of special card editions (Icon,
    TOTY, Prime, Legend, Event) the XI is built from.

Like ``services.xi_rules`` this module imports nothing beyond the standard
library, so the maths can be reused by the match engine, the Mini App, the
bot's XI builder and the tests without dragging in Telegram or SQLAlchemy.

────────────────────────────────────────────────────────────────────────
Why country chemistry is a table and not N×(N−1)÷2
────────────────────────────────────────────────────────────────────────
The obvious design is "every same-country pair is a link, links are worth a
fixed number of points, cap the total at 80". It reads well and it is wrong,
because pair counting grows quadratically while the cap does not:

    7-4 → 27 links → 135 raw points, capped to 80. 55 points thrown away.

Once a shape overshoots the cap by that much, chemistry stops being a
decision. Worse, the cheapest route to a full 80 becomes *one huge block plus
filler*: under pair counting 7-1-1-1-1 scores a perfect 80, so the system that
was meant to rewards squad cohesion actually pays you to stack one nation and
play four unconnected cards. Every shape that reached 80 needed a block of 5+,
and no shape whose biggest block was 4 could ever get there.

So blocks are scored from a concave table instead. The marginal value of the
Nth countryman rises to a peak at the 4th and then falls away:

    N        1    2    3    4    5    6    7
    value    0    8   18   30   40   46   50
    step        +8  +10  +12  +10   +6   +4

Three consequences, all deliberate:

  • A "national core" of 4 is the sweet spot, which is how cricket squads
    actually read (a top order, a pace battery).
  • One country tops out at 50, i.e. 62% of the 80. No single nation can
    carry an XI, so every squad needs at least two genuine blocks.
  • 4-4-3 (78) now beats 7-3-1 (68). Diverse squads are competitive rather
    than merely legal, and 7-1-1-1-1 falls to 50.

Only 7-4, 6-5 and 5-5-1 reach a full 80, and seven different shapes land
within 5 points of it — so players pick a shape they like instead of copying
one meta build.

────────────────────────────────────────────────────────────────────────
The Icon rule
────────────────────────────────────────────────────────────────────────
A concave table still pays a lone countryman nothing, which would quietly
delete every legend from a smaller cricket nation — a single Brian Lara or
Richard Hadlee in an XI would be pure chemistry dead weight.

So: **an Icon counts as two players of its country when sizing the block**
(but still only one against the 7-per-country squad limit, and block value is
still read at N=7 maximum). Because the table is steep at the bottom and flat
at the top, that lifts a lone Lara from 0 to 8 and a 3-man West Indies core to
30, while doing nothing at all for a nation already sitting on 6 or 7 players.
The bonus lands exactly where the tail is and is worthless to the meta.
"""

# ── Country chemistry ───────────────────────────────────────────────
# Block value by effective block size. See the module docstring for the
# derivation; index 0/1 are present so lookups never need a guard.
COUNTRY_BLOCK_VALUE = {0: 0, 1: 0, 2: 8, 3: 18, 4: 30, 5: 40, 6: 46, 7: 50}

MAX_PLAYERS_PER_COUNTRY = 7      # hard squad rule, on real headcount
COUNTRY_CHEMISTRY_CAP = 80
XI_SIZE = 11

# An Icon counts double when sizing its national block.
ICON_BLOCK_WEIGHT = 2

# ── Special chemistry ───────────────────────────────────────────────
# Two components, deliberately weighted towards *variety* over *quantity* so
# the 20 points cannot simply be bought by stacking the rarest tier. An XI of
# eleven Icons scores 8; an XI carrying one card of each of the five types
# scores the full 20.
SPECIAL_TYPE_POINTS = 3          # per distinct special type in the XI
SPECIAL_TYPE_MAX_TYPES = 5       # → 15 from variety
SPECIAL_DEPTH_POINTS = 1         # per special card in the XI
SPECIAL_DEPTH_MAX_CARDS = 5      # → 5 from depth
SPECIAL_CHEMISTRY_CAP = 20

TOTAL_CHEMISTRY_CAP = COUNTRY_CHEMISTRY_CAP + SPECIAL_CHEMISTRY_CAP

# Canonical special types, most prestigious first. Order is also the matching
# precedence: "Ultimate Legend" contains both "ultimate" and "legend", and an
# "Icon Prime" is an Icon. Player.version is free text entered by admins, so
# matching is keyword based rather than an enum lookup.
SPECIAL_TYPES = ("icon", "toty", "prime", "legend", "event")

_TYPE_KEYWORDS = (
    ("icon", ("icon", "immortal", "hall of fame")),
    ("toty", ("toty", "team of the year", "team of the season", "totm",
              "team of the month")),
    ("prime", ("prime", "peak", "moments")),
    ("legend", ("legend", "ultimate", "goat")),
    # Everything else that is not a base card is an Event edition: World Cup,
    # IPL, Ashes, Gold, and any future named drop. Deliberately a catch-all so
    # a new edition is scored the day it ships without a config change.
    ("event", ()),
)

# Versions that are not special at all. Anything else non-empty is an Event.
_BASE_VERSIONS = ("", "base", "basic", "standard", "normal", "common", "none")

# Admins write the edition with and without a "card" suffix — the live pool
# uses "Base card", "Legend card", "Star Card" — so the noise word is stripped
# before matching. Without this every one of the 1228 base cards in
# data/players.json would fall through to the Event catch-all and score.
_VERSION_NOISE = (" card", " cards", " edition", " version")


def special_type(version):
    """The canonical special type for a ``Player.version`` string, or ``None``.

    ``None`` means an ordinary base card, which earns no special chemistry.
    """
    text = str(version or "").strip().lower()
    for noise in _VERSION_NOISE:
        if text.endswith(noise):
            text = text[: -len(noise)].strip()
            break
    if text in _BASE_VERSIONS:
        return None
    for name, keywords in _TYPE_KEYWORDS:
        if any(word in text for word in keywords):
            return name
    return "event"


def is_icon(player):
    """True when the card is an Icon, which counts double in its block."""
    return special_type(getattr(player, "version", None)) == "icon"


# Spelling variants that must fold onto one nation, because a split block is
# silently and badly wrong: it costs the player real chemistry for a data-entry
# difference they cannot see. The live pool carries both "Ireland Republic"
# (46 cards) and "Republic of Ireland" (3), which would otherwise score as two
# separate countries. Keys are compared lowercased and whitespace-collapsed.
COUNTRY_ALIASES = {
    "republic of ireland": "Ireland",
    "ireland republic": "Ireland",
    "eire": "Ireland",
    "uae": "United Arab Emirates",
    "usa": "United States",
    "united states of america": "United States",
    "windies": "West Indies",
    "chinese taipei": "Taiwan",
    "holland": "Netherlands",
    "png": "Papua New Guinea",
}


def country_of(player):
    """Canonical country label. Blank/missing data becomes ``"Unknown"``.

    Unknown is treated as a real country name, so a squad of cards with
    missing country data forms one block rather than silently scoring zero.
    The caller surfaces it in the breakdown so an admin can spot bad rows.
    """
    raw = " ".join(str(getattr(player, "country", "") or "").split())
    if not raw:
        return "Unknown"
    return COUNTRY_ALIASES.get(raw.lower(), raw)


def country_block_value(effective_size):
    """Chemistry from one national block, given its Icon-weighted size."""
    if effective_size < 0:
        effective_size = 0
    return COUNTRY_BLOCK_VALUE[min(effective_size, MAX_PLAYERS_PER_COUNTRY)]


def country_chemistry(players):
    """Country chemistry (0-80) plus a per-country breakdown.

    Returns ``(total, blocks)`` where ``blocks`` is a list of dicts sorted by
    value, each carrying ``country``, ``count`` (real headcount), ``icons``,
    ``effective`` (Icon-weighted, capped at 7) and ``value``.
    """
    counts = {}
    icons = {}
    for player in players:
        name = country_of(player)
        counts[name] = counts.get(name, 0) + 1
        if is_icon(player):
            icons[name] = icons.get(name, 0) + 1

    blocks = []
    for name, count in counts.items():
        icon_count = icons.get(name, 0)
        # Icons count twice for sizing, but the table is never read past 7 —
        # that is what stops Icons inflating an already-maxed block.
        effective = min(count + icon_count * (ICON_BLOCK_WEIGHT - 1),
                        MAX_PLAYERS_PER_COUNTRY)
        blocks.append({
            "country": name,
            "count": count,
            "icons": icon_count,
            "effective": effective,
            "value": country_block_value(effective),
        })

    blocks.sort(key=lambda b: (-b["value"], -b["count"], b["country"]))
    total = min(sum(b["value"] for b in blocks), COUNTRY_CHEMISTRY_CAP)
    return total, blocks


def special_chemistry(players):
    """Special-card chemistry (0-20) plus its breakdown.

    Returns ``(total, detail)``. ``detail`` carries ``types`` (the distinct
    special types present, in prestige order), ``cards`` (how many special
    cards are in the XI), and the two component scores.
    """
    present = []
    cards = 0
    for player in players:
        kind = special_type(getattr(player, "version", None))
        if kind is None:
            continue
        cards += 1
        if kind not in present:
            present.append(kind)

    present.sort(key=SPECIAL_TYPES.index)
    variety = min(len(present), SPECIAL_TYPE_MAX_TYPES) * SPECIAL_TYPE_POINTS
    depth = min(cards, SPECIAL_DEPTH_MAX_CARDS) * SPECIAL_DEPTH_POINTS
    total = min(variety + depth, SPECIAL_CHEMISTRY_CAP)
    return total, {
        "types": present,
        "cards": cards,
        "variety_points": variety,
        "depth_points": depth,
    }


def validate_country_rule(players):
    """Check the one hard squad rule: max 7 players from any one country.

    Returns ``(valid, errors)`` to match ``services.xi_rules``. Note there is
    deliberately no "at least 2 countries" rule — with an 11-man XI and a
    7-per-country ceiling, two countries are already forced by arithmetic, so
    such a rule could never fire and would only add UI noise.
    """
    counts = {}
    for player in players:
        name = country_of(player)
        counts[name] = counts.get(name, 0) + 1

    errors = [
        f"Max {MAX_PLAYERS_PER_COUNTRY} players from one country "
        f"({name}: {count})"
        for name, count in sorted(counts.items())
        if count > MAX_PLAYERS_PER_COUNTRY
    ]
    return len(errors) == 0, errors


def calculate_chemistry(players):
    """Full chemistry report for a Playing XI.

    ``players`` is any iterable of objects exposing ``country`` and
    ``version`` — a ``Player`` row, a ChallengePlayer shim, or a test stub.

    A short XI (a forfeit, an incomplete lineup) is scored on the players it
    actually has and then pro-rated by ``len(players) / 11``, so an XI cannot
    farm a high score by fielding only its best-connected cards. A full or
    over-long XI is never pro-rated.
    """
    players = list(players)
    country_total, blocks = country_chemistry(players)
    special_total, special_detail = special_chemistry(players)

    raw_total = country_total + special_total
    shortfall = max(0, XI_SIZE - len(players))
    if shortfall:
        # int() floors, so a short XI is never rounded up into a full score.
        country_total = int(country_total * len(players) / XI_SIZE)
        special_total = int(special_total * len(players) / XI_SIZE)

    valid, errors = validate_country_rule(players)
    return {
        "total": country_total + special_total,
        "country": country_total,
        "special": special_total,
        "blocks": blocks,
        "special_detail": special_detail,
        "shape": "-".join(str(b["count"]) for b in
                          sorted(blocks, key=lambda b: -b["count"])),
        "players_counted": len(players),
        "prorated": bool(shortfall),
        "raw_total": raw_total,
        "valid": valid,
        "errors": errors,
    }


# ── Match effect ────────────────────────────────────────────────────
# Chemistry is a tie-breaker, never a substitute for card quality. It maps to
# a small bonus band only: a 100-chemistry XI plays ~3% above its raw ratings
# and a 0-chemistry XI plays exactly at them. Nothing is ever *penalised*
# below par, so a new player fielding whatever they pulled is not taxed for
# it — they are simply not yet earning the bonus.
CHEMISTRY_MAX_BONUS_PCT = 3.0


def chemistry_bonus_pct(total):
    """Performance bonus (0.0-3.0%) earned by a chemistry score of ``total``."""
    clamped = max(0, min(int(total), TOTAL_CHEMISTRY_CAP))
    return round(CHEMISTRY_MAX_BONUS_PCT * clamped / TOTAL_CHEMISTRY_CAP, 3)
