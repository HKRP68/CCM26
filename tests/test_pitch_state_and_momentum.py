"""Dynamic Pitch State (section 2) and Momentum / Pressure / MPI (8A-8C).

Both modules are pure table lookups, so these tests are about the *rules* rather
than about the simulation: that the pitch changes for the chase and not before
it, that the doc's over windows land on the doc's overs and still work in a
short format, and that the two accumulators cannot be gamed into rewarding the
side they are supposed to punish.
"""

import unittest

from engine import approach_modifiers
from engine import momentum as M
from engine import pitch_state as ps


class WearTests(unittest.TestCase):
    """The bug section 2 exists to fix: CIPL measured wear per innings, so the
    chase was bowled on a brand-new square every match."""

    def test_wear_runs_across_the_match_not_the_innings(self):
        self.assertEqual(ps.carry_wear(1, 0, 120), 0.0)
        self.assertEqual(ps.carry_wear(1, 120, 120), 0.5)
        self.assertEqual(ps.carry_wear(2, 0, 120), 0.5)
        self.assertEqual(ps.carry_wear(2, 120, 120), 1.0)

    def test_the_chase_never_starts_on_a_fresh_pitch(self):
        self.assertGreater(ps.carry_wear(2, 0, 120), ps.carry_wear(1, 100, 120))

    def test_wear_is_monotonic_and_bounded(self):
        seen = [ps.carry_wear(inn, b, 120)
                for inn in (1, 2) for b in range(0, 121, 10)]
        self.assertEqual(seen, sorted(seen))
        self.assertTrue(all(0.0 <= w <= 1.0 for w in seen))

    def test_junk_inputs_do_not_raise(self):
        self.assertEqual(ps.carry_wear(None, None, None), 0.0)
        self.assertEqual(ps.carry_wear(2, 999, 120), 1.0)
        self.assertEqual(ps.carry_wear(3, 60, 120), 1.0)


class EvolutionTests(unittest.TestCase):
    def test_every_configured_pitch_has_a_rule(self):
        for pitch in ("Flat", "Dead", "Hard", "Even", "Bouncy", "Dry",
                      "Green", "Dusty"):
            self.assertIn(pitch, ps.INNINGS2_EVOLUTION, pitch)
            self.assertTrue(ps.evolution_note(pitch), pitch)

    def test_an_unknown_pitch_costs_nothing(self):
        self.assertEqual(ps.innings2_effects("Sandpaper", 10, 20), {})
        self.assertEqual(ps.innings2_trace("Sandpaper", 10, 20), [])
        self.assertIsNone(ps.evolution_note("Sandpaper"))

    def test_the_doc_windows_land_on_the_doc_overs(self):
        graveyard = [o for o in range(1, 21)
                     if "graveyard" in " ".join(ps.innings2_trace("Dusty", o, 20))]
        self.assertEqual(graveyard, [7, 8, 9, 10, 11, 12])
        lottery = [o for o in range(1, 21)
                   if "cracks open" in " ".join(ps.innings2_trace("Dry", o, 20))]
        self.assertEqual(lottery, [16, 17, 18, 19, 20])
        flattened = [o for o in range(1, 21)
                     if "flattened" in " ".join(ps.innings2_trace("Green", o, 20))]
        self.assertEqual(flattened, list(range(9, 21)))

    def test_windows_still_exist_in_a_short_format(self):
        """A 5-over game must not silently lose every pitch rule."""
        fired = {label for o in range(1, 6)
                 for label in ps.innings2_trace("Green", o, 5)}
        self.assertTrue(fired)

    def test_dry_is_harder_at_the_death_than_before_it(self):
        early = ps.innings2_effects("Dry", 5, 20)
        death = ps.innings2_effects("Dry", 18, 20)
        self.assertLess(death["Six"], early["Six"])
        self.assertGreater(death.get("Wicket", 1.0), 1.0)

    def test_green_flattens_out_rather_than_staying_lethal(self):
        early = ps.innings2_effects("Green", 4, 20)
        late = ps.innings2_effects("Green", 15, 20)
        self.assertLess(late["Wicket"], early["Wicket"])
        self.assertGreater(late["Four"], 1.0)

    def test_spin_gated_rules_need_a_spinner(self):
        self.assertNotIn("Wicket", ps.innings2_effects("Dusty", 3, 20, is_spin=False))
        self.assertIn("Wicket", ps.innings2_effects("Dusty", 3, 20, is_spin=True))

    def test_flat_barely_changes_which_is_the_point(self):
        for value in ps.innings2_effects("Flat", 12, 20).values():
            self.assertLess(abs(value - 1.0), 0.05)


