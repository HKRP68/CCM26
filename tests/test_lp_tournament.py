"""The Lets Play Tournament: a competition whose teams are Telegram users.

These cover the parts that are genuinely new — everything else (points table,
net run rate, the round-robin generator, the knockout bracket) is the Challenge
League tournament engine, already covered by its own tests, and is exercised here
only to prove a user-team tournament really does drive it:

  • a participant is a Telegram id, and can be entered before they have ever
    run /debut
  • one entry per person, a squad cap that holds, and no squad edits once a
    result is in
  • the two competition families are independent: one Challenge League and one
    Lets Play tournament can be live at the same time, and neither activation
    knocks the other off the air
  • a completed match is credited to the right two users, in the right fixture
  • fixture gating: you may only play a pairing you still have a fixture for,
    and only while the tournament is actually running
  • /lptour refuses everything the rules refuse, and delegates to /letsplay
    with the tournament attached when the rules allow it
"""

import asyncio
import itertools
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

_TG = itertools.count(880_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.lp_tournament_service",
                 "services.league_schedule_service", "services.knockout_service")


def setUpModule():
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


class LPTCase(unittest.TestCase):
    """A drafted Lets Play tournament with four registered users available."""

    def setUp(self):
        from database import get_session
        from models import User
        from services import lp_tournament_service as lpt

        self.session = get_session()
        self.lpt = lpt
        self.users = []
        for i in range(4):
            u = User(telegram_id=next(_TG), username=f"lpuser{i}", roster_count=0)
            self.session.add(u)
            self.users.append(u)
        self.session.flush()
        self.tour = lpt.create_tournament(self.session, "Test Cup",
                                          league_format="single", max_teams=8)
        lpt.activate(self.session, self.tour.id, keep_draft=True)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    # ── helpers ──

    def enter_all(self):
        for u in self.users:
            self.lpt.add_participant(self.session, self.tour.id, u.telegram_id)
        self.session.commit()

    def go_live(self):
        from services import tournament_service as ts
        ts.set_status(self.session, self.tour.id, "active")
        self.session.commit()

    def record(self, batting_first, chasing, *, inn1=180, inn2=170):
        """Record a completed match between two entrants, first-innings winner."""
        from models import Match
        from services import tournament_service as ts
        t1 = self.lpt.team_for_tg(self.session, self.tour.id,
                                  batting_first.telegram_id)
        t2 = self.lpt.team_for_tg(self.session, self.tour.id, chasing.telegram_id)
        match = Match(user1_id=batting_first.id, user2_id=chasing.id,
                      status="active", match_type="letsplay", overs=20,
                      tournament_id=self.tour.id)
        self.session.add(match)
        self.session.flush()
        state = {
            "match_id": match.id,
            "tournament_id": self.tour.id,
            "tournament_tteam_by_user": {batting_first.id: t1.id, chasing.id: t2.id},
            "inn1_bat_team_id": batting_first.id,
            "inn1_bowl_team_id": chasing.id,
            "bat_team_id": chasing.id,
            "inn1_runs": inn1, "inn1_wickets": 6,
            "total_runs": inn2, "total_wickets": 9,
            "inn1_bat_stats": {1: {"balls": 120, "runs": inn1, "name": "Opener",
                                   "out": True}},
            "bat_stats": {2: {"balls": 120, "runs": inn2, "name": "Chaser",
                              "out": True}},
        }
        row = ts.record_tournament_match(self.session, state)
        self.session.commit()
        return row


# ══════════════════════════════════════════════════════════════════════
# Entering the field by Telegram id
# ══════════════════════════════════════════════════════════════════════

