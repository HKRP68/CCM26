"""Dynamic retention — the franchise offers, the player decides.

Classic retention (``auction_service.retain`` with the slab ladder) prices a
player by the ORDER he is kept in, so a 96 costs what an 83 does. Dynamic mode
prices him by his RATING, and lets him say no:

* **Slots.** An ordered table — Elite 92–96 from ₹23 Cr, Premium 87–91 from
  ₹18 Cr, Core 83–86 from ₹12 Cr by default — every one editable, addable and
  removable. A player fills the highest open slot his rating reaches, and no
  offer may go below that slot's floor. The retention budget
  (``season.retention_max_spend_lakh``, ₹53 Cr by default) caps the total, so
  money can move between slots but never under a floor.
* **Demand.** Every rating has a public expected range (the Demand Meter) and
  a hidden Minimum Acceptable Price (MAP): the middle of that range, moved by a
  hidden personality, a loyalty discount for long service and a little jitter.
  All of it is seeded from ``(season, player)``, so walking away and coming
  back cannot reroll it.
* **Negotiation.** The owner offers; the player ACCEPTs (offer ≥ MAP),
  COUNTERs (close — he names his price, which the owner can take with a
  button) or REJECTs. Counters and rejections each spend one of the chances
  (three by default); spending the last sends him into the auction, and his
  franchise cannot approach him again. An insulting lowball makes him dig in.

Every signing goes through ``auction_service.retain`` so the squad, overseas,
role, purse and reachability rules — and the ledger — are exactly the ones
classic retention uses.
"""

import copy
import json
import math
import random
import re
from datetime import datetime

from models import (AuctionFranchise, AuctionLot, AuctionRetentionOffer,
                    AuctionRetentionTalk, AuctionSeason)
from services import auction_service as A
from services.auction_service import AuctionError

MODE_CLASSIC = "classic"
MODE_DYNAMIC = "dynamic"
MODES = (MODE_CLASSIC, MODE_DYNAMIC)

TALK_OPEN = "open"
TALK_SIGNED = "signed"
TALK_FAILED = "failed"
TALK_WITHDRAWN = "withdrawn"

ACCEPT = "accept"
COUNTER = "counter"
REJECT = "reject"
WALKOUT = "walkout"

# Where a player who walks out of talks is filed in the pool.
WALKOUT_SET = "🔨 Walked out"

MAX_SLOTS = 8

PRESETS = {
    3: [
        {"key": "elite", "label": "Elite", "emoji": "🥇",
         "min_rating": 92, "max_rating": 96, "floor_lakh": 2300},
        {"key": "premium", "label": "Premium", "emoji": "🥈",
         "min_rating": 87, "max_rating": 91, "floor_lakh": 1800},
        {"key": "core", "label": "Core", "emoji": "🥉",
         "min_rating": 83, "max_rating": 86, "floor_lakh": 1200},
    ],
    2: [
        {"key": "elite", "label": "Elite", "emoji": "🥇",
         "min_rating": 92, "max_rating": 96, "floor_lakh": 2300},
        {"key": "flexible", "label": "Flexible", "emoji": "🥈",
         "min_rating": 0, "max_rating": 999, "floor_lakh": 1800},
    ],
}

DEFAULT_BUDGET_LAKH = 5300

PERSONALITY_ORDER = ("demanding", "balanced", "loyal", "money", "superstar")
PERSONALITY_INFO = {
    "demanding": ("🦁", "Demanding"),
    "balanced": ("⚖️", "Balanced"),
    "loyal": ("🤝", "Loyal"),
    "money": ("💰", "Money-Minded"),
    "superstar": ("⭐", "Superstar"),
}

DEFAULT_RULES = {
    "chances": 3,
    # [rating, low_lakh, high_lakh] — interpolated between, clamped outside.
    "demand_curve": [[83, 1300, 1700], [86, 1600, 2000], [89, 2000, 2400],
                     [92, 2400, 2800], [96, 2800, 3200]],
    # [minimum rating, label], highest first.
    "meter": [[94, "🔴 VERY HIGH"], [89, "🟠 HIGH"], [85, "🟡 MODERATE"],
              [0, "🟢 FAIR"]],
    "personalities": {
        "demanding": {"factor": 1.12, "weight": 25, "enabled": True},
        "balanced": {"factor": 1.00, "weight": 35, "enabled": True},
        "loyal": {"factor": 0.92, "weight": 20, "enabled": True},
        "money": {"factor": 1.06, "weight": 15, "enabled": True},
        "superstar": {"factor": 1.15, "weight": 30, "enabled": True},
    },
    "superstar_min_rating": 93,
    "loyal_per_season_pct": 3,
    "loyal_max_pct": 12,
    "counter_pct": 90,
    "lowball_pct": 75,
    "lowball_penalty_pct": 5,
    "jitter_pct": 4,
    "reveal_personality": False,
}

