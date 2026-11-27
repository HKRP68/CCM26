"""Simulate the interactive Approach engine using the live bot captain.

Run development arms against training before freezing settings. Pass
--holdout for the post-freeze comparison. The manifest pins the raw counts.
"""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import math
from pathlib import Path
import random
from statistics import mean, pstdev
from time import perf_counter

from engine.realism import odi
from engine.realism.context import chase_bucket
from services import bot_captain, cipl_match as cm
from tools.odi_reference import quantiles

ARMS = ("neutral", "adaptive", "rating_gap", "green", "dead", "tactics", "t20", "hundred")
INTENTS = ("defensive", "rotate", "balanced", "aggressive", "ultra")
PLANS = ("defensive", "balanced", "mixed", "aggressive", "variation")
_SETTINGS = None


def team(prefix, boost):
    ratings = ((82, 28), (78, 31), (80, 27), (76, 25), (74, 36), (71, 73),
               (69, 76), (54, 79), (47, 82), (38, 84), (30, 76))
    styles = ("Right arm Fast", "Off Spinner", "Leg Spinner", "Fast", "Medium-fast", "Fast")
    return [{
        "roster_id": 100 * prefix + i, "player_id": 100 * prefix + i, "name": f"{prefix}-{i + 1}",
        "bat_rating": min(99, a + boost), "bowl_rating": min(99, b + boost),
        "category": ("Wicket Keeper" if i == 1 else "Batsman" if i < 5 else
                     "All-rounder" if i < 7 else "Bowler"),
        "bat_hand": ("Left" if i in (1, 4, 7) else "Right"),
        "bowl_style": styles[max(0, i - 5)], "country": f"Country {i % 4}",
    } for i, (a, b) in enumerate(ratings)]


