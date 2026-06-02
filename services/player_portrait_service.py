"""Player portrait uploads used inside auto-generated player cards.

A portrait is intentionally separate from a custom full-card image. Custom card
art remains the highest-priority output; this service only supplies the player
cut-out for the generated fallback design.
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


def save_player_portrait(player, file_bytes, original_filename):
    """Validate and store an uploaded portrait, then update ``player.image_url``.

    Returns ``(success, message)``. The caller owns the database transaction.
    Transparent PNG/WebP images look best, but regular photos are accepted.
    """
    ext = _ext_from_filename(original_filename)
    if ext not in ALLOWED_EXT:
        return False, f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXT))}"
    if not file_bytes:
        return False, "Please choose a player image to upload."
    if len(file_bytes) > MAX_BYTES:
        return False, f"File too large ({len(file_bytes) / 1024 / 1024:.1f} MB). Max is 5 MB."

    try:
        image = Image.open(io.BytesIO(file_bytes))
        image.verify()
        image = Image.open(io.BytesIO(file_bytes))
        if image.width < MIN_DIM or image.height < MIN_DIM:
            return False, f"Image too small ({image.width}×{image.height}). Minimum is {MIN_DIM}×{MIN_DIM}."
    except Exception as exc:
        return False, f"Not a valid image file: {exc}"

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
    """Remove the uploaded portrait and restore silhouette rendering."""
    _remove_files(player.id)
    player.image_url = None


def get_player_portrait(player):
    """Return the portrait as an RGBA Pillow image, or ``None`` when absent."""
    path = (getattr(player, "image_url", None) or "").strip()
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


def has_player_portrait(player):
    """Return whether the player's configured portrait exists on disk."""
    return get_player_portrait(player) is not None
