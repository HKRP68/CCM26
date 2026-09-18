"""Match summary card renderer — the light "poster" design.

The layout reproduces the supplied reference poster (1675×939) rather than the
dark metal card this module used to draw: a near-white paper ground with gold
corner wedges, a branded header, two innings blocks each led by a team crest
panel, a navy result bar, and a Player of the Match strip carrying a five-metric
performance showcase.

Every geometry constant below is expressed in *reference pixels* — the same
1675×939 space the reference image is measured in — and multiplied by ``SCALE``
at the point of use. The card is drawn at 2× and LANCZOS-downsampled, because
PIL does not antialias text or polygon edges and a 1× draw looks visibly ragged
at the sizes this design uses.

Two things are deliberately resolved outside the stored payload, by
``services.scorecard_delivery``: the per-innings accent colours and the team
crests. A card redrawn months later then follows the theme and the logos in
force *now*, which is what /lastscorecard should show.
"""

import io
import logging
import math
import os
import re
from datetime import datetime
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    from services.scorecard_card import normalize_scorecard_text_settings
except Exception:  # pragma: no cover - defensive import fallback for legacy paths
    normalize_scorecard_text_settings = None

logger = logging.getLogger(__name__)

# ── Canvas ───────────────────────────────────────────────────────────
CANVAS_W = 1675
CANVAS_H = 939
SCALE = 2

# ── Palette (from the reference's own :root tokens) ──────────────────
INK = (7, 26, 52)
INK_SOFT = (33, 38, 52)
GOLD = (199, 146, 44)
GOLD_BRIGHT = (255, 195, 45)
GOLD_PALE = (233, 201, 132)
TEAM_A = (170, 0, 27)
TEAM_B = (0, 101, 179)
PAPER = (252, 252, 254)
PAPER_EDGE = (237, 242, 247)
LINE = (217, 222, 231)
ROW_LINE = (226, 229, 234)
HEAD_WASH = (240, 243, 247)
COL_HEAD = (96, 112, 134)
NAME_INK = (16, 27, 45)
VALUE_INK = (12, 22, 38)
RESULT_ACCENT = (66, 185, 255)
POTM_BG_A = (7, 27, 53)
POTM_BG_B = (14, 48, 89)
METRIC_LABEL = (198, 212, 232)
WHITE = (255, 255, 255)
# Impact substitutes get a green row instead of their side's colour, so a
# changed XI is obvious on the card without reading the names.
IMPACT_GREEN = (22, 140, 66)

# ── Geometry, measured off the reference ─────────────────────────────
PAD_L = 51
PAD_R = 54
CONTENT_R = CANVAS_W - PAD_R            # 1621

HEADER_H = 182
LOGO_BOX = (44, 2, 296, 174)            # the CricMaster Ultra crest
BRAND_RULE_X = 300
BRAND_CAPS_X = 312
BRAND_CAPS_TOP = 50
BRAND_CAPS_LEAD = 29
HEADLINE_CY = 60                        # vertical centre of MATCH SUMMARY
HEADLINE_CX = 840
GOLD_RULE = (509, 109, 1135, 114)
PILL = (483, 126, 1190, 167)
HEADLINE_SPAN = 697                     # MATCH+SUMMARY, edge to edge
TAGLINE_X = 1322
TAGLINE_TOP = 47
TAGLINE_LEAD = 29
WATERMARK_BOX = (1452, 96, 1660, 166)

INN1_Y = 186
INN2_Y = 463
INN_H = 265
BAR_H = 68
CREST_X1_TOP = 292                      # crest panel's right edge at its top…
CREST_X1_BOT = 243                      # …and at its bottom (an angled cut)
TABLE_X = 217                           # the colour bar's left edge
SCORE_X = 1351                          # score panel's left edge
BAR_NAME_X = 292
OVERS_R = 1278                          # right edge of the "20 OVERS" label

HEAD_ROW_H = 34                         # column-header band inside a block
ROW_PITCH = 38.4
TABLE_DIV = 919                         # BATTERS | BOWLERS divider
NAME_L_X = 308
NAME_R_X = 959
COL_L1_CX = 686                         # RUNS
COL_L2_CX = 813                         # BALLS
COL_R1_CX = 1375                        # FIGURES
COL_R2_CX = 1520                        # OVERS

RESULT_X0 = 83
RESULT_X1 = 1603
RESULT_Y = 736
RESULT_H = 52
RESULT_BEVELS = ((290, 320), (1365, 1395))

POTM_Y = 794
POTM_H = 125
POTM_TROPHY_CX = 102
POTM_TITLE_X = 153
POTM_PHOTO = (318, 480)                 # photo band, x
POTM_NAME_X = 508
POTM_SHOWCASE_CX = 1105
POTM_METRIC_EDGES = (790, 912, 1042, 1160, 1278, 1420)
POTM_SCRIPT_CX = 1548

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOGO_PATH = os.path.join(_ROOT_DIR, "assets", "logo.png")
_FONT_DIR = os.path.join(_ROOT_DIR, "assets", "fonts")

# Each family lists real filenames in fallback order. A name that is not on disk
# silently drops the whole family to DejaVu, which is how every "body" string on
# the old card ended up in the wrong face for months — so the test suite asserts
# these resolve.
_HEADLINE_FONTS = (
    # A wide black grotesque. The reference's headline, team names, scores and
    # metric values are all set in one, and a condensed face (Bebas, Anton)
    # lands about 40% narrow at the same cap height.
    os.path.join(_FONT_DIR, "ArchivoBlack-Regular.ttf"),
    os.path.join(_FONT_DIR, "Montserrat-ExtraBold.ttf"),
    os.path.join(_FONT_DIR, "Anton-Regular.ttf"),
    os.path.join(_FONT_DIR, "BebasNeue-Regular.ttf"),
)
_DISPLAY_FONTS = (
    os.path.join(_FONT_DIR, "BebasNeue-Regular.ttf"),
    "/usr/share/fonts/truetype/bebas-neue/BebasNeue-Regular.ttf",
    "/usr/share/fonts/opentype/bebas-neue/BebasNeue-Regular.otf",
)
_BODY_FONTS = (
    os.path.join(_FONT_DIR, "BricolageGrotesque-Regular.ttf"),
    os.path.join(_FONT_DIR, "RussoOne-Regular.ttf"),
)
_ITALIC_FONTS = (
    os.path.join(_FONT_DIR, "Lato-Italic.ttf"),
)
_SCRIPT_FONTS = (
    os.path.join(_FONT_DIR, "Caveat-Bold.ttf"),
)

