"""Legacy "standard" cricket player-card generator.

This is the reference-inspired 1024×640 card design that pre-dated the tier
card. It is offered as a selectable card style ("standard") via the admin Card
Template page. Kept self-contained — its own fonts, colours and cache — so it
never clashes with the current tier renderer in services/card_generator.py.

The custom-per-player-image override is handled by the dispatcher in
services/card_generator.generate_card(); this module only renders the
procedural standard card.
"""

import io
import logging

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

W, H = 1024, 640
GREEN = (0, 82, 53)
GREEN_2 = (57, 154, 86)
PALE_GREEN = (215, 238, 205)
CREAM = (250, 247, 238)
RED = (239, 37, 66)
INK = (3, 74, 48)

COUNTRY_CODES = {
    "India": "IND", "Australia": "AUS", "England": "ENG", "Pakistan": "PAK",
    "South Africa": "SA", "New Zealand": "NZ", "Sri Lanka": "SL",
    "Bangladesh": "BAN", "Afghanistan": "AFG", "West Indies": "WI",
    "Zimbabwe": "ZIM", "Ireland": "IRE", "Netherlands": "NED",
    "Scotland": "SCO", "UAE": "UAE", "Nepal": "NEP", "USA": "USA",
    "Canada": "CAN", "Italy": "ITA", "Oman": "OMN", "Namibia": "NAM",
}


