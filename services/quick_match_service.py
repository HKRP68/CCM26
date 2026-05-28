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
        "rating": p.rating,
        "bat_rating": p.bat_rating or 0,
        "bowl_rating": p.bowl_rating or 0,
        "category": p.category, "country": p.country,
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
                        "player_id": p.id, "name": p.name, "rating": p.rating, "bat_rating": p.bat_rating or 0, "bowl_rating": p.bowl_rating or 0,
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
                    "player_id": p.id, "name": p.name, "rating": p.rating, "bat_rating": p.bat_rating or 0, "bowl_rating": p.bowl_rating or 0,
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


def validate_user_xi(xi):
    """Check XI list (from get_user_xi) follows cricket rules.

    Mirrors bot_team_service.validate_team_xi:
      - exactly 11 players
      - 3-5 Batsmen, 3-5 Bowlers, 1-2 Wicket Keepers, 1-3 All-rounders

    Returns (ok: bool, errors: list[str], counts: dict)
    """
    counts = {"Batsman": 0, "Bowler": 0,
              "Wicket Keeper": 0, "All-rounder": 0}
    for p in xi:
        cat = p.get("category", "")
        if cat in counts:
            counts[cat] += 1

    errs = []
    if len(xi) != 11:
        errs.append(f"Need exactly 11 players in your playing XI (have {len(xi)})")
        return False, errs, counts

    if counts["Batsman"] < 3:
        errs.append(f"Need at least 3 Batsmen (have {counts['Batsman']})")
    elif counts["Batsman"] > 5:
        errs.append(f"Maximum 5 Batsmen (have {counts['Batsman']})")
    if counts["Bowler"] < 3:
        errs.append(f"Need at least 3 Bowlers (have {counts['Bowler']})")
    elif counts["Bowler"] > 5:
        errs.append(f"Maximum 5 Bowlers (have {counts['Bowler']})")
    if counts["Wicket Keeper"] < 1:
        errs.append(f"Need at least 1 Wicket Keeper (have {counts['Wicket Keeper']})")
    elif counts["Wicket Keeper"] > 2:
        errs.append(f"Maximum 2 Wicket Keepers (have {counts['Wicket Keeper']})")
    if counts["All-rounder"] < 1:
        errs.append(f"Need at least 1 All-rounder (have {counts['All-rounder']})")
    elif counts["All-rounder"] > 3:
        errs.append(f"Maximum 3 All-rounders (have {counts['All-rounder']})")

    return len(errs) == 0, errs, counts


