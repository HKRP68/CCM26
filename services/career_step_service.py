"""Website-managed artwork for each step of the /cmucareer wizard.

Every wizard step is a photo message with buttons underneath, and the photo for
each step is uploaded from the website. Images are stored on disk and mirrored
to the Telegram storage channel, so the bot re-sends them by ``file_id`` without
a disk read and they survive an ephemeral-host redeploy — the same durability
pattern as ``services.player_image_service`` and the /CMUshop gallery.
"""

import io
import logging
import os

from models import CareerStepImage

logger = logging.getLogger(__name__)

IMAGES_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "career_steps",
)
ALLOWED_EXT = {"png", "jpg", "jpeg", "webp"}
MAX_BYTES = 5 * 1024 * 1024
MIN_DIM = 200

# The wizard steps, in the order the player walks through them.
STEP_KEYS = ("intro", "country", "initial", "surname", "bat_hand",
             "bowl_hand", "bowl_type", "face", "confirm")
STEP_LABELS = {
    "intro": "Welcome / start screen",
    "country": "Choose your country",
    "initial": "Choose your first-name initial",
    "surname": "Choose your surname initial",
    "bat_hand": "Choose your batting hand",
    "bowl_hand": "Choose your bowling hand",
    "bowl_type": "Choose your bowling type",
    "face": "Choose your face (fallback when a face has no preview yet)",
    "confirm": "Final confirmation",
}


def _ensure_dir():
    os.makedirs(IMAGES_ROOT, exist_ok=True)


def _ext(filename):
    return (filename.rsplit(".", 1)[-1] or "").lower() if "." in (filename or "") else ""


def save_step_image(session, step_key, file_bytes, filename, uploaded_by=None):
    """Validate and store one step's photo. Caller commits."""
    if step_key not in STEP_KEYS:
        return False, "Unknown wizard step."
    ext = _ext(filename)
    if ext not in ALLOWED_EXT:
        return False, f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_EXT))}."
    if not file_bytes:
        return False, "Please choose an image to upload."
    if len(file_bytes) > MAX_BYTES:
        return False, (f"Image is too large ({len(file_bytes) / 1024 / 1024:.1f} MB). "
                       f"Max is {MAX_BYTES // 1024 // 1024} MB.")
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(file_bytes))
        image.verify()
        image = Image.open(io.BytesIO(file_bytes))
        if image.width < MIN_DIM or image.height < MIN_DIM:
            return False, (f"Image is too small ({image.width}×{image.height}). "
                           f"Minimum is {MIN_DIM}×{MIN_DIM}.")
    except Exception as exc:
        return False, f"Not a valid image: {exc}"

    _ensure_dir()
    # One file per step — drop older extensions so nothing is left orphaned.
    for old in ALLOWED_EXT:
        path = os.path.join(IMAGES_ROOT, f"{step_key}.{old}")
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    path = os.path.join(IMAGES_ROOT, f"{step_key}.{ext}")
    try:
        with open(path, "wb") as handle:
            handle.write(file_bytes)
    except OSError as exc:
        logger.exception("career step image write failed")
        return False, f"Disk write failed: {exc}"

    row = (session.query(CareerStepImage)
           .filter(CareerStepImage.step_key == step_key).first())
    relative = os.path.relpath(
        path, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not row:
        row = CareerStepImage(step_key=step_key, is_active=True)
        session.add(row)
    row.image_path = relative
    row.uploaded_by = uploaded_by
    # A new picture means the cached Telegram file_id points at the old one.
    row.tg_file_id = None
    _mirror_to_storage(row, path)
    return True, f"Saved the image for “{STEP_LABELS[step_key]}”."


def _mirror_to_storage(row, path):
    """Upload to the Telegram storage channel so redeploys keep the image."""
    try:
        from services.tg_storage_service import is_configured, upload_photo_sync
        if not is_configured():
            return
        file_id = upload_photo_sync(path, caption=f"career-step:{row.step_key}")
        if file_id:
            row.tg_file_id = file_id
    except Exception:
        logger.exception("career step image storage mirror failed")


def remove_step_image(session, step_key):
    """Delete one step's photo. Caller commits."""
    if step_key not in STEP_KEYS:
        return False, "Unknown wizard step."
    removed = False
    for ext in ALLOWED_EXT:
        path = os.path.join(IMAGES_ROOT, f"{step_key}.{ext}")
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed = True
            except OSError:
                logger.exception("career step image delete failed")
    row = (session.query(CareerStepImage)
           .filter(CareerStepImage.step_key == step_key).first())
    if row:
        session.delete(row)
        removed = True
    if not removed:
        return False, "No image is uploaded for that step."
    return True, f"Removed the image for “{STEP_LABELS[step_key]}”."


def step_photo(session, step_key):
    """What to send as this step's photo: a Telegram ``file_id``, bytes, or None.

    The wizard passes the result straight to ``send_photo`` / ``InputMediaPhoto``,
    both of which accept either form.
    """
    if step_key not in STEP_KEYS:
        return None
    row = (session.query(CareerStepImage)
           .filter(CareerStepImage.step_key == step_key,
                   CareerStepImage.is_active.is_(True)).first())
    if not row:
        return None
    if row.tg_file_id:
        return row.tg_file_id
    path = row.image_path
    if path and not os.path.isabs(path):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), path)
    if path and os.path.isfile(path):
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            logger.exception("career step image read failed")
    return None