class ParticipantTests(LPTCase):
    def test_a_participant_is_identified_by_telegram_id(self):
        team, _cleared = self.lpt.add_participant(
            self.session, self.tour.id, self.users[0].telegram_id)
        self.session.commit()
        self.assertEqual(team.user_tg_id, self.users[0].telegram_id)
        # And it is NOT a Challenge League team — that column stays empty, which
        # is what keeps the two kinds of tournament from colliding.
        self.assertIsNone(team.challenge_team_id)

    def test_the_team_name_defaults_to_their_handle(self):
        team, _ = self.lpt.add_participant(self.session, self.tour.id,
                                           self.users[0].telegram_id)
        self.assertEqual(team.name, "lpuser0")

    def test_an_admin_may_name_the_team(self):
        team, _ = self.lpt.add_participant(self.session, self.tour.id,
                                           self.users[0].telegram_id,
                                           name="Mumbai Mavericks")
        self.assertEqual(team.name, "Mumbai Mavericks")

    def test_someone_who_has_never_played_can_still_be_entered(self):
        # The whole point of keying on the Telegram id: an admin builds the draw
        # from ids, and the people in it may not have accounts yet.
        team, _ = self.lpt.add_participant(self.session, self.tour.id, 4_242_424)
        self.session.commit()
        self.assertEqual(team.user_tg_id, 4_242_424)
        self.assertEqual(team.name, "Player 4242424")

    def test_a_placeholder_name_is_filled_in_once_they_register(self):
        from models import User
        team, _ = self.lpt.add_participant(self.session, self.tour.id, 4_242_425)
        self.session.add(User(telegram_id=4_242_425, username="latecomer",
                              roster_count=0))
        self.session.flush()
        changed = self.lpt.sync_participant_names(self.session, self.tour.id)
        self.session.commit()
        self.assertEqual(changed, 1)
        self.assertEqual(team.name, "latecomer")

    def test_sync_never_overwrites_a_name_an_admin_chose(self):
        from models import User
        team, _ = self.lpt.add_participant(self.session, self.tour.id, 4_242_426,
                                           name="Hand Picked XI")
        self.session.add(User(telegram_id=4_242_426, username="whoever",
                              roster_count=0))
        self.session.flush()
        self.lpt.sync_participant_names(self.session, self.tour.id)
        self.assertEqual(team.name, "Hand Picked XI")

    def test_the_same_person_cannot_be_entered_twice(self):
        self.lpt.add_participant(self.session, self.tour.id,
                                 self.users[0].telegram_id)
        self.session.commit()
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.add_participant(self.session, self.tour.id,
                                     self.users[0].telegram_id)

    def test_two_people_may_not_share_a_team_name(self):
        self.lpt.add_participant(self.session, self.tour.id,
                                 self.users[0].telegram_id, name="Same Name")
        team, _ = self.lpt.add_participant(self.session, self.tour.id,
                                           self.users[1].telegram_id,
                                           name="Same Name")
        self.session.commit()
        # The clash is broken by the id rather than refused — an admin pasting a
        # list of ids should not have the whole entry rejected over a name.
        self.assertNotEqual(team.name, "Same Name")
        self.assertIn(str(self.users[1].telegram_id), team.name)

    def test_the_squad_cap_is_enforced(self):
        self.tour.max_teams = 2
        self.session.flush()
        self.lpt.add_participant(self.session, self.tour.id,
                                 self.users[0].telegram_id)
        self.lpt.add_participant(self.session, self.tour.id,
                                 self.users[1].telegram_id)
        self.session.commit()
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.add_participant(self.session, self.tour.id,
                                     self.users[2].telegram_id)

    def test_a_group_id_is_refused_as_a_participant(self):
        # Negative ids are chats, not people, and would never be able to play.
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.add_participant(self.session, self.tour.id, -100123)

    def test_junk_is_refused_with_a_readable_message(self):
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.add_participant(self.session, self.tour.id, "not-an-id")

    def test_removing_a_participant_takes_them_out(self):
        self.enter_all()
        name, _cleared = self.lpt.remove_participant(
            self.session, self.tour.id, self.users[0].telegram_id)
        self.session.commit()
        self.assertEqual(name, "lpuser0")
        self.assertEqual(len(self.lpt.participants(self.session, self.tour.id)), 3)

    def test_renaming_refuses_a_name_another_team_holds(self):
        self.enter_all()
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.rename_participant(self.session, self.tour.id,
                                        self.users[0].telegram_id, "lpuser1")


