"""CMU S1 in-match batting/bowling stats cards.

The layout is based on ``cmu_batting_stats_card.html`` and
``cmu_bowling_stats_card-4.html``. Text labels and font sizes are read from the
website-managed card-template state, which is mirrored to the Telegram storage
channel by ``card_template_storage_service``.
"""

from __future__ import annotations

import io
import json
import logging
import os
from functools import lru_cache
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except Exception:  # Pillow is a runtime dependency; keep admin settings usable if absent locally.
    Image = ImageDraw = ImageFont = ImageFilter = None

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_ROOT = os.path.join(ROOT, "assets", "fonts")
CARD_TEMPLATES_ROOT = os.path.join(ROOT, "data", "card_templates")

def _first_existing(*paths: str) -> str:
    for path in paths:
        if os.path.isfile(path):
            return path
    return paths[0]

TEXT_FONT_PATH = _first_existing(
    os.path.join(FONT_ROOT, "BricolageGrotesque-Regular.ttf"),
    os.path.join(CARD_TEMPLATES_ROOT, "BricolageGrotesque-Regular.ttf"),
)
DISPLAY_FONT_PATH = _first_existing(
    os.path.join(FONT_ROOT, "RussoOne-Regular.ttf"),
    os.path.join(CARD_TEMPLATES_ROOT, "RussoOne-Regular.ttf"),
)
LOGO_PATH = _first_existing(
    os.path.join(ROOT, "assets", "logo.png"),
    os.path.join(CARD_TEMPLATES_ROOT, "cmu_logo.png"),
)

W, H = 1536, 864

RED = (255, 31, 55, 255)
RED_DARK = (156, 7, 21, 255)
BLUE = (33, 148, 255, 255)
GOLD = (255, 199, 103, 255)
WHITE = (247, 248, 251, 255)
MUTED = (174, 181, 194, 255)
PANEL = (5, 16, 31, 210)
DEFAULT_STATS_CARD_SETTINGS: dict[str, Any] = {
    "stats_enabled": True,
    "title_batting": "CMU S1 Stats Batting Stats",
    "title_bowling": "CMU S1 Bowling Stats",
    "badge_batting": "Batter",
    "badge_bowling": "Bowler",
    "ovr_label": "OVR",
    "bat_rating_label": "Batting Rating",
    "bowl_rating_label": "Bowling Rating",
    "bat_meta_suffix": "Hand Bat",
    "name_font_size": 100,
    "rating_label_font_size": 28,
    "rating_num_font_size": 74,
    "badge_font_size": 30,
    "meta_font_size": 29,
    "title_font_size": 34,
    "stat_label_font_size": 29,
    "stat_value_font_size": 62,
    "stat_value_second_font_size": 58,
    "batting_labels": {
        "inns": "Inns", "runs": "Runs", "hs": "HS", "avg": "Avg", "sr": "SR",
        "hundreds": "100s", "fifties": "50s", "fours": "4s", "sixes": "6s", "ducks": "Ducks",
    },
    "bowling_labels": {
        "inns": "Inns", "wickets": "Wickets", "bbf": "BBF", "avg": "Avg", "econ": "Econ",
        "sr": "SR", "hat_tricks": "Hattricks", "five_fers": "5-Fers", "three_fers": "3-Fers",
    },
}

_NUM_LIMITS = {
    "name_font_size": (40, 128),
    "rating_label_font_size": (12, 42),
    "rating_num_font_size": (28, 96),
    "badge_font_size": (14, 44),
    "meta_font_size": (12, 42),
    "title_font_size": (16, 52),
    "stat_label_font_size": (12, 42),
    "stat_value_font_size": (24, 78),
    "stat_value_second_font_size": (24, 76),
}
_TEXT_LIMIT = 42



@lru_cache(maxsize=96)
def _font(path: str, size: int):
    try:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    except Exception:
        pass
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", size)
    except Exception:
        return ImageFont.load_default()


def text_font(size: int):
    return _font(TEXT_FONT_PATH, int(size))


def display_font(size: int):
    return _font(DISPLAY_FONT_PATH, int(size))


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(float(value))))
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any, default: str, limit: int = _TEXT_LIMIT) -> str:
    value = str(value if value is not None else default).strip()
    value = " ".join(value.split())
    return (value or default)[:limit]


