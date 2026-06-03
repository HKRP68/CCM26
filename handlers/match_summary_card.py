"""Match summary card renderer.

The layout intentionally mirrors ``Match summary.html``: a 1600px dark metal
card, split header, stadium strip, two innings blocks with top batters/bowlers,
a result bar, and a POTM bar.  Text objects are admin-editable through the
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


def _draw_header(draw, img, text_settings, match_no):
    x, y, w, h = 10, 10, 1580, 128
    _rounded(draw, [x, y, x + w, y + h], 20, (8, 12, 18, 245), BORDER, 1)
    draw.rectangle([x + (w - 180) // 2, y, x + (w + 180) // 2, y + h], fill=(255, 255, 255, 8))
    draw.line([(x + (w - 180) // 2, y), (x + (w - 180) // 2, y + h)], fill=(255, 255, 255, 22))
    draw.line([(x + (w + 180) // 2, y), (x + (w + 180) // 2, y + h)], fill=(255, 255, 255, 22))
    draw.polygon([(x, y + h), (x + 140, y + h), (x + 140, y + h - 72)], fill=(255, 70, 40, 215))
    draw.polygon([(x + w, y + h), (x + w - 140, y + h), (x + w - 140, y + h - 72)], fill=(0, 170, 255, 215))

    f = _font_for(text_settings, "header_title", 72, family="display")
    left = _text(text_settings, "header_title", "SUMMARY").upper()
    _draw_shadowed_text(draw, _xy(text_settings, "header_title", x + 85, y + 30), left, f, tracking=3)
    right = _text(text_settings, "match_no", f"MATCH #{match_no or '—'}").upper()
    rw = _tracked_width(draw, right, f, 3)
    rx, ry = _xy(text_settings, "match_no", x + w - 85 - rw, y + 30)
    _draw_shadowed_text(draw, (rx, ry), right, f, tracking=3)

    logo = _load_logo()
    if logo:
        lx = x + (w - logo.width) // 2
        ly = y + (h - logo.height) // 2
        img.alpha_composite(logo, (lx, ly))


def _draw_stadium(draw, text_settings, stadium):
    x, y, w, h = 10, 144, 1580, 42
    _rounded(draw, [x, y, x + w, y + h], 18, (10, 14, 20, 235), (255, 190, 80, 55), 1)
    draw.polygon([(x + int(w * .35), y), (x + int(w * .35) + 140, y), (x + int(w * .35) + 110, y + h)], fill=(255, 190, 80, 65))
    draw.polygon([(x + int(w * .65), y), (x + int(w * .65) - 140, y), (x + int(w * .65) - 110, y + h)], fill=(255, 190, 80, 65))
    f = _font_for(text_settings, "stadium", 24, family="display")
    txt = _text(text_settings, "stadium", (stadium or "MATCH")).upper()
    tw = _tracked_width(draw, txt, f, 4)
    sx, sy = _xy(text_settings, "stadium", x + (w - tw) // 2, y + 7)
    _draw_shadowed_text(draw, (sx, sy), txt, f, tracking=4)


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
    row_h = 54
    name_f = _font_for(text_settings, "row_name", 28, italic=True, family="body")
    num_f = _font_for(text_settings, "row_number", 28, family="body")
    mini_f = _font_for(text_settings, "potm_badge", 16, family="body")
    for i, (name, val1, val2) in enumerate(rows):
        ry = y + i * row_h
        tint = (color[0], color[1], color[2], 38)
        draw.rectangle([x, ry, x + w, ry + row_h], fill=tint)
        if right_accent:
            draw.rectangle([x + w - 4, ry, x + w, ry + row_h], fill=(*color, 235))
        else:
            draw.rectangle([x, ry, x + 4, ry + row_h], fill=(*color, 235))
        draw.line([(x, ry + row_h), (x + w, ry + row_h)], fill=(255, 255, 255, 25))
        name_txt = _fit_text(draw, str(name).upper(), name_f, w - 245)
        nx, ny = _xy(text_settings, "row_name", x + 18, ry + 12)
        _draw_shadowed_text(draw, (nx, ny), name_txt, name_f)
        if potm_name and str(name).lower() == str(potm_name).lower():
            bw = _tw(draw, name_txt, name_f)
            bx = min(x + w - 250, nx + bw + 10)
            _rounded(draw, [bx, ry + 16, bx + 62, ry + 38], 6, (255, 190, 40, 245))
            draw.text((bx + 7, ry + 17), "POTM", font=mini_f, fill=(29, 18, 0))
        for idx, val in enumerate((val1, val2)):
            cx = x + w - 200 + idx * 100
            txt = str(val).upper()
            tw = _tw(draw, txt, num_f)
            px, py = _xy(text_settings, "row_number", cx + (90 - tw) // 2, ry + 12)
            _draw_shadowed_text(draw, (px, py), txt, num_f)


def _draw_innings(draw, x, y, w, text_settings, *, team, runs, wickets, overs, overs_total, batters, bowlers, bat_color, bowl_color, potm_name):
    block_h = 74 + 216
    _rounded(draw, [x, y, x + w, y + block_h], 18, (6, 12, 20, 240), BORDER, 1)
    draw.rectangle([x, y, x + w, y + 74], fill=(0, 0, 0, 35))
    draw.line([(x, y + 74), (x + w, y + 74)], fill=(255, 255, 255, 28))
    f_team = _font_for(text_settings, "innings_team", 62, family="display")
    f_meta = _font_for(text_settings, "innings_meta", 30, family="display")
    f_score = _font_for(text_settings, "innings_score", 66, family="display")
    team_txt = _fit_text(draw, str(team).upper(), f_team, w - 520, 6)
    _draw_shadowed_text(draw, _xy(text_settings, "innings_team", x + 18, y + 12), team_txt, f_team, tracking=3)
    meta_txt = _text(text_settings, "innings_meta", f"OVERS {overs}/{overs_total}.0").upper()
    score_txt = _text(text_settings, "innings_score", f"{runs}/{wickets}").upper()
    score_w = _tracked_width(draw, score_txt, f_score, 3)
    meta_w = _tracked_width(draw, meta_txt, f_meta, 2)
    sx, sy = _xy(text_settings, "innings_score", x + w - 18 - score_w, y + 7)
    mx, my = _xy(text_settings, "innings_meta", sx - 24 - meta_w, y + 23)
    _draw_shadowed_text(draw, (mx, my), meta_txt, f_meta, tracking=2)
    _draw_shadowed_text(draw, (sx, sy), score_txt, f_score, tracking=3)
    grid_y = y + 74
    half = w // 2
    _draw_rows(draw, x, grid_y, half, _normalise_batters(batters), color=bat_color, right_accent=False, text_settings=text_settings, potm_name=potm_name)
    _draw_rows(draw, x + half, grid_y, half, _normalise_bowlers(bowlers), color=bowl_color, right_accent=True, text_settings=text_settings, potm_name=potm_name)
    draw.line([(x + half, grid_y), (x + half, y + block_h)], fill=(255, 255, 255, 28))


def _draw_result(draw, x, y, w, text_settings, winner_name, win_margin_text):
    h = 70
    _rounded(draw, [x, y, x + w, y + h], 18, (8, 12, 18, 242), (255, 190, 80, 55), 1)
    draw.polygon([(x, y + h), (x + 110, y + h), (x + 110, y)], fill=(255, 70, 50, 90))
    draw.polygon([(x + w, y + h), (x + w - 110, y + h), (x + w - 110, y)], fill=(0, 170, 255, 90))
    f = _font_for(text_settings, "result", 52, family="display")
    txt = _text(text_settings, "result", f"{(winner_name or '—').upper()} WON {str(win_margin_text or '').upper()} 🏆").upper()
    txt = _fit_text(draw, txt, f, w - 80, 10)
    tw = _tracked_width(draw, txt, f, 3)
    rx, ry = _xy(text_settings, "result", x + (w - tw) // 2, y + 8)
    _draw_shadowed_text(draw, (rx, ry), txt, f, tracking=3)


def _draw_potm(draw, x, y, w, text_settings, potm_name, potm_stats):
    h = 96
    _rounded(draw, [x, y, x + w, y + h], 18, (10, 10, 10, 244), (255, 180, 50, 70), 1)
    draw.rectangle([x, y, x + 170, y + h], fill=(255, 120, 20, 48))
    draw.line([(x + 170, y), (x + 170, y + h)], fill=(255, 255, 255, 30))
    draw.line([(x + 760, y), (x + 760, y + h)], fill=(255, 255, 255, 30))
    f_badge = _font_for(text_settings, "potm_badge", 44, family="display")
    f_name = _font_for(text_settings, "potm_name", 58, family="display")
    f_label = _font_for(text_settings, "potm_label", 40, family="display")
    f_value = _font_for(text_settings, "potm_value", 62, family="display")
    badge = _text(text_settings, "potm_badge", "POTM").upper()
    bw = _tracked_width(draw, badge, f_badge, 2)
    _draw_shadowed_text(draw, _xy(text_settings, "potm_badge", x + (170 - bw) // 2, y + 24), badge, f_badge, fill=(255, 242, 197), tracking=2)
    name = _fit_text(draw, _text(text_settings, "potm_name", potm_name or "—").upper(), f_name, 525, 5)
    _draw_shadowed_text(draw, _xy(text_settings, "potm_name", x + 194, y + 20), name, f_name, fill=GOLD, tracking=3)
    label = _text(text_settings, "potm_label", "PERFORMANCE:").upper()
    _draw_shadowed_text(draw, _xy(text_settings, "potm_label", x + 784, y + 28), label, f_label, fill=GOLD, tracking=2)
    value = _fit_text(draw, _text(text_settings, "potm_value", potm_stats or "—").upper(), f_value, 430, 3)
    _draw_shadowed_text(draw, _xy(text_settings, "potm_value", x + 1080, y + 16), value, f_value, tracking=2)
    draw.text((x + w - 86, y + 22), "⭐", font=_font(54, family="fallback"), fill=(255, 216, 77))


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
        W, H = 1600, 900
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        _draw_bg(img)
        draw = ImageDraw.Draw(img, "RGBA")
        _rounded(draw, [0, 0, W - 1, H - 1], 24, (*CARD_BG, 246), (255, 255, 255, 42), 1)
        draw.rounded_rectangle([8, 8, W - 9, H - 9], radius=18, outline=(255, 255, 255, 25), width=1)

        _draw_header(draw, img, text_settings, match_no)
        _draw_stadium(draw, text_settings, stadium or (match_date.strftime("%d %b %Y") if isinstance(match_date, datetime) else "MATCH"))

        top_per_team = top_per_team or {}
        inn1_data = top_per_team.get("inn1", {})
        inn2_data = top_per_team.get("inn2", {})
        x, w = 10, 1580
        _draw_innings(draw, x, 200, w, text_settings,
                      team=inn1_data.get("team") or inn1_team,
                      runs=inn1_runs, wickets=inn1_wickets, overs=inn1_overs,
                      overs_total=overs_total,
                      batters=inn1_data.get("batters", []),
                      bowlers=inn1_data.get("bowlers", []),
                      bat_color=BLUE, bowl_color=RED, potm_name=potm_name)
        _draw_innings(draw, x, 430, w, text_settings,
                      team=inn2_data.get("team") or inn2_team,
                      runs=inn2_runs, wickets=inn2_wickets, overs=inn2_overs,
                      overs_total=overs_total,
                      batters=inn2_data.get("batters", []),
                      bowlers=inn2_data.get("bowlers", []),
                      bat_color=RED, bowl_color=BLUE, potm_name=potm_name)
        _draw_result(draw, x, 660, w, text_settings, winner_name, win_margin_text)
        _draw_potm(draw, x, 742, w, text_settings, potm_name, potm_stats)

        out = Image.new("RGB", img.size, (3, 4, 6))
        out.paste(img, mask=img.split()[-1])
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Failed to render match summary card")
        return None
