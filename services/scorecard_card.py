"""Scorecard renderers for batting + bowling innings cards.

Layout matches the user-uploaded HTML mockups (Bat1st, bat2nd, Bowl1st,
BOWL-2-SCORECARD) with these enhancements:

  - Bot logo in the header (instead of HTML's "BOT LOGO" placeholder)
  - Match number tracked and displayed in header
  - Per-batsman row color:
      not_out → subtle green tint (still readable)
      out     → subtle red tint
      dnb     → gray tint (player in XI but didn't bat)
  - Admin-customizable accent color per innings (passed in as accent_hex)
  - Font family/size/style follows Batting Scorecard.html where available:
      Bebas Neue for display labels, Bricolage Grotesque for table/body text

Color philosophy:
  - Accent color is admin-tunable per innings (header border, RTG col, etc.)
  - Row status colors (green/red/gray) are FIXED — they convey meaning
    and shouldn't be customizable.
"""

import io
import os
import logging
import json
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

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGO_PATH = os.path.join(_ROOT_DIR, "assets", "logo.png")
_FONT_DIR = os.path.join(_ROOT_DIR, "assets", "fonts")

# The HTML mockup imports Bebas Neue for big condensed display text and
# Bricolage Grotesque for table/body text.  The renderer first looks for
# vendored fonts in assets/fonts, then for system fonts installed by Docker,
# and finally falls back to DejaVu so scorecards still render everywhere.
_BEBAS_FONT_CANDIDATES = (
    os.path.join(_FONT_DIR, "BebasNeue-Regular.ttf"),
    "/usr/share/fonts/truetype/bebas-neue/BebasNeue-Regular.ttf",
    "/usr/share/fonts/opentype/bebas-neue/BebasNeue-Regular.otf",
)
_BRICOLAGE_FONT_CANDIDATES = (
    os.path.join(_FONT_DIR, "BricolageGrotesque-SemiBold.ttf"),
    os.path.join(_FONT_DIR, "BricolageGrotesque-Bold.ttf"),
    os.path.join(_FONT_DIR, "BricolageGrotesque-ExtraBold.ttf"),
    os.path.join(_FONT_DIR, "BricolageGrotesque.ttf"),
)


def _first_existing(paths):
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None


def _load_font(path, size):
    try:
        if path:
            return ImageFont.truetype(path, size)
    except (OSError, IOError):
        pass
    return None


# ── Drawing helpers ────────────────────────────────────────────────────

def _font(size, bold=False, italic=False, family="body"):
    if family == "display":
        display_font = _load_font(_first_existing(_BEBAS_FONT_CANDIDATES), size)
        if display_font:
            return display_font
    elif family == "body":
        body_font = _load_font(_first_existing(_BRICOLAGE_FONT_CANDIDATES), size)
        if body_font:
            return body_font

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



def _draw_text_with_tracking(draw, xy, text, *, fill, font, tracking=0):
    """Draw text with CSS-like letter spacing for mockup fidelity."""
    if not tracking:
        draw.text(xy, text, fill=fill, font=font)
        return
    x, y = xy
    for char in str(text):
        draw.text((x, y), char, fill=fill, font=font)
        x += _tw(draw, char, font) + tracking


def _tracked_width(draw, text, font, tracking=0):
    text = str(text)
    if not text:
        return 0
    return sum(_tw(draw, char, font) for char in text) + tracking * max(0, len(text) - 1)


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




SCORECARD_FONT_OPTIONS = {
    "display": "Bebas Neue",
    "body": "Bricolage Grotesque",
    "fallback": "DejaVu Sans",
}

# Editable text objects exposed in the website designer.  The first group keeps
# the existing grouped controls for backwards compatibility; the later keys map
# to the exact text objects from the supplied Batting/Bowling Scorecard HTML
# JSON samples so admins can tune each header/name independently.
SCORECARD_TEXT_FIELDS = {
    "batting": [
        ("team", "Team names", "TEAM"),
        ("tag", "BATTING green tag", "BATTING"),
        ("venue", "Venue / match text", "MATCH"),
        ("table_header", "All table headers", ""),
        ("header_batter", "BATTER", "BATTER"),
        ("header_dismissal", "DISMISSAL", "DISMISSAL"),
        ("header_runs", "R", "R"),
        ("header_balls", "B", "B"),
        ("header_fours", "4", "4"),
        ("header_sixes", "6", "6"),
        ("header_sr", "SR", "SR"),
        ("player_name", "BATTER NAME", "BATTER NAME"),
        ("dismissal_text", "Dismissal row text", "DISMISSAL"),
        ("table_body", "Other table row numbers", ""),
        ("stat_label", "Bottom stat labels", ""),
        ("stat_value", "Bottom stat values", ""),
        ("target", "Target / chase line", "TARGET"),
    ],
    "bowling": [
        ("team", "Team names", "TEAM"),
        ("tag", "BOWLING green tag", "BOWLING"),
        ("table_header", "All table headers", ""),
        ("bowler_name", "Bowler NAME", "Bowler NAME"),
        ("header_overs", "OVERS", "OVERS"),
        ("header_dots", "DOTS", "DOTS"),
        ("header_runs", "RUNS", "RUNS"),
        ("header_wickets", "WICKETS", "WICKETS"),
        ("header_economy", "ECONOMY", "ECONOMY"),
        ("table_body", "Other bowling row numbers", ""),
        ("fow_title", "Fall of Wickets title", "FALL OF WICKETS"),
        ("fow_body", "Fall of Wickets cells", ""),
        ("stat_label", "Bottom stat labels", ""),
        ("stat_value", "Bottom stat values", ""),
    ],
    "summary": [
        ("header_title", "SUMMARY header", "SUMMARY"),
        ("match_no", "MATCH # header", ""),
        ("stadium", "Stadium bar", ""),
        ("innings_team", "Innings team names", ""),
        ("innings_meta", "Overs text", ""),
        ("innings_score", "Score text", ""),
        ("row_name", "Batter/Bowler names", ""),
        ("row_number", "Runs/Balls/Wickets/Overs values", ""),
        ("result", "Result bar", ""),
        ("potm_badge", "POTM badge", "POTM"),
        ("potm_name", "POTM name", ""),
        ("potm_label", "Performance label", "PERFORMANCE:"),
        ("potm_value", "Performance value", ""),
    ],
}


