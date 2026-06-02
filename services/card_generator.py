"""Reference-inspired cricket player-card generator."""

import io
import logging

from PIL import Image, ImageDraw, ImageFont

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
    """Load a bundled system font without silently shrinking card typography.

    The previous condensed-font paths are not present in the production image,
    which made Pillow fall back to its tiny bitmap default font. DejaVu Sans is
    installed in the container; reference-style condensation is applied while
    drawing instead of depending on an optional condensed font file.
    """
    path = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        logger.warning("Card font %s is missing; using Pillow fallback", path)
        return ImageFont.load_default()


def _text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _fit_font(draw, text, max_width, start_size, min_size=18, width_scale=1.0):
    """Find the largest font size after optional horizontal condensation."""
    for size in range(start_size, min_size - 1, -2):
        font = _font(size, bold=True)
        if _text_width(draw, text, font) * width_scale <= max_width:
            return font
    return _font(min_size, bold=True)


def _draw_condensed_text(image, pos, text, font, fill, width_scale=0.78):
    """Draw large sports-card text compressed horizontally like the reference."""
    scratch_draw = ImageDraw.Draw(image)
    bbox = scratch_draw.textbbox((0, 0), text, font=font, stroke_width=1)
    width = max(1, bbox[2] - bbox[0] + 8)
    height = max(1, bbox[3] - bbox[1] + 8)
    text_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(text_layer).text(
        (4 - bbox[0], 4 - bbox[1]), text, font=font, fill=fill, stroke_width=1,
    )
    condensed_width = max(1, int(text_layer.width * width_scale))
    text_layer = text_layer.resize((condensed_width, text_layer.height), Image.Resampling.LANCZOS)
    image.alpha_composite(text_layer, pos)


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


def _draw_silhouette(layer):
    """Draw a player shadow where an uploaded player portrait will appear."""
    draw = ImageDraw.Draw(layer)
    shadow = (0, 55, 40, 205)
    shadow_dark = (0, 43, 31, 225)
    # Head, neck, shoulders, torso and arms form a strong cricket-player shadow.
    draw.ellipse((690, 85, 835, 230), fill=shadow_dark)
    draw.rounded_rectangle((733, 200, 795, 285), radius=18, fill=shadow_dark)
    draw.polygon([(625, 270), (740, 220), (825, 225), (910, 300),
                  (880, 625), (630, 625)], fill=shadow)
    draw.polygon([(650, 285), (588, 390), (545, 560), (622, 582), (702, 370)], fill=shadow)
    draw.polygon([(888, 295), (955, 410), (987, 570), (910, 590), (835, 370)], fill=shadow_dark)
    # Soft green rim gives the fallback intentional card-art depth.
    draw.line([(622, 580), (645, 302), (742, 235), (824, 238), (906, 306), (916, 586)],
              fill=(102, 184, 121, 190), width=8, joint="curve")


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

    # Keep the entire portrait visible while anchoring it to the bottom-right.
    max_w, max_h = 510, 585
    portrait.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
    x = W - portrait.width - 42
    y = H - portrait.height - 14
    # Light shadow makes transparent cut-outs read cleanly on the pale backdrop.
    alpha = portrait.getchannel("A")
    shadow = Image.new("RGBA", portrait.size, (0, 48, 34, 0))
    shadow.putalpha(alpha.point(lambda value: int(value * 0.35)))
    layer.alpha_composite(shadow, (x + 10, y + 8))
    layer.alpha_composite(portrait, (x, y))
    return layer


def _draw_label_box(image, draw, xy, value, label, icon):
    x, y, width, height = xy
    points = [(x + 18, y), (x + width - 14, y), (x + width, y + 16),
              (x + width, y + height - 16), (x + width - 16, y + height),
              (x + 16, y + height), (x, y + height - 16), (x, y + 16)]
    _outlined_polygon(draw, points, (255, 253, 246, 245), (87, 168, 102), 3)
    draw.text((x + 25, y + 4), str(value), font=_font(68, True), fill=RED)
    draw.text((x + width - 58, y + 25), icon, font=_font(30, True), fill=INK)
    label_font = _fit_font(draw, label, width - 34, 22, 17, width_scale=0.84)
    _draw_condensed_text(image, (x + 18, y + height - 34), label, label_font, INK, 0.84)


