"""Legacy / Realistic / Balanced comparison (Codex Prompt 05, §22).

Runs matched-seed matches under each preset and reports the balance metrics a
Balanced engine must satisfy: it should add game volatility and drama over an
innings WITHOUT inflating average totals, wicket rates or unfairly tilting
results. Rating monotonicity (a stronger side wins more) must survive.

    python -m tools.drama_balance_report --n 60 --pitch Even

``--n`` is matches per arm; each runs off one seeded RNG (reproducible from
--seed). Ratings are shifted only on in-memory copies; data/players.json is
never touched.
"""

import argparse
import os
import statistics

from tools import pitch_calibration as pc
from tools.compare_realism import _bias_xi


def _set_env(preset):
    os.environ["SIM_REALISM_ENABLED"] = "1"
    os.environ["SIM_DRAMA_PRESET"] = preset
    os.environ["SIM_DRAMA_ENABLED"] = "0" if preset == "REALISTIC" else "1"
    from engine.realism import config as rcfg
    from engine.drama import config as dcfg
    rcfg.clear_cache()
    dcfg.clear_cache()


def _run(preset, pitch, n, overs, seed, strong_bias=0):
    _set_env(preset)
    from services.sim_match import simulate_match
    firsts, sds_src, close, highlights, fifties, hundreds = [], [], 0, [], 0, 0
    tensions = []
    stronger_wins, decided = 0, 0
    for i in range(n):
        a = pc.make_xi("A", seed * 1000 + i)
        b = pc.make_xi("B", seed * 1000 + 500 + i)
        if strong_bias:
            a = _bias_xi(a, strong_bias, strong_bias)
        with pc._muted_logging():
            m = simulate_match(a, b, overs, pitch, "A", "B", seed=seed * 7919 + i)
        i1 = m["innings1"]
        firsts.append(i1["runs"])
        scores = [bs["runs"] for bs in i1["bat_stats"].values()]
        fifties += sum(1 for s in scores if s >= 50)
        hundreds += sum(1 for s in scores if s >= 100)
        if abs(m["innings1"]["runs"] - m["innings2"]["runs"]) <= 10:
            close += 1
        if i1.get("drama"):
            highlights.append(len(i1["drama"]["highlights"]))
            tensions.append(i1["drama"]["final_tension"])
        w = m["result"]["winner"]
        if w:
            decided += 1
            if strong_bias and w == "A":
                stronger_wins += 1
    return {
        "mean": statistics.mean(firsts),
        "median": statistics.median(firsts),
        "sd": statistics.pstdev(firsts) if len(firsts) > 1 else 0.0,
        "close_pct": 100.0 * close / max(1, n),
        "fifties_pi": fifties / max(1, n),
        "hundreds_pi": hundreds / max(1, n),
        "highlights_pi": statistics.mean(highlights) if highlights else 0.0,
        "mean_tension": statistics.mean(tensions) if tensions else 0.0,
        "stronger_pct": (100.0 * stronger_wins / decided) if (strong_bias and decided) else float("nan"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--pitch", default="Even")
    ap.add_argument("--overs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--strong-bias", type=int, default=12)
    args = ap.parse_args(argv)

    try:
        arms = {p: _run(p, args.pitch, args.n, args.overs, args.seed)
                for p in ("REALISTIC", "BALANCED")}
        mono = {p: _run(p, args.pitch, args.n, args.overs, args.seed + 1,
                        strong_bias=args.strong_bias)
                for p in ("REALISTIC", "BALANCED")}
    finally:
        for k in ("SIM_DRAMA_ENABLED", "SIM_DRAMA_PRESET"):
            os.environ.pop(k, None)

    print("\nDrama balance report — %d matches/arm, pitch %s, %d overs\n"
          % (args.n, args.pitch, args.overs))
    rows = [
        ("mean 1st-inn total", "mean", "%8.1f"),
        ("median 1st-inn total", "median", "%8.1f"),
        ("stdev 1st-inn total", "sd", "%8.1f"),
        ("close match (<=10) %", "close_pct", "%8.1f"),
        ("50s / innings", "fifties_pi", "%8.3f"),
        ("100s / innings", "hundreds_pi", "%8.3f"),
        ("highlights / innings", "highlights_pi", "%8.2f"),
        ("mean final tension", "mean_tension", "%8.1f"),
    ]
    print("%-24s %10s %10s" % ("metric", "REALISTIC", "BALANCED"))
    print("-" * 48)
    for label, key, fmt in rows:
        print(("%-24s " + fmt + " " + fmt)
              % (label, arms["REALISTIC"][key], arms["BALANCED"][key]))
    print("-" * 48)
    print(("%-24s " + "%8.1f" + " " + "%8.1f")
          % ("stronger-side win %% (+%d)" % args.strong_bias,
             mono["REALISTIC"]["stronger_pct"], mono["BALANCED"]["stronger_pct"]))
    print("\nBalanced should lift volatility (stdev, highlights, tension) with")
    print("mean/median totals and rating monotonicity close to Realistic.\n")


if __name__ == "__main__":
    main()
