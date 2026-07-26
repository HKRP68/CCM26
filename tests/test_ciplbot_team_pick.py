"""The bot must only ever take a league team it can actually field an XI from.

Picking the opponent needs the squad size of every team in the league. That is a
grouped COUNT (`_league_squad_sizes`) rather than a query per team, and the SQL
has two easy ways to be subtly wrong: an inner join silently drops empty teams,
and forgetting the league filter merges a same-named team from another league
into the count. Both would surface as "the bot picked a team with no players",
so they are pinned here against a real database.

Follows the throwaway-sqlite pattern from `tests/test_trait_return_to_inventory.py`.
"""

import os
import sys
import tempfile
import unittest


_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "handlers.challenge")


def setUpModule():
    """Point the real SQLAlchemy models at a throwaway sqlite file."""
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class BotTeamPickTests(unittest.TestCase):
    """One league of three teams — full, thin and empty — plus a decoy team of
    the same name in a second league."""

    @classmethod
    def setUpClass(cls):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            ChallengePlayer)

        session = get_session()
        try:
            mode = ChallengeMode(name="Leagues", is_active=True)
            session.add(mode)
            session.flush()
            league = ChallengeLeague(mode_id=mode.id, name="IPL",
                                     short_code="ipl", is_active=True)
            decoy_league = ChallengeLeague(mode_id=mode.id, name="BBL",
                                           short_code="bbl", is_active=True)
            session.add_all([league, decoy_league])
            session.flush()
            cls.league_id = league.id

            for name, size in (("Full FC", 15), ("Thin FC", 4), ("Empty FC", 0)):
                team = ChallengeTeam(league_id=league.id, name=name, is_active=True)
                session.add(team)
                session.flush()
                for i in range(size):
                    session.add(ChallengePlayer(team_id=team.id,
                                                name=f"{name} p{i}",
                                                details_json="{}"))

            # Same team NAME in another league, with a big squad. It must not be
            # folded into the IPL counts.
            decoy = ChallengeTeam(league_id=decoy_league.id, name="Full FC",
                                  is_active=True)
            session.add(decoy)
            session.flush()
            for i in range(20):
                session.add(ChallengePlayer(team_id=decoy.id, name=f"bbl p{i}",
                                            details_json="{}"))
            session.commit()
        finally:
            session.close()

    def _draft(self):
        return {"league_id": self.league_id, "league_key": "ipl",
                "teams": ["Full FC", "Thin FC", "Empty FC"]}

    def test_squad_sizes_are_league_scoped_and_include_empty_teams(self):
        from database import get_session
        from handlers.challenge import _league_squad_sizes

        session = get_session()
        try:
            sizes = _league_squad_sizes(session, self._draft())
        finally:
            session.close()
        # Empty FC must be present at 0 (an inner join would drop it), and
        # Full FC must be 15, not 35 (the BBL team of the same name).
        self.assertEqual(sizes, {"Full FC": 15, "Thin FC": 4, "Empty FC": 0})

    def test_only_picks_a_team_that_can_field_an_xi(self):
        from handlers.challenge import _pick_bot_league_team
        draft = self._draft()
        # The user took Thin FC, so Full FC is the only fieldable team left.
        picks = {_pick_bot_league_team(draft, "Thin FC") for _ in range(30)}
        self.assertEqual(picks, {"Full FC"})

    def test_returns_none_when_no_remaining_team_has_eleven(self):
        from handlers.challenge import _pick_bot_league_team
        draft = self._draft()
        # The user took the only full squad — surfacing None lets the caller
        # report a real league-configuration problem instead of starting a
        # match the bot cannot field.
        picks = {_pick_bot_league_team(draft, "Full FC") for _ in range(30)}
        self.assertEqual(picks, {None})


if __name__ == "__main__":
    unittest.main()
