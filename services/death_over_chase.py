"""Death Over Chase Momentum System.

A realism guardrail for chases in the death overs, shared by every over-by-over
chat match: ``/cipl``, ``/letsplay`` and the admin Challenge League commands
(``/c<league>``) — they all simulate through ``services.cipl_match.simulate_over``.

The problem it solves: when the chasing side has settled, non-bowler batters at
the crease and needs a *reasonable* number of runs, the raw engine can still
manufacture an unrealistic collapse. This module:

  1. Detects, once, which death-over chase scenario applies (sections 3-17 of the
     design spec) and a base "chase success" percentage.
  2. Rolls that percentage **a single time** at the start of the death-over
     phase to decide whether the chase should succeed, and freezes it.
  3. Each subsequent ball, returns a *bias* over the engine's raw outcome
     weights nudging deliveries toward the frozen result — success mode favours
     boundaries/singles and suppresses dots/wickets; failure mode tightens the
     screws for a close (not impossible) loss.

It is a **bias layer**, not an override: ``engine.ball_outcome.calculate_outcome``
still samples the actual delivery (so ratings, pitch, traits, dismissal types and
commentary stay intact). The bias is applied through that function's generic
``weight_hook``.

This mirrors the spec faithfully but keeps the percentages from the spec's
*function code* (sections 3-17), which are authoritative over the looser
"Clean Rule Summary" table.

No player traits are consulted here — the system is purely match-state driven.
"""

import logging
import random

logger = logging.getLogger(__name__)

# Engine raw-weight keys (engine.ball_outcome). The spec's outcome vocabulary
# (dot/one/two/three/four/six/wicket/extra) maps onto these.
K_DOT = "Dot"
K_ONE = "Single"
K_TWO = "Double"
K_THREE = "Three"
K_FOUR = "Four"
K_SIX = "Six"
K_WICKET = "Wicket"
K_EXTRA = "Extras"


# ── small helpers ────────────────────────────────────────────────────

def _is_bowler(player):
    return (player or {}).get("category") == "Bowler"


def _is_non_bowler(player):
    return not _is_bowler(player)


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


# ── snapshot ─────────────────────────────────────────────────────────

def build_snapshot(state, *, balls_left, runs_needed, striker, non_striker,
                   striker_balls, non_striker_balls):
    """Assemble the minimal state the detector needs from a cipl_match state.

    ``striker`` / ``non_striker`` are the engine player dicts (with ``category``
    and ``bat_rating``); ``*_balls`` are the balls each has faced so far.
    """
    return {
        "innings": int(state.get("innings", 0) or 0),
        "ballsLeft": int(balls_left),
        "runsNeeded": int(runs_needed),
        "wicketsLeft": int(state.get("wicket_limit", 10)) - int(state.get("total_wickets", 0)),
        "teamScore": int(state.get("total_runs", 0) or 0),
        "partnershipRuns": int(state.get("partnership_runs", 0) or 0),
        "striker": {
            "category": (striker or {}).get("category", "Batsman"),
            "balls": int(striker_balls or 0),
            "batRating": int((striker or {}).get("bat_rating", 50) or 50),
        },
        "nonStriker": {
            "category": (non_striker or {}).get("category", "Batsman"),
            "balls": int(non_striker_balls or 0),
            "batRating": int((non_striker or {}).get("bat_rating", 50) or 50),
        },
    }


# ── base scenarios (spec sections 3-17; function-code percentages) ────

def _both_bowlers_batting(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 6 and s["runsNeeded"] <= 15
            and _is_bowler(s["striker"]) and _is_bowler(s["nonStriker"])):
        return {"active": True, "scenario": "Both Bowlers Batting Rule",
                "chaseSuccessChance": None, "useNormalEngine": True}
    return {"active": False}


