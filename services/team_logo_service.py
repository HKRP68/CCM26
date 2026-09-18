"""Team crests, and the admin approval they go through first.

A user sets their team's crest with ``/setteamlogo``. That image is then drawn
on every scorecard of every match their team plays, where every other player in
the chat sees it — so it is not applied on upload. It is held as a
:class:`models.TeamLogoRequest` until a bot admin approves it from their DM, and
only then copied onto the ``users`` row.

Shaped after :mod:`services.career_change_service`, which is the same pipeline
for career-player names, and which set the two rules this follows:

* **Nothing changes while a request is pending.** Whatever crest the team has
  today keeps rendering until an admin decides.
* **Services never commit; the caller does.** Every function here mutates the
  session it is handed and leaves the commit to the handler or route, so one
  Telegram update is one transaction.

The bytes are normalised to PNG on submit and stored through
:mod:`services.asset_store`, because the host filesystem is rebuilt on every
deploy; the on-disk copy under ``data/team_logos`` is a cache that
``asset_store.ensure`` heals on a read miss. Telegram's own ``file_id`` is kept
alongside so a DM or ``/myteam`` can re-send the image without a render.
"""

import io
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
OPEN_STATUSES = (STATUS_PENDING,)
DONE_STATUSES = (STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELLED)

# Upload limits. The first three mirror services/player_image_service.py, which
# is the validator the website has used for custom card art for a long time.
MAX_BYTES = 5 * 1024 * 1024
MIN_DIM = 200
# PNG only. Not arbitrary strictness: a PNG is the only common format that can
# carry a transparent background, and a crest without one draws as a square tile
# sitting on the team's colour instead of on it. services/player_portrait_service
# takes the same line for the global portrait via its ``png_only=`` flag.
ALLOWED_FORMATS = {"PNG"}

# Refusing a JPG without saying how to fix it teaches nobody anything, so the
# refusal carries the how-to. Shared with the "needs transparency" rejection
# reason below, so the advice reads the same wherever it surfaces.
TRANSPARENCY_HELP = (
    "<b>How to get one:</b>\n"
    "• Already have a logo? Run it through a free background remover — "
    "remove.bg, photoroom.com, or Canva's BG Remover — then download it "
    "as a PNG.\n"
    "• On a phone, most gallery and editing apps can export PNG from "
    "<i>Share → Save as</i>.\n"
    "• Making one from scratch? Canva and Figma both export PNG with "
    "<i>transparent background</i> ticked."
)
# A crest sits in a tall-ish panel on the card. Anything wilder than 2:1 either
# renders as a sliver or forces the panel to letterbox it into uselessness.
MAX_ASPECT = 2.0
# Stored size. Larger buys nothing: the panel is ~165x230 card pixels, drawn at
# 2x, so 512 is already more than the card can show.
STORE_DIM = 512

_LOGO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "team_logos")
_ASSET_PREFIX = "data/team_logos"

# Submission limits. Every upload is a DM an admin has to action, and without
# these one user can fill the queue on their own — resubmitting the instant a
# rejection lands, forever.
DAILY_SUBMISSION_CAP = 5
# A rejection is a "no, not this". Coming straight back with another image is
# usually the same image lightly edited, so there is a pause.
REJECT_COOLDOWN_SECONDS = 60 * 60
# After this many rejections in a row with nothing approved between them, the
# next attempt is held until an admin lifts it with /logoqueue.
CONSECUTIVE_REJECT_LIMIT = 3

# The reasons an admin can reject with in one tap. ``key`` rides in the callback
# data, so it stays short and stable — renaming one changes what an in-flight
# button means.
REJECT_REASONS = (
    ("own", "🎨 Not your artwork",
     "It looks like someone else's artwork or a copyrighted logo. "
     "Please upload something you made or have the right to use."),
    ("nsfw", "🚫 Offensive / NSFW",
     "The image is not suitable for a game every age plays. "
     "Please pick something else."),
    ("qual", "🔍 Too low quality",
     "The image is too small or too blurry to read once it is drawn on a "
     "scorecard. Please upload a sharper, larger version."),
    ("ads", "🔗 Ads or links",
     "The image carries advertising, a link or a handle. "
     "Crests only, please."),
    ("shape", "📐 Wrong shape",
     "The image is too wide or too tall for the crest panel. "
     "A square-ish image works best."),
    ("alpha", "🪟 Needs a transparent background",
     "The logo has a solid background, so it shows as a square tile on the "
     "scorecard instead of sitting on your team's colour.\n\n"
     + TRANSPARENCY_HELP),
)
REJECT_REASON_MAP = {key: (label, message) for key, label, message in REJECT_REASONS}
CUSTOM_REASON_KEY = "custom"


