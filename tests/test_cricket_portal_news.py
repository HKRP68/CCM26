"""Transfer news publishes only completed, competition-linked public facts."""
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_league_seasons_page as pages
setUpModule = pages.setUpModule
tearDownModule = pages.tearDownModule


class PortalTransferNewsTests(pages.PageCase):
    def setUp(self):
        super().setUp()
        from models import AuctionSeason, AuctionFranchise, AuctionLot
        self.auction = AuctionSeason(name=f'Transfer season {self.tag}')
        self.other = AuctionSeason(name=f'Other transfer season {self.tag}')
        self.session.add_all([self.auction, self.other])
        self.session.flush()
        self.a = AuctionFranchise(season_id=self.auction.id, name='Mumbai')
        self.b = AuctionFranchise(season_id=self.auction.id, name='Chennai')
        self.lot = AuctionLot(season_id=self.auction.id, name='Alpha')
        self.other_lot = AuctionLot(season_id=self.other.id, name='Unrelated Player')
        self.session.add_all([self.a, self.b, self.lot, self.other_lot])
        self.session.flush()
        self.seasons = [SimpleNamespace(auction_season_id=self.auction.id)]

    def trade(self, **changes):
        from models import AuctionTrade
        values = dict(season_id=self.auction.id, team_a_id=self.a.id, team_b_id=self.b.id,
            status='completed', lots_a_json=json.dumps([self.lot.id]), lots_b_json='[]',
            completed_at=datetime(2026, 10, 31, 15, 30), note='PRIVATE_NOTE',
            approved_by_tg_id=91919191, chat_id=-100123456, cash_lakh=987654)
        values.update(changes)
        row = AuctionTrade(**values)
        self.session.add(row)
        self.session.flush()
        return row

    def test_completed_linked_transfer_has_player_direction_and_no_private_fields(self):
        from services.cricket_portal_news import transfer_stories
        self.trade()
        stories = transfer_stories(self.session, self.seasons)
        self.assertEqual(1, len(stories))
        self.assertEqual('transfer', stories[0]['kind'])
        self.assertIn('Alpha: Mumbai → Chennai.', stories[0]['body'])
        self.assertEqual('2026-10-31T15:30:00Z', stories[0]['date'])
        text = json.dumps(stories)
        for hidden in ('PRIVATE_NOTE', '91919191', '-100123456', '987654', 'approved_by', 'chat_id', 'snapshot_json'):
            self.assertNotIn(hidden, text)

    def test_pending_reversed_cancelled_and_unrelated_season_do_not_publish(self):
        from services.cricket_portal_news import transfer_stories
        for status in ('offered', 'pending_admin', 'reversed', 'cancelled'):
            self.trade(status=status)
        self.trade(season_id=self.other.id)
        self.assertEqual([], transfer_stories(self.session, self.seasons))
        self.assertEqual([], transfer_stories(self.session, [SimpleNamespace(auction_season_id=None)]))

    def test_malformed_lots_and_cross_season_ids_are_safe_and_do_not_invent_players_or_dates(self):
        from services.cricket_portal_news import transfer_stories
        self.trade(lots_a_json=json.dumps([True, {}, 'bad', self.other_lot.id]),
            lots_b_json='not JSON', completed_at=None, snapshot_json='[]')
        stories = transfer_stories(self.session, self.seasons)
        self.assertEqual(1, len(stories))
        self.assertNotIn('Alpha', stories[0]['body'])
        self.assertNotIn('Unrelated Player', stories[0]['body'])
        self.assertIsNone(stories[0]['date'])

    def test_execution_snapshot_preserves_the_recorded_player_name(self):
        from services.cricket_portal_news import transfer_stories
        self.trade(snapshot_json=json.dumps({'a': [{'name': 'Original Alpha', 'price': 991122}],
            'b': [], 'private_note': 'PRIVATE_NOTE'}))
        self.lot.name = 'Renamed catalogue entry'
        self.session.flush()
        story = transfer_stories(self.session, self.seasons)[0]
        self.assertIn('Original Alpha:', story['body'])
        self.assertNotIn('Renamed catalogue', story['body'])
        self.assertNotIn('991122', json.dumps(story))
