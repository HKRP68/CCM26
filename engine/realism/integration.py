"""The seam between the realism model and ``engine.ball_outcome``.

``calculate_outcome`` is the single funnel every realism-pilot mode goes
through — /cipl (``services/cipl_match.py``), /sim (``services/sim_match.py``)
and /letsplay all call it — so wiring the model in here reaches all of them
without touching a command, a scorecard or a state machine. This module keeps
that wiring in one readable place; ``ball_outcome`` only asks it three
questions.

Three modes:

``off``     the legacy engine, untouched, ball for ball. The default.
``shadow``  legacy decides the match; the realism distribution is computed
            alongside and recorded. No extra random numbers are drawn, so the
            match is bit-identical to an ``off`` run from the same seed.
``live``    the realism distribution *is* the ball's distribution.

In ``live`` mode some legacy layers are bypassed, because leaving them on
would apply the same influence twice — which §5 of the brief rules out:

  bypassed  the hand-set pitch scoring matrix (replaced by the fitted
            baseline), the powerplay/death/second-innings-death phase boosts
            and death surge (phase is the baseline's own hierarchy level), the
            T20 wicket-realism and duel-recalibration multipliers (the wicket
            rate is now a measured anchor), the legacy pitch-wear model
            (``conditions.pitch_multipliers`` carries wear), and the
            per-ball rating duel (``rating_curve`` carries rating influence).

  kept      the game-state momentum engine, pressure effects, approach
            modifiers, the /letsplay trait hook, free-hit handling, the
            fielding drop/misfield mechanics, wicket-type and extras-type
            selection, and the normalised-wicket safety valve.

Everything kept is information the realism model does not itself model;
everything bypassed is information it now models better. Each of those
choices is a flag in ``config/realism.json`` so the double-count can be
measured rather than argued about.
"""

import logging

from engine.realism import config as rcfg
from engine.realism import shadow
from engine.realism.context import from_engine_call
from engine.realism.outcome_distribution import (
    RealismUnavailable, _reference_format, build,
)

logger = logging.getLogger(__name__)

OFF, SHADOW, LIVE = "off", "shadow", "live"
_logged = set()


def _log_once(key, level, msg, *args):
    if key in _logged:
        return
    _logged.add(key)
    getattr(logger, level)(msg, *args)


def reset_log_state():
    _logged.clear()


def mode_for(format_config=None, cfg: dict = None) -> str:
    """Which mode applies to this delivery.

    ``shadow`` wins over ``live`` when both flags are set: measuring is the
    safer interpretation of an ambiguous configuration, and a half-rolled-out
    deployment should never silently switch matches over.
    """
    shadow_on = rcfg.shadow_mode_enabled()
    live_on = rcfg.realism_enabled()
    if not (shadow_on or live_on):
        return OFF
    fmt = getattr(format_config, "name", None) or "T20"
    if fmt not in rcfg.enabled_formats():
        _log_once("fmt:%s" % fmt, "info",
                  "realism: format %s is not in SIM_REALISM_FORMATS; using the "
                  "legacy engine", fmt)
        return OFF
    return SHADOW if shadow_on else LIVE


def try_build(mode, *, batter, bowler, pitch, over_number, innings,
              batter_runs, balls_faced, batting_position, format_config,
              game_state, pitch_wear, is_day_night, batting_approach,
              bowling_approach, approach_context, free_hit,
              bat_rating_effective=None, bowl_rating_effective=None,
              match_context=None, apply_tactics=True):
    """Build the realism distribution, or return ``None`` to use legacy.

    Never raises. A missing reference, a malformed context or a bug in the
    model all resolve the same way: log once, return ``None``, and the
    delivery is decided by the engine that has always decided it. This is the
    §11 requirement that a bad reference fails safely to the original engine
    rather than silently substituting invented probabilities.
    """
    if mode == OFF:
        return None
    cfg = rcfg.get_config()
    try:
        ctx = from_engine_call(
            batter, bowler, pitch=pitch, over_number=over_number,
            innings=innings, batter_runs=batter_runs, balls_faced=balls_faced,
            batting_position=batting_position, format_config=format_config,
            game_state=game_state, pitch_wear=pitch_wear,
            is_day_night=is_day_night, batting_approach=batting_approach,
            bowling_approach=bowling_approach,
            approach_context=approach_context, free_hit=free_hit,
            bat_rating_effective=bat_rating_effective,
            bowl_rating_effective=bowl_rating_effective,
            match_context=match_context)
        explained = build(ctx, cfg, apply_tactics=apply_tactics, explain=True)
    except RealismUnavailable as exc:
        if not cfg["fallback"].get("legacy_on_missing_reference", True):
            raise
        _log_once("unavailable:%s" % exc, "warning",
                  "realism: unavailable (%s); using the legacy engine", exc)
        return None
    except Exception:
        _log_once("build-error", "exception",
                  "realism: distribution build failed; using the legacy engine")
        return None
    explained["ctx"] = ctx
    return explained


def record_shadow(legacy_weights: dict, explained: dict):
    """Record a shadow-mode divergence. Swallows every error."""
    try:
        shadow.record(legacy_weights, explained["dist"],
                      explained.get("phase"))
    except Exception:
        _log_once("shadow-error", "exception",
                  "realism: shadow recording failed; the match is unaffected")


def bypass(name: str, cfg: dict = None) -> bool:
    """Is legacy layer *name* bypassed in live mode?

    ``name`` is one of ``phase_boosts``, ``rating_duel``,
    ``wicket_realism_mult``, ``pitch_wear``.
    """
    integ = (cfg or rcfg.get_config())["integration"]
    keep = {
        "phase_boosts": integ.get("keep_legacy_phase_boosts", False),
        "rating_duel": integ.get("keep_legacy_rating_duel", False),
        "wicket_realism_mult": integ.get("keep_legacy_wicket_realism_mult", False),
        # Wear is part of conditions.pitch_multipliers in realism mode.
        "pitch_wear": False,
    }.get(name, False)
    return not keep


def supported_formats():
    """Formats that have a reference AND are switched on, for reporting."""
    from engine.realism.outcome_distribution import available_for
    out = {}
    for fmt in ("T20", "The100", "ODI", "ListA", "Test"):
        out[fmt] = {
            "reference": _reference_format(fmt),
            "reference_available": available_for(fmt),
            "enabled": fmt in rcfg.enabled_formats(),
        }
    return out
