"""Approaches, pitches and conditions in realism mode.

Covers the second brief's requirements directly:

  1  existing names, labels, order and controls preserved
  3  no overlapping pitch / phase / rating / momentum / chase / weather layers
  4  every batting approach is a scoring-versus-risk trade-off
  5  every bowling approach is a prevention-versus-wickets trade-off
  6  tactical counters survive, with no unbeatable choice
  7  pitches change probabilities, they do not enforce totals
  8  weather, dew and wear are bounded
  9  no identity-derived hidden player ability
 10  the bot captain reasons in win probability
 11  no forced finishes, convergence or result steering
 12  extras, wickets, strike rotation and free hits unchanged
 13  all 25 pairings, every pitch, every phase
 14  rating fairness, no dominance, realistic spread, natural upsets
"""

import itertools
import os
import unittest

from engine import approach_modifiers as am
from engine.realism import (
    approaches, baselines, conditions, integration as ri, tilt,
    win_probability as wp,
)
from engine.realism.config import clear_cache, get_config
from engine.realism.context import BallContext
from engine.realism.outcome_distribution import OUTCOMES, build, expected_runs

BAT = ["defensive", "rotate", "balanced", "aggressive", "ultra"]
BOWL = ["defensive", "balanced", "mixed", "variation", "aggressive"]
PITCHES = ["Dusty", "Green", "Dry", "Bouncy", "Even", "Hard", "Flat", "Dead"]
PHASE_BALLS = {"powerplay": 10, "middle": 60, "death": 110}
PAIRINGS = list(itertools.product(BAT, BOWL))


def _ctx(bat, bowl, pitch="Even", phase="middle", **kw):
    args = dict(bat_rating=75.0, bowl_rating=75.0, pitch=pitch,
                balls_bowled=PHASE_BALLS[phase], batting_approach=bat,
                bowling_approach=bowl)
    args.update(kw)
    return BallContext(**args)


def _dist(bat, bowl, pitch="Even", phase="middle", **kw):
    return build(_ctx(bat, bowl, pitch, phase, **kw))


def _summary(dist):
    return {
        "runs": expected_runs(dist) + dist.get("Extras", 0.0),
        "wicket": dist["Wicket"],
        "boundary": dist["Four"] + dist["Six"],
        "dot": dist["Dot"],
    }


class PreservedControlsTests(unittest.TestCase):
    """Requirement 1: nothing user-facing may change."""

    def test_approach_names_labels_and_order_are_unchanged(self):
        self.assertEqual([k for k, _e, _l in am.BATTING_APPROACHES],
                         ["defensive", "rotate", "balanced", "aggressive",
                          "ultra"])
        self.assertEqual([k for k, _e, _l in am.BOWLING_APPROACHES],
                         ["defensive", "balanced", "mixed", "aggressive",
                          "variation"])
        self.assertEqual([l for _k, _e, l in am.BATTING_APPROACHES],
                         ["Defensive", "Rotate Strike", "Balanced",
                          "Aggressive", "Ultra Attack"])
        self.assertEqual([l for _k, _e, l in am.BOWLING_APPROACHES],
                         ["Defensive", "Balanced", "Mixed", "Aggressive",
                          "Variation"])

    def test_realism_model_covers_exactly_the_existing_approaches(self):
        self.assertEqual(set(approaches.BATTING_FRONTIER), am.BATTING_KEYS)
        self.assertEqual(set(approaches.BOWLING_FRONTIER), am.BOWLING_KEYS)

    def test_blurbs_and_keyboards_still_work(self):
        self.assertEqual(set(am.BATTING_BLURBS), am.BATTING_KEYS)
        self.assertEqual(set(am.BOWLING_BLURBS), am.BOWLING_KEYS)
        self.assertTrue(am.batting_keyboard_rows("x"))
        self.assertTrue(am.bowling_keyboard_rows("x"))
        self.assertEqual(am.batting_label("ultra"), "💥 Ultra Attack")

    def test_pitch_types_are_unchanged(self):
        """Every existing surface is still registered and still modelled.

        Asserts the registry recognises each one (rather than checking its
        classification against a hardcoded vocabulary, which only tests that
        the test knows the vocabulary) and that the realism engine has a
        shape for it, so no surface silently falls through to the default.
        """
        from engine import pitch_registry
        for pitch in PITCHES:
            self.assertIsNotNone(conditions._PITCH_SHAPE.get(pitch),
                                 "%s has no realism pitch shape" % pitch)
            favours = pitch_registry.favours(pitch)
            self.assertTrue(favours,
                            "%s is not classified by the registry" % pitch)
            self.assertIsInstance(favours, str)

    def test_the_realism_engine_models_every_registered_pitch(self):
        from engine import ball_outcome
        for pitch in ball_outcome.PITCH_RUN_FACTOR:
            self.assertIn(pitch, conditions._PITCH_SHAPE,
                          "%s is a known surface with no realism shape" % pitch)


