"""Realism engine configuration — flags in the environment, tuning in JSON.

Two layers, matching what the repository already does elsewhere:

* **Feature flags** come from environment variables, the way ``config.py``
  reads ``RICH_TEXT`` and friends. These are the switches an operator flips
  without a deploy:

      SIM_REALISM_ENABLED          "0" (default) | "1"
      SIM_REALISM_SHADOW_MODE      "0" (default) | "1"
      SIM_REALISM_REFERENCE_VERSION "v1" (default)
      SIM_REALISM_FORMATS          comma list, default "T20"
      SIM_REALISM_BASELINE         competition/era key, default "" (auto)

* **Tuning numbers** come from ``config/realism.json``, the way the
  conditions engine reads ``config/sim_engine.json`` — with an optional
  ``config/realism.local.json`` overlay for local experiments, which is the
  same convention ``engine/sim/config.py`` uses.

The default is OFF. A deployment that does nothing keeps the existing engine,
ball for ball. That is deliberate: see §10 of the brief and the rollback
section of ``docs/realism_engine_v1.md``.
"""

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(_HERE, "config", "realism.json")
LOCAL_CONFIG_PATH = os.path.join(_HERE, "config", "realism.local.json")
REFERENCE_ROOT = os.path.join(_HERE, "data", "simulation_reference")

_TRUTHY = ("1", "true", "yes", "on")
_lock = threading.Lock()
_cache = {}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def realism_enabled() -> bool:
    """Is the realism model allowed to decide live outcomes?"""
    return _env_flag("SIM_REALISM_ENABLED", False)


def shadow_mode_enabled() -> bool:
    """Compute the realism distribution but keep the legacy outcome.

    Shadow mode never consumes a random number and never changes a match.
    """
    return _env_flag("SIM_REALISM_SHADOW_MODE", False)


def reference_version() -> str:
    return (os.getenv("SIM_REALISM_REFERENCE_VERSION") or "v1").strip() or "v1"


def enabled_formats() -> frozenset:
    """Formats the realism model is allowed to drive.

    Defaults to T20 only. ODI/ListA reference data ships, but its provenance
    is weaker (no measured in-repo anchor), so it stays opt-in — a mode is
    never quietly switched over. See §10 of the brief.
    """
    raw = os.getenv("SIM_REALISM_FORMATS")
    if raw is None:
        return frozenset(("T20", "The100"))
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def baseline_override() -> str:
    """Force a competition/era baseline key, or "" to use the file default."""
    return (os.getenv("SIM_REALISM_BASELINE") or "").strip()