# What the player says. Picked deterministically per talk and attempt, so a
# redrawn card never changes its words.
LINES = {
    REJECT: {
        "demanding": ["I know what I'm worth. This isn't it.",
                      "You'll have to do much better than that.",
                      "That number is an insult to my record."],
        "balanced": ["I don't think this offer reflects my value.",
                     "Not quite there. Try again.",
                     "I want a better deal."],
        "loyal": ["I love this club, but I need a fairer offer.",
                  "I want to stay — meet me halfway.",
                  "This shirt means a lot. The number should too."],
        "money": ["Show me the money.",
                  "Salary talks. This doesn't.",
                  "My agent laughed at that."],
        "superstar": ["Do you know who you're talking to?",
                      "Stars don't sign for that.",
                      "Come back with a superstar offer."],
    },
    COUNTER: {
        "demanding": ["Close. Pay me {ask} and it's done.",
                      "{ask}. Not a rupee less."],
        "balanced": ["We're nearly there — make it {ask}.",
                     "{ask} and I'll sign today."],
        "loyal": ["For this club? {ask} and I'm yours.",
                  "Make it {ask} and I'm staying home."],
        "money": ["{ask}. Final."],
        "superstar": ["{ask} — that's the superstar rate.",
                      "Get to {ask} and we'll talk legacy."],
    },
    ACCEPT: {
        "demanding": ["Finally, respect. Deal!"],
        "balanced": ["Deal! I'm retained."],
        "loyal": ["Home is where the heart is. Deal!"],
        "money": ["Now we're talking. Sign it."],
        "superstar": ["The star stays. Deal!"],
    },
    WALKOUT: {
        "demanding": ["I'm done here. See you at the auction."],
        "balanced": ["We couldn't agree. I'll test the market."],
        "loyal": ["It hurts, but I have to move on."],
        "money": ["Someone out there will pay me. Bye."],
        "superstar": ["The auction will show you what I'm worth."],
    },
}
LOWBALL_LINE = "😤 That was insulting — he's digging in."


# ──────────────────────────────────────────────────────────────────────
# Mode
# ──────────────────────────────────────────────────────────────────────

def mode(season):
    raw = (getattr(season, "retention_mode", None) or "").strip().lower()
    return MODE_DYNAMIC if raw == MODE_DYNAMIC else MODE_CLASSIC


def is_dynamic(season):
    return mode(season) == MODE_DYNAMIC


def _anything_kept(session, season):
    if (session.query(AuctionLot.id)
            .filter(AuctionLot.season_id == season.id,
                    AuctionLot.status == A.LOT_SOLD,
                    AuctionLot.acquisition == A.ACQ_RETAINED).first()):
        return True
    if (session.query(AuctionRetentionTalk.id)
            .filter(AuctionRetentionTalk.season_id == season.id,
                    AuctionRetentionTalk.status == TALK_OPEN).first()):
        return True
    return bool(session.query(AuctionRetentionOffer.id)
                .filter(AuctionRetentionOffer.season_id == season.id,
                        AuctionRetentionOffer.status == A.OFFER_PENDING).first())


def set_mode(session, season, new_mode, *, by_tg_id=None):
    """Switch the season between classic and dynamic retention.

    Refused once anybody is kept, or an offer or talk is waiting: a season
    half-retained under one system and half under the other has no single
    rule a franchise could be held to. Release them first.
    """
    new_mode = (new_mode or "").strip().lower()
    if new_mode not in MODES:
        raise AuctionError("Retention mode is “classic” or “dynamic”.")
    if new_mode == mode(season):
        return season
    if season.status != A.STATUS_SETUP:
        raise AuctionError("Retention mode can only change before the "
                           "auction opens.")
    if _anything_kept(session, season):
        raise AuctionError("Players are already retained (or an offer or "
                           "negotiation is waiting) under the current system. "
                           "Release them with /aunretain and clear the offers "
                           "first, then switch.")
    season.retention_mode = new_mode
    if new_mode == MODE_DYNAMIC:
        season.max_retentions = len(slots(season))
        if season.retention_max_spend_lakh is None:
            season.retention_max_spend_lakh = DEFAULT_BUDGET_LAKH
    session.flush()
    A.log_event(session, season, "retention_mode",
                "🔁 Retention is now <b>"
                + ("dynamic — owners negotiate, players decide"
                   if new_mode == MODE_DYNAMIC else "classic — the slab ladder")
                + "</b>.", by_tg_id=by_tg_id, by_admin=True)
    return season


# ──────────────────────────────────────────────────────────────────────
# Slots
# ──────────────────────────────────────────────────────────────────────

def preset(n):
    if int(n) not in PRESETS:
        raise AuctionError("Presets are 3 (Elite / Premium / Core) and "
                           "2 (Elite / Flexible).")
    return copy.deepcopy(PRESETS[int(n)])


def _slug(label):
    key = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower()).strip("-")
    return key[:24] or "slot"


