"""Does the engine produce cricket that reads like cricket?

`tools/pitch_calibration.py` guards the team total. These guard the *texture* —
the things a player reads on a scorecard and can tell are wrong: how batters get
out, whether anyone ever hits a four, what a batter does when they have been
tied down. Every number here was measured with `python -m tools.realism_audit`
against real T20 reference figures; the assertions are the shape of those
findings, kept loose enough to survive tuning but tight enough to catch a
regression that would make the game feel synthetic again.
"""

import unittest

import yaml

from engine import ball_outcome as bo
import services.cipl_match as cm


def _shares(bowling_type, **kw):
    types, weights = bo._get_wicket_type_by_bowling(bowling_type, **kw)
    total = sum(weights)
    return {t: w / total * 100 for t, w in zip(types, weights)}


class DismissalMixTests(unittest.TestCase):
    """The old table gave pace bowlers 20% LBW and 4% stumpings, and made
    catches a minority mode. Real T20 is the other way round: most wickets are
    catches, LBW is uncommon, and a stumping off a quick is a freak."""

    def test_catching_is_the_dominant_mode_for_every_bowling_style(self):
        for style in ("Fast", "Leg spin", "Medium"):
            share = _shares(style)["Caught"]
            self.assertGreater(share, 45.0, style)
            self.assertLess(share, 70.0, style)

    def test_lbw_is_uncommon(self):
        for style in ("Fast", "Leg spin", "Medium"):
            self.assertLess(_shares(style)["LBW"], 13.0, style)

    def test_a_stumping_off_a_quick_is_a_freak_not_a_mode(self):
        self.assertLess(_shares("Fast")["Stumped"], 1.5)
        # Off spin it is a real weapon.
        self.assertGreater(_shares("Off spin")["Stumped"], 8.0)

    def test_run_outs_do_not_depend_on_who_is_bowling(self):
        """A run out is a fielding accident between the wickets. The style of
        the bowler who happened to deliver the ball has nothing to do with it."""
        shares = [_shares(s)["Run Out"] for s in ("Fast", "Leg spin", "Medium")]
        self.assertAlmostEqual(max(shares), min(shares), delta=0.5)

    def test_every_row_is_a_distribution(self):
        for style in ("Fast", "Leg spin", "Medium"):
            _types, weights = bo._get_wicket_type_by_bowling(style)
            self.assertAlmostEqual(sum(weights), 100.0, delta=0.5, msg=style)
            self.assertTrue(all(w >= 0 for w in weights), style)

    def test_the_death_overs_are_where_batters_hole_out(self):
        death = _shares("Fast", phase="death", batting_approach="ultra")
        powerplay = _shares("Fast", phase="powerplay", batting_approach="defensive")
        self.assertGreater(death["Caught"], powerplay["Caught"] + 20)
        # …and the stumps come back into play against a blocker with the new ball.
        self.assertGreater(powerplay["Bowled"], death["Bowled"])
        self.assertGreater(powerplay["LBW"], death["LBW"])

    def test_the_shot_the_batter_played_shows_up_in_the_dismissal(self):
        slog = _shares("Fast", batting_approach="ultra")
        block = _shares("Fast", batting_approach="defensive")
        self.assertGreater(slog["Caught"], block["Caught"])
        self.assertGreater(block["Bowled"], slog["Bowled"])
        # Busy running is what gets you run out.
        self.assertGreater(_shares("Fast", batting_approach="rotate")["Run Out"],
                           block["Run Out"])

    def test_a_caller_with_no_context_still_gets_a_sane_mix(self):
        """Everything outside CIPL calls this with the bowling style alone."""
        types, weights = bo._get_wicket_type_by_bowling("Fast")
        self.assertEqual(len(types), len(weights))
        self.assertAlmostEqual(sum(weights), 100.0, delta=0.5)

    def test_an_unknown_bowling_style_falls_back_rather_than_raising(self):
        share = _shares("Underarm lob")
        self.assertGreater(share["Caught"], 40.0)


class BoundaryBalanceTests(unittest.TestCase):
    """Fours are the currency of T20. Every pitch profile used to carry more
    twos than fours, which is backwards — real T20 runs about 11.5% fours
    against 6.5% twos — and it made scorecards read as though nobody could find
    the rope."""

    def setUp(self):
        with open("config/ground_conditions.yaml", encoding="utf-8") as f:
            self.profiles = yaml.safe_load(f)["pitch_profiles"]

    def test_fours_outnumber_twos_on_every_pitch(self):
        for name, profile in self.profiles.items():
            m = profile["scoring_matrix"]
            self.assertGreater(m["Four"], m["Double"],
                               f"{name}: more twos than fours")

    def test_the_ratio_is_near_the_real_one(self):
        """Real T20 sits near 0.57 twos per four."""
        for name, profile in self.profiles.items():
            m = profile["scoring_matrix"]
            self.assertLess(m["Double"] / m["Four"], 0.80, name)

    def test_every_matrix_is_still_a_distribution(self):
        for name, profile in self.profiles.items():
            self.assertAlmostEqual(sum(profile["scoring_matrix"].values()), 1.0,
                                   delta=0.006, msg=name)


