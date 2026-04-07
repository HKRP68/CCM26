"""Seed the database from data/players.json (3,165 real cricket players)."""

import json
import random
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_session
from models import Player

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "players.json")

# ── Field normalisation helpers ──────────────────────────────────────

def normalise_category(raw: str) -> str:
    raw = raw.strip()
    low = raw.lower()
    if low == "batsman":
        return "Batsman"
    if low == "bowler":
        return "Bowler"
    if low == "all-rounder":
        return "All-rounder"
    if low in ("wicketkeeper", "wicket keeper", "wk"):
        return "Wicket Keeper"
    return raw.title()


def parse_bat_hand(raw: str) -> str:
    return "Left" if "left" in raw.lower() else "Right"


def parse_bowl_hand(raw: str) -> str:
    low = raw.strip().lower()
    if "left" in low:
        return "Left"
    return "Right"


def parse_bowl_style(raw: str) -> str:
    """Map the detailed bowling descriptions to one of the 4 config categories."""
    low = raw.strip().lower().replace("\n", "")
    if "leg" in low:
        return "Leg Spinner"
    if "off" in low:
        return "Off Spinner"
    if "fast" in low and "medium" not in low:
        return "Fast"
    # "medium fast", "medium", etc.
    return "Medium Pacer"


# ── Plausible stat generation ────────────────────────────────────────

def generate_stats(rating: int, category: str, bat_rating: int, bowl_rating: int) -> dict:
    """Generate plausible career stats based on rating and category."""
    r = rating
    is_bat = category in ("Batsman", "Wicket Keeper")
    is_bowl = category == "Bowler"
    # is_allrounder otherwise

    # Scale factor: higher rating → better stats
    scale = max(0.2, (r - 50) / 50)  # 0.0 at 50, 1.0 at 100

    if is_bat:
        bat_avg = round(random.uniform(20, 32) + scale * random.uniform(10, 25), 1)
        strike_rate = round(random.uniform(55, 75) + scale * random.uniform(10, 50), 1)
        runs = int(random.uniform(500, 3000) + scale * random.uniform(2000, 12000))
        centuries = int(scale * random.uniform(1, 45))
        bowl_avg = round(random.uniform(30, 80), 1) if bowl_rating > 20 else 0.0
        economy = round(random.uniform(4.0, 8.0), 1) if bowl_rating > 20 else 0.0
        wickets = int(random.uniform(0, 20) * scale) if bowl_rating > 20 else 0
    elif is_bowl:
        bat_avg = round(random.uniform(5, 18) + scale * random.uniform(2, 12), 1)
        strike_rate = round(random.uniform(30, 60) + scale * random.uniform(5, 30), 1)
        runs = int(random.uniform(50, 500) + scale * random.uniform(100, 2000))
        centuries = 0
        bowl_avg = round(random.uniform(18, 35) - scale * random.uniform(0, 8), 1)
        bowl_avg = max(12.0, bowl_avg)
        economy = round(random.uniform(3.0, 6.5) - scale * random.uniform(0, 1.5), 1)
        economy = max(2.5, economy)
        wickets = int(random.uniform(30, 100) + scale * random.uniform(50, 400))
    else:  # All-rounder
        bat_avg = round(random.uniform(18, 28) + scale * random.uniform(5, 20), 1)
        strike_rate = round(random.uniform(55, 75) + scale * random.uniform(5, 35), 1)
        runs = int(random.uniform(500, 2000) + scale * random.uniform(1000, 6000))
        centuries = int(scale * random.uniform(0, 20))
        bowl_avg = round(random.uniform(22, 40) - scale * random.uniform(0, 8), 1)
        bowl_avg = max(15.0, bowl_avg)
        economy = round(random.uniform(3.5, 6.5) - scale * random.uniform(0, 1.0), 1)
        economy = max(3.0, economy)
        wickets = int(random.uniform(20, 80) + scale * random.uniform(30, 250))

    return {
        "bat_avg": bat_avg,
        "strike_rate": strike_rate,
        "runs": runs,
        "centuries": centuries,
        "bowl_avg": bowl_avg,
        "economy": economy,
        "wickets": wickets,
    }


# ── Main seed function ───────────────────────────────────────────────

def seed():
    init_db()

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    session = get_session()

    # Check if already seeded
    existing = session.query(Player).count()
    if existing > 0:
        print(f"⚠️  Database already has {existing} players. Skipping seed.")
        print("   Delete cricket_bot.db and re-run to re-seed.")
        session.close()
        return

    added = 0
    skipped = 0

    for entry in raw_data:
        name = entry.get("Player Name", "").strip()
        if not name:
            skipped += 1
            continue

        try:
            rating = int(entry.get("overall all", 0))
        except (ValueError, TypeError):
            skipped += 1
            continue

        if rating < 50:
            skipped += 1
            continue
        if rating > 100:
            rating = 100

        category = normalise_category(entry.get("Category", "Batsman"))
        bat_hand = parse_bat_hand(entry.get("Batting Style", "Right-handed"))
        bowl_hand = parse_bowl_hand(entry.get("Bowling Style", "Right arm medium fast"))
        bowl_style = parse_bowl_style(entry.get("Bowling Style", "Right arm medium fast"))
        country = entry.get("Country", "Unknown").strip()
        version = entry.get("Version ", "Base card").strip() or "Base card"

        try:
            bat_rating = int(entry.get("Batting Rating", 0))
        except (ValueError, TypeError):
            bat_rating = 0
        try:
            bowl_rating = int(entry.get("Bowling Rating", 0))
        except (ValueError, TypeError):
            bowl_rating = 0

        stats = generate_stats(rating, category, bat_rating, bowl_rating)

        player = Player(
            name=name,
            version=version,
            rating=rating,
            category=category,
            country=country,
            bat_hand=bat_hand,
            bowl_hand=bowl_hand,
            bowl_style=bowl_style,
            bat_rating=bat_rating,
            bowl_rating=bowl_rating,
            bat_avg=stats["bat_avg"],
            strike_rate=stats["strike_rate"],
            runs=stats["runs"],
            centuries=stats["centuries"],
            bowl_avg=stats["bowl_avg"],
            economy=stats["economy"],
            wickets=stats["wickets"],
            is_active=True,
        )
        session.add(player)
        added += 1

    session.commit()
    session.close()

    print(f"✅ Seeded {added:,} players ({skipped} skipped)")

    # Print distribution summary
    session = get_session()
    tiers = [
        (95, 100), (90, 94), (85, 89), (80, 84), (75, 79),
        (70, 74), (65, 69), (60, 64), (55, 59), (50, 54),
    ]
    print("\n   Rating Distribution:")
    for lo, hi in tiers:
        c = session.query(Player).filter(Player.rating >= lo, Player.rating <= hi).count()
        print(f"   {lo:>3}-{hi}: {c:>4} players")
    session.close()


if __name__ == "__main__":
    seed()
