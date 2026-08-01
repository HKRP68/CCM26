"""Career Player faces — the selectable card designs behind /cmucareer.

Each face is a *complete blank card*: the player's portrait and the "CAREER
PLAYER" banner are already part of the uploaded artwork, and the card generator
only fills the empty slots (name, category, OVR, batting power, bowling specs,
flag + country, batting and bowling style).

That makes a face nothing more than a card-template variant — ``career_<slot>``
— so it inherits the existing per-variant blank upload, layout editor, live
preview and save button for free. This module owns the small amount that is
genuinely new: creating and ordering faces, and keeping the wizard's preview
photo in step with the uploaded artwork.
"""

import logging
import os

from models import CareerFace
from services.card_template_service import (
    ALLOWED_EXT, CAREER_VARIANT_PREFIX, TEMPLATES_ROOT,
    remove_template_image, save_template_image,
)

logger = logging.getLogger(__name__)

MAX_FACES = 40


def variant_for(slot):
    """Card-template variant key for a face slot."""
    return f"{CAREER_VARIANT_PREFIX}{int(slot)}"


def list_faces(session, active_only=False):
    """Faces in display order, newest slot last."""
    query = session.query(CareerFace)
    if active_only:
        query = query.filter(CareerFace.is_active.is_(True))
    return query.order_by(CareerFace.sort_order, CareerFace.slot).all()


def get_face(session, slot):
    return session.query(CareerFace).filter(CareerFace.slot == int(slot)).first()


def selectable_faces(session):
    """Faces the wizard may offer: active, and with artwork actually uploaded.

    A face with no blank card would render on the base template and look wrong,
    so it is held back until the admin uploads its artwork.
    """
    from services.card_template_service import template_image_path
    out = []
    for face in list_faces(session, active_only=True):
        if template_image_path(session, variant_for(face.slot),
                               fallback_to_base=False):
            out.append(face)
    return out


def next_slot(session):
    """Lowest unused slot number, so deleting face 2 frees that slot again."""
    used = {row.slot for row in session.query(CareerFace.slot).all()}
    slot = 1
    while slot in used:
        slot += 1
    return slot


def add_face(session, label=None):
    """Create an empty face. Caller commits. Returns ``(face, error)``."""
    if session.query(CareerFace).count() >= MAX_FACES:
        return None, f"You already have the maximum of {MAX_FACES} faces."
    slot = next_slot(session)
    face = CareerFace(
        slot=slot,
        label=(label or "").strip()[:60] or f"Face {slot}",
        sort_order=slot,
        is_active=True,
    )
    session.add(face)
    session.flush()
    return face, None


def save_face_artwork(session, slot, file_bytes, filename):
    """Store a face's blank card. Caller commits. Returns ``(ok, message)``.

    The artwork is written through the shared card-template store, so it lands
    under the ``template_career_<slot>`` stem the renderer and the Telegram
    storage mirror both already look for.
    """
    face = get_face(session, slot)
    if not face:
        return False, "That face no longer exists."
    ok, message, path = save_template_image(file_bytes, filename,
                                            variant=variant_for(slot))
    if not ok:
        return False, message
    face.template_path = os.path.relpath(
        path, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # The wizard's preview photo is re-uploaded from the new artwork on next
    # use; drop the stale Telegram file_id so it can't show the old design.
    face.preview_file_id = None
    return True, f"{face.label or f'Face {slot}'} artwork saved."


def remove_face(session, slot):
    """Delete a face and its artwork. Caller commits.

    Career players already wearing this face keep rendering — their variant
    falls back to face 1 and then to the base card — so removing a design never
    breaks somebody's existing card.
    """
    face = get_face(session, slot)
    if not face:
        return False, "That face no longer exists."
    label = face.label or f"Face {slot}"
    remove_template_image(variant_for(slot))
    session.delete(face)
    return True, f"Removed {label}."


def set_face_fields(session, slot, label=None, is_active=None, sort_order=None):
    """Rename, reorder or (de)activate a face. Caller commits."""
    face = get_face(session, slot)
    if not face:
        return False, "That face no longer exists."
    if label is not None:
        face.label = (label or "").strip()[:60] or f"Face {slot}"
    if is_active is not None:
        face.is_active = bool(is_active)
    if sort_order is not None:
        try:
            face.sort_order = int(sort_order)
        except (TypeError, ValueError):
            pass
    return True, "Face updated."


def artwork_path(slot):
    """On-disk path of a face's blank card, or ``None``."""
    stem = f"template_{variant_for(slot)}"
    for ext in ALLOWED_EXT:
        path = os.path.join(TEMPLATES_ROOT, f"{stem}.{ext}")
        if os.path.isfile(path):
            return path
    return None


def face_in_use_count(session, slot):
    """How many career players currently wear this face."""
    from models import Player
    return (session.query(Player)
            .filter(Player.is_career.is_(True),
                    Player.career_face == variant_for(slot))
            .count())
