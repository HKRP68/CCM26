"""
Scenario-Based Simulation Engine
================================
Steers matches toward dramatic finishes by:
  1. Free Play (overs 0-14): Light probability nudges to keep match on trajectory
  2. Convergence (overs 15-17): Stronger nudges to reach target state for finale
  3. Finale (overs 18-19): Scripted ball-by-ball sequences for maximum drama

Supported scenarios:
  - last_ball_six: Chasing team wins by hitting a 6 on the final ball
  - win_by_1_run: Defending team wins; chasing team falls 1 run short
  - super_over_thriller: Match ties after 20 overs, triggers super over
"""

import random
import logging

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  Scenario definitions: target corridors and finale parameters
# --------------------------------------------------------------------------- #

SCENARIO_CONFIG = {
    "last_ball_six": {
        "label": "Last-Ball Six",
        # Ideal state at start of over 18 (12 balls left)
        "convergence_target": {
            "runs_needed_range": (20, 28),
            "wickets_range": (3, 6),
        },
        "finale_last_ball": {"runs": 6, "type": "run"},
    },
    "win_by_1_run": {
        "label": "Win by 1 Run",
        "convergence_target": {
            "runs_needed_range": (18, 24),
            "wickets_range": (5, 8),
        },
        "finale_last_ball": {"runs": 0, "type": "run"},  # dot ball to lose
    },
    "super_over_thriller": {
        "label": "Super Over Thriller",
        "convergence_target": {
            "runs_needed_range": (20, 26),
            "wickets_range": (4, 7),
        },
        "finale_last_ball": None,  # needs exact tie — dynamic
    },
}

# --------------------------------------------------------------------------- #
#  Wicket type helpers (mirrors ball_outcome.py distributions)
# --------------------------------------------------------------------------- #

PACE_WICKET_TYPES = ["Caught", "Bowled", "LBW", "Caught", "Caught"]
SPIN_WICKET_TYPES = ["Caught", "Stumped", "Bowled", "LBW", "Caught"]


def _pick_wicket_type(bowler):
    """Pick a realistic wicket type based on bowling style."""
    btype = bowler.get("bowling_type", "Medium")
    if btype in ("Fast", "Medium"):
        return random.choice(PACE_WICKET_TYPES)
    return random.choice(SPIN_WICKET_TYPES)


def _pick_fielder(bowling_team, bowler_name):
    """Pick a random fielder (not the bowler) for caught dismissals."""
    candidates = [p["name"] for p in bowling_team if p["name"] != bowler_name]
    return random.choice(candidates) if candidates else bowler_name


# --------------------------------------------------------------------------- #
#  Outcome dict builders
# --------------------------------------------------------------------------- #

def _make_run_outcome(runs, batter, bowler):
    """Build a standard run outcome dict."""
    desc_map = {
        0: f"Dot ball. {bowler['name']} keeps it tight.",
        1: f"{batter['name']} nudges for a single.",
        2: f"Good running! {batter['name']} takes two.",
        3: f"Excellent running between the wickets — three!",
        4: f"FOUR! {batter['name']} finds the boundary!",
        6: f"SIX! {batter['name']} launches it into the stands!",
    }
    return {
        "type": "run",
        "runs": runs,
        "description": desc_map.get(runs, f"{batter['name']} scores {runs}."),
        "wicket_type": None,
        "is_extra": False,
        "batter_out": False,
    }


