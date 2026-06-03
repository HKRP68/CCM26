"""Website-managed cricket card template assets and layout controls.

The settings mirror ``cricket_card_generator_website_v7-1.html``: an admin can
upload the global blank template and optional font, then tune cutout, flag, and
text placement from the website without a redeploy. The legacy image-map parser
is retained for compatibility with previously stored configuration.
"""

import os
import re
import logging
import json
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


# Storage config (mirrors player_image_service)
TEMPLATES_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "card_templates")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
ALLOWED_FONT_EXT = {"ttf", "otf"}
MAX_BYTES = 5 * 1024 * 1024     # 5 MB
MIN_DIM = 200                    # min width/height in pixels

# Defaults mirror cricket_card_generator_website_v7-1.html so the website
# produces the same layout while allowing admins to tune each value.
DEFAULT_TEMPLATE_SETTINGS = {
    "player_x": 675, "player_y": 25, "player_w": 780, "player_h": 1000,
    "player_scale": 100, "player_opacity": 100, "trim_transparent": True,
    "protect_bottom_box": True, "flag_scale": 100, "flag_y_offset": 0,
    "name_x": 52, "name_y": 195, "name_font_size": 140, "name_letter_gap": 0,
    "name_max_width": 650, "name_line_gap": 150,
    "ovr_x": 1366, "ovr_y": 166, "ovr_font_size": 128, "ovr_letter_gap": 0,
    "ovr_max_width": 240,
    "bat_x": 173, "bat_y": 686, "bat_font_size": 92, "bat_letter_gap": 0,
    "bat_max_width": 150,
    "bowl_x": 531, "bowl_y": 686, "bowl_font_size": 92, "bowl_letter_gap": 0,
    "bowl_max_width": 150,
    "cat_x": 58, "cat_y": 590, "cat_font_size": 42, "cat_letter_gap": 22,
    "cat_max_width": 650,
    "country_x": 405, "country_y": 925, "country_font_size": 58,
    "country_letter_gap": 0, "country_max_width": 280,
    "bat_style_x": 1110, "bat_style_y": 895,
    "bat_style_font_size": 38, "bat_style_letter_gap": 0, "bat_style_max_width": 335,
    "bowl_style_x": 1110, "bowl_style_y": 988,
    "bowl_style_font_size": 38, "bowl_style_letter_gap": 0, "bowl_style_max_width": 335,
}
SETTING_LIMITS = {
    "player_x": (-1536, 3072), "player_y": (-1024, 2048),
    "player_w": (1, 3072), "player_h": (1, 2048),
    "player_scale": (1, 400), "player_opacity": (1, 100),
    "flag_scale": (10, 300), "flag_y_offset": (-1024, 1024),
    "name_x": (-1536, 3072), "name_y": (-1024, 2048),
    "name_font_size": (8, 200), "name_letter_gap": (0, 200),
    "name_max_width": (1, 1536), "name_line_gap": (1, 1024),
    "ovr_x": (-1536, 3072), "ovr_y": (-1024, 2048),
    "ovr_font_size": (8, 200), "ovr_letter_gap": (0, 200), "ovr_max_width": (1, 1536),
    "bat_x": (-1536, 3072), "bat_y": (-1024, 2048),
    "bat_font_size": (8, 200), "bat_letter_gap": (0, 200), "bat_max_width": (1, 1536),
    "bowl_x": (-1536, 3072), "bowl_y": (-1024, 2048),
    "bowl_font_size": (8, 200), "bowl_letter_gap": (0, 200), "bowl_max_width": (1, 1536),
    "cat_x": (-1536, 3072), "cat_y": (-1024, 2048),
    "cat_font_size": (8, 200), "cat_letter_gap": (0, 200), "cat_max_width": (1, 1536),
    "country_x": (-1536, 3072), "country_y": (-1024, 2048),
    "country_font_size": (8, 200), "country_letter_gap": (0, 200), "country_max_width": (1, 1536),
    "bat_style_x": (-1536, 3072), "bat_style_y": (-1024, 2048),
    "bat_style_font_size": (8, 200), "bat_style_letter_gap": (0, 200), "bat_style_max_width": (1, 1536),
    "bowl_style_x": (-1536, 3072), "bowl_style_y": (-1024, 2048),
    "bowl_style_font_size": (8, 200), "bowl_style_letter_gap": (0, 200), "bowl_style_max_width": (1, 1536),
}


