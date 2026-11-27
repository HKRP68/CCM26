"""Measured outcome model for interactive men's ODI Approach matches."""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path

from engine.realism import baselines
from engine.realism.approaches import COUNTERS

_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS = _ROOT / "config/odi_approach.json"
_RUNS = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6}
_OUTCOMES = tuple(_RUNS) + ("Wicket", "Extras")
_ROTATION = ("Single", "Double", "Three")
_BOUNDARIES = ("Four", "Six")
_EXTRA_TYPES = {"wides": "Wide", "noballs": "No Ball", "byes": "Byes",
                "legbyes": "Leg Bye", "penalty": "Penalty"}


class ODIUnavailable(ValueError):
    """An ODI cannot proceed without its pinned settings and observed data."""


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


@lru_cache(maxsize=8)
def _reference(version, digest):
    try:
        path = Path(baselines.reference_path(version, "ODI"))
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("reference hash differs")
        doc = baselines.validate(json.loads(raw))
        comp = doc["default_competition"]
        if (doc["format"] != "ODI" or not doc["provenance"].get("all_nodes_observed")
                or doc["provenance"].get("mode") != "cricsheet"
                or any(n["count_kind"] != "observed"
                       or any(type(v) is not int or v < 0 for v in n["counts"].values())
                       for n in doc["nodes"].values())
                or any(f"ODI/{comp}/{phase}" not in doc["nodes"]
                       for phase in ("powerplay", "middle", "death"))
                or any(not doc["extras_variants"].get(phase)
                       for phase in ("powerplay", "middle", "death"))):
            raise ValueError("observed ODI phase coverage is missing")
        doc = baselines._prepare(doc)
        doc["_alpha"] = {"competition": 0, "phase": 60, "innings": 120,
                         "wickets": 220, "chase": 1450}
        return doc
    except (OSError, KeyError, TypeError, ValueError, baselines.ReferenceError) as exc:
        raise ODIUnavailable("ODI measured reference is unavailable. Resume after restoring it.") from exc


def validate_reference(cfg):
    return _reference(cfg["reference_version"], cfg["reference_sha256"])


def _validate_settings(cfg):
    batting = {"defensive", "rotate", "balanced", "aggressive", "ultra"}
    bowling = {"defensive", "balanced", "mixed", "variation", "aggressive"}
    phases = {"powerplay", "middle", "death"}
    def valid_rows(name, keys, width, lo, hi):
        rows = cfg[name]
        return set(rows) == keys and all(
            len(row) == width and all(lo <= n <= hi for n in row)
            for row in rows.values())
    if (cfg["version"] != "odi-approach-1"
            or not valid_rows("batting", batting, 3, 0.4, 1.8)
            or not valid_rows("bowling", bowling, 3, 0.4, 1.8)
            or not valid_rows("phase_gains", phases, 2, 0.5, 1.5)
            or not valid_rows("innings_gains", {"inn1", "inn2"}, 2, 0.5, 1.5)
            or not valid_rows("pitch", {"Hard", "Green", "Dry", "Flat", "Dead"}, 2, 0.5, 1.5)
            or not valid_rows("fatigue", {"pace", "spin"}, 3, 0, 7.9)
            or set(cfg["phase_rates"]) != phases or set(cfg["boundary_bias"]) != phases
            or any(not 2 <= n <= 18 for n in cfg["phase_rates"].values())
            or any(not 0.5 <= n <= 1.5 for n in cfg["boundary_bias"].values())
            or not 0 <= cfg["counter_strength"] <= 1
            or not 1 <= cfg["repeat_grace"] < 30
            or len(cfg["run_out_completed_runs"]) != 3
            or any(not 0 <= w <= 100 for w in cfg["run_out_completed_runs"])
            or not sum(cfg["run_out_completed_runs"])):
        raise ValueError("invalid ODI approach settings")
    rating = cfg["rating"]
    if (not 1 <= rating["scale"] <= 100 or not -30 <= rating["gap_anchor"] <= 30
            or any(not 0 <= rating[key] <= 2 for key in ("runs", "wickets", "level"))):
        raise ValueError("invalid ODI rating settings")


