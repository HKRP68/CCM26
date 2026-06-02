"""Premium player card generator matching reference design."""

import io
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

W, H = 700, 420

# ── Tier definitions from reference images ──────────────────────────
TIERS = {
    "ultimate": {  # 95-100: Gold/dark olive
        "bg": (35, 35, 18), "bg2": (22, 22, 8),
        "border": (184, 150, 11), "accent": (255, 215, 0),
        "cat_col": (200, 170, 50),
    },
    "legend": {  # 90-94: Dark navy/indigo
        "bg": (15, 23, 42), "bg2": (30, 27, 75),
        "border": (67, 56, 202), "accent": (129, 140, 248),
        "cat_col": (129, 140, 248),
    },
    "epic": {  # 85-89: Purple
        "bg": (75, 30, 150), "bg2": (55, 20, 120),
        "border": (124, 58, 237), "accent": (167, 139, 250),
        "cat_col": (190, 170, 240),
    },
    "rare": {  # 80-84: Chocolate/amber
        "bg": (72, 40, 12), "bg2": (50, 26, 8),
        "border": (184, 134, 11), "accent": (218, 165, 32),
        "cat_col": (218, 165, 32),
    },
    "super": {  # 75-79: Dark brown/orange
        "bg": (62, 33, 10), "bg2": (42, 22, 6),
        "border": (160, 100, 30), "accent": (184, 115, 51),
        "cat_col": (200, 150, 60),
    },
    "silver": {  # 60-74: Steel/slate
        "bg": (55, 65, 81), "bg2": (31, 41, 55),
        "border": (107, 114, 128), "accent": (148, 163, 184),
        "cat_col": (148, 163, 184),
    },
    "bronze": {  # 50-59: Bronze/dark brown
        "bg": (52, 28, 8), "bg2": (35, 18, 4),
        "border": (120, 85, 20), "accent": (139, 105, 20),
        "cat_col": (160, 120, 40),
    },
}


def _get_tier(rating):
    if rating >= 95: return TIERS["ultimate"]
    if rating >= 90: return TIERS["legend"]
    if rating >= 85: return TIERS["epic"]
    if rating >= 80: return TIERS["rare"]
    if rating >= 75: return TIERS["super"]
    if rating >= 60: return TIERS["silver"]
    return TIERS["bronze"]


COUNTRY_CODES = {
    "India": "IND", "Australia": "AUS", "England": "ENG", "Pakistan": "PAK",
    "South Africa": "SA", "New Zealand": "NZ", "Sri Lanka": "SL",
    "Bangladesh": "BAN", "Afghanistan": "AFG", "West Indies": "WI",
    "Zimbabwe": "ZIM", "Ireland": "IRE", "Netherlands": "NED",
    "Scotland": "SCO", "UAE": "UAE", "Nepal": "NEP", "USA": "USA",
    "Canada": "CAN", "Italy": "ITA", "Oman": "OMN", "Namibia": "NAM",
}


def _font(size, bold=False):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_corner_brackets(draw, w, h, color, dot_color):
    """Draw L-shaped corner brackets with dots like the reference."""
    blen = 35  # bracket arm length
    bw = 2     # bracket line width
    m = 18     # margin from edge
    dc = 4     # dot circle radius

    # Top-left
    draw.line([(m, m), (m + blen, m)], fill=color, width=bw)
    draw.line([(m, m), (m, m + blen)], fill=color, width=bw)
    draw.ellipse([m - dc, m - dc, m + dc, m + dc], fill=dot_color)

    # Top-right
    draw.line([(w - m, m), (w - m - blen, m)], fill=color, width=bw)
    draw.line([(w - m, m), (w - m, m + blen)], fill=color, width=bw)
    draw.ellipse([w - m - dc, m - dc, w - m + dc, m + dc], fill=dot_color)

    # Bottom-left
    draw.line([(m, h - m), (m + blen, h - m)], fill=color, width=bw)
    draw.line([(m, h - m), (m, h - m - blen)], fill=color, width=bw)
    draw.ellipse([m - dc, h - m - dc, m + dc, h - m + dc], fill=dot_color)

    # Bottom-right
    draw.line([(w - m, h - m), (w - m - blen, h - m)], fill=color, width=bw)
    draw.line([(w - m, h - m), (w - m, h - m - blen)], fill=color, width=bw)
    draw.ellipse([w - m - dc, h - m - dc, w - m + dc, h - m + dc], fill=dot_color)


