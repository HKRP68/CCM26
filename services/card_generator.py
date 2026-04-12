"""Generate premium horizontal player card images with Pillow."""

import io
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

W, H = 700, 420
BORDER = 3

# ── Tier colours ────────────────────────────────────────────────────
TIERS = {
    "ultimate":  {"border": "#ffd700", "bg1": "#1a1a1a", "bg2": "#000000", "accent": "#ffd700", "label": "ULTIMATE LEGEND"},
    "legend":    {"border": "#6366f1", "bg1": "#0f172a", "bg2": "#1e1b4b", "accent": "#818cf8", "label": "LEGEND"},
    "elite":     {"border": "#ef4444", "bg1": "#1a0505", "bg2": "#2d0a0a", "accent": "#f87171", "label": "ELITE"},
    "rare":      {"border": "#10b981", "bg1": "#052e16", "bg2": "#064e3b", "accent": "#34d399", "label": "RARE"},
    "super":     {"border": "#3b82f6", "bg1": "#0c1629", "bg2": "#1e3a5f", "accent": "#60a5fa", "label": "SUPER"},
    "common":    {"border": "#06b6d4", "bg1": "#0c1a1f", "bg2": "#164e63", "accent": "#22d3ee", "label": "COMMON"},
    "silver":    {"border": "#94a3b8", "bg1": "#1e293b", "bg2": "#334155", "accent": "#cbd5e1", "label": "SILVER"},
    "bronze":    {"border": "#92400e", "bg1": "#422006", "bg2": "#451a03", "accent": "#92400e", "label": "BRONZE"},
}

def _get_tier(rating):
    if rating >= 95: return TIERS["ultimate"]
    if rating >= 90: return TIERS["legend"]
    if rating >= 85: return TIERS["elite"]
    if rating >= 80: return TIERS["rare"]
    if rating >= 75: return TIERS["super"]
    if rating >= 70: return TIERS["common"]
    if rating >= 60: return TIERS["silver"]
    return TIERS["bronze"]