# ══════════════════════════════════════════════════════════════════════
# Lookups
# ══════════════════════════════════════════════════════════════════════

def _flush(session):
    """Make the caller's own uncommitted changes visible to the next query.

    ``SessionLocal`` is built with ``autoflush=False`` (database.py), so a
    status this service just set is *not* seen by a later filter on that
    status until something flushes. Every read below is a decision about what
    the caller has already done, so they all flush first. This stays inside the
    caller's transaction — a rollback still discards it.
    """
    try:
        session.flush()
    except Exception:
        logger.warning("team logo flush failed", exc_info=True)


def pending_request(session, user_id):
    """The user's open request, or ``None``. There is at most one."""
    from models import TeamLogoRequest
    _flush(session)
    return (session.query(TeamLogoRequest)
            .filter(TeamLogoRequest.user_id == user_id,
                    TeamLogoRequest.status == STATUS_PENDING)
            .order_by(TeamLogoRequest.created_at.desc())
            .first())


def get_request(session, request_id):
    from models import TeamLogoRequest
    return session.get(TeamLogoRequest, request_id)


def list_requests(session, *, status=STATUS_PENDING, limit=25, offset=0):
    from models import TeamLogoRequest
    query = session.query(TeamLogoRequest)
    if status:
        query = query.filter(TeamLogoRequest.status == status)
    return (query.order_by(TeamLogoRequest.created_at.asc())
            .offset(offset).limit(limit).all())


def pending_count(session):
    from models import TeamLogoRequest
    return (session.query(TeamLogoRequest)
            .filter(TeamLogoRequest.status == STATUS_PENDING).count())


def request_summary(request):
    """One dict for a bot caption, the website queue or a log line."""
    if request is None:
        return None
    return {
        "id": request.id,
        "user_id": request.user_id,
        "telegram_id": request.telegram_id,
        "team_name": request.team_name,
        "status": request.status,
        "review_note": request.review_note,
        "reviewed_by": request.reviewed_by,
        "width": request.width,
        "height": request.height,
        "byte_size": request.byte_size,
        "created_at": request.created_at,
        "decided_at": request.decided_at,
    }


# ══════════════════════════════════════════════════════════════════════
# Validation and storage
# ══════════════════════════════════════════════════════════════════════

_FRIENDLY_FORMAT = {"JPEG": "a JPG", "WEBP": "a WEBP", "GIF": "a GIF",
                    "BMP": "a BMP", "TIFF": "a TIFF"}


def _not_png_message(fmt):
    """Why a non-PNG is refused, and what to do about it."""
    what = _FRIENDLY_FORMAT.get(fmt, f"a {fmt}" if fmt else "that")
    return (f"That's {what} — team logos have to be <b>PNG</b>.\n\n"
            "A PNG can carry a transparent background, which is what lets your "
            "crest sit on your team's colour instead of in a white box.\n\n"
            + TRANSPARENCY_HELP +
            "\n\nThen send it here again.")


