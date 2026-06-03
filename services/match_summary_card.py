"""Match summary card renderer.

The layout intentionally mirrors ``Match summary.html`` as a 2048×1280
reference-ratio dark metal card, with a large match header, stadium strip,
two 50/50 batting/bowling innings grids, a result bar, and a POTM footer.
Text objects are admin-editable through the
Scorecard Designer by sharing the same ``scorecard_text_settings`` JSON used by
batting/bowling scorecards.
"""

import io
import logging
import os
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    from services.scorecard_card import normalize_scorecard_text_settings
except Exception:  # pragma: no cover - defensive import fallback for legacy paths
    normalize_scorecard_text_settings = None

logger = logging.getLogger(__name__)

RED = (255, 70, 50)
BLUE = (0, 170, 255)
GOLD = (255, 207, 76)
TEXT = (242, 243, 246)
CARD_BG = (7, 11, 18)
PANEL_BG = (8, 12, 18)
BORDER = (255, 255, 255, 35)

CANVAS_W = 2048
CANVAS_H = 1280
OUTER_PAD = 24
CARD_W = CANVAS_W - OUTER_PAD * 2
HEADER_Y = 24
HEADER_H = 190
STADIUM_Y = HEADER_Y + HEADER_H + 8
STADIUM_H = 54
INNINGS_GAP = 18
INNINGS_H = 322
INNINGS_1_Y = STADIUM_Y + STADIUM_H + 18
INNINGS_2_Y = INNINGS_1_Y + INNINGS_H + INNINGS_GAP
RESULT_Y = INNINGS_2_Y + INNINGS_H + 22
RESULT_H = 92
POTM_Y = RESULT_Y + RESULT_H + 22
POTM_H = 122

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGO_PATH = os.path.join(_ROOT_DIR, "assets", "logo.png")
_FONT_DIR = os.path.join(_ROOT_DIR, "assets", "fonts")
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


def _font(size, bold=False, italic=False, family="body"):
    candidate = None
    if family == "display":
        candidate = _first_existing(_BEBAS_FONT_CANDIDATES)
    elif family == "body":
        candidate = _first_existing(_BRICOLAGE_FONT_CANDIDATES)
    if candidate:
        try:
            return ImageFont.truetype(candidate, size)
        except (OSError, IOError):
            pass
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


def _settings(text_settings):
    if normalize_scorecard_text_settings:
        return normalize_scorecard_text_settings(text_settings).get("summary", {})
    return {}


def _setting(text_settings, key):
    return _settings(text_settings).get(key, {"text": "", "font": "body", "size": 0, "x": 0, "y": 0})


def _font_for(text_settings, key, size, *, bold=True, italic=False, family="body"):
    s = _setting(text_settings, key)
    fam = s.get("font") or family
    if fam == "fallback":
        fam = "fallback"
    elif fam not in {"display", "body"}:
        fam = family
    return _font(max(6, size + int(s.get("size", 0) or 0)), bold=bold, italic=italic, family=fam)


def _xy(text_settings, key, x, y):
    s = _setting(text_settings, key)
    return x + int(s.get("x", 0) or 0), y + int(s.get("y", 0) or 0)


def _text(text_settings, key, default):
    s = _setting(text_settings, key)
    return str(s.get("text") or default)


def _tw(draw, text, font):
    bb = draw.textbbox((0, 0), str(text), font=font)
    return bb[2] - bb[0]


def _fit_text(draw, text, font, max_width, min_len=4):
    text = str(text)
    while _tw(draw, text, font) > max_width and len(text) > min_len:
        text = text[:-2] + "…"
    return text


def _draw_tracked(draw, xy, text, font, fill, tracking=0):
    if not tracking:
        draw.text(xy, str(text), font=font, fill=fill)
        return
    x, y = xy
    for ch in str(text):
        draw.text((x, y), ch, font=font, fill=fill)
        x += _tw(draw, ch, font) + tracking


def _tracked_width(draw, text, font, tracking=0):
    return sum(_tw(draw, ch, font) for ch in str(text)) + max(0, len(str(text)) - 1) * tracking


