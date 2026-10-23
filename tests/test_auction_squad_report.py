"""Full signed squads, separate rights and accurate net purse use in both views."""
from tests import test_auction_features as F

setUpModule = F.setUpModule
tearDownModule = F.tearDownModule


class SquadReportTests(F.FeatureCase):
    max_squad = 40

    def setUp(self):
        super().setUp()
        from services import auction_squad_report as REPORT
        self.REPORT = REPORT
        self.season.max_retentions = 40
        self.session.commit()

    def sign(self, name, category='Batsman', overall=91, batting=94, bowling=73,
             team=None, version='Base', price=100):
        from models import Player
        values = dict(F.PLAYER_DEFAULTS)
        values.update(name=f'{name} {self.tag}', rating=overall, category=category,
                      bat_rating=batting, bowl_rating=bowling, version=version)
        player = Player(**values)
        self.session.add(player)
        self.session.flush()
        lot = self.A.retain(self.session, self.season, team or self.mumbai, player, price)
        self.session.commit()
        return lot

    def test_squad_groups_categories_and_shows_all_three_ratings_and_versions(self):
        from services import auction_rich as AR
        batter = self.sign('Batter', batting=96, bowling=45)
        bowler = self.sign('Bowler', 'Bowler', overall=93, batting=49, bowling=97, version='IPL2026')
        self.sign('All rounder one', 'All Rounder')
        self.sign('All rounder two', 'All-rounder')
        self.sign('Keeper', 'Wicket Keeper')
        blocks, text = AR.squad_view(self.session, self.season, self.mumbai)
        for heading in ('Batsmen (1)', 'Bowlers (1)', 'All-rounders (2)', 'Wicket-keepers (1)'):
            self.assertIn(heading, text)
        self.assertIn(f'{batter.name} [Base] · Batsman · OVR 91 · BAT 96 · BOWL 45', text)
        self.assertIn(f'{bowler.name} [IPL2026] · Bowler · OVR 93 · BAT 49 · BOWL 97', text)
        headings = [block['text'] for block in blocks if block['type'] == 'heading']
        self.assertIn('All-rounders (2)', headings)
        tables = [block for block in blocks if block['type'] == 'table']
        self.assertEqual(4, len(tables))
        cells = [str(cell) for table in tables for row in table['cells'] for cell in row]
        self.assertTrue(any('BAT' in cell for cell in cells))
        self.assertTrue(any('BOWL' in cell for cell in cells))
        self.assertTrue(any('Category' in cell for cell in cells))

    def test_squad_rating_is_the_signed_squad_average_in_both_renderings(self):
        from services import auction_rich as AR
        self.sign('First', overall=90, batting=94, bowling=40)
        self.sign('Second', 'Bowler', overall=86, batting=60, bowling=92)
        blocks, text = AR.squad_view(self.session, self.season, self.mumbai)
        expected = 'Squad rating: 88.0 OVR · BAT 77.0 · BOWL 66.0'
        self.assertIn(expected, text)
        self.assertIn(expected, str(blocks))
        _, empty = AR.squad_view(self.session, self.season, self.delhi)
        self.assertIn('Squad rating: — OVR', empty)

    def test_net_used_and_contract_value_differ_after_purse_adjustment(self):
        self.sign('Paid batter', price=1000)
        self.A.correct_purse(self.session, self.season, self.mumbai, 200)
        self.session.commit()
        data = self.REPORT.team_data(self.session, self.season, self.mumbai)
        self.assertEqual(800, data['used'])
        self.assertEqual(1000, data['contract_value'])
        self.assertEqual(9200, data['left'])
        self.assertEqual(data['total'], data['used'] + data['left'])
        self.assertIn('used (net) ₹8 Cr', self.REPORT.render_team(self.season, data))

    def test_summary_lists_every_team_including_empty_teams(self):
        from services import auction_rich as AR
        self.sign('Mumbai batter')
        self.sign('Chennai bowler', 'Bowler', team=self.chennai)
        _, text = AR.teams_summary_view(self.session, self.season)
        for team in (self.mumbai, self.chennai, self.delhi):
            self.assertIn(team.name, text)
        self.assertIn('Mumbai batter', text)
        self.assertIn('Chennai bowler', text)
        self.assertIn('Nobody signed yet', text)
        self.assertIn('min 2, max 40', text)
        self.assertIn('Current contracts:', text)
        self.assertIn('Max bid:', text)

    def test_role_shortages_and_limits_use_actual_configured_rules(self):
        self.sign('Batter')
        self.A.set_role_rules(self.session, self.season, {'Bowler': 2}, {'Bowler': 5})
        self.session.commit()
        data = self.REPORT.team_data(self.session, self.season, self.mumbai)
        text = self.REPORT.render_team(self.season, data)
        self.assertIn('Bowler 0 · min 2, max 5', text)
        self.assertIn('Needs: 2 Bowler', text)
        self.assertEqual(1, data['needed'])
        self.assertEqual(39, data['slots'])

    def test_unretained_rights_are_shown_separately_without_charging_or_materializing(self):
        from models import Player
        from tests._previous_league import link_squads
        lot = self.sign('Signed')
        player = Player(name=f'Unretained {self.tag}', rating=90, **F.PLAYER_DEFAULTS)
        player.bat_rating, player.bowl_rating = 80, 93
        self.session.add(player)
        self.session.flush()
        signed = self.session.query(Player).filter_by(id=lot.player_id).one()
        link_squads(self.session, self.A, self.season, {'Mumbai': [signed, player],
                                                      'Chennai': [self.players[0]]})
        before = len(self.A.lots(self.session, self.season.id))
        data = self.REPORT.team_data(self.session, self.season, self.mumbai)
        text = self.REPORT.render_team(self.season, data)
        self.assertIn('Unretained squad rights — not signed', text)
        self.assertIn(player.name, text)
        self.assertEqual(1, data['count'])
        self.assertEqual(100, data['contract_value'])
        self.assertEqual(9900, data['left'])
        self.assertEqual(before, len(self.A.lots(self.session, self.season.id)))
        self.assertIn('Squad rating: 91.0 OVR', text)

    def test_summary_command_and_asquad_all_are_public_and_split_without_omission(self):
        from handlers import auction as H
        for i in range(30):
            self.sign('Full squad ' + str(i) + ' ' + 'LongName' * 8,
                      'Bowler' if i % 2 else 'Batsman')
        out = self.run_handler(H.ateamsummary_handler, 9999)
        text = '\n'.join(out.replies)
        self.assertGreater(len(out.replies), 1)
        self.assertTrue(all(len(reply) <= 4096 for reply in out.replies))
        self.assertEqual(30, text.count('LongName' * 8))
        self.assertIn('Chennai', text)
        self.assertIn('Delhi', text)
        for handler, args in ((H.asquad_handler, ('all',)),
                              (H.ateamsummary_handler, ('Mumbai',))):
            out = self.run_handler(handler, 9999, args)
            self.assertEqual(30, '\n'.join(out.replies).count('LongName' * 8))

    def test_html_escapes_names_and_custom_categories(self):
        self.sign('<Bad & Name>', '<Custom>')
        text = self.A.render_squad(self.session, self.season, self.mumbai)
        self.assertIn('&lt;Bad &amp; Name&gt;', text)
        self.assertIn('&lt;Custom&gt;', text)
        self.assertNotIn('<Bad & Name>', text)
