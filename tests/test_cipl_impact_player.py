"""Impact Player in the over-by-over engine (/letsplay + Challenge League).

Each check here is a way the swap can quietly corrupt a live match, which is
why it is worth a test rather than a glance:

  * ``striker_idx`` / ``non_striker_idx`` / ``next_batsman_idx`` are positional
    indices into ``batting_order``. Insert a substitute at the wrong end and the
    wrong player is suddenly on strike, with no error anywhere.
  * A batter at the crease must not be replaceable — that is the real Impact
    Player rule, and it is also what keeps those indices pointing at someone who
    is still on the field.
  * A substitute for a *dismissed* batter has to be INSERTED at the chosen slot.
    Appending (what the Mini App path does) buries them at number 12, which is
    the bug that made the batting-position picker necessary in the first place.
  * A side that bowls first and uses its swap gets a brand-new batting order at
    the innings break. Copying the 12-entry XI straight across would open the
    chase with the player who left the field.
  * A player swapped out must never bowl again or take a catch.
"""

import unittest

from services import impact_player
from services import cipl_match
from services.match_state_store import (
    A_PICK_CIPL_BOWLER, A_PICK_BAT_APPROACH, A_COMPLETED,
)


def _p(rid, name, category="Batsman", bat=70, bowl=40):
    return {"roster_id": rid, "name": name, "category": category,
            "rating": max(bat, bowl), "bat_rating": bat, "bowl_rating": bowl}


def _state(**over):
    """A live over-by-over state, built the way a real match builds one.

    Goes through build_cipl_state rather than hand-rolling a dict so the
    innings-break tests exercise the real end_first_innings against the real
    set of keys.
    """
    bat = [_p(i, f"B{i}", bat=80 - i) for i in range(1, 12)]
    bowl = [_p(100 + i, f"W{i}", "Bowler", bat=30, bowl=80 - i)
            for i in range(1, 12)]
    state = cipl_match.build_cipl_state(
        match_id=1, overs=20,
        bat_user_id=1, bowl_user_id=2, bat_user_tg=11, bowl_user_tg=22,
        bat_xi=bat, bowl_xi=bowl,
        bat_team_name="Alpha", bowl_team_name="Beta", chat_id=-100,
        bat_bench=[_p(50, "SuperSub", bat=95),
                   _p(51, "BenchAll", "All-rounder", bat=60, bowl=70)],
        bowl_bench=[_p(150, "BenchPace", "Bowler", bat=20, bowl=90)])
    # Mid-innings: two down, third and fourth at the crease.
    state.update({
        "current_over": 8,
        "striker_idx": 2, "non_striker_idx": 3, "next_batsman_idx": 4,
        "total_runs": 60, "total_wickets": 2,
    })
    state["bat_stats"] = {str(i): {"runs": 0, "balls": 0, "out": i <= 2}
                          for i in range(1, 12)}
    state.update(over)
    return state


class WindowTests(unittest.TestCase):
    def test_every_pre_over_step_is_a_legal_window(self):
        s = _state()
        for action in impact_player.CIPL_LEGAL_ACTIONS:
            self.assertIsNotNone(
                impact_player.cipl_break_label(s, action),
                f"{action} should be a legal Impact Player window")

    def test_a_finished_match_is_not_a_window(self):
        self.assertIsNone(
            impact_player.cipl_break_label(_state(), A_COMPLETED))

    def test_the_first_over_of_the_chase_reads_as_the_innings_break(self):
        s = _state(innings=2, current_over=1)
        self.assertEqual(
            impact_player.cipl_break_label(s, A_PICK_CIPL_BOWLER),
            "innings break")