def normalise_image(raw):
    """Validate and re-encode an upload.

    Returns ``(png_bytes, width, height, opaque)``, where ``opaque`` marks an
    image with no transparency — accepted, but it will draw as a square tile,
    so the uploader and the reviewing admin are both told.

    Raises :class:`ValueError` with a message meant for the uploader.

    Re-encoding is the point, not a side effect: it drops EXIF, animation
    frames and anything the decoder found interesting, so what is stored is a
    plain PNG the card renderer can open without surprises.
    """
    from PIL import Image
    if not raw:
        raise ValueError("That came through empty — try sending the image again.")
    if len(raw) > MAX_BYTES:
        raise ValueError(
            f"That file is {len(raw) / 1024 / 1024:.1f} MB. "
            f"The limit is {MAX_BYTES // 1024 // 1024} MB.")
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = (probe.format or "").upper()
        probe.verify()          # cheap structural check; consumes the object
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        raise ValueError("That does not look like an image I can read. "
                         "Send a PNG.")
    if fmt not in ALLOWED_FORMATS:
        raise ValueError(_not_png_message(fmt))
    width, height = image.size
    if width < MIN_DIM or height < MIN_DIM:
        raise ValueError(f"That image is {width}×{height}. "
                         f"It needs to be at least {MIN_DIM}×{MIN_DIM}.")
    if max(width, height) / max(1, min(width, height)) > MAX_ASPECT:
        raise ValueError(f"That image is {width}×{height}, which is too long "
                         "and thin for the crest panel. Something square-ish "
                         "works best.")

    image = image.convert("RGBA")
    # A PNG can still be fully opaque, and then it is a square tile rather than
    # a crest. Cheapest possible check: the alpha channel's darkest pixel.
    try:
        opaque = image.getchannel("A").getextrema()[0] == 255
    except Exception:
        opaque = False
    if max(image.size) > STORE_DIM:
        factor = STORE_DIM / max(image.size)
        image = image.resize(
            (max(1, int(width * factor)), max(1, int(height * factor))),
            Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), image.width, image.height, opaque


def _asset_key(user_id):
    """A key no other upload can land on.

    This must be unique per *submission*, not per user: while a new crest is in
    review the previously approved one is still being drawn on cards, and a
    shared key would let the unreviewed bytes overwrite it. A timestamp alone is
    not enough — two uploads in the same second collide — so a random suffix
    carries the uniqueness and the timestamp is only there to keep the keys
    legible when someone lists them.
    """
    return (f"{_ASSET_PREFIX}/{int(user_id)}-{int(time.time())}"
            f"-{secrets.token_hex(4)}.png")


def _store(session, key, png, uploaded_by=None):
    """Write the bytes to the durable store and the on-disk cache.

    Deliberately *not* ``asset_store.put``: like ``drop``, it opens its own
    session, and this runs inside the caller's open transaction. A second
    pooled connection writing while the first is mid-transaction is the
    pool-exhaustion trap ``services/player_image_service`` documents, and on
    SQLite it fails outright. Writing through the caller's session also means
    the bytes and the request row land together or not at all.
    """
    import hashlib
    from models import StoredAsset
    from services import asset_store

    if len(png) > asset_store.MAX_ASSET_BYTES:
        logger.warning("team logo %s is over the asset cap", key)
        return False

    path = asset_store.absolute_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(png)
    except OSError:
        # The disk copy is only a cache; stored_assets is the source of truth.
        logger.warning("team logo cache write failed for %s", key, exc_info=True)

    try:
        row = session.query(StoredAsset).filter(StoredAsset.key == key).first()
        if row is None:
            row = StoredAsset(key=key)
            session.add(row)
        row.filename = os.path.basename(key)
        row.content_type = "image/png"
        row.data = png
        row.byte_size = len(png)
        row.sha256 = hashlib.sha256(png).hexdigest()
        if uploaded_by:
            row.updated_by = str(uploaded_by)[:100]
        return True
    except Exception:
        logger.exception("team logo could not be stored under %s", key)
        return False


def _forget(session, key):
    """Drop a crest's bytes, using the caller's session for the database row.

    Deliberately *not* ``asset_store.drop``: that opens its own session, and
    every caller here is mid-transaction with uncommitted changes of its own.
    A second connection writing the same tables blocks on Postgres and fails
    outright on SQLite, and because the failure is swallowed the row survives
    as an orphan — the bytes of a rejected logo would outlive the rejection.

    The row goes through the caller's transaction, so it is removed if and only
    if the decision that removed it is committed. The disk copy is only a cache
    and is unlinked immediately.
    """
    if not key:
        return
    from models import StoredAsset
    from services import asset_store
    try:
        (session.query(StoredAsset)
         .filter(StoredAsset.key == key)
         .delete(synchronize_session=False))
    except Exception:
        logger.warning("team logo row delete failed for %s", key, exc_info=True)
    try:
        path = asset_store.absolute_path(key)
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def logo_bytes_for_key(key):
    """The PNG bytes behind a stored key, healing the disk cache on a miss."""
    if not key:
        return None
    from services import asset_store
    path = asset_store.absolute_path(key)
    try:
        if not os.path.isfile(path):
            asset_store.ensure(path)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                return handle.read()
    except OSError:
        logger.warning("team logo read failed for %s", key, exc_info=True)
    return None