class AllPairingsValidTests(unittest.TestCase):
    """Requirement 13: all 25 pairings, every pitch, every phase."""

    def test_every_pairing_on_every_pitch_in_every_phase_is_valid(self):
        checked = 0
        for (bat, bowl), pitch, phase in itertools.product(
                PAIRINGS, PITCHES, PHASE_BALLS):
            dist = _dist(bat, bowl, pitch, phase)
            self.assertEqual(set(dist), set(OUTCOMES))
            for o, p in dist.items():
                self.assertGreaterEqual(p, 0.0, "%s/%s %s %s %s"
                                        % (bat, bowl, pitch, phase, o))
                self.assertEqual(p, p)
            self.assertAlmostEqual(sum(dist.values()), 1.0, places=9,
                                   msg="%s/%s %s %s" % (bat, bowl, pitch, phase))
            checked += 1
        self.assertEqual(checked, 25 * len(PITCHES) * len(PHASE_BALLS))

    def test_every_pairing_keeps_every_outcome_possible(self):
        for (bat, bowl), pitch in itertools.product(PAIRINGS, PITCHES):
            dist = _dist(bat, bowl, pitch, "death")
            for o in OUTCOMES:
                self.assertGreater(dist[o], 0.0,
                                   "%s/%s %s %s" % (bat, bowl, pitch, o))

    def test_pairings_survive_both_innings_and_a_chase(self):
        for bat, bowl in PAIRINGS:
            for inn, target in ((1, None), (2, 181)):
                dist = _dist(bat, bowl, innings=inn, target=target, runs=90,
                             wickets=4)
                self.assertAlmostEqual(sum(dist.values()), 1.0, places=9)


class BattingTradeOffTests(unittest.TestCase):
    """Requirement 4: scoring opportunity against dismissal risk."""

    def test_the_intent_ladder_buys_runs_with_wickets(self):
        """The core trade-off, on every pitch in every phase.

        One exception, and it is deliberate rather than a gap: the engine
        clamps the normalised wicket share to a floor
        (``rating_curve.min_wicket_share``) so that no batter is ever
        un-dismissable. On a dead pitch against defensive bowling the two
        most cautious intents both sit on that floor, where they cannot be
        strictly ordered — the floor is doing its job. Monotonicity is
        asserted strictly everywhere else and non-strictly there.
        """
        floor = float(get_config()["rating_curve"]["min_wicket_share"])
        strict = 0
        for pitch, phase in itertools.product(PITCHES, PHASE_BALLS):
            for bowl in BOWL:
                rows = [_summary(_dist(b, bowl, pitch, phase)) for b in BAT]
                for lo, hi in zip(rows, rows[1:]):
                    self.assertGreater(hi["runs"], lo["runs"],
                                       "%s %s %s runs" % (pitch, phase, bowl))
                    if lo["wicket"] <= floor + 1e-9:
                        self.assertGreaterEqual(
                            hi["wicket"], lo["wicket"] - 1e-9,
                            "%s %s %s wicket at the floor"
                            % (pitch, phase, bowl))
                    else:
                        self.assertGreater(
                            hi["wicket"], lo["wicket"],
                            "%s %s %s wicket" % (pitch, phase, bowl))
                        strict += 1
        # 8 pitches x 3 phases x 5 plans x 4 ladder steps = 480 comparisons.
        # The floor should bind in a handful of them, not routinely.
        self.assertGreater(strict, 430,
                           "the wicket floor is binding too often — the "
                           "trade-off is being flattened, not protected")

    def test_more_intent_means_more_boundaries_and_fewer_dots(self):
        for bowl in BOWL:
            low = _summary(_dist("defensive", bowl))
            high = _summary(_dist("ultra", bowl))
            self.assertGreater(high["boundary"], low["boundary"], bowl)
            self.assertLess(high["dot"], low["dot"], bowl)

    def test_rotate_trades_boundaries_for_strike(self):
        """The one approach a single scaling factor cannot express."""
        rot = _dist("rotate", "balanced")
        dfn = _dist("defensive", "balanced")
        self.assertGreater(rot["Single"] + rot["Double"],
                           dfn["Single"] + dfn["Double"])
        self.assertLess(rot["Four"] + rot["Six"], dfn["Dot"],
                        "rotate must be about running, not hitting")
        self.assertGreater(expected_runs(rot), expected_runs(dfn))

    def test_no_batting_approach_is_free(self):
        """Nothing may score more than balanced at no extra risk."""
        bal = _summary(_dist("balanced", "balanced"))
        for b in BAT:
            if b == "balanced":
                continue
            got = _summary(_dist(b, "balanced"))
            if got["runs"] > bal["runs"]:
                self.assertGreater(got["wicket"], bal["wicket"], b)
            if got["wicket"] < bal["wicket"]:
                self.assertLess(got["runs"], bal["runs"], b)