class MomentumTests(unittest.TestCase):
    def test_the_event_table(self):
        self.assertAlmostEqual(
            M.update_momentum(0.0, {"runs": 14, "boundaries": 2}),
            2 * M.MOM_BOUNDARY + M.MOM_BIG_OVER)
        self.assertAlmostEqual(
            M.update_momentum(0.0, {"runs": 0, "is_maiden": True}),
            M.MOM_QUIET_OVER + M.MOM_MAIDEN)
        self.assertAlmostEqual(
            M.update_momentum(0.0, {"runs": 6, "longest_dot_run": 3}),
            M.MOM_DOT_RUN)

    def test_partnership_milestones_fire_once_on_the_crossing(self):
        crossed = M.update_momentum(
            0.0, {"runs": 6, "partnership_before": 48, "partnership_after": 54})
        self.assertAlmostEqual(crossed, M.MOM_PARTNERSHIP_50)
        # Already past it — no second payout.
        again = M.update_momentum(
            0.0, {"runs": 6, "partnership_before": 60, "partnership_after": 66})
        self.assertAlmostEqual(again, 0.0)
        century = M.update_momentum(
            0.0, {"runs": 6, "partnership_before": 96, "partnership_after": 102})
        self.assertAlmostEqual(century, M.MOM_PARTNERSHIP_100)

    def test_a_wicket_never_helps_the_batting_side(self):
        """The doc says "reset toward 0, then -0.4". Read literally, a side at
        -1.6 would IMPROVE to -0.4 by losing another wicket."""
        for before in (2.0, 0.5, 0.0, -0.5, -1.6):
            after = M.update_momentum(before, {"runs": 2, "wickets": 1})
            self.assertLessEqual(after, before, f"momentum rose from {before}")

    def test_momentum_is_capped_both_ways(self):
        high = 0.0
        for _ in range(20):
            high = M.update_momentum(high, {"runs": 24, "boundaries": 4})
        self.assertEqual(high, M.MOMENTUM_CAP)
        low = 0.0
        for _ in range(20):
            low = M.update_momentum(low, {"runs": 0, "is_maiden": True,
                                          "longest_dot_run": 6})
        self.assertEqual(low, -M.MOMENTUM_CAP)

    def test_a_missing_event_contributes_nothing(self):
        """Every key is optional, and that has to include ``runs``. Reading an
        absent runs count as a quiet over would drift the accumulator downward
        every over for a caller that simply cannot see the score."""
        self.assertEqual(M.update_momentum(0.4, {}), 0.4)
        self.assertEqual(M.update_momentum(None, None), 0.0)
        self.assertEqual(M.update_momentum(0.4, {"boundaries": 1}),
                         0.4 + M.MOM_BOUNDARY)

    def test_an_explicit_zero_is_still_a_quiet_over(self):
        """Absent is not the same as nought — a maiden really did cost them."""
        self.assertAlmostEqual(M.update_momentum(0.4, {"runs": 0}),
                               0.4 + M.MOM_QUIET_OVER)