def _hex_shape(w, h, cut_x=28, cut_y=50):
    """Card outline polygon — slightly clipped corners."""
    return [
        (cut_x, 0), (w - cut_x, 0),
        (w, cut_y), (w, h - cut_y),
        (w - cut_x, h), (cut_x, h),
        (0, h - cut_y), (0, cut_y),
    ]


def _draw_gradient_text(draw, pos, text, font, top_color, bottom_color):
    """Draw text with a top-to-bottom color gradient."""
    x, y = pos
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = bbox[3] - bbox[1]

    # Create a temporary image for gradient text
    tmp = Image.new("RGBA", (bbox[2] - bbox[0] + 10, text_h + 10), (0, 0, 0, 0))
    tmp_draw = ImageDraw.Draw(tmp)
    tmp_draw.text((0, 0), text, fill=top_color, font=font)

    # Simple gradient: blend top and bottom colors
    for row in range(text_h):
        t = row / max(text_h - 1, 1)
        r = int(top_color[0] * (1 - t) + bottom_color[0] * t)
        g = int(top_color[1] * (1 - t) + bottom_color[1] * t)
        b = int(top_color[2] * (1 - t) + bottom_color[2] * t)
        for col in range(tmp.width):
            px = tmp.getpixel((col, row))
            if px[3] > 0:
                tmp.putpixel((col, row), (r, g, b, px[3]))

    return tmp, (x, y)


# ── Website-managed Base card (ported from v5 HTML generator) ─────────────

_TEMPLATE_W, _TEMPLATE_H = 1536, 1024
_DARK_GREEN = (0, 86, 50, 255)
_LIGHT_GREEN = (92, 174, 88, 255)
_RED = (237, 37, 67, 255)
_LINE_GREEN = (124, 178, 145, 255)


def _template_font(tcfg, size):
    """Use the admin-uploaded font, with a condensed bold fallback."""
    paths = [
        tcfg.get("font_path"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-BoldOblique.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for path in paths:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, max(1, int(size)))
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _fit_template_font(draw, tcfg, text, max_width, start_size, min_size):
    size = int(start_size)
    while size > int(min_size):
        font = _template_font(tcfg, size)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
        size -= 2
    return _template_font(tcfg, min_size)


def _draw_fit(draw, tcfg, text, xy, max_width, start_size, min_size, color,
              anchor="la"):
    text = str(text or "")
    font = _fit_template_font(draw, tcfg, text, max_width, start_size, min_size)
    draw.text(xy, text, font=font, fill=color, anchor=anchor)


def _split_template_name(name):
    parts = str(name or "").strip().upper().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _trim_transparent(portrait):
    """Match the HTML auto-trim control by dropping transparent PNG padding."""
    alpha = portrait.getchannel("A")
    bbox = alpha.getbbox()
    return portrait.crop(bbox) if bbox else portrait


def _composite_template_portrait(base, player, settings):
    """Draw the same player-page cutout used by the HTML generator."""
    try:
        from services.player_portrait_service import get_player_portrait
        portrait = get_player_portrait(player)
    except Exception:
        portrait = None
    if portrait is None:
        return
    if settings["trim_transparent"]:
        portrait = _trim_transparent(portrait)
    box_w, box_h = settings["player_w"], settings["player_h"]
    scale = min(box_w / portrait.width, box_h / portrait.height)
    scale *= settings["player_scale"] / 100
    width, height = max(1, int(portrait.width * scale)), max(1, int(portrait.height * scale))
    portrait = portrait.resize((width, height), Image.LANCZOS)
    if settings["player_opacity"] < 100:
        alpha = portrait.getchannel("A").point(
            lambda value: int(value * settings["player_opacity"] / 100))
        portrait.putalpha(alpha)
    x = int(settings["player_x"] + (box_w - width) / 2)
    y = int(settings["player_y"] + box_h - height)
    base.alpha_composite(portrait, (x, y))


def _restore_bottom_box(base, clean_template):
    """Redraw the v5 bottom-right details polygon above the cutout."""
    mask = Image.new("L", (_TEMPLATE_W, _TEMPLATE_H), 0)
    ImageDraw.Draw(mask).polygon([(1032, 833), (1536, 833), (1536, 1024),
                                  (845, 1024)], fill=255)
    base.paste(clean_template, (0, 0), mask)


def _draw_india_flag(draw, bbox):
    x, y, width, height = bbox
    stripe = height / 3
    draw.rectangle((x, y, x + width, y + stripe), fill=(255, 153, 51, 255))
    draw.rectangle((x, y + stripe, x + width, y + 2 * stripe), fill=(255, 255, 255, 255))
    draw.rectangle((x, y + 2 * stripe, x + width, y + height), fill=(19, 136, 8, 255))
    cx, cy, radius = x + width / 2, y + height / 2, max(8, height / 8)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                 outline=(16, 47, 90, 255), width=3)