# ── Field aliases ───────────────────────────────────────────────────────────
# Normalised image-map label (alt/title) → logical field key. The renderer maps
# each key to a player attribute and a display string.
FIELD_ALIASES = {
    "player name": "name",
    "name": "name",
    "overall": "rating",
    "ovr": "rating",
    "rating": "rating",
    "country name": "country",
    "country": "country",
    "batting style": "bat_style",
    "bat style": "bat_style",
    "bowling style": "bowl_style",
    "bowl style": "bowl_style",
    "batting power": "bat_rating",
    "bat power": "bat_rating",
    "batting": "bat_rating",
    "bowling specs": "bowl_rating",
    "bowling spec": "bowl_rating",
    "bowling": "bowl_rating",
    "player photo": "__photo__",
    "player image": "__photo__",
    "portrait": "__photo__",
    "photo": "__photo__",
}


def _ensure_dir():
    if not os.path.exists(TEMPLATES_ROOT):
        try:
            os.makedirs(TEMPLATES_ROOT, exist_ok=True)
        except Exception:
            logger.exception("Failed to create %s", TEMPLATES_ROOT)


def _ext_from_filename(filename):
    return (filename.rsplit(".", 1)[-1] or "").lower() if "." in (filename or "") else ""


def save_template_image(file_bytes, original_filename, variant="base"):
    variant = normalise_template_variant(variant)
    """Validate and store an uploaded template background image.

    Returns (success, message, path). Validates extension, size and that it's a
    real image of at least MIN_DIM x MIN_DIM. Overwrites any existing template
    (we keep a single global template). Older-extension copies are removed so
    only one template.* file remains.
    """
    ext = _ext_from_filename(original_filename)
    if ext not in ALLOWED_EXT:
        return False, f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXT))}", None
    if not file_bytes:
        return False, "Please choose a template image to upload.", None
    if len(file_bytes) > MAX_BYTES:
        return False, (f"File too large ({len(file_bytes)/1024/1024:.1f} MB). "
                       f"Max is {MAX_BYTES/1024/1024:.0f} MB."), None

    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(file_bytes))
        img.verify()
        img = Image.open(_io.BytesIO(file_bytes))
        w, h = img.size
        if w < MIN_DIM or h < MIN_DIM:
            return False, f"Image too small ({w}×{h}). Minimum is {MIN_DIM}×{MIN_DIM}.", None
    except Exception as e:
        return False, f"Not a valid image file: {e}", None

    _ensure_dir()
    # Drop the previous file for this rarity so each tab has one blank template.
    stem = "template" if variant == "base" else f"template_{variant}"
    for old in ALLOWED_EXT:
        p = os.path.join(TEMPLATES_ROOT, f"{stem}.{old}")
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass

    path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
    try:
        with open(path, "wb") as f:
            f.write(file_bytes)
    except OSError as e:
        logger.exception("save_template_image disk write failed")
        return False, f"Disk write failed: {e}", None

    return True, "Template image saved.", path


def remove_template_image(variant="base"):
    """Remove the uploaded blank-card image for exactly one rarity variant."""
    variant = normalise_template_variant(variant)
    stem = "template" if variant == "base" else f"template_{variant}"
    removed = False
    for ext in ALLOWED_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError:
                logger.exception("Failed to remove template image %s", path)
    return removed


def save_template_font(file_bytes, original_filename):
    """Validate and store the optional global card font (TTF or OTF)."""
    ext = _ext_from_filename(original_filename)
    if ext not in ALLOWED_FONT_EXT:
        return False, "Unsupported font type. Allowed: otf, ttf", None
    if not file_bytes:
        return False, "Please choose a font file to upload.", None
    if len(file_bytes) > MAX_BYTES:
        return False, f"Font too large. Max is {MAX_BYTES / 1024 / 1024:.0f} MB.", None
    _ensure_dir()
    for old in ALLOWED_FONT_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"font.{old}")
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    path = os.path.join(TEMPLATES_ROOT, f"font.{ext}")
    try:
        with open(path, "wb") as handle:
            handle.write(file_bytes)
        from PIL import ImageFont
        ImageFont.truetype(path, 24)
    except Exception as exc:
        try:
            os.remove(path)
        except OSError:
            pass
        return False, f"Not a valid font file: {exc}", None
    return True, "Font file saved.", path


def remove_template_font():
    """Remove the optional shared font file and restore the renderer fallback."""
    removed = False
    for ext in ALLOWED_FONT_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"font.{ext}")
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError:
                logger.exception("Failed to remove template font %s", path)
    return removed


