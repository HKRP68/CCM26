"""Are the 25 approach pairings balanced, and is the frontier neutral?

    # the matrix, on one pitch and phase
    python -m tools.calibrate_approaches matrix --pitch Even --phase middle

    # dominance across every pitch x phase x wicket-cost cell
    python -m tools.calibrate_approaches dominance

    # is the approach family mean-neutral on every baseline node?
    python -m tools.calibrate_approaches neutrality

    # live innings per pairing, through the real engine
    python -m tools.calibrate_approaches live --n 30

Offline. Nothing here runs during a match.

WHAT "BALANCED" MEANS HERE
--------------------------
Not that every approach is equally good — that would be a pointless choice.
It means:

1. **No universally dominant approach.** No batting intent may be the best
   reply to all five bowling plans, and no bowling plan the best reply to all
   five intents, across the pitch x phase x match-situation grid.
2. **Every approach has a purpose.** Each of the ten must be the best reply
   in at least one cell, or it is a button nobody should ever press.
3. **Risk is priced.** Scoring more must cost dismissal risk monotonically,
   so there is no free lunch along the frontier.
4. **The family is mean-neutral.** Averaged over how captains actually pick,
   the approach layer must not move the innings total or the wicket rate off
   the baseline's measured anchor — it decides the *distribution* of runs,
   not how many there are.

Ranking needs a value function, because raw runs always favours Ultra Attack
and raw wickets always favours Aggressive bowling. The value used is the one
the game itself uses: runs minus the wicket cost, swept across the range of
wicket costs a real innings passes through (about 5 runs with a full shed in
the last over, about 25 at 150/6 with overs left). An approach that only wins
at one end of that sweep is not dominant.
"""

import argparse
import collections
import itertools
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from engine.realism import approaches, baselines                  # noqa: E402
from engine.realism.outcome_distribution import expected_runs     # noqa: E402

BAT = ["defensive", "rotate", "balanced", "aggressive", "ultra"]
BOWL = ["defensive", "balanced", "mixed", "variation", "aggressive"]
PITCHES = ["Dusty", "Green", "Dry", "Bouncy", "Even", "Hard", "Flat", "Dead"]
PHASES = ["powerplay", "middle", "death"]

#: Runs a wicket is worth. The sweep, not a single number: a wicket in the
#: last over with the shed full is worth about 5 runs and one at 150/6 with
#: overs left about 25, and an approach that is only correct at one end of
#: that range is not a dominant choice.
WICKET_COSTS = (5.0, 10.0, 15.0, 20.0, 25.0)


def _baseline(pitch, phase, competition=None):
    """The reference distribution for a pitch and phase, pitch-adjusted.

    The pitch enters through ``conditions.pitch_multipliers`` exactly as it
    does on a delivery, so the matrix is the one the engine really plays.
    """
    from engine.realism import conditions
    from engine.realism.config import get_config

    doc = baselines.load(format_name="T20", reload=False)
    if not doc:
        raise SystemExit("no T20 reference; run tools.build_realism_reference")
    comp = competition or baselines.competition_key(doc)
    base = baselines.smoothed(doc, "T20/%s/%s" % (comp, phase))
    pitch_mult = conditions.pitch_multipliers(pitch, 0.0, "Fast-medium",
                                              get_config())
    weights = {o: base[o] * pitch_mult.get(o, 1.0) for o in base}
    total = sum(weights.values()) or 1.0
    return {o: w / total for o, w in weights.items()}


def cell(bat, bowl, base, bowler_rating=75.0):
    return approaches.expected(bat, bowl, base, bowler_rating)


def value(e, wicket_cost):
    """Batting-side value of a delivery: runs minus what the risk costs."""
    return e["runs"] - wicket_cost * e["wicket"]