class OutgoingRulesTests(unittest.TestCase):
    def test_batters_at_the_crease_cannot_be_replaced(self):
        s = _state()
        opts = impact_player.cipl_options(s, 1, A_PICK_CIPL_BOWLER)
        rids = {p["roster_id"] for p in opts["replaceable_players"]}
        striker = s["batting_order"][s["striker_idx"]]["roster_id"]
        non_striker = s["batting_order"][s["non_striker_idx"]]["roster_id"]
        self.assertNotIn(striker, rids)
        self.assertNotIn(non_striker, rids)

    def test_the_bowler_picked_for_this_over_cannot_be_replaced(self):
        s = _state()
        s["current_bowler"] = s["bowl_xi"][0]
        opts = impact_player.cipl_options(s, 2, A_PICK_BAT_APPROACH)
        rids = {p["roster_id"] for p in opts["replaceable_players"]}
        self.assertNotIn(s["bowl_xi"][0]["roster_id"], rids)

    def test_naming_a_crease_batter_explains_why_not(self):
        # A stale button can still name one. The rejection must say why, not
        # echo the generic "Impact Player is available" status line.
        s = _state()
        crease = s["batting_order"][s["striker_idx"]]["roster_id"]
        ok, msg, _ = impact_player.cipl_use(
            s, 1, 50, crease, A_PICK_CIPL_BOWLER)
        self.assertFalse(ok)
        self.assertIn("crease", msg.lower())

    def test_a_spectator_gets_nothing(self):
        opts = impact_player.cipl_options(_state(), 999, A_PICK_CIPL_BOWLER)
        self.assertFalse(opts["ok"])


class BattingOrderTests(unittest.TestCase):
    def test_inserting_at_any_legal_slot_leaves_the_crease_untouched(self):
        for slot, _label in impact_player.cipl_batting_slots(_state(), 9):
            s = _state()
            striker = s["batting_order"][s["striker_idx"]]["name"]
            non_striker = s["batting_order"][s["non_striker_idx"]]["name"]
            ok, msg, _ = impact_player.cipl_use(
                s, 1, 50, 9, A_PICK_CIPL_BOWLER, bat_position=slot)
            self.assertTrue(ok, msg)
            self.assertEqual(
                s["batting_order"][s["striker_idx"]]["name"], striker,
                f"slot {slot} moved the striker")
            self.assertEqual(
                s["batting_order"][s["non_striker_idx"]]["name"], non_striker,
                f"slot {slot} moved the non-striker")

    def test_no_legal_slot_is_below_the_next_batsman(self):
        s = _state()
        slots = [k for k, _ in impact_player.cipl_batting_slots(s, 9)]
        self.assertTrue(all(k >= s["next_batsman_idx"] for k in slots), slots)

    def test_a_slot_below_the_next_batsman_is_clamped_not_honoured(self):
        s = _state()
        ok, msg, _ = impact_player.cipl_use(
            s, 1, 50, 9, A_PICK_CIPL_BOWLER, bat_position=0)
        self.assertTrue(ok, msg)
        landed = next(i for i, p in enumerate(s["batting_order"])
                      if p["roster_id"] == 50)
        self.assertGreaterEqual(landed, 4)

    def test_a_substitute_for_a_yet_to_bat_player_takes_their_slot(self):
        s = _state()
        was = next(i for i, p in enumerate(s["batting_order"])
                   if p["roster_id"] == 9)
        ok, msg, _ = impact_player.cipl_use(s, 1, 50, 9, A_PICK_CIPL_BOWLER)
        self.assertTrue(ok, msg)
        self.assertEqual(s["batting_order"][was]["roster_id"], 50)
        self.assertNotIn(9, [p["roster_id"] for p in s["batting_order"]])

    def test_a_substitute_for_a_dismissed_batter_is_inserted_not_appended(self):
        # Player 1 is already out: they must stay in the order for the
        # scorecard, and the substitute must land where the captain asked
        # rather than at the end of the list.
        s = _state()
        ok, msg, _ = impact_player.cipl_use(
            s, 1, 50, 1, A_PICK_CIPL_BOWLER, bat_position=4)
        self.assertTrue(ok, msg)
        names = [p["roster_id"] for p in s["batting_order"]]
        self.assertIn(1, names, "the dismissed batter fell out of the order")
        self.assertEqual(names[4], 50)
        self.assertNotEqual(names[-1], 50, "substitute was appended last")