class BowlingTradeOffTests(unittest.TestCase):
    """Requirement 5: run prevention against wicket-taking."""

    def test_no_plan_dominates_another_on_the_frontier(self):
        for a, b in itertools.permutations(approaches.BOWLING_FRONTIER, 2):
            ra = approaches.BOWLING_FRONTIER[a]
            rb = approaches.BOWLING_FRONTIER[b]
            better_runs = ra["runs"] <= rb["runs"]
            better_wkts = ra["wicket"] >= rb["wicket"]
            strictly = ra["runs"] < rb["runs"] or ra["wicket"] > rb["wicket"]
            self.assertFalse(better_runs and better_wkts and strictly,
                             "%s dominates %s" % (a, b))

    def test_taking_more_wickets_costs_runs(self):
        order = sorted(BOWL, key=lambda w: approaches.BOWLING_FRONTIER[w]["wicket"])
        runs = [approaches.BOWLING_FRONTIER[w]["runs"] for w in order]
        self.assertEqual(runs, sorted(runs),
                         "plans ordered by wickets must also be ordered by "
                         "runs conceded, or one is a free lunch")

    def test_attacking_bowling_takes_wickets_and_leaks(self):
        for bat in BAT:
            agg = _summary(_dist(bat, "aggressive"))
            dfn = _summary(_dist(bat, "defensive"))
            self.assertGreater(agg["wicket"], dfn["wicket"], bat)
            self.assertGreater(agg["runs"], dfn["runs"], bat)

    def test_boundary_denial_plans_actually_deny_boundaries(self):
        for bat in ("aggressive", "ultra"):
            base = _dist(bat, "balanced")
            for plan in ("mixed", "variation"):
                got = _dist(bat, plan)
                self.assertLess(got["Four"] + got["Six"],
                                base["Four"] + base["Six"],
                                "%s vs %s" % (bat, plan))


class NoDominantChoiceTests(unittest.TestCase):
    """Requirements 6 and 14: counters work, nothing is unbeatable."""

    #: Runs a wicket is worth across an innings. The same sweep
    #: ``tools/calibrate_approaches.py`` uses.
    COSTS = (5.0, 10.0, 15.0, 20.0, 25.0)

    def _grid(self):
        out = {}
        for pitch, phase in itertools.product(PITCHES, PHASE_BALLS):
            for bat, bowl in PAIRINGS:
                out[(pitch, phase, bat, bowl)] = _summary(
                    _dist(bat, bowl, pitch, phase))
        return out

    def setUp(self):
        self.grid = self._grid()

    def _value(self, cell, cost):
        return cell["runs"] - cost * cell["wicket"]

    def test_no_batting_approach_is_best_everywhere(self):
        best = set()
        for pitch, phase in itertools.product(PITCHES, PHASE_BALLS):
            for cost in self.COSTS:
                for bowl in BOWL:
                    best.add(max(BAT, key=lambda b: self._value(
                        self.grid[(pitch, phase, b, bowl)], cost)))
        self.assertGreater(len(best), 1,
                           "one batting intent answers every situation")
        self.assertEqual(best, set(BAT),
                         "every batting approach must be the right call "
                         "somewhere, or it is a button nobody should press")

    def test_no_bowling_approach_is_best_everywhere(self):
        best = set()
        for pitch, phase in itertools.product(PITCHES, PHASE_BALLS):
            for cost in self.COSTS:
                for bat in BAT:
                    best.add(min(BOWL, key=lambda w: self._value(
                        self.grid[(pitch, phase, bat, w)], cost)))
        self.assertGreater(len(best), 1)
        self.assertEqual(best, set(BOWL),
                         "every bowling plan must be the right call somewhere")

    def test_every_batting_intent_has_a_bowling_answer(self):
        """Nothing a batter picks is safe from every plan."""
        for bat in BAT:
            worst = min(BOWL, key=lambda w: self._value(
                self.grid[("Even", "middle", bat, w)], 15.0))
            best = max(BOWL, key=lambda w: self._value(
                self.grid[("Even", "middle", bat, w)], 15.0))
            self.assertNotEqual(
                worst, best,
                "%s plays the same against every plan" % bat)

    def test_variation_is_the_answer_to_a_slogger_and_not_to_a_blocker(self):
        """The counter structure must be cricket, not a flat bonus."""
        slog_bal = self.grid[("Even", "death", "ultra", "balanced")]
        slog_var = self.grid[("Even", "death", "ultra", "variation")]
        block_bal = self.grid[("Even", "death", "defensive", "balanced")]
        block_var = self.grid[("Even", "death", "defensive", "variation")]
        self.assertLess(slog_var["runs"], slog_bal["runs"])
        self.assertGreater(
            slog_var["wicket"] / slog_bal["wicket"],
            block_var["wicket"] / block_bal["wicket"],
            "pace off must hurt a slogger more than it hurts a blocker")

    def test_the_import_time_guards_actually_catch_their_bugs(self):
        """The assertions in approaches._validate are the real safety net.

        A table of numbers cannot be reviewed for dominance by reading it —
        the original bowling frontier had Variation beating Balanced on every
        axis and nobody spotted it — so the guards must demonstrably fire.
        """
        import importlib
        try:
            approaches.BOWLING_FRONTIER["variation"] = {
                "runs": 0.95, "boundary_share": 0.78, "wicket": 1.14}
            with self.assertRaises(AssertionError) as caught:
                approaches._validate()
            self.assertIn("dominates", str(caught.exception))
        finally:
            importlib.reload(approaches)
        try:
            approaches.BATTING_FRONTIER["ultra"] = {
                "runs": 1.42, "boundary_share": 1.95, "wicket": 0.50}
            with self.assertRaises(AssertionError) as caught:
                approaches._validate()
            self.assertIn("must increase", str(caught.exception))
        finally:
            importlib.reload(approaches)
        # Reloading rebuilds the normalised tables; make sure they came back.
        self.assertAlmostEqual(
            sum(approaches.BAT_MIX[k] / sum(approaches.BAT_MIX.values())
                * approaches.BATTING_NORMALISED[k]["runs"]
                for k in approaches.BAT_MIX), 1.0, places=9)

    def test_counters_are_bounded(self):
        for key, row in approaches.COUNTERS.items():
            for axis, v in row.items():
                self.assertGreaterEqual(v, 0.70, "%s %s" % (key, axis))
                self.assertLessEqual(v, 1.45, "%s %s" % (key, axis))


