"""The distribution itself: valid, normalised, bounded, and legal cricket.

Covers requirement 3 of the brief's test list in depth, plus the §5
guarantees — finite non-negative probabilities summing to 1, no impossible
outcomes, bounded condition effects, and no influence applied twice.
"""

import itertools
import unittest

from engine.realism import conditions
from engine.realism.config import get_config
from engine.realism.context import (
    BallContext, chase_bucket, wickets_bucket,
)
from engine.realism.outcome_distribution import (
    OUTCOMES, RealismUnavailable, build, expected_runs, summarise,
)

ENGINE_OUTCOMES = {"Dot", "Single", "Double", "Three", "Four", "Six",
                   "Wicket", "Extras"}


def _assert_distribution(test, dist, msg=""):
    test.assertEqual(set(dist), ENGINE_OUTCOMES, msg)
    for o, p in dist.items():
        test.assertEqual(p, p, "%s %s is NaN" % (msg, o))
        test.assertNotIn(p, (float("inf"), float("-inf")),
                         "%s %s is infinite" % (msg, o))
        test.assertGreaterEqual(p, 0.0, "%s %s is negative" % (msg, o))
        test.assertLessEqual(p, 1.0, "%s %s exceeds 1" % (msg, o))
    test.assertAlmostEqual(sum(dist.values()), 1.0, places=9, msg=msg)


class NormalisationTests(unittest.TestCase):
    """Test 3: every outcome probability is valid and normalised."""

    def test_the_default_context_is_a_valid_distribution(self):
        _assert_distribution(self, build(BallContext()))

    def test_valid_across_a_wide_sweep_of_contexts(self):
        pitches = ("Even", "Flat", "Green", "Dusty", "Dead", "Bouncy", "Dry",
                   "Hard", "NotAPitch")
        count = 0
        for bat, bowl in itertools.product((30, 55, 75, 99), (30, 55, 75, 100)):
            for pitch in pitches:
                for balls in (0, 35, 36, 95, 96, 119):
                    for inn, target in ((1, None), (2, 185)):
                        for wkts in (0, 3, 6, 9):
                            dist = build(BallContext(
                                bat_rating=bat, bowl_rating=bowl, pitch=pitch,
                                balls_bowled=balls, innings=inn, target=target,
                                wickets=wkts, runs=balls, pitch_wear=balls / 120.0))
                            _assert_distribution(
                                self, dist,
                                "%d/%d %s b=%d i=%d w=%d"
                                % (bat, bowl, pitch, balls, inn, wkts))
                            count += 1
        self.assertGreater(count, 1000)

    def test_extreme_and_nonsense_inputs_still_normalise(self):
        for ctx in (
            BallContext(bat_rating=0, bowl_rating=0),
            BallContext(bat_rating=1e6, bowl_rating=-1e6),
            BallContext(bat_rating=float("nan"), bowl_rating=float("nan")),
            BallContext(balls_bowled=10 ** 6, innings_balls=1),
            BallContext(innings=2, target=1, runs=10 ** 6),
            BallContext(pitch_wear=50.0, dew=50.0, bowler_fatigue=-5.0),
            BallContext(bowling_type=None, bat_style=None, bowl_hand=None),
            BallContext(wickets=-4),
        ):
            _assert_distribution(self, build(ctx), repr(ctx)[:60])

    def test_no_outcome_outside_the_engine_vocabulary(self):
        """§5: never create impossible outcomes."""
        self.assertEqual(set(build(BallContext())), ENGINE_OUTCOMES)
        self.assertEqual(set(OUTCOMES), ENGINE_OUTCOMES)

    def test_every_outcome_keeps_some_probability(self):
        """A model that zeroes an outcome has removed it from cricket."""
        dist = build(BallContext(bat_rating=99, bowl_rating=30, pitch="Dead"))
        for o in OUTCOMES:
            self.assertGreater(dist[o], 0.0, o)

    def test_free_hit_does_not_force_a_zero_total(self):
        dist = build(BallContext(free_hit=True, bat_rating=95, bowl_rating=60))
        _assert_distribution(self, dist)


