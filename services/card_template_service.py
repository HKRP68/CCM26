"""Card template service — admin-uploaded card background + image-map layout.

The admin can upload a card *template* background image and paste the HTML
image-map ``<area>`` code that describes where each player field is drawn. This
module stores the uploaded image, parses the area code into clean bounding
boxes, and resolves which player attribute each region maps to.

Storage layout:
  data/card_templates/template.<ext>   (single global template image)

The image-map coordinates are interpreted in the template image's native pixel
space, so the renderer draws at the template's real resolution (no scaling).
"""

import os
import re
import logging
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


# Storage config (mirrors player_image_service)
TEMPLATES_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "data", "card_templates")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
MAX_BYTES = 5 * 1024 * 1024     # 5 MB
MIN_DIM = 200                    # min width/height in pixels


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


def save_template_image(file_bytes, original_filename):
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
    # Drop any previous template.* so only one remains.
    for old in ALLOWED_EXT:
        p = os.path.join(TEMPLATES_ROOT, f"template.{old}")
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass

    path = os.path.join(TEMPLATES_ROOT, f"template.{ext}")
    try:
        with open(path, "wb") as f:
            f.write(file_bytes)
    except OSError as e:
        logger.exception("save_template_image disk write failed")
        return False, f"Disk write failed: {e}", None

    return True, "Template image saved.", path


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


def template_image_path(session=None):
    """Return the configured template image path if it exists on disk, else None."""
    from services.config_service import get_config
    cfg = get_config(session)
    path = (cfg.get("card_template_image_path") or "").strip()
    if not path:
        return None
    if not os.path.isabs(path):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, path)
    return path if os.path.isfile(path) else None


def get_template_config(session=None):
    """Return the active template-card configuration as a dict.

    Keys: style, image_path (absolute or None), regions (parsed list),
    show_portrait (bool), area_code (raw string).
    """
    from services.config_service import get_config
    cfg = get_config(session)
    return {
        "style": (cfg.get("card_style") or "tier"),
        "image_path": template_image_path(session),
        "area_code": cfg.get("card_template_area_code") or "",
        "regions": parse_area_code(cfg.get("card_template_area_code") or ""),
        "show_portrait": bool(cfg.get("card_template_show_portrait", True)),
    }
