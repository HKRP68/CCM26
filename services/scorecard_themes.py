"""Illustrated backdrops, metallic frames and seasonal scorecard details.

Bundled artwork contains no cricket data. Text, crests and collectible cards
are rendered live. Cached plates and palettes are never mutated by a render.
"""

import logging
import math
import random
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from services.scorecard_theme_catalog import (
    CHRISTMAS, DIWALI, NEW_YEAR, NORMAL, REPUBLIC_DAY, SANKRANTI, WINTER,
    normalize_theme, theme_definition,
)

logger = logging.getLogger(__name__)
SUMMARY_FOOTER_H = 128
_ROOT = Path(__file__).resolve().parents[1]
_ART = _ROOT / "static" / "scorecard_themes"
_FONTS = _ROOT / "assets" / "fonts"


@lru_cache(maxsize=9)
def _artwork(theme):
    with Image.open(_ART / f"{theme}.webp") as source:
        return source.convert("RGBA")


def _mix(a, b, fraction):
    return tuple(round(x + (y - x) * fraction) for x, y in zip(a, b))


@lru_cache(maxsize=64)
def _font(size, family="script"):
    filename = {"script": "Caveat-Bold.ttf", "display": "BebasNeue-Regular.ttf",
                "headline": "ArchivoBlack-Regular.ttf"}[family]
    try:
        return ImageFont.truetype(str(_FONTS / filename), max(8, round(size)))
    except OSError:
        return ImageFont.load_default()


