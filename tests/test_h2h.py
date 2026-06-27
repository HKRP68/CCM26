"""Tests for the /h2h head-to-head tally (handlers.h2h.tally_h2h).

Pure-logic tests using lightweight fake match rows — no DB or Telegram needed.
The handler module imports telegram/database/models at import time, so we stub
those (mirroring tests/test_challenge_league_commands.py) before importing it.
"""

import sys
import types
import unittest
from types import SimpleNamespace


def _load_h2h_with_stubs():
    telegram = types.ModuleType("telegram")
    telegram.Update = type("Update", (), {})
    sys.modules["telegram"] = telegram

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["telegram.ext"] = telegram_ext

    sqlalchemy = types.ModuleType("sqlalchemy")
    sqlalchemy.or_ = lambda *a, **k: None
    sqlalchemy.and_ = lambda *a, **k: None
    sys.modules["sqlalchemy"] = sqlalchemy

    database = types.ModuleType("database")
    database.get_session = lambda: None
    sys.modules["database"] = database

    models = types.ModuleType("models")
    models.Match = type("Match", (), {})
    models.User = type("User", (), {})
    sys.modules["models"] = models

    tus = types.ModuleType("services.telegram_user_service")
    tus.resolve_command_target = lambda *a, **k: (None, "missing")
    sys.modules["services.telegram_user_service"] = tus

    sys.modules.pop("handlers.h2h", None)
    from handlers.h2h import tally_h2h, _plain_name
    return tally_h2h, _plain_name


tally_h2h, _plain_name = _load_h2h_with_stubs()


def _m(winner_id=None, loser_id=None, margin_type="runs"):
    return SimpleNamespace(winner_id=winner_id, loser_id=loser_id,
                           margin_type=margin_type)


class H2HTallyTests(unittest.TestCase):
    ME, OPP = 10, 20

    def test_counts_wins_losses_and_ties(self):
        matches = [
            _m(winner_id=self.ME, loser_id=self.OPP),
            _m(winner_id=self.ME, loser_id=self.OPP),
            _m(winner_id=self.OPP, loser_id=self.ME),
            _m(margin_type="tie"),
        ]
        self.assertEqual(tally_h2h(matches, self.ME, self.OPP), (2, 1, 1))

    def test_perspective_is_symmetric(self):
        matches = [
            _m(winner_id=self.ME, loser_id=self.OPP),
            _m(winner_id=self.OPP, loser_id=self.ME),
        ]
        self.assertEqual(tally_h2h(matches, self.ME, self.OPP), (1, 1, 0))
        # Same matches from the opponent's point of view flips wins/losses.
        self.assertEqual(tally_h2h(matches, self.OPP, self.ME), (1, 1, 0))

    def test_no_winner_recorded_counts_as_tie(self):
        matches = [_m(winner_id=None, loser_id=None, margin_type="runs")]
        self.assertEqual(tally_h2h(matches, self.ME, self.OPP), (0, 0, 1))

    def test_empty_series(self):
        self.assertEqual(tally_h2h([], self.ME, self.OPP), (0, 0, 0))

    def test_plain_name_never_pings(self):
        # Uses first_name / team_name, never the @username, so /h2h won't ping.
        user = SimpleNamespace(first_name="Raj", team_name="Royals", username="rajg")
        self.assertEqual(_plain_name(user), "Raj")
        self.assertNotIn("@", _plain_name(user))
        no_first = SimpleNamespace(first_name="", team_name="Royals", username="rajg")
        self.assertEqual(_plain_name(no_first), "Royals")
        anon = SimpleNamespace(first_name="", team_name="", username="rajg")
        self.assertEqual(_plain_name(anon), "Player")


if __name__ == "__main__":
    unittest.main()