# ══════════════════════════════════════════════════════════════════════
# The request lifecycle
# ══════════════════════════════════════════════════════════════════════

def submission_block(session, user_id):
    """Why this user may not submit right now, or ``None``.

    Returns ``(reason, message)``. Checked before the image is decoded, so a
    throttled user does not pay for a validation pass they cannot use.
    """
    from models import TeamLogoRequest
    _flush(session)
    since = datetime.utcnow() - timedelta(days=1)
    today = (session.query(TeamLogoRequest)
             .filter(TeamLogoRequest.user_id == user_id,
                     TeamLogoRequest.created_at >= since).count())
    if today >= DAILY_SUBMISSION_CAP:
        return ("daily_cap",
                f"You have sent {today} logos in the last 24 hours, which is "
                f"the limit. Try again tomorrow.")

    recent = (session.query(TeamLogoRequest)
              .filter(TeamLogoRequest.user_id == user_id)
              .order_by(TeamLogoRequest.created_at.desc())
              .limit(CONSECUTIVE_REJECT_LIMIT).all())
    streak = 0
    for row in recent:
        if row.status != STATUS_REJECTED:
            break
        streak += 1
    if streak >= CONSECUTIVE_REJECT_LIMIT:
        return ("held",
                f"Your last {streak} logos were turned down, so new uploads "
                "are paused. Message a bot admin and they can lift it.")

    last_reject = next((r for r in recent if r.status == STATUS_REJECTED), None)
    if last_reject is not None and last_reject.decided_at:
        waited = (datetime.utcnow() - last_reject.decided_at).total_seconds()
        if waited < REJECT_COOLDOWN_SECONDS:
            minutes = int((REJECT_COOLDOWN_SECONDS - waited) // 60) + 1
            return ("cooldown",
                    f"Your last logo was turned down. You can send another in "
                    f"{minutes} minute{'s' if minutes != 1 else ''} — please "
                    "read the reason first.")
    return None


def clear_hold(session, user_id):
    """Lift a consecutive-rejection hold, so the owner can try again.

    The streak is counted off the most recent rows, so marking them
    ``cancelled`` ends it without rewriting what was decided or why.
    """
    from models import TeamLogoRequest
    rows = (session.query(TeamLogoRequest)
            .filter(TeamLogoRequest.user_id == user_id,
                    TeamLogoRequest.status == STATUS_REJECTED)
            .order_by(TeamLogoRequest.created_at.desc())
            .limit(CONSECUTIVE_REJECT_LIMIT).all())
    for row in rows:
        row.status = STATUS_CANCELLED
    return len(rows)


def submit_logo(session, user, raw_bytes, *, file_id=None, ignore_limits=False):
    """Queue a crest for review. Returns ``{"ok": ..., ...}``; caller commits."""
    from models import TeamLogoRequest
    existing = pending_request(session, user.id)
    if existing is not None:
        return {"ok": False, "error": "pending", "request": existing}
    if not ignore_limits:
        blocked = submission_block(session, user.id)
        if blocked:
            return {"ok": False, "error": blocked[0], "message": blocked[1]}
    try:
        png, width, height, opaque = normalise_image(raw_bytes)
    except ValueError as exc:
        return {"ok": False, "error": "invalid", "message": str(exc)}

    key = _asset_key(user.id)
    if not _store(session, key, png, uploaded_by=user.telegram_id):
        return {"ok": False, "error": "storage",
                "message": "I could not save that image. Please try again."}

    request = TeamLogoRequest(
        user_id=user.id, telegram_id=user.telegram_id,
        team_name=(user.team_name or None), asset_key=key, file_id=file_id,
        byte_size=len(png), width=width, height=height, status=STATUS_PENDING)
    session.add(request)
    session.flush()             # the admin DM needs the id before the commit
    invalidate_cache(user.id)
    return {"ok": True, "request": request, "png": png, "opaque": opaque}


def approve_request(session, request, *, reviewer=None):
    """Apply a pending crest to its owner's team. Caller commits."""
    from models import User
    if request is None or request.status != STATUS_PENDING:
        return {"ok": False, "error": "not_pending"}
    user = session.query(User).filter(User.id == request.user_id).first()
    if user is None:
        request.status = STATUS_CANCELLED
        request.review_note = "The account no longer exists."
        request.reviewed_by = str(reviewer or "")[:80] or None
        request.decided_at = datetime.utcnow()
        return {"ok": False, "error": "no_user"}

    previous = user.team_logo_asset_key
    user.team_logo_asset_key = request.asset_key
    user.team_logo_file_id = request.file_id
    user.team_logo_updated_at = datetime.utcnow()
    request.status = STATUS_APPROVED
    request.reviewed_by = str(reviewer or "")[:80] or None
    request.decided_at = datetime.utcnow()
    # Only now is the old crest safe to forget: until this point it was still
    # the one being drawn.
    if previous and previous != request.asset_key:
        _forget(session, previous)
    invalidate_cache(user.id, user.team_name)
    return {"ok": True, "request": request, "user": user}


def reject_request(session, request, *, reviewer=None, note=None):
    """Refuse a pending crest, with the reason the owner will be shown."""
    if request is None or request.status != STATUS_PENDING:
        return {"ok": False, "error": "not_pending"}
    request.status = STATUS_REJECTED
    request.review_note = (str(note or "").strip()[:300] or None)
    request.reviewed_by = str(reviewer or "")[:80] or None
    request.decided_at = datetime.utcnow()
    _forget(session, request.asset_key)
    request.asset_key = None
    invalidate_cache(request.user_id)
    return {"ok": True, "request": request}


def cancel_request(session, request, *, by_owner=True):
    """The owner withdrew it before anyone looked."""
    if request is None or request.status != STATUS_PENDING:
        return {"ok": False, "error": "not_pending"}
    request.status = STATUS_CANCELLED
    request.review_note = "Withdrawn by the owner." if by_owner else None
    request.decided_at = datetime.utcnow()
    _forget(session, request.asset_key)
    request.asset_key = None
    invalidate_cache(request.user_id)
    return {"ok": True, "request": request}


def remove_logo(session, user):
    """Clear an approved crest. Needs no review — removing shows nobody anything."""
    had = bool(user.team_logo_asset_key)
    _forget(session, user.team_logo_asset_key)
    user.team_logo_asset_key = None
    user.team_logo_file_id = None
    user.team_logo_updated_at = datetime.utcnow()
    invalidate_cache(user.id, user.team_name)
    return had


# ══════════════════════════════════════════════════════════════════════
# What the card renderers call
# ══════════════════════════════════════════════════════════════════════

# Card rendering is hot — three cards an innings, each looking up two teams —
# and a crest changes about once in a user's lifetime, so the bytes are cached
# by team name with a short TTL. The TTL alone would be enough; the explicit
# invalidation on every write just means an approval shows up immediately
# rather than within the minute.
_CACHE_TTL = 300
_cache = {}


def invalidate_cache(user_id=None, team_name=None):
    if user_id is None and team_name is None:
        _cache.clear()
        return
    for key in (("u", user_id), ("t", _norm_name(team_name))):
        _cache.pop(key, None)


def _norm_name(name):
    # Team names are free text, so match on case and spacing the way a human
    # would read them as "the same team".
    return re.sub(r"\s+", " ", str(name or "")).strip().lower() or None


def _cached(key, loader):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[0] > now:
        return hit[1]
    value = loader()
    _cache[key] = (now + _CACHE_TTL, value)
    return value


def logo_png_for_user(session, user_id):
    """The approved crest bytes for one user, or ``None``."""
    if not user_id:
        return None

    def load():
        from models import User
        user = session.query(User).filter(User.id == user_id).first()
        return logo_bytes_for_key(user.team_logo_asset_key) if user else None

    return _cached(("u", user_id), load)


def logo_png_for_team_name(session, team_name):
    """The approved crest bytes for whoever owns this team name, or ``None``.

    The scorecards carry team *names*, not user ids — that is what the stored
    payloads have held since long before crests existed — so the lookup goes
    through the name. Two users with the same team name is possible and rare;
    the most recently approved crest wins, which at least stays stable rather
    than alternating between them.
    """
    name = _norm_name(team_name)
    if not name:
        return None

    def load():
        from models import User
        from sqlalchemy import func
        user = (session.query(User)
                .filter(User.team_logo_asset_key.isnot(None),
                        func.lower(func.trim(User.team_name)) == name)
                .order_by(User.team_logo_updated_at.desc())
                .first())
        return logo_bytes_for_key(user.team_logo_asset_key) if user else None

    return _cached(("t", name), load)
