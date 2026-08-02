"""Seed the Daily and Monthly quest catalogue.

Replaces the markdown bulk-import (``seed_quests_v2.py`` + ``data/quests_table.md``)
that produced the live catalogue. That import guessed an event key out of each
quest's English description, and anything it could not parse — powerplay
scoring, catches, run-outs, yorkers, super overs, "70% win rate" — became
``event_key='manual'``: a quest with no trigger behind it, whose bar can only
move if an admin types a number in by hand.

Users are dealt three daily and five monthly quests at random out of that
catalogue, so on a normal day one or two of a player's three quests were ones
that could not be completed by playing. That is the "quest is not counting"
report, and no amount of event plumbing fixes it while the catalogue is full of
quests nothing fires.

So this catalogue is hand-written under one rule: **every quest maps to an event
the game actually fires**, and the target is reachable inside the quest's
period. Each entry names its trigger explicitly rather than having one inferred.

Cumulative triggers count over the quest's own period, so the same trigger
reads differently by type — ``wickets_taken`` is "wickets today" on a Daily and
"Wickets Taken in Month" on a Monthly.

Difficulty is graded on a tier, which prices the reward:

    reward_points  base points x tier
    reward_coins   base coins  x tier
    reward_gems    monthly only, base gems x tier

Idempotent and self-cleaning, exactly like ``seed_career_quests.py``: a quest
already present by name is updated in place, and any *other* active daily or
monthly quest is deactivated so the legacy import stops being dealt. Pinned
quests (``always_assign``, e.g. the rewarded-ad quest) and Career Player quests
are never touched.

    python seed_quests_v3.py            # replace the daily/monthly catalogue
    python seed_quests_v3.py --dry-run  # report what would change
    python seed_quests_v3.py --keep-legacy   # add/update only, retire nothing
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_session, init_db          # noqa: E402
from models import Quest                           # noqa: E402

# Base rewards, multiplied by the quest's tier.
DAILY_BASE = {"points": 10, "coins": 400, "gems": 0}
MONTHLY_BASE = {"points": 50, "coins": 1500, "gems": 3}

# (name, description, event_key, target, emoji, tier)
#
# Targets assume the three-a-day / five-a-month draw in
# services.quest_service.QUESTS_PER_USER, so a tier-1 daily is one good match
# and a tier-4 monthly is a month of steady play.
DAILY_QUESTS = [
    # ── Turning up ───────────────────────────────────────────────────────
    ("Take the Field",
     "Play 1 match today", "match_played", 1, "🏏", 1),
    ("Triple Header",
     "Play 3 matches today", "match_played", 3, "🏟", 2),
    ("Day's Work",
     "Win 1 match today", "match_won", 1, "🏆", 1),
    ("Back-to-Back",
     "Win 2 matches today", "match_won", 2, "🏆", 2),
    ("Perfect Day",
     "Win 4 matches today", "match_won", 4, "👑", 3),

    # ── Batting: runs ────────────────────────────────────────────────────
    ("Get Off the Mark",
     "Score 60 runs today across all your matches", "runs_scored", 60, "🏏", 1),
    ("Runs on the Board",
     "Score 150 runs today across all your matches", "runs_scored", 150, "🏏", 2),
    ("Runs Galore",
     "Score 300 runs today across all your matches", "runs_scored", 300, "📈", 3),
    ("Score a Quick Fifty",
     "Have a batter reach 50 in a match today", "fifty", 1, "5️⃣", 2),
    ("Two Half-Centuries",
     "Have batters reach 50 twice today", "fifty", 2, "5️⃣", 3),
    ("Century Maker",
     "Have a batter reach 100 in a match today", "hundred", 1, "💯", 3),
    ("Big Knock",
     "Have one batter score 70+ in a single innings today",
     "runs_in_innings", 70, "🔥", 2),

    # ── Batting: boundaries ──────────────────────────────────────────────
    ("Hit 6 Sixes",
     "Hit 6 sixes today across all your matches", "sixes_hit", 6, "6️⃣", 1),
    ("Six Machine",
     "Hit 15 sixes today across all your matches", "sixes_hit", 15, "💥", 2),
    ("Six Storm",
     "Hit 8 sixes with one batter in a single innings today",
     "sixes_in_match", 8, "🌪", 3),
    ("Find the Rope",
     "Hit 10 fours today across all your matches", "fours_hit", 10, "4️⃣", 1),
    ("Boundary Rider",
     "Hit 25 boundaries (4s + 6s) today", "boundaries_hit", 25, "💫", 2),
    ("Boundary Blitz",
     "Hit 12 boundaries (4s + 6s) with one batter in an innings today",
     "boundaries_in_match", 12, "⚡", 3),

    # ── Batting: phases and situations ───────────────────────────────────
    ("Powerplay Master",
     "Score 45+ in the first 6 overs of one of your innings today",
     "powerplay_runs", 45, "⚡", 2),
    ("Powerplay Blaster",
     "Score 60+ in the first 6 overs of one of your innings today",
     "powerplay_runs", 60, "⚡", 3),
    ("Death Over Destroyer",
     "Score 50+ in the last 5 overs of one of your innings today",
     "death_over_runs", 50, "💀", 3),
    ("Unbeaten Knock",
     "Finish an innings with a batter not out today", "not_out_innings", 1,
     "🛡", 1),
    ("Comeback Kid",
     "Win a match batting second today", "chase_won", 1, "🔥", 2),
    ("Hold Your Nerve",
     "Win a match batting first today", "defended_total", 1, "🧱", 2),

    # ── Bowling ──────────────────────────────────────────────────────────
    ("Take 3 Wickets in a Match",
     "Take 3 wickets with one bowler in a match today",
     "wickets_in_match", 3, "🎳", 2),
    ("Five-For",
     "Take 5 wickets with one bowler in a match today",
     "wickets_in_match", 5, "🖐", 3),
    ("Wicket Hunt",
     "Take 5 wickets today across all your matches", "wickets_taken", 5,
     "🎳", 1),
    ("Wicket Rush",
     "Take 12 wickets today across all your matches", "wickets_taken", 12,
     "🎯", 2),
    ("Clean Sheet",
     "Bowl a maiden over today", "maiden_over", 1, "🚫", 1),
    ("Dot Ball Pressure",
     "Bowl 30 dot balls today", "dot_balls", 30, "⚫", 2),
    ("Economy King",
     "Bowl a 4-over spell under 5.00 an over today", "economy_under_5", 1,
     "🛡", 2),
    ("Economy Assassin",
     "Bowl a 4-over spell under 4.50 an over today", "economy_under_4_5", 1,
     "🥷", 3),
    ("Three-Fer",
     "Have a bowler take 3+ wickets in a match today", "three_fer", 1,
     "🎳", 2),

    # ── All-round and rewards ────────────────────────────────────────────
    ("All-Rounder Performance",
     "Score 30+ runs AND take 2+ wickets in the same match today",
     "allrounder_match", 1, "🌟", 2),
    ("Daily Collector",
     "Collect your /daily reward", "daily", 1, "📅", 1),
    ("Spin the Wheel",
     "Use /gspin once today", "gspin", 1, "🎡", 1),
    ("Squad Builder",
     "Claim a player with /claim today", "claim", 1, "📇", 1),
    ("Trait Fitter",
     "Apply a trait with /traitapply today", "trait_apply", 1, "💠", 1),
    ("Pack Ripper",
     "Open a pack today", "pack_open", 1, "🎁", 1),
]

MONTHLY_QUESTS = [
    # ── Volume ───────────────────────────────────────────────────────────
    ("Season Regular",
     "Play 20 matches this month", "match_played", 20, "🏟", 1),
    ("Ever Present",
     "Play 60 matches this month", "match_played", 60, "🏟", 3),
    ("Winning Month",
     "Win 10 matches this month", "match_won", 10, "🏆", 1),
    ("Win Streak",
     "Win 25 matches this month", "match_won", 25, "🏆", 2),
    ("Title Chase",
     "Win 50 matches this month", "match_won", 50, "👑", 4),

    # ── Batting ──────────────────────────────────────────────────────────
    ("Run Machine",
     "Score 1,500 runs this month", "runs_scored", 1500, "🏏", 2),
    ("Run Mountain",
     "Score 4,000 runs this month", "runs_scored", 4000, "⛰", 4),
    ("Half-Century Habit",
     "Score ten 50s this month", "fifty", 10, "5️⃣", 2),
    ("Century Crusher",
     "Score 3 centuries this month", "hundred", 3, "💯", 3),
    ("Hundred Club",
     "Score 8 centuries this month", "hundred", 8, "💯", 4),
    ("Six Machine",
     "Hit 100 sixes this month", "sixes_hit", 100, "6️⃣", 2),
    ("Six Legend",
     "Hit 250 sixes this month", "sixes_hit", 250, "💥", 4),
    ("Four Hundred Fours",
     "Hit 400 fours this month", "fours_hit", 400, "4️⃣", 4),
    ("Boundary Season",
     "Hit 300 boundaries (4s + 6s) this month", "boundaries_hit", 300,
     "💫", 2),
    ("Monster Innings",
     "Score 120+ with one batter in a single innings this month",
     "runs_in_innings", 120, "🔥", 3),
    ("Boundary Barrage",
     "Hit 15 boundaries (4s + 6s) with one batter in an innings this month",
     "boundaries_in_match", 15, "⚡", 3),

    # ── Bowling — including the Wickets Taken in Month ladder ────────────
    ("Wickets Taken in Month",
     "Take 25 wickets this month", "wickets_taken", 25, "🎳", 1),
    ("Wicket Machine",
     "Take 50 wickets this month", "wickets_taken", 50, "🎳", 2),
    ("Wicket Monster",
     "Take 100 wickets this month", "wickets_taken", 100, "🎯", 3),
    ("Wicket Legend",
     "Take 200 wickets this month", "wickets_taken", 200, "🏅", 4),
    ("Three-Fer Collector",
     "Have a bowler take 3+ wickets in a match, 10 times this month",
     "three_fer", 10, "🎳", 2),
    ("Five-For Club",
     "Have a bowler take 5+ wickets in a match, 3 times this month",
     "five_fer", 3, "🖐", 3),
    ("Hat-Trick Hero",
     "Take a hat-trick this month", "hattrick", 1, "🎩", 4),
    ("Clean Sheet Bowler",
     "Bowl 15 maiden overs this month", "maiden_over", 15, "🚫", 2),
    ("Dot Ball Dominance",
     "Bowl 600 dot balls this month", "dot_balls", 600, "⚫", 3),
    ("Miser",
     "Bowl 10 spells under 5.00 an over this month", "economy_under_5", 10,
     "🛡", 3),

    # ── Situations ───────────────────────────────────────────────────────
    ("Comeback King",
     "Win 5 matches chasing this month", "chase_won", 5, "🔥", 2),
    ("Fortress",
     "Win 10 matches defending a total this month", "defended_total", 10,
     "🧱", 3),
    ("Powerplay Season",
     "Score 70+ in the first 6 overs of an innings this month",
     "powerplay_runs", 70, "⚡", 3),
    ("Finisher",
     "Score 60+ in the last 5 overs of an innings this month",
     "death_over_runs", 60, "💀", 3),
    ("Super Over Hero",
     "Win a Super Over this month", "super_over_won", 1, "🥇", 4),
    ("All-Rounder Legend",
     "Score 30+ and take 2+ wickets in the same match, 10 times this month",
     "allrounder_match", 10, "🌟", 3),

    # ── Off the field ────────────────────────────────────────────────────
    ("Daily Devotion",
     "Collect your /daily reward 20 times this month", "daily", 20, "📅", 2),
    ("Market Mover",
     "Buy 5 players from /playermarket this month", "market_buy", 5, "🛒", 2),
    ("Trait Investor",
     "Buy 10 traits from /traitshop this month", "trait_buy", 10, "💠", 2),
    ("Squad Depth",
     "Claim 20 players with /claim this month", "claim", 20, "📇", 2),
]


def _rewards(base, tier):
    """Scale a base reward by a quest's difficulty tier (1-4)."""
    return {
        "points": base["points"] * tier,
        "coins": base["coins"] * tier,
        "gems": base["gems"] * tier,
    }