class MeanNeutralityTests(unittest.TestCase):
    """Requirement 2: the real-data baseline stays the starting point."""

    def test_the_approach_layer_does_not_move_the_innings_level(self):
        doc = baselines.load(format_name="T20", reload=True)
        self.assertIsNotNone(doc)
        comp = baselines.competition_key(doc)
        bt = sum(approaches.BAT_MIX.values())
        wt = sum(approaches.BOWL_MIX.values())
        for phase in ("powerplay", "middle", "death"):
            base = baselines.smoothed(doc, "T20/%s/%s" % (comp, phase))
            runs = wkt = 0.0
            for b, bs in approaches.BAT_MIX.items():
                for w, ws in approaches.BOWL_MIX.items():
                    p = (bs / bt) * (ws / wt)
                    e = approaches.expected(b, w, base, 75.0)
                    runs += p * e["runs"]
                    wkt += p * e["wicket"]
            base_runs = expected_runs(base) + base["Extras"]
            self.assertAlmostEqual(runs / base_runs, 1.0, delta=0.03, msg=phase)
            self.assertAlmostEqual(wkt / base["Wicket"], 1.0, delta=0.03,
                                   msg=phase)

    def test_the_mix_correction_is_measured_not_configured(self):
        cfg = get_config()
        self.assertNotIn("approach_mix_correction", cfg.get("conditions", {}))
        doc = baselines.load(format_name="T20")
        base = baselines.smoothed(doc, "T20/%s"
                                  % baselines.competition_key(doc))
        fix = approaches.mix_correction(base)
        self.assertIn("runs", fix)
        self.assertGreater(fix["runs"], 0.5)
        self.assertLess(fix["runs"], 2.0)


class NoOverlapTests(unittest.TestCase):
    """Requirement 3: no influence may be applied twice."""

    def test_realism_mode_runs_only_the_allowlisted_legacy_layers(self):
        allow = ri.approach_context_overrides()
        self.assertFalse(allow["base_tables"])
        self.assertEqual(set(allow["allow_layers"]),
                         {"predictability", "chemistry"})
        for layer in ("pitch", "phase", "momentum", "fatigue", "mind_games",
                      "carry"):
            self.assertNotIn(layer, allow["allow_layers"],
                             "%s is modelled by the realism engine already"
                             % layer)

    def test_the_allowlist_names_only_real_layers(self):
        for layer in ri.approach_context_overrides()["allow_layers"]:
            self.assertIn(layer, am.SITUATIONAL_LAYERS, layer)

    def test_suppressed_layers_really_are_inert(self):
        base = {"Dot": 1.0, "Single": 1.0, "Double": 1.0, "Three": 1.0,
                "Four": 1.0, "Six": 1.0, "Wicket": 1.0, "Extras": 1.0}
        ctx = dict(ri.approach_context_overrides(),
                   pitch="Dead", phase="death", bowling_type="Off spin",
                   bowler_over_index=4, batting_momentum=True,
                   bowling_momentum=True, mind_games=True,
                   carry={"mult": {"Six": 9.0}})
        got = am.apply_approach_modifiers(base, "ultra", "variation",
                                          bowler_rating=90, context=ctx)
        self.assertEqual(got, base,
                         "with only predictability and chemistry allowed and "
                         "neither triggered, the legacy layer must be a no-op")

    def test_legacy_mode_still_applies_everything(self):
        base = {o: 1.0 for o in OUTCOMES}
        ctx = {"pitch": "Dead", "phase": "death", "bowling_type": "Fast",
               "bowler_over_index": 4, "batting_momentum": True}
        got = am.apply_approach_modifiers(base, "ultra", "variation",
                                          bowler_rating=90, context=ctx)
        self.assertNotEqual(got, base)

    def test_phase_is_not_applied_twice(self):
        """The baseline carries phase; the approach layer must not repeat it."""
        pp = approaches.targets("ultra", "balanced")
        death = approaches.targets("ultra", "balanced")
        self.assertEqual(pp, death,
                         "approach targets must not depend on phase — the "
                         "baseline is already conditioned on it")
        # But the resulting distribution must still differ by phase, because
        # the baseline it tilts does.
        self.assertNotEqual(_dist("ultra", "balanced", phase="powerplay"),
                            _dist("ultra", "balanced", phase="death"))