class PressureTests(unittest.TestCase):
    def test_a_steep_ask_late_stacks_with_the_rate_gap(self):
        p = M.update_pressure(0.0, {"required_rr": 13.0, "current_rr": 8.0,
                                    "overs_left": 4})
        self.assertAlmostEqual(p, M.PR_RRR_GAP + M.PR_STEEP_ASK)

    def test_the_finish_line_relieves_pressure(self):
        p = M.update_pressure(1.0, {"required_rr": 6.0, "current_rr": 9.0,
                                    "runs_needed": 20, "wickets_in_hand": 6})
        self.assertAlmostEqual(p, 1.0 + M.PR_FINISH_LINE)

    def test_pressure_is_capped_both_ways(self):
        p = 0.0
        for _ in range(20):
            p = M.update_pressure(p, {"required_rr": 15.0, "current_rr": 7.0,
                                      "overs_left": 2, "wickets": 1})
        self.assertEqual(p, M.PRESSURE_CAP)


class MpiTests(unittest.TestCase):
    def test_the_docs_own_worked_example(self):
        """"Momentum +1.2 and Pressure +0.8 = Net MPI +0.4 -> Base Rate"."""
        net = M.mpi(1.2, 0.8)
        self.assertAlmostEqual(net, 0.4)
        self.assertEqual(M.mpi_effects(net), {})
        self.assertEqual(M.mpi_band(net), "Even")

    def test_the_bands_run_the_right_way(self):
        self.assertGreater(M.mpi_effects(1.8)["Six"], 1.0)
        self.assertLess(M.mpi_effects(1.8)["Wicket"], 1.0)
        self.assertLess(M.mpi_effects(-1.8)["Six"], 1.0)
        self.assertGreater(M.mpi_effects(-1.8)["Wicket"], 1.0)

    def test_scoring_rises_and_wickets_fall_monotonically_with_the_index(self):
        sixes = [M.mpi_effects(v).get("Six", 1.0) for v in (-1.8, -1.0, 0.0, 1.0, 1.8)]
        wkts = [M.mpi_effects(v).get("Wicket", 1.0) for v in (-1.8, -1.0, 0.0, 1.0, 1.8)]
        self.assertEqual(sixes, sorted(sixes))
        self.assertEqual(wkts, sorted(wkts, reverse=True))

    def test_the_neutral_band_is_genuinely_inert(self):
        for v in (-0.4, -0.1, 0.0, 0.2, 0.4):
            self.assertEqual(M.mpi_effects(v), {}, v)

    def test_every_reachable_index_has_a_band(self):
        for tenth in range(-20, 21):
            self.assertTrue(M.mpi_band(tenth / 10.0))


if __name__ == "__main__":
    unittest.main()