class SquadLockTests(LPTCase):
    """A scheduled tournament that is under way can no longer be reshaped."""

    def setUp(self):
        super().setUp()
        self.enter_all()
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.go_live()
        self.record(self.users[0], self.users[1])

    def test_no_new_entrant_once_the_schedule_is_under_way(self):
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.add_participant(self.session, self.tour.id, 5_555_555)

    def test_no_removal_after_a_result(self):
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.remove_participant(self.session, self.tour.id,
                                        self.users[2].telegram_id)

    def test_no_reschedule_after_a_result(self):
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.generate_schedule(self.session, self.tour.id)

    def test_a_reset_unfreezes_the_field(self):
        from services import tournament_service as ts
        ts.reset_tournament(self.session, self.tour.id)
        self.session.commit()
        team, _ = self.lpt.add_participant(self.session, self.tour.id, 5_555_556)
        self.assertIsNotNone(team.id)

    def test_a_free_play_tournament_still_takes_a_latecomer(self):
        # No fixture list to invalidate, so a late entrant breaks nothing: they
        # simply start on zero. Blocking this would make the simplest way to run
        # a tournament the least flexible one.
        from models import TournamentMatch
        (self.session.query(TournamentMatch)
         .filter_by(tournament_id=self.tour.id, status="scheduled")
         .delete(synchronize_session=False))
        self.tour.schedule_generated = False
        self.session.commit()
        team, _cleared = self.lpt.add_participant(self.session, self.tour.id,
                                                 5_555_558)
        self.session.commit()
        self.assertIsNotNone(team.id)
        self.assertEqual(team.points, 0)

    def test_a_free_play_tournament_still_refuses_a_removal_after_a_result(self):
        # The other side of it: removing somebody would orphan the matches they
        # played and silently take those points off whoever beat them.
        from models import TournamentMatch
        (self.session.query(TournamentMatch)
         .filter_by(tournament_id=self.tour.id, status="scheduled")
         .delete(synchronize_session=False))
        self.tour.schedule_generated = False
        self.session.commit()
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.remove_participant(self.session, self.tour.id,
                                        self.users[0].telegram_id)

    def test_changing_the_field_before_any_result_clears_the_schedule(self):
        from models import TournamentMatch
        from services import tournament_service as ts
        ts.reset_tournament(self.session, self.tour.id)
        self.session.commit()
        _team, cleared = self.lpt.add_participant(self.session, self.tour.id,
                                                  5_555_557)
        self.session.commit()
        self.assertTrue(cleared)
        self.assertFalse(self.tour.schedule_generated)
        # Stale fixtures are gone rather than left pointing at the old field.
        self.assertEqual(
            self.session.query(TournamentMatch)
            .filter_by(tournament_id=self.tour.id).count(), 0)


# ══════════════════════════════════════════════════════════════════════
# The two competition families are independent
# ══════════════════════════════════════════════════════════════════════

class KindIsolationTests(LPTCase):
    def _challenge_tournament(self, *, active):
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam)
        from services import tournament_service as ts
        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                 tournament_command="/cipl_tournament")
        self.session.add(league)
        self.session.flush()
        squads = [ChallengeTeam(league_id=league.id, name=f"Squad {next(_TG)}")
                  for _ in range(4)]
        for squad in squads:
            self.session.add(squad)
        self.session.flush()
        tour = Tournament(name=f"CIPL {next(_TG)}", league_id=league.id,
                          league_name=league.name, status="active")
        self.session.add(tour)
        self.session.flush()
        for squad in squads:
            self.session.add(TournamentTeam(tournament_id=tour.id,
                                            challenge_team_id=squad.id,
                                            name=squad.name))
        if active:
            ts.activate_tournament(self.session, tour.id)
        self.session.commit()
        return tour

    def test_a_new_tournament_defaults_to_the_challenge_kind(self):
        from services import tournament_service as ts
        cipl = self._challenge_tournament(active=False)
        self.assertEqual(ts.tournament_kind(cipl), ts.KIND_CHALLENGE)

    def test_a_lets_play_tournament_is_not_the_challenge_league_one(self):
        from services import tournament_service as ts
        # self.tour is a live Lets Play tournament, and nothing else exists.
        self.assertIsNone(ts.get_active_tournament(self.session))
        self.assertEqual(
            ts.get_active_tournament(self.session, kind=ts.KIND_LETSPLAY).id,
            self.tour.id)

    def test_both_kinds_can_be_live_at_once(self):
        from services import tournament_service as ts
        cipl = self._challenge_tournament(active=True)
        self.session.expire_all()
        self.assertEqual(ts.get_active_tournament(self.session).id, cipl.id)
        self.assertEqual(
            ts.get_active_tournament(self.session, kind=ts.KIND_LETSPLAY).id,
            self.tour.id)

    def test_activating_a_lets_play_tournament_leaves_the_league_one_alone(self):
        from models import Tournament
        cipl = self._challenge_tournament(active=True)
        second = self.lpt.create_tournament(self.session, f"Second {next(_TG)}")
        self.lpt.activate(self.session, second.id)
        self.session.commit()
        self.session.expire_all()
        self.assertTrue(self.session.query(Tournament).get(cipl.id).is_active)
        # …but it does take the previous Lets Play tournament off the air.
        self.assertFalse(self.session.query(Tournament).get(self.tour.id).is_active)

    def test_kind_reads_as_challenge_when_the_column_is_null(self):
        # Databases migrated before the column existed can hold NULL, and every
        # tournament that predates Lets Play is a Challenge League one.
        from services import tournament_service as ts
        self.assertEqual(ts.tournament_kind(SimpleNamespace(kind=None)),
                         ts.KIND_CHALLENGE)

    def test_a_lets_play_lookup_by_id_refuses_a_challenge_tournament(self):
        cipl = self._challenge_tournament(active=False)
        self.assertIsNone(self.lpt.get_tournament(self.session, cipl.id))

    def test_get_tournament_finds_a_lets_play_one(self):
        self.assertEqual(
            self.lpt.get_tournament(self.session, self.tour.id).id, self.tour.id)