class ContextTests(unittest.TestCase):

    def test_phase_boundaries_match_the_t20_engine_split(self):
        """Overs 0-5 / 6-15 / 16-19, as engine/format_config defines them."""
        for balls, phase in ((0, "powerplay"), (35, "powerplay"),
                             (36, "middle"), (95, "middle"),
                             (96, "death"), (119, "death")):
            self.assertEqual(BallContext(balls_bowled=balls).phase, phase, balls)

    def test_phase_scales_to_a_hundred_ball_innings(self):
        """The Hundred has a powerplay and a death too, they are just shorter."""
        for balls, phase in ((0, "powerplay"), (29, "powerplay"),
                             (30, "middle"), (79, "middle"), (80, "death")):
            ctx = BallContext(format_name="The100", innings_balls=100,
                              balls_bowled=balls)
            self.assertEqual(ctx.phase, phase, balls)

    def test_required_rate_is_only_defined_when_chasing(self):
        self.assertIsNone(BallContext(innings=1, target=200).required_rate)
        self.assertIsNone(BallContext(innings=2, target=None).required_rate)
        ctx = BallContext(innings=2, target=181, runs=90, balls_bowled=60)
        self.assertAlmostEqual(ctx.required_rate, 9.1, places=1)

    def test_required_rate_is_safe_at_the_end_of_an_innings(self):
        self.assertIsNone(BallContext(innings=2, target=200, runs=100,
                                      balls_bowled=120).required_rate)

    def test_buckets_cover_the_whole_range(self):
        self.assertEqual({wickets_bucket(w) for w in range(0, 11)},
                         {"w0_2", "w3_5", "w6_plus"})
        self.assertIsNone(chase_bucket(None))
        self.assertEqual({chase_bucket(r) for r in (0, 5.9, 6, 8.9, 9, 11.9, 12, 40)},
                         {"rrr_lt6", "rrr_6_9", "rrr_9_12", "rrr_12_plus"})

    def test_node_path_descends_the_documented_hierarchy(self):
        # 100 needed off 60 balls is 10.0 an over -> the 9-12 band.
        ctx = BallContext(innings=2, target=200, runs=100, balls_bowled=60,
                          wickets=4)
        self.assertEqual(ctx.required_rate, 10.0)
        self.assertEqual(ctx.node_path("ipl_modern"),
                         ["T20", "ipl_modern", "middle", "inn2", "w3_5",
                          "rrr_9_12"])