def normalise_template_settings(raw=None):
    """Return safe numeric/boolean layout settings merged with HTML defaults."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    # Preserve the former shared style size when loading settings saved before
    # batting and bowling style controls became independently configurable.
    if "style_font_size" in raw or "style_max_width" in raw:
        raw = dict(raw)
        if "style_font_size" in raw:
            raw.setdefault("bat_style_font_size", raw["style_font_size"])
            raw.setdefault("bowl_style_font_size", raw["style_font_size"])
        if "style_max_width" in raw:
            raw.setdefault("bat_style_max_width", raw["style_max_width"])
            raw.setdefault("bowl_style_max_width", raw["style_max_width"])
    result = dict(DEFAULT_TEMPLATE_SETTINGS)
    for key, (minimum, maximum) in SETTING_LIMITS.items():
        try:
            result[key] = max(minimum, min(maximum, int(float(raw.get(key, result[key])))))
        except (TypeError, ValueError):
            pass
    for key in ("trim_transparent", "protect_bottom_box"):
        if key in raw:
            result[key] = bool(raw[key])
    return result


class _AreaParser(HTMLParser):
    """Collect <area> tag attributes from an image-map snippet."""

    def __init__(self):
        super().__init__()
        self.areas = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "area":
            self.areas.append(dict(attrs))


def _coords_to_bbox(coords_str):
    """Flatten a coords string ("x1,y1,x2,y2,...") to a bounding box.

    Robust to rect/poly/circle and messy lists: takes the min/max of all x and
    y values. Returns (x0, y0, x1, y1) or None when unparseable.
    """
    nums = [int(round(float(n))) for n in re.findall(r"-?\d+(?:\.\d+)?", coords_str or "")]
    if len(nums) < 4:
        return None
    xs = nums[0::2]
    ys = nums[1::2]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def parse_area_code(html):
    """Parse image-map <area> HTML into a list of placement regions.

    Each region is a dict: {label, field, bbox=(x0,y0,x1,y1)}. Areas whose
    label doesn't match a known field (FIELD_ALIASES) are skipped. Coordinates
    are kept in the template's native pixel space.
    """
    if not html or not html.strip():
        return []
    parser = _AreaParser()
    try:
        parser.feed(html)
    except Exception:
        logger.exception("parse_area_code: failed to parse image-map HTML")
        return []

    regions = []
    for attrs in parser.areas:
        label_raw = (attrs.get("alt") or attrs.get("title") or "").strip()
        field = FIELD_ALIASES.get(label_raw.lower())
        if not field:
            continue
        bbox = _coords_to_bbox(attrs.get("coords", ""))
        if not bbox:
            continue
        regions.append({"label": label_raw, "field": field, "bbox": bbox})
    return regions


def template_asset_path(stored):
    """Resolve a stored template/font path relative to the project root."""
    path = (stored or "").strip()
    if not path:
        return None
    if not os.path.isabs(path):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, path)
    return path if os.path.isfile(path) else None


CARD_TEMPLATE_VARIANTS = ("base", "star", "legend")


def normalise_template_variant(variant):
    """Return one of the three website-managed blank-card template tabs."""
    value = str(variant or "base").strip().lower()
    return value if value in CARD_TEMPLATE_VARIANTS else "base"


def player_template_variant(player):
    """Select a blank template from the player's version label."""
    version = str(getattr(player, "version", "") or "").lower()
    if "legend" in version:
        return "legend"
    if "star" in version:
        return "star"
    return "base"


def template_image_path(session=None, variant="base"):
    """Return the uploaded image for exactly one rarity template, if present."""
    variant = normalise_template_variant(variant)
    stem = "template" if variant == "base" else f"template_{variant}"
    for ext in ALLOWED_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
        if os.path.isfile(path):
            return path
    if variant == "base":
        # Legacy fallback for deployments that still have the old DB path.
        from services.config_service import get_config
        return template_asset_path(get_config(session).get("card_template_image_path"))
    return None


def list_template_variants(session=None):
    """Describe each selectable rarity tab and whether it has its own upload."""
    result = {}
    for variant in CARD_TEMPLATE_VARIANTS:
        result[variant] = {"uploaded": bool(template_image_path(session, variant))}
    return result


def get_template_config(session=None, variant="base"):
    """Return the active template-card configuration without hitting DB first."""
    from services.config_service import get_config
    try:
        from services.card_template_storage_service import get_state
        state = get_state() or {}
    except Exception:
        logger.exception("card-template storage state unavailable")
        state = {}
    cfg = {} if state else get_config(session)
    style = state.get("card_style") or cfg.get("card_style") or "tier"
    show_portrait = state.get("show_portrait", cfg.get("card_template_show_portrait", True))
    settings = state.get("settings", cfg.get("card_template_settings"))
    font_path = None
    for ext in ALLOWED_FONT_EXT:
        candidate = os.path.join(TEMPLATES_ROOT, f"font.{ext}")
        if os.path.isfile(candidate):
            font_path = candidate
            break
    if not font_path:
        font_path = template_asset_path(cfg.get("card_template_font_path"))
    return {
        "style": str(style or "tier").lower(),
        "variant": normalise_template_variant(variant),
        "image_path": template_image_path(session, variant),
        "area_code": cfg.get("card_template_area_code") or "",
        "regions": parse_area_code(cfg.get("card_template_area_code") or ""),
        "show_portrait": bool(show_portrait),
        "font_path": font_path,
        "settings": normalise_template_settings(settings),
    }
