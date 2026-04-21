"""Generate bowler stats card — CricMaster Ultra Broadcast design.
Same look as batsman_card.py but with target icon and bowling-specific stats.
"""

import io
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

W, H = 1200, 560

BG_TOP    = (8, 12, 22)
BG_BOTTOM = (16, 22, 36)
RED       = (255, 60, 70)
DIM_RED   = (200, 40, 50)
WHITE     = (240, 245, 250)
DIM       = (120, 130, 150)
DIVIDER   = (60, 70, 90)
TEAL      = (0, 200, 180)


def _font(size, bold=False, italic=False):
    if bold and italic:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"
    elif bold:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    elif italic:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf"
    else:
        path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(path, size)
    except (OSError, IOError):
        try:
            return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
        except (OSError, IOError):
            return ImageFont.load_default()


def _draw_gradient(img, top, bot):
    w, h = img.size
    pixels = img.load()
    for y in range(h):
        t = y / h
        r = int(top[0] * (1 - t) + bot[0] * t)
        g = int(top[1] * (1 - t) + bot[1] * t)
        b = int(top[2] * (1 - t) + bot[2] * t)
        for x in range(w):
            pixels[x, y] = (r, g, b)


def _text_width(draw, text, font):
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _get_bowler_style_label(bowl_style):
    if not bowl_style:
        return "MEDIUM"
    s = bowl_style.upper()
    if "FAST" in s: return "FAST"
    if "MEDIUM" in s: return "MEDIUM"
    if "OFF SPIN" in s or "OFF-SPIN" in s or "OFF SPINNER" in s: return "OFF SPIN"
    if "LEG SPIN" in s or "LEG-SPIN" in s or "LEG SPINNER" in s: return "LEG SPIN"
    if "SPIN" in s: return "SPIN"
    return s


