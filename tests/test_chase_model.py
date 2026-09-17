"""Tests for the calibrated chase model (engine/chase_model.py) and its
wiring into services/cipl_match.py.

The point of the module is that the final over of a chase lands on real-cricket
numbers, so most of what is worth asserting is distributional. The file is in
three parts:

  * the table and the target model — cheap, exact, always run;
  * the tilt / DP / solver mechanics, including a Monte Carlo cross-check of the
    backward induction (everything downstream is meaningless if the DP is wrong);
  * Monte Carlo band calibration through the real engine, which is the actual
    claim being made and is therefore slow. A trimmed grid runs by default; set
    ``SLOW_TESTS=1`` for the full one.
"""

import os
import random
import unittest

from engine import chase_model as lo
from services import cipl_match as cm

SLOW = os.getenv("SLOW_TESTS") == "1"

BASE_WEIGHTS = {"Dot": 30.0, "Single": 25.0, "Double": 8.0, "Three": 1.0,
                "Four": 12.0, "Six": 8.0, "Wicket": 6.0, "Extras": 2.0}


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #

class TableTests(unittest.TestCase):
    def test_six_ball_column_reproduces_the_anchors(self):
        # The hand-written rows ARE the spec; the generated surface has to land
        # on them or the whole module is calibrated to something else.
        for edge, row in lo.WIN_ANCHORS_6.items():
            for i, want in enumerate(row, start=1):
                self.assertAlmostEqual(lo.PAR_TABLE[edge][6][i], want, delta=0.6,
                                       msg="edge %d, %d off 6" % (edge, i))

    def test_monotone_in_runs_needed(self):
        for edge in lo.TABLE_EDGES:
            for b in range(1, lo.MAX_BALLS + 1):
                row = lo.PAR_TABLE[edge][b]
                for r in range(2, lo.MAX_RUNS + 1):
                    self.assertLessEqual(row[r], row[r - 1] + 1e-9,
                                         msg="edge %d, %d balls, %d runs" % (edge, b, r))

    def test_monotone_in_balls_left(self):
        for edge in lo.TABLE_EDGES:
            for r in range(1, lo.MAX_RUNS + 1):
                for b in range(2, lo.MAX_BALLS + 1):
                    self.assertGreaterEqual(lo.PAR_TABLE[edge][b][r],
                                            lo.PAR_TABLE[edge][b - 1][r] - 1e-9)

    def test_monotone_in_rating_edge(self):
        for b in range(1, lo.MAX_BALLS + 1):
            for r in range(1, lo.MAX_RUNS + 1):
                vals = [lo.PAR_TABLE[e][b][r] for e in lo.TABLE_EDGES]
                self.assertEqual(vals, sorted(vals),
                                 msg="%d off %d is not monotone in edge" % (r, b))

    def test_probabilities_stay_off_the_rails(self):
        for edge in lo.TABLE_EDGES:
            for b in range(1, lo.MAX_BALLS + 1):
                for r in range(1, lo.MAX_RUNS + 1):
                    p = lo.PAR_TABLE[edge][b][r]
                    self.assertGreater(p, 0.0)
                    self.assertLess(p, 100.0)


class WholeInningsTests(unittest.TestCase):
    """The surface spans the whole second innings, not just the final over.

    The hand-written anchors only correct the last over; from 12 balls out the
    backward induction stands on its own. These are the real T20 reference
    points it has to land on for that claim to be worth anything.
    """

    # (balls left, runs needed, real T20 win% for a par contest with wickets
    # in hand). Spread across required rates of 8, 12 and 15 an over so the
    # whole shape is pinned, not one slice of it.
    REFERENCE = [
        (6, 8, 85), (6, 12, 60), (6, 16, 30), (6, 20, 13),
        (12, 16, 72), (12, 24, 33), (12, 30, 15),
        (18, 24, 78), (18, 36, 33), (18, 45, 10),
        (24, 32, 80), (24, 48, 33), (24, 60, 8),
        (30, 40, 82), (30, 60, 33), (30, 75, 7),
        (60, 80, 85), (60, 120, 25),
        (90, 120, 70), (114, 160, 50),
    ]

    def test_tracks_real_cricket_across_every_horizon(self):
        errs = []
        for balls, need, real in self.REFERENCE:
            got = lo.PAR_TABLE[0][balls][need]
            errs.append(got - real)
            self.assertAlmostEqual(
                got, real, delta=8.0,
                msg="%d off %d (%.1f rpo): %.1f%%, real ~%d%%"
                    % (need, balls, need * 6.0 / balls, got, real))
        rms = (sum(e * e for e in errs) / len(errs)) ** 0.5
        self.assertLess(rms, 4.0, msg="RMS error %.1f across the surface" % rms)

    def test_an_ordinary_chase_is_not_a_lost_cause(self):
        # The bug that started this: the old runs x wickets matrix pinned a
        # routine chase at 20% from ball one, because every ask above 51 runs
        # collapsed into its last row. 160 off 114 is a coin flip.
        self.assertGreater(lo.PAR_TABLE[0][114][160], 35.0)
        self.assertLess(lo.PAR_TABLE[0][114][160], 65.0)

    def test_no_cliff_at_the_anchor_seam(self):
        # The correction applies at 6 balls and not at 7, so the join is where a
        # discontinuity would show. A 45-point step is exactly the artefact this
        # work exists to remove.
        #
        # Measured against the neighbouring steps rather than a fixed number: an
        # extra delivery is genuinely worth a lot when the ask is steep, so "12
        # points" is not by itself evidence of a seam. What would be evidence is
        # the seam step standing out from the steps either side of it.
        n = lo.ANCHOR_BALLS
        for edge in lo.TABLE_EDGES:
            for r in range(1, 60):
                row = lo.PAR_TABLE[edge]
                seam = row[n + 1][r] - row[n][r]
                neighbours = max(row[n][r] - row[n - 1][r],
                                 row[n + 2][r] - row[n + 1][r])
                # The floor keeps the ratio rule from firing where both steps
                # are small. The observed worst is 10.4, in the most
                # extrapolated bucket at a near-certain ask; 12 is still four
                # times smaller than the 45-point drop this guards against.
                self.assertLessEqual(
                    seam, max(neighbours * 1.6, 12.0),
                    msg="edge %d, %d needed: seam step %+.1f against "
                        "neighbouring steps of %+.1f" % (edge, r, seam, neighbours))

    def test_a_thinning_batting_side_is_priced(self):
        # Flat batting depth was what made a full-innings chase read as 83%.
        self.assertGreater(lo.WICKET_DEGRADE, 0.0)
        self.assertGreater(
            lo.target_probabilities(60, 60, wickets_in_hand=8)["win"],
            lo.target_probabilities(60, 60, wickets_in_hand=1)["win"])


