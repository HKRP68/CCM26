import asyncio
import unittest
from html.parser import HTMLParser

from services.telegram_text import split_html, utf16_length
from test_event_media_caption import _Ctx, _fire, _row


class _Reader(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ''
        self.stack = []
    def handle_data(self, data):
        self.text += data
    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
    def handle_endtag(self, tag):
        assert self.stack.pop() == tag


class LongTextTests(unittest.TestCase):
    def test_splitting_preserves_text_and_balanced_formatting_with_emoji_and_entities(self):
        original = '🏏 & runs <more> ' * 700
        from html import escape
        parts = split_html('<b><i>' + escape(original) + '</i></b>')
        text = ''
        for part in parts:
            reader = _Reader()
            reader.feed(part)
            self.assertFalse(reader.stack)
            self.assertLessEqual(utf16_length(reader.text), 3500)
            text += reader.text
        self.assertEqual(text, original)
        self.assertGreater(len(parts), 1)

    def test_long_media_caption_is_sent_after_media_in_chunks(self):
        text = '<b>' + '🏏' * 4000 + '</b>'
        calls = _fire(_Ctx(), [_row(caption=text)])
        self.assertEqual(calls[0][0], 'send_animation')
        self.assertNotIn('caption', calls[0][1])
        self.assertGreater(len(calls), 2)
        self.assertTrue(all(name == 'send_message' for name, _ in calls[1:]))

    def test_long_text_without_media_is_split(self):
        calls = _fire(_Ctx(), [_row(source='', caption='x' * 12000)])
        self.assertEqual(''.join(kwargs['text'] for _, kwargs in calls), 'x' * 12000)
        self.assertTrue(all(len(kwargs['text']) <= 3500 for _, kwargs in calls))