class OnePerTeamTests(unittest.TestCase):
    def test_a_team_only_gets_one(self):
        s = _state()
        self.assertTrue(impact_player.cipl_use(
            s, 1, 50, 9, A_PICK_CIPL_BOWLER)[0])
        ok, msg, _ = impact_player.cipl_use(s, 1, 51, 8, A_PICK_CIPL_BOWLER)
        self.assertFalse(ok)
        self.assertIn("already used", msg)

    def test_the_usage_survives_the_innings_swap(self):
        s = _state()
        impact_player.cipl_use(s, 2, 150, 105, A_PICK_CIPL_BOWLER)
        cipl_match.end_first_innings(s)
        # Team 2 is now batting; it must not get a second swap.
        opts = impact_player.cipl_options(s, 2, A_PICK_CIPL_BOWLER)
        self.assertTrue(opts["used"])
        self.assertFalse(opts["can_use"])

    def test_a_substitute_cannot_be_swapped_back_out(self):
        s = _state()
        impact_player.cipl_use(s, 1, 50, 9, A_PICK_CIPL_BOWLER)
        opts = impact_player.cipl_options(s, 2, A_PICK_CIPL_BOWLER)
        self.assertNotIn(50, {p["roster_id"] for p in opts["incoming_options"]})


class BowlingSideTests(unittest.TestCase):
    def test_the_replaced_bowler_can_never_bowl_again(self):
        s = _state()
        s["bowl_stats"] = {"101": {"balls": 12, "runs": 20, "wickets": 1,
                                   "overs_done": 2}}
        ok, msg, _ = impact_player.cipl_use(
            s, 2, 150, 101, A_PICK_CIPL_BOWLER)
        self.assertTrue(ok, msg)
        eligible = {p["roster_id"] for p in cipl_match.eligible_bowlers(s)}
        self.assertNotIn(101, eligible)
        self.assertIn(150, eligible)

    def test_the_replaced_bowler_keeps_their_figures(self):
        s = _state()
        s["bowl_stats"] = {"101": {"balls": 12, "runs": 20, "wickets": 1,
                                   "overs_done": 2}}
        impact_player.cipl_use(s, 2, 150, 101, A_PICK_CIPL_BOWLER)
        self.assertEqual(s["bowl_stats"]["101"]["wickets"], 1)
        self.assertEqual(s["bowl_stats"]["101"]["runs"], 20)

    def test_the_substitute_arrives_with_a_full_quota(self):
        s = _state()
        impact_player.cipl_use(s, 2, 150, 101, A_PICK_CIPL_BOWLER)
        sub = cipl_match.find_player(s["bowl_xi"], 150)
        self.assertEqual(cipl_match.overs_left(s, sub),
                         cipl_match.max_bowler_overs(s))

    def test_a_replaced_player_cannot_field_the_catch(self):
        s = _state()
        impact_player.cipl_use(s, 2, 150, 101, A_PICK_CIPL_BOWLER)
        names = {cipl_match._pick_fielder(s, None) for _ in range(200)}
        self.assertNotIn("W1", names)


class InningsBreakRebuildTests(unittest.TestCase):
    """The silent one: it only reproduces for the side that bowls first."""

    def test_the_chase_never_opens_with_a_player_who_left_the_field(self):
        s = _state()
        impact_player.cipl_use(s, 2, 150, 105, A_PICK_CIPL_BOWLER,
                               bat_position=3)
        cipl_match.end_first_innings(s)
        order = s["batting_order"]
        self.assertEqual(len(order), 11, [p["name"] for p in order])
        self.assertNotIn(105, [p["roster_id"] for p in order])
        openers = {order[s["striker_idx"]]["roster_id"],
                   order[s["non_striker_idx"]]["roster_id"]}
        self.assertNotIn(105, openers)

    def test_the_substitute_bats_where_the_captain_asked(self):
        s = _state()
        impact_player.cipl_use(s, 2, 150, 105, A_PICK_CIPL_BOWLER,
                               bat_position=3)
        cipl_match.end_first_innings(s)
        self.assertEqual(s["batting_order"][3]["roster_id"], 150)

    def test_without_a_swap_the_order_is_untouched(self):
        s = _state()
        expected = [p["roster_id"] for p in s["bowl_xi"]]
        cipl_match.end_first_innings(s)
        self.assertEqual([p["roster_id"] for p in s["batting_order"]], expected)


