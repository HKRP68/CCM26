"""Load and validate ``config/sim_engine.json``.

The shipped JSON is the single source of every default. Local tuning goes in
``config/sim_engine.local.json`` (or the file named by ``SIM_ENGINE_CONFIG``),
holding only the keys being changed; it is deep-merged over the defaults.

Validation never raises: out-of-range values are clamped and recorded in
``validation_warnings()`` so a typo in a balancing pass degrades gracefully
instead of taking the bot down.
"""

import copy
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(_ROOT, "config", "sim_engine.json")
LOCAL_PATH = os.path.join(_ROOT, "config", "sim_engine.local.json")

PITCH_KEYS = ("paceHelp", "seamHelp", "swingHelp", "spinTurn", "bounce",
              "battingEase", "paceWicketChance", "spinWicketChance", "crackRate")
PITCH_MIN, PITCH_MAX = 0.1, 3.0

_lock = threading.Lock()
_cache = None
_warnings = []


def deep_merge(base, override):
    """Return a new dict: ``override`` merged into ``base`` recursively."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def validate(cfg):
    """Clamp *cfg* in place into legal ranges. Returns the list of warnings."""
    warns = []

    def fix(container, key, lo, hi, label):
        v = container.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return
        c = _clamp(v, lo, hi)
        if c != v:
            warns.append(f"{label}={v} clamped to {c}")
            container[key] = c

    for name, prof in (cfg.get("pitches") or {}).items():
        for k in PITCH_KEYS:
            if k not in prof:
                warns.append(f"pitches.{name}.{k} missing, using 1.0")
                prof[k] = 1.0
            fix(prof, k, PITCH_MIN, PITCH_MAX, f"pitches.{name}.{k}")
    drama = cfg.get("drama") or {}
    fix(drama, "dramaSlider", 0, 100, "drama.dramaSlider")
    meta = cfg.get("meta") or {}
    fix(meta, "live_strength", 0.0, 1.0, "meta.live_strength")
    rain = (cfg.get("weather") or {}).get("rain") or {}
    fix(rain, "basePerOverProb", 0.0, 0.05, "weather.rain.basePerOverProb")
    for fmt, base in ((cfg.get("outcome") or {}).get("base") or {}).items():
        for k in list(base):
            fix(base, k, 0.0, 1.0, f"outcome.base.{fmt}.{k}")
    return warns


def build(override=None, path=None, local_path=None, use_store=False):
    """Build a validated config:
    defaults ← local file ← admin overrides (``use_store``) ← ``override`` dict.

    Pure apart from reading the files (and the database when ``use_store``);
    used by :func:`get_config` and by tests that want a tweaked config.
    """
    cfg = _read(path or DEFAULT_PATH)
    lp = local_path if local_path is not None else os.environ.get("SIM_ENGINE_CONFIG", LOCAL_PATH)
    if lp and os.path.exists(lp):
        try:
            cfg = deep_merge(cfg, _read(lp))
        except Exception as exc:  # a broken local file must not stop the bot
            logger.warning("sim_engine: ignoring unreadable %s: %s", lp, exc)
    if use_store:
        from engine.sim import store
        saved = store.current("modifiers")
        if isinstance(saved, dict):
            cfg = deep_merge(cfg, saved)
    if override:
        cfg = deep_merge(cfg, override)
    warns = validate(cfg)
    cfg["_warnings"] = warns
    return cfg


def get_config():
    """The process-wide config (cached). Call :func:`reload` after editing."""
    global _cache, _warnings
    if _cache is None:
        with _lock:
            if _cache is None:
                try:
                    _cache = build(use_store=True)
                except Exception as exc:
                    logger.error("sim_engine: config failed to load: %s", exc)
                    _cache = {"_warnings": [str(exc)], "_broken": True}
                _warnings = list(_cache.get("_warnings", []))
                for w in _warnings:
                    logger.warning("sim_engine config: %s", w)
    return _cache


def reload():
    """Drop the cache so the next :func:`get_config` re-reads the files."""
    global _cache
    with _lock:
        _cache = None
    return get_config()


def validation_warnings():
    get_config()
    return list(_warnings)


def section(cfg, *path, default=None):
    """``cfg[path[0]][path[1]]...`` with a default instead of KeyError."""
    node = cfg
    for p in path:
        if not isinstance(node, dict) or p not in node:
            return default
        node = node[p]
    return node
