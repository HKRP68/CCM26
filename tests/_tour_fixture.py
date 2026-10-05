"""Shared SQLite fixture for the tournament-watch family of tests.

Each test module calls :func:`setup_module_db` / :func:`teardown_module_db`
from its own ``setUpModule``/``tearDownModule`` and subclasses
:class:`FourTeamCase` — four teams, single round-robin (3 rounds of 2), a
generated schedule and a top-4 knockout type unless the class says otherwise.
"""

import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402

_TG = itertools.count(990_001)
MODULE_NAMES = ("database", "models", "config",
                "services.tournament_service", "services.cl_tournament_view",
                "services.cl_tournament_rich", "services.match_reminder_service",
                "services.league_schedule_service", "services.knockout_service",
                "services.tournament_watch")
_STATE = {}


def setup_module_db():
    _STATE["prev"] = os.environ.get("DATABASE_URL")
    _STATE["saved"] = _module_swap.save(MODULE_NAMES)
    _module_swap.unload(MODULE_NAMES)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    _STATE["tmp"] = tmp.name
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp.name}"
    from database import Base, engine
    import models  # noqa: F401
    _STATE["engine"] = engine
    Base.metadata.create_all(bind=engine)


def teardown_module_db():
    try:
        _STATE["engine"].dispose()
    except Exception:
        pass
    if _STATE.get("prev") is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _STATE["prev"]
    _module_swap.restore(_STATE["saved"])
    try:
        os.unlink(_STATE["tmp"])
    except OSError:
        pass


def next_tg():
    return next(_TG)


class FourTeamCase(unittest.TestCase):
    KNOCKOUT = "top4_sf"
    NAMES = ("Alpha", "Bravo", "Charlie", "Delta")

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam, User)
        from services import tournament_service, league_schedule_service

        self.session = get_session()
        self.ts = tournament_service
        self.lss = league_schedule_service
        mode = ChallengeMode(name=f"Mode {next_tg()}")
        self.session.add(mode)
        self.session.flush()
        league = ChallengeLeague(mode_id=mode.id, name=f"League {next_tg()}",
                                 short_code="WCH")
        self.session.add(league)
        self.session.flush()
        self.tour = Tournament(
            name="Watch Cup", league_id=league.id, league_name=league.name,
            kind="challenge", status="active", league_format="single_rr",
            overs=20, max_teams=8, knockout_type=self.KNOCKOUT,
            announce_chat_id=-100555)
        self.session.add(self.tour)
        self.session.flush()
        self.teams, self.owners = {}, {}
        for i, name in enumerate(self.NAMES):
            owner = User(telegram_id=next_tg(), username=f"own{next_tg()}")
            self.session.add(owner)
            ct = ChallengeTeam(league_id=league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.session.flush()
            tt = TournamentTeam(tournament_id=self.tour.id, challenge_team_id=ct.id,
                                name=name, sort_order=i,
                                owner_tg_id=owner.telegram_id, owner_name=name)
            self.session.add(tt)
            self.session.flush()
            self.teams[name] = tt
            self.owners[name] = owner
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def fixtures(self, round_no=None, stage="league"):
        from models import TournamentMatch
        q = (self.session.query(TournamentMatch)
             .filter_by(tournament_id=self.tour.id, stage=stage))
        if round_no is not None:
            q = q.filter_by(round_no=round_no)
        return q.order_by(TournamentMatch.match_no).all()

    def finish_round(self, round_no, outcome="1"):
        for fx in self.fixtures(round_no):
            if fx.status != "completed":
                self.ts.simulate_fixture(self.session, fx.id, outcome)
        self.session.commit()

    def finish_league(self):
        for rnd in (1, 2, 3):
            self.finish_round(rnd)
