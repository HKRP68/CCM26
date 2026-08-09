"""Tests for the Challenge League over-by-over Approach simulation.

Covers engine/approach_modifiers.py and services/cipl_match.py — both pure
Python (no Telegram / SQLAlchemy needed), so they run under plain unittest.
"""

import json
import os
import random
import unittest

from engine import approach_modifiers as am
from services import cipl_match as cm


BASE_WEIGHTS = {
    "Dot": 20.0, "Single": 33.0, "Double": 10.0, "Three": 3.0,
    "Four": 16.0, "Six": 10.0, "Wicket": 4.0, "Extras": 2.0,
}


class ApproachModifierTests(unittest.TestCase):
    def test_balanced_is_neutral(self):
        out = am.apply_approach_modifiers(BASE_WEIGHTS, "balanced", "balanced")
        self.assertEqual(out, BASE_WEIGHTS)

    def test_unknown_keys_fall_back_to_balanced(self):
        out = am.apply_approach_modifiers(BASE_WEIGHTS, "nonsense", "garbage")
        self.assertEqual(out, BASE_WEIGHTS)

    def test_defensive_batting_lowers_boundaries_and_wickets(self):
        out = am.apply_approach_modifiers(BASE_WEIGHTS, "defensive", "balanced")
        self.assertLess(out["Four"], BASE_WEIGHTS["Four"])
        self.assertLess(out["Six"], BASE_WEIGHTS["Six"])
        self.assertLess(out["Wicket"], BASE_WEIGHTS["Wicket"])
        self.assertGreater(out["Dot"], BASE_WEIGHTS["Dot"])

    def test_ultra_batting_raises_six_and_wicket(self):
        out = am.apply_approach_modifiers(BASE_WEIGHTS, "ultra", "balanced")
        self.assertGreater(out["Six"], BASE_WEIGHTS["Six"])
        self.assertGreater(out["Wicket"], BASE_WEIGHTS["Wicket"])

    def test_aggressive_bowling_raises_wickets(self):
        out = am.apply_approach_modifiers(BASE_WEIGHTS, "balanced", "aggressive")
        self.assertGreater(out["Wicket"], BASE_WEIGHTS["Wicket"])

    def test_mixed_six_suppression_scales_with_rating(self):
        hi = am.apply_approach_modifiers(BASE_WEIGHTS, "balanced", "mixed", bowler_rating=85)
        lo = am.apply_approach_modifiers(BASE_WEIGHTS, "balanced", "mixed", bowler_rating=45)
        self.assertLess(hi["Six"], lo["Six"])  # high-rated bowler concedes fewer sixes

    def test_combined_multipliers(self):
        """The pick-only layers multiply: batting intent × bowling plan ×
        counter × the special combination the pair makes."""
        out = am.apply_approach_modifiers(BASE_WEIGHTS, "ultra", "aggressive")
        expected = (BASE_WEIGHTS["Six"]
                    * am._BATTING_MULT["ultra"]["Six"]
                    * am._BOWLING_MULT["aggressive"]["Six"]
                    * am._COUNTER_MULT[("ultra", "aggressive")]["Six"]
                    * am.SPECIAL_COMBOS[("ultra", "aggressive")]["mult"]["Six"])
        self.assertAlmostEqual(out["Six"], expected, places=5)

    def test_the_counter_matrix_makes_the_over_non_separable(self):
        """Without an interaction term the game collapses to one right answer.

        Ultra into Variation must not be the same trade as Ultra into Balanced
        scaled by a constant — if it were, the best batting approach would be
        the same whatever the bowler did, which is exactly the bug the counter
        matrix exists to fix.
        """
        def six_ratio(bowl):
            a = am.apply_approach_modifiers(BASE_WEIGHTS, "ultra", bowl)["Six"]
            b = am.apply_approach_modifiers(BASE_WEIGHTS, "rotate", bowl)["Six"]
            return a / b

        self.assertNotAlmostEqual(six_ratio("variation"), six_ratio("balanced"),
                                  places=3)

    def test_no_negative_weights(self):
        weird = dict(BASE_WEIGHTS, Six=-5.0)
        out = am.apply_approach_modifiers(weird, "defensive", "defensive")
        self.assertGreaterEqual(out["Six"], 0.0)


