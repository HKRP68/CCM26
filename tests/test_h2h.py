"""Tests for the /h2h head-to-head tally (handlers.h2h.tally_h2h).

Pure-logic tests using lightweight fake match rows — no DB or Telegram needed.
The handler module imports telegram/database/models at import time, so we stub
those (mirroring tests/test_challenge_league_commands.py) before importing it.
"""

import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace


# Modules we inject only when the real one is missing (bare test env). They are
# removed in tearDownModule so we never clobber real modules under `discover`
# in an environment where telegram/sqlalchemy ARE installed.
_INJECTED = []


def _is_available(name):
    """True if the real module actually imports.

    We attempt a real import rather than just `find_spec`, because local modules
    like `database`/`models` exist as files (so find_spec succeeds) yet fail to
    import when their third-party deps (sqlalchemy) are absent — exactly the case
    where we still want to inject a stub.
    """
    if name in sys.modules:
        return True
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def _stub(name, build):
    if not _is_available(name):
        sys.modules[name] = build()
        _INJECTED.append(name)


def _load_h2h_with_stubs():
    def _telegram():
        m = types.ModuleType("telegram")
        m.Update = type("Update", (), {})
        return m

    def _telegram_ext():
        m = types.ModuleType("telegram.ext")
        m.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
        return m

    def _sqlalchemy():
        m = types.ModuleType("sqlalchemy")
        m.or_ = lambda *a, **k: None
        m.and_ = lambda *a, **k: None
        return m

    def _database():
        m = types.ModuleType("database")
        m.get_session = lambda: None
        return m

    def _models():
        m = types.ModuleType("models")
        m.Match = type("Match", (), {})
        m.User = type("User", (), {})
        return m

    def _tus():
        m = types.ModuleType("services.telegram_user_service")
        m.resolve_command_target = lambda *a, **k: (None, "missing")
        return m

    _stub("telegram", _telegram)
    _stub("telegram.ext", _telegram_ext)
    _stub("sqlalchemy", _sqlalchemy)
    _stub("database", _database)
    _stub("models", _models)
    _stub("services.telegram_user_service", _tus)

    sys.modules.pop("handlers.h2h", None)
    from handlers.h2h import tally_h2h, _plain_name
    return tally_h2h, _plain_name


def tearDownModule():
    for name in _INJECTED:
        sys.modules.pop(name, None)
    sys.modules.pop("handlers.h2h", None)


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