class DotPressureTests(unittest.TestCase):
    """A batter who cannot get the ball away goes looking for the boundary.
    Without it, dots cluster and maidens become routine."""

    def test_dots_are_counted_only_while_unbroken(self):
        self.assertEqual(cm._trailing_dots(["0", "0", "0"]), 3)
        self.assertEqual(cm._trailing_dots(["0", "4", "0"]), 1)
        self.assertEqual(cm._trailing_dots(["4", "1", "2"]), 0)
        self.assertEqual(cm._trailing_dots([]), 0)

    def test_a_wide_neither_breaks_the_run_nor_extends_it(self):
        """The batter did not face a legal ball, so the pressure is unchanged."""
        self.assertEqual(cm._trailing_dots(["0", "0", "WD", "0"]), 3)
        self.assertEqual(cm._trailing_dots(["4", "WD", "NB"]), 0)

    def test_nothing_fires_below_three_dots(self):
        for marks in ([], ["0"], ["0", "0"]):
            self.assertIsNone(cm._make_dot_pressure_hook(marks), marks)

    def test_pressure_builds_with_every_dot(self):
        weights = {"Dot": 0.33, "Single": 0.36, "Double": 0.07, "Three": 0.006,
                   "Four": 0.12, "Six": 0.05, "Wicket": 0.04, "Extras": 0.024}

        def dot_share(marks):
            hook = cm._make_dot_pressure_hook(marks)
            out = hook(weights) if hook else weights
            return out["Dot"] / sum(out.values())

        shares = [dot_share(["0"] * n) for n in (0, 3, 4, 5)]
        self.assertEqual(shares, sorted(shares, reverse=True))
        # By five dots the batter is markedly less likely to play out a sixth.
        self.assertLess(shares[-1], shares[0] * 0.65)

    def test_the_forced_shot_costs_wickets_as_well_as_yielding_runs(self):
        """It has to cut both ways, or it is just free runs for being becalmed."""
        for n in (3, 4, 5):
            self.assertGreater(cm.DOT_PRESSURE[n]["Wicket"], 1.0)
            self.assertGreater(cm.DOT_PRESSURE[n]["Four"], 1.0)
            self.assertLess(cm.DOT_PRESSURE[n]["Dot"], 1.0)

    def test_it_does_not_run_past_the_last_ball_of_an_over(self):
        """Six dots is a maiden; there is no seventh to apply pressure to."""
        self.assertLessEqual(cm.DOT_PRESSURE_MAX, 5)


class SetBatterTests(unittest.TestCase):
    """Being set is worth something, but it plateaus. The T20 curve used to top
    out at +20% (+26% with the balls-faced layer) and never stop, which put
    centuries in a fifth of all innings against a real rate near 3.5%."""

    @staticmethod
    def _four_weight(runs, balls=20):
        """Weight of a Four for a batter on ``runs``, holding everything else —
        including balls faced — fixed, so only the confidence curve varies."""
        return bo.compute_weighted_prob(
            "Four", 0.11, batting=75, bowling=75, fielding=70, pitch="Even",
            bowling_type="Fast", streak={}, batter_runs=runs, balls_faced=balls)

    def test_getting_set_helps(self):
        curve = [self._four_weight(r) for r in (0, 15, 25, 40, 60)]
        self.assertEqual(curve, sorted(curve))
        self.assertGreater(curve[-1], curve[0])

    def test_but_it_plateaus_rather_than_compounding(self):
        """A hundred is no easier to extend than a fifty. Without this the
        batter who got in simply ran away with the innings."""
        fifty, hundred = self._four_weight(60), self._four_weight(120)
        self.assertAlmostEqual(fifty, hundred, delta=fifty * 0.02)

    def test_being_set_is_worth_a_useful_edge_not_a_different_player(self):
        """Mind the amplification. The confidence curve tops out at +12% on
        *effective batting*, but the weighting function is steeply non-linear,
        so it arrives as about +50% on the boundary weight — roughly a 3.5x
        multiplier on whatever the constant says. That is why trimming the curve
        from 1.20 to 1.12 moved par by twenty runs, and it is the first thing to
        know before touching it again.
        """
        ratio = self._four_weight(60) / self._four_weight(0)
        self.assertGreater(ratio, 1.30)
        self.assertLess(ratio, 1.70)

    def test_a_new_batter_is_still_vulnerable(self):
        """The first few balls are the dangerous ones, and that is separate from
        the confidence curve — it keys on balls faced, not runs."""
        self.assertLess(self._four_weight(0, balls=1), self._four_weight(0, balls=20))


if __name__ == "__main__":
    unittest.main()


