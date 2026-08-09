"""Does a simulated match look like real T20 cricket?

``tools/pitch_calibration.py`` answers a different question — whether a team
total lands where the spec says it should. A side can post a perfectly par 200
while the scorecard underneath it is nonsense: every second dismissal an LBW,
more twos than fours, nobody ever caught at the death. This harness measures the
*texture* — the things a player actually reads on a scorecard — against the
real-world figures, and prints both side by side.

    python -m tools.realism_audit --pitch Even --n 100

Each benchmark in ``REAL`` is a share of the same denominator the engine figure
uses, so the two columns are directly comparable. They are approximate — real
T20 varies by ground, season and format — so treat a flagged row as "look at
this", not as a failure. The tolerance is deliberately generous for the same
reason.

One measurement trap is baked in rather than left to be rediscovered: maidens
are reported split into **pure** and **wicket** maidens. A wicket ball is a dot
ball, and a new batter arrives with a settling penalty, so wicket maidens are
several times more common than pure ones. Counting them together makes the
engine look four times more miserly than it is — which is exactly the wrong
conclusion to draw, and the reason this note is here.
"""

import argparse
import collections
import random
import statistics
import sys

from tools import pitch_calibration as pc
import services.cipl_match as cm

# Real T20 reference points. Ball outcomes are shares of legal balls;
# dismissals are shares of all dismissals.
REAL_BALLS = {"0": 35.5, "1": 33.0, "2": 6.5, "3": 0.5, "4": 11.5, "6": 5.5,
              "W": 4.7}
REAL_DISMISSALS = {"Caught": 57.0, "Bowled": 19.0, "Run Out": 9.0,
                   "LBW": 6.5, "Stumped": 5.0}
REAL_PURE_MAIDEN_PCT = 0.15      # of all overs bowled
REAL_WICKET_MAIDEN_PCT = 0.7

# How a captain who is trying to win actually picks. Uniform selection is not a
# neutral choice — it bats defensively in a fifth of the overs, which no T20
# side does, and it drags every measured figure with it.
BAT_APPROACHES = ["defensive", "rotate", "balanced", "aggressive", "ultra"]
BAT_WEIGHTS = [0, 3, 7, 5, 3]
BOWL_APPROACHES = ["defensive", "balanced", "mixed", "aggressive", "variation"]
BOWL_WEIGHTS = [1, 5, 4, 4, 3]


def _new_stats():
    return {
        "balls": collections.Counter(), "legal": 0, "overs": 0,
        "pure_maidens": 0, "wicket_maidens": 0, "innings": 0,
        "dismissals": collections.Counter(), "totals": [], "wickets": [],
        "extras": [], "scores": [], "by_position": collections.defaultdict(list),
        "econ": [], "bowler_wickets": [], "bowlers_used": [],
        "best_partnership": [],
    }


def _play_innings(state, S):
    guard = 0
    while not cm.is_innings_over(state) and guard < 40:
        guard += 1
        eligible = cm.eligible_bowlers(state)
        if not eligible:
            break
        state["current_bowler"] = random.choice(eligible[:4])
        state["batting_approach"] = random.choices(BAT_APPROACHES, BAT_WEIGHTS)[0]
        state["bowling_approach"] = random.choices(BOWL_APPROACHES, BOWL_WEIGHTS)[0]
        summary = cm.simulate_over(state) or {}
        marks = summary.get("over_timeline") or []
        for mark in marks:
            S["balls"][mark] += 1
        # Count legal balls, not calls of simulate_over. A chase that finishes
        # mid-over still produces a summary, and counting it as a whole over
        # inflates the denominator with an over that could never have been a
        # maiden — which quietly understates both maiden rates. Six legal balls
        # is also the right maiden test: an over of five legal balls and a wide
        # has six marks, and is not a maiden.
        legal_balls = sum(1 for m in marks if m not in ("WD", "NB"))
        S["legal"] += legal_balls
        S["overs"] += legal_balls / 6
        if summary.get("over_runs") == 0 and legal_balls == 6:
            S["wicket_maidens" if "W" in marks else "pure_maidens"] += 1