def _weak_batter_on_strike(s):
    # A tail-ender on strike caps the chase at 50% — this rule exists to SUPPRESS
    # a generous chance, so it is flagged no-boost (otherwise the +40 "200+
    # protection" boost, which only inspects the non-striker, would lift it back
    # to 95% and defeat the cap).
    if (s["innings"] == 2 and s["ballsLeft"] <= 6 and s["runsNeeded"] <= 15
            and _is_bowler(s["striker"]) and _is_non_bowler(s["nonStriker"])):
        return {"active": True, "scenario": "Weak Batter On Strike Rule",
                "chaseSuccessChance": 50, "noBoost": True}
    return {"active": False}


def _very_easy_last_over(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 6 and s["runsNeeded"] <= 6
            and s["wicketsLeft"] >= 3):
        return {"active": True, "scenario": "Very Easy Last Over Chase",
                "chaseSuccessChance": 90}
    return {"active": False}


def _easy_last_over(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 6 and s["runsNeeded"] <= 10
            and s["wicketsLeft"] >= 4
            and (s["striker"]["balls"] >= 12 or s["nonStriker"]["balls"] >= 12)):
        return {"active": True, "scenario": "Easy Last Over Chase",
                "chaseSuccessChance": 88}
    return {"active": False}


def _last_over_smart(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 6 and s["runsNeeded"] <= 15
            and _is_non_bowler(s["striker"]) and _is_non_bowler(s["nonStriker"])
            and s["striker"]["balls"] >= 10 and s["nonStriker"]["balls"] >= 10):
        return {"active": True, "scenario": "Last Over Smart Chase",
                "chaseSuccessChance": 80}
    return {"active": False}


def _one_set_batter_finish(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 6 and s["runsNeeded"] <= 14
            and _is_non_bowler(s["striker"]) and s["striker"]["balls"] >= 20):
        return {"active": True, "scenario": "One Set Batter Finish Rule",
                "chaseSuccessChance": 86}
    return {"active": False}


def _final_three_balls(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 3 and s["runsNeeded"] <= 7
            and _is_non_bowler(s["striker"]) and s["striker"]["batRating"] >= 80):
        return {"active": True, "scenario": "Final 3 Balls Chase Rule",
                "chaseSuccessChance": 70}
    return {"active": False}


def _last_two_overs_controlled(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 12 and s["runsNeeded"] <= 25
            and _is_non_bowler(s["striker"]) and _is_non_bowler(s["nonStriker"])
            and s["striker"]["balls"] >= 8 and s["nonStriker"]["balls"] >= 8):
        return {"active": True, "scenario": "Last 2 Overs Controlled Chase",
                "chaseSuccessChance": 80}
    return {"active": False}


def _wickets_left_safety(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 18
            and s["runsNeeded"] <= s["ballsLeft"] * 2 and s["wicketsLeft"] >= 6):
        return {"active": True, "scenario": "Wickets Left Safety Rule",
                "chaseSuccessChance": 78}
    return {"active": False}


def _last_two_overs_pressure(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 12
            and 26 <= s["runsNeeded"] <= 32 and s["wicketsLeft"] >= 5
            and (s["striker"]["batRating"] >= 88 or s["nonStriker"]["batRating"] >= 88)):
        return {"active": True, "scenario": "Last 2 Overs Pressure Chase",
                "chaseSuccessChance": 55}
    return {"active": False}


def _tough_but_possible(s):
    if (s["innings"] == 2 and s["ballsLeft"] <= 6
            and 16 <= s["runsNeeded"] <= 20 and s["wicketsLeft"] >= 4
            and _is_non_bowler(s["striker"]) and _is_non_bowler(s["nonStriker"])
            and (s["striker"]["batRating"] >= 85 or s["nonStriker"]["batRating"] >= 85)):
        return {"active": True, "scenario": "Tough But Possible Last Over Chase",
                "chaseSuccessChance": 65}
    return {"active": False}


