"""Generate batsman stats card — CricMaster Ultra Broadcast design.
Dark navy gradient, red accent, two-column stats layout.
"""

import io
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Card dimensions - wide panoramic aspect ratio for broadcast look
W, H = 1200, 560

# Colors (CricMaster palette)
BG_TOP    = (8, 12, 22)         # near-black navy top
BG_BOTTOM = (16, 22, 36)        # slightly lighter navy bottom
RED       = (255, 60, 70)        # vivid red accent
DIM_RED   = (200, 40, 50)
WHITE     = (240, 245, 250)
LIGHT     = (210, 215, 225)
DIM       = (120, 130, 150)     # muted gray labels
DIVIDER   = (60, 70, 90)
TEAL      = (0, 200, 180)       # bottom accent line


def _font(size, bold=False, italic=False):
    candidates = []
    if bold and italic:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    elif bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    elif italic:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_gradient(img, top_color, bottom_color):
    w, h = img.size
    pixels = img.load()
    for y in range(h):
        t = y / h
        r = int(top_color[0] * (1 - t) + bottom_color[0] * t)
        g = int(top_color[1] * (1 - t) + bottom_color[1] * t)
        b = int(top_color[2] * (1 - t) + bottom_color[2] * t)
        for x in range(w):
            pixels[x, y] = (r, g, b)


def _text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _get_bowler_style_label(bowl_style):
    """Simplify bowling style for display."""
    if not bowl_style:
        return "MEDIUM"
    s = bowl_style.upper()
    if "FAST" in s: return "FAST"
    if "MEDIUM" in s: return "MEDIUM"
    if "OFF SPIN" in s or "OFF-SPIN" in s: return "OFF SPIN"
    if "LEG SPIN" in s or "LEG-SPIN" in s: return "LEG SPIN"
    if "SPIN" in s: return "SPIN"
    return s