class ApproachBalanceTests(unittest.TestCase):
    """The approach layer has to stay a *choice*.

    Before this was enforced, Ultra Attack beat every other batting approach in
    every bowling column and Defensive bowling beat every plan in every batting
    row: two dominant strategies, and the other eight options were decoration —
    which is exactly why the bots were easy to beat by rote.

    Scoring uses ``services.bot_tactics``, whose ball model is fitted to the
    live engine, across the situations a player actually meets (phases, rating
    match-ups, pitches). A raw multiplier table with no rating model is not
    enough here: the rating gap reshapes the outcome mix far more than the
    approaches do, so dominance measured without it gives the wrong answer.
    """

    BAT = [k for k, _e, _l in am.BATTING_APPROACHES]
    BOWL = [k for k, _e, _l in am.BOWLING_APPROACHES]

    # Batting Defensive is a survival tool and Balanced bowling is the neutral
    # baseline; neither is meant to top a runs-scored table, so neither is held
    # to the "best somewhere" bar. Everything else must earn its place.
    SITUATIONAL = {"bat": {"defensive"}, "bowl": {"balanced"}}

    def _states(self):
        from tests.test_bot_captain import _player
        out = []
        for over in (3, 9, 18):
            for bat_r, bowl_r in ((70, 70), (85, 60), (55, 85)):
                for pitch, faced in (("Hard", 10), ("Flat", 0), ("Green", 10)):
                    order = [_player(100 + i, f"B{i}", "Batsman", 20, bat_r)
                             for i in range(11)]
                    out.append({
                        "overs": 20, "current_over": over, "pitch_type": pitch,
                        "batting_order": order, "striker_idx": 0,
                        "total_wickets": 2, "wicket_limit": 10,
                        "bat_stats": {str(order[0]["roster_id"]):
                                      {"runs": int(faced * 1.2), "balls": faced}},
                        "current_bowler": _player(1, "W", "Bowler", bowl_r, 30),
                    })
        return out

    def _grid(self, state):
        """Projected innings total for each of the 25 match-ups in ``state``."""
        from services import bot_tactics as bt
        base = bt.ball_probabilities(state)
        grid = {}
        for ba in self.BAT:
            for bo in self.BOWL:
                p = bt.approach_probabilities(base, ba, bo, 75)
                rpo = sum(p[k] * bt.RUN_VALUE[k] for k in p) * 6.0
                wpo = p["Wicket"] * 6.0
                overs = min(20.0, 10.0 / wpo) if wpo > 0 else 20.0
                grid[(ba, bo)] = rpo * overs
        return grid

    def test_every_approach_is_the_right_answer_somewhere(self):
        best_bat, best_bowl = set(), set()
        for state in self._states():
            grid = self._grid(state)
            for bo in self.BOWL:
                best_bat.add(max(self.BAT, key=lambda ba: grid[(ba, bo)]))
            for ba in self.BAT:
                best_bowl.add(min(self.BOWL, key=lambda bo: grid[(ba, bo)]))
        missing_bat = set(self.BAT) - best_bat - self.SITUATIONAL["bat"]
        missing_bowl = set(self.BOWL) - best_bowl - self.SITUATIONAL["bowl"]
        self.assertFalse(missing_bat,
                         f"batting {sorted(missing_bat)} is never the best reply "
                         f"to anything — it may as well not be on the keyboard")
        self.assertFalse(missing_bowl,
                         f"bowling {sorted(missing_bowl)} is never the best reply "
                         f"to anything — it may as well not be on the keyboard")

    def test_the_best_reply_depends_on_what_the_opponent_picked(self):
        """The property that makes the over a game rather than a lookup.

        The two sides are held to different bars on purpose, because the
        measured behaviour differs and pretending otherwise would make this
        test a wish rather than a check. The bowling captain faces a real
        decision in *every* situation sampled — what beats a slogger is not
        what beats an accumulator. The batting captain faces one less often:
        plenty of scorelines (the death, a big rating mismatch) genuinely do
        have one right intent whatever the bowler tries, and forcing a dilemma
        there would mean making the game lie.
        """
        varied_bat = varied_bowl = 0
        states = self._states()
        for state in states:
            grid = self._grid(state)
            if len({max(self.BAT, key=lambda ba: grid[(ba, bo)])
                    for bo in self.BOWL}) > 1:
                varied_bat += 1
            if len({min(self.BOWL, key=lambda bo: grid[(ba, bo)])
                    for ba in self.BAT}) > 1:
                varied_bowl += 1
        self.assertGreater(varied_bat, len(states) * 0.25)
        self.assertGreater(varied_bowl, len(states) * 0.80)

    def test_no_approach_is_dominated_in_every_situation(self):
        alive_bat = {k: False for k in self.BAT}
        alive_bowl = {k: False for k in self.BOWL}
        for state in self._states():
            grid = self._grid(state)
            for ba in self.BAT:
                if not any(all(grid[(o, bo)] > grid[(ba, bo)] for bo in self.BOWL)
                           for o in self.BAT if o != ba):
                    alive_bat[ba] = True
            for bo in self.BOWL:
                if not any(all(grid[(ba, o)] < grid[(ba, bo)] for ba in self.BAT)
                           for o in self.BOWL if o != bo):
                    alive_bowl[bo] = True
        dead_bat = [k for k, ok in alive_bat.items()
                    if not ok and k not in self.SITUATIONAL["bat"]]
        dead_bowl = [k for k, ok in alive_bowl.items()
                     if not ok and k not in self.SITUATIONAL["bowl"]]
        self.assertFalse(dead_bat, f"batting {dead_bat} is strictly dominated "
                                   f"in every situation tested")
        self.assertFalse(dead_bowl, f"bowling {dead_bowl} is strictly dominated "
                                    f"in every situation tested")