def _draw_template_flag(base, draw, tcfg, country, settings):
    """Draw a website-uploaded country PNG at the same flag size as v5."""
    frame_x, frame_y, frame_w, frame_h = 66, 846, 280, 122
    scale = settings["flag_scale"] / 100
    width, height = max(1, int(frame_w * scale)), max(1, int(frame_h * scale))
    x = int(frame_x + (frame_w - width) / 2)
    y = int(frame_y + (frame_h - height) / 2 + settings["flag_y_offset"])
    try:
        from services.country_flag_service import get_country_flag
        flag = get_country_flag(country)
    except Exception:
        flag = None
    if flag is not None:
        base.alpha_composite(flag.resize((width, height), Image.LANCZOS), (x, y))
    elif str(country).strip().lower() == "india":
        _draw_india_flag(draw, (x, y, width, height))
    else:
        draw.rectangle((x, y, x + width, y + height), outline=_DARK_GREEN, width=3)
        _draw_fit(draw, tcfg, str(country).upper(),
                  (x + width / 2, y + height / 2), width - 20, 30, 16,
                  _DARK_GREEN, anchor="mm")


def _template_batting_style(player):
    return f"{str(getattr(player, 'bat_hand', '') or '').upper()}-HANDED"


def _template_bowling_style(player):
    hand = str(getattr(player, "bowl_hand", "") or "").upper()
    style = str(getattr(player, "bowl_style", "") or "").upper()
    return f"{hand} ARM {style}".strip()


def generate_template_card(player) -> bytes | None:
    """Render a Base card using the website-managed v5 HTML layer order."""
    if getattr(player, "parent_player_id", None):
        return None
    try:
        from services.card_template_service import get_template_config
        tcfg = get_template_config()
    except Exception:
        logger.exception("template config load failed")
        return None
    if not tcfg.get("image_path"):
        return None
    cached = _TEMPLATE_CARD_CACHE.get(player.id)
    if cached is not None:
        return cached
    try:
        clean_template = Image.open(tcfg["image_path"]).convert("RGBA")
        clean_template = clean_template.resize((_TEMPLATE_W, _TEMPLATE_H), Image.LANCZOS)
        base = clean_template.copy()
        settings = tcfg["settings"]
        if tcfg.get("show_portrait", True):
            _composite_template_portrait(base, player, settings)
        if settings["protect_bottom_box"]:
            _restore_bottom_box(base, clean_template)

        draw = ImageDraw.Draw(base)
        first_name, last_name = _split_template_name(player.name)
        _draw_fit(draw, tcfg, first_name, (settings["name_x"], settings["name_y"] + 110),
                  650, 140, 72, _DARK_GREEN)
        if last_name:
            _draw_fit(draw, tcfg, last_name, (settings["name_x"], settings["name_y"] + 260),
                      650, 140, 72, _DARK_GREEN)
        category_x = settings["cat_x"]
        category_font = _template_font(tcfg, settings["cat_font_size"])
        for character in str(player.category).upper():
            draw.text((category_x, settings["cat_y"]), character, font=category_font, fill=_LIGHT_GREEN)
            category_x += draw.textlength(character, font=category_font) + settings["cat_letter_gap"]
        _draw_fit(draw, tcfg, int(player.rating), (settings["ovr_x"], settings["ovr_y"]),
                  240, 128, 72, _RED, anchor="mm")
        _draw_fit(draw, tcfg, int(player.bat_rating), (settings["bat_x"], settings["bat_y"]),
                  150, 92, 48, _RED, anchor="mm")
        _draw_fit(draw, tcfg, int(player.bowl_rating), (settings["bowl_x"], settings["bowl_y"]),
                  150, 92, 48, _RED, anchor="mm")
        _draw_template_flag(base, draw, tcfg, player.country, settings)
        _draw_fit(draw, tcfg, str(player.country).upper(), (405, 925), 280, 58, 30, _DARK_GREEN)
        _draw_fit(draw, tcfg, _template_batting_style(player),
                  (settings["bat_style_x"], settings["bat_style_y"]),
                  settings["style_max_width"], settings["style_font_size"], 20, _DARK_GREEN)
        _draw_fit(draw, tcfg, _template_bowling_style(player),
                  (settings["bowl_style_x"], settings["bowl_style_y"]),
                  settings["style_max_width"], settings["style_font_size"], 20, _DARK_GREEN)
        draw.line((1030, 939, 1442, 939), fill=_LINE_GREEN, width=2)

        buf = io.BytesIO()
        base.convert("RGB").save(buf, format="PNG", quality=95)
        result = buf.getvalue()
        if len(_TEMPLATE_CARD_CACHE) > 500:
            _TEMPLATE_CARD_CACHE.clear()
        _TEMPLATE_CARD_CACHE[player.id] = result
        return result
    except Exception:
        logger.exception("Template card generation failed")
        return None