# Backwards-compatible aliases for callers/tests that inspect the old constants.
_BEBAS_FONT_CANDIDATES = _DISPLAY_FONTS
_BODY_FONT_CANDIDATES = _BODY_FONTS
_BODY_ITALIC_FONT_CANDIDATES = _ITALIC_FONTS
_BRICOLAGE_FONT_CANDIDATES = _BODY_FONTS

_FAMILIES = {
    "headline": _HEADLINE_FONTS,
    "display": _DISPLAY_FONTS,
    "body": _BODY_FONTS,
    "italic": _ITALIC_FONTS,
    "script": _SCRIPT_FONTS,
}


# ══════════════════════════════════════════════════════════════════════
# Scaling, fonts, text
# ══════════════════════════════════════════════════════════════════════

def s(v):
    """Reference pixels → canvas pixels."""
    return int(round(v * SCALE))


def sbox(x0, y0, x1, y1):
    return [s(x0), s(y0), s(x1), s(y1)]


def _first_existing(paths):
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None


@lru_cache(maxsize=512)
def _cached_truetype(path, size):
    # Fonts are immutable and a card loads dozens of (path, size) pairs;
    # ``truetype`` re-parses the file on every call. Exceptions aren't cached.
    return ImageFont.truetype(path, size)


def _font(size, family="body"):
    """A font at *reference* point size — the scale factor is applied here."""
    px = max(6, s(size))
    candidate = _first_existing(_FAMILIES.get(family, _BODY_FONTS))
    if candidate:
        try:
            return _cached_truetype(candidate, px)
        except (OSError, IOError):
            logger.warning("Unable to load bundled summary-card font %s", candidate)
    fallback = {
        "italic": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "headline": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "display": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    }.get(family, "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    try:
        return _cached_truetype(fallback, px)
    except (OSError, IOError):
        return ImageFont.load_default()


def _settings(text_settings):
    if normalize_scorecard_text_settings:
        try:
            return normalize_scorecard_text_settings(text_settings).get("summary", {})
        except Exception:
            return {}
    return {}


def _setting(text_settings, key):
    return _settings(text_settings).get(
        key, {"text": "", "font": "body", "size": 0, "x": 0, "y": 0})


def _font_for(text_settings, key, size, family="body"):
    cfg = _setting(text_settings, key)
    fam = cfg.get("font") or family
    if fam not in _FAMILIES:
        fam = family
    return _font(size + int(cfg.get("size", 0) or 0), family=fam)


def _xy(text_settings, key, x, y):
    cfg = _setting(text_settings, key)
    return x + int(cfg.get("x", 0) or 0), y + int(cfg.get("y", 0) or 0)


def _txt(text_settings, key, default):
    cfg = _setting(text_settings, key)
    return str(cfg.get("text") or default)


def _tw(draw, text, font):
    bb = draw.textbbox((0, 0), str(text), font=font)
    return (bb[2] - bb[0]) / SCALE


def _th(draw, text, font):
    bb = draw.textbbox((0, 0), str(text), font=font)
    return (bb[3] - bb[1]) / SCALE


def _tracked_w(draw, text, font, tracking=0):
    text = str(text)
    if not text:
        return 0
    return sum(_tw(draw, ch, font) for ch in text) + (len(text) - 1) * tracking


def _draw_text(draw, xy, text, font, fill, tracking=0, anchor="ls", weight=0):
    """Draw at a reference-pixel position. ``anchor`` follows PIL's convention;
    tracking (letter-spacing) is applied per character because PIL has none.

    ``weight`` thickens the strokes. Bricolage Grotesque ships Regular only, and
    the reference's player names are appreciably heavier than regular, so the
    weight is faked with a same-colour stroke rather than a second font file.
    """
    x, y = xy
    text = str(text)
    stroke = max(0, int(round(weight * SCALE)))
    if not tracking:
        draw.text((s(x), s(y)), text, font=font, fill=fill, anchor=anchor,
                  stroke_width=stroke, stroke_fill=fill)
        return
    total = _tracked_w(draw, text, font, tracking)
    if anchor[0] == "m":
        x -= total / 2
    elif anchor[0] == "r":
        x -= total
    cursor = x
    char_anchor = "l" + anchor[1]
    for ch in text:
        draw.text((s(cursor), s(y)), ch, font=font, fill=fill, anchor=char_anchor,
                  stroke_width=stroke, stroke_fill=fill)
        cursor += _tw(draw, ch, font) + tracking


def _fit(draw, text, font, max_w, min_len=4):
    """Ellipsise to fit. Team names here are user-supplied and up to 50 chars,
    so nothing on this card may assume the reference's short names."""
    text = str(text)
    while _tw(draw, text, font) > max_w and len(text) > min_len:
        text = text[:-2] + "…"
    return text


def _fitted_font(draw, text, family, size, max_w, min_size=None):
    """Shrink a font until the string fits, rather than ellipsising it."""
    min_size = min_size or max(10, int(size * 0.55))
    font = _font(size, family=family)
    while size > min_size and _tw(draw, text, font) > max_w:
        size -= 1
        font = _font(size, family=family)
    return font


def _draw_italic_text(img, xy, text, font, fill, shear=0.20, anchor="ls",
                      tracking=0):
    """Right-leaning text, for the headline and the big scores.

    None of the bundled display faces ship an italic, so the glyphs are drawn to
    a transparent layer and sheared. Doing it per string (rather than shearing
    the whole card) keeps the rest of the layout upright.
    """
    text = str(text)
    if not text:
        return
    probe = ImageDraw.Draw(img)
    w = _tracked_w(probe, text, font, tracking)
    h = _th(probe, text, font)
    pad = h * 1.2
    lw, lh = s(w + h * shear + pad * 2), s(h + pad * 2)
    layer = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    _draw_text(ld, (pad, pad + h), text, font, fill, tracking=tracking, anchor="ls")
    # Map output → input: x_in = x_out + shear*(y_out - height) leans the top right.
    layer = layer.transform(
        (lw, lh), Image.AFFINE, (1, shear, -shear * lh, 0, 1, 0),
        resample=Image.BICUBIC)
    x, y = xy
    if anchor[0] == "m":
        x -= w / 2
    elif anchor[0] == "r":
        x -= w
    if anchor[1] == "s":
        y -= h
    elif anchor[1] == "m":
        y -= h / 2
    img.alpha_composite(layer, (s(x - pad), s(y - pad)))


# ══════════════════════════════════════════════════════════════════════
# Colour helpers
# ══════════════════════════════════════════════════════════════════════

def _hex_to_rgb(value, default):
    if not value:
        return default
    try:
        raw = str(value).strip().lstrip("#")
        if len(raw) == 3:
            raw = "".join(ch * 2 for ch in raw)
        if len(raw) < 6:
            return default
        return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
    except (TypeError, ValueError):
        return default


def _shade(color, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def _mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _diagonal_gradient(size, c0, c1):
    """A 135° two-stop gradient, the crest panel's fill."""
    w, h = size
    grad = Image.new("RGB", size, c0)
    gd = ImageDraw.Draw(grad)
    span = w + h
    step = max(2, span // 120)
    for i in range(0, span, step):
        gd.line([(i, 0), (0, i)], fill=_mix(c0, c1, min(1.0, i / span)),
                width=step + 2)
    return grad


def _readable_on(color):
    """White or ink, whichever survives on ``color``."""
    lum = 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]
    return INK if lum > 168 else WHITE


# ══════════════════════════════════════════════════════════════════════
# Background furniture
# ══════════════════════════════════════════════════════════════════════

def _draw_paper(img):
    """linear-gradient(135deg, #fff 0 72%, #edf2f7) plus the corner furniture."""
    w, h = img.size
    grad = Image.new("RGB", (w, h), PAPER)
    gd = ImageDraw.Draw(grad)
    span = w + h
    for i in range(0, span, 4):
        t = i / span
        t = 0.0 if t <= 0.72 else (t - 0.72) / 0.28
        gd.line([(i, 0), (0, i)], fill=_mix(PAPER, PAPER_EDGE, t), width=6)
    img.paste(grad.convert("RGBA"), (0, 0))
    draw = ImageDraw.Draw(img, "RGBA")

    # Navy corner wedges with a gold diagonal band riding on each — the poster's
    # top-left and bottom-right accents.
    draw.polygon([s(0), s(0), s(123), s(0), s(0), s(123)], fill=(*INK, 255))
    draw.polygon([s(0), s(124), s(124), s(0), s(146), s(0), s(0), s(146)],
                 fill=(*GOLD, 245))
    # Faint dot grid inside the navy corner, as on the reference.
    for cx in range(13, 120, 19):
        for cy in range(13, 120, 19):
            if cx + cy < 118:
                draw.ellipse([s(cx - 1.5), s(cy - 1.5), s(cx + 1.5), s(cy + 1.5)],
                             fill=(255, 255, 255, 52))


def _load_logo(box):
    try:
        if not os.path.exists(_LOGO_PATH):
            return None
        img = Image.open(_LOGO_PATH).convert("RGBA")
        img.thumbnail((s(box[2] - box[0]), s(box[3] - box[1])), Image.LANCZOS)
        return img
    except Exception:
        logger.warning("summary card: brand logo unreadable", exc_info=True)
        return None


def _open_crest(png_bytes, target):
    """A team crest from raw bytes, contain-fitted into ``target`` (reference px).

    ``thumbnail`` only ever shrinks, so a small upload would sit lost in the
    middle of the panel; this scales in both directions and keeps the aspect.
    """
    if not png_bytes:
        return None
    try:
        crest = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        tw, th = s(target[0]), s(target[1])
        if not crest.width or not crest.height:
            return None
        factor = min(tw / crest.width, th / crest.height)
        size = (max(1, int(crest.width * factor)), max(1, int(crest.height * factor)))
        return crest.resize(size, Image.LANCZOS)
    except Exception:
        # A user-supplied image that PIL cannot decode must never cost the card.
        logger.warning("summary card: team crest unreadable", exc_info=True)
        return None


def _initials(name, limit=3):
    words = re.findall(r"[A-Za-z0-9]+", str(name or ""))
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:limit].upper()
    return "".join(w[0] for w in words[:limit]).upper()


# ══════════════════════════════════════════════════════════════════════
# Header
# ══════════════════════════════════════════════════════════════════════

def _draw_header(img, draw, ts, *, match_no, stadium, header_left, header_right,
                 tagline):
    logo = _load_logo(LOGO_BOX)
    if logo:
        lx = LOGO_BOX[0] + ((LOGO_BOX[2] - LOGO_BOX[0]) - logo.width / SCALE) / 2
        ly = LOGO_BOX[1] + ((LOGO_BOX[3] - LOGO_BOX[1]) - logo.height / SCALE) / 2
        img.alpha_composite(logo, (s(lx), s(ly)))

    draw.line([(s(BRAND_RULE_X), s(46)), (s(BRAND_RULE_X), s(140))],
              fill=(*GOLD, 210), width=s(2))
    caps_font = _font_for(ts, "brand_tagline", 14, family="display")
    caps = _txt(ts, "brand_tagline", "PLAY|MANAGE|COMPETE|DOMINATE").split("|")
    cx, cy = _xy(ts, "brand_tagline", BRAND_CAPS_X, BRAND_CAPS_TOP)
    for i, word in enumerate(caps[:4]):
        _draw_text(draw, (cx, cy + i * BRAND_CAPS_LEAD), word.strip().upper(),
                   caps_font, INK_SOFT, tracking=3.2, anchor="lm", weight=0.3)

    # MATCH SUMMARY — two words, two colours, one italic display face.
    left = (header_left or _txt(ts, "header_title", "MATCH")).upper()
    right = (header_right if header_right is not None
             else _txt(ts, "header_summary", "SUMMARY")).upper()
    gap = 22
    track = 1.5
    # Sized to the reference's own 697px headline footprint, so the two words
    # land where the poster puts them whatever the string is.
    f_head = _font_for(ts, "header_title", 67, family="headline")
    span = _tracked_w(draw, left, f_head, track) + gap + _tracked_w(draw, right, f_head, track)
    if span > HEADLINE_SPAN or span < HEADLINE_SPAN * 0.8:
        f_head = _fitted_font(draw, f"{left} {right}", "headline",
                              int(67 * HEADLINE_SPAN / max(span, 1)),
                              HEADLINE_SPAN, min_size=30)
    wl = _tracked_w(draw, left, f_head, track)
    wr = _tracked_w(draw, right, f_head, track)
    total = wl + gap + wr
    hx, hy = _xy(ts, "header_title", HEADLINE_CX - total / 2, HEADLINE_CY)
    _draw_italic_text(img, (hx, hy), left, f_head, (*INK, 255),
                      anchor="lm", tracking=track)
    _draw_italic_text(img, (hx + wl + gap, hy), right, f_head, (*GOLD, 255),
                      anchor="lm", tracking=track)

    draw.rectangle(sbox(*GOLD_RULE), fill=(*GOLD, 255))

    # Venue pill.
    pill_font = _font_for(ts, "stadium", 21, family="display")
    venue = _txt(ts, "match_no", f"MATCH {match_no}" if match_no else "MATCH")
    place = _txt(ts, "stadium", (stadium or "").strip())
    label = f"{venue}   •   {place}".upper() if place else venue.upper()
    x0, y0, x1, y1 = PILL
    label = _fit(draw, label, pill_font, (x1 - x0) - 74, 8)
    width = max(_tracked_w(draw, label, pill_font, 2.4) + 74, 300)
    px0 = HEADLINE_CX - width / 2
    draw.rounded_rectangle(sbox(px0, y0, px0 + width, y1),
                           radius=s((y1 - y0) / 2), fill=(*INK, 255))
    sx, sy = _xy(ts, "stadium", HEADLINE_CX, (y0 + y1) / 2 + 1)
    _draw_text(draw, (sx, sy), label, pill_font, WHITE, tracking=2.4, anchor="mm")

    # Right-hand strapline and the faded wordmark beneath it.
    tag_font = _font_for(ts, "tagline", 15, family="display")
    words = (tagline or _txt(ts, "tagline", "CRICKET|BEYOND|BOUNDARIES")).split("|")
    tx, ty = _xy(ts, "tagline", TAGLINE_X, TAGLINE_TOP)
    for i, word in enumerate(words[:3]):
        _draw_text(draw, (tx, ty + i * TAGLINE_LEAD), word.strip().upper(),
                   tag_font, INK_SOFT if i else GOLD, tracking=3.4, anchor="lm",
                   weight=0.3)
    mark_font = _font(62, family="headline")
    _draw_italic_text(img, ((WATERMARK_BOX[0] + WATERMARK_BOX[2]) / 2,
                            (WATERMARK_BOX[1] + WATERMARK_BOX[3]) / 2),
                      "CMU", mark_font, (*INK, 34), anchor="mm")


# ══════════════════════════════════════════════════════════════════════
# Innings block
# ══════════════════════════════════════════════════════════════════════

def _normalise_batters(rows):
    """(name, runs, balls, is_impact, not_out) — the flags ride along because
    ``_draw_rows`` tints an Impact row green and colours a not-out score."""
    out = []
    for b in (rows or [])[:4]:
        not_out = not b.get("out", False)
        out.append((b.get("name", "—"),
                    f"{b.get('runs', 0)}{'*' if not_out else ''}",
                    str(b.get("balls", 0)), bool(b.get("impact")), not_out))
    while len(out) < 4:
        out.append(("—", "—", "—", False, False))
    return out


def _normalise_bowlers(rows):
    out = []
    for b in (rows or [])[:4]:
        out.append((b.get("name", "—"),
                    f"{b.get('wickets', 0)}-{b.get('runs', 0)}",
                    str(b.get("overs", "0")), bool(b.get("impact")), False))
    while len(out) < 4:
        out.append(("—", "—", "—", False, False))
    return out


def _draw_rows(draw, ts, rows, *, x_name, cx1, cx2, top, color, potm_name,
               max_name_w):
    name_f = _font_for(ts, "row_name", 23, family="body")
    # RUNS/FIGURES carry the weight in the reference; BALLS/OVERS are regular.
    num_f = _font_for(ts, "row_number", 21, family="headline")
    alt_f = _font(23 + int(_setting(ts, "row_number").get("size", 0) or 0),
                  family="body")
    badge_f = _font(12, family="display")
    for i, (name, v1, v2, impact, not_out) in enumerate(rows[:4]):
        y0 = top + i * ROW_PITCH
        y1 = top + (i + 1) * ROW_PITCH
        cy = (y0 + y1) / 2
        if impact:
            draw.rectangle(sbox(x_name - 18, y0, cx2 + 60, y1),
                           fill=(*IMPACT_GREEN, 30))
        draw.line([(s(x_name - 18), s(y1)), (s(cx2 + 60), s(y1))],
                  fill=(*ROW_LINE, 255), width=s(1))

        label = _fit(draw, str(name).upper(), name_f, max_name_w)
        nx, ny = _xy(ts, "row_name", x_name, cy + 1)
        _draw_text(draw, (nx, ny), label, name_f,
                   IMPACT_GREEN if impact else NAME_INK, anchor="lm", weight=0.45)
        if potm_name and str(name).strip().lower() == str(potm_name).strip().lower():
            bx = nx + _tw(draw, label, name_f) + 12
            if bx + 54 < cx1 - 34:
                draw.rounded_rectangle(sbox(bx, cy - 11, bx + 54, cy + 11),
                                       radius=s(5), fill=(*GOLD_BRIGHT, 255))
                _draw_text(draw, (bx + 27, cy + 1), "POTM", badge_f, INK,
                           tracking=0.8, anchor="mm")

        v1x, v1y = _xy(ts, "row_number", cx1, cy + 1)
        _draw_text(draw, (v1x, v1y), str(v1).upper(), num_f,
                   color if not_out else VALUE_INK, anchor="mm")
        _draw_text(draw, (cx2, cy + 1), str(v2).upper(), alt_f, (64, 78, 98),
                   anchor="mm")


def _draw_innings(img, draw, ts, y, *, team, runs, wickets, overs, overs_total,
                  is_hundred, batters, bowlers, color, crest_png, potm_name):
    bar_y1 = y + BAR_H
    block_y1 = y + INN_H
    dark = _shade(color, 0.62)
    on_bar = _readable_on(color)

    # White card body first, so the crest and bar sit on top of it.
    draw.rounded_rectangle(sbox(PAD_L, y, CONTENT_R, block_y1), radius=s(16),
                           fill=(255, 255, 255, 255), outline=(*LINE, 190),
                           width=s(1))

    # Crest panel: rounded on the outside, cut on a diagonal down the inside
    # edge, so it narrows from CREST_X1_TOP to CREST_X1_BOT. Drawn as a
    # gradient rectangle behind a shaped mask — a polygon punched out of an
    # already-composited layer leaves black, not transparency.
    pw, ph = s(CREST_X1_TOP - PAD_L), s(block_y1 - y)
    panel = _diagonal_gradient((pw, ph), color, dark)
    mask = Image.new("L", (pw, ph), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=s(16), fill=255)
    md.polygon([(pw, 0), (pw, ph), (s(CREST_X1_BOT - PAD_L), ph)], fill=0)
    img.paste(panel, (s(PAD_L), s(y)), mask)

    crest = _open_crest(crest_png, (CREST_X1_BOT - PAD_L - 16, INN_H - 34))
    if crest:
        img.alpha_composite(crest, (s(PAD_L + (CREST_X1_BOT - PAD_L) / 2) - crest.width // 2,
                                    s(y + INN_H / 2) - crest.height // 2))
    else:
        mono = _fitted_font(draw, _initials(team), "headline", 56,
                            CREST_X1_BOT - PAD_L - 34)
        _draw_text(draw, ((PAD_L + CREST_X1_BOT) / 2, y + INN_H / 2 + 4),
                   _initials(team), mono, (*WHITE, 232), tracking=2, anchor="mm")

    # Colour bar across the top, with a darker score panel on the right.
    bar = Image.new("RGBA", (s(CANVAS_W), s(CANVAS_H)), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar, "RGBA")
    bd.rounded_rectangle(sbox(TABLE_X, y, CONTENT_R, bar_y1), radius=s(14),
                         fill=(*color, 255))
    bd.rectangle(sbox(TABLE_X, y + BAR_H - 16, CONTENT_R, bar_y1), fill=(*color, 255))
    bd.polygon([(s(SCORE_X + 26), s(y)), (s(CONTENT_R), s(y)),
                (s(CONTENT_R), s(bar_y1)), (s(SCORE_X), s(bar_y1))],
               fill=(*dark, 255))
    bd.rounded_rectangle(sbox(CONTENT_R - 30, y, CONTENT_R, bar_y1), radius=s(14),
                         fill=(*dark, 255))
    bd.rectangle(sbox(CONTENT_R - 30, y + BAR_H - 16, CONTENT_R, bar_y1),
                 fill=(*dark, 255))
    img.alpha_composite(bar)

    name = str(team or "—").upper()
    avail = OVERS_R - BAR_NAME_X - 150
    f_team = _font_for(ts, "innings_team", 38, family="headline")
    if _tw(draw, name, f_team) > avail:
        f_team = _fitted_font(draw, name, "headline", 38, avail, min_size=20)
        name = _fit(draw, name, f_team, avail, 5)
    tx, ty = _xy(ts, "innings_team", BAR_NAME_X, y + BAR_H / 2 + 1)
    _draw_text(draw, (tx, ty), name, f_team, on_bar, tracking=1.4, anchor="lm")

    f_overs = _font_for(ts, "innings_meta", 20, family="display")
    meta = _txt(ts, "innings_meta",
                f"{overs}/{overs_total} BALLS" if is_hundred else f"{overs} OVERS").upper()
    mx, my = _xy(ts, "innings_meta", OVERS_R, y + BAR_H / 2 + 1)
    _draw_text(draw, (mx, my), meta, f_overs, on_bar, tracking=1.6, anchor="rm")

    f_score = _font_for(ts, "innings_score", 53, family="headline")
    score = _txt(ts, "innings_score", f"{runs}-{wickets}").upper()
    px, py = _xy(ts, "innings_score", (SCORE_X + 26 + CONTENT_R) / 2,
                 y + BAR_H / 2 + 2)
    _draw_italic_text(img, (px, py), score, f_score, (*_readable_on(dark), 255),
                      anchor="mm", tracking=1)

    # Column-header band.
    head_y0 = bar_y1
    head_y1 = bar_y1 + HEAD_ROW_H
    draw.rectangle(sbox(CREST_X1_BOT + 8, head_y0, CONTENT_R - 6, head_y1),
                   fill=(*HEAD_WASH, 255))
    f_col = _font_for(ts, "table_header", 17, family="display")
    heads = (
        (_txt(ts, "header_batters", "BATTERS"), NAME_L_X, "lm"),
        (_txt(ts, "header_runs", "RUNS"), COL_L1_CX, "mm"),
        (_txt(ts, "header_balls", "BALLS"), COL_L2_CX, "mm"),
        (_txt(ts, "header_bowlers", "BOWLERS"), NAME_R_X, "lm"),
        (_txt(ts, "header_figures", "FIGURES"), COL_R1_CX, "mm"),
        (_txt(ts, "header_overs", "OVERS"), COL_R2_CX, "mm"),
    )
    for label, x, anchor in heads:
        _draw_text(draw, (x, (head_y0 + head_y1) / 2 + 1), label.upper(), f_col,
                   COL_HEAD, tracking=1.5, anchor=anchor)
    draw.line([(s(CREST_X1_BOT + 8), s(head_y1)), (s(CONTENT_R - 6), s(head_y1))],
              fill=(*LINE, 255), width=s(1))

    # The team-coloured divider between the two tables.
    draw.rectangle(sbox(TABLE_DIV - 2, head_y0 + 6, TABLE_DIV + 2, block_y1 - 8),
                   fill=(*color, 255))

    _draw_rows(draw, ts, _normalise_batters(batters), x_name=NAME_L_X,
               cx1=COL_L1_CX, cx2=COL_L2_CX, top=head_y1, color=color,
               potm_name=potm_name, max_name_w=COL_L1_CX - NAME_L_X - 96)
    _draw_rows(draw, ts, _normalise_bowlers(bowlers), x_name=NAME_R_X,
               cx1=COL_R1_CX, cx2=COL_R2_CX, top=head_y1, color=color,
               potm_name=potm_name, max_name_w=COL_R1_CX - NAME_R_X - 96)


# ══════════════════════════════════════════════════════════════════════
# Result bar
# ══════════════════════════════════════════════════════════════════════

def _draw_result(img, draw, ts, winner_name, win_margin_text, accent):
    y0, y1 = RESULT_Y, RESULT_Y + RESULT_H
    # The poster runs a gold diagonal off each edge, level with the result bar.
    for x_edge, sign in ((0, 1), (CANVAS_W, -1)):
        draw.polygon([(s(x_edge), s(y0 + 25)), (s(x_edge + sign * 92), s(y1 + 48)),
                      (s(x_edge + sign * 92), s(y1 + 70)), (s(x_edge), s(y0 + 47))],
                     fill=(*GOLD, 245))
    draw.rectangle(sbox(RESULT_X0, y0, RESULT_X1, y1), fill=(*INK, 255))
    for bx0, bx1 in RESULT_BEVELS:
        draw.polygon([(s(bx0 + 14), s(y0)), (s(bx1), s(y0)),
                      (s(bx1 - 14), s(y1)), (s(bx0), s(y1))], fill=(*GOLD, 255))
    draw.polygon([(s(RESULT_X0 + 14), s(y0)), (s(RESULT_X0), s(y0)),
                  (s(RESULT_X0), s(y1)), (s(RESULT_X0 + 14), s(y1))],
                 fill=(*GOLD, 255))
    draw.polygon([(s(RESULT_X1 - 14), s(y0)), (s(RESULT_X1), s(y0)),
                  (s(RESULT_X1), s(y1)), (s(RESULT_X1 - 14), s(y1))],
                 fill=(*GOLD, 255))

    head = f"{(winner_name or '—').upper()} WON"
    tail = str(win_margin_text or "").upper().strip()
    if tail.startswith("BY "):
        head, tail = head + " BY", tail[3:]
    f = _font_for(ts, "result", 32, family="headline")
    max_w = (RESULT_BEVELS[1][0] - RESULT_BEVELS[0][1]) - 40
    while (_tracked_w(draw, head + " " + tail, f, 1.6) > max_w
           and f.size > s(16)):
        f = _font((f.size / SCALE) - 1, family="headline")
    gap = _tw(draw, " ", f) + 1.6
    wh = _tracked_w(draw, head, f, 1.6)
    wt = _tracked_w(draw, tail, f, 1.6)
    cx = (RESULT_X0 + RESULT_X1) / 2
    rx, ry = _xy(ts, "result", cx - (wh + gap + wt) / 2, (y0 + y1) / 2 + 1)
    _draw_text(draw, (rx, ry), head, f, WHITE, tracking=1.6, anchor="lm")
    _draw_text(draw, (rx + wh + gap, ry), tail, f, accent, tracking=1.6,
               anchor="lm")


# ══════════════════════════════════════════════════════════════════════
# Player of the Match
# ══════════════════════════════════════════════════════════════════════

def _draw_script(img, draw, xy, text, font, angle=8):
    """The gold script flourish, set on a rise like the reference's."""
    cx, cy = xy
    words = str(text).split(" ")
    lines = [" ".join(words[:-1]), words[-1]] if len(words) > 1 else [words[0]]
    lines = [ln for ln in lines if ln]
    layer = Image.new("RGBA", (s(320), s(150)), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for i, line in enumerate(lines):
        ld.text((s(160), s(52 + i * 44)), line, font=font, fill=(*GOLD_BRIGHT, 255),
                anchor="mm")
    # A tapered underline sweeping out from the last word.
    ly = 52 + (len(lines) - 1) * 44 + 26
    for k in range(9):
        ld.line([(s(78 + k * 2), s(ly + k * 0.18)), (s(244 - k * 1.4), s(ly + 8 + k * 0.5))],
                fill=(*GOLD_BRIGHT, 255 - k * 18), width=s(1.4))
    layer = layer.rotate(angle, resample=Image.BICUBIC, center=(s(160), s(75)))
    img.alpha_composite(layer, (s(cx - 160), s(cy - 75)))


def _draw_trophy(draw, cx, cy, h=52):
    """A gold trophy drawn from primitives.

    None of the bundled faces carry U+1F3C6, and DejaVu renders it as a tofu
    box, so the emoji is not an option on a card this size.
    """
    w = h * 0.62
    top, bot = cy - h / 2, cy + h / 2
    cup_b = top + h * 0.52
    draw.polygon([(s(cx - w / 2), s(top)), (s(cx + w / 2), s(top)),
                  (s(cx + w * 0.30), s(cup_b)), (s(cx - w * 0.30), s(cup_b))],
                 fill=(*GOLD_BRIGHT, 255))
    draw.ellipse([s(cx - w / 2), s(top - h * 0.06), s(cx + w / 2), s(top + h * 0.10)],
                 fill=(*GOLD_BRIGHT, 255))
    for side in (-1, 1):
        draw.arc([s(cx + side * w * 0.46 - w * 0.34), s(top + h * 0.02),
                  s(cx + side * w * 0.46 + w * 0.34), s(top + h * 0.40)],
                 start=0, end=360, fill=(*GOLD, 255), width=s(3.2))
    draw.rectangle([s(cx - w * 0.09), s(cup_b), s(cx + w * 0.09), s(bot - h * 0.16)],
                   fill=(*GOLD, 255))
    draw.rounded_rectangle([s(cx - w * 0.36), s(bot - h * 0.16), s(cx + w * 0.36), s(bot)],
                           radius=s(3), fill=(*GOLD_BRIGHT, 255))


def _draw_potm(img, draw, ts, *, name, team, photo_png, metrics, flourish):
    y0, y1 = POTM_Y, POTM_Y + POTM_H
    band = Image.new("RGBA", (s(CANVAS_W), s(y1 - y0)), (0, 0, 0, 0))
    bd = ImageDraw.Draw(band, "RGBA")
    for i in range(s(CANVAS_W)):
        bd.line([(i, 0), (i, s(y1 - y0))],
                fill=(*_mix(POTM_BG_A, POTM_BG_B, i / s(CANVAS_W)), 255))
    img.alpha_composite(band, (0, s(y0)))
    draw.line([(0, s(y0)), (s(CANVAS_W), s(y0))], fill=(*GOLD, 220), width=s(2))

    _draw_trophy(draw, POTM_TROPHY_CX, (y0 + y1) / 2)

    f_title = _font_for(ts, "potm_badge", 24, family="display")
    title = _txt(ts, "potm_badge", "PLAYER|OF THE MATCH").split("|")
    bx, by = _xy(ts, "potm_badge", POTM_TITLE_X, (y0 + y1) / 2)
    _draw_text(draw, (bx, by - 14), title[0].strip().upper(), f_title, WHITE,
               tracking=1.4, anchor="lm")
    if len(title) > 1:
        _draw_text(draw, (bx, by + 16), title[1].strip().upper(),
                   _font(17, family="display"), GOLD_PALE, tracking=1.4,
                   anchor="lm")

    photo = _open_crest(photo_png, (POTM_PHOTO[1] - POTM_PHOTO[0], POTM_H + 16))
    if photo:
        # Bottom-aligned, so a cut-out reads as standing on the bar.
        img.alpha_composite(photo, (s((POTM_PHOTO[0] + POTM_PHOTO[1]) / 2) - photo.width // 2,
                                    s(y1) - photo.height))

    parts = str(name or "—").strip().split()
    first, last = (" ".join(parts[:-1]), parts[-1]) if len(parts) > 1 else ("", parts[0] if parts else "—")
    nx, ny = _xy(ts, "potm_name", POTM_NAME_X, (y0 + y1) / 2)
    name_w = POTM_METRIC_EDGES[0] - POTM_NAME_X - 116
    if first:
        _draw_text(draw, (nx, ny - 32), first.upper(), _font(19, family="display"),
                   GOLD_PALE, tracking=2.2, anchor="lm")
    f_last = _fitted_font(draw, last.upper(), "headline", 44, name_w, min_size=22)
    _draw_italic_text(img, (nx, ny + 6), last.upper(), f_last, (*WHITE, 255),
                      anchor="lm")
    if team:
        team_txt = _fit(draw, str(team).upper(), _font(13, family="display"),
                        name_w, 4)
        _draw_text(draw, (nx, ny + 36), team_txt, _font(13, family="display"),
                   METRIC_LABEL, tracking=2.0, anchor="lm")

    _draw_text(draw, (POTM_SHOWCASE_CX, y0 + 20),
               _txt(ts, "potm_label", "PERFORMANCE SHOWCASE").upper(),
               _font_for(ts, "potm_label", 15, family="display"), GOLD_PALE,
               tracking=3.0, anchor="mm")

    f_value = _font_for(ts, "potm_value", 31, family="headline")
    f_label = _font(12, family="display")
    for i, (value, label) in enumerate(metrics[:5]):
        x0, x1 = POTM_METRIC_EDGES[i], POTM_METRIC_EDGES[i + 1]
        if i:
            draw.line([(s(x0), s(y0 + 42)), (s(x0), s(y1 - 22))],
                      fill=(255, 255, 255, 62), width=s(1))
        cx = (x0 + x1) / 2
        _draw_italic_text(img, (cx, y0 + 66), str(value).upper(), f_value,
                          (*WHITE, 255), anchor="mm")
        _draw_text(draw, (cx, y0 + 92), str(label).upper(), f_label,
                   METRIC_LABEL, tracking=1.6, anchor="mm")

    script = _txt(ts, "game_changer", flourish or "Game Changer!")
    f_script = _font_for(ts, "game_changer", 34, family="script")
    gx, gy = _xy(ts, "game_changer", POTM_SCRIPT_CX, (y0 + y1) / 2)
    _draw_script(img, draw, (gx, gy), script, f_script)


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════

_BAT_STAT_RE = re.compile(r"(\d+)\s*\*?\s*\((\d+)\)")
_BOWL_STAT_RE = re.compile(r"(\d+)\s*[-/]\s*(\d+)")
DASH = "—"


def _num(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _potm_metrics(potm_stats, runs, balls, fours, sixes, sr, *,
                  wickets=None, conceded=None, overs=None, economy=None,
                  dots=None):
    """Five (value, label) pairs for the performance showcase.

    Values the payload carries win; anything missing is recovered from the
    ``potm_stats`` display string, so cards archived before those keys existed
    still fill the strip. A bowling Player of the Match gets a bowling
    showcase — five dashes under RUNS/BALLS/FOURS/SIXES is not a performance.
    """
    stats = str(potm_stats or "")
    if runs is None or balls is None:
        m = _BAT_STAT_RE.search(stats)
        if m:
            runs = runs if runs is not None else m.group(1)
            balls = balls if balls is not None else m.group(2)
    if wickets is None or conceded is None:
        m = _BOWL_STAT_RE.search(stats)
        if m and not _BAT_STAT_RE.search(stats):
            wickets = wickets if wickets is not None else m.group(1)
            conceded = conceded if conceded is not None else m.group(2)

    batted = _num(runs) is not None and _num(balls) is not None
    bowled = _num(wickets) is not None
    if bowled and not batted:
        if economy is None and _num(conceded) is not None and _num(overs):
            economy = round(_num(conceded) / _num(overs), 2)
        return [
            (wickets if wickets not in (None, "") else DASH, "WICKETS"),
            (conceded if conceded not in (None, "") else DASH, "RUNS"),
            (overs if overs not in (None, "") else DASH, "OVERS"),
            (economy if economy not in (None, "") else DASH, "ECONOMY"),
            (dots if dots not in (None, "") else DASH, "DOTS"),
        ]

    if sr is None and batted:
        sr = round(_num(runs) * 100.0 / _num(balls), 1) if _num(balls) else None
    star = "*" if "*" in stats else ""
    return [
        (f"{runs}{star}" if runs not in (None, "") else DASH, "RUNS"),
        (balls if balls not in (None, "") else DASH, "BALLS"),
        (fours if fours not in (None, "") else DASH, "FOURS"),
        (sixes if sixes not in (None, "") else DASH, "SIXES"),
        (sr if sr not in (None, "") else DASH, "STRIKE RATE"),
    ]


def _flourish(potm_stats, runs, wickets):
    """The script line. Named for what the player actually did, so the card
    reads as written rather than stamped."""
    try:
        if wickets is not None and int(wickets) >= 5:
            return "Five For!"
    except (TypeError, ValueError):
        pass
    try:
        if runs is not None and int(runs) >= 100:
            return "Century!"
        if runs is not None and int(runs) >= 50:
            return "Match Winner!"
    except (TypeError, ValueError):
        pass
    return "Game Changer!"


def generate_match_summary(*,
    inn1_team, inn1_runs, inn1_wickets, inn1_overs,
    inn2_team, inn2_runs, inn2_wickets, inn2_overs,
    winner_name, win_margin_text,
    overs_total,
    is_hundred=False,
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
    header_left=None,
    header_right=None,
    inn1_color=None,
    inn2_color=None,
    inn1_logo_png=None,
    inn2_logo_png=None,
    potm_photo_png=None,
    potm_runs=None,
    potm_balls=None,
    potm_fours=None,
    potm_sixes=None,
    potm_wickets=None,
    potm_conceded=None,
    potm_overs=None,
    potm_economy=None,
    potm_dots=None,
    potm_sr=None,
    tagline=None,
    dynamic_flourish=False,
) -> bytes | None:
    """Render the match summary card. Returns PNG bytes or ``None`` on failure."""
    try:
        ts = text_settings
        img = Image.new("RGBA", (s(CANVAS_W), s(CANVAS_H)), (255, 255, 255, 255))
        _draw_paper(img)
        draw = ImageDraw.Draw(img, "RGBA")

        colour_a = _hex_to_rgb(inn1_color, TEAM_A)
        colour_b = _hex_to_rgb(inn2_color, TEAM_B)

        venue = stadium or (match_date.strftime("%d %b %Y")
                            if isinstance(match_date, datetime) else "")
        _draw_header(img, draw, ts, match_no=match_no, stadium=venue,
                     header_left=header_left, header_right=header_right,
                     tagline=tagline)

        top_per_team = top_per_team or {}
        inn1 = top_per_team.get("inn1", {})
        inn2 = top_per_team.get("inn2", {})
        _draw_innings(img, draw, ts, INN1_Y,
                      team=inn1.get("team") or inn1_team,
                      runs=inn1_runs, wickets=inn1_wickets, overs=inn1_overs,
                      overs_total=overs_total, is_hundred=is_hundred,
                      batters=inn1.get("batters", []),
                      bowlers=inn1.get("bowlers", []),
                      color=colour_a, crest_png=inn1_logo_png,
                      potm_name=potm_name)
        _draw_innings(img, draw, ts, INN2_Y,
                      team=inn2.get("team") or inn2_team,
                      runs=inn2_runs, wickets=inn2_wickets, overs=inn2_overs,
                      overs_total=overs_total, is_hundred=is_hundred,
                      batters=inn2.get("batters", []),
                      bowlers=inn2.get("bowlers", []),
                      color=colour_b, crest_png=inn2_logo_png,
                      potm_name=potm_name)

        # The winner's own colour would vanish against the navy bar, so the
        # margin keeps the reference's fixed cyan.
        _draw_result(img, draw, ts, winner_name, win_margin_text, RESULT_ACCENT)

        metrics = _potm_metrics(potm_stats, potm_runs, potm_balls, potm_fours,
                                potm_sixes, potm_sr, wickets=potm_wickets,
                                conceded=potm_conceded, overs=potm_overs,
                                economy=potm_economy, dots=potm_dots)
        _draw_potm(img, draw, ts, name=potm_name, team=potm_team,
                   photo_png=potm_photo_png, metrics=metrics,
                   flourish=(_flourish(potm_stats, potm_runs, potm_wickets)
                             if dynamic_flourish else "Game Changer!"))

        out = Image.new("RGB", img.size, (255, 255, 255))
        out.paste(img, mask=img.split()[-1])
        out = out.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("Failed to render match summary card")
        return None
