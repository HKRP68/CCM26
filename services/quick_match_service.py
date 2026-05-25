"""Phase-based quick match simulation for the Mini App.

A full 20-over T20 compressed into 3 phases per innings:
  - Powerplay (overs 1-6, ~36 balls)
  - Middle (overs 7-15, ~54 balls)
  - Death (overs 16-20, ~30 balls)

For each phase, the user picks ONE of: aggressive / balanced / defensive.
That choice modifies the phase's run rate and wicket probability. Total
taps per match: 1 (toss) + 6 (3 phases × 2 innings) = 7.

This is intentionally lightweight — a real ball-by-ball sim lives in
match_engine.py / probability_engine.py. Quick match is for users who
want a 60-second match.
"""

import random
from datetime import datetime


# Base balls per phase (20-over T20 split: 6/9/5 overs)
PHASE_DEFS = [
    {"name": "Powerplay", "balls": 36, "base_rr": 7.5, "base_wickets": 0.85},
    {"name": "Middle",    "balls": 54, "base_rr": 6.5, "base_wickets": 0.95},
    {"name": "Death",     "balls": 30, "base_rr": 9.5, "base_wickets": 1.20},
]

# Choice modifiers: (rr_mult, wicket_mult)
CHOICE_MODS = {
    "aggressive": (1.30, 1.55),  # higher runs, much higher risk
    "balanced":   (1.00, 1.00),
    "defensive":  (0.75, 0.55),  # fewer runs, much safer
}


def simulate_phase(phase, choice, batting_rating, bowling_rating, wickets_left):
    """Return (runs, wickets_lost) for one phase.

    `phase` is one of PHASE_DEFS, `choice` is 'aggressive'/'balanced'/'defensive'.
    `batting_rating` and `bowling_rating` are avg ratings of the relevant teams.

    Algorithm:
      - Compute expected run rate, modified by choice + bat/bowl skill gap
      - Compute expected wickets, modified by choice + skill gap
      - Add a bit of randomness so two identical matches differ
    """
    rr_mult, wkt_mult = CHOICE_MODS.get(choice, (1.0, 1.0))

    # Skill differential: +/- 15 OVR shifts RR by about ±15%
    skill_diff = (batting_rating - bowling_rating) / 100.0
    skill_rr_mult = 1.0 + skill_diff
    skill_wkt_mult = 1.0 - skill_diff * 0.7  # better batter = fewer wickets

    # Expected runs in this phase
    overs = phase["balls"] / 6.0
    expected_runs = phase["base_rr"] * overs * rr_mult * skill_rr_mult
    # Random variance: ±25%
    actual_runs = max(0, int(expected_runs * random.uniform(0.75, 1.25)))

    # Expected wickets
    expected_wkts = phase["base_wickets"] * wkt_mult * skill_wkt_mult
    # Roll for each wicket; each independent
    wickets_lost = 0
    remaining = wickets_left
    # Sample from a Poisson-ish distribution: bound to remaining wickets
    while remaining > 0 and wickets_lost < int(expected_wkts * 1.5 + 1):
        if random.random() < expected_wkts / 3.0:
            wickets_lost += 1
            remaining -= 1
            expected_wkts -= 1.0
            if expected_wkts <= 0:
                break
        else:
            break

    return actual_runs, wickets_lost


def simulate_innings(choices, batting_rating, bowling_rating, target=None):
    """Simulate a 3-phase innings given the user's phase choices.

    Returns dict:
      {phases: [{name, choice, runs, wickets_lost, score_after, balls_after}, ...],
       total_runs, total_wickets, balls_faced, all_out, won, chasing_target}
    """
    runs_total = 0
    wickets_total = 0
    wickets_left = 10
    balls_faced = 0
    phase_results = []

    for i, phase in enumerate(PHASE_DEFS):
        choice = choices[i] if i < len(choices) else "balanced"
        if wickets_left <= 0:
            phase_results.append({
                "name": phase["name"], "choice": choice,
                "runs": 0, "wickets_lost": 0,
                "score_after": runs_total, "wickets_after": wickets_total,
                "balls_after": balls_faced, "all_out": True,
            })
            continue

        runs, wkts = simulate_phase(phase, choice, batting_rating, bowling_rating, wickets_left)
        wkts = min(wkts, wickets_left)

        # Target chase short-circuit: stop if we've passed the target
        if target is not None and (runs_total + runs) >= target:
            runs = target - runs_total  # exact runs needed
            balls_faced += int(phase["balls"] * (runs / max(1, runs)))  # all balls used
            wkts = 0  # didn't lose the wicket on the winning ball usually
            runs_total += runs
            wickets_total += wkts
            wickets_left -= wkts
            phase_results.append({
                "name": phase["name"], "choice": choice,
                "runs": runs, "wickets_lost": wkts,
                "score_after": runs_total, "wickets_after": wickets_total,
                "balls_after": balls_faced, "chased": True,
            })
            break

        runs_total += runs
        wickets_total += wkts
        wickets_left -= wkts
        balls_faced += phase["balls"]
        phase_results.append({
            "name": phase["name"], "choice": choice,
            "runs": runs, "wickets_lost": wkts,
            "score_after": runs_total, "wickets_after": wickets_total,
            "balls_after": balls_faced,
        })

    return {
        "phases": phase_results,
        "total_runs": runs_total,
        "total_wickets": wickets_total,
        "balls_faced": balls_faced,
        "all_out": wickets_left <= 0,
        "chased": target is not None and runs_total >= (target or 0),
    }


