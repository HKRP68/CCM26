"""Legacy-vs-realism fairness report (Prompt 04, requirement 7).

Runs the two engines side by side on controlled match-ups and prints the
fairness invariants a cricket sim must satisfy regardless of how it models a
ball:

  * symmetry        — two identical XIs should each win ~50% of the time;
  * toss neutrality — the toss winner should not win materially more than 50%
                      (the Fairness-fix 4 coin flip replaced the old "home side
                      always wins the toss" default);
  * no bat-first edge — the side batting first should win ~50% (a small chase
                      edge is normal);
  * rating monotonicity — a stronger XI should beat a weaker one well above 50%.

Usage:
    python -m tools.fairness_report --n 60 --pitch Even
    python -m tools.fairness_report --n 60 --strong-bias 12

``--n`` is matches per scenario per arm. Each match runs off one seeded RNG
(engine/sim/rng), so the whole report is reproducible from ``--seed``. Ratings
are shifted only on in-memory copies; data/players.json is never touched.
"""

import argparse
import os

from tools.compare_realism import _set_env, _bias_xi, _xi_strength
from tools import pitch_calibration as pc


def _simulate(home_xi, away_xi, pitch, overs, seed):
    # Imported lazily so the realism flag set by _set_env is read fresh.
    from services.sim_match import simulate_match
    with pc._muted_logging():
        return simulate_match(home_xi, away_xi, overs, pitch,
                              "A", "B", seed=seed)


def _run_scenario(realism, pitch, n, overs, base_seed, strong_bias=0):
    """Return fairness tallies for one arm (legacy or realism)."""
    _set_env(realism)
    first_xi_wins = 0            # the XI object passed as team A
    bat_first_wins = 0
    toss_winner_wins = 0
    stronger_wins = 0
    decided = 0
    strong_decided = 0

    for i in range(n):
        a = pc.make_xi("A", base_seed * 1000 + i)
        b = pc.make_xi("B", base_seed * 1000 + 500 + i)
        if strong_bias:
            a = _bias_xi(a, strong_bias, strong_bias)   # team A is the strong side
        # Alternate which XI is "home" so any name/order bias cancels.
        if i % 2 == 0:
            home, away, a_is_home = a, b, True
        else:
            home, away, a_is_home = b, a, False

        m = _simulate(home, away, pitch, overs, base_seed * 7919 + i)
        winner = m["result"]["winner"]
        if not winner:
            continue                                     # tie / no result
        decided += 1

        a_name = "A" if a_is_home else "B"               # the strong/first XI's label
        if winner == a_name:
            first_xi_wins += 1
            if strong_bias:
                stronger_wins += 1
        if strong_bias:
            strong_decided += 1

        if winner == m["innings1"]["batting_team"]:
            bat_first_wins += 1
        if winner == m["toss"]["winner"]:
            toss_winner_wins += 1

    pct = lambda x: 100.0 * x / max(1, decided)
    return {
        "decided": decided,
        "symmetry_win_pct": pct(first_xi_wins),
        "bat_first_win_pct": pct(bat_first_wins),
        "toss_winner_win_pct": pct(toss_winner_wins),
        "stronger_win_pct": (100.0 * stronger_wins / max(1, strong_decided)
                             if strong_bias else float("nan")),
    }


def _print_row(label, legacy, realism, ideal, fmt="%7.1f"):
    lv = legacy if legacy == legacy else float("nan")      # noqa: E711 (NaN check)
    print(("%-26s " + fmt + " " + fmt + "   %s")
          % (label, legacy, realism, ideal))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=40, help="matches per scenario per arm")
    ap.add_argument("--pitch", default="Even")
    ap.add_argument("--overs", type=int, default=20)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--strong-bias", type=int, default=12,
                    help="rating points added to the strong XI for monotonicity")
    args = ap.parse_args(argv)

    try:
        # Symmetry / toss / bat-first use equal sides.
        sym_legacy = _run_scenario(False, args.pitch, args.n, args.overs, args.seed)
        sym_realism = _run_scenario(True, args.pitch, args.n, args.overs, args.seed)
        # Monotonicity uses a biased strong side.
        mon_legacy = _run_scenario(False, args.pitch, args.n, args.overs,
                                   args.seed + 1, strong_bias=args.strong_bias)
        mon_realism = _run_scenario(True, args.pitch, args.n, args.overs,
                                    args.seed + 1, strong_bias=args.strong_bias)
    finally:
        os.environ.pop("SIM_REALISM_ENABLED", None)
        os.environ.pop("SIM_REALISM_SHADOW_MODE", None)

    print("\nFairness report — %d matches/scenario/arm, pitch %s, %d overs"
          % (args.n, args.pitch, args.overs))
    print("equal sides: %d/%d decided (legacy/realism); strong+%d: %d/%d decided\n"
          % (sym_legacy["decided"], sym_realism["decided"], args.strong_bias,
             mon_legacy["decided"], mon_realism["decided"]))
    print("%-26s %7s %7s   %s" % ("metric", "legacy", "realism", "ideal"))
    print("-" * 60)
    _print_row("symmetry win %", sym_legacy["symmetry_win_pct"],
               sym_realism["symmetry_win_pct"], "~50 (equal sides)")
    _print_row("toss-winner win %", sym_legacy["toss_winner_win_pct"],
               sym_realism["toss_winner_win_pct"], "~50 (no toss edge)")
    _print_row("bat-first win %", sym_legacy["bat_first_win_pct"],
               sym_realism["bat_first_win_pct"], "~50 (small chase edge ok)")
    _print_row("stronger-side win %", mon_legacy["stronger_win_pct"],
               mon_realism["stronger_win_pct"], ">50 (monotonic)")
    print("-" * 60)
    print("Each match runs off one seeded RNG; home/away is alternated so a")
    print("naming bias cancels. Figures are approximate at this n — raise --n")
    print("for tighter intervals.\n")


if __name__ == "__main__":
    main()