def validate_slots(rows, budget_lakh=None):
    """Clean a slot list, or raise naming the first thing wrong with it."""
    if not isinstance(rows, list) or not rows:
        raise AuctionError("Retention needs at least one slot.")
    if len(rows) > MAX_SLOTS:
        raise AuctionError(f"At most {MAX_SLOTS} retention slots.")
    cleaned, labels, keys = [], set(), set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise AuctionError(f"Slot {index} is not a slot.")
        label = str(row.get("label") or "").strip()[:24]
        if not label:
            raise AuctionError(f"Slot {index} has no name.")
        if label.lower() in labels:
            raise AuctionError(f"Two slots are called “{label}”.")
        labels.add(label.lower())
        low = A._as_int(row.get("min_rating"), -1)
        high = A._as_int(row.get("max_rating"), -1)
        if low < 0 or high < 0:
            raise AuctionError(f"Slot “{label}” needs a rating range.")
        if low > high:
            raise AuctionError(f"Slot “{label}”: the range {low}–{high} runs "
                               f"backwards.")
        floor = A._as_int(row.get("floor_lakh"), -1)
        if floor < 0:
            raise AuctionError(f"Slot “{label}” needs a starting price of "
                               f"zero or more.")
        key = _slug(row.get("key") or label)
        base, n = key, 2
        while key in keys:
            key = f"{base[:21]}-{n}"
            n += 1
        keys.add(key)
        emoji = str(row.get("emoji") or "").strip()[:8] or "🔒"
        cleaned.append({"key": key, "label": label, "emoji": emoji,
                        "min_rating": low, "max_rating": high,
                        "floor_lakh": floor})
    if budget_lakh is not None:
        floors = sum(s["floor_lakh"] for s in cleaned)
        if floors > int(budget_lakh):
            raise AuctionError(
                f"The slots' starting prices add up to "
                f"{A.render_money(floors)}, more than the "
                f"{A.render_money(int(budget_lakh))} retention budget.")
    return cleaned


def slots(season):
    raw = A._loads(getattr(season, "retention_slots_json", None), [])
    if raw:
        try:
            return validate_slots(raw)
        except AuctionError:
            pass
    return preset(3)


def budget(season):
    cap = getattr(season, "retention_max_spend_lakh", None)
    return None if cap is None else int(cap)


def _used_slot_keys(session, season):
    """Slot keys a signed retention or an open talk is using, season-wide."""
    used = {k for (k,) in session.query(AuctionLot.retention_slot)
            .filter(AuctionLot.season_id == season.id,
                    AuctionLot.status == A.LOT_SOLD,
                    AuctionLot.acquisition == A.ACQ_RETAINED,
                    AuctionLot.retention_slot.isnot(None)).all()}
    used |= {k for (k,) in session.query(AuctionRetentionTalk.slot_key)
             .filter(AuctionRetentionTalk.season_id == season.id,
                     AuctionRetentionTalk.status == TALK_OPEN).all()}
    return used


def save_slots(session, season, rows, *, by_tg_id=None):
    """Replace the slot table. A slot in use cannot vanish or change shape."""
    if A.retention_locked(season) or season.status != A.STATUS_SETUP:
        raise AuctionError("Retention is closed — its slots are final.")
    cleaned = validate_slots(rows, budget(season))
    before = {s["key"]: s for s in slots(season)}
    after = {s["key"]: s for s in cleaned}
    for key in _used_slot_keys(session, season):
        old = before.get(key)
        new = after.get(key)
        if old is None:
            continue
        if new is None:
            raise AuctionError(f"The {old['label']} slot is already filled "
                               f"or under negotiation — it cannot be removed.")
        for field in ("min_rating", "max_rating", "floor_lakh"):
            if old[field] != new[field]:
                raise AuctionError(
                    f"The {old['label']} slot is already filled or under "
                    f"negotiation — its range and price are fixed now.")
    season.retention_slots_json = json.dumps(cleaned, ensure_ascii=False,
                                             separators=(",", ":"))
    if is_dynamic(season):
        season.max_retentions = len(cleaned)
    session.flush()
    return cleaned


def find_slot(season, ref):
    """A slot by 1-based number, key or label."""
    table = slots(season)
    text = str(ref or "").strip()
    if text.isdigit():
        n = int(text)
        if 1 <= n <= len(table):
            return n - 1, table[n - 1]
        raise AuctionError(f"There is no slot {n} — there are {len(table)}.")
    lowered = text.lower()
    for index, slot in enumerate(table):
        if lowered in (slot["key"], slot["label"].lower()):
            return index, slot
    raise AuctionError(f"No slot called “{text}”. /aretslot lists them.")


def add_slot(session, season, label, low, high, floor_lakh, emoji=None, *,
             position=None, by_tg_id=None):
    table = slots(season)
    row = {"label": label, "min_rating": low, "max_rating": high,
           "floor_lakh": floor_lakh, "emoji": emoji or "🔒"}
    if position is None:
        table.append(row)
    else:
        table.insert(max(0, int(position) - 1), row)
    return save_slots(session, season, table, by_tg_id=by_tg_id)


def edit_slot(session, season, ref, *, label=None, low=None, high=None,
              floor_lakh=None, emoji=None, by_tg_id=None):
    table = slots(season)
    index, slot = find_slot(season, ref)
    slot = dict(slot)
    if label is not None:
        slot["label"] = label
    if low is not None:
        slot["min_rating"] = low
    if high is not None:
        slot["max_rating"] = high
    if floor_lakh is not None:
        slot["floor_lakh"] = floor_lakh
    if emoji is not None:
        slot["emoji"] = emoji
    table[index] = slot
    return save_slots(session, season, table, by_tg_id=by_tg_id)


def remove_slot(session, season, ref, *, by_tg_id=None):
    table = slots(season)
    index, _slot = find_slot(season, ref)
    del table[index]
    return save_slots(session, season, table, by_tg_id=by_tg_id)


def move_slot(session, season, ref, position, *, by_tg_id=None):
    table = slots(season)
    index, slot = find_slot(season, ref)
    del table[index]
    position = max(1, min(len(table) + 1, A._as_int(position, 1)))
    table.insert(position - 1, slot)
    return save_slots(session, season, table, by_tg_id=by_tg_id)


