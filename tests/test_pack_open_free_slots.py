"""Opening a pack from the inventory needs free roster slots.

A captain must have at least ``PACK_OPEN_MIN_FREE_SLOTS`` (3) empty slots
before /openpack (or the Mini App) will open a pack; otherwise the pack stays
in the inventory, untouched, and no player is rolled.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from config import MAX_ROSTER, PACK_OPEN_MIN_FREE_SLOTS
from services import pack_service


def _session(inv):
    session = mock.MagicMock()
    session.query.return_value.filter.return_value.first.return_value = inv
    return session


class PackOpenFreeSlotTests(unittest.TestCase):
    def test_rule_is_three_free_slots(self):
        self.assertEqual(PACK_OPEN_MIN_FREE_SLOTS, 3)

    def test_too_full_a_roster_is_refused_and_the_pack_kept(self):
        inv = SimpleNamespace(id=7, pack_id=1)
        session = _session(inv)
        user = SimpleNamespace(id=1, roster_count=MAX_ROSTER - 2)
        result = pack_service.open_unopened_pack(session, user, 7)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "roster_full")
        self.assertIn("3 free roster slots", result["message"])
        session.delete.assert_not_called()
        session.get.assert_not_called()          # nothing rolled

    def test_a_full_roster_is_refused(self):
        session = _session(SimpleNamespace(id=7, pack_id=1))
        user = SimpleNamespace(id=1, roster_count=MAX_ROSTER)
        self.assertFalse(
            pack_service.open_unopened_pack(session, user, 7)["success"])

    def test_three_free_slots_get_past_the_check(self):
        session = _session(SimpleNamespace(id=7, pack_id=1))
        session.get.return_value = None          # stop right after the check
        user = SimpleNamespace(id=1, roster_count=MAX_ROSTER - 3)
        result = pack_service.open_unopened_pack(session, user, 7)
        self.assertNotEqual(result.get("error"), "roster_full")
        session.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
