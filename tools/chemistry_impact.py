"""Team Chemistry impact harness — how much does chemistry actually move a game?

The chemistry ceilings in ``services/chemistry.py`` are only worth what they do
to a scoreboard, and that is not something you can read off the constants: the
role boost enters as an effective-rating lift, the clutch multiplier doubles it
for the last quarter of the innings, and the partnership bond moves the run
distribution directly. This runs the real ``services.probability_engine`` over
whole innings and reports the swing between the worst and best chemistry a side
can have.

It exists because ``docs/team-chemistry.md`` §7.6.1 quotes that swing, and a
number in a document that nothing reproduces is a number that quietly goes stale
the next time somebody tunes a constant.

Usage:
    python -m tools.chemistry_impact                  # current constants, n=250
    python -m tools.chemistry_impact --n 1000
    python -m tools.chemistry_impact --compare 4      # ...vs an older ceiling

``--compare`` re-runs the whole table with ``ROLE_MAX_BONUS`` forced to the given
value and the pre-uplift bond deltas, which is how the "+9.6% → +15.0%"
like-for-like figure in the doc was produced. Both halves run in one process
against one harness, so the comparison is meaningful even though the absolute
totals depend on this harness's simplifications (a fixed delivery mix, no pitch
wear, no fielding quality) rather than on a real match.
"""

from __future__ import annotations

import argparse
import logging
import random
import statistics

from services import chemistry
from services import probability_engine as pe
from services.probability_engine import calculate_outcome

# Equal sides, so the only thing moving between rows is chemistry.
BASE_RATING = 80.0
TOTAL_OVERS = 20
DEFAULT_INNINGS = 250
SEED = 20260802

SHOTS = ("defensive", "normal", "aggressive")
# A fixed spread of deliveries rather than a bowling AI — the point is to hold
# everything except chemistry still.
DELIVERIES = (
    ("Fast Pacer", "Right", "stock", "good"),
    ("Medium Pacer", "Right", "stock", "full"),
    ("Off Spinner", "Right", "stock", "good"),
)

# The bond deltas before the uplift, for --compare.
LEGACY_BOND = {"BOND_TWO_BONUS": 1.6, "BOND_ONE_BONUS": 1.0,
               "BOND_DOT_PENALTY": 2.6}


def simulate_innings(bat_rating, chem_boost, bond, rng):
    """One innings. Returns ``(runs, wickets)``.

    Chemistry is applied exactly as ``handlers.match._ball_outcome`` applies it:
    the role boost lifts the effective batting rating, ``clutch_multiplier``
    doubles that lift at the death, and the crease-pair bond is handed to the
    probability engine to move the run distribution.
    """
    runs = wickets = 0
    for over in range(1, TOTAL_OVERS + 1):
        style, hand, variation, length = rng.choice(DELIVERIES)
        for _ in range(6):
            if wickets >= 10:
                return runs, wickets
            clutch = chemistry.clutch_multiplier(over, TOTAL_OVERS)
            outcome = calculate_outcome(
                style, hand, variation, length, "balanced",
                over, TOTAL_OVERS, rng.choice(SHOTS),
                bat_rating + chem_boost * clutch, BASE_RATING,
                chem_bond=bond)
            if outcome["type"] == "wicket":
                wickets += 1
            else:
                runs += outcome.get("runs", 0)
    return runs, wickets


def measure(chem_pct, innings, bond=0.0, bat_rating=BASE_RATING, ceiling=None):
    """Average score and wickets for a side at ``chem_pct`` of full chemistry.

    ``ceiling`` overrides ``ROLE_MAX_BONUS['Batsman']`` so an older tuning can be
    measured in the same harness. Every row reseeds from ``SEED``, so rows differ
    only by their chemistry, not by their luck.
    """
    if ceiling is None:
        ceiling = chemistry.ROLE_MAX_BONUS["Batsman"]
    boost = ceiling * chem_pct / 100.0
    rng = random.Random(SEED)
    results = [simulate_innings(bat_rating, boost, bond, rng)
               for _ in range(innings)]
    scores = [r for r, _w in results]
    return statistics.fmean(scores), statistics.fmean(w for _r, w in results)


def run_table(innings, ceiling=None, label=""):
    """Print the §7.6.1 table and return the worst-to-best swing as a fraction."""
    if label:
        print(f"\n── {label} ──")
    print(f"| {'Squad':<28} | Avg score | Wickets |")
    print(f"|{'-' * 30}|----------:|--------:|")
    rows = (("0 chemistry", 0, 0.0),
            ("50 chemistry", 50, 0.0),
            ("100 chemistry", 100, 0.0),
            ("100 chemistry + full bond", 100, 1.0))
    scores = {}
    for name, pct, bond in rows:
        avg, wkts = measure(pct, innings, bond=bond, ceiling=ceiling)
        scores[name] = avg
        print(f"| {name:<28} | {avg:9.1f} | {wkts:7.1f} |")

    worst = scores["0 chemistry"]
    best = scores["100 chemistry + full bond"]
    swing = (best - worst) / worst
    print(f"\nworst → best: {best - worst:+.1f} runs ({swing:+.1%})")
    return swing


def run_rating_check(innings, ceiling=None):
    """The binding constraint: card quality must still beat chemistry."""
    weak, _ = measure(100, innings, bat_rating=72.0, ceiling=ceiling)
    strong, _ = measure(0, innings, bat_rating=85.0, ceiling=ceiling)
    print(f"\n| 72-rated batting, perfect chemistry | {weak:9.1f} |")
    print(f"| 85-rated batting, zero chemistry    | {strong:9.1f} |")
    print(f"card quality wins by {strong - weak:.0f} runs")
    return strong > weak


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_INNINGS,
                        help=f"innings per row (default {DEFAULT_INNINGS})")
    parser.add_argument("--compare", type=float, metavar="CEILING",
                        help="also run the table at this older ROLE_MAX_BONUS "
                             "with the pre-uplift bond deltas")
    args = parser.parse_args()

    # The engine falls back to static shot mods without a database, and says so
    # on every ball. That is fine here and would drown the table.
    logging.disable(logging.CRITICAL)

    current_ceiling = chemistry.ROLE_MAX_BONUS["Batsman"]
    print(f"ROLE_MAX_BONUS[Batsman] = {current_ceiling}   "
          f"FIELDING_MAX_BONUS = {chemistry.FIELDING_MAX_BONUS:g}   "
          f"bond = {pe.BOND_TWO_BONUS}/{pe.BOND_ONE_BONUS}/"
          f"{pe.BOND_DOT_PENALTY}")
    print(f"{args.n} innings per row, {TOTAL_OVERS} overs, "
          f"equal {BASE_RATING:g}-rated sides, seed {SEED}")

    current = run_table(args.n, label="current")
    run_rating_check(args.n)

    if args.compare is not None:
        saved = {name: getattr(pe, name) for name in LEGACY_BOND}
        try:
            for name, value in LEGACY_BOND.items():
                setattr(pe, name, value)
            previous = run_table(args.n, ceiling=args.compare,
                                 label=f"ceiling {args.compare:g}, "
                                       f"pre-uplift bond")
        finally:
            for name, value in saved.items():
                setattr(pe, name, value)
        print(f"\nlike-for-like: {previous:+.1%} → {current:+.1%}")


if __name__ == "__main__":
    main()
