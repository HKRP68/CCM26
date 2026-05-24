"""Match summary card — sent after the match ends.

Design follows the user-uploaded match_summary.html layout:
  - Header: brand + "MATCH SUMMARY" + team names
  - Two team sections (one per innings), each with:
      - Team name + total score + overs (right-aligned)
      - Top Batters table (4 rows)
      - Top Bowlers table (4 rows from opposing team)
  - Result bar: 🏆 RESULT text + POTM block
  - Footer: date + stadium

Color scheme: dark navy bg (#0d1117), red accents on innings 1 (lava red),
teal accents on innings 2 (lagoon teal), gold for trophy/winner.
"""

import io
import os
import logging
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Color palette
PRIMARY    = (196, 30, 58)       # Lava Red (innings 1)
SECONDARY  = (0, 201, 167)       # Lagoon Teal (innings 2)
GOLD       = (251, 191, 36)      # Trophy gold
BG         = (13, 17, 23)        # GitHub-dark / page bg
BG_DARK    = (8, 12, 20)
CARD_BG    = (20, 27, 39)
HEADER_BG  = (26, 10, 10)        # dark wine for innings-1 headers
TEXT       = (241, 245, 249)
DIM        = (148, 158, 178)
MUTED      = (95, 105, 125)
SEP        = (40, 50, 70)

_LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "assets", "logo.png")


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


def _gradient(img, top, bottom):
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


def _load_logo(target=80):
    try:
        if not os.path.exists(_LOGO_PATH):
            return None
        img = Image.open(_LOGO_PATH).convert("RGBA")
        img.thumbnail((target, target), Image.LANCZOS)
        return img
    except Exception:
        return None


def _draw_team_header(draw, x, y, w, team_name, score, wickets, overs,
                      accent, fonts):
    """Draw a team-section header row: [accent strip] TEAM NAME ... OVERS  R/W."""
    h = 70
    draw.rounded_rectangle([x, y, x + w, y + h], radius=10,
                            fill=CARD_BG, outline=SEP, width=1)
    draw.rounded_rectangle([x, y, x + 6, y + h], radius=3, fill=accent)

    text_x = x + 24
    text_y = y + h // 2 - 14
    # Truncate team name if absurdly long
    name_disp = team_name.upper()
    while _tw(draw, name_disp, fonts["team"]) > w // 2 and len(name_disp) > 6:
        name_disp = name_disp[:-2] + "…"
    draw.text((text_x, text_y), name_disp,
              fill=TEXT, font=fonts["team"])

    overs_text = f"{overs} OVERS"
    runs_text = str(score)
    sep_text = "/"
    wkts_text = str(wickets)

    score_font = fonts["score"]
    overs_font = fonts["overs"]

    runs_w = _tw(draw, runs_text, score_font)
    slash_w = _tw(draw, sep_text, score_font)
    wkts_w = _tw(draw, wkts_text, score_font)
    overs_w = _tw(draw, overs_text, overs_font)

    pad = 14
    right_x = x + w - 24
    wx = right_x - wkts_w
    sx = wx - pad - slash_w
    rx = sx - pad - runs_w
    ox = rx - 20 - overs_w
    sy = y + h // 2 - score_font.size // 2 - 2
    oy = y + h // 2 - overs_font.size // 2

    draw.text((ox, oy), overs_text, fill=DIM, font=overs_font)
    draw.text((rx, sy), runs_text, fill=TEXT, font=score_font)
    draw.text((sx, sy), sep_text, fill=accent, font=score_font)
    draw.text((wx, sy), wkts_text, fill=TEXT, font=score_font)

    return h


