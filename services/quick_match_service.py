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


# ── XI-based simulation (v2) ──────────────────────────────────────────

def get_user_xi(session, user_id):
    """Return the user's playing XI (positions 1-11), sorted by position.

    Returns: list of dicts with {roster_id, player_id, name, rating,
    category, country, version, position, is_captain}.
    """
    from models import UserRoster, User
    user = session.query(User).get(user_id)
    captain_rid = user.captain_roster_id if user else None
    from models import Player as _P
    rows = (session.query(UserRoster, _P)
            .join(_P, UserRoster.player_id == _P.id)
            .filter(UserRoster.user_id == user_id,
                    UserRoster.order_position >= 1,
                    UserRoster.order_position <= 11)
            .order_by(UserRoster.order_position.asc()).all())
    return [{
        "roster_id": r.id, "player_id": p.id, "name": p.name,
        "rating": p.rating, "category": p.category, "country": p.country,
        "version": p.version or "Base",
        "position": r.order_position,
        "is_captain": (captain_rid == r.id),
    } for r, p in rows]


def _generate_bot_xi(session, target_rating, opponent_country=None):
    """Pick 11 players matching cricket XI rules to make a bot opponent.

    target_rating: average rating to aim for (so the bot scales to user).
    opponent_country: prefer players from a different country than user.

    Returns: list of dicts (same shape as get_user_xi minus roster_id).
    """
    from models import Player
    # We try to assemble a valid XI: 4 bat, 4 bowl, 1 wk, 2 ar (default).
    # If the player pool is small, fall back to whatever's available.
    target_counts = {
        "Batsman": 4, "Bowler": 4, "Wicket Keeper": 1, "All-rounder": 2,
    }
    bot_xi = []
    used_ids = set()

    for cat, want in target_counts.items():
        # Window of ratings around target — wider if pool is small
        for window in (5, 10, 15, 20, 100):
            cands = (session.query(Player)
                     .filter(Player.is_active == True,
                             Player.category == cat,
                             Player.rating >= max(50, target_rating - window),
                             Player.rating <= min(100, target_rating + window),
                             ~Player.id.in_(used_ids) if used_ids else True)
                     .all())
            if len(cands) >= want:
                # Random pick (so bot team varies)
                random.shuffle(cands)
                picked = cands[:want]
                for p in picked:
                    bot_xi.append({
                        "player_id": p.id, "name": p.name, "rating": p.rating,
                        "category": p.category, "country": p.country,
                        "version": p.version or "Base",
                        "position": len(bot_xi) + 1,
                        "is_captain": False,
                    })
                    used_ids.add(p.id)
                break
        else:
            # Couldn't find enough — fill from anywhere
            cands = (session.query(Player)
                     .filter(Player.is_active == True, Player.category == cat,
                             ~Player.id.in_(used_ids) if used_ids else True)
                     .all())
            random.shuffle(cands)
            for p in cands[:want]:
                bot_xi.append({
                    "player_id": p.id, "name": p.name, "rating": p.rating,
                    "category": p.category, "country": p.country,
                    "version": p.version or "Base",
                    "position": len(bot_xi) + 1,
                    "is_captain": False,
                })
                used_ids.add(p.id)

    # Set captain to highest-rated player
    if bot_xi:
        cap_idx = max(range(len(bot_xi)), key=lambda i: bot_xi[i]["rating"])
        bot_xi[cap_idx]["is_captain"] = True

    return bot_xi


def _xi_average(xi):
    if not xi: return 60
    return sum(p["rating"] for p in xi) / len(xi)