# --------------------------------------------------------------------------
# Tuning file
# --------------------------------------------------------------------------
DEFAULTS = {
    "rating_curve": {
        # tanh((gap - gap_anchor) / scale). 17.0 matches
        # engine/rating_duel.GAP_SCALE so the two models agree about what
        # "a big rating gap" means.
        "scale": 17.0,
        # Global dial on how much ratings matter. 1.0 is the calibrated value.
        "strength": 1.0,
        # Per-outcome elasticity to the rating gap (log-space slope).
        "gap_elasticity": {
            "Dot": -0.30, "Single": 0.02, "Double": 0.10, "Three": 0.05,
            "Four": 0.42, "Six": 0.55, "Wicket": -0.38, "Extras": 0.0,
        },
        # Per-outcome elasticity to the batter's absolute class, independent
        # of the gap: a 40-rated batter at parity still cannot hit sixes.
        "level_elasticity": {
            "Dot": -0.10, "Single": 0.04, "Double": 0.08, "Three": 0.04,
            "Four": 0.20, "Six": 0.30, "Wicket": -0.08, "Extras": 0.0,
        },
        # WHERE THE CURVE IS NEUTRAL, measured rather than assumed.
        #
        # The reference baseline describes real professional T20. The question
        # "which CCM26 match-up is that?" has a measured answer: over simulated
        # CIPL innings the ball-weighted mean is bat_rating 62.1 against
        # bowl_rating 73.5, a mean gap of -11.3. (Bowlers out-rate batters
        # because every XI bats its bowlers but no XI bowls its batters.)
        #
        # Anchoring at 75/75 instead — the obvious-looking choice — put the
        # curve's origin two-thirds of a standard deviation away from where
        # the data lives. The normaliser then had to divide the wicket
        # multiplier by 1.47 to pull the level back, which over-rewarded high
        # ratings: a 90-rated batter facing 75-rated bowling was dismissed
        # once every 67 balls against a real elite figure near 29.
        #
        # Re-measure with: python -m tools.compare_realism --calibrate-curve
        "gap_anchor": -11.33,
        "level_anchor": 62.14,
        "level_span": 25.0,
        # Extras are a bowler-error outcome, not a contest: scaled by the
        # bowler's class alone, against the measured mean BOWLING rating
        # (73.5) rather than the batting anchor.
        "extras_bowler_elasticity": -0.35,
        "bowl_level_anchor": 73.47,
        # Hard bounds on any single rating multiplier.
        "max_multiplier": 2.75,
        # A premium batter is never un-dismissable; and no delivery is a
        # coin-flip. The cap is a safety valve, not a shaper: if it binds on
        # more than a few percent of balls the curve needs re-normalising
        # (see `normaliser` below), not a higher cap.
        "min_wicket_share": 0.012,
        "max_wicket_share": 0.140,
        # MEASURED per-outcome normaliser, so the curve is unbiased ON AVERAGE
        # over the match-ups that actually occur, not just at the anchor.
        #
        # exp() is convex, so by Jensen's inequality the mean of
        # exp(k * advantage) over a spread of match-ups exceeds
        # exp(k * mean advantage) = 1 even when the mean advantage is zero.
        # A real XI is full of #9-#11 batters facing good bowlers, so the
        # uncorrected curve lifted the innings wicket rate from the 5.3%
        # measured anchor to 6.4% before a single downstream layer ran.
        #
        # It is keyed BY PHASE because the rating distribution is correlated
        # with phase — a side's best batters bat in the powerplay and its
        # bowlers bat at the death — while the baseline is already
        # conditioned on phase. A single flat divisor left the curve unbiased
        # overall but 23% high on powerplay scoring.
        #
        # The defaults below are the identity. The real, measured values are
        # in config/realism.json; re-measure with
        #     python -m tools.compare_realism --calibrate-curve --n 120
        #
        # The structure must stay dict-of-dicts here so the deep merge with
        # config/realism.json produces a clean per-phase map rather than a
        # mixture of phase rows and stray per-outcome floats. A flat
        # {outcome: divisor} map is still honoured at runtime for backward
        # compatibility — see rating_curve._normaliser_for.
        "normaliser": {
            phase: {"Dot": 1.0, "Single": 1.0, "Double": 1.0, "Three": 1.0,
                    "Four": 1.0, "Six": 1.0, "Wicket": 1.0, "Extras": 1.0}
            for phase in ("default", "powerplay", "middle", "death")
        },
    },
    "conditions": {
        # Each condition family may move an outcome by at most this factor,
        # and all of them together by at most `total_bound`.
        "bound": 1.60,
        "total_bound": 2.20,
        # Strength dials per family (0 disables that family cleanly).
        "pitch_strength": 1.0,
        "weather_strength": 1.0,
        "tactics_strength": 1.0,
        "fatigue_strength": 1.0,
        "pressure_strength": 1.0,
        "milestone_strength": 1.0,
        # The realism approach model (engine/realism/approaches.py) replaces
        # the legacy approach tables in realism mode. 0 disables it cleanly
        # and falls back to a neutral tactics effect.
        "approach_strength": 1.0,
        "approach_bound": 2.60,
        # The milestone family gets a wider bound than the condition families.
        # `bound` exists so that no single CONDITION can overwhelm the
        # baseline; the milestone family is not a condition, it is a
        # mean-neutral redistribution of a fixed amount of dismissal risk
        # across the innings. Its top band is a batter past ninety, and
        # clipping that to 1.60 defeats the purpose — the band exists
        # precisely to stop such an innings running away.
        "milestone_bound": 2.50,
        "chase_strength": 1.0,
        "style_strength": 1.0,
    },
    "smoothing": {
        # alpha per hierarchy level: how many parent-prior observations a
        # child node must beat before it is trusted on its own.
        "alpha": {
            "competition": 250.0,
            "phase": 400.0,
            "innings": 600.0,
            "wickets": 900.0,
            "chase": 1200.0,
        },
    },
    "integration": {
        # In realism mode the legacy rating/phase layers are bypassed because
        # the baseline already carries phase shape and the rating curve
        # already carries rating influence. Flip these on only to study the
        # double-count.
        "keep_legacy_phase_boosts": False,
        "keep_legacy_rating_duel": False,
        "keep_legacy_wicket_realism_mult": False,
        # Layers that carry genuinely separate information stay on.
        "apply_game_state": True,
        "apply_pressure_effects": True,
        "apply_approach_modifiers": True,
        "apply_weight_hook": True,
        # Which of engine/approach_modifiers' layers still run in realism
        # mode. An ALLOWLIST, not a denylist, so a layer added there later is
        # off here until someone decides it does not double-count.
        #
        # Everything left out is now modelled better elsewhere: the base
        # approach tables and the counter matrix by
        # engine/realism/approaches.py (as a priced trade-off rather than a
        # hand-set grid), the pitch and phase rules by the baseline's own
        # pitch family and phase hierarchy level, momentum by the game-state
        # engine, fatigue by conditions.fatigue_multipliers, and the
        # read/out-think mind game by the counters.
        #
        # What survives models something this engine does not: how readable a
        # captain has become over several overs, and how settled a batting
        # pair is.
        "legacy_approach_layers": ["predictability", "chemistry"],
        # Result-steering and score-convergence hooks are off in realism
        # mode (brief §11). Named individually so a deployment can see
        # exactly what it is turning back on.
        "drama_hooks": {
            "score_ceiling": False,
            "contest": False,
            "pitch_character": False,
            "clutch_finale": False,
            "wicket_mixup": False,
        },
        # Hidden per-player ability derived from a player's identity rather
        # than their card (engine/approach_modifiers.batting_matchup_delta's
        # stable_unit fallback). Off in realism mode: two cards with the same
        # ratings and styles must be the same player (brief §9).
        "identity_derived_ability": False,
        # MEASURED compensation for the net effect of those kept layers.
        #
        # The baseline is an innings average, so it already contains the
        # average of everything the momentum engine, the pressure engine and
        # the approach system react to. Those layers are kept because they
        # carry per-ball information the baseline cannot (what the last 18
        # deliveries did, what the captain just chose), but their *net* effect
        # over an innings is not 1.0 — they were calibrated against a
        # different, lower base. Left uncorrected the engine produced 8.4
        # wickets an innings against a 6.3 anchor.
        #
        # Keyed BY PHASE, for the same reason the rating-curve normaliser is:
        # those layers do not inflate every phase equally. The momentum
        # engine, the pressure engine and aggressive captaincy all bite
        # hardest at the death, and a single global factor measured 12.8
        # runs an over there against a baseline of 11.0 while the powerplay
        # was already correct.
        #
        # These factors are measured, not guessed:
        #     python -m tools.compare_realism --calibrate-gain --n 120
        # prints the residual per phase and the recommended values.
        # Re-measure after any change to the kept layers or the reference
        # data. A flat {outcome: factor} map is still honoured for backward
        # compatibility.
        "downstream_gain": {
            phase: {"Dot": 1.0, "Single": 1.0, "Double": 1.0, "Three": 1.0,
                    "Four": 1.0, "Six": 1.0, "Wicket": 1.0, "Extras": 1.0}
            for phase in ("default", "powerplay", "middle", "death")
        },
    },
    "fallback": {
        # If the reference is missing/invalid, revert to the legacy engine.
        "legacy_on_missing_reference": True,
        "log_once": True,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("realism: could not read %s; ignoring it", path)
        return {}


def get_config(reload: bool = False) -> dict:
    """Merged tuning config: DEFAULTS < realism.json < realism.local.json."""
    with _lock:
        if reload or "cfg" not in _cache:
            cfg = _deep_merge(DEFAULTS, _read_json(CONFIG_PATH))
            cfg = _deep_merge(cfg, _read_json(LOCAL_CONFIG_PATH))
            _cache["cfg"] = cfg
        return _cache["cfg"]


def clear_cache():
    """Drop the cached tuning config (used by tests)."""
    with _lock:
        _cache.clear()
