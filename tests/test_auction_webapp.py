"""Real signed Mini App requests against an isolated shared auction database."""

import hashlib
import hmac
import itertools
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent))
import _module_swap

_MODULE_NAMES = ('database', 'models', 'config', 'services.player_service',
                 'services.player_query', 'services.auction_service',
                 'services.auction_preseason', 'services.auction_trade_service',
                 'services.retention_negotiation', 'services.auction_webapp',
                 'services.auction_webapp_routes', 'services.auction_sets_io',
                 'services.auction_auto_sets', 'services.auction_simulator')
_SAVED = {}
_ENV = {}
_FILE = None
_ENGINE = None
_IDS = itertools.count(1)
ALICE, BOB, COOWNER, OUTSIDER, ADMIN = 801111, 802222, 803333, 804444, 809999
TOKEN = 'auction-test-token'


def setUpModule():
    global _SAVED, _ENV, _FILE, _ENGINE
    _SAVED = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
    _ENV = {k: os.environ.get(k) for k in ('DATABASE_URL', 'BOT_TOKEN')}
    _FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    _FILE.close()
    os.environ['DATABASE_URL'] = 'sqlite:///' + _FILE.name
    os.environ['BOT_TOKEN'] = TOKEN
    from database import Base, engine
    import models
    _ENGINE = engine
    Base.metadata.create_all(engine)


def tearDownModule():
    _ENGINE.dispose()
    for key, value in _ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _module_swap.restore(_SAVED)
    Path(_FILE.name).unlink(missing_ok=True)


def signed(tg_id, **extra):
    fields = dict(user=json.dumps(dict(id=tg_id, first_name='Tester')),
                  auth_date=str(int(time.time())), **extra)
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, '\n'.join(f'{k}={fields[k]}' for k in sorted(fields)).encode(), hashlib.sha256).hexdigest()
    return 'tma ' + urlencode(fields)


class AuctionWebappTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask, request
        from database import get_session
        from models import Player
        from services import auction_service as A, auction_webapp as W, auction_trade_service as T
        from services.auction_webapp_routes import register
        from services.webapp_auth import verify_init_data, get_user_id

        self.db = get_session()
        self.A, self.W, self.T = A, W, T
        self.admin_patch = patch.object(W, 'is_admin', side_effect=lambda uid: uid == ADMIN)
        self.admin_patch.start()
        self.bot_patch = patch.object(T, 'is_bot_admin', side_effect=lambda uid: uid == ADMIN)
        self.bot_patch.start()
        self.app = Flask(__name__)
        self.app.config['TESTING'] = True
        self.membership = None

        def auth(**kwargs):
            raw = request.headers.get('Authorization', '').removeprefix('tma ')
            uid = get_user_id(verify_init_data(raw, TOKEN))
            if not uid:
                return None, None, (dict(ok=False, error='bad_init_data'), 401)
            # Auction ownership does not require a collecting-game /debut.
            return (get_session(), None, uid), uid, None

        register(self.app, auth, lambda fn: fn, lambda *a, **k: self.membership)
        self.client = self.app.test_client()
        tag = next(_IDS)
        self.season = A.create_season(self.db, 'Auction '+str(tag), min_squad_size=0,
                                      max_squad_size=5, max_overseas=5, auto_accelerated=0)
        A.bind_chat(self.db, self.season, -10080000-tag)
        self.alice = A.create_franchise(self.db, self.season, 'Mumbai', owner_tg_id=ALICE)
        self.bob = A.create_franchise(self.db, self.season, 'Chennai', owner_tg_id=BOB)
        A.set_co_owners(self.db, self.alice, [COOWNER])
        self.players = []
        for i, role in enumerate(('Batsman', 'Bowler', 'Wicket Keeper', 'All-rounder')):
            p = Player(name=f'Player {tag}-{i}', rating=85+i, category=role, country='India',
                       version='Base', bat_hand='Right', bowl_hand='Right',
                       bowl_style='Medium Pacer', bat_rating=85, bowl_rating=80, is_active=True)
            self.db.add(p)
            self.players.append(p)
        self.db.flush()
        A.add_players_to_pool(self.db, self.season, self.players, set_name='Main')
        self.db.commit()
        self.sid, self.aid, self.bid = self.season.id, self.alice.id, self.bob.id

    def tearDown(self):
        self.db.close()
        self.admin_patch.stop()
        self.bot_patch.stop()

    def read(self, view='live', tg=ALICE, **body):
        return self.post('', dict(season_id=self.sid, view=view, **body), tg)

    def post(self, suffix, body, tg=ALICE):
        res = self.client.post('/api/webapp/auction'+suffix, json=body,
                               headers={'Authorization': signed(tg)})
        return res.status_code, res.get_json()

    def action(self, action, tg=ALICE, **body):
        return self.post('/action', dict(season_id=self.sid, action=action, **body), tg)

    def start(self):
        self.A.start(self.db, self.season)
        self.db.commit()
        return self.live_context()

    def live_context(self):
        code, data = self.read()
        self.assertEqual(code, 200, data)
        l = data['lot']
        return dict(lot_id=l['id'], stage=l['rtm_stage'], expected_bids=l['bids'])

    def refresh_db(self):
        self.db.rollback()
        self.db.expire_all()

    def test_owner_and_coowner_can_release_retention_only_while_paused(self):
        self.season.max_retentions = 3
        self.db.commit()
        kept = self.A.retain(self.db, self.season, self.alice, self.players[0])
        self.db.commit()
        kept_id = kept.id
        self.start()
        code, refused = self.action('release', lot_id=kept_id)
        self.assertNotEqual(200, code, refused)
        self.assertIn('/apause', refused['message'])
        self.assert_ok(self.action('admin_pause', tg=ADMIN))
        self.refresh_db()
        original_cards = (self.alice.rtm_cards_total, self.alice.rtm_cards_used)
        original_lot = self.season.current_lot_id
        code, refused = self.action('release', tg=BOB, lot_id=kept_id)
        self.assertNotEqual(200, code, refused)
        self.assert_ok(self.action('release', tg=COOWNER, lot_id=kept_id))
        self.refresh_db()
        self.assertEqual(self.A.LOT_QUEUED, kept.status)
        self.assertEqual(self.alice.purse_total_lakh, self.alice.purse_remaining_lakh)
        self.assertEqual(original_cards, (self.alice.rtm_cards_total, self.alice.rtm_cards_used))
        self.assertEqual(original_lot, self.season.current_lot_id)
        self.assertEqual(self.A.ledger_total(self.db, self.aid), self.alice.purse_remaining_lakh)
        code, refused = self.action('release', lot_id=kept_id)
        self.assertNotEqual(200, code, refused)
        self.assert_ok(self.action('admin_start', tg=ADMIN))

    def assert_ok(self, result):
        code, data = result
        self.assertEqual(code, 200, data)
        self.assertTrue(data['ok'], data)
        return data

    def test_admin_signing_and_removal_keep_squad_and_ledger_in_sync(self):
        initial = self.alice.purse_remaining_lakh
        self.assert_ok(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                   player_id=self.players[0].id, price_lakh=125, note='Correct team selection'))
        self.refresh_db()
        lot = self.A.squad(self.db, self.aid)[0]
        self.assertEqual((lot.acquisition, lot.sold_price_lakh, self.alice.squad_size), ('admin', 125, 1))
        self.assertEqual(self.alice.purse_remaining_lakh, initial - 125)
        self.assertEqual(self.A.ledger_total(self.db, self.aid), initial - 125)
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, team_id=self.bid,
                                     player_id=self.players[0].id, price_lakh=125, note='Duplicate')[0], 400)
        self.assert_ok(self.action('admin_team_remove_player', tg=ADMIN, team_id=self.aid,
                                   lot_id=lot.id, note='Reverse correction'))
        self.refresh_db()
        self.assertEqual((lot.status, lot.sold_to_id, lot.bid_count), ('queued', None, 0))
        self.assertEqual(self.alice.squad_size, 0)
        self.assertEqual(self.alice.purse_remaining_lakh, initial)
        self.assertEqual(self.A.ledger_total(self.db, self.aid), initial)
        self.assertEqual(self.action('admin_team_remove_player', tg=ADMIN, team_id=self.aid,
                                     lot_id=lot.id, note='Repeated request')[0], 409)
        self.refresh_db()
        self.assertEqual(self.A.ledger_total(self.db, self.aid), initial)

    def test_team_management_is_admin_only_and_ledgers_stay_private(self):
        for action in ('admin_team_purse', 'admin_team_add_player', 'admin_team_remove_player'):
            for tg in (ALICE, COOWNER, BOB):
                self.assertEqual(self.action(action, tg=tg, team_id=self.aid)[0], 403)
        self.A.correct_purse(self.db, self.season, self.bob, 25, note='Private admin note')
        self.db.commit()
        owner = self.assert_ok(self.read('teams', managed_team_id=self.bid))
        self.assertNotIn('team_ledger', owner)
        self.assertNotIn('Private admin note', json.dumps(owner))
        admin = self.assert_ok(self.read('teams', tg=ADMIN, managed_team_id=self.bid))
        self.assertEqual(admin['managed_team_id'], self.bid)
        self.assertIn('Private admin note', json.dumps(admin['team_ledger']))
        self.assertIn('purse_lakh', self.assert_ok(self.read('admin', tg=ADMIN))['teams'][0])

    def test_purse_modes_preview_balance_and_refuse_stale_or_invalid_changes(self):
        initial = self.alice.purse_remaining_lakh
        self.assert_ok(self.action('admin_team_purse', tg=ADMIN, team_id=self.aid, mode='add',
                                   amount_lakh=200, expected_purse_lakh=initial, note='Bonus'))
        self.assertEqual(self.action('admin_team_purse', tg=ADMIN, team_id=self.aid, mode='add',
                                     amount_lakh=200, expected_purse_lakh=initial, note='Duplicate')[0], 409)
        self.assert_ok(self.action('admin_team_purse', tg=ADMIN, team_id=self.aid, mode='deduct',
                                   amount_lakh=100, expected_purse_lakh=initial+200, note='Penalty'))
        self.assert_ok(self.action('admin_team_purse', tg=ADMIN, team_id=self.aid, mode='set',
                                   amount_lakh=750, expected_purse_lakh=initial+100, note='Set agreed balance'))
        for values in ({'amount_lakh': 751}, {'amount_lakh': -1}, {'amount_lakh': 1.5},
                       {'amount_lakh': True}, {'note': ''}, {'mode': 'unknown'}):
            args = dict(team_id=self.aid, mode='deduct', amount_lakh=25,
                        expected_purse_lakh=750, note='Invalid change')
            args.update(values)
            self.assertEqual(self.action('admin_team_purse', tg=ADMIN, **args)[0], 400, values)
        self.refresh_db()
        self.assertEqual(self.alice.purse_remaining_lakh, 750)
        self.assertEqual(self.A.ledger_total(self.db, self.aid), 750)

    def test_purse_correction_preserves_a_standing_winning_bid(self):
        current = self.start()
        lot = self.A.current_lot(self.db, self.season)
        self.assert_ok(self.action('bid', amount_lakh=lot.base_price_lakh, **current))
        self.refresh_db()
        self.assertEqual(self.action('admin_team_purse', tg=ADMIN, team_id=self.aid, mode='set',
                                     amount_lakh=0, expected_purse_lakh=self.alice.purse_remaining_lakh,
                                     note='Cannot fund bid')[0], 400)

    def test_signing_checks_rules_and_refusals_roll_back_new_lots(self):
        self.season.max_squad_size = 0
        self.db.commit()
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                     player_id=self.players[0].id, price_lakh=0, note='Full squad')[0], 400)
        self.refresh_db()
        self.assertEqual(self.alice.squad_size, 0)
        self.season.max_squad_size = 5
        self.players[0].is_career = True
        self.db.commit()
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                     player_id=self.players[0].id, price_lakh=0, note='Career card')[0], 400)
        self.refresh_db()
        self.players[0].is_career = False
        self.db.commit()
        self.assert_ok(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                   player_id=self.players[0].id, price_lakh=0, note='Free signing'))
        self.refresh_db()
        self.assertEqual(self.alice.squad_size, 1)
        self.assertEqual(self.A.ledger_total(self.db, self.aid), self.alice.purse_remaining_lakh)

    def test_live_and_published_squads_are_protected(self):
        self.start()
        args = dict(team_id=self.aid, player_id=self.players[-1].id, price_lakh=20, note='Unsafe edit')
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, **args)[0], 400)
        self.assert_ok(self.action('admin_pause', tg=ADMIN))
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, **args)[0], 400)
        self.refresh_db()
        self.season.published_at = datetime.utcnow()
        self.db.commit()
        self.assertEqual(self.action('admin_team_purse', tg=ADMIN, team_id=self.aid, mode='add',
                                     amount_lakh=10, expected_purse_lakh=self.alice.purse_remaining_lakh,
                                     note='Published')[0], 400)

    def test_team_removal_cannot_refund_another_franchises_player(self):
        self.assert_ok(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                   player_id=self.players[0].id, price_lakh=125, note='Assign player'))
        self.refresh_db()
        lot = self.A.squad(self.db, self.aid)[0]
        self.assertEqual(self.action('admin_team_remove_player', tg=ADMIN, team_id=self.bid,
                                     lot_id=lot.id, note='Wrong team')[0], 409)
        self.refresh_db()
        self.assertEqual(self.alice.squad_size, 1)
        self.assertEqual(self.A.ledger_total(self.db, self.bid), self.bob.purse_remaining_lakh)

    def test_removal_review_rejects_a_changed_signing_or_refund(self):
        self.assert_ok(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                   player_id=self.players[0].id, price_lakh=125, note='Assign'))
        self.refresh_db()
        lot = self.A.squad(self.db, self.aid)[0]
        args = dict(team_id=self.aid, lot_id=lot.id, note='Stale review')
        self.assertEqual(self.action('admin_team_remove_player', tg=ADMIN,
                                     expected_price_lakh=100, **args)[0], 409)
        self.assertEqual(self.action('admin_team_remove_player', tg=ADMIN,
                                     expected_price_lakh=125, expected_sold_at='old signing', **args)[0], 409)
        self.refresh_db()
        self.assertEqual(self.alice.squad_size, 1)
        self.assertEqual(self.A.ledger_total(self.db, self.aid), self.alice.purse_remaining_lakh)

    def test_catalogue_identifies_signed_players_and_default_signing_prices(self):
        self.assert_ok(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                   player_id=self.players[0].id, price_lakh=20, note='Assign'))
        catalogue = self.assert_ok(self.post('/catalogue', dict(season_id=self.sid,
                                                              query=self.players[0].name), ADMIN))
        player = next(p for p in catalogue['players'] if p['id'] == self.players[0].id)
        self.assertEqual(player['status'], 'sold')
        self.assertFalse(player['available'])
        self.assertGreater(player['base_lakh'], 0)

    def test_simultaneous_purse_submissions_apply_the_displayed_change_once(self):
        initial = self.alice.purse_remaining_lakh
        self.db.rollback()
        def submit(_):
            with self.app.test_client() as client:
                return client.post('/api/webapp/auction/action', headers={'Authorization': signed(ADMIN)},
                                   json=dict(season_id=self.sid, action='admin_team_purse', team_id=self.aid,
                                             mode='add', amount_lakh=25, expected_purse_lakh=initial,
                                             note='Concurrent correction')).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            codes = list(executor.map(submit, range(2)))
        self.assertEqual(codes.count(200), 1, codes)
        self.refresh_db()
        self.assertEqual(self.alice.purse_remaining_lakh, initial + 25)
        self.assertEqual(self.A.ledger_total(self.db, self.aid), initial + 25)

    def test_manual_signings_respect_role_rating_and_overseas_rules(self):
        args = dict(team_id=self.aid, player_id=self.players[0].id, price_lakh=0, note='Check squad rules')
        self.A.set_role_rules(self.db, self.season, {}, {'Batsman': 0})
        self.db.commit()
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, **args)[0], 400)
        self.refresh_db()
        self.A.set_role_rules(self.db, self.season, {'Bowler': 1}, {})
        self.season.max_squad_size = 1
        self.db.commit()
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, **args)[0], 400)
        self.refresh_db()
        self.A.set_role_rules(self.db, self.season, {}, {})
        self.A.set_rating_rules(self.db, self.season, [{'max_rating': 83, 'min_players': 1}])
        self.db.commit()
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, **args)[0], 400)
        self.refresh_db()
        self.A.set_rating_rules(self.db, self.season, [])
        self.season.max_overseas = 0
        lot = next(l for l in self.A.lots(self.db, self.sid) if l.player_id == self.players[0].id)
        lot.is_overseas = True
        self.db.commit()
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, **args)[0], 400)
        self.refresh_db()
        self.assertEqual(self.alice.squad_size, 0)

    def test_removing_retention_draft_and_rtm_signings_restores_counters(self):
        self.season.max_retentions = 2
        self.db.commit()
        retained = self.A.retain(self.db, self.season, self.alice, self.players[0], 100)
        self.db.commit()
        self.assert_ok(self.action('admin_team_remove_player', tg=ADMIN, team_id=self.aid,
                                   lot_id=retained.id, note='Release retention'))
        self.refresh_db()
        self.assertEqual(self.alice.retained_count, 0)
        for acquisition, counter in [('drafted', 'draft_picks_used'), ('rtm', 'rtm_cards_used')]:
            self.assert_ok(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                       player_id=self.players[0].id, price_lakh=100, note='Counter fixture'))
            self.refresh_db()
            lot = self.A.squad(self.db, self.aid)[0]
            lot.acquisition = acquisition
            setattr(self.alice, counter, 1)
            self.db.commit()
            self.assert_ok(self.action('admin_team_remove_player', tg=ADMIN, team_id=self.aid,
                                       lot_id=lot.id, note='Restore signing counter'))
            self.refresh_db()
            self.assertEqual(getattr(self.alice, counter), 0)
            self.assertEqual(lot.status, self.A.LOT_QUEUED)
        self.assertEqual(self.A.ledger_total(self.db, self.aid), self.alice.purse_remaining_lakh)

    def test_a_failed_signing_does_not_leave_a_new_catalogue_lot(self):
        player = self.players[-1]
        lot = next(l for l in self.A.lots(self.db, self.sid) if l.player_id == player.id)
        self.A.remove_lot(self.db, self.season, lot)
        self.season.max_squad_size = 0
        self.db.commit()
        self.assertEqual(self.action('admin_team_add_player', tg=ADMIN, team_id=self.aid,
                                     player_id=player.id, price_lakh=0, note='Rollback the entire signing')[0], 400)
        self.refresh_db()
        self.assertNotIn(player.id, [l.player_id for l in self.A.lots(self.db, self.sid)])

    def test_arena_shares_live_data_and_keeps_private_admin_data_out(self):
        ctx = self.start()
        self.assert_ok(self.action('bid', amount_lakh=20, **ctx))
        arena = self.assert_ok(self.read('arena'))
        self.assertEqual(arena['lot']['bidder_id'], self.aid)
        self.assertEqual(arena['lot']['bid_lakh'], 20)
        self.assertEqual(arena['lot']['bat_rating'], 85)
        self.assertEqual(arena['max_overseas'], 5)
        self.assertEqual(arena['teams'][0]['squad'], [])
        for key in ('team_ledger', 'settings', 'ledger', 'retention_rules', 'shortlist'):
            self.assertNotIn(key, arena)
        self.assertFalse(arena['admin'])
        self.assertFalse(arena['bot_admin'])

    def test_arena_sold_and_unsold_results_come_from_shared_events(self):
        ctx = self.start()
        self.assert_ok(self.action('admin_unsold', tg=ADMIN, **ctx))
        result = self.assert_ok(self.read('arena'))['last_result']
        self.assertEqual(result['id'], ctx['lot_id'])
        self.assertEqual(result['status'], 'unsold')
        unsold_event = result['event_id']
        self.assert_ok(self.action('admin_next', tg=ADMIN))
        ctx = self.live_context()
        self.assert_ok(self.action('bid', amount_lakh=20, **ctx))
        ctx = self.live_context()
        self.assert_ok(self.action('admin_sold', tg=ADMIN, **ctx))
        arena = self.assert_ok(self.read('arena'))
        self.assertEqual(arena['last_result']['status'], 'sold')
        self.assertEqual(arena['last_result']['price_lakh'], 20)
        self.assertEqual(arena['last_result']['sold_to_id'], self.aid)
        self.assertGreater(arena['last_result']['event_id'], unsold_event)
        buyer = next(t for t in arena['teams'] if t['id'] == self.aid)
        self.assertEqual(buyer['squad'][0]['id'], ctx['lot_id'])

    def test_delegated_auction_admin_cannot_enter_the_bot_admin_panel(self):
        self.A.add_auction_admin(self.db, ALICE)
        self.db.commit()
        arena = self.assert_ok(self.read('arena'))
        self.assertFalse(arena['bot_admin'])
        self.assertFalse(arena['admin'])
        self.assertEqual(self.read('admin', bot_admin=True, admin=True)[0], 403)
        self.assertEqual(self.post('/catalogue', dict(season_id=self.sid))[0], 403)
        self.assertEqual(self.post('/export', dict(season_id=self.sid, format='franchises'))[0], 403)
        self.assertEqual(self.action('admin_start', bot_admin=True, admin=True)[0], 403)
        self.assertEqual(self.action('admin_team_purse', team_id=self.aid, mode='add', amount_lakh=10,
                                     expected_purse_lakh=10000, note='Not a bot admin')[0], 403)
        self.assertEqual(self.post('/action', dict(action='admin_create', name='No', bot_admin=True))[0], 403)
        admin = self.assert_ok(self.read('admin', tg=ADMIN))
        self.assertTrue(admin['bot_admin'])
        self.assertTrue(self.assert_ok(self.read('arena', tg=ADMIN))['bot_admin'])

    def test_unsigned_and_tampered_requests_are_rejected(self):
        res = self.client.post('/api/webapp/auction', json={'season_id':self.sid})
        self.assertEqual(res.status_code, 401)
        bad = signed(ALICE).replace(str(ALICE), str(OUTSIDER))
        res = self.client.post('/api/webapp/auction', json={'season_id':self.sid}, headers={'Authorization':bad})
        self.assertEqual(res.status_code, 401)

    def test_json_shape_is_validated(self):
        res = self.client.post('/api/webapp/auction/action', json=['bid'], headers={'Authorization':signed(ALICE)})
        self.assertEqual(res.status_code, 400)

    def test_owners_and_coowners_see_the_same_season(self):
        for uid in (ALICE, COOWNER):
            data = self.assert_ok(self.read(tg=uid))
            self.assertEqual(data['my_team_ids'], [self.aid])
            self.assertEqual(data['season']['id'], self.sid)
        self.assertEqual(self.read(tg=OUTSIDER)[0], 403)

    def test_member_can_watch_but_not_bid(self):
        self.membership = {'ok':True, 'result':{'status':'member'}}
        data = self.assert_ok(self.read(tg=OUTSIDER))
        self.assertEqual(data['my_team_ids'], [])
        ctx = self.start()
        self.assertEqual(self.action('bid', tg=OUTSIDER, amount_lakh=100, **ctx)[0], 403)

    def test_membership_failure_does_not_grant_access(self):
        self.membership = {'ok':False, 'description':'Unavailable'}
        self.assertEqual(self.read(tg=OUTSIDER)[0], 403)

    def test_owner_cannot_use_another_teams_buttons(self):
        ctx = self.start()
        self.assertEqual(self.action('bid', team_id=self.bid, amount_lakh=100, **ctx)[0], 403)

    def test_miniapp_and_telegram_bids_share_the_lot_and_audit_log(self):
        from models import AuctionBid, AuctionEvent
        ctx = self.start()
        self.assert_ok(self.action('bid', tg=COOWNER, amount_lakh=100, **ctx))
        self.refresh_db()
        lot = self.A.current_lot(self.db, self.season)
        self.assertEqual((lot.current_bid_lakh, lot.current_bidder_id), (100,self.aid))
        self.A.place_bid(self.db,self.season,lot,self.bob,110,source='tg',by_tg_id=BOB)
        self.db.commit()
        data = self.assert_ok(self.read())
        self.assertEqual(data['lot']['bid_lakh'],110)
        rows = self.db.query(AuctionBid).filter_by(lot_id=lot.id).order_by(AuctionBid.id).all()
        self.assertEqual([b.source for b in rows],['miniapp','tg'])
        self.assertEqual(rows[0].by_tg_id,COOWNER)
        self.assertEqual(self.db.query(AuctionEvent).filter_by(season_id=self.sid,kind='bid').count(),2)

    def test_stale_buttons_never_raise_the_price_or_bid_on_the_next_player(self):
        ctx = self.start()
        self.assert_ok(self.action('bid',amount_lakh=100,**ctx))
        code, data = self.action('bid',tg=BOB,amount_lakh=200,**ctx)
        self.assertEqual((code,data['error']),(409,'stale'))
        self.refresh_db()
        lot = self.A.current_lot(self.db,self.season)
        self.A.sell_lot(self.db,self.season,lot,by_admin=True)
        self.A.open_next_lot(self.db,self.season)
        self.db.commit()
        code,_ = self.action('bid',tg=BOB,amount_lakh=200,**ctx)
        self.assertIn(code,(409,429))
        self.refresh_db()
        self.assertIsNone(self.A.current_lot(self.db,self.season).current_bid_lakh)

    def test_competing_bids_have_exactly_one_winner(self):
        from models import AuctionBid
        ctx=self.start()
        def bid(uid):
            with self.app.test_client() as c:
                res=c.post('/api/webapp/auction/action',json=dict(action='bid',season_id=self.sid,amount_lakh=100,**ctx),headers={'Authorization':signed(uid)})
                return res.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes=list(pool.map(bid,[ALICE,BOB]))
        self.assertEqual(sorted(codes),[200,409])
        self.refresh_db()
        self.assertEqual(self.db.query(AuctionBid).filter_by(lot_id=ctx['lot_id']).count(),1)

    def test_deadline_paused_and_fractional_bids_are_rejected(self):
        ctx=self.start()
        self.assertEqual(self.action('bid',amount_lakh=1.5,**ctx)[0],400)
        self.refresh_db()
        lot=self.A.current_lot(self.db,self.season)
        lot.deadline_at=datetime.utcnow()-timedelta(seconds=1)
        self.db.commit()
        self.assertEqual(self.action('bid',tg=BOB,amount_lakh=100,**ctx)[0],400)
        self.assert_ok(self.action('admin_pause',tg=ADMIN))
        self.assert_ok(self.read())

    def test_owner_cannot_start_or_reconfigure_an_auction(self):
        for name,args in [('admin_start',{}),('admin_settings',{'settings':{'max_squad_size':50}}),('admin_create',{'name':'No'})]:
            self.assertEqual(self.action(name,**args)[0],403)

    def test_pool_and_franchise_management_are_admin_only(self):
        for action, fields in (
            ('admin_pool_range',dict(low=70,high=100)),
            ('admin_pool_players',dict(player_ids=[self.players[0].id])),
            ('admin_pool_auto_sets',{}),
            ('admin_sets_import',dict(sets={'sets':[]})),
            ('admin_franchise',dict(name='Unauthorized',owner_tg_id=ALICE)),
            ('admin_franchise_import',dict(franchises={'franchises':[]})),
        ):
            code,data=self.action(action,admin=True,**fields)
            self.assertEqual(code,403,(action,data))
        self.assertEqual(self.post('/catalogue',dict(season_id=self.sid),ALICE)[0],403)
        data=self.assert_ok(self.read())
        self.assertFalse(data['admin'])
        self.assertNotIn('settings',data)

    def test_fast_miniapp_rebid_is_allowed_after_another_franchise_raises(self):
        ctx=self.start()
        self.assert_ok(self.action('bid',amount_lakh=100,**ctx))
        ctx=self.live_context()
        self.assert_ok(self.action('bid',tg=BOB,amount_lakh=110,**ctx))
        ctx=self.live_context()
        self.assert_ok(self.action('bid',amount_lakh=120,**ctx))
        self.assertEqual(self.assert_ok(self.read())['lot']['bids'],3)

    def test_duplicate_miniapp_bid_explains_its_rate_limit(self):
        ctx=self.start()
        self.assert_ok(self.action('bid',amount_lakh=100,**ctx))
        ctx=self.live_context()
        code,data=self.action('bid',amount_lakh=110,**ctx)
        self.assertEqual(code,429,data)
        self.assertIn('wait',data['message'])
        self.assertGreaterEqual(data['retry_after'],1)

    def test_all_views_serialize_and_private_shortlists_stay_private(self):
        for view in ('live','pool','teams','preseason','trades','rules'):
            self.assert_ok(self.read(view))
        self.assertEqual(self.read('admin')[0],403)
        self.assert_ok(self.read('admin',tg=ADMIN))
        self.membership={'ok':True,'result':{'status':'member'}}
        self.assertEqual(self.read('preseason',tg=OUTSIDER,team_id=self.aid)[0],403)

    def test_pool_filters_and_page_limits(self):
        data=self.assert_ok(self.read('pool',query=self.players[1].name,role='Bowler'))
        self.assertEqual([p['name'] for p in data['players']],[self.players[1].name])
        self.assertEqual(self.read('pool',page=True)[0],400)

    def test_admin_create_bind_and_add_franchise(self):
        data=self.assert_ok(self.action('admin_create',tg=ADMIN,name='New season'))
        sid=data['season_id']
        self.assert_ok(self.post('/action',dict(season_id=sid,action='admin_bind',chat_id='-1009222333'),ADMIN))
        self.assert_ok(self.post('/action',dict(season_id=sid,action='admin_franchise',name='New team',owner_tg_id=str(ALICE),co_owners=[str(COOWNER)]),ADMIN))
        result=self.assert_ok(self.post('',dict(season_id=sid),COOWNER))
        self.assertEqual(len(result['my_team_ids']),1)

    def test_settings_are_validated_and_roll_back_as_one_change(self):
        self.assert_ok(self.action('admin_settings',tg=ADMIN,settings={'opening_purse_lakh':12000,'max_squad_size':6,'direct_bids':False,'rtm_enabled':True,'rtm_per_team':2}))
        self.refresh_db()
        self.assertEqual(self.alice.purse_remaining_lakh,12000)
        self.assertFalse(self.A.direct_bids_on(self.season))
        self.assertEqual(self.alice.rtm_cards_total,2)
        self.assertEqual(self.action('admin_settings',tg=ADMIN,settings={'opening_purse_lakh':15000,'max_squad_size':0})[0],400)
        self.refresh_db()
        self.assertEqual(self.alice.purse_remaining_lakh,12000)
        self.assertEqual(self.action('admin_settings',tg=ADMIN,settings={'status':'completed'})[0],400)

    def test_admin_sale_uses_current_bid_and_updates_same_ledger(self):
        ctx=self.start()
        self.assert_ok(self.action('bid',amount_lakh=100,**ctx))
        self.assertEqual(self.action('admin_sold',tg=ADMIN,**ctx)[0],409)
        ctx=self.live_context()
        self.assert_ok(self.action('admin_sold',tg=ADMIN,**ctx))
        self.refresh_db()
        self.assertEqual(self.alice.squad_size,1)
        self.assertEqual(self.alice.purse_remaining_lakh,9900)
        self.assertEqual(self.A.ledger_total(self.db,self.aid),9900)

    def test_destructive_actions_require_exact_confirmation(self):
        self.assertEqual(self.action('admin_cancel',tg=ADMIN,confirm_name='wrong')[0],400)
        self.refresh_db()
        self.assertEqual(self.season.status,'setup')
        self.assert_ok(self.action('admin_cancel',tg=ADMIN,confirm_name=self.season.name))

    def test_exports_roundtrip_and_are_admin_only(self):
        self.assertEqual(self.post('/export',dict(season_id=self.sid),ALICE)[0],403)
        data=self.assert_ok(self.post('/export',dict(season_id=self.sid),ADMIN))
        content=json.loads(data['content'])
        self.assertEqual(len(content['sets'][0]['players']),4)
        self.assert_ok(self.action('admin_sets_import',tg=ADMIN,sets=content))
        franchise=self.assert_ok(self.post('/export',dict(season_id=self.sid,format='franchises'),ADMIN))
        self.assert_ok(self.action('admin_franchise_import',tg=ADMIN,franchises=json.loads(franchise['content'])))

    def test_classic_retention_offer_is_answered_only_by_its_team(self):
        self.season.max_retentions=2
        self.db.commit()
        self.assert_ok(self.action('admin_retention_offer',tg=ADMIN,team_id=self.aid,player_id=self.players[0].id,amount_lakh=100))
        data=self.assert_ok(self.read('preseason'))
        offer=data['offers'][0]
        self.assertEqual(self.action('retention_answer',tg=BOB,offer_id=offer['id'],accept=True)[0],403)
        self.assert_ok(self.action('retention_answer',tg=COOWNER,offer_id=offer['id'],accept=True))
        self.refresh_db()
        self.assertEqual(self.alice.retained_count,1)
        self.assertEqual(self.alice.purse_remaining_lakh,9900)

    def test_retention_confirmation_rejects_a_changed_preview(self):
        self.season.max_retentions=2
        self.db.commit()
        data=self.assert_ok(self.read('preseason'))
        token=data['retention']['token']
        self.assert_ok(self.action('retain',player_id=self.players[0].id))
        self.assertEqual(self.action('retention_confirm',token=token)[0],400)
        fresh=self.assert_ok(self.read('preseason'))
        self.assert_ok(self.action('retention_confirm',token=fresh['retention']['token']))

    def test_rtm_intent_final_raise_and_decision_share_the_same_stage(self):
        self.A.set_rtm_rules(self.db,self.season,enabled=True,per_team=1)
        ctx=self.start()
        lot=self.A.current_lot(self.db,self.season)
        lot.previous_franchise_id=self.bid
        self.A.place_bid(self.db,self.season,lot,self.alice,100,by_tg_id=ALICE)
        self.A.offer_rtm(self.db,self.season,lot,holder=self.bob)
        self.db.commit()
        intent=self.live_context()
        self.assert_ok(self.action('rtm',tg=BOB,accept=True,**intent))
        final=self.live_context()
        self.assertEqual(self.action('rtm',accept=True,**intent)[0],409)
        self.assert_ok(self.action('bid',amount_lakh=110,**final))
        decision=self.live_context()
        self.assert_ok(self.action('rtm',tg=BOB,accept=True,**decision))
        self.refresh_db()
        lot=self.db.get(type(lot),lot.id)
        self.assertEqual((lot.sold_to_id,lot.sold_price_lakh),(self.bid,110))
        self.assertEqual(self.bob.rtm_cards_used,1)

    def test_dynamic_retention_keeps_hidden_demands_private(self):
        from services import retention_negotiation as RN
        RN.set_mode(self.db,self.season,'dynamic')
        RN.save_slots(self.db,self.season,[dict(key='open',label='Open',emoji='🏏',min_rating=1,max_rating=100,floor_lakh=100)])
        self.db.commit()
        self.assert_ok(self.action('talk_start',player_id=self.players[0].id))
        data=self.assert_ok(self.read('preseason'))
        talk=data['talks'][0]
        self.assertNotIn('map_lakh',json.dumps(data))
        self.assertEqual(self.action('talk_offer',tg=BOB,talk_id=talk['id'],attempts=0,amount_lakh=100)[0],403)
        # An invalid monetary offer must not consume a negotiation attempt.
        self.assertEqual(self.action('talk_offer',talk_id=talk['id'],attempts=0,amount_lakh=-1)[0],400)
        fresh=self.assert_ok(self.read('preseason'))['talks'][0]
        self.assertEqual(fresh['attempts'],0)
        self.assert_ok(self.action('talk_offer',talk_id=talk['id'],attempts=0,amount_lakh=100))

    def test_expansion_shortlist_pick_and_turn_authorization(self):
        from services import auction_preseason as P
        self.A.set_franchise_picks(self.db,self.season,self.alice,1)
        self.A.set_franchise_picks(self.db,self.season,self.bob,1)
        self.A.set_draft_order(self.db,self.season,[self.aid,self.bid])
        self.db.commit()
        data=self.assert_ok(self.read('preseason'))
        player=data['eligible']['players'][0]
        self.assert_ok(self.action('shortlist',operation='add',player_id=player['player_id']))
        own=self.assert_ok(self.read('preseason'))
        other=self.assert_ok(self.read('preseason',tg=BOB))
        self.assertEqual(own['shortlist'][0]['player_id'],player['player_id'])
        self.assertEqual(other['shortlist'],[])
        self.assertEqual(self.action('draft_pick',tg=BOB,player_id=player['player_id'])[0],400)
        self.assert_ok(self.action('draft_pick',player_id=player['player_id']))
        self.refresh_db()
        self.assertEqual(self.alice.draft_picks_used,1)
        self.assertEqual(self.A.pick_turn(self.db,self.season).id,self.bid)
        self.assertEqual(self.A.ledger_total(self.db,self.aid),self.alice.purse_remaining_lakh)
        self.assert_ok(self.action('draft_skip',tg=BOB))
        self.refresh_db()
        self.assertIsNone(self.A.pick_turn(self.db,self.season))

    def test_auto_sets_and_catalogue_use_active_master_cards(self):
        data=self.assert_ok(self.post('/catalogue',dict(season_id=self.sid,query=self.players[0].name),ADMIN))
        self.assertEqual(data['players'][0]['id'],self.players[0].id)
        self.assert_ok(self.action('admin_pool_auto_sets',tg=ADMIN))
        self.assert_ok(self.read('pool'))

    def seed_trade_squads(self):
        self.season.max_retentions=2
        self.A.retain(self.db,self.season,self.alice,self.players[0],100)
        self.A.retain(self.db,self.season,self.bob,self.players[1],100)
        self.db.commit()

    def test_trade_builder_is_private_and_the_receiver_alone_can_answer(self):
        self.seed_trade_squads()
        tid=self.assert_ok(self.action('trade_open',other_team_id=self.bid))['trade_id']
        self.assertFalse(any(t['id']==tid for t in self.assert_ok(self.read('trades',tg=BOB))['trades']))
        data=self.assert_ok(self.read('trades'))
        trade=next(t for t in data['trades'] if t['id']==tid)
        lot=next(t for t in data['teams'] if t['id']==self.aid)['players'][0]
        self.assertEqual(self.action('trade_toggle',tg=BOB,trade_id=tid,version=trade['version'],lot_id=lot['id'])[0],403)
        self.assert_ok(self.action('trade_toggle',trade_id=tid,version=trade['version'],lot_id=lot['id']))
        trade=next(t for t in self.assert_ok(self.read('trades'))['trades'] if t['id']==tid)
        self.assert_ok(self.action('trade_send',trade_id=tid,version=trade['version']))
        trade=next(t for t in self.assert_ok(self.read('trades',tg=BOB))['trades'] if t['id']==tid)
        self.assertEqual(self.action('trade_accept',trade_id=tid,version=trade['version'])[0],403)
        self.assert_ok(self.action('trade_accept',tg=BOB,trade_id=tid,version=trade['version']))
        self.assert_ok(self.action('admin_trade_approve',tg=ADMIN,trade_id=tid))
        self.refresh_db()
        from models import AuctionLot
        self.assertEqual(self.db.get(AuctionLot,lot['id']).sold_to_id,self.bid)
        self.assertEqual(self.action('admin_trade_approve',tg=ADMIN,trade_id=tid)[0],400)

    def test_trade_block_and_auction_admin_approval_boundary(self):
        self.seed_trade_squads()
        lot=self.A.squad(self.db,self.aid)[0]
        self.assert_ok(self.action('trade_block_add',lot_id=lot.id,note='Looking for a bowler'))
        data=self.assert_ok(self.read('trades'))
        self.assertEqual(data['block'][0]['note'],'Looking for a bowler')
        self.A.add_auction_admin(self.db,OUTSIDER)
        self.db.commit()
        self.assertEqual(self.action('admin_trade_approve',tg=OUTSIDER,trade_id=999999)[0],403)
        self.assertEqual(self.action('admin_add_admin',tg=OUTSIDER,telegram_id=123456)[0],403)

    def test_previous_squad_rights_have_persistent_ids_for_release_buttons(self):
        from models import AuctionLot
        old=self.A.create_season(self.db,'Previous '+str(self.sid),max_retentions=2,
                                  min_squad_size=0,max_squad_size=5)
        a=self.A.create_franchise(self.db,old,'Mumbai',owner_tg_id=ALICE)
        b=self.A.create_franchise(self.db,old,'Chennai',owner_tg_id=BOB)
        self.A.retain(self.db,old,a,self.players[0],100)
        self.A.retain(self.db,old,b,self.players[1],100)
        old.status=self.A.STATUS_COMPLETED
        self.A.publish_to_league(self.db,old)
        self.A.link_previous_season(self.db,self.season,old.league_id)
        self.season.max_retentions=2
        original=self.db.query(AuctionLot).filter_by(season_id=self.sid,player_id=self.players[0].id).one()
        self.A.remove_lot(self.db,self.season,original)
        self.db.commit()
        data=self.assert_ok(self.read('preseason'))
        right=next(l for l in data['releasable'] if l['player_id']==self.players[0].id)
        self.refresh_db()
        self.assertIsNotNone(self.db.get(AuctionLot,right['id']))
        self.assert_ok(self.action('release',lot_id=right['id']))
        self.refresh_db()
        self.assertEqual(self.db.get(AuctionLot,right['id']).acquisition,self.A.ACQ_RELEASED_TRADE)

    def test_switch_links_preserve_season_and_group(self):
        from services.miniapp_buttons import auction_button, auction_launch
        from services import auction_rich
        with patch.dict(os.environ,{'BOT_USERNAME':'TestBot','MINIAPP_NAME':'arena','WEBAPP_URL':'https://app.example/webapp'}):
            button=auction_button(self.season)
            self.assertIn(f'startapp=auction_{self.sid}_c{self.season.chat_id}',button.url)
            self.assertEqual(auction_launch(f'auction_{self.sid}_c{self.season.chat_id}'),dict(season_id=self.sid,chat_id=self.season.chat_id))
            private=auction_button(self.season,is_private=True)
            self.assertEqual(private.web_app.url,f'https://app.example/webapp#auction_{self.sid}')
            keyboard=auction_rich.bid_keyboard(self.season,None)
            self.assertTrue(any('Switch to Miniapp' in b.text for row in keyboard.inline_keyboard for b in row))
        data=self.assert_ok(self.read())
        self.assertTrue(data['season']['telegram_url'].startswith('https://t.me/c/'))


if __name__ == '__main__':
    unittest.main()