def default_scorecard_text_settings():
    """Return editable text controls for the website scorecard preview page."""
    settings = {}
    for card_type, fields in SCORECARD_TEXT_FIELDS.items():
        settings[card_type] = {}
        for field in fields:
            key, _label = field[:2]
            default_text = field[2] if len(field) > 2 else ""
            summary_display = {
                "header_title", "match_no", "stadium", "innings_team",
                "innings_meta", "innings_score", "result", "potm_badge",
                "potm_name", "potm_label", "potm_value",
            }
            settings[card_type][key] = {
                "text": default_text,
                "font": "display" if key in {"team", "stat_value"} or (card_type == "summary" and key in summary_display) else "body",
                "size": 0,
                "x": 0,
                "y": 0,
            }
    return settings


def normalize_scorecard_text_settings(value):
    """Merge saved JSON controls with defaults, coercing safe numeric ranges."""
    defaults = default_scorecard_text_settings()
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except Exception:
            value = {}
    if not isinstance(value, dict):
        value = {}
    for card_type, fields in defaults.items():
        src_card = value.get(card_type) if isinstance(value.get(card_type), dict) else {}
        for key, default in fields.items():
            src = src_card.get(key) if isinstance(src_card.get(key), dict) else {}
            font = str(src.get("font") or default["font"]).lower()
            if font not in SCORECARD_FONT_OPTIONS:
                font = default["font"]
            text = str(src.get("text", default.get("text", "")) or "")[:80]
            merged = {"text": text, "font": font}
            for n, lo, hi in (("size", -30, 60), ("x", -300, 300), ("y", -300, 300)):
                try:
                    merged[n] = max(lo, min(hi, int(src.get(n, default[n]))))
                except (TypeError, ValueError):
                    merged[n] = default[n]
            fields[key] = merged
    return defaults


def _settings_for(text_settings, card_type, key):
    return normalize_scorecard_text_settings(text_settings).get(card_type, {}).get(key, {
        "font": "body", "size": 0, "x": 0, "y": 0,
    })


def _ui_font_family(setting):
    font = setting.get("font")
    if font in ("display", "fallback"):
        return font
    return "body"


def _font_from_setting(text_settings, card_type, key, base_size, *, bold=False, italic=False, family=None):
    setting = _settings_for(text_settings, card_type, key)
    use_family = family or _ui_font_family(setting)
    return _font(max(6, base_size + int(setting.get("size", 0))), bold=bold, italic=italic, family=use_family)


def _offset_xy(text_settings, card_type, key, x, y):
    setting = _settings_for(text_settings, card_type, key)
    return x + int(setting.get("x", 0)), y + int(setting.get("y", 0))


