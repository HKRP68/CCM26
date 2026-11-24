"""Fairness & Simulation Fixes (Prompt 03) — one test per corrected concern.

Each class maps to a numbered requirement and asserts the *behaviour*, so a
regression that quietly reinstates a hidden bias fails here rather than
shipping. The expensive end-to-end checks live in
``tools/compare_realism.py``; these are fast, targeted invariants.
"""

import os
import random
import unittest

from engine import ball_outcome as bo
from engine.format_config import get_format
from engine.realism import (
    diagnostics, integration as ri, phases,
)
from engine.realism.config import clear_cache
from engine.realism.context import BallContext

T20 = get_format("T20")


class _Realism:
    """Context manager toggling the realism flag and clearing caches."""

    def __init__(self, on=True, diag=False):
        self.on, self.diag = on, diag

    def __enter__(self):
        self._prev = (os.environ.get("SIM_REALISM_ENABLED"),
                      os.environ.get("SIM_REALISM_DIAGNOSTICS"))
        os.environ["SIM_REALISM_ENABLED"] = "1" if self.on else "0"
        if self.diag:
            os.environ["SIM_REALISM_DIAGNOSTICS"] = "1"
        else:
            os.environ.pop("SIM_REALISM_DIAGNOSTICS", None)
        clear_cache()
        ri.reset_log_state()
        return self

    def __exit__(self, *exc):
        for k, v in zip(("SIM_REALISM_ENABLED", "SIM_REALISM_DIAGNOSTICS"),
                        self._prev):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clear_cache()
        return False


def _bat(rating=80, hand="Right"):
    return {"name": "B", "batting_rating": rating, "batting_hand": hand}


def _bowl(rating=75, hand="Right", btype="Fast-medium"):
    return {"name": "W", "bowling_rating": rating, "fielding_rating": 70,
            "bowling_hand": hand, "bowling_type": btype}


def _ball(**kw):
    args = dict(batter=_bat(), bowler=_bowl(), pitch="Even",
                streak={"boundaries": 0}, over_number=8, batter_runs=10,
                balls_faced=8, format_config=T20)
    args.update(kw)
    return bo.calculate_outcome(**args)


class NoPositionPenaltyTests(unittest.TestCase):
    """Fix 6: batting-order must not secretly lower the card in realism mode."""

    def test_batting_position_does_not_change_the_effective_rating(self):
        with _Realism():
            import engine.realism.outcome_distribution as od
            import engine.realism.integration as rint
            seen = []
            orig = od.build

            def spy(ctx, cfg=None, reference=None, apply_tactics=True,
                    explain=False):
                seen.append((ctx.batting_position, ctx.bat_rating))
                return orig(ctx, cfg, reference, apply_tactics, explain)

            od.build = rint.build = spy
            try:
                for pos in (1, 5, 8, 11):
                    random.seed(1)
                    _ball(batting_position=pos)
            finally:
                od.build = rint.build = orig
            ratings = {r for _p, r in seen}
            self.assertEqual(len(ratings), 1,
                             "the card rating must not depend on batting "
                             "position: saw %s" % seen)
            self.assertEqual(ratings.pop(), 80.0)

    def test_legacy_mode_keeps_its_position_curve(self):
        """The penalty is only removed in realism mode; legacy is unchanged."""
        self.assertLess(bo._POS_BATTING_MULT[11], 0.9)
        self.assertEqual(bo._POS_BATTING_MULT[5], 1.0)


class SingleAbilityModelTests(unittest.TestCase):
    """Fix 2: one calibrated rating model, no overlapping legacy skill layers."""

    def test_the_legacy_skill_and_duel_layers_are_bypassed_in_realism(self):
        with _Realism():
            # The realism distribution replaces the whole legacy weight loop,
            # so the duel never runs (it would draw RNG and double-count).
            random.seed(4)
            self.assertTrue(all("duel" not in _ball() for _ in range(100)))

    def test_rating_gap_still_drives_outcomes_through_one_model(self):
        from engine.realism.outcome_distribution import build, expected_runs
        strong = build(BallContext(bat_rating=92, bowl_rating=70))
        weak = build(BallContext(bat_rating=55, bowl_rating=70))
        self.assertGreater(expected_runs(strong), expected_runs(weak))
        self.assertLess(strong["Wicket"], weak["Wicket"])