class SituationalLayerTests(unittest.TestCase):
    """The Approach Interaction System's context layers: pitch, phase, the mind
    game, momentum, bowler fatigue, partnership chemistry and carry-overs.

    Each one is checked by the direction it moves the over in, not by a magic
    number, so a retune of the tables stays free while the *cricket* they encode
    stays pinned.
    """

    def _runs(self, weights):
        """Expected runs per ball, the only summary of a weight dict that
        matters here — a layer that shuffles weight between dots and sixes
        without moving this has done nothing."""
        value = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4,
                 "Six": 6, "Wicket": 0, "Extras": 1}
        total = sum(weights.values())
        return sum(weights[k] * value[k] for k in weights) / total

    def _apply(self, bat, bowl, **ctx):
        return am.apply_approach_modifiers(
            BASE_WEIGHTS, bat, bowl, bowler_rating=70,
            context=am.make_context(**ctx) if ctx else None)

    def _wicket_share(self, weights):
        return weights["Wicket"] / sum(weights.values())

    # ── the layers are opt-in ────────────────────────────────────────
    def test_no_context_means_no_situational_layers(self):
        """Every caller that predates the system must be untouched by it."""
        for bat in am.BATTING_KEYS:
            for bowl in am.BOWLING_KEYS:
                bare = am.apply_approach_modifiers(BASE_WEIGHTS, bat, bowl,
                                                   bowler_rating=70)
                mult = am.approach_multipliers(bat, bowl, bowler_rating=70)
                self.assertEqual(
                    bare, {k: v * mult.get(k, 1.0) for k, v in BASE_WEIGHTS.items()},
                    f"{bat}/{bowl} picked up a situational layer with no context")

    def test_empty_context_is_harmless(self):
        """A context that knows nothing must not change the over."""
        out = self._apply("aggressive", "mixed", mind_games=False)
        bare = am.apply_approach_modifiers(BASE_WEIGHTS, "aggressive", "mixed",
                                           bowler_rating=70)
        for key in BASE_WEIGHTS:
            self.assertAlmostEqual(out[key], bare[key], places=6)

    # ── pitch ────────────────────────────────────────────────────────
    def test_pitch_rewards_hitting_on_hard_and_punishes_it_on_dry(self):
        hard = self._apply("aggressive", "balanced", pitch="Hard", mind_games=False)
        dry = self._apply("aggressive", "balanced", pitch="Dry", mind_games=False)
        self.assertGreater(self._runs(hard), self._runs(dry))

    def test_pitch_rewards_nudging_on_dry_and_punishes_it_on_dusty(self):
        dry = self._apply("rotate", "balanced", pitch="Dry", mind_games=False)
        dusty = self._apply("rotate", "balanced", pitch="Dusty", mind_games=False)
        self.assertGreater(self._runs(dry), self._runs(dusty))

    def test_flat_pitch_blunts_the_attacking_bowling_plans(self):
        flat = self._apply("balanced", "aggressive", pitch="Flat", mind_games=False)
        even = self._apply("balanced", "aggressive", pitch="Even", mind_games=False)
        self.assertLess(self._wicket_share(flat), self._wicket_share(even))

    def test_green_seam_only_bites_in_the_powerplay(self):
        early = self._apply("balanced", "balanced", pitch="Green",
                            phase="powerplay", mind_games=False)
        late = self._apply("balanced", "balanced", pitch="Green",
                           phase="death", mind_games=False)
        self.assertLess(self._runs(early), self._runs(late))

    # ── phase ────────────────────────────────────────────────────────
    def test_ultra_is_worth_more_at_the_death_and_defensive_is_worth_less(self):
        for approach, expect_more in (("ultra", True), ("defensive", False)):
            death = self._apply(approach, "balanced", phase="death", mind_games=False)
            middle = self._apply(approach, "balanced", phase="middle", mind_games=False)
            if expect_more:
                self.assertGreater(self._runs(death), self._runs(middle))
            else:
                self.assertLess(self._runs(death), self._runs(middle))

    def test_powerplay_field_restrictions_cost_the_bowler_wickets(self):
        pp = self._apply("balanced", "balanced", phase="powerplay", mind_games=False)
        mid = self._apply("balanced", "balanced", phase="middle", mind_games=False)
        self.assertLess(self._wicket_share(pp), self._wicket_share(mid))

    def test_hitting_spin_off_the_square_is_harder_in_the_middle(self):
        spin = self._apply("aggressive", "balanced", phase="middle",
                           bowling_type="Off spin", mind_games=False)
        pace = self._apply("aggressive", "balanced", phase="middle",
                           bowling_type="Fast", mind_games=False)
        self.assertLess(self._runs(spin), self._runs(pace))

    # ── the mind game ────────────────────────────────────────────────
    def test_reading_the_plan_pays_and_can_be_switched_off(self):
        self.assertIn("defensive", am._READS["rotate"])
        read = self._apply("rotate", "defensive", pitch="Even")
        blind = self._apply("rotate", "defensive", pitch="Even", mind_games=False)
        self.assertGreater(self._runs(read), self._runs(blind))
        self.assertLessEqual(self._wicket_share(read), self._wicket_share(blind))

    def test_walking_into_the_wrong_plan_costs_a_wicket_chance(self):
        """The other side of the same coin — and the doc's own example of it."""
        self.assertNotIn("variation", am._READS["aggressive"])
        missed = self._apply("aggressive", "variation", pitch="Even")
        blind = self._apply("aggressive", "variation", pitch="Even",
                            mind_games=False)
        self.assertGreater(self._wicket_share(missed), self._wicket_share(blind))
        self.assertLess(self._runs(missed), self._runs(blind))

    def test_the_mind_game_prices_both_directions(self):
        """A read bonus with no matching penalty would be a standing gift to the
        batting side. Every cell must pay one way or the other."""
        for bat in am.BATTING_KEYS:
            self.assertTrue(am._READS[bat], f"{bat} reads nothing")
            self.assertNotEqual(am._READS[bat], am.BOWLING_KEYS,
                                f"{bat} reads everything — the layer is a constant")
            for bowl in am.BOWLING_KEYS:
                live = self._apply(bat, bowl, pitch="Even")
                blind = self._apply(bat, bowl, pitch="Even", mind_games=False)
                if bowl in am._READS[bat]:
                    self.assertGreater(self._runs(live), self._runs(blind))
                else:
                    self.assertGreater(self._wicket_share(live),
                                       self._wicket_share(blind))

    # ── momentum, fatigue, chemistry ─────────────────────────────────
    def test_a_batting_side_in_the_ascendancy_carries_it_into_the_next_over(self):
        hot = self._apply("balanced", "balanced", batting_momentum=True)
        cold = self._apply("balanced", "balanced")
        self.assertGreater(self._runs(hot), self._runs(cold))

    def test_a_bowler_on_a_roll_is_more_likely_to_strike_again(self):
        hot = self._apply("balanced", "balanced", bowling_momentum=True)
        cold = self._apply("balanced", "balanced")
        self.assertGreater(self._wicket_share(hot), self._wicket_share(cold))

    def test_fatigue_only_bites_late_in_a_spell_and_gets_worse(self):
        first = self._apply("balanced", "balanced", bowler_over_index=1)
        second = self._apply("balanced", "balanced", bowler_over_index=2)
        third = self._apply("balanced", "balanced", bowler_over_index=3)
        fourth = self._apply("balanced", "balanced", bowler_over_index=4)
        self.assertEqual(self._runs(first), self._runs(second))   # nothing yet
        self.assertLess(self._wicket_share(third), self._wicket_share(second))
        self.assertLess(self._wicket_share(fourth), self._wicket_share(third))
        self.assertGreater(self._runs(fourth), self._runs(third))

    def test_a_settled_pair_scores_more_freely(self):
        settled = self._apply("balanced", "balanced", partnership_overs=5)
        fresh = self._apply("balanced", "balanced", partnership_overs=1)
        self.assertGreater(self._runs(settled), self._runs(fresh))
        # The threshold is a threshold, not a ramp.
        self.assertEqual(self._runs(fresh),
                         self._runs(self._apply("balanced", "balanced",
                                                partnership_overs=3.9)))

    # ── special combinations ─────────────────────────────────────────
    def test_every_named_combination_resolves_and_has_flavour(self):
        for (bat, bowl), combo in am.SPECIAL_COMBOS.items():
            self.assertEqual(am.special_combo(bat, bowl)["name"], combo["name"])
            name, text = am.over_flavour(bat, bowl)
            self.assertEqual(name, combo["name"])
            self.assertTrue(text)

    def test_an_unnamed_matchup_falls_back_to_the_matrix_flavour(self):
        self.assertIsNone(am.special_combo("ultra", "mixed"))
        name, text = am.over_flavour("ultra", "mixed")
        self.assertIsNone(name)
        self.assertEqual(text, am.MATRIX_FLAVOUR[("ultra", "mixed")])

    def test_the_shootout_makes_the_over_volatile(self):
        """Ultra into Aggressive must buy its runs with wickets, not for free —
        both ends of the over move, and the safe outcomes drain."""
        mult = am.approach_multipliers("ultra", "aggressive", bowler_rating=70)
        without_combo = {
            key: (am._BATTING_MULT["ultra"].get(key, 1.0)
                  * am._BOWLING_MULT["aggressive"].get(key, 1.0)
                  * am._COUNTER_MULT[("ultra", "aggressive")].get(key, 1.0))
            for key in BASE_WEIGHTS}
        self.assertGreater(mult["Six"], without_combo["Six"])
        self.assertGreater(mult["Wicket"], without_combo["Wicket"])
        self.assertLess(mult["Dot"], without_combo["Dot"])
        self.assertLess(mult["Single"], without_combo["Single"])

    def test_carry_overs_fire_only_on_their_own_condition(self):
        # Blockathon: only a strangled over releases the frustration.
        self.assertIsNone(am.combo_carry("defensive", "defensive", 7, 0))
        blocked = am.combo_carry("defensive", "defensive", 2, 0)
        self.assertEqual(blocked["name"], "The Blockathon")
        # Survival: only if the batter is still there.
        self.assertIsNone(am.combo_carry("defensive", "aggressive", 4, 1))
        survived = am.combo_carry("defensive", "aggressive", 4, 0)
        self.assertLess(survived["mult"]["Wicket"], 1.0)
        # Purist: exactly par, or nothing.
        self.assertIsNone(am.combo_carry("balanced", "balanced", 7, 0))
        self.assertIsNotNone(am.combo_carry("balanced", "balanced", 8, 0))
        # A match-up with no carry rule never produces one.
        self.assertIsNone(am.combo_carry("ultra", "mixed", 20, 0))

    def test_a_carry_actually_changes_the_next_over(self):
        carry = am.combo_carry("defensive", "defensive", 2, 0)
        with_carry = self._apply("balanced", "balanced", carry=carry)
        without = self._apply("balanced", "balanced")
        self.assertGreater(self._runs(with_carry), self._runs(without))

    def test_layers_stack_multiplicatively(self):
        """Two layers together are the product of the two — no layer is lost,
        and none is applied twice."""
        ctx = dict(pitch="Hard", phase="death", batting_momentum=True,
                   mind_games=False)
        both = am.approach_multipliers("ultra", "balanced", bowler_rating=70,
                                       context=am.make_context(**ctx))
        base = am.approach_multipliers("ultra", "balanced", bowler_rating=70)
        pitch_only = am.approach_multipliers(
            "ultra", "balanced", bowler_rating=70,
            context=am.make_context(pitch="Hard", mind_games=False))
        phase_only = am.approach_multipliers(
            "ultra", "balanced", bowler_rating=70,
            context=am.make_context(phase="death", mind_games=False))
        momentum_only = am.approach_multipliers(
            "ultra", "balanced", bowler_rating=70,
            context=am.make_context(batting_momentum=True, mind_games=False))
        for key in ("Six", "Four", "Dot"):
            expected = (base[key]
                        * (pitch_only[key] / base[key])
                        * (phase_only[key] / base[key])
                        * (momentum_only[key] / base[key]))
            self.assertAlmostEqual(both[key], expected, places=6)

    def test_no_layer_can_drive_a_weight_negative(self):
        ctx = am.make_context(pitch="Dry", phase="death", bowler_over_index=4,
                              carry={"mult": {"Six": 0.0}})
        for bat in am.BATTING_KEYS:
            for bowl in am.BOWLING_KEYS:
                out = am.apply_approach_modifiers(BASE_WEIGHTS, bat, bowl,
                                                  bowler_rating=70, context=ctx)
                for key, val in out.items():
                    self.assertGreaterEqual(val, 0.0, f"{bat}/{bowl} {key}")