class BuildCostTests(unittest.TestCase):
    def test_importing_the_module_does_not_build_the_table(self):
        # The first cut of this shipped a 2.13s import — paid by the bot, every
        # worker and every test run — because the surface was inducted at module
        # load. It is deterministic, so it is built once per process on demand.
        import importlib
        import time
        mod = importlib.import_module("engine.chase_model")
        importlib.reload(mod)
        t0 = time.time()
        importlib.reload(mod)
        self.assertLess(time.time() - t0, 0.5)

    def test_the_table_is_built_once_and_cached(self):
        first = lo.tables()
        self.assertIs(lo.tables(), first)
        self.assertIs(lo.PAR_TABLE, first[0])


# --------------------------------------------------------------------------- #
# The target model
# --------------------------------------------------------------------------- #

class TargetModelTests(unittest.TestCase):
    ELITE = dict(striker_bat=95, non_striker_bat=95, bowler_rating=80,
                 wickets_in_hand=6, striker_balls=12)
    PAR = dict(striker_bat=75, non_striker_bat=75, bowler_rating=75,
               wickets_in_hand=6, striker_balls=12)
    WEAK = dict(striker_bat=60, non_striker_bat=60, bowler_rating=85,
                wickets_in_hand=6, striker_balls=12)

    def test_the_headline_spec(self):
        # 4 to 6 needed with high-rated batsmen: 98% bat / ~1% Super Over /
        # ~1% bowl. This is the number the whole model was built around.
        for need in (4, 5, 6):
            p = lo.target_probabilities(need, 6, **self.ELITE)
            self.assertAlmostEqual(p["win"], 98.0, delta=1.0)
            self.assertLessEqual(p["tie"], 2.0)
            self.assertLessEqual(p["lose"], 2.5)

    def test_bands_land_where_the_spec_says(self):
        cases = [
            (self.ELITE, 12, 71.0, 6.0), (self.ELITE, 18, 25.0, 5.0),
            (self.ELITE, 24, 6.5, 3.0),
            (self.PAR, 5, 94.0, 2.5), (self.PAR, 12, 60.0, 5.0),
            (self.PAR, 18, 20.0, 4.0), (self.PAR, 24, 5.0, 2.5),
            (self.WEAK, 5, 87.0, 3.0), (self.WEAK, 12, 40.0, 6.0),
            (self.WEAK, 24, 1.5, 1.5),
        ]
        for profile, need, want, tol in cases:
            got = lo.target_probabilities(need, 6, **profile)["win"]
            self.assertAlmostEqual(got, want, delta=tol,
                                   msg="%d off 6 -> %.1f, wanted ~%.1f" % (need, got, want))

    def test_parts_always_sum_to_a_hundred(self):
        for need in (1, 5, 13, 22, 45):
            for balls in range(1, 7):
                p = lo.target_probabilities(need, balls, **self.PAR)
                self.assertAlmostEqual(p["win"] + p["tie"] + p["lose"], 100.0, places=6)

    def test_monotone_in_the_things_it_claims_to_depend_on(self):
        base = lo.target_probabilities(12, 6, **self.PAR)["win"]
        better_bat = lo.target_probabilities(
            12, 6, **dict(self.PAR, striker_bat=90))["win"]
        better_bowl = lo.target_probabilities(
            12, 6, **dict(self.PAR, bowler_rating=90))["win"]
        fewer_wkts = lo.target_probabilities(
            12, 6, **dict(self.PAR, wickets_in_hand=1))["win"]
        self.assertGreater(better_bat, base)
        self.assertLess(better_bowl, base)
        self.assertLess(fewer_wkts, base)

    def test_traits_and_conditions_move_it_the_right_way(self):
        fin = [{"effect_key": "bat_finisher", "level": 5}]
        death = [{"effect_key": "bowl_death", "level": 5}]
        base = lo.target_probabilities(12, 6, **self.PAR)["win"]
        self.assertGreater(
            lo.target_probabilities(12, 6, striker_traits=fin, **self.PAR)["win"], base)
        self.assertLess(
            lo.target_probabilities(12, 6, bowler_traits=death, **self.PAR)["win"], base)
        self.assertGreater(
            lo.target_probabilities(12, 6, conditions={"dew": "Heavy"},
                                    **self.PAR)["win"], base)
        self.assertGreater(
            lo.target_probabilities(12, 6, part_time_bowler=True, **self.PAR)["win"], base)

    def test_shift_is_clamped_so_the_table_stays_the_authority(self):
        everything = lo.situational_shift(
            wickets_in_hand=6, striker_balls=40,
            striker_traits=[{"effect_key": k} for k in lo.BAT_CLUTCH_TRAITS],
            pitch="Flat", momentum=3.0, pressure=-3.0,
            conditions={"dew": "Heavy"}, fielding_quality=20,
            part_time_bowler=True)
        self.assertLessEqual(everything, lo.SHIFT_CLAMP + 1e-9)
        nothing = lo.situational_shift(
            wickets_in_hand=0, striker_balls=0,
            bowler_traits=[{"effect_key": k} for k in lo.BOWL_DEATH_TRAITS],
            pitch="Dusty", momentum=-3.0, pressure=3.0, fielding_quality=95)
        self.assertGreaterEqual(nothing, -lo.SHIFT_CLAMP - 1e-9)

    def test_an_impossible_ask_is_gone_not_merely_unlikely(self):
        # 38 needed off 6 cannot be done off the bat; 37 can (six no-ball six).
        self.assertEqual(lo.target_probabilities(38, 6)["win"], 0.0)
        self.assertGreater(lo.target_probabilities(36, 6)["win"], 0.0)
        self.assertEqual(lo.target_probabilities(8, 1)["win"], 0.0)

    def test_scores_level_at_the_end_is_a_tie_not_a_loss(self):
        self.assertEqual(lo.target_probabilities(1, 0)["tie"], 100.0)
        self.assertEqual(lo.target_probabilities(2, 0)["lose"], 100.0)