# ──────────────────────────────────────────────────────────────────────
# Rules
# ──────────────────────────────────────────────────────────────────────

def _num(value, name, low, high, *, integer=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AuctionError(f"“{name}” must be a number.")
    if not (low <= number <= high):
        raise AuctionError(f"“{name}” must be between {low:g} and {high:g}.")
    return int(round(number)) if integer else round(number, 4)


def validate_rules(raw):
    """Defaults, overlaid with whatever ``raw`` sets, checked field by field."""
    rules = copy.deepcopy(DEFAULT_RULES)
    raw = raw if isinstance(raw, dict) else {}
    for key in ("chances", "superstar_min_rating", "loyal_per_season_pct",
                "loyal_max_pct", "counter_pct", "lowball_pct",
                "lowball_penalty_pct", "jitter_pct"):
        if key in raw and raw[key] is not None:
            bounds = {"chances": (1, 10), "superstar_min_rating": (0, 999),
                      "loyal_per_season_pct": (0, 50),
                      "loyal_max_pct": (0, 90), "counter_pct": (1, 100),
                      "lowball_pct": (0, 100),
                      "lowball_penalty_pct": (0, 100),
                      "jitter_pct": (0, 50)}[key]
            rules[key] = _num(raw[key], key, *bounds, integer=True)
    if "reveal_personality" in raw:
        rules["reveal_personality"] = bool(raw["reveal_personality"])
    if raw.get("demand_curve") is not None:
        curve = []
        for point in raw["demand_curve"]:
            if not isinstance(point, (list, tuple)) or len(point) != 3:
                raise AuctionError("Each demand point is rating, low, high.")
            rating = _num(point[0], "rating", 0, 999, integer=True)
            low = _num(point[1], "low", 0, 10 ** 7, integer=True)
            high = _num(point[2], "high", 0, 10 ** 7, integer=True)
            if low > high:
                raise AuctionError(f"Demand at {rating}: the low end is above "
                                   f"the high end.")
            curve.append([rating, low, high])
        if not curve:
            raise AuctionError("The demand curve needs at least one point.")
        curve.sort(key=lambda p: p[0])
        if len({p[0] for p in curve}) != len(curve):
            raise AuctionError("The demand curve lists one rating twice.")
        rules["demand_curve"] = curve
    if raw.get("meter") is not None:
        meter = []
        for row in raw["meter"]:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise AuctionError("Each meter band is minimum rating, label.")
            meter.append([_num(row[0], "meter rating", 0, 999, integer=True),
                          str(row[1]).strip()[:24] or "—"])
        meter.sort(key=lambda r: -r[0])
        rules["meter"] = meter or rules["meter"]
    if isinstance(raw.get("personalities"), dict):
        for name, conf in raw["personalities"].items():
            if name not in rules["personalities"] or not isinstance(conf, dict):
                continue
            target = rules["personalities"][name]
            if conf.get("factor") is not None:
                target["factor"] = _num(conf["factor"], f"{name} factor",
                                        0.3, 3.0)
            if conf.get("weight") is not None:
                target["weight"] = _num(conf["weight"], f"{name} weight",
                                        0, 1000, integer=True)
            if "enabled" in conf:
                target["enabled"] = bool(conf["enabled"])
    return rules


def parse_demand_curve(raw):
    """``83=13-17 | 89=20-24`` (crore) → ``[[83, 1300, 1700], ...]`` in lakh."""
    curve = []
    for part in (raw or "").replace("\n", "|").split("|"):
        part = part.strip()
        if not part:
            continue
        rating, sep, span = part.partition("=")
        low, dash, high = span.partition("-")
        if not sep or not dash or not rating.strip().isdigit():
            raise AuctionError(f"“{part}” should look like 89=20-24.")
        curve.append([int(rating), A.parse_amount(low), A.parse_amount(high)])
    if not curve:
        raise AuctionError("Give at least one point, like 89=20-24.")
    return curve


def format_demand_curve(season_or_rules):
    """The inverse of ``parse_demand_curve``, for a form to pre-fill."""
    table = (season_or_rules if isinstance(season_or_rules, dict)
             else rules(season_or_rules))

    def crore(lakh):
        return f"{lakh / 100:g}"
    return " | ".join(f"{r}={crore(lo)}-{crore(hi)}"
                      for r, lo, hi in table["demand_curve"])


def rules(season):
    raw = A._loads(getattr(season, "retention_rules_json", None), {})
    try:
        return validate_rules(raw)
    except AuctionError:
        return copy.deepcopy(DEFAULT_RULES)


def save_rules(session, season, new_rules):
    if A.retention_locked(season) or season.status != A.STATUS_SETUP:
        raise AuctionError("Retention is closed — its rules are final.")
    cleaned = validate_rules(new_rules)
    season.retention_rules_json = json.dumps(cleaned, ensure_ascii=False,
                                             separators=(",", ":"))
    session.flush()
    return cleaned


def update_rules(session, season, **changes):
    """Merge ``changes`` into the current rules and save."""
    current = rules(season)
    for key, value in changes.items():
        if key == "personalities":
            for name, conf in value.items():
                current["personalities"].setdefault(name, {}).update(conf)
        else:
            current[key] = value
    return save_rules(session, season, current)


def reset_rules(session, season):
    season.retention_rules_json = None
    session.flush()
    return rules(season)


def set_budget(session, season, lakh):
    lakh = int(lakh)
    if lakh <= 0:
        raise AuctionError("The retention budget must be above zero.")
    floors = sum(s["floor_lakh"] for s in slots(season))
    if is_dynamic(season) and floors > lakh:
        raise AuctionError(f"The slots' starting prices already add up to "
                           f"{A.render_money(floors)} — the budget cannot be "
                           f"lower than that.")
    season.retention_max_spend_lakh = lakh
    session.flush()
    return lakh


# ──────────────────────────────────────────────────────────────────────
# Demand
# ──────────────────────────────────────────────────────────────────────

def demand_range(season_or_rules, rating):
    """``(low, high)`` in lakh for this rating — the public Demand Meter."""
    table = (season_or_rules if isinstance(season_or_rules, dict)
             else rules(season_or_rules))
    curve = table["demand_curve"]
    rating = A._as_int(rating, 0)
    if rating <= curve[0][0]:
        return curve[0][1], curve[0][2]
    if rating >= curve[-1][0]:
        return curve[-1][1], curve[-1][2]
    for (r0, l0, h0), (r1, l1, h1) in zip(curve, curve[1:]):
        if r0 <= rating <= r1:
            t = (rating - r0) / float(r1 - r0) if r1 != r0 else 0.0
            low = l0 + (l1 - l0) * t
            high = h0 + (h1 - h0) * t
            return _round_to(low, 25), _round_to(high, 25)
    return curve[-1][1], curve[-1][2]


def demand_meter(season_or_rules, rating):
    table = (season_or_rules if isinstance(season_or_rules, dict)
             else rules(season_or_rules))
    rating = A._as_int(rating, 0)
    for minimum, label in table["meter"]:
        if rating >= minimum:
            return label
    return table["meter"][-1][1]


def _round_to(value, step):
    return int(round(float(value) / step) * step)


def _ceil_to(value, step):
    return int(math.ceil(float(value) / step) * step)


def _rng(season_id, player_id, salt=""):
    return random.Random(f"ccm-retention:{season_id}:{player_id}:{salt}")


def personality_for(season, player_id, rating, table=None):
    """The player's hidden temperament. Deterministic per season and player."""
    table = table or rules(season)
    rating = A._as_int(rating, 0)
    pool = []
    for name in PERSONALITY_ORDER:
        conf = table["personalities"].get(name) or {}
        if not conf.get("enabled", True) or int(conf.get("weight", 0)) <= 0:
            continue
        if name == "superstar" and rating < table["superstar_min_rating"]:
            continue
        pool.append((name, int(conf["weight"])))
    if not pool:
        return "balanced"
    total = sum(w for _n, w in pool)
    pick = _rng(season.id, player_id, "personality").uniform(0, total)
    running = 0
    for name, weight in pool:
        running += weight
        if pick <= running:
            return name
    return pool[-1][0]


def tenure(session, season, franchise, player_id):
    """Seasons this franchise (under any name) has held this player in a row.

    Walks back through ``previous_season_id`` with the franchise's
    ``carried_from_id``. A hand-linked season has neither, so it falls back to
    last season's league: one season if he was this side's there.
    """
    count = 0
    here_season, here_franchise = season, franchise
    for _ in range(12):
        prev_id = getattr(here_season, "previous_season_id", None)
        carried = getattr(here_franchise, "carried_from_id", None)
        if not prev_id or not carried:
            break
        held = (session.query(AuctionLot.id)
                .filter(AuctionLot.season_id == int(prev_id),
                        AuctionLot.player_id == int(player_id),
                        AuctionLot.status == A.LOT_SOLD,
                        AuctionLot.sold_to_id == int(carried)).first())
        if not held:
            break
        count += 1
        here_season = (session.query(AuctionSeason)
                       .filter(AuctionSeason.id == int(prev_id)).first())
        here_franchise = (session.query(AuctionFranchise)
                          .filter(AuctionFranchise.id == int(carried)).first())
        if here_season is None or here_franchise is None:
            break
    if count == 0:
        try:
            holder = A.previous_squad_map(session, season).get(int(player_id))
        except Exception:
            holder = None
        if holder is not None and holder.id == franchise.id:
            count = 1
    return count


def minimum_acceptable(season, player_id, rating, personality, seasons_held,
                       floor_lakh, table=None):
    """The hidden MAP, in lakh."""
    table = table or rules(season)
    low, high = demand_range(table, rating)
    mid = (low + high) / 2.0
    factor = float(table["personalities"].get(personality, {})
                   .get("factor", 1.0))
    discount = 0.0
    if personality == "loyal":
        discount = min(table["loyal_max_pct"],
                       table["loyal_per_season_pct"] * int(seasons_held)) / 100.0
    jitter = table["jitter_pct"] / 100.0
    wobble = _rng(season.id, player_id, "map").uniform(-jitter, jitter)
    value = mid * factor * (1.0 - discount) * (1.0 + wobble)
    return max(int(floor_lakh), _round_to(value, 25))


# ──────────────────────────────────────────────────────────────────────
# Which slot, and whether an offer is allowed
# ──────────────────────────────────────────────────────────────────────

def filled_slots(session, franchise):
    """``{slot_key: lot}`` for this franchise's signed dynamic retentions."""
    rows = (session.query(AuctionLot)
            .filter(AuctionLot.sold_to_id == franchise.id,
                    AuctionLot.status == A.LOT_SOLD,
                    AuctionLot.acquisition == A.ACQ_RETAINED).all())
    return {lot.retention_slot: lot for lot in rows if lot.retention_slot}


def open_talk_for(session, season, franchise):
    return (session.query(AuctionRetentionTalk)
            .filter(AuctionRetentionTalk.season_id == season.id,
                    AuctionRetentionTalk.franchise_id == franchise.id,
                    AuctionRetentionTalk.status == TALK_OPEN).first())


def open_slots(session, season, franchise, *, ignore_talk=None):
    """This franchise's slots nobody fills and no other open talk holds."""
    taken = set(filled_slots(session, franchise))
    talk = open_talk_for(session, season, franchise)
    if talk is not None and (ignore_talk is None or talk.id != ignore_talk.id):
        taken.add(talk.slot_key)
    return [s for s in slots(season) if s["key"] not in taken]


def slot_for(session, season, franchise, rating, *, ignore_talk=None):
    """The slot a player of this rating would fill, or raise saying why none.

    The first open slot whose range holds the rating. Failing that, the
    first open slot entirely BELOW it — a 94 may take the Premium slot once
    Elite is gone — because a franchise with a spare slot should be able to
    use it on a better player. His demand still comes from his rating, so
    that is no discount.
    """
    rating = A._as_int(rating, 0)
    free = open_slots(session, season, franchise, ignore_talk=ignore_talk)
    if not free:
        raise AuctionError(f"{franchise.name} has no retention slot left.")
    for slot in free:
        if slot["min_rating"] <= rating <= slot["max_rating"]:
            return slot
    for slot in free:
        if slot["max_rating"] < rating:
            return slot
    names = ", ".join(f"{s['label']} {s['min_rating']}–{s['max_rating']}"
                      for s in free)
    raise AuctionError(f"No open slot takes a {rating} OVR player — "
                       f"{franchise.name} has {names}.")


def budget_left(session, season, franchise):
    cap = budget(season)
    if cap is None:
        return None
    return cap - A.retention_spent(session, franchise.id)


def check_price(session, season, franchise, slot, price):
    """The floor and the budget. Every refusal names the number that works."""
    symbol = season.currency_label or "₹"
    if price < int(slot["floor_lakh"]):
        raise AuctionError(
            f"The {slot['label']} slot starts at "
            f"{A.render_money(slot['floor_lakh'], symbol)} — offer at least "
            f"that.")
    left = budget_left(session, season, franchise)
    if left is not None and price > left:
        raise AuctionError(
            f"{franchise.name} has {A.render_money(max(0, left), symbol)} of "
            f"its retention budget left — "
            f"{A.render_money(price, symbol)} is over it.")


def check_signing(session, season, franchise, player, price, slot_key=None):
    """``retain()``'s dynamic-mode gate. Returns the slot key to record."""
    if slot_key:
        _index, slot = find_slot(season, slot_key)
        if slot["key"] in filled_slots(session, franchise):
            raise AuctionError(f"{franchise.name}'s {slot['label']} slot is "
                               f"already filled.")
    else:
        slot = slot_for(session, season, franchise, player.rating)
    check_price(session, season, franchise, slot, price)
    return slot["key"]


# ──────────────────────────────────────────────────────────────────────
# Talks
# ──────────────────────────────────────────────────────────────────────

def _require_open_window(season):
    if season.status != A.STATUS_SETUP:
        raise AuctionError("Retention happens before the auction opens.")
    if A.retention_locked(season):
        raise AuctionError("Retention is closed for this auction.")
    left = A.retention_seconds_left(season)
    if left is not None and left <= 0:
        raise AuctionError("The retention deadline has passed.")
    if not is_dynamic(season):
        raise AuctionError("This auction uses classic retention — an admin "
                           "offers it with /aretain.")


def talk(session, talk_id):
    return (session.query(AuctionRetentionTalk)
            .filter(AuctionRetentionTalk.id == int(talk_id)).first())


def open_talks(session, season_id):
    return (session.query(AuctionRetentionTalk)
            .filter(AuctionRetentionTalk.season_id == season_id,
                    AuctionRetentionTalk.status == TALK_OPEN)
            .order_by(AuctionRetentionTalk.id.asc()).all())


def talks_for(session, season_id, franchise_id=None):
    query = (session.query(AuctionRetentionTalk)
             .filter(AuctionRetentionTalk.season_id == season_id))
    if franchise_id is not None:
        query = query.filter(AuctionRetentionTalk.franchise_id == franchise_id)
    return query.order_by(AuctionRetentionTalk.id.asc()).all()


def chances_left(season, t):
    return max(0, rules(season)["chances"] - int(t.attempts or 0))


def start_talk(session, season, franchise, player, *, by_tg_id=None,
               chat_id=None):
    """Open a negotiation, or return the one already open with this player."""
    _require_open_window(season)
    existing = open_talk_for(session, season, franchise)
    if existing is not None:
        if existing.player_id == player.id:
            return existing
        raise AuctionError(f"{franchise.name} is already negotiating with "
                           f"{existing.player_name}. Finish or walk away from "
                           f"that first.")
    if int(franchise.retained_count or 0) >= len(slots(season)):
        raise AuctionError(f"{franchise.name} has filled every retention slot.")
    failed = (session.query(AuctionRetentionTalk)
              .filter(AuctionRetentionTalk.season_id == season.id,
                      AuctionRetentionTalk.player_id == player.id,
                      AuctionRetentionTalk.status == TALK_FAILED).first())
    if failed is not None:
        raise AuctionError(f"{player.name} walked out of retention talks — "
                           f"he is going to the auction.")
    other = (session.query(AuctionRetentionTalk)
             .filter(AuctionRetentionTalk.season_id == season.id,
                     AuctionRetentionTalk.player_id == player.id,
                     AuctionRetentionTalk.status == TALK_OPEN).first())
    if other is not None:
        raise AuctionError(f"{player.name} is already in talks with another "
                           f"franchise.")
    lot = (session.query(AuctionLot)
           .filter(AuctionLot.season_id == season.id,
                   AuctionLot.player_id == player.id).first())
    if lot is not None and lot.status != A.LOT_QUEUED:
        raise AuctionError(f"{lot.name} is already {lot.status} in this "
                           f"auction.")
    # The same eligibility classic retention applies, checked up front so an
    # owner is not left negotiating for somebody they could never sign.
    rating = A._as_int(player.rating, 0)
    low = getattr(season, "retention_min_rating", None)
    high = getattr(season, "retention_max_rating", None)
    if low is not None and rating < int(low):
        raise AuctionError(f"{player.name} is rated {rating}; this auction "
                           f"only allows retaining {int(low)} and above.")
    if high is not None and rating > int(high):
        raise AuctionError(f"{player.name} is rated {rating}; this auction "
                           f"only allows retaining {int(high)} and below.")
    allowed = A.retention_categories(season)
    if allowed and (player.category or "") not in allowed:
        raise AuctionError(f"{player.name} is a {player.category}; this "
                           f"auction only allows retaining "
                           f"{', '.join(allowed)}.")

    slot = slot_for(session, season, franchise, rating)
    left = budget_left(session, season, franchise)
    if left is not None and left < slot["floor_lakh"]:
        raise AuctionError(
            f"{franchise.name} has {A.render_money(max(0, left), season.currency_label)} "
            f"of its retention budget left — not enough for the "
            f"{slot['label']} slot's {A.render_money(slot['floor_lakh'], season.currency_label)} "
            f"starting price.")
    table = rules(season)
    temperament = personality_for(season, player.id, rating, table)
    held = tenure(session, season, franchise, player.id)
    target = minimum_acceptable(season, player.id, rating, temperament, held,
                                slot["floor_lakh"], table)
    t = AuctionRetentionTalk(
        season_id=season.id, franchise_id=franchise.id, player_id=player.id,
        player_name=(player.name or "")[:150], rating=rating,
        slot_key=slot["key"], status=TALK_OPEN, attempts=0,
        map_lakh=target, personality=temperament, tenure=held,
        opened_by_tg_id=by_tg_id, chat_id=chat_id)
    from sqlalchemy.exc import IntegrityError
    try:
        with session.begin_nested():
            session.add(t)
            session.flush()
    except IntegrityError:
        raise AuctionError(f"{player.name} is already in talks with another "
                           f"franchise.")
    return t


def _line(t, verdict, **fmt):
    options = LINES[verdict].get(t.personality) or LINES[verdict]["balanced"]
    pick = _rng(t.id, t.attempts, verdict).choice(options)
    return pick.format(**fmt)


def _slot_of(season, t):
    for slot in slots(season):
        if slot["key"] == t.slot_key:
            return slot
    raise AuctionError("That negotiation's slot no longer exists.")


def _check_actor(session, t, tg_id, *, admin=False):
    franchise = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == t.franchise_id).first())
    if franchise is None:
        raise AuctionError("That franchise is no longer in the auction.")
    if not admin and not A.may_bid_for(franchise, tg_id):
        raise AuctionError(f"Only {franchise.name}'s owner or a co-owner can "
                           f"negotiate for it.")
    return franchise


