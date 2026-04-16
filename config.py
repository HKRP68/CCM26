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
    (0.20,  50,  59),   # 20% Bronze
    (0.40,  60,  69),   # 20% Silver
    (0.70,  70,  79),   # 30% Common/Super (increased)
    (0.85,  80,  84),   # 15% Rare
    (0.94,  85,  89),   # 9% Epic
    (0.985, 90,  94),   # 4.5% Legend
    (1.0,   95, 100),   # 1.5% Ultimate (was 0.5%)
]

# ── Gspin wheel outcomes ────────────────────────────────────────────
GSPIN_OUTCOMES = [
    (0.55,  "red",    "coins",  (5000, 10000)),   # 55% coins
    (0.80,  "yellow", "player", (70, 79)),         # 25% 70-79 card (was 79-85)
    (0.93,  "blue",   "gems",   (10, 500)),        # 13% gems
    (0.99,  "green",  "player", (80, 85)),         # 6% 80-85 card (was 85-90)
    (1.0,   "purple", "player", (86, 90)),         # 1% 86-90 card (was 90-95)
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
    100: (5_100_000, 3_570_000), 99: (4_600_000, 3_220_000),
    98: (4_100_000, 2_870_000), 97: (3_600_000, 2_520_000),
    96: (3_100_000, 2_170_000), 95: (2_600_000, 1_716_000),
    94: (2_255_000, 1_488_300), 93: (1_910_000, 1_260_600),
    92: (1_565_000, 1_032_900), 91: (1_220_000, 805_200),
    90: (1_420_000, 880_400), 89: (1_220_000, 756_000),
    88: (1_020_000, 632_400), 87: (820_000, 508_400),
    86: (745_000, 461_900), 85: (677_000, 392_660),
    84: (356_000, 206_480), 83: (187_000, 108_460),
    82: (98_000, 56_840), 81: (51_000, 29_580),
    80: (27_000, 14_580), 79: (15_400, 8_316),
    78: (8_800, 4_752), 77: (5_030, 2_716),
    76: (2_875, 1_553), 75: (1_643, 822),
    74: (1_807, 904), 73: (1_642, 821),
    72: (1_493, 747), 71: (1_357, 679),
    70: (1_233, 678), 69: (1_195, 657),
    68: (1_138, 626), 67: (1_084, 596),
    66: (1_033, 568), 65: (983, 590),
    64: (950, 570), 63: (900, 540),
    62: (825, 495), 61: (775, 465),
    60: (700, 420), 59: (625, 375),
    58: (550, 330), 57: (475, 285),
    56: (400, 240), 55: (325, 195),
    54: (275, 165), 53: (250, 150),
    52: (225, 135), 51: (200, 120),
    50: (200, 120),
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