class ConditionBoundTests(unittest.TestCase):
    """§5: adjustments are bounded, individually and jointly."""

    def test_each_family_respects_its_own_bound(self):
        cfg = get_config()
        bound = float(cfg["conditions"]["bound"])
        families = [
            conditions.pitch_multipliers("Dead", 1.0, "Off spin"),
            conditions.pitch_multipliers("Green", 1.0, "Fast"),
            conditions.weather_multipliers(1.0, True, 2, "death"),
            conditions.tactics_multipliers("ultra", "aggressive"),
            conditions.fatigue_multipliers(0.0),
            conditions.wicket_pressure_multipliers(9, 0),
            conditions.chase_multipliers(20.0, 9.0, 12, 8),
            conditions.style_multipliers("spin_away"),
        ]
        for fam in families:
            for o, m in fam.items():
                self.assertLessEqual(m, bound + 1e-9, o)
                self.assertGreaterEqual(m, 1.0 / bound - 1e-9, o)

    def test_the_milestone_family_has_its_own_wider_bound(self):
        """It redistributes a fixed amount of risk, so it is not a condition."""
        cfg = get_config()
        own = float(cfg["conditions"]["milestone_bound"])
        self.assertGreater(own, float(cfg["conditions"]["bound"]),
                           "clipping the past-ninety band at the condition "
                           "bound would defeat its purpose")
        for runs in (0, 25, 40, 60, 85, 120):
            for o, m in conditions.milestone_multipliers(runs).items():
                self.assertLessEqual(m, own + 1e-9, "%s runs=%d" % (o, runs))
                self.assertGreaterEqual(m, 1.0 / own - 1e-9,
                                        "%s runs=%d" % (o, runs))

    def test_the_milestone_family_touches_only_the_wicket(self):
        """Raising a set batter's SCORING rate is a feedback loop, not a model."""
        for runs in (0, 60, 120):
            m = conditions.milestone_multipliers(runs)
            for o in OUTCOMES:
                if o == "Wicket":
                    continue
                self.assertAlmostEqual(m[o], 1.0, places=12,
                                       msg="%s runs=%d" % (o, runs))

    def test_the_joint_bound_holds_when_everything_stacks(self):
        cfg = get_config()
        total = float(cfg["conditions"]["total_bound"])
        combined = conditions.combine([
            conditions.pitch_multipliers("Green", 1.0, "Off spin"),
            conditions.weather_multipliers(1.0, True, 2, "death"),
            conditions.tactics_multipliers("ultra", "aggressive"),
            conditions.fatigue_multipliers(0.0),
            conditions.wicket_pressure_multipliers(9, 0),
            conditions.chase_multipliers(30.0, 9.0, 6, 9),
            conditions.style_multipliers("spin_away"),
        ], total)
        for o, m in combined.items():
            self.assertLessEqual(m, total + 1e-9, o)
            self.assertGreaterEqual(m, 1.0 / total - 1e-9, o)

    def test_a_strength_of_zero_disables_a_family_cleanly(self):
        cfg = {k: dict(v) if isinstance(v, dict) else v
               for k, v in get_config().items()}
        cfg["conditions"] = dict(cfg["conditions"])
        cfg["conditions"]["pitch_strength"] = 0.0
        fam = conditions.pitch_multipliers("Dead", 1.0, "Fast", cfg)
        for o, m in fam.items():
            self.assertAlmostEqual(m, 1.0, places=12, msg=o)

    def test_the_pressure_family_is_mean_neutral(self):
        """The baseline already contains the average new-batter effect.

        The bands are read from the module rather than restated here, so this
        cannot drift away from the implementation it is checking.
        """
        bands = conditions._SETTLE_BANDS
        self.assertAlmostEqual(sum(b[1] for b in bands), 1.0, places=9,
                               msg="the band shares must partition an innings")
        for col, name in ((2, "runs"), (3, "wicket")):
            mean = sum(b[1] * b[col] for b in bands)
            self.assertAlmostEqual(mean, 1.0, places=9,
                                   msg="%s column is not mean-neutral" % name)
        # And through the public function, where bounds and strength apply.
        probe = [(b[1], 0 if b[0] == 2 else min(b[0], 10 ** 6) - 1)
                 for b in bands[:-1]]
        probe.append((bands[-1][1], 40))
        mean = sum(share * conditions.wicket_pressure_multipliers(3, bf)["Wicket"]
                   for share, bf in probe)
        self.assertAlmostEqual(mean, 1.0, delta=0.03)

    def test_the_milestone_family_is_mean_neutral(self):
        """Score-based risk must redistribute, not raise the innings level."""
        bands = conditions._MILESTONE_BANDS
        self.assertAlmostEqual(sum(b[1] for b in bands), 1.0, places=6,
                               msg="the band shares must partition an innings")
        for col, name in ((2, "runs"), (3, "wicket")):
            self.assertAlmostEqual(sum(b[1] * b[col] for b in bands), 1.0,
                                   places=9,
                                   msg="%s column is not mean-neutral" % name)

    def test_risk_rises_with_the_score_a_batter_has_made(self):
        """§12: the individual score distribution, not just the team total.

        A model whose hazard depends only on balls survived produces a
        correct team total with the runs piled into one batter. Risk has to
        rise with what the batter has actually scored.
        """
        last_w = -1.0
        for runs in (0, 25, 40, 60, 85, 120):
            w = conditions.milestone_multipliers(runs)["Wicket"]
            self.assertGreaterEqual(w, last_w, "runs=%d" % runs)
            last_w = w
        self.assertGreater(conditions.milestone_multipliers(90)["Wicket"],
                           conditions.milestone_multipliers(5)["Wicket"] * 1.5,
                           "the effect must be large enough to matter")

    def test_batter_runs_is_match_state_not_a_player_attribute(self):
        """Two cards with the same ratings on the same score are identical."""
        a = build(BallContext(bat_rating=82, bowl_rating=74, batter_runs=70))
        b = build(BallContext(bat_rating=82, bowl_rating=74, batter_runs=70))
        self.assertEqual(a, b)
        self.assertNotEqual(
            a, build(BallContext(bat_rating=82, bowl_rating=74, batter_runs=0)))

    def test_a_set_batter_accelerates_and_takes_more_risk(self):
        """§12: a batter who is in must not become un-dismissable."""
        settling = conditions.wicket_pressure_multipliers(3, 1)
        settled = conditions.wicket_pressure_multipliers(3, 15)
        accelerating = conditions.wicket_pressure_multipliers(3, 40)
        self.assertGreater(settling["Wicket"], settled["Wicket"],
                           "a new batter must be the most vulnerable")
        self.assertGreater(accelerating["Wicket"], settled["Wicket"],
                           "a set batter hitting out must get out more often "
                           "than one still building")
        self.assertGreater(accelerating["Four"] + accelerating["Six"],
                           settled["Four"] + settled["Six"],
                           "and must score faster while doing it")

    def test_tactics_can_be_switched_off_to_avoid_double_counting(self):
        """§5: the approach system and the tactics family model one decision."""
        ctx = BallContext(batting_approach="ultra", bowling_approach="aggressive")
        with_tactics = build(ctx, apply_tactics=True)
        without = build(ctx, apply_tactics=False)
        self.assertNotEqual(with_tactics, without)
        self.assertEqual(without, build(BallContext(), apply_tactics=False))


