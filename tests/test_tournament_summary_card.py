"""The supplied HTML stays intact while match data and readable colours change."""

import hashlib
import io
import itertools
import json
import re
import unittest
from unittest.mock import patch

from PIL import Image

from services import match_summary_card as base
from services import tournament_summary_card as card


def bound_data(html):
    return json.loads(re.search(r'id="cmu-match-data">(.*?)</script>', html, re.S)[1])


class OriginalHTMLTests(unittest.TestCase):
    def test_the_supplied_template_is_preserved_byte_for_byte(self):
        self.assertEqual(hashlib.sha256(card.TEMPLATE.read_bytes()).hexdigest(),
                         '38758bd92de0fb8733291052fbd1f1fd5e5909fc3fb4d20f87975dba98c38c2b')
        self.assertEqual(card.build_tournament_summary_html(), card.original_html())

    def test_actual_scores_and_badges_bind_to_the_existing_layout(self):
        html = card.build_tournament_summary_html(card.preview_payload())
        data = bound_data(html)
        self.assertEqual(data['innings'][0]['score'], '6/195')
        self.assertEqual(data['innings'][1]['score'], '6/199')
        self.assertEqual(data['margin'], 'WIN BY 4 WICKETS')
        self.assertTrue(data['innings'][1]['batters'][0]['potm'])
        self.assertTrue(data['innings'][1]['batters'][2]['impact'])
        self.assertEqual(data['figures'], '48*(26)')
        self.assertIn('class="season-ribbon"', html)
        self.assertIn('class="winner-inner"', html)
        self.assertIn('class="potm-strip-inner"', html)
        self.assertNotIn('TOP PERFORMERS', html)

    def test_match_names_are_text_even_when_they_contain_html(self):
        data = card.preview_payload()
        hostile = '</script><script>window.compromised=true</script>'
        data.update(inn1_team=hostile, potm_name=hostile)
        html = card.build_tournament_summary_html(data)
        self.assertNotIn(hostile, html)
        self.assertEqual(bound_data(html)['innings'][0]['team'], hostile.upper())

    def test_bowling_awards_and_ties_keep_their_recorded_values(self):
        data = card.preview_payload()
        data.update(winner_name='Match Tied', win_margin_text='', potm_stats='5/21',
                    potm_runs=None, potm_balls=None, potm_wickets=5, potm_conceded=21)
        view = bound_data(card.build_tournament_summary_html(data))
        self.assertEqual(view['winner'], 'MATCH TIED')
        self.assertEqual(view['margin'], '')
        self.assertEqual(view['figures'], '5/21')


class ReadabilityTests(unittest.TestCase):
    def test_the_whole_header_gradient_has_readable_text(self):
        for colour in itertools.product((0, 32, 64, 96, 128, 160, 192, 224, 255), repeat=3):
            palette = card.header_palette(colour)
            ink = base._hex_to_rgb(palette['on-team'], card.WHITE)
            for key in ('header-top', 'header-mid', 'header-bottom'):
                stop = base._hex_to_rgb(palette[key], colour)
                self.assertGreaterEqual(card.contrast_ratio(stop, ink), 4.5, (colour, key))
            self.assertEqual(base._hex_to_rgb(palette['team'], colour), colour)


class OriginalHTMLExportTests(unittest.TestCase):
    def test_png_export_uses_the_original_html_dimensions(self):
        image = Image.open(io.BytesIO(base.generate_match_summary(**card.preview_payload())))
        self.assertEqual(image.size, (1536, 864))

    def test_bad_logos_and_long_names_still_export(self):
        data = card.preview_payload()
        data.update(inn1_team='Long tournament team name ' * 6,
                    potm_name='Player name ' * 12, inn1_logo_png=b'bad image')
        with patch.object(base.logger, 'warning'):
            png = base.generate_match_summary(**data)
        self.assertEqual(Image.open(io.BytesIO(png)).size, (1536, 864))


if __name__ == '__main__':
    unittest.main()
