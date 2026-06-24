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
        out = am.apply_approach_modifiers(BASE_WEIGHTS, "ultra", "aggressive")
        # ultra Six 2.2 * aggressive Six 1.2 = 2.64
        self.assertAlmostEqual(out["Six"], BASE_WEIGHTS["Six"] * 2.2 * 1.2, places=5)

    def test_no_negative_weights(self):
        weird = dict(BASE_WEIGHTS, Six=-5.0)
        out = am.apply_approach_modifiers(weird, "defensive", "defensive")
        self.assertGreaterEqual(out["Six"], 0.0)


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


if __name__ == "__main__":
    unittest.main()