# --------------------------------------------------------------------------- #
# Tilt, DP, solver
# --------------------------------------------------------------------------- #

class TiltTests(unittest.TestCase):
    def test_aggression_costs_dot_balls_and_wickets(self):
        # THE fix. The old clutch hook raised Six while LOWERING Dot, which is
        # free money and is where the inflated win rates came from. A batter
        # swinging for six misses more often, so intent raises Dot too.
        calm = lo.tilt(BASE_WEIGHTS, intent=0.0, execution=0.0)
        hard = lo.tilt(BASE_WEIGHTS, intent=1.0, execution=0.0)
        self.assertGreater(hard["Six"], calm["Six"])
        self.assertGreater(hard["Four"], calm["Four"])
        self.assertGreater(hard["Dot"], calm["Dot"])
        self.assertGreater(hard["Wicket"], calm["Wicket"])
        self.assertLess(hard["Single"], calm["Single"])

    def test_cruising_milks_singles_and_protects_wickets(self):
        calm = lo.tilt(BASE_WEIGHTS, intent=0.0)
        cruise = lo.tilt(BASE_WEIGHTS, intent=lo.INTENT_MIN)
        self.assertGreater(cruise["Single"], calm["Single"])
        self.assertLess(cruise["Six"], calm["Six"])
        self.assertLess(cruise["Wicket"], calm["Wicket"])

    def test_execution_moves_every_outcome_the_same_way(self):
        # Monotonicity of P(win) in execution is the precondition that makes the
        # bisection in solve_execution valid, so it gets asserted directly.
        good = lo.tilt(BASE_WEIGHTS, execution=1.0)
        bad = lo.tilt(BASE_WEIGHTS, execution=-1.0)
        for up in ("Six", "Four", "Single", "Double"):
            self.assertGreater(good[up], bad[up])
        for down in ("Dot", "Wicket"):
            self.assertLess(good[down], bad[down])

    def test_intent_follows_the_ask(self):
        self.assertAlmostEqual(lo.intent_for(2, 6), lo.INTENT_MIN, places=6)
        self.assertGreater(lo.intent_for(12, 6), 0.5)
        self.assertAlmostEqual(lo.intent_for(18, 6), lo.INTENT_MAX, places=6)

    def test_weights_stay_positive_and_normalised(self):
        for i in (-0.35, 0.0, 1.0):
            for e in (-lo.EXEC_LIMIT, 0.0, lo.EXEC_LIMIT):
                d = lo.tilt(BASE_WEIGHTS, i, e)
                self.assertAlmostEqual(sum(d.values()), 1.0, places=9)
                for v in d.values():
                    self.assertGreaterEqual(v, 0.0)

    def test_wicket_share_cap_mirrors_the_engine_and_its_free_hit_exemption(self):
        heavy = dict(BASE_WEIGHTS, Wicket=400.0)
        self.assertLessEqual(lo._normalise_capped(heavy)["Wicket"],
                             lo.WICKET_SHARE_CAP + 1e-9)
        self.assertGreater(lo._normalise_capped(heavy, free_hit=True)["Wicket"],
                           lo.WICKET_SHARE_CAP)