class SituationalBalanceTests(ApproachBalanceTests):
    """The same no-dominant-approach bar as :class:`ApproachBalanceTests`, but
    measured through the situational layers.

    The base 5×5 game was balanced by measurement; a pitch/phase/mind-game layer
    on top of it could quietly hand one intent every column back. Re-running the
    dominance checks with the live context is what stops that shipping. The
    situations come from the parent class, so both grids are scored over the
    same phases, pitches and rating match-ups.
    """

    def _grid(self, state):
        from services import bot_tactics as bt
        base = bt.ball_probabilities(state)
        ctx = bt._approach_context(state)
        self.assertIsNotNone(ctx, "the situational context failed to build")
        grid = {}
        for ba in self.BAT:
            for bo in self.BOWL:
                p = bt.approach_probabilities(base, ba, bo, 75, context=ctx)
                rpo = sum(p[k] * bt.RUN_VALUE[k] for k in p) * 6.0
                wpo = p["Wicket"] * 6.0
                overs = min(20.0, 10.0 / wpo) if wpo > 0 else 20.0
                grid[(ba, bo)] = rpo * overs
        return grid


class ApproachContextTests(unittest.TestCase):
    """``cipl_match`` is what turns a live match state into the context above —
    and it is the *same* builder the AI captain plans with."""

    def test_phase_boundaries(self):
        s = _make_state(overs=20)
        for over, expected in ((1, "powerplay"), (6, "powerplay"), (7, "middle"),
                               (16, "middle"), (17, "death"), (20, "death")):
            s["current_over"] = over
            self.assertEqual(cm.approach_phase(s), expected, f"over {over}")

    def test_hundred_powerplay_is_five_sets_not_six(self):
        h = _make_state(overs=20, ball_format="The100")
        h["current_over"] = 5
        self.assertEqual(cm.approach_phase(h), "powerplay")
        h["current_over"] = 6
        self.assertEqual(cm.approach_phase(h), "middle")

    def test_short_formats_still_have_all_three_phases(self):
        s = _make_state(overs=5)
        seen = {cm.approach_phase(dict(s, current_over=o)) for o in range(1, 6)}
        self.assertEqual(seen, {"powerplay", "middle", "death"})

    def test_the_bot_plans_in_the_same_phase_the_engine_bowls(self):
        from services import bot_tactics as bt
        s = _make_state(overs=20)
        for over in range(1, 21):
            s["current_over"] = over
            self.assertEqual(bt.phase(s), cm.approach_phase(s))

    def test_context_reads_the_live_situation(self):
        s = _make_state(overs=20)
        s["current_over"] = 18
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        rid = str(s["current_bowler"]["roster_id"])
        s["bowl_stats"][rid].update({"balls": 12, "wickets": 1,
                                     "last_over_wickets": 1})
        s["partnership_balls"] = 30
        s["over_runs"] = [6, 14]
        ctx = cm.approach_context(s)
        self.assertEqual(ctx["pitch"], "Hard")
        self.assertEqual(ctx["phase"], "death")
        self.assertEqual(ctx["bowler_over_index"], 3)      # 2 bowled, this is the 3rd
        self.assertEqual(ctx["partnership_overs"], 5.0)
        # Batting momentum is no longer read here. It moved to the tracked
        # accumulator in engine.momentum, applied per ball through the MPI hook;
        # leaving the 12-runs-last-over boolean in the approach context as well
        # would price the same ascendancy twice. See _make_mpi_hook.
        self.assertFalse(ctx["batting_momentum"])
        self.assertTrue(ctx["bowling_momentum"])           # struck in their last
        self.assertIn(ctx["bowling_type"], ("Fast", "Fast-medium", "Medium-fast",
                                            "Off spin", "Leg spin"))

    def test_context_is_quiet_at_the_start_of_an_innings(self):
        s = _make_state(overs=20)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        ctx = cm.approach_context(s)
        self.assertEqual(ctx["bowler_over_index"], 1)
        self.assertEqual(ctx["partnership_overs"], 0.0)
        self.assertFalse(ctx["batting_momentum"])
        self.assertFalse(ctx["bowling_momentum"])
        self.assertIsNone(ctx["carry"])

    def test_context_survives_a_missing_bowler(self):
        """/rcl can rebuild a state before a bowler has been picked."""
        ctx = cm.approach_context(_make_state(overs=20))
        self.assertEqual(ctx["bowler_over_index"], 1)
        self.assertIsNone(ctx["bowling_type"])

    def test_simulate_over_reports_and_carries_the_combination(self):
        random.seed(11)
        s = _make_state(overs=20)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "aggressive"
        s["batting_approach"] = "ultra"
        summary = cm.simulate_over(s)
        self.assertEqual(summary["combo"], "The Shootout")
        self.assertEqual(summary["flavour"],
                         am.SPECIAL_COMBOS[("ultra", "aggressive")]["flavour"])
        # The Shootout has no carry rule, so nothing is handed on.
        self.assertIsNone(summary["carry"])
        self.assertIsNone(s["approach_carry"])
        # A pair with no name still describes itself, from the 5x5 matrix.
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "mixed"
        s["batting_approach"] = "ultra"
        summary = cm.simulate_over(s)
        self.assertIsNone(summary["combo"])
        self.assertEqual(summary["flavour"], am.MATRIX_FLAVOUR[("ultra", "mixed")])

    def test_a_survived_over_carries_into_the_next_one(self):
        s = _make_state(overs=20)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "aggressive"
        s["batting_approach"] = "defensive"
        # Seed a run of overs until one is survived intact, then check the carry.
        for seed in range(30):
            random.seed(seed)
            trial = _make_state(overs=20)
            trial["current_bowler"] = cm.eligible_bowlers(trial)[0]
            trial["bowling_approach"] = "aggressive"
            trial["batting_approach"] = "defensive"
            summary = cm.simulate_over(trial)
            if summary["over_wickets"] == 0:
                self.assertEqual(summary["carry"]["name"], "The Survival")
                self.assertEqual(trial["approach_carry"], summary["carry"])
                # And the next over sees it.
                trial["current_bowler"] = cm.eligible_bowlers(trial)[0]
                self.assertEqual(cm.approach_context(trial)["carry"],
                                 summary["carry"])
                return
        self.skipTest("no wicketless Defensive-vs-Aggressive over in 30 seeds")

    def test_the_carry_does_not_survive_the_interval(self):
        s = _make_state(overs=5)
        s["approach_carry"] = {"mult": {"Wicket": 0.9}, "name": "The Survival"}
        cm.end_first_innings(s)
        self.assertIsNone(s["approach_carry"])
        self.assertIsNone(cm.approach_context(s)["carry"])

    def test_bowler_momentum_is_the_bowlers_own_wickets(self):
        """A wicket at the other end is not this bowler's rhythm."""
        random.seed(3)
        s = _make_state(overs=20)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        rid = str(s["current_bowler"]["roster_id"])
        s["bowl_stats"][rid]["wickets"] = 2      # from earlier overs
        s["bowling_approach"] = "defensive"
        s["batting_approach"] = "defensive"
        before = s["bowl_stats"][rid]["wickets"]
        cm.simulate_over(s)
        self.assertEqual(s["bowl_stats"][rid]["last_over_wickets"],
                         s["bowl_stats"][rid]["wickets"] - before)