def generate_bowler_card(name, rating, bowl_rating, stats, bat_hand="Right",
                         bowl_hand="Right", bowl_style="Medium Pacer") -> bytes | None:
    """Generate the CricMaster bowler stats card.

    stats dict keys: bowl_inns, wickets_taken, runs_conceded, balls_bowled,
                     bowl_avg, bowl_sr, econ, hat_tricks, five_fers, three_fers,
                     bbf_str (e.g. "5/28")
    """
    try:
        img = Image.new("RGB", (W, H), BG_TOP)
        _draw_gradient(img, BG_TOP, BG_BOTTOM)
        draw = ImageDraw.Draw(img, "RGBA")

        # Fonts
        f_ovr_label = _font(22, bold=True)
        f_ovr_num = _font(120, bold=True)
        f_tag = _font(20, bold=True)
        f_engine = _font(16, bold=True)
        f_stat_label = _font(22, bold=True)
        f_stat_val = _font(36, bold=True)

        # Name (auto-shrink)
        name_upper = name.upper()
        name_size = 76
        max_name_width = 620
        while name_size > 34:
            tf = _font(name_size, bold=True, italic=True)
            if _text_width(draw, name_upper, tf) <= max_name_width:
                break
            name_size -= 4
        f_name = _font(name_size, bold=True, italic=True)
        draw.text((50, 40), name_upper, fill=RED, font=f_name)

        # OVR
        ovr_text = str(rating)
        ovr_w = _text_width(draw, ovr_text, f_ovr_num)
        label_w = _text_width(draw, "OVERALL RATING", f_ovr_label)
        ovr_x = W - 50 - ovr_w
        label_x = ovr_x - label_w - 20
        draw.text((label_x, 80), "OVERALL RATING", fill=DIM, font=f_ovr_label)
        draw.text((ovr_x, 25), ovr_text, fill=WHITE, font=f_ovr_num)

        # Tags row (BATTER | Right Hand | Right Hand | MEDIUM)
        tag_y = 150
        batter_tag = "BATTER"
        tag_pad_x = 20
        bw = _text_width(draw, batter_tag, f_tag)

        draw.rounded_rectangle(
            [50, tag_y, 50 + bw + tag_pad_x * 2, tag_y + 38],
            radius=6, outline=RED, width=2, fill=(255, 60, 70, 35)
        )
        draw.text((50 + tag_pad_x, tag_y + 6), batter_tag, fill=RED, font=f_tag)

        tag_x = 50 + bw + tag_pad_x * 2 + 25
        tags = [bat_hand.upper() + " HAND", bowl_hand.upper() + " HAND",
                _get_bowler_style_label(bowl_style)]
        for tag in tags:
            draw.rectangle([tag_x, tag_y + 5, tag_x + 2, tag_y + 33], fill=DIVIDER)
            tag_x += 20
            draw.text((tag_x, tag_y + 6), tag, fill=DIM, font=f_tag)
            tag_x += _text_width(draw, tag, f_tag) + 20

        # Subtitle
        subtitle_y = 215
        subtitle_font = _font(18, bold=True, italic=True)
        draw.text((50, subtitle_y),
                  "C R I C M A S T E R   U L T R A   B R O A D C A S T   S Y S T E M S",
                  fill=DIM_RED, font=subtitle_font)
        engine_txt = "STATISTICAL ANALYSIS ENGINE V4.0"
        eng_w = _text_width(draw, engine_txt, f_engine)
        draw.text((W - 50 - eng_w, subtitle_y + 2), engine_txt, fill=DIM, font=f_engine)

        # Divider
        draw.line([(50, 255), (W - 50, 255)], fill=DIVIDER, width=1)

        # BOWLING PROFILE block with target icon
        icon_x, icon_y = 55, 255
        icon_size = 48
        draw.rounded_rectangle(
            [icon_x, icon_y, icon_x + icon_size, icon_y + icon_size],
            radius=8, fill=(26, 32, 48)
        )
        # Target icon — concentric circles (red)
        cx, cy = icon_x + icon_size // 2, icon_y + icon_size // 2
        # Outer ring
        draw.ellipse([cx - 18, cy - 18, cx + 18, cy + 18],
                     outline=RED, width=3)
        # Middle ring
        draw.ellipse([cx - 12, cy - 12, cx + 12, cy + 12],
                     outline=RED, width=2)
        # Bullseye
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=RED)
        # Small accent dot (the "G" notch)
        draw.ellipse([cx + 14, cy - 18, cx + 22, cy - 10], fill=RED)

        f_section_small = _font(16, bold=True)
        f_bes_num_small = _font(44, bold=True)
        draw.text((icon_x + icon_size + 15, icon_y - 2),
                  "BOWLING PROFILE", fill=DIM, font=f_section_small)
        draw.text((icon_x + icon_size + 15, icon_y + 14),
                  str(bowl_rating), fill=RED, font=f_bes_num_small)

        # Stats — two columns
        stats_y_start = 330
        row_height = 42

        left_col_x_label = 55
        left_col_x_value = 640
        right_col_x_label = 720
        right_col_x_value = W - 55

        bowl_inns = stats.get("bowl_inns", 0)
        wickets = stats.get("wickets_taken", 0)
        bowl_avg = stats.get("bowl_avg", 0)
        bowl_sr = stats.get("bowl_sr", 0)
        econ = stats.get("econ", 0)
        hat_tricks = stats.get("hat_tricks", 0)
        five_fers = stats.get("five_fers", 0)
        three_fers = stats.get("three_fers", 0)
        bbf_str = str(stats.get("bbf_str", "-"))

        def fmt(v):
            if isinstance(v, float) and v == int(v):
                return f"{int(v)}"
            if isinstance(v, float):
                return f"{v:.2f}".rstrip('0').rstrip('.')
            return str(v)

        left_stats = [
            ("INNS", fmt(bowl_inns)),
            ("BBF", bbf_str),
            ("ECON", f"{econ:.2f}" if isinstance(econ, (int, float)) and econ else "0"),
            ("HATTRICKS", fmt(hat_tricks)),
            ("3-FERS", fmt(three_fers)),
        ]
        right_stats = [
            ("WICKETS", fmt(wickets)),
            ("AVG", f"{bowl_avg:.2f}" if isinstance(bowl_avg, (int, float)) and bowl_avg else "0"),
            ("SR", f"{bowl_sr:.2f}" if isinstance(bowl_sr, (int, float)) and bowl_sr else "0"),
            ("5-FERS", fmt(five_fers)),
            ("", ""),
        ]

        for i, (label, value) in enumerate(left_stats):
            y = stats_y_start + i * row_height
            draw.text((left_col_x_label, y + 6), label, fill=DIM, font=f_stat_label)
            val_w = _text_width(draw, value, f_stat_val)
            draw.text((left_col_x_value - val_w, y - 2), value, fill=WHITE, font=f_stat_val)

        for i, (label, value) in enumerate(right_stats):
            if not label:
                continue
            y = stats_y_start + i * row_height
            draw.text((right_col_x_label, y + 6), label, fill=DIM, font=f_stat_label)
            val_w = _text_width(draw, value, f_stat_val)
            draw.text((right_col_x_value - val_w, y - 2), value, fill=WHITE, font=f_stat_val)

        # Bottom teal accent line
        draw.line([(50, H - 15), (300, H - 15)], fill=TEAL, width=3)

        buf = io.BytesIO()
        img.save(buf, format="PNG", quality=95)
        buf.seek(0)
        return buf.getvalue()
    except Exception:
        logger.exception("Bowler card generation failed")
        return None