class BenchSnapshotTests(unittest.TestCase):
    def test_a_state_built_without_a_bench_degrades_quietly(self):
        # In-flight matches saved before this feature have no bench key.
        s = _state()
        s.pop("bat_bench")
        opts = impact_player.cipl_options(s, 1, A_PICK_CIPL_BOWLER)
        self.assertTrue(opts["ok"])
        self.assertFalse(opts["can_use"])
        self.assertIn("No substitutes", opts["message"])

    def test_build_cipl_state_carries_the_bench_through(self):
        s = cipl_match.build_cipl_state(
            match_id=1, overs=20, bat_user_id=1, bowl_user_id=2,
            bat_user_tg=11, bowl_user_tg=22,
            bat_xi=[_p(1, "A")], bowl_xi=[_p(2, "B")],
            bat_team_name="Alpha", bowl_team_name="Beta", chat_id=-1,
            bat_bench=[_p(9, "Sub")], bowl_bench=[])
        self.assertEqual([p["roster_id"] for p in s["bat_bench"]], [9])
        self.assertEqual(s["bowl_bench"], [])


class OneUseSurvivesEverythingTests(unittest.TestCase):
    """One swap per team, for the WHOLE match.

    The usage record lives in the match state, so anything that reloads,
    re-renders or re-serialises that state is a chance to hand a captain a
    second swap. These drive the real paths rather than asserting on the dict.
    """

    def _used_a_swap(self):
        s = _state()
        ok, msg, _ = impact_player.cipl_use(s, 1, 50, 9, A_PICK_CIPL_BOWLER)
        self.assertTrue(ok, msg)
        return s

    def test_it_survives_the_json_round_trip_persistence_actually_does(self):
        # State is stored as JSON. If the usage keys did not survive that trip
        # (they are str(user_id) for exactly this reason) every restart would
        # refill both teams' swaps.
        import json
        s = self._used_a_swap()
        reloaded = json.loads(json.dumps(s, default=str))
        opts = impact_player.cipl_options(reloaded, 1, A_PICK_CIPL_BOWLER)
        self.assertTrue(opts["used"])
        self.assertFalse(opts["can_use"])
        ok, msg, _ = impact_player.cipl_use(
            reloaded, 1, 51, 8, A_PICK_CIPL_BOWLER)
        self.assertFalse(ok)
        self.assertIn("already used", msg)

    def test_rcl_cannot_hand_out_a_second(self):
        # /rcl runs _resume_locked, which only re-reads state and re-renders a
        # prompt. Drive it for real with the sends stubbed out.
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch
        import handlers.cipl_play as cp

        s = self._used_a_swap()
        ctx = SimpleNamespace(bot=None, bot_data={}, job_queue=None)

        async def _gs(c, m):
            return s

        async def _na(c, m):
            return A_PICK_CIPL_BOWLER

        async def _prompt(context, mid, state=None, first=False):
            return None

        with patch.object(cp, "_gs", _gs), \
             patch.object(cp, "_get_next_action", _na), \
             patch.object(cp, "_prompt_bowler", _prompt), \
             patch.object(cp, "_super_over_active", lambda c, m: False), \
             patch.object(cp, "_innings_quota_used", lambda st: False):
            asyncio.run(cp._resume_locked(ctx, 7))

        opts = impact_player.cipl_options(s, 1, A_PICK_CIPL_BOWLER)
        self.assertTrue(opts["used"], "/rcl refilled the swap")
        self.assertFalse(opts["can_use"])

    def test_both_sides_are_tracked_independently(self):
        s = _state()
        self.assertTrue(impact_player.cipl_use(
            s, 1, 50, 9, A_PICK_CIPL_BOWLER)[0])
        # The bowling side still has theirs...
        self.assertTrue(impact_player.cipl_options(
            s, 2, A_PICK_CIPL_BOWLER)["can_use"])
        self.assertTrue(impact_player.cipl_use(
            s, 2, 150, 105, A_PICK_CIPL_BOWLER)[0])
        # ...and now neither does, in either innings.
        cipl_match.end_first_innings(s)
        for uid in (1, 2):
            opts = impact_player.cipl_options(s, uid, A_PICK_CIPL_BOWLER)
            self.assertTrue(opts["used"], f"user {uid} was refilled")
            self.assertFalse(opts["can_use"])

    def test_the_button_disappears_once_both_have_used_theirs(self):
        import handlers.cipl_play as cp
        s = _state()
        impact_player.cipl_use(s, 1, 50, 9, A_PICK_CIPL_BOWLER)
        self.assertIsNotNone(cp._impact_button(s, 7), "one side still has one")
        impact_player.cipl_use(s, 2, 150, 105, A_PICK_CIPL_BOWLER)
        self.assertIsNone(cp._impact_button(s, 7))

    def test_a_benchless_match_is_not_offered_the_button_at_all(self):
        """A /cdraft squad is dealt exactly eleven, so neither side can ever
        swap. Offering a button whose only possible answer is "no substitutes"
        is worse than not offering one."""
        import handlers.cipl_play as cp
        s = _state(bat_bench=[], bowl_bench=[])
        self.assertIsNone(cp._impact_button(s, 7))

    def test_one_side_with_a_bench_still_gets_the_shared_button(self):
        import handlers.cipl_play as cp
        s = _state(bowl_bench=[])
        self.assertIsNotNone(cp._impact_button(s, 7))
        self.assertTrue(impact_player.cipl_available_to(s, 1))
        self.assertFalse(impact_player.cipl_available_to(s, 2))

    def test_availability_ignores_the_window_but_not_the_swap_itself(self):
        # cipl_available_to answers "is this side's swap still on the table",
        # which is what decides whether a button is worth showing — not whether
        # this instant is a legal break.
        s = _state()
        self.assertTrue(impact_player.cipl_available_to(s, 1))
        self.assertFalse(
            impact_player.cipl_options(s, 1, A_COMPLETED)["can_use"],
            "the window really is shut")
        impact_player.cipl_use(s, 1, 50, 9, A_PICK_CIPL_BOWLER)
        self.assertFalse(impact_player.cipl_available_to(s, 1))

    def test_a_spectator_has_nothing_available(self):
        self.assertFalse(impact_player.cipl_available_to(_state(), 999))

    def test_the_remaining_bench_drops_anyone_a_swap_has_spent(self):
        s = _state()
        self.assertEqual(
            [p["roster_id"] for p in impact_player.cipl_bench_for(s, "bat")],
            [50, 51])
        impact_player.cipl_use(s, 1, 50, 9, A_PICK_CIPL_BOWLER)
        self.assertEqual(
            [p["roster_id"] for p in impact_player.cipl_bench_for(s, "bat")],
            [51], "the substitute who came on is no longer a substitute")