class ConditionDirectionTests(unittest.TestCase):
    """Conditions must remain meaningful (§16) and point the right way."""

    def test_a_flat_deck_scores_more_than_a_green_one(self):
        flat = build(BallContext(pitch="Dead"))
        green = build(BallContext(pitch="Green"))
        self.assertGreater(expected_runs(flat), expected_runs(green))
        self.assertLess(flat["Wicket"], green["Wicket"])

    def test_wear_helps_spin_more_than_pace(self):
        spin_new = build(BallContext(pitch="Dry", pitch_wear=0.0,
                                     bowling_type="Off spin"))
        spin_worn = build(BallContext(pitch="Dry", pitch_wear=1.0,
                                      bowling_type="Off spin"))
        pace_new = build(BallContext(pitch="Dry", pitch_wear=0.0,
                                     bowling_type="Fast"))
        pace_worn = build(BallContext(pitch="Dry", pitch_wear=1.0,
                                      bowling_type="Fast"))
        self.assertGreater(spin_worn["Wicket"] / spin_new["Wicket"],
                           pace_worn["Wicket"] / pace_new["Wicket"])

    def test_dew_only_applies_to_a_night_chase(self):
        day1 = build(BallContext(innings=1, is_day_night=True, balls_bowled=60))
        night2 = build(BallContext(innings=2, is_day_night=True, balls_bowled=60,
                                   target=180, runs=85))
        plain2 = build(BallContext(innings=2, is_day_night=False, balls_bowled=60,
                                   target=180, runs=85))
        self.assertGreater(expected_runs(night2), expected_runs(plain2))
        self.assertEqual(day1, build(BallContext(innings=1, is_day_night=False,
                                                 balls_bowled=60)))

    def test_a_tiring_bowler_concedes_more_and_sprays_more(self):
        fresh = build(BallContext(bowler_fatigue=1.0))
        tired = build(BallContext(bowler_fatigue=0.6))
        self.assertGreater(expected_runs(tired), expected_runs(fresh))
        self.assertGreater(tired["Extras"], fresh["Extras"])

    def test_the_first_ball_of_any_chase_is_par(self):
        """A chase is measured against its own target, not a generic rate."""
        for target in (90, 150, 186, 240):
            ctx = BallContext(innings=2, target=target, runs=0, balls_bowled=0)
            self.assertAlmostEqual(ctx.required_rate, ctx.par_rate, places=9)
            fam = conditions.chase_multipliers(ctx.required_rate, ctx.par_rate,
                                               120, 0)
            for o, m in fam.items():
                self.assertAlmostEqual(m, 1.0, places=12,
                                       msg="target=%d %s" % (target, o))

    def test_par_rate_is_none_when_not_chasing(self):
        self.assertIsNone(BallContext(innings=1, target=180).par_rate)
        self.assertIsNone(BallContext(innings=2, target=None).par_rate)

    def test_an_impossible_ask_produces_risk_not_a_rescue(self):
        """§9: the chase reacts to the scoreboard; it does not steer to a result."""
        cruise = build(BallContext(innings=2, target=120, runs=100,
                                   balls_bowled=60, wickets=1))
        gone = build(BallContext(innings=2, target=240, runs=60,
                                 balls_bowled=60, wickets=4))
        self.assertGreater(gone["Wicket"], cruise["Wicket"])
        self.assertGreater(gone["Four"] + gone["Six"],
                           cruise["Four"] + cruise["Six"])

    def test_the_death_overs_score_faster_than_the_middle(self):
        middle = build(BallContext(balls_bowled=60))
        death = build(BallContext(balls_bowled=110))
        self.assertGreater(expected_runs(death), expected_runs(middle) * 1.10)
        self.assertGreater(death["Six"], middle["Six"])

    def test_the_powerplay_is_polarised_not_just_fast(self):
        """Real powerplays are high-dot AND high-boundary at once."""
        pp = build(BallContext(balls_bowled=10))
        middle = build(BallContext(balls_bowled=60))
        self.assertGreater(pp["Dot"], middle["Dot"])
        self.assertGreater(pp["Four"] + pp["Six"], middle["Four"] + middle["Six"])


class ExplainAndSummariseTests(unittest.TestCase):

    def test_explain_reports_every_stage_and_its_provenance(self):
        out = build(BallContext(bat_rating=85, bowl_rating=70), explain=True)
        for key in ("dist", "baseline", "ratings", "conditions", "gain",
                    "node", "node_meta", "competition", "model_version", "phase"):
            self.assertIn(key, out)
        _assert_distribution(self, out["dist"])
        _assert_distribution(self, out["baseline"])
        self.assertIn(out["node_meta"]["count_kind"], ("observed", "pseudo"))

    def test_summarise_is_consistent_with_the_distribution(self):
        dist = build(BallContext(bat_rating=85, bowl_rating=70))
        s = summarise(dist)
        self.assertAlmostEqual(s["exp_runs"], expected_runs(dist), places=12)
        self.assertAlmostEqual(s["boundary"], dist["Four"] + dist["Six"],
                               places=12)

    def test_build_draws_no_random_numbers(self):
        """This is what makes shadow mode free (§10)."""
        import random
        state = random.getstate()
        for _ in range(200):
            build(BallContext(bat_rating=80, bowl_rating=70))
        self.assertEqual(state, random.getstate())


if __name__ == "__main__":
    unittest.main()