# ══════════════════════════════════════════════════════════════════════
# Recording a result
# ══════════════════════════════════════════════════════════════════════

class ResultTests(LPTCase):
    def setUp(self):
        super().setUp()
        self.enter_all()
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.go_live()

    def test_the_result_is_credited_to_the_two_users_who_played(self):
        row = self.record(self.users[0], self.users[1])
        winner = self.lpt.team_for_tg(self.session, self.tour.id,
                                      self.users[0].telegram_id)
        loser = self.lpt.team_for_tg(self.session, self.tour.id,
                                     self.users[1].telegram_id)
        self.assertEqual(row.winner_team_id, winner.id)
        self.assertEqual(winner.won, 1)
        self.assertEqual(winner.points, self.tour.points_win)
        self.assertEqual(loser.lost, 1)
        self.assertEqual(loser.points, self.tour.points_loss)

    def test_the_result_fills_the_scheduled_fixture_rather_than_adding_a_row(self):
        from models import TournamentMatch
        before = (self.session.query(TournamentMatch)
                  .filter_by(tournament_id=self.tour.id).count())
        row = self.record(self.users[0], self.users[1])
        after = (self.session.query(TournamentMatch)
                 .filter_by(tournament_id=self.tour.id).count())
        self.assertEqual(before, after)
        self.assertEqual(row.status, "completed")

    def test_net_run_rate_moves_the_right_way(self):
        from services import tournament_service as ts
        self.record(self.users[0], self.users[1], inn1=200, inn2=150)
        by_name = {tt.name: tt
                   for tt in ts.points_table(self.session, self.tour.id)}
        self.assertGreater(by_name["lpuser0"]._nrr, 0)
        self.assertLess(by_name["lpuser1"]._nrr, 0)

    def test_a_tie_gives_both_sides_the_tie_points(self):
        self.record(self.users[0], self.users[1], inn1=160, inn2=160)
        a = self.lpt.team_for_tg(self.session, self.tour.id,
                                 self.users[0].telegram_id)
        b = self.lpt.team_for_tg(self.session, self.tour.id,
                                 self.users[1].telegram_id)
        self.assertEqual((a.tied, b.tied), (1, 1))
        self.assertEqual(a.points, self.tour.points_tie)
        self.assertEqual(b.points, self.tour.points_tie)

    def test_recording_is_idempotent_on_the_match(self):
        from services import tournament_service as ts
        row = self.record(self.users[0], self.users[1])
        t1 = self.lpt.team_for_tg(self.session, self.tour.id,
                                  self.users[0].telegram_id)
        t2 = self.lpt.team_for_tg(self.session, self.tour.id,
                                  self.users[1].telegram_id)
        again = ts.record_tournament_match(self.session, {
            "match_id": row.match_id, "tournament_id": self.tour.id,
            "tournament_tteam_by_user": {self.users[0].id: t1.id,
                                         self.users[1].id: t2.id},
            "inn1_bat_team_id": self.users[0].id,
            "bat_team_id": self.users[1].id,
            "inn1_runs": 180, "total_runs": 170,
        })
        self.assertIsNone(again)
        self.assertEqual(t1.played, 1)

    def test_a_state_pointing_at_another_tournaments_team_records_nothing(self):
        from services import tournament_service as ts
        from models import Match, TournamentMatch
        other = self.lpt.create_tournament(self.session, f"Other {next(_TG)}")
        foreign, _ = self.lpt.add_participant(self.session, other.id,
                                              self.users[0].telegram_id)
        mine = self.lpt.team_for_tg(self.session, self.tour.id,
                                    self.users[1].telegram_id)
        match = Match(user1_id=self.users[0].id, user2_id=self.users[1].id,
                      status="active", match_type="letsplay", overs=20)
        self.session.add(match)
        self.session.flush()
        before = (self.session.query(TournamentMatch)
                  .filter_by(tournament_id=self.tour.id,
                             status="completed").count())
        ts.record_tournament_match(self.session, {
            "match_id": match.id, "tournament_id": self.tour.id,
            "tournament_tteam_by_user": {self.users[0].id: foreign.id,
                                         self.users[1].id: mine.id},
            "inn1_bat_team_id": self.users[0].id,
            "bat_team_id": self.users[1].id,
            "inn1_runs": 180, "total_runs": 170,
        })
        self.session.commit()
        after = (self.session.query(TournamentMatch)
                 .filter_by(tournament_id=self.tour.id,
                            status="completed").count())
        self.assertEqual(before, after)


