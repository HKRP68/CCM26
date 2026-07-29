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


# ════════════════════════════════════════════════════════════════════
# Role chemistry — the /cmuchem breakdown
# ════════════════════════════════════════════════════════════════════
# /cmuchem re-cuts the same 80/20 score by role, so a manager can see *which
# part* of the XI is badly connected rather than one opaque number. It adds no
# new scoring rules: every figure below is derived from the country blocks and
# special-card variety computed above.
#
# Each role line reads:  comp1/20 + comp2/20 = +bonus/max
#
#   comp1 — how well that role's players sit inside the XI's national blocks.
#   comp2 — the Playing XI Bonus (special-card variety), shared by every role
#           because a varied card pool lifts the whole side, not one unit.
#   bonus — (comp1 + comp2) / 40 × the role's maximum.
#
# All-rounders are halved on comp1 and capped at 3 rather than 4: they already
# collect the batting and bowling benefit, so paying them a full third share
# would count the same connection twice.
#
# Two ceilings here have to be *reachable*, or this card repeats the mistake the
# 80/20 rework exists to fix — a maximum nobody can hit reads as a broken stat:
#
#   • The all-rounder line divides by its own halved ceiling (10 + 20 = 30),
#     not the full 40. Dividing by 40 caps ALR at 2.25 of 3, so +3/3 could
#     never appear however good the squad was.
#   • Role connection is measured against a *three*-man core, not the four-man
#     sweet spot. Full diversity wants four countries, and 4 countries × 4
#     players is 16 — more than an XI holds — so a four-block target would make
#     role_total 15 and diversity 10 mutually exclusive. Three-man cores fit
#     (3-3-3-2), so both ceilings can be reached by the same squad. The 80-point
#     country score still peaks at four; this axis only asks "is this player
#     connected to anyone?", and a three-man core answers yes.

ROLE_ORDER = ("Batsman", "Bowler", "Wicket Keeper", "All-rounder")
ROLE_LABEL = {"Batsman": "BAT", "Bowler": "BOWL",
              "Wicket Keeper": "WK", "All-rounder": "ALR"}
ROLE_EMOJI = {"Batsman": "🟥", "Bowler": "🟦",
              "Wicket Keeper": "🟦", "All-rounder": "🟧"}
ROLE_MAX_BONUS = {"Batsman": 4, "Bowler": 4, "Wicket Keeper": 4,
                  "All-rounder": 3}
# Roles whose country component is halved to avoid double-counting.
ROLE_HALVED = ("All-rounder",)

ROLE_COMPONENT_MAX = 20

# A player counts as fully connected once their country block reaches three.
# See the note above on why this is the 3-block and not the 4-block. Measuring
# against the 7-block (50) would be worse still — it would quietly re-introduce
# pressure to stack, which is what the block curve exists to remove.
ROLE_CONNECTION_TARGET = COUNTRY_BLOCK_VALUE[3]     # 18

# Squad-wide bonuses, both scored against a target of four.
DIVERSITY_TARGET_COUNTRIES = 4
DIVERSITY_MAX = 10
VARIETY_TARGET_TYPES = 4
VARIETY_MAX = 10

# 4 + 4 + 4 + 3 roles, + diversity, + variety, + the Playing XI Bonus.
CMUCHEM_TOTAL_MAX = (sum(ROLE_MAX_BONUS.values())
                     + DIVERSITY_MAX + VARIETY_MAX + SPECIAL_CHEMISTRY_CAP)

_CATEGORY_ALIASES = {
    "wk": "Wicket Keeper", "keeper": "Wicket Keeper",
    "wicketkeeper": "Wicket Keeper", "wicket keeper": "Wicket Keeper",
    "wicket keeper batter": "Wicket Keeper",
    "wicket keeper batsman": "Wicket Keeper",
    "all rounder": "All-rounder", "allrounder": "All-rounder",
    "all-rounder": "All-rounder", "alr": "All-rounder",
    "bowler": "Bowler", "bowl": "Bowler",
    "batsman": "Batsman", "batter": "Batsman", "bat": "Batsman",
}


def _round_half_up(value):
    """Round halves upward. ``round()`` is banker's rounding, which would show
    a 0.5 role bonus as +0 and read like a bug on the card."""
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def role_of(player):
    """Normalised role for a player card.

    Unrecognised roles fall back to Batsman, matching how
    ``services.xi_rules.validate_roster_xi`` buckets an unknown category.
    """
    raw = str(getattr(player, "category", "") or "").strip()
    if raw in ROLE_MAX_BONUS:
        return raw
    return _CATEGORY_ALIASES.get(raw.lower().replace("-", " "), "Batsman")


def country_diversity(players):
    """Squad-wide country spread, 0-10, full marks at four countries."""
    countries = {country_of(p) for p in players}
    score = min(DIVERSITY_MAX,
                _round_half_up(len(countries) * DIVERSITY_MAX
                               / DIVERSITY_TARGET_COUNTRIES))
    return score, len(countries)


def card_variety(players):
    """Squad-wide special-type spread, 0-10, full marks at four types."""
    types = {special_type(getattr(p, "version", None)) for p in players}
    types.discard(None)
    score = min(VARIETY_MAX,
                _round_half_up(len(types) * VARIETY_MAX
                               / VARIETY_TARGET_TYPES))
    return score, len(types)