class DPTests(unittest.TestCase):
    DIST = lo._normalise_capped(BASE_WEIGHTS)

    def _flat(self, dist):
        return lambda r, b: dist

    def test_matches_a_brute_force_simulation(self):
        # The DP is load-bearing for everything else, so cross-check it against
        # the thing it is a shortcut for.
        for runs, balls, wkts in ((5, 6, 5), (12, 6, 5), (9, 3, 2), (2, 1, 5),
                                  (18, 4, 3)):
            p_win, p_tie = lo.solve_over(self._flat(self.DIST), runs, balls, wkts)
            keys = list(lo.OUTCOMES)
            weights = [self.DIST[k] for k in keys]
            rng = random.Random(1234 + runs * 31 + balls)
            n = 40000
            wins = ties = 0
            for _ in range(n):
                r, b, w = runs, balls, wkts
                while True:
                    if r <= 0:
                        wins += 1
                        break
                    if b <= 0 or w <= 0:
                        ties += 1 if r == 1 else 0
                        break
                    o = rng.choices(keys, weights=weights)[0]
                    if o == "Wicket":
                        w -= 1
                        b -= 1
                    elif o == "Extras":
                        r -= 1
                        if rng.random() >= lo.EXTRA_FREE_BALL_SHARE:
                            b -= 1
                    else:
                        r -= lo.OUTCOME_RUNS[o]
                        b -= 1
            self.assertAlmostEqual(wins / n, p_win, delta=0.015,
                                   msg="win %d off %d, %d in hand" % (runs, balls, wkts))
            self.assertAlmostEqual(ties / n, p_tie, delta=0.012,
                                   msg="tie %d off %d, %d in hand" % (runs, balls, wkts))

    def test_win_probability_rises_with_execution(self):
        for runs, balls in ((5, 6), (12, 6), (18, 6), (7, 3)):
            prev = -1.0
            for ex in (-1.6, -0.8, 0.0, 0.8, 1.6):
                p = lo.solve_over(
                    lambda r, b, _e=ex: lo.tilt(self.DIST, lo.intent_for(r, b), _e),
                    runs, balls, 5)[0]
                self.assertGreater(p, prev, msg="%d off %d at %.1f" % (runs, balls, ex))
                prev = p

    def test_win_and_tie_are_a_valid_split(self):
        for runs in range(1, 25):
            for balls in range(1, 7):
                w, t = lo.solve_over(self._flat(self.DIST), runs, balls, 4)
                self.assertGreaterEqual(w, 0.0)
                self.assertGreaterEqual(t, 0.0)
                self.assertLessEqual(w + t, 1.0 + 1e-9)

    def test_terminal_states(self):
        self.assertEqual(lo.solve_over(self._flat(self.DIST), 0, 6, 5), (1.0, 0.0))
        self.assertEqual(lo.solve_over(self._flat(self.DIST), 1, 0, 5), (0.0, 1.0))
        self.assertEqual(lo.solve_over(self._flat(self.DIST), 4, 0, 5), (0.0, 0.0))
        self.assertEqual(lo.solve_over(self._flat(self.DIST), 1, 6, 0), (0.0, 1.0))


class ContinuationTests(unittest.TestCase):
    PAR = dict(runs_needed=12, balls_left=6, striker_bat=75, non_striker_bat=75,
               next_bat=75, bowler_rating=75, wickets_in_hand=6, striker_balls=0)

    def test_balls_faced_advances_with_the_over(self):
        # The bug this guards: a frozen striker_balls left every continuation
        # carrying the current striker's new-batter penalty while the ball that
        # actually followed did not, so the controller planned against a future
        # worse than the one it got and the over finished well above target.
        cont = lo.continuation(dict(self.PAR))
        here = cont(12, 6, 6)[0]
        one_later = cont(12, 5, 6)[0]
        # Same ask, one ball fewer, but the striker is no longer brand new — so
        # this must not simply be the same number minus a ball's worth.
        cold = lo.target_probabilities(12, 5, striker_bat=75, non_striker_bat=75,
                                       bowler_rating=75, wickets_in_hand=6,
                                       striker_balls=0)["win"] / 100.0
        self.assertGreater(one_later, cold)
        self.assertLess(one_later, here)

    def test_a_wicket_brings_the_next_batter_in_cold(self):
        rabbit = lo.continuation(dict(self.PAR, next_bat=25))
        another = lo.continuation(dict(self.PAR, next_bat=90))
        self.assertLess(rabbit(12, 5, 5, new_batter=True)[0],
                        another(12, 5, 5, new_batter=True)[0])
        # ...and only on the branch where somebody actually got out.
        self.assertEqual(rabbit(12, 5, 5)[0], another(12, 5, 5)[0])

    def test_terminal_states(self):
        cont = lo.continuation(dict(self.PAR))
        self.assertEqual(cont(0, 3, 5), (1.0, 0.0))
        self.assertEqual(cont(1, 0, 5), (0.0, 1.0))
        self.assertEqual(cont(4, 0, 5), (0.0, 0.0))
        self.assertEqual(cont(1, 3, 0), (0.0, 1.0))