def _sign(session, season, franchise, t, price, *, by_tg_id=None, now=None):
    from models import Player
    player = session.query(Player).filter(Player.id == t.player_id).first()
    if player is None:
        raise AuctionError(f"{t.player_name} is no longer in the catalogue.")
    lot = A.retain(session, season, franchise, player, price,
                   now=now, by_tg_id=by_tg_id, slot_key=t.slot_key,
                   talk_rounds=int(t.attempts or 0))
    t.status = TALK_SIGNED
    t.signed_price_lakh = int(lot.sold_price_lakh or price)
    t.counter_lakh = None
    t.closed_at = now or datetime.utcnow()
    return lot


def _walk_out(session, season, franchise, t, *, by_tg_id=None, now=None):
    """He goes to the auction: a queued lot, filed where the room can see."""
    from models import Player
    t.status = TALK_FAILED
    t.counter_lakh = None
    t.closed_at = now or datetime.utcnow()
    player = session.query(Player).filter(Player.id == t.player_id).first()
    lot = None
    if player is not None:
        lot = A._signing_lot(session, season, player, verb="sent to auction")
        lot.set_name = WALKOUT_SET
        if lot.previous_franchise_id is None:
            lot.previous_franchise_id = franchise.id
        session.flush()
    A.log_event(session, season, "retention_failed",
                f"🔴 <b>Retention failed.</b> {A._e(t.player_name)} "
                f"({t.rating} OVR) walks out on "
                f"<b>{A._e(franchise.name)}</b> and enters the auction 🔨",
                lot=lot, franchise=franchise, by_tg_id=by_tg_id,
                detail={"rounds": int(t.attempts or 0)})
    return lot