def draw_theme_background(image, theme, *, scale=1, footer_height=SUMMARY_FOOTER_H,
                          header_height=182):
    """Keep artwork in proportion even on four-innings posters."""
    key = normalize_theme(theme)
    palette = theme_definition(key)
    try:
        artwork = _artwork(key)
    except (OSError, ValueError):
        logger.warning("Scorecard artwork unavailable for %s; using its palette", key)
        image.paste((*palette.band[0], 255), (0, 0, image.width, image.height))
        return
    top = min(round(header_height * scale), image.height // 3)
    bottom = min(round((footer_height + 28) * scale), image.height // 3)
    source_top, source_bottom = round(artwork.height * .19), round(artwork.height * .80)
    # Stretch the quiet middle, not the foreground objects or snowy mountains.
    slices = ((0, source_top, 0, top),
              (source_top, source_bottom, top, image.height - bottom))
    for sy0, sy1, y0, y1 in slices:
        section = artwork.crop((0, sy0, artwork.width, sy1)).resize(
            (image.width, y1 - y0), Image.Resampling.LANCZOS)
        image.paste(section, (0, y0))
    # Keep foreground figures and objects whole and in proportion. Stretch
    # only the quiet clearing between the two corner illustrations.
    foreground_y = round(artwork.height * palette.foreground_top)
    corner = round(artwork.width * .28)
    corner_w = min(image.width // 3, round(corner * bottom / (artwork.height - foreground_y)))
    sections = ((0, corner, 0, corner_w),
                (corner, artwork.width - corner, corner_w, image.width - corner_w),
                (artwork.width - corner, artwork.width, image.width - corner_w, image.width))
    for sx0, sx1, x0, x1 in sections:
        section = artwork.crop((sx0, foreground_y, sx1, artwork.height)).resize(
            (x1 - x0, bottom), Image.Resampling.LANCZOS)
        image.paste(section, (x0, image.height - bottom))
    if key != REPUBLIC_DAY:
        from PIL import ImageFilter
        wash = Image.new("RGBA", (image.width, top))
        wd = ImageDraw.Draw(wash)
        wd.rounded_rectangle((round(image.width * .27), round(top * .03),
                              round(image.width * .75), round(top * .60)),
                             radius=round(20 * scale), fill=(5, 13, 31, 125))
        image.alpha_composite(wash.filter(ImageFilter.GaussianBlur(18 * scale)))


def _snow_cap(image, box, scale, seed):
    """Textured snow on the border, with long icicles in the crest area only."""
    x0, y0, x1, _y1 = box
    width = x1 - x0
    rng = random.Random(seed)
    layer = Image.new("RGBA", (round((width + 14) * scale), round(28 * scale)))
    draw = ImageDraw.Draw(layer)
    def point(x, y):
        return round(x * scale), round(y * scale)
    top, bottom = [], []
    for x in range(0, math.ceil(width + 14), 3):
        top.append(point(x, 5 + math.sin(x * .09) * 1.1 + rng.uniform(-1.1, 1.1)))
        bottom.append(point(x, 11 + math.sin(x * .06) * 1.5 + rng.uniform(-.7, .7)))
    draw.polygon(top + list(reversed(bottom)), fill=(226, 244, 255, 255))
    draw.line(top, fill=(255, 255, 255, 255), width=max(1, round(2 * scale)))
    for x in range(9, math.ceil(width), 37):
        length = rng.uniform(7, 15) if x < 190 else rng.uniform(2, 5)
        draw.polygon([point(x, 10), point(x + 5, 10), point(x + 2, 11 + length)],
                     fill=(211, 238, 255, 240))
        draw.line([point(x + 2, 11), point(x + 2, 9 + length)],
                  fill=(255, 255, 255, 220), width=max(1, round(scale * .7)))
    for _ in range(max(50, round(width * 1.7))):
        x, y = rng.uniform(0, width + 10), rng.uniform(6, 10)
        draw.point(point(x, y), fill=(255, 255, 255, rng.randrange(100, 240)))
    image.alpha_composite(layer, (round((x0 - 7) * scale), round((y0 - 9) * scale)))


def draw_theme_panel(image, box, theme, *, scale=1, radius=16, snow=True):
    """Shaded metal, bright rims and an outer glow clear of the table's text."""
    from PIL import ImageChops, ImageFilter
    key = normalize_theme(theme)
    palette = theme_definition(key)
    x0, y0, x1, y1 = box
    pad = 8
    width, height = round((x1 - x0 + pad * 2) * scale), round((y1 - y0 + pad * 2) * scale)
    rim = Image.new("RGBA", (width, height))
    mask = Image.new("L", rim.size)
    md = ImageDraw.Draw(mask)
    bounds = (round(pad * scale), round(pad * scale),
              width - round(pad * scale) - 1, height - round(pad * scale) - 1)
    md.rounded_rectangle(bounds, radius=round(radius * scale), outline=255,
                         width=max(2, round(2.5 * scale)))
    rd = ImageDraw.Draw(rim)
    dark = tuple(round(c * .49) for c in palette.accent)
    for y in range(height):
        t = y / max(1, height - 1)
        if key == REPUBLIC_DAY:
            color = (palette.accent if t < .34 else (246, 247, 241) if t < .66
                     else (33, 142, 82))
        else:
            color = (_mix(palette.pale, palette.accent, min(1, t * 2)) if t < .5
                     else _mix(dark, palette.pale, (t - .5) * 2))
        rd.line([(0, y), (width, y)], fill=(*color, 255))
    rim.putalpha(mask)
    glow = Image.new("RGBA", rim.size, (*palette.accent, 0))
    interior = Image.new("L", rim.size)
    ImageDraw.Draw(interior).rounded_rectangle(bounds, radius=round(radius * scale), fill=255)
    glow_alpha = mask.filter(ImageFilter.GaussianBlur(3 * scale)).point(lambda a: a // 2)
    glow.putalpha(ImageChops.multiply(glow_alpha, ImageChops.invert(interior)))
    location = (round((x0 - pad) * scale), round((y0 - pad) * scale))
    image.alpha_composite(glow, location)
    image.alpha_composite(rim, location)
    if snow and palette.snowy:
        _snow_cap(image, box, scale, seed=round(y0) + (117 if key == CHRISTMAS else 29))


def draw_header_ornament(image, theme, *, scale=1, cx=840, cy=112):
    key = normalize_theme(theme)
    if key == NORMAL:
        return
    palette = theme_definition(key)
    layer = Image.new("RGBA", (round(42 * scale), round(30 * scale)))
    draw = ImageDraw.Draw(layer)
    cx0, cy0 = round(21 * scale), round(15 * scale)
    color = (*palette.pale, 255)
    if key == REPUBLIC_DAY:
        color = (17, 62, 111, 255)
        r = round(10 * scale)
        draw.ellipse((cx0 - r, cy0 - r, cx0 + r, cy0 + r), outline=color,
                     width=max(1, round(scale)))
        for i in range(24):
            a = i * math.tau / 24
            draw.line([(cx0, cy0), (cx0 + math.cos(a) * r, cy0 + math.sin(a) * r)],
                      fill=color, width=max(1, round(scale * .65)))
    elif palette.snowy:
        for i in range(6):
            a = i * math.tau / 6
            dx, dy = math.cos(a), math.sin(a)
            draw.line([(cx0, cy0), (cx0 + dx * 11 * scale, cy0 + dy * 11 * scale)],
                      fill=color, width=max(1, round(scale)))
            for sign in (-1, 1):
                draw.line([(cx0 + dx * 6 * scale, cy0 + dy * 6 * scale),
                           (cx0 + (dx * 8 + sign * dy * 3) * scale,
                            cy0 + (dy * 8 - sign * dx * 3) * scale)],
                          fill=color, width=max(1, round(scale)))
    else:
        draw.polygon([(cx0, cy0 - 9 * scale), (cx0 + 6 * scale, cy0),
                      (cx0, cy0 + 9 * scale), (cx0 - 6 * scale, cy0)], fill=color)
        draw.ellipse((cx0 - scale, cy0 - scale, cx0 + scale, cy0 + scale),
                     fill=(*palette.band[0], 255))
    image.alpha_composite(layer, (round(cx * scale) - cx0, round(cy * scale) - cy0))


def _centred_text(draw, text, cx, cy, font, color, scale):
    shadow = max(1, round(scale))
    draw.text((cx + shadow, cy + shadow), text, font=font, anchor="mm", fill=(0, 0, 0, 125))
    draw.text((cx, cy), text, font=font, anchor="mm", fill=(*color, 255))


def draw_theme_footer(image, theme, *, scale=1, height=SUMMARY_FOOTER_H):
    """Foreground art is in the plate; greetings stay crisp at full resolution."""
    key = normalize_theme(theme)
    if key not in (NEW_YEAR, SANKRANTI, REPUBLIC_DAY):
        return
    palette = theme_definition(key)
    layer = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(layer)
    cx = image.width // 2
    cy = image.height - round(height * .51 * scale)
    if key == NEW_YEAR:
        now = datetime.now(timezone.utc)
        year = now.year + (1 if now.month >= 10 else 0)
        title, caption, family, size = "Happy New Year", str(year), "script", 60
    elif key == SANKRANTI:
        title, caption, family, size = "Happy Makar Sankranti", "LET YOUR GAME SOAR", "script", 54
    else:
        title, caption, family, size = "REPUBLIC DAY", "CELEBRATING THE SPIRIT OF INDIA", "headline", 40
        draw_header_ornament(layer, key, scale=scale, cx=cx / scale,
                             cy=(image.height / scale) - height + 22)
    title_font = _font(size * scale, family)
    while draw.textlength(title, font=title_font) > image.width * .51 and size > 24:
        size -= 1
        title_font = _font(size * scale, family)
    color = palette.ink if key == REPUBLIC_DAY else palette.pale
    _centred_text(draw, title, cx, cy, title_font, color, scale)
    _centred_text(draw, caption, cx, cy + round(37 * scale),
                   _font(16 * scale, "display"), color, scale)
    image.alpha_composite(layer)


def frame_innings_card(image, theme=DIWALI):
    """Surround an intact innings card with the same art as its match summary."""
    key = normalize_theme(theme)
    if key == NORMAL:
        return image.copy()
    palette = theme_definition(key)
    scale = 2
    pad, top, footer = 36, 96, SUMMARY_FOOTER_H
    width, height = image.width + pad * 2, image.height + top + footer
    framed = Image.new("RGBA", (width * scale, height * scale))
    draw_theme_background(framed, key, scale=scale, footer_height=footer, header_height=top)
    framed.alpha_composite(image.convert("RGBA").resize(
        (image.width * scale, image.height * scale), Image.Resampling.LANCZOS),
        (pad * scale, top * scale))
    draw_theme_panel(framed, (pad - 2, top - 2, width - pad + 2,
                             top + image.height + 2), key, scale=scale, radius=24)
    draw = ImageDraw.Draw(framed)
    has_footer_title = key in (NEW_YEAR, SANKRANTI, REPUBLIC_DAY)
    header = palette.edition if has_footer_title else palette.greeting
    _centred_text(draw, header, width * scale // 2, 50 * scale,
                   _font((30 if has_footer_title else 42) * scale,
                         "display" if has_footer_title else "script"), palette.ink, scale)
    draw_theme_footer(framed, key, scale=scale, height=footer)
    return framed.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)


def draw_base_innings_background(image):
    """An opaque stadium plate behind the standard innings card's glass panels."""
    backdrop = Image.new("RGBA", image.size)
    draw_theme_background(backdrop, NORMAL, header_height=190, footer_height=28)
    image.paste(backdrop.convert("RGB"))


def finish_base_innings_card(image, *, bowling=False):
    """Use the same brushed-metal rim as the broadcast match summary."""
    result = image.convert("RGBA")
    inset, radius = (14, 24) if bowling else (22, 30)
    draw_theme_panel(result, (inset, inset, image.width - inset, image.height - inset),
                     NORMAL, radius=radius, snow=False)
    return result.convert("RGB")


# Compatibility for older imports; new callers pass an explicit theme.
def draw_diwali_background(image, *, scale=1, footer_height=SUMMARY_FOOTER_H):
    draw_theme_background(image, DIWALI, scale=scale, footer_height=footer_height)


def draw_diwali_footer(image, *, scale=1, height=SUMMARY_FOOTER_H):
    draw_theme_footer(image, DIWALI, scale=scale, height=height)