class NoIdentityAbilityTests(unittest.TestCase):
    """Requirement 9: ratings and styles are the only permanent ability."""

    def test_identity_derived_ability_is_off_in_realism_mode(self):
        self.assertFalse(ri.allow_identity_ability())

    def test_two_identical_cards_get_identical_match_up_deltas(self):
        a = {"name": "Alpha", "batting_rating": 75, "roster_id": "r-1"}
        b = {"name": "Beta", "batting_rating": 75, "roster_id": "r-2"}
        for style in ("Off spin", "Leg spin", "Fast", "Medium-fast"):
            self.assertEqual(
                am.batting_matchup_delta(a, style, allow_identity=False),
                am.batting_matchup_delta(b, style, allow_identity=False),
                style)
            self.assertEqual(
                am.batting_matchup_delta(a, style, allow_identity=False), 0.0)

    def test_legacy_mode_keeps_its_inferred_profiles(self):
        a = {"name": "Alpha", "batting_rating": 75, "roster_id": "r-1"}
        b = {"name": "Beta", "batting_rating": 75, "roster_id": "r-2"}
        self.assertNotEqual(am.batting_matchup_delta(a, "Off spin"),
                            am.batting_matchup_delta(b, "Off spin"))

    def test_legacy_deliveries_still_get_their_identity_deltas(self):
        """The regression this gate actually caused, at the call site.

        ``allow_identity_ability()`` reads the config, which is set
        regardless of mode — so passing it straight through switched the
        fallback off in LEGACY mode too. The helper's own test still passed
        (it calls with the default), and the only thing that noticed was
        ``tools/trait_balance``'s mirrored neutral control, where two
        identical XIs stopped splitting their mirror 50/50. So this asserts
        the behaviour where the bug was: through ``calculate_outcome``.
        """
        import random
        from engine import ball_outcome
        from engine.format_config import get_format
        os.environ.pop("SIM_REALISM_ENABLED", None)
        clear_cache()
        self.addCleanup(clear_cache)
        fmt = get_format("T20")
        seen = []
        original = ball_outcome.calculate_outcome

        def spy(batter, bowler, *a, **kw):
            from engine.approach_modifiers import batting_matchup_delta
            seen.append(batting_matchup_delta(
                batter, bowler.get("bowling_type"),
                allow_identity=(True
                                if not ri.live_for(kw.get("format_config"))
                                else ri.allow_identity_ability())))
            return original(batter, bowler, *a, **kw)

        bowler = {"name": "W", "bowling_rating": 75, "fielding_rating": 70,
                  "bowling_hand": "Right", "bowling_type": "Off spin"}
        for rid in ("r-alpha", "r-beta"):
            random.seed(3)
            spy({"name": "X", "batting_rating": 75, "batting_hand": "Right",
                 "roster_id": rid}, bowler, "Even", {"boundaries": 0}, 8, 10,
                format_config=fmt, balls_faced=12,
                batting_approach="balanced", bowling_approach="balanced",
                approach_context={"pitch": "Even", "phase": "middle",
                                  "bowling_type": "Off spin"})
        self.assertNotEqual(seen[0], seen[1],
                            "legacy mode must keep its inferred profiles — "
                            "disabling them outside realism mode is a "
                            "behaviour change")

    def test_an_explicit_profile_is_still_data_not_an_invention(self):
        self.assertEqual(
            am.batting_matchup_delta({"bat_rating": 75, "vs_spin": 67},
                                     "Off spin", allow_identity=False),
            -8.0)

    def test_the_distribution_ignores_everything_but_ratings_and_styles(self):
        a = _dist("ultra", "variation")
        b = build(_ctx("ultra", "variation", extra={
            "roster_id": "x", "player_id": 99, "name": "Someone Else",
            "overall_rating": 99, "form": 90}))
        self.assertEqual(a, b)


class PitchProbabilisticTests(unittest.TestCase):
    """Requirement 7: pitches shift probabilities, they do not set totals."""

    def test_pitches_are_ordered_but_overlapping(self):
        rows = {p: _summary(_dist("balanced", "balanced", p)) for p in PITCHES}
        self.assertGreater(rows["Dead"]["runs"], rows["Green"]["runs"])
        self.assertLess(rows["Dead"]["wicket"], rows["Green"]["wicket"])
        # Nothing is deterministic: every surface keeps every outcome live.
        for pitch, row in rows.items():
            self.assertGreater(row["wicket"], 0.005, pitch)
            self.assertGreater(row["boundary"], 0.02, pitch)

    def test_a_bowling_pitch_cannot_stop_an_attacking_batter_scoring(self):
        green = _summary(_dist("ultra", "aggressive", "Green"))
        self.assertGreater(green["runs"], 1.0,
                           "a surface must not veto an intent outright")

    def test_no_par_score_ceiling_is_applied_in_realism_mode(self):
        for name in ("score_ceiling", "contest", "pitch_character"):
            self.assertFalse(ri.drama_hook_enabled(name), name)

    def test_pitch_effects_stay_inside_their_bound(self):
        cfg = get_config()
        bound = float(cfg["conditions"]["bound"])
        for pitch in PITCHES:
            for wear in (0.0, 0.5, 1.0):
                for style in ("Fast", "Off spin"):
                    fam = conditions.pitch_multipliers(pitch, wear, style, cfg)
                    for o, m in fam.items():
                        self.assertLessEqual(m, bound + 1e-9,
                                             "%s %s %s" % (pitch, wear, o))
                        self.assertGreaterEqual(m, 1.0 / bound - 1e-9,
                                                "%s %s %s" % (pitch, wear, o))