def _text_from_setting(text_settings, card_type, key, default):
    setting = _settings_for(text_settings, card_type, key)
    text = str(setting.get("text", "") or "")
    return text if text else default

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

    Layout (top to bottom, ~140px tall):
      ┌─ Brand strip (small) ──────────────────────────────────────┐
      │ CRICMASTERULTRA                              MATCH #42      │
      ├─ Main row ─────────────────────────────────────────────────┤
      │ [logo]  [BAT 1 SCORECARD]                  230/10           │
      │         TEAM A vs TEAM B                  49.1 OVERS        │
      └────────────────────────────────────────────────────────────┘
                  ▲ accent-colored bottom border
    """
    H = 170
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

    # ── Main row: logo + badge + title (left), score (right) ──
    main_y = y + 56

    # Logo (left) — bumped up to match larger fonts
    logo_size = 100
    logo_x = x + strip_pad_x
    logo_y = main_y
    _draw_logo_block(img, draw, logo_x, logo_y, size=logo_size)

    # Badge with "BAT 1 SCORECARD" label, color-filled, sized to font
    badge_text = label
    badge_w = _tw(draw, badge_text, f_badge) + 28
    badge_h = 34
    badge_x = logo_x + logo_size + 22
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
                               overs_str, batsmen_rows, fall_of_wickets,
                               extras_dict, is_first_innings=True,
                               match_title="MATCH", target=None,
                               chase_outcome=None, stadium=None,
                               *, match_no=None, accent_hex=None, text_settings=None) -> bytes | None:
    """Generate an HTML-mockup-style batting scorecard.

    The visual follows ``Batting Scorecard.html``: a wide glass card, top team
    name boxes with a green "BATTING" marker on the batting side, venue strip,
    batting table, and large stat tiles at the bottom. ``accent_hex`` remains
    admin-configurable and is used for headers, totals, and highlights.
    """
    try:
        default_accent = PRIMARY_DEFAULT if is_first_innings else SECONDARY_DEFAULT
        accent = _hex_to_rgb(accent_hex, default_accent)
        batting_green = (54, 219, 120)
        out_red = (255, 70, 80)
        notout_blue = (39, 184, 255)
        gold = GOLD

        W = 1600
        H = 1024
        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img, "RGBA")

        draw.ellipse([-220, -190, 520, 440], fill=(accent[0], accent[1], accent[2], 48))
        draw.ellipse([1080, -180, 1840, 460], fill=(0, 145, 255, 42))
        draw.rounded_rectangle([22, 22, W - 22, H - 22], radius=30,
                               fill=(7, 13, 20, 218), outline=(255, 255, 255, 45), width=1)
        draw.rounded_rectangle([34, 34, W - 34, H - 34], radius=24,
                               outline=(255, 255, 255, 26), width=1)

        card_x, card_y = 34, 34
        card_w = W - 68
        top_h = 150
        top_y = card_y

        draw.rounded_rectangle([card_x, top_y, card_x + card_w, top_y + top_h], radius=24,
                               fill=(255, 255, 255, 13), outline=(255, 255, 255, 44), width=1)
        draw.rectangle([card_x, top_y, card_x + card_w // 2, top_y + top_h],
                       fill=(accent[0], accent[1], accent[2], 22))
        draw.rectangle([card_x + card_w // 2, top_y, card_x + card_w, top_y + top_h],
                       fill=(0, 140, 255, 18))

        f_team = _font_from_setting(text_settings, "batting", "team", 66, family="display")
        f_tag = _font_from_setting(text_settings, "batting", "tag", 14, bold=True)
        f_logo = _font(22, bold=True, italic=True)
        f_meta = _font(15, bold=True)
        f_venue = _font_from_setting(text_settings, "batting", "venue", 23, bold=True)
        # Match the HTML table typography: header text is large/italic,
        # while body cells use a heavy Bricolage-style weight.  Dismissals are
        # intentionally bold (not regular italic) so they do not shrink/fade in
        # the rendered PNG compared with the browser mockup.
        f_th = _font_from_setting(text_settings, "batting", "table_header", 26, bold=True, italic=True)
        f_name = _font_from_setting(text_settings, "batting", "player_name", 31, bold=True)
        f_dism = _font_from_setting(text_settings, "batting", "dismissal_text", 22, bold=True)
        f_dism_notout = _font_from_setting(text_settings, "batting", "dismissal_text", 22, bold=True, italic=True)
        f_cell = _font_from_setting(text_settings, "batting", "table_body", 25, bold=True)
        f_stat_sub = _font_from_setting(text_settings, "batting", "stat_label", 14, bold=True)
        f_stat_label = _font_from_setting(text_settings, "batting", "stat_label", 22, bold=True)
        f_stat_note = _font_from_setting(text_settings, "batting", "stat_label", 14)
        table_header_tracking = 2
        table_body_tracking = 1

        def fit_text(text, font, max_w):
            text = str(text or "—").upper()
            if _tw(draw, text, font) <= max_w:
                return text
            while len(text) > 3 and _tw(draw, text + "…", font) > max_w:
                text = text[:-1]
            return text + "…"

        def text_vcenter_y(y1, y2, font):
            bb = draw.textbbox((0, 0), "Hg", font=font)
            text_h = bb[3] - bb[1]
            return y1 + ((y2 - y1 - text_h) // 2) - bb[1]

        def draw_team_box(x1, y1, x2, y2, name, active=False):
            if active:
                draw.rounded_rectangle([x1, y1, x2, y2], radius=22,
                                       fill=(47, 208, 111, 46),
                                       outline=(80, 255, 155, 112), width=2)
                tag_x1, tag_y1 = _offset_xy(text_settings, "batting", "tag", x1 + 20, y1 - 12)
                draw.rounded_rectangle([tag_x1, tag_y1, tag_x1 + 108, tag_y1 + 32],
                                       radius=14, fill=batting_green)
                tx, ty = _offset_xy(text_settings, "batting", "tag", x1 + 33, y1 - 4)
                draw.text((tx, ty), _text_from_setting(text_settings, "batting", "tag", "BATTING"), fill=(7, 20, 13), font=f_tag)
            else:
                draw.rounded_rectangle([x1, y1, x2, y2], radius=22,
                                       fill=(255, 255, 255, 8),
                                       outline=(255, 255, 255, 24), width=1)
            txt = fit_text(name, f_team, x2 - x1 - 52)
            tw = _tw(draw, txt, f_team)
            tx, ty = _offset_xy(text_settings, "batting", "team", x1 + (x2 - x1 - tw) // 2, y1 + 18)
            draw.text((tx, ty), txt, fill=(238, 243, 251), font=f_team)

        left_x1, left_x2 = card_x + 40, card_x + 40 + 520
        right_x2, right_x1 = card_x + card_w - 40, card_x + card_w - 40 - 520
        box_y1, box_y2 = top_y + 30, top_y + 122
        draw_team_box(left_x1, box_y1, left_x2, box_y2, team_name, active=True)
        draw_team_box(right_x1, box_y1, right_x2, box_y2, opponent_name, active=False)

        logo_size = 118
        logo_x = card_x + (card_w - logo_size) // 2
        logo_y = top_y + 18
        logo = _load_logo(target=logo_size)
        if logo:
            img.paste(logo, (logo_x, logo_y), logo)
        else:
            draw.rounded_rectangle([logo_x, logo_y, logo_x + logo_size, logo_y + logo_size],
                                   radius=18, fill=(18, 28, 44), outline=(255, 255, 255, 42), width=1)
            bot = "BOT"
            draw.text((logo_x + (logo_size - _tw(draw, bot, f_logo)) // 2, logo_y + 44),
                      bot, fill=accent, font=f_logo)
        if match_no:
            mt = f"MATCH #{match_no}"
            draw.text((logo_x + (logo_size - _tw(draw, mt, f_meta)) // 2, logo_y + logo_size + 2),
                      mt, fill=DIM, font=f_meta)

        venue_text = (stadium or match_title or "MATCH").upper()
        venue_y = top_y + top_h + 10
        draw.rounded_rectangle([card_x, venue_y, card_x + card_w, venue_y + 48], radius=14,
                               fill=(255, 255, 255, 14), outline=(255, 255, 255, 34), width=1)
        venue_fit = fit_text(venue_text, f_venue, card_w - 80)
        draw.text((card_x + (card_w - _tw(draw, venue_fit, f_venue)) // 2, venue_y + 10),
                  venue_fit, fill=(217, 222, 231), font=f_venue)

        table_y = venue_y + 60
        table_x = card_x + 12
        table_w = card_w - 24
        table_h = 564
        draw.rounded_rectangle([table_x, table_y, table_x + table_w, table_y + table_h], radius=24,
                               fill=(8, 16, 25, 204), outline=(255, 255, 255, 50), width=1)

        cols = [
            (_text_from_setting(text_settings, "batting", "header_batter", "BATTER"), 0.28, "l", "header_batter"),
            (_text_from_setting(text_settings, "batting", "header_dismissal", "DISMISSAL"), 0.34, "l", "header_dismissal"),
            (_text_from_setting(text_settings, "batting", "header_runs", "R"), 0.075, "c", "header_runs"),
            (_text_from_setting(text_settings, "batting", "header_balls", "B"), 0.075, "c", "header_balls"),
            (_text_from_setting(text_settings, "batting", "header_fours", "4"), 0.065, "c", "header_fours"),
            (_text_from_setting(text_settings, "batting", "header_sixes", "6"), 0.065, "c", "header_sixes"),
            (_text_from_setting(text_settings, "batting", "header_sr", "SR"), 0.10, "c", "header_sr"),
        ]
        total_ratio = sum(c[1] for c in cols)
        inner_x = table_x + 12
        inner_w = table_w - 24
        header_y = table_y + 12
        row_h = 45
        header_h = 44
        draw.rounded_rectangle([inner_x, header_y, inner_x + inner_w, header_y + header_h],
                               radius=12, fill=(255, 255, 255, 13))
        cx = inner_x
        for idx, (label, ratio, align, setting_key) in enumerate(cols):
            cw = int(inner_w * ratio / total_ratio)
            header_font = _font_from_setting(text_settings, "batting", setting_key, 22, bold=True, italic=True)
            th_y = text_vcenter_y(header_y, header_y + header_h, header_font)
            if align == "l":
                _draw_text_with_tracking(draw, _offset_xy(text_settings, "batting", setting_key, cx + 22, th_y), label,
                                         fill=gold, font=header_font, tracking=table_header_tracking)
            else:
                lw = _tracked_width(draw, label, header_font, table_header_tracking)
                _draw_text_with_tracking(draw, _offset_xy(text_settings, "batting", setting_key, cx + (cw - lw) // 2, th_y), label,
                                         fill=gold, font=header_font, tracking=table_header_tracking)
            if idx < len(cols) - 1:
                draw.line([(cx + cw, header_y), (cx + cw, header_y + header_h)],
                          fill=(255, 255, 255, 18), width=1)
            cx += cw

        rows = list(batsmen_rows or [])[:11]
        while len(rows) < 11:
            rows.append({"name": "—", "dismissal": "—", "runs": 0, "balls": 0,
                         "fours": 0, "sixes": 0, "strike_rate": 0.0, "status": "dnb"})

        row_y = header_y + header_h
        for b in rows:
            status = b.get("status")
            if not status:
                dism = (b.get("dismissal") or "").lower()
                if dism in ("did not bat", "dnb", "—", "-"):
                    status = "dnb"
                elif dism == "not out":
                    status = "not_out"
                else:
                    status = "out"

            if status == "out":
                fill = (255, 35, 45, 36)
                stripe = out_red
                sr_color = out_red
            elif status == "not_out":
                fill = (0, 140, 255, 38)
                stripe = notout_blue
                sr_color = notout_blue
            else:
                fill = (255, 255, 255, 0)
                stripe = (255, 255, 255, 0)
                sr_color = DIM

            draw.rectangle([inner_x, row_y, inner_x + inner_w, row_y + row_h], fill=fill)
            if status in ("out", "not_out"):
                draw.rectangle([inner_x, row_y, inner_x + 4, row_y + row_h], fill=stripe)
            draw.line([(inner_x, row_y + row_h - 1), (inner_x + inner_w, row_y + row_h - 1)],
                      fill=(255, 255, 255, 22), width=1)

            # HTML table cells have subtle vertical borders; adding them keeps
            # the batting columns visually aligned with the browser mockup.
            sep_x = inner_x
            for _label, ratio, _align, _setting_key in cols[:-1]:
                sep_x += int(inner_w * ratio / total_ratio)
                draw.line([(sep_x, row_y), (sep_x, row_y + row_h)],
                          fill=(255, 255, 255, 18), width=1)

            dism = b.get("dismissal", "—")
            if status == "not_out":
                dism = "NOT OUT"
            elif status == "dnb" and str(dism).lower() in ("did not bat", "dnb"):
                dism = "—"
            runs = f"{b.get('runs', 0)}*" if status == "not_out" else str(b.get("runs", 0))
            values = [
                fit_text(b.get("name", "—"), f_name, int(inner_w * cols[0][1] / total_ratio) - 44),
                fit_text(dism, f_dism, int(inner_w * cols[1][1] / total_ratio) - 44),
                runs,
                str(b.get("balls", 0)),
                str(b.get("fours", 0)),
                str(b.get("sixes", 0)),
                f"{float(b.get('strike_rate', 0) or 0):.2f}",
            ]
            cx = inner_x
            for idx, ((label, ratio, align, setting_key), value) in enumerate(zip(cols, values)):
                cw = int(inner_w * ratio / total_ratio)
                if idx == 0:
                    font_use = f_name
                    color = TEXT_DIM if status == "dnb" else TEXT
                elif idx == 1:
                    font_use = f_dism_notout if status == "not_out" else f_dism
                    color = notout_blue if status == "not_out" else (TEXT_DIM if status == "dnb" else (214, 219, 228))
                else:
                    font_use = f_cell
                    color = sr_color if label == "SR" else (TEXT_DIM if status == "dnb" else TEXT)
                ty = text_vcenter_y(row_y, row_y + row_h, font_use)
                tracking = table_body_tracking if idx in (0, 1) else 0
                if align == "l":
                    _draw_text_with_tracking(draw, _offset_xy(text_settings, "batting", "player_name", cx + 22, ty), str(value),
                                             fill=color, font=font_use, tracking=tracking)
                else:
                    vw = _tracked_width(draw, str(value), font_use, tracking)
                    body_key = "dismissal_text" if idx == 1 else "table_body"
                    _draw_text_with_tracking(draw, _offset_xy(text_settings, "batting", body_key, cx + (cw - vw) // 2, ty), str(value),
                                             fill=color, font=font_use, tracking=tracking)
                cx += cw
            row_y += row_h

        bottom_y = table_y + table_h + 18
        gap = 16
        stat_h = 138
        # Match the HTML mockup's 1fr 1fr 1fr 1.35fr bottom grid so the
        # innings score tile has enough room for large totals instead of every
        # tile being squeezed into equal widths.
        unit_w = (card_w - gap * 3) / 4.35
        stat_widths = [int(unit_w), int(unit_w), int(unit_w)]
        stat_widths.append(card_w - gap * 3 - sum(stat_widths))
        legal_balls = _overs_to_balls(overs_str)
        run_rate = (total_runs * 6 / legal_balls) if legal_balls else 0
        stats = [
            ("SCORING PACE", f"{run_rate:.1f}", "RUN RATE", "runs per over", out_red, False),
            ("ADDITIONAL RUNS", str(extras_dict.get("total", 0)), "EXTRAS", "wides, no-balls, byes", gold, False),
            ("PROGRESS", str(overs_str), "OVERS", "innings completed", notout_blue, False),
            ("INNINGS SCORE", f"{total_runs}/{total_wickets}", "TOTAL", f"after {overs_str} overs", gold, True),
        ]
        sx = card_x
        for i, (subtitle, value, label_txt, note, color, is_total) in enumerate(stats):
            sy = bottom_y
            sw = stat_widths[i]
            tile_fill = (10, 14, 20, 222)
            tile_outline = (255, 255, 255, 42)
            if is_total:
                tile_fill = (28, 24, 16, 226)
                tile_outline = (255, 215, 110, 72)
            draw.rounded_rectangle([sx, sy, sx + sw, sy + stat_h], radius=24,
                                   fill=tile_fill, outline=tile_outline, width=1)
            if is_total:
                _draw_horizontal_gradient(draw, sx + 1, sy, sw - 1, 4, gold, (255, 239, 176))
            else:
                draw.rectangle([sx + 1, sy, sx + sw - 1, sy + 4], fill=color)

            # Do not draw the decorative stat icon/emoji elements in the PNG
            # scorecard.  They render as missing-glyph boxes in some runtime
            # fonts, so the tiles use only the accent rule plus text content.
            draw.text(_offset_xy(text_settings, "batting", "stat_label", sx + 24, sy + 31), subtitle, fill=(158, 168, 184), font=f_stat_sub)

            value_font = _font_from_setting(text_settings, "batting", "stat_value", 88 if is_total else 68, family="display")
            value_color = gold if is_total else TEXT
            # In the HTML, the large stat value sits below the icon/title row
            # and starts from the card padding, not beside the icon.
            value_y = sy + (61 if is_total else 60)
            draw.text(_offset_xy(text_settings, "batting", "stat_value", sx + 24, value_y), str(value), fill=value_color, font=value_font)
            draw.text(_offset_xy(text_settings, "batting", "stat_label", sx + 24, sy + 97), label_txt, fill=(221, 227, 238), font=f_stat_label)
            draw.text(_offset_xy(text_settings, "batting", "stat_label", sx + 24, sy + 122), note, fill=(144, 160, 182), font=f_stat_note)
            sx += sw + gap

        if target is not None and not is_first_innings:
            outcome_txt = ""
            if chase_outcome == "won":
                outcome_txt = " · CHASE COMPLETED ✓"
            elif chase_outcome == "tied":
                outcome_txt = " · MATCH TIED"
            elif chase_outcome == "lost":
                outcome_txt = " · TARGET MISSED"
            full = f"TARGET: {target}{outcome_txt}"
            f_tgt = _font_from_setting(text_settings, "batting", "target", 16, bold=True)
            draw.text(_offset_xy(text_settings, "batting", "target", (W - _tw(draw, full, f_tgt)) // 2, H - 42), full, fill=accent, font=f_tgt)

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
                                *, match_no=None, accent_hex=None, text_settings=None) -> bytes | None:
    """Generate a bowling scorecard that follows ``Bowling Scorecard.html``.

    The bowling team is highlighted with the fixed green team box. ``accent_hex``
    remains the editable innings accent used for red/blue glows, table rules,
    Fall of Wickets labels, and bottom-bar highlights.
    """
    try:
        default_accent = PRIMARY_DEFAULT if is_first_innings else SECONDARY_DEFAULT
        accent = _hex_to_rgb(accent_hex, default_accent)
        bowling_green = (54, 219, 120)
        blue = (0, 170, 255)
        gold = GOLD

        W, H = 1600, 900
        img = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(img, "RGBA")

        # Page glow and glass card, matching the uploaded HTML proportions.
        draw.ellipse([-220, -170, 520, 380], fill=(accent[0], accent[1], accent[2], 54))
        draw.ellipse([1080, -170, 1840, 390], fill=(0, 150, 255, 50))
        draw.rounded_rectangle([14, 14, W - 14, H - 14], radius=24,
                               fill=(5, 12, 20, 236), outline=(255, 255, 255, 56), width=1)
        draw.rounded_rectangle([22, 22, W - 22, H - 22], radius=20,
                               outline=(255, 255, 255, 32), width=1)

        card_x, card_y, card_w = 14, 14, W - 28
        header_h = 130
        header_y = card_y + 0
        draw.rounded_rectangle([card_x + 14, header_y, card_x + card_w - 14, header_y + header_h],
                               radius=18, fill=(255, 255, 255, 14), outline=(255, 255, 255, 36), width=1)
        draw.rectangle([card_x + 15, header_y + 1, card_x + card_w // 2, header_y + header_h - 1],
                       fill=(accent[0], accent[1], accent[2], 28))
        draw.rectangle([card_x + card_w // 2, header_y + 1, card_x + card_w - 15, header_y + header_h - 1],
                       fill=(0, 145, 255, 28))

        f_team = _font_from_setting(text_settings, "bowling", "team", 70, family="display")
        f_tag = _font_from_setting(text_settings, "bowling", "tag", 14, bold=True)
        f_th = _font_from_setting(text_settings, "bowling", "table_header", 25, bold=True, italic=True)
        f_name = _font_from_setting(text_settings, "bowling", "bowler_name", 31, bold=True, italic=True)
        f_cell = _font_from_setting(text_settings, "bowling", "table_body", 30, bold=True)
        f_fow_title = _font_from_setting(text_settings, "bowling", "fow_title", 28, bold=True, italic=True)
        f_fow_over = _font_from_setting(text_settings, "bowling", "fow_body", 22, bold=True, italic=True)
        f_fow_score = _font_from_setting(text_settings, "bowling", "fow_body", 35, bold=True)
        f_stat_value = _font_from_setting(text_settings, "bowling", "stat_value", 68, family="display")
        f_total_value = _font_from_setting(text_settings, "bowling", "stat_value", 78, family="display")
        f_stat_label = _font_from_setting(text_settings, "bowling", "stat_label", 26, bold=True, italic=True)

        def fit_text(text, font, max_w):
            text = str(text or "—").upper()
            if _tw(draw, text, font) <= max_w:
                return text
            while len(text) > 3 and _tw(draw, text + "…", font) > max_w:
                text = text[:-1]
            return text + "…"

        def text_vcenter_y(y1, y2, font):
            bb = draw.textbbox((0, 0), "Hg", font=font)
            text_h = bb[3] - bb[1]
            return y1 + ((y2 - y1 - text_h) // 2) - bb[1]

        def draw_team_box(x1, y1, x2, y2, name, active=False):
            if active:
                draw.rounded_rectangle([x1, y1, x2, y2], radius=22,
                                       fill=(46, 210, 113, 54),
                                       outline=(95, 255, 165, 118), width=2)
                tag_x, tag_y = _offset_xy(text_settings, "bowling", "tag", x1 + 20, y1 - 12)
                draw.rounded_rectangle([tag_x, tag_y, tag_x + 112, tag_y + 32],
                                       radius=16, fill=bowling_green)
                tx, ty = _offset_xy(text_settings, "bowling", "tag", x1 + 33, y1 - 4)
                draw.text((tx, ty), _text_from_setting(text_settings, "bowling", "tag", "BOWLING"), fill=(7, 20, 13), font=f_tag)
            else:
                draw.rounded_rectangle([x1, y1, x2, y2], radius=22,
                                       fill=(255, 255, 255, 8), outline=(255, 255, 255, 24), width=1)
            txt = fit_text(name, f_team, x2 - x1 - 52)
            tw = _tw(draw, txt, f_team)
            tx, ty = _offset_xy(text_settings, "bowling", "team", x1 + (x2 - x1 - tw) // 2, y1 + 18)
            draw.text((tx, ty), txt, fill=(241, 242, 245), font=f_team)

        left_x1, left_x2 = card_x + 40, card_x + 40 + 520
        right_x2, right_x1 = card_x + card_w - 40, card_x + card_w - 40 - 520
        draw_team_box(left_x1, header_y + 16, left_x2, header_y + 108, team_name, active=True)
        draw_team_box(right_x1, header_y + 16, right_x2, header_y + 108, opponent_name or "OPPONENT", active=False)

        logo_size = 115
        logo_x = card_x + (card_w - logo_size) // 2
        logo_y = header_y + 8
        logo = _load_logo(target=logo_size)
        if logo:
            img.paste(logo, (logo_x, logo_y), logo)
        else:
            draw.rounded_rectangle([logo_x, logo_y, logo_x + logo_size, logo_y + logo_size],
                                   radius=18, fill=(18, 28, 44), outline=(255, 255, 255, 42), width=1)
            bot = "BOT"
            draw.text((logo_x + (logo_size - _tw(draw, bot, f_cell)) // 2, logo_y + 42), bot, fill=DIM, font=f_cell)

        # Main bowling table.
        table_x, table_y = card_x + 14, card_y + 138
        table_w, table_h = card_w - 28, 455
        draw.rounded_rectangle([table_x, table_y, table_x + table_w, table_y + table_h],
                               radius=16, fill=(6, 15, 25, 218), outline=(255, 255, 255, 46), width=1)
        draw.rectangle([table_x + 1, table_y + 1, table_x + table_w - 1, table_y + 58],
                       fill=(255, 255, 255, 26))

        cols = [
            ("", 0.37, "l", "bowler_name"),
            (_text_from_setting(text_settings, "bowling", "header_overs", "OVERS"), 0.13, "c", "header_overs"),
            (_text_from_setting(text_settings, "bowling", "header_dots", "DOTS"), 0.12, "c", "header_dots"),
            (_text_from_setting(text_settings, "bowling", "header_runs", "RUNS"), 0.12, "c", "header_runs"),
            (_text_from_setting(text_settings, "bowling", "header_wickets", "WICKETS"), 0.14, "c", "header_wickets"),
            (_text_from_setting(text_settings, "bowling", "header_economy", "ECONOMY"), 0.16, "c", "header_economy"),
        ]
        total_ratio = sum(c[1] for c in cols)
        header_h = 46
        row_h = 51
        cx = table_x
        for idx, (label, ratio, align, setting_key) in enumerate(cols):
            cw = int(table_w * ratio / total_ratio)
            if label:
                header_font = _font_from_setting(text_settings, "bowling", setting_key, 25, bold=True, italic=True)
                lx = cx + 36 if align == "l" else cx + (cw - _tw(draw, label, header_font)) // 2
                ly = text_vcenter_y(table_y, table_y + header_h, header_font)
                draw.text(_offset_xy(text_settings, "bowling", setting_key, lx, ly), label, fill=(241, 244, 250), font=header_font)
            if idx < len(cols) - 1:
                draw.line([(cx + cw, table_y), (cx + cw, table_y + table_h)], fill=(255, 255, 255, 30), width=1)
            cx += cw
        draw.line([(table_x, table_y + header_h), (table_x + table_w, table_y + header_h)], fill=(255, 255, 255, 46), width=1)

        rows = list(bowlers_rows or [])[:8]
        while len(rows) < 8:
            rows.append({"name": "-", "overs": "-", "dots": "-", "runs_conceded": "-", "wickets": "-", "economy": "-", "empty": True})
        row_y = table_y + header_h
        for i, b in enumerate(rows):
            empty = b.get("empty")
            fill = (255, 255, 255, 10 if not empty else 0)
            draw.rectangle([table_x, row_y, table_x + table_w, row_y + row_h], fill=fill)
            if not empty and i < 5:
                draw.rectangle([table_x, row_y, table_x + 4, row_y + row_h], fill=accent)
                draw.rectangle([table_x + table_w - 4, row_y, table_x + table_w, row_y + row_h], fill=blue)
            draw.line([(table_x, row_y + row_h - 1), (table_x + table_w, row_y + row_h - 1)], fill=(255, 255, 255, 34), width=1)
            values = [
                fit_text(b.get("name", "—"), f_name, int(table_w * cols[0][1] / total_ratio) - 72),
                str(b.get("overs", "0")),
                str(b.get("dots", b.get("dot_balls", 0))),
                str(b.get("runs_conceded", 0)),
                str(b.get("wickets", 0)),
                "-" if empty else f"{float(b.get('economy', 0) or 0):.2f}",
            ]
            cx = table_x
            for idx, ((label, ratio, align, setting_key), value) in enumerate(zip(cols, values)):
                cw = int(table_w * ratio / total_ratio)
                font_use = f_name if idx == 0 else f_cell
                color = (0, 0, 0, 0) if empty else (242, 244, 248)
                ty = text_vcenter_y(row_y, row_y + row_h, font_use)
                if idx == 0:
                    xy = _offset_xy(text_settings, "bowling", "bowler_name", cx + 36, ty)
                else:
                    vw = _tw(draw, str(value), font_use)
                    xy = _offset_xy(text_settings, "bowling", "table_body", cx + (cw - vw) // 2, ty)
                draw.text(xy, str(value), fill=color, font=font_use)
                cx += cw
            row_y += row_h

        # Fall of wickets panel.
        fow_x, fow_y, fow_w, fow_h = table_x, table_y + table_h + 12, table_w, 155
        draw.rounded_rectangle([fow_x, fow_y, fow_x + fow_w, fow_y + fow_h], radius=16,
                               fill=(5, 14, 25, 224), outline=(255, 255, 255, 46), width=1)
        draw.text(_offset_xy(text_settings, "bowling", "fow_title", fow_x + 12, fow_y + 12),
                  _text_from_setting(text_settings, "bowling", "fow_title", "FALL OF WICKETS"), fill=(244, 246, 251), font=f_fow_title)
        grid_x, grid_y, grid_w, grid_h = fow_x + 12, fow_y + 50, fow_w - 24, 88
        cell_w = grid_w // 10
        fow = list(fall_of_wickets or [])[:10]
        for i in range(10):
            cx = grid_x + i * cell_w
            draw.rectangle([cx, grid_y, cx + cell_w, grid_y + grid_h], fill=(255, 255, 255, 14))
            if i < 9:
                draw.line([(cx + cell_w, grid_y), (cx + cell_w, grid_y + grid_h)], fill=(255, 255, 255, 40), width=1)
            suffix = "th"
            if i == 0: suffix = "st"
            elif i == 1: suffix = "nd"
            elif i == 2: suffix = "rd"
            over_label = f"{i+1}{suffix}"
            score_label = ""
            if i < len(fow):
                item = fow[i]
                try:
                    _w, score, _over = item
                    score_label = str(score)
                except Exception:
                    score_label = str(item)
            oy = text_vcenter_y(grid_y, grid_y + 38, f_fow_over)
            sy = text_vcenter_y(grid_y + 38, grid_y + grid_h, f_fow_score)
            draw.text(_offset_xy(text_settings, "bowling", "fow_body", cx + (cell_w - _tw(draw, over_label, f_fow_over)) // 2, oy),
                      over_label, fill=gold, font=f_fow_over)
            draw.text(_offset_xy(text_settings, "bowling", "fow_body", cx + (cell_w - _tw(draw, score_label, f_fow_score)) // 2, sy),
                      score_label, fill=(242, 244, 248), font=f_fow_score)

        # Bottom stat bar.
        bottom_y = fow_y + fow_h + 12
        bottom_h = 100
        draw.rounded_rectangle([table_x, bottom_y, table_x + table_w, bottom_y + bottom_h], radius=18,
                               fill=(5, 13, 23, 236), outline=(255, 255, 255, 56), width=1)
        bowling_balls = sum(_overs_to_balls(b.get("overs", "0")) for b in bowlers_rows or [])
        conceded = sum(int(b.get("runs_conceded", 0) or 0) for b in bowlers_rows or [])
        wkts = sum(int(b.get("wickets", 0) or 0) for b in bowlers_rows or [])
        team_economy = (conceded * 6 / bowling_balls) if bowling_balls else 0
        stats = [
            (str(wkts), "WICKETS"),
            (f"{team_economy:.1f}", "ECONOMY"),
            (str(opp_overs or "0.0"), "OVERS"),
            (f"{opp_score or 0}/{opp_wickets or 0}", "OPP SCORE"),
        ]
        unit_w = table_w / 4.25
        widths = [int(unit_w), int(unit_w), int(unit_w), table_w - int(unit_w) * 3]
        sx = table_x
        for i, ((value, label), sw) in enumerate(zip(stats, widths)):
            if i:
                draw.line([(sx, bottom_y), (sx, bottom_y + bottom_h)], fill=(255, 255, 255, 42), width=1)
            vf = f_total_value if i == 3 else f_stat_value
            vx = sx + sw // 2 - _tw(draw, value, vf) // 2 - 54
            vy = text_vcenter_y(bottom_y, bottom_y + bottom_h, vf)
            lx = sx + sw // 2 + 16
            ly = text_vcenter_y(bottom_y, bottom_y + bottom_h, f_stat_label)
            draw.text(_offset_xy(text_settings, "bowling", "stat_value", vx, vy), value, fill=(244, 245, 247), font=vf)
            draw.text(_offset_xy(text_settings, "bowling", "stat_label", lx, ly), label, fill=(231, 234, 240), font=f_stat_label)
            sx += sw

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Failed to render bowling scorecard")
        return None
