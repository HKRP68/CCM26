"""Deleting tournament fixtures, and resetting a user so they can /debut again.

Both run against a real SQLite database with foreign keys switched ON, so a
delete that would trip a constraint on Postgres fails here too.

  • **fixtures.** A scheduled fixture deletes outright. A live or completed one
    needs ``force`` — and a forced delete of a completed result takes it out of
    the points table, and clears the bracket and reminder rows pointing at it.
  • **reset.** The account is emptied (squad, career card, traits, trades,
    stats, balances) without deleting the ``User`` row, and flagged
    ``needs_debut``; the bot gate then turns every command but the doorway ones
    back to /debut.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_SEQ = itertools.count(770_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.league_schedule_service",
                 "services.knockout_service", "services.user_reset_service",
                 "services.career_service", "services.player_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from sqlalchemy import event
    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys = ON")

    engine.dispose()
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
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


# ══════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════

class FixtureDeleteTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import Tournament, TournamentTeam
        from services import league_schedule_service, tournament_service

        self.session = get_session()
        self.ls = league_schedule_service
        self.ts = tournament_service
        self.tour = Tournament(name=f"Cup {next(_SEQ)}", kind="challenge",
                               status="active", format="League",
                               league_format="single_rr", overs=20, max_teams=8,
                               schedule_generated=True)
        self.session.add(self.tour)
        self.session.flush()
        self.a = TournamentTeam(tournament_id=self.tour.id, name="Alpha")
        self.b = TournamentTeam(tournament_id=self.tour.id, name="Bravo")
        self.session.add_all([self.a, self.b])
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def fixture(self, **kw):
        from models import TournamentMatch
        kw.setdefault("status", "scheduled")
        kw.setdefault("stage", "league")
        fx = TournamentMatch(tournament_id=self.tour.id, team1_id=self.a.id,
                             team2_id=self.b.id, match_no=next(_SEQ), round_no=1,
                             **kw)
        self.session.add(fx)
        self.session.commit()
        return fx

    def exists(self, fid):
        from models import TournamentMatch
        self.session.expire_all()
        return self.session.get(TournamentMatch, fid) is not None

    def test_a_scheduled_fixture_is_deleted(self):
        fx = self.fixture()
        self.assertEqual(self.ls.delete_fixture(self.session, fx.id), self.tour.id)
        self.session.commit()
        self.assertFalse(self.exists(fx.id))

    def test_a_missing_fixture_is_a_no_op(self):
        self.assertIsNone(self.ls.delete_fixture(self.session, 987654))

    def test_live_and_completed_fixtures_need_force(self):
        for status in ("live", "completed"):
            fx = self.fixture(status=status)
            with self.assertRaisesRegex(ValueError, status):
                self.ls.delete_fixture(self.session, fx.id)
            self.assertTrue(self.exists(fx.id))

    def test_force_deletes_a_live_fixture(self):
        fx = self.fixture(status="live")
        self.ls.delete_fixture(self.session, fx.id, force=True)
        self.session.commit()
        self.assertFalse(self.exists(fx.id))

    def test_force_deleting_a_result_takes_it_out_of_the_table(self):
        from models import TournamentTeam
        fx = self.fixture(status="completed", winner_team_id=self.a.id,
                          inn1_runs=180, inn1_wickets=5, inn1_balls=120,
                          inn2_runs=150, inn2_wickets=9, inn2_balls=120)
        self.ts.recompute_tournament(self.session, self.tour.id)
        self.session.commit()
        self.session.expire_all()
        self.assertEqual(self.session.get(TournamentTeam, self.a.id).won, 1)

        self.ls.delete_fixture(self.session, fx.id, force=True)
        self.session.commit()
        self.session.expire_all()
        self.assertFalse(self.exists(fx.id))
        self.assertEqual(self.session.get(TournamentTeam, self.a.id).won or 0, 0)
        self.assertEqual(self.session.get(TournamentTeam, self.a.id).points or 0, 0)

    def test_pointers_at_the_fixture_are_cleared(self):
        from models import TournamentMatch, TournamentMatchReminder
        fx = self.fixture()
        feeder = self.fixture(feeds_winner_to_id=fx.id, feeds_loser_to_id=fx.id)
        self.session.add(TournamentMatchReminder(
            tournament_id=self.tour.id, tournament_match_id=fx.id))
        self.session.commit()

        self.ls.delete_fixture(self.session, fx.id)
        self.session.commit()
        self.session.expire_all()
        feeder = self.session.get(TournamentMatch, feeder.id)
        self.assertIsNone(feeder.feeds_winner_to_id)
        self.assertIsNone(feeder.feeds_loser_to_id)
        self.assertEqual(self.session.query(TournamentMatchReminder)
                         .filter_by(tournament_match_id=fx.id).count(), 0)


# ══════════════════════════════════════════════════════════════════════
# Reset user
# ══════════════════════════════════════════════════════════════════════

class UserResetTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import (Player, PlayerTrait, Trade, Trait, TraitInventory,
                            User, UserRoster, UserStats)

        self.session = get_session()
        s = self.session
        n = next(_SEQ)

        def player(**kw):
            p = Player(name=f"P{next(_SEQ)}", rating=80, category="Batsman",
                       country="India", bat_hand="Right", bowl_hand="Right",
                       bowl_style="Medium", **kw)
            s.add(p)
            return p

        self.user = User(telegram_id=n, first_name="Reset Me", team_name="Kept XI",
                         total_coins=50_000, total_gems=40, quest_points=12,
                         roster_count=2, matches_played=9, matches_won=5,
                         subscription_tier="gold")
        self.other = User(telegram_id=n + 500_000, first_name="Opponent")
        s.add_all([self.user, self.other])
        s.flush()

        p1, p2, p3 = player(), player(), player()
        career = player(is_career=True, career_owner_user_id=self.user.id)
        s.flush()
        r1 = UserRoster(user_id=self.user.id, player_id=p1.id)
        r2 = UserRoster(user_id=self.user.id, player_id=p2.id)
        rc = UserRoster(user_id=self.user.id, player_id=career.id)
        ro = UserRoster(user_id=self.other.id, player_id=p3.id)
        s.add_all([r1, r2, rc, ro])
        s.add(UserStats(user_id=self.user.id))
        s.flush()

        trait = Trait(name=f"T{n}", category="bat", description="d", effect_key="k")
        s.add(trait)
        s.flush()
        s.add(PlayerTrait(user_id=self.user.id, roster_id=r1.id, trait_id=trait.id))
        s.add(TraitInventory(user_id=self.user.id, trait_id=trait.id))
        s.add(Trade(initiator_id=self.other.id, receiver_id=self.user.id,
                    initiator_player_id=p3.id, receiver_player_id=p2.id,
                    initiator_roster_id=ro.id, receiver_roster_id=r2.id,
                    expires_at=datetime.utcnow() + timedelta(days=1)))
        s.commit()
        self.other_roster_id = ro.id

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def test_progress_is_wiped_and_the_account_kept(self):
        from models import (Player, PlayerTrait, Trade, TraitInventory, User,
                            UserRoster)
        from services.user_reset_service import reset_user

        result = reset_user(self.session, self.user)
        self.session.commit()
        self.session.expire_all()
        uid = self.user.id

        self.assertEqual(result["before"]["coins"], 50_000)
        user = self.session.get(User, uid)
        self.assertIsNotNone(user)
        self.assertTrue(user.needs_debut)
        self.assertEqual((user.total_coins, user.total_gems, user.quest_points,
                          user.roster_count, user.matches_played), (0, 0, 0, 0, 0))
        # Identity and paid-for things stay.
        self.assertEqual(user.team_name, "Kept XI")
        self.assertEqual(user.subscription_tier, "gold")

        for model in (UserRoster, PlayerTrait, TraitInventory):
            self.assertEqual(self.session.query(model)
                             .filter(model.user_id == uid).count(), 0, model)
        self.assertEqual(self.session.query(Trade).count(), 0)
        self.assertEqual(self.session.query(Player)
                         .filter(Player.career_owner_user_id == uid).count(), 0)
        # The opponent's squad is untouched.
        self.assertIsNotNone(self.session.get(UserRoster, self.other_roster_id))

    def test_the_gate_sends_everything_but_the_doorway_back_to_debut(self):
        from services.user_reset_service import should_block_update

        def cmd(text):
            return SimpleNamespace(
                callback_query=None,
                effective_message=SimpleNamespace(text=text, caption=None,
                                                  entities=None),
                message=SimpleNamespace(text=text, caption=None, entities=None))

        self.assertTrue(should_block_update(cmd("/myroster")))
        self.assertTrue(should_block_update(cmd("/daily")))
        self.assertFalse(should_block_update(cmd("/debut")))
        self.assertFalse(should_block_update(cmd("/start")))
        self.assertFalse(should_block_update(cmd("just chatting")))
        tap = SimpleNamespace(callback_query=SimpleNamespace(data="roster_page_2"))
        self.assertTrue(should_block_update(tap))


if __name__ == "__main__":
    unittest.main()