def get_base_chase_scenario(s):
    """Highest-priority applicable scenario (spec section 18 order, with the
    weak-batter rule placed just after the both-bowlers guard so a tail-ender on
    strike can't inherit a settled-pair chance)."""
    for fn in (
        _both_bowlers_batting,
        _weak_batter_on_strike,
        _very_easy_last_over,
        _easy_last_over,
        _last_over_smart,
        _one_set_batter_finish,
        _final_three_balls,
        _last_two_overs_controlled,
        _wickets_left_safety,
        _last_two_overs_pressure,
        _tough_but_possible,
    ):
        res = fn(s)
        if res.get("active"):
            return res
    return {"active": False, "scenario": "Normal Engine",
            "chaseSuccessChance": None, "useNormalEngine": True}


# ── boosts (spec sections 7-11) ──────────────────────────────────────

def _set_pair_boost(s, chance):
    if (s["innings"] == 2 and s["runsNeeded"] <= s["ballsLeft"] * 2.5
            and s["striker"]["balls"] >= 15 and s["nonStriker"]["balls"] >= 15
            and s["partnershipRuns"] >= 40):
        return _clamp(chance + 15, 0, 95)
    return chance


def _high_score_momentum(s, chance):
    if (s["innings"] == 2 and s["teamScore"] >= 180 and s["runsNeeded"] <= 30
            and s["ballsLeft"] <= 18 and s["wicketsLeft"] >= 4):
        return _clamp(chance + 20, 0, 95)
    return chance


def _two_hundred_protection(s, chance):
    striker_set = _is_non_bowler(s["striker"]) and s["striker"]["balls"] >= 10
    non_set = _is_non_bowler(s["nonStriker"]) and s["nonStriker"]["balls"] >= 10
    if (s["innings"] == 2 and s["teamScore"] >= 200 and s["runsNeeded"] <= 18
            and s["ballsLeft"] <= 9 and (striker_set or non_set)):
        return _clamp(chance + 40, 0, 95)
    return chance


def apply_chase_boosts(s, base):
    if not base.get("active") or base.get("useNormalEngine") or base.get("noBoost"):
        return base
    chance = base["chaseSuccessChance"]
    chance = _set_pair_boost(s, chance)
    chance = _high_score_momentum(s, chance)
    chance = _two_hundred_protection(s, chance)
    chance = _clamp(chance, 0, 95)
    return {**base, "chaseSuccessChance": chance}


def detect_scenario(s):
    return apply_chase_boosts(s, get_base_chase_scenario(s))


def decide_chase_result(scenario, *, rng=random):
    """Roll the frozen success/failure decision (spec section 21)."""
    if (not scenario.get("active") or scenario.get("useNormalEngine")
            or scenario.get("chaseSuccessChance") is None):
        return {"useNormalEngine": True, "chaseWillSucceed": None}
    roll = rng.randint(1, 100)
    return {
        "useNormalEngine": False,
        "chaseWillSucceed": roll <= scenario["chaseSuccessChance"],
        "roll": roll,
        "chance": scenario["chaseSuccessChance"],
        "scenario": scenario["scenario"],
    }


# ── weight bias (guardrail applied to engine raw weights) ────────────
#
# Multipliers are derived from the spec's success / failure outcome tables
# (sections 22, 26-27) expressed as ratios over the default last-over weights,
# so the engine keeps its own per-player character while being steered.

_SUCCESS_MULT = {
    K_DOT: 0.33, K_ONE: 0.80, K_TWO: 1.14, K_THREE: 1.0,
    K_FOUR: 1.56, K_SIX: 2.20, K_WICKET: 0.42, K_EXTRA: 1.0,
}

_FAILURE_MULT = {
    K_DOT: 1.44, K_ONE: 1.16, K_TWO: 1.14, K_THREE: 1.0,
    K_FOUR: 0.72, K_SIX: 0.50, K_WICKET: 1.50, K_EXTRA: 1.0,
}


def _scale(weights, key, factor):
    if key in weights:
        weights[key] = max(0.0, weights[key] * factor)