def _rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _load_logo(target=(165, 110)):
    try:
        if not os.path.exists(_LOGO_PATH):
            return None
        img = Image.open(_LOGO_PATH).convert("RGBA")
        img.thumbnail(target, Image.LANCZOS)
        return img
    except Exception:
        return None


def _draw_shadowed_text(draw, xy, text, font, fill=TEXT, tracking=0):
    x, y = xy
    _draw_tracked(draw, (x, y + 4), text, font, (0, 0, 0, 150), tracking)
    _draw_tracked(draw, (x, y), text, font, fill, tracking)


def _draw_bg(img):
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    for y in range(h):
        t = y / max(1, h - 1)
        base = int(5 * (1 - t) + 3 * t), int(6 * (1 - t) + 4 * t), int(8 * (1 - t) + 6 * t)
        draw.line([(0, y), (w, y)], fill=base)
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow, "RGBA")
    gd.ellipse((-180, -160, 520, 380), fill=(255, 60, 40, 55))
    gd.ellipse((w - 520, -160, w + 180, 380), fill=(0, 170, 255, 55))
    glow = glow.filter(ImageFilter.GaussianBlur(80))
    img.alpha_composite(glow)


def _draw_glow_block(img, box, color, *, blur=28, alpha=150):
    """Paint a soft rectangular glow behind a hard-edged accent block."""
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow, "RGBA")
    gd.rounded_rectangle(box, radius=16, fill=(*color, alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(blur))
    img.alpha_composite(glow)


def _draw_header(draw, img, text_settings, match_no):
    x, y, w, h = OUTER_PAD, HEADER_Y, CARD_W, HEADER_H
    _rounded(draw, [x, y, x + w, y + h], 34, (7, 10, 16, 248),
             (255, 255, 255, 38), 2)
    draw.rounded_rectangle([x + 4, y + 4, x + w - 4, y + h - 4],
                           radius=30, outline=(255, 255, 255, 15), width=1)

    # Neon lower-corner blocks from the reference image.
    red_box = [x + 34, y + h - 58, x + 235, y + h - 18]
    cyan_box = [x + w - 235, y + h - 58, x + w - 34, y + h - 18]
    _draw_glow_block(img, red_box, RED, blur=24, alpha=150)
    _draw_glow_block(img, cyan_box, BLUE, blur=24, alpha=150)
    draw.rounded_rectangle(red_box, radius=10, fill=(*RED, 185))
    draw.rounded_rectangle(cyan_box, radius=10, fill=(*BLUE, 185))

    # Thin vertical centre divider accents, with a muted centre bay for the logo.
    mid = x + w // 2
    draw.rectangle([mid - 118, y + 1, mid + 118, y + h - 1],
                   fill=(255, 255, 255, 7))
    for dx, col in ((-118, RED), (-98, (255, 255, 255)),
                    (98, (255, 255, 255)), (118, BLUE)):
        draw.line([(mid + dx, y + 22), (mid + dx, y + h - 22)],
                  fill=(*col, 50), width=2)
    draw.line([(mid, y + 22), (mid, y + h - 22)],
              fill=(255, 255, 255, 36), width=1)

    f = _font_for(text_settings, "header_title", 104, family="display")
    left = _text(text_settings, "header_title", "SUMMARY").upper()
    _draw_shadowed_text(draw, _xy(text_settings, "header_title", x + 98, y + 48),
                        left, f, tracking=4)
    right = _text(text_settings, "match_no", f"MATCH #{match_no or '—'}").upper()
    rw = _tracked_width(draw, right, f, 4)
    rx, ry = _xy(text_settings, "match_no", x + w - 98 - rw, y + 48)
    _draw_shadowed_text(draw, (rx, ry), right, f, tracking=4)

    logo = _load_logo(target=(210, 138))
    if logo:
        lx = mid - logo.width // 2
        ly = y + (h - logo.height) // 2
        img.alpha_composite(logo, (lx, ly))


def _draw_stadium(draw, text_settings, stadium):
    x, y, w, h = OUTER_PAD, STADIUM_Y, CARD_W, STADIUM_H
    _rounded(draw, [x, y, x + w, y + h], 10, (84, 53, 17, 232),
             (255, 205, 76, 82), 1)
    draw.rounded_rectangle([x + 1, y + 1, x + w - 1, y + h - 1],
                           radius=9, fill=(111, 69, 20, 170))
    draw.line([(x + 14, y + 8), (x + w - 14, y + 8)],
              fill=(255, 226, 130, 70), width=1)
    draw.line([(x + 14, y + h - 8), (x + w - 14, y + h - 8)],
              fill=(48, 27, 7, 115), width=1)

    # Angled divider slashes flanking the venue text.
    slash_w = 18
    for base in (x + 535, x + 570, x + 605,
                 x + w - 535, x + w - 570, x + w - 605):
        draw.polygon([(base, y + h), (base + slash_w, y + h),
                      (base + slash_w + 30, y), (base + 30, y)],
                     fill=(255, 207, 76, 128))

    f = _font_for(text_settings, "stadium", 38, family="display")
    txt = _text(text_settings, "stadium", (stadium or "MATCH")).upper()
    txt = _fit_text(draw, txt, f, w - 620, 6)
    tw = _tracked_width(draw, txt, f, 5)
    sx, sy = _xy(text_settings, "stadium", x + (w - tw) // 2, y + 7)
    _draw_shadowed_text(draw, (sx, sy), txt, f, fill=(255, 237, 174), tracking=5)

def _normalise_batters(rows):
    out = []
    for b in (rows or [])[:4]:
        runs = b.get("runs", 0)
        star = "*" if not b.get("out", False) else ""
        out.append((b.get("name", "—"), f"{runs}{star}", str(b.get("balls", 0))))
    while len(out) < 4:
        out.append(("—", "—", "—"))
    return out


def _normalise_bowlers(rows):
    out = []
    for b in (rows or [])[:4]:
        out.append((b.get("name", "—"), f"{b.get('wickets', 0)}-{b.get('runs', 0)}", str(b.get("overs", "0"))))
    while len(out) < 4:
        out.append(("—", "—", "—"))
    return out



def _draw_rows(draw, x, y, w, rows, *, color, right_accent, text_settings, potm_name=None):
    row_h = 62
    name_f = _font_for(text_settings, "row_name", 34, italic=True, family="body")
    num_f = _font_for(text_settings, "row_number", 36, family="display")
    mini_f = _font_for(text_settings, "potm_badge", 18, family="body")
    for i, (name, val1, val2) in enumerate(rows[:4]):
        ry = y + i * row_h
        tint = (color[0], color[1], color[2], 46)
        draw.rectangle([x, ry, x + w, ry + row_h], fill=tint)
        draw.line([(x, ry + row_h), (x + w, ry + row_h)], fill=(255, 255, 255, 28))
        if right_accent:
            draw.rectangle([x + w - 7, ry, x + w, ry + row_h], fill=(*color, 225))
        else:
            draw.rectangle([x, ry, x + 7, ry + row_h], fill=(*color, 225))

        name_txt = _fit_text(draw, str(name).upper(), name_f, w - 320)
        nx, ny = _xy(text_settings, "row_name", x + 28, ry + 13)
        _draw_shadowed_text(draw, (nx, ny), name_txt, name_f)
        if potm_name and str(name).strip().lower() == str(potm_name).strip().lower():
            bw = _tw(draw, name_txt, name_f)
            bx = min(x + w - 255, nx + bw + 14)
            _rounded(draw, [bx, ry + 18, bx + 72, ry + 44], 7, (255, 194, 44, 245))
            draw.text((bx + 8, ry + 19), "POTM", font=mini_f, fill=(28, 17, 0))

        number_area_x = x + w - 245
        for idx, val in enumerate((val1, val2)):
            cx = number_area_x + idx * 112
            txt = str(val).upper()
            tw = _tw(draw, txt, num_f)
            px, py = _xy(text_settings, "row_number", cx + (95 - tw) // 2, ry + 9)
            _draw_shadowed_text(draw, (px, py), txt, num_f)


def _draw_innings(draw, x, y, w, text_settings, *, team, runs, wickets,
                  overs, overs_total, batters, bowlers, bat_color,
                  bowl_color, potm_name):
    block_h = INNINGS_H
    title_h = 74
    _rounded(draw, [x, y, x + w, y + block_h], 22, (6, 11, 18, 242), BORDER, 1)
    draw.rectangle([x + 1, y + 1, x + w - 1, y + title_h], fill=(0, 0, 0, 42))
    draw.line([(x, y + title_h), (x + w, y + title_h)], fill=(255, 255, 255, 35), width=1)

    f_team = _font_for(text_settings, "innings_team", 70, family="display")
    f_meta = _font_for(text_settings, "innings_meta", 34, family="display")
    f_score = _font_for(text_settings, "innings_score", 78, family="display")
    team_txt = _fit_text(draw, str(team or "—").upper(), f_team, w - 650, 6)
    _draw_shadowed_text(draw, _xy(text_settings, "innings_team", x + 32, y + 4),
                        team_txt, f_team, tracking=3)

    meta_txt = _text(text_settings, "innings_meta", f"OVERS {overs}/{overs_total}.0").upper()
    score_txt = _text(text_settings, "innings_score", f"{runs}/{wickets}").upper()
    score_w = _tracked_width(draw, score_txt, f_score, 4)
    meta_w = _tracked_width(draw, meta_txt, f_meta, 2)
    sx, sy = _xy(text_settings, "innings_score", x + w - 34 - score_w, y - 2)
    mx, my = _xy(text_settings, "innings_meta", x + w - 370 - meta_w, y + 24)
    _draw_shadowed_text(draw, (mx, my), meta_txt, f_meta,
                        fill=(222, 228, 236), tracking=2)
    _draw_shadowed_text(draw, (sx, sy), score_txt, f_score, tracking=4)

    grid_y = y + title_h
    half = w // 2
    # Exact 50/50 split: batting list on the left, bowling list on the right.
    _draw_rows(draw, x, grid_y, half, _normalise_batters(batters),
               color=bat_color, right_accent=False,
               text_settings=text_settings, potm_name=potm_name)
    _draw_rows(draw, x + half, grid_y, half, _normalise_bowlers(bowlers),
               color=bowl_color, right_accent=True,
               text_settings=text_settings, potm_name=potm_name)
    draw.line([(x + half, grid_y), (x + half, y + block_h)],
              fill=(255, 255, 255, 36), width=2)


def _draw_result(draw, img, x, y, w, text_settings, winner_name, win_margin_text):
    h = RESULT_H
    _rounded(draw, [x, y, x + w, y + h], 22, (8, 12, 18, 244), (255, 190, 80, 58), 1)
    red_box = [x + 26, y + 21, x + 184, y + h - 21]
    cyan_box = [x + w - 184, y + 21, x + w - 26, y + h - 21]
    _draw_glow_block(img, red_box, RED, blur=24, alpha=135)
    _draw_glow_block(img, cyan_box, BLUE, blur=24, alpha=135)
    draw.rounded_rectangle(red_box, radius=12, fill=(*RED, 170))
    draw.rounded_rectangle(cyan_box, radius=12, fill=(*BLUE, 170))
    f = _font_for(text_settings, "result", 62, family="display")
    txt = _text(text_settings, "result", f"{(winner_name or '—').upper()} WON {str(win_margin_text or '').upper()} 🏆").upper()
    txt = _fit_text(draw, txt, f, w - 420, 10)
    tw = _tracked_width(draw, txt, f, 3)
    rx, ry = _xy(text_settings, "result", x + (w - tw) // 2, y + 14)
    _draw_shadowed_text(draw, (rx, ry), txt, f, tracking=3)


def _draw_potm(draw, x, y, w, text_settings, potm_name, potm_stats):
    h = POTM_H
    _rounded(draw, [x, y, x + w, y + h], 20, (78, 49, 16, 240), (255, 207, 76, 90), 1)
    draw.rounded_rectangle([x + 1, y + 1, x + w - 1, y + h - 1],
                           radius=19, fill=(104, 64, 18, 188))
    badge_w = 202
    name_w = 640
    draw.rectangle([x, y, x + badge_w, y + h], fill=(66, 39, 11, 220))
    draw.rectangle([x + badge_w, y, x + badge_w + name_w, y + h], fill=(121, 75, 20, 210))
    draw.line([(x + badge_w, y), (x + badge_w, y + h)], fill=(255, 240, 170, 45), width=2)
    draw.line([(x + badge_w + name_w, y), (x + badge_w + name_w, y + h)], fill=(255, 240, 170, 45), width=2)
    draw.line([(x + 18, y + 10), (x + w - 18, y + 10)], fill=(255, 228, 132, 75), width=1)

    f_badge = _font_for(text_settings, "potm_badge", 56, family="display")
    f_name = _font_for(text_settings, "potm_name", 68, family="display")
    f_label = _font_for(text_settings, "potm_label", 44, family="display")
    f_value = _font_for(text_settings, "potm_value", 66, family="display")
    badge = _text(text_settings, "potm_badge", "POTM").upper()
    bw = _tracked_width(draw, badge, f_badge, 2)
    _draw_shadowed_text(draw, _xy(text_settings, "potm_badge", x + (badge_w - bw) // 2, y + 29), badge, f_badge, fill=(255, 242, 197), tracking=2)

    name = _fit_text(draw, _text(text_settings, "potm_name", potm_name or "—").upper(), f_name, name_w - 54, 5)
    _draw_shadowed_text(draw, _xy(text_settings, "potm_name", x + badge_w + 32, y + 24), name, f_name, fill=GOLD, tracking=3)

    label = _text(text_settings, "potm_label", "PERFORMANCE:").upper()
    _draw_shadowed_text(draw, _xy(text_settings, "potm_label", x + badge_w + name_w + 38, y + 36), label, f_label, fill=GOLD, tracking=2)
    value = _fit_text(draw, _text(text_settings, "potm_value", potm_stats or "—").upper(), f_value, w - badge_w - name_w - 470, 3)
    _draw_shadowed_text(draw, _xy(text_settings, "potm_value", x + badge_w + name_w + 355, y + 25), value, f_value, tracking=2)
    draw.text((x + w - 98, y + 28), "⭐", font=_font(64, family="fallback"), fill=(255, 216, 77))


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
    match_no=None,
    text_settings=None,
) -> bytes | None:
    """Render the match summary card. Returns PNG bytes or ``None`` on failure."""
    try:
        W, H = CANVAS_W, CANVAS_H
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _draw_bg(img)
        draw = ImageDraw.Draw(img, "RGBA")
        _rounded(draw, [0, 0, W - 1, H - 1], 34, (*CARD_BG, 246),
                 (255, 255, 255, 42), 1)
        draw.rounded_rectangle([12, 12, W - 13, H - 13], radius=26,
                               outline=(255, 255, 255, 25), width=1)

        _draw_header(draw, img, text_settings, match_no)
        stadium_text = stadium or (match_date.strftime("%d %b %Y") if isinstance(match_date, datetime) else "MATCH")
        _draw_stadium(draw, text_settings, stadium_text)

        top_per_team = top_per_team or {}
        inn1_data = top_per_team.get("inn1", {})
        inn2_data = top_per_team.get("inn2", {})
        x, w = OUTER_PAD, CARD_W
        _draw_innings(draw, x, INNINGS_1_Y, w, text_settings,
                      team=inn1_data.get("team") or inn1_team,
                      runs=inn1_runs, wickets=inn1_wickets, overs=inn1_overs,
                      overs_total=overs_total,
                      batters=inn1_data.get("batters", []),
                      bowlers=inn1_data.get("bowlers", []),
                      bat_color=BLUE, bowl_color=RED, potm_name=potm_name)
        _draw_innings(draw, x, INNINGS_2_Y, w, text_settings,
                      team=inn2_data.get("team") or inn2_team,
                      runs=inn2_runs, wickets=inn2_wickets, overs=inn2_overs,
                      overs_total=overs_total,
                      batters=inn2_data.get("batters", []),
                      bowlers=inn2_data.get("bowlers", []),
                      bat_color=RED, bowl_color=BLUE, potm_name=potm_name)
        _draw_result(draw, img, x, RESULT_Y, w, text_settings, winner_name, win_margin_text)
        _draw_potm(draw, x, POTM_Y, w, text_settings, potm_name, potm_stats)

        out = Image.new("RGB", img.size, (3, 4, 6))
        out.paste(img, mask=img.split()[-1])
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Failed to render match summary card")
        return None
