"""Chase / Defence chance model.

A situation-aware estimate of how likely the chasing side is to overhaul the
target, driven by a runs-needed × wickets-lost matrix plus small player / pitch /
momentum modifiers. It is used as a *controlled steering modifier* on the
ball-outcome probability matrix during a 2nd-innings chase — it never selects the
winner outright; the match is always decided ball by ball.

Public API:
    base_chase_chance(runs_needed, wickets_lost) -> int        (matrix only)
    final_chase_chance(runs_needed, wickets_lost, **mods) -> dict
    chase_steer_effects(chase_chance, strength=1.0) -> dict     (pressure_effects)
"""

# Chasing chance (%) by runs-needed band × wickets-lost band.
#   columns: 0 = 0-3 lost, 1 = 4-5 lost, 2 = 6-7 lost, 3 = 8-9 lost
CHASE_MATRIX = {
    "1-15":  [90, 82, 68, 52],
    "16-20": [82, 72, 55, 35],
    "21-25": [74, 63, 44, 25],
    "26-30": [66, 56, 35, 18],
    "31-35": [57, 47, 28, 12],
    "36-40": [48, 38, 20, 8],
    "41-45": [38, 28, 14, 5],
    "46-50": [27, 19, 9, 3],
    "51+":   [16, 10, 4, 1],
}


def get_run_range(runs_needed):
    if runs_needed <= 15: return "1-15"
    if runs_needed <= 20: return "16-20"
    if runs_needed <= 25: return "21-25"
    if runs_needed <= 30: return "26-30"
    if runs_needed <= 35: return "31-35"
    if runs_needed <= 40: return "36-40"
    if runs_needed <= 45: return "41-45"
    if runs_needed <= 50: return "46-50"
    return "51+"


def get_wicket_index(wickets_lost):
    if wickets_lost <= 3: return 0
    if wickets_lost <= 5: return 1
    if wickets_lost <= 7: return 2
    return 3


def base_chase_chance(runs_needed, wickets_lost):
    """Matrix-only chasing chance (%), no modifiers."""
    return CHASE_MATRIX[get_run_range(max(0, runs_needed))][get_wicket_index(max(0, wickets_lost))]


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ── Modifier helpers (operate on the engine-friendly state dict) ──────────

def batter_modifier(striker, non_striker, striker_runs=0):
    """+5 a set, elite (90+) batter on strike; +4 two recognised batters at the
    crease; -6 if both are genuine non-batsmen (rated < 65)."""
    sb = int((striker or {}).get("bat_rating", 50) or 50)
    nb = int((non_striker or {}).get("bat_rating", 50) or 50)
    mod = 0
    # "Set batter rated 90+": elite and already established (10+ runs) on strike.
    if sb >= 90 and striker_runs >= 10:
        mod += 5
    elif sb >= 90:
        mod += 3
    if sb >= 70 and nb >= 70:
        mod += 4
    if sb < 65 and nb < 65:
        mod -= 6
    return mod


def bowler_modifier(bowler, is_emergency=False):
    """Emergency part-timer +8; weak front-liner (<70) +5; elite death bowler
    (90+) -5 to the chasing side."""
    if is_emergency:
        return 8
    br = int((bowler or {}).get("bowl_rating", 50) or 50)
    if br >= 90:
        return -5
    if br < 70:
        return 5
    return 0


_SLOW_PITCHES = {"Green", "Dry", "Dusty", "Bouncy"}
_FLAT_PITCHES = {"Flat", "Dead"}


def pitch_modifier(pitch):
    if pitch in _FLAT_PITCHES:
        return 3
    if pitch in _SLOW_PITCHES:
        return -3
    return 0


def momentum_modifier(required_rr, recent_runs=None, recent_balls=None):
    """Small momentum nudge: an easy required rate (and recent scoring) favours
    the chase; a steep ask works against it. Bounded to roughly ±6."""
    mod = 0.0
    # Required run rate comfort: 8 rpo is the pivot.
    mod += _clamp((8.0 - float(required_rr)) * 0.8, -6.0, 4.0)
    # Recent scoring burst (last few balls) adds a touch of momentum.
    if recent_runs is not None and recent_balls:
        recent_rate = recent_runs / recent_balls * 6.0
        mod += _clamp((recent_rate - 8.0) * 0.25, -2.0, 3.0)
    return _clamp(mod, -6.0, 6.0)


def final_chase_chance(runs_needed, wickets_lost, batter_mod=0, bowler_mod=0,
                       pitch_mod=0, momentum_mod=0):
    """Full chasing-chance estimate with clamped modifiers. Returns a dict with
    the base chance, total modifier and clamped final chasing / defending %."""
    base = base_chase_chance(runs_needed, wickets_lost)
    total_mod = _clamp(batter_mod + bowler_mod + pitch_mod + momentum_mod, -15, 15)
    chasing = int(round(_clamp(base + total_mod, 1, 99)))
    return {
        "runs_needed": runs_needed,
        "wickets_lost": wickets_lost,
        "wickets_remaining": 10 - wickets_lost,
        "base_chasing_chance": base,
        "total_modifier": round(total_mod, 1),
        "chasing_chance": chasing,
        "defending_chance": 100 - chasing,
    }


# ── Steering: chance → ball-outcome bias ─────────────────────────────────

# How hard a 0/100 chance pulls the per-ball weights (tuned in cipl_match).
STEER_BOUNDARY_K = 0.55   # ±55% boundaries at the extremes
STEER_WICKET_K = 0.50     # ∓50% wickets
STEER_DOT_K = 0.06        # ∓0.06 dot share
STEER_SINGLE_K = 0.18     # ±18% singles


def chase_steer_effects(chasing_chance, strength=1.0):
    """Translate a chasing chance (%) into a ``pressure_effects`` bias dict that
    nudges the ball-outcome weights toward the favoured side. ``tilt`` is 0 at a
    50/50 contest, +1 when the chase is nailed-on, -1 when it's hopeless.

    Chasing favoured  → more boundaries/singles, fewer dots, fewer wickets.
    Defending favoured→ the reverse.
    """
    tilt = _clamp((chasing_chance - 50) / 50.0, -1.0, 1.0) * strength
    return {
        "boundary_modifier": 1.0 + STEER_BOUNDARY_K * tilt,
        "wicket_modifier": 1.0 - STEER_WICKET_K * tilt,
        "dot_bonus": -STEER_DOT_K * tilt,
        "single_boost": 1.0 + STEER_SINGLE_K * tilt,
    }
