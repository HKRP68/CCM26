"""Over-to-over variation benchmark (Historical Cricket Intelligence, Feature 2).

Runs the realism engine with the over-volatility effect OFF then ON over matched
seeds and reports the over-level distribution against the modern-T20 targets in
data/simulation_reference/v1/t20_over_volatility.json. The key checks: the
runs-per-over SD moves toward the historical band, the low/explosive-over tails
fatten, and the innings mean does NOT shift (zero-mean).

    python -m tools.compare_over_volatility --n 120 --pitch Even

Ratings use tools.pitch_calibration.make_xi (equal sides); data/players.json is
never touched.
"""

import argparse
import json
import os
import statistics as S

REF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "simulation_reference", "v1", "t20_over_volatility.json")


def _measure(n, pitch, overs, seed):
    from tests.test_sim_match import _make_xi
    from services.sim_match import simulate_match
    oruns, owkts, totals, inn_wkts = [], [], [], []
    for s in range(n):
        m = simulate_match(_make_xi("H", 80), _make_xi("A", 80), overs, pitch,
                           "A", "B", seed=seed + s)
        totals.append(m["innings1"]["runs"])
        for inn in ("innings1", "innings2"):
            inn_wkts.append(m[inn]["wickets"])
            pr = pw = 0
            for ov in m[inn]["over_summaries"]:
                try:
                    cr, cw = ov["team_score"].split("/")
                    cr, cw = int(cr), int(cw)
                except Exception:
                    continue
                oruns.append(cr - pr)
                owkts.append(cw - pw)
                pr, pw = cr, cw
    n_over = max(1, len(oruns))
    return {
        "over_runs_mean": S.mean(oruns),
        "over_runs_sd": S.pstdev(oruns),
        "maiden_rate": sum(1 for x in oruns if x == 0) / n_over,
        "low_rate_le2": sum(1 for x in oruns if x <= 2) / n_over,
        "explosive_ge18": sum(1 for x in oruns if x >= 18) / n_over,
        "two_wkt_over": sum(1 for x in owkts if x >= 2) / max(1, len(owkts)),
        "mean_first_innings": S.mean(totals),
        "wkts_per_innings": S.mean(inn_wkts),
        "n_overs": len(oruns),
    }


def _run(over_vol, n, pitch, overs, seed):
    os.environ["SIM_REALISM_ENABLED"] = "1"
    os.environ["SIM_REALISM_OVER_VOL"] = "1" if over_vol else "0"
    from engine.realism import config as rc
    rc.clear_cache()
    from tools import pitch_calibration as pc
    with pc._muted_logging():
        return _measure(n, pitch, overs, seed)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--pitch", default="Even")
    ap.add_argument("--overs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=5000)
    args = ap.parse_args(argv)

    try:
        off = _run(False, args.n, args.pitch, args.overs, args.seed)
        on = _run(True, args.n, args.pitch, args.overs, args.seed)
    finally:
        os.environ.pop("SIM_REALISM_OVER_VOL", None)

    tgt = {}
    try:
        tgt = json.load(open(REF))["targets"]
    except Exception:
        pass

    print("\nOver-volatility benchmark — %d matches/arm, pitch %s, %d overs"
          % (args.n, args.pitch, args.overs))
    print("(%d overs measured per arm)\n" % off["n_overs"])
    rows = [
        ("runs/over mean", "over_runs_mean", "runs_per_over_mean", "%8.2f"),
        ("runs/over SD", "over_runs_sd", "runs_per_over_sd", "%8.2f"),
        ("maiden-over %", "maiden_rate", "maiden_over_rate", "%8.3f"),
        ("low-over (<=2) %", "low_rate_le2", "low_over_rate_le2", "%8.3f"),
        ("explosive (>=18) %", "explosive_ge18", "explosive_over_rate_ge18", "%8.3f"),
        ("2+ wkt over %", "two_wkt_over", "two_plus_wicket_over_rate", "%8.3f"),
    ]
    print("%-20s %8s %8s %10s" % ("metric", "OFF", "ON", "real target"))
    print("-" * 50)
    for label, key, tkey, fmt in rows:
        pct = key.endswith(("rate", "_le2", "_ge18", "wkt_over", "_rate"))
        scale = 100.0 if "%" in label else 1.0
        tv = tgt.get(tkey)
        tv_s = ("%8.3f" % (tv * scale)) if (tv is not None and "%" in label) \
            else (("%8.2f" % tv) if tv is not None else "       -")
        print(("%-20s " + fmt + " " + fmt + " %10s")
              % (label, off[key] * scale, on[key] * scale, tv_s))
    print("-" * 50)
    print("mean 1st-inn total   %8.2f %8.2f   (zero-mean: delta %.2f)"
          % (off["mean_first_innings"], on["mean_first_innings"],
             on["mean_first_innings"] - off["mean_first_innings"]))
    print("wickets / innings    %8.2f %8.2f"
          % (off["wkts_per_innings"], on["wkts_per_innings"]))
    print("\nThe ON arm should move the SD and tails toward the real target")
    print("while leaving the mean total within noise of OFF.\n")


if __name__ == "__main__":
    main()
