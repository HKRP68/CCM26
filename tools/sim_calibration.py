"""Monte-Carlo balance check for the standalone Conditions Engine.

    python -m tools.sim_calibration --format T20 --n 200
    python -m tools.sim_calibration --format ODI --pitch Green --n 100

Prints, per pitch: mean/median first-innings total, wickets, sixes, chase
win %, rain % and tie %. Edit ``config/sim_engine.json`` (or a
``sim_engine.local.json``) and re-run — no code changes needed.
"""

import argparse
import statistics

from engine.sim import stadium as stadium_mod, teams
from engine.sim.config import get_config
from engine.sim.match import MatchSetup, simulate_match

SIDES = ["India", "Australia", "England", "South Africa", "Pakistan", "New Zealand"]
PITCHES = ["Dusty", "Green", "Dry", "Bouncy", "Even", "Hard", "Flat"]


def collect(fmt, pitch, n, seed=1, cfg=None):
    cfg = cfg or get_config()
    rows = teams.load_rows()
    xis = {c: teams.pick_xi(c, rows) for c in SIDES}
    grounds = stadium_mod.all_stadiums()
    firsts, wkts, sixes, chase, rain, ties, draws, results = [], [], [], 0, 0, 0, 0, 0
    for i in range(n):
        a = xis[SIDES[i % len(SIDES)]]
        b = xis[SIDES[(i + 1 + i // len(SIDES)) % len(SIDES)]]
        if a is b:
            b = xis[SIDES[(i + 2) % len(SIDES)]]
        g = grounds[i % len(grounds)] if grounds else None
        res = simulate_match(MatchSetup(fmt=fmt, team1=a, team2=b, pitch=pitch, stadium=g,
                                        seed=f"cal-{seed}-{i}", commentary=False), cfg)
        inn = res["innings"]
        firsts.append(inn[0]["runs"])
        wkts.append(inn[0]["wickets"])
        sixes.append(sum(b["sixes"] for x in inn for b in x["batting"]))
        if res["rain"]:
            rain += 1
        r = res["result"]
        if r["margin"] == "tie":
            ties += 1
        elif r["margin"] == "draw":
            draws += 1
        elif fmt != "Test" and r["winner"] == inn[-1]["team"]:
            chase += 1
        results += 1
    return {
        "pitch": pitch, "n": n,
        "mean": round(statistics.mean(firsts), 1), "median": statistics.median(firsts),
        "p10": sorted(firsts)[n // 10], "p90": sorted(firsts)[(9 * n) // 10],
        "wkts": round(statistics.mean(wkts), 2), "sixes": round(statistics.mean(sixes), 1),
        "chase%": round(100.0 * chase / n, 1), "rain%": round(100.0 * rain / n, 1),
        "tie%": round(100.0 * ties / n, 1), "draw%": round(100.0 * draws / n, 1),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--format", default="T20", choices=["T20", "ODI", "Test"])
    ap.add_argument("--pitch", default=None)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args(argv)
    pitches = [args.pitch] if args.pitch else PITCHES
    print(f"{'pitch':<8}{'mean':>7}{'med':>6}{'p10':>6}{'p90':>6}{'wkts':>6}{'6s':>6}"
          f"{'chase%':>8}{'rain%':>7}{'tie%':>6}{'draw%':>7}")
    for p in pitches:
        r = collect(args.format, p, args.n, args.seed)
        print(f"{p:<8}{r['mean']:>7}{r['median']:>6}{r['p10']:>6}{r['p90']:>6}{r['wkts']:>6}"
              f"{r['sixes']:>6}{r['chase%']:>8}{r['rain%']:>7}{r['tie%']:>6}{r['draw%']:>7}")


if __name__ == "__main__":
    main()