def _split_name(draw, name):
    """Stack player names into at most two display lines like the reference."""
    words = name.upper().split()
    if not words:
        return ["PLAYER"]
    if len(words) == 1:
        return words
    if len(words) == 2:
        return words

    # Keep every part of a longer name while reserving the first line for the
    # leading name. The second line automatically scales down only when needed.
    return [words[0], " ".join(words[1:])]


def generate_card(player) -> bytes | None:
    """Return custom full-card art first, otherwise create the standard card.

    The standard card uses an uploaded player portrait when available. Without
    one it intentionally renders a player shadow so the card still looks
    complete and the replacement area is obvious to admins.
    """
    # Highest priority: a complete custom card upload replaces every generated layer.
    try:
        from services.player_image_service import get_custom_image_bytes
        custom = get_custom_image_bytes(player.id)
        if custom:
            return custom
    except Exception:
        logger.exception("Could not load custom card image; generating fallback")

    cached = _CARD_CACHE.get(player.id)
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
        _draw_condensed_text(img, (102, 64), "ACTIVE PLAYER", _font(42, True), INK, 0.82)

        # Name, role and dividing accent line. The reference uses oversized,
        # narrow display lettering that dominates the left half of the card.
        lines = _split_name(draw, name)
        y = 136
        for line in lines:
            font = _fit_font(draw, line, 610, 148, 78, width_scale=0.76)
            _draw_condensed_text(img, (42, y), line, font, INK, 0.76)
            y += 108
        role_y = min(y + 22, 350)
        draw = ImageDraw.Draw(img)
        draw.line((48, role_y - 14, 430, role_y - 14), fill=GREEN_2, width=3)
        draw.line((430, role_y - 14, 540, role_y - 14), fill=(255, 164, 40), width=3)
        role = "  ".join(category.upper())
        _draw_condensed_text(img, (48, role_y), role, _font(31, True), GREEN_2, 0.82)
        draw = ImageDraw.Draw(img)

        # OVR block.
        _draw_condensed_text(img, (864, 50), "OVR", _font(32, True), INK, 0.84)
        rating_font = _fit_font(draw, str(rating), 160, 116, 82, width_scale=0.82)
        _draw_condensed_text(img, (838, 88), str(rating), rating_font, RED, 0.82)
        draw = ImageDraw.Draw(img)
        draw.line((850, 190, 895, 190), fill=INK, width=2)
        draw.text((905, 172), "★", font=_font(30, True), fill=INK)
        draw.line((943, 190, 986, 190), fill=INK, width=2)

        # Portrait or shadow lives above the background but beneath information panels.
        img.alpha_composite(_player_layer(player))
        draw = ImageDraw.Draw(img)

        _draw_label_box(img, draw, (44, 376, 210, 118), bat_rating, "BATTING POWER", "◆")
        _draw_label_box(img, draw, (272, 376, 210, 118), bowl_rating, "BOWLING SPECS", "●")

        cc = COUNTRY_CODES.get(country, country[:3].upper())
        _draw_condensed_text(img, (54, 536), country.upper(), _font(43, True), INK, 0.80)
        draw = ImageDraw.Draw(img)
        draw.line((54, 590, 182, 590), fill=GREEN_2, width=2)
        draw.text((192, 566), f"★  {cc}", font=_font(23, True), fill=INK)

        # Bottom-right style panel overlays portrait like the supplied reference.
        panel = [(670, 520), (W - 20, 520), (W - 20, H - 22), (610, H - 22)]
        _outlined_polygon(draw, panel, (252, 250, 242, 238), (87, 168, 102), 4)
        _draw_condensed_text(img, (706, 540), f"{bat_hand.upper()}-HANDED", _font(27, True), INK, 0.84)
        draw = ImageDraw.Draw(img)
        draw.line((686, 579, 982, 579), fill=(87, 168, 102), width=2)
        _draw_condensed_text(img, (706, 590), f"{bowl_hand.upper()} ARM {bowl_style.upper()}", _font(23, True), INK, 0.84)

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        result = buf.getvalue()
        if len(_CARD_CACHE) > 500:
            _CARD_CACHE.clear()
        _CARD_CACHE[player.id] = result
        return result
    except Exception:
        logger.exception("Card generation failed")
        return None


_CARD_CACHE = {}


def invalidate_card_cache(player_id=None):
    """Drop cached generated cards after player details or portrait changes."""
    if player_id is None:
        _CARD_CACHE.clear()
    else:
        _CARD_CACHE.pop(player_id, None)