def _mk(rid, name, cat, bat, bowl, style="Fast"):
    return {"roster_id": rid, "player_id": rid, "name": name, "rating": max(bat, bowl),
            "category": cat, "bat_rating": bat, "bowl_rating": bowl,
            "bowl_style": style, "bowl_hand": "Right", "bat_hand": "Right"}


def _make_state(overs=5, ball_format="T20"):
    bat_xi = ([_mk(i, "Bat%d" % i, "Batsman", 80 - i, 30) for i in range(1, 8)]
              + [_mk(i, "AR%d" % i, "All-rounder", 60, 70, "Off spin") for i in range(8, 12)])
    bowl_xi = ([_mk(100 + i, "Bwl%d" % i, "Bowler", 30, 80 - i, "Fast") for i in range(1, 7)]
               + [_mk(100 + i, "BAR%d" % i, "All-rounder", 60, 70, "Leg spin") for i in range(7, 12)])
    return cm.build_cipl_state(1, overs, 10, 20, 111, 222, bat_xi, bowl_xi,
                               "Team A", "Team B", -100, "Hard", False,
                               ball_format=ball_format)


class CiplMatchTests(unittest.TestCase):
    def test_eligible_bowlers_only_bowlers_and_allrounders(self):
        s = _make_state()
        names = {p["category"] for p in cm.eligible_bowlers(s)}
        self.assertTrue(names.issubset({"Bowler", "All-rounder"}))

    def test_eligible_bowlers_excludes_previous_over_bowler(self):
        s = _make_state()
        first = cm.eligible_bowlers(s)[0]
        s["prev_bowler_rid"] = first["roster_id"]
        self.assertNotIn(first["roster_id"],
                         [p["roster_id"] for p in cm.eligible_bowlers(s)])

    def test_bowler_quota_enforced(self):
        s = _make_state(overs=5)  # quota = ceil(5/5) = 1 over each
        bowler = cm.eligible_bowlers(s)[0]
        # Pretend the bowler already bowled a full over.
        s["bowl_stats"][str(bowler["roster_id"])]["balls"] = 6
        self.assertNotIn(bowler["roster_id"],
                         [p["roster_id"] for p in cm.eligible_bowlers(s)])

    def test_simulate_over_consumes_six_legal_balls(self):
        random.seed(1)
        s = _make_state()
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "balanced"
        s["batting_approach"] = "balanced"
        summary = cm.simulate_over(s)
        legal = [t for t in summary["over_timeline"] if t not in ("WD", "NB")]
        # Either six legal balls, or fewer if all out / innings ended.
        self.assertTrue(len(legal) == 6 or s["total_wickets"] >= s["wicket_limit"])

    def test_full_match_runs_and_produces_result(self):
        random.seed(42)
        s = _make_state(overs=5)
        guard = 0
        while not cm.is_innings_over(s) and guard < 50:
            s["current_bowler"] = cm.eligible_bowlers(s)[0]
            s["bowling_approach"] = "aggressive"
            s["batting_approach"] = "balanced"
            cm.simulate_over(s)
            guard += 1
        self.assertEqual(s["innings"], 1)
        cm.end_first_innings(s)
        self.assertEqual(s["innings"], 2)
        self.assertEqual(s["target"], s["inn1_runs"] + 1)
        guard = 0
        while not cm.is_innings_over(s) and guard < 50:
            s["current_bowler"] = cm.eligible_bowlers(s)[0]
            s["bowling_approach"] = "defensive"
            s["batting_approach"] = "ultra"
            cm.simulate_over(s)
            guard += 1
        result = cm.compute_result(s)
        self.assertIn(result["margin_type"], ("runs", "wickets", "tie"))

    def test_stats_totals_are_consistent(self):
        random.seed(7)
        s = _make_state()
        while not cm.is_innings_over(s):
            s["current_bowler"] = cm.eligible_bowlers(s)[0]
            s["bowling_approach"] = "balanced"
            s["batting_approach"] = "aggressive"
            cm.simulate_over(s)
        # Bowler-conceded runs + extras should not exceed the team total
        # (extras are tracked separately from bowler figures for leg byes).
        bowl_runs = sum(v["runs"] for v in s["bowl_stats"].values())
        self.assertLessEqual(bowl_runs, s["total_runs"])
        self.assertGreaterEqual(s["total_wickets"], 0)

    def test_current_run_rate(self):
        s = _make_state()
        self.assertEqual(cm.current_run_rate(s), 0.0)  # no balls yet
        s["current_over"] = 2          # 1 completed over
        s["current_ball"] = 0
        s["total_runs"] = 12
        self.assertAlmostEqual(cm.current_run_rate(s), 12.0)

    def test_simulate_over_snapshots_last_over(self):
        random.seed(5)
        s = _make_state()
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "balanced"
        s["batting_approach"] = "balanced"
        cm.simulate_over(s)
        self.assertTrue(s.get("last_over_timeline"))
        self.assertTrue(s.get("last_over_commentary"))
        # Each snapshot entry carries the over label + text used by the card.
        for e in s["last_over_commentary"]:
            self.assertIn("over", e)
            self.assertIn("text", e)