class ImpactMarkingTests(unittest.TestCase):
    """A substitute is marked wherever their name appears."""

    def _swapped(self):
        s = _state()
        ok, msg, _ = impact_player.cipl_use(
            s, 1, 50, 9, A_PICK_CIPL_BOWLER, bat_position=4)
        self.assertTrue(ok, msg)
        s["bat_stats"]["50"] = {"runs": 44, "balls": 21, "fours": 4,
                                "sixes": 2, "out": False}
        return s

    def test_only_the_substitute_is_marked(self):
        self.assertEqual(
            impact_player.display_name({"name": "Sub", "impact_replacement": True}),
            "Sub -IP")
        self.assertEqual(impact_player.display_name({"name": "Regular"}),
                         "Regular")
        # The player who was replaced keeps their own name — they batted.
        self.assertEqual(
            impact_player.display_name({"name": "Dropped", "active": False,
                                        "impact_replaced": True}),
            "Dropped")

    def test_the_flag_reaches_the_batting_order_not_just_the_xi(self):
        # Every text scorecard renders from batting_order, so a flag that only
        # lands on the XI list shows up nowhere.
        s = self._swapped()
        sub = next(p for p in s["batting_order"] if p["roster_id"] == 50)
        self.assertTrue(impact_player.is_impact(sub))

    def test_the_chat_scorecard_line_carries_it(self):
        import handlers.cipl_play as cp
        s = self._swapped()
        sub = next(p for p in s["batting_order"] if p["roster_id"] == 50)
        self.assertIn("-IP", cp._bat_line(sub, s["bat_stats"]))
        self.assertIn("-IP", cp._compact_bat_line(sub, s["bat_stats"]))

    def test_the_summary_card_rows_carry_it(self):
        import handlers.cipl_play as cp
        s = self._swapped()
        bats, _bowls = cp._summary_rows(
            s["bat_stats"], s["batting_order"], s["bowl_stats"], s["bowl_xi"])
        sub_row = next(b for b in bats if b["name"] == "SuperSub")
        self.assertTrue(sub_row["impact"])
        self.assertTrue(all(not b["impact"] for b in bats
                            if b["name"] != "SuperSub"))


if __name__ == "__main__":
    unittest.main()
