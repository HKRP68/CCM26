"""Ground-conditions loader (adapted from SimCricketX engine/ground_config.py).

Reads data/ground_conditions.yaml for pitch metadata — descriptions, run
factors, ideal toss choice and phase boosts — used by the auto-sim (/sim) to
make pitches feel distinct beyond the raw outcome table in probability_engine.

PyYAML is optional: if it (or the file) is unavailable, a built-in default
profile set is used so the bot keeps working before a redeploy.
"""

import logging
import os

logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "ground_conditions.yaml")

# Built-in fallback (mirrors the key numbers in ground_conditions.yaml). Run
# factor: <1 bowling-friendly, >1 batting-friendly. ideal_toss derives from it.
_FALLBACK = {
    "Green": {"description": "Seamer-friendly surface — pace dominates, low scoring",
              "run_factor": 0.85, "ideal_toss": "bowl", "favours": "Pace"},
    "Dry":   {"description": "Spin-friendly surface — spinners thrive, tough to score",
              "run_factor": 0.85, "ideal_toss": "bat", "favours": "Spin"},
    "Hard":  {"description": "True bounce with carry — balanced, slight batting edge",
              "run_factor": 1.10, "ideal_toss": "bat", "favours": "Balanced"},
    "Flat":  {"description": "Even bounce — batting paradise, bowlers need skill",
              "run_factor": 1.08, "ideal_toss": "bowl", "favours": "Batting"},
    "Dead":  {"description": "Lifeless track — batting festival guaranteed",
              "run_factor": 1.30, "ideal_toss": "bowl", "favours": "Batting"},
}

# Pitch-correct toss choice (from SimCricketX format_config.correct_toss_choice).
# "bat" = bat first; "bowl" = bowl first.
_IDEAL_TOSS = {"Green": "bowl", "Dry": "bat", "Hard": "bat",
               "Flat": "bowl", "Dead": "bowl"}

_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache
    profiles = {}
    try:
        import yaml  # optional dependency
        with open(_DATA_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        for name, prof in (raw.get("pitch_profiles") or {}).items():
            rf = float(prof.get("run_factor", 1.0))
            profiles[name] = {
                "description": prof.get("description", ""),
                "run_factor": rf,
                "ideal_toss": _IDEAL_TOSS.get(name, "bat" if rf <= 1.0 else "bowl"),
                "favours": _favours_from(prof.get("wicket_factors") or {}),
            }
        if raw.get("phase_boosts"):
            profiles["__phase_boosts__"] = raw["phase_boosts"]
    except Exception as e:
        logger.info("ground_conditions.yaml unavailable (%s); using fallback", e)

    # Merge fallback for any pitch the YAML didn't define.
    for name, prof in _FALLBACK.items():
        profiles.setdefault(name, prof)
    _cache = profiles
    return _cache


def _favours_from(wicket_factors):
    """Heuristic 'favours' label from the wicket-factor map."""
    if not wicket_factors:
        return "Balanced"
    pace = max((v for k, v in wicket_factors.items()
                if any(t in k.lower() for t in ("fast", "medium"))), default=0)
    spin = max((v for k, v in wicket_factors.items()
                if "spin" in k.lower()), default=0)
    if pace and pace >= spin and pace > 1.05:
        return "Pace"
    if spin and spin > 1.05:
        return "Spin"
    return "Balanced"


def list_pitches():
    """Pitch names usable by the sim (those the outcome engine also supports)."""
    from services.probability_engine import PITCH_MODS
    return [p for p in _load() if not p.startswith("__") and p in PITCH_MODS]


def get_pitch_meta(name):
    """Return {description, run_factor, ideal_toss, favours} for a pitch."""
    return _load().get(name) or _FALLBACK.get(name) or _FALLBACK["Hard"]
