"""Fixture winner identity survives captain changes, draft saves and team edits."""

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class TournamentWinnerSettingsTests(F.FourTeamCase):
    def test_challenge_draft_targets_the_playing_coowner_after_json_roundtrip(self):
        from models import TournamentTeam, User
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        selected = self.session.get(TournamentTeam, fixture.team2_id)
        opponent = self.session.get(TournamentTeam, fixture.team1_id)
        coowner = User(telegram_id=F.next_tg(), username='playing_captain')
        self.session.add(coowner)
        self.session.flush()
        selected.co_owner_ids_json = json.dumps([coowner.telegram_id])
        self.session.commit()
        settings.set_winner(self.session, self.tour, fixture.id, selected.id)
        draft = json.loads(json.dumps({
            'tournament_team_by_user': {
                coowner.id: selected.challenge_team_id,
                self.owners[opponent.name].id: opponent.challenge_team_id,
            },
        }))

        settings.apply_to_draft(self.session, fixture, draft)

        self.assertEqual(draft['_planned_winner_user_id'], coowner.id)
        self.assertNotEqual(draft['_planned_winner_user_id'],
                            self.owners[selected.name].id)
        self.assertIsInstance(draft['_planned_winner_user_id'], int)
        self.assertEqual(fixture.planned_winner_team_id, selected.id)

    def test_letsplay_draft_maps_tournament_team_ids_to_users(self):
        from models import Tournament, TournamentMatch, TournamentTeam
        from services import tournament_match_settings as settings

        tour = Tournament(name='Roster Cup', kind='letsplay', status='active', overs=20)
        self.session.add(tour)
        self.session.flush()
        teams = [TournamentTeam(tournament_id=tour.id, name=name,
                                user_tg_id=self.owners[name].telegram_id)
                 for name in ('Alpha', 'Bravo')]
        self.session.add_all(teams)
        self.session.flush()
        fixture = TournamentMatch(tournament_id=tour.id, status='scheduled',
                                  team1_id=teams[0].id, team2_id=teams[1].id)
        self.session.add(fixture)
        self.session.commit()
        settings.set_winner(self.session, tour, fixture.id, teams[0].id)
        draft = json.loads(json.dumps({
            'lpt_team_ids': {self.owners[team.name].id: team.id for team in teams},
            # A stale CL mapping must not override Lets Play's team identities.
            'tournament_team_by_user': {self.owners['Bravo'].id: teams[0].id},
        }))

        settings.apply_to_draft(self.session, fixture, draft)

        self.assertEqual(draft['_planned_winner_user_id'], self.owners['Alpha'].id)
        self.assertIsInstance(draft['_planned_winner_user_id'], int)
        self.assertIsNone(teams[0].challenge_team_id)

    def test_normal_setting_removes_a_stale_draft_winner(self):
        from models import TournamentTeam
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        selected = self.session.get(TournamentTeam, fixture.team1_id)
        settings.set_winner(self.session, self.tour, fixture.id, selected.id)
        draft = {'tournament_team_by_user': {
            self.owners[selected.name].id: selected.challenge_team_id,
        }}
        settings.apply_to_draft(self.session, fixture, draft)
        self.assertIsNotNone(draft['_planned_winner_user_id'])

        settings.set_winner(self.session, self.tour, fixture.id, None)
        settings.apply_to_draft(self.session, fixture, draft)

        self.assertIsNone(draft['_planned_winner_user_id'])

    def test_draft_refuses_a_selected_team_no_longer_in_the_fixture(self):
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        outsider = next(team for team in self.teams.values()
                        if team.id not in (fixture.team1_id, fixture.team2_id))
        # Mimic an old bracket/fixture edit that left a stale stored selection.
        fixture.planned_winner_team_id = outsider.id
        self.session.flush()
        draft = {'_planned_winner_user_id': 999,
                 'tournament_team_by_user': {
                     self.owners[outsider.name].id: outsider.challenge_team_id,
                 }}

        with self.assertRaisesRegex(ValueError, 'no longer in this fixture'):
            settings.apply_to_draft(self.session, fixture, draft)
        self.assertIsNone(draft['_planned_winner_user_id'])

    def test_draft_refuses_a_selection_without_a_matching_playing_captain(self):
        from models import TournamentTeam
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        selected = self.session.get(TournamentTeam, fixture.team1_id)
        opponent = self.session.get(TournamentTeam, fixture.team2_id)
        settings.set_winner(self.session, self.tour, fixture.id, selected.id)
        draft = {'tournament_team_by_user': {
            self.owners[opponent.name].id: opponent.challenge_team_id,
        }}

        with self.assertRaisesRegex(ValueError, 'could not be matched to a captain'):
            settings.apply_to_draft(self.session, fixture, draft)
        self.assertIsNone(draft['_planned_winner_user_id'])

    def test_swapping_out_the_selected_team_clears_the_setting(self):
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        original = (fixture.team1_id, fixture.team2_id)
        replacement = next(team.id for team in self.teams.values()
                           if team.id not in original)
        for slot, new_team_id in ((1, replacement), (2, replacement), (1, None)):
            with self.subTest(slot=slot, new_team_id=new_team_id):
                fixture.team1_id, fixture.team2_id = original
                self.session.commit()
                settings.set_winner(self.session, self.tour, fixture.id, original[slot - 1])

                self.lss.swap_fixture_team(self.session, fixture.id, slot, new_team_id)
                self.session.commit()
                self.session.refresh(fixture)

                self.assertIsNone(fixture.planned_winner_team_id)

    def test_swapping_the_other_team_preserves_the_selected_winner(self):
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        selected_id = fixture.team1_id
        replacement = next(team.id for team in self.teams.values()
                           if team.id not in (fixture.team1_id, fixture.team2_id))
        settings.set_winner(self.session, self.tour, fixture.id, selected_id)

        self.lss.swap_fixture_team(self.session, fixture.id, 2, replacement)
        self.session.commit()
        self.session.refresh(fixture)

        self.assertEqual(fixture.planned_winner_team_id, selected_id)
        self.assertEqual(fixture.team2_id, replacement)

    def _assert_concurrent_fixture_change_rejects_winner(self, change):
        from database import get_session
        from models import TournamentMatch, TournamentTeam
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        fixture_id, first_id, second_id = fixture.id, fixture.team1_id, fixture.team2_id
        replacement_id = next(team.id for team in self.teams.values()
                              if team.id not in (first_id, second_id))
        settings.set_winner(self.session, self.tour, fixture_id, second_id)
        self.session.commit()
        real_get = self.session.get
        observed = {}

        def get_with_concurrent_change(model, ident, *args, **kwargs):
            value = real_get(model, ident, *args, **kwargs)
            if model is TournamentTeam and not observed:
                # set_winner has already read and validated the scheduled row.
                # SQLite ignores FOR UPDATE, so another connection can start
                # the match or replace a team before the winner UPDATE occurs.
                other = get_session()
                try:
                    concurrent = other.get(TournamentMatch, fixture_id)
                    if change == 'kickoff':
                        concurrent.status = 'live'
                    else:
                        self.lss.swap_fixture_team(
                            other, fixture_id, change, replacement_id)
                    other.commit()
                    observed.update(status=concurrent.status,
                                    team1_id=concurrent.team1_id,
                                    team2_id=concurrent.team2_id,
                                    winner=concurrent.planned_winner_team_id)
                finally:
                    other.close()
            return value

        with mock.patch.object(self.session, 'get', side_effect=get_with_concurrent_change):
            with self.assertRaisesRegex(ValueError, 'fixture changed'):
                settings.set_winner(self.session, self.tour, fixture_id, first_id)
        self.assertTrue(observed, 'the competing transaction ran after validation')
        self.session.rollback()
        self.session.refresh(fixture)
        self.assertEqual(fixture.status, observed['status'])
        self.assertEqual(fixture.team1_id, observed['team1_id'])
        self.assertEqual(fixture.team2_id, observed['team2_id'])
        self.assertEqual(fixture.planned_winner_team_id, observed['winner'])

    def test_sqlite_rejects_winner_if_match_starts_after_status_validation(self):
        self._assert_concurrent_fixture_change_rejects_winner('kickoff')

    def test_sqlite_rejects_winner_if_selected_team_is_swapped_after_validation(self):
        self._assert_concurrent_fixture_change_rejects_winner(1)

    def test_sqlite_rejects_winner_if_opponent_is_swapped_after_validation(self):
        self._assert_concurrent_fixture_change_rejects_winner(2)

    def test_swap_clears_a_selection_saved_after_its_session_loaded_the_fixture(self):
        from database import get_session
        from models import TournamentMatch
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        fixture_id, selected_id = fixture.id, fixture.team1_id
        replacement_id = next(team.id for team in self.teams.values()
                              if team.id not in (fixture.team1_id, fixture.team2_id))
        other = get_session()
        try:
            stale = other.get(TournamentMatch, fixture_id)
            self.assertIsNone(stale.planned_winner_team_id)
            settings.set_winner(self.session, self.tour, fixture_id, selected_id)
            self.session.commit()
            # Keep the other session's stale identity-map entry alive.
            self.assertIsNone(stale.planned_winner_team_id)

            self.lss.swap_fixture_team(other, fixture_id, 1, replacement_id)
            other.commit()
            self.session.refresh(fixture)

            self.assertEqual(fixture.team1_id, replacement_id)
            self.assertIsNone(fixture.planned_winner_team_id)
        finally:
            other.close()

    def test_sqlite_swap_clears_a_winner_saved_after_the_swap_read(self):
        from database import get_session
        from models import Tournament
        from services import tournament_match_settings as settings

        fixture = self.fixtures(1)[0]
        fixture_id, selected_id = fixture.id, fixture.team1_id
        tour_id = self.tour.id
        replacement_id = next(team.id for team in self.teams.values()
                              if team.id not in (fixture.team1_id, fixture.team2_id))
        real_require_team = self.lss._require_team
        concurrent_ran = []

        def require_team_after_concurrent_selection(session, tid, team_id):
            value = real_require_team(session, tid, team_id)
            other = get_session()
            try:
                settings.set_winner(other, other.get(Tournament, tour_id),
                                    fixture_id, selected_id)
                other.commit()
                concurrent_ran.append(True)
            finally:
                other.close()
            return value

        with mock.patch.object(self.lss, '_require_team',
                               side_effect=require_team_after_concurrent_selection):
            self.lss.swap_fixture_team(self.session, fixture_id, 1, replacement_id)
        self.session.commit()
        self.session.refresh(fixture)

        self.assertTrue(concurrent_ran)
        self.assertEqual(fixture.team1_id, replacement_id)
        self.assertIsNone(fixture.planned_winner_team_id)