# ══════════════════════════════════════════════════════════════════════
# Who may play, and when
# ══════════════════════════════════════════════════════════════════════

class CheckPairTests(LPTCase):
    def setUp(self):
        super().setUp()
        self.enter_all()

    def test_a_draft_tournament_cannot_be_played(self):
        with self.assertRaises(self.lpt.LPTError) as ctx:
            self.lpt.check_pair(self.session, self.tour,
                                self.users[0].telegram_id,
                                self.users[1].telegram_id)
        self.assertIn("hasn't started", str(ctx.exception))

    def test_a_paused_tournament_cannot_be_played(self):
        from services import tournament_service as ts
        ts.set_status(self.session, self.tour.id, "paused")
        with self.assertRaises(self.lpt.LPTError) as ctx:
            self.lpt.check_pair(self.session, self.tour,
                                self.users[0].telegram_id,
                                self.users[1].telegram_id)
        self.assertIn("paused", str(ctx.exception))

    def test_a_completed_tournament_cannot_be_played(self):
        from services import tournament_service as ts
        ts.set_status(self.session, self.tour.id, "completed")
        with self.assertRaises(self.lpt.LPTError) as ctx:
            self.lpt.check_pair(self.session, self.tour,
                                self.users[0].telegram_id,
                                self.users[1].telegram_id)
        self.assertIn("completed", str(ctx.exception))

    def test_two_entrants_may_play_a_live_free_play_tournament(self):
        self.go_live()
        host, guest = self.lpt.check_pair(self.session, self.tour,
                                          self.users[0].telegram_id,
                                          self.users[1].telegram_id)
        self.assertEqual(host.name, "lpuser0")
        self.assertEqual(guest.name, "lpuser1")

    def test_free_play_lets_a_pairing_repeat(self):
        # No schedule generated → no fixture list to consume.
        self.go_live()
        self.record(self.users[0], self.users[1])
        self.lpt.check_pair(self.session, self.tour, self.users[0].telegram_id,
                            self.users[1].telegram_id)

    def test_a_non_entrant_is_refused(self):
        self.go_live()
        with self.assertRaises(self.lpt.LPTError) as ctx:
            self.lpt.check_pair(self.session, self.tour,
                                self.users[0].telegram_id, 7_777_777)
        self.assertIn("aren't entered", str(ctx.exception))

    def test_playing_yourself_is_refused(self):
        self.go_live()
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.check_pair(self.session, self.tour,
                                self.users[0].telegram_id,
                                self.users[0].telegram_id)

    def test_a_scheduled_tournament_refuses_a_pairing_with_no_fixture_left(self):
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.go_live()
        self.record(self.users[0], self.users[1])
        with self.assertRaises(self.lpt.LPTError) as ctx:
            self.lpt.check_pair(self.session, self.tour,
                                self.users[0].telegram_id,
                                self.users[1].telegram_id)
        self.assertIn("no fixture left", str(ctx.exception))

    def test_a_refusal_is_plain_text_so_the_caller_escapes_it_once(self):
        # LPTError messages quote names the user chose. If the service escaped
        # them too, the handler's escape would double it and the player would be
        # shown "&amp;lt;b&amp;gt;".
        self.tour.name = "Bob's <Cup> & Co"
        self.lpt.rename_participant(self.session, self.tour.id,
                                    self.users[1].telegram_id, "A & B <XI>")
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.go_live()
        self.record(self.users[0], self.users[1])
        with self.assertRaises(self.lpt.LPTError) as ctx:
            self.lpt.check_pair(self.session, self.tour,
                                self.users[0].telegram_id,
                                self.users[1].telegram_id)
        message = str(ctx.exception)
        self.assertIn("A & B <XI>", message)
        self.assertIn("Bob's <Cup> & Co", message)
        self.assertNotIn("&amp;", message)
        self.assertNotIn("&lt;", message)

    def test_a_double_round_robin_leaves_the_return_fixture_playable(self):
        self.tour.league_format = "double_rr"
        self.session.flush()
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.go_live()
        self.record(self.users[0], self.users[1])
        # Home and away: the pair still owe each other one.
        self.lpt.check_pair(self.session, self.tour, self.users[0].telegram_id,
                            self.users[1].telegram_id)