def _success_context_bias(weights, s):
    """Per-ball refinements for success mode (spec sections 12 & 26)."""
    rn, bl, wl = s["runsNeeded"], s["ballsLeft"], s["wicketsLeft"]

    # Anti-Unrealistic Collapse Rule (section 12): settled pair, reasonable ask.
    if (rn <= 12 and bl >= 6 and wl >= 5
            and _is_non_bowler(s["striker"]) and _is_non_bowler(s["nonStriker"])):
        _scale(weights, K_DOT, 0.80)
        _scale(weights, K_WICKET, 0.80)
        _scale(weights, K_ONE, 1.10)
        _scale(weights, K_TWO, 1.05)
        _scale(weights, K_FOUR, 1.15)
        _scale(weights, K_SIX, 1.10)

    if bl > 0 and rn >= bl * 1.5:
        # Steep ask the system has nonetheless decided to back: the steeper the
        # required rate, the harder the bias leans on boundaries (and the more it
        # suppresses dots/wickets) so a decided-successful chase actually gets
        # home, without resorting to an all-sixes cartoon over.
        steep = _clamp((rn / bl) / 2.0, 0.6, 1.5)   # ~0.75 at RRR 9 → 1.5 cap
        _scale(weights, K_FOUR, 1.0 + 0.30 * steep)
        _scale(weights, K_SIX, 1.0 + 0.55 * steep)
        _scale(weights, K_DOT, 1.0 - 0.30 * steep)
        _scale(weights, K_WICKET, 1.0 - 0.30 * steep)
    elif rn <= 6 and bl <= 6:
        # Tiny ask → close it out in ones/twos, fewer wickets, less drama.
        _scale(weights, K_ONE, 1.30)
        _scale(weights, K_TWO, 1.20)
        _scale(weights, K_WICKET, 0.70)
        _scale(weights, K_SIX, 0.85)


def _finish_early_bias(weights, s):
    """Reduce last-ball drama: when a decided-successful chase has a comfortable
    ask and a set batter on strike, lift scoring so the target is usually reached
    with balls to spare instead of crawling to the final delivery."""
    if s["runsNeeded"] <= s["ballsLeft"] and (
            s["striker"]["balls"] >= 10 or s["nonStriker"]["balls"] >= 10):
        _scale(weights, K_DOT, 0.70)
        _scale(weights, K_ONE, 1.20)
        _scale(weights, K_TWO, 1.15)
        _scale(weights, K_FOUR, 1.25)
        _scale(weights, K_SIX, 1.20)


def make_bias_hook(decision, snapshot_fn):
    """Return a ``weight_hook`` (raw_weights -> raw_weights) that biases each
    delivery toward the frozen ``decision``. ``snapshot_fn`` is called per ball
    to read the live runs/balls/wickets situation. Returns None when the chase
    should fall through to the normal engine.
    """
    if not decision or decision.get("useNormalEngine"):
        return None
    succeed = decision.get("chaseWillSucceed")
    if succeed is None:
        return None
    mult = _SUCCESS_MULT if succeed else _FAILURE_MULT

    def _hook(raw_weights):
        w = dict(raw_weights)
        for key, factor in mult.items():
            _scale(w, key, factor)
        s = snapshot_fn()
        if succeed and s is not None:
            _success_context_bias(w, s)
            _finish_early_bias(w, s)
        return w

    return _hook


def get_message(snapshot, scenario):
    """Optional bot-facing summary (spec section 30); callers may ignore it."""
    if not scenario.get("active") or scenario.get("useNormalEngine"):
        return None
    return (
        "🔥 Smart Chase Momentum Activated!\n"
        f"Need: {snapshot['runsNeeded']} runs from {snapshot['ballsLeft']} balls\n"
        f"Scenario: {scenario['scenario']}\n"
        f"Chase Chance: {scenario['chaseSuccessChance']}%"
    )