def new_settings():
    """Read and verify the settings before committing a new match."""
    try:
        cfg = json.loads(_SETTINGS.read_bytes())
        _validate_settings(cfg)
        version = cfg["reference_version"]
        path = Path(baselines.manifest_path(version))
        entry = json.loads(path.read_bytes())["formats"]["ODI"]
        cfg["reference_sha256"] = entry["sha256"]
        validate_reference(cfg)
        return cfg
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ODIUnavailable("ODI tuning or its measured reference is unavailable.") from exc


def enabled():
    return os.getenv("CMU_ODI_APPROACH_ENABLED", "1").strip().lower() in {"1", "yes", "true", "on"}


def settings(state):
    cfg = state.get("odi_settings")
    if (not isinstance(cfg, dict) or cfg.get("version") != "odi-approach-1"
            or not cfg.get("reference_version") or not cfg.get("reference_sha256")):
        raise ODIUnavailable("This ODI is missing its saved engine settings.")
    return cfg


@dataclass(frozen=True)
class Workload:
    fatigue: float
    spell_overs: int
    legal_balls: int
    last_over: int
    rest_overs: int
    penalty: float

    @property
    def label(self):
        return "Tired" if self.penalty >= 0.50 else "Working" if self.penalty > 0 else "Fresh"


def workload(history, innings, over, rid, legal_balls, spin, cfg):
    effort, recovery, onset = cfg["fatigue"]["spin" if spin else "pace"]
    fatigue = last = spell = 0
    for item in history:
        unit = item["over"]
        if (item.get("innings") != innings or str(item.get("bowler_rid")) != str(rid)
                or unit >= over or item.get("balls", 0) < 6):
            continue
        fatigue = _clamp(fatigue - recovery * max(0, unit - last - 1) + effort, 0, 8)
        spell = spell + 1 if last and unit - last == 2 else 1
        last = unit
    rest = over - last - 1 if last else over - 1
    fatigue = _clamp(fatigue - rest * recovery, 0, 8)
    return Workload(fatigue, spell if rest <= 1 else 0, legal_balls, last, rest,
                    _clamp((fatigue - onset) / (8 - onset), 0, 1))


def _normalise(weights):
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


def _rate_shape(base, runs, boundary, wicket, extras):
    """Allocate rotation and boundary run mass, with dots the residual.

    The scoring groups retain their conditional shapes. A common feasibility
    factor preserves their ratio in an extreme context where dots run out.
    """
    spread = {k: base[k] for k in (*_ROTATION, *_BOUNDARIES)}
    for group, target in ((_ROTATION, runs - boundary), (_BOUNDARIES, boundary)):
        current = sum(base[k] * _RUNS[k] for k in group)
        scale = max(0, target) / max(1e-9, current)
        for k in group:
            spread[k] *= scale
    scoring = sum(spread.values())
    mass = 1 - wicket - extras
    factor = min(1.0, (mass - 0.015) / max(scoring, 1e-9))
    return {"Dot": mass - scoring * factor, **{k: v * factor for k, v in spread.items()},
            "Wicket": wicket, "Extras": extras}


def run_expectancy(dist):
    return sum(value * dist[key] for key, value in _RUNS.items())


def boundary_expectancy(dist):
    return sum(_RUNS[k] * dist[k] for k in _BOUNDARIES)


