"""The realism outcome model: context in, normalised distribution out.

This is the single function the brief (§5) asks for. It takes a
:class:`~engine.realism.context.BallContext` and returns a probability
distribution over the eight outcomes the existing engine already understands —
``Dot Single Double Three Four Six Wicket Extras`` — so no scorecard, state
machine or match-state contract changes.

Order of operations, once each:

    1. real-data baseline for (format, competition/era, phase, innings,
       wickets[, chase band]) with hierarchical smoothing   [baselines]
    2. rating effect from bat_rating vs bowl_rating          [rating_curve]
    3. bounded condition families, each applied once        [conditions]
       and, when the captains have actually chosen, the approach
       trade-off priced against this baseline                [approaches]
    4. the measured downstream gain, which compensates for the legacy layers
       realism mode deliberately keeps (see config "downstream_gain")
    5. normalise, clamp the wicket share, re-normalise

Guarantees, all asserted by ``tests/test_realism_outcomes.py``:

    * every probability is finite and >= 0
    * the probabilities sum to 1 within 1e-9
    * only outcomes the engine supports appear
    * the wicket share is never 0 (except on a free hit, where the caller
      restricts the dismissal to a run out) and never above its cap

No RNG is drawn anywhere in this module. That is what makes shadow mode cost
nothing and change nothing: the live match keeps drawing from the same random
stream in the same order whether or not the realism distribution was computed
alongside it.
"""

import logging

from engine.realism import approaches, baselines, conditions, rating_curve
from engine.realism.config import get_config

logger = logging.getLogger(__name__)

OUTCOMES = baselines.OUTCOMES
_TOL = 1e-9


class RealismUnavailable(Exception):
    """No usable reference for this context — caller must use the legacy engine."""


def _normalise(weights: dict) -> dict:
    total = 0.0
    clean = {}
    for o in OUTCOMES:
        v = float(weights.get(o, 0.0))
        if v != v or v in (float("inf"), float("-inf")) or v < 0.0:
            v = 0.0
        clean[o] = v
        total += v
    if total <= 0.0:
        raise RealismUnavailable("all outcome weights collapsed to zero")
    return {o: v / total for o, v in clean.items()}


def build(ctx, cfg: dict = None, reference=None,
          apply_tactics: bool = True, explain: bool = False) -> dict:
    """Return the normalised realism distribution for *ctx*.

    Raises :class:`RealismUnavailable` when no reference file can serve this
    context; the caller is expected to fall back to the legacy engine.

    ``apply_tactics`` is False when the caller will run CCM26's own
    ``engine.approach_modifiers`` on the result — the approach system and the
    tactics family model the same decision, and applying both is the double
    count §5 of the brief rules out.
    """
    cfg = cfg or get_config()
    doc = reference if reference is not None else baselines.load(
        format_name=_reference_format(ctx.format_name))
    if not doc:
        raise RealismUnavailable("no reference for format %r" % ctx.format_name)
    if "_smoothed" not in doc:
        # Official revisions serialize the reference without this runtime cache.
        doc = {**doc, "_smoothed": {}}

    comp = baselines.competition_key(doc, ctx.competition)
    path = ctx.node_path(comp)
    base, node_key, meta = baselines.lookup(doc, path)

    # The phase is passed so the curve can use its per-phase normaliser: the
    # rating distribution is correlated with phase and the baseline already
    # carries the phase level.
    ratings = rating_curve.multipliers(ctx.bat_rating, ctx.bowl_rating, cfg,
                                       phase=ctx.phase)

    c = cfg["conditions"]
    families = [
        conditions.pitch_multipliers(ctx.pitch, ctx.pitch_wear,
                                     ctx.bowling_type, cfg),
        conditions.weather_multipliers(ctx.dew, ctx.is_day_night, ctx.innings,
                                       ctx.phase, cfg),
        conditions.fatigue_multipliers(ctx.bowler_fatigue, cfg),
        conditions.wicket_pressure_multipliers(ctx.wickets, ctx.balls_faced, cfg),
        conditions.milestone_multipliers(ctx.batter_runs, cfg),
        conditions.chase_multipliers(ctx.required_rate, ctx.par_rate,
                                     ctx.balls_remaining, ctx.wickets, cfg),
        conditions.style_multipliers(ctx.matchup, cfg),
    ]
    approach_mult = None
    if apply_tactics:
        if ctx.batting_approach is None and ctx.bowling_approach is None:
            # No captaincy to model: the coarse intent family stands in.
            families.append(conditions.tactics_multipliers(
                ctx.batting_approach, ctx.bowling_approach, cfg))
        else:
            # Real approach picks. engine/realism/approaches prices them as a
            # trade-off against THIS baseline, which is what makes the same
            # intent mean different things in the powerplay and at the death
            # without a phase table. Applied outside `combine` because it has
            # its own wider bound: an approach is a decision, not a condition,
            # and Ultra Attack genuinely doubles the six rate.
            approach_mult = approaches.multipliers(
                ctx.batting_approach, ctx.bowling_approach, base,
                bowler_rating=ctx.bowl_rating, cfg=cfg)
    cond = conditions.combine(families, float(c["total_bound"]))
    if approach_mult:
        cond = {o: cond[o] * approach_mult.get(o, 1.0) for o in OUTCOMES}

    gain = rating_curve.phase_map(
        (cfg.get("integration") or {}).get("downstream_gain") or {}, ctx.phase)
    weights = {o: base[o] * ratings[o] * cond[o] * float(gain.get(o, 1.0))
               for o in OUTCOMES}
    dist = _normalise(weights)
    dist = _normalise(rating_curve.clamp_wicket_share(dist, cfg, ctx.free_hit))

    if explain:
        return {
            "dist": dist,
            "baseline": base,
            "ratings": ratings,
            "conditions": cond,
            "approach": approach_mult,
            "gain": {o: float(gain.get(o, 1.0)) for o in OUTCOMES},
            "node": node_key,
            "node_meta": meta,
            "competition": comp,
            "model_version": doc.get("model_version"),
            "phase": ctx.phase,
        }
    return dist


def _reference_format(format_name: str) -> str:
    """Which reference file serves a CCM26 format.

    The Hundred is a 100-ball T20 in everything the ball model cares about —
    the phases are shorter but the cricket is the same — and
    :attr:`BallContext.phase` already scales to the innings length, so it
    reads the T20 reference. ListA reads the ODI reference. Test cricket has
    no v1 reference at all and therefore never leaves the legacy engine.
    """
    f = str(format_name or "T20")
    if f in ("T20", "The100"):
        return "T20"
    if f in ("ODI", "ListA"):
        return "ODI"
    return f


def available_for(format_name: str) -> bool:
    """Is there a usable reference for this format right now?"""
    return baselines.load(format_name=_reference_format(format_name)) is not None


def expected_runs(dist: dict) -> float:
    """Expected runs off the bat for a distribution (extras excluded)."""
    return (dist.get("Single", 0.0) + 2 * dist.get("Double", 0.0)
            + 3 * dist.get("Three", 0.0) + 4 * dist.get("Four", 0.0)
            + 6 * dist.get("Six", 0.0))


def summarise(dist: dict) -> dict:
    """Readable aggregates, used by the shadow recorder and audit tools."""
    return {
        "exp_runs": expected_runs(dist),
        "dot": dist.get("Dot", 0.0),
        "boundary": dist.get("Four", 0.0) + dist.get("Six", 0.0),
        "wicket": dist.get("Wicket", 0.0),
        "extras": dist.get("Extras", 0.0),
    }