def _generate_batting_card(xi, runs_total, balls_faced, wickets_lost):
    """Distribute team total across batters realistically.

    Top order (positions 1-3): largest share, more balls each
    Middle order (4-6): moderate share
    Lower order (7-11): small share, mostly come in if 6+ wickets fell

    KEY LOGIC: weighting uses each player's BATTING RATING and their CATEGORY.
    A bowler at position 8 won't be hitting a century because:
      - Bowlers have low bat_rating (typically 30-50)
      - Lower position has low pos_w
      - Category multiplier strongly penalizes bowlers (0.35) and rewards
        batsmen (1.0) and all-rounders (0.8)

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

    # Category multiplier — how good is this player at batting RELATIVE to
    # other players in their position. Bowlers are heavily downweighted so
    # they don't end up scoring centuries.
    CAT_BAT_MULT = {
        "Batsman":       1.00,
        "Wicket Keeper": 0.85,
        "All-rounder":   0.80,
        "Bowler":        0.30,   # strong penalty — bowlers don't score big
    }

    # Distribute runs: weighted by batting rating, category, AND position
    weights = []
    for i, p in enumerate(batting_who_played):
        # Position weight: top order gets significantly more
        if i < 3: pos_w = 1.5
        elif i < 5: pos_w = 1.2
        elif i < 7: pos_w = 0.9
        elif i < 9: pos_w = 0.45
        else:      pos_w = 0.25

        # Use bat_rating (not OVR) — bowlers have low bat_rating
        # Fall back to OVR if bat_rating is 0 (legacy data)
        bat = p.get("bat_rating") or 0
        if bat <= 0:
            bat = p.get("rating", 60)
        # Scale so 80 bat_rating ≈ 1.0, 40 ≈ 0.4
        # Use a steeper curve: (bat/75)^2 so bad batters are very weak
        rating_w = max(0.1, (bat / 75.0) ** 1.5)

        # Category multiplier
        cat_w = CAT_BAT_MULT.get(p["category"], 0.6)

        # Random noise — narrower (less wild outcomes)
        noise = random.uniform(0.75, 1.25)

        weights.append(pos_w * rating_w * cat_w * noise)

    total_w = sum(weights) or 1.0
    # Initial run distribution
    distribution = [int(runs_total * w / total_w) for w in weights]

    # ── Cap individual scores by quality ──
    # A T20 century by a #9 bowler with 40 bat_rating is unrealistic.
    # Cap each batter's max score by their quality, position, AND a hard
    # category cap. The cap is generous enough that genuinely good batters
    # can still score big.
    def _max_score(p, pos_idx):
        bat = p.get("bat_rating") or p.get("rating", 60)
        cat = p["category"]
        # Hard category cap (a bowler simply doesn't bat like a top-order)
        if cat == "Bowler":          base_cap = 35
        elif cat == "All-rounder":   base_cap = 80
        elif cat == "Wicket Keeper": base_cap = 100
        else:                        base_cap = 150  # Batsman
        # Position cap: lower order can't accumulate as many balls
        if pos_idx < 3:   pos_cap = 150  # opener/#3 can do anything
        elif pos_idx < 5: pos_cap = 110
        elif pos_idx < 7: pos_cap = 85
        elif pos_idx < 9: pos_cap = 50
        else:             pos_cap = 30
        # Quality cap: rating directly limits ceiling
        quality_cap = int((bat / 100.0) * 130)  # 100 OVR → 130 max
        return max(8, min(base_cap, pos_cap, quality_cap))

    # Apply caps + redistribute overflow to higher-quality batters
    for attempt in range(3):  # iterate to redistribute overflow
        overflow = 0
        capacity = []  # (idx, remaining_capacity, eligible_to_take_more)
        for i, p in enumerate(batting_who_played):
            cap = _max_score(p, i)
            if distribution[i] > cap:
                overflow += distribution[i] - cap
                distribution[i] = cap
            else:
                # How much more this batter can absorb
                capacity.append((i, cap - distribution[i], p))

        if overflow == 0 or not capacity:
            break

        # Distribute overflow to remaining capacity weighted by quality
        cap_weights = [(idx, cap_left, weights[idx])
                       for idx, cap_left, _ in capacity if cap_left > 0]
        if not cap_weights:
            break
        total_cw = sum(w for _, _, w in cap_weights) or 1.0
        for idx, cap_left, w in cap_weights:
            add = min(cap_left, int(overflow * w / total_cw))
            distribution[idx] += add
            overflow -= add
            if overflow <= 0: break

    # Fix rounding so it sums to total (cap may have lost a few runs — that's OK,
    # we'll absorb the diff to whoever has the most capacity)
    diff = runs_total - sum(distribution)
    if diff != 0:
        # Add to the highest-weighted batter who has room
        for idx in sorted(range(len(distribution)), key=lambda i: -weights[i]):
            cap = _max_score(batting_who_played[idx], idx)
            room = cap - distribution[idx]
            take = max(-distribution[idx], min(diff, room))
            distribution[idx] += take
            diff -= take
            if diff == 0: break

    # ── Ball distribution: also category/quality-weighted ──
    # Top order faces more, and quality batters use their balls efficiently.
    ball_weights = []
    for i, p in enumerate(batting_who_played):
        if i < 3:   pos_w = 1.4
        elif i < 5: pos_w = 1.15
        elif i < 7: pos_w = 0.9
        elif i < 9: pos_w = 0.55
        else:       pos_w = 0.35
        cat_w = CAT_BAT_MULT.get(p["category"], 0.6)
        ball_weights.append(pos_w * cat_w * random.uniform(0.85, 1.15))
    total_bw = sum(ball_weights) or 1.0
    ball_dist = [max(1, int(balls_faced * w / total_bw))
                 for w in ball_weights]
    diff_b = balls_faced - sum(ball_dist)
    if ball_dist:
        ball_dist[0] += diff_b

    # ── Sanity: balls must support runs at a reasonable strike rate ──
    # If we gave 50 runs to a tailender who only faced 8 balls, the SR
    # would be 625 — absurd. Cap balls-driven SR per category.
    MAX_SR_BY_CAT = {
        "Batsman": 250, "Wicket Keeper": 220,
        "All-rounder": 220, "Bowler": 180,
    }
    for i, p in enumerate(batting_who_played):
        max_sr = MAX_SR_BY_CAT.get(p["category"], 200)
        # Required balls for current runs at max_sr
        if distribution[i] > 0:
            min_balls = max(1, int(distribution[i] * 100 / max_sr))
            if ball_dist[i] < min_balls:
                ball_dist[i] = min_balls

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
        # Prioritize by bowl_rating (not OVR), then by being a Bowler
        bowlers.sort(key=lambda p: (
            -(p.get("bowl_rating") or 0),
            0 if p["category"] == "Bowler" else 1,
        ))
        bowlers = bowlers[:5]

    # Helper: effective bowling skill = bowl_rating with category boost
    def _bowl_skill(p):
        br = p.get("bowl_rating") or 0
        if br <= 0:
            br = p.get("rating", 60)
        # Bowlers get full credit; ARs are slightly weaker bowlers
        cat_mult = 1.0 if p["category"] == "Bowler" else 0.85
        return max(20, br * cat_mult)

    # Distribute balls/overs: skew toward higher-skill bowlers
    weights = [(_bowl_skill(p) / 80.0) * random.uniform(0.85, 1.15)
               for p in bowlers]
    total_w = sum(weights) or 1.0
    ball_alloc = [max(6, int(balls_bowled * w / total_w))
                  for w in weights]
    # Cap each at 24 balls (4 overs)
    ball_alloc = [min(b, 24) for b in ball_alloc]
    # Distribute the leftovers if any
    diff = balls_bowled - sum(ball_alloc)
    if diff > 0 and bowlers:
        # Spread extra balls round-robin, preferring higher-skill bowlers
        order = sorted(range(len(bowlers)), key=lambda i: -_bowl_skill(bowlers[i]))
        for j in range(diff):
            idx = order[j % len(order)]
            if ball_alloc[idx] < 24:
                ball_alloc[idx] += 1

    # Distribute runs: BETTER bowler concedes FEWER runs (inverse skill)
    # Also weight by balls they bowled (a bowler with 24 balls concedes more
    # total than one with 6 balls, even at the same economy).
    inv_weights = []
    for i, p in enumerate(bowlers):
        skill = _bowl_skill(p)
        # Inverse skill (better → lower weight for runs given up)
        # Use a steeper inverse curve: skill 90 → 0.6, skill 50 → 1.7
        inv = (80.0 / skill) ** 1.4
        # Multiply by share of balls bowled
        balls_share = max(1, ball_alloc[i]) / max(1, balls_bowled)
        inv_weights.append(inv * balls_share * random.uniform(0.85, 1.15))
    total_iw = sum(inv_weights) or 1.0
    run_alloc = [int(runs_conceded * w / total_iw) for w in inv_weights]
    diff_r = runs_conceded - sum(run_alloc)
    if run_alloc:
        run_alloc[0] += diff_r

    # Distribute wickets — weighted by bowl_skill
    wkt_alloc = [0] * len(bowlers)
    remaining_wkts = wickets_taken
    while remaining_wkts > 0:
        if not bowlers: break
        wkt_weights = [_bowl_skill(p) / 80.0 for p in bowlers]
        # Reduce weight if bowler is at max wickets (cap 5 in a T20)
        for i in range(len(wkt_weights)):
            if wkt_alloc[i] >= 5:
                wkt_weights[i] = 0.01
        total = sum(wkt_weights)
        if total <= 0: break
        roll = random.uniform(0, total)
        acc = 0
        for i, w in enumerate(wkt_weights):
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


# ════════════════════════════════════════════════════════════════════
# Phase-by-phase quick match (v3) — reactive gameplay
# ════════════════════════════════════════════════════════════════════
#
# State lives in-memory per user. User picks a phase tactic, server simulates
# that one phase, returns result, waits for next pick. Allows reactive
# decisions ("we lost wickets in PP, defend in middle") instead of one
# up-front pick. State expires after 30 min of inactivity. Process restart
# loses in-flight matches — acceptable for a "quick" match.
#
# Quick Match stats are tracked SEPARATELY from global match stats. They do
# NOT count toward leaderboard, matches_played/won/lost, or career records.
# Quick Match has its own counters and a daily limit (default 5/day).

import threading
import uuid as _uuid
import time

_PHASE_MATCH_STORE = {}  # tg_id -> state dict
_PHASE_MATCH_LOCK = threading.Lock()
_PHASE_MATCH_TTL_SEC = 30 * 60  # 30 min

# Reward constants — easy to find when admins want to tune
QUICK_MATCH_WIN_COIN_REWARD = 250
QUICK_MATCH_WIN_DOMINANT_BONUS = 100  # extra for winning by >50 runs
QUICK_MATCH_TIE_COIN_REWARD = 100

# Daily limit fallback if GameConfig is missing
DEFAULT_DAILY_QUICK_MATCH_LIMIT = 5


def _now_ts():
    return int(time.time())


def _today_str():
    """Today's date in UTC as YYYY-MM-DD (for daily-counter comparisons)."""
    return datetime.utcnow().strftime("%Y-%m-%d")