class SolverTests(unittest.TestCase):
    DIST = lo._normalise_capped(BASE_WEIGHTS)
    PAR = dict(runs_needed=12, balls_left=6, striker_bat=75, non_striker_bat=75,
               next_bat=75, bowler_rating=75, wickets_in_hand=6, striker_balls=4)

    def _cont(self, **over):
        return lo.continuation(dict(self.PAR, **over))

    def _outlook(self, cont, r, b, w, ex):
        d = lo.tilt(self.DIST, lo.intent_for(r, b), ex)
        return lo.one_ball_outlook(cont, d, r, b, w)[0]

    def test_hits_a_reachable_target(self):
        for r, b, want in ((12, 6, 0.55), (5, 6, 0.94), (18, 6, 0.22),
                           (9, 4, 0.40)):
            cont = self._cont(runs_needed=r, balls_left=b)
            ex, _ = lo.solve_execution(self.DIST, cont, r, b, 5, want)
            self.assertAlmostEqual(self._outlook(cont, r, b, 5, ex), want,
                                   delta=lo.EXEC_DEADBAND + 0.01,
                                   msg="%d off %d" % (r, b))

    def test_clamps_rather_than_chasing_an_unreachable_target(self):
        cont = self._cont(runs_needed=30)
        ex, _ = lo.solve_execution(self.DIST, cont, 30, 6, 5, 0.95)
        self.assertAlmostEqual(ex, lo.EXEC_LIMIT, places=6)
        cont = self._cont(runs_needed=1)
        ex, _ = lo.solve_execution(self.DIST, cont, 1, 6, 5, 0.01)
        self.assertAlmostEqual(ex, -lo.EXEC_LIMIT, places=6)

    def test_stands_down_inside_the_deadband(self):
        cont = self._cont()
        natural = self._outlook(cont, 12, 6, 5, 0.0)
        ex, _ = lo.solve_execution(self.DIST, cont, 12, 6, 5, natural)
        self.assertEqual(ex, 0.0)

    def test_no_single_outcome_is_moved_beyond_the_realism_bound(self):
        # The controller is allowed to miss its target rather than turn the
        # delivery into something that is not cricket any more.
        for ex in (-4.0, -lo.EXEC_LIMIT, 0.0, lo.EXEC_LIMIT, 4.0):
            for o in lo.OUTCOMES:
                m = lo._exec_multiplier(o, ex)
                self.assertGreaterEqual(m, lo.EXEC_MULT_FLOOR - 1e-9)
                self.assertLessEqual(m, lo.EXEC_MULT_CEIL + 1e-9)

    def test_hook_declines_when_there_is_nothing_to_steer(self):
        self.assertIsNone(lo.make_chase_hook(dict(self.PAR, runs_needed=0)))
        self.assertIsNone(lo.make_chase_hook(dict(self.PAR, balls_left=0)))
        self.assertIsNone(lo.make_chase_hook(dict(self.PAR, wickets_in_hand=0)))
        self.assertIsNone(lo.make_chase_hook(dict(self.PAR, runs_needed=99)))

    def test_hook_returns_usable_weights(self):
        hook = lo.make_chase_hook(dict(self.PAR))
        out = hook(dict(BASE_WEIGHTS))
        self.assertEqual(set(out), set(BASE_WEIGHTS))
        for v in out.values():
            self.assertGreaterEqual(v, 0.0)
        self.assertGreater(sum(out.values()), 0.0)

    def test_hook_steers_the_delivery_onto_the_model(self):
        # The closed-loop claim, checked one ball at a time: after the hook, the
        # delivery's own outlook against the model equals what the model asked
        # for. Everything the Monte Carlo grid measures follows from this
        # holding at every ball of the over.
        for r, b in ((5, 6), (12, 6), (12, 3), (18, 4), (3, 1)):
            inputs = dict(self.PAR, runs_needed=r, balls_left=b)
            want = lo.target_probabilities(
                **{k: v for k, v in inputs.items() if k != "next_bat"})["win"] / 100.0
            out = lo.make_chase_hook(inputs)(dict(BASE_WEIGHTS))
            got = lo.one_ball_outlook(
                lo.continuation(inputs), lo._normalise_capped(out), r, b,
                inputs["wickets_in_hand"])[0]
            self.assertAlmostEqual(got, want, delta=0.03,
                                   msg="%d off %d: steered to %.3f, model %.3f"
                                       % (r, b, got, want))

    def test_the_model_never_touches_the_rng(self):
        # engine.chase_model must not draw: one extra random() call would shift
        # the stream for every seeded test in the suite and silently
        # re-baseline dozens of unrelated ones.
        names = ("random", "choice", "choices", "randint", "uniform")
        originals = {n: getattr(random, n) for n in names}
        calls = []

        def _spy(name, fn):
            def _wrapped(*a, **k):
                calls.append(name)
                return fn(*a, **k)
            return _wrapped

        for n in names:
            setattr(random, n, _spy(n, originals[n]))
        try:
            lo.make_chase_hook(dict(self.PAR))(dict(BASE_WEIGHTS))
            lo.target_probabilities(12, 6, striker_bat=90)
        finally:
            for n, fn in originals.items():
                setattr(random, n, fn)
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def _mk(rid, name, cat, bat, bowl, style="Fast", traits=None):
    d = {"roster_id": rid, "player_id": rid, "name": name, "rating": max(bat, bowl),
         "category": cat, "bat_rating": bat, "bowl_rating": bowl,
         "bowl_style": style, "bowl_hand": "Right", "bat_hand": "Right"}
    if traits:
        d["traits"] = traits
    return d


