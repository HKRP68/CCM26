import unittest
from unittest.mock import patch

from services import tournament_service


class ParseAllowedIdsTests(unittest.TestCase):
    def test_parses_mixed_separators(self):
        self.assertEqual(
            tournament_service.parse_allowed_ids("111, 222\n333;444 555"),
            {111, 222, 333, 444, 555},
        )

    def test_skips_junk_and_non_positive(self):
        self.assertEqual(
            tournament_service.parse_allowed_ids("111, abc, -5, 0, 222"),
            {111, 222},
        )

    def test_empty_returns_empty_set(self):
        self.assertEqual(tournament_service.parse_allowed_ids(None), set())
        self.assertEqual(tournament_service.parse_allowed_ids(""), set())


class IsTournamentCommandAllowedTests(unittest.TestCase):
    def test_empty_list_allows_everyone(self):
        cfg = {"tournament_allowed_ids": None}
        self.assertTrue(tournament_service.is_tournament_command_allowed(123, cfg=cfg))

    def test_non_empty_list_allows_only_listed_ids(self):
        cfg = {"tournament_allowed_ids": "111, 222"}
        self.assertTrue(tournament_service.is_tournament_command_allowed(111, cfg=cfg))
        with patch(
            "services.admin_ids.configured_admin_ids", return_value=set()
        ):
            self.assertFalse(
                tournament_service.is_tournament_command_allowed(333, cfg=cfg))

    def test_bot_admin_always_allowed(self):
        cfg = {"tournament_allowed_ids": "111"}
        with patch(
            "services.admin_ids.configured_admin_ids", return_value={999}
        ):
            self.assertTrue(
                tournament_service.is_tournament_command_allowed(999, cfg=cfg))

    def test_none_user_is_blocked(self):
        cfg = {"tournament_allowed_ids": "111"}
        self.assertFalse(tournament_service.is_tournament_command_allowed(None, cfg=cfg))


if __name__ == "__main__":
    unittest.main()