def _expire_stale_matches():
    """Drop matches older than TTL. Cheap O(N) sweep."""
    cutoff = _now_ts() - _PHASE_MATCH_TTL_SEC
    with _PHASE_MATCH_LOCK:
        stale = [k for k, v in _PHASE_MATCH_STORE.items()
                 if v.get("last_activity", 0) < cutoff]
        for k in stale:
            _PHASE_MATCH_STORE.pop(k, None)


def get_phase_match(tg_id):
    """Return active phase match for user, or None."""
    _expire_stale_matches()
    with _PHASE_MATCH_LOCK:
        return _PHASE_MATCH_STORE.get(tg_id)


def drop_phase_match(tg_id):
    """Clear any active match (used on completion or abandon)."""
    with _PHASE_MATCH_LOCK:
        _PHASE_MATCH_STORE.pop(tg_id, None)


def get_daily_limit(session):
    """Read daily Quick Match limit from GameConfig (with fallback)."""
    try:
        from models import GameConfig
        cfg = session.query(GameConfig).first()
        if cfg and cfg.daily_quick_match_limit is not None:
            return max(0, int(cfg.daily_quick_match_limit))
    except Exception:
        pass
    return DEFAULT_DAILY_QUICK_MATCH_LIMIT


def _reset_daily_counter_if_new_day(user):
    """If the user's daily counter is from a prior day, reset to 0.
    Caller is responsible for committing."""
    today = _today_str()
    if user.quick_matches_today_date != today:
        user.quick_matches_today = 0
        user.quick_matches_today_date = today