# --------------------------------------------------------------------------
def cmd_matrix(args):
    base = _baseline(args.pitch, args.phase, args.competition)
    print("\n%s pitch, %s overs — baseline %.3f runs/ball, %.4f wicket/ball"
          % (args.pitch, args.phase,
             expected_runs(base) + base["Extras"], base["Wicket"]))
    for label, key, fmt in (("expected runs per ball", "runs", "%9.3f"),
                            ("wicket probability", "wicket", "%9.4f"),
                            ("boundary probability", "boundary", "%9.4f"),
                            ("dot probability", "dot", "%9.4f")):
        print("\n  %s  (rows = batting, cols = bowling)" % label)
        print("  %-11s" % "", "".join("%9s" % w[:9] for w in BOWL))
        for b in BAT:
            print("  %-11s" % b,
                  "".join(fmt % cell(b, w, base)[key] for w in BOWL))
    print("\n  batting-side value, wicket cost %.0f runs" % args.wicket_cost)
    print("  %-11s" % "", "".join("%9s" % w[:9] for w in BOWL))
    for b in BAT:
        print("  %-11s" % b,
              "".join("%9.3f" % value(cell(b, w, base), args.wicket_cost)
                      for w in BOWL))
    return 0


def _best_replies(competition=None):
    """Best batting reply per (pitch, phase, cost, bowling plan), and mirror."""
    bat_best = collections.Counter()
    bowl_best = collections.Counter()
    bat_columns = collections.defaultdict(set)
    bowl_columns = collections.defaultdict(set)
    cells = 0
    for pitch, phase in itertools.product(PITCHES, PHASES):
        base = _baseline(pitch, phase, competition)
        table = {(b, w): cell(b, w, base) for b in BAT for w in BOWL}
        for cost in WICKET_COSTS:
            for w in BOWL:
                best = max(BAT, key=lambda b: value(table[(b, w)], cost))
                bat_best[best] += 1
                bat_columns[best].add((pitch, phase, cost, w))
                cells += 1
            for b in BAT:
                # The bowling side wants the batting value as low as possible.
                best = min(BOWL, key=lambda w: value(table[(b, w)], cost))
                bowl_best[best] += 1
                bowl_columns[best].add((pitch, phase, cost, b))
    return bat_best, bowl_best, bat_columns, bowl_columns, cells


def cmd_dominance(args):
    bat_best, bowl_best, _bc, _wc, cells = _best_replies(args.competition)
    print("\n%d pitches x %d phases x %d wicket costs = %d best-reply cells "
          "per side" % (len(PITCHES), len(PHASES), len(WICKET_COSTS), cells))
    ok = True
    for label, counter, keys in (("batting", bat_best, BAT),
                                 ("bowling", bowl_best, BOWL)):
        print("\n  how often each %s approach is the best reply" % label)
        for k in keys:
            n = counter.get(k, 0)
            flag = ""
            if n == 0:
                flag = "  <-- NEVER the best reply: a button nobody should press"
                ok = False
            elif n == cells:
                flag = "  <-- DOMINANT: best in every single cell"
                ok = False
            print("    %-11s %5d  (%5.1f%%)%s" % (k, n, 100.0 * n / cells, flag))
    # Within a single cell group, one approach owning all five columns is
    # only interesting at a realistic wicket cost. At cost 5 — a wicket in
    # the last over with the shed full — all-out attack SHOULD be the right
    # answer to everything, and reporting that as a balance problem would be
    # reporting correct cricket as a bug. So the sweep's cheap end is
    # excluded from this notice and from the pass/fail above.
    sweeps = collections.Counter()
    for pitch, phase in itertools.product(PITCHES, PHASES):
        base = _baseline(pitch, phase, args.competition)
        table = {(b, w): cell(b, w, base) for b in BAT for w in BOWL}
        for cost in WICKET_COSTS:
            if cost < 10.0:
                continue
            winners = {max(BAT, key=lambda b: value(table[(b, w)], cost))
                       for w in BOWL}
            if len(winners) == 1:
                sweeps[(winners.pop(), cost)] += 1
    if sweeps:
        print("\n  cells (cost >= 10) where one intent answered all five plans")
        for (who, cost), n in sorted(sweeps.items(), key=lambda x: -x[1]):
            print("    %-11s at wicket cost %-5.0f %3d of %d pitch/phase cells"
                  % (who, cost, n, len(PITCHES) * len(PHASES)))
    print("\n%s" % ("BALANCED: no dominant choice, every approach has a use"
                    if ok else "NOT BALANCED — see the flags above"))
    return 0 if ok else 1