def seed(session, *, retire_legacy=True, dry_run=False):
    """Create or refresh the daily/monthly catalogue. Caller commits.

    Returns ``{"created", "updated", "retired"}``.
    """
    counts = {"created": 0, "updated": 0, "retired": 0}
    order = 0

    for quest_type, catalogue, base in (
        ("daily", DAILY_QUESTS, DAILY_BASE),
        ("monthly", MONTHLY_QUESTS, MONTHLY_BASE),
    ):
        for name, description, event_key, target, emoji, tier in catalogue:
            order += 1
            # Matched on name + type: the two catalogues deliberately reuse a
            # few names ("Six Machine" is a daily and a monthly), and they are
            # different quests with different targets.
            quest = (session.query(Quest)
                     .filter(Quest.name == name,
                             Quest.quest_type == quest_type).first())
            if quest is None:
                counts["created"] += 1
                if dry_run:
                    continue
                quest = Quest(name=name, quest_type=quest_type)
                session.add(quest)
            else:
                counts["updated"] += 1
                if dry_run:
                    continue
            reward = _rewards(base, tier)
            quest.description = description
            quest.quest_type = quest_type
            quest.event_key = event_key
            quest.target_count = target
            quest.reward_points = reward["points"]
            quest.reward_coins = reward["coins"]
            quest.reward_gems = reward["gems"]
            quest.is_active = True
            quest.career_only = False
            quest.emoji = emoji
            quest.sort_order = order * 10

    if not retire_legacy:
        return counts

    # Retire every other active daily/monthly quest, so the untrackable legacy
    # import stops being dealt. Deactivated rather than deleted: users hold
    # progress rows against them and /mq reads those by quest id.
    #
    # Two kinds are deliberately spared:
    #   - pinned quests (always_assign), which an admin put in the rotation on
    #     purpose and which include the manually-driven ones like the ad quest;
    #   - career_only quests, which belong to seed_career_quests.py.
    keep = {(name, "daily") for name, *_r in DAILY_QUESTS}
    keep |= {(name, "monthly") for name, *_r in MONTHLY_QUESTS}
    live = (session.query(Quest)
            .filter(Quest.quest_type.in_(("daily", "monthly")),
                    Quest.is_active.is_(True),
                    Quest.always_assign.is_(False),
                    Quest.career_only.is_(False))
            .all())
    for quest in live:
        if (quest.name, quest.quest_type) in keep:
            continue
        counts["retired"] += 1
        if not dry_run:
            quest.is_active = False
    return counts


def main():
    """Command-line entry point — see the module docstring for the flags."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    parser.add_argument("--keep-legacy", action="store_true",
                        help="add/update only; leave existing quests active")
    args = parser.parse_args()

    init_db()
    session = get_session()
    try:
        counts = seed(session, retire_legacy=not args.keep_legacy,
                      dry_run=args.dry_run)
        if not args.dry_run:
            session.commit()
        prefix = "would " if args.dry_run else ""
        print(f"✅ Daily/Monthly quests: {prefix}create {counts['created']}, "
              f"{prefix}update {counts['updated']}, "
              f"{prefix}retire {counts['retired']} legacy.")
        print(f"   {len(DAILY_QUESTS)} daily + {len(MONTHLY_QUESTS)} monthly live, "
              f"every one on an auto-tracked event trigger.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
