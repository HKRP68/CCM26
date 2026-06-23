"""Tests for the Challenge League Tournament command allowlist logic."""

import unittest
from unittest.mock import patch

from services import tournament_service


class ParseAllowedIdsTests(unittest.TestCase):
    """parse_allowed_ids should leniently extract positive Telegram IDs."""

    def test_parses_mixed_separators(self):
        """Commas, spaces, newlines and semicolons all separate IDs."""
        self.assertEqual(
            tournament_service.parse_allowed_ids("111, 222\n333;444 555"),
            {111, 222, 333, 444, 555},
        )

    def test_skips_junk_and_non_positive(self):
        """Non-numeric, zero and negative tokens are dropped."""
        self.assertEqual(
            tournament_service.parse_allowed_ids("111, abc, -5, 0, 222"),
            {111, 222},
        )

    def test_empty_returns_empty_set(self):
        """None or empty input yields an empty set."""
        self.assertEqual(tournament_service.parse_allowed_ids(None), set())
        self.assertEqual(tournament_service.parse_allowed_ids(""), set())


class IsTournamentCommandAllowedTests(unittest.TestCase):
    """is_tournament_command_allowed gates the tournament command correctly."""

    def test_empty_list_allows_everyone(self):
        """An empty allowlist leaves the command open to all users."""
        cfg = {"tournament_allowed_ids": None}
        self.assertTrue(tournament_service.is_tournament_command_allowed(123, cfg=cfg))

    def test_non_empty_list_allows_only_listed_ids(self):
        """A non-empty allowlist admits listed IDs and blocks non-admins."""
        cfg = {"tournament_allowed_ids": "111, 222"}
        self.assertTrue(tournament_service.is_tournament_command_allowed(111, cfg=cfg))
        with patch(
            "services.admin_ids.configured_admin_ids", return_value=set()
        ):
            self.assertFalse(
                tournament_service.is_tournament_command_allowed(333, cfg=cfg))

    def test_bot_admin_always_allowed(self):
        """Bot admins pass even when not on the allowlist."""
        cfg = {"tournament_allowed_ids": "111"}
        with patch(
            "services.admin_ids.configured_admin_ids", return_value={999}
        ):
            self.assertTrue(
                tournament_service.is_tournament_command_allowed(999, cfg=cfg))

    def test_none_user_is_blocked(self):
        """A missing user id is never allowed."""
        cfg = {"tournament_allowed_ids": "111"}
        self.assertFalse(tournament_service.is_tournament_command_allowed(None, cfg=cfg))


if __name__ == "__main__":
    unittest.main()