class HundredFormatTests(unittest.TestCase):
    """The Hundred (100-ball) format on the Challenge League engine: 20 sets of
    5 balls, strike ends change every 10 balls, a bowler may bowl two consecutive
    sets (a 10-ball spell) but not a third, and an innings ends at 100 balls."""

    def _hundred(self, overs=20):
        return _make_state(overs=overs, ball_format="The100")

    def test_unit_and_total_balls(self):
        h = self._hundred()
        self.assertEqual(cm.balls_per_unit(h), 5)
        self.assertEqual(cm.total_balls(h), 100)
        t = _make_state(overs=20)  # T20 is unchanged
        self.assertEqual(cm.balls_per_unit(t), 6)
        self.assertEqual(cm.total_balls(t), 120)

    def test_innings_ends_at_100_balls_not_120(self):
        h = self._hundred()
        h["current_over"], h["current_ball"] = 20, 0   # 19 sets done → 95 balls
        self.assertEqual(cm.balls_bowled(h), 95)
        self.assertFalse(cm.is_innings_over(h))
        h["current_over"], h["current_ball"] = 20, 5   # 20 sets done → 100 balls
        self.assertEqual(cm.balls_bowled(h), 100)
        self.assertTrue(cm.is_innings_over(h))

    def test_format_overs_shows_ball_count(self):
        h = self._hundred()
        h["current_over"], h["current_ball"] = 9, 3    # (8*5)+3 = 43 balls
        self.assertEqual(cm.balls_bowled(h), 43)
        self.assertEqual(cm.format_overs(h), "43 balls")

    def test_powerplay_spec_is_25_balls(self):
        spec = cm.FORMAT_SPECS["The100"]
        self.assertEqual(spec["powerplay_units"] * spec["balls_per_unit"], 25)
        t20 = cm.FORMAT_SPECS["T20"]
        self.assertEqual(t20["powerplay_units"] * t20["balls_per_unit"], 36)

    def test_bowler_quota_is_20_balls(self):
        h = self._hundred()
        self.assertEqual(cm.max_bowler_overs(h), 4)    # 4 sets = 20 balls
        b = cm.eligible_bowlers(h)[0]
        rid = b["roster_id"]
        h["bowl_stats"][str(rid)]["balls"] = 20        # at the 20-ball cap
        self.assertNotIn(rid, [p["roster_id"] for p in cm.eligible_bowlers(h)])
        h["bowl_stats"][str(rid)]["balls"] = 15        # 3 sets — still has a set left
        self.assertIn(rid, [p["roster_id"] for p in cm.eligible_bowlers(h)])

    def test_bowler_may_bowl_two_consecutive_sets_not_three(self):
        h = self._hundred()
        rid = cm.eligible_bowlers(h)[0]["roster_id"]
        # One set into the spell → may return for a 2nd consecutive set.
        h["spell_rid"], h["spell_units"] = rid, 1
        h["bowl_stats"][str(rid)]["balls"] = 5
        self.assertIn(rid, [p["roster_id"] for p in cm.eligible_bowlers(h)])
        # Two consecutive sets bowled → blocked from a 3rd.
        h["spell_units"] = 2
        h["bowl_stats"][str(rid)]["balls"] = 10
        self.assertNotIn(rid, [p["roster_id"] for p in cm.eligible_bowlers(h)])

    def test_t20_still_blocks_back_to_back_overs(self):
        t = _make_state(overs=20)  # T20 rotation rule is unchanged
        first = cm.eligible_bowlers(t)[0]
        t["prev_bowler_rid"] = first["roster_id"]
        self.assertNotIn(first["roster_id"],
                         [p["roster_id"] for p in cm.eligible_bowlers(t)])

    def test_strike_changes_every_10_balls_not_after_first_set(self):
        # Force every delivery to a dot so there are no mid-set odd-run swaps —
        # isolating the end-change rule (every 10 legal balls in The Hundred).
        h = self._hundred()
        dot = {"type": "run", "runs": 0, "is_extra": False, "batter_out": False}
        orig = cm.calculate_outcome
        cm.calculate_outcome = lambda *a, **k: dict(dot)
        try:
            start = h["striker_idx"]
            h["current_bowler"] = cm.eligible_bowlers(h)[0]
            h["bowling_approach"] = h["batting_approach"] = "balanced"
            cm.simulate_over(h)                       # set 1 (5 balls) — no end change
            self.assertEqual(h["striker_idx"], start)
            h["current_bowler"] = cm.eligible_bowlers(h)[0]
            h["bowling_approach"] = h["batting_approach"] = "balanced"
            cm.simulate_over(h)                       # set 2 (10 balls) — ends change
            self.assertNotEqual(h["striker_idx"], start)
        finally:
            cm.calculate_outcome = orig

    def test_full_hundred_match_completes(self):
        random.seed(123)
        h = self._hundred()
        guard = 0
        while not cm.is_innings_over(h) and guard < 60:
            h["current_bowler"] = cm.eligible_bowlers(h)[0]
            h["bowling_approach"] = "balanced"
            h["batting_approach"] = "aggressive"
            cm.simulate_over(h)
            guard += 1
        # An innings never runs past 100 balls.
        self.assertLessEqual(cm.balls_bowled(h), 100)
        cm.end_first_innings(h)
        self.assertEqual(h["innings"], 2)
        self.assertEqual(h["spell_units"], 0)         # spell tracker reset at the break
        guard = 0
        while not cm.is_innings_over(h) and guard < 60:
            h["current_bowler"] = cm.eligible_bowlers(h)[0]
            h["bowling_approach"] = "defensive"
            h["batting_approach"] = "balanced"
            cm.simulate_over(h)
            guard += 1
        result = cm.compute_result(h)
        self.assertIn(result["margin_type"], ("runs", "wickets", "tie"))


