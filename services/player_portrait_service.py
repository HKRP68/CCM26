"""Player portrait uploads used inside website-generated player cards.

A portrait is intentionally separate from a custom full-card image. Custom card
art remains the highest-priority output; this service supplies the player cutout
for the website card generator and an optional global fallback cutout.
"""

import io
import logging
import os

from PIL import Image

logger = logging.getLogger(__name__)

PORTRAITS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "player_portraits",
)
GLOBAL_PORTRAIT_PATH = os.path.join(PORTRAITS_ROOT, "global_player.png")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
MAX_BYTES = 5 * 1024 * 1024
MIN_DIM = 200


def _ensure_dir():
    os.makedirs(PORTRAITS_ROOT, exist_ok=True)


def _ext_from_filename(filename):
    return (filename.rsplit(".", 1)[-1] or "").lower() if "." in (filename or "") else ""


def _path_for(player_id, ext):
    _ensure_dir()
    return os.path.join(PORTRAITS_ROOT, f"{player_id}.{ext}")


def _remove_files(player_id):
    for ext in ALLOWED_EXT:
        path = _path_for(player_id, ext)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                logger.exception("Failed to remove portrait %s", path)


def _validate_image(file_bytes, original_filename, *, png_only=False):
    ext = _ext_from_filename(original_filename)
    allowed = {"png"} if png_only else ALLOWED_EXT
    if ext not in allowed:
        if png_only:
            return False, "Global player fallback must be a PNG file.", None
        return False, f"Unsupported file type. Allowed: {', '.join(sorted(allowed))}", None
    if not file_bytes:
        return False, "Please choose a player image to upload.", None
    if len(file_bytes) > MAX_BYTES:
        return False, f"File too large ({len(file_bytes) / 1024 / 1024:.1f} MB). Max is 5 MB.", None
    try:
        image = Image.open(io.BytesIO(file_bytes))
        image.verify()
        image = Image.open(io.BytesIO(file_bytes))
        if image.width < MIN_DIM or image.height < MIN_DIM:
            return False, f"Image too small ({image.width}×{image.height}). Minimum is {MIN_DIM}×{MIN_DIM}.", None
    except Exception as exc:
        return False, f"Not a valid image file: {exc}", None
    return True, "", ext


def save_player_portrait(player, file_bytes, original_filename):
    """Validate and store an uploaded portrait, then update ``player.image_url``."""
    ok, message, ext = _validate_image(file_bytes, original_filename)
    if not ok:
        return False, message
    _remove_files(player.id)
    path = _path_for(player.id, ext)
    try:
        with open(path, "wb") as file_obj:
            file_obj.write(file_bytes)
    except OSError as exc:
        logger.exception("save_player_portrait disk write failed")
        return False, f"Disk write failed: {exc}"
    player.image_url = path
    return True, "Player image saved."


def remove_player_portrait(player):
    """Remove the uploaded portrait so rendering can use the global fallback."""
    _remove_files(player.id)
    player.image_url = None


def _load_portrait(path):
    """Load one configured portrait path as RGBA, or return ``None``."""
    if not path:
        return None
    candidates = [path]
    if not os.path.isabs(path):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        candidates.append(os.path.join(project_root, path.lstrip("/")))
        candidates.append(os.path.join(project_root, "static", "players", os.path.basename(path)))
    for candidate in candidates:
        if os.path.isfile(candidate):
            try:
                with Image.open(candidate) as image:
                    return image.convert("RGBA")
            except Exception:
                logger.exception("Could not read player portrait %s", candidate)
                return None
    return None


def get_global_player_portrait():
    """Return the admin-uploaded global fallback PNG as an RGBA image."""
    return _load_portrait(GLOBAL_PORTRAIT_PATH)


def get_player_portrait(player, include_global=True):
    """Return the player's RGBA portrait, falling back to the global PNG."""
    portrait = _load_portrait((getattr(player, "image_url", None) or "").strip())
    if portrait is not None or not include_global:
        return portrait
    return get_global_player_portrait()


def has_player_portrait(player, include_global=False):
    """Return whether the player has a portrait, optionally including fallback."""
    return get_player_portrait(player, include_global=include_global) is not None


def save_global_player_portrait(file_bytes, original_filename):
    """Validate and store the global PNG used when a player has no portrait."""
    ok, message, _ = _validate_image(file_bytes, original_filename, png_only=True)
    if not ok:
        return False, message
    _ensure_dir()
    try:
        with open(GLOBAL_PORTRAIT_PATH, "wb") as file_obj:
            file_obj.write(file_bytes)
    except OSError as exc:
        logger.exception("save_global_player_portrait disk write failed")
        return False, f"Disk write failed: {exc}"
    return True, "Global player fallback PNG saved."


def remove_global_player_portrait():
    """Remove the global fallback PNG. Return whether a file was removed."""
    if not os.path.isfile(GLOBAL_PORTRAIT_PATH):
        return False
    try:
        os.remove(GLOBAL_PORTRAIT_PATH)
        return True
    except OSError:
        logger.exception("Failed to remove global portrait %s", GLOBAL_PORTRAIT_PATH)
        return False


def has_global_player_portrait():
    """Return whether a readable global fallback PNG is configured."""
    return get_global_player_portrait() is not None
