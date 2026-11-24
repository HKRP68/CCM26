"""How the two numbers on the cards bend a baseline distribution.

The brief (§7) asks for a bounded, continuous, calibrated rating effect driven
by ``bat_rating`` and ``bowl_rating`` only — never ``overall_rating``, which
is derived from those two and would be a double count.

Three terms, all multiplicative on the baseline weight of each outcome:

1. **Gap term** — the relative contest.

       advantage = tanh((bat - bowl - gap_anchor) / scale)
       mult      = exp(gap_elasticity[outcome] * advantage * strength)

   ``tanh`` is used for the reason the brief gives: it is steep through the
   realistic range and saturates smoothly, so a 40-point gap is a big edge
   without being a certainty. ``scale`` is 17.0, matching
   ``engine/rating_duel.GAP_SCALE``, so the two models agree on what "a big
   gap" means.

2. **Level term** — absolute class, independent of the gap.

   At rating parity a 40 vs 40 contest is not a 90 vs 90 contest. Two
   tail-enders bowling at each other produce dots and edges, not sixes. This
   term is a function of the *batter's* rating relative to 75, which is the
   roster's competent-professional anchor.

   This is not "counting the rating twice": the gap term is the duel, the
   level term is the standard of cricket being played. They answer different
   questions and are calibrated jointly against the real-data anchors.

3. **Extras term** — a wide is a bowler error, not a contest, so extras
   scale with the bowler's class alone and ignore the batter entirely.

A fourth step is calibration rather than modelling: the three terms above
are divided by their **measured mean over the match-ups that actually
occur**. ``exp`` is convex, so by Jensen's inequality the average of
``exp(k * advantage)`` across an XI exceeds 1 even when the average advantage
is zero — a real side bats its #9, #10 and #11 against good bowlers. Without
that divisor the curve quietly raised the innings wicket rate from the
measured 5.3% anchor to 6.4% before any other layer ran. The divisors live in
``config/realism.json`` and are measured, not chosen:

    python -m tools.compare_realism --calibrate-curve --n 120

Everything is then bounded. ``max_multiplier`` caps any single factor, and
after normalisation the wicket share is clamped into
``[min_wicket_share, max_wicket_share]``. The floor is the part that matters
for the brief's §13.6: a 99-rated batter facing a 30-rated bowler still has
at least a 1.2% chance of being dismissed on any ball — about once every 83
balls — so a premium card is never un-dismissable.

Everything in this module is a pure function. No RNG is drawn, which is what
makes shadow mode free (§10).
"""

import math

from engine.realism.config import get_config

OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")


def advantage(bat_rating: float, bowl_rating: float, scale: float = None,
              anchor: float = 0.0) -> float:
    """Signed dominance in (-1, 1); positive means the batter is on top.

    *anchor* shifts the origin: the gap at which the curve is neutral.
    :func:`multipliers` passes the measured ``gap_anchor`` so that "neutral"
    means the average match-up in the reference data, not an arbitrary
    rating parity. Called without it this is the plain tanh of the gap.
    """
    cfg = get_config()["rating_curve"]
    sc = float(scale if scale is not None else cfg["scale"])
    if sc <= 0:
        return 0.0
    return math.tanh(((float(bat_rating) - float(bowl_rating))
                      - float(anchor)) / sc)


# The rating scale is 30-100, so the level term lives in about (-1.8, +1.0).
# It is clamped anyway: this function must never raise, because a delivery
# must never crash, and an out-of-range rating from a hand-built team file
# would otherwise overflow math.exp downstream.
LEVEL_CLAMP = 3.0


def batter_level(bat_rating: float) -> float:
    """Batter class relative to the 75 anchor, clamped to +/-3."""
    cfg = get_config()["rating_curve"]
    span = float(cfg["level_span"]) or 25.0
    try:
        z = (float(bat_rating) - float(cfg["level_anchor"])) / span
    except (TypeError, ValueError):
        return 0.0
    if z != z:                      # NaN
        return 0.0
    return max(-LEVEL_CLAMP, min(LEVEL_CLAMP, z))


def _bounded(x: float, bound: float) -> float:
    if bound <= 1.0:
        return 1.0
    return max(1.0 / bound, min(bound, x))


def phase_map(table: dict, phase: str = None) -> dict:
    """Select a phase row from a ``{phase: {outcome: value}}`` table.

    Tolerates the flat ``{outcome: value}`` form for backward compatibility,
    and never silently substitutes one phase's values for another's: a
    per-phase table with this phase missing falls back to its ``"default"``
    row, or to no correction at all.
    """
    if not table:
        return {}
    if phase and isinstance(table.get(phase), dict):
        return table[phase]
    if any(isinstance(v, dict) for v in table.values()):
        fallback = table.get("default")
        return fallback if isinstance(fallback, dict) else {}
    return table


