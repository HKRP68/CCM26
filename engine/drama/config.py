"""Drama preset policy and configuration loading (Codex Prompt 05, §4 & §21).

Three presets live in ``config/drama_presets.json``. This module loads them,
applies the environment switches, validates/clamps the numbers so an invalid
file can never crash a live match, and resolves the preset that a new match is
stamped with. The chosen preset is persisted in match metadata at creation
(see the match loops), so flipping a global setting cannot alter an ongoing
match.

Environment switches
--------------------
``SIM_DRAMA_PRESET``   REALISTIC (default) | BALANCED | CINEMATIC — which policy
                       a new match is created under.
``SIM_DRAMA_ENABLED``  "1"/"0" — master override for the selected preset's own
                       ``enabled`` flag. Unset leaves the preset's file value,
                       which defaults to false everywhere, so a deployment that
                       sets nothing keeps the legacy (realism-only) behaviour.

Nothing here reads a player attribute or touches data/players.json.
"""

import json
import os
import threading
import copy

logger_name = __name__

_TRUTHY = {"1", "true", "yes", "on"}
PRESET_NAMES = ("REALISTIC", "BALANCED", "CINEMATIC")

_lock = threading.Lock()
_cache = {}

# Embedded fallback — a byte-for-byte mirror of config/drama_presets.json, used
# only if the file is missing or unparseable so the engine still has a policy.
# ln(1.15) = 0.1397619...; ln(1.25) = 0.2231435...
_LN_115 = 0.139762
_LN_125 = 0.223144

DEFAULTS = {
    "default_preset": "REALISTIC",
    "presets": {
        "REALISTIC": {
            "preset": "REALISTIC", "enabled": False,
            "scripted_outcomes_allowed": False, "forced_finish_allowed": False,
            "tension": {"enabled": True, "max_score": 100},
            "momentum": {"enabled": False, "window_legal_balls": 12,
                         "ema_previous_weight": 0.80, "max_absolute_state": 0.75},
            "clutch": {"enabled": False, "max_activations_per_side_per_innings": 1,
                       "max_duration_legal_balls": 6, "max_activation_probability": 0.12},
            "rare_volatility": {"enabled": False, "probability_per_eligible_over": 0.015,
                                "max_activations_per_innings": 1},
            "effect_limits": {"min_log_multiplier": -_LN_115, "max_log_multiplier": _LN_115,
                              "max_absolute_wicket_probability_delta": 0.012,
                              "max_absolute_expected_runs_delta": 0.08},
            "highlights": {"enabled": True},
        },
        "BALANCED": {
            "preset": "BALANCED", "enabled": False,
            "scripted_outcomes_allowed": False, "forced_finish_allowed": False,
            "tension": {"enabled": True, "max_score": 100},
            "momentum": {"enabled": True, "window_legal_balls": 12,
                         "ema_previous_weight": 0.80, "max_absolute_state": 0.75},
            "clutch": {"enabled": True, "max_activations_per_side_per_innings": 1,
                       "max_duration_legal_balls": 6, "max_activation_probability": 0.12},
            "rare_volatility": {"enabled": True, "probability_per_eligible_over": 0.015,
                                "max_activations_per_innings": 1},
            "effect_limits": {"min_log_multiplier": -_LN_115, "max_log_multiplier": _LN_115,
                              "max_absolute_wicket_probability_delta": 0.012,
                              "max_absolute_expected_runs_delta": 0.08},
            "highlights": {"enabled": True},
        },
        "CINEMATIC": {
            "preset": "CINEMATIC", "enabled": False,
            "scripted_outcomes_allowed": True, "forced_finish_allowed": True,
            "tension": {"enabled": True, "max_score": 100},
            "momentum": {"enabled": True, "window_legal_balls": 12,
                         "ema_previous_weight": 0.80, "max_absolute_state": 0.85},
            "clutch": {"enabled": True, "max_activations_per_side_per_innings": 2,
                       "max_duration_legal_balls": 6, "max_activation_probability": 0.20},
            "rare_volatility": {"enabled": True, "probability_per_eligible_over": 0.030,
                                "max_activations_per_innings": 2},
            "effect_limits": {"min_log_multiplier": -_LN_125, "max_log_multiplier": _LN_125,
                              "max_absolute_wicket_probability_delta": 0.020,
                              "max_absolute_expected_runs_delta": 0.14},
            "highlights": {"enabled": True},
        },
    },
}


def _config_path():
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(here, "config", "drama_presets.json")


def _env_flag(name, default=None):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _clampf(v, lo, hi, default):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:                       # NaN
        return default
    return max(lo, min(hi, x))


def _clampi(v, lo, hi, default):
    try:
        x = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, x))


