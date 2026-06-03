"""Scorecard renderers for batting + bowling innings cards.

Layout matches the user-uploaded HTML mockups (Bat1st, bat2nd, Bowl1st,
BOWL-2-SCORECARD) with these enhancements:

  - Bot logo centered at the top of the header (instead of HTML's "BOT LOGO" placeholder)
  - Match number tracked and displayed in header
  - Per-batsman row color:
      not_out → subtle green tint (still readable)
      out     → subtle red tint
      dnb     → gray tint (player in XI but didn't bat)
  - Admin-customizable accent color per innings (passed in as accent_hex)

Color philosophy:
  - Accent color is admin-tunable per innings (header border, RTG col, etc.)
  - Row status colors (green/red/gray) are FIXED — they convey meaning
    and shouldn't be customizable.
"""

import io
import os
import logging
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ── Default accent colors (admin can override) ────────────────────────
PRIMARY_DEFAULT   = (196, 30, 58)        # Lava Red (innings 1)
SECONDARY_DEFAULT = (0, 201, 167)        # Lagoon Teal (innings 2)

# ── Fixed colors ──────────────────────────────────────────────────────
GOLD       = (251, 191, 36)
BG         = (8, 12, 20)                  # page bg (#080c14)
CARD_BG    = (13, 17, 23)                 # card bg (#0d1117)
CARD_BG_DK = (10, 13, 18)
ROW_ALT    = (20, 27, 39)                 # alternating row stripe (when no status)
ROW_OUT    = (60, 18, 24)                 # red-tinted bg for OUT rows
ROW_NOTOUT = (16, 60, 38)                 # green-tinted bg for NOT OUT rows
ROW_DNB    = (32, 36, 44)                 # gray-tinted bg for DNB rows
HEADER_BG  = (26, 10, 10)                 # innings header gradient start
HEADER_BG2 = (10, 26, 26)
TEXT       = (255, 255, 255)
TEXT_DIM   = (180, 190, 210)              # dimmed text for DNB rows
DIM        = (160, 174, 192)
MUTED      = (113, 128, 150)
SEP        = (45, 55, 72)
EXTRA_BG   = (15, 23, 42)

_LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "assets", "logo.png")


# ── Drawing helpers ────────────────────────────────────────────────────

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