def get_quick_match_quota(session, user):
    """Return (used_today, limit, remaining) for the user's Quick Match quota."""
    _reset_daily_counter_if_new_day(user)
    limit = get_daily_limit(session)
    used = user.quick_matches_today or 0
    remaining = max(0, limit - used)
    return used, limit, remaining


def start_phase_match(session, user, tg_id, toss_choice, opponent_difficulty="medium"):
    """Initialise a new phase-by-phase match. Returns initial state dict.

    Checks:
      - User has a valid playing XI (3-5 bat / 3-5 bowl / 1-2 wk / 1-3 ar)
      - User has Quick Match attempts remaining today (daily limit)

    `toss_choice`: 'bat' or 'bowl' (user's pick if they win toss).
    """
    # Daily limit check (BEFORE XI generation — cheaper)
    _reset_daily_counter_if_new_day(user)
    used, limit, remaining = get_quick_match_quota(session, user)
    if remaining <= 0:
        return {
            "ok": False,
            "error": "daily_limit_reached",
            "message": (
                f"You've played {used}/{limit} Quick Matches today. "
                f"Limit resets at UTC midnight."
            ),
            "used": used, "limit": limit, "remaining": 0,
        }

    user_xi = get_user_xi(session, user.id)
    ok, errs, counts = validate_user_xi(user_xi)
    if not ok:
        return {
            "ok": False, "error": "invalid_xi",
            "message": " · ".join(errs),
            "errors": errs, "counts": counts,
        }

    user_rating = _xi_average(user_xi)
    diff_mod = {"easy": -8, "medium": 0, "hard": +8}.get(opponent_difficulty, 0)
    target_bot_rating = max(55, min(95, user_rating + diff_mod))
    bot_xi = _generate_bot_xi(session, int(target_bot_rating))
    bot_rating = _xi_average(bot_xi) if bot_xi else target_bot_rating

    # Toss
    toss_won = random.random() < 0.5
    if toss_won:
        user_bats_first = (toss_choice == "bat")
    else:
        user_bats_first = (random.random() < 0.5)

    state = {
        "match_id": str(_uuid.uuid4())[:12],
        "tg_id": tg_id,
        "user_id": user.id,
        "user_xi": user_xi,
        "bot_xi": bot_xi,
        "user_rating": round(user_rating, 1),
        "bot_rating": round(bot_rating, 1),
        "user_bats_first": user_bats_first,
        "toss_won": toss_won,
        "toss_choice": toss_choice,
        "difficulty": opponent_difficulty,
        "innings": 1,
        "phase_index": 0,
        "innings_1": {"runs": 0, "wickets": 0, "balls": 0, "phase_history": []},
        "innings_2": {"runs": 0, "wickets": 0, "balls": 0, "phase_history": []},
        "target": None,
        "complete": False,
        "user_won": None,
        "coin_reward": 0,
        "mom": None,
        "started_at": _now_ts(),
        "last_activity": _now_ts(),
    }

    with _PHASE_MATCH_LOCK:
        _PHASE_MATCH_STORE[tg_id] = state

    return {
        "ok": True,
        "state": _public_state(state),
        "quota": {"used": used, "limit": limit, "remaining": remaining},
    }


