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


# ── Template card (admin-uploaded background + image-map layout) ─────────────

# Default text colour for the template card — dark green to suit the cream/green
# reference template. Reads well on light backgrounds.
_TEMPLATE_TEXT_COLOR = (20, 75, 55)


def _player_field_text(player, field):
    """Resolve the display string for a template-card field key."""
    if field == "name":
        return str(player.name).upper()
    if field == "rating":
        return str(int(player.rating))
    if field == "country":
        return str(player.country)
    if field == "bat_style":
        return f"{player.bat_hand}-hand Bat"
    if field == "bowl_style":
        hand = str(getattr(player, "bowl_hand", "") or "").strip()
        style = str(getattr(player, "bowl_style", "") or "").strip()
        return f"{hand}-arm {style}".strip(" -") if hand else style
    if field == "bat_rating":
        return str(int(player.bat_rating))
    if field == "bowl_rating":
        return str(int(player.bowl_rating))
    return ""


def _fit_font(text, max_w, max_h, bold=True):
    """Pick the largest DejaVu font whose rendered text fits within max_w/max_h."""
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lo, hi, best = 6, max(8, int(max_h)), None
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _font(mid, bold=bold)
        bbox = measure.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= max_w and th <= max_h:
            best = font
            lo = mid + 1
        else:
            hi = mid - 1
    return best or _font(6, bold=bold)


def _draw_text_in_box(draw, bbox, text, color, bold=True):
    """Draw text centred within bbox, auto-sized to fit."""
    if not text:
        return
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    # Pad each axis by a small fraction of *that* axis so a wide-but-short box
    # (e.g. a country strip) keeps usable height instead of being crushed.
    pad_x = max(2, int(bw * 0.04))
    pad_y = max(2, int(bh * 0.06))
    max_w, max_h = bw - 2 * pad_x, bh - 2 * pad_y
    if max_w <= 0 or max_h <= 0:
        return
    font = _fit_font(text, max_w, max_h, bold=bold)
    tb = draw.textbbox((0, 0), text, font=font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    tx = x0 + (x1 - x0 - tw) // 2 - tb[0]
    ty = y0 + (y1 - y0 - th) // 2 - tb[1]
    draw.text((tx, ty), text, fill=color, font=font)


def _composite_portrait(base, bbox, player):
    """Cover-fit the player's portrait into bbox on the base RGBA image."""
    try:
        from services.player_portrait_service import get_player_portrait
        portrait = get_player_portrait(player)
    except Exception:
        portrait = None
    if portrait is None:
        return
    x0, y0, x1, y1 = bbox
    bw, bh = x1 - x0, y1 - y0
    if bw <= 0 or bh <= 0:
        return
    pw, ph = portrait.size
    scale = max(bw / pw, bh / ph)
    nw, nh = max(1, int(pw * scale)), max(1, int(ph * scale))
    resized = portrait.resize((nw, nh), Image.LANCZOS)
    # Centre-crop to the box
    left = (nw - bw) // 2
    top = (nh - bh) // 2
    cropped = resized.crop((left, top, left + bw, top + bh))
    base.paste(cropped, (x0, y0), cropped)


def generate_template_card(player) -> bytes | None:
    """Render a card onto the admin-uploaded template at image-map regions.

    Returns None when no usable template image is configured (the caller then
    falls back to the procedural tier card). Cached by player_id; cache is
    cleared via invalidate_template_card_cache() when the admin edits the
    template config.
    """
    try:
        from services.card_template_service import get_template_config
        tcfg = get_template_config()
    except Exception:
        logger.exception("template config load failed")
        return None

    image_path = tcfg.get("image_path")
    if not image_path:
        return None  # No template uploaded — fall back to tier card

    cached = _TEMPLATE_CARD_CACHE.get(player.id)
    if cached is not None:
        return cached

    try:
        base = Image.open(image_path).convert("RGBA")
        regions = tcfg.get("regions") or []
        show_portrait = tcfg.get("show_portrait", True)

        # Composite portrait first so text draws on top.
        if show_portrait:
            for r in regions:
                if r["field"] == "__photo__":
                    _composite_portrait(base, r["bbox"], player)

        draw = ImageDraw.Draw(base)
        for r in regions:
            field = r["field"]
            if field == "__photo__":
                continue
            text = _player_field_text(player, field)
            _draw_text_in_box(draw, r["bbox"], text, _TEMPLATE_TEXT_COLOR, bold=True)

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

    When the global card style is set to "template", renders onto the
    admin-uploaded template image; if no template is configured the procedural
    tier card is used instead.

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

    # Template card style — render on admin-uploaded template if active.
    try:
        from services.config_service import get_card_style
        if get_card_style() == "template":
            tpl = generate_template_card(player)
            if tpl is not None:
                return tpl
            # else: no template configured → fall through to tier card
    except Exception:
        logger.exception("template card dispatch failed; using tier card")

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
# the template image or image-map code (in addition to per-player edits).
_TEMPLATE_CARD_CACHE = {}


def invalidate_card_cache(player_id=None):
    """Drop cached generated cards. Call after admin edits a player.
    If player_id is None, drops all."""
    if player_id is None:
        _CARD_CACHE.clear()
    else:
        _CARD_CACHE.pop(player_id, None)


def invalidate_template_card_cache(player_id=None):
    """Drop cached template-rendered cards. Call after admin edits the template
    image, image-map code, or card style. If player_id is None, drops all."""
    if player_id is None:
        _TEMPLATE_CARD_CACHE.clear()
    else:
        _TEMPLATE_CARD_CACHE.pop(player_id, None)
