"""Split long Telegram HTML without cutting entities, emoji or open tags."""
from html import escape
from html.parser import HTMLParser


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def split_html(text, limit=3500):
    if utf16_length(text) <= limit:
        return [text] if text else []

    class Splitter(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.parts = []
            self.buffer = []
            self.units = 0

        def flush(self):
            if self.units:
                self.parts.append(''.join(self.buffer) + ''.join(f'</{tag}>' for tag, _ in reversed(self.stack)))
            self.buffer = [opening for _, opening in self.stack]
            self.units = 0

        def handle_starttag(self, tag, attrs):
            if tag == 'br':
                self.handle_data('\n')
                return
            opening = self.get_starttag_text()
            self.stack.append((tag, opening))
            self.buffer.append(opening)

        def handle_endtag(self, tag):
            if self.stack and self.stack[-1][0] == tag:
                self.stack.pop()
                self.buffer.append(f'</{tag}>')

        def handle_data(self, data):
            for char in data:
                width = 2 if ord(char) > 0xffff else 1
                if self.units + width > limit:
                    self.flush()
                self.buffer.append(escape(char, quote=False))
                self.units += width

    parser = Splitter()
    parser.feed(text)
    parser.close()
    parser.flush()
    return parser.parts


async def send_html(bot, chat_id, text):
    for part in split_html(text):
        await bot.send_message(chat_id=chat_id, text=part, parse_mode='HTML')