def generate_card(player) -> bytes | None:
    """Generate a premium card PNG matching the reference design.

    If the admin has uploaded a custom card image for this player and it's
    active, returns those bytes instead. Falls back to the auto-generated
    card otherwise.

    For Base players with an uploaded portrait (or the global fallback PNG),
    renders onto the admin-uploaded website template before falling back to the
    procedural tier card.

    Generated cards are cached in memory by player_id — a player's card art
    doesn't change at runtime, so re-generating it is wasted CPU + memory.
    Cache is invalidated when admin edits a player.
    """
    # Custom image override — short-circuit if admin uploaded one
    try:
        from services.player_image_service import get_custom_image_bytes
        custom = get_custom_image_bytes(player.id)
        if custom:
            return custom
    except Exception:
        pass  # Fall through to auto-generation

    # Player-image website card — use it automatically before the procedural
    # fallback whenever this Base player has an uploaded portrait or the admin
    # configured a global fallback PNG. Custom full-card art remains first.
    try:
        from services.player_portrait_service import has_player_portrait
        if (not getattr(player, "parent_player_id", None)
                and has_player_portrait(player, include_global=True)):
            tpl = generate_template_card(player)
            if tpl is not None:
                return tpl
            # No blank website template configured → fall through to tier card.
    except Exception:
        logger.exception("player-image card dispatch failed; using tier card")

    # Generated-card cache check
    cached = _CARD_CACHE.get(player.id)
    if cached is not None:
        return cached

    try:
        # Read all attributes
        name = str(player.name)
        rating = int(player.rating)
        category = str(player.category)
        country = str(player.country)
        bat_hand = str(player.bat_hand)
        bowl_style = str(player.bowl_style)
        bat_rating = int(player.bat_rating)
        bowl_rating = int(player.bowl_rating)

        tier = _get_tier(rating)

        # ── Canvas ──────────────────────────────────────────────────
        img = Image.new("RGB", (W, H), (5, 5, 5))
        draw = ImageDraw.Draw(img)

        # Background shape with gradient
        shape = _hex_shape(W, H)
        # Draw outer border shape
        draw.polygon(shape, fill=tier["border"])
        # Inner shape (3px inset)
        inner = _hex_shape(W - 6, H - 6, cut_x=26, cut_y=48)
        inner = [(x + 3, y + 3) for x, y in inner]

        # Fill inner with gradient (top to bottom)
        bg1, bg2 = tier["bg"], tier["bg2"]
        for y in range(H):
            t = y / H
            r = int(bg1[0] * (1 - t) + bg2[0] * t)
            g = int(bg1[1] * (1 - t) + bg2[1] * t)
            b = int(bg1[2] * (1 - t) + bg2[2] * t)
            draw.line([(4, y), (W - 4, y)], fill=(r, g, b))

        # Re-draw outer border lines only
        draw.polygon(shape, outline=tier["border"], fill=None)

        # Corner brackets
        _draw_corner_brackets(draw, W, H, tier["border"], tier["accent"])

        # ── LEFT: OVR number (metallic gradient) ────────────────────
        f_ovr = _font(110, bold=True)
        ovr_text = str(rating)

        gradient_top = (220, 220, 220)
        gradient_bot = (140, 140, 140)
        ovr_img, _ = _draw_gradient_text(draw, (0, 0), ovr_text, f_ovr, gradient_top, gradient_bot)
        img.paste(ovr_img, (50, 50), ovr_img)

        # "OVR" label
        f_ovr_label = _font(14, bold=True)
        draw.text((55, 175), "O V R", fill=(255, 255, 255, 140), font=f_ovr_label)

        # Country code badge
        cc = COUNTRY_CODES.get(country, country[:3].upper())
        f_cc = _font(18, bold=True)
        badge_x, badge_y = 50, 220
        draw.rounded_rectangle([badge_x, badge_y, badge_x + 80, badge_y + 45],
                               radius=6, fill=(30, 50, 80))
        draw.rounded_rectangle([badge_x + 1, badge_y + 1, badge_x + 79, badge_y + 44],
                               radius=5, fill=(40, 60, 100))
        bbox = draw.textbbox((0, 0), cc, font=f_cc)
        tw = bbox[2] - bbox[0]
        draw.text((badge_x + (80 - tw) // 2, badge_y + 10), cc, fill=(255, 255, 255), font=f_cc)

        # ── RIGHT: Player name (large, bold, uppercase) ─────────────
        rx = 230
        f_name = _font(52, bold=True)

        # Split name into lines if too long
        words = name.upper().split()
        lines = []
        current = ""
        for w in words:
            test = (current + " " + w).strip()
            bbox = draw.textbbox((0, 0), test, font=f_name)
            if bbox[2] - bbox[0] > 430:
                if current:
                    lines.append(current)
                current = w
            else:
                current = test
        if current:
            lines.append(current)

        name_y = 30
        for line in lines[:2]:  # max 2 lines
            draw.text((rx, name_y), line, fill=(255, 255, 255), font=f_name)
            name_y += 58

        # Category (spaced uppercase)
        cat_spaced = "  ".join(category.upper())
        f_cat = _font(12, bold=True)
        draw.text((rx, name_y + 5), cat_spaced, fill=tier["cat_col"], font=f_cat)

        # ── Batting / Bowling ratings ───────────────────────────────
        rating_y = name_y + 45
        f_rat = _font(50, bold=True)
        f_rat_label = _font(11, bold=True)

        draw.text((rx, rating_y), str(bat_rating), fill=(255, 255, 255), font=f_rat)
        draw.text((rx, rating_y + 56), "B A T T I N G", fill=(255, 255, 255, 120), font=f_rat_label)

        draw.text((rx + 200, rating_y), str(bowl_rating), fill=(255, 255, 255), font=f_rat)
        draw.text((rx + 200, rating_y + 56), "B O W L I N G", fill=(255, 255, 255, 120), font=f_rat_label)

        # ── Divider ─────────────────────────────────────────────────
        div_y = rating_y + 90
        draw.line([(rx, div_y), (W - 45, div_y)], fill=(255, 255, 255, 50), width=1)

        # ── Style info ──────────────────────────────────────────────
        f_label = _font(11, bold=True)
        f_val = _font(13, bold=True)

        row_y = div_y + 18
        draw.text((rx, row_y), "B A T T I N G   S T Y L E", fill=(255, 255, 255, 90), font=f_label)
        draw.text((rx + 300, row_y), f"{bat_hand}-hand bat", fill=(255, 255, 255), font=f_val)

        row_y += 32
        draw.text((rx, row_y), "B O W L I N G   S T Y L E", fill=(255, 255, 255, 90), font=f_label)
        draw.text((rx + 300, row_y), bowl_style, fill=(255, 255, 255), font=f_val)

        # ── Export ──────────────────────────────────────────────────
        buf = io.BytesIO()
        img.save(buf, format="PNG", quality=95)
        buf.seek(0)
        result = buf.getvalue()
        # Cache so we don't regenerate this card. Cap to avoid runaway memory.
        if len(_CARD_CACHE) > 500:
            _CARD_CACHE.clear()
        _CARD_CACHE[player.id] = result
        return result

    except Exception:
        logger.exception("Card generation failed")
        return None


# Module-level cache for generated cards. Player art doesn't change at runtime,
# so we keep up to 500 generated cards in memory. Invalidated when admin edits
# the underlying Player row.
_CARD_CACHE = {}

# Separate cache for template-rendered cards. Invalidated when the admin changes
# the template image, font, layout controls, or the underlying player.
_TEMPLATE_CARD_CACHE = {}


def invalidate_card_cache(player_id=None):
    """Drop procedural and website-template cards after a player edit."""
    if player_id is None:
        _CARD_CACHE.clear()
        _TEMPLATE_CARD_CACHE.clear()
    else:
        _CARD_CACHE.pop(player_id, None)
        _TEMPLATE_CARD_CACHE.pop(player_id, None)


def invalidate_template_card_cache(player_id=None):
    """Drop cached template cards after editing an image, font, or layout."""
    if player_id is None:
        _TEMPLATE_CARD_CACHE.clear()
    else:
        _TEMPLATE_CARD_CACHE.pop(player_id, None)
