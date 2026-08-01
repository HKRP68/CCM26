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
    # Whether to composite the player cutout onto this variant. Per-variant so a
    # Career face — whose artwork already contains the face — can turn it off
    # while the rarity cards keep drawing one.
    "show_portrait": True,
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

# Layout settings that are checkboxes rather than numbers.
BOOL_SETTING_KEYS = ("trim_transparent", "protect_bottom_box", "show_portrait")

# Text colours, per variant. The defaults reproduce the hardcoded v7.1 palette
# exactly, so Base/Star/Legend are untouched; a Career face template is dark
# artwork, so its own defaults (below) are light instead of dark green.
DEFAULT_COLOR_SETTINGS = {
    "name_color": "#005632",
    "cat_color": "#5cae58",
    "ovr_color": "#ed2543",
    "bat_color": "#ed2543",
    "bowl_color": "#ed2543",
    "country_color": "#005632",
    "bat_style_color": "#005632",
    "bowl_style_color": "#005632",
    "line_color": "#7cb291",
}
# Career faces ship on dark artwork, so the palette is sampled straight off the
# reference card design: near-white names and country, teal category line, and
# cyan for the OVR and the two rating numbers. Admins can still recolour any of
# it per face on the website.
CAREER_COLOR_SETTINGS = {
    "name_color": "#f5f6f7",
    "cat_color": "#00aeb5",
    "ovr_color": "#00e7fe",
    "bat_color": "#00d9f5",
    "bowl_color": "#00d9f5",
    "country_color": "#e7ebf0",
    "bat_style_color": "#e8eef5",
    "bowl_style_color": "#e8eef5",
    "line_color": "#1c6b7d",
}
COLOR_SETTING_KEYS = tuple(DEFAULT_COLOR_SETTINGS)

# Starting layout nudges for a Career face, applied on top of the base layout.
# The career artwork puts its Batting Power / Bowling Specs boxes where the base
# card puts the flag, so the flag and country drop below them. These are only
# defaults — every face is tuned independently on the website.
CAREER_LAYOUT_SETTINGS = {
    "flag_y_offset": 18,
    "country_y": 928,
    "bat_style_y": 880,
    "bowl_style_y": 958,
}

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def normalise_color(value, fallback="#000000"):
    """Return a ``#rrggbb`` string, falling back when the input isn't a hex colour."""
    text = str(value or "").strip()
    if not text.startswith("#"):
        text = f"#{text}"
    if not _HEX_RE.match(text):
        return fallback
    if len(text) == 4:  # expand #abc → #aabbcc
        text = "#" + "".join(ch * 2 for ch in text[1:])
    return text.lower()


def color_to_rgba(value, fallback="#000000"):
    """Convert a settings hex colour to the RGBA tuple Pillow wants."""
    text = normalise_color(value, fallback)
    return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16), 255)


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


def _remember(path, data=None):
    """Keep an uploaded file in the database so a redeploy can restore it.

    Best-effort: the file is already safely on disk, so a storage hiccup must
    never fail the upload the admin just made.
    """
    global _RESTORE_ATTEMPTED
    # A fresh upload means the once-per-process restore guard should let a later
    # miss look again, rather than reporting "nothing there" from a stale check.
    _RESTORE_ATTEMPTED = False
    try:
        from services.asset_store import put
        put(path, data)
    except Exception:
        logger.exception("could not persist %s to the asset store", path)


def _forget(path):
    """Drop a deleted file from the store, so a restore can't resurrect it."""
    try:
        from services.asset_store import drop
        drop(path)
    except Exception:
        logger.exception("could not drop %s from the asset store", path)


# A miss is the common case (Star/Legend often have no template of their own),
# and card rendering is latency-sensitive, so the database is consulted at most
# once per process rather than on every lookup. An upload resets this, so a file
# added after start-up is still picked up straight away.
_RESTORE_ATTEMPTED = False


def _restore_root(force=False):
    """Refill this directory from the database when the disk copy is gone.

    Hosts wipe ``data/`` on deploy, so a container can come up with the tables
    listing templates that are not on disk. Re-materialising here means the
    first card render heals it rather than silently falling back to another
    variant's design.
    """
    global _RESTORE_ATTEMPTED
    if _RESTORE_ATTEMPTED and not force:
        return 0
    _RESTORE_ATTEMPTED = True
    try:
        from services.asset_store import ensure_dir
        return ensure_dir("data/card_templates")
    except Exception:
        logger.exception("could not restore card templates from the asset store")
        return 0


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

    _remember(path, file_bytes)
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
                _forget(path)
                removed = True
            except OSError:
                logger.exception("Failed to remove template image %s", path)
    return removed