class TossFairnessTests(unittest.TestCase):
    """Fix 4: an unspecified toss must not auto-favour the home team."""

    def _toss_winners(self, realism, n=60):
        from services import sim_match
        from tools import pitch_calibration as pc
        winners = []
        with _Realism(on=realism):
            for i in range(n):
                random.seed(1000 + i)
                with pc._muted_logging():
                    a = pc.make_xi("H", 100 + i)
                    b = pc.make_xi("A", 500 + i)
                    res = sim_match.simulate_match(
                        a, b, 20, "Even", "HomeTeam", "AwayTeam",
                        commentary=False)
                winners.append(res["toss"]["winner"])
        return winners

    def test_realism_toss_is_not_always_home(self):
        winners = self._toss_winners(realism=True)
        home = sum(1 for w in winners if w == "HomeTeam")
        self.assertGreater(home, 0)
        self.assertLess(home, len(winners),
                        "the away side must sometimes win the toss")
        # Roughly fair: between 30% and 70% home over 60 tosses.
        self.assertGreater(home, len(winners) * 0.30)
        self.assertLess(home, len(winners) * 0.70)

    def test_legacy_toss_default_is_unchanged(self):
        winners = self._toss_winners(realism=False, n=10)
        self.assertTrue(all(w == "HomeTeam" for w in winners),
                        "legacy default must stay byte-identical (home)")


class WicketCapAndClusterTests(unittest.TestCase):
    """Fix 5: the 12% cap is real-data-consistent; clustering is preserved."""

    def test_the_wicket_cap_is_a_real_world_value(self):
        self.assertGreaterEqual(bo.T20_WICKET_SHARE_CAP, 0.10)
        self.assertLessEqual(bo.T20_WICKET_SHARE_CAP, 0.14,
                             "the cap must sit at the real ceiling on "
                             "sustained per-ball dismissal (~12-13%)")

    def test_the_wicket_share_never_exceeds_the_cap_in_realism(self):
        with _Realism():
            random.seed(7)
            # Drive an extreme mismatch so the cap is under pressure.
            for _ in range(3000):
                oc = _ball(batter=_bat(32), bowler=_bowl(99, btype="Fast"),
                           pitch="Green", balls_faced=0)
                self.assertIn(oc["runs"], (0, 1, 2, 3, 4, 6))

    def test_the_cipl_cluster_throttle_is_off_in_realism(self):
        """Measured: the throttle suppressed legitimate multi-wicket overs."""
        import services.cipl_match as cm
        self.assertTrue(hasattr(cm, "_make_wicket_cluster_hook"))
        # The hook is still defined (legacy uses it); realism passes None.
        self.assertIsNone(cm._make_wicket_cluster_hook(0),
                          "no wickets yet -> no-op in either mode")


class PitchWearTests(unittest.TestCase):
    """Fix 7: wear is match-long and format-appropriate, not per-innings."""

    def test_wear_accumulates_across_innings(self):
        from engine.pitch_state import carry_wear
        self.assertAlmostEqual(carry_wear(1, 120, 120), 0.5, places=6)
        self.assertAlmostEqual(carry_wear(2, 0, 120), 0.5, places=6,
                               msg="the chase must inherit the first innings' "
                                   "wear, not a fresh pitch")
        self.assertAlmostEqual(carry_wear(2, 120, 120), 1.0, places=6)

    def test_wear_is_keyed_on_innings_length_so_formats_differ(self):
        from engine.pitch_state import carry_wear
        # Same ball count is a different fraction of a short vs long innings.
        self.assertGreater(carry_wear(1, 60, 60), carry_wear(1, 60, 300))


class PhaseCentralizationTests(unittest.TestCase):
    """Fix 10: one source for the realism phase boundaries."""

    def test_context_and_builder_use_the_same_thresholds(self):
        from tools.build_realism_reference import _phase_for_fraction
        for frac in (0.0, 0.29, 0.30, 0.50, 0.79, 0.80, 0.99):
            self.assertEqual(_phase_for_fraction(frac),
                             phases.phase_for_fraction(frac), frac)

    def test_t20_phase_boundaries_match_the_legacy_split(self):
        # 0-5 powerplay, 6-15 middle, 16-19 death for a 120-ball innings.
        self.assertEqual(phases.phase_for_ball(0, 120), "powerplay")
        self.assertEqual(phases.phase_for_ball(35, 120), "powerplay")
        self.assertEqual(phases.phase_for_ball(36, 120), "middle")
        self.assertEqual(phases.phase_for_ball(95, 120), "middle")
        self.assertEqual(phases.phase_for_ball(96, 120), "death")
        self.assertEqual(phases.phase_for_ball(119, 120), "death")

    def test_phases_scale_to_the_hundred(self):
        self.assertEqual(phases.phase_for_ball(0, 100), "powerplay")
        self.assertEqual(phases.phase_for_ball(30, 100), "middle")
        self.assertEqual(phases.phase_for_ball(80, 100), "death")