def _draw_top_table(draw, x, y, w, title, rows, accent, fonts,
                     numeric_cols=None):
    numeric_cols = numeric_cols or set()
    title_h = 30
    row_h = 30
    pad = 12
    table_h = title_h + (max(len(rows), 1) * row_h) + 8

    draw.rounded_rectangle([x, y, x + w, y + title_h], radius=8,
                            fill=accent)
    draw.text((x + pad, y + 6), title, fill=BG, font=fonts["table_title"])

    row_y = y + title_h + 6
    if not rows:
        draw.text((x + pad, row_y + 4),
                  "— no data —", fill=DIM, font=fonts["row"])
        return table_h

    cols = len(rows[0])
    if cols == 3:
        col_widths = [w - 130, 60, 60]
    elif cols == 5:
        col_widths = [w - 240, 55, 55, 55, 75]
    else:
        col_widths = [w // cols] * cols

    for r_idx, row in enumerate(rows):
        cx = x
        ry = row_y + r_idx * row_h
        for c_idx, cell in enumerate(row):
            cw = col_widths[c_idx]
            text = str(cell)
            if c_idx in numeric_cols:
                tw_v = _tw(draw, text, fonts["row"])
                draw.text((cx + cw - tw_v - pad, ry + 4),
                          text, fill=TEXT, font=fonts["row"])
            else:
                display = text
                while _tw(draw, display, fonts["row"]) > cw - pad * 2 and len(display) > 4:
                    display = display[:-2] + "…"
                draw.text((cx + pad, ry + 4),
                          display, fill=TEXT, font=fonts["row"])
            cx += cw

    return table_h


def _draw_result_bar(draw, x, y, w, *, winner_name, win_margin_text,
                     potm_name, potm_stats, fonts):
    h = 100
    draw.rounded_rectangle([x, y, x + w, y + h], radius=12,
                            fill=CARD_BG, outline=GOLD, width=2)

    trophy_size = 60
    trophy_x = x + 18
    trophy_y = y + (h - trophy_size) // 2
    draw.ellipse([trophy_x, trophy_y,
                  trophy_x + trophy_size, trophy_y + trophy_size],
                  fill=GOLD)
    trophy_txt = "🏆"
    tt_w = _tw(draw, trophy_txt, fonts["trophy"])
    draw.text((trophy_x + (trophy_size - tt_w) // 2,
               trophy_y + 8),
              trophy_txt, fill=BG, font=fonts["trophy"])

    text_x = trophy_x + trophy_size + 20
    label_y = y + 18
    draw.text((text_x, label_y), "RESULT",
              fill=DIM, font=fonts["res_label"])
    result_txt = f"{(winner_name or '—').upper()} WON {(win_margin_text or '').upper()}".strip()
    avail_w = w // 2 - 40
    while _tw(draw, result_txt, fonts["res_text"]) > avail_w and len(result_txt) > 10:
        result_txt = result_txt[:-2] + "…"
    draw.text((text_x, label_y + 20), result_txt,
              fill=TEXT, font=fonts["res_text"])

    div_x = x + w // 2 + 30
    draw.line([(div_x, y + 18), (div_x, y + h - 18)],
              fill=SEP, width=2)

    potm_x = div_x + 20
    draw.text((potm_x, label_y), "PLAYER OF THE MATCH",
              fill=DIM, font=fonts["res_label"])
    potm_display = (potm_name or "—").upper()
    avail_potm = w - (potm_x - x) - 200
    while _tw(draw, potm_display, fonts["potm_name"]) > avail_potm and len(potm_display) > 6:
        potm_display = potm_display[:-2] + "…"
    draw.text((potm_x, label_y + 20),
              potm_display, fill=TEXT, font=fonts["potm_name"])

    if potm_stats:
        stats_text = potm_stats
        sw = _tw(draw, stats_text, fonts["potm_stats"])
        draw.text((x + w - 20 - sw, y + h // 2 - 11),
                  stats_text, fill=GOLD, font=fonts["potm_stats"])

    return h


def generate_match_summary(*,
    inn1_team, inn1_runs, inn1_wickets, inn1_overs,
    inn2_team, inn2_runs, inn2_wickets, inn2_overs,
    winner_name, win_margin_text,
    overs_total,
    potm_name=None, potm_rating=None, potm_team=None,
    potm_stats=None, potm_impact=None,
    top_scorer=None,
    top_wicket=None,
    top_per_team=None,
    stadium=None,
    match_date=None,
    is_spectator=False,
) -> bytes | None:
    """Render the match summary card. Returns PNG bytes or None on failure."""
    try:
        W = 1400
        H = 1300

        img = Image.new("RGB", (W, H), BG)
        _gradient(img, BG, BG_DARK)
        draw = ImageDraw.Draw(img, "RGBA")

        f_brand = _font(26, bold=True, italic=True)
        f_title = _font(56, bold=True, italic=True)
        f_subtitle = _font(38, bold=True, italic=True)
        f_team = _font(44, bold=True, italic=True)
        f_score = _font(62, bold=True)
        f_overs = _font(22, bold=True)
        f_table_title = _font(18, bold=True)
        f_row = _font(22, bold=True)
        f_res_label = _font(14, bold=True)
        f_res_text = _font(24, bold=True, italic=True)
        f_potm_name = _font(22, bold=True)
        f_potm_stats = _font(28, bold=True)
        f_footer = _font(16)
        f_trophy = _font(36)

        # Header
        logo = _load_logo(target=100)
        header_y = 30
        if logo:
            img.paste(logo, (50, header_y), logo)
            brand_x = 170
        else:
            brand_x = 50

        draw.text((brand_x, header_y + 8), "CRICMASTERULTRA",
                  fill=GOLD, font=f_brand)
        draw.text((brand_x, header_y + 44), "MATCH SUMMARY",
                  fill=TEXT, font=f_title)

        right_y = header_y + 16
        if match_date:
            date_text = match_date.strftime("%d %b %Y").upper()
            dw = _tw(draw, date_text, f_overs)
            draw.text((W - 50 - dw, right_y),
                      date_text, fill=DIM, font=f_overs)
        if is_spectator:
            spec_text = "SPECTATOR MATCH"
            sw = _tw(draw, spec_text, f_overs)
            draw.text((W - 50 - sw, right_y + 36),
                      spec_text, fill=SECONDARY, font=f_overs)

        subtitle_y = header_y + 116
        sub_text = f"{inn1_team.upper()}  vs  {inn2_team.upper()}"
        sub_w = _tw(draw, sub_text, f_subtitle)
        draw.text(((W - sub_w) // 2, subtitle_y),
                  sub_text, fill=TEXT, font=f_subtitle)

        line_y = subtitle_y + 60
        draw.line([(W // 2 - 240, line_y), (W // 2 + 240, line_y)],
                  fill=GOLD, width=3)

        # Sections
        section_x = 50
        section_w = W - 100
        section_y = line_y + 30

        if not top_per_team:
            top_per_team = {
                "inn1": {"team": inn1_team, "bowl_team": inn2_team,
                         "batters": [], "bowlers": []},
                "inn2": {"team": inn2_team, "bowl_team": inn1_team,
                         "batters": [], "bowlers": []},
            }

        fonts = {
            "team": f_team, "score": f_score, "overs": f_overs,
            "table_title": f_table_title, "row": f_row,
            "res_label": f_res_label, "res_text": f_res_text,
            "potm_name": f_potm_name, "potm_stats": f_potm_stats,
            "trophy": f_trophy,
        }

        for inn_key, (runs, wkts, overs_s, accent) in (
            ("inn1", (inn1_runs, inn1_wickets, inn1_overs, PRIMARY)),
            ("inn2", (inn2_runs, inn2_wickets, inn2_overs, SECONDARY)),
        ):
            data = top_per_team.get(inn_key, {})
            team_n = data.get("team") or (
                inn1_team if inn_key == "inn1" else inn2_team)

            th_h = _draw_team_header(draw, section_x, section_y, section_w,
                                       team_n, runs, wkts, overs_s,
                                       accent, fonts)

            tables_y = section_y + th_h + 12
            gap = 20
            table_w = (section_w - gap) // 2

            batter_rows = []
            for b in data.get("batters", [])[:4]:
                name = b.get("name", "—")
                runs_str = f"{b.get('runs', 0)}{'*' if not b.get('out', False) else ''}"
                balls_str = f"({b.get('balls', 0)})"
                batter_rows.append([name, runs_str, balls_str])

            bat_h = _draw_top_table(draw, section_x, tables_y, table_w,
                                      "TOP BATTERS", batter_rows,
                                      accent, fonts, numeric_cols={1, 2})

            bowler_rows = []
            for bw in data.get("bowlers", [])[:4]:
                bowler_rows.append([
                    bw.get("name", "—"),
                    bw.get("overs", "0"),
                    str(bw.get("runs", 0)),
                    str(bw.get("wickets", 0)),
                    f"{bw.get('econ', 0):.2f}",
                ])
            bowl_h = _draw_top_table(draw,
                                       section_x + table_w + gap, tables_y,
                                       table_w,
                                       "TOP BOWLERS", bowler_rows,
                                       accent, fonts,
                                       numeric_cols={1, 2, 3, 4})

            section_y = tables_y + max(bat_h, bowl_h) + 25

        # Result bar
        result_y = section_y + 5
        rh = _draw_result_bar(draw, 50, result_y, W - 100,
                                 winner_name=winner_name,
                                 win_margin_text=win_margin_text,
                                 potm_name=potm_name,
                                 potm_stats=potm_stats,
                                 fonts=fonts)

        # Footer
        footer_y = result_y + rh + 15
        footer_bits = []
        if match_date:
            footer_bits.append(f"📅 {match_date.strftime('%d %b %Y').upper()}")
        if stadium:
            footer_bits.append(f"📍 {stadium.upper()}")
        footer_bits.append(f"🏏 {overs_total} OVER MATCH")
        footer_text = "  |  ".join(footer_bits)
        fw = _tw(draw, footer_text, f_footer)
        draw.text(((W - fw) // 2, footer_y),
                  footer_text, fill=DIM, font=f_footer)

        actual_h = footer_y + 50
        if actual_h < H:
            img = img.crop((0, 0, W, actual_h))

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    except Exception:
        logger.exception("Failed to render match summary card")
        return None