class BoundedConditionsTests(unittest.TestCase):
    """Requirement 8: weather, dew and wear are bounded, not invented."""

    def test_dew_and_wear_are_bounded(self):
        cfg = get_config()
        bound = float(cfg["conditions"]["bound"])
        for dew in (0.0, 0.5, 1.0):
            for inn in (1, 2):
                fam = conditions.weather_multipliers(dew, True, inn, "death",
                                                     cfg)
                for o, m in fam.items():
                    self.assertLessEqual(m, bound + 1e-9)
                    self.assertGreaterEqual(m, 1.0 / bound - 1e-9)

    def test_dew_only_moves_a_night_chase(self):
        day = conditions.weather_multipliers(0.0, False, 1, "middle")
        self.assertEqual(set(day.values()), {1.0})

    def test_conditions_cannot_outweigh_the_baseline(self):
        """Everything at once must stay inside the joint bound."""
        cfg = get_config()
        total = float(cfg["conditions"]["total_bound"])
        fam = conditions.combine([
            conditions.pitch_multipliers("Green", 1.0, "Off spin", cfg),
            conditions.weather_multipliers(1.0, True, 2, "death", cfg),
            conditions.fatigue_multipliers(0.0, cfg),
            conditions.wicket_pressure_multipliers(9, 0, cfg),
            conditions.milestone_multipliers(120, cfg),
            conditions.chase_multipliers(30.0, 9.0, 6, 9, cfg),
            conditions.style_multipliers("spin_away", cfg),
        ], total)
        for o, m in fam.items():
            self.assertLessEqual(m, total + 1e-9, o)
            self.assertGreaterEqual(m, 1.0 / total - 1e-9, o)


class RatingFairnessTests(unittest.TestCase):
    """Requirement 14: ratings remain the permanent measure of ability."""

    def test_ratings_outweigh_the_approach_choice(self):
        """A far better batter on a cautious plan still beats a poor one slogging."""
        good = _summary(_dist("rotate", "balanced", bat_rating=95,
                              bowl_rating=70))
        poor = _summary(_dist("ultra", "balanced", bat_rating=45,
                              bowl_rating=70))
        self.assertGreater(good["runs"], poor["runs"],
                           "a 50-point rating gap must beat an intent change")

    def test_the_approach_choice_still_matters_at_equal_ratings(self):
        a = _summary(_dist("defensive", "balanced"))
        b = _summary(_dist("ultra", "balanced"))
        self.assertGreater(b["runs"] / a["runs"], 1.5,
                           "intent must be a real decision")

    def test_approach_effects_are_the_same_for_equally_rated_cards(self):
        for bat, bowl in PAIRINGS:
            a = _dist(bat, bowl, bat_rating=80, bowl_rating=72)
            b = _dist(bat, bowl, bat_rating=80, bowl_rating=72)
            self.assertEqual(a, b, "%s/%s" % (bat, bowl))

    def test_every_pairing_keeps_a_rating_gradient(self):
        for bat, bowl in PAIRINGS:
            weak = _summary(_dist(bat, bowl, bat_rating=50))
            strong = _summary(_dist(bat, bowl, bat_rating=90))
            self.assertGreater(strong["runs"], weak["runs"],
                               "%s/%s" % (bat, bowl))
            self.assertLess(strong["wicket"], weak["wicket"],
                            "%s/%s" % (bat, bowl))

    def test_no_approach_makes_a_premium_batter_undismissable(self):
        floor = float(get_config()["rating_curve"]["min_wicket_share"])
        for bat, bowl in PAIRINGS:
            dist = _dist(bat, bowl, bat_rating=99, bowl_rating=30)
            self.assertGreaterEqual(dist["Wicket"], floor - 1e-12,
                                    "%s/%s" % (bat, bowl))


class WinProbabilityTests(unittest.TestCase):
    """Requirement 10: the captain reasons about winning."""

    def test_a_bigger_total_is_worth_more(self):
        last = -1.0
        for total in range(80, 281, 20):
            p = wp.win_with_total(total)
            self.assertGreater(p, last, total)
            self.assertTrue(0.0 <= p <= 1.0)
            last = p

    def test_the_fifty_fifty_total_is_a_realistic_one(self):
        lo, hi = 120, 260
        for _ in range(40):
            mid = (lo + hi) / 2.0
            if wp.win_with_total(mid) < 0.5:
                lo = mid
            else:
                hi = mid
        self.assertGreater((lo + hi) / 2.0, 150.0)
        self.assertLess((lo + hi) / 2.0, 215.0)

    def test_runs_matter_more_and_wickets_less_as_an_innings_runs_out(self):
        """The whole point of the objective: it changes shape over an innings."""
        early = wp.over_win_delta(10, 108, 10)
        late = wp.over_win_delta(170, 6, 4)
        early_ratio = early[1] / early[0]
        late_ratio = late[1] / late[0]
        self.assertGreater(early_ratio, late_ratio * 3.0,
                           "a wicket must be far dearer early than late")
        self.assertGreater(late[0], early[0],
                           "a run must be worth more late than early")

    def test_a_chase_and_a_first_innings_agree_about_a_total(self):
        """Both halves must value the same scoreboard identically."""
        from services import bot_tactics as bt
        for total in (140, 180, 220):
            mine = 1.0 - wp.chase_success(total + 1, 120, 10)
            theirs = 1.0 - bt.chase_win_prob(total + 1, 120, 10)
            self.assertAlmostEqual(mine, theirs, places=6, msg=str(total))

    def test_the_objective_is_bounded_and_safe_at_the_edges(self):
        for args in ((0, 0, 10), (0, 120, 0), (500, 1, 1), (-5, 60, 5)):
            p = wp.first_innings_win_prob(*args)
            self.assertTrue(0.0 <= p <= 1.0, str(args))

    def test_the_bot_uses_it_only_when_realism_is_driving(self):
        from services import bot_tactics as bt
        self.assertTrue(hasattr(bt, "_realism_objective"))
        self.assertFalse(bt._realism_objective({}))