def baseline(ctx, cfg):
    doc = _reference(cfg["reference_version"], cfg["reference_sha256"])
    comp = doc["default_competition"]
    base, node, meta = baselines.lookup(doc, ctx.node_path(comp))
    rating = cfg["rating"]
    edge = math.tanh((ctx.bat_rating - ctx.bowl_rating - rating["gap_anchor"]) / rating["scale"])
    level = _clamp((ctx.bat_rating - 68) / 30, -1, 1)
    pitch_runs, pitch_wickets = cfg["pitch"].get(ctx.pitch, cfg["pitch"]["Hard"])
    gain_runs, gain_wickets = cfg["phase_gains"][ctx.phase]
    inn_runs, inn_wickets = cfg["innings_gains"][ctx.innings_key]
    x = ctx.extra
    spin = ctx.bowling_family == "spin"
    # Fresh pace can move the new ball on a green surface. Dry wear gives spin
    # its opportunity. Dew is modelled here, once, using the live conditions.
    movement = (0.08 * (1 - ctx.over / 10) * (1.6 if ctx.pitch == "Green" else 1)
                if ctx.phase == "powerplay" and not spin else 0.0)
    grip = 0.10 * ctx.pitch_wear if ctx.pitch == "Dry" and spin else 0.0
    settle = 0.09 * (min(ctx.balls_faced, 55) / 55 - 0.45)
    fatigue = x["workload"].penalty
    fielding = _clamp(((x["fielding"] or 65) - 65) / 35, -1, 1)
    c = x["chemistry"]
    pressure = x["pressure"]
    momentum = _clamp(x["momentum"] / 100, -1, 1)
    runs = (run_expectancy(base) * pitch_runs * gain_runs * inn_runs
            * math.exp(rating["runs"] * edge + rating["level"] * level
                       + settle + 0.05 * c + 0.025 * momentum + 0.03 * pressure
                       + 0.04 * ctx.dew - 0.50 * movement - 0.024 * fielding))
    wicket = base["Wicket"] * pitch_wickets * gain_wickets * inn_wickets * math.exp(
        -rating["wickets"] * edge - 0.65 * settle - 0.04 * c - 0.025 * momentum
        + movement + grip - 0.05 * ctx.dew + 0.05 * pressure + 0.035 * fielding)
    extras = _clamp(base["Extras"] * (1 + (74 - ctx.bowl_rating) * 0.004 + ctx.dew * 0.07),
                    0.005, 0.18)
    share = _clamp(cfg["boundary_bias"][ctx.phase] * boundary_expectancy(base) / run_expectancy(base)
                   + 0.05 * edge + 0.04 * level - 0.03 * c - 0.3 * movement, 0.15, 0.8)
    shaped = _rate_shape(base, runs, runs * share, _clamp(wicket, 0.005, 0.16), extras)
    if fatigue:
        shaped["Wicket"] *= 1 - 0.12 * fatigue
        shaped["Dot"] *= 1 - 0.06 * fatigue
        for key in ("Single", "Double", "Four", "Six"):
            shaped[key] *= 1 + 0.08 * fatigue
        shaped = _normalise(shaped)
    return shaped, node, meta


def approach_distribution(base, ctx, cfg, bat=None, bowl=None, trait_hook=None):
    bat = bat or "balanced"
    bowl = bowl or "balanced"
    bat_runs, bat_bound, bat_wkt = cfg["batting"][bat]
    bowl_runs, bowl_bound, bowl_wkt = cfg["bowling"][bowl]
    counter = COUNTERS.get((bat, bowl), {})
    strength = cfg["counter_strength"]
    counter_runs = 1 + (counter.get("runs", 1) - 1) * strength
    counter_wkt = 1 + (counter.get("wicket", 1) - 1) * strength
    boundary_bias = 1 + (counter.get("boundary_share", 1) - 1) * strength
    x = ctx.extra
    grace = cfg["repeat_grace"]
    read_bat = _clamp((x["bat_repeat"] + 1 - grace) / 3, 0, 1) if x["last_bat"] == bat else 0
    read_bowl = _clamp((x["bowl_repeat"] + 1 - grace) / 3, 0, 1) if x["last_bowl"] == bowl else 0
    phase_push = 1.03 if ctx.phase == "death" and ctx.wickets <= 5 else 1.0
    if bat not in ("aggressive", "ultra"):
        phase_push = 1.0
    carry = x["carry"]
    run_factor = _clamp(bat_runs * bowl_runs * counter_runs * phase_push
                        * (1 - 0.035 * read_bat + 0.035 * read_bowl + carry), 0.70, 1.40)
    wkt_factor = _clamp(bat_wkt * bowl_wkt * counter_wkt
                        * (1 + 0.05 * read_bat - 0.035 * read_bowl), 0.60, 1.80)
    scoring = run_expectancy(base) * run_factor
    boundary_share = _clamp(boundary_expectancy(base) / run_expectancy(base)
                            * bat_bound * bowl_bound * boundary_bias, 0.12, 0.85)
    weights = _rate_shape(base, scoring, scoring * boundary_share,
                          min(0.19, base["Wicket"] * wkt_factor), base["Extras"])
    if trait_hook is not None:
        weights = trait_hook(weights) or weights
    if ctx.free_hit:
        weights["Wicket"] *= 0.10
        weights["Four"] *= 1.08
        weights["Six"] *= 1.08
    return _normalise(weights)


