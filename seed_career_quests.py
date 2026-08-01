"""Seed the Career Player weekly quests.

These are the quests only Career Player owners are assigned. Each owner is dealt
:data:`services.quest_service.CAREER_QUESTS_PER_USER` of them a week — two
batting, two bowling and one all-round — and clearing every one of them builds
the streak that pays the gem jackpot.

The catalogue is deliberately much larger than a week's deal. With ~70 quests
and five slots the odds of two owners drawing the same week are negligible, and
the assigner also pushes last week's five to the back of the queue, so a card
stays fresh from one Monday to the next.

Every quest is graded on a tier, which sets what it pays:

    reward_points  flat, one figure for the whole weekly career set
    reward_gems    GameConfig.career_quest_gems x the tier multiplier

so "score 350 runs" is worth four times "score 50", while the website keeps a
single knob for the whole economy.

Idempotent, and self-cleaning — a quest already present by name is updated in
place, and any career weekly quest *not* in this catalogue is deactivated, so
re-running after a rebalance leaves exactly this set live:

    python seed_career_quests.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_session, init_db          # noqa: E402
from models import Quest                           # noqa: E402

# Every weekly career quest pays the same quest points; difficulty is priced in
# gems instead, so the points economy stays comparable across quest types.
CAREER_WEEKLY_REWARD_POINTS = 100

# tier -> gem multiplier
TIERS = {1: 1, 2: 2, 3: 3, 4: 4}

# (name, description, event_key, target, emoji, tier)
#
# Names keep the "Career: " prefix so they are unmistakable in the admin quest
# list, which mixes them with every other quest in the game.
CAREER_QUESTS = [
    # ── Batting: raw runs ────────────────────────────────────────────────
    ("Career: Warming Up",
     "Score 50 runs with your Career Player this week",
     "career_runs_scored", 50, "🏏", 1),
    ("Career: Run Machine",
     "Score 100 runs with your Career Player this week",
     "career_runs_scored", 100, "🏏", 2),
    ("Career: Run Accumulator",
     "Score 200 runs with your Career Player this week",
     "career_runs_scored", 200, "🏏", 3),
    ("Career: Run Mountain",
     "Score 350 runs with your Career Player this week",
     "career_runs_scored", 350, "⛰", 4),

    # ── Batting: fours ───────────────────────────────────────────────────
    ("Career: Finding the Rope",
     "Hit 8 fours with your Career Player",
     "career_fours_hit", 8, "4️⃣", 1),
    ("Career: Four Play",
     "Hit 15 fours with your Career Player",
     "career_fours_hit", 15, "4️⃣", 2),
    ("Career: Boundary Expert",
     "Hit 30 fours with your Career Player",
     "career_fours_hit", 30, "🎯", 3),

    # ── Batting: sixes ───────────────────────────────────────────────────
    ("Career: Clear the Ropes",
     "Hit 5 sixes with your Career Player",
     "career_sixes_hit", 5, "6️⃣", 1),
    ("Career: Maximum Impact",
     "Hit 10 sixes with your Career Player this week",
     "career_sixes_hit", 10, "💥", 2),
    ("Career: Six Machine",
     "Hit 20 sixes with your Career Player",
     "career_sixes_hit", 20, "💥", 3),
    ("Career: Into Orbit",
     "Hit 35 sixes with your Career Player",
     "career_sixes_hit", 35, "🚀", 4),

    # ── Batting: milestones ──────────────────────────────────────────────
    ("Career: Half Century",
     "Score a fifty with your Career Player",
     "career_fifty", 1, "5️⃣", 1),
    ("Career: Fifty Fifty",
     "Score 2 fifties with your Career Player",
     "career_fifty", 2, "5️⃣", 2),
    ("Career: Fifty Specialist",
     "Score 3 half-centuries with your Career Player",
     "career_fifty", 3, "🏅", 3),
    ("Career: Big Hundred",
     "Score a century with your Career Player",
     "career_hundred", 1, "💯", 3),
    ("Career: Twin Tons",
     "Score 2 centuries with your Career Player",
     "career_hundred", 2, "💯", 4),
    ("Career: Seventy Five",
     "Pass 75 in an innings with your Career Player",
     "career_score_75_plus", 1, "🔷", 2),
    ("Career: Big Innings",
     "Pass 75 in an innings twice with your Career Player",
     "career_score_75_plus", 2, "🔷", 4),

    # ── Batting: consistency ─────────────────────────────────────────────
    ("Career: Contributor",
     "Score 25+ in an innings twice with your Career Player",
     "career_score_25_plus", 2, "📈", 1),
    ("Career: Reliable",
     "Score 25+ in an innings 4 times with your Career Player",
     "career_score_25_plus", 4, "📈", 2),
    ("Career: Consistency King",
     "Score 25+ in an innings 8 times with your Career Player",
     "career_score_25_plus", 8, "👑", 4),
    ("Career: Unbeaten",
     "Finish not out twice with your Career Player",
     "career_not_out", 2, "🛡", 1),
    ("Career: Anchor",
     "Finish not out 3 times with your Career Player",
     "career_not_out", 3, "⚓", 2),
    ("Career: Survivor",
     "Remain not out 5 times with your Career Player",
     "career_not_out", 5, "🛡", 3),

    # ── Batting: chasing ─────────────────────────────────────────────────
    ("Career: Second Innings",
     "Score 50 runs batting second with your Career Player",
     "career_chase_runs", 50, "🌙", 2),
    ("Career: Chase Expert",
     "Score 100 runs while chasing with your Career Player",
     "career_chase_runs", 100, "🌙", 3),
    ("Career: Chase Master",
     "Score 200 runs while chasing with your Career Player",
     "career_chase_runs", 200, "🌗", 4),

    # ── Batting: tempo ───────────────────────────────────────────────────
    ("Career: Fast Start",
     "Score 40 runs in innings struck at 150+ (10+ balls faced)",
     "career_quickfire_runs", 40, "⚡", 2),
    ("Career: Quickfire",
     "Score 75 runs in innings struck at 150+ (10+ balls faced)",
     "career_quickfire_runs", 75, "⚡", 3),
    ("Career: Strike Master",
     "Score 150 runs in innings struck at 150+ (10+ balls faced)",
     "career_quickfire_runs", 150, "🔥", 4),
    ("Career: Rope Burner",
     "Take 60 of your runs in fours and sixes",
     "career_boundary_runs", 60, "🪢", 2),
    ("Career: Boundary Blitz",
     "Take 120 of your runs in fours and sixes",
     "career_boundary_runs", 120, "🎆", 3),
    ("Career: Boundary Barrage",
     "Take 220 of your runs in fours and sixes",
     "career_boundary_runs", 220, "🎆", 4),

    # ── Batting: time at the crease ──────────────────────────────────────
    ("Career: Occupation",
     "Face 80 balls with your Career Player this week",
     "career_balls_faced", 80, "⏳", 2),
    ("Career: Time at the Crease",
     "Face 150 balls with your Career Player this week",
     "career_balls_faced", 150, "⏳", 3),
    ("Career: Marathon Man",
     "Face 260 balls with your Career Player this week",
     "career_balls_faced", 260, "🕰", 4),

    # ── Bowling: wickets ─────────────────────────────────────────────────
    ("Career: Wicket Hunter",
     "Take 5 wickets with your Career Player this week",
     "career_wickets_taken", 5, "🎳", 1),
    ("Career: Strike Force",
     "Take 10 wickets with your Career Player this week",
     "career_wickets_taken", 10, "🔥", 2),
    ("Career: Wicket Machine",
     "Take 20 wickets with your Career Player this week",
     "career_wickets_taken", 20, "☠️", 3),
    ("Career: Destroyer",
     "Take 30 wickets with your Career Player this week",
     "career_wickets_taken", 30, "💀", 4),

    # ── Bowling: hauls ───────────────────────────────────────────────────
    ("Career: Three For",
     "Take 3 wickets in a single match with your Career Player",
     "career_three_fer", 1, "3️⃣", 2),
    ("Career: Triple Threat",
     "Take 3 wickets in a match on 3 occasions",
     "career_three_fer", 3, "3️⃣", 4),
    ("Career: Five For",
     "Take 5 wickets in a single match with your Career Player",
     "career_five_fer", 1, "5️⃣", 4),
    ("Career: Hat-trick Hero",
     "Take a hat-trick with your Career Player",
     "career_hattrick", 1, "🎩", 4),

    # ── Bowling: control ─────────────────────────────────────────────────
    ("Career: Tight Lines",
     "Bowl 15 dot balls with your Career Player",
     "career_dot_balls", 15, "🔹", 1),
    ("Career: Pressure Builder",
     "Bowl 30 dot balls with your Career Player",
     "career_dot_balls", 30, "🔹", 2),
    ("Career: Dot Ball King",
     "Bowl 60 dot balls with your Career Player",
     "career_dot_balls", 60, "👑", 3),
    ("Career: The Squeeze",
     "Bowl 100 dot balls with your Career Player",
     "career_dot_balls", 100, "🗜", 4),
    ("Career: First Maiden",
     "Bowl a maiden over with your Career Player",
     "career_maiden_overs", 1, "🅼", 1),
    ("Career: Maiden Specialist",
     "Bowl 3 maiden overs with your Career Player",
     "career_maiden_overs", 3, "🅼", 3),
    ("Career: Maiden Master",
     "Bowl 6 maiden overs with your Career Player",
     "career_maiden_overs", 6, "🏵", 4),
    ("Career: Miser",
     "Bowl a 4-over spell at an economy under 6",
     "career_economy_spell", 1, "🪙", 2),
    ("Career: Economy Class",
     "Bowl two 4-over spells at an economy under 6",
     "career_economy_spell", 2, "📉", 3),
    ("Career: Untouchable",
     "Bowl four 4-over spells at an economy under 6",
     "career_economy_spell", 4, "🧊", 4),

    # ── Bowling: workload ────────────────────────────────────────────────
    ("Career: Workhorse",
     "Bowl 20 overs with your Career Player this week",
     "career_balls_bowled", 120, "🐴", 2),
    ("Career: Ever Present",
     "Bowl 40 overs with your Career Player this week",
     "career_balls_bowled", 240, "🐴", 3),
    ("Career: Iron Arm",
     "Bowl 60 overs with your Career Player this week",
     "career_balls_bowled", 360, "💪", 4),

    # ── All-round: appearances ───────────────────────────────────────────
    ("Career: Match Fit",
     "Play 3 matches with your Career Player in the XI",
     "career_match_played", 3, "🎖", 1),
    ("Career: Regular Starter",
     "Play 5 matches with your Career Player in the XI",
     "career_match_played", 5, "⚡", 2),
    ("Career: Ever Selected",
     "Play 8 matches with your Career Player in the XI",
     "career_match_played", 8, "📋", 3),
    ("Career: Season Long",
     "Play 12 matches with your Career Player in the XI",
     "career_match_played", 12, "🗂", 4),

    # ── All-round: winning ───────────────────────────────────────────────
    ("Career: Winning Feeling",
     "Win 2 matches with your Career Player in the XI",
     "career_match_won", 2, "🏆", 1),
    ("Career: Winning Habit",
     "Win 5 matches with your Career Player in the XI",
     "career_match_won", 5, "🏆", 3),
    ("Career: Champion Week",
     "Win 8 matches with your Career Player in the XI",
     "career_match_won", 8, "👑", 4),

    # ── All-round: the real thing ────────────────────────────────────────
    ("Career: Genuine All-Rounder",
     "Score 25+ and take 2+ wickets in the same match",
     "career_allrounder_match", 1, "⭐", 3),
    ("Career: Both Departments",
     "Score 25+ and take 2+ wickets in the same match, twice",
     "career_allrounder_match", 2, "⚖️", 3),
    ("Career: Double Threat",
     "Score 25+ and take 2+ wickets in the same match, 3 times",
     "career_allrounder_match", 3, "🌟", 4),
    ("Career: Star of the Show",
     "Win Player of the Match with your Career Player",
     "career_potm", 1, "🌟", 3),
    ("Career: Match Winner",
     "Win Player of the Match 3 times with your Career Player",
     "career_potm", 3, "🏅", 4),
]


def career_quest_gems(session):
    """Base gems a career weekly quest pays, from the website config."""
    try:
        from services.config_service import get_config
        return int(get_config(session).get("career_quest_gems") or 15)
    except Exception:
        return 15


def seed(session):
    """Create or refresh the career weekly quests. Caller commits.

    Returns ``(created, updated, retired)``.
    """
    base_gems = career_quest_gems(session)
    created = updated = 0
    for order, (name, description, event_key, target,
                emoji, tier) in enumerate(CAREER_QUESTS, start=1):
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
        quest.reward_points = CAREER_WEEKLY_REWARD_POINTS
        quest.reward_coins = 0
        quest.reward_gems = base_gems * TIERS[tier]
        quest.is_active = True
        quest.always_assign = False
        quest.career_only = True
        quest.emoji = emoji
        quest.sort_order = order * 10

    # Retire anything career-weekly that this catalogue no longer defines, so a
    # rebalance does not leave an older generation of quests in the draw. They
    # are deactivated rather than deleted: users may hold progress rows against
    # them, and /mq reads those by quest id.
    keep = {name for name, *_rest in CAREER_QUESTS}
    live = (session.query(Quest)
            .filter(Quest.quest_type == "weekly",
                    Quest.career_only.is_(True),
                    Quest.is_active.is_(True))
            .all())
    retired = 0
    for quest in live:
        if quest.name not in keep:
            quest.is_active = False
            retired += 1
    return created, updated, retired


def main():
    init_db()
    session = get_session()
    try:
        created, updated, retired = seed(session)
        session.commit()
        base = career_quest_gems(session)
        from services.quest_service import CAREER_QUESTS_PER_USER
        print(f"✅ Career weekly quests: {created} created, {updated} updated, "
              f"{retired} retired — {len(CAREER_QUESTS)} live.")
        print(f"   {CAREER_WEEKLY_REWARD_POINTS} quest points each; "
              f"{base}–{base * 4} 💎 by tier.")
        print(f"   Each owner is dealt {CAREER_QUESTS_PER_USER} a week "
              f"(2 batting, 2 bowling, 1 all-round).")
    finally:
        session.close()


if __name__ == "__main__":
    main()
