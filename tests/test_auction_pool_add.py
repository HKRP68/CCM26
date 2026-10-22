"""Named editions enter existing sets atomically and keep the waiting queue grouped."""
from tests import test_auction_features as F

setUpModule = F.setUpModule
tearDownModule = F.tearDownModule


class NamedPoolTests(F.FeatureCase):
    def setUp(self):
        super().setUp()
        from services import auction_pool_add
        self.P = auction_pool_add

    def card(self, name, rating=90, category='Batsman', version='Base', **extra):
        from models import Player
        values = dict(F.PLAYER_DEFAULTS)
        values.update(name=f'{name} {self.tag}', rating=rating,
                      category=category, version=version, **extra)
        player = Player(**values)
        self.session.add(player)
        self.session.flush()
        return player

    def seed(self, name='Seed', rating=90, category='Batsman', label='85-90 OVR'):
        player = self.card(name, rating, category)
        self.A.add_players_to_pool(self.session, self.season, [player], set_name=label)
        self.session.commit()
        return player

    def add(self, raw):
        result = self.P.add_named_players(self.session, self.season, raw)
        self.session.commit()
        return result

    def test_named_versions_go_to_rating_sets_and_queue_stays_together(self):
        self.seed('Low seed', 88, label='85-90 OVR')
        self.seed('High seed', 94, label='91-96 OVR')
        base = self.card('Mitchell Starc', 89, 'Bowler')
        icon = self.card('Mitchell Starc', 95, 'Bowler', 'Icon')
        result = self.add(f'{base.name.lower()} | base, {icon.name} | ICON, ')
        self.assertEqual(['85-90 OVR', '91-96 OVR'], [r['set'] for r in result])
        self.assertEqual([True, True], [r['added'] for r in result])
        labels = [self.A.set_label(lot) for lot in self.A.queued_lots(self.session, self.season)]
        self.assertEqual(['85-90 OVR', '85-90 OVR', '91-96 OVR', '91-96 OVR'], labels)
        self.assertEqual([1, 2], [r['set_no'] for r in result])

    def test_role_tiers_and_marquee_follow_existing_rating_floors(self):
        self.seed('Marquee seed', 97, label='Marquee')
        self.seed('Top bowler', 90, 'Bowler', 'Bowlers 1')
        self.seed('Second bowler', 80, 'Bowler', 'Bowlers 2')
        top = self.card('Elite new', 99, 'Bowler')
        first = self.card('First new', 95, 'Bowler')
        second = self.card('Second new', 82, 'Bowler')
        result = self.add(', '.join(f'{p.name} | Base' for p in (top, first, second)))
        self.assertEqual(['Marquee', 'Bowlers 1', 'Bowlers 2'], [r['set'] for r in result])

    def test_custom_named_set_matches_observed_rating_and_role(self):
        self.seed('Top bat', 96, 'Batsman', 'Top batters')
        self.seed('Low bat', 90, 'Batsman', 'Top batters')
        self.seed('Bowler', 94, 'Bowler', 'Seam attack')
        player = self.card('New bat', 93)
        self.assertEqual('Top batters', self.add(f'{player.name} | Base')[0]['set'])

    def test_equal_role_tier_boundaries_use_the_first_tier(self):
        self.seed('First bowler', 90, 'Bowler', 'Bowlers 1')
        self.seed('Second bowler', 90, 'Bowler', 'Bowlers 2')
        player = self.card('Tied rating', 90, 'Bowler')
        self.assertEqual('Bowlers 1', self.add(f'{player.name} | Base')[0]['set'])

    def test_batch_fails_before_any_player_is_added_if_name_or_version_is_wrong(self):
        self.seed()
        valid = self.card('Valid', 89)
        self.session.commit()
        for missing in ('Unknown Player | Base', f'{valid.name} | Unknown Version'):
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(self.A.AuctionError, 'No active master card'):
                    self.P.add_named_players(self.session, self.season, f'{valid.name} | Base, {missing}')
                self.session.rollback()
                self.assertEqual(1, len(self.A.lots(self.session, self.season.id)))

    def test_ambiguous_catalogue_edition_is_refused(self):
        self.seed()
        player = self.card('Duplicate')
        self.card('Duplicate')
        with self.assertRaisesRegex(self.A.AuctionError, 'Multiple active cards'):
            self.P.add_named_players(self.session, self.season, f'{player.name} | Base')
        self.assertEqual(1, len(self.A.lots(self.session, self.season.id)))

    def test_duplicates_are_skipped_and_existing_sold_players_are_preserved(self):
        player = self.seed()
        lot = self.A.lots(self.session, self.season.id)[0]
        lot.status = self.A.LOT_SOLD
        lot.sold_to_id = self.mumbai.id
        lot.sold_price_lakh = 100
        lot.acquisition = self.A.ACQ_RETAINED
        self.session.commit()
        result = self.add(f'{player.name} | Base, {player.name} | Base')
        self.assertEqual([False, False], [r['added'] for r in result])
        self.assertEqual(self.A.LOT_SOLD, lot.status)
        self.assertEqual(self.mumbai.id, lot.sold_to_id)
        self.assertEqual(1, len(self.A.lots(self.session, self.season.id)))

    def test_repeated_new_player_is_added_once(self):
        self.seed()
        player = self.card('Repeated', 89)
        result = self.add(f'{player.name} | Base, {player.name} | Base')
        self.assertEqual([True, False], [r['added'] for r in result])
        self.assertEqual(2, len(self.A.lots(self.session, self.season.id)))

    def test_unmatched_role_or_rating_is_not_filed_in_an_incorrect_set(self):
        self.seed(label='85-90 OVR')
        player = self.card('Outside', 99)
        with self.assertRaisesRegex(self.A.AuctionError, 'No existing set fits'):
            self.P.add_named_players(self.session, self.season, f'{player.name} | Base')

    def test_no_sets_gives_configuration_guidance(self):
        player = self.card('New')
        with self.assertRaisesRegex(self.A.AuctionError, 'Create auction sets'):
            self.P.add_named_players(self.session, self.season, f'{player.name} | Base')

    def test_custom_single_role_set_does_not_absorb_other_roles(self):
        self.seed('Seamer', 90, 'Bowler', 'Seam attack')
        player = self.card('Wicket keeper', 90, 'Wicket Keeper')
        with self.assertRaisesRegex(self.A.AuctionError, 'No existing set fits'):
            self.P.add_named_players(self.session, self.season, f'{player.name} | Base')

    def test_long_batch_reply_splits_at_player_boundaries(self):
        from handlers import auction as H
        self.seed()
        players = [self.card('New player ' + str(i) + ' ' + 'LongName' * 10, 89)
                   for i in range(30)]
        self.session.commit()
        text = ', '.join(f'{p.name} | Base' for p in players)
        with F.AdminEnv(F.CAROL):
            out = self.run_handler(H.aaddpl_handler, F.CAROL, (text,))
        self.assertGreater(len(out.replies), 1)
        self.assertTrue(all(len(reply) <= 4096 for reply in out.replies))
        self.assertIn('30 added', out.replies[0])
        self.assertEqual(30, '\n'.join(out.replies).count('→ set #1'))

    def test_overlapping_equal_ranges_are_refused_without_guessing(self):
        self.seed('Range one', 90, label='85-90 OVR')
        self.seed('Range two', 89, label='85–90 OVR')
        player = self.card('Ambiguous set', 88)
        with self.assertRaisesRegex(self.A.AuctionError, 'More than one existing set fits'):
            self.P.add_named_players(self.session, self.season, f'{player.name} | Base')
        self.assertEqual(2, len(self.A.lots(self.session, self.season.id)))

    def test_inactive_and_career_cards_are_excluded(self):
        self.seed()
        players = (self.card('Inactive', is_active=False), self.card('Career', is_career=True))
        self.session.commit()
        for player in players:
            with self.assertRaisesRegex(self.A.AuctionError, 'No active master card'):
                self.P.add_named_players(self.session, self.season, f'{player.name} | Base')
            self.session.rollback()

    def test_invalid_entry_requires_both_player_and_version(self):
        for raw in ('', ',', 'Player', 'Player |', '| Base', 'Player | Base | Set'):
            with self.subTest(raw=raw), self.assertRaises(self.A.AuctionError):
                self.P.parse_players(raw)

    def test_live_auction_requires_pause(self):
        self.seed()
        player = self.card('During live', 89)
        self.season.status = self.A.STATUS_LIVE
        self.session.commit()
        with self.assertRaisesRegex(self.A.AuctionError, 'Pause the auction'):
            self.P.add_named_players(self.session, self.season, f'{player.name} | Base')

    def test_paused_addition_preserves_current_and_sold_lot_numbers(self):
        self.seed('First', 90)
        self.seed('Next', 89)
        lots = self.A.lots(self.session, self.season.id)
        first, sold = lots
        first.status = self.A.LOT_ON_BLOCK
        sold.status = self.A.LOT_SOLD
        sold.sold_to_id = self.mumbai.id
        self.season.current_lot_id = first.id
        self.season.status = self.A.STATUS_PAUSED
        self.session.commit()
        original = (first.lot_no, sold.lot_no)
        player = self.card('Paused add', 88)
        result = self.add(f'{player.name} | Base')
        self.assertTrue(result[0]['added'])
        self.assertEqual(original, (first.lot_no, sold.lot_no))
        self.assertEqual(first.id, self.season.current_lot_id)

    def test_command_is_admin_only_and_reports_the_destination(self):
        from handlers import auction as H
        self.seed()
        player = self.card('Via command', 89)
        self.session.commit()
        with F.AdminEnv(F.CAROL):
            denied = self.run_handler(H.aaddpl_handler, F.ALICE,
                                      (player.name, '|', 'Base'))
            self.assertIn('Only auction admins', denied.replies[-1])
            out = self.run_handler(H.aaddpl_handler, F.CAROL,
                                   (player.name, '|', 'Base', ','))
        self.assertIn('1 added', out.replies[-1])
        self.assertIn('set #1', out.replies[-1])
        self.assertIn('85-90 OVR', out.replies[-1])