class MiniAppSpectateOnlyTests(unittest.TestCase):
    """The /cipl Mini App must be spectate-only: the over-by-over Approach game
    is driven from the Telegram chat, so the Mini App never lets anyone (not even
    the two captains) bowl, bat, pick players, or use the Impact Player."""

    def test_cipl_state_is_flagged_view_only(self):
        from services.match_webapp_service import is_view_only_match
        s = _make_state()
        self.assertEqual(s.get("mode"), "cipl_approach")
        self.assertTrue(is_view_only_match(s))

    def test_non_cipl_state_is_interactive(self):
        from services.match_webapp_service import is_view_only_match
        self.assertFalse(is_view_only_match({"mode": "something_else"}))
        self.assertFalse(is_view_only_match({}))
        self.assertFalse(is_view_only_match(None))

    def test_gameplay_mutators_reject_cipl_matches(self):
        # Each mutator must refuse a /cipl match. They short-circuit on the
        # view-only guard before touching the DB-backed store, so a captain id
        # is accepted as the caller and the rejection is purely mode-driven.
        from unittest import mock
        from services import match_webapp_service as mws

        s = _make_state()
        bat_uid = s["bat_team_id"]
        bowl_uid = s["bowl_team_id"]
        msg = mws.VIEW_ONLY_MESSAGE

        with mock.patch.object(mws.mwa, "get_state", return_value=s):
            self.assertEqual(mws.set_delivery(1, bowl_uid, "yorker"), (False, msg))
            self.assertEqual(mws.set_delivery_action(1, bowl_uid, "yorker"),
                             (False, msg, None))
            self.assertEqual(mws.set_shot_action(1, bat_uid, "pull"),
                             (False, msg, None))
            self.assertEqual(mws.play_shot(1, bat_uid, 0), (False, msg))
            self.assertEqual(mws.select_wicket_batsman(1, bat_uid, 2),
                             (False, msg, None))
            self.assertEqual(mws.select_new_bowler(1, bowl_uid, 101),
                             (False, msg))


class ScenarioEngineIntegrationTests(unittest.TestCase):
    """The dramatic-finish ScenarioEngine is wired into the 2nd-innings chase
    as an optional realism layer, gated to 20-over matches."""

    def setUp(self):
        self._prev = os.environ.get("CIPL_SCENARIO_PROBABILITY")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("CIPL_SCENARIO_PROBABILITY", None)
        else:
            os.environ["CIPL_SCENARIO_PROBABILITY"] = self._prev

    def test_not_armed_for_non_t20_overs(self):
        os.environ["CIPL_SCENARIO_PROBABILITY"] = "1.0"
        s = _make_state(overs=5)
        s["target"], s["innings"] = 80, 2
        cm._maybe_enable_scenario(s)
        self.assertIsNone(s["scenario"])  # corridors are T20-only

    def test_off_switch_never_arms(self):
        os.environ["CIPL_SCENARIO_PROBABILITY"] = "0.0"
        s = _make_state(overs=20)
        s["target"], s["innings"] = 180, 2
        cm._maybe_enable_scenario(s)
        self.assertIsNone(s["scenario"])

    def test_armed_for_t20_when_probability_high(self):
        os.environ["CIPL_SCENARIO_PROBABILITY"] = "1.0"
        s = _make_state(overs=20)
        s["target"], s["innings"] = 180, 2
        cm._maybe_enable_scenario(s)
        self.assertIsNotNone(s["scenario"])
        self.assertIn(s["scenario"]["type"], cm.SCENARIO_TYPES)
        # Engine reconstructs and runs without raising.
        self.assertIsNotNone(cm._load_scenario_engine(s))

    def test_load_inactive_outside_second_innings(self):
        s = _make_state(overs=20)
        s["scenario"] = {"type": "last_ball_six", "active": True}
        s["innings"] = 1
        self.assertIsNone(cm._load_scenario_engine(s))

    def test_full_t20_match_with_scenario_survives_json_round_trip(self):
        os.environ["CIPL_SCENARIO_PROBABILITY"] = "1.0"
        random.seed(123)
        s = _make_state(overs=20)
        guard = 0
        while not cm.is_innings_over(s) and guard < 80:
            s["current_bowler"] = cm.eligible_bowlers(s)[0]
            s["bowling_approach"] = "balanced"
            s["batting_approach"] = "balanced"
            cm.simulate_over(s)
            guard += 1
        cm.end_first_innings(s)
        self.assertEqual(s["innings"], 2)
        self.assertIsNotNone(s["scenario"])  # armed at prob 1.0 for 20 overs
        guard = 0
        while not cm.is_innings_over(s) and guard < 80:
            # Persisting the JSON state between overs must not lose scenario state.
            s = json.loads(json.dumps(s))
            s["current_bowler"] = cm.eligible_bowlers(s)[0]
            s["bowling_approach"] = "balanced"
            s["batting_approach"] = "balanced"
            cm.simulate_over(s)
            guard += 1
        result = cm.compute_result(s)
        self.assertIn(result["margin_type"], ("runs", "wickets", "tie"))