def cmd_neutrality(args):
    doc = baselines.load(format_name="T20", reload=True)
    if not doc:
        raise SystemExit("no T20 reference")
    comp = args.competition or baselines.competition_key(doc)
    bt = sum(approaches.BAT_MIX.values())
    wt = sum(approaches.BOWL_MIX.values())
    print("\nmix-weighted mean vs baseline, per node "
          "(the approach layer must not move the level)")
    print("  %-40s %10s %10s %8s %8s"
          % ("node", "runs", "baseline", "drift", "wkt drift"))
    worst = 0.0
    nodes = [k for k in doc["nodes"]
             if k.startswith("T20/%s" % comp) and k.count("/") <= 3]
    for node in sorted(nodes):
        base = baselines.smoothed(doc, node)
        runs = wkt = 0.0
        for b, bs in approaches.BAT_MIX.items():
            for w, ws in approaches.BOWL_MIX.items():
                p = (bs / bt) * (ws / wt)
                e = approaches.expected(b, w, base, 75.0)
                runs += p * e["runs"]
                wkt += p * e["wicket"]
        base_runs = expected_runs(base) + base["Extras"]
        d_runs = runs / base_runs - 1.0 if base_runs else 0.0
        d_wkt = wkt / base["Wicket"] - 1.0 if base["Wicket"] else 0.0
        worst = max(worst, abs(d_runs), abs(d_wkt))
        print("  %-40s %10.4f %10.4f %+7.2f%% %+7.2f%%"
              % (node, runs, base_runs, 100 * d_runs, 100 * d_wkt))
    print("\nworst drift %.2f%% — %s" % (100 * worst,
          "neutral" if worst < 0.02 else "RECHECK the mix correction"))
    return 0 if worst < 0.02 else 1


def cmd_live(args):
    """Play real innings per pairing, through the engine, and compare."""
    os.environ["SIM_REALISM_ENABLED"] = "1"
    from engine.realism import config as rcfg
    rcfg.clear_cache()
    import random
    from tools import pitch_calibration as pc
    from tools import realism_audit as ra
    import services.cipl_match as cm

    print("\nlive innings per pairing on %s (%d innings each, %d overs)"
          % (args.pitch, args.n, args.overs))
    print("  %-11s" % "", "".join("%12s" % w[:11] for w in BOWL))
    rows = {}
    for b in BAT:
        cells_out = []
        for w in BOWL:
            totals, wkts = [], []
            for i in range(args.n):
                random.seed(hash((b, w, i)) & 0xFFFF)
                with pc._muted_logging():
                    xi_a = pc.make_xi("A", 900 + i)
                    xi_b = pc.make_xi("B", 1900 + i)
                    st = cm.build_cipl_state(1, args.overs, 10, 20, 111, 222,
                                             xi_a, xi_b, "A", "B", -1,
                                             args.pitch, False,
                                             ball_format="T20")
                    guard = 0
                    while not cm.is_innings_over(st) and guard < 40:
                        guard += 1
                        elig = cm.eligible_bowlers(st)
                        if not elig:
                            break
                        st["current_bowler"] = random.choice(elig[:4])
                        st["batting_approach"] = b
                        st["bowling_approach"] = w
                        cm.simulate_over(st)
                totals.append(st["total_runs"])
                wkts.append(st["total_wickets"])
            rows[(b, w)] = (statistics.mean(totals), statistics.mean(wkts))
            cells_out.append("%6.1f/%3.1f" % rows[(b, w)])
        print("  %-11s" % b, "".join("%12s" % c for c in cells_out))
    print("\n  cells are mean total / mean wickets over %d innings." % args.n)
    print("  No row should beat every other row in its column on BOTH.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--competition", default="")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("matrix", help="the 25 cells on one pitch and phase")
    m.add_argument("--pitch", default="Even", choices=PITCHES)
    m.add_argument("--phase", default="middle", choices=PHASES)
    m.add_argument("--wicket-cost", type=float, default=15.0)
    m.set_defaults(func=cmd_matrix)

    d = sub.add_parser("dominance", help="sweep every pitch/phase/cost cell")
    d.set_defaults(func=cmd_dominance)

    n = sub.add_parser("neutrality", help="mean-neutrality per baseline node")
    n.set_defaults(func=cmd_neutrality)

    l = sub.add_parser("live", help="real innings per pairing")
    l.add_argument("--pitch", default="Even", choices=PITCHES)
    l.add_argument("--overs", type=int, default=20)
    l.add_argument("--n", type=int, default=20)
    l.set_defaults(func=cmd_live)

    args = ap.parse_args(argv)
    args.competition = args.competition or None
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