def _make_wicket_outcome(batter, bowler, bowling_team, runs=0, wicket_type=None):
    """Build a wicket outcome dict."""
    wtype = wicket_type if wicket_type else _pick_wicket_type(bowler)
    if wtype == "Run Out":
        # Allow explicit 0-run run outs (e.g. last-ball failed single).
        if runs is None:
            runs = 1
    fielder = ""
    if wtype == "Caught":
        fielder = _pick_fielder(bowling_team, bowler["name"])
        desc = f"OUT! {batter['name']} caught by {fielder} off {bowler['name']}!"
    elif wtype == "Bowled":
        desc = f"BOWLED! {bowler['name']} cleans up {batter['name']}!"
    elif wtype == "LBW":
        desc = f"LBW! {bowler['name']} traps {batter['name']} in front!"
    elif wtype == "Stumped":
        fielder = _pick_fielder(bowling_team, bowler["name"])
        desc = f"STUMPED! {batter['name']} stranded out of the crease!"
    elif wtype == "Run Out":
        fielder = _pick_fielder(bowling_team, bowler["name"])
        desc = f"RUN OUT! {batter['name']} is short of the crease!"
    else:
        desc = f"OUT! {batter['name']} is dismissed!"

    return {
        "type": "wicket",
        "runs": runs,
        "description": desc,
        "wicket_type": wtype,
        "is_extra": False,
        "batter_out": True,
        "fielder_out": fielder,
    }


# --------------------------------------------------------------------------- #
#  Finale script generators
# --------------------------------------------------------------------------- #

VALID_RUNS = (0, 1, 2, 3, 4, 6)  # Only legal run values in cricket


def _snap_to_valid(r):
    """Snap a run value to the nearest legal cricket outcome."""
    if r in VALID_RUNS:
        return r
    if r <= 0:
        return 0
    if r >= 6:
        return 6
    # 5 → 6 or 4 (prefer 4 to avoid inflation); other odd gaps similarly
    best = min(VALID_RUNS, key=lambda v: (abs(v - r), -v))
    return best


