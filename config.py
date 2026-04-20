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
CLAIM_RARITY = [
    (0.22,  50,  59),   # 22% Bronze
    (0.44,  60,  69),   # 22% Silver
    (0.76,  70,  79),   # 32% Common/Super
    (0.90,  80,  84),   # 14% Rare
    (0.985, 85,  89),   # 8.5% Epic
    (0.998, 90,  94),   # 1.3% Legend
    (1.0,   95, 100),   # 0.2% Ultimate
]

# ── Gspin wheel outcomes ────────────────────────────────────────────
GSPIN_OUTCOMES = [
    (0.55,  "red",    "coins",  (5000, 10000)),
    (0.82,  "yellow", "player", (70, 79)),
    (0.95,  "blue",   "gems",   (10, 500)),
    (0.997, "green",  "player", (80, 85)),
    (1.0,   "purple", "player", (86, 90)),
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