def _normaliser_for(rc: dict, phase: str = None) -> dict:
    """The divisor set for a phase, falling back to the flat one.

    ``normaliser`` may be either ``{outcome: divisor}`` or
    ``{phase: {outcome: divisor}}``. The per-phase form exists because the
    rating distribution is CORRELATED WITH PHASE: a side's best batters bat
    in the powerplay and its bowlers bat at the death, so the mean match-up
    differs sharply between phases.

    That matters because the baseline is already conditioned on phase, and
    its phase profile was measured from real cricket in which the good
    batters also batted early. A single flat divisor makes the curve
    unbiased overall while leaving it biased *within* each phase — measured
    at +23% on powerplay scoring, which pushed first-innings totals to 198
    against a real figure near 176. Normalising per phase puts the phase
    level back where the baseline says it should be and leaves the curve to
    do the only job it should: separate the good cards from the poor ones
    inside that phase.
    """
    return phase_map(rc.get("normaliser") or {}, phase)


def multipliers(bat_rating: float, bowl_rating: float, cfg: dict = None,
                phase: str = None) -> dict:
    """Per-outcome rating multipliers. Pure; no RNG.

    Returns a dict over :data:`OUTCOMES`, every value finite and > 0.
    ``phase`` selects the measured normaliser set (see
    :func:`_normaliser_for`); omitting it uses the flat set.
    """
    rc = (cfg or get_config())["rating_curve"]
    strength = float(rc.get("strength", 1.0))
    bound = float(rc.get("max_multiplier", 2.75))
    adv = advantage(bat_rating, bowl_rating,
                    anchor=float(rc.get("gap_anchor", 0.0)))
    lvl = batter_level(bat_rating)
    gap_e = rc["gap_elasticity"]
    lvl_e = rc["level_elasticity"]

    out = {}
    for name in OUTCOMES:
        if name == "Extras":
            # Bowler error only. A 95-rated bowler sprays fewer wides.
            e = float(rc.get("extras_bowler_elasticity", -0.35))
            bowl_z = ((float(bowl_rating)
                       - float(rc.get("bowl_level_anchor", rc["level_anchor"])))
                      / (float(rc["level_span"]) or 25.0))
            bowl_z = max(-LEVEL_CLAMP, min(LEVEL_CLAMP, bowl_z))
            z = e * bowl_z * strength
        else:
            z = (float(gap_e.get(name, 0.0)) * adv
                 + float(lvl_e.get(name, 0.0)) * lvl) * strength
        # exp() is bounded afterwards anyway; clamping the exponent first
        # means a hand-edited elasticity cannot raise OverflowError mid-ball.
        out[name] = _bounded(math.exp(max(-50.0, min(50.0, z))), bound)
    # Divide by the measured mean multiplier so the curve redistributes
    # between match-ups without shifting the overall level away from the
    # baseline's measured anchor. See config "normaliser".
    norm = _normaliser_for(rc, phase)
    return {k: v / max(1e-6, float(norm.get(k, 1.0))) for k, v in out.items()}


def clamp_wicket_share(dist: dict, cfg: dict = None,
                       free_hit: bool = False) -> dict:
    """Keep the normalised wicket probability inside its calibrated band.

    Solving ``w / (w + other) = share`` for the wicket weight gives
    ``w = share / (1 - share) * other`` — the same trick the legacy engine's
    safety valve uses (``engine/ball_outcome.py`` §4d), so the two agree.

    On a free hit the floor is skipped: the only legal dismissal there is a
    run out and the caller handles that separately.
    """
    rc = (cfg or get_config())["rating_curve"]
    lo = float(rc.get("min_wicket_share", 0.0))
    hi = float(rc.get("max_wicket_share", 1.0))
    w = float(dist.get("Wicket", 0.0))
    other = sum(float(v) for k, v in dist.items() if k != "Wicket")
    if other <= 0.0:
        return dict(dist)
    out = dict(dist)
    if not free_hit and lo > 0.0:
        floor_w = (lo / (1.0 - lo)) * other
        if w < floor_w:
            w = floor_w
    if hi < 1.0:
        cap_w = (hi / (1.0 - hi)) * other
        if w > cap_w:
            w = cap_w
    out["Wicket"] = w
    return out
