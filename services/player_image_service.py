"""Player Image service — manages custom card images.

When the bot needs to show a player card:
  1. Call get_custom_image_bytes(player_id)
  2. If bytes returned → use those
  3. If None → fall back to auto-generated card

Storage layout:
  data/player_images/<player_id>.<ext>

Validation:
  - Allowed: PNG, JPG, JPEG, WebP
  - Max size: 5 MB
  - Min dimensions: 200x200 (cards smaller than this look bad)
  - Recommended: 600x800 or similar 3:4 aspect (fits Telegram nicely)
"""

import os
import logging
from datetime import datetime

from models import PlayerImage

logger = logging.getLogger(__name__)


# Storage config
IMAGES_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "player_images")
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
MAX_BYTES = 5 * 1024 * 1024     # 5 MB
MIN_DIM = 200                    # min width/height in pixels


def _ensure_dir():
    if not os.path.exists(IMAGES_ROOT):
        try:
            os.makedirs(IMAGES_ROOT, exist_ok=True)
        except Exception:
            logger.exception(f"Failed to create {IMAGES_ROOT}")


def _ext_from_filename(filename):
    return (filename.rsplit(".", 1)[-1] or "").lower() if "." in (filename or "") else ""


def _path_for(player_id, ext):
    _ensure_dir()
    return os.path.join(IMAGES_ROOT, f"{player_id}.{ext}")


def get_custom_image_bytes(player_id):
    """Return raw bytes of the custom image if one is set & active, else None.

    Looks up DB → reads file. Cheap operation (single small DB query + file read).
    Designed to be called every time a card is needed; the bot returns this
    instead of calling the generator if non-None.

    Result is cached in-memory with an LRU. Cache invalidated when admin
    uploads/removes/toggles. Cuts disk I/O + DB round-trips on hot paths.
    """
    # Check cache first
    if player_id in _IMG_CACHE:
        return _IMG_CACHE[player_id]
    try:
        from database import get_session
        session = get_session()
        try:
            row = (session.query(PlayerImage)
                   .filter(PlayerImage.player_id == player_id,
                           PlayerImage.is_active == True).first())
            if not row:
                _IMG_CACHE[player_id] = None
                return None
            path = row.image_path
            if not path or not os.path.isfile(path):
                logger.warning(f"PlayerImage row exists for player {player_id} but file missing: {path}")
                _IMG_CACHE[player_id] = None
                return None
            with open(path, "rb") as f:
                data = f.read()
            # Cap cache size to avoid runaway memory
            if len(_IMG_CACHE) > 200:
                _IMG_CACHE.clear()
            _IMG_CACHE[player_id] = data
            return data
        finally:
            session.close()
    except Exception:
        logger.exception(f"get_custom_image_bytes failed for player {player_id}")
        return None


def _invalidate_image_cache(player_id=None):
    """Drop cached image bytes. Call after upload/remove/toggle.
    If player_id is None, drops all."""
    if player_id is None:
        _IMG_CACHE.clear()
    else:
        _IMG_CACHE.pop(player_id, None)


# Module-level image bytes cache (LRU-ish, capped at 200 entries)
_IMG_CACHE = {}