def font_stem(variant=None):
    """Filename stem for a font: the shared one, or one variant's own.

    ``None`` (or "shared") is the font every card falls back to; a variant key
    gives that card its own typeface, so a Career face can match its artwork
    without changing how the rarity cards look.
    """
    if not variant or variant == "shared":
        return "font"
    return f"font_{normalise_template_variant(variant)}"


def save_template_font(file_bytes, original_filename, variant=None):
    """Validate and store a card font (TTF or OTF).

    Without ``variant`` this is the shared font every card falls back to; with
    one it is that card's own font.
    """
    ext = _ext_from_filename(original_filename)
    if ext not in ALLOWED_FONT_EXT:
        return False, "Unsupported font type. Allowed: otf, ttf", None
    if not file_bytes:
        return False, "Please choose a font file to upload.", None
    if len(file_bytes) > MAX_BYTES:
        return False, f"Font too large. Max is {MAX_BYTES / 1024 / 1024:.0f} MB.", None
    _ensure_dir()
    stem = font_stem(variant)
    for old in ALLOWED_FONT_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"{stem}.{old}")
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
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
    _remember(path, file_bytes)
    return True, "Font file saved.", path


def remove_template_font(variant=None):
    """Remove a font file and fall back to the next one down the chain."""
    stem = font_stem(variant)
    removed = False
    for ext in ALLOWED_FONT_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
        if os.path.isfile(path):
            try:
                os.remove(path)
                _forget(path)
                removed = True
            except OSError:
                logger.exception("Failed to remove template font %s", path)
    return removed


def font_file_path(variant=None):
    """This variant's OWN uploaded font file, or ``None``. No fallback."""
    stem = font_stem(variant)
    for ext in ALLOWED_FONT_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
        if os.path.isfile(path):
            return path
    # Nothing on disk: this container may have been rebuilt since the upload,
    # so refill from the database and look once more.
    if _restore_root():
        for ext in ALLOWED_FONT_EXT:
            path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
            if os.path.isfile(path):
                return path
    return None


def resolve_font_path(variant=None, session=None, cfg=None):
    """The font to render this variant with.

    Order: the variant's own upload, then the shared upload, then the legacy
    ``GameConfig`` path. ``None`` means the renderer uses its built-in
    condensed fallback.
    """
    own = font_file_path(variant) if variant else None
    if own:
        return own
    shared = font_file_path(None)
    if shared:
        return shared
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config(session)
    return template_asset_path(cfg.get("card_template_font_path"))


def list_template_fonts(session=None):
    """``{variant: True}`` for every variant with a font of its own."""
    return {variant: bool(font_file_path(variant))
            for variant in all_template_variants(session)}


def normalise_template_settings(raw=None, defaults=None):
    """Return safe numeric/boolean layout settings merged with HTML defaults.

    ``defaults`` lets one variant inherit another's layout (Star/Legend falling
    back to Base) instead of the built-in v7.1 numbers; any key missing from
    ``raw`` is taken from there.
    """
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
    result.update(DEFAULT_COLOR_SETTINGS)
    if isinstance(defaults, dict):
        for key in result:
            if key in defaults:
                result[key] = defaults[key]
    for key, (minimum, maximum) in SETTING_LIMITS.items():
        try:
            result[key] = max(minimum, min(maximum, int(float(raw.get(key, result[key])))))
        except (TypeError, ValueError):
            pass
    for key in BOOL_SETTING_KEYS:
        if key in raw:
            result[key] = bool(raw[key])
    for key in COLOR_SETTING_KEYS:
        result[key] = normalise_color(raw.get(key, result[key]),
                                      DEFAULT_COLOR_SETTINGS[key])
    return result


def default_show_portrait(variant):
    """Whether this variant should composite the player cutout by default.

    Career face templates already contain the player's face in the artwork, so
    drawing a cutout on top would cover it. Every other variant keeps the
    historical behaviour of drawing one.
    """
    return not is_career_variant(variant)


