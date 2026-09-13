"""Tournament Draft service — teams pick their squads, live, pick by pick.

The Challenge League can already *play* a franchise competition; what it never
had was a way to *fill* the squads, short of an admin typing every player into
every team by hand. A draft is that missing front end:

  • an admin uploads a **player pool** (a spreadsheet carrying tier, icon
    eligibility, gender and country alongside the usual ratings) and a
    **pick order** (Round, Pick, Tier, Team, Owner, Owner Tag ID);
  • the draft is bound to one Telegram group, and owners type ``/pick`` there
    when their slot comes up;
  • a clock runs on every pick, and picks for anyone who lets it run out;
  • when the last slot is filled, ``publish_to_league`` writes the squads into a
    real ``ChallengeLeague``, so the drafted teams can play immediately.

**The tier rule.** A pick slot's tier is a *ceiling*, not a requirement: a
Platinum slot accepts Platinum, Gold, Silver or Bronze. Picking below the slot's
tier is allowed and simply spends the slot — there is no refund and no extra
pick. One useful consequence is that a team's tier quota enforces itself: a
Platinum player fits *only* in a Platinum slot, so a team with two Platinum slots
can never end up with three Platinum players.

**The overseas rule.** A player is *home* when the country on their pool row is
the draft's ``home_country``, and overseas otherwise — the pool's own country
column decides, not a label, so a draft whose home country is England counts
English players as home without anybody re-typing the sheet. ``is_indian`` is
the (now badly named) column that carries the answer.

Contract, matching ``services.lp_tournament_service``: every function takes the
session first, **nothing here commits**, and every refusal is a ``DraftError``
carrying **plain text** (never HTML) written for whoever triggered it. The
``render_*`` helpers at the bottom are the opposite — they build HTML message
bodies and escape as they go.
"""

import json
import logging
import random
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import func

from models import (
    ChallengeLeague, ChallengeMode, ChallengePlayer, ChallengeTeam,
    DraftPick, DraftPlayer, DraftTeam, Player, PlayerDraft,
)

logger = logging.getLogger(__name__)


class DraftError(Exception):
    """A refusal carrying a message written for whoever typed the command.

    Plain text, never HTML — it quotes player and team names, which are
    user-supplied. Callers escape it once when they render it.
    """


# ── Lifecycle ─────────────────────────────────────────────────────────
STATUS_SETUP = "setup"
STATUS_LIVE = "live"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

_STATUS_LABEL = {
    STATUS_SETUP: "📝 Setup",
    STATUS_LIVE: "🟢 Live",
    STATUS_PAUSED: "⏸ Paused",
    STATUS_COMPLETED: "🏁 Completed",
    STATUS_CANCELLED: "🚫 Cancelled",
}

# ── The tier ladder ───────────────────────────────────────────────────
# Highest first. A draft may override this, but this is the ladder the feature
# was specified around and the one a new draft starts with.
DEFAULT_TIER_ORDER = ["Platinum", "Gold", "Silver", "Bronze"]
TIER_EMOJI = {
    "platinum": "💎", "diamond": "💠", "gold": "🥇",
    "silver": "🥈", "bronze": "🥉", "emerald": "💚", "icon": "⭐",
}

# Bounded by the columns they are written to.
MAX_DRAFT_NAME = 120
MAX_TEAM_NAME = 120
MAX_PLAYER_NAME = 150

# Clock bounds. One minute is the shortest useful pick window (and what a test
# draft wants); two hours is long enough that anything beyond it is a typo.
MIN_PICK_SECONDS = 60
MAX_PICK_SECONDS = 7200

# The four roles the match engine understands, and the spellings people type.
# ``admin._CSV_CAT_MAP`` is the older twin of this map; both exist because a
# service must not import the Flask admin module.
_CATEGORIES = ("Batsman", "Bowler", "All-rounder", "Wicket Keeper")
_CAT_MAP = {
    "bat": "Batsman", "batsman": "Batsman", "batter": "Batsman",
    "bowl": "Bowler", "bowler": "Bowler",
    "wk": "Wicket Keeper", "keeper": "Wicket Keeper",
    "wicketkeeper": "Wicket Keeper", "wicket keeper": "Wicket Keeper",
    "wicket-keeper": "Wicket Keeper", "wk batsman": "Wicket Keeper",
    "wicket keeper batter": "Wicket Keeper",
    "ar": "All-rounder", "all rounder": "All-rounder",
    "allrounder": "All-rounder", "all-rounder": "All-rounder",
}
_HAND_MAP = {"r": "Right", "right": "Right", "rh": "Right", "rhb": "Right",
             "l": "Left", "left": "Left", "lh": "Left", "lhb": "Left"}

_TRUE_WORDS = {"1", "y", "yes", "true", "t", "on", "icon", "eligible"}
_FALSE_WORDS = {"0", "n", "no", "false", "f", "off", "", "-"}

# ── Home country ──────────────────────────────────────────────────────
#
# A pool player is "home" when the country on their sheet row is the draft's
# ``home_country``, and overseas otherwise. The **country column is the source
# of truth**: an ``indian_status`` column is a per-sheet label that means
# nothing once a draft's home country isn't India, and a pool uploaded without
# one used to flag every single player as home — which is how a draft ends up
# with eleven "Indians" from four different countries.
#
# The folding below exists because the country column is typed by hand: "IND",
# "Ind.", "Indian" and "India" are one country, and comparing them raw makes
# three of those four overseas.

# Cells that name no country at all. These leave the flag alone rather than
# guessing, so a value corrected by hand survives a re-sync.
_UNKNOWN_COUNTRIES = {"unknown", "unkown", "n a", "na", "none", "null", "nil",
                      "tbd", "tba", "other", "others", "?", "--"}

# Demonyms and short codes → the canonical country name they fold onto. Only
# the cricketing nations: this is a draft pool, not a gazetteer.
_COUNTRY_ALIASES = {
    "ind": "india", "indian": "india", "bharat": "india",
    "aus": "australia", "australian": "australia", "aussie": "australia",
    "eng": "england", "english": "england", "englishman": "england",
    "pak": "pakistan", "pakistani": "pakistan",
    "sa": "south africa", "rsa": "south africa", "south african": "south africa",
    "proteas": "south africa",
    "nz": "new zealand", "new zealander": "new zealand", "kiwi": "new zealand",
    "blackcaps": "new zealand", "black caps": "new zealand",
    "sl": "sri lanka", "sri lankan": "sri lanka", "lankan": "sri lanka",
    "ban": "bangladesh", "bd": "bangladesh", "bangladeshi": "bangladesh",
    "afg": "afghanistan", "afghan": "afghanistan", "afghani": "afghanistan",
    "wi": "west indies", "windies": "west indies", "west indian": "west indies",
    "caribbean": "west indies",
    "zim": "zimbabwe", "zimbabwean": "zimbabwe",
    "ire": "ireland", "irish": "ireland",
    "sco": "scotland", "scot": "scotland", "scottish": "scotland",
    "ned": "netherlands", "nl": "netherlands", "holland": "netherlands",
    "dutch": "netherlands",
    "nep": "nepal", "nepali": "nepal", "nepalese": "nepal",
    "uae": "united arab emirates", "emirati": "united arab emirates",
    "usa": "united states", "us": "united states", "american": "united states",
    "united states of america": "united states",
    "can": "canada", "canadian": "canada",
    "ken": "kenya", "kenyan": "kenya",
    "nam": "namibia", "namibian": "namibia",
    "oma": "oman", "omani": "oman",
    "png": "papua new guinea", "papuan": "papua new guinea",
    "sgp": "singapore", "singaporean": "singapore",
    "hk": "hong kong",
    "welsh": "wales",
}

# The flag shown for a home player. ISO-3166 pairs are turned into regional
# indicators; the four that have no country code of their own are spelled out.
_COUNTRY_ISO2 = {
    "india": "IN", "australia": "AU", "pakistan": "PK", "south africa": "ZA",
    "new zealand": "NZ", "sri lanka": "LK", "bangladesh": "BD",
    "afghanistan": "AF", "zimbabwe": "ZW", "ireland": "IE",
    "netherlands": "NL", "nepal": "NP", "united arab emirates": "AE",
    "united states": "US", "canada": "CA", "kenya": "KE", "namibia": "NA",
    "oman": "OM", "papua new guinea": "PG", "singapore": "SG",
    "hong kong": "HK", "malaysia": "MY", "japan": "JP", "italy": "IT",
    "germany": "DE", "france": "FR", "uganda": "UG", "jersey": "JE",
    "bermuda": "BM", "qatar": "QA", "kuwait": "KW", "bahrain": "BH",
    "saudi arabia": "SA", "thailand": "TH", "china": "CN",
}
_COUNTRY_FLAG_OVERRIDES = {
    "england": "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F",
    "scotland": "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F",
    "wales": "\U0001F3F4\U000E0067\U000E0062\U000E0077\U000E006C\U000E0073\U000E007F",
    "west indies": "\U0001F3DD",
}

DEFAULT_HOME_COUNTRY = "India"
DEFAULT_HOME_FLAG = "\U0001F3E0"   # 🏠 — a home country we have no flag for
OVERSEAS_FLAG = "\u2708\uFE0F"      # ✈️

# Words that state home/overseas without naming a country. These stay generic:
# "Overseas" means overseas whatever the draft's home country is.
_OVERSEAS_WORDS = {"overseas", "foreign", "foreigner", "abroad", "away",
                   "international", "import", "non domestic", "non local"}
_DOMESTIC_WORDS = {"domestic", "home", "local", "native", "national"}