def role_chemistry(players, xi_bonus=None):
    """Per-role chemistry lines for /cmuchem.

    ``xi_bonus`` is the shared Playing XI Bonus (0-20); computed from
    ``special_chemistry`` when not supplied. Returns a list of dicts in
    ``ROLE_ORDER``, each carrying the two components, the halved flag, the
    earned bonus and its maximum.
    """
    players = list(players)
    if xi_bonus is None:
        xi_bonus = special_chemistry(players)[0]

    # Block value per country across the whole XI — a role's players are scored
    # on how they sit in the *team's* blocks, not on clustering among themselves.
    _total, blocks = country_chemistry(players)
    value_by_country = {b["country"]: b["value"] for b in blocks}

    grouped = {role: [] for role in ROLE_ORDER}
    for player in players:
        grouped[role_of(player)].append(player)

    lines = []
    for role in ROLE_ORDER:
        members = grouped[role]
        if members:
            connection = sum(
                min(1.0, value_by_country.get(country_of(p), 0)
                    / ROLE_CONNECTION_TARGET)
                for p in members) / len(members)
        else:
            connection = 0.0

        raw = connection * ROLE_COMPONENT_MAX
        halved = role in ROLE_HALVED
        effective = raw / 2 if halved else raw
        maximum = ROLE_MAX_BONUS[role]
        # Divide by what this role can actually score, so a halved role can
        # still reach its stated ceiling.
        ceiling = (ROLE_COMPONENT_MAX / 2 if halved
                   else ROLE_COMPONENT_MAX) + SPECIAL_CHEMISTRY_CAP
        bonus = _round_half_up((effective + xi_bonus) / ceiling * maximum)

        lines.append({
            "role": role,
            "label": ROLE_LABEL[role],
            "emoji": ROLE_EMOJI[role],
            "players": len(members),
            "country_component": _round_half_up(raw),
            "country_component_exact": raw,
            "xi_component": xi_bonus,
            "halved": halved,
            "bonus": bonus,
            "max_bonus": maximum,
        })
    return lines


def calculate_role_report(players):
    """The full /cmuchem report.

    Overall is the honest sum of the parts shown on screen — role bonuses plus
    country diversity, card variety and the Playing XI Bonus — out of
    ``CMUCHEM_TOTAL_MAX`` (55), so every figure on the card adds up.
    """
    players = list(players)
    xi_bonus, special_detail = special_chemistry(players)
    lines = role_chemistry(players, xi_bonus=xi_bonus)
    diversity, country_count = country_diversity(players)
    variety, type_count = card_variety(players)

    role_total = sum(line["bonus"] for line in lines)
    return {
        "roles": lines,
        "role_total": role_total,
        "role_max": sum(ROLE_MAX_BONUS.values()),
        "diversity": diversity,
        "diversity_countries": country_count,
        "diversity_target": DIVERSITY_TARGET_COUNTRIES,
        "diversity_max": DIVERSITY_MAX,
        "variety": variety,
        "variety_types": type_count,
        "variety_target": VARIETY_TARGET_TYPES,
        "variety_max": VARIETY_MAX,
        "xi_bonus": xi_bonus,
        "xi_bonus_max": SPECIAL_CHEMISTRY_CAP,
        "special_detail": special_detail,
        "total": role_total + diversity + variety + xi_bonus,
        "total_max": CMUCHEM_TOTAL_MAX,
        "players_counted": len(players),
    }


def render_chemistry_card(players):
    """The /cmuchem card as Telegram HTML.

    Lives here rather than in the handler so it can be rendered — and tested —
    without importing Telegram, and reused by the Mini App.
    """
    report = calculate_role_report(players)

    lines = ["🧪 <b>TEAM CHEMISTRY</b>", "━━━━━━━━━━━━━━━━━━━"]
    for role in report["roles"]:
        # All-rounders show the halving inline so the number explains itself.
        left = (f"({role['country_component']}/{ROLE_COMPONENT_MAX} ÷ 2)"
                if role["halved"]
                else f"{role['country_component']}/{ROLE_COMPONENT_MAX}")
        lines.append(
            f"{role['emoji']} <b>{role['label']}</b>: "
            f"<code>{left} + {role['xi_component']}/{ROLE_COMPONENT_MAX} "
            f"= +{role['bonus']}/{role['max_bonus']}</code>"
        )

    lines += [
        "",
        f"<b>Country Diversity</b>: "
        f"<code>{report['diversity']}/{report['diversity_max']}</code> "
        f"({report['diversity_countries']}/{report['diversity_target']} countries)",
        f"<b>Card Variety</b>: "
        f"<code>{report['variety']}/{report['variety_max']}</code> "
        f"({report['variety_types']}/{report['variety_target']} special types)",
        f"<b>Playing XI Bonus</b>: "
        f"<code>{report['xi_bonus']}/{report['xi_bonus_max']}</code>",
        f"<b>Overall Chemistry</b>: "
        f"<code>{report['total']}/{report['total_max']}</code>",
        "<i>ALR boost is halved as they benefit from BAT &amp; BOWL</i>",
    ]
    return "\n".join(lines)


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