# ── Country codes for flag ──────────────────────────────────────────
COUNTRY_CODES = {
    "India": "IND", "Australia": "AUS", "England": "ENG", "Pakistan": "PAK",
    "South Africa": "SA", "New Zealand": "NZ", "Sri Lanka": "SL",
    "Bangladesh": "BAN", "Afghanistan": "AFG", "West Indies": "WI",
    "Zimbabwe": "ZIM", "Ireland": "IRE", "Netherlands": "NED",
    "Scotland": "SCO", "UAE": "UAE", "Nepal": "NEP", "USA": "USA",
    "Canada": "CAN", "Kenya": "KEN", "Namibia": "NAM", "Oman": "OMN",
    "Italy": "ITA", "Germany": "GER", "Japan": "JPN", "China": "CHN",
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


def _hex_polygon(w, h, cut=0.04):
    """Hexagonal card shape points."""
    cx = int(w * cut)
    cy = int(h * 0.12)
    return [
        (cx, 0), (w - cx, 0), (w, cy), (w, h - cy),
        (w - cx, h), (cx, h), (0, h - cy), (0, cy),
    ]


def generate_card(player) -> bytes | None:
    """Generate a premium horizontal card PNG. Returns bytes or None."""
    try:
        # Read all attributes while player is accessible
        name = player.name
        rating = player.rating
        category = player.category
        country = player.country
        bat_hand = player.bat_hand
        bowl_style = player.bowl_style
        bat_rating = player.bat_rating
        bowl_rating = player.bowl_rating

        tier = _get_tier(rating)
        border_col = tier["border"]
        bg1 = tier["bg1"]
        accent = tier["accent"]
        label = tier["label"]

        # ── Create canvas ───────────────────────────────────────────
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Outer shape (border colour)
        poly = _hex_polygon(W, H)
        draw.polygon(poly, fill=border_col)

        # Inner shape (dark background) — inset by BORDER
        inner_poly = _hex_polygon(W - BORDER * 2, H - BORDER * 2)
        inner_poly = [(x + BORDER, y + BORDER) for x, y in inner_poly]
        draw.polygon(inner_poly, fill=bg1)

        # Subtle gradient overlay
        for y in range(H):
            alpha = int(30 * (y / H))
            draw.line([(BORDER, y), (W - BORDER, y)],
                      fill=(255, 255, 255, alpha))

        # ── LEFT SECTION: Rating + Country ──────────────────────────
        lx = 55  # left margin

        # Large OVR number
        f_ovr = _font(90, bold=True)
        draw.text((lx, 60), str(rating), fill="#ffffff", font=f_ovr)

        # "OVR" label
        f_ovr_label = _font(14, bold=True)
        draw.text((lx + 2, 155), "OVR", fill=(255, 255, 255, 150), font=f_ovr_label)

        # Country code badge
        cc = COUNTRY_CODES.get(country, country[:3].upper())
        f_cc = _font(16, bold=True)

        # Country badge background
        badge_y = 200
        badge_w = 70
        badge_h = 32
        draw.rounded_rectangle(
            [lx, badge_y, lx + badge_w, badge_y + badge_h],
            radius=4, fill=accent)
        # Center text in badge
        bbox = draw.textbbox((0, 0), cc, font=f_cc)
        tw = bbox[2] - bbox[0]
        draw.text((lx + (badge_w - tw) // 2, badge_y + 6), cc, fill="#000000", font=f_cc)

        # Tier label
        f_tier = _font(10, bold=True)
        draw.text((lx, 250), label, fill=accent, font=f_tier)

        # ── RIGHT SECTION: Name + Ratings + Style ───────────────────
        rx = 210  # right section start

        # Player name (large, uppercase)
        f_name = _font(36, bold=True)
        # Truncate if too long
        display_name = name.upper()
        if len(display_name) > 18:
            display_name = display_name[:17] + "…"
        draw.text((rx, 35), display_name, fill="#ffffff", font=f_name)

        # Category
        f_cat = _font(11, bold=True)
        draw.text((rx, 80), category.upper(), fill=accent, font=f_cat)

        # ── Batting / Bowling ratings (large numbers) ───────────────
        rating_y = 120

        # Batting
        f_rating_num = _font(42, bold=True)
        f_rating_label = _font(10, bold=True)
        draw.text((rx, rating_y), str(bat_rating), fill="#ffffff", font=f_rating_num)
        draw.text((rx, rating_y + 48), "BATTING", fill=(255, 255, 255, 120), font=f_rating_label)

        # Bowling
        bx = rx + 180
        draw.text((bx, rating_y), str(bowl_rating), fill="#ffffff", font=f_rating_num)
        draw.text((bx, rating_y + 48), "BOWLING", fill=(255, 255, 255, 120), font=f_rating_label)

        # ── Divider line ────────────────────────────────────────────
        div_y = 240
        draw.line([(rx, div_y), (W - 50, div_y)], fill=(255, 255, 255, 30), width=1)

        # ── Style info ──────────────────────────────────────────────
        f_label = _font(10, bold=True)
        f_val = _font(12, bold=True)

        row_y = div_y + 15
        # Batting Style
        draw.text((rx, row_y), "BATTING STYLE", fill=(255, 255, 255, 100), font=f_label)
        draw.text((rx + 280, row_y), f"{bat_hand}-hand bat", fill="#ffffff", font=f_val)

        row_y += 30
        # Bowling Style
        draw.text((rx, row_y), "BOWLING STYLE", fill=(255, 255, 255, 100), font=f_label)
        draw.text((rx + 280, row_y), bowl_style, fill="#ffffff", font=f_val)

        row_y += 30
        # Country
        draw.text((rx, row_y), "COUNTRY", fill=(255, 255, 255, 100), font=f_label)
        draw.text((rx + 280, row_y), country, fill="#ffffff", font=f_val)

        # ── Convert to RGB and export ───────────────────────────────
        final = Image.new("RGB", (W, H), (5, 5, 5))
        final.paste(img, (0, 0), img)

        buf = io.BytesIO()
        final.save(buf, format="PNG", quality=95)
        buf.seek(0)
        return buf.getvalue()

    except Exception:
        logger.exception("Card generation failed")
        return None
