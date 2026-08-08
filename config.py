"""Configuration and constants for the Cricket Bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Bot ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///cricket_bot.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Media storage channel ───────────────────────────────────────────
# Optional: a private Telegram channel where the bot is admin, used as
# a persistent file_id store for uploaded GIFs. If set, file uploads via
# the admin website go to this channel and we save the resulting file_id
# instead of a disk path (which gets wiped on every deploy).
# Format: a chat_id like "-1001234567890" (negative for channels/groups).
MEDIA_STORAGE_CHAT_ID = os.getenv("MEDIA_STORAGE_CHAT_ID", "").strip()

# ── Cooldowns (seconds) ─────────────────────────────────────────────
# These are the base (free-user) cooldowns. Paid tiers get a proportional
# reduction applied via services.subscription_service.cooldown_seconds
# (Bronze −0 min/hr, Silver −5 → ×0.917, Platinum −10 → ×0.833,
# Diamond −15 → ×0.75). The resulting ladder:
#
#   command  | Free & Bronze | Silver  | Platinum | Diamond
#   /claim   | 1h            | 55m     | 50m      | 45m
#   /sim     | 4h            | 3h 40m  | 3h 20m   | 3h
#   /gspin   | 8h            | 7h 20m  | 6h 40m   | 6h
#   /daily   | 24h           | 22h     | 20h      | 18h
#
# An admin cooldown override saved on the website (BotCommand.cooldown_seconds)
# replaces the base value below; the tier reduction still applies on top.
CLAIM_COOLDOWN = 3600    # 1 hour
DAILY_COOLDOWN = 86400   # 24 hours
GSPIN_COOLDOWN = 28800   # 8 hours
XIMAGE_COOLDOWN = 3600  # /ximage render cooldown (1 hour)
# /sim simulates a whole match server-side (two innings, scorecard images and a
# full commentary feed), so it is rate-limited like a reward command rather than
# a free action. A challenge — /sim in reply to another player — spends BOTH
# players' cooldowns, so a player already on cooldown cannot be challenged.
SIM_COOLDOWN = 14400     # 4 hours

# ── Paid subscription tiers ─────────────────────────────────────────
# Manually granted by an admin from the website (no self-serve payment).
# A tier stays active for `duration_days` from activation.
#   instant                 — one-time rewards credited on activation.
#   upgrade_from            — per-source top-up bundle credited when a member
#                             steps UP into this tier (see below).
#   mysterybox_cooldown_days — /cmumysterybox recurrence.
#   cooldown_reduction_min_per_hour — shaves this many minutes off every hour
#                             of a normal command cooldown (Bronze 0, Silver 5,
#                             Platinum 10, Diamond 15) — see the ladder above.
#   market_discount_pct     — % off player purchases (Platinum 5, Diamond 10).
#   weekly_card             — enables /cmuweekly (guaranteed 85+ card, 7-day cd).
#   coin_chests             — enables /cmuchest (recurring coin chests).
#   daily_login_multiplier  — multiplies the Mini App daily login reward.
#   premium_commands        — unlocks /autobuild and /wpmbot.
#   autoplay                — unlocks the Mini App Autoplay button.
#
# DECLARATION ORDER IS THE TIER RANK (bronze < silver < platinum < diamond) —
# services.subscription_service.tier_rank reads it, so keep tiers ordered from
# cheapest to richest.
#
# Upgrade top-ups follow one rule: roughly HALF the raw instant difference
# between the two tiers, plus whichever signature packs the source tier never
# granted. The member keeps their remaining paid time, so an upgrade tops them
# up instead of handing out a second subscription. The halves are also chosen so
# hopping (Bronze → Silver → Diamond) never pays more than going direct.
SUBSCRIPTION_TIERS = {
    "bronze": {
        "label": "🥉 Bronze",
        "price_inr": 19,
        "duration_days": 30,
        "instant": {"coins": 29000, "gems": 150, "quest_points": 0,
                    "packs": []},
        "mysterybox_cooldown_days": 15,
        # Bronze runs on the free-user cooldowns — the entry tier buys the
        # rewards and premium commands, not faster timers.
        "cooldown_reduction_min_per_hour": 0,
        "market_discount_pct": 0,
        "weekly_card": False,
        "coin_chests": None,
        "daily_login_multiplier": 1,
        "premium_commands": True,
        # Autoplay stays a Silver-and-above perk — Bronze is the entry tier.
        "autoplay": False,
    },
    "silver": {
        "label": "🥈 Silver",
        "price_inr": 59,
        "duration_days": 30,
        "instant": {"coins": 49000, "gems": 499, "quest_points": 499,
                    "packs": ["Star Pack"]},
        "upgrade_from": {
            "bronze": {"coins": 10000, "gems": 175, "quest_points": 250,
                       "packs": ["Star Pack"]},
        },
        "mysterybox_cooldown_days": 8,
        "cooldown_reduction_min_per_hour": 5,
        "market_discount_pct": 0,
        "weekly_card": False,
        "coin_chests": None,
        "daily_login_multiplier": 1,
        "premium_commands": True,
        "autoplay": True,
    },
    "platinum": {
        "label": "🏆 Platinum",
        "price_inr": 99,
        "duration_days": 30,
        "instant": {"coins": 1000000, "gems": 1000, "quest_points": 1000,
                    "packs": ["Legend Pack"]},
        # Rewards for UPGRADING into Platinum from a lower tier (keyed by the
        # source tier). Deliberately a small top-up, NOT the full `instant`
        # bundle: the member keeps their remaining paid time and already
        # received the lower tier's rewards, so an upgrade tops them up rather
        # than handing out a second subscription. Only the new signature pack
        # is granted — the Star Pack the user got from Silver is already theirs.
        # Set "packs": [] here if you don't want any pack on upgrade.
        "upgrade_from": {
            "bronze": {"coins": 485000, "gems": 425, "quest_points": 500,
                       "packs": ["Legend Pack"]},
            "silver": {"coins": 451000, "gems": 251, "quest_points": 251,
                       "packs": ["Legend Pack"]},
        },
        "mysterybox_cooldown_days": 4,
        "cooldown_reduction_min_per_hour": 10,
        "market_discount_pct": 5,
        "weekly_card": True,
        "coin_chests": {"count": 3, "min": 60000, "max": 99000,
                        "cooldown_days": 10},
        "daily_login_multiplier": 1,
        "premium_commands": True,
        "autoplay": True,
    },
    "diamond": {
        "label": "💎 Diamond",
        "price_inr": 149,
        "duration_days": 30,
        "instant": {"coins": 2000000, "gems": 1500, "quest_points": 1500,
                    "packs": ["Legend Pack", "Ultimate Legend Pack"]},
        "upgrade_from": {
            "bronze": {"coins": 985000, "gems": 675, "quest_points": 750,
                       "packs": ["Legend Pack", "Ultimate Legend Pack"]},
            "silver": {"coins": 975000, "gems": 500, "quest_points": 500,
                       "packs": ["Legend Pack", "Ultimate Legend Pack"]},
            # Platinum already handed out the Legend Pack — only the Ultimate
            # Legend Pack is new on this step.
            "platinum": {"coins": 500000, "gems": 250, "quest_points": 250,
                         "packs": ["Ultimate Legend Pack"]},
        },
        "mysterybox_cooldown_days": 2,
        "cooldown_reduction_min_per_hour": 15,
        "market_discount_pct": 10,
        "weekly_card": True,
        "coin_chests": {"count": 5, "min": 70000, "max": 110000,
                        "cooldown_days": 7},
        "daily_login_multiplier": 2,
        "premium_commands": True,
        "autoplay": True,
    },
}

# /cmuweekly guaranteed player band (any 85+ OVR).
WEEKLY_CARD_MIN_OVR = 85
WEEKLY_CARD_MAX_OVR = 99
WEEKLY_CARD_COOLDOWN_DAYS = 7

# ── Mystery box (/cmumysterybox) weighted reward tables ─────────────
# (cumulative_roll_ceiling, low, high). A roll of Math.random()*100 in
# (prev_ceiling, ceiling] selects the band; getRandomInt(low, high) is inclusive.
MYSTERYBOX_COIN_BANDS = [
    (1.0,  58001, 60000),   # 1%
    (5.0,  52001, 58000),   # 4%
    (15.0, 45001, 52000),   # 10%
    (40.0, 36001, 45000),   # 25%
    (100.0, 30000, 36000),  # 60%
]
# gems and questPoints share the same distribution.
MYSTERYBOX_CURRENCY_BANDS = [
    (1.0,  296, 300),   # 1%
    (5.0,  286, 295),   # 4%
    (15.0, 276, 285),   # 10%
    (40.0, 261, 275),   # 25%
    (100.0, 250, 260),  # 60%
]
# (cumulative_roll_ceiling, ovr_low, ovr_high, label). Also reused by
# /cmuweekly so its guaranteed 85+ card favours 85-87 and tapers upward.
MYSTERYBOX_PLAYER_BANDS = [
    (0.3,  97, 99, "97+ OVR ⭐ JACKPOT"),   # 0.3%
    (1.5,  94, 96, "94-96 OVR — Epic"),      # 1.2%
    (10.0, 91, 93, "91-93 OVR — Rare"),      # 8.5%
    (35.0, 88, 90, "88-90 OVR — Uncommon"),  # 25%
    (100.0, 85, 87, "85-87 OVR — Common"),   # 65%
]
# Each Mystery Box open grants exactly ONE reward type, rolled from these
# weights (cumulative_roll_ceiling, type). Coins-heavy, player rare.
MYSTERYBOX_REWARD_TYPE_BANDS = [
    (50.0,  "coins"),        # 50%
    (70.0,  "gems"),         # 20%
    (90.0,  "questPoints"),  # 20%
    (100.0, "player"),       # 10%
]

# ── Debut rewards ───────────────────────────────────────────────────
DEBUT_COINS = 1500
DEBUT_GEMS = 30

# ── Squad size ──────────────────────────────────────────────────────
# How many cards a captain may hold, Career Player included. Every
# acquisition path checks this, and a squad already over it is brought back
# down by ``migrate_squad_and_trait_limits.py``.
MAX_ROSTER = 19

# ── Coins → Gems conversion (/coins2gems) ───────────────────────────
# How many coins buy one gem. The conversion is one-way and irreversible;
# coins spent are always rounded DOWN to a whole multiple of this value.
COINS_PER_GEM = 1000

# ── Claim reward ────────────────────────────────────────────────────
# No coins from /claim — players still pull a free card.
CLAIM_COINS = 0

# ── Daily reward ────────────────────────────────────────────────────
DAILY_COINS = 1500
DAILY_PLAYERS = 2
STREAK_MILESTONE = 14
STREAK_MISS_DAYS = 2

# ── Claim rarity distribution (cumulative thresholds) ───────────────
# Tuned so 80+ feels genuinely rare, not routine.
CLAIM_RARITY = [
    (0.26,  50,  59),   # 26% Bronze (fodder)
    (0.51,  60,  69),   # 25% Silver
    (0.89,  70,  79),   # 38% Super (the core band — most pulls)
    (0.95,  80,  84),   # 6%  Rare (noticeable celebration moment)
    (0.985, 85,  89),   # 3.5% Epic
    (0.9995, 90, 94),   # 1.45% Legend
    (1.0,   95, 100),   # 0.05% Ultimate (≈1 in 2000)
]

# ── Lucky Card Pick outcomes (legacy fallback for /gspin) ───────────
# Most spins = coins/gems. Player pulls skew low-mid.
GSPIN_OUTCOMES = [
    (0.58,  "red",    "coins",  (1500, 3000)),    # 58% coins
    (0.82,  "yellow", "player", (65, 78)),         # 24% 65-78 card
    (0.95,  "blue",   "gems",   (3, 150)),         # 13% gems
    (0.992, "green",  "player", (79, 84)),         # 4.2% 79-84 card
    (1.0,   "purple", "player", (85, 90)),         # 0.8% 85-90 card
]

GSPIN_EMOJIS = {
    "red": "🟥", "yellow": "🟨", "blue": "🟦",
    "green": "🟩", "purple": "⭐",
}

GSPIN_NAMES = {
    "red": "Red", "yellow": "Yellow", "blue": "Blue",
    "green": "Green", "purple": "Purple",
}

# ── Player categories ──────────────────────────────────────────────
CATEGORIES = ["Batsman", "Bowler", "All-rounder", "Wicket Keeper"]
BAT_HANDS = ["Right", "Left"]
BOWL_HANDS = ["Right", "Left"]
BOWL_STYLES = ["Fast", "Off Spinner", "Leg Spinner", "Medium Pacer"]

# ── Buy / Sell values by rating ─────────────────────────────────────
# Buy values raised +30%, sell values cut -40% to tighten the economy.
BUY_SELL = {
    100: (6_435_000, 1_962_000),
    99:  (5_840_000, 1_750_000),
    98:  (5_160_000, 1_520_000),
    97:  (4_450_000, 1_290_000),
    96:  (3_800_000, 1_090_000),
    95:  (3_340_000,   940_000),
    94:  (2_800_000,   774_000),
    93:  (2_350_000,   642_000),
    92:  (2_020_000,   539_000),
    91:  (1_810_000,   475_000),
    90:  (1_640_000,   424_000),
    89:  (1_520_000,   386_000),
    88:  (1_255_000,   330_000),
    87:  (1_066_000,   305_000),
    86:    (969_000,   277_000),
    85:    (880_000,   236_000),
    84:    (463_000,   124_000),
    83:    (243_000,    65_000),
    82:    (127_000,    34_100),
    81:     (66_300,    17_800),
    80:     (35_100,     8_760),
    79:     (20_000,     5_000),
    78:     (11_400,     2_850),
    77:      (6_540,     1_630),
    76:      (3_740,       940),
    75:      (3_300,       760),
    74:      (2_910,       670),
    73:      (2_560,       590),
    72:      (2_260,       520),
    71:      (1_990,       460),
    70:      (1_760,       440),
    69:      (1_550,       390),
    68:      (1_480,       380),
    67:      (1_400,       360),
    66:      (1_340,       340),
    65:      (1_280,       350),
    64:      (1_240,       340),
    63:      (1_170,       320),
    62:      (1_070,       300),
    61:      (1_010,       280),
    60:        (910,       250),
    59:        (810,       225),
    58:        (715,       200),
    57:        (620,       170),
    56:        (520,       140),
    55:        (420,       120),
    54:        (360,       100),
    53:        (325,        90),
    52:        (290,        80),
    51:        (260,        72),
    50:        (210,        54),
}

def get_buy_value(rating: int) -> int:
    """Coin cost to buy a player of the given rating (falls back to base 260)."""
    return BUY_SELL.get(rating, (260, 72))[0]

def get_sell_value(rating: int) -> int:
    """Coins returned for selling/releasing a player of the given rating."""
    return BUY_SELL.get(rating, (260, 72))[1]

def get_tier_colour(rating: int) -> tuple:
    if rating >= 95:   return ("LEGENDARY", "#e6ac00", "#fff8e1")
    elif rating >= 90: return ("EPIC", "#9b59b6", "#f3e5f5")
    elif rating >= 85: return ("RARE", "#2980b9", "#e3f2fd")
    elif rating >= 80: return ("UNCOMMON", "#27ae60", "#e8f5e9")
    elif rating >= 70: return ("COMMON", "#7f8c8d", "#eceff1")
    else:              return ("BASIC", "#95a5a6", "#fafafa")

# ── Phase 2: Trading & Roster ──────────────────────────────────────
TRADE_EXPIRES_SECONDS = 60
MAX_ACTIVE_TRADES = 1
TRADE_MIN_RATING = 75
# Charged to BOTH captains on a completed trade, as a percentage of the traded
# card's buy value (both cards are the same OVR, so both pay the same).
TRADE_FEE_PERCENT = 1
ROSTER_PAGE_SIZE = 10

# ══════════════════════════════════════════════════════════════════════
# TRAIT SYSTEM
# ══════════════════════════════════════════════════════════════════════

# Master trait list. Seeded on startup (admin can toggle is_active).
#
# NOTE: the list the bot actually seeds and renders is
# ``services.trait_service.TRAIT_DEFINITIONS`` — it carries the effect keys the
# engine routes on, plus each trait's rarity. This copy is the older, shorter
# summary table and is kept only so nothing importing it breaks; edit the
# service list when adding or re-tuning a trait.
TRAIT_DEFINITIONS = [
    # Batting
    {"key": "finisher", "name": "Finisher", "category": "batting", "emoji": "🔥",
     "description": "Boundary boost in the last 3 overs"},
    {"key": "power_hitter", "name": "Power Hitter", "category": "batting", "emoji": "💥",
     "description": "More sixes, but slightly higher wicket risk"},
    {"key": "anchor", "name": "Anchor", "category": "batting", "emoji": "⚓",
     "description": "Much lower wicket chance, fewer sixes"},
    {"key": "fast_starter", "name": "Fast Starter", "category": "batting", "emoji": "⚡",
     "description": "Boundary boost in first 10 balls"},
    {"key": "clutch_player", "name": "Clutch Player", "category": "batting", "emoji": "🎯",
     "description": "Boundary boost when RRR > 8"},

    # Bowling
    {"key": "death_specialist", "name": "Death Specialist", "category": "bowling", "emoji": "💀",
     "description": "Fewer sixes, more wickets in last 3 overs"},
    {"key": "wicket_hunter", "name": "Wicket Hunter", "category": "bowling", "emoji": "🏹",
     "description": "Higher wicket chance throughout"},
    {"key": "dot_ball_specialist", "name": "Dot Ball Specialist", "category": "bowling", "emoji": "🚫",
     "description": "More dot balls every over"},
    {"key": "powerplay_king", "name": "Powerplay King", "category": "bowling", "emoji": "👑",
     "description": "Dominant in overs 1-3"},
    {"key": "yorker_specialist", "name": "Yorker Specialist", "category": "bowling", "emoji": "🎯",
     "description": "Extra accuracy in the last 3 overs"},

    # Fielding
    {"key": "safe_hands", "name": "Safe Hands", "category": "fielding", "emoji": "🧤",
     "description": "Lower catch-drop chance (catches stick)"},
    {"key": "sniper_arm", "name": "Sniper Arm", "category": "fielding", "emoji": "🎯",
     "description": "Higher run-out chance"},

    # Mental
    {"key": "consistency_king", "name": "Consistency King", "category": "mental", "emoji": "🧠",
     "description": "Shrinks variance — fewer extremes both ways"},
    {"key": "momentum_player", "name": "Momentum Player", "category": "mental", "emoji": "📈",
     "description": "Gets stronger as the innings progresses"},
]

# Level → base effect percentage (additive building block for engine)
TRAIT_LEVEL_EFFECT = {
    1: 0.05,   # 5%
    2: 0.10,   # 10%
    3: 0.15,   # 15%
    4: 0.20,   # 20%
    5: 0.25,   # 25%
}

# Hidden bonus applied when max-level
TRAIT_MAX_BONUS = 0.02   # extra 2% at L5

# Diminishing-returns multipliers for stacking traits on one player.
# (sorted by level desc, then applied: slot 0 × 1.0, slot 1 × 0.7, slot 2 × 0.5)
TRAIT_STACK_WEIGHTS = [1.0, 0.7, 0.5]

# Hard cap on combined effective boost per side of the ball
# (prevents 3 high-level traits from snowballing)
TRAIT_BOOST_CAP = 0.25   # 25%

# Max traits per player
TRAIT_MAX_PER_PLAYER = 3
# Max same-category traits per player (prevents 3 batting traits on one batsman)
TRAIT_MAX_SAME_CATEGORY = 2
# Elite traits are the top of the ladder — one per player, ever. Without this a
# captain could stack Master Blaster + Run Machine + GOAT Instinct on a single
# card and the rest of the catalogue would stop mattering.
TRAIT_MAX_ELITE_PER_PLAYER = 1

# Max traits equipped across a whole squad. Sized to exactly the number of
# ordinary cards (MAX_ROSTER - 1, the squad minus the Career Player), so the
# budget stretches to one trait each — but the per-card limit is
# TRAIT_MAX_PER_PLAYER, so stacking a second or third trait on a key card
# means leaving other cards bare. That trade is the decision the cap exists
# to create.
#
# The Career Player is EXEMPT: it carries its own TRAIT_MAX_PER_PLAYER
# allowance on top of this budget, so a fully kitted account holds
# TRAIT_MAX_PER_SQUAD + TRAIT_MAX_PER_PLAYER == 21 equipped traits.
TRAIT_MAX_PER_SQUAD = 18

# ── Rarity ────────────────────────────────────────────────────────
# Every trait carries a rarity, and it decides three things:
#   • how often the shared market rolls it (``weight``),
#   • what a Lv.1 copy costs (``price_mult`` × TRAIT_MARKET_BUY_COST),
#   • how much resale and the swap fee scale with it (the same multiplier).
#
# Upgrade costs are deliberately NOT scaled: levelling an Elite trait costs the
# same gems as levelling a Common one. Rarity is a barrier to *getting* the
# trait, not a tax on using it — and scaling both would put a Lv.5 Elite at
# 24,000 gems, which nobody would ever build.
TRAIT_RARITIES = {
    "common": {"label": "Common", "emoji": "⚪", "price_mult": 1, "weight": 50},
    "rare":   {"label": "Rare",   "emoji": "🔵", "price_mult": 2, "weight": 30},
    "epic":   {"label": "Epic",   "emoji": "🟣", "price_mult": 4, "weight": 15},
    "elite":  {"label": "Elite",  "emoji": "⭐", "price_mult": 8, "weight": 5},
}
TRAIT_DEFAULT_RARITY = "common"
TRAIT_RARITY_ORDER = ["common", "rare", "epic", "elite"]


def trait_rarity_key(rarity) -> str:
    """Normalise anything (None, a Trait row's column, mixed case) to a key."""
    key = str(rarity or "").strip().lower()
    return key if key in TRAIT_RARITIES else TRAIT_DEFAULT_RARITY


def trait_rarity_meta(rarity) -> dict:
    return TRAIT_RARITIES[trait_rarity_key(rarity)]


def trait_rarity_mult(rarity) -> int:
    """Price multiplier for a rarity. Common is 1, so old callers are unchanged."""
    return int(trait_rarity_meta(rarity)["price_mult"])


def trait_rarity_label(rarity) -> str:
    meta = trait_rarity_meta(rarity)
    return f"{meta['emoji']} {meta['label']}"


def trait_list_price(rarity=None) -> int:
    """Gems the market asks for a fresh Lv.1 copy at this rarity."""
    return TRAIT_MARKET_BUY_COST * trait_rarity_mult(rarity)

# ── Market ────────────────────────────────────────────────────────
TRAIT_MARKET_SLOTS = 5
TRAIT_MARKET_REFRESH_HOURS = 24

# How often the trait market may be set to refresh, in hours. Only divisors of
# 24 are offered: they are the intervals that tile a day evenly, so "every 12
# hours starting 12 AM" lands on 12 AM and 12 PM every day forever instead of
# drifting round the clock. Admin-selectable from /markets on the website; the
# live values live in GameConfig, not here.
TRAIT_MARKET_REFRESH_INTERVALS = (1, 2, 3, 4, 6, 8, 12, 24)


def clamp_refresh_interval(hours) -> int:
    """Snap an interval to the nearest allowed value (24 if unparseable).

    Ties break toward the SHORTER interval — an ambiguous 7 becomes 6, not 8 —
    so a mis-set value errs toward refreshing more often rather than less.
    """
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return 24
    if hours in TRAIT_MARKET_REFRESH_INTERVALS:
        return hours
    return min(TRAIT_MARKET_REFRESH_INTERVALS, key=lambda h: (abs(h - hours), h))


def refresh_anchor_hours(start_hour, interval_hours) -> list:
    """The IST clock hours a market refreshes at, sorted ascending.

    ``refresh_anchor_hours(0, 12) == [0, 12]`` — midnight and noon.
    """
    interval = clamp_refresh_interval(interval_hours)
    try:
        start = int(start_hour) % 24
    except (TypeError, ValueError):
        start = 0
    return sorted((start + k * interval) % 24 for k in range(24 // interval))


def format_hour_ist(hour) -> str:
    """Render an IST clock hour as '12:00 AM' / '1:00 PM'."""
    hour = int(hour) % 24
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:00 {suffix}"


TRAIT_MARKET_BUY_COST = 150  # gems — flat cost for L1 trait
TRAIT_MARKET_REROLL_COST = 30  # gems

# Daily purchase limit
TRAIT_DAILY_PURCHASE_LIMIT = 2
TRAIT_DAILY_REROLL_LIMIT = 3

# Discount on 1 random slot
TRAIT_DISCOUNT_CHANCE = 0.5  # 50% of shops have a discount item
TRAIT_DISCOUNT_RANGE = (10, 20)  # 10% or 20% off

# ── Upgrade costs ─────────────────────────────────────────────────
TRAIT_UPGRADE_COSTS = {
    1: 200,    # L1 → L2
    2: 400,    # L2 → L3
    3: 800,    # L3 → L4
    4: 1500,   # L4 → L5
}

# ── Replace / swap ────────────────────────────────────────────────
TRAIT_REPLACE_COST = 250  # gems

# ── Trait resale (/selltrait) and trait-for-trait trading (/tradetrait) ──
#
# A trait's "buy value" is everything it has cost by the time it reaches a
# level: the shop price for Lv.1, plus every upgrade paid on the way up. Derived
# from the two tables above rather than typed out, so tuning the shop price or
# an upgrade cost re-prices resale and trading with it instead of leaving three
# numbers to drift apart.
#
#   Level      1      2      3      4      5
#   buy      150    350    750  1,550  3,050   (invested gems)
#   floor    120    320    720  1,520  3,020   (cheapest this level can cost)
#   sell     119    319    719  1,519  3,019   (+50%, capped below the floor)
#   trade fee 15     25     45     85    160   (each side, in gems)
#
# That table is the COMMON tier. Rarer traits scale the Lv.1 price (and only the
# Lv.1 price) by ``trait_rarity_mult`` — see TRAIT_RARITIES — so an Elite Lv.1
# lists at 1,200 gems while its upgrades cost exactly what a Common's do. Every
# function below takes an optional ``rarity``; omitting it means Common, which
# is why every existing caller and price is unchanged.


def _trait_buy_values():
    values, running = {}, TRAIT_MARKET_BUY_COST
    values[1] = running
    for level in sorted(TRAIT_UPGRADE_COSTS):
        running += TRAIT_UPGRADE_COSTS[level]
        values[level + 1] = running
    return values


TRAIT_BUY_VALUE = _trait_buy_values()
TRAIT_MAX_LEVEL = max(TRAIT_BUY_VALUE)

# ── Resale ────────────────────────────────────────────────────────
# Resale used to return 30% below buy value, which made /selltrait a last
# resort: a Lv.5 trait cost 3,050 gems to build and handed back 2,135, so a
# captain reshaping a squad ate a 915-gem hole and mostly hoarded dead traits
# instead. Resale is now worth 50% more than that table at every level.
TRAIT_SELL_BASELINE_DISCOUNT_PCT = 30   # the old table, kept as the reference
TRAIT_SELL_UPLIFT_PCT = 50              # ...and how much more it now returns

# ...with ONE hard ceiling, which is what stops the uplift printing gems.
#
# A trait must never sell for more than it could have been bought for, or the
# shop becomes a gem tap: buy, sell, repeat. The cheapest a level can ever cost
# is not its list price — the daily shop discounts one slot by up to
# TRAIT_DISCOUNT_RANGE[1], so a Lv.1 trait is available for 120 gems, and every
# higher level is that 120 plus its upgrades (upgrade costs are never
# discounted). A flat +50% clears that floor at *every* level (Lv.1 would pay
# 157 for a trait bought at 120), so the uplift is applied and then capped one
# gem below the floor. In practice that delivers the full +50% nowhere and
# +13/+30/+37/+40/+41% at Lv.1→5 — the ceiling binds hardest at Lv.1, where the
# shop discount is deepest relative to the price.
TRAIT_SELL_FLOOR_MARGIN = 1             # gems kept below the floor, always

# Trading is priced off the same investment: a flat fee at Lv.1, plus a share of
# the upgrade gems sunk into the trait above it. That keeps a Lv.1 swap cheap
# while a Lv.5 swap still costs real money — roughly doubling per level —
# without charging a fresh trait's worth of gems for the privilege.
#
# The old top end was the problem: 320 gems each side to swap two Lv.5 traits is
# more than a fresh trait costs, and two captains who each held what the other
# wanted simply didn't trade.
#
# The ceiling on the fee is the cost of the obvious alternative — selling the
# trait and buying another. A swap has to be cheaper than that or /tradetrait
# has no reason to exist. Raising resale to just under cost pulled that ceiling
# right down with it: the loss on a sale is now a flat 31 gems at every level,
# so the fee has to clear 31 everywhere, not just at the bottom. Hence base 15
# with a 0.5% share of the upgrade gems: 15 / 16 / 18 / 22 / 29 for Lv.1→5.
# It still rises with the gems sunk into the trait, just inside a much smaller
# band than the resale economy used to leave room for.
TRAIT_TRADE_FEE_BASE = 15            # gems, Lv.1
TRAIT_TRADE_FEE_UPGRADE_PCT = 0.5    # + this % of the upgrade gems above Lv.1


def _clamp_trait_level(level) -> int:
    try:
        level = int(level)
    except (TypeError, ValueError):
        return 1
    return max(1, min(TRAIT_MAX_LEVEL, level))


def _trait_floor_costs():
    """Cheapest gems each level can possibly have cost.

    The Lv.1 shop price at the deepest discount the shop can roll, plus every
    upgrade above it at full price — upgrades are never discounted. This is the
    line ``trait_sell_value`` has to stay under.
    """
    best_discount = max(TRAIT_DISCOUNT_RANGE)
    running = TRAIT_MARKET_BUY_COST * (100 - best_discount) // 100
    values = {1: running}
    for level in sorted(TRAIT_UPGRADE_COSTS):
        running += TRAIT_UPGRADE_COSTS[level]
        values[level + 1] = running
    return values


TRAIT_FLOOR_COST = _trait_floor_costs()


def _rarity_premium(rarity) -> int:
    """Extra gems a rarity adds to the Lv.1 price (0 for Common)."""
    return TRAIT_MARKET_BUY_COST * (trait_rarity_mult(rarity) - 1)


def trait_buy_value(level, rarity=None) -> int:
    """Gems invested in a trait by the time it reaches ``level``."""
    return TRAIT_BUY_VALUE[_clamp_trait_level(level)] + _rarity_premium(rarity)


def trait_floor_cost(level, rarity=None) -> int:
    """Cheapest gems a trait of ``level`` could have been acquired for.

    The rarity premium is discountable — it is part of the list price the shop
    marks down — so it enters the floor at the deepest discount the shop rolls,
    exactly as the Common list price does.
    """
    best_discount = max(TRAIT_DISCOUNT_RANGE)
    premium = _rarity_premium(rarity) * (100 - best_discount) // 100
    return TRAIT_FLOOR_COST[_clamp_trait_level(level)] + premium


def trait_sell_value(level, rarity=None) -> int:
    """Gems returned for selling a trait.

    50% more than the original resale table, then held one gem below what the
    trait could have cost to acquire — see the notes above ``TRAIT_SELL_*``.
    Selling is therefore always a loss, however cheaply the trait was bought,
    and that stays true at every rarity because both sides of the comparison
    scale together.
    """
    level = _clamp_trait_level(level)
    baseline = (trait_buy_value(level, rarity)
                * (100 - TRAIT_SELL_BASELINE_DISCOUNT_PCT) // 100)
    uplifted = baseline * (100 + TRAIT_SELL_UPLIFT_PCT) // 100
    ceiling = trait_floor_cost(level, rarity) - TRAIT_SELL_FLOOR_MARGIN
    return max(1, min(uplifted, ceiling))


def trait_trade_fee(level, rarity=None) -> int:
    """Gems each side pays to swap a trait of ``level``.

    Scales with rarity for the same reason the price does — a swap of two Elite
    traits moves far more value than a swap of two Commons — while staying well
    under the cost of the alternative (sell one, buy the other back).
    """
    upgrades_above_l1 = (TRAIT_BUY_VALUE[_clamp_trait_level(level)]
                         - TRAIT_BUY_VALUE[1])
    # int() after the division, not floor division on the percentage — the
    # share is now a fraction of a percent, which integer division would
    # collapse to zero and flatten the fee to a single number at every level.
    fee = TRAIT_TRADE_FEE_BASE * trait_rarity_mult(rarity) + int(
        upgrades_above_l1 * TRAIT_TRADE_FEE_UPGRADE_PCT / 100)
    return max(1, fee)

# ── Aliases so trait_service / trait_engine can use consistent names ──
TRAIT_LEVEL_PCT = {k: int(v * 100) for k, v in TRAIT_LEVEL_EFFECT.items()}
TRAIT_MAX_EFFECTIVE_PCT = int(TRAIT_BOOST_CAP * 100)
TRAIT_LEVEL_5_HIDDEN_BONUS_PCT = int(TRAIT_MAX_BONUS * 100)
TRAIT_SHOP_SLOTS = TRAIT_MARKET_SLOTS
TRAIT_SHOP_DAILY_PURCHASE_LIMIT = TRAIT_DAILY_PURCHASE_LIMIT
TRAIT_SHOP_BASE_PRICE = TRAIT_MARKET_BUY_COST
TRAIT_REROLL_COST = TRAIT_MARKET_REROLL_COST
TRAIT_DAILY_DISCOUNT_MIN = TRAIT_DISCOUNT_RANGE[0]
TRAIT_DAILY_DISCOUNT_MAX = TRAIT_DISCOUNT_RANGE[1]

# ── Match rewards (static fallbacks; real values live in GameConfig DB row) ───
WINNER_REWARD_PER_OVER = 300   # coins per over for match winner
LOSER_REWARD_PER_OVER = 150    # coins per over for match loser


def get_max_overs_per_bowler(total_overs: int) -> int:
    """Max overs a single bowler may bowl. Standard rule: total_overs // 5."""
    return max(1, total_overs // 5) if total_overs >= 5 else 1

