"""A plain 1200×630 banner for auto-generated CMU News stories.

Nothing clever: a kind-coloured gradient, a kicker line and the headline, so an
auto story has a thumbnail in the Mini App feed and something to post to the
channel. Emoji are stripped because the card fonts have no glyphs for them.
"""

import io
import os
import re

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "assets", "fonts")
_HEAD_FONT = "Anton-Regular.ttf"
_KICKER_FONT = "Montserrat-ExtraBold.ttf"

# (top colour, bottom colour, accent, default kicker)
_STYLE = {
    "tournament_champion": ((120, 72, 8), (22, 16, 6), (255, 204, 77), "CHAMPIONS"),
    "auction_record": ((6, 92, 64), (6, 22, 18), (0, 212, 170), "RECORD BUY"),
    "auction_complete": ((24, 60, 120), (8, 14, 30), (74, 158, 255), "AUCTION WRAP"),
    "season_winners": ((96, 24, 110), (20, 8, 28), (220, 130, 255), "SEASON WINNERS"),
    "ranked_season": ((120, 20, 40), (26, 6, 12), (255, 77, 109), "RANKED CHAMPION"),
    "hall_of_fame": ((70, 70, 90), (14, 14, 22), (240, 240, 255), "HALL OF FAME"),
}
_DEFAULT = ((30, 40, 60), (10, 14, 22), (74, 158, 255), "CMU NEWS")

W, H = 1200, 630

_NON_TEXT = re.compile(r"[^\w\s.,:;!?'\"()&%+/#@₹$-]", re.UNICODE)


def _font(name, size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(os.path.join(_FONT_DIR, name), size)
    except Exception:
        return ImageFont.load_default()


def _wrap(draw, text, font, width):
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _plain(text):
    return " ".join(_NON_TEXT.sub("", text or "").split())


def render_banner(kind, headline, kicker=None):
    """Return PNG bytes for an auto story's banner."""
    from PIL import Image, ImageDraw
    top, bottom, accent, default_kicker = _STYLE.get(kind, _DEFAULT)
    image = Image.new("RGB", (W, H), bottom)
    draw = ImageDraw.Draw(image)
    for y in range(H):
        t = y / (H - 1)
        colour = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (W, y)], fill=colour)
    # Accent stripes, for a little broadcast-graphic energy.
    draw.rectangle([0, 0, W, 10], fill=accent)
    draw.polygon([(W - 360, H), (W - 180, H), (W, H - 220), (W, H - 400)],
                 fill=tuple(min(255, c + 18) for c in bottom))

    kicker_text = (_plain(kicker) or default_kicker).upper()
    kfont = _font(_KICKER_FONT, 34)
    draw.rectangle([70, 80, 70 + draw.textlength(kicker_text, font=kfont) + 40, 136],
                   fill=accent)
    draw.text((90, 86), kicker_text, font=kfont, fill=(10, 12, 18))

    title = _plain(headline).upper() or "CMU NEWS"
    size = 96
    while size > 44:
        hfont = _font(_HEAD_FONT, size)
        lines = _wrap(draw, title, hfont, W - 160)
        if len(lines) <= 3:
            break
        size -= 8
    lines = lines[:4]
    y = 180
    for line in lines:
        draw.text((70, y), line, font=hfont, fill=(255, 255, 255))
        y += int(size * 1.12)

    ffont = _font(_KICKER_FONT, 26)
    draw.text((70, H - 70), "CRICMASTER  •  CMU NEWS", font=ffont, fill=accent)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
