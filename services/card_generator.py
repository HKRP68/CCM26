"""Generate player card images with Pillow."""

import io
import logging
from PIL import Image, ImageDraw, ImageFont

from config import get_tier_colour, get_buy_value, get_sell_value

logger = logging.getLogger(__name__)

W, H = 480, 680
MARGIN = 24


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def generate_card(player) -> bytes | None:
    """Generate a PNG card image for a player. Returns bytes or None on failure."""
    try:
        tier_name, tier_colour, tier_bg = get_tier_colour(player.rating)
        buy_val = get_buy_value(player.rating)
        sell_val = get_sell_value(player.rating)

        img = Image.new("RGB", (W, H), "#1a1a2e")
        draw = ImageDraw.Draw(img)

        f_title = _get_font(22, bold=True)
        f_rating_big = _get_font(52, bold=True)
        f_label = _get_font(14, bold=True)
        f_value = _get_font(16)
        f_tier = _get_font(13, bold=True)
        f_small = _get_font(12)

        # Top accent bar
        draw.rectangle([0, 0, W, 6], fill=tier_colour)

        # Tier badge
        draw.rounded_rectangle([MARGIN, 18, MARGIN + 110, 42], radius=4, fill=tier_colour)
        draw.text((MARGIN + 8, 20), tier_name, fill="#ffffff", font=f_tier)

        # Rating circle
        cx, cy, cr = W - 70, 60, 42
        draw.ellipse([cx - cr, cy - cr, cx + cr, cy + cr], fill=tier_colour)
        rating_text = str(player.rating)
        bbox = draw.textbbox((0, 0), rating_text, font=f_rating_big)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2 - 4), rating_text, fill="#ffffff", font=f_rating_big)
        draw.text((cx - 12, cy + cr - 8), "OVR", fill="#ffffff", font=f_small)

        # Player name
        name_y = 60
        draw.text((MARGIN, name_y), player.name, fill="#ffffff", font=f_title)

        # Country & Category
        sub = f"{player.country}  |  {player.category}"
        draw.text((MARGIN, name_y + 30), sub, fill="#aaaaaa", font=f_value)

        # Divider
        div_y = 120
        draw.line([(MARGIN, div_y), (W - MARGIN, div_y)], fill="#333355", width=1)

        # Silhouette placeholder
        sil_y = 130
        draw.rounded_rectangle([MARGIN, sil_y, W - MARGIN, sil_y + 180], radius=12, fill="#16213e")
        # Cricket icon text
        icon_font = _get_font(18, bold=True)
        draw.text((W // 2 - 60, sil_y + 70), "CRICKET", fill="#333355", font=_get_font(28, bold=True))
        draw.text((W // 2 - 50, sil_y + 105), "SIMULATOR", fill="#333355", font=icon_font)

        # Stats grid
        grid_y = 330
        draw.rounded_rectangle([MARGIN, grid_y, W - MARGIN, grid_y + 160], radius=10, fill="#16213e")

        col1, col2 = MARGIN + 16, W // 2 + 16
        row_h = 26
        stats_left = [
            ("BAT HAND", player.bat_hand),
            ("BOWL STYLE", player.bowl_style),
            ("BAT AVG", f"{player.bat_avg:.1f}"),
            ("STRIKE RATE", f"{player.strike_rate:.1f}"),
            ("RUNS", f"{player.runs:,}"),
            ("CENTURIES", str(player.centuries)),
        ]
        stats_right = [
            ("BOWL HAND", player.bowl_hand),
            ("BAT RTG", str(player.bat_rating)),
            ("BOWL AVG", f"{player.bowl_avg:.1f}"),
            ("ECONOMY", f"{player.economy:.1f}"),
            ("WICKETS", f"{player.wickets:,}"),
            ("BOWL RTG", str(player.bowl_rating)),
        ]

        for i, (lbl, val) in enumerate(stats_left):
            y = grid_y + 10 + i * row_h
            draw.text((col1, y), lbl, fill="#888888", font=f_small)
            draw.text((col1 + 100, y), str(val), fill="#ffffff", font=f_value)

        for i, (lbl, val) in enumerate(stats_right):
            y = grid_y + 10 + i * row_h
            draw.text((col2, y), lbl, fill="#888888", font=f_small)
            draw.text((col2 + 100, y), str(val), fill="#ffffff", font=f_value)

        # Value bar
        val_y = 510
        draw.rounded_rectangle([MARGIN, val_y, W - MARGIN, val_y + 50], radius=8, fill="#0f3460")
        draw.text((MARGIN + 12, val_y + 8), "BUY", fill="#888888", font=f_small)
        draw.text((MARGIN + 12, val_y + 24), f"{buy_val:,}", fill="#f1c40f", font=f_value)
        draw.text((W // 2 + 12, val_y + 8), "SELL", fill="#888888", font=f_small)
        draw.text((W // 2 + 12, val_y + 24), f"{sell_val:,}", fill="#2ecc71", font=f_value)

        # Version footer
        foot_y = 580
        draw.text((MARGIN, foot_y), f"Version: {player.version}", fill="#555555", font=f_small)
        draw.text((MARGIN, foot_y + 18), "Cricket Simulator Bot", fill="#333355", font=f_small)

        # Bottom accent bar
        draw.rectangle([0, H - 4, W, H], fill=tier_colour)

        buf = io.BytesIO()
        img.save(buf, format="PNG", quality=95)
        buf.seek(0)
        return buf.getvalue()

    except Exception:
        logger.exception("Card generation failed")
        return None