# Canonical names typed without their space ("newzealand", "srilanka").
_COUNTRY_COMPACT = {name.replace(" ", ""): name for name in
                    set(_COUNTRY_ALIASES.values()) | set(_COUNTRY_ISO2)
                    | set(_COUNTRY_FLAG_OVERRIDES)}


# ──────────────────────────────────────────────────────────────────────
# Small parsers
# ──────────────────────────────────────────────────────────────────────

def _loads(raw, fallback):
    """A ``*_json`` column as Python, falling back rather than raising.

    A hand-edited JSON column should degrade to the default, not take a live
    draft down mid-pick.
    """
    if not raw:
        return fallback
    try:
        value = json.loads(raw)
    except Exception:
        logger.warning("draft: unreadable JSON column, using fallback")
        return fallback
    if type(value) is not type(fallback):
        return fallback
    return value


def _dumps(value):
    return json.dumps(value, separators=(",", ":"))


def _as_bool(raw, default=False):
    text = str(raw or "").strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def _as_int(raw, default=0):
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def country_key(raw):
    """Fold a country cell onto one comparable spelling.

    ``"IND"``, ``"Ind."``, ``"Indian"`` and ``" india "`` are the same country
    and must compare equal, or a pool typed by three different people ends up
    with three different nationalities. Returns ``""`` for a blank cell or one
    that names no country ("Unknown", "N/A", "-").
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    folded = " ".join("".join(
        ch if (ch.isalnum() or ch.isspace()) else " " for ch in text).split())
    if not folded or folded in _UNKNOWN_COUNTRIES or folded in _FALSE_WORDS:
        return ""
    if folded in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[folded]
    compact = folded.replace(" ", "")
    return (_COUNTRY_ALIASES.get(compact)
            or _COUNTRY_COMPACT.get(compact)
            or folded)


def clean_home_country(raw):
    """A draft's ``home_country`` as it is stored: trimmed, bounded, never blank.

    Kept as the admin typed it (the column is shown back to them) rather than
    folded — ``country_key`` does the comparing.
    """
    return (str(raw or "").strip()[:60] or DEFAULT_HOME_COUNTRY)


def is_home_country(country, home_country):
    """``True``/``False`` for "is this the draft's home country", or ``None``.

    ``None`` means the question can't be answered — one side is blank or names
    no country — and every caller treats that as "leave it alone" rather than
    guessing, because guessing is what flagged an entire pool as Indian.
    """
    home = country_key(home_country)
    theirs = country_key(country)
    if not home or not theirs:
        return None
    return theirs == home


def country_flag(country):
    """The flag emoji for a country, or ``🏠`` when it isn't one we know."""
    key = country_key(country)
    if not key:
        return DEFAULT_HOME_FLAG
    if key in _COUNTRY_FLAG_OVERRIDES:
        return _COUNTRY_FLAG_OVERRIDES[key]
    iso = _COUNTRY_ISO2.get(key)
    if not iso:
        return DEFAULT_HOME_FLAG
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in iso)


def home_flag(draft):
    """The flag a draft's *home* players wear. Overseas always wear ✈️."""
    return country_flag(getattr(draft, "home_country", None))


def player_flag(player, draft=None):
    return OVERSEAS_FLAG if not player.is_indian else home_flag(draft)


def is_home_player(status_raw, home_country=DEFAULT_HOME_COUNTRY, country=""):
    """Whether a pool row is a *home* player for this draft.

    The row's **country decides** whenever it names one: that is the fact the
    spreadsheet actually carries, and it is the only thing that stays correct
    when a draft's home country isn't India.

    The ``indian_status`` column is the fallback for a sheet that has no usable
    country — the three shapes people type there: a word ("Overseas",
    "Domestic"), a flag (1/0, yes/no), or a country name. A row with neither is
    counted as home, which is what the column defaults to.
    """
    decided = is_home_country(country, home_country)
    if decided is not None:
        return decided

    text = " ".join(str(status_raw or "").strip().lower()
                    .replace("-", " ").replace("_", " ").split())
    if not text:
        return True
    if text in _OVERSEAS_WORDS:
        return False
    if text in _DOMESTIC_WORDS:
        return True
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    # "non-Indian", "not India" — a negated country is overseas either way.
    for prefix in ("non ", "not "):
        if text.startswith(prefix):
            return False
    decided = is_home_country(text, home_country)
    return True if decided is None else decided


def normalise_category(raw):
    text = str(raw or "").strip()
    if not text:
        return "Batsman"
    folded = text.lower().replace("_", " ")
    return _CAT_MAP.get(folded, _CAT_MAP.get(folded.replace("-", " "), text))


def normalise_hand(raw, default="Right"):
    text = str(raw or "").strip()
    if not text:
        return default
    return _HAND_MAP.get(text.lower(), text)


# ──────────────────────────────────────────────────────────────────────
# Lookup
# ──────────────────────────────────────────────────────────────────────

def get_draft(session, draft_id):
    return session.query(PlayerDraft).filter(PlayerDraft.id == draft_id).first()


def draft_for_chat(session, chat_id):
    """The draft bound to this group, or None. One draft per chat by index."""
    if not chat_id:
        return None
    return (session.query(PlayerDraft)
            .filter(PlayerDraft.chat_id == int(chat_id)).first())


def list_drafts(session):
    return (session.query(PlayerDraft)
            .order_by(PlayerDraft.created_at.desc(), PlayerDraft.id.desc()).all())


def teams(session, draft_id):
    return (session.query(DraftTeam)
            .filter(DraftTeam.draft_id == draft_id)
            .order_by(DraftTeam.sort_order, DraftTeam.name).all())


def find_team(session, draft_id, query):
    """A team by exact name, short name, or unique partial — None if unclear."""
    text = (query or "").strip().lower()
    if not text:
        return None
    rows = teams(session, draft_id)
    for row in rows:
        if (row.name or "").lower() == text or (row.short_name or "").lower() == text:
            return row
    hits = [r for r in rows if text in (r.name or "").lower()]
    return hits[0] if len(hits) == 1 else None


def co_owner_ids(team):
    return [int(x) for x in _loads(getattr(team, "co_owner_ids_json", None), [])
            if str(x).lstrip("-").isdigit()]


def may_pick_for(team, tg_id):
    """True when this Telegram user is the team's owner or a co-owner.

    Deliberately *not* widened to bot admins: an admin who needs to unstick a
    draft has ``/dskip``, which is recorded as an auto-pick rather than passed
    off as the owner's own choice.
    """
    if tg_id is None or team is None:
        return False
    tg_id = int(tg_id)
    return tg_id == (team.owner_tg_id or 0) or tg_id in co_owner_ids(team)


def add_co_owner(session, team, tg_id):
    """Let one more Telegram id pick for this team. Returns the new list."""
    tg_id = int(tg_id)
    if tg_id <= 0:
        raise DraftError("A co-owner is a positive Telegram user id.")
    if tg_id == (team.owner_tg_id or 0):
        raise DraftError("That id is already the team's owner.")
    ids = co_owner_ids(team)
    if tg_id in ids:
        raise DraftError("That id is already a co-owner of this team.")
    ids.append(tg_id)
    team.co_owner_ids_json = _dumps(ids)
    return ids


def set_co_owners(session, team, tg_ids):
    """Replace the co-owner list wholesale — what the website's form posts."""
    cleaned, seen = [], set()
    for raw in tg_ids or []:
        value = _as_int(raw, 0)
        if value > 0 and value != (team.owner_tg_id or 0) and value not in seen:
            seen.add(value)
            cleaned.append(value)
    team.co_owner_ids_json = _dumps(cleaned)
    return cleaned


def team_for_actor(session, draft_id, tg_id):
    """The team this user may pick for, or None."""
    for team in teams(session, draft_id):
        if may_pick_for(team, tg_id):
            return team
    return None


def status_label(draft):
    return _STATUS_LABEL.get(draft.status, draft.status or "?")


# ──────────────────────────────────────────────────────────────────────
# The tier ladder — a slot's tier is a ceiling
# ──────────────────────────────────────────────────────────────────────

def tier_order(draft):
    order = _loads(getattr(draft, "tier_order_json", None), [])
    order = [str(t).strip() for t in order if str(t).strip()]
    return order or list(DEFAULT_TIER_ORDER)


