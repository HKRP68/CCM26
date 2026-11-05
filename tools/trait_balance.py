"""Audit traits through complete, equal-rated, headless LetsPlay matches.

    python -m tools.trait_balance --n 200 --output /tmp/trait-balance.json
    python -m tools.trait_balance --pitches Even --levels 4 5 --n 100

Team A carries the selected loadout and team B has no traits. Each seed is
played twice, with A batting first and second. Singles use five equipped
traits per team; ``both`` uses ten. Ratings, approaches and seeds stay fixed.
Reports include 95% intervals across seed pairs, activation rates and neutral
controls. They measure impact rather than asserting that every trait must
produce exactly the same win rate.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
import json
import math
from pathlib import Path
import random
import statistics
from unittest.mock import patch

from services import cipl_match as cm
from services import config_service, trait_engine
from tools.pitch_calibration import _muted_logging, make_xi


PITCHES = ("Flat", "Even", "Green")
LEVELS = (1, 3, 4, 5)
PROFILES = ("none", "batting", "bowling", "both", "bat_anchor", "bat_finisher",
            "bowl_wicket_hunter", "bowl_death")
BATTING_LOADOUT = ("bat_power_hitter", "bat_anchor", "bat_power_hitter",
                   "bat_anchor", "bat_finisher")
BOWLING_LOADOUT = ("bowl_wicket_hunter", "bowl_death", "bowl_dot_specialist",
                   "bowl_wicket_hunter", "bowl_death")
SEED = 20261105


def equip(xi, profile, level):
    """Equip equal-sized loadouts; trait levels do not change player ratings."""
    batting = (BATTING_LOADOUT if profile in ("batting", "both") else
               (profile,) * 5 if profile.startswith("bat_") else ())
    bowling = (BOWLING_LOADOUT if profile in ("bowling", "both") else
               (profile,) * 5 if profile.startswith("bowl_") else ())
    for players, keys in ((xi[:5], batting), (xi[-5:], bowling)):
        for player, key in zip(players, keys):
            player.setdefault("traits", []).append(
                {"effect_key": key, "level": level, "display_name": key})


@contextmanager
def _activation_counter():
    original = trait_engine.apply_traits
    available, active = Counter(), Counter()

    def tracked(probs, striker_traits, bowler_traits, ctx):
        # Harness loadouts have one trait per card, so a returned label has a
        # unique owner on each delivery. Count wrong-role activations too: the
        # audit should expose them rather than silently excluding them.
        equipped = {}
        for role, traits in (("batter", striker_traits), ("bowler", bowler_traits)):
            for trait in traits or ():
                key = trait["effect_key"]
                available[(key, role)] += 1
                equipped[f"{key} Lv.{trait['level']}"] = (key, role)
        activated = original(probs, striker_traits, bowler_traits, ctx)
        for label in activated:
            active[equipped[label]] += 1
        return activated

    with patch.object(trait_engine, "apply_traits", tracked):
        yield available, active


def _innings(state):
    for _ in range(21):
        if cm.is_innings_over(state):
            return
        # Rotate the front-line attack so five specialists finish with four
        # overs each. Always choosing the highest-rated bowler spends their
        # quotas early and can force a batsman to bowl the final over, hiding
        # whether Death Specialist is useful with a sensible bowling plan.
        state["current_bowler"] = min(
            cm.eligible_bowlers(state),
            key=lambda p: (cm._overs_bowled(state, p["roster_id"]),
                           -p.get("bowl_rating", 0)))
        state["batting_approach"] = "balanced"
        state["bowling_approach"] = "balanced"
        cm.simulate_over(state)
    raise RuntimeError("trait audit innings did not terminate")


def simulate_one(pitch, profile, level, seed, a_bats_first):
    """Run the production over loop, innings change and result calculation."""
    random.seed(seed)
    a, b = make_xi("A", 1), make_xi("B", 101)
    equip(a, profile, level)
    first, second = (a, b) if a_bats_first else (b, a)
    first_name, second_name = ("A", "B") if a_bats_first else ("B", "A")
    state = cm.build_cipl_state(
        1, 20, 10, 20, 111, 222, first, second,
        first_name, second_name, -1, pitch, False)
    # Personal-roster mode uses the calibrated chase controller, bypassing
    # CIPL's scripted finale. This is the same flag LetsPlay sets.
    state["clutch_finale"] = True
    with _activation_counter() as (available, active):
        _innings(state)
        innings1 = (state["total_runs"], state["total_wickets"])
        cm.end_first_innings(state)
        _innings(state)
        innings2 = (state["total_runs"], state["total_wickets"])
    result = cm.compute_result(state)
    focal, opponent = (innings1, innings2) if a_bats_first else (innings2, innings1)
    return {
        "points": 0.5 if result["tie"] else float(result["winner"] == "A"),
        "runs": focal[0], "wickets": focal[1],
        "opponent_runs": opponent[0], "opponent_wickets": opponent[1],
        "tie": result["tie"], "available": available, "active": active,
    }


def mean_interval(values):
    """Mean and a normal 95% interval, using seed pairs as sampling units."""
    mean = statistics.fmean(values)
    error = (1.96 * statistics.stdev(values) / math.sqrt(len(values))
             if len(values) > 1 else None)
    return mean, [mean - error, mean + error] if error is not None else None


def summarize(rows, baseline):
    paired_points = [statistics.fmean(r["points"] for r in rows[i:i + 2])
                     for i in range(0, len(rows), 2)]
    base_points = [statistics.fmean(r["points"] for r in baseline[i:i + 2])
                   for i in range(0, len(baseline), 2)]
    mean, interval = mean_interval(paired_points)
    edge, edge_interval = mean_interval([a - b for a, b in zip(paired_points, base_points)])
    available, active = Counter(), Counter()
    for row in rows:
        available.update(row["available"])
        active.update(row["active"])
    return {
        "matches": len(rows), "seed_pairs": len(paired_points),
        "win_share_pct": 100 * mean,
        "win_share_95_pct": ([100 * max(0, interval[0]),
                              100 * min(1, interval[1])] if interval else None),
        "edge_vs_neutral_pp": 100 * edge,
        "edge_vs_neutral_95_pp": [100 * v for v in edge_interval] if edge_interval else None,
        "mean_runs": statistics.fmean(r["runs"] for r in rows),
        "mean_wickets": statistics.fmean(r["wickets"] for r in rows),
        "opponent_mean_runs": statistics.fmean(r["opponent_runs"] for r in rows),
        "opponent_mean_wickets": statistics.fmean(r["opponent_wickets"] for r in rows),
        "ties": sum(r["tie"] for r in rows),
        "activation": {
            f"{key}:{role}": {"opportunities": count, "activated": active[(key, role)],
                              "pct": 100 * active[(key, role)] / count}
            for (key, role), count in sorted(available.items())},
    }


def collect(pitches=PITCHES, levels=LEVELS, profiles=PROFILES, n=200,
            seed=SEED, progress=None):
    if n < 2 or n % 2:
        raise ValueError("n must be an even number of matches, at least 2")
    if not pitches or any(p not in ("Flat", "Hard", "Even", "Bouncy", "Dry", "Green", "Dusty", "Dead")
                          for p in pitches):
        raise ValueError("choose supported pitches")
    if not levels or any(level not in range(1, 6) for level in levels):
        raise ValueError("levels must be between 1 and 5")
    if not profiles or any(p not in PROFILES for p in profiles):
        raise ValueError("choose supported loadout profiles")
    rng_state = random.getstate()
    report = {"seed": seed, "matches_per_row": n, "pitches": list(pitches),
              "levels": list(levels), "profiles": list(profiles),
              "conditions": "defaults; balanced approaches; rotating regular bowlers; commentary disabled",
              "simulated_matches": len(pitches) * n *
                  (1 + len(levels) * sum(profile != "none" for profile in profiles)),
              "rows": []}
    try:
        with (_muted_logging(),
              patch.object(config_service, "get_config", return_value=dict(config_service.DEFAULTS)),
              patch.object(cm, "_COMMENTARY", None)):
            for pitch in pitches:
                # Neutral controls have no trait level, so run them once.
                baseline = [simulate_one(pitch, "none", 1, seed + i // 2, i % 2 == 0)
                            for i in range(n)]
                for level in levels:
                    for profile in profiles:
                        rows = (baseline if profile == "none" else
                                [simulate_one(pitch, profile, level, seed + i // 2, i % 2 == 0)
                                 for i in range(n)])
                        summary = {"pitch": pitch, "level": level, "profile": profile,
                                   **summarize(rows, baseline)}
                        report["rows"].append(summary)
                        if progress:
                            progress(summary)
    finally:
        random.setstate(rng_state)
    return report


def write_csv(report, path):
    """Export compact, reviewable measurements alongside the full JSON report."""
    fields = ["pitch", "level", "profile", "matches", "seed_pairs", "win_share_pct",
              "win_95_low_pct", "win_95_high_pct", "edge_vs_neutral_pp",
              "edge_95_low_pp", "edge_95_high_pp", "mean_runs", "mean_wickets",
              "opponent_mean_runs", "opponent_mean_wickets", "ties",
              "correct_role_opportunities", "correct_role_activations", "wrong_role_activations"]
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in report["rows"]:
            entry = {key: row[key] for key in fields if key in row}
            for prefix, values in (("win", row["win_share_95_pct"]),
                                   ("edge", row["edge_vs_neutral_95_pp"])):
                suffix = "pct" if prefix == "win" else "pp"
                for i, bound in enumerate(("low", "high")):
                    entry[f"{prefix}_95_{bound}_{suffix}"] = values[i] if values else ""
            opportunities = activations = wrong = 0
            for key_role, counts in row["activation"].items():
                key, role = key_role.split(":")
                if trait_engine.trait_allowed(key, role):
                    opportunities += counts["opportunities"]
                    activations += counts["activated"]
                else:
                    wrong += counts["activated"]
            entry.update(correct_role_opportunities=opportunities,
                         correct_role_activations=activations, wrong_role_activations=wrong)
            writer.writerow({key: round(value, 4) if isinstance(value, float) else value
                             for key, value in entry.items()})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=200, help="even number of matches per row")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--pitches", nargs="+", default=list(PITCHES))
    parser.add_argument("--levels", nargs="+", type=int, default=list(LEVELS))
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=list(PROFILES))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--csv", type=Path, help="also write a compact CSV report")
    args = parser.parse_args(argv)
    print("pitch level profile                 win%   edge pp   runs  opp runs", flush=True)

    def progress(row):
        print(f"{row['pitch']:<6} {row['level']:>3} {row['profile']:<23} "
              f"{row['win_share_pct']:>5.1f} {row['edge_vs_neutral_pp']:>9.1f} "
              f"{row['mean_runs']:>6.1f} {row['opponent_mean_runs']:>9.1f}", flush=True)

    try:
        report = collect(args.pitches, args.levels, args.profiles, args.n, args.seed, progress)
    except ValueError as exc:
        parser.error(str(exc))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.csv:
        write_csv(report, args.csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
