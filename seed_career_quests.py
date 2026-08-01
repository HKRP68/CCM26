"""Seed the Career Player weekly quests.

These are the quests only Career Player owners are assigned, and clearing all of
them in a week builds the streak that pays the gem jackpot. Each pays the gem
amount configured on the website (``GameConfig.career_quest_gems``, default 15).

Idempotent — a quest already present by name is updated in place rather than
duplicated, so this is safe to re-run after a rebalance:

    python seed_career_quests.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_session, init_db          # noqa: E402
from models import Quest                           # noqa: E402
from services.quest_service import (               # noqa: E402
    WEEKLY_DEFAULT_REWARD_POINTS,
)

# (name, description, event_key, target, emoji, sort_order)
CAREER_QUESTS = [
    ("Career: Match Fit",
     "Play 3 matches with your Career Player in the XI",
     "career_match_played", 3, "🎖", 10),
    ("Career: Run Machine",
     "Score 100 runs with your Career Player this week",
     "career_runs_scored", 100, "🏏", 20),
    ("Career: Wicket Hunter",
     "Take 5 wickets with your Career Player this week",
     "career_wickets_taken", 5, "🎳", 30),
    ("Career: Half Century",
     "Score a fifty with your Career Player",
     "career_fifty", 1, "5️⃣", 40),
    ("Career: Maximum Impact",
     "Hit 10 sixes with your Career Player this week",
     "career_sixes_hit", 10, "💥", 50),
    ("Career: All-Round Week",
     "Play 5 matches with your Career Player in the XI",
     "career_match_played", 5, "⚡", 60),
    ("Career: Big Hundred",
     "Score a century with your Career Player",
     "career_hundred", 1, "💯", 70),
    ("Career: Strike Force",
     "Take 10 wickets with your Career Player this week",
     "career_wickets_taken", 10, "🔥", 80),
]


def career_quest_gems(session):
    """Gems each career weekly quest pays, from the website config."""
    try:
        from services.config_service import get_config
        return int(get_config(session).get("career_quest_gems") or 15)
    except Exception:
        return 15


def seed(session):
    """Create or refresh the career weekly quests. Caller commits."""
    gems = career_quest_gems(session)
    created = updated = 0
    for name, description, event_key, target, emoji, order in CAREER_QUESTS:
        quest = session.query(Quest).filter(Quest.name == name).first()
        if quest is None:
            quest = Quest(name=name)
            session.add(quest)
            created += 1
        else:
            updated += 1
        quest.description = description
        quest.quest_type = "weekly"
        quest.event_key = event_key
        quest.target_count = target
        quest.reward_points = WEEKLY_DEFAULT_REWARD_POINTS
        quest.reward_coins = 0
        quest.reward_gems = gems
        quest.is_active = True
        quest.always_assign = False
        quest.career_only = True
        quest.emoji = emoji
        quest.sort_order = order
    return created, updated


def main():
    init_db()
    session = get_session()
    try:
        created, updated = seed(session)
        session.commit()
        gems = career_quest_gems(session)
        print(f"✅ Career weekly quests: {created} created, {updated} updated "
              f"— {gems} 💎 each.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