def _generate_batting_card(xi, runs_total, balls_faced, wickets_lost):
    """Distribute team total across batters realistically.

    Top order (positions 1-3): largest share, more balls each
    Middle order (4-6): moderate share
    Lower order (7-11): small share, mostly come in if 6+ wickets fell

    Returns: list of {position, name, rating, runs, balls, sr, out, dismissal}
    """
    if not xi:
        return []
    # Determine batting order — usually positions 1-11
    batting = sorted(xi[:11], key=lambda p: p["position"])

    # How many batters actually faced balls?
    # Each wicket means another batter came in. We need to use up to:
    # wickets_lost + 1 batters (plus the one not yet dismissed)
    n_batted = min(11, wickets_lost + 2)
    batting_who_played = batting[:n_batted]

    if not batting_who_played:
        return []

    # Distribute runs: weighted toward top order, scaled by rating
    weights = []
    for i, p in enumerate(batting_who_played):
        # Position weight: top order gets more
        if i < 3: pos_w = 1.4
        elif i < 6: pos_w = 1.0
        else: pos_w = 0.55
        # Rating weight
        rating_w = max(0.4, p["rating"] / 80.0)
        # Random noise so identical XI gives slightly different cards
        noise = random.uniform(0.6, 1.4)
        weights.append(pos_w * rating_w * noise)

    total_w = sum(weights) or 1.0
    # Initial run distribution
    distribution = [int(runs_total * w / total_w) for w in weights]
    # Fix rounding so it sums to total
    diff = runs_total - sum(distribution)
    if distribution:
        distribution[0] += diff  # add remainder to top batter

    # Distribute balls similarly (top order faces more)
    ball_weights = []
    for i, p in enumerate(batting_who_played):
        if i < 3: pos_w = 1.3
        elif i < 6: pos_w = 1.0
        else: pos_w = 0.65
        ball_weights.append(pos_w * random.uniform(0.7, 1.3))
    total_bw = sum(ball_weights) or 1.0
    ball_dist = [max(1, int(balls_faced * w / total_bw))
                 for w in ball_weights]
    # Re-fix sum
    diff_b = balls_faced - sum(ball_dist)
    if ball_dist:
        ball_dist[0] += diff_b

    # Mark dismissals: first N batters got out (where N = wickets_lost)
    dismissal_types = ["c", "b", "lbw", "run out", "st", "c & b"]
    cards = []
    for i, p in enumerate(batting_who_played):
        runs = max(0, distribution[i])
        balls = max(1, ball_dist[i])
        sr = round(runs * 100 / balls, 1) if balls > 0 else 0
        out = i < wickets_lost
        dismissal = random.choice(dismissal_types) if out else "not out"
        cards.append({
            "position": p["position"],
            "name": p["name"],
            "rating": p["rating"],
            "category": p["category"],
            "is_captain": p.get("is_captain", False),
            "runs": runs,
            "balls": balls,
            "fours": runs // 12,  # rough estimate
            "sixes": max(0, (runs - 30) // 18) if runs > 30 else 0,
            "strike_rate": sr,
            "out": out,
            "dismissal": dismissal,
        })
    # Add DNB rows for batters who didn't bat
    for i in range(len(batting_who_played), 11):
        if i < len(batting):
            p = batting[i]
            cards.append({
                "position": p["position"], "name": p["name"],
                "rating": p["rating"], "category": p["category"],
                "is_captain": p.get("is_captain", False),
                "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
                "strike_rate": 0, "out": False, "dismissal": "did not bat",
            })
    return cards


def _generate_bowling_card(xi, runs_conceded, balls_bowled, wickets_taken):
    """Distribute opposition's score across the BOWLING XI's bowlers.

    Only Bowlers and All-rounders bowl. Each bowls up to 4 overs (24 balls).
    Returns: list of {position, name, rating, overs, runs, wickets, economy}
    """
    if not xi:
        return []
    # Bowlers + ARs from this XI
    bowlers = [p for p in xi
               if p["category"] in ("Bowler", "All-rounder")]
    if not bowlers:
        # Fall back to anyone (shouldn't happen with valid XI)
        bowlers = xi[:5]
    # Limit to 5 bowlers max in a T20 (one bowls 4 overs at most)
    if len(bowlers) > 5:
        bowlers.sort(key=lambda p: -p["rating"])
        bowlers = bowlers[:5]

    # Distribute balls/overs: skew toward higher-rated bowlers
    weights = [max(0.5, p["rating"] / 80.0) * random.uniform(0.7, 1.3)
               for p in bowlers]
    total_w = sum(weights) or 1.0
    ball_alloc = [max(6, int(balls_bowled * w / total_w))
                  for w in weights]
    # Cap each at 24 balls (4 overs)
    ball_alloc = [min(b, 24) for b in ball_alloc]
    # Distribute the leftovers if any
    diff = balls_bowled - sum(ball_alloc)
    if diff > 0 and bowlers:
        # Spread extra balls round-robin
        for i in range(diff):
            idx = i % len(bowlers)
            if ball_alloc[idx] < 24:
                ball_alloc[idx] += 1

    # Distribute runs (better bowler = fewer runs)
    inv_weights = [1.0 / max(0.5, p["rating"] / 80.0) * random.uniform(0.7, 1.3)
                   for p in bowlers]
    total_iw = sum(inv_weights) or 1.0
    run_alloc = [int(runs_conceded * w / total_iw) for w in inv_weights]
    diff_r = runs_conceded - sum(run_alloc)
    if run_alloc: run_alloc[0] += diff_r

    # Distribute wickets — weighted by rating
    wkt_alloc = [0] * len(bowlers)
    remaining_wkts = wickets_taken
    while remaining_wkts > 0:
        # Pick a bowler weighted by rating
        if not bowlers: break
        weights = [max(0.5, p["rating"] / 80.0) for p in bowlers]
        total = sum(weights)
        roll = random.uniform(0, total)
        acc = 0
        for i, w in enumerate(weights):
            acc += w
            if roll <= acc:
                wkt_alloc[i] += 1
                remaining_wkts -= 1
                break

    cards = []
    for i, p in enumerate(bowlers):
        balls = ball_alloc[i] if i < len(ball_alloc) else 0
        runs = max(0, run_alloc[i]) if i < len(run_alloc) else 0
        wkts = wkt_alloc[i] if i < len(wkt_alloc) else 0
        overs = balls // 6
        rem_balls = balls % 6
        overs_str = f"{overs}.{rem_balls}" if rem_balls else f"{overs}.0"
        economy = round(runs * 6 / balls, 2) if balls > 0 else 0
        cards.append({
            "position": p["position"], "name": p["name"],
            "rating": p["rating"], "category": p["category"],
            "is_captain": p.get("is_captain", False),
            "overs": overs_str, "balls": balls, "runs": runs,
            "wickets": wkts, "economy": economy,
        })
    return cards


def play_quick_match_xi(session, user, user_choices, opponent_difficulty="medium"):
    """Quick match v2: uses the user's actual XI + generates a bot XI.

    Returns the same shape as play_quick_match BUT with:
      - user_xi, bot_xi (full XI lists)
      - innings_1.batting_card, innings_1.bowling_card (per-player breakdown)
      - same for innings_2
      - mom (man of the match): highest-scoring batter or 4+wicket bowler
    """
    # Get user's actual XI
    user_xi = get_user_xi(session, user.id)
    if len(user_xi) < 11:
        # Not a full XI — fall back to old behavior
        # (caller has already validated >= 5 roster size at endpoint level)
        return {
            "ok": False,
            "error": "xi_incomplete",
            "message": f"Your playing XI has {len(user_xi)} players, need 11. "
                       f"Set up your XI in the Manage XI screen first.",
        }

    user_rating = _xi_average(user_xi)

    # Generate bot XI at the appropriate rating
    diff_mod = {"easy": -8, "medium": 0, "hard": +8}.get(opponent_difficulty, 0)
    target_bot_rating = max(55, min(95, user_rating + diff_mod))
    bot_xi = _generate_bot_xi(session, int(target_bot_rating))
    if len(bot_xi) < 11:
        # Player pool too sparse — use what we have
        pass
    bot_rating = _xi_average(bot_xi) if bot_xi else target_bot_rating

    # Toss
    toss_won = random.random() < 0.5
    toss_choice = user_choices.get("toss_choice", "bat")
    if toss_won:
        user_bats_first = (toss_choice == "bat")
    else:
        user_bats_first = (random.random() < 0.5)

    # Bot phase strategy
    bot_choices_inn1 = [random.choice(["aggressive", "balanced", "defensive"])
                        for _ in range(3)]
    bot_choices_inn2 = [random.choice(["aggressive", "balanced", "defensive"])
                        for _ in range(3)]

    user_choices_1 = user_choices.get("innings_1", ["balanced"] * 3)
    user_choices_2 = user_choices.get("innings_2", ["balanced"] * 3)

    # Innings 1
    if user_bats_first:
        inn1 = simulate_innings(user_choices_1, user_rating, bot_rating)
        target = inn1["total_runs"] + 1
        # Bot reacts to target — chase aggressively if behind
        if target > 180:
            bot_choices_inn2 = ["aggressive", "aggressive", "aggressive"]
        elif target > 140:
            bot_choices_inn2 = ["balanced", "balanced", "aggressive"]
        inn2 = simulate_innings(bot_choices_inn2, bot_rating, user_rating, target=target)
        # Per-player scorecard:
        # Innings 1: user batting, bot bowling
        inn1["batting_card"] = _generate_batting_card(
            user_xi, inn1["total_runs"], inn1["balls_faced"], inn1["total_wickets"])
        inn1["bowling_card"] = _generate_bowling_card(
            bot_xi, inn1["total_runs"], inn1["balls_faced"], inn1["total_wickets"])
        # Innings 2: bot batting, user bowling
        inn2["batting_card"] = _generate_batting_card(
            bot_xi, inn2["total_runs"], inn2["balls_faced"], inn2["total_wickets"])
        inn2["bowling_card"] = _generate_bowling_card(
            user_xi, inn2["total_runs"], inn2["balls_faced"], inn2["total_wickets"])
        inn1["team_name"] = "Your team"
        inn1["is_user"] = True
        inn2["team_name"] = "Opponent"
        inn2["is_user"] = False
    else:
        inn1 = simulate_innings(bot_choices_inn1, bot_rating, user_rating)
        target = inn1["total_runs"] + 1
        inn2 = simulate_innings(user_choices_2, user_rating, bot_rating, target=target)
        inn1["batting_card"] = _generate_batting_card(
            bot_xi, inn1["total_runs"], inn1["balls_faced"], inn1["total_wickets"])
        inn1["bowling_card"] = _generate_bowling_card(
            user_xi, inn1["total_runs"], inn1["balls_faced"], inn1["total_wickets"])
        inn2["batting_card"] = _generate_batting_card(
            user_xi, inn2["total_runs"], inn2["balls_faced"], inn2["total_wickets"])
        inn2["bowling_card"] = _generate_bowling_card(
            bot_xi, inn2["total_runs"], inn2["balls_faced"], inn2["total_wickets"])
        inn1["team_name"] = "Opponent"
        inn1["is_user"] = False
        inn2["team_name"] = "Your team"
        inn2["is_user"] = True

    # Determine winner
    inn2_runs = inn2["total_runs"]
    if inn2_runs >= target:
        user_won = not user_bats_first
    elif inn2_runs == target - 1:
        user_won = None
    else:
        user_won = user_bats_first

    # Award coins — based on margin
    coin_reward = 0
    if user_won is True:
        coin_reward = 500
        # Bonus for dominant win
        if abs(inn1["total_runs"] - inn2_runs) > 50:
            coin_reward += 200
    elif user_won is None:
        coin_reward = 200

    # Man of the match: highest run-getter from winner OR best bowler if low-scoring
    def _find_mom(inn, is_user_inn):
        bc = inn.get("batting_card", [])
        if bc:
            top = max(bc, key=lambda b: b["runs"])
            if top["runs"] >= 30:
                return {"name": top["name"], "category": top["category"],
                        "rating": top["rating"], "from_user_team": is_user_inn,
                        "highlight": f"{top['runs']}({top['balls']})",
                        "kind": "batter"}
        bw = inn.get("bowling_card", [])
        if bw:
            top_b = max(bw, key=lambda b: (b["wickets"], -b["runs"]))
            if top_b["wickets"] >= 3:
                return {"name": top_b["name"], "category": top_b["category"],
                        "rating": top_b["rating"], "from_user_team": not is_user_inn,
                        "highlight": f"{top_b['wickets']}/{top_b['runs']}",
                        "kind": "bowler"}
        return None

    # Search winning side first
    mom = None
    if user_won is True:
        if user_bats_first:
            mom = _find_mom(inn1, True) or _find_mom(inn2, False)
        else:
            mom = _find_mom(inn2, True) or _find_mom(inn1, False)
    elif user_won is False:
        if user_bats_first:
            mom = _find_mom(inn2, False) or _find_mom(inn1, True)
        else:
            mom = _find_mom(inn1, False) or _find_mom(inn2, True)

    # Track event for quests (best-effort)
    try:
        from services.quest_service import safe_track
        safe_track(session, user.id, "quick_match_played", 1)
        if user_won is True:
            safe_track(session, user.id, "quick_match_won", 1)
    except Exception:
        pass

    # Pay rewards
    if coin_reward > 0:
        user.total_coins = (user.total_coins or 0) + coin_reward
        try:
            from services.activity_service import log_activity
            log_activity(session, user.id, "quick_match",
                         f"Quick match {'won' if user_won else ('tied' if user_won is None else 'lost')}",
                         coins_change=coin_reward)
        except Exception:
            pass

    return {
        "ok": True,
        "toss_won": toss_won,
        "user_bats_first": user_bats_first,
        "user_team_rating": round(user_rating, 1),
        "opponent_team_rating": round(bot_rating, 1),
        "user_xi": user_xi,
        "bot_xi": bot_xi,
        "innings_1": inn1,
        "innings_2": inn2,
        "target": target,
        "user_won": user_won,
        "coin_reward": coin_reward,
        "mom": mom,
        "played_at": datetime.utcnow().isoformat(),
    }
