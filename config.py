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
CLAIM_COOLDOWN = 3600
DAILY_COOLDOWN = 86400
GSPIN_COOLDOWN = 28800
XIMAGE_COOLDOWN = 3600  # /ximage render cooldown (1 hour)

# ── Debut rewards ───────────────────────────────────────────────────
DEBUT_COINS = 1500
DEBUT_GEMS = 30
MAX_ROSTER = 25

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

# ── Gspin wheel outcomes ────────────────────────────────────────────
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

# ── Match rewards (static fallbacks; real values live in GameConfig DB row) ───
WINNER_REWARD_PER_OVER = 300   # coins per over for match winner
LOSER_REWARD_PER_OVER = 150    # coins per over for match loser


def get_max_overs_per_bowler(total_overs: int) -> int:
    """Max overs a single bowler may bowl. Standard rule: total_overs // 5."""
    return max(1, total_overs // 5) if total_overs >= 5 else 1