def make_offer(session, season, t, price, tg_id, *, now=None):
    """The owner's offer, and the player's answer.

    Returns ``(verdict, line, lot_or_None)``. ``verdict`` is ACCEPT, COUNTER,
    REJECT or WALKOUT. Everything happens in the caller's transaction, so a
    refusal from ``retain()`` (the purse moved, the squad filled) rolls the
    attempt back too — an offer the franchise could not have paid never costs
    it a chance.
    """
    if t is None or t.season_id != season.id:
        raise AuctionError("That negotiation is not from this auction.")
    if t.status != TALK_OPEN:
        raise AuctionError(f"That negotiation is already {t.status}.")
    _require_open_window(season)
    franchise = _check_actor(session, t, tg_id)
    table = rules(season)
    slot = _slot_of(season, t)
    price = int(price)
    symbol = season.currency_label or "₹"
    if t.last_offer_lakh is not None and price <= int(t.last_offer_lakh):
        raise AuctionError(f"Your last offer was "
                           f"{A.render_money(t.last_offer_lakh, symbol)} — "
                           f"a new one has to be higher.")
    check_price(session, season, franchise, slot, price)

    t.attempts = int(t.attempts or 0) + 1
    t.last_offer_lakh = price
    t.counter_lakh = None
    target = int(t.map_lakh)
    _low, high = demand_range(table, t.rating)
    superstar_snub = (t.personality == "superstar" and t.attempts == 1
                      and price < high)

    if price >= target and not superstar_snub:
        line = _line(t, ACCEPT)
        t.last_verdict, t.last_reply = ACCEPT, line
        lot = _sign(session, season, franchise, t, price, by_tg_id=tg_id,
                    now=now)
        return ACCEPT, line, lot

    offended = price < target * table["lowball_pct"] / 100.0
    if offended and table["lowball_penalty_pct"]:
        t.map_lakh = _ceil_to(target * (1 + table["lowball_penalty_pct"] / 100.0), 25)

    if t.attempts >= table["chances"]:
        line = _line(t, WALKOUT)
        t.last_verdict, t.last_reply = WALKOUT, line
        _walk_out(session, season, franchise, t, by_tg_id=tg_id, now=now)
        session.flush()
        return WALKOUT, line, None

    near = price >= int(t.map_lakh) * table["counter_pct"] / 100.0
    if near and not superstar_snub and t.personality != "money":
        ask = _ceil_to(int(t.map_lakh), 50)
        t.counter_lakh = ask
        line = _line(t, COUNTER, ask=A.render_money(ask, symbol))
        t.last_verdict, t.last_reply = COUNTER, line
    else:
        line = _line(t, REJECT)
        if offended:
            line = f"{line}\n{LOWBALL_LINE}"
        t.last_verdict, t.last_reply = REJECT, line
    session.flush()
    return t.last_verdict, line, None