def _tw(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _load_logo(target=60):
    try:
        if not os.path.exists(_LOGO_PATH):
            return None
        img = Image.open(_LOGO_PATH).convert("RGBA")
        img.thumbnail((target, target), Image.LANCZOS)
        return img
    except Exception:
        return None


def _hex_to_rgb(s, fallback):
    """Parse '#rrggbb' or 'rrggbb'. Returns (r, g, b) tuple."""
    if not s:
        return fallback
    s = s.strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 6:
        try:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
        except ValueError:
            return fallback
    return fallback


def _draw_horizontal_gradient(draw, x, y, w, h, c1, c2):
    for i in range(w):
        t = i / max(1, w - 1)
        r = int(c1[0] * (1 - t) + c2[0] * t)
        g = int(c1[1] * (1 - t) + c2[1] * t)
        b = int(c1[2] * (1 - t) + c2[2] * t)
        draw.line([(x + i, y), (x + i, y + h - 1)], fill=(r, g, b))


def _overs_to_balls(overs):
    """Convert cricket over notation (for example ``15.2``) to legal balls."""
    try:
        whole, _, partial = str(overs or "0").partition(".")
        return max(0, int(whole)) * 6 + max(0, int(partial or 0))
    except (TypeError, ValueError):
        return 0


def _draw_section_title(draw, x, y, w, title, subtitle, accent):
    """Add a compact section banner between the hero and the score table."""
    h = 42
    draw.rectangle([x, y, x + w, y + h], fill=EXTRA_BG)
    draw.rectangle([x, y, x + 5, y + h], fill=accent)
    draw.text((x + 18, y + 9), title, fill=TEXT, font=_font(16, bold=True))
    subtitle_font = _font(11, bold=True)
    subtitle_w = _tw(draw, subtitle, subtitle_font)
    draw.text((x + w - subtitle_w - 18, y + 13), subtitle,
              fill=accent, font=subtitle_font)
    return h


def _draw_metric_strip(draw, x, y, w, metrics, accent):
    """Render an evenly spaced row of match-summary metrics."""
    h = 68
    gap = 10
    metric_w = int((w - gap * (len(metrics) - 1)) / max(1, len(metrics)))
    for i, (label, value) in enumerate(metrics):
        bx = x + i * (metric_w + gap)
        draw.rounded_rectangle([bx, y, bx + metric_w, y + h], radius=8,
                               fill=EXTRA_BG, outline=SEP, width=1)
        draw.text((bx + 14, y + 11), label, fill=accent,
                  font=_font(10, bold=True))
        draw.text((bx + 14, y + 27), str(value), fill=TEXT,
                  font=_font(24, bold=True))
    return h


def _draw_logo_block(img, draw, x, y, size=60):
    """Render the bot logo in the header. Falls back to a dark square + text
    if assets/logo.png is missing."""
    logo = _load_logo(target=size)
    if logo:
        # Paste with alpha
        img.paste(logo, (x, y), logo)
        return
    # Fallback: simple square with brand
    draw.rounded_rectangle([x, y, x + size, y + size], radius=4,
                            fill=(40, 50, 70), outline=SEP, width=1)
    f_lbl = _font(11, bold=True)
    label = "BOT"
    label_w = _tw(draw, label, f_lbl)
    draw.text((x + (size - label_w) // 2, y + size // 2 - 7),
              label, fill=DIM, font=f_lbl)


def _draw_header(draw, img, *, x, y, w, accent, label, match_title, match_no,
                 home_team, away_team, score, wickets, overs,
                 bowling=False):
    """Shared header for batting + bowling scorecards.

    Layout (top to bottom, ~190px tall):
      ┌─ Brand strip (small) ──────────────────────────────────────┐
      │ CRICMASTERULTRA       [centered bot logo]     MATCH #42     │
      ├─ Main row ─────────────────────────────────────────────────┤
      │ [BAT 1 SCORECARD]                         230/10            │
      │ TEAM A vs TEAM B                         49.1 OVERS         │
      └────────────────────────────────────────────────────────────┘
                  ▲ accent-colored bottom border
    """
    H = 190
    # Gradient bg
    _draw_horizontal_gradient(draw, x, y, w, H, HEADER_BG, CARD_BG)
    # Bottom accent border
    draw.line([(x, y + H), (x + w, y + H)], fill=accent, width=3)

    # Fonts — bumped for mobile readability after Telegram compression
    f_brand = _font(16, bold=True, italic=True)
    f_match_no = _font(16, bold=True)
    f_badge = _font(18, bold=True)
    f_title = _font(42, bold=True, italic=True)
    f_vs = _font(28, bold=True, italic=True)
    f_score = _font(72, bold=True)
    f_overs = _font(18, bold=True)
    f_match_title = _font(14, bold=True)

    # ── Top strip: brand left, match-no chip right ──
    strip_pad_x = 24
    strip_y = y + 14

    # Brand text
    draw.text((x + strip_pad_x, strip_y), "CRICMASTERULTRA",
              fill=accent, font=f_brand)

    # Match no chip (top right) — rounded badge sized to font
    if match_no:
        mn_text = f"MATCH #{match_no}"
        mn_w = _tw(draw, mn_text, f_match_no)
        chip_pad = 12
        chip_w = mn_w + chip_pad * 2
        chip_h = 28
        chip_x = x + w - strip_pad_x - chip_w
        chip_y = strip_y - 4
        draw.rounded_rectangle(
            [chip_x, chip_y, chip_x + chip_w, chip_y + chip_h],
            radius=5, fill=accent)
        draw.text((chip_x + chip_pad, chip_y + 5),
                  mn_text, fill=BG, font=f_match_no)

    # Optional match-title text under the chip (small, dim) — e.g. "SUPER LEAGUE"
    if match_title and match_title.strip() and match_title.upper() != "MATCH":
        mt_text = match_title.upper()
        mt_w = _tw(draw, mt_text, f_match_title)
        draw.text((x + w - strip_pad_x - mt_w, strip_y + 32),
                  mt_text, fill=MUTED, font=f_match_title)

    # Centered bot logo at the top of the scorecard header. The website
    # preview route uses this same renderer, so keep the logo placement here
    # rather than in the Mini App scorecard markup.
    logo_size = 84
    logo_x = x + (w - logo_size) // 2
    logo_y = y + 10
    _draw_logo_block(img, draw, logo_x, logo_y, size=logo_size)

    # ── Main row: badge + title (left), score (right) ──
    main_y = y + 82

    # Badge with "BAT 1 SCORECARD" label, color-filled, sized to font
    badge_text = label
    badge_w = _tw(draw, badge_text, f_badge) + 28
    badge_h = 34
    badge_x = x + strip_pad_x
    badge_y = main_y + 4
    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=5, fill=accent)
    draw.text((badge_x + 14, badge_y + 8),
              badge_text, fill=BG, font=f_badge)

    # Title line (TEAM A vs TEAM B  or  TEAM BOWLING)
    title_y = badge_y + badge_h + 14
    if bowling:
        team_str = home_team.upper()
        tag_str = " BOWLING"
        draw.text((badge_x, title_y),
                  team_str, fill=TEXT, font=f_title)
        team_w = _tw(draw, team_str, f_title)
        draw.text((badge_x + team_w, title_y),
                  tag_str, fill=accent, font=f_title)
    else:
        a_text = home_team.upper()
        vs_text = "vs"
        b_text = away_team.upper()
        a_w = _tw(draw, a_text, f_title)
        v_w = _tw(draw, vs_text, f_vs)
        # vertically nudge "vs" down a bit because it's smaller
        vs_y_adjust = (f_title.size - f_vs.size) // 2 + 2

        draw.text((badge_x, title_y),
                  a_text, fill=TEXT, font=f_title)
        draw.text((badge_x + a_w + 14, title_y + vs_y_adjust),
                  vs_text, fill=DIM, font=f_vs)
        draw.text((badge_x + a_w + 14 + v_w + 14, title_y),
                  b_text, fill=accent, font=f_title)

    # ── Right side: score block (batting only) ──
    if not bowling:
        right_x = x + w - strip_pad_x
        runs_text = str(score)
        sep_text = "/"
        wkts_text = str(wickets)
        overs_text = f"{overs} OVERS"

        runs_w = _tw(draw, runs_text, f_score)
        sep_w = _tw(draw, sep_text, f_score)
        wkts_w = _tw(draw, wkts_text, f_score)
        overs_w = _tw(draw, overs_text, f_overs)

        score_total_w = runs_w + sep_w + wkts_w + 20
        score_y = main_y + 8
        sx = right_x - score_total_w

        draw.text((sx, score_y), runs_text, fill=TEXT, font=f_score)
        draw.text((sx + runs_w + 8, score_y), sep_text, fill=accent, font=f_score)
        draw.text((sx + runs_w + 8 + sep_w + 8, score_y), wkts_text,
                  fill=TEXT, font=f_score)
        draw.text((right_x - overs_w, score_y + 80),
                  overs_text, fill=DIM, font=f_overs)

    return H


def _draw_table_header(draw, x, y, w, columns, accent):
    h = 38
    draw.rectangle([x, y, x + w, y + h], fill=CARD_BG_DK)
    draw.line([(x, y + h), (x + w, y + h)], fill=accent, width=1)

    f_col = _font(12, bold=True)
    total_ratio = sum(c[1] for c in columns)
    pad = 14
    cx = x
    for label, ratio, align in columns:
        cw = int(w * ratio / total_ratio)
        if align == "l":
            draw.text((cx + pad, y + 12), label, fill=MUTED, font=f_col)
        elif align == "r":
            label_w = _tw(draw, label, f_col)
            draw.text((cx + cw - label_w - pad, y + 12),
                      label, fill=MUTED, font=f_col)
        else:
            label_w = _tw(draw, label, f_col)
            draw.text((cx + (cw - label_w) // 2, y + 12),
                      label, fill=MUTED, font=f_col)
        cx += cw

    return h


def _draw_table_row(draw, x, y, w, columns, values, accent, *,
                     status="out", rtg_color=None):
    """Draw one batting/bowling row.

    status ∈ {'out', 'not_out', 'dnb', None}:
      'out'     → red-tinted row bg
      'not_out' → green-tinted row bg
      'dnb'     → gray-tinted row bg + dimmed text
      None / 'plain' → no fill (used for bowling rows)
    """
    h = 44

    # Background fill based on status
    if status == "out":
        bg_color = ROW_OUT
    elif status == "not_out":
        bg_color = ROW_NOTOUT
    elif status == "dnb":
        bg_color = ROW_DNB
    else:
        bg_color = None

    if bg_color:
        draw.rectangle([x, y, x + w, y + h], fill=bg_color)

    # Bottom hairline
    draw.line([(x, y + h - 1), (x + w, y + h - 1)], fill=SEP, width=1)

    f_cell = _font(14, bold=True)
    f_name = _font(15, bold=True)
    f_dismissal = _font(12, italic=True)
    f_rtg = _font(13, bold=True)

    # Text color is dimmed for DNB rows
    base_text = TEXT_DIM if status == "dnb" else TEXT

    total_ratio = sum(c[1] for c in columns)
    pad = 14
    cx = x

    for c_idx, ((label, ratio, align), val) in enumerate(zip(columns, values)):
        cw = int(w * ratio / total_ratio)
        text_str = str(val)

        is_rtg = label == "RTG"
        is_name = label in ("BATSMAN", "BATTER", "BOWLER")
        is_dismissal = label == "DISMISSAL"
        is_runs = label == "R" and not is_name and not is_dismissal

        font_use = f_cell
        color = base_text

        if is_rtg:
            font_use = f_rtg
            # RTG colored by accent, but DNB rows dim it
            color = DIM if status == "dnb" else (rtg_color or accent)
        elif is_dismissal:
            font_use = f_dismissal
            # DNB shows "did not bat" — render in muted gray italic
            color = MUTED if status == "dnb" else DIM
        elif is_name:
            font_use = f_name
            # DNB names dim slightly
            color = TEXT_DIM if status == "dnb" else TEXT
        elif is_runs and status == "not_out":
            # not-out R column shows the runs in accent
            color = accent

        # If DNB row has zero-only stats, render those numbers as muted dashes
        if status == "dnb" and not is_name and not is_rtg and not is_dismissal:
            text_str = "—"
            color = MUTED

        display = text_str
        while _tw(draw, display, font_use) > cw - pad * 2 and len(display) > 4:
            display = display[:-2] + "…"

        if align == "l":
            draw.text((cx + pad, y + (h - font_use.size) // 2),
                      display, fill=color, font=font_use)
        elif align == "r":
            d_w = _tw(draw, display, font_use)
            draw.text((cx + cw - d_w - pad, y + (h - font_use.size) // 2),
                      display, fill=color, font=font_use)
        else:
            d_w = _tw(draw, display, font_use)
            draw.text((cx + (cw - d_w) // 2, y + (h - font_use.size) // 2),
                      display, fill=color, font=font_use)
        cx += cw

    return h


# ══════════════════════════════════════════════════════════════════════
#  BATTING SCORECARD
# ══════════════════════════════════════════════════════════════════════

def generate_batting_scorecard(team_name, opponent_name, total_runs, total_wickets,
                               overs_str, batsmen_rows, fall_of_wickets, extras_dict,
                               is_first_innings=True, match_title="MATCH",
                               target=None, chase_outcome=None,
                               *, match_no=None, accent_hex=None) -> bytes | None:
    """Generate batting scorecard.

    Args:
        team_name, opponent_name, total_runs, total_wickets, overs_str,
        batsmen_rows, fall_of_wickets, extras_dict — as before
        is_first_innings: True = red default accent, False = teal default
        match_title: subtitle text
        target, chase_outcome: for innings 2
        match_no: int, shown in header (e.g. "MATCH #42")
        accent_hex: '#rrggbb' override; if None, falls back to PRIMARY/SECONDARY

    batsmen_rows can include 'status' field:
        'out' / 'not_out' / 'dnb' (default 'out' if dismissal != 'not out',
        else 'not_out'). DNB rows render in gray.
    """
    try:
        default_accent = PRIMARY_DEFAULT if is_first_innings else SECONDARY_DEFAULT
        accent = _hex_to_rgb(accent_hex, default_accent)
        innings_label = "1ST INNINGS" if is_first_innings else "2ND INNINGS"
        label = f"{innings_label} · BATTING"

        W = 1400
        row_h = 44
        header_h = 190
        section_h = 42
        table_header_h = 38
        summary_h = 68
        bottom_h = 160
        H = (header_h + section_h + table_header_h + (len(batsmen_rows) * row_h)
             + summary_h + bottom_h + 50)

        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img, "RGBA")

        card_x, card_y = 20, 20
        card_w = W - 40
        card_h = H - 40
        draw.rounded_rectangle([card_x, card_y, card_x + card_w, card_y + card_h],
                                radius=10, fill=CARD_BG, outline=SEP, width=1)

        _draw_header(draw, img,
                     x=card_x, y=card_y, w=card_w,
                     accent=accent, label=label, match_title=match_title,
                     match_no=match_no,
                     home_team=team_name, away_team=opponent_name,
                     score=total_runs, wickets=total_wickets, overs=overs_str)

        table_x = card_x
        table_w = card_w
        section_y = card_y + header_h
        _draw_section_title(draw, table_x, section_y, table_w,
                            "BATTING CARD", f"{team_name.upper()} INNINGS", accent)
        table_y = section_y + section_h

        cols = [
            ("RTG", 0.07, "c"),
            ("BATTER", 0.22, "l"),
            ("DISMISSAL", 0.30, "l"),
            ("R", 0.07, "r"),
            ("B", 0.07, "r"),
            ("4S", 0.07, "r"),
            ("6S", 0.07, "r"),
            ("SR", 0.13, "r"),
        ]
        _draw_table_header(draw, table_x, table_y, table_w, cols, accent)

        row_y = table_y + table_header_h
        for b in batsmen_rows:
            # Determine status — caller may set it explicitly, else infer
            status = b.get("status")
            if not status:
                dism = (b.get("dismissal") or "").lower()
                if dism in ("did not bat", "dnb"):
                    status = "dnb"
                elif dism == "not out":
                    status = "not_out"
                else:
                    status = "out"

            if status == "not_out":
                runs_text = f"{b.get('runs', 0)}*"
            elif status == "dnb":
                runs_text = "0"
            else:
                runs_text = str(b.get("runs", 0))

            values = [
                str(b.get("rating", "—")),
                b.get("name", "—").upper(),
                b.get("dismissal", "—"),
                runs_text,
                str(b.get("balls", 0)),
                str(b.get("fours", 0)),
                str(b.get("sixes", 0)),
                f"{b.get('strike_rate', 0):.1f}",
            ]
            _draw_table_row(draw, table_x, row_y, table_w, cols, values,
                             accent, status=status)
            row_y += row_h

        # Summary strip + bottom section: EXTRAS + FALL OF WICKETS
        summary_x = card_x + 20
        summary_w = card_w - 40
        legal_balls = _overs_to_balls(overs_str)
        run_rate = (total_runs * 6 / legal_balls) if legal_balls else 0
        _draw_metric_strip(draw, summary_x, row_y + 12, summary_w, [
            ("TOTAL", f"{total_runs}/{total_wickets}"),
            ("OVERS", overs_str),
            ("RUN RATE", f"{run_rate:.2f}"),
        ], accent)
        bottom_y = row_y + 12 + summary_h + 12
        bottom_padding = 20

        extras_w = 240
        extras_h = 110
        extras_x = card_x + bottom_padding
        draw.rounded_rectangle(
            [extras_x, bottom_y, extras_x + extras_w, bottom_y + extras_h],
            radius=8, fill=EXTRA_BG, outline=SEP, width=1)

        f_extras_lbl = _font(11, bold=True)
        f_extras_val = _font(38, bold=True)
        f_extras_detail = _font(11, bold=True)

        draw.text((extras_x + 16, bottom_y + 12),
                  "EXTRAS", fill=accent, font=f_extras_lbl)
        draw.text((extras_x + 16, bottom_y + 28),
                  str(extras_dict.get("total", 0)),
                  fill=TEXT, font=f_extras_val)

        detail_y = bottom_y + 80
        detail_text = "    ".join([
            f"WD {extras_dict.get('wd', 0)}",
            f"NB {extras_dict.get('nb', 0)}",
            f"B {extras_dict.get('b', 0)}",
            f"LB {extras_dict.get('lb', 0)}",
        ])
        draw.text((extras_x + 16, detail_y),
                  detail_text, fill=DIM, font=f_extras_detail)

        fow_x = extras_x + extras_w + 20
        fow_w = card_x + card_w - fow_x - bottom_padding
        draw.rounded_rectangle(
            [fow_x, bottom_y, fow_x + fow_w, bottom_y + extras_h],
            radius=8, fill=EXTRA_BG, outline=SEP, width=1)

        draw.text((fow_x + 16, bottom_y + 12),
                  "FALL OF WICKETS", fill=accent, font=f_extras_lbl)

        f_fow_num = _font(15, bold=True)
        f_fow_score = _font(15, bold=True)
        f_fow_over = _font(10, italic=True)
        if fall_of_wickets:
            available_w = fow_w - 32
            num_fow = min(len(fall_of_wickets), 10)
            pill_w = available_w / max(num_fow, 1)
            pill_y = bottom_y + 42

            for i, fow in enumerate(fall_of_wickets[:10]):
                if isinstance(fow, dict):
                    num = fow.get("num", i + 1)
                    score_v = fow.get("score", "—")
                    over_v = fow.get("over", "—")
                elif isinstance(fow, (tuple, list)):
                    num = fow[0] if len(fow) > 0 else i + 1
                    score_v = fow[1] if len(fow) > 1 else "—"
                    over_v = fow[2] if len(fow) > 2 else "—"
                else:
                    continue
                pill_x = fow_x + 16 + i * pill_w + pill_w / 2

                num_text = str(num)
                num_w = _tw(draw, num_text, f_fow_num)
                draw.text((int(pill_x - num_w / 2), pill_y),
                          num_text, fill=accent, font=f_fow_num)
                draw.line([(int(pill_x), pill_y + 22),
                           (int(pill_x), pill_y + 30)],
                          fill=SEP, width=1)
                sc_text = str(score_v)
                sc_w = _tw(draw, sc_text, f_fow_score)
                draw.text((int(pill_x - sc_w / 2), pill_y + 30),
                          sc_text, fill=TEXT, font=f_fow_score)
                ov_text = f"({over_v})"
                ov_w = _tw(draw, ov_text, f_fow_over)
                draw.text((int(pill_x - ov_w / 2), pill_y + 52),
                          ov_text, fill=DIM, font=f_fow_over)
        else:
            f_none = _font(13, italic=True)
            draw.text((fow_x + 16, bottom_y + 50),
                      "No wickets fell", fill=DIM, font=f_none)

        if target is not None and not is_first_innings:
            tgt_y = bottom_y + extras_h + 8
            tgt_label = f"TARGET: {target}"
            outcome_txt = ""
            if chase_outcome == "won":
                outcome_txt = " · CHASE COMPLETED ✓"
            elif chase_outcome == "tied":
                outcome_txt = " · MATCH TIED"
            elif chase_outcome == "lost":
                outcome_txt = " · TARGET MISSED"
            full = tgt_label + outcome_txt
            f_tgt = _font(13, bold=True)
            tw_val = _tw(draw, full, f_tgt)
            draw.text(((W - tw_val) // 2, tgt_y),
                      full, fill=accent, font=f_tgt)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Failed to render batting scorecard")
        return None


# ══════════════════════════════════════════════════════════════════════
#  BOWLING SCORECARD
# ══════════════════════════════════════════════════════════════════════

def generate_bowling_scorecard(team_name, bowlers_rows, fall_of_wickets,
                                is_first_innings=True, match_title="MATCH",
                                opponent_name=None, opp_score=None,
                                opp_wickets=None, opp_overs=None,
                                stadium=None,
                                *, match_no=None, accent_hex=None) -> bytes | None:
    """Generate bowling scorecard."""
    try:
        default_accent = PRIMARY_DEFAULT if is_first_innings else SECONDARY_DEFAULT
        accent = _hex_to_rgb(accent_hex, default_accent)
        innings_label = "1ST INNINGS" if is_first_innings else "2ND INNINGS"
        label = f"{innings_label} · BOWLING"

        W = 1400
        row_h = 44
        header_h = 190
        section_h = 42
        table_header_h = 38
        summary_h = 68
        bottom_h = 120
        footer_h = 40
        H = (header_h + section_h + table_header_h + (len(bowlers_rows) * row_h)
             + summary_h + bottom_h + footer_h + 50)

        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img, "RGBA")

        card_x, card_y = 20, 20
        card_w = W - 40
        card_h = H - 40
        draw.rounded_rectangle([card_x, card_y, card_x + card_w, card_y + card_h],
                                radius=10, fill=CARD_BG, outline=SEP, width=1)

        _draw_header(draw, img,
                     x=card_x, y=card_y, w=card_w,
                     accent=accent, label=label, match_title=match_title,
                     match_no=match_no,
                     home_team=team_name,
                     away_team=opponent_name or "OPPONENT",
                     score=0, wickets=0, overs="",
                     bowling=True)

        table_x = card_x
        table_w = card_w
        section_y = card_y + header_h
        _draw_section_title(draw, table_x, section_y, table_w,
                            "BOWLING CARD", f"{team_name.upper()} ATTACK", accent)
        table_y = section_y + section_h

        cols = [
            ("BOWLER", 0.40, "l"),
            ("O", 0.10, "r"),
            ("M", 0.10, "r"),
            ("R", 0.13, "r"),
            ("W", 0.12, "r"),
            ("ECON", 0.15, "r"),
        ]
        _draw_table_header(draw, table_x, table_y, table_w, cols, accent)

        row_y = table_y + table_header_h
        for i, b in enumerate(bowlers_rows):
            stripe = (i % 2 == 1)
            values = [
                b.get("name", "—").upper(),
                str(b.get("overs", "0")),
                str(b.get("maidens", 0)),
                str(b.get("runs_conceded", 0)),
                str(b.get("wickets", 0)),
                f"{b.get('economy', 0):.2f}",
            ]
            # Bowling rows don't have out/not-out — use plain alternating stripe
            if stripe:
                draw.rectangle([table_x, row_y, table_x + table_w, row_y + row_h],
                                fill=ROW_ALT)
            draw.line([(table_x, row_y + row_h - 1),
                        (table_x + table_w, row_y + row_h - 1)],
                       fill=SEP, width=1)

            f_cell = _font(14, bold=True)
            f_name = _font(15, bold=True)
            f_special = _font(14, bold=True)
            total_ratio = sum(c[1] for c in cols)
            pad = 14
            cx = table_x
            for c_idx, (col_def, val) in enumerate(zip(cols, values)):
                cw = int(table_w * col_def[1] / total_ratio)
                col_label, _, align = col_def

                font_use = f_name if col_label == "BOWLER" else f_cell
                color = TEXT
                # Highlight W in accent
                if col_label == "W":
                    color = accent
                    font_use = f_special
                # Highlight ECON in accent
                elif col_label == "ECON":
                    color = accent
                    font_use = f_special

                display = str(val)
                while _tw(draw, display, font_use) > cw - pad * 2 and len(display) > 4:
                    display = display[:-2] + "…"

                if align == "l":
                    draw.text((cx + pad, row_y + (row_h - font_use.size) // 2),
                              display, fill=color, font=font_use)
                elif align == "r":
                    d_w = _tw(draw, display, font_use)
                    draw.text((cx + cw - d_w - pad,
                                row_y + (row_h - font_use.size) // 2),
                              display, fill=color, font=font_use)
                else:
                    d_w = _tw(draw, display, font_use)
                    draw.text((cx + (cw - d_w) // 2,
                                row_y + (row_h - font_use.size) // 2),
                              display, fill=color, font=font_use)
                cx += cw

            row_y += row_h

        # Bowling summary + opponent innings panel
        summary_x = card_x + 20
        summary_w = card_w - 40
        bowling_balls = sum(_overs_to_balls(b.get("overs", "0")) for b in bowlers_rows)
        conceded = sum(b.get("runs_conceded", 0) for b in bowlers_rows)
        team_economy = (conceded * 6 / bowling_balls) if bowling_balls else 0
        _draw_metric_strip(draw, summary_x, row_y + 12, summary_w, [
            ("WICKETS", sum(b.get("wickets", 0) for b in bowlers_rows)),
            ("MAIDENS", sum(b.get("maidens", 0) for b in bowlers_rows)),
            ("TEAM ECONOMY", f"{team_economy:.2f}"),
        ], accent)
        opp_y = row_y + 12 + summary_h + 12
        opp_x = card_x + 20
        opp_w = card_w - 40
        opp_h = 80
        draw.rounded_rectangle([opp_x, opp_y, opp_x + opp_w, opp_y + opp_h],
                                radius=8, fill=EXTRA_BG, outline=SEP, width=1)

        f_lbl = _font(11, bold=True)
        f_team = _font(20, bold=True, italic=True)
        f_score = _font(34, bold=True)
        f_overs_sm = _font(13, bold=True)

        draw.text((opp_x + 20, opp_y + 14),
                  "RUN SCORED BY OPPONENTS", fill=accent, font=f_lbl)

        content_y = opp_y + 34
        if opponent_name:
            opp_t = opponent_name.upper()
            draw.text((opp_x + 20, content_y + 4),
                      opp_t, fill=TEXT, font=f_team)
            team_w = _tw(draw, opp_t, f_team)

            if opp_score is not None:
                s_str = str(opp_score)
                sep_str = "/"
                w_str = str(opp_wickets or 0)
                o_str = f"({opp_overs or '?'} OVERS)"

                sx = opp_x + 20 + team_w + 30
                draw.text((sx, content_y - 4),
                          s_str, fill=TEXT, font=f_score)
                rw = _tw(draw, s_str, f_score)
                draw.text((sx + rw + 6, content_y - 4),
                          sep_str, fill=accent, font=f_score)
                sepw = _tw(draw, sep_str, f_score)
                draw.text((sx + rw + 6 + sepw + 6, content_y - 4),
                          w_str, fill=TEXT, font=f_score)
                wkw = _tw(draw, w_str, f_score)
                draw.text((sx + rw + 6 + sepw + 6 + wkw + 18,
                            content_y + 12),
                          o_str, fill=DIM, font=f_overs_sm)

        footer_y = opp_y + opp_h + 12
        if stadium:
            f_footer = _font(12, bold=True)
            footer_text = stadium.upper()
            fw = _tw(draw, footer_text, f_footer)
            draw.text(((W - fw) // 2, footer_y),
                      footer_text, fill=DIM, font=f_footer)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Failed to render bowling scorecard")
        return None
