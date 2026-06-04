import unittest

import services.config_service as config_service
from services.config_service import get_wpm_result_cards
from services.match_engine import transition_to_second_innings


class WpmResultCardsSelectionTests(unittest.TestCase):
    def setUp(self):
        self._orig_refresh = config_service._refresh

    def tearDown(self):
        config_service._refresh = self._orig_refresh

    def _stub(self, value):
        config_service._refresh = lambda session=None: {"wpm_result_cards": value}

    def test_default_summary_only(self):
        self._stub("summary")
        self.assertEqual(get_wpm_result_cards(), ["summary"])

    def test_all_cards_kept_in_reading_order(self):
        self._stub("summary,bowl2,bat1,bowl1,bat2")
        self.assertEqual(get_wpm_result_cards(),
                         ["bat1", "bowl1", "bat2", "bowl2", "summary"])

    def test_unknown_tokens_dropped(self):
        self._stub("bat1, garbage ,SUMMARY")
        self.assertEqual(get_wpm_result_cards(), ["bat1", "summary"])

    def test_empty_falls_back_to_summary(self):
        self._stub("")
        self.assertEqual(get_wpm_result_cards(), ["summary"])
        self._stub(None)
        self.assertEqual(get_wpm_result_cards(), ["summary"])


class TransitionSnapshotsInnings1Tests(unittest.TestCase):
    def _state(self):
        bat = [{"roster_id": 1, "name": "A"}, {"roster_id": 2, "name": "B"}]
        bowl = [{"roster_id": 3, "name": "C"}, {"roster_id": 4, "name": "D"}]
        return {
            "innings": 1, "overs": 20, "target": None,
            "bat_team_id": 1, "bowl_team_id": 2,
            "bat_team_name": "Team A", "bowl_team_name": "Team B",
            "bat_xi": bat, "bowl_xi": bowl, "batting_order": list(bat),
            "current_over": 21, "current_ball": 0,
            "total_runs": 150, "total_wickets": 7,
            "wides": 3, "noballs": 1, "legbyes": 2,
            "fow": [(40, 5), (80, 11)],
            "bat_stats": {"1": {}, "2": {}}, "bowl_stats": {"3": {}, "4": {}},
            "over_runs": [], "partnership_history": [],
        }

    def test_innings1_fow_and_extras_snapshotted(self):
        s = self._state()
        target = transition_to_second_innings(s)
        self.assertEqual(target, 151)
        self.assertEqual(s["inn1_fow"], [(40, 5), (80, 11)])
        self.assertEqual(s["inn1_wides"], 3)
        self.assertEqual(s["inn1_noballs"], 1)
        self.assertEqual(s["inn1_legbyes"], 2)
        # team swap happened; the inn1 batting team name is preserved
        self.assertEqual(s["inn1_team"], "Team A")
        self.assertEqual(s["bat_team_name"], "Team B")
        # the inn1 bowling team is now the (current) bowling side name
        self.assertEqual(s["bowl_team_name"], "Team A")


if __name__ == "__main__":
    unittest.main()
