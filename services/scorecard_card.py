"""Generate innings scorecards — CricMaster Ultra design.

Four cards:
  Batting 1st innings  — PRIMARY colors (Lava Red)
  Bowling 2nd innings  — PRIMARY colors (Lava Red)
  Batting 2nd innings  — SECONDARY colors (Lagoon Teal)
  Bowling 1st innings  — SECONDARY colors (Lagoon Teal)

Color scheme 1: Lava Red / Lagoon Teal / Midnight Slate / Soft Pearl
"""

import io
import os
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Scheme 1 - Lava Red + Lagoon Teal
PRIMARY    = (255, 42, 42)      # #FF2A2A Lava Red
SECONDARY  = (0, 201, 167)      # #00C9A7 Lagoon Teal
BG         = (15, 23, 42)       # #0F172A Midnight Slate
BG_DARK    = (6, 11, 24)        # near black for gradient bottom
TEXT       = (241, 245, 249)    # #F1F5F9 Soft Pearl
OPPONENT   = (110, 120, 140)    # muted — for "vs OPPONENT"
DIM        = (130, 140, 160)    # dimmed text for labels / small stats
ROW_SEP    = (40, 50, 70)       # row divider

# Logo path — user can replace this file anytime
_LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "assets", "logo.png")


def _load_logo(target_size=120):
    """Load the bot logo, resized to fit header. Returns PIL Image with alpha or None."""
    try:
        if not os.path.exists(_LOGO_PATH):
            return None
        img = Image.open(_LOGO_PATH).convert("RGBA")
        # Preserve aspect ratio, fit inside target_size box
        img.thumbnail((target_size, target_size), Image.LANCZOS)
        return img
    except Exception:
        logger.warning("Could not load logo")
        return None


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
        return ImageFont.load_default()


def _draw_gradient(img, top, bottom):
    w, h = img.size
    pixels = img.load()
    for y in range(h):
        t = y / h
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        for x in range(w):
            pixels[x, y] = (r, g, b)


