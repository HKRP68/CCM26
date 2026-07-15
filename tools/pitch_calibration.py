"""Pitch Rule Engine calibration harness.

Runs headless Monte-Carlo CIPL matches per pitch and checks that the simulation's
real output matches the Pitch Rule Engine spec encoded in
``config/ground_conditions.yaml``:

  * First-innings distribution — Floor (P10) / Par (median) / Ceiling (P90) /
    Anomaly (max) per pitch.
  * Sub-100 Rule — a side all-out under 100 is very rare (<~2%).
  * Chase Win % by first-innings total band.
  * Fighting Match Rule — chases stay close and deep: tight median defended
    margin, low capitulation rate, most matches reaching the closing overs.

This is the tuning instrument for the constants in ``services/cipl_match.py``
(variance sigma, floor guard, corridor, BASELINE_STRENGTH) and the YAML targets.

Usage:
    python -m tools.pitch_calibration                 # all pitches, n=300
    python -m tools.pitch_calibration --n 500
    python -m tools.pitch_calibration --pitch Flat --n 800 --verbose

Exit code is non-zero if any pitch is out of tolerance, so it can gate a tuning
pass in CI or a pre-commit hook.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import random
import statistics
import sys

from engine import ground_config
from services import cipl_match as cm


@contextlib.contextmanager
def _muted_logging():
    """Silence engine logging for the duration of a calibration run only.

    Importing this module must NOT disable logging process-wide (the test suite
    imports it), so suppression is scoped here and the previous disable level is
    restored afterwards — even if the run raises.
    """
    prev = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(prev)

# Spec pitches (in the spec's order); Dead is engine-extra, calibrated loosely.
SPEC_PITCHES = ["Flat", "Hard", "Even", "Bouncy", "Dry", "Green", "Dusty"]

# Both XIs are identical in quality so neither side is favoured — the chase win %
# then reflects the PITCH (target defended) rather than a team mismatch. Ratings
# are tuned so the neutral "Even" pitch lands mid-par; run_factors carry the rest.
_BAT = [70, 68, 66, 64, 62, 60]          # top six specialist batters
_LOWER = [(52, 71, "Off spin"),          # 7: batting all-rounder
          (44, 73, "Leg spin"),          # 8: bowling all-rounder
          (32, 77, "Fast"),              # 9: strike bowler
          (24, 76, "Fast-medium"),       # 10
          (18, 74, "Medium-fast")]       # 11


def _mk(rid, name, cat, bat, bowl, style):
    return {"roster_id": rid, "player_id": rid, "name": name,
            "rating": max(bat, bowl), "category": cat,
            "bat_rating": bat, "bowl_rating": bowl,
            "bowl_style": style, "bowl_hand": "Right", "bat_hand": "Right"}


def make_xi(tag, offset):
    """A realistic, self-contained XI: six batters, a spin-bowling all-rounder,
    a leg-spinning all-rounder, and three seamers — so pitch wicket_factors for
    both pace and spin come into play."""
    xi = [_mk(offset + i, f"{tag}Bat{i+1}", "Batsman", _BAT[i], 20, "Medium-fast")
          for i in range(len(_BAT))]
    for j, (bat, bowl, style) in enumerate(_LOWER):
        cat = "All-rounder" if j < 2 else "Bowler"
        xi.append(_mk(offset + len(_BAT) + j, f"{tag}L{j+1}", cat, bat, bowl, style))
    return xi


def _run_innings(state, guard=40):
    while not cm.is_innings_over(state) and guard > 0:
        state["current_bowler"] = cm.eligible_bowlers(state)[0]
        state["bowling_approach"] = "balanced"
        state["batting_approach"] = "balanced"
        cm.simulate_over(state)
        guard -= 1


def simulate_one(pitch, overs=20):
    """Play one full headless CIPL match on *pitch*; return a metrics dict."""
    with _muted_logging():
        a, b = make_xi("A", 1), make_xi("B", 101)
        state = cm.build_cipl_state(1, overs, 10, 20, 111, 222, a, b,
                                    "A", "B", -1, pitch, False, ball_format="T20")
        _run_innings(state)
        inn1_runs, inn1_wkts = state["total_runs"], state["total_wickets"]
        inn1_balls = cm.balls_bowled(state)
        cm.end_first_innings(state)
        _run_innings(state)
        inn2_runs, inn2_wkts = state["total_runs"], state["total_wickets"]
        inn2_balls = cm.balls_bowled(state)
        innings_balls = cm.total_balls(state)
        result = cm.compute_result(state)
    return {
        "inn1_runs": inn1_runs, "inn1_wkts": inn1_wkts, "inn1_balls": inn1_balls,
        "inn2_runs": inn2_runs, "inn2_wkts": inn2_wkts, "inn2_balls": inn2_balls,
        "innings_balls": innings_balls,
        "chase_won": result["margin_type"] == "wickets",
        "margin_type": result["margin_type"], "margin": result["margin"],
    }


def _pct(sorted_vals, q):
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1)))
    return sorted_vals[idx]


def collect(pitch, n):
    rows = [simulate_one(pitch) for _ in range(n)]
    inn1 = sorted(r["inn1_runs"] for r in rows)

    # Sub-100 Rule: a side bowled out (10 down) under 100 is very rare. Measured
    # as a fraction of ALL innings played (the true "how often does this happen").
    innings_played = 0
    allout_sub100 = 0
    allout_total = 0
    for r in rows:
        for runs, wkts in ((r["inn1_runs"], r["inn1_wkts"]),
                           (r["inn2_runs"], r["inn2_wkts"])):
            innings_played += 1
            if wkts >= 10:
                allout_total += 1
                if runs < 100:
                    allout_sub100 += 1

    # Chase win % grouped by the pitch's own first-innings total bands.
    bands = (ground_config.get_pitch_profile(pitch) or {}).get("chase_win_pct") or []
    bands = sorted(bands, key=lambda x: x["max"])
    band_stats = {b["max"]: {"pct_spec": b["pct"], "won": 0, "n": 0} for b in bands}
    for r in rows:
        for b in bands:
            if r["inn1_runs"] <= b["max"]:
                band_stats[b["max"]]["n"] += 1
                band_stats[b["max"]]["won"] += 1 if r["chase_won"] else 0
                break

    # Fighting Match Rule metrics. "Capitulation" = a blown-away loss, not merely
    # a defended win: lost by a wide margin OR the chase folded well before the
    # closing overs. A 30-45 run loss chasing a big total is a fighting loss.
    defended = [r for r in rows if r["margin_type"] == "runs"]
    def_margins = sorted(r["margin"] for r in defended)
    capitulations = sum(
        1 for r in defended
        if r["margin"] > 55 or (r["inn2_wkts"] >= 10
                                and r["inn2_balls"] < r["innings_balls"] * 0.75))
    # Depth: reached the closing 2 overs (ball 108+ of 120).
    deep = sum(1 for r in rows if r["inn2_balls"] >= r["innings_balls"] - 12)

    return {
        "n": n, "rows": rows, "inn1_sorted": inn1,
        "floor": _pct(inn1, 0.10), "par": statistics.median(inn1),
        "ceiling": _pct(inn1, 0.90), "anomaly": inn1[-1] if inn1 else 0,
        "mean": statistics.mean(inn1) if inn1 else 0,
        "sub100_rate": (allout_sub100 / innings_played * 100) if innings_played else 0.0,
        "allout_total": allout_total,
        "chase_win_overall": sum(1 for r in rows if r["chase_won"]) / n * 100,
        "band_stats": band_stats,
        "def_margin_median": (statistics.median(def_margins) if def_margins else 0),
        "capitulation_rate": (capitulations / len(defended) * 100) if defended else 0.0,
        "deep_rate": deep / n * 100,
    }


# ── Tolerances ─────────────────────────────────────────────────────────
# Par (median) is the primary, tight target. Floor/Ceiling are the spec's soft
# "scores fluctuate anywhere between" guides, so their tolerances are wider.
TOL = {"floor_lo": 35, "floor_hi": 35, "par_pad": 10, "ceiling_lo": 35,
       "ceiling_hi": 40, "sub100_max": 3.0, "chase_band": 20, "capitulation_max": 30,
       "anomaly_pad": 35, "deep_min": 45.0, "margin_max": 55.0}
# Chase bands with fewer than this many samples are too noisy to judge — they are
# reported as an explicit "n<min (not judged)" check rather than silently skipped.
MIN_BAND_N = 12
# Capitulation = a blown-away loss (mirrors collect(): margin > this, or an early
# fold). Kept in sync with the counting there so the report label is accurate.
CAPITULATION_MARGIN = 55


def evaluate(pitch, m):
    dyn = ground_config.get_scoring_dynamics(pitch) or {}
    checks = []

    def chk(name, ok, detail):
        checks.append((name, ok, detail))

    floor, par_lo, par_hi = dyn.get("floor", 0), dyn.get("par_low", 0), dyn.get("par_high", 0)
    ceiling, anomaly = dyn.get("ceiling", 999), dyn.get("anomaly", 999)

    chk("par", par_lo - TOL["par_pad"] <= m["par"] <= par_hi + TOL["par_pad"],
        f"median {m['par']:.0f} vs par {par_lo}-{par_hi}")
    chk("floor", floor - TOL["floor_lo"] <= m["floor"] <= floor + TOL["floor_hi"],
        f"P10 {m['floor']:.0f} vs floor {floor}")
    chk("ceiling", ceiling - TOL["ceiling_lo"] <= m["ceiling"] <= ceiling + TOL["ceiling_hi"],
        f"P90 {m['ceiling']:.0f} vs ceiling {ceiling}")
    chk("anomaly", m["anomaly"] <= anomaly + TOL["anomaly_pad"],
        f"max {m['anomaly']:.0f} vs anomaly {anomaly} (+{TOL['anomaly_pad']})")
    chk("sub100", m["sub100_rate"] <= TOL["sub100_max"],
        f"{m['sub100_rate']:.1f}% all-out<100 (max {TOL['sub100_max']}%)")
    # Fighting Match Rule: matches run deep, losses stay close.
    chk("deep", m["deep_rate"] >= TOL["deep_min"],
        f"{m['deep_rate']:.0f}% reach ov18 (min {TOL['deep_min']}%)")
    chk("margin", m["def_margin_median"] <= TOL["margin_max"],
        f"median defended margin {m['def_margin_median']:.0f} (max {TOL['margin_max']})")
    chk("capitulation", m["capitulation_rate"] <= TOL["capitulation_max"],
        f"{m['capitulation_rate']:.0f}% blown out "
        f"(>{CAPITULATION_MARGIN} runs or early fold; max {TOL['capitulation_max']}%)")

    for mx, st in m["band_stats"].items():
        if st["n"] >= MIN_BAND_N:
            obs = st["won"] / st["n"] * 100
            chk(f"chase<={mx}", abs(obs - st["pct_spec"]) <= TOL["chase_band"],
                f"chase {obs:.0f}% vs spec {st['pct_spec']}% (n={st['n']})")
        else:
            # Not enough samples to judge — surface it explicitly rather than
            # silently passing, so a thin band can't hide a miscalibration.
            chk(f"chase<={mx}", True,
                f"n={st['n']} < {MIN_BAND_N} (not judged)")
    return checks


def print_report(pitch, m, checks, verbose=False):
    dyn = ground_config.get_scoring_dynamics(pitch) or {}
    passed = sum(1 for _, ok, _ in checks if ok)
    tag = "PASS" if passed == len(checks) else "FAIL"
    print(f"\n━━ {pitch:7} [{tag} {passed}/{len(checks)}]  n={m['n']}")
    print(f"   1st-inns:  floor(P10)={m['floor']:.0f} [{dyn.get('floor')}]   "
          f"par(med)={m['par']:.0f} [{dyn.get('par_low')}-{dyn.get('par_high')}]   "
          f"ceiling(P90)={m['ceiling']:.0f} [{dyn.get('ceiling')}]   "
          f"anomaly(max)={m['anomaly']:.0f} [{dyn.get('anomaly')}]")
    print(f"   sub-100 all-out: {m['sub100_rate']:.1f}%    "
          f"chase win overall: {m['chase_win_overall']:.0f}%    "
          f"deep(≥ov18): {m['deep_rate']:.0f}%    "
          f"defended margin median: {m['def_margin_median']:.0f}    "
          f"capitulation: {m['capitulation_rate']:.0f}%")
    print("   chase win% by band: " + "  ".join(
        f"≤{mx}:{(st['won']/st['n']*100 if st['n'] else 0):.0f}%/{st['pct_spec']}%(n{st['n']})"
        for mx, st in m["band_stats"].items()))
    if verbose:
        for name, ok, detail in checks:
            print(f"      [{'ok ' if ok else 'XX '}] {name}: {detail}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="CIPL pitch calibration harness")
    ap.add_argument("--n", type=int, default=300, help="matches per pitch")
    ap.add_argument("--pitch", default=None, help="single pitch (default: all spec pitches)")
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument("--verbose", action="store_true", help="show per-check pass/fail")
    args = ap.parse_args(argv)
    if args.n <= 0:
        ap.error("--n must be greater than zero")

    random.seed(args.seed)
    pitches = [args.pitch] if args.pitch else SPEC_PITCHES
    all_pass = True
    for pitch in pitches:
        m = collect(pitch, args.n)
        checks = evaluate(pitch, m)
        if not all(ok for _, ok, _ in checks):
            all_pass = False
        print_report(pitch, m, checks, verbose=args.verbose)

    print("\n" + ("✅ ALL PITCHES WITHIN TOLERANCE" if all_pass
                  else "❌ SOME PITCHES OUT OF TOLERANCE — tune constants/YAML"))
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