def get_user_team_rating(session, user_id, top_n=11):
    """Average rating of the user's top N players by rating.

    Default top 11. If user has fewer than N players, just averages what
    they have (or returns 60 as a floor).
    """
    from models import UserRoster, Player
    rows = (session.query(Player.rating)
            .join(UserRoster, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user_id)
            .order_by(Player.rating.desc()).limit(top_n).all())
    if not rows:
        return 60
    return sum(r[0] for r in rows) / len(rows)


def play_quick_match(session, user, user_choices, opponent_difficulty="medium"):
    """Run a full quick match.

    Args:
      user_choices: dict {
        toss_choice: 'bat' | 'bowl',
        innings_1: [phase1, phase2, phase3],
        innings_2: [phase1, phase2, phase3],
      }
      opponent_difficulty: 'easy' (-10 OVR) / 'medium' (=) / 'hard' (+10 OVR)

    Returns full match result dict.
    """
    user_rating = get_user_team_rating(session, user.id)

    # Bot's rating depends on difficulty + user's level
    diff_mod = {"easy": -10, "medium": 0, "hard": +10}.get(opponent_difficulty, 0)
    bot_rating = max(50, min(100, user_rating + diff_mod))

    # Toss: 50/50; if user picks bat, they bat first
    toss_won = random.random() < 0.5
    toss_choice = user_choices.get("toss_choice", "bat")
    if toss_won:
        # User won toss → their preference holds
        user_bats_first = (toss_choice == "bat")
    else:
        # Bot won toss → flips a coin too
        user_bats_first = (random.random() < 0.5)

    # Bot phase strategy: simple — match user difficulty or random
    bot_choices = [random.choice(["aggressive", "balanced", "defensive"])
                   for _ in range(3)]
    bot_choices_2 = [random.choice(["aggressive", "balanced", "defensive"])
                     for _ in range(3)]

    user_choices_1 = user_choices.get("innings_1", ["balanced"] * 3)
    user_choices_2 = user_choices.get("innings_2", ["balanced"] * 3)

    # Innings 1
    if user_bats_first:
        inn1 = simulate_innings(user_choices_1, user_rating, bot_rating)
        target = inn1["total_runs"] + 1
        # Bot reacts to target — chase aggressively if behind
        if target > 180:
            bot_choices_2 = ["aggressive", "aggressive", "aggressive"]
        elif target > 140:
            bot_choices_2 = ["balanced", "balanced", "aggressive"]
        inn2 = simulate_innings(bot_choices_2, bot_rating, user_rating, target=target)
    else:
        # Bot bats first
        inn1 = simulate_innings(bot_choices, bot_rating, user_rating)
        target = inn1["total_runs"] + 1
        inn2 = simulate_innings(user_choices_2, user_rating, bot_rating, target=target)

    # Determine winner
    inn2_runs = inn2["total_runs"]
    if inn2_runs >= target:
        # Second innings team chased successfully
        user_won = not user_bats_first  # user batted second AND won
    elif inn2_runs == target - 1:
        # Tied
        user_won = None
    else:
        user_won = user_bats_first

    # Award coins/gems (light — quick match shouldn't grind)
    coin_reward = 0
    if user_won is True:
        coin_reward = 500
    elif user_won is None:
        coin_reward = 200

    return {
        "toss_won": toss_won,
        "user_bats_first": user_bats_first,
        "user_team_rating": round(user_rating, 1),
        "opponent_team_rating": round(bot_rating, 1),
        "innings_1": inn1,
        "innings_2": inn2,
        "target": target,
        "user_won": user_won,
        "coin_reward": coin_reward,
        "played_at": datetime.utcnow().isoformat(),
    }