def _tw(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


# ═══════════════════════════════════════════════════════════════════════
# BATTING SCORECARD
# ═══════════════════════════════════════════════════════════════════════

def generate_batting_scorecard(team_name, opponent_name, total_runs, total_wickets,
                               overs_str, batsmen_rows, fall_of_wickets, extras_dict,
                               is_first_innings=True, match_title="MATCH") -> bytes | None:
    """Generate batting scorecard.

    Args:
        team_name: batting team
        opponent_name: bowling team
        total_runs, total_wickets, overs_str: e.g. 230, 10, "49.1"
        batsmen_rows: list of dicts with keys:
            rating, name, dismissal, runs, balls, fours, sixes, strike_rate
        fall_of_wickets: list of tuples [(wicket_num, score, over), ...]
        extras_dict: {wd, nb, b, lb, total}
        is_first_innings: True = primary (red), False = secondary (teal)
        match_title: subtitle text (e.g. "SUPER LEAGUE FINAL")

    Returns PNG bytes.
    """
    try:
        accent = PRIMARY if is_first_innings else SECONDARY
        innings_label = "INNINGS 1" if is_first_innings else "INNINGS 2"

        # Canvas
        W = 1700
        base_h = 440
        row_h = 56
        H = base_h + len(batsmen_rows) * row_h + 200

        img = Image.new("RGB", (W, H), BG)
        _draw_gradient(img, BG, BG_DARK)
        draw = ImageDraw.Draw(img, "RGBA")

        # ── Fonts ────
        f_innings_tag = _font(20, bold=True)
        f_team_big = _font(96, bold=True, italic=True)
        f_vs = _font(34, bold=True, italic=True)
        f_opp = _font(38, bold=True, italic=True)
        f_score_big = _font(100, bold=True)
        f_score_slash = _font(100, bold=True)
        f_overs_num = _font(38, bold=True)
        f_overs_label = _font(18, bold=True)
        f_brand = _font(16, bold=True, italic=True)
        f_match = _font(16, bold=True)
        f_col_header = _font(20, bold=True)
        f_batsman = _font(26, bold=True)
        f_dismissal = _font(20, italic=True)
        f_stat = _font(26, bold=True)
        f_stat_small = _font(20, bold=True)
        f_ext_label = _font(18, bold=True)
        f_fow_num = _font(26, bold=True)
        f_fow_over = _font(16, italic=True)

        # ── LOGO (top-left) ────
        logo = _load_logo(target_size=130)
        logo_x = 45
        logo_y = 35
        content_start_x = 45  # where text content starts (after logo area)
        if logo:
            img.paste(logo, (logo_x, logo_y), logo)
            content_start_x = logo_x + logo.size[0] + 25
        else:
            content_start_x = 45

        # ── Innings tag with accent line ────
        tag_y = 45
        draw.line([(content_start_x, tag_y + 12), (content_start_x + 55, tag_y + 12)],
                  fill=accent, width=4)
        tag_text = f"BATTING  •  {innings_label}"
        draw.text((content_start_x + 70, tag_y), tag_text, fill=accent, font=f_innings_tag)

        # Match title (next to tag, gray)
        tag_w = _tw(draw, tag_text, f_innings_tag)
        draw.text((content_start_x + 70 + tag_w + 30, tag_y + 2),
                  match_title.upper(), fill=DIM, font=f_match)

        # Brand top-right
        brand = "CRICMASTERULTRA"
        brand_w = _tw(draw, brand, f_brand)
        draw.text((W - 50 - brand_w, tag_y + 2), brand, fill=DIM, font=f_brand)

        # ── Team name (BIG, white, italic bold) ────
        team_y = 85
        team_upper = team_name.upper()
        # Auto-shrink if team name is too long
        team_size = 96
        max_team_width = 820
        while team_size > 48:
            tf = _font(team_size, bold=True, italic=True)
            if _tw(draw, team_upper, tf) <= max_team_width:
                break
            team_size -= 6
        f_team_big = _font(team_size, bold=True, italic=True)
        draw.text((content_start_x, team_y), team_upper, fill=TEXT, font=f_team_big)
        team_w = _tw(draw, team_upper, f_team_big)

        # ── vs OPPONENT (smaller, dim gray, italic) ────
        vs_y = team_y + team_size // 2 + 5
        vs_text = "VS"
        opp_upper = opponent_name.upper()
        draw.text((content_start_x + team_w + 30, vs_y),
                  vs_text, fill=OPPONENT, font=f_vs)
        vs_w = _tw(draw, vs_text, f_vs)
        draw.text((content_start_x + team_w + 30 + vs_w + 18, vs_y),
                  opp_upper, fill=OPPONENT, font=f_opp)

        # ── Score top-right ────
        score_str = f"{total_runs}"
        wkt_str = f"{total_wickets}"
        score_w = _tw(draw, score_str, f_score_big)
        slash_w = _tw(draw, "/", f_score_slash)
        wkt_w = _tw(draw, wkt_str, f_score_big)
        overs_num_w = _tw(draw, str(overs_str), f_overs_num)
        overs_lbl_w = _tw(draw, "OVERS", f_overs_label)
        overs_total_w = overs_num_w + 10 + overs_lbl_w
        total_score_w = score_w + slash_w + wkt_w + 25 + overs_total_w

        score_x = W - 50 - total_score_w
        score_y = 75
        draw.text((score_x, score_y), score_str, fill=TEXT, font=f_score_big)
        draw.text((score_x + score_w, score_y), "/", fill=accent, font=f_score_slash)
        draw.text((score_x + score_w + slash_w, score_y), wkt_str, fill=TEXT, font=f_score_big)
        overs_x = score_x + score_w + slash_w + wkt_w + 25
        draw.text((overs_x, score_y + 45), str(overs_str), fill=TEXT, font=f_overs_num)
        draw.text((overs_x + overs_num_w + 10, score_y + 63), "OVERS", fill=DIM, font=f_overs_label)

        # ── Separator line below header ────
        header_bot = 225
        draw.line([(50, header_bot), (W - 50, header_bot)], fill=ROW_SEP, width=1)

        # ── Column headers ────
        col_y = 245
        col_rtg_x = 50
        col_bat_x = 140
        col_dis_x = 560
        col_r_x = W - 380
        col_b_x = W - 300
        col_4_x = W - 220
        col_6_x = W - 140
        col_sr_x = W - 50

        draw.text((col_rtg_x, col_y), "R T G", fill=accent, font=f_col_header)
        draw.text((col_bat_x, col_y), "B A T S M A N", fill=accent, font=f_col_header)
        draw.text((col_dis_x, col_y), "D I S M I S S A L", fill=accent, font=f_col_header)

        # Right-aligned column labels
        def r_align(x, text, font, fill):
            tw = _tw(draw, text, font)
            draw.text((x - tw, col_y), text, fill=fill, font=font)

        r_align(col_r_x, "R", f_col_header, accent)
        r_align(col_b_x, "B", f_col_header, accent)
        r_align(col_4_x, "4S", f_col_header, accent)
        r_align(col_6_x, "6S", f_col_header, accent)
        r_align(col_sr_x, "SR", f_col_header, accent)

        # Divider below headers
        draw.line([(50, col_y + 32), (W - 50, col_y + 32)], fill=accent, width=1)

        # ── Batsmen rows ────
        row_y_start = col_y + 55
        for i, bat in enumerate(batsmen_rows):
            y = row_y_start + i * row_h

            # Rating (teal/secondary color accent on the right-most column; here use secondary)
            rating_str = str(bat.get("rating", "-"))
            draw.text((col_rtg_x, y + 8), rating_str, fill=DIM, font=f_stat_small)

            # Batsman name
            draw.text((col_bat_x, y), bat["name"].upper(), fill=TEXT, font=f_batsman)

            # Dismissal
            dismissal_text = bat.get("dismissal", "not out") or "not out"
            draw.text((col_dis_x, y + 5), dismissal_text, fill=DIM, font=f_dismissal)

            # Stats — runs in accent (red/teal), rest dim
            runs = bat.get("runs", 0)
            balls = bat.get("balls", 0)
            fours = bat.get("fours", 0)
            sixes = bat.get("sixes", 0)
            sr = bat.get("strike_rate", 0)

            # Not-out batsmen highlighted teal/secondary, out batsmen in accent
            not_out = not bat.get("dismissal") or "not out" in (bat.get("dismissal", "").lower())
            runs_color = SECONDARY if not_out else accent

            def r_align_row(x, text, font, fill, offset_y=0):
                tw = _tw(draw, text, font)
                draw.text((x - tw, y + offset_y), text, fill=fill, font=font)

            r_align_row(col_r_x, str(runs), f_stat, runs_color)
            r_align_row(col_b_x, str(balls), f_stat_small, DIM, offset_y=4)
            r_align_row(col_4_x, str(fours), f_stat_small, DIM, offset_y=4)
            r_align_row(col_6_x, str(sixes), f_stat_small, DIM, offset_y=4)
            r_align_row(col_sr_x, f"{sr:.1f}" if isinstance(sr, (int, float)) else str(sr),
                        f_stat_small, DIM, offset_y=4)

            # Row divider
            draw.line([(50, y + row_h - 6), (W - 50, y + row_h - 6)], fill=ROW_SEP, width=1)

        # ── EXTRAS + FALL OF WICKETS section ────
        footer_y = row_y_start + len(batsmen_rows) * row_h + 20

        # Heavy divider
        draw.rectangle([0, footer_y - 2, W, footer_y], fill=(accent[0], accent[1], accent[2], 100))

        # Extras
        ex_y = footer_y + 20
        # Red bullet
        draw.ellipse([50, ex_y + 8, 62, ex_y + 20], fill=accent)
        draw.text((72, ex_y + 3), "E X T R A S", fill=accent, font=f_ext_label)

        ex = extras_dict or {}
        ex_total = ex.get("total", 0)
        wd = ex.get("wd", 0)
        nb = ex.get("nb", 0)
        b = ex.get("b", 0)
        lb = ex.get("lb", 0)

        draw.text((50, ex_y + 35), str(ex_total), fill=TEXT, font=_font(42, bold=True))
        ex_details = f"WD: {wd}   NB: {nb}   B: {b}   LB: {lb}"
        draw.text((110, ex_y + 52), ex_details, fill=DIM, font=f_stat_small)

        # Fall of wickets (right side)
        fow_x_start = 350
        draw.ellipse([fow_x_start, ex_y + 8, fow_x_start + 12, ex_y + 20], fill=accent)
        draw.text((fow_x_start + 22, ex_y + 3), "F A L L   O F   W I C K E T S",
                  fill=accent, font=f_ext_label)

        fow_y = ex_y + 38
        # Arrange FOW in 2 rows × 5 cols
        cols = 6
        col_width = (W - fow_x_start - 50) // cols
        for i, fow in enumerate(fall_of_wickets[:12]):
            if not fow: continue
            wicket_num = i + 1
            if len(fow) == 2:
                score_val, over_val = fow
            else:
                score_val = fow[0] if len(fow) > 0 else ""
                over_val = fow[1] if len(fow) > 1 else ""

            col = i % cols
            row = i // cols
            fx = fow_x_start + col * col_width
            fy = fow_y + row * 38

            # Wicket number (small dim)
            draw.text((fx, fy + 5), str(wicket_num), fill=DIM, font=_font(14, bold=True))
            # Score (big)
            draw.text((fx + 15, fy - 3), str(score_val), fill=TEXT, font=f_fow_num)
            # Over (small italic dim next to score)
            score_w_tmp = _tw(draw, str(score_val), f_fow_num)
            draw.text((fx + 15 + score_w_tmp + 5, fy + 6),
                      f"({over_val})", fill=DIM, font=f_fow_over)

        buf = io.BytesIO()
        img.save(buf, format="PNG", quality=95)
        buf.seek(0)
        return buf.getvalue()

    except Exception:
        logger.exception("Batting scorecard generation failed")
        return None


# ═══════════════════════════════════════════════════════════════════════
# BOWLING SCORECARD
# ═══════════════════════════════════════════════════════════════════════

def generate_bowling_scorecard(team_name, bowlers_rows, fall_of_wickets,
                               is_first_innings=True, match_title="MATCH") -> bytes | None:
    """Generate bowling scorecard.

    Args:
        team_name: bowling team
        bowlers_rows: list of dicts with keys:
            name, overs (str like "9.3"), maidens, runs_conceded, wickets, economy
        fall_of_wickets: list of tuples [(wicket_num, score, over), ...]
        is_first_innings: True = secondary (teal), False = primary (red)
            IMPORTANT: bowling color is OPPOSITE of batting — bowl1st = teal, bowl2nd = red
        match_title: subtitle text

    Returns PNG bytes.
    """
    try:
        # Bowl 1st = secondary (teal), Bowl 2nd = primary (red)
        accent = SECONDARY if is_first_innings else PRIMARY
        innings_label = "INNINGS 1" if is_first_innings else "INNINGS 2"

        W = 1700
        base_h = 280
        row_h = 68
        fow_h = 180
        H = base_h + len(bowlers_rows) * row_h + fow_h

        img = Image.new("RGB", (W, H), BG)
        _draw_gradient(img, BG, BG_DARK)
        draw = ImageDraw.Draw(img, "RGBA")

        f_innings_tag = _font(20, bold=True)
        f_brand = _font(16, bold=True, italic=True)
        f_match = _font(16, bold=True)
        f_col_header = _font(22, bold=True)
        f_bowler = _font(30, bold=True)
        f_stat_big = _font(36, bold=True)
        f_stat_dim = _font(30, bold=True)
        f_ext_label = _font(18, bold=True)
        f_fow_num = _font(30, bold=True)
        f_fow_over = _font(16, italic=True)
        f_fow_num_label = _font(14, bold=True)

        # ── LOGO (top-left) ────
        logo = _load_logo(target_size=130)
        logo_x = 45
        logo_y = 35
        if logo:
            img.paste(logo, (logo_x, logo_y), logo)
            content_start_x = logo_x + logo.size[0] + 25
        else:
            content_start_x = 45

        # ── Innings tag with accent line ────
        tag_y = 45
        draw.line([(content_start_x, tag_y + 12), (content_start_x + 55, tag_y + 12)],
                  fill=accent, width=4)
        tag_text = f"BOWLING  •  {innings_label}"
        draw.text((content_start_x + 70, tag_y), tag_text, fill=accent, font=f_innings_tag)

        tag_w = _tw(draw, tag_text, f_innings_tag)
        draw.text((content_start_x + 70 + tag_w + 30, tag_y + 2),
                  match_title.upper(), fill=DIM, font=f_match)

        brand = "CRICMASTERULTRA"
        brand_w = _tw(draw, brand, f_brand)
        draw.text((W - 50 - brand_w, tag_y + 2), brand, fill=DIM, font=f_brand)

        # ── Team name (BIG white italic bold) ────
        team_y = 85
        team_upper = team_name.upper()
        team_size = 96
        max_team_width = 900
        while team_size > 48:
            tf = _font(team_size, bold=True, italic=True)
            if _tw(draw, team_upper, tf) <= max_team_width:
                break
            team_size -= 6
        f_team_big = _font(team_size, bold=True, italic=True)
        draw.text((content_start_x, team_y), team_upper, fill=TEXT, font=f_team_big)
        team_w = _tw(draw, team_upper, f_team_big)

        # BOWLING label (muted, next to team)
        f_sub = _font(36, bold=True, italic=True)
        draw.text((content_start_x + team_w + 25, team_y + team_size // 2 - 10),
                  "BOWLING", fill=OPPONENT, font=f_sub)

        # ── Divider ────
        header_bot = 220
        draw.line([(50, header_bot), (W - 50, header_bot)], fill=ROW_SEP, width=1)

        # ── Column headers ────
        col_y = 240
        col_bowler_x = 50
        col_o_x = W - 650
        col_m_x = W - 520
        col_r_x = W - 390
        col_w_x = W - 260
        col_econ_x = W - 80

        draw.text((col_bowler_x, col_y), "B O W L E R", fill=accent, font=f_col_header)

        def r_align(x, text, font, fill):
            tw = _tw(draw, text, font)
            draw.text((x - tw, col_y), text, fill=fill, font=font)

        r_align(col_o_x, "O", f_col_header, accent)
        r_align(col_m_x, "M", f_col_header, accent)
        r_align(col_r_x, "R", f_col_header, accent)
        r_align(col_w_x, "W", f_col_header, accent)
        r_align(col_econ_x, "E C O N", f_col_header, accent)

        draw.line([(50, col_y + 32), (W - 50, col_y + 32)], fill=accent, width=1)

        # ── Bowlers ────
        row_y_start = col_y + 60
        for i, bw in enumerate(bowlers_rows):
            y = row_y_start + i * row_h

            draw.text((col_bowler_x, y), bw["name"].upper(), fill=TEXT, font=f_bowler)

            def r_align_row(x, text, font, fill):
                tw = _tw(draw, text, font)
                draw.text((x - tw, y + 4), text, fill=fill, font=font)

            overs_v = str(bw.get("overs", "0"))
            maidens_v = str(bw.get("maidens", 0))
            runs_v = str(bw.get("runs_conceded", 0))
            wickets_v = str(bw.get("wickets", 0))
            econ = bw.get("economy", 0)
            econ_v = f"{econ:.2f}" if isinstance(econ, (int, float)) else str(econ)

            # Highlight W in accent
            r_align_row(col_o_x, overs_v, f_stat_dim, DIM)
            r_align_row(col_m_x, maidens_v, f_stat_dim, DIM)
            r_align_row(col_r_x, runs_v, f_stat_dim, DIM)
            r_align_row(col_w_x, wickets_v, f_stat_big, accent)
            r_align_row(col_econ_x, econ_v, f_stat_dim, accent if (isinstance(econ, (int, float)) and econ < 5) else DIM)

            draw.line([(50, y + row_h - 8), (W - 50, y + row_h - 8)], fill=ROW_SEP, width=1)

        # ── FALL OF WICKETS ────
        footer_y = row_y_start + len(bowlers_rows) * row_h + 20
        draw.rectangle([0, footer_y - 2, W, footer_y], fill=(accent[0], accent[1], accent[2], 100))

        fow_header_y = footer_y + 20
        draw.ellipse([50, fow_header_y + 6, 62, fow_header_y + 18], fill=accent)
        draw.text((72, fow_header_y), "F A L L   O F   W I C K E T S", fill=accent, font=f_ext_label)

        fow_y = fow_header_y + 40
        cols = 6
        col_width = (W - 100) // cols
        for i, fow in enumerate(fall_of_wickets[:12]):
            if not fow: continue
            wicket_num = i + 1
            if len(fow) == 2:
                score_val, over_val = fow
            else:
                score_val = fow[0] if len(fow) > 0 else ""
                over_val = fow[1] if len(fow) > 1 else ""

            col = i % cols
            row = i // cols
            fx = 50 + col * col_width
            fy = fow_y + row * 45

            draw.text((fx, fy + 6), str(wicket_num), fill=DIM, font=f_fow_num_label)
            draw.text((fx + 18, fy - 4), str(score_val), fill=TEXT, font=f_fow_num)
            sw = _tw(draw, str(score_val), f_fow_num)
            draw.text((fx + 18 + sw + 6, fy + 2), "AT",
                      fill=DIM, font=_font(12, bold=True))
            draw.text((fx + 18 + sw + 6, fy + 18),
                      f"{over_val} ov", fill=accent, font=f_fow_over)

        buf = io.BytesIO()
        img.save(buf, format="PNG", quality=95)
        buf.seek(0)
        return buf.getvalue()

    except Exception:
        logger.exception("Bowling scorecard generation failed")
        return None
