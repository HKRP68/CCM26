"""Chase / Defence chance model.

A situation-aware estimate of how likely the chasing side is to overhaul the
target, driven by a runs-needed × wickets-lost matrix plus small player / pitch /
momentum modifiers. It is used as a *controlled steering modifier* on the
ball-outcome probability matrix during a 2nd-innings chase — it never selects the
winner outright; the match is always decided ball by ball.

Section 9 of the Unified T20 Engine v3.0 (the Context-Aware Chase Algorithm)
is implemented as the matrix plus the Step-2 factors below. One of the doc's
factors is deliberately absent: **"+4% per wicket remaining"**. Wickets in hand
are already an axis of ``CHASE_MATRIX`` — they are the only thing its four
columns measure — so adding the linear term as well would price wickets twice
and make a 0-down chase of 60 read as a near-certainty.

Public API:
    base_chase_chance(runs_needed, wickets_lost) -> int        (matrix only)
    final_chase_chance(runs_needed, wickets_lost, **mods) -> dict
    chase_steer_effects(chase_chance, strength=1.0) -> dict     (pressure_effects)

    batter_modifier / bowler_modifier / pitch_modifier          (Step 2)
    momentum_modifier / mpi_modifier
    powerplay_modifier / deterioration_modifier / nothing_to_lose_modifier
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
    if runs_needed <= 15:
        return "1-15"
    if runs_needed <= 20:
        return "16-20"
    if runs_needed <= 25:
        return "21-25"
    if runs_needed <= 30:
        return "26-30"
    if runs_needed <= 35:
        return "31-35"
    if runs_needed <= 40:
        return "36-40"
    if runs_needed <= 45:
        return "41-45"
    if runs_needed <= 50:
        return "46-50"
    return "51+"


def get_wicket_index(wickets_lost):
    if wickets_lost <= 3:
        return 0
    if wickets_lost <= 5:
        return 1
    if wickets_lost <= 7:
        return 2
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


# Section 9 Step 2: an 85+ bowler with the Death Bowler trait, bowling in the
# closing overs, is the single biggest thing standing between a chase and the
# line — and the one the matrix cannot see, because the matrix does not know who
# is holding the ball.
DEATH_BOWLER_PENALTY = -8
DEATH_BOWLER_MIN_RATING = 85


def bowler_modifier(bowler, is_emergency=False, is_death_phase=False,
                    has_death_trait=False):
    """Emergency part-timer +8; weak front-liner (<70) +5; elite bowler (90+) -5
    to the chasing side; and an 85+ Death Bowler in overs 16-20 is -8 on top.

    ``is_death_phase`` / ``has_death_trait`` default false, so a caller that
    knows nothing about traits gets exactly the previous behaviour.
    """
    if is_emergency:
        return 8
    br = int((bowler or {}).get("bowl_rating", 50) or 50)
    mod = 0
    if br >= 90:
        mod -= 5
    elif br < 70:
        mod += 5
    if (is_death_phase and has_death_trait
            and br >= DEATH_BOWLER_MIN_RATING):
        mod += DEATH_BOWLER_PENALTY
    return mod


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


# Section 9 Step 2 weights.
MPI_MOMENTUM_PER_POINT = 5.0    # +5% per +1.0 momentum
MPI_MOMENTUM_CAP = 10.0
MPI_PRESSURE_PER_POINT = 6.0    # -6% per +1.0 pressure
MPI_PRESSURE_CAP = 12.0
POWERPLAY_BONUS = 10            # 60+ in the chasing side's powerplay
POWERPLAY_RUNS = 60
DETERIORATION_PENALTY = -5      # batting last on a surface that has broken up
DETERIORATING_PITCHES = {"Dusty", "Dry"}
NTL_BONUS = 5                   # nothing to lose: swing at a monster target
NTL_TARGET = 260
NTL_PITCHES = {"Flat", "Dead"}


def mpi_modifier(momentum=0.0, pressure=0.0):
    """Momentum and chasing pressure as a chase-chance swing.

    Prefer this over :func:`momentum_modifier` wherever the tracked accumulators
    from ``engine.momentum`` are available: that function has to *infer* momentum
    from the required rate and the last few balls, which is a proxy for the thing
    this one is handed directly. Each side is capped separately, per the doc.
    """
    up = _clamp(float(momentum or 0.0) * MPI_MOMENTUM_PER_POINT,
                -MPI_MOMENTUM_CAP, MPI_MOMENTUM_CAP)
    down = _clamp(float(pressure or 0.0) * MPI_PRESSURE_PER_POINT,
                  -MPI_PRESSURE_CAP, MPI_PRESSURE_CAP)
    return up - down


def powerplay_modifier(powerplay_runs):
    """A chasing side that took 60+ off the powerplay has banked the hardest
    part of the ask."""
    return POWERPLAY_BONUS if int(powerplay_runs or 0) >= POWERPLAY_RUNS else 0


def deterioration_modifier(pitch, innings=2):
    """Batting last on a surface that grips and cracks."""
    if int(innings or 0) != 2:
        return 0
    return DETERIORATION_PENALTY if pitch in DETERIORATING_PITCHES else 0


def nothing_to_lose_modifier(target, pitch):
    """Chasing 260+ on a road: there is no way to bat sensibly and win, so the
    side swings from ball one — and occasionally that comes off."""
    if pitch in NTL_PITCHES and int(target or 0) >= NTL_TARGET:
        return NTL_BONUS
    return 0


def feasibility_factor(runs_needed, balls_left):
    """0..1 scale applied to the chasing chance for the balls actually left.

    The matrix is keyed only on runs-needed × wickets, so without this a steep
    last-over ask (e.g. 16 off 1) would still read as batting-favoured. Full
    (1.0) up to ~2 runs/ball, fading to ~0 by 6 runs/ball (all-sixes territory).
    """
    if balls_left <= 0:
        return 0.0
    rpb = runs_needed / balls_left
    if rpb <= 2.0:
        return 1.0
    return max(0.0, (6.0 - rpb) / 4.0)


def apply_feasibility(info, runs_needed, balls_left):
    """Scale an ``info`` dict's chasing chance toward 1% as the required rate
    becomes unachievable for the balls left. Mutates and returns ``info``."""
    f = feasibility_factor(runs_needed, balls_left)
    if f <= 0.0:
        # Not "unlikely" — gone. Six an over off the last ball is not a chase.
        chance = 0
    else:
        chance = max(LOWER_CAP,
                     int(round(LOWER_CAP + (info["chasing_chance"] - LOWER_CAP) * f)))
    info["chasing_chance"] = chance
    info["defending_chance"] = 100 - chance
    return info


# Section 9 Step 4. No chase is safe until the winning runs are hit, and while
# it is mathematically possible somebody has done it — so the live estimate never
# reads as certainty in either direction. 0% is reserved for elimination, which
# is :func:`apply_feasibility`'s business, not this function's.
UPPER_CAP = 95
LOWER_CAP = 3

# Step 2 now contributes more terms than it used to (powerplay, deterioration,
# nothing-to-lose, the death bowler, and the MPI pair), so the total swing is
# allowed to be larger — but still small enough that the matrix, not the
# modifiers, decides what kind of chase this is.
MODIFIER_CLAMP = 25


def final_chase_chance(runs_needed, wickets_lost, batter_mod=0, bowler_mod=0,
                       pitch_mod=0, momentum_mod=0, situation_mod=0):
    """Full chasing-chance estimate with clamped modifiers. Returns a dict with
    the base chance, total modifier and clamped final chasing / defending %.

    ``situation_mod`` carries the Step-2 factors that are properties of the match
    rather than of the two players at the crease — powerplay, deterioration,
    nothing-to-lose — summed by the caller.
    """
    base = base_chase_chance(runs_needed, wickets_lost)
    total_mod = _clamp(batter_mod + bowler_mod + pitch_mod + momentum_mod
                       + situation_mod, -MODIFIER_CLAMP, MODIFIER_CLAMP)
    chasing = int(round(_clamp(base + total_mod, LOWER_CAP, UPPER_CAP)))
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


def chase_steer_effects(chasing_chance, strength=1.0):
    """Translate a chasing chance (%) into a ``pressure_effects`` bias dict that
    nudges the ball-outcome weights toward the favoured side. ``tilt`` is 0 at a
    50/50 contest, +1 when the chase is nailed-on, -1 when it's hopeless.

    Chasing favoured  → more boundaries, fewer dots, fewer wickets.
    Defending favoured→ the reverse.

    Note: deliberately does NOT emit ``single_boost`` — that key would override
    the pressure engine's ``strike_rotation_penalty`` branch in calculate_outcome
    and silently disable it. The boundary / wicket / dot nudges carry the steer.
    """
    base_tilt = _clamp((chasing_chance - 50) / 50.0, -1.0, 1.0)
    # Clamp again AFTER applying strength so a strength > 1 can't push a
    # multiplier negative (e.g. boundary_modifier below 0).
    tilt = _clamp(base_tilt * float(strength), -1.0, 1.0)
    return {
        "boundary_modifier": 1.0 + STEER_BOUNDARY_K * tilt,
        "wicket_modifier": 1.0 - STEER_WICKET_K * tilt,
        "dot_bonus": -STEER_DOT_K * tilt,
    }