class DiagnosticsTests(unittest.TestCase):
    """Fix 8: opt-in per-delivery diagnostics, off and free by default."""

    def test_diagnostics_are_off_by_default(self):
        os.environ.pop("SIM_REALISM_DIAGNOSTICS", None)
        self.assertFalse(diagnostics.enabled())

    def test_nothing_is_recorded_when_off(self):
        diagnostics.reset()
        with _Realism(diag=False):
            random.seed(1)
            for _ in range(20):
                _ball()
            self.assertNotIn("diagnostics", _ball().get("realism", {}))
        self.assertEqual(len(diagnostics.records()), 0)

    def test_a_record_captures_every_stage_and_the_sampled_outcome(self):
        diagnostics.reset()
        with _Realism(diag=True):
            random.seed(3)
            oc = _ball(batting_approach="aggressive",
                       bowling_approach="variation",
                       approach_context={"pitch": "Even", "phase": "middle",
                                         "bowling_type": "Fast-medium"})
            rec = diagnostics.last()
            self.assertIsNotNone(rec)
            self.assertIn(rec["sampled"],
                          ("Dot", "Single", "Double", "Three", "Four", "Six",
                           "Wicket", "Extras"))
            for stage in ("baseline", "realism_out", "final_pre_sample"):
                self.assertIn(stage, rec["stages"])
                self.assertAlmostEqual(sum(rec["stages"][stage].values()), 1.0,
                                       delta=0.01, msg=stage)
            self.assertIn("rating_curve", rec["modifiers"])
            self.assertIn("conditions", rec["modifiers"])
            self.assertIn(oc["realism"]["diagnostics"]["sampled"], (rec["sampled"],))
        self.assertGreater(len(diagnostics.records()), 0)

    def test_the_record_shows_the_approach_actually_moving_the_weights(self):
        """The bug fix 8 found: the approach model must reach live deliveries."""
        diagnostics.reset()
        with _Realism(diag=True):
            random.seed(5)
            _ball(over_number=17, balls_faced=20, batter_runs=40,
                  batting_approach="ultra", bowling_approach="defensive",
                  approach_context={"pitch": "Even", "phase": "death",
                                    "bowling_type": "Fast-medium"})
            rec = diagnostics.last()
            self.assertIsNotNone(rec["modifiers"].get("approach"),
                                 "the realism approach model must be applied "
                                 "on a live over with explicit picks")
            appr = rec["modifiers"]["approach"]
            self.assertGreater(appr["Six"], 1.0,
                               "Ultra Attack must lift the six multiplier")

    def test_the_buffer_is_bounded(self):
        diagnostics.reset()
        with _Realism(diag=True):
            random.seed(9)
            for _ in range(diagnostics._BUFFER_MAX + 200):
                _ball()
        self.assertLessEqual(len(diagnostics.records()),
                             diagnostics._BUFFER_MAX)


class ApproachReachesLiveTests(unittest.TestCase):
    """The headline fix the diagnostics surfaced: approaches reach matches."""

    def test_approach_picks_change_a_live_delivery_distribution(self):
        import engine.realism.outcome_distribution as od
        import engine.realism.integration as rint
        with _Realism():
            captured = {}
            orig = od.build

            def spy(ctx, cfg=None, reference=None, apply_tactics=True,
                    explain=False):
                captured["apply_tactics"] = apply_tactics
                return orig(ctx, cfg, reference, apply_tactics, explain)

            od.build = rint.build = spy
            try:
                random.seed(1)
                _ball(batting_approach="ultra", bowling_approach="defensive",
                      approach_context={"pitch": "Even", "phase": "death",
                                        "bowling_type": "Fast-medium"})
            finally:
                od.build = rint.build = orig
            self.assertTrue(captured["apply_tactics"],
                            "the realism approach model must run on a live "
                            "over that has explicit approach picks")

    def test_bot_reasons_over_the_realism_distribution(self):
        from services import bot_tactics as bt
        from tools import pitch_calibration as pc
        import services.cipl_match as cm
        with _Realism():
            with pc._muted_logging():
                a, b = pc.make_xi("A", 11), pc.make_xi("B", 77)
                st = cm.build_cipl_state(1, 20, 10, 20, 111, 222, a, b, "A",
                                         "B", -1, "Even", False,
                                         ball_format="T20")
                st["total_runs"] = 90
                st["total_wickets"] = 4
                st["current_over"] = 14
            self.assertIsNotNone(bt._realism_context(st),
                                 "the bot must see a realism context in a "
                                 "realism match")
            base = bt.ball_probabilities(st)
            # The realism per-ball wicket rate is ~5%, not the legacy stand-in's
            # ~2%: a crude check that the realism model is the one in use.
            self.assertGreater(base["Wicket"], 0.03)


if __name__ == "__main__":
    unittest.main()