class FixtureReservationTests(LPTCase):
    def setUp(self):
        super().setUp()
        self.enter_all()
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.go_live()
        self.a = self.lpt.team_for_tg(self.session, self.tour.id,
                                      self.users[0].telegram_id)
        self.b = self.lpt.team_for_tg(self.session, self.tour.id,
                                      self.users[1].telegram_id)

    def test_reserving_marks_the_fixture_live(self):
        from models import TournamentMatch
        fixture_id = self.lpt.reserve_pair_fixture(self.session, self.tour,
                                                   self.a.id, self.b.id)
        self.session.commit()
        self.assertEqual(
            self.session.query(TournamentMatch).get(fixture_id).status, "live")

    def test_a_second_reservation_of_the_same_pairing_is_refused(self):
        self.lpt.reserve_pair_fixture(self.session, self.tour, self.a.id, self.b.id)
        self.session.commit()
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.reserve_pair_fixture(self.session, self.tour,
                                          self.a.id, self.b.id)

    def test_a_released_fixture_can_be_reserved_again(self):
        from services import league_schedule_service as lss
        fixture_id = self.lpt.reserve_pair_fixture(self.session, self.tour,
                                                   self.a.id, self.b.id)
        self.session.commit()
        lss.release_fixture(self.session, fixture_id)
        self.session.commit()
        self.assertEqual(
            self.lpt.reserve_pair_fixture(self.session, self.tour,
                                          self.a.id, self.b.id),
            fixture_id)

    def test_free_play_has_nothing_to_reserve(self):
        from models import TournamentMatch
        (self.session.query(TournamentMatch)
         .filter_by(tournament_id=self.tour.id).delete(synchronize_session=False))
        self.tour.schedule_generated = False
        self.tour.knockout_generated = False
        self.session.commit()
        self.assertIsNone(
            self.lpt.reserve_pair_fixture(self.session, self.tour,
                                          self.a.id, self.b.id))


# ══════════════════════════════════════════════════════════════════════
# The playoff bracket
# ══════════════════════════════════════════════════════════════════════