def match(job):
    global _SETTINGS
    arm, seed, index = job
    rng = random.Random(seed)
    random.seed(seed)
    fmt = "T20" if arm == "t20" else "The100" if arm == "hundred" else "ODI"
    is_odi = fmt == "ODI"
    pitch = {"green": "Green", "dead": "Dead"}.get(arm, "Hard")
    first = 1 if index % 2 == 0 else 2
    if is_odi and _SETTINGS is None:
        _SETTINGS = odi.new_settings()
    strong_boost = 14 if arm == "rating_gap" else 0
    boosts = {1: strong_boost, 2: 0}
    state = cm.build_cipl_state(
        f"{arm}-{seed}", 50 if is_odi else 20, first, 3 - first, first, 3 - first,
        team(first, boosts[first]), team(3 - first, boosts[3 - first]),
        f"Team {first}", f"Team {3 - first}", -1234, pitch_type=pitch, ball_format=fmt,
        odi_settings=_SETTINGS)
    state["rng_seed"] = seed
    state["bot_difficulty"] = "normal"
    if is_odi:
        state["is_bot_match"] = True
    phase_outcomes = {"0": "Dot", "1": "Single", "2": "Double", "3": "Three",
                      "4": "Four", "6": "Six", "W": "Wicket", "WD": "Extras",
                      "NB": "Extras", "LB": "Extras"}
    phases = defaultdict(Counter)
    tactics = Counter()
    chase_bands = set()
    innings, over_times = [], []
    errors = []
    start = perf_counter()
    for number in (1, 2):
        for _ in range(state["overs"] + 1):
            if cm.is_innings_over(state):
                break
            if is_odi:
                choice = bot_captain.pick_bowler(state)
            else:
                choice = next(iter(cm.eligible_bowlers(state)), None)
            if choice is None:
                errors.append("quota exhaustion")
                break
            if is_odi and (choice["roster_id"] == state["prev_bowler_rid"]
                           or not cm.overs_left(state, choice)):
                errors.append("illegal bowler")
                break
            state["current_bowler"] = choice
            if arm == "tactics":
                state["batting_approach"] = INTENTS[(index + state["current_over"] - 1) % 5]
                state["bowling_approach"] = PLANS[(index // 5 + (state["current_over"] - 1) // 2) % 5]
            elif arm in ("neutral", "t20", "hundred"):
                state["batting_approach"] = state["bowling_approach"] = "balanced"
            else:
                state["bowling_approach"] = bot_captain.pick_bowling_approach(state)
                state["batting_approach"] = bot_captain.pick_batting_approach(state)
            tactics.update((f"bat_{state['batting_approach']}", f"bowl_{state['bowling_approach']}"))
            if number == 2 and is_odi:
                chase_bands.add(chase_bucket(cm.chase(state)["rrr"], "ODI"))
            t0 = perf_counter()
            summary = cm.simulate_over(state)
            while summary.get("delivery_paused"):
                summary = cm.simulate_over(state)
            over_times.append(perf_counter() - t0)
            if arm != "neutral":
                bot_captain.observe_over(state, summary)
        if not cm.is_innings_over(state):
            errors.append("innings unfinished")
        balls = cm.balls_bowled(state)
        if balls != sum(p["balls"] for p in state["bowl_stats"].values()):
            errors.append("legal ball mismatch")
        if state["total_runs"] != state["extras_total"] + sum(p["runs"] for p in state["bat_stats"].values()):
            errors.append("scorecard runs mismatch")
        if is_odi and (any(p["balls"] > 60 for p in state["bowl_stats"].values()) or
                       balls > 300 or state["total_wickets"] > 10):
            errors.append("illegal innings")
        batting = [p["runs"] for p in state["bat_stats"].values()]
        innings.append({"runs": state["total_runs"], "balls": balls, "wickets": state["total_wickets"],
                        "extras": state["extras_total"], "centuries": sum(r >= 100 for r in batting),
                        "double_centuries": sum(r >= 200 for r in batting)})
        for row in state["approach_log"]:
            counted = (row["odi_outcomes"] if is_odi else Counter(
                phase_outcomes.get(str(mark), "Extras") for mark in row["timeline"]))
            deliveries = row["deliveries"] if is_odi else len(row["timeline"])
            for phase in (row["phase"], f"{row['phase']}/inn{number}"):
                phases[phase].update(counted)
                phases[phase].update(runs=row["runs"], balls=row["balls"], deliveries=deliveries,
                                     wickets=row["wickets"], overs=int(row["balls"] == cm.balls_per_unit(state)))
                if row["balls"] == cm.balls_per_unit(state):
                    phases[phase][f"over_{row['runs']}"] += 1
        if number == 1:
            cm.end_first_innings(state)
    won = state["total_runs"] >= state["target"]
    tied = state["total_runs"] == state["target"] - 1
    winner = state["bat_team_id"] if won else state["bowl_team_id"]
    return {
        "inn": innings, "phases": dict(phases), "tactics": tactics,
        "chases": {b: "won" if won else "tie" if tied else "lost" for b in chase_bands | {"all"}},
        "strong_win": (not tied and winner == 1), "tie": tied, "errors": errors,
        "state_bytes": len(json.dumps(state, separators=(",", ":"))),
        "over_times": over_times, "duration": perf_counter() - start,
        "signature": [seed, innings, dict(tactics)],
    }


def summarize(matches):
    combined = defaultdict(Counter)
    bands = defaultdict(Counter)
    tactics = Counter()
    errors = Counter()
    for sample in matches:
        for phase, row in sample["phases"].items():
            combined[phase].update(row)
        for band, result in sample["chases"].items():
            bands[band][result] += 1
        tactics.update(sample["tactics"])
        errors.update(sample["errors"])
    phases = {}
    for phase, row in combined.items():
        d, b = row["deliveries"], row["balls"]
        overs = [int(key[5:]) for key, n in row.items() if key.startswith("over_") for _ in range(n)]
        phases[phase] = {"counts": dict(row), "rpo": 6 * row["runs"] / b,
                         "wickets_per_delivery": row["wickets"] / d,
                         "over_run_sd": pstdev(overs) if overs else 0,
                         "shares": {key: row[key] / d for key in odi._OUTCOMES}}
    innings = {}
    for idx in range(2):
        rows = [s["inn"][idx] for s in matches]
        scores = [r["runs"] for r in rows]
        innings[f"inn{idx + 1}"] = {
            "n": len(rows), "mean": mean(scores), "sd": pstdev(scores), "quantiles": quantiles(scores),
            "350_plus": sum(r >= 350 for r in scores), "400_plus": sum(r >= 400 for r in scores),
            "wickets": mean(r["wickets"] for r in rows),
            "early_endings": sum(r["balls"] < 300 for r in rows),
            **{field: sum(r[field] for r in rows) for field in ("extras", "centuries", "double_centuries")},
        }
    return {"matches": len(matches), "errors": dict(errors), "phases": phases, "innings": innings,
            "ties": sum(s["tie"] for s in matches), "chases": dict(bands), "tactics": dict(tactics),
            "team_one_win_rate": mean(s["strong_win"] for s in matches),
            "max_state_bytes": max(s["state_bytes"] for s in matches),
            "mean_over_ms": 1000 * mean(t for s in matches for t in s["over_times"]),
            "p95_over_ms": quantiles([1000 * t for s in matches for t in s["over_times"]])["p95"],
            "reproducibility_sha256": hashlib.sha256(json.dumps(
                [s["signature"] for s in matches], sort_keys=True).encode()).hexdigest()}


def compare(result, reference):
    def interval(p, n):
        centre = (p + 1.96 ** 2 / (2 * n)) / (1 + 1.96 ** 2 / n)
        margin = 1.96 * math.sqrt((p * (1 - p) + 1.96 ** 2 / (4 * n)) / n) / (1 + 1.96 ** 2 / n)
        return [max(0, centre - margin), min(1, centre + margin)]
    phase_results = {}
    for phase in ("powerplay", "middle", "death"):
        actual, real = result["phases"][phase], reference["metrics"][phase]
        delta = {k: actual["shares"][k] - real.get(k, 0) / real["deliveries"] for k in odi._OUTCOMES}
        phase_results[phase] = {
            "run_rate_error": actual["rpo"] - real["runs_per_over"],
            "share_errors": delta,
            "rare_share_binomial_ci": {
                key: {"simulated": interval(actual["shares"][key], actual["counts"]["deliveries"]),
                      "empirical": interval(real.get(key, 0) / real["deliveries"], real["deliveries"])}
                for key in ("Three", "Six", "Wicket", "Extras")},
            "within_tolerance": (abs(actual["rpo"] - real["runs_per_over"]) <= 0.5
                                 and all(abs(v) <= 0.03 for v in delta.values()))}
    means = {}
    for key, row in result["innings"].items():
        real = reference["innings"][key]
        error = (row["mean"] - real["mean"]) / real["mean"]
        means[key] = {"relative_error": error, "within_tolerance": abs(error) <= 0.10,
                      "empirical_mean_se": real["sd"] / math.sqrt(real["n"]),
                      "simulated_mean_se": row["sd"] / math.sqrt(row["n"])}
    return {"phases": phase_results, "innings": means,
            "passed": all(x["within_tolerance"] for x in [*phase_results.values(), *means.values()])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=261923)
    parser.add_argument("--legacy-n", type=int, default=120)
    parser.add_argument("--arm", action="append", choices=ARMS)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--development", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    cfg = odi.new_settings()
    folder = Path("data/simulation_reference") / cfg["reference_version"]
    split = "holdout" if args.holdout else "development" if args.development else "training"
    sample_file = folder / f"odi_{split}_report.json"
    raw = sample_file.read_bytes()
    data = json.loads(raw)
    report = {
        "seed": args.seed, "n_requested_per_arm": args.n,
        "n_requested_per_short_format_arm": args.legacy_n,
        "config_sha256": hashlib.sha256(odi._SETTINGS.read_bytes()).hexdigest(),
        "reference_sha256": cfg["reference_sha256"], "comparison_data_sha256": hashlib.sha256(raw).hexdigest(),
        "comparison": "2023 holdout" if args.holdout else f"2019 {split}",
        "processes": args.jobs, "arms": {},
    }
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for arm in args.arm or ARMS:
            started = perf_counter()
            n = args.legacy_n if arm in ("t20", "hundred") else args.n
            samples = list(pool.map(match, ((arm, args.seed + i, i) for i in range(n)), chunksize=4))
            result = summarize(samples)
            result["wall_seconds"] = perf_counter() - started
            if arm in ("neutral", "adaptive"):
                result["comparison"] = compare(result, data)
            report["arms"][arm] = result
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
            print(arm, result["wall_seconds"], result["errors"],
                  {k: round(x["mean"], 1) for k, x in result["innings"].items()}, flush=True)


if __name__ == "__main__":
    main()