def chase_state(bat=75, bowl=75, need=12, clutch=True, pitch="Even",
                    wickets=4, over=20, striker_traits=None, bowler_traits=None):
    """A 2nd-innings state parked at the start of the final over of a chase."""
    bat_xi = [_mk(1, "B1", "Batsman", bat, 30, traits=striker_traits)] \
        + [_mk(i, "B%d" % i, "Batsman", bat, 30) for i in range(2, 8)] \
        + [_mk(i, "AR%d" % i, "All-rounder", 60, 70, "Off spin") for i in range(8, 12)]
    bowl_xi = [_mk(101, "W1", "Bowler", 30, bowl, traits=bowler_traits)] \
        + [_mk(100 + i, "W%d" % i, "Bowler", 30, bowl) for i in range(2, 7)] \
        + [_mk(100 + i, "BAR%d" % i, "All-rounder", 60, 70, "Leg spin")
           for i in range(7, 12)]
    s = cm.build_cipl_state(1, 20, 10, 20, 111, 222, bat_xi, bowl_xi,
                            "A", "B", -100, pitch, False)
    if clutch:
        s["clutch_finale"] = True
    s["innings"] = 2
    s["current_over"] = over
    s["current_ball"] = 0
    s["total_runs"] = 140
    s["target"] = 140 + need
    s["total_wickets"] = wickets
    s["current_bowler"] = bowl_xi[0]
    s["bowling_approach"] = "balanced"
    s["batting_approach"] = "aggressive"
    return s


def play_out(s):
    """Run a state to the end of the innings through the real over loop.

    Deliberately terminated on ``is_innings_over`` rather than on the over
    number: ``simulate_over`` does not advance the pointer past the last over,
    so a ``current_over <= 20`` loop silently re-bowls a finished innings. That
    mistake produced "36 off 12 wins 100% of the time" during development, which
    is why every multi-over measurement here asserts balls consumed.
    """
    start = cm.balls_bowled(s)
    guard = 0
    while not cm.is_innings_over(s):
        guard += 1
        if guard > 25:
            raise AssertionError("over loop did not terminate")
        s["current_bowler"] = s["bowl_xi"][s["current_over"] % 2]
        s["batting_approach"] = "aggressive"
        s["bowling_approach"] = "balanced"
        cm.simulate_over(s)
    return cm.balls_bowled(s) - start


def monte_overs(n, bat, bowl, need, overs, **kw):
    """(win%, tie%, sixes/chase, wickets/chase, balls/chase) over ``overs``."""
    wins = ties = sixes = wkts = balls = 0
    for k in range(n):
        random.seed(k * 104729 + need)
        s = chase_state(bat=bat, bowl=bowl, need=need, over=21 - overs, **kw)
        start, w0 = s["total_runs"], s["total_wickets"]
        balls += play_out(s)
        got = s["total_runs"] - start
        if got >= need:
            wins += 1
        elif got == need - 1:
            ties += 1
        wkts += s["total_wickets"] - w0
        sixes += sum(1 for b in (s.get("ball_history") or [])
                     if int(b.get("runs", 0) or 0) == 6)
    return (100.0 * wins / n, 100.0 * ties / n,
            sixes / float(n), wkts / float(n), balls / float(n))


def monte(n=300, **kw):
    """(win rate, tie rate) over ``n`` seeded simulations of the final over."""
    wins = ties = 0
    for k in range(n):
        random.seed(k * 7919 + kw.get("need", 12))
        s = chase_state(**kw)
        start = s["total_runs"]
        need = s["target"] - start
        cm.simulate_over(s)
        got = s["total_runs"] - start
        if got >= need:
            wins += 1
        elif got == need - 1:
            ties += 1
    return 100.0 * wins / n, 100.0 * ties / n


class ActivationTests(unittest.TestCase):
    def test_active_in_the_final_over_of_a_live_chase(self):
        self.assertTrue(cm.chase_model_active(chase_state(over=20)))

    def test_active_across_the_whole_death(self):
        # Five overs, not one: overs 16-20 of a live chase.
        for over in (16, 17, 18, 19, 20):
            self.assertTrue(cm.chase_model_active(chase_state(over=over)),
                            msg="over %d" % over)

    def test_inactive_before_the_death(self):
        self.assertFalse(cm.chase_model_active(chase_state(over=15)))
        self.assertFalse(cm.chase_model_active(chase_state(over=8)))

    def test_inactive_in_the_first_innings(self):
        s = chase_state()
        s["innings"] = 1
        s["target"] = None
        self.assertFalse(cm.chase_model_active(s))

    def test_inactive_once_the_chase_is_done(self):
        s = chase_state(need=12)
        s["total_runs"] = s["target"]
        self.assertFalse(cm.chase_model_active(s))

    def test_inactive_when_all_out(self):
        s = chase_state()
        s["total_wickets"] = s.get("wicket_limit", cm.WICKET_LIMIT)
        self.assertFalse(cm.chase_model_active(s))

    def test_window_follows_the_format_not_the_over_number(self):
        # The Hundred's unit is 5 balls, so its window is the last 5.
        s = chase_state()
        s["ball_format"] = "The100"
        s["current_over"] = 20
        s["current_ball"] = 0
        self.assertEqual(cm.balls_per_unit(s), 5)
        self.assertEqual(cm.chase_balls_left(s), 5)
        self.assertTrue(cm.chase_model_active(s))

    def test_respects_a_non_default_wicket_limit(self):
        s = chase_state(wickets=2)
        s["wicket_limit"] = 2
        self.assertFalse(cm.chase_model_active(s))

    def test_kill_switch(self):
        s = chase_state()
        orig = cm.CHASE_MODEL_CONTROL
        cm.CHASE_MODEL_CONTROL = False
        try:
            self.assertFalse(cm.chase_model_active(s))
        finally:
            cm.CHASE_MODEL_CONTROL = orig


