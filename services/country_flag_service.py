"""Website-managed PNG country flags for website-managed player template cards."""

import io
import logging
import os
import re

from PIL import Image

logger = logging.getLogger(__name__)

FLAGS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "country_flags",
)
MAX_BYTES = 2 * 1024 * 1024
MIN_DIM = 16


def normalise_country(country):
    """Return a stable, filesystem-safe key for a country display name."""
    return re.sub(r"[^a-z0-9]+", "_", (country or "").strip().lower()).strip("_")


def _ensure_dir():
    os.makedirs(FLAGS_ROOT, exist_ok=True)


def _path_for(country):
    key = normalise_country(country)
    return os.path.join(FLAGS_ROOT, f"{key}.png") if key else None


def save_country_flag(country, file_bytes, original_filename):
    """Validate and save a PNG flag. Returns ``(success, message)``."""
    country = (country or "").strip()
    if not normalise_country(country):
        return False, "Please enter a country name."
    if not (original_filename or "").lower().endswith(".png"):
        return False, "Country flags must be PNG files."
    if not file_bytes:
        return False, "Please choose a country flag PNG to upload."
    if len(file_bytes) > MAX_BYTES:
        return False, "Flag file is too large. Max is 2 MB."
    try:
        image = Image.open(io.BytesIO(file_bytes))
        image.verify()
        image = Image.open(io.BytesIO(file_bytes))
        if image.format != "PNG":
            return False, "Country flags must be PNG files."
        if image.width < MIN_DIM or image.height < MIN_DIM:
            return False, f"Flag image is too small ({image.width}×{image.height}). Minimum is {MIN_DIM}×{MIN_DIM}."
    except Exception as exc:
        return False, f"Not a valid PNG image: {exc}"

    _ensure_dir()
    path = _path_for(country)
    try:
        with open(path, "wb") as handle:
            handle.write(file_bytes)
    except OSError as exc:
        logger.exception("save_country_flag disk write failed")
        return False, f"Disk write failed: {exc}"
    return True, f"Flag saved for {country}."


def remove_country_flag(country):
    """Remove a configured flag PNG. Returns whether a file was removed."""
    path = _path_for(country)
    if path and os.path.isfile(path):
        os.remove(path)
        return True
    return False


def get_country_flag(country):
    """Return a country's configured flag as RGBA Pillow image, or ``None``."""
    path = _path_for(country)
    if not path or not os.path.isfile(path):
        return None
    try:
        with Image.open(path) as image:
            return image.convert("RGBA")
    except Exception:
        logger.exception("Could not read country flag %s", path)
        return None


def list_country_flags():
    """Return uploaded flag keys for the website manager."""
    if not os.path.isdir(FLAGS_ROOT):
        return []
    return sorted(
        os.path.splitext(filename)[0].replace("_", " ").title()
        for filename in os.listdir(FLAGS_ROOT)
        if filename.lower().endswith(".png")
    )