class KnockoutTests(LPTCase):
    def setUp(self):
        super().setUp()
        self.enter_all()
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.go_live()

    def _play_out_the_league(self):
        from models import TournamentMatch
        from services import tournament_service as ts
        fixtures = (self.session.query(TournamentMatch)
                    .filter_by(tournament_id=self.tour.id, status="scheduled")
                    .all())
        for i, fixture in enumerate(fixtures):
            ts.record_manual_result(self.session, fixture.id,
                                    inn1_runs=150 + i, inn1_wickets=6,
                                    inn1_overs=20, inn2_runs=140,
                                    inn2_wickets=8, inn2_overs=20)
        self.session.commit()

    def test_a_bracket_needs_a_playoff_format(self):
        self._play_out_the_league()
        with self.assertRaises(self.lpt.LPTError) as ctx:
            self.lpt.generate_knockout(self.session, self.tour.id)
        self.assertIn("no playoff format", str(ctx.exception))

    def test_an_unknown_playoff_format_is_refused(self):
        with self.assertRaises(self.lpt.LPTError):
            self.lpt.generate_knockout(self.session, self.tour.id, "world-cup")

    def test_a_bracket_is_refused_while_the_league_is_unfinished(self):
        with self.assertRaises(self.lpt.LPTError) as ctx:
            self.lpt.generate_knockout(self.session, self.tour.id, "top4")
        self.assertIn("isn't finished", str(ctx.exception))

    def test_a_finished_league_seeds_the_bracket(self):
        self._play_out_the_league()
        created = self.lpt.generate_knockout(self.session, self.tour.id, "top4")
        self.session.commit()
        # Top-4: two semi-finals and a final.
        self.assertEqual(created, 3)
        self.assertTrue(self.tour.knockout_generated)

    def test_the_top_seeds_are_the_ones_in_the_semi_finals(self):
        from models import TournamentMatch
        from services import tournament_service as ts
        self._play_out_the_league()
        self.lpt.generate_knockout(self.session, self.tour.id, "top4")
        self.session.commit()
        top4 = {tt.id for tt in ts.points_table(self.session, self.tour.id)[:4]}
        semis = (self.session.query(TournamentMatch)
                 .filter_by(tournament_id=self.tour.id, stage="semifinal").all())
        seeded = {s.team1_id for s in semis} | {s.team2_id for s in semis}
        self.assertEqual(seeded, top4)


# ══════════════════════════════════════════════════════════════════════
# Rendering — these go out to real chats, so they must never crash or leak
# ══════════════════════════════════════════════════════════════════════

class RenderTests(LPTCase):
    def test_every_view_renders_for_an_empty_tournament(self):
        for text in (self.lpt.render_overview(self.session, self.tour),
                     self.lpt.render_table(self.session, self.tour),
                     self.lpt.render_fixtures(self.session, self.tour),
                     self.lpt.render_teams(self.session, self.tour)):
            self.assertIn("Test Cup", text)

    def test_the_fixtures_view_says_free_play_when_nothing_is_scheduled(self):
        self.enter_all()
        self.assertIn("free-play",
                      self.lpt.render_fixtures(self.session, self.tour))

    def test_a_players_own_fixtures_are_pulled_out_for_them(self):
        self.enter_all()
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        text = self.lpt.render_fixtures(self.session, self.tour,
                                        viewer_tg_id=self.users[0].telegram_id)
        self.assertIn("Your next matches", text)

    def test_a_watcher_who_is_not_playing_gets_no_personal_section(self):
        self.enter_all()
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        text = self.lpt.render_fixtures(self.session, self.tour,
                                        viewer_tg_id=9_999_999)
        self.assertNotIn("Your next matches", text)

    def test_the_teams_view_never_pings_anybody(self):
        self.enter_all()
        text = self.lpt.render_teams(self.session, self.tour)
        # A squad list that mentioned everyone in it would notify the whole field
        # every time somebody typed /lptteams.
        self.assertNotIn("@", text)
        self.assertNotIn("tg://", text)

    def test_a_team_name_with_markup_in_it_is_escaped(self):
        self.lpt.add_participant(self.session, self.tour.id,
                                 self.users[0].telegram_id,
                                 name="<b>Bold & Co</b>")
        self.session.commit()
        text = self.lpt.render_teams(self.session, self.tour)
        self.assertIn("&lt;b&gt;Bold &amp; Co", text)
        self.assertNotIn("<b>Bold", text)

    def test_the_champion_is_named_once_the_final_is_won(self):
        from models import TournamentMatch
        from services import tournament_service as ts
        self.enter_all()
        winner = self.lpt.team_for_tg(self.session, self.tour.id,
                                      self.users[0].telegram_id)
        self.session.add(TournamentMatch(
            tournament_id=self.tour.id, stage="final", status="completed",
            team1_id=winner.id, winner_team_id=winner.id, match_no=99))
        self.session.commit()
        self.assertIn("Champion",
                      self.lpt.render_overview(self.session, self.tour))


# ══════════════════════════════════════════════════════════════════════
# /lptour — the command players actually type
# ══════════════════════════════════════════════════════════════════════

class _Message:
    def __init__(self, text="/lptour", reply_user=None):
        self.text = text
        self.replies = []
        self.reply_to_message = (SimpleNamespace(from_user=reply_user)
                                 if reply_user is not None else None)
        self.entities = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(message_id=len(self.replies))