def normalise_stats_card_settings(raw: Any = None) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    result = dict(DEFAULT_STATS_CARD_SETTINGS)
    result["batting_labels"] = dict(DEFAULT_STATS_CARD_SETTINGS["batting_labels"])
    result["bowling_labels"] = dict(DEFAULT_STATS_CARD_SETTINGS["bowling_labels"])
    result["stats_enabled"] = bool(raw.get("stats_enabled", result["stats_enabled"]))
    for key in ("title_batting", "title_bowling", "badge_batting", "badge_bowling",
                "ovr_label", "bat_rating_label", "bowl_rating_label", "bat_meta_suffix"):
        result[key] = _clean_text(raw.get(key), result[key], 64 if key.startswith("title_") else _TEXT_LIMIT)
    for key, (lo, hi) in _NUM_LIMITS.items():
        result[key] = _clamp_int(raw.get(key), result[key], lo, hi)
    for group in ("batting_labels", "bowling_labels"):
        incoming = raw.get(group) if isinstance(raw.get(group), dict) else {}
        for key, default in DEFAULT_STATS_CARD_SETTINGS[group].items():
            result[group][key] = _clean_text(incoming.get(key), default, 18)
    return result


def get_stats_card_settings() -> dict[str, Any]:
    try:
        from services.card_template_storage_service import get_state
        state = get_state() or {}
        return normalise_stats_card_settings(state.get("stats_settings"))
    except ModuleNotFoundError:
        return normalise_stats_card_settings()
    except Exception:
        logger.exception("stats-card storage state unavailable")
        return normalise_stats_card_settings()


def _fmt(v: Any, decimals: int | None = None) -> str:
    if v in (None, ""):
        return "0"
    if isinstance(v, float):
        if decimals is not None:
            return f"{v:.{decimals}f}"
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return str(v)
    return str(v)


def _poly() -> list[tuple[int, int]]:
    return [(61, 35), (1475, 35), (1521, 86), (1521, 778), (1475, 829), (61, 829), (15, 778), (15, 86)]


def _inner_poly(inset=26) -> list[tuple[int, int]]:
    return [(61 + inset, 35 + inset), (1475 - inset, 35 + inset), (1521 - inset, 86 + inset),
            (1521 - inset, 778 - inset), (1475 - inset, 829 - inset), (61 + inset, 829 - inset),
            (15 + inset, 778 - inset), (15 + inset, 86 + inset)]


def _box_poly(x, y, w, h, cut=22):
    return [(x + cut, y), (x + w - cut, y), (x + w, y + cut), (x + w, y + h - cut),
            (x + w - cut, y + h), (x + cut, y + h), (x, y + h - cut), (x, y + cut)]


def _draw_gradient_bg(img: Image.Image):
    pix = img.load()
    for y in range(H):
        t = y / H
        r = int(8 * (1 - t) + 2 * t)
        g = int(16 * (1 - t) + 7 * t)
        b = int(29 * (1 - t) + 18 * t)
        for x in range(W):
            # Side colour glow like the HTML card.
            red_boost = max(0, 1 - abs(x - 80) / 260) * 18
            blue_boost = max(0, 1 - abs(x - (W - 80)) / 280) * 18
            pix[x, y] = (min(255, int(r + red_boost)), g, min(255, int(b + blue_boost)), 255)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _fit_font(draw, text, base_size, max_width, font_factory):
    size = int(base_size)
    while size > 24:
        font = font_factory(size)
        if _text_size(draw, text, font)[0] <= max_width:
            return font
        size -= 4
    return font_factory(size)


def _draw_centered(draw, box, text, font, fill):
    x0, y0, x1, y1 = box
    tw, th = _text_size(draw, text, font)
    draw.text((x0 + (x1 - x0 - tw) / 2, y0 + (y1 - y0 - th) / 2 - 2), text, font=font, fill=fill)


def _split_name(name: str) -> tuple[str, str]:
    parts = str(name or "Player").strip().split()
    if not parts:
        return "Player", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _draw_logo(base: Image.Image):
    if not os.path.isfile(LOGO_PATH):
        return
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
        logo.thumbnail((330, 235), Image.Resampling.LANCZOS)
        glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
        gx, gy = 1112, 95
        glow_draw = ImageDraw.Draw(glow, "RGBA")
        glow_draw.ellipse((gx - 15, gy - 5, gx + 330, gy + 235), fill=(255, 199, 103, 35))
        base.alpha_composite(glow.filter(ImageFilter.GaussianBlur(18)))
        x = 1145 + (330 - logo.width) // 2
        y = 88 + (235 - logo.height) // 2
        base.alpha_composite(logo, (x, y))
    except Exception:
        logger.exception("Failed to draw stats-card logo")