def save_custom_image(session, player_id, file_bytes, original_filename,
                      label=None, uploaded_by=None):
    """Save uploaded image bytes to disk + create/update PlayerImage row.

    Validates: extension, file size, image dimensions (uses Pillow if available).
    Returns: (success, message). Does NOT commit — caller controls transaction.
    """
    # Validate extension
    ext = _ext_from_filename(original_filename)
    if ext not in ALLOWED_EXT:
        return False, f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXT))}"

    # Validate size
    if len(file_bytes) > MAX_BYTES:
        return False, f"File too large ({len(file_bytes)/1024/1024:.1f} MB). Max is {MAX_BYTES/1024/1024:.0f} MB."

    # Validate it's a real image + dimensions
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(file_bytes))
        img.verify()  # raises if not a valid image
        # Re-open after verify (verify exhausts the stream)
        img = Image.open(_io.BytesIO(file_bytes))
        w, h = img.size
        if w < MIN_DIM or h < MIN_DIM:
            return False, f"Image too small ({w}×{h}). Minimum is {MIN_DIM}×{MIN_DIM}."
    except Exception as e:
        return False, f"Not a valid image file: {e}"

    # Save to disk (overwrite existing)
    _ensure_dir()
    # Remove any prior file for this player (could be different extension)
    for old_ext in ALLOWED_EXT:
        old_path = _path_for(player_id, old_ext)
        if os.path.isfile(old_path):
            try: os.remove(old_path)
            except Exception: pass

    path = _path_for(player_id, ext)
    try:
        with open(path, "wb") as f:
            f.write(file_bytes)
    except Exception as e:
        logger.exception("save_custom_image disk write failed")
        return False, f"Disk write failed: {e}"

    # DB row (upsert)
    row = (session.query(PlayerImage)
           .filter(PlayerImage.player_id == player_id).first())
    if row:
        row.image_path = path
        row.label = (label or "").strip() or None
        row.is_active = True
        row.uploaded_at = datetime.utcnow()
        row.uploaded_by = uploaded_by
        # New bytes → old file_id is stale, must re-upload
        row.tg_file_id = None
    else:
        row = PlayerImage(
            player_id=player_id, image_kind="default",
            image_path=path, label=(label or "").strip() or None,
            is_active=True,
            uploaded_at=datetime.utcnow(),
            uploaded_by=uploaded_by,
        )
        session.add(row)
    session.flush()

    # Best-effort upload to Telegram storage channel. No-op when not configured.
    try:
        from services import tg_storage_service
        if tg_storage_service.is_configured():
            from models import Player as _P
            player_name = ""
            try:
                p = session.query(_P).get(player_id)
                if p: player_name = f"{p.name} ({p.rating})"
            except Exception:
                pass
            file_id = tg_storage_service.upload_photo_sync(
                path,
                caption=f"player_image #{player_id} {player_name}",
            )
            if file_id:
                row.tg_file_id = file_id
                session.flush()
                logger.info(f"Uploaded player {player_id} image to channel")
    except Exception:
        logger.exception("Channel upload failed (non-fatal)")

    # Invalidate the image cache so next read fetches the new file
    _invalidate_image_cache(player_id)
    return True, "Saved."


def get_tg_file_id(session, player_id):
    """Return cached Telegram file_id for this player's custom image if any.

    Use in handlers to skip disk reads:
        file_id = get_tg_file_id(db, player.id)
        if file_id:
            await ctx.bot.send_photo(chat_id, photo=file_id, caption=...)
        else:
            # fall back to local file
    """
    row = (session.query(PlayerImage)
           .filter(PlayerImage.player_id == player_id,
                   PlayerImage.is_active == True).first())
    if row and row.tg_file_id:
        return row.tg_file_id
    return None


def remove_custom_image(session, player_id):
    """Delete custom image (DB row + disk file).
    Returns (success, message). Caller commits."""
    row = (session.query(PlayerImage)
           .filter(PlayerImage.player_id == player_id).first())
    if not row:
        return False, "No custom image set."
    # Remove file
    if row.image_path and os.path.isfile(row.image_path):
        try:
            os.remove(row.image_path)
        except Exception:
            logger.exception(f"Failed to remove {row.image_path}")
    session.delete(row)
    session.flush()
    _invalidate_image_cache(player_id)
    return True, "Removed."


def toggle_active(session, player_id):
    """Flip is_active flag. Returns (success, new_state)."""
    row = (session.query(PlayerImage)
           .filter(PlayerImage.player_id == player_id).first())
    if not row:
        return False, None
    row.is_active = not row.is_active
    session.flush()
    _invalidate_image_cache(player_id)
    return True, row.is_active


def get_metadata(session, player_id):
    """Return dict of metadata for admin UI (or None)."""
    row = (session.query(PlayerImage)
           .filter(PlayerImage.player_id == player_id).first())
    if not row:
        return None
    file_size = 0
    if row.image_path and os.path.isfile(row.image_path):
        try: file_size = os.path.getsize(row.image_path)
        except Exception: pass
    return {
        "id": row.id,
        "image_path": row.image_path,
        "label": row.label,
        "is_active": row.is_active,
        "uploaded_at": row.uploaded_at,
        "uploaded_by": row.uploaded_by,
        "file_size": file_size,
        "exists_on_disk": bool(row.image_path and os.path.isfile(row.image_path)),
    }


def list_players_with_custom_images(session):
    """Return set of player_ids that have an active custom image."""
    rows = (session.query(PlayerImage.player_id)
            .filter(PlayerImage.is_active == True).all())
    return {r[0] for r in rows}