def _public_state(state):
    """Client-safe snapshot of state."""
    return {
        "match_id": state["match_id"],
        "toss_won": state["toss_won"],
        "toss_choice": state["toss_choice"],
        "user_bats_first": state["user_bats_first"],
        "user_team_rating": state["user_rating"],
        "opponent_team_rating": state["bot_rating"],
        "user_xi": state["user_xi"],
        "bot_xi": state["bot_xi"],
        "innings": state["innings"],
        "phase_index": state["phase_index"],
        "innings_1": state["innings_1"],
        "innings_2": state["innings_2"],
        "target": state["target"],
        "complete": state["complete"],
        "user_won": state["user_won"],
        "coin_reward": state["coin_reward"],
        "mom": state["mom"],
    }


def play_phase(session, user, tg_id, choice):
    """Advance the active match by one phase.

    Returns: {ok, phase_result, state, innings_complete, match_complete, scorecard?, quota?}
    """
    state = get_phase_match(tg_id)
    if not state:
        return {"ok": False, "error": "no_match",
                "message": "No active match. Start a new one."}
    if state["complete"]:
        return {"ok": False, "error": "already_complete",
                "message": "Match already finished."}

    state["last_activity"] = _now_ts()

    innings_key = f"innings_{state['innings']}"
    inn = state[innings_key]
    phase_idx = state["phase_index"]
    if phase_idx >= len(PHASE_DEFS):
        return {"ok": False, "error": "no_more_phases",
                "message": "Innings already complete."}

    phase = PHASE_DEFS[phase_idx]

    user_batting_this_innings = (
        (state["innings"] == 1 and state["user_bats_first"]) or
        (state["innings"] == 2 and not state["user_bats_first"])
    )

    valid_choices = ("aggressive", "balanced", "defensive")
    if choice not in valid_choices:
        choice = "balanced"

    user_choice = choice
    bot_choice = random.choice(valid_choices)

    if user_batting_this_innings:
        batting_choice = user_choice
        batting_rating = state["user_rating"]
        bowling_rating = state["bot_rating"]
    else:
        batting_choice = bot_choice
        batting_rating = state["bot_rating"]
        bowling_rating = state["user_rating"]

    wickets_left = 10 - inn["wickets"]
    if wickets_left <= 0:
        runs, wickets_lost, balls_this_phase = 0, 0, 0
    else:
        target = state["target"]
        runs, wickets_lost = simulate_phase(
            phase, batting_choice, batting_rating, bowling_rating, wickets_left)
        balls_this_phase = phase["balls"]
        # Target chase early-finish
        if target is not None and (inn["runs"] + runs) >= target:
            runs = target - inn["runs"]
            balls_this_phase = int(phase["balls"] * random.uniform(0.6, 1.0))
            wickets_lost = 0

    wickets_lost = min(wickets_lost, wickets_left)

    inn["runs"] += runs
    inn["wickets"] += wickets_lost
    inn["balls"] += balls_this_phase
    inn["phase_history"].append({
        "name": phase["name"],
        "user_choice": user_choice if user_batting_this_innings else None,
        "user_bowling_choice": user_choice if not user_batting_this_innings else None,
        "bot_choice": bot_choice,
        "user_batting_this_innings": user_batting_this_innings,
        "runs": runs,
        "wickets_lost": wickets_lost,
        "balls_used": balls_this_phase,
        "score_after": inn["runs"],
        "wickets_after": inn["wickets"],
    })

    phase_result = inn["phase_history"][-1].copy()
    state["phase_index"] += 1

    innings_complete = False
    target_chased = False
    if state["target"] is not None and inn["runs"] >= state["target"]:
        target_chased = True
        innings_complete = True
    if state["phase_index"] >= len(PHASE_DEFS) or inn["wickets"] >= 10:
        innings_complete = True

    match_complete = False
    scorecard = None
    quota_after = None
    if innings_complete:
        if state["innings"] == 1:
            state["target"] = inn["runs"] + 1
            state["innings"] = 2
            state["phase_index"] = 0
        else:
            match_complete = True
            state["complete"] = True
            scorecard = _finalize_match(session, user, state, target_chased)
            # After completing, return the post-match quota
            used, limit, remaining = get_quick_match_quota(session, user)
            quota_after = {"used": used, "limit": limit, "remaining": remaining}

    result = {
        "ok": True,
        "phase_result": phase_result,
        "state": _public_state(state),
        "innings_complete": innings_complete,
        "match_complete": match_complete,
        "scorecard": scorecard,
    }
    if quota_after is not None:
        result["quota"] = quota_after
    return result