class LiveWinProbabilityTests(unittest.TestCase):
    def test_chase_chance_now_defers_to_the_controller_in_the_window(self):
        info = cm.chase_chance_now(chase_state(need=12, over=20))
        self.assertEqual(info.get("source"), "chase_model")
        # The chart has to show the number the simulation is being steered onto.
        want = lo.target_probabilities(
            **cm._chase_inputs(chase_state(need=12, over=20)))["win"]
        self.assertAlmostEqual(info["chasing_chance"], want, delta=1.5)
        self.assertEqual(info["chasing_chance"] + info["defending_chance"], 100)

    def test_the_model_supplies_the_win_pct_for_the_whole_innings(self):
        # Wider than the steer window on purpose. The old matrix pinned any ask
        # above 51 runs at 20% from ball one, so a side chasing 161 with ten
        # wickets standing read as a heavy underdog for fifteen overs. The
        # chart has no reason to fall back to that.
        for over in (1, 8, 15, 18, 20):
            info = cm.chase_chance_now(chase_state(need=60, over=over))
            self.assertEqual(info.get("source"), "chase_model",
                             msg="over %d" % over)
            self.assertIn("base_chasing_chance", info)

    def test_a_routine_chase_is_not_a_lost_cause_on_the_chart(self):
        s = chase_state(need=160, over=1, wickets=0)
        s["total_runs"] = 0
        s["target"] = 160
        self.assertGreater(cm.chase_chance_now(s)["chasing_chance"], 30)


class ScopeTests(unittest.TestCase):
    """The scripted 'dramatic finish' no longer overrides the last over — in
    either mode. Ratings decide it, which is the whole point of the change."""

    def _armed(self, clutch, over=20, finish_ball=120):
        s = chase_state(need=18, clutch=clutch, over=over)
        s["scenario"] = {"type": "controlled_finish", "active": True,
                         "finish_ball": finish_ball, "finale_script": None,
                         "finale_ball_index": 0, "convergence_logged": False,
                         "endgame_checked_overs": []}
        return s

    def _count_overrides(self, state):
        calls = [0]
        orig = cm.ScenarioEngine.get_override_outcome

        def wrap(self, b, bo):
            calls[0] += 1
            return orig(self, b, bo)

        cm.ScenarioEngine.get_override_outcome = wrap
        try:
            random.seed(5)
            cm.simulate_over(state)
        finally:
            cm.ScenarioEngine.get_override_outcome = orig
        return calls[0]

    def test_challenge_league_last_over_is_not_scripted(self):
        self.assertEqual(self._count_overrides(self._armed(clutch=False)), 0)

    def test_letsplay_last_over_is_not_scripted(self):
        self.assertEqual(self._count_overrides(self._armed(clutch=True)), 0)

    def test_scripted_finales_still_run_before_the_death(self):
        # Outside the model's five-over window the scenario engine is untouched,
        # so a finale that closes out before over 16 still drives its own over.
        s = self._armed(clutch=False, over=13, finish_ball=78)
        self.assertGreater(self._count_overrides(s), 0)