def _update(sender_tg, reply_user=None):
    msg = _Message(reply_user=reply_user)
    return SimpleNamespace(
        effective_message=msg, message=msg,
        effective_user=SimpleNamespace(id=sender_tg, is_bot=False,
                                       username=None, first_name="Host"),
        effective_chat=SimpleNamespace(id=-100_1),
    )


class LptourCommandTests(LPTCase):
    """The gate in front of /lptour, and what it hands to /letsplay."""

    def setUp(self):
        super().setUp()
        self.enter_all()
        self.go_live()
        import handlers.lp_tournament as module
        self.module = module
        # Capture the hand-off instead of running a real match: everything past
        # this point is the /letsplay flow, which has its own tests.
        self.handed = []
        import handlers.letsplay as letsplay
        self._real_letsplay = letsplay.letsplay_handler

        async def _spy(update, context, tour_ctx=None):
            self.handed.append(tour_ctx)
        letsplay.letsplay_handler = _spy

    def tearDown(self):
        import handlers.letsplay as letsplay
        letsplay.letsplay_handler = self._real_letsplay
        super().tearDown()

    def _run(self, sender, target_tg):
        update = _update(sender.telegram_id,
                         reply_user=SimpleNamespace(id=target_tg, is_bot=False))
        context = SimpleNamespace(args=[], bot_data={})
        asyncio.run(self.module.lptour_handler(update, context))
        return update.effective_message.replies

    def test_a_pair_of_entrants_is_handed_to_letsplay_with_the_tournament(self):
        replies = self._run(self.users[0], self.users[1].telegram_id)
        self.assertEqual(replies, [])
        self.assertEqual(len(self.handed), 1)
        ctx = self.handed[0]
        self.assertEqual(ctx["tournament_id"], self.tour.id)
        self.assertEqual(ctx["tournament_name"], "Test Cup")
        self.assertEqual(ctx["guest_user_id"], self.users[1].id)
        self.assertEqual(ctx["host_team_name"], "lpuser0")
        self.assertEqual(ctx["guest_team_name"], "lpuser1")

    def test_a_non_entrant_is_told_so_and_no_match_starts(self):
        from models import User
        outsider = User(telegram_id=next(_TG), username="outsider",
                        roster_count=0)
        self.session.add(outsider)
        self.session.commit()
        replies = self._run(self.users[0], outsider.telegram_id)
        self.assertEqual(self.handed, [])
        self.assertIn("entered in", replies[0])
        self.assertIn("/lptadd", replies[0])

    def test_a_refusal_reaches_the_chat_html_escaped(self):
        # The other half of the plain-text-error contract: the service hands over
        # raw names, and the handler escapes them exactly once before Telegram
        # parses the reply as HTML.
        self.tour.name = "Bob's <Cup>"
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        self.record(self.users[0], self.users[1])
        replies = self._run(self.users[0], self.users[1].telegram_id)
        self.assertEqual(self.handed, [])
        self.assertIn("&lt;Cup&gt;", replies[0])
        self.assertNotIn("<Cup>", replies[0])

    def test_a_paused_tournament_starts_nothing(self):
        from services import tournament_service as ts
        ts.set_status(self.session, self.tour.id, "paused")
        self.session.commit()
        replies = self._run(self.users[0], self.users[1].telegram_id)
        self.assertEqual(self.handed, [])
        self.assertIn("paused", replies[0])

    def test_with_no_target_it_explains_how_to_use_it(self):
        update = _update(self.users[0].telegram_id)
        context = SimpleNamespace(args=[], bot_data={})
        asyncio.run(self.module.lptour_handler(update, context))
        self.assertEqual(self.handed, [])
        self.assertIn("/lptour", update.effective_message.replies[0])

    def test_with_no_active_tournament_it_says_so(self):
        from services import tournament_service as ts
        ts.deactivate_tournament(self.session, self.tour.id)
        self.session.commit()
        replies = self._run(self.users[0], self.users[1].telegram_id)
        self.assertEqual(self.handed, [])
        self.assertIn("No Lets Play Tournament", replies[0])

    def test_a_consumed_fixture_cannot_be_replayed(self):
        self.lpt.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        self.record(self.users[0], self.users[1])
        replies = self._run(self.users[0], self.users[1].telegram_id)
        self.assertEqual(self.handed, [])
        self.assertIn("no fixture left", replies[0])


if __name__ == "__main__":
    unittest.main()