def set_tier_order(draft, tiers):
    cleaned, seen = [], set()
    for tier in tiers:
        name = str(tier or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            cleaned.append(name)
    if not cleaned:
        raise DraftError("A draft needs at least one tier in its ladder.")
    draft.tier_order_json = _dumps(cleaned)
    return cleaned


def normalise_tier(draft, raw):
    """Match a typed tier to the draft's ladder, case-insensitively.

    Returns the ladder's own spelling, or None when it isn't on the ladder —
    which is what makes an unknown tier an import error rather than a silent
    fifth tier nobody configured.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return None
    for tier in tier_order(draft):
        if tier.lower() == text:
            return tier
    return None


def tier_rank(draft, tier):
    """Position on the ladder, 0 = highest. None when off-ladder."""
    text = str(tier or "").strip().lower()
    for index, name in enumerate(tier_order(draft)):
        if name.lower() == text:
            return index
    return None


def tier_allows(draft, slot_tier, player_tier):
    """The ceiling rule: a slot takes its own tier or anything below it."""
    slot = tier_rank(draft, slot_tier)
    player = tier_rank(draft, player_tier)
    if slot is None or player is None:
        return False
    return player >= slot


def tier_emoji(tier):
    return TIER_EMOJI.get(str(tier or "").strip().lower(), "▫️")


def tier_badge(tier):
    return f"{tier_emoji(tier)} {tier}".strip()


# ──────────────────────────────────────────────────────────────────────
# Creating and configuring a draft
# ──────────────────────────────────────────────────────────────────────

def create_draft(session, name, *, pick_seconds=900, tiers=None,
                 home_country=DEFAULT_HOME_COUNTRY, max_overseas=11,
                 role_minimums=None):
    clean = (name or "").strip()
    if not clean:
        raise DraftError("Give the draft a name.")
    draft = PlayerDraft(
        name=clean[:MAX_DRAFT_NAME],
        status=STATUS_SETUP,
        pick_seconds=clamp_pick_seconds(pick_seconds),
        home_country=clean_home_country(home_country),
        max_overseas=max(0, min(99, _as_int(max_overseas, 11))),
        role_minimums_json=_dumps(role_minimums or {}),
    )
    set_tier_order(draft, tiers or DEFAULT_TIER_ORDER)
    session.add(draft)
    session.flush()
    return draft


def clamp_pick_seconds(value):
    seconds = _as_int(value, 900)
    return max(MIN_PICK_SECONDS, min(MAX_PICK_SECONDS, seconds))


def role_minimums(draft):
    raw = _loads(getattr(draft, "role_minimums_json", None), {})
    out = {}
    for role, count in raw.items():
        role = normalise_category(role)
        count = _as_int(count, 0)
        if count > 0:
            out[role] = count
    return out


def set_role_minimums(draft, mapping):
    cleaned = {}
    for role, count in (mapping or {}).items():
        role = normalise_category(role)
        count = _as_int(count, 0)
        if count > 0:
            cleaned[role] = count
    draft.role_minimums_json = _dumps(cleaned)
    return cleaned


def bind_chat(session, draft, chat_id):
    """Bind the draft to a group. Refuses to steal another draft's chat."""
    chat_id = int(chat_id)
    other = draft_for_chat(session, chat_id)
    if other is not None and other.id != draft.id:
        raise DraftError(f"This chat is already running the draft “{other.name}”.")
    draft.chat_id = chat_id
    return draft


# ──────────────────────────────────────────────────────────────────────
# Spreadsheet import
# ──────────────────────────────────────────────────────────────────────
#
# Both importers match columns **by header name** rather than by position, so an
# admin can reorder or add columns without the import silently reading ratings
# out of the country column. A sheet with no recognisable header falls back to
# the documented order below.

POOL_COLUMNS = ["name", "rating", "tier", "icon_eligible", "gender",
                "indian_status", "category", "country", "bat_hand", "bowl_hand",
                "bowl_style", "bat_rating", "bowl_rating"]

ORDER_COLUMNS = ["round_no", "pick_no", "tier", "team_name", "owner_name",
                 "owner_tg_id"]

_POOL_ALIASES = {
    "name": "name", "player": "name", "playername": "name", "player_name": "name",
    "rating": "rating", "ovr": "rating", "overall": "rating",
    "tier": "tier", "grade": "tier", "band": "tier",
    "iconeligible": "icon_eligible", "icon_eligible": "icon_eligible",
    "icon": "icon_eligible", "iseligibleicon": "icon_eligible",
    "gender": "gender", "sex": "gender",
    "indianstatus": "indian_status", "indian_status": "indian_status",
    "indian": "indian_status", "nationality_status": "indian_status",
    "overseas": "indian_status", "domestic": "indian_status",
    "category": "category", "role": "category", "type": "category",
    "country": "country", "nation": "country", "nationality": "country",
    "bathand": "bat_hand", "bat_hand": "bat_hand", "batting_hand": "bat_hand",
    "bowlhand": "bowl_hand", "bowl_hand": "bowl_hand", "bowling_hand": "bowl_hand",
    "bowlstyle": "bowl_style", "bowl_style": "bowl_style",
    "bowling_style": "bowl_style", "style": "bowl_style",
    "batrating": "bat_rating", "bat_rating": "bat_rating", "batting": "bat_rating",
    "bowlrating": "bowl_rating", "bowl_rating": "bowl_rating", "bowling": "bowl_rating",
}

_ORDER_ALIASES = {
    "roundno": "round_no", "round_no": "round_no", "round": "round_no", "rd": "round_no",
    "picknumber": "pick_no", "pick_number": "pick_no", "pickno": "pick_no",
    "pick_no": "pick_no", "pick": "pick_no",
    "tier": "tier", "grade": "tier",
    "teamname": "team_name", "team_name": "team_name", "team": "team_name",
    "franchise": "team_name",
    "ownername": "owner_name", "owner_name": "owner_name", "owner": "owner_name",
    "ownertagid": "owner_tg_id", "owner_tag_id": "owner_tg_id",
    "ownertag": "owner_tg_id", "owner_id": "owner_tg_id",
    "telegramid": "owner_tg_id", "telegram_id": "owner_tg_id", "tgid": "owner_tg_id",
    "owner_telegram_id": "owner_tg_id",
}


# Every canonical column name is also a valid header for itself. Spelling these
# out by hand is how "owner_tg_id" ended up being the one header the order sheet
# would not accept, so they are added programmatically instead.
for _canonical in POOL_COLUMNS:
    _POOL_ALIASES.setdefault(_canonical, _canonical)
for _canonical in ORDER_COLUMNS:
    _ORDER_ALIASES.setdefault(_canonical, _canonical)
del _canonical


def _fold_header(cell):
    """``" Bowl Style "`` → ``"bowl_style"``, so spacing and case don't matter."""
    text = str(cell or "").strip().lower()
    out = []
    for char in text:
        out.append(char if char.isalnum() else "_")
    folded = "".join(out).strip("_")
    while "__" in folded:
        folded = folded.replace("__", "_")
    return folded


def _column_map(first_row, aliases, positional):
    """``({field: column index}, header_consumed)`` for a sheet's first row.

    A row counts as a header when at least half of its filled cells name known
    columns — enough to recognise a real header without mistaking a data row
    whose first cell happens to read "Rating".
    """
    filled = [c for c in first_row if str(c or "").strip()]
    mapping, hits = {}, 0
    for index, cell in enumerate(first_row):
        field = aliases.get(_fold_header(cell))
        if field and field not in mapping:
            mapping[field] = index
            hits += 1
    if filled and hits >= max(2, (len(filled) + 1) // 2):
        return mapping, True
    return {field: i for i, field in enumerate(positional)}, False


def _cell(row, mapping, field):
    index = mapping.get(field)
    if index is None or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def import_pool(session, draft, rows, *, replace=False):
    """Load the player pool from a sheet. Returns ``(added, updated, errors)``.

    Re-uploading is safe: a player already in the pool is updated in place, so a
    corrected spreadsheet can simply be uploaded again. ``replace=True`` clears
    the pool first — refused once picking has started, because deleting a player
    somebody has already drafted would orphan their squad.
    """
    rows = [r for r in (rows or []) if any(str(c or "").strip() for c in r)]
    if not rows:
        raise DraftError("That file had no rows in it.")

    mapping, header = _column_map(rows[0], _POOL_ALIASES, POOL_COLUMNS)
    if "name" not in mapping:
        raise DraftError("No “name” column found. The pool needs at least a "
                         "name and a tier per row.")
    body = rows[1:] if header else rows

    if replace:
        if picks_made(session, draft.id):
            raise DraftError("Picking has already started — the pool can no "
                             "longer be replaced. Use /dundo to roll picks back "
                             "first, or start a new draft.")
        (session.query(DraftPlayer)
         .filter(DraftPlayer.draft_id == draft.id)
         .delete(synchronize_session=False))
        session.flush()

    ladder = tier_order(draft)
    existing = {(p.name or "").lower(): p for p in
                session.query(DraftPlayer).filter(DraftPlayer.draft_id == draft.id).all()}
    # One lookup for the whole file rather than one per row: a 600-player pool
    # against a 3,000-card catalogue is otherwise 600 round trips.
    catalogue = _catalogue_by_name(session)

    added = updated = 0
    errors = []
    seen = set()
    for offset, row in enumerate(body, start=2 if header else 1):
        name = _cell(row, mapping, "name")[:MAX_PLAYER_NAME]
        if not name:
            errors.append(f"Row {offset}: no player name.")
            continue
        key = name.lower()
        if key in seen:
            errors.append(f"Row {offset}: “{name}” appears twice in this file.")
            continue
        seen.add(key)

        tier = normalise_tier(draft, _cell(row, mapping, "tier"))
        if tier is None:
            errors.append(f"Row {offset}: “{name}” has tier "
                          f"“{_cell(row, mapping, 'tier') or '(blank)'}”, which is "
                          f"not on this draft's ladder ({', '.join(ladder)}).")
            continue

        rating = max(0, min(100, _as_int(_cell(row, mapping, "rating"), 70)))
        values = {
            "tier": tier,
            "rating": rating,
            "icon_eligible": _as_bool(_cell(row, mapping, "icon_eligible")),
            "gender": (_cell(row, mapping, "gender") or "")[:10] or None,
            "is_indian": is_home_player(_cell(row, mapping, "indian_status"),
                                        draft.home_country,
                                        _cell(row, mapping, "country")),
            "category": normalise_category(_cell(row, mapping, "category"))[:30],
            "country": (_cell(row, mapping, "country") or "Unknown")[:60],
            "bat_hand": normalise_hand(_cell(row, mapping, "bat_hand"))[:10],
            "bowl_hand": normalise_hand(_cell(row, mapping, "bowl_hand"))[:10],
            "bowl_style": (_cell(row, mapping, "bowl_style") or "Medium Pacer")[:30],
            "bat_rating": max(0, min(100, _as_int(_cell(row, mapping, "bat_rating"), rating))),
            "bowl_rating": max(0, min(100, _as_int(_cell(row, mapping, "bowl_rating"), rating))),
            "source_player_id": catalogue.get(key),
        }

        row_obj = existing.get(key)
        if row_obj is None:
            row_obj = DraftPlayer(draft_id=draft.id, name=name, **values)
            session.add(row_obj)
            existing[key] = row_obj
            added += 1
        else:
            for field, value in values.items():
                setattr(row_obj, field, value)
            updated += 1

    session.flush()
    return added, updated, errors


def home_status_counts(session, draft):
    """``(home, overseas, unknown, wrong)`` for the whole pool.

    ``unknown`` counts rows whose country names nothing we can read; ``wrong``
    counts rows whose stored flag disagrees with their country — the number a
    re-sync would move, and the one worth showing an admin before they run it.
    """
    home = overseas = unknown = wrong = 0
    for player in (session.query(DraftPlayer)
                   .filter(DraftPlayer.draft_id == draft.id).all()):
        if player.is_indian:
            home += 1
        else:
            overseas += 1
        decided = is_home_country(player.country, draft.home_country)
        if decided is None:
            unknown += 1
        elif decided != bool(player.is_indian):
            wrong += 1
    return home, overseas, unknown, wrong


def resync_home_status(session, draft):
    """Re-flag the whole pool from each player's country. Returns ``(changed, unknown)``.

    ``import_pool`` already gets this right, but a draft whose home country was
    set (or corrected) after the pool went in carries rows decided under the old
    answer — including a live draft, which cannot simply be re-imported because
    replacing the pool is refused once picking has started.

    Rows whose country names nothing readable are left exactly as they are, so a
    flag an admin fixed by hand is not undone by a re-sync.
    """
    changed = unknown = 0
    for player in (session.query(DraftPlayer)
                   .filter(DraftPlayer.draft_id == draft.id).all()):
        decided = is_home_country(player.country, draft.home_country)
        if decided is None:
            unknown += 1
            continue
        if bool(player.is_indian) != decided:
            player.is_indian = decided
            changed += 1
    session.flush()
    return changed, unknown


def set_home_country(session, draft, raw):
    """Set the draft's home country and re-flag the pool against it.

    The two always move together: a home country nobody re-synced against is
    just a label, and the squad each team ends up with depends on the flags.
    """
    draft.home_country = clean_home_country(raw)
    return (draft.home_country,) + resync_home_status(session, draft)


def _catalogue_by_name(session):
    """``{lowercased name: players.id}`` for the base cards.

    The link is what lets a pick post the player's real card image. Base cards
    win over variants so a pool entry resolves to the plain card rather than to
    whichever "World Cup 2023" edition happens to sort first.
    """
    out = {}
    query = (session.query(Player.id, Player.name, Player.parent_player_id)
             .filter(Player.is_active.is_(True)))
    for pid, name, parent in query.all():
        key = (name or "").strip().lower()
        if not key:
            continue
        if key not in out or parent is None:
            out[key] = pid
    return out


def import_order(session, draft, rows, *, replace=False):
    """Load the pick order from a sheet. Returns ``(picks, teams_created, errors)``.

    Teams are created from the sheet as they are first seen, so the order sheet
    is the only thing an admin has to prepare: Team Name, Owner Name and Owner
    Tag ID travel with the picks.
    """
    rows = [r for r in (rows or []) if any(str(c or "").strip() for c in r)]
    if not rows:
        raise DraftError("That file had no rows in it.")

    mapping, header = _column_map(rows[0], _ORDER_ALIASES, ORDER_COLUMNS)
    if "team_name" not in mapping:
        raise DraftError("No “Team Name” column found. The order sheet needs "
                         "Round No, Pick Number, Tier and Team Name.")
    body = rows[1:] if header else rows

    if picks_made(session, draft.id):
        raise DraftError("Picking has already started — the order can no longer "
                         "be changed. Use /dundo to roll picks back first.")

    if replace:
        (session.query(DraftPick)
         .filter(DraftPick.draft_id == draft.id)
         .delete(synchronize_session=False))
        draft.current_pick_id = None
        draft.pick_deadline_at = None
        session.flush()

    by_name = {(t.name or "").lower(): t for t in teams(session, draft.id)}
    taken = {(p.round_no, p.pick_no) for p in
             session.query(DraftPick).filter(DraftPick.draft_id == draft.id).all()}

    created = 0
    parsed = []
    errors = []
    for offset, row in enumerate(body, start=2 if header else 1):
        team_name = _cell(row, mapping, "team_name")[:MAX_TEAM_NAME]
        if not team_name:
            errors.append(f"Row {offset}: no team name.")
            continue
        tier = normalise_tier(draft, _cell(row, mapping, "tier"))
        if tier is None:
            errors.append(f"Row {offset}: tier "
                          f"“{_cell(row, mapping, 'tier') or '(blank)'}” is not on "
                          f"this draft's ladder.")
            continue
        round_no = _as_int(_cell(row, mapping, "round_no"), 0)
        pick_no = _as_int(_cell(row, mapping, "pick_no"), 0)
        if round_no <= 0 or pick_no <= 0:
            errors.append(f"Row {offset}: Round No and Pick Number must both be "
                          f"whole numbers of 1 or more.")
            continue
        if (round_no, pick_no) in taken:
            errors.append(f"Row {offset}: R{round_no} P{pick_no} is listed twice.")
            continue
        taken.add((round_no, pick_no))

        key = team_name.lower()
        team = by_name.get(key)
        if team is None:
            team = DraftTeam(draft_id=draft.id, name=team_name,
                             short_name=_short_code(team_name),
                             sort_order=len(by_name))
            session.add(team)
            session.flush()
            by_name[key] = team
            created += 1
        # Owner details travel with every row; a later row may be the one that
        # carries them, so fill in rather than overwrite with blanks.
        owner_name = _cell(row, mapping, "owner_name")
        owner_tg = _as_int(_cell(row, mapping, "owner_tg_id"), 0)
        if owner_name and not team.owner_name:
            team.owner_name = owner_name[:120]
        if owner_tg > 0 and not team.owner_tg_id:
            team.owner_tg_id = owner_tg

        parsed.append((round_no, pick_no, tier, team.id))

    # ``overall_no`` is recomputed across the whole draft rather than taken from
    # the file, so a sheet uploaded in the wrong row order still runs R1 before
    # R2 — the clock walks this column, not the sheet. New rows are inserted
    # above every existing one first (the column is unique, so they cannot all
    # go in as 0) and the renumber below puts the whole list back in order.
    highest = (session.query(func.max(DraftPick.overall_no))
               .filter(DraftPick.draft_id == draft.id).scalar() or 0)
    for offset, (round_no, pick_no, tier, team_id) in enumerate(
            sorted(parsed), start=1):
        session.add(DraftPick(draft_id=draft.id, round_no=round_no,
                              pick_no=pick_no, overall_no=highest + offset,
                              tier=tier, team_id=team_id, status="pending"))
    session.flush()
    renumber_order(session, draft.id)
    return len(parsed), created, errors


def renumber_order(session, draft_id):
    """Rewrite ``overall_no`` from (round, pick) so the clock order is right."""
    rows = (session.query(DraftPick)
            .filter(DraftPick.draft_id == draft_id)
            .order_by(DraftPick.round_no, DraftPick.pick_no, DraftPick.id).all())
    if not rows:
        return 0
    # Two passes through a scratch range. ``overall_no`` is unique per draft, so
    # writing the final numbers directly would collide with whichever row still
    # holds the number we are about to assign.
    scratch = -1
    for pick in rows:
        pick.overall_no = scratch
        scratch -= 1
    session.flush()
    for index, pick in enumerate(rows, start=1):
        pick.overall_no = index
    session.flush()
    return len(rows)


def _short_code(name):
    """A 4-letter code from a team name — the same rule Lets Play teams use."""
    letters = "".join(ch for ch in (name or "") if ch.isalnum())
    return (letters[:4].upper() or "TEAM")


# ──────────────────────────────────────────────────────────────────────
# The clock
# ──────────────────────────────────────────────────────────────────────

def picks_made(session, draft_id):
    """How many slots have been resolved. The lock behind every reshape guard."""
    return (session.query(DraftPick)
            .filter(DraftPick.draft_id == draft_id,
                    DraftPick.status != "pending").count())


def pending_picks(session, draft_id, team_id=None):
    query = (session.query(DraftPick)
             .filter(DraftPick.draft_id == draft_id, DraftPick.status == "pending"))
    if team_id is not None:
        query = query.filter(DraftPick.team_id == team_id)
    return query.order_by(DraftPick.overall_no).all()


def current_pick(session, draft):
    """The pick on the clock, re-derived when the stored pointer is stale.

    The pointer can go stale in one way that matters: a pick resolved by another
    process between this read and the last write. Falling back to "the lowest
    pending slot" makes that self-healing rather than a wedged draft.
    """
    if draft.current_pick_id:
        pick = (session.query(DraftPick)
                .filter(DraftPick.id == draft.current_pick_id).first())
        if pick is not None and pick.status == "pending":
            return pick
    return (session.query(DraftPick)
            .filter(DraftPick.draft_id == draft.id, DraftPick.status == "pending")
            .order_by(DraftPick.overall_no).first())


def advance(session, draft):
    """Put the next pending slot on the clock. Returns it, or None when done.

    Completing the draft here — rather than in ``make_pick`` — means a draft
    finished by the clock, by a manual pick or by ``/dskip`` all end the same way.
    """
    nxt = (session.query(DraftPick)
           .filter(DraftPick.draft_id == draft.id, DraftPick.status == "pending")
           .order_by(DraftPick.overall_no).first())
    if nxt is None:
        draft.current_pick_id = None
        draft.pick_deadline_at = None
        draft.warn_sent = False
        if draft.status == STATUS_LIVE:
            draft.status = STATUS_COMPLETED
        return None
    draft.current_pick_id = nxt.id
    draft.warn_sent = False
    draft.pick_deadline_at = (datetime.utcnow()
                              + timedelta(seconds=clamp_pick_seconds(draft.pick_seconds)))
    return nxt


def seconds_left(draft, now=None):
    """Whole seconds left on the clock; 0 when expired, None when not running."""
    if not draft.pick_deadline_at or draft.status != STATUS_LIVE:
        return None
    delta = (draft.pick_deadline_at - (now or datetime.utcnow())).total_seconds()
    return max(0, int(delta))


def format_clock(seconds):
    if seconds is None:
        return "—"
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def start(session, draft):
    if draft.status == STATUS_LIVE:
        raise DraftError("That draft is already live.")
    if draft.status in (STATUS_COMPLETED, STATUS_CANCELLED):
        raise DraftError(f"That draft is {draft.status} — it cannot be restarted.")
    if not draft.chat_id:
        raise DraftError("Bind the draft to this group with /dbind first.")
    if not pending_picks(session, draft.id) and not picks_made(session, draft.id):
        raise DraftError("Upload a draft order before starting.")
    if not session.query(DraftPlayer).filter(DraftPlayer.draft_id == draft.id).count():
        raise DraftError("Upload a player pool before starting.")
    missing = [t.name for t in teams(session, draft.id) if not t.owner_tg_id]
    if missing:
        raise DraftError("These teams have no Owner Tag ID, so nobody could pick "
                         "for them: " + ", ".join(missing[:6]))
    draft.status = STATUS_LIVE
    return advance(session, draft)


def pause(session, draft):
    if draft.status != STATUS_LIVE:
        raise DraftError("That draft is not live.")
    draft.status = STATUS_PAUSED
    # Drop the deadline rather than freezing it: the pause is open-ended, and a
    # stale deadline would auto-pick the instant the draft resumed.
    draft.pick_deadline_at = None
    return draft


def resume(session, draft):
    if draft.status != STATUS_PAUSED:
        raise DraftError("That draft is not paused.")
    draft.status = STATUS_LIVE
    pick = current_pick(session, draft)
    if pick is None:
        return advance(session, draft)
    draft.current_pick_id = pick.id
    draft.warn_sent = False
    draft.pick_deadline_at = (datetime.utcnow()
                              + timedelta(seconds=clamp_pick_seconds(draft.pick_seconds)))
    return pick


def set_status(session, draft, status):
    if status not in _STATUS_LABEL:
        raise DraftError(f"Unknown status “{status}”.")
    draft.status = status
    if status in (STATUS_COMPLETED, STATUS_CANCELLED, STATUS_PAUSED):
        draft.pick_deadline_at = None
    return draft


# ──────────────────────────────────────────────────────────────────────
# Squads
# ──────────────────────────────────────────────────────────────────────

def squad(session, team_id):
    """A team's drafted players, best tier first then best rating."""
    rows = (session.query(DraftPlayer)
            .filter(DraftPlayer.picked_by_team_id == team_id).all())
    return rows


def squad_sorted(session, draft, team_id):
    rows = squad(session, team_id)
    return sorted(rows, key=lambda p: (tier_rank(draft, p.tier) if tier_rank(draft, p.tier)
                                       is not None else 99, -(p.rating or 0), p.name or ""))


def tier_slots(session, draft_id, team_id):
    """``{tier: total slots}`` for a team, from its rows in the order sheet."""
    out = {}
    rows = (session.query(DraftPick)
            .filter(DraftPick.draft_id == draft_id, DraftPick.team_id == team_id).all())
    for pick in rows:
        out[pick.tier] = out.get(pick.tier, 0) + 1
    return out


def overseas_count(session, team_id):
    return (session.query(DraftPlayer)
            .filter(DraftPlayer.picked_by_team_id == team_id,
                    DraftPlayer.is_indian.is_(False)).count())


def role_gap(session, draft, team_id):
    """``{role: how many more this team still needs}`` for unmet minimums."""
    minimums = role_minimums(draft)
    if not minimums:
        return {}
    have = {}
    for player in squad(session, team_id):
        have[player.category] = have.get(player.category, 0) + 1
    gap = {}
    for role, needed in minimums.items():
        short = needed - have.get(role, 0)
        # An all-rounder is a legitimate way to satisfy a bowler minimum; the
        # XI rules already treat them that way, and refusing the pick that does
        # would be a rule the squad sheet never stated.
        if role == "Bowler":
            short -= have.get("All-rounder", 0)
        if short > 0:
            gap[role] = short
    return gap


# ──────────────────────────────────────────────────────────────────────
# Picking
# ──────────────────────────────────────────────────────────────────────

def available(session, draft_id, tier=None):
    query = (session.query(DraftPlayer)
             .filter(DraftPlayer.draft_id == draft_id,
                     DraftPlayer.picked_by_team_id.is_(None)))
    if tier:
        query = query.filter(DraftPlayer.tier == tier)
    return query.order_by(DraftPlayer.rating.desc(), DraftPlayer.name).all()


def find_available(session, draft_id, query_text):
    """``(exact_match_or_None, candidates)`` for a typed player name.

    Exact wins outright; otherwise prefix matches beat substring matches. An
    ambiguous name is handed back as a list rather than guessed at, because
    guessing "Kohli" wrong costs a team its pick and cannot be taken back
    without an admin.
    """
    text = (query_text or "").strip().lower()
    if not text:
        return None, []
    pool = available(session, draft_id)
    exact = [p for p in pool if (p.name or "").lower() == text]
    if len(exact) == 1:
        return exact[0], []
    prefix = [p for p in pool if (p.name or "").lower().startswith(text)]
    if len(prefix) == 1:
        return prefix[0], []
    if prefix:
        return None, prefix
    contains = [p for p in pool if text in (p.name or "").lower()]
    if len(contains) == 1:
        return contains[0], []
    return None, contains


def validate_pick(session, draft, pick, player):
    """Raise ``DraftError`` when this player may not go into this slot."""
    if player is None:
        raise DraftError("No such player in this draft's pool.")
    if player.draft_id != draft.id:
        raise DraftError(f"{player.name} is not in this draft's pool.")
    if player.picked_by_team_id:
        owner = session.query(DraftTeam).filter(
            DraftTeam.id == player.picked_by_team_id).first()
        raise DraftError(f"{player.name} has already gone to "
                         f"{owner.name if owner else 'another team'}.")
    if pick is None or pick.status != "pending":
        raise DraftError("That pick has already been made.")

    if not tier_allows(draft, pick.tier, player.tier):
        raise DraftError(
            f"{player.name} is {player.tier}, and this is a {pick.tier} slot. "
            f"A slot takes its own tier or below "
            f"({' > '.join(tier_order(draft))}).")

    cap = draft.max_overseas if draft.max_overseas is not None else 11
    if not player.is_indian:
        used = overseas_count(session, pick.team_id)
        if used + 1 > cap:
            raise DraftError(f"{player.name} is overseas and this squad is "
                             f"already at its limit of {cap}.")

    gap = role_gap(session, draft, pick.team_id)
    if gap:
        # Reachability, not the finished squad: refuse the pick that makes a
        # minimum impossible while there are still slots left to satisfy it.
        role = normalise_category(player.category)
        remaining = dict(gap)
        if role in remaining:
            remaining[role] -= 1
        elif role == "All-rounder" and remaining.get("Bowler"):
            remaining["Bowler"] -= 1
        still_needed = sum(v for v in remaining.values() if v > 0)
        slots_after = max(0, len(pending_picks(session, draft.id, pick.team_id)) - 1)
        if still_needed > slots_after:
            wanted = ", ".join(f"{n}× {r}" for r, n in sorted(gap.items()))
            raise DraftError(
                f"That squad still needs {wanted} and would have only "
                f"{slots_after} pick(s) left. Pick one of those roles first.")
    return True


def make_pick(session, draft, pick, player, *, by_tg_id=None, is_auto=False):
    """Claim a player for a slot and move the clock on. Returns the pick.

    The player and the slot are both claimed with a conditional UPDATE rather
    than a read-then-write: two co-owners can type ``/pick`` on the same tick,
    and only one of them may win.
    """
    validate_pick(session, draft, pick, player)
    now = datetime.utcnow()

    claimed = (session.query(DraftPlayer)
               .filter(DraftPlayer.id == player.id,
                       DraftPlayer.picked_by_team_id.is_(None))
               .update({"picked_by_team_id": pick.team_id, "picked_at": now},
                       synchronize_session=False))
    if not claimed:
        raise DraftError(f"{player.name} has just been taken by someone else.")

    slot = (session.query(DraftPick)
            .filter(DraftPick.id == pick.id, DraftPick.status == "pending")
            .update({"draft_player_id": player.id, "status": "done",
                     "picked_by_tg_id": by_tg_id, "is_auto": bool(is_auto),
                     "picked_at": now}, synchronize_session=False))
    if not slot:
        # Someone else resolved the slot in between. Hand the player back rather
        # than leaving them claimed for a pick that never happened.
        (session.query(DraftPlayer)
         .filter(DraftPlayer.id == player.id)
         .update({"picked_by_team_id": None, "picked_at": None},
                 synchronize_session=False))
        raise DraftError("That pick has just been made by someone else.")

    # The two UPDATEs above bypassed the identity map, so every loaded row is
    # now potentially stale. Flush first: this session is autoflush=False, and
    # expiring before flushing would throw away pending work (the draft's own
    # status, for one) instead of writing it.
    session.flush()
    session.expire_all()
    _drop_from_queues(session, draft.id, player.id)
    advance(session, draft)
    return session.query(DraftPick).filter(DraftPick.id == pick.id).first()


def skip_pick(session, draft, pick):
    """Resolve a slot with nobody in it and move on.

    Only used when the clock ran out and the pool has no legal player left for
    that squad. Wedging the draft on one impossible slot would be worse.
    """
    (session.query(DraftPick)
     .filter(DraftPick.id == pick.id, DraftPick.status == "pending")
     .update({"status": "skipped", "is_auto": True,
              "picked_at": datetime.utcnow()}, synchronize_session=False))
    session.flush()
    session.expire_all()
    advance(session, draft)
    return pick


def auto_pick(session, draft, pick):
    """Pick for a team whose clock ran out. Returns the player, or None.

    Two steps, in order:

    1. **The owner's queue.** ``/dqueue`` lets an owner line up who they want;
       the first queued player that is still legal wins. Being asleep should not
       mean losing the player you already said you wanted.
    2. **The tier's average.** Otherwise, among the legal players in the slot's
       tier, take the one closest to that set's mean rating — the middle of the
       band rather than its best or worst card. Deterministic on purpose: the
       same draft state always auto-picks the same player, so the result can be
       explained afterwards and asserted in a test.

    When the slot's own tier has nothing legal left, it steps down the ladder —
    which the ceiling rule already permits. ``None`` means nothing legal remains
    anywhere and the caller should skip the slot.
    """
    team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
    for player_id in queue_ids(team):
        player = (session.query(DraftPlayer)
                  .filter(DraftPlayer.id == player_id,
                          DraftPlayer.draft_id == draft.id,
                          DraftPlayer.picked_by_team_id.is_(None)).first())
        if player is None:
            continue
        if is_legal_pick(session, draft, pick, player):
            return player

    for _tier, legal in legal_by_tier(session, draft, pick):
        mean = sum(p.rating or 0 for p in legal) / len(legal)
        return sorted(legal, key=lambda p: (abs((p.rating or 0) - mean),
                                            -(p.rating or 0), p.name or ""))[0]
    return None


def is_legal_pick(session, draft, pick, player):
    """``validate_pick`` as a yes/no, for the paths that filter rather than refuse."""
    try:
        validate_pick(session, draft, pick, player)
    except DraftError:
        return False
    return True


def legal_by_tier(session, draft, pick):
    """Yield ``(tier, [legal players])`` down the ladder from the slot's tier.

    The slot's own tier comes first and the ladder is only stepped down when it
    has nothing legal left — which the ceiling rule already permits, and which
    is what keeps one impossible slot from wedging the draft. Tiers with nothing
    legal are skipped, so a caller can take the first pair it is given.
    """
    start_rank = tier_rank(draft, pick.tier)
    if start_rank is None:
        return
    for tier in tier_order(draft)[start_rank:]:
        legal = [player for player in available(session, draft.id, tier=tier)
                 if is_legal_pick(session, draft, pick, player)]
        if legal:
            yield tier, legal


def random_pick(session, draft, pick, *, rng=None):
    """A **random** legal player from the slot's allotted tier, or ``None``.

    What ``/dautopick`` grants a team that isn't in the room. ``auto_pick`` is
    deliberately deterministic — the clock's choice has to be explainable
    afterwards — but running that same rule over every offline team hands them
    all the middle of the band, pick after pick. An admin granting a pick wants
    the tier honoured and the name inside it left to chance, so this ignores the
    queue (an owner who lined one up is not the absent owner this is for) and
    draws uniformly from what is legal.
    """
    for _tier, legal in legal_by_tier(session, draft, pick):
        return (rng or random).choice(legal)
    return None


def resolve_expired(session, draft, pick, *, by_tg_id=None):
    """Auto-pick (or skip) one expired slot. Returns ``(pick, player_or_None)``."""
    return _resolve_with(session, draft, pick, auto_pick(session, draft, pick),
                         by_tg_id=by_tg_id)


def resolve_random(session, draft, pick, *, by_tg_id=None, rng=None):
    """Grant one slot a random legal player. Returns ``(pick, player_or_None)``.

    Recorded as an auto-pick like ``/dskip``'s, because that is what it is: the
    team on the clock did not choose this player and the board must not say
    they did.
    """
    return _resolve_with(session, draft, pick,
                         random_pick(session, draft, pick, rng=rng),
                         by_tg_id=by_tg_id)


def _resolve_with(session, draft, pick, player, *, by_tg_id=None):
    if player is None:
        return skip_pick(session, draft, pick), None
    return make_pick(session, draft, pick, player,
                     by_tg_id=by_tg_id, is_auto=True), player


def undo_last(session, draft):
    """Roll the most recent resolved pick back onto the clock.

    The escape hatch a live draft always ends up needing — a mis-typed name, a
    clock that expired during an argument, an owner who picked for the wrong
    team. Returns ``(pick, player_or_None)``.
    """
    last = (session.query(DraftPick)
            .filter(DraftPick.draft_id == draft.id, DraftPick.status != "pending")
            .order_by(DraftPick.overall_no.desc()).first())
    if last is None:
        raise DraftError("No picks have been made yet.")
    player = None
    if last.draft_player_id:
        player = (session.query(DraftPlayer)
                  .filter(DraftPlayer.id == last.draft_player_id).first())
        if player is not None:
            player.picked_by_team_id = None
            player.picked_at = None
    last.draft_player_id = None
    last.status = "pending"
    last.is_auto = False
    last.picked_by_tg_id = None
    last.picked_at = None
    session.flush()
    # A completed draft that gets a pick undone is live again — otherwise the
    # slot sits on the clock in a draft nothing can pick in.
    if draft.status == STATUS_COMPLETED:
        draft.status = STATUS_LIVE
    draft.current_pick_id = last.id
    draft.warn_sent = False
    draft.pick_deadline_at = (datetime.utcnow()
                              + timedelta(seconds=clamp_pick_seconds(draft.pick_seconds))
                              ) if draft.status == STATUS_LIVE else None
    return last, player


# ──────────────────────────────────────────────────────────────────────
# The owner's queue
# ──────────────────────────────────────────────────────────────────────

MAX_QUEUE = 25


def queue_ids(team):
    if team is None:
        return []
    return [int(x) for x in _loads(getattr(team, "queue_json", None), [])
            if str(x).isdigit()]


def queue_add(session, draft, team, player):
    ids = queue_ids(team)
    if player.id in ids:
        raise DraftError(f"{player.name} is already in your queue.")
    if len(ids) >= MAX_QUEUE:
        raise DraftError(f"Your queue is full ({MAX_QUEUE}). Clear it with "
                         f"/dqueue clear.")
    ids.append(player.id)
    team.queue_json = _dumps(ids)
    return ids


def queue_clear(team):
    team.queue_json = _dumps([])
    return []


def queue_players(session, team):
    ids = queue_ids(team)
    if not ids:
        return []
    rows = {p.id: p for p in session.query(DraftPlayer)
            .filter(DraftPlayer.id.in_(ids)).all()}
    return [rows[i] for i in ids if i in rows]


def _drop_from_queues(session, draft_id, player_id):
    """Take a drafted player out of every team's wishlist.

    Without this, an auto-pick would keep trying a player who is gone, and an
    owner's queue would quietly fill up with names nobody can have.
    """
    for team in teams(session, draft_id):
        ids = queue_ids(team)
        if player_id in ids:
            team.queue_json = _dumps([i for i in ids if i != player_id])


# ──────────────────────────────────────────────────────────────────────
# Rendering — these build HTML and escape as they go
# ──────────────────────────────────────────────────────────────────────

RULE = "━━━━━━━━━━━━━━━━"


def player_line(player, *, with_tier=True):
    bits = [f"<b>{escape(player.name or '')}</b>", f"{player.rating} OVR"]
    if with_tier:
        bits.append(tier_badge(player.tier))
    return " · ".join(bits)


def bowling_line(player):
    """``"Right-arm Fast"`` — the way a commentator would say it.

    Joining the two columns with a bare separator gives "Right · Fast", which
    reads like two unrelated facts rather than one bowling style.
    """
    hand = (player.bowl_hand or "").strip()
    style = (player.bowl_style or "").strip()
    if style in ("", "-", "None", "N/A", "n/a"):
        return ""
    if hand.lower() in ("right", "left"):
        return f"{hand}-arm {style}"
    return " ".join(x for x in (hand, style) if x)


def player_detail(player, draft=None):
    bits = [escape(player.category or ""),
            f"{player_flag(player, draft)} {escape(player.country or '')}"]
    if player.bat_hand:
        bits.append(escape(f"{player.bat_hand}-hand bat"))
    bowling = bowling_line(player)
    if bowling:
        bits.append(escape(bowling))
    if player.icon_eligible:
        bits.append("⭐ Icon")
    return " · ".join(b for b in bits if b)


def render_squad(session, draft, team):
    """A team's squad, grouped by tier, with slot progress per tier."""
    rows = squad_sorted(session, draft, team.id)
    slots = tier_slots(session, draft.id, team.id)
    total_slots = sum(slots.values())
    out = [f"🏏 <b>{escape((team.name or '').upper())}</b>",
           f"Squad {len(rows)}/{total_slots}"]
    if team.owner_name:
        out.append(f"Owner: {escape(team.owner_name)}")
    out.append(RULE)

    by_tier = {}
    for player in rows:
        by_tier.setdefault(player.tier, []).append(player)
    for tier in tier_order(draft):
        if tier not in slots and tier not in by_tier:
            continue
        picked = by_tier.get(tier, [])
        out.append(f"{tier_badge(tier)} — {len(picked)}/{slots.get(tier, 0)}")
        for player in picked:
            flag = player_flag(player, draft)
            out.append(f"   • {escape(player.name or '')} "
                       f"<code>{player.rating}</code> {flag} "
                       f"{escape(player.category or '')}")
        if not picked:
            out.append("   <i>none yet</i>")

    out.append(RULE)
    overseas = overseas_count(session, team.id)
    cap = draft.max_overseas if draft.max_overseas is not None else 11
    out.append(f"{home_flag(draft)} {len(rows) - overseas} home · "
               f"{OVERSEAS_FLAG} {overseas}/{cap} overseas")
    gap = role_gap(session, draft, team.id)
    if gap:
        out.append("⚠️ Still needs " +
                   ", ".join(f"{n}× {escape(r)}" for r, n in sorted(gap.items())))
    return "\n".join(out)


def render_turn(session, draft, pick, *, mention=None):
    """The "you're on the clock" line, used after a pick and by the reminder."""
    if pick is None:
        return "🏁 <b>Draft complete</b> — every slot is filled."
    team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
    who = mention or escape(team.owner_name or team.name or "Owner")
    left = seconds_left(draft)
    clock = f" · {format_clock(left)} on the clock" if left is not None else ""
    return (f"⏭ <b>R{pick.round_no} P{pick.pick_no}</b> · {tier_badge(pick.tier)} · "
            f"<b>{escape((team.name if team else '').upper())}</b>\n"
            f"Next turn: {who}{clock}")


def render_pick(session, draft, pick, player, *, next_pick=None, next_mention=None):
    """The announcement posted (as the player's card caption) after a pick."""
    team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
    head = "⏱ <b>AUTO-PICK</b>\n" if pick.is_auto else ""
    lines = [
        f"{head}🏏 <b>{escape((team.name if team else '').upper())}</b>",
        f"R{pick.round_no} P{pick.pick_no} · {tier_badge(pick.tier)} slot",
        "",
        player_line(player),
        player_detail(player, draft),
    ]
    # Spending a Platinum slot on a Gold player is legal and irreversible, so it
    # is said out loud rather than left for the owner to notice three picks later.
    if tier_rank(draft, player.tier) != tier_rank(draft, pick.tier):
        lines.append(f"<i>⚠️ {escape(pick.tier)} slot used on a "
                     f"{escape(player.tier)} player.</i>")
    lines.append(RULE)

    rows = squad_sorted(session, draft, pick.team_id)
    slots = sum(tier_slots(session, draft.id, pick.team_id).values())
    lines.append(f"<b>Updated Squad</b> ({len(rows)}/{slots})")
    for row in rows:
        lines.append(f"{tier_emoji(row.tier)} {escape(row.tier)} — "
                     f"{escape(row.name or '')}")
    overseas = overseas_count(session, pick.team_id)
    cap = draft.max_overseas if draft.max_overseas is not None else 11
    lines.append("")
    lines.append(f"{home_flag(draft)} {len(rows) - overseas} home · "
                 f"{OVERSEAS_FLAG} {overseas}/{cap} overseas")
    lines.append(RULE)
    lines.append(render_turn(session, draft, next_pick, mention=next_mention))
    return "\n".join(lines)


def render_board(session, draft, *, limit=6, mention=None):
    """The live board: who is on the clock, and the last few picks."""
    lines = [f"🎯 <b>{escape(draft.name or 'Draft')}</b> · {status_label(draft)}"]
    pool_left = len(available(session, draft.id))
    done = picks_made(session, draft.id)
    total = done + len(pending_picks(session, draft.id))
    lines.append(f"Picks {done}/{total} · {pool_left} players left")
    lines.append(RULE)

    pick = current_pick(session, draft)
    if draft.status == STATUS_LIVE and pick is not None:
        lines.append(render_turn(session, draft, pick, mention=mention))
    elif draft.status == STATUS_PAUSED:
        lines.append("⏸ Paused — an admin will resume it.")
    elif draft.status == STATUS_COMPLETED:
        lines.append("🏁 Every slot is filled.")
    else:
        lines.append("Not started yet.")

    recent = (session.query(DraftPick)
              .filter(DraftPick.draft_id == draft.id, DraftPick.status != "pending")
              .order_by(DraftPick.overall_no.desc()).limit(limit).all())
    if recent:
        lines.append(RULE)
        lines.append("<b>Recent picks</b>")
        for row in reversed(recent):
            team = session.query(DraftTeam).filter(DraftTeam.id == row.team_id).first()
            name = "— skipped —"
            if row.draft_player_id:
                player = (session.query(DraftPlayer)
                          .filter(DraftPlayer.id == row.draft_player_id).first())
                if player is not None:
                    name = f"{player.name} ({player.rating})"
            mark = " ⏱" if row.is_auto else ""
            lines.append(f"R{row.round_no}P{row.pick_no} "
                         f"{escape(team.short_name or team.name if team else '?')} — "
                         f"{escape(name)}{mark}")
    return "\n".join(lines)


def render_order(session, draft, *, limit=12):
    """The next slots, so owners can see their turn coming."""
    upcoming = pending_picks(session, draft.id)[:limit]
    if not upcoming:
        return "🏁 No picks left — every slot is filled."
    lines = [f"📋 <b>Up next</b> — {escape(draft.name or 'Draft')}"]
    for pick in upcoming:
        team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
        lines.append(f"R{pick.round_no} P{pick.pick_no} · {tier_badge(pick.tier)} · "
                     f"{escape(team.name if team else '?')}")
    return "\n".join(lines)


def render_available(session, draft, *, tier=None, limit=15):
    """The best of what's left, optionally within one tier."""
    pool = available(session, draft.id, tier=tier)
    title = f"📦 <b>Available</b>{f' · {escape(tier)}' if tier else ''}"
    if not pool:
        return f"{title}\nNothing left."
    lines = [f"{title} — {len(pool)} player(s)"]
    for player in pool[:limit]:
        flag = player_flag(player, draft)
        lines.append(f"{tier_emoji(player.tier)} {escape(player.name or '')} "
                     f"<code>{player.rating}</code> {flag} "
                     f"{escape(player.category or '')}")
    if len(pool) > limit:
        lines.append(f"<i>…and {len(pool) - limit} more.</i>")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# The pool browser (/dsearch)
# ──────────────────────────────────────────────────────────────────────
#
# ``render_available`` answers "what are the best names left"; this answers
# "is *this* player still there, and if not, who has him" — the question an
# owner actually asks two rounds before their pick comes round.

SEARCH_PAGE = 8          # players per page: a phone screen, not a scroll
AVAIL_ANY, AVAIL_FREE, AVAIL_GONE = "", "y", "n"


def pool_flag(player, draft=None):
    """The player's **own** country flag, falling back to home/overseas.

    A list of ✈️ ✈️ ✈️ says only "not from here"; 🇦🇺 🇦🇫 🏴󠁧󠁢󠁥󠁮󠁧󠁿 says who they are,
    which is what an owner watching the overseas cap is really reading for.
    """
    key = country_key(getattr(player, "country", None))
    if key and (key in _COUNTRY_FLAG_OVERRIDES or key in _COUNTRY_ISO2):
        return country_flag(key)
    return player_flag(player, draft)


def search_pool(session, draft, *, tier=None, role=None, availability=AVAIL_ANY,
                query="", home=None):
    """The pool, filtered. Best rating first, available before taken.

    Every filter is optional and they stack. ``availability`` is ``AVAIL_FREE``
    for what is still there, ``AVAIL_GONE`` for what has gone, ``AVAIL_ANY``
    for both — the default, because a browser that hides the taken players
    can't answer "who got Kohli".
    """
    rows = (session.query(DraftPlayer)
            .filter(DraftPlayer.draft_id == draft.id).all())
    text = (query or "").strip().lower()
    out = []
    for player in rows:
        if tier and player.tier != tier:
            continue
        if role and normalise_category(player.category) != role:
            continue
        if availability == AVAIL_FREE and player.picked_by_team_id is not None:
            continue
        if availability == AVAIL_GONE and player.picked_by_team_id is None:
            continue
        if home is not None and bool(player.is_indian) is not home:
            continue
        if text and not _matches_text(player, text):
            continue
        out.append(player)
    out.sort(key=lambda p: (p.picked_by_team_id is not None,
                            tier_rank(draft, p.tier) if tier_rank(draft, p.tier)
                            is not None else 99,
                            -(p.rating or 0), (p.name or "").lower()))
    return out


def _matches_text(player, text):
    """Name substring, or the player's country by any of its spellings.

    ``/dsearch australia`` is a question people ask (who is left from where),
    and answering it "no player is called australia" would be obtuse.
    """
    if text in (player.name or "").lower():
        return True
    key = country_key(text)
    return bool(key) and key == country_key(player.country)


def parse_search_query(draft, raw):
    """Turn ``"plat bowler available kohli"`` into the filters it names.

    Owners type what they mean rather than filling in a form, so a tier, a
    role, an availability word and a home/overseas word are all recognised
    wherever they appear and everything left over is the name to match.
    Returns ``(tier, role, availability, home, name_text)``.
    """
    tier = role = home = None
    availability = AVAIL_ANY
    leftovers = []
    for token in (raw or "").split():
        folded = token.strip().lower()
        if not folded:
            continue
        matched_tier = _match_tier(draft, folded)
        if matched_tier and tier is None:
            tier = matched_tier
            continue
        matched_role = _match_role(folded)
        if matched_role and role is None:
            role = matched_role
            continue
        if folded in ("available", "free", "left", "open", "unsold", "🟢"):
            availability = AVAIL_FREE
            continue
        if folded in ("taken", "gone", "picked", "sold", "drafted", "🔴"):
            availability = AVAIL_GONE
            continue
        if folded in ("home", "domestic", "local"):
            home = True
            continue
        if folded in ("overseas", "foreign", "away"):
            home = False
            continue
        leftovers.append(token)
    return tier, role, availability, home, " ".join(leftovers).strip()


def _match_tier(draft, folded):
    """``"plat"`` → ``"Platinum"``. Prefixes count; three letters is enough."""
    for tier in tier_order(draft):
        name = tier.lower()
        if folded == name or (len(folded) >= 3 and name.startswith(folded)):
            return tier
    return None


def _match_role(folded):
    role = _CAT_MAP.get(folded, _CAT_MAP.get(folded.replace("-", " ")))
    if role:
        return role
    for known in _CATEGORIES:
        if folded and known.lower().startswith(folded) and len(folded) >= 3:
            return known
    return None


def render_search(session, draft, rows, *, tier=None, role=None,
                  availability=AVAIL_ANY, home=None, query="", page=0):
    """One page of the pool browser. Returns ``(text, page, pages)``.

    ``page`` comes back clamped, so a stale button on an old message can't ask
    for page 9 of a list that is now 2 pages long.
    """
    pages = max(1, -(-len(rows) // SEARCH_PAGE))
    page = max(0, min(page, pages - 1))
    window = rows[page * SEARCH_PAGE:(page + 1) * SEARCH_PAGE]

    bits = [escape(tier) if tier else "All tiers"]
    if role:
        bits.append(escape(role))
    if home is not None:
        bits.append("home" if home else "overseas")
    if availability == AVAIL_FREE:
        bits.append("🟢 available")
    elif availability == AVAIL_GONE:
        bits.append("🔴 taken")
    if query:
        bits.append(f"“{escape(query)}”")

    head = [f"🔎 <b>Pool</b> — {' · '.join(bits)}"]
    if not rows:
        head.append("<i>Nothing in the pool matches that.</i>")
        return "\n".join(head), 0, 1

    free = sum(1 for p in rows if p.picked_by_team_id is None)
    head.append(f"{len(rows)} player(s) · 🟢 {free} available · "
                f"🔴 {len(rows) - free} taken")
    head.append(RULE)

    holders = _team_names(session, draft)
    for player in window:
        taken = player.picked_by_team_id is not None
        line = (f"{'🔴' if taken else '🟢'} {tier_emoji(player.tier)} "
                f"<b>{escape(player.name or '')}</b> "
                f"<code>{player.rating}</code> {pool_flag(player, draft)} "
                f"{escape(player.category or '')}")
        if taken:
            line += f" — {escape(holders.get(player.picked_by_team_id, 'taken'))}"
        head.append(line)

    if pages > 1:
        head.append(RULE)
        head.append(f"<i>Page {page + 1} of {pages}</i>")
    return "\n".join(head), page, pages


def render_search_one(session, draft, player):
    """The full card for a single match — what one exact name is worth showing."""
    holders = _team_names(session, draft, short=False)
    taken = player.picked_by_team_id is not None
    lines = [f"{'🔴' if taken else '🟢'} <b>{escape(player.name or '')}</b>",
             player_line(player),
             player_detail(player, draft)]
    if taken:
        lines.append(f"Picked by <b>"
                     f"{escape(holders.get(player.picked_by_team_id, '—'))}</b>")
    else:
        lines.append("<i>Still available.</i>")
        current = current_pick(session, draft)
        if current is not None and draft.status == STATUS_LIVE:
            if tier_allows(draft, current.tier, player.tier):
                lines.append(f"Fits the {tier_badge(current.tier)} slot on the "
                             f"clock.")
            else:
                lines.append(f"<i>Too high for the {tier_badge(current.tier)} "
                             f"slot on the clock.</i>")
    return "\n".join(lines)


def _team_names(session, draft, *, short=True):
    """``{team id: name}`` — short in a list, in full when there is room."""
    return {team.id: ((team.short_name or team.name) if short
                      else (team.name or team.short_name)) or "—"
            for team in teams(session, draft.id)}


def tier_summary(session, draft):
    """``[(tier, available, total)]`` down the ladder, for the browser's header."""
    counts = {}
    for player in (session.query(DraftPlayer)
                   .filter(DraftPlayer.draft_id == draft.id).all()):
        total, free = counts.get(player.tier, (0, 0))
        counts[player.tier] = (total + 1,
                               free + (player.picked_by_team_id is None))
    out = []
    for tier in tier_order(draft):
        if tier in counts:
            total, free = counts[tier]
            out.append((tier, free, total))
    for tier, (total, free) in counts.items():      # tiers off the ladder
        if tier not in tier_order(draft):
            out.append((tier, free, total))
    return out


def render_teams(session, draft):
    lines = [f"👥 <b>{escape(draft.name or 'Draft')}</b> — the field"]
    for team in teams(session, draft.id):
        filled = len(squad(session, team.id))
        total = sum(tier_slots(session, draft.id, team.id).values())
        owner = escape(team.owner_name or "—")
        extra = len(co_owner_ids(team))
        co = f" (+{extra} co-owner{'s' if extra != 1 else ''})" if extra else ""
        lines.append(f"• <b>{escape(team.name or '')}</b> — {filled}/{total} · "
                     f"{owner}{co}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Publishing — the draft's squads become a playable Challenge League
# ──────────────────────────────────────────────────────────────────────

def _ensure_default_mode(session):
    """The Challenge Mode a published league hangs off.

    Mirrors ``admin._ensure_default_challenge_mode``; a service cannot import
    the Flask admin module, which builds an app object at import time.
    """
    mode = (session.query(ChallengeMode)
            .order_by(ChallengeMode.sort_order, ChallengeMode.id).first())
    if mode:
        return mode
    mode = ChallengeMode(name="League Battles",
                         description="Default league battle data", sort_order=0)
    session.add(mode)
    session.flush()
    return mode


def _details_json(player):
    """The ``details_json`` blob a ChallengePlayer carries.

    The keys up to ``is_overseas`` are the contract
    ``services.cipl_match.cp_to_player_dict`` reads — the engine takes every
    rating and handedness from here, not from the master ``players`` row — so
    they must match ``admin._challenge_player_details_from_source`` exactly. The
    draft-only keys after it are ignored by every existing reader and are kept
    so a published squad still knows which tier each player came from.
    """
    return json.dumps({
        "source_player_id": player.source_player_id,
        "name": player.name,
        "version": "Base",
        "country": player.country,
        "category": player.category,
        "role": player.category,
        "rating": player.rating,
        "bat_rating": player.bat_rating or 0,
        "bowl_rating": player.bowl_rating or 0,
        "bat_hand": player.bat_hand,
        "bowl_hand": player.bowl_hand,
        "bowl_style": player.bowl_style,
        "is_overseas": not bool(player.is_indian),
        "tier": player.tier,
        "icon_eligible": bool(player.icon_eligible),
        "gender": player.gender or "",
    }, separators=(",", ":"))


def publish_to_league(session, draft, *, league_name=None):
    """Write the drafted squads into a Challenge League. Returns the league.

    Idempotent: publishing twice re-syncs the same league rather than creating a
    second one, so a correction made with ``/dundo`` can simply be republished.
    """
    if draft.status != STATUS_COMPLETED:
        raise DraftError("Finish the draft first — only a completed draft can "
                         "be published.")
    picked = (session.query(DraftPlayer)
              .filter(DraftPlayer.draft_id == draft.id,
                      DraftPlayer.picked_by_team_id.isnot(None)).all())
    if not picked:
        raise DraftError("Nothing was drafted, so there is nothing to publish.")

    league = None
    if draft.league_id:
        league = (session.query(ChallengeLeague)
                  .filter(ChallengeLeague.id == draft.league_id).first())
    if league is None:
        mode = _ensure_default_mode(session)
        league = ChallengeLeague(mode_id=mode.id,
                                 name=(league_name or draft.name or "Draft League")[:120],
                                 short_code=_short_code(draft.name),
                                 is_active=True)
        session.add(league)
        session.flush()
    league.home_country = draft.home_country or None
    league.max_overseas = max(0, min(11, draft.max_overseas if draft.max_overseas
                                     is not None else 11))

    by_team = {}
    for player in picked:
        by_team.setdefault(player.picked_by_team_id, []).append(player)

    for team in teams(session, draft.id):
        ct = (session.query(ChallengeTeam)
              .filter(ChallengeTeam.league_id == league.id,
                      ChallengeTeam.name == (team.name or "")[:120]).first())
        if ct is None:
            ct = ChallengeTeam(league_id=league.id, name=(team.name or "")[:120],
                               short_name=team.short_name,
                               sort_order=team.sort_order)
            session.add(ct)
            session.flush()
        ct.logo_url = team.logo_url or ct.logo_url
        existing = {(cp.name or "").lower(): cp for cp in
                    session.query(ChallengePlayer)
                    .filter(ChallengePlayer.team_id == ct.id).all()}
        for order, player in enumerate(
                sorted(by_team.get(team.id, []),
                       key=lambda p: (tier_rank(draft, p.tier) or 99,
                                      -(p.rating or 0)))):
            key = (player.name or "").lower()
            cp = existing.get(key)
            if cp is None:
                cp = ChallengePlayer(team_id=ct.id, name=(player.name or "")[:150])
                session.add(cp)
            cp.source_player_id = player.source_player_id
            cp.details_json = _details_json(player)
            cp.is_overseas = not bool(player.is_indian)
            cp.sort_order = order

    draft.league_id = league.id
    draft.published_at = datetime.utcnow()
    session.flush()
    return league