def _distribute_runs(target_runs, num_balls, include_wicket=True, include_boundary=True):
    """
    Generate a sequence of run values that sum to target_runs over num_balls.
    Every value is a legal cricket outcome: 0, 1, 2, 3, 4, or 6.
    Returns a list of (runs, is_wicket) tuples.
    """
    if num_balls <= 0 or target_runs < 0:
        return []

    sequence = []
    remaining = target_runs
    balls_left = num_balls

    # Place dramatic moments first
    wicket_pos = None
    boundary_pos = None

    if include_wicket and balls_left >= 4 and remaining >= 4:
        wicket_pos = random.randint(1, min(balls_left - 2, balls_left // 2 + 1))

    if include_boundary and balls_left >= 3 and remaining >= 8:
        candidates = [i for i in range(balls_left) if i != wicket_pos]
        if candidates:
            boundary_pos = random.choice(candidates[:len(candidates) // 2 + 1])

    for i in range(balls_left):
        balls_after = balls_left - i - 1

        if i == wicket_pos:
            # Run out with 1 run scored
            sequence.append((1, True))
            remaining -= 1
        elif i == boundary_pos and remaining >= 4:
            sequence.append((4, False))
            remaining -= 4
        elif remaining <= 0:
            sequence.append((0, False))
        else:
            # Pick a valid run value based on average needed
            avg_needed = remaining / max(1, balls_after + 1)
            if avg_needed <= 0.5:
                r = 0
            elif avg_needed <= 1.5:
                r = random.choice([0, 1, 1])
            elif avg_needed <= 2.5:
                r = random.choice([1, 1, 2])
            elif avg_needed <= 4:
                r = random.choice([1, 2, 2, 4])
            else:
                r = random.choice([2, 4, 4, 6])
            # Never exceed remaining, and snap to valid
            r = _snap_to_valid(min(r, remaining))
            sequence.append((r, False))
            remaining -= r

    # Final adjustment pass: fix any deficit/surplus using ONLY valid swaps
    # Try multiple passes to converge
    for _ in range(10):
        if remaining == 0:
            break
        adjusted = False
        # Iterate backwards for deficit (need more runs), forwards for surplus
        indices = range(len(sequence) - 1, -1, -1) if remaining > 0 else range(len(sequence))
        for i in indices:
            runs_val, is_wkt = sequence[i]
            if is_wkt:
                continue  # Don't touch wicket balls
            # Try each valid value that moves us closer to target
            for candidate in sorted(VALID_RUNS, reverse=(remaining > 0)):
                diff = candidate - runs_val
                if remaining > 0 and diff > 0 and diff <= remaining:
                    sequence[i] = (candidate, False)
                    remaining -= diff
                    adjusted = True
                    break
                elif remaining < 0 and diff < 0 and diff >= remaining:
                    sequence[i] = (candidate, False)
                    remaining -= diff
                    adjusted = True
                    break
            if remaining == 0:
                break
        if not adjusted:
            break

    # Validation: ensure every value is legal
    for i in range(len(sequence)):
        runs_val, is_wkt = sequence[i]
        if runs_val not in VALID_RUNS:
            sequence[i] = (_snap_to_valid(runs_val), is_wkt)

    return sequence


def _generate_last_ball_six_script(runs_needed, wickets_remaining, balls_left):
    """
    Script for 'last ball six': batter hits 6 on the final ball to win.
    Builds a sequence where all runs except 6 are scored in balls 1..(N-1),
    then a 6 on ball N.
    """
    if balls_left <= 1:
        # Only 1 ball left, just hit the six
        return [{"runs": 6, "is_wicket": False}]

    runs_before_last = runs_needed - 6
    if runs_before_last < 0:
        runs_before_last = 0

    include_wicket = wickets_remaining >= 4 and balls_left >= 6
    include_boundary = balls_left >= 5

    pre_sequence = _distribute_runs(
        runs_before_last, balls_left - 1,
        include_wicket=include_wicket,
        include_boundary=include_boundary
    )

    script = []
    for runs_val, is_wkt in pre_sequence:
        script.append({"runs": runs_val, "is_wicket": is_wkt})

    # The climactic final ball
    script.append({"runs": 6, "is_wicket": False})
    return script


def _generate_win_by_1_run_script(runs_needed, wickets_remaining, balls_left):
    """
    Script for 'win by 1 run': chasing team falls 1 run short.
    Supports two last-ball endings:
      1) Wicket on the final ball (Caught/Run Out)
      2) 6 needed to win on final ball, batter hits 4
    Ending mode is chosen at the start of the last over based on runs needed.
    """
    if balls_left <= 0:
        return []

    def _add_tension_wickets(script_local):
        """
        Replace a couple of late dot balls with wicket balls so the finish feels
        less "dot-ball heavy" while preserving total runs.
        """
        if not script_local:
            return script_local

        # Keep this conservative to avoid unrealistic collapses.
        if wickets_remaining >= 5:
            desired_total_wickets = 2
        elif wickets_remaining >= 3:
            desired_total_wickets = 1
        else:
            desired_total_wickets = 0

        existing_wickets = sum(1 for b in script_local if b.get("is_wicket"))
        wickets_to_add = max(0, desired_total_wickets - existing_wickets)
        if wickets_to_add == 0:
            return script_local

        # Leave enough wickets in hand so the chase isn't all-out before the finish.
        max_safe_additional = max(0, wickets_remaining - existing_wickets - 1)
        wickets_to_add = min(wickets_to_add, max_safe_additional)
        if wickets_to_add <= 0:
            return script_local

        # Prefer wicket moments in the latter half, but never on the final ball.
        start_idx = max(0, len(script_local) // 2 - 1)
        candidate_indices = [
            i for i in range(start_idx, max(0, len(script_local) - 1))
            if (
                not script_local[i].get("is_wicket")
                and script_local[i].get("runs", 0) == 0
            )
        ]
        if not candidate_indices:
            candidate_indices = [
                i for i in range(0, max(0, len(script_local) - 1))
                if (
                    not script_local[i].get("is_wicket")
                    and script_local[i].get("runs", 0) == 0
                )
            ]
        if not candidate_indices:
            return script_local

        random.shuffle(candidate_indices)
        for idx in candidate_indices[:wickets_to_add]:
            script_local[idx] = {
                "runs": 0,
                "is_wicket": True,
                "wicket_type": random.choice(["Caught", "Bowled", "LBW"])
            }

        return script_local

    def _build_finish(need_now, balls_now, mode):
        """
        Build remaining-ball script ending in the requested mode while
        preserving a 1-run loss margin.
        """
        if balls_now <= 0:
            return []

        if mode == "six_needed_hit_four":
            # Final ball: 6 to win (5 tie), batter hits 4.
            need_before_last_ball = 6
            last_ball = {"runs": 4, "is_wicket": False}
        else:
            # Final ball: 2 to win (1 tie), wicket trying to score.
            need_before_last_ball = 2
            last_ball = {
                "runs": 0,
                "is_wicket": True,
                # Avoid Run Out here; match logic credits 1 run on run-outs.
                "wicket_type": random.choice(["Caught", "Bowled", "LBW"])
            }

        pre_balls = balls_now - 1
        runs_before_last_ball = need_now - need_before_last_ball
        if runs_before_last_ball < 0:
            return None
        if pre_balls >= 0 and runs_before_last_ball > pre_balls * 6:
            return None

        script_local = []
        if pre_balls > 0:
            pre_seq = _distribute_runs(
                runs_before_last_ball,
                pre_balls,
                include_wicket=(wickets_remaining >= 5 and pre_balls >= 6),
                include_boundary=(pre_balls >= 4)
            )
            for runs_val, is_wkt in pre_seq:
                script_local.append({"runs": runs_val, "is_wicket": is_wkt})

        script_local.append(last_ball)
        return script_local

    def _pick_mode_from_last_over_need(last_over_need):
        # Higher required runs in last over -> use big-hit finish.
        if last_over_need >= 6:
            return "six_needed_hit_four"
        return "last_ball_wicket"

    # Case 1: already in last over (or later) -> decide directly from current need.
    if balls_left <= 6:
        primary_mode = _pick_mode_from_last_over_need(runs_needed)
        script = _build_finish(runs_needed, balls_left, primary_mode)
        if script is None:
            fallback_mode = "last_ball_wicket" if primary_mode == "six_needed_hit_four" else "six_needed_hit_four"
            script = _build_finish(runs_needed, balls_left, fallback_mode)
        if script is None:
            return []
        return _add_tension_wickets(script)

    # Case 2: before last over -> shape pre-last-over balls, then choose ending
    # from projected start-of-last-over requirement.
    pre_last_over_balls = balls_left - 6
    min_last_over_need = max(2, runs_needed - pre_last_over_balls * 6)
    max_last_over_need = min(runs_needed, 36)
    if min_last_over_need > max_last_over_need:
        min_last_over_need = max_last_over_need

    projected_last_over_need = round(runs_needed * 6 / balls_left)
    projected_last_over_need = max(min_last_over_need, min(max_last_over_need, projected_last_over_need))

    primary_mode = _pick_mode_from_last_over_need(projected_last_over_need)
    if primary_mode == "six_needed_hit_four" and projected_last_over_need < 6:
        projected_last_over_need = 6
    if primary_mode == "last_ball_wicket" and projected_last_over_need < 2:
        projected_last_over_need = 2

    last_over_script = _build_finish(projected_last_over_need, 6, primary_mode)
    if last_over_script is None:
        fallback_mode = "last_ball_wicket" if primary_mode == "six_needed_hit_four" else "six_needed_hit_four"
        last_over_script = _build_finish(projected_last_over_need, 6, fallback_mode)
        if last_over_script is None:
            return []

    runs_before_last_over = runs_needed - projected_last_over_need
    if runs_before_last_over < 0:
        runs_before_last_over = 0

    pre_sequence = _distribute_runs(
        runs_before_last_over,
        pre_last_over_balls,
        include_wicket=(wickets_remaining >= 5 and pre_last_over_balls >= 6),
        include_boundary=(pre_last_over_balls >= 4)
    )

    script = []
    for runs_val, is_wkt in pre_sequence:
        script.append({"runs": runs_val, "is_wicket": is_wkt})
    script.extend(last_over_script)
    return _add_tension_wickets(script)


def _generate_super_over_script(runs_needed, wickets_remaining, balls_left):
    """
    Script for 'super over thriller': match ties exactly.
    Score exactly (runs_needed - 1) across all balls so score == target - 1.
    """
    total_to_score = runs_needed - 1  # tie means score == target - 1
    if total_to_score < 0:
        total_to_score = 0

    include_wicket = wickets_remaining >= 5 and balls_left >= 6

    sequence = _distribute_runs(
        total_to_score, balls_left,
        include_wicket=include_wicket,
        include_boundary=(balls_left >= 5)
    )

    script = []
    for runs_val, is_wkt in sequence:
        script.append({"runs": runs_val, "is_wicket": is_wkt})
    return script


# --------------------------------------------------------------------------- #
#  Main ScenarioEngine class
# --------------------------------------------------------------------------- #

class ScenarioEngine:
    """
    Manages scenario-based match steering.
    Attached to a Match instance when scenario_mode is set.
    """

    def __init__(self, scenario_type, match):
        self.scenario_type = scenario_type
        self.match = match
        self.config = SCENARIO_CONFIG.get(scenario_type, {})
        self.finale_script = None
        self.finale_ball_index = 0
        self.active = True
        self._convergence_logged = False
        self._endgame_checked_overs = set()  # tracks which overs have been checked

        logger.info(f"[Scenario] Initialized: {scenario_type}")

    def on_innings_transition(self):
        """Called when innings transitions from 1st to 2nd."""
        self.finale_script = None
        self.finale_ball_index = 0
        self._convergence_logged = False
        self._endgame_checked_overs = set()
        logger.info(f"[Scenario] Innings transition — ready for 2nd innings steering")

    def _is_endgame_scenario_feasible(self, runs_needed, wickets_remaining, balls_left):
        """
        Decide whether scenario steering is still believable from the start
        of the last 3 overs. If not, fall back to normal simulation.
        """
        if runs_needed <= 0 or wickets_remaining <= 0 or balls_left <= 0:
            return False

        required_rr = (runs_needed * 6) / max(1, balls_left)

        # Universal realism guard: very low pressure + wickets in hand should not
        # be dragged into forced last-ball drama.
        if balls_left >= 10 and runs_needed <= 4 and wickets_remaining >= 4:
            return False
        if balls_left >= 8 and wickets_remaining >= 5 and required_rr < 3.0:
            return False

        # Scenario-specific feasibility checks.
        if self.scenario_type == "last_ball_six":
            if runs_needed < 6:
                return False
            if balls_left >= 12 and runs_needed < 10 and wickets_remaining >= 4:
                return False
            if runs_needed > balls_left * 5:
                return False
        elif self.scenario_type == "win_by_1_run":
            if balls_left >= 12 and runs_needed < 7 and wickets_remaining >= 4:
                return False
            if runs_needed > balls_left * 5:
                return False
        elif self.scenario_type == "super_over_thriller":
            if balls_left >= 12 and runs_needed < 6 and wickets_remaining >= 4:
                return False
            if runs_needed - 1 > balls_left * 5:
                return False

        return True

    def _evaluate_endgame_feasibility_if_needed(self):
        """
        Per-over feasibility check at the start of each of the last 3 overs
        (overs 17, 18, 19 in 0-indexed terms). Disables scenario mode when
        forcing the scripted path would look unnatural.

        Runs once per over so it catches situations that become too easy *during*
        the finale (e.g. batting team needed 15 at the start of over 17 but only
        4 at the start of over 18 because the scenario steered them there).
        """
        if not self.active or self.match.innings != 2:
            return
        if self.match.target is None:
            return

        current_over = self.match.current_over
        if current_over < 17:
            return

        # Only check once per over (not on every ball)
        if current_over in self._endgame_checked_overs:
            return

        runs_needed = self.match.target - self.match.score
        wickets_remaining = 10 - self.match.wickets
        balls_left = (self.match.fmt.overs - current_over) * 6 - self.match.current_ball

        self._endgame_checked_overs.add(current_over)
        if not self._is_endgame_scenario_feasible(runs_needed, wickets_remaining, balls_left):
            self.active = False
            self.finale_script = None
            self.finale_ball_index = 0
            logger.info(
                "[Scenario] Disabled at over %s.%s — infeasible endgame: "
                "need=%s, wkts=%s, balls=%s",
                current_over,
                self.match.current_ball,
                runs_needed,
                wickets_remaining,
                balls_left,
            )

    # ------------------------------------------------------------------ #
    #  Phase detection
    # ------------------------------------------------------------------ #

    def get_phase(self):
        """Return the current scenario phase."""
        if self.match.innings == 1:
            return "first_innings"

        # Per-over realism gate: re-evaluates at the start of each of overs 17/18/19.
        self._evaluate_endgame_feasibility_if_needed()

        if not self.active:
            return "inactive"

        over = self.match.current_over
        if over < 15:
            return "free_play"
        elif over < 18:
            return "convergence"
        else:
            return "finale"

    # ------------------------------------------------------------------ #
    #  Bias for free-play and convergence phases
    # ------------------------------------------------------------------ #

    def get_scenario_bias(self, match_state):
        """
        Return pressure_effects modifications for the current phase.
        Returns a dict of multiplicative modifiers to merge into pressure_effects.
        Only active during 2nd innings, free_play and convergence phases.
        """
        phase = self.get_phase()

        if phase in ("first_innings", "inactive", "finale"):
            return {}

        if self.match.innings != 2 or self.match.target is None:
            return {}

        runs_needed = self.match.target - self.match.score
        balls_remaining = (self.match.fmt.overs - self.match.current_over) * 6 - self.match.current_ball
        wickets_remaining = 10 - self.match.wickets

        if balls_remaining <= 0:
            return {}

        target = self.config.get("convergence_target", {})
        ideal_rn_low, ideal_rn_high = target.get("runs_needed_range", (20, 28))
        ideal_wk_low, ideal_wk_high = target.get("wickets_range", (3, 6))

        # Calculate how many balls until finale (over 18)
        balls_to_finale = max(1, (18 - self.match.current_over) * 6 - self.match.current_ball)

        # Ideal runs needed at start of finale
        ideal_runs_needed = (ideal_rn_low + ideal_rn_high) / 2
        ideal_wickets_in_hand = 10 - (ideal_wk_low + ideal_wk_high) / 2

        # Current trajectory: where will we be at over 18 at current rate?
        if balls_remaining > balls_to_finale:
            current_rr = self.match.score / max(1, self.match.current_over * 6 + self.match.current_ball)
            projected_score_at_18 = self.match.score + current_rr * balls_to_finale
            projected_rn_at_18 = self.match.target - projected_score_at_18
        else:
            projected_rn_at_18 = runs_needed

        bias = {}

        if phase == "free_play":
            # Light nudges: ±15%
            strength = 0.15
        else:
            # Convergence: ±30%
            strength = 0.30
            if not self._convergence_logged:
                logger.info(f"[Scenario] Convergence started: need={runs_needed}, wkts={wickets_remaining}, target_rn=({ideal_rn_low}-{ideal_rn_high})")
                self._convergence_logged = True

        # Scoring bias: if team is too far ahead, slow them; if behind, speed up
        if projected_rn_at_18 < ideal_rn_low:
            # Team scoring too fast — reduce boundaries, boost dots
            bias["boundary_modifier"] = 1 - strength
            bias["dot_bonus"] = 0.03 * (strength / 0.15)
        elif projected_rn_at_18 > ideal_rn_high:
            # Team scoring too slow — boost boundaries, reduce dots
            bias["boundary_modifier"] = 1 + strength
            bias["dot_bonus"] = -0.02 * (strength / 0.15)

        # Wicket bias
        current_wickets = self.match.wickets
        if current_wickets < ideal_wk_low and phase == "convergence":
            # Not enough wickets — slightly boost
            bias["wicket_modifier"] = 1 + strength * 0.5
        elif current_wickets > ideal_wk_high:
            # Too many wickets — reduce
            bias["wicket_modifier"] = 1 - strength * 0.7

        return bias

    # ------------------------------------------------------------------ #
    #  Finale: scripted outcome overrides
    # ------------------------------------------------------------------ #

    def get_override_outcome(self, batter, bowler):
        """
        During finale phase, return a scripted outcome dict.
        Returns None if not in finale phase or script is exhausted.
        """
        if self.get_phase() != "finale":
            return None

        # Generate script on first call
        if self.finale_script is None:
            self._generate_finale_script()
            if self.finale_script is None:
                # Couldn't generate — fall back to normal sim
                self.active = False
                logger.warning("[Scenario] Could not generate finale script, falling back to normal")
                return None

        # Check if we have balls left in the script
        if self.finale_ball_index >= len(self.finale_script):
            return None

        ball_spec = self.finale_script[self.finale_ball_index]
        self.finale_ball_index += 1

        # Build the outcome dict
        if ball_spec.get("is_wicket"):
            outcome = _make_wicket_outcome(
                batter,
                bowler,
                self.match.bowling_team,
                runs=ball_spec.get("runs", 1),
                wicket_type=ball_spec.get("wicket_type")
            )
        else:
            outcome = _make_run_outcome(ball_spec["runs"], batter, bowler)

        logger.debug(f"[Scenario] Finale ball {self.finale_ball_index}: {outcome['runs']}r {'W' if outcome.get('batter_out') else ''}")
        return outcome

    def _generate_finale_script(self):
        """Build the finale ball sequence based on current match state."""
        runs_needed = self.match.target - self.match.score
        wickets_remaining = 10 - self.match.wickets
        balls_left = (self.match.fmt.overs - self.match.current_over) * 6 - self.match.current_ball

        logger.info(f"[Scenario] Generating finale: type={self.scenario_type}, "
                     f"runs_needed={runs_needed}, wickets={wickets_remaining}, balls={balls_left}")

        # Sanity checks
        if balls_left <= 0 or wickets_remaining <= 0:
            self.finale_script = None
            return

        if self.scenario_type == "last_ball_six":
            self._generate_last_ball_six(runs_needed, wickets_remaining, balls_left)
        elif self.scenario_type == "win_by_1_run":
            self._generate_win_by_1_run(runs_needed, wickets_remaining, balls_left)
        elif self.scenario_type == "super_over_thriller":
            self._generate_super_over(runs_needed, wickets_remaining, balls_left)
        else:
            self.finale_script = None

    def _generate_last_ball_six(self, runs_needed, wickets_remaining, balls_left):
        """Last-Ball Six: hit 6 on the very last ball to win."""
        # We need runs_needed >= 6 for this to work
        if runs_needed < 6:
            # Already too close — a last-ball six is impossible/meaningless.
            # The per-over feasibility check should have caught this; if we
            # still land here, abort cleanly rather than padding with dots.
            logger.warning(
                "[Scenario] Last-ball-six aborted at script generation: "
                "only %s needed off %s balls — disabling scenario",
                runs_needed, balls_left,
            )
            self.active = False
            self.finale_script = None
            return

        if runs_needed > balls_left * 5:
            # Too far behind — can't realistically script this
            logger.warning(f"[Scenario] Last-ball six impossible: need {runs_needed} off {balls_left}")
            self.finale_script = None
            return

        self.finale_script = _generate_last_ball_six_script(
            runs_needed, wickets_remaining, balls_left
        )

    def _generate_win_by_1_run(self, runs_needed, wickets_remaining, balls_left):
        """Win by 1 run: chasing team falls 1 run short."""
        if runs_needed <= 0:
            # Already won — can't do this scenario
            self.finale_script = None
            return

        if runs_needed > balls_left * 5:
            logger.warning(f"[Scenario] Win-by-1-run impossible: need {runs_needed} off {balls_left}")
            self.finale_script = None
            return

        self.finale_script = _generate_win_by_1_run_script(
            runs_needed, wickets_remaining, balls_left
        )

    def _generate_super_over(self, runs_needed, wickets_remaining, balls_left):
        """Super Over Thriller: match ties exactly."""
        if runs_needed <= 0:
            self.finale_script = None
            return

        if runs_needed - 1 > balls_left * 5:
            logger.warning(f"[Scenario] Super-over tie impossible: need {runs_needed} off {balls_left}")
            self.finale_script = None
            return

        self.finale_script = _generate_super_over_script(
            runs_needed, wickets_remaining, balls_left
        )
