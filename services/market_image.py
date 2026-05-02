"""Composite market image generator.

Renders all market slots as a single PNG laid out in a 3-column grid like
the reference design. Bot sends this one image instead of 6 separate cards.

Cached by hash of (slot_id, purchased_count) tuples so we regenerate only
when the market actually changes.
"""

import io
import logging
import os
from PIL import Image, ImageDraw, ImageFont

from models import Player

logger = logging.getLogger(__name__)


# Layout
W = 1280            # full image width
COL_W = 410         # per-card width
COL_H = 230         # per-card height
GAP = 12            # gap between cards
COLS = 3
TITLE_H = 200       # space for title bar at top
PADDING = 28        # outer padding

# Colors (hex strings, render as RGB tuples)
BG = (10, 17, 38)              # deep blue
TITLE_FILL = (16, 12, 35)
CARD_BG = (40, 50, 75)
CARD_BG_ACCENT = (110, 70, 35)  # bronze (for "premium" alternate)
TEXT_PRIMARY = (250, 250, 252)
TEXT_DIM = (180, 190, 210)
TEXT_LABEL = (130, 140, 165)
ACCENT = (140, 200, 255)
SOLD_OUT = (200, 80, 80)
BORDER = (90, 110, 145)


def _font(size, bold=False):
    """Try multiple fallback fonts. Returns PIL ImageFont."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _country_code(country):
    """Map full country name → 3-letter code (matches the reference image)."""
    if not country:
        return "—"
    c = country.upper()
    table = {
        "INDIA": "IND", "AUSTRALIA": "AUS", "ENGLAND": "ENG",
        "PAKISTAN": "PAK", "NEW ZEALAND": "NZ", "SOUTH AFRICA": "SA",
        "WEST INDIES": "WI", "SRI LANKA": "SL", "BANGLADESH": "BAN",
        "AFGHANISTAN": "AFG", "ZIMBABWE": "ZIM", "IRELAND": "IRE",
        "NETHERLANDS": "NED", "SCOTLAND": "SCO", "USA": "USA",
        "UNITED ARAB EMIRATES": "UAE",
    }
    if c in table: return table[c]
    if len(country) >= 3:
        return country[:3].upper()
    return c


def _draw_card(draw, base, x, y, slot_index, player, slot_row, accent=False):
    """Draw a single market slot card at (x, y)."""
    bg = CARD_BG_ACCENT if accent else CARD_BG
    # Card background with subtle border
    draw.rounded_rectangle([x, y, x + COL_W, y + COL_H], radius=12,
                           fill=bg, outline=BORDER, width=2)

    # Slot label "S/N: 001"
    sn = f"S/N: {slot_index:03d}"
    draw.text((x + 8, y - 22), sn, fill=TEXT_DIM, font=_font(13))

    # Big rating number on the left
    rating_str = str(player.rating or 0)
    rating_font = _font(60, bold=True)
    draw.text((x + 18, y + 22), rating_str, fill=TEXT_PRIMARY, font=rating_font)
    draw.text((x + 30, y + 90), "OVR", fill=TEXT_LABEL, font=_font(13))

    # Country pill below rating
    code = _country_code(player.country)
    pill_w = 50; pill_h = 22
    pill_x = x + 22
    pill_y = y + 115
    draw.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
                           radius=4, fill=(50, 70, 110), outline=BORDER, width=1)
    # Center text in pill
    bbox = draw.textbbox((0, 0), code, font=_font(12, bold=True))
    tw = bbox[2] - bbox[0]
    draw.text((pill_x + (pill_w - tw) // 2, pill_y + 4), code,
              fill=TEXT_PRIMARY, font=_font(12, bold=True))

    # Right side: name (top), category, batting/bowling
    text_x = x + 130
    # Name (split into 2 lines if too long)
    name = (player.name or "—").upper()
    name_font = _font(18, bold=True)
    # Word wrap
    words = name.split()
    lines = []
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        bbox = draw.textbbox((0, 0), test, font=name_font)
        if bbox[2] - bbox[0] < COL_W - 145 and len(lines) < 2:
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    lines = lines[:2]
    for i, l in enumerate(lines):
        draw.text((text_x, y + 18 + i * 22), l, fill=TEXT_PRIMARY, font=name_font)

    # Category subtitle
    cat = (player.category or "—").upper()
    draw.text((text_x, y + 60), cat, fill=TEXT_LABEL, font=_font(13))

    # BATTING / BOWLING ratings (two columns)
    b_y = y + 95
    bat_rating = player.bat_rating or 0
    bowl_rating = player.bowl_rating or 0
    rating_lbl_font = _font(11)
    rating_val_font = _font(28, bold=True)
    draw.text((text_x, b_y), str(bat_rating), fill=TEXT_PRIMARY, font=rating_val_font)
    draw.text((text_x + 4, b_y + 36), "BATTING", fill=TEXT_LABEL, font=rating_lbl_font)
    draw.text((text_x + 90, b_y), str(bowl_rating), fill=TEXT_PRIMARY, font=rating_val_font)
    draw.text((text_x + 90, b_y + 36), "BOWLING", fill=TEXT_LABEL, font=rating_lbl_font)

    # Separator
    sep_y = y + 158
    draw.line([(text_x, sep_y), (x + COL_W - 18, sep_y)], fill=BORDER, width=1)

    # Style lines (BATTING STYLE / BOWLING STYLE)
    label_font = _font(10)
    val_font = _font(11)
    s_y = sep_y + 8
    draw.text((text_x, s_y), "BATTING STYLE", fill=TEXT_LABEL, font=label_font)
    bat_style = "Right-hand bat" if (player.bat_hand or "").lower().startswith("r") else "Left-hand bat"
    draw.text((text_x + 130, s_y), bat_style, fill=TEXT_PRIMARY, font=val_font)
    s_y += 18
    draw.text((text_x, s_y), "BOWLING STYLE", fill=TEXT_LABEL, font=label_font)
    bowl_style = player.bowl_style or "—"
    # Truncate if too long
    if len(bowl_style) > 18:
        bowl_style = bowl_style[:18]
    draw.text((text_x + 130, s_y), bowl_style, fill=TEXT_PRIMARY, font=val_font)

    # Sold-out overlay
    sold_out = (slot_row.purchased_count >= slot_row.quantity)
    if sold_out:
        draw.rectangle([x, y, x + COL_W, y + COL_H],
                       fill=None, outline=SOLD_OUT, width=4)
        # "SOLD" tag
        tag_w = 80; tag_h = 26
        tag_x = x + COL_W - tag_w - 10
        tag_y = y + 8
        draw.rounded_rectangle([tag_x, tag_y, tag_x + tag_w, tag_y + tag_h],
                               radius=4, fill=SOLD_OUT)
        draw.text((tag_x + 18, tag_y + 5), "SOLD", fill=(255, 255, 255), font=_font(13, bold=True))


def generate_market_image(session, slots, title="CRICMASTERULTRA PLAYER MARKET",
                          subtitle=None):
    """Build a composite image showing all market slots.

    Args:
      session: SQLAlchemy session (used to look up Player rows)
      slots: list of GlobalPlayerMarket rows
      title: top header text
      subtitle: optional subtitle (e.g. "Market Update 12:00 IST")

    Returns: bytes of the PNG, or None on failure.
    """
    try:
        if not slots:
            return None

        # Resolve players
        players = {p.id: p for p in
                   session.query(Player).filter(
                       Player.id.in_([s.player_id for s in slots])).all()}

        # Calculate canvas height based on row count
        rows = (len(slots) + COLS - 1) // COLS
        H = TITLE_H + (COL_H + GAP) * rows + PADDING * 2

        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img)

        # Title bar — dark with rounded top
        title_y = 24
        title_bar_w = 760
        title_bar_x = (W - title_bar_w) // 2
        title_bar_h = 130
        draw.rounded_rectangle(
            [title_bar_x, title_y, title_bar_x + title_bar_w, title_y + title_bar_h],
            radius=14, fill=TITLE_FILL, outline=BORDER, width=2)

        # Title text
        title_font = _font(34, bold=True)
        bbox = draw.textbbox((0, 0), title, font=title_font)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) // 2, title_y + 30), title,
                  fill=TEXT_PRIMARY, font=title_font)

        if subtitle:
            sub_font = _font(18)
            bbox = draw.textbbox((0, 0), subtitle, font=sub_font)
            sw = bbox[2] - bbox[0]
            draw.text(((W - sw) // 2, title_y + 80), subtitle,
                      fill=TEXT_DIM, font=sub_font)

        # Grid of cards
        grid_top = TITLE_H + 12
        grid_left = (W - (COLS * COL_W + (COLS - 1) * GAP)) // 2

        for i, slot in enumerate(slots):
            row = i // COLS
            col = i % COLS
            cx = grid_left + col * (COL_W + GAP)
            cy = grid_top + row * (COL_H + GAP)

            player = players.get(slot.player_id)
            if not player:
                continue

            # Alternate accent for visual variety (matches the reference)
            accent = (i in (1, 5))  # tweak which slots get the bronze look
            _draw_card(draw, img, cx, cy, slot.slot_index + 1,
                       player, slot, accent=accent)

        # Save
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("generate_market_image failed")
        return None