class CalibrationTests(unittest.TestCase):
    """The actual claim: the final over lands on real-cricket numbers."""

    PROFILES = {"elite": (95, 80), "par": (75, 75), "weak": (60, 85)}
    N = 800 if SLOW else 300
    # The controller lands on the model within about 2 points at rating parity.
    # At the extremes the engine's own natural death weights are far enough from
    # real last-over results that the per-outcome realism bound (see
    # last_over.EXEC_MULT_CEIL) runs out before the correction is complete, and
    # roughly 8 points of residual is left on purpose — the alternative is a
    # delivery whose six weight has been multiplied by five, which is no longer
    # the game underneath it. The tolerance also has to cover ~3 standard errors
    # of the sample itself (about 9 points at n=300, 5 at n=800).
    TOL = 10.0 if SLOW else 13.0

    def _target(self, profile, need):
        bat, bowl = self.PROFILES[profile]
        return lo.target_probabilities(
            need, 6, striker_bat=bat, non_striker_bat=bat, bowler_rating=bowl,
            wickets_in_hand=6, striker_balls=0)

    def _check(self, profile, need, clutch=True):
        bat, bowl = self.PROFILES[profile]
        win, tie = monte(self.N, bat=bat, bowl=bowl, need=need, clutch=clutch)
        want = self._target(profile, need)["win"]
        self.assertAlmostEqual(
            win, want, delta=self.TOL,
            msg="%s needing %d off 6: simulated %.1f%%, model says %.1f%%"
                % (profile, need, win, want))
        return win, tie

    def test_band_4_to_6_with_high_rated_batsmen(self):
        # The headline case: high-rated batsmen needing 4 to 6 off the last over
        # win it nearly every time, and the rare failure is about as likely to
        # be a Super Over as a defeat.
        win, tie = self._check("elite", 5)
        self.assertGreater(win, 92.0)
        self.assertLess(tie, 5.0)

    def test_band_7_to_12(self):
        self._check("par", 9)
        self._check("par", 12)

    def test_band_13_to_15(self):
        self._check("par", 14)

    def test_band_16_to_20(self):
        self._check("par", 18)

    def test_band_21_to_28(self):
        win, _ = self._check("par", 24)
        self.assertLess(win, 15.0)

    def test_band_29_plus_is_a_miracle(self):
        win, _ = monte(self.N, bat=95, bowl=80, need=30)
        self.assertLess(win, 8.0)

    def test_weak_batting_still_gets_a_gettable_target(self):
        # Tailenders do get 5 off 6. The old engine had them at 70%.
        win, _ = self._check("weak", 5)
        self.assertGreater(win, 70.0)

    def test_elite_batting_no_longer_walks_a_big_ask(self):
        # Was 34.2% before the controller; real T20 is about 5%.
        win, _ = self._check("elite", 24)
        self.assertLess(win, 16.0)

    def test_the_two_modes_now_agree(self):
        # 12 off 6 at rating parity used to be 67.8% in /letsplay and 47.8% in
        # Challenge League. Same situation, same game, one number — and since
        # the controller is the only thing steering the over now, the two are
        # the same code path and should agree exactly on the same seeds.
        for need in (5, 12, 18):
            lp = monte(self.N, bat=75, bowl=75, need=need, clutch=True)
            cl = monte(self.N, bat=75, bowl=75, need=need, clutch=False)
            self.assertEqual(lp, cl, msg="modes diverge at %d off 6" % need)

    def test_ratings_still_decide_the_finish(self):
        weak, _ = monte(self.N, bat=45, bowl=85, need=12)
        strong, _ = monte(self.N, bat=95, bowl=70, need=12)
        self.assertGreater(strong, weak + 20.0)

    def test_super_over_rate_tracks_the_model(self):
        # Ties are conditional on a live last over, not on all T20 matches, so
        # the honest figure peaks in the middle bands where the ask regularly
        # comes down to two or three off the final ball. What matters is that
        # the simulation and the model agree on it, and that a comfortable ask
        # rarely produces one.
        for need in (5, 12, 18):
            _, tie = monte(self.N, bat=75, bowl=75, need=need)
            want = self._target("par", need)["tie"]
            self.assertLess(tie, want + 6.0,
                            msg="tie at %d off 6: %.1f%% vs model %.1f%%"
                                % (need, tie, want))
        _, easy = monte(self.N, bat=95, bowl=80, need=5)
        self.assertLess(easy, 5.0)

    def test_multi_over_chases_track_the_model(self):
        # The model steers five overs now, not one, so the claim has to hold
        # over the whole window and not just at the wire.
        for overs, need, real in ((2, 24, 33), (3, 36, 33), (5, 60, 33)):
            win, _, _, _, balls = monte_overs(self.N, 75, 75, need, overs)
            self.assertLessEqual(balls, overs * 6,
                                 msg="%d overs consumed %.1f balls" % (overs, balls))
            want = lo.target_probabilities(
                need, overs * 6, striker_bat=75, non_striker_bat=75,
                bowler_rating=75, wickets_in_hand=6, striker_balls=0)["win"]
            self.assertAlmostEqual(
                win, want, delta=self.TOL,
                msg="%d off %d: simulated %.1f%%, model %.1f%% (real ~%d%%)"
                    % (need, overs * 6, win, want, real))

    def test_drama_adds_events_without_moving_the_odds(self):
        # The whole point of the dial. Paired on identical seeds: the win rate
        # must not move (the controller re-solves against the shaped
        # distribution) while sixes and wickets both rise. A drama term that
        # shifted the win rate would be a bug, not a feature.
        cases = [(12, 1), (18, 1), (24, 2), (40, 5)]
        original = lo.DRAMA
        try:
            lo.DRAMA = 0.0
            off = [monte_overs(self.N, 75, 75, n, o) for n, o in cases]
            lo.DRAMA = 0.25
            on = [monte_overs(self.N, 75, 75, n, o) for n, o in cases]
        finally:
            lo.DRAMA = original
        shift = sum(b[0] - a[0] for a, b in zip(off, on)) / len(cases)
        self.assertLess(abs(shift), 4.0,
                        msg="drama moved the win rate by %+.1f points" % shift)
        self.assertGreater(sum(b[2] for b in on), sum(a[2] for a in off))
        self.assertGreater(sum(b[3] for b in on), sum(a[3] for a in off))

    def test_drama_can_be_turned_off(self):
        self.assertEqual(lo.drama_strength(6, 30), lo.DRAMA)
        self.assertLess(lo.drama_strength(30, 30), lo.DRAMA)
        original = lo.DRAMA
        try:
            lo.DRAMA = 0.0
            self.assertEqual(lo.drama_strength(6, 30), 0.0)
        finally:
            lo.DRAMA = original

    @unittest.skipUnless(SLOW, "set SLOW_TESTS=1 for the full grid")
    def test_full_band_grid(self):
        for profile in self.PROFILES:
            for need in (5, 9, 12, 14, 18, 24):
                for clutch in (True, False):
                    self._check(profile, need, clutch)


if __name__ == "__main__":
    unittest.main()