class PredictabilityTests(unittest.TestCase):
    """Doing the same thing over after over should cost you.

    The mind-game layer prices a single hidden pick; nothing priced the pattern,
    so a captain could bowl Variations from the 7th to the 15th and the batting
    side never once set up for it.
    """

    def setUp(self):
        from engine import approach_modifiers as am
        self.am = am

    def test_nothing_happens_below_the_threshold(self):
        for run in range(0, self.am.REPEAT_FROM):
            self.assertEqual(self.am.repeat_multipliers(bat_repeat=run), {}, run)
            self.assertEqual(self.am.repeat_multipliers(bowl_repeat=run), {}, run)

    def test_a_readable_batter_scores_less_and_gets_out_more(self):
        m = self.am.repeat_multipliers(bat_repeat=self.am.REPEAT_FROM)
        self.assertLess(m["Four"], 1.0)
        self.assertLess(m["Six"], 1.0)
        self.assertGreater(m["Wicket"], 1.0)
        self.assertGreater(m["Dot"], 1.0)

    def test_a_readable_bowler_is_punished_the_other_way(self):
        m = self.am.repeat_multipliers(bowl_repeat=self.am.REPEAT_FROM)
        self.assertGreater(m["Four"], 1.0)
        self.assertLess(m["Wicket"], 1.0)

    def test_it_gets_worse_the_longer_it_goes_on(self):
        sixes = [self.am.repeat_multipliers(bat_repeat=r)["Six"]
                 for r in range(self.am.REPEAT_FROM, self.am.REPEAT_CAP + 1)]
        self.assertEqual(sixes, sorted(sixes, reverse=True))

    def test_but_it_stops_getting_worse(self):
        """A side has to remain able to bat. Past the cap it plateaus."""
        capped = self.am.repeat_multipliers(bat_repeat=self.am.REPEAT_CAP)
        for run in (self.am.REPEAT_CAP + 1, self.am.REPEAT_CAP + 40):
            self.assertEqual(self.am.repeat_multipliers(bat_repeat=run), capped)

    def test_two_equally_obvious_captains_cancel_exactly(self):
        """Nobody has learned anything the other has not, so the over is
        untouched. Applying both tables and multiplying them would not give
        this — the run factors compose to 0.80 on the six, so two obstinate
        captains would quietly produce a low-scoring over for no reason."""
        for run in range(self.am.REPEAT_FROM, self.am.REPEAT_CAP + 1):
            self.assertEqual(self.am.repeat_multipliers(run, run), {}, run)

    def test_only_the_difference_in_readability_is_priced(self):
        gap_one = self.am.repeat_multipliers(self.am.REPEAT_CAP,
                                             self.am.REPEAT_CAP - 1)
        plain_one = self.am.repeat_multipliers(self.am.REPEAT_FROM, 0)
        self.assertEqual(gap_one, plain_one)

    def test_a_fixed_approach_calibration_run_is_untouched(self):
        """tools/pitch_calibration bowls balanced-vs-balanced every over. Both
        sides are maximally readable, so this layer must be inert there or it
        would silently move par."""
        self.assertEqual(self.am.repeat_multipliers(20, 20), {})

    def test_the_run_resets_the_moment_the_captain_changes(self):
        """The cost is for being readable, not for using an approach — one
        different over has to buy the surprise back."""
        state = {"bat_repeat": 5, "last_bat_approach": "ultra",
                 "batting_approach": "rotate"}
        self.assertEqual(cm._repeat_if_same(state, "bat"), 1)
        state["batting_approach"] = "ultra"
        self.assertEqual(cm._repeat_if_same(state, "bat"), 6)

    def test_before_the_pick_is_in_the_context_looks_one_ahead(self):
        """approach_context is built before the captain has chosen, and the AI
        has to see the number its *next* pick would produce."""
        state = {"bat_repeat": 3, "last_bat_approach": "ultra",
                 "batting_approach": None}
        self.assertEqual(cm._repeat_if_same(state, "bat"), 4)

    def test_the_engine_context_carries_both_runs(self):
        ctx = self.am.make_context(bat_repeat=5, bowl_repeat=2)
        self.assertEqual(ctx["bat_repeat"], 5)
        self.assertEqual(ctx["bowl_repeat"], 2)
        mult = self.am.approach_multipliers("ultra", "balanced", context=ctx)
        plain = self.am.approach_multipliers(
            "ultra", "balanced", context=self.am.make_context())
        self.assertLess(mult["Six"], plain["Six"])

    def test_the_bot_cannot_serve_a_stale_payoff_matrix(self):
        """The repeat counts change the payoffs, so they must change the cache
        key — otherwise the bot keeps reasoning from before it was read."""
        from services import bot_tactics
        a = bot_tactics._context_fingerprint(self.am.make_context(bat_repeat=1))
        b = bot_tactics._context_fingerprint(self.am.make_context(bat_repeat=5))
        self.assertNotEqual(a, b)
