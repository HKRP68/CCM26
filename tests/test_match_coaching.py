"""Coaching uses observed facts, survives sparse archives and stays read-only."""

import asyncio
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from services.match_coaching import (
    build_post_match_coaching, render_coaching_html, send_post_match_coaching,
)


def finished_state():
    return {
        "match_id": 77, "chat_id": 55,
        "innings": 2, "overs": 20, "ball_format": "T20",
        "current_over": 20, "current_ball": 6,
        "inn1_overs": "20.0", "inn1_wickets": 3, "total_wickets": 0,
        "inn1_bat_team": "Rangers", "inn1_bowl_team": "Kings",
        "inn1_bat_team_id": 1, "inn1_bowl_team_id": 2,
        "bat_team_name": "Kings", "bowl_team_name": "Rangers",
        "bat_team_id": 2, "bowl_team_id": 1,
        "inn1_runs": 180, "total_runs": 160, "target": 181,
        "inn1_over_runs": [9] * 20, "over_runs": [8] * 20,
        "inn1_fow": [[65, 1, "One", "7.1"], [66, 2, "Two", "7.2"],
                     [66, 3, "Three", "7.3"]],
        "chase_history": [{"over": 17, "chasing": 60},
                          {"over": 18, "chasing": 45},
                          {"over": 19, "chasing": 20}],
        "inn1_bat_xi": [], "inn1_bowl_xi": [], "bat_xi": [], "bowl_xi": [],
        "inn1_bat_stats": {}, "inn1_bowl_stats": {}, "bat_stats": {}, "bowl_stats": {},
    }


def finisher(state, *, level=5, balls=0):
    state["inn1_bat_xi"] = [{"roster_id": 7, "name": "Late Batter", "category": "Batsman",
                             "traits": [{"effect_key": "bat_finisher", "level": level}]}]
    state["inn1_bat_stats"] = {"7": {"balls": balls, "out": False}}


class CoachingFactsTests(unittest.TestCase):
    def test_two_distinct_turning_points_rank_wickets_and_recorded_chase_changes(self):
        turns = build_post_match_coaching(finished_state())["turning_points"]
        self.assertEqual([(t["innings"], t["over"]) for t in turns], [(1, 8), (2, 19)])
        self.assertIn("3 wickets", turns[0]["text"])
        self.assertIn("45% to 20%", turns[1]["text"])

    def test_invalid_probability_does_not_become_a_zero_percent_fact(self):
        state = finished_state()
        state["inn1_fow"] = []
        state["chase_history"] = [{"over": 1, "chasing": 50},
                                   {"over": 2, "chasing": None},
                                   {"over": 3, "chasing": "junk"}]
        self.assertEqual(build_post_match_coaching(state)["turning_points"], [])

    def test_duplicate_chase_entries_cannot_duplicate_a_turning_point(self):
        state = finished_state()
        state["chase_history"].insert(2, {"over": 18, "chasing": 44})
        turns = build_post_match_coaching(state)["turning_points"]
        self.assertEqual(len({(p["innings"], p["over"]) for p in turns}), len(turns))

    def test_fallback_needs_no_probability_or_momentum_history(self):
        state = finished_state()
        state.pop("chase_history")
        turns = build_post_match_coaching(state)["turning_points"]
        self.assertEqual(len(turns), 1)
        self.assertIn("3 wickets", turns[0]["text"])

    def test_missing_run_totals_do_not_turn_known_wickets_into_zero_run_claims(self):
        state = finished_state()
        state["inn1_over_runs"] = []
        state["chase_history"] = []
        turn = build_post_match_coaching(state)["turning_points"][0]
        self.assertIn("3 wickets fell", turn["text"])
        self.assertNotIn("0 runs", turn["text"])

    def test_unknown_run_values_do_not_generate_an_invented_quiet_spell_tip(self):
        state = {"bat_team_name": "Rangers", "over_runs": [None, "missing"]}
        self.assertEqual(build_post_match_coaching(state), {"turning_points": [], "tips": []})

    def test_no_facts_are_invented_for_empty_or_malformed_snapshots(self):
        for state in (None, {}, {"over_runs": "missing", "bat_stats": []},
                      {"over_runs": [None, "junk"], "fow": [None, [0, 1, "X", "12.x"]]}):
            with self.subTest(state=state):
                coaching = build_post_match_coaching(state)
                self.assertEqual(coaching["turning_points"], [])
                if not isinstance(state, dict) or not state.get("bat_team_name"):
                    self.assertEqual(coaching["tips"], [])

    def test_hundred_uses_sets_and_correctly_reads_ball_markers(self):
        state = finished_state()
        state["ball_format"] = "The100"
        state["inn1_fow"] = [[20, 1, "One", "30 balls"],
                              [20, 2, "Two", "30 balls"],
                              [20, 3, "Three", "30 balls"]]
        turn = build_post_match_coaching(state)["turning_points"][0]
        self.assertEqual(turn["over"], 6)
        self.assertIn("Set 6", turn["text"])
        self.assertNotIn("Over", turn["text"])

    def test_coaching_never_changes_the_match_state(self):
        state = finished_state()
        finisher(state)
        before = copy.deepcopy(state)
        first = build_post_match_coaching(state)
        self.assertEqual(first, build_post_match_coaching(state))
        self.assertEqual(state, before)