def normalise_variant_settings(raw=None, shared=None, variants=None,
                               shared_show_portrait=True):
    """Return ``{variant: settings}`` covering every template tab.

    Each variant keeps its own independent layout. One with no saved layout of
    its own inherits its fallback (Star/Legend → the pre-split ``shared``
    layout; Career face N → face 1's layout), so upgrading a deployment that
    only ever had one shared layout leaves all three rarity cards rendering
    exactly as before, and adding a new face starts from a tuned layout rather
    than the raw v7.1 defaults.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    shared_settings = normalise_template_settings(shared)
    shared_settings["show_portrait"] = bool(shared_show_portrait)
    if variants is None:
        variants = all_template_variants()

    result = {}
    # Rarities first, then faces in order, so a face can inherit from face 1
    # (and face 1 from base) within this single pass. Faces are sorted by slot
    # so face 1 is always resolved before the faces that inherit from it.
    ordered = list(CARD_TEMPLATE_VARIANTS)
    faces = sorted({v for v in variants if is_career_variant(v)},
                   key=career_variant_slot)
    ordered += faces + [v for v in variants
                        if v not in ordered and v not in faces]
    for variant in ordered:
        fallback = _variant_fallback(variant)
        defaults = dict(result.get(fallback) or shared_settings)
        if is_career_variant(variant):
            # A face with no layout of its own must not inherit "draw the
            # cutout" from the base card — its artwork already has the face on
            # it — nor the base card's dark text, which would be unreadable on
            # the dark career artwork. Face 2+ still inherits face 1's own
            # tuning, which is the point of the fallback chain.
            defaults["show_portrait"] = False
            if not is_career_variant(fallback):
                defaults.update(CAREER_COLOR_SETTINGS)
                defaults.update(CAREER_LAYOUT_SETTINGS)
        entry = raw.get(variant)
        result[variant] = (normalise_template_settings(entry, defaults=defaults)
                           if isinstance(entry, (dict, str)) else dict(defaults))
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

# Career Player faces are template variants too, but unlike the three rarities
# they are created on the website, so the set is dynamic: one variant per active
# CareerFace row, keyed "career_<slot>".
CAREER_VARIANT_PREFIX = "career_"
_CAREER_VARIANT_RE = re.compile(r"^career_(\d+)$")


def is_career_variant(variant):
    """True for a Career Player face variant such as ``career_3``."""
    return bool(_CAREER_VARIANT_RE.match(str(variant or "").strip().lower()))


def career_variant_slot(variant):
    """The face slot number behind a career variant key, or ``None``."""
    match = _CAREER_VARIANT_RE.match(str(variant or "").strip().lower())
    return int(match.group(1)) if match else None


def career_variants(session=None, active_only=True):
    """Career Player face variants, in display order.

    Reads the ``career_faces`` table the website manages. Returns an empty list
    (never raises) when the table does not exist yet, so a deployment that has
    not migrated still renders the three rarity cards normally.

    ``active_only`` picks the two different questions this answers. The default
    is "which faces may be offered and rendered"; pass ``False`` for "which
    faces own artwork", which is what the Telegram storage mirror needs —
    deactivating a face must not stop its blank card being backed up, or the
    next redeploy loses artwork the ``CareerFace`` row still points at.
    """
    own = False
    try:
        from models import CareerFace
        if session is None:
            from database import get_session
            session = get_session()
            own = True
        query = session.query(CareerFace)
        if active_only:
            query = query.filter(CareerFace.is_active.is_(True))
        rows = query.order_by(CareerFace.sort_order, CareerFace.slot).all()
        return [f"{CAREER_VARIANT_PREFIX}{row.slot}" for row in rows]
    except Exception:
        logger.debug("career face variants unavailable", exc_info=True)
        return []
    finally:
        if own and session is not None:
            session.close()


def all_template_variants(session=None):
    """Every editable card template tab: the three rarities, then each face."""
    return list(CARD_TEMPLATE_VARIANTS) + career_variants(session)


def normalise_template_variant(variant, session=None):
    """Return a known template variant key, falling back to ``base``.

    Accepts the three rarity tabs plus any ``career_<n>`` face. Career keys are
    accepted on shape alone rather than by re-reading ``career_faces`` on every
    render — the callers that need to know whether a face really exists check
    for its uploaded template instead.
    """
    value = str(variant or "base").strip().lower()
    if value in CARD_TEMPLATE_VARIANTS or is_career_variant(value):
        return value
    return "base"


def player_template_variant(player):
    """Select a blank template for this player.

    A Career Player always renders on its own chosen face — that artwork already
    contains the player's portrait — so it is checked before the rarity labels.
    """
    if getattr(player, "is_career", False):
        face = str(getattr(player, "career_face", "") or "").strip().lower()
        if is_career_variant(face):
            return face
    version = str(getattr(player, "version", "") or "").lower()
    if "legend" in version:
        return "legend"
    if "star" in version:
        return "star"
    return "base"


def template_stem(variant):
    """Filename stem holding this variant's blank card under TEMPLATES_ROOT."""
    variant = normalise_template_variant(variant)
    return "template" if variant == "base" else f"template_{variant}"


def _variant_fallback(variant):
    """The variant to borrow a template/layout from when this one has none.

    A Career face falls back to the first face (so adding Face 5 starts from the
    layout already tuned on Face 1) and only then to the base card. Star/Legend
    fall straight back to base, exactly as before.
    """
    if is_career_variant(variant) and career_variant_slot(variant) != 1:
        return f"{CAREER_VARIANT_PREFIX}1"
    return "base" if variant != "base" else None


