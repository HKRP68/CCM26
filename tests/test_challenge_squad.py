"""League roster commands, printed ratings, owner lookup and complete replies."""
import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap
import _tour_fixture as F

_SAVED = None


def setUpModule():
    global _SAVED
    _SAVED = _module_swap.save(('handlers.challenge_squad',))
    _module_swap.unload(('handlers.challenge_squad',))
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()
    _module_swap.restore(_SAVED)


class SquadTests(F.FourTeamCase):
    def setUp(self):
        # Previous fixtures stay in the module database; hide their leagues so
        # browsing tests exercise just this test's public field.
        from database import get_session
        from models import ChallengeMode
        with get_session() as session:
            session.query(ChallengeMode).update({'is_active': False})
            session.commit()
        super().setUp()
        from handlers import challenge_squad as H
        from services import challenge_squad as S
        from models import ChallengeTeam, ChallengeLeague
        self.H, self.S = H, S
        self.tour.is_active = True
        self.league = self.session.get(ChallengeLeague, self.tour.league_id)
        self.alpha = self.session.get(ChallengeTeam, self.teams['Alpha'].challenge_team_id)
        self.session.commit()

    def player(self, name, details=None, source=None, team=None):
        from models import ChallengePlayer
        row = ChallengePlayer(team_id=(team or self.alpha).id, name=name,
                              details_json=json.dumps(details) if details is not None else None,
                              source_player_id=source.id if source is not None else None)
        self.session.add(row)
        self.session.commit()
        return row

    def message(self):
        return SimpleNamespace(reply_text=AsyncMock(), chat_id=-1001, message_id=1)

    def command(self, query='', viewer=None):
        msg = self.message()
        update = SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(
            id=self.owners['Alpha'].telegram_id if viewer is None else viewer))
        asyncio.run(self.H.clsquad_handler(update, SimpleNamespace(args=query.split())))
        text = '\n'.join(call.args[0] for call in msg.reply_text.call_args_list)
        return msg, text

    def callback(self, data, viewer=None):
        msg = self.message()
        q = SimpleNamespace(data=data, message=msg, answer=AsyncMock(), edit_message_text=AsyncMock(),
                            from_user=SimpleNamespace(id=viewer or self.owners['Alpha'].telegram_id))
        asyncio.run(self.H.clsquad_callback(SimpleNamespace(callback_query=q), None))
        return q, '\n'.join(call.args[0] for call in msg.reply_text.call_args_list)

    def test_all_requested_columns_and_average_match_both_renderings(self):
        self.player('Batter', {'category': 'Batsman', 'rating': 90, 'bat_rating': 96, 'bowl_rating': 40})
        self.player('Bowler', {'category': 'Bowler', 'rating': 86, 'bat_rating': 60, 'bowl_rating': 94})
        blocks, text = self.S.squad_view(self.session, self.alpha)
        self.assertIn('Batter · Batsman · OVR 90 · BAT 96 · BOWL 40', text)
        expected = 'Squad rating: 88.0 OVR · BAT 78.0 · BOWL 67.0'
        self.assertIn(expected, text)
        self.assertIn(expected, str(blocks))
        for column in ('Player', 'Category', 'Overall', 'Bat', 'Bowl'):
            self.assertIn(column, str(blocks))

    def test_snapshot_overrides_master_card_and_missing_fields_fall_back(self):
        from models import Player
        master = Player(name='Master', rating=99, category='Bowler', country='India',
                        bat_hand='Right', bowl_hand='Right', bowl_style='Fast', bat_rating=77, bowl_rating=98)
        self.session.add(master)
        self.session.commit()
        row = self.player('League edition', {'Overall': '88', 'BAT': '92', 'Category': 'Batsman'}, source=master)
        parsed = self.S.player_row(row)
        self.assertEqual((parsed.rating, parsed.bat_rating, parsed.bowl_rating), (88, 92, 98))
        self.assertEqual(parsed.category, 'Batsman')

    def test_invalid_or_missing_ratings_are_unknown_and_do_not_lower_average(self):
        from services import squad_ratings
        self.player('Known', {'rating': 90, 'bat_rating': 0, 'bowl_rating': 80})
        unknown = self.player('Unknown', {'rating': 'nan', 'bat_rating': 'inf', 'bowl_rating': '<bad>'})
        unknown.details_json = '["not a player object"]'
        self.session.commit()
        _, text = self.S.squad_view(self.session, self.alpha)
        self.assertIn('Unknown · Unknown · OVR — · BAT — · BOWL —', text)
        self.assertIn('Squad rating: 90.0 OVR · BAT 0.0 · BOWL 80.0', text)
        for bad in (None, True, 'nan', 'inf', -1, 'oops'):
            self.assertIsNone(squad_ratings.number(bad))

    def test_owner_and_co_owner_get_their_tournament_roster_without_a_name(self):
        self.player('Owned player', {'category': 'All-rounder', 'rating': 91, 'bat_rating': 92, 'bowl_rating': 89})
        self.teams['Alpha'].name = 'Renamed tournament franchise'
        self.teams['Alpha'].co_owner_ids_json = '[987654321]'
        self.session.commit()
        for viewer in (self.owners['Alpha'].telegram_id, 987654321):
            _, text = self.command(viewer=viewer)
            self.assertIn('Renamed tournament franchise', text)
            self.assertIn(self.tour.name, text)
            self.assertIn('Owned player', text)

    def test_multiple_owned_teams_offer_a_picker(self):
        self.teams['Bravo'].co_owner_ids_json = json.dumps([self.owners['Alpha'].telegram_id])
        self.session.commit()
        msg, text = self.command()
        self.assertIn('Your tournament teams', text)
        buttons = msg.reply_text.call_args.kwargs['reply_markup'].inline_keyboard
        targets = {button.callback_data for row in buttons for button in row}
        self.assertEqual(targets, {f'clsq_owned_{self.teams[name].id}' for name in ('Alpha', 'Bravo')})

    def test_owner_can_view_a_team_before_the_tournament_is_activated(self):
        self.tour.is_active = False
        self.tour.status = 'draft'
        self.session.commit()
        self.player('Upcoming player', {'rating': 88})
        _, text = self.command()
        self.assertIn('Upcoming player', text)

    def test_unlinked_legacy_team_resolves_by_name_only_inside_its_league(self):
        self.teams['Alpha'].challenge_team_id = None
        self.session.commit()
        self.player('Legacy roster player', {'rating': 88})
        _, text = self.command()
        self.assertIn('Legacy roster player', text)

    def test_lets_play_team_is_not_treated_as_a_challenge_league_squad(self):
        self.tour.kind = 'letsplay'
        self.session.commit()
        _, text = self.command()
        self.assertIn('choose a league', text)
        self.assertNotIn('No Challenge League roster is linked', text)

    def test_non_owner_browses_and_named_search_is_public(self):
        self.player('Public player', {'rating': 84})
        _, text = self.command(viewer=-900)
        self.assertIn('choose a league', text)
        for query in (f'#{self.league.id} | Alpha', f'{self.league.name} | Alpha', 'Alpha'):
            _, text = self.command(query, viewer=-900)
            self.assertIn('Public player', text)

    def test_duplicate_names_across_leagues_require_a_selection(self):
        from models import ChallengeLeague, ChallengeTeam
        other = ChallengeLeague(mode_id=self.league.mode_id, name='Other league')
        self.session.add(other)
        self.session.flush()
        other_team = ChallengeTeam(league_id=other.id, name='Alpha')
        self.session.add(other_team)
        self.session.commit()
        _, text = self.command('Alpha', viewer=-900)
        self.assertIn('Which', text)
        _, text = self.command(f'#{other.id} | Alpha', viewer=-900)
        self.assertIn('No players are configured', text)
        self.assertIn('Squad rating: — OVR', text)

    def test_html_escapes_player_and_category_names(self):
        self.player('<Name & edition>', {'category': '<Custom>', 'rating': 91})
        _, text = self.command('Alpha', viewer=-900)
        self.assertIn('&lt;Name &amp; edition&gt;', text)
        self.assertIn('&lt;Custom&gt;', text)
        self.assertNotIn('<Custom>', text)

    def test_long_squads_are_complete_for_commands_and_picker_callbacks(self):
        names = [f'Player {i:02d} ' + 'Long name ' * 12 for i in range(45)]
        for name in names:
            self.player(name, {'category': 'Batsman', 'rating': 85, 'bat_rating': 90, 'bowl_rating': 40})
        msg, text = self.command('Alpha', viewer=-900)
        self.assertGreater(msg.reply_text.await_count, 1)
        self.assertTrue(all(len(call.args[0]) <= 4096 for call in msg.reply_text.call_args_list))
        q, picked = self.callback(f'clsq_team_{self.alpha.id}')
        self.assertGreater(q.message.reply_text.await_count, 1)
        for name in names:
            self.assertEqual(text.count(name), 1)
            self.assertEqual(picked.count(name), 1)
        q.edit_message_text.assert_not_awaited()

    def test_callbacks_validate_ids_and_do_not_show_inactive_teams(self):
        for data in ('clsq_team_bad', 'clsq_team_999999999', 'clsq_other_2', 'other_team_1'):
            q, text = self.callback(data)
            self.assertTrue(q.answer.call_args.kwargs.get('show_alert'))
            self.assertEqual(text, '')
        self.alpha.is_active = False
        self.session.commit()
        q, _ = self.callback(f'clsq_team_{self.alpha.id}')
        self.assertTrue(q.answer.call_args.kwargs.get('show_alert'))

    def test_tournament_mapping_does_not_cross_leagues(self):
        from models import ChallengeLeague, ChallengeTeam
        other = ChallengeLeague(mode_id=self.league.mode_id, name='Wrong league')
        self.session.add(other)
        self.session.flush()
        wrong = ChallengeTeam(league_id=other.id, name='Wrong team')
        self.session.add(wrong)
        self.session.flush()
        self.teams['Alpha'].challenge_team_id = wrong.id
        self.session.commit()
        _, text = self.command()
        self.assertIn('No Challenge League roster is linked', text)

    def test_team_picker_paginates_and_owned_selection_shows_roster(self):
        from models import ChallengeTeam
        for i in range(22):
            self.session.add(ChallengeTeam(league_id=self.league.id, name=f'Extra team {i}', sort_order=10+i))
        self.session.commit()
        q, _ = self.callback(f'clsq_league_{self.league.id}')
        markup = q.edit_message_text.call_args.kwargs['reply_markup']
        self.assertIn(f'clsq_league_{self.league.id}_1', [b.callback_data for row in markup.inline_keyboard for b in row])
        q, _ = self.callback(f'clsq_league_{self.league.id}_1')
        self.assertIn('Page 2', q.edit_message_text.call_args.args[0])
        self.player('Selected owner player', {'rating': 83})
        _, text = self.callback(f'clsq_owned_{self.teams["Alpha"].id}')
        self.assertIn('Selected owner player', text)


if __name__ == '__main__':
    unittest.main()