class CoachingTipTests(unittest.TestCase):
    def test_unused_finisher_is_named_with_an_actionable_batting_order_tip(self):
        state = finished_state()
        finisher(state)
        tip = build_post_match_coaching(state)["tips"][0]
        self.assertEqual(tip["team_id"], 1)
        self.assertEqual(tip["kind"], "unused_finisher")
        self.assertIn("Late Batter faced no balls", tip["text"])
        self.assertIn("Move them up", tip["text"])

    def test_no_unused_finisher_claim_when_the_match_did_not_reach_death(self):
        state = finished_state()
        finisher(state)
        state["inn1_over_runs"] = [9] * 10
        self.assertNotIn("unused_finisher", [t["kind"] for t in build_post_match_coaching(state)["tips"]])

    def test_missing_stats_or_a_dismissed_or_inactive_finisher_is_not_blamed(self):
        for change in ("missing", "out", "inactive", "faced"):
            state = finished_state()
            finisher(state)
            if change == "missing":
                state["inn1_bat_stats"] = {}
            elif change == "out":
                state["inn1_bat_stats"]["7"]["out"] = True
            elif change == "inactive":
                state["inn1_bat_xi"][0]["active"] = False
            else:
                state["inn1_bat_stats"]["7"]["balls"] = 5
            with self.subTest(change=change):
                self.assertNotIn("unused_finisher", [t["kind"] for t in build_post_match_coaching(state)["tips"]])

    def test_traits_disabled_by_agreement_do_not_generate_trait_coaching(self):
        state = finished_state()
        finisher(state)
        state["traits_enabled"] = False
        self.assertNotIn("unused_finisher", [t["kind"] for t in build_post_match_coaching(state)["tips"]])

    def test_legacy_trait_cache_is_supported_without_database_queries(self):
        state = finished_state()
        finisher(state)
        traits = state["inn1_bat_xi"][0].pop("traits")
        state["_traits_cache"] = {"7": traits}
        self.assertEqual(build_post_match_coaching(state)["tips"][0]["kind"], "unused_finisher")

    def test_an_ice_finisher_in_innings_one_has_no_live_chase_trait_to_miss(self):
        state = finished_state()
        finisher(state)
        state["inn1_bat_xi"][0]["traits"][0]["effect_key"] = "elite_ice_finisher"
        self.assertNotIn("unused_finisher", [t["kind"] for t in build_post_match_coaching(state)["tips"]])

    def test_death_bowler_quota_tip_uses_actual_bowling_records_in_both_formats(self):
        for fmt, balls in (("T20", 6), ("The100", 5)):
            state = finished_state()
            state["ball_format"] = fmt
            state["inn1_bowl_xi"] = [{"roster_id": 51, "name": "Death Ace", "category": "Bowler",
                                      "traits": [{"effect_key": "bowl_death", "level": 5}]}]
            state["inn1_approach_log"] = [
                {"over": over, "bowler_rid": 51, "balls": balls} for over in (1, 3, 7, 9)]
            with self.subTest(fmt=fmt):
                tips = build_post_match_coaching(state)["tips"]
                tip = next(t for t in tips if t["kind"] == "early_death_quota")
                self.assertEqual(tip["team_id"], 2)
                self.assertIn("Reserve at least one", tip["text"])
                state["inn1_approach_log"][-1]["over"] = 19
                self.assertNotIn("early_death_quota", [t["kind"] for t in build_post_match_coaching(state)["tips"]])

    def test_repetition_tip_needs_a_contiguous_recorded_streak(self):
        state = finished_state()
        for overs, expected in (((1, 2, 3), False), ((1, 2, 3, 4), True), ((1, 2, 8, 9), False)):
            state["inn1_approach_log"] = [{"over": over, "bat": "ultra"} for over in overs]
            with self.subTest(overs=overs):
                kinds = [t["kind"] for t in build_post_match_coaching(state)["tips"]]
                self.assertEqual("repeated_approach" in kinds, expected)

    def test_dot_pressure_uses_recorded_dots_and_balls(self):
        state = finished_state()
        state["inn1_bat_stats"] = {"7": {"balls": 20, "dots": 12}}
        tip = build_post_match_coaching(state)["tips"][0]
        self.assertEqual(tip["kind"], "dot_pressure")
        self.assertIn("12 dot balls from 20 balls", tip["text"])
        self.assertIn("singles", tip["text"])

    def test_same_team_names_still_get_a_tip_for_each_captain(self):
        state = finished_state()
        state["inn1_bat_team"] = state["bat_team_name"] = "United"
        tips = build_post_match_coaching(state)["tips"]
        self.assertEqual({tip["team_id"] for tip in tips}, {1, 2})
        self.assertEqual(len(tips), 2)

    def test_older_archives_can_recover_team_identity_from_the_swapped_sides(self):
        state = finished_state()
        del state["inn1_bat_team_id"]
        del state["inn1_bowl_team_id"]
        self.assertEqual({tip["team_id"] for tip in build_post_match_coaching(state)["tips"]}, {1, 2})