@lru_cache(maxsize=64)
def extra_profile(version, digest, phase, free_hit):
    doc = _reference(version, digest)
    variants = doc["extras_variants"][phase]
    if free_hit:
        variants = [row for row in variants if row[4] in ("", "run out", "obstructing the field",
                                                         "hit the ball twice")]
    total = sum(row[-1] for row in variants)
    return (tuple(variants), sum((r[1] + r[2]) * r[-1] for r in variants) / total,
            sum(r[-1] for r in variants if r[0] in ("wides", "noballs")) / total,
            sum(r[-1] for r in variants if r[4]) / total)


def expectations(dist, ctx, cfg):
    _, extra_runs, illegal, joint_wicket = extra_profile(
        cfg["reference_version"], cfg["reference_sha256"], ctx.phase, ctx.free_hit)
    legal = 1 - dist["Extras"] * illegal
    return ((run_expectancy(dist) + dist["Extras"] * extra_runs + dist["Wicket"] * 0.055) / legal,
            (dist["Wicket"] + dist["Extras"] * joint_wicket) / legal)


def sample(dist, ctx, cfg, rng, node, meta, bat=None):
    from engine.ball_outcome import _get_wicket_type_by_bowling

    chosen = rng.choices(_OUTCOMES, [dist[k] for k in _OUTCOMES])[0]
    result = {"type": "run", "runs": _RUNS.get(chosen, 0), "batter_out": False,
              "is_extra": False, "odi_outcome": chosen,
              "realism": {"node": node, "phase": ctx.phase, "count_kind": meta["count_kind"],
                          "n": meta["n"], "version": cfg["version"]}}
    if chosen == "Extras":
        variants, *_ = extra_profile(cfg["reference_version"], cfg["reference_sha256"],
                                     ctx.phase, ctx.free_hit)
        kind, penalty, bat_runs, byes, dismissal, non_striker, _ = rng.choices(
            variants, [row[-1] for row in variants])[0]
        result.update(type="extra", is_extra=True, extra_type=_EXTRA_TYPES[kind],
                      runs=bat_runs if kind == "noballs" else penalty,
                      odi_extras=penalty, odi_bat_runs=bat_runs, odi_byes=byes)
        if dismissal:
            result.update(batter_out=True, wicket_type=dismissal.title(),
                          run_out_non_striker=bool(non_striker))
    elif chosen == "Wicket":
        kinds, weights = _get_wicket_type_by_bowling(
            ctx.bowling_type, ctx.phase, bat, run_out_scale=0.75)
        kind = "Run Out" if ctx.free_hit else rng.choices(kinds, weights)[0]
        result.update(type="wicket", batter_out=True, wicket_type=kind)
        if kind == "Run Out":
            result.update(runs=rng.choices((0, 1, 2), cfg["run_out_completed_runs"])[0],
                          run_out_non_striker=rng.random() < 0.35,
                          run_out_crossed=rng.random() < 0.5)
    return result