def _draw_base(settings: dict[str, Any]):
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is required to render CMU stats cards")
    img = Image.new("RGBA", (W, H), (2, 6, 13, 255))
    _draw_gradient_bg(img)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay, "RGBA")
    od.polygon(_poly(), outline=(255, 255, 255, 82), width=2)
    od.polygon(_inner_poly(), outline=(255, 255, 255, 46), width=2)
    od.line((638, 78, 898, 78), fill=GOLD, width=5)
    od.line((515, 792, 775, 792), fill=GOLD, width=5)
    # Light scan lines.
    for y in range(0, H, 5):
        od.line((0, y, W, y), fill=(255, 255, 255, 5), width=1)
    img.alpha_composite(overlay)
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_logo(img)
    return img, draw


def _draw_rating_box(draw, x, y, label, num, red=False, settings=None):
    settings = settings or get_stats_card_settings()
    poly = _box_poly(x, y, 150, 172, 22)
    draw.polygon(poly, fill=(8, 21, 39, 238))
    draw.line(poly + [poly[0]], fill=RED if red else BLUE, width=3)
    f_label = _fit_font(draw, str(label).upper(), settings["rating_label_font_size"], 134, text_font)
    f_num = display_font(settings["rating_num_font_size"])
    _draw_centered(draw, (x + 8, y + 20, x + 142, y + 72), str(label).upper(), f_label, RED if red else (210, 216, 227, 255))
    _draw_centered(draw, (x + 8, y + 76, x + 142, y + 158), str(num), f_num, RED if red else WHITE)


def _draw_header(draw, name, rating, skill_rating, skill_label, badge, meta_parts, settings):
    first, last = _split_name(name)
    first = first.upper(); last = last.upper()
    max_name_w = 800 - (28 * max(0, len(meta_parts) - 1))
    f_name = _fit_font(draw, f"{first} {last}".strip(), settings["name_font_size"], max_name_w, display_font)
    y = 88
    x = 78
    draw.text((x, y), first, font=f_name, fill=RED)
    fw, _ = _text_size(draw, first, f_name)
    if last:
        draw.text((x + fw + 22, y), last, font=f_name, fill=WHITE)
    _draw_rating_box(draw, 816, 82, settings["ovr_label"], rating, False, settings)
    _draw_rating_box(draw, 980, 82, skill_label, skill_rating, True, settings)

    # Role / meta row.
    row_y = 327
    draw.line((78, row_y - 22, 1110, row_y - 22), fill=(255, 255, 255, 28), width=1)
    draw.line((78, row_y + 54, 1110, row_y + 54), fill=(255, 199, 103, 82), width=1)
    badge_poly = [(78 + 18, row_y), (78 + 230, row_y), (78 + 212, row_y + 55), (78, row_y + 55)]
    draw.polygon(badge_poly, fill=RED_DARK)
    draw.line(badge_poly + [badge_poly[0]], fill=(255, 255, 255, 75), width=1)
    _draw_centered(draw, (78, row_y, 78 + 230, row_y + 55), badge.upper(), text_font(settings["badge_font_size"]), WHITE)
    cursor = 336
    f_meta = text_font(settings["meta_font_size"])
    for part in meta_parts:
        draw.line((cursor, row_y + 5, cursor, row_y + 49), fill=(255, 255, 255, 135), width=3)
        cursor += 28
        draw.text((cursor, row_y + 11), str(part).upper(), font=f_meta, fill=(216, 221, 230, 255))
        cursor += _text_size(draw, str(part).upper(), f_meta)[0] + 28


