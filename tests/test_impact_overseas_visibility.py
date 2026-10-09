"""The IPL overseas Impact rule, as the captain sees it.

The rule itself is pinned in tests/test_impact_xi_rules.py. These cover what
makes it visible — the ✈️ status line, the ✈️ tag on names, the "hidden" note
when overseas subs are held back — and that the cap is read from the league /
tournament at launch rather than only from the Select XI cache.
"""

import unittest
from unittest import mock

from handlers import challenge, cipl_play
from services import crickidex_arena, impact_player
from services.match_state_store import A_PICK_CIPL_BOWLER
from tests.test_impact_xi_rules import _p, _state


def _bowl_xi_with(state, overseas_count):
    """Make exactly ``overseas_count`` of the bowling XI overseas."""
    for i, p in enumerate(state["bowl_xi"]):
        p["is_overseas"] = i < overseas_count
    return state


class StatusLineTests(unittest.TestCase):
    def test_under_the_cap_says_overseas_may_come_on(self):
        state = _bowl_xi_with(_state([]), 3)
        state["xi_rules"]["max_overseas"] = 4
        self.assertEqual(impact_player.overseas_status(state, "bowl"), (3, 4))
        line = impact_player.overseas_status_line(state, "bowl")
        self.assertIn("3/4", line)
        self.assertIn("may come on", line)

    def test_at_the_cap_says_not_allowed(self):
        state = _bowl_xi_with(_state([]), 4)
        state["xi_rules"]["max_overseas"] = 4
        line = impact_player.overseas_status_line(state, "bowl")
        self.assertIn("4/4", line)
        self.assertIn("not allowed", line)

    def test_no_cap_shows_nothing(self):
        state = _state([], max_overseas=11)
        self.assertIsNone(impact_player.overseas_status(state, "bowl"))
        self.assertEqual(impact_player.overseas_status_line(state, "bowl"), "")
        self.assertEqual(
            impact_player.overseas_status_line(_state([], rules=False), "bowl"), "")

    def test_multi_uses_the_captains_own_cap(self):
        state = _bowl_xi_with(_state([]), 3)
        state["xi_rules"]["max_overseas"] = 11
        state["xi_rules"]["by_user"] = {"22": {"min_overseas": 0,
                                               "max_overseas": 3}}
        self.assertEqual(impact_player.overseas_status(state, "bowl"), (3, 3))


class ChatPickerTests(unittest.TestCase):
    def test_overseas_names_carry_a_plane(self):
        self.assertIn("✈️", cipl_play._impact_label(_p(1, "Os", overseas=True)))
        self.assertNotIn("✈️", cipl_play._impact_label(_p(2, "Home")))

    def test_step2_names_hidden_overseas_subs_at_the_cap(self):
        bench = [_p(50, "OsSub", "Bowler", 80, overseas=True, bowl=85),
                 _p(51, "HomeSub", "Bowler", 80, bowl=85)]
        state = _bowl_xi_with(_state(bench, min_overseas=0), 4)
        state["xi_rules"]["max_overseas"] = 4
        state["xi_rules"]["rating_rules"] = []
        opts = impact_player.cipl_options(state, 2, A_PICK_CIPL_BOWLER)
        legal = impact_player.cipl_incoming_for(state, "bowl", 8,
                                                opts["incoming_options"])
        self.assertEqual([p["roster_id"] for p in legal], [51])
        block = cipl_play._impact_overseas_block(state, "bowl", legal,
                                                 opts["bench_all"])
        self.assertIn("4/4", block)
        self.assertIn("1 overseas sub hidden", block)

    def test_no_cap_no_block(self):
        state = _state([], max_overseas=11)
        self.assertEqual(cipl_play._impact_overseas_block(state, "bowl"), "")


class MiniAppPayloadTests(unittest.TestCase):
    def test_payload_carries_the_overseas_count_and_flag(self):
        bench = [_p(51, "HomeSub", "Bowler", 80, bowl=85)]
        state = _bowl_xi_with(_state(bench, min_overseas=0), 2)
        state["xi_rules"]["rating_rules"] = []
        payload = crickidex_arena._approach_impact_payload(
            state, 2, A_PICK_CIPL_BOWLER)
        self.assertEqual(payload["overseas"]["have"], 2)
        self.assertEqual(payload["overseas"]["max"], 2)
        self.assertFalse(payload["overseas"]["allowed"])
        flags = {p["id"]: p["isOverseas"] for p in payload["replaceablePlayers"]}
        self.assertTrue(flags[2])
        self.assertFalse(flags[3])


class FreshLimitsTests(unittest.TestCase):
    def _run(self, draft, league, tour_limits=None):
        session = mock.MagicMock()
        session.get.return_value = object() if tour_limits else None
        with mock.patch.object(challenge, "get_session", return_value=session), \
             mock.patch.object(challenge, "_resolve_draft_league",
                               return_value=league), \
             mock.patch("services.tournament_service.overseas_limits",
                        return_value=tour_limits):
            return challenge.fresh_overseas_limits(draft)

    def test_league_cap_is_read_without_the_xi_cache(self):
        league = mock.Mock(min_overseas=0, max_overseas=4)
        self.assertEqual(self._run({"league_key": "ipl"}, league), (0, 4))

    def test_tournament_override_wins(self):
        league = mock.Mock(min_overseas=0, max_overseas=4)
        draft = {"league_key": "ipl", "is_tournament": True, "tournament_id": 7}
        self.assertEqual(self._run(draft, league, (1, 3)), (1, 3))

    def test_multi_and_cdraft_keep_the_draft_values(self):
        self.assertIsNone(challenge.fresh_overseas_limits({"multi": True}))
        self.assertIsNone(challenge.fresh_overseas_limits({"mode": "cdraft"}))
        self.assertIsNone(
            challenge.fresh_overseas_limits({"mode": "auction_league"}))

    def test_no_league_falls_back(self):
        self.assertIsNone(self._run({"league_key": "x"}, None))


if __name__ == "__main__":
    unittest.main()
