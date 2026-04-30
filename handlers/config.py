"""Configuration and constants for the Cricket Bot."""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Bot ──────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///cricket_bot.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Cooldowns (seconds) ─────────────────────────────────────────────
CLAIM_COOLDOWN = 3600
DAILY_COOLDOWN = 86400
GSPIN_COOLDOWN = 28800

# ── Debut rewards ───────────────────────────────────────────────────
DEBUT_COINS = 5000
DEBUT_GEMS = 100
MAX_ROSTER = 25

# ── Claim reward ────────────────────────────────────────────────────
CLAIM_COINS = 500

# ── Daily reward ────────────────────────────────────────────────────
DAILY_COINS = 5000
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

# ── Gspin wheel outcomes ────────────────────────────────────────────
# Most spins = coins/gems. Player pulls skew low-mid.
GSPIN_OUTCOMES = [
    (0.58,  "red",    "coins",  (5000, 10000)),   # 58% coins
    (0.82,  "yellow", "player", (65, 78)),         # 24% 65-78 card
    (0.95,  "blue",   "gems",   (10, 500)),        # 13% gems
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
BUY_SELL = {
    100: (4_950_000, 3_270_000),
    99:  (4_490_000, 2_920_000),
    98:  (3_970_000, 2_540_000),
    97:  (3_420_000, 2_150_000),
    96:  (2_920_000, 1_810_000),
    95:  (2_570_000, 1_570_000),
    94:  (2_150_000, 1_290_000),
    93:  (1_810_000, 1_070_000),
    92:  (1_550_000,   899_000),
    91:  (1_390_000,   792_000),
    90:  (1_260_000,   706_000),
    89:  (1_170_000,   644_000),
    88:    (965_000,   550_000),
    87:    (820_000,   508_000),
    86:    (745_000,   462_000),
    85:    (677_000,   393_000),
    84:    (356_000,   206_000),
    83:    (187_000,   108_000),
    82:     (98_000,    56_800),
    81:     (51_000,    29_600),
    80:     (27_000,    14_600),
    79:     (15_400,     8_320),
    78:      (8_800,     4_750),
    77:      (5_030,     2_720),
    76:      (2_880,     1_560),
    75:      (2_540,     1_270),
    74:      (2_240,     1_120),
    73:      (1_970,       980),
    72:      (1_740,       870),
    71:      (1_530,       760),
    70:      (1_350,       740),
    69:      (1_190,       654),
    68:      (1_140,       627),
    67:      (1_080,       594),
    66:      (1_030,       566),
    65:        (983,       590),
    64:        (950,       570),
    63:        (900,       540),
    62:        (825,       495),
    61:        (775,       465),
    60:        (700,       420),
    59:        (625,       375),
    58:        (550,       330),
    57:        (475,       285),
    56:        (400,       240),
    55:        (325,       195),
    54:        (275,       165),
    53:        (250,       150),
    52:        (225,       135),
    51:        (200,       120),
    50:        (160,        90),
}

def get_buy_value(rating: int) -> int:
    return BUY_SELL.get(rating, (200, 120))[0]

def get_sell_value(rating: int) -> int:
    return BUY_SELL.get(rating, (200, 120))[1]

def get_tier_colour(rating: int) -> tuple:
    if rating >= 95:   return ("LEGENDARY", "#e6ac00", "#fff8e1")
    elif rating >= 90: return ("EPIC", "#9b59b6", "#f3e5f5")
    elif rating >= 85: return ("RARE", "#2980b9", "#e3f2fd")
    elif rating >= 80: return ("UNCOMMON", "#27ae60", "#e8f5e9")
    elif rating >= 70: return ("COMMON", "#7f8c8d", "#eceff1")
    else:              return ("BASIC", "#95a5a6", "#fafafa")

# ── Phase 2: Trading & Roster ──────────────────────────────────────
TRADE_EXPIRES_SECONDS = 20
MAX_ACTIVE_TRADES = 1
TRADE_MIN_RATING = 75
TRADE_FEE_PERCENT = 5
ROSTER_PAGE_SIZE = 10

# ══════════════════════════════════════════════════════════════════════
# TRAIT SYSTEM
# ══════════════════════════════════════════════════════════════════════

# Master trait list. Seeded on startup (admin can toggle is_active).
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

# ── Market ────────────────────────────────────────────────────────
TRAIT_MARKET_SLOTS = 5
TRAIT_MARKET_REFRESH_HOURS = 24
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

