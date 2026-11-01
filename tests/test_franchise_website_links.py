"""Telegram auction links can opt into the separate website safely."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from services.miniapp_buttons import auction_button, franchise_website_url


class FranchiseWebsiteLinkTests(unittest.TestCase):
    def setUp(self):
        self.season = SimpleNamespace(id=12, chat_id=-1001234567890)

    def test_website_link_is_a_browser_url_in_groups_and_private_chats(self):
        with patch.dict('os.environ', {'FRANCHISE_WEBSITE_URL': 'https://auction.example/franchise'}):
            for private in (True, False):
                button = auction_button(self.season, is_private=private)
                self.assertEqual(button.text, '🌐 Open auction website')
                self.assertIsNone(button.web_app)
                url = urlsplit(button.url)
                self.assertEqual(url.path, '/franchise')
                self.assertEqual(parse_qs(url.query), {
                    'season_id': ['12'], 'chat_id': ['-1001234567890']})

    def test_existing_query_survives_but_old_route_hints_are_replaced(self):
        with patch.dict('os.environ', {'FRANCHISE_WEBSITE_URL':
                        'https://auction.example?source=telegram&empty=&season_id=99&chat_id=-1'}):
            url = urlsplit(franchise_website_url(self.season))
            self.assertEqual(url.path, '/')
            self.assertEqual(parse_qs(url.query, keep_blank_values=True), {
                'source': ['telegram'], 'empty': [''],
                'season_id': ['12'], 'chat_id': ['-1001234567890']})

    def test_absent_group_hint_is_omitted(self):
        with patch.dict('os.environ', {'FRANCHISE_WEBSITE_URL': 'https://auction.example/'}):
            for chat_id in (None, 123, 'bad'):
                season = SimpleNamespace(id=12, chat_id=chat_id)
                self.assertEqual(parse_qs(urlsplit(franchise_website_url(season)).query),
                                 {'season_id': ['12']})

    def test_invalid_config_preserves_existing_miniapp_group_and_private_links(self):
        settings = {'BOT_USERNAME': 'test_bot', 'MINIAPP_NAME': 'arena',
                    'WEBAPP_URL': 'https://mini.example/webapp'}
        invalid = ('', 'http://auction.example/', 'https:///franchise',
                   'https://name:password@auction.example/',
                   'https://auction.example/#old-route',
                   'https://auction.example:bad/',
                   'https://auction.example/line\nbreak')
        for value in invalid:
            with self.subTest(value=value), patch.dict('os.environ', {
                    **settings, 'FRANCHISE_WEBSITE_URL': value}):
                self.assertIsNone(franchise_website_url(self.season))
                group = auction_button(self.season)
                self.assertEqual(group.text, '📱 Switch to Miniapp')
                self.assertEqual(group.url,
                                 'https://t.me/test_bot/arena?startapp=auction_12_c-1001234567890')
                private = auction_button(self.season, is_private=True)
                self.assertEqual(private.web_app.url, 'https://mini.example/webapp#auction_12')


if __name__ == '__main__':
    unittest.main()