class NoResultSteeringTests(unittest.TestCase):
    """Requirement 11: no forced finishes, no convergence."""

    def test_every_steering_hook_is_off_in_realism_mode(self):
        for name in ("score_ceiling", "contest", "pitch_character",
                     "clutch_finale", "wicket_mixup"):
            self.assertFalse(ri.drama_hook_enabled(name), name)

    def test_the_hooks_still_exist_for_exhibition_modes(self):
        from services import match_drama
        for name in ("make_score_ceiling_hook", "make_contest_hook",
                     "make_pitch_character_hook", "maybe_mixup"):
            self.assertTrue(callable(getattr(match_drama, name)), name)

    def test_a_pairing_cannot_see_a_planned_result(self):
        a = _dist("ultra", "variation", innings=2, target=181, runs=175,
                  balls_bowled=118)
        b = build(_ctx("ultra", "variation", innings=2, target=181, runs=175,
                       balls_bowled=118,
                       extra={"_planned_winner_user_id": 7,
                              "_planned_super_overs": True}))
        self.assertEqual(a, b)

    def test_the_last_ball_of_a_tight_chase_stays_a_contest(self):
        for bat in BAT:
            dist = _dist(bat, "variation", innings=2, target=181, runs=180,
                         balls_bowled=119, wickets=6)
            self.assertGreater(dist["Dot"], 0.10, bat)
            self.assertGreater(dist["Wicket"], 0.01, bat)


class PreservedRulesTests(unittest.TestCase):
    """Requirement 12: extras, wickets, strike rotation, free hits."""

    def test_free_hit_restrictions_hold_for_every_pairing(self):
        import random
        from engine.ball_outcome import calculate_outcome
        from engine.format_config import get_format
        os.environ["SIM_REALISM_ENABLED"] = "1"
        clear_cache()
        baselines.clear_cache()
        self.addCleanup(os.environ.pop, "SIM_REALISM_ENABLED", None)
        self.addCleanup(clear_cache)
        fmt = get_format("T20")
        random.seed(99)
        outs = 0
        for bat, bowl in PAIRINGS:
            for _ in range(120):
                oc = calculate_outcome(
                    batter={"name": "b", "batting_rating": 80,
                            "batting_hand": "Right"},
                    bowler={"name": "w", "bowling_rating": 75,
                            "fielding_rating": 70, "bowling_hand": "Right",
                            "bowling_type": "Fast-medium"},
                    pitch="Even", streak={"boundaries": 0}, over_number=17,
                    batter_runs=30, balls_faced=20, format_config=fmt,
                    free_hit=True, batting_approach=bat,
                    bowling_approach=bowl,
                    approach_context={"pitch": "Even", "phase": "death",
                                      "bowling_type": "Fast-medium"})
                self.assertIn(oc["runs"], (0, 1, 2, 3, 4, 6))
                if oc["batter_out"]:
                    outs += 1
                    self.assertEqual(oc["wicket_type"], "Run Out",
                                     "%s/%s" % (bat, bowl))
                if oc["is_extra"]:
                    self.assertIn(oc["extra_type"],
                                  ("Wide", "No Ball", "Leg Bye", "Byes"))
        self.assertGreater(outs, 0, "a free hit must still allow a run out")

    def test_extras_are_not_an_approach_decision(self):
        """A captain's intent does not change how often a bowler sprays one."""
        rates = {(b, w): _dist(b, w)["Extras"] for b, w in PAIRINGS}
        spread = max(rates.values()) / min(rates.values())
        self.assertLess(spread, 1.30,
                        "extras moved %.2fx across pairings; they are a "
                        "bowler-error outcome, not a tactic" % spread)

    def test_a_wicket_still_never_awards_runs(self):
        import random
        from engine.ball_outcome import calculate_outcome
        from engine.format_config import get_format
        os.environ["SIM_REALISM_ENABLED"] = "1"
        clear_cache()
        baselines.clear_cache()
        self.addCleanup(os.environ.pop, "SIM_REALISM_ENABLED", None)
        self.addCleanup(clear_cache)
        fmt = get_format("T20")
        random.seed(7)
        for bat, bowl in PAIRINGS:
            for _ in range(60):
                oc = calculate_outcome(
                    batter={"name": "b", "batting_rating": 60,
                            "batting_hand": "Right"},
                    bowler={"name": "w", "bowling_rating": 85,
                            "fielding_rating": 70, "bowling_hand": "Right",
                            "bowling_type": "Fast"},
                    pitch="Green", streak={"boundaries": 0}, over_number=3,
                    batter_runs=2, balls_faced=2, format_config=fmt,
                    batting_approach=bat, bowling_approach=bowl)
                if oc["batter_out"] and oc["wicket_type"] != "Run Out":
                    self.assertEqual(oc["runs"], 0, "%s/%s" % (bat, bowl))