class CoachingDeliveryTests(unittest.TestCase):
    def test_html_and_telegram_escape_names_and_bound_message_length(self):
        coaching = {"turning_points": [{"text": '<script>&"' * 600}] * 2,
                    "tips": [{"team": "<b>&" * 200, "text": '<img onerror="x">&' * 300}] * 2}
        for telegram in (False, True):
            rendered = render_coaching_html(coaching, telegram=telegram)
            self.assertNotIn("<script>", rendered)
            self.assertNotIn("<img ", rendered)
            self.assertIn("&lt;", rendered)
            if telegram:
                self.assertLess(len(rendered), 4096)
                self.assertEqual(rendered.count("<blockquote"), 1)
                self.assertEqual(rendered.count("</blockquote>"), 1)

    def test_send_failure_does_not_interrupt_match_completion(self):
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("offline")))
        with self.assertLogs("services.match_coaching", level="ERROR"):
            asyncio.run(send_post_match_coaching(bot, 55, finished_state()))
        bot.send_message.assert_awaited_once()

    def test_no_extra_chat_message_when_the_snapshot_has_no_coaching_evidence(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        asyncio.run(send_post_match_coaching(bot, 55, {}))
        bot.send_message.assert_not_awaited()

    def test_archived_arena_results_include_the_same_read_only_coaching(self):
        from services.crickidex_arena import _build_result
        state = json.loads(json.dumps(finished_state()))
        finisher(state)
        state["_completed_rewards"] = {"winner_coins": 1, "loser_coins": 1}
        result = _build_result(None, state, SimpleNamespace(user1_id=1, user2_id=2),
                               None, {"telegramId": 11}, {"telegramId": 22})
        self.assertEqual(result["coaching"], build_post_match_coaching(state))
        self.assertTrue(result["coaching"]["tips"])

    def test_offline_analysis_contains_the_coaching_summary_once(self):
        from services.match_analysis import build_match_analysis_html
        state = finished_state()
        finisher(state)
        rendered = build_match_analysis_html(state)
        self.assertIn("Post-match coaching", rendered)
        self.assertIn("Late Batter faced no balls", rendered)
        self.assertEqual(rendered.count(">Turning points<"), 1)

    def test_real_analysis_delivery_sends_chat_coaching_and_includes_it_in_the_file(self):
        from handlers import cipl_play
        state = finished_state()
        finisher(state)
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        with (patch.object(cipl_play, "_cipl_calc_potm", return_value=None),
              patch("services.match_webapp_service.build_scorecard", return_value=None),
              patch("services.tg_storage_service.is_configured", return_value=False)):
            asyncio.run(cipl_play._send_match_analysis(SimpleNamespace(bot=bot), 77, state, {}))
        bot.send_message.assert_awaited_once()
        self.assertIn("Late Batter faced no balls", bot.send_message.await_args.args[1])
        bot.send_document.assert_awaited_once()
        document = bot.send_document.await_args.kwargs["document"].getvalue().decode()
        self.assertIn("Late Batter faced no balls", document)


if __name__ == "__main__":
    unittest.main()