try:
    import handlers.cipl_play as cp  # needs python-telegram-bot
    _HAVE_CP = True
except Exception:  # pragma: no cover - environment without Telegram deps
    _HAVE_CP = False


@unittest.skipUnless(_HAVE_CP, "handlers.cipl_play (python-telegram-bot) not importable")
class ApproachCardTests(unittest.TestCase):
    def _state_with_codes(self, overs=5):
        s = _make_state(overs)
        s.update(bat_team_code="MI", bowl_team_code="CSK",
                 bat_team_emoji="🔵", bowl_team_emoji="🟡")
        return s

    def test_first_over_card_has_no_commentary(self):
        s = self._state_with_codes()
        card = cp._approach_card(s)
        self.assertIn("MI", card)
        self.assertIn("CRR -", card)
        self.assertNotIn("COMMENTARY", card)   # no prior over yet
        self.assertIn("—", card)               # empty over-emoji strip

    def test_card_after_over_has_expandable_commentary(self):
        random.seed(11)
        s = self._state_with_codes()
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "balanced"
        s["batting_approach"] = "aggressive"
        cm.simulate_over(s)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]  # next over's bowler picked
        card = cp._approach_card(s)
        self.assertIn("<blockquote expandable>", card)
        self.assertIn("COMMENTARY", card)
        self.assertRegex(card, r"\d+\(\d+\)")            # striker runs(balls)

    def test_hex_to_circle_buckets(self):
        self.assertEqual(cp._hex_to_circle("#ff0000"), "🔴")
        self.assertEqual(cp._hex_to_circle("#0000ff"), "🔵")
        self.assertEqual(cp._hex_to_circle("bad"), "🏏")

    def test_second_innings_card_shows_chase(self):
        random.seed(13)
        s = self._state_with_codes()
        guard = 0
        while not cm.is_innings_over(s) and guard < 50:
            s["current_bowler"] = cm.eligible_bowlers(s)[0]
            s["bowling_approach"] = "balanced"
            s["batting_approach"] = "balanced"
            cm.simulate_over(s)
            guard += 1
        cm.end_first_innings(s)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        card = cp._approach_card(s)
        self.assertIn("RRR -", card)
        self.assertIn("Need", card)
        self.assertNotIn("COMMENTARY", card)             # stale snapshot cleared


class CommentaryEngineTests(unittest.TestCase):
    """The over-by-over flow uses the full SimCricketX commentary engine, not the
    terse built-in fallback lines."""

    def test_engine_loaded(self):
        self.assertIsNotNone(cm._COMMENTARY, "CommentaryEngine should load")
        self.assertTrue(cm._COMMENTARY.events)
        self.assertTrue(cm._COMMENTARY.narratives)

    def test_commentary_is_rich_and_varied(self):
        random.seed(7)
        s = _make_state(overs=5)
        for _ in range(5):
            if cm.is_innings_over(s):
                break
            s["current_bowler"] = cm.eligible_bowlers(s)[0]
            s["bowling_approach"] = "aggressive"
            s["batting_approach"] = "ultra"
            cm.simulate_over(s)
        texts = [e["text"] for e in s["commentary_log"]]
        self.assertGreater(len(texts), 0)
        # Every line is non-empty and varied (engine output, not one fixed string).
        self.assertTrue(all(t.strip() for t in texts))
        self.assertGreater(len(set(texts)), 5)
        # The old terse dot-ball template should no longer be the text source.
        self.assertFalse(any(t.startswith("Dot ball. ") for t in texts))

    def test_partnership_and_wkt_marks_tracked(self):
        random.seed(3)
        s = _make_state(overs=5)
        s["current_bowler"] = cm.eligible_bowlers(s)[0]
        s["bowling_approach"] = "balanced"
        s["batting_approach"] = "balanced"
        cm.simulate_over(s)
        self.assertIn("partnership_runs", s)
        self.assertIsInstance(s["partnership_runs"], int)
        self.assertIsInstance(s.get("wkt_marks"), list)


class HundredSpectatorTests(unittest.TestCase):
    """The read-only Watch Match Mini App must show Hundred progress in balls
    (not 6-ball overs) and compute balls-remaining against 100, not 120."""

    def test_chase_requirements_balls_remaining_format_aware(self):
        from services.match_engine import chase_requirements
        base = {"innings": 2, "target": 80, "total_runs": 40, "overs": 20,
                "current_over": 11, "current_ball": 0}
        hundred = dict(base, ball_format="The100")  # 10 sets = 50 of 100 balls
        self.assertEqual(chase_requirements(hundred)["balls_remaining"], 50)
        t20 = dict(base, ball_format="T20")          # 10 overs = 60 of 120 balls
        self.assertEqual(chase_requirements(t20)["balls_remaining"], 60)
        # No ball_format (standard /wpm, /cm) behaves exactly like T20.
        self.assertEqual(chase_requirements(dict(base))["balls_remaining"], 60)

    def test_overs_display_and_bpu(self):
        from services.match_webapp_service import _overs_display, _state_bpu
        hundred = {"ball_format": "The100", "current_over": 9, "current_ball": 3}
        self.assertEqual(_state_bpu(hundred), 5)
        self.assertEqual(_overs_display(hundred), "43")   # (8*5)+3 balls
        over_fmt = {"current_over": 9, "current_ball": 3}  # no ball_format → T20
        self.assertEqual(_state_bpu(over_fmt), 6)
        self.assertEqual(_overs_display(over_fmt), "8.3")  # 51 balls → 8.3 overs


if __name__ == "__main__":
    unittest.main()