def template_image_path(session=None, variant="base", fallback_to_base=True):
    """Return the template image to render for one variant, if present.

    By default a variant without its own upload falls back (Star/Legend → base,
    Career face N → face 1 → base) so a single committed blank covers everything
    at render time. Callers that need to know whether *this* variant has its own
    asset (upload state, not render source) pass ``fallback_to_base=False``.
    """
    variant = normalise_template_variant(variant)
    stem = template_stem(variant)
    for ext in ALLOWED_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
        if os.path.isfile(path):
            return path
    # Nothing on disk for this variant: the container may have been rebuilt
    # since the upload, so refill from the database and look once more.
    if _restore_root():
        for ext in ALLOWED_EXT:
            path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
            if os.path.isfile(path):
                return path
    if variant == "base":
        # Legacy fallback for deployments that still have the old DB path.
        from services.config_service import get_config
        return template_asset_path(get_config(session).get("card_template_image_path"))
    if not fallback_to_base:
        return None
    return template_image_path(session, variant=_variant_fallback(variant))


def template_file_path(variant="base"):
    """Path to a committed/admin-uploaded template *file* for this variant.

    Checks only the on-disk template files in ``TEMPLATES_ROOT`` (following the
    same fallback chain as ``template_image_path``), excluding the legacy
    ``GameConfig`` image path. Used to decide whether to auto-activate the
    template style: an actual template file is a clear "use this" signal,
    whereas a legacy DB image path is not — for the latter we keep respecting
    the saved ``card_style``.
    """
    variant = normalise_template_variant(variant)
    stem = template_stem(variant)
    for ext in ALLOWED_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
        if os.path.isfile(path):
            return path
    if _restore_root():
        for ext in ALLOWED_EXT:
            path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
            if os.path.isfile(path):
                return path
    fallback = _variant_fallback(variant)
    return template_file_path(fallback) if fallback else None


def list_template_variants(session=None):
    """Describe each selectable template tab and whether it has its own upload."""
    result = {}
    for variant in all_template_variants(session):
        # Report each variant's own upload state — not the render-time
        # fallback — so the admin page shows accurate per-variant status.
        result[variant] = {
            "uploaded": bool(template_image_path(session, variant, fallback_to_base=False))
        }
    return result


def get_variant_settings(session=None, state=None, variants=None):
    """Return every variant's saved layout as ``{variant: settings}``.

    Reads the runtime state first (per-variant layouts, with the legacy shared
    layout as the inheritance base) and only falls back to the legacy
    ``GameConfig`` column when no state file exists at all.
    """
    if state is None:
        try:
            from services.card_template_storage_service import get_state
            state = get_state() or {}
        except Exception:
            logger.exception("card-template storage state unavailable")
            state = {}
    if state:
        shared = state.get("settings")
        shared_portrait = state.get("show_portrait", True)
    else:
        from services.config_service import get_config
        cfg = get_config(session)
        shared = cfg.get("card_template_settings")
        shared_portrait = cfg.get("card_template_show_portrait", True)
    if variants is None:
        variants = all_template_variants(session)
    return normalise_variant_settings(state.get("variant_settings"), shared,
                                      variants=variants,
                                      shared_show_portrait=shared_portrait)


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
    image_path = template_image_path(session, variant)
    # Style resolution priority:
    #  1. An admin-saved choice (runtime/pinned state) always wins.
    #  2. A committed/admin-uploaded template *file* auto-activates the template
    #     style — so committing a template needs no admin save and no baked
    #     state.json (which would shadow pinned state on storage-backed deploys).
    #     The legacy GameConfig image path does NOT auto-activate; there we respect
    #     the saved card_style (which may be an explicit "tier").
    #  3. Otherwise fall back to legacy config or the procedural tier card.
    if state.get("card_style"):
        style = state["card_style"]
    elif template_file_path(variant):
        style = "template"
    else:
        style = cfg.get("card_style") or "tier"
    variant = normalise_template_variant(variant)
    # Every variant carries its own show_portrait, seeded from the old global
    # flag — so Base/Star/Legend behave exactly as before, while a Career face
    # defaults to off because its artwork already contains the player's face.
    all_settings = get_variant_settings(session, state=state,
                                        variants=all_template_variants(session) + [variant])
    settings = all_settings.get(variant) or all_settings["base"]
    show_portrait = settings.get("show_portrait", True)
    # This card's own font if one was uploaded for it, else the shared font.
    # ``cfg`` is passed through as-is (it is {} whenever runtime state exists),
    # so the legacy GameConfig path is consulted exactly when it was before.
    font_path = resolve_font_path(variant, session=session, cfg=cfg)
    return {
        "style": str(style or "tier").lower(),
        "variant": variant,
        "image_path": image_path,
        "area_code": cfg.get("card_template_area_code") or "",
        "regions": parse_area_code(cfg.get("card_template_area_code") or ""),
        "show_portrait": bool(show_portrait),
        "font_path": font_path,
        "settings": settings,
    }