def _font(size, bold=False):
    path = ("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"
            if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        # Fall back to the regular DejaVu faces, then Pillow's default.
        alt = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
               if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        try:
            return ImageFont.truetype(alt, size)
        except OSError:
            return ImageFont.load_default()


def _fit_font(draw, text, max_width, start_size, min_size=18):
    for size in range(start_size, min_size - 1, -2):
        font = _font(size, bold=True)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _font(min_size, bold=True)


def _outlined_polygon(draw, points, fill, outline=GREEN_2, width=3):
    draw.polygon(points, fill=fill)
    draw.line(points + [points[0]], fill=outline, width=width, joint="curve")


def _draw_background(draw):
    draw.rectangle((0, 0, W, H), fill=CREAM)
    # Diagonal bands and subtle field-like stripes on the right.
    for offset in range(-120, 650, 44):
        draw.polygon([(560 + offset, H), (610 + offset, H), (W, 140 + offset), (W, 95 + offset)],
                     fill=(238, 243, 221))
    for offset in range(0, 650, 72):
        draw.polygon([(710 + offset, H), (742 + offset, H), (W, 380 + offset), (W, 348 + offset)],
                     fill=PALE_GREEN)
    # Reference-style clipped frame.
    frame = [(26, 8), (W - 34, 8), (W - 8, 34), (W - 8, H - 42),
             (W - 34, H - 10), (30, H - 10), (8, H - 34), (8, 34)]
    draw.line(frame + [frame[0]], fill=GREEN_2, width=5, joint="curve")
    inset = [(34, 18), (W - 42, 18), (W - 18, 42), (W - 18, H - 50),
             (W - 42, H - 20), (38, H - 20), (18, H - 42), (18, 42)]
    draw.line(inset + [inset[0]], fill=(121, 193, 132), width=2, joint="curve")


def _draw_halo(layer):
    """Paint the soft green-and-gold glow used behind portrait artwork."""
    halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(halo)
    draw.ellipse((570, 34, 1028, 660), fill=(111, 191, 78, 64))
    draw.ellipse((634, 82, 988, 626), fill=(255, 213, 72, 44))
    halo = halo.filter(ImageFilter.GaussianBlur(34))
    layer.alpha_composite(halo)


def _draw_silhouette(layer):
    """Draw the bowler shadow used when no portrait exists."""
    _draw_halo(layer)
    draw = ImageDraw.Draw(layer)
    shadow = (30, 58, 33, 220)
    shadow_soft = (42, 74, 46, 205)
    # Full-height bowler in a raised-arm action.
    draw.ellipse((735, 82, 837, 190), fill=shadow)
    draw.rounded_rectangle((774, 176, 806, 224), radius=8, fill=shadow)
    draw.polygon([(650, 216), (625, 354), (670, 358), (686, 278),
                  (692, 340), (842, 340), (842, 278), (862, 358),
                  (907, 354), (883, 216), (824, 198), (768, 195), (708, 200)], fill=shadow_soft)
    draw.polygon([(662, 229), (606, 185), (542, 112), (516, 124),
                  (582, 208), (646, 254)], fill=shadow)
    draw.polygon([(864, 232), (913, 285), (958, 380), (932, 398),
                  (890, 310), (842, 252)], fill=shadow)
    draw.polygon([(692, 336), (677, 432), (652, 604), (689, 610),
                  (728, 440), (742, 342)], fill=shadow_soft)
    draw.polygon([(796, 338), (816, 442), (866, 602), (903, 594),
                  (862, 434), (842, 338)], fill=shadow)
    draw.ellipse((638, 594, 700, 620), fill=(30, 58, 33, 175))
    draw.ellipse((854, 584, 916, 612), fill=(30, 58, 33, 175))
    draw.ellipse((622, 612, 930, 638), fill=(0, 0, 0, 42))


def _player_layer(player):
    """Return a full-card portrait layer or the silhouette fallback."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    try:
        from services.player_portrait_service import get_player_portrait
        portrait = get_player_portrait(player)
    except Exception:
        logger.exception("Could not load player portrait")
        portrait = None

    if portrait is None:
        _draw_silhouette(layer)
        return layer

    _draw_halo(layer)
    # Keep the entire portrait visible while anchoring it to the bottom-right.
    max_w, max_h = 510, 585
    portrait.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    x = W - portrait.width - 42
    y = H - portrait.height - 14
    layer.alpha_composite(portrait, (x, y))
    return layer


def _draw_label_box(draw, xy, value, label, icon):
    x, y, width, height = xy
    points = [(x + 18, y), (x + width - 14, y), (x + width, y + 16),
              (x + width, y + height - 16), (x + width - 16, y + height),
              (x + 16, y + height), (x, y + height - 16), (x, y + 16)]
    _outlined_polygon(draw, points, (255, 253, 246, 245), (87, 168, 102), 3)
    draw.text((x + 25, y + 12), str(value), font=_font(58, True), fill=RED)
    draw.text((x + width - 58, y + 25), icon, font=_font(30, True), fill=INK)
    draw.text((x + 24, y + height - 36), label, font=_font(20, True), fill=INK)


def _split_name(draw, name):
    words = name.upper().split()
    if not words:
        return ["PLAYER"]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        font = _fit_font(draw, candidate, 540, 76, 42)
        if draw.textbbox((0, 0), candidate, font=font)[2] > 540 or len(candidate) > 17:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines[:2]


def generate_standard_card(player) -> bytes | None:
    """Create the standard card using an uploaded portrait or bowler shadow."""
    cached = _LEGACY_CARD_CACHE.get(player.id)
    if cached is not None:
        return cached

    try:
        name = str(player.name)
        rating = int(player.rating)
        category = str(player.category)
        country = str(player.country)
        bat_hand = str(player.bat_hand)
        bowl_hand = str(player.bowl_hand)
        bowl_style = str(player.bowl_style)
        bat_rating = int(player.bat_rating)
        bowl_rating = int(player.bowl_rating)

        img = Image.new("RGBA", (W, H), CREAM + (255,))
        draw = ImageDraw.Draw(img)
        _draw_background(draw)

        # Status banner.
        banner = [(88, 48), (432, 48), (454, 70), (454, 94), (432, 116),
                  (88, 116), (70, 98), (70, 66)]
        _outlined_polygon(draw, banner, (250, 248, 239), GREEN_2, 3)
        version = str(getattr(player, "version", "") or "Base card").upper()
        version_font = _fit_font(draw, version, 310, 34, 22)
        draw.text((102, 65), version, font=version_font, fill=INK)

        # Name, role and dividing accent line.
        lines = _split_name(draw, name)
        y = 142
        for line in lines:
            font = _fit_font(draw, line, 560, 78, 40)
            draw.text((48, y), line, font=font, fill=INK, stroke_width=1)
            y += font.size - 4
        role_y = min(y + 8, 324)
        draw.line((48, role_y - 10, 430, role_y - 10), fill=GREEN_2, width=3)
        draw.line((430, role_y - 10, 520, role_y - 10), fill=(255, 164, 40), width=3)
        draw.text((48, role_y), "  ".join(category.upper()), font=_font(25, True), fill=GREEN_2)

        # OVR block.
        draw.text((866, 54), "OVR", font=_font(25, True), fill=INK)
        rating_font = _fit_font(draw, str(rating), 150, 94, 62)
        draw.text((844, 84), str(rating), font=rating_font, fill=RED)
        draw.line((850, 190, 895, 190), fill=INK, width=2)
        draw.text((905, 172), "★", font=_font(30, True), fill=INK)
        draw.line((943, 190, 986, 190), fill=INK, width=2)

        # Portrait or shadow lives above the background but beneath information panels.
        img.alpha_composite(_player_layer(player))
        draw = ImageDraw.Draw(img)

        _draw_label_box(draw, (44, 376, 210, 118), bat_rating, "BATTING POWER", "◆")
        _draw_label_box(draw, (272, 376, 210, 118), bowl_rating, "BOWLING SPECS", "●")

        cc = COUNTRY_CODES.get(country, country[:3].upper())
        draw.text((54, 540), country.upper(), font=_font(35, True), fill=INK)
        draw.line((54, 590, 182, 590), fill=GREEN_2, width=2)
        draw.text((192, 569), f"★  {cc}", font=_font(20, True), fill=INK)

        # Bottom-right style panel overlays portrait like the supplied reference.
        panel = [(670, 520), (W - 20, 520), (W - 20, H - 22), (610, H - 22)]
        _outlined_polygon(draw, panel, (252, 250, 242, 238), (87, 168, 102), 4)
        draw.text((706, 542), f"{bat_hand.upper()}-HANDED", font=_font(23, True), fill=INK)
        draw.line((686, 579, 982, 579), fill=(87, 168, 102), width=2)
        draw.text((706, 592), f"{bowl_hand.upper()} ARM {bowl_style.upper()}", font=_font(19, True), fill=INK)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        result = buf.getvalue()
        if len(_LEGACY_CARD_CACHE) > 500:
            _LEGACY_CARD_CACHE.clear()
        _LEGACY_CARD_CACHE[player.id] = result
        return result
    except Exception:
        logger.exception("Standard card generation failed")
        return None


# Module-level cache for the standard card. Invalidated when admin edits a
# player or switches the card style.
_LEGACY_CARD_CACHE = {}


def invalidate_legacy_card_cache(player_id=None):
    """Drop cached standard cards. If player_id is None, drops all."""
    if player_id is None:
        _LEGACY_CARD_CACHE.clear()
    else:
        _LEGACY_CARD_CACHE.pop(player_id, None)