def _record_innings(state, S):
    S["innings"] += 1
    S["totals"].append(state["total_runs"])
    S["wickets"].append(state["total_wickets"])
    S["extras"].append(state.get("extras_total", 0))
    for pos, player in enumerate(state.get("batting_order") or [], 1):
        bs = (state.get("bat_stats") or {}).get(str(player["roster_id"]), {})
        if bs.get("balls"):
            S["scores"].append(bs["runs"])
            S["by_position"][pos].append((bs["runs"], bs["balls"]))
        if bs.get("out") and bs.get("how_out"):
            S["dismissals"][bs["how_out"]] += 1
    used = 0
    for player in state.get("bowl_xi") or []:
        bw = (state.get("bowl_stats") or {}).get(str(player["roster_id"]), {})
        if bw.get("balls"):
            used += 1
            S["econ"].append(bw["runs"] / (bw["balls"] / 6))
            S["bowler_wickets"].append(bw.get("wickets", 0))
    S["bowlers_used"].append(used)
    stands = state.get("partnership_history") or []
    if stands:
        S["best_partnership"].append(max(p.get("runs", 0) for p in stands))


def collect(pitch, n, overs=20):
    S = _new_stats()
    for _ in range(n):
        with pc._muted_logging():
            a, b = pc.make_xi("A", 1), pc.make_xi("B", 101)
            state = cm.build_cipl_state(1, overs, 10, 20, 111, 222, a, b,
                                        "A", "B", -1, pitch, False,
                                        ball_format="T20")
            for innings in (1, 2):
                _play_innings(state, S)
                _record_innings(state, S)
                if innings == 1:
                    cm.end_first_innings(state)
    return S


def _row(label, got, real, tol_frac=0.30, tol_min=1.0):
    flag = "" if abs(got - real) <= max(tol_min, real * tol_frac) else "  <-- off"
    return f"   {label:12} {got:6.2f}%   real {real:5.2f}%{flag}"


def report(pitch, S):
    legal = S["legal"] or 1
    overs = S["overs"] or 1
    print(f"\n═══ {pitch}  ·  {S['innings']} innings, {overs:.0f} overs ═══")
    print(f"   totals median {statistics.median(S['totals']):.0f}   "
          f"wickets/innings {statistics.mean(S['wickets']):.2f}   "
          f"extras/innings {statistics.mean(S['extras']):.1f}")

    print("\n── every ball (share of legal balls) ──")
    for mark in ("0", "1", "2", "3", "4", "6", "W"):
        print(_row(mark, S["balls"][mark] / legal * 100, REAL_BALLS[mark]))

    print("\n── how they got out ──")
    total = sum(S["dismissals"].values()) or 1
    for mode, real in sorted(REAL_DISMISSALS.items(), key=lambda kv: -kv[1]):
        print(_row(mode, S["dismissals"][mode] / total * 100, real))

    print("\n── maidens (pure and wicket maidens are different animals) ──")
    print(_row("pure", S["pure_maidens"] / overs * 100, REAL_PURE_MAIDEN_PCT,
               tol_min=0.2))
    print(_row("with wicket", S["wicket_maidens"] / overs * 100,
               REAL_WICKET_MAIDEN_PCT, tol_min=0.4))

    print("\n── batting ──")
    scores = S["scores"]
    print(f"   50+ per innings {sum(1 for s in scores if s >= 50)/S['innings']:.2f}"
          f"   (real ~0.75)")
    print(f"   100s per innings {sum(1 for s in scores if s >= 100)/S['innings']:.3f}"
          f"  (real ~0.035)")
    print("   strike rate by position   (real: openers ~135, finishers ~150)")
    for pos in sorted(S["by_position"]):
        rows = S["by_position"][pos]
        runs = sum(r for r, _ in rows)
        balls = sum(b for _, b in rows)
        if balls:
            print(f"      #{pos:<2} SR {runs/balls*100:6.1f}   "
                  f"avg {runs/len(rows):5.1f} off {balls/len(rows):4.1f}")

    print("\n── bowling ──")
    econ = sorted(S["econ"])
    print(f"   economy median {statistics.median(econ):.2f}   "
          f"P10 {econ[len(econ)//10]:.2f}   P90 {econ[len(econ)*9//10]:.2f}"
          f"   (real ~8.3, 6.0-11.5)")
    print(f"   3+ wicket hauls per innings "
          f"{sum(1 for w in S['bowler_wickets'] if w >= 3)/S['innings']:.2f}"
          f"   (real ~0.35)")
    print(f"   bowlers used {statistics.mean(S['bowlers_used']):.1f}   (real ~5.5)")
    if S["best_partnership"]:
        print(f"   best partnership median "
              f"{statistics.median(S['best_partnership']):.0f}   (real ~55)")


def main(argv=None):
    ap = argparse.ArgumentParser(description="T20 realism audit")
    ap.add_argument("--pitch", default="Even")
    ap.add_argument("--n", type=int, default=100, help="matches")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    random.seed(args.seed)
    report(args.pitch, collect(args.pitch, args.n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