def _finalize_match(session, user, state, target_chased):
    """Build final scorecard, determine winner, award coins, bump QM-only
    counters, log activity. Does NOT touch global match stats (matches_played
    etc) — Quick Match is intentionally separate from the leaderboard."""
    inn1 = state["innings_1"]
    inn2 = state["innings_2"]
    target = state["target"]
    user_bats_first = state["user_bats_first"]

    # Winner
    if inn2["runs"] >= target:
        user_won = not user_bats_first
    elif inn2["runs"] == target - 1:
        user_won = None  # tied
    else:
        user_won = user_bats_first

    # Build per-player batting + bowling cards
    if user_bats_first:
        inn1_batting_xi = state["user_xi"]
        inn1_bowling_xi = state["bot_xi"]
        inn2_batting_xi = state["bot_xi"]
        inn2_bowling_xi = state["user_xi"]
        inn1_team = "Your team"
        inn2_team = "Opponent"
    else:
        inn1_batting_xi = state["bot_xi"]
        inn1_bowling_xi = state["user_xi"]
        inn2_batting_xi = state["user_xi"]
        inn2_bowling_xi = state["bot_xi"]
        inn1_team = "Opponent"
        inn2_team = "Your team"

    inn1_full = {
        "team_name": inn1_team,
        "is_user": user_bats_first,
        "total_runs": inn1["runs"],
        "total_wickets": inn1["wickets"],
        "balls_faced": inn1["balls"],
        "phases": inn1["phase_history"],
        "batting_card": _generate_batting_card(
            inn1_batting_xi, inn1["runs"], inn1["balls"], inn1["wickets"]),
        "bowling_card": _generate_bowling_card(
            inn1_bowling_xi, inn1["runs"], inn1["balls"], inn1["wickets"]),
    }
    inn2_full = {
        "team_name": inn2_team,
        "is_user": not user_bats_first,
        "total_runs": inn2["runs"],
        "total_wickets": inn2["wickets"],
        "balls_faced": inn2["balls"],
        "phases": inn2["phase_history"],
        "batting_card": _generate_batting_card(
            inn2_batting_xi, inn2["runs"], inn2["balls"], inn2["wickets"]),
        "bowling_card": _generate_bowling_card(
            inn2_bowling_xi, inn2["runs"], inn2["balls"], inn2["wickets"]),
    }

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

    mom = None
    if user_won is True:
        mom = (_find_mom(inn1_full, user_bats_first)
               or _find_mom(inn2_full, not user_bats_first))
    elif user_won is False:
        mom = (_find_mom(inn2_full, not user_bats_first)
               or _find_mom(inn1_full, user_bats_first))

    # ── Coin rewards (reduced from 500 → 250 per user request) ──
    coin_reward = 0
    if user_won is True:
        coin_reward = QUICK_MATCH_WIN_COIN_REWARD  # 250
        # Dominant win bonus
        if abs(inn1["runs"] - inn2["runs"]) > 50:
            coin_reward += QUICK_MATCH_WIN_DOMINANT_BONUS  # 100
    elif user_won is None:
        coin_reward = QUICK_MATCH_TIE_COIN_REWARD  # 100

    state["user_won"] = user_won
    state["coin_reward"] = coin_reward
    state["mom"] = mom

    # ── Apply rewards + bump QUICK-MATCH-ONLY counters ──
    # IMPORTANT: matches_played/won/lost are NOT touched. Quick Match has
    # its own counters so it doesn't pollute the leaderboard.
    if coin_reward > 0:
        user.total_coins = (user.total_coins or 0) + coin_reward

    user.quick_matches_played = (user.quick_matches_played or 0) + 1
    if user_won is True:
        user.quick_matches_won = (user.quick_matches_won or 0) + 1
    elif user_won is False:
        user.quick_matches_lost = (user.quick_matches_lost or 0) + 1

    # Daily counter
    _reset_daily_counter_if_new_day(user)
    user.quick_matches_today = (user.quick_matches_today or 0) + 1

    # Activity log (mark as quick match so admins can distinguish)
    try:
        from services.activity_service import log_activity
        outcome = ("won" if user_won is True
                   else ("tied" if user_won is None else "lost"))
        log_activity(session, user.id, "quickmatch",
                     f"[Quick Match] {outcome}: "
                     f"{inn1['runs']}/{inn1['wickets']} vs "
                     f"{inn2['runs']}/{inn2['wickets']}",
                     coins_change=coin_reward)
    except Exception:
        pass

    # Quest tracking — use QUICK-MATCH-SPECIFIC event keys so they don't
    # count for "Win 3 matches" quests (which are for real matches). Quests
    # defined with event_key='quick_match_played' or 'quick_match_won' will
    # still progress.
    try:
        from services.quest_service import safe_track
        safe_track(session, user.id, "quick_match_played", 1)
        if user_won is True:
            safe_track(session, user.id, "quick_match_won", 1)
    except Exception:
        pass

    return {
        "user_won": user_won,
        "coin_reward": coin_reward,
        "mom": mom,
        "innings_1": inn1_full,
        "innings_2": inn2_full,
        "target": target,
        "user_bats_first": user_bats_first,
        "user_team_rating": state["user_rating"],
        "opponent_team_rating": state["bot_rating"],
        "user_xi": state["user_xi"],
        "bot_xi": state["bot_xi"],
        "played_at": datetime.utcnow().isoformat(),
    }