if __name__ == "__main__":
    unittest.main()


class LiveDistributionTests(unittest.TestCase):
    """Requirement 14: realistic score distributions and natural upsets.

    These play real innings through ``services.cipl_match``, so they are the
    slow ones. Sample sizes are the smallest that make the assertions mean
    something rather than the largest that would be informative — the full
    picture is ``python -m tools.calibrate_approaches live`` and
    ``python -m tools.compare_realism``.
    """

    OVERS = 20

    @classmethod
    def setUpClass(cls):
        os.environ["SIM_REALISM_ENABLED"] = "1"
        clear_cache()
        baselines.clear_cache()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("SIM_REALISM_ENABLED", None)
        clear_cache()
        baselines.clear_cache()

    def _innings(self, n, pitch="Even", bat=None, bowl=None, bat_bias=0,
                 seed0=0):
        import random
        from tools import pitch_calibration as pc
        from tools import realism_audit as ra
        import services.cipl_match as cm
        totals, wickets = [], []
        for i in range(n):
            random.seed(seed0 + i)
            with pc._muted_logging():
                xi_a = pc.make_xi("A", 2000 + i)
                xi_b = pc.make_xi("B", 5000 + i)
                if bat_bias:
                    xi_a = [dict(p, bat_rating=max(30, min(100, float(
                        p.get("bat_rating") or p.get("rating") or 50)
                        + bat_bias))) for p in xi_a]
                st = cm.build_cipl_state(1, self.OVERS, 10, 20, 111, 222,
                                         xi_a, xi_b, "A", "B", -1, pitch,
                                         False, ball_format="T20")
                guard = 0
                while not cm.is_innings_over(st) and guard < 40:
                    guard += 1
                    elig = cm.eligible_bowlers(st)
                    if not elig:
                        break
                    st["current_bowler"] = random.choice(elig[:4])
                    st["batting_approach"] = bat or random.choices(
                        ra.BAT_APPROACHES, ra.BAT_WEIGHTS)[0]
                    st["bowling_approach"] = bowl or random.choices(
                        ra.BOWL_APPROACHES, ra.BOWL_WEIGHTS)[0]
                    cm.simulate_over(st)
            totals.append(st["total_runs"])
            wickets.append(st["total_wickets"])
        return totals, wickets

    def test_innings_totals_have_a_realistic_spread(self):
        import statistics
        totals, wickets = self._innings(24, seed0=100)
        mean = statistics.mean(totals)
        self.assertGreater(mean, 140, "totals: %s" % sorted(totals))
        self.assertLess(mean, 235, "totals: %s" % sorted(totals))
        self.assertGreater(max(totals) - min(totals), 50,
                           "every innings landed on the same total")
        self.assertGreater(statistics.mean(wickets), 3.5)
        self.assertLess(statistics.mean(wickets), 9.0)

    def test_no_single_pairing_runs_away_with_the_innings(self):
        """Ultra Attack held for 20 overs must not simply be the answer."""
        import statistics
        ultra, ultra_w = self._innings(10, bat="ultra", bowl="variation",
                                       seed0=300)
        balanced, bal_w = self._innings(10, bat="balanced", bowl="variation",
                                        seed0=300)
        self.assertGreater(statistics.mean(ultra_w),
                           statistics.mean(bal_w) - 0.5,
                           "all-out attack must cost wickets over an innings")
        self.assertGreater(max(balanced), min(ultra),
                           "the two must overlap, not separate cleanly")

    def test_a_weaker_batting_side_still_sometimes_outscores_a_stronger_one(self):
        """Natural upsets, at a squad gap a league actually contains.

        The bias is +/-5 on every batter — a ten-point difference between the
        two line-ups — because that is roughly what separates a good squad
        from a poor one. Measured at +/-12 the stronger side won 14 of 14:
        over a full twenty overs a twenty-four-point team-wide batting gap is
        genuinely decisive, and asserting otherwise would be asserting that
        ratings do not matter. The upset has to live at a realistic gap, not
        at any gap.
        """
        strong, _ = self._innings(16, bat_bias=5, seed0=700)
        weak, _ = self._innings(16, bat_bias=-5, seed0=700)
        wins = sum(1 for s, w in zip(strong, weak) if s > w)
        self.assertGreater(wins, len(strong) * 0.55,
                           "a ten-point batting edge must usually tell: "
                           "%s vs %s" % (strong, weak))
        self.assertLess(wins, len(strong),
                        "and must not be a guarantee: %s vs %s"
                        % (strong, weak))
        self.assertGreater(max(weak), min(strong),
                           "the two distributions must overlap")

    def test_pitches_shift_totals_without_fixing_them(self):
        import statistics
        flat, _ = self._innings(12, pitch="Dead", seed0=900)
        green, _ = self._innings(12, pitch="Green", seed0=900)
        self.assertGreater(statistics.mean(flat), statistics.mean(green),
                           "a dead pitch must score more than a green one")
        self.assertGreater(max(green), min(flat),
                           "the pitch must shift the distribution, not "
                           "separate it: a green-pitch innings has to be "
                           "able to beat a dead-pitch one")