def generate_batsman_card(name, rating, bat_rating, stats, bat_hand="Right",
                          bowl_hand="Right", bowl_style="Medium Pacer") -> bytes | None:
    """Generate the CricMaster Ultra batsman stats card.

    stats dict keys: bat_inns, runs, fifties, hundreds, fours, sixes,
                     bat_avg, bat_sr, ducks, hs_str
    """
    try:
        img = Image.new("RGB", (W, H), BG_TOP)
        _draw_gradient(img, BG_TOP, BG_BOTTOM)
        draw = ImageDraw.Draw(img, "RGBA")

        # ── Fonts ────────────────────────────────────────────────
        f_ovr_label = _font(22, bold=True)
        f_ovr_num = _font(120, bold=True)
        f_tag = _font(20, bold=True)
        f_tag_active = _font(20, bold=True)
        f_engine = _font(16, bold=True)
        f_stat_label = _font(22, bold=True)
        f_stat_val = _font(36, bold=True)

        # ── Player name (big italic red, top-left) ──────────────
        name_upper = name.upper()
        name_size = 76
        max_name_width = 620  # hard limit, leaves ample room for OVR block
        while name_size > 34:
            test_font = _font(name_size, bold=True, italic=True)
            if _text_width(draw, name_upper, test_font) <= max_name_width:
                break
            name_size -= 4
        f_name = _font(name_size, bold=True, italic=True)
        draw.text((50, 40), name_upper, fill=RED, font=f_name)

        # ── OVR (top-right) ──────────────────────────────────────
        ovr_text = str(rating)
        ovr_w = _text_width(draw, ovr_text, f_ovr_num)
        label_w = _text_width(draw, "OVERALL RATING", f_ovr_label)
        ovr_x = W - 50 - ovr_w
        label_x = ovr_x - label_w - 20

        draw.text((label_x, 80), "OVERALL RATING", fill=DIM, font=f_ovr_label)
        draw.text((ovr_x, 25), ovr_text, fill=WHITE, font=f_ovr_num)

        # ── Tags row (BATTER | Right Hand | Right Hand | MEDIUM) ─
        # BATTER is active (red background box)
        tag_y = 150
        batter_tag = "BATTER"
        tag_pad_x = 20
        tag_pad_y = 8
        bw = _text_width(draw, batter_tag, f_tag_active)

        # Active BATTER tag — red outlined box with transparent fill
        draw.rounded_rectangle(
            [50, tag_y, 50 + bw + tag_pad_x * 2, tag_y + 38],
            radius=6,
            outline=RED, width=2,
            fill=(255, 60, 70, 35)  # semi-transparent red
        )
        draw.text((50 + tag_pad_x, tag_y + 6), batter_tag, fill=RED, font=f_tag_active)

        # Separator + other tags (gray)
        tag_x = 50 + bw + tag_pad_x * 2 + 25

        tags = [bat_hand.upper() + " HAND", bowl_hand.upper() + " HAND",
                _get_bowler_style_label(bowl_style)]
        for i, tag in enumerate(tags):
            # Divider bar
            draw.rectangle([tag_x, tag_y + 5, tag_x + 2, tag_y + 33], fill=DIVIDER)
            tag_x += 20
            draw.text((tag_x, tag_y + 6), tag, fill=DIM, font=f_tag)
            tag_x += _text_width(draw, tag, f_tag) + 20

        # ── Subtitle line: CRICMASTER ULTRA BROADCAST SYSTEMS ────
        subtitle_y = 215
        # Use a smaller italic font for the wide-tracking subtitle and LESS letter spacing
        subtitle_font = _font(18, bold=True, italic=True)
        subtitle_txt = "C R I C M A S T E R   U L T R A   B R O A D C A S T   S Y S T E M S"
        draw.text((50, subtitle_y), subtitle_txt, fill=DIM_RED, font=subtitle_font)

        # Right side: STATISTICAL ANALYSIS ENGINE V4.0
        engine_txt = "STATISTICAL ANALYSIS ENGINE V4.0"
        eng_w = _text_width(draw, engine_txt, f_engine)
        draw.text((W - 50 - eng_w, subtitle_y + 2), engine_txt, fill=DIM, font=f_engine)

        # ── Divider line ─────────────────────────────────────────
        draw.line([(50, 255), (W - 50, 255)], fill=DIVIDER, width=1)

        # ── BATTING PROFILE block (left, with icon) ──────────────
        icon_x, icon_y = 55, 255
        icon_size = 48
        # Icon box — dark subtle background
        draw.rounded_rectangle(
            [icon_x, icon_y, icon_x + icon_size, icon_y + icon_size],
            radius=8, fill=(26, 32, 48)
        )
        # Cricket bat glyph (simple orange bat)
        bat_color = (255, 140, 40)
        # handle
        draw.rectangle([icon_x + 20, icon_y + 8, icon_x + 24, icon_y + 22], fill=bat_color)
        # blade
        draw.rounded_rectangle([icon_x + 14, icon_y + 22, icon_x + 30, icon_y + 42],
                               radius=3, fill=bat_color)
        # ball
        draw.ellipse([icon_x + 31, icon_y + 32, icon_x + 40, icon_y + 41],
                     fill=(200, 40, 50))

        f_section_small = _font(16, bold=True)
        f_bes_num_small = _font(44, bold=True)
        draw.text((icon_x + icon_size + 15, icon_y - 2),
                  "BATTING PROFILE", fill=DIM, font=f_section_small)
        draw.text((icon_x + icon_size + 15, icon_y + 14),
                  str(bat_rating), fill=RED, font=f_bes_num_small)

        # ── Stats — two columns ──────────────────────────────────
        stats_y_start = 330
        row_height = 42

        left_col_x_label = 55
        left_col_x_value = 640  # right-aligned end
        right_col_x_label = 720
        right_col_x_value = W - 55  # right-aligned end

        inns = stats.get("bat_inns", 0)
        runs = stats.get("runs", 0)
        hs = str(stats.get("hs_str", "-"))
        avg = stats.get("bat_avg", 0)
        sr = stats.get("bat_sr", 0)
        hundreds = stats.get("hundreds", 0)
        fifties = stats.get("fifties", 0)
        fours = stats.get("fours", 0)
        sixes = stats.get("sixes", 0)
        ducks = stats.get("ducks", 0)

        # Format numbers: strip .0 if whole
        def fmt(v):
            if isinstance(v, float) and v == int(v):
                return f"{int(v)}"
            if isinstance(v, float):
                return f"{v:.2f}".rstrip('0').rstrip('.')
            return str(v)

        left_stats = [
            ("INNS", fmt(inns)),
            ("HS", hs),
            ("SR", f"{sr:.2f}" if isinstance(sr, (int, float)) and sr else "0"),
            ("50S", fmt(fifties)),
            ("6S", fmt(sixes)),
        ]
        right_stats = [
            ("RUNS", fmt(runs)),
            ("AVG", f"{avg:.2f}" if isinstance(avg, (int, float)) and avg else "0"),
            ("100S", fmt(hundreds)),
            ("4S", fmt(fours)),
            ("DUCKS", fmt(ducks)),
        ]

        # Labels at left edge, values right-aligned within their column
        for i, (label, value) in enumerate(left_stats):
            y = stats_y_start + i * row_height
            draw.text((left_col_x_label, y + 6), label, fill=DIM, font=f_stat_label)
            val_w = _text_width(draw, value, f_stat_val)
            draw.text((left_col_x_value - val_w, y - 2), value, fill=WHITE, font=f_stat_val)

        for i, (label, value) in enumerate(right_stats):
            y = stats_y_start + i * row_height
            draw.text((right_col_x_label, y + 6), label, fill=DIM, font=f_stat_label)
            val_w = _text_width(draw, value, f_stat_val)
            draw.text((right_col_x_value - val_w, y - 2), value, fill=WHITE, font=f_stat_val)

        # ── Bottom teal accent line ──────────────────────────────
        draw.line([(50, H - 15), (300, H - 15)], fill=TEAL, width=3)

        buf = io.BytesIO()
        img.save(buf, format="PNG", quality=95)
        buf.seek(0)
        return buf.getvalue()

    except Exception:
        logger.exception("Batsman card generation failed")
        return None