def accept_counter(session, season, t, tg_id, *, now=None):
    """Take the player's asking price. Spends no chance."""
    if t is None or t.season_id != season.id:
        raise AuctionError("That negotiation is not from this auction.")
    if t.status != TALK_OPEN:
        raise AuctionError(f"That negotiation is already {t.status}.")
    if not t.counter_lakh:
        raise AuctionError(f"{t.player_name} has not named a price.")
    _require_open_window(season)
    franchise = _check_actor(session, t, tg_id)
    price = int(t.counter_lakh)
    check_price(session, season, franchise, _slot_of(season, t), price)
    line = _line(t, ACCEPT)
    t.last_verdict, t.last_reply = ACCEPT, line
    t.last_offer_lakh = price
    return _sign(session, season, franchise, t, price, by_tg_id=tg_id, now=now)


def withdraw_talk(session, season, t, tg_id=None, *, admin=False, now=None):
    """End the talks.

    Before any offer, the owner simply changes their mind. After one, walking
    away is final — the player goes to the auction — or a franchise could
    probe his price, walk off and come back with a clean slate. An admin
    cancelling (``admin=True``) is the undo for a mistake, and always clean.
    """
    if t is None or t.season_id != season.id:
        raise AuctionError("That negotiation is not from this auction.")
    if t.status != TALK_OPEN:
        raise AuctionError(f"That negotiation is already {t.status}.")
    franchise = _check_actor(session, t, tg_id, admin=admin)
    if admin or not int(t.attempts or 0):
        t.status = TALK_WITHDRAWN
        t.counter_lakh = None
        t.closed_at = now or datetime.utcnow()
        session.flush()
        return None
    line = _line(t, WALKOUT)
    t.last_verdict, t.last_reply = WALKOUT, line
    lot = _walk_out(session, season, franchise, t, by_tg_id=tg_id, now=now)
    session.flush()
    return lot


def personality_label(name):
    emoji, label = PERSONALITY_INFO.get(name, ("⚖️", "Balanced"))
    return f"{emoji} {label}"