def _draw_stats_panel(draw, title, rows, second_cols, settings):
    x, y, w, h = 78, 430, 1380, 378
    poly = _box_poly(x, y, w, h, 42)
    draw.polygon(poly, fill=PANEL)
    draw.line(poly + [poly[0]], fill=(255, 255, 255, 46), width=1)
    inner = _box_poly(x + 12, y + 12, w - 24, h - 24, 34)
    draw.line(inner + [inner[0]], fill=(255, 255, 255, 34), width=1)
    draw.text((134, 465), title.upper(), font=text_font(settings["title_font_size"]), fill=GOLD)
    draw.line((134, 612, 1402, 612), fill=(255, 255, 255, 36), width=1)

    def row(items, row_y, cols, value_size):
        col_w = 1268 / cols
        for i, (label, value) in enumerate(items):
            cx0 = 134 + i * col_w
            cx1 = 134 + (i + 1) * col_w
            if i < cols - 1:
                draw.line((cx1, row_y + 10, cx1, row_y + 90), fill=(255, 255, 255, 70), width=2)
            _draw_centered(draw, (cx0, row_y, cx1, row_y + 34), label.upper(), text_font(settings["stat_label_font_size"]), MUTED)
            _draw_centered(draw, (cx0, row_y + 38, cx1, row_y + 100), value, display_font(value_size), WHITE)

    row(rows[0], 514, 5, settings["stat_value_font_size"])
    row(rows[1], 650, second_cols, settings["stat_value_second_font_size"])


def generate_batting_stats_card(name, rating, bat_rating, stats, bat_hand="Right", **_) -> bytes | None:
    settings = get_stats_card_settings()
    try:
        img, draw = _draw_base(settings)
        meta = [f"{bat_hand} {settings['bat_meta_suffix']}"]
        _draw_header(draw, name, rating, bat_rating, settings["bat_rating_label"], settings["badge_batting"], meta, settings)
        labels = settings["batting_labels"]
        row1 = [(labels["inns"], _fmt(stats.get("bat_inns", 0))),
                (labels["runs"], _fmt(stats.get("runs", 0))),
                (labels["hs"], str(stats.get("hs_str", "-"))),
                (labels["avg"], _fmt(stats.get("bat_avg", 0), 2) if stats.get("bat_avg") else "0"),
                (labels["sr"], _fmt(stats.get("bat_sr", 0), 2) if stats.get("bat_sr") else "0")]
        row2 = [(labels["hundreds"], _fmt(stats.get("hundreds", 0))),
                (labels["fifties"], _fmt(stats.get("fifties", 0))),
                (labels["fours"], _fmt(stats.get("fours", 0))),
                (labels["sixes"], _fmt(stats.get("sixes", 0))),
                (labels["ducks"], _fmt(stats.get("ducks", 0)))]
        _draw_stats_panel(draw, settings["title_batting"], (row1, row2), 5, settings)
        buf = io.BytesIO(); img.convert("RGB").save(buf, format="PNG", quality=95)
        return buf.getvalue()
    except Exception:
        logger.exception("CMU batting stats card generation failed")
        return None


def generate_bowling_stats_card(name, rating, bowl_rating, stats, bat_hand="Right", bowl_style="Medium Pacer", **_) -> bytes | None:
    settings = get_stats_card_settings()
    try:
        img, draw = _draw_base(settings)
        meta = [f"{bat_hand} {settings['bat_meta_suffix']}", bowl_style]
        _draw_header(draw, name, rating, bowl_rating, settings["bowl_rating_label"], settings["badge_bowling"], meta, settings)
        labels = settings["bowling_labels"]
        row1 = [(labels["inns"], _fmt(stats.get("bowl_inns", 0))),
                (labels["wickets"], _fmt(stats.get("wickets_taken", 0))),
                (labels["bbf"], str(stats.get("bbf_str", "-"))),
                (labels["avg"], _fmt(stats.get("bowl_avg", 0), 2) if stats.get("bowl_avg") else "0"),
                (labels["econ"], _fmt(stats.get("econ", 0), 2) if stats.get("econ") else "0")]
        row2 = [(labels["sr"], _fmt(stats.get("bowl_sr", 0), 2) if stats.get("bowl_sr") else "0"),
                (labels["hat_tricks"], _fmt(stats.get("hat_tricks", 0))),
                (labels["five_fers"], _fmt(stats.get("five_fers", 0))),
                (labels["three_fers"], _fmt(stats.get("three_fers", 0)))]
        _draw_stats_panel(draw, settings["title_bowling"], (row1, row2), 4, settings)
        buf = io.BytesIO(); img.convert("RGB").save(buf, format="PNG", quality=95)
        return buf.getvalue()
    except Exception:
        logger.exception("CMU bowling stats card generation failed")
        return None