def _validate_preset(name, raw):
    """Merge a raw preset over the embedded default and clamp every number to a
    safe range, so an edited or corrupt file can never crash a live match."""
    base = copy.deepcopy(DEFAULTS["presets"].get(name, DEFAULTS["presets"]["BALANCED"]))
    if not isinstance(raw, dict):
        return base
    for section, val in raw.items():
        if isinstance(val, dict) and isinstance(base.get(section), dict):
            base[section].update(val)
        else:
            base[section] = val

    base["preset"] = name
    for k in ("enabled", "scripted_outcomes_allowed", "forced_finish_allowed"):
        base[k] = bool(base.get(k, False))

    t = base["tension"]
    t["enabled"] = bool(t.get("enabled", True))
    t["max_score"] = _clampi(t.get("max_score", 100), 1, 100, 100)

    m = base["momentum"]
    m["enabled"] = bool(m.get("enabled", False))
    m["window_legal_balls"] = _clampi(m.get("window_legal_balls", 12), 1, 60, 12)
    m["ema_previous_weight"] = _clampf(m.get("ema_previous_weight", 0.80), 0.0, 0.999, 0.80)
    m["max_absolute_state"] = _clampf(m.get("max_absolute_state", 0.75), 0.0, 1.0, 0.75)

    c = base["clutch"]
    c["enabled"] = bool(c.get("enabled", False))
    c["max_activations_per_side_per_innings"] = _clampi(
        c.get("max_activations_per_side_per_innings", 1), 0, 10, 1)
    c["max_duration_legal_balls"] = _clampi(c.get("max_duration_legal_balls", 6), 1, 36, 6)
    c["max_activation_probability"] = _clampf(
        c.get("max_activation_probability", 0.12), 0.0, 1.0, 0.12)

    r = base["rare_volatility"]
    r["enabled"] = bool(r.get("enabled", False))
    r["probability_per_eligible_over"] = _clampf(
        r.get("probability_per_eligible_over", 0.015), 0.0, 1.0, 0.015)
    r["max_activations_per_innings"] = _clampi(
        r.get("max_activations_per_innings", 1), 0, 10, 1)

    e = base["effect_limits"]
    # The log-multiplier window must be symmetric and positive on the high side.
    hi = _clampf(e.get("max_log_multiplier", _LN_115), 0.0, 1.0, _LN_115)
    lo = _clampf(e.get("min_log_multiplier", -_LN_115), -1.0, 0.0, -_LN_115)
    e["max_log_multiplier"] = hi
    e["min_log_multiplier"] = lo
    e["max_absolute_wicket_probability_delta"] = _clampf(
        e.get("max_absolute_wicket_probability_delta", 0.012), 0.0, 1.0, 0.012)
    e["max_absolute_expected_runs_delta"] = _clampf(
        e.get("max_absolute_expected_runs_delta", 0.08), 0.0, 6.0, 0.08)

    base["highlights"]["enabled"] = bool(base["highlights"].get("enabled", True))
    return base


def _load(reload=False):
    with _lock:
        if not reload and "data" in _cache:
            return _cache["data"]
        raw = None
        try:
            with open(_config_path(), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = None
        presets_raw = (raw or {}).get("presets", {}) if isinstance(raw, dict) else {}
        default_preset = (raw or {}).get("default_preset", "REALISTIC") \
            if isinstance(raw, dict) else "REALISTIC"
        if default_preset not in PRESET_NAMES:
            default_preset = "REALISTIC"
        data = {
            "default_preset": default_preset,
            "presets": {name: _validate_preset(name, presets_raw.get(name, {}))
                        for name in PRESET_NAMES},
        }
        _cache["data"] = data
        return data


def clear_cache():
    with _lock:
        _cache.pop("data", None)


def default_preset():
    return _load()["default_preset"]


def active_preset():
    """The preset a NEW match is created under (env override, else file default)."""
    raw = os.getenv("SIM_DRAMA_PRESET")
    if raw:
        name = raw.strip().upper()
        if name in PRESET_NAMES:
            return name
    return default_preset()


def preset_config(name=None, reload=False):
    """The validated config dict for a preset, with the master-enable override
    applied. ``name`` defaults to :func:`active_preset`."""
    data = _load(reload=reload)
    if name is None:
        name = active_preset()
    name = str(name).upper()
    if name not in data["presets"]:
        name = "REALISTIC"
    cfg = copy.deepcopy(data["presets"][name])
    master = _env_flag("SIM_DRAMA_ENABLED", None)
    if master is not None:
        cfg["enabled"] = bool(master)
    return cfg


def overlay_active(cfg):
    """Does this preset config actually apply a drama overlay to outcomes?"""
    return bool(cfg.get("enabled"))


def scripted_outcomes_allowed(cfg):
    """May this preset use the ScenarioEngine's scripted finales / overrides?"""
    return bool(cfg.get("scripted_outcomes_allowed"))