class LiveWiringTests(unittest.TestCase):
    """The rules above only matter if the simulation actually reads them.

    These drive real CIPL innings through ``services.cipl_match`` — the same
    engine ``/cipl``, ``/letsplay`` and both bot variants use — and assert that
    the v3.0 layers are switched on and recording.
    """

    @classmethod
    def setUpClass(cls):
        import random
        from tools import pitch_calibration as pc
        import services.cipl_match as cm
        cls.cm = cm
        random.seed(31337)
        with pc._muted_logging():
            a, b = pc.make_xi("A", 1), pc.make_xi("B", 101)
            state = cm.build_cipl_state(1, 20, 10, 20, 111, 222, a, b,
                                        "A", "B", -1, "Dry", False,
                                        ball_format="T20")
            pc._run_innings(state)
            cls.inn1_wear = ps.carry_wear(1, cm.balls_bowled(state),
                                          cm.total_balls(state))
            cm.end_first_innings(state)
            cls.wear_at_chase_start = ps.carry_wear(
                2, cm.balls_bowled(state), cm.total_balls(state))
            pc._run_innings(state)
        cls.state = state

    def test_the_chase_begins_on_a_worn_pitch(self):
        self.assertGreaterEqual(self.wear_at_chase_start, 0.5)
        self.assertGreaterEqual(self.wear_at_chase_start, self.inn1_wear)

    def test_momentum_is_tracked_over_by_over(self):
        history = self.state.get("momentum_history")
        self.assertTrue(history)
        self.assertTrue(all(-M.MOMENTUM_CAP <= v <= M.MOMENTUM_CAP
                            for v in history))
        self.assertTrue(self.state.get("inn1_momentum_history"))

    def test_pressure_is_tracked_in_the_chase_only(self):
        self.assertTrue(self.state.get("pressure_history"))
        self.assertTrue(all(-M.PRESSURE_CAP <= v <= M.PRESSURE_CAP
                            for v in self.state["pressure_history"]))

    def test_the_chase_probability_is_recorded(self):
        history = self.state.get("chase_history") or []
        self.assertTrue(history)
        for entry in history:
            self.assertIn("over", entry)
            self.assertTrue(0 <= entry["chasing"] <= 100)

    def test_the_approach_duel_is_recorded_for_both_innings(self):
        for key in ("approach_log", "inn1_approach_log"):
            log = self.state.get(key) or []
            self.assertTrue(log, key)
            for over in log:
                self.assertIn(over["bat"], approach_modifiers.BATTING_KEYS)
                self.assertIn(over["bowl"], approach_modifiers.BOWLING_KEYS)

    def test_every_logged_over_carries_its_ball_marks(self):
        """The analysis report lists the duel ball by ball, so the marks have to
        survive the over that produced them — ``last_over_timeline`` is
        overwritten every over and only ever holds the most recent one."""
        bpu = 6
        for key in ("approach_log", "inn1_approach_log"):
            for over in self.state.get(key) or []:
                marks = over.get("timeline")
                self.assertTrue(marks, f"{key} over {over.get('over')} has no marks")
                legal = sum(1 for m in marks if m not in ("WD", "NB"))
                # A completed over is exactly bpu legal balls; the last over of
                # an innings can be short when the side is bowled out or the
                # chase finishes mid-over.
                self.assertLessEqual(legal, bpu, over)

    def test_the_marks_agree_with_the_runs_they_are_logged_against(self):
        """A timeline that disagrees with its own over would put a fiction in
        the report — the one place a reader checks the engine's working."""
        for over in self.state.get("inn1_approach_log") or []:
            wickets = sum(1 for m in over["timeline"] if m == "W")
            self.assertEqual(wickets, over["wickets"], over)

    def test_no_over_is_missing_from_the_duel_record(self):
        """A chase won or lost mid-over used to be left out of the approach log
        and the chase history, because both were gated behind ``over_completed``
        along with the momentum tables — so the analysis report dropped the one
        over that decided the match from the duel table, the match-up grid and
        the win-probability chart."""
        for key, runs_key in (("inn1_approach_log", "inn1_over_runs"),
                              ("approach_log", "over_runs")):
            self.assertEqual(len(self.state[key]), len(self.state[runs_key]), key)

    def test_the_win_probability_series_reaches_the_result(self):
        """chase_chance_now has nothing to say once the chase is decided, so the
        deciding over needs an explicit terminal point — otherwise the chart
        stops an over short and reads as though the match never finished."""
        history = self.state.get("chase_history") or []
        self.assertTrue(history)
        self.assertIn(history[-1]["chasing"], (0, 100))
        self.assertEqual(history[-1]["over"], self.state["current_over"])

    def test_the_pitch_trace_is_recorded_for_the_chase(self):
        trace = self.state.get("dps_trace") or []
        self.assertTrue(trace, "Dry has innings-2 rules; none were recorded")

    def test_histories_have_one_entry_per_completed_over(self):
        """They are drawn as time series, so a missing over would shift the
        whole chart against the scoreboard."""
        overs = len(self.state.get("over_runs") or [])
        for key in ("momentum_history", "pressure_history"):
            self.assertLessEqual(abs(len(self.state[key]) - overs), 1, key)

    def test_a_fresh_innings_starts_level(self):
        """Momentum does not survive the interval."""
        self.assertTrue(self.state["inn1_momentum_history"])
        self.assertNotEqual(self.state["momentum_history"][:1],
                            self.state["inn1_momentum_history"][-1:] or [None])
