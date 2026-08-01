"""Durable storage for website uploads.

The app runs on a host with an ephemeral filesystem: everything written under
``data/`` is gone on the next deploy. That used to mean re-uploading every card
template, font, country flag and wizard image by hand after each release.

The database is the one store that survives a redeploy unconditionally and
needs no extra configuration, so every website upload is written twice: to disk
(the fast path every renderer already reads) and to ``stored_assets`` (the copy
that refills the disk). Nothing needs to be uploaded twice, ever.

Two ways a file comes back:

* **On boot** — :func:`restore_missing` writes back every stored asset whose
  file is absent, so a fresh container is warm before the first request.
* **On demand** — :func:`ensure` re-materialises a single file the moment
  something reads it and finds it missing, so a container that booted before an
  upload still self-heals.

This complements the Telegram storage channel rather than replacing it: where
``STORAGE_CHAT_ID`` is configured, that mirror still runs. This one just works
without it.
"""

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories whose contents are website uploads worth keeping. A path outside
# these is refused, so this can never be pointed at arbitrary files on disk.
DURABLE_ROOTS = (
    "data/card_templates",     # blank cards, shared and per-variant fonts
    "data/country_flags",      # flag PNGs used on every generated card
    "data/career_steps",       # /cmucareer wizard artwork
    "data/player_portraits",   # per-player cutouts + the global fallback
    "data/player_images",      # full custom card art
    "data/event_media",        # milestone media and sounds
)

# Anything larger is a mistake rather than an upload — the upload validators cap
# well below this, so this is just a backstop against bloating the database.
MAX_ASSET_BYTES = 8 * 1024 * 1024

# Files that are runtime state or documentation rather than uploaded artwork.
_SKIP_NAMES = {"state.json", ".gitkeep", ".gitignore"}
_SKIP_EXTENSIONS = {".md", ".txt", ".json"}


def _relative_key(path):
    """Project-relative, forward-slashed key for a path, or ``None``.

    ``None`` means "not something we store": outside the project, outside the
    durable roots, or a runtime-state file.
    """
    try:
        absolute = os.path.abspath(path)
        relative = os.path.relpath(absolute, PROJECT_ROOT)
    except (TypeError, ValueError):
        return None
    if relative.startswith(os.pardir):
        return None
    key = relative.replace(os.sep, "/")
    basename = os.path.basename(key)
    if basename in _SKIP_NAMES:
        return None
    if os.path.splitext(basename)[1].lower() in _SKIP_EXTENSIONS:
        return None
    if not any(key == root or key.startswith(root + "/") for root in DURABLE_ROOTS):
        return None
    return key


def absolute_path(key):
    """Absolute on-disk path for a stored key."""
    return os.path.join(PROJECT_ROOT, key.replace("/", os.sep))


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _session():
    from database import get_session
    return get_session()


# ── Writing ─────────────────────────────────────────────────────────────────

def put(path, data=None, uploaded_by=None, content_type=None):
    """Store a file's bytes so a redeploy can bring it back.

    ``data`` may be passed to avoid a re-read when the caller already has the
    bytes. Best-effort by design: a storage failure must never fail the upload
    the admin just made, because the file is already safely on disk.
    """
    key = _relative_key(path)
    if not key:
        return False
    try:
        if data is None:
            with open(path, "rb") as handle:
                data = handle.read()
    except OSError:
        logger.exception("asset_store could not read %s", path)
        return False
    if not data:
        return False
    if len(data) > MAX_ASSET_BYTES:
        logger.warning("asset_store skipping %s — %.1f MB is over the %.0f MB cap",
                       key, len(data) / 1024 / 1024, MAX_ASSET_BYTES / 1024 / 1024)
        return False

    from models import StoredAsset
    session = _session()
    try:
        row = session.query(StoredAsset).filter(StoredAsset.key == key).first()
        if row is None:
            row = StoredAsset(key=key)
            session.add(row)
        row.filename = os.path.basename(key)
        row.content_type = content_type
        row.data = data
        row.byte_size = len(data)
        row.sha256 = _digest(data)
        if uploaded_by:
            row.updated_by = str(uploaded_by)[:100]
        session.commit()
        logger.info("asset_store saved %s (%.1f KB)", key, len(data) / 1024)
        return True
    except Exception:
        session.rollback()
        logger.exception("asset_store could not save %s", key)
        return False
    finally:
        session.close()


def drop(path):
    """Forget a file, so a restore does not resurrect something just deleted."""
    key = _relative_key(path)
    if not key:
        return False
    from models import StoredAsset
    session = _session()
    try:
        deleted = (session.query(StoredAsset)
                   .filter(StoredAsset.key == key)
                   .delete(synchronize_session=False))
        session.commit()
        return bool(deleted)
    except Exception:
        session.rollback()
        logger.exception("asset_store could not drop %s", key)
        return False
    finally:
        session.close()


def drop_prefix(prefix):
    """Forget every stored file under a key prefix. Returns how many went."""
    from models import StoredAsset
    session = _session()
    try:
        deleted = (session.query(StoredAsset)
                   .filter(StoredAsset.key.like(f"{prefix}%"))
                   .delete(synchronize_session=False))
        session.commit()
        return deleted
    except Exception:
        session.rollback()
        logger.exception("asset_store could not drop prefix %s", prefix)
        return 0
    finally:
        session.close()


# ── Reading back ────────────────────────────────────────────────────────────

def _write_out(key, data):
    path = absolute_path(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path
    except OSError:
        logger.exception("asset_store could not write %s back to disk", key)
        return None


def ensure(path):
    """Re-create one missing file from the database. Returns True if it is there.

    Callers use this on a read miss, so a container that booted before an upload
    still heals itself without a restart.
    """
    if path and os.path.isfile(path):
        return True
    key = _relative_key(path)
    if not key:
        return False
    from models import StoredAsset
    session = _session()
    try:
        row = session.query(StoredAsset).filter(StoredAsset.key == key).first()
        if not row or not row.data:
            return False
        return _write_out(key, row.data) is not None
    except Exception:
        logger.exception("asset_store could not restore %s", key)
        return False
    finally:
        session.close()


def ensure_dir(root):
    """Re-create every stored file under one durable root that is missing.

    Used where a caller scans a directory (the flag list, the face templates)
    rather than asking for one known path.
    """
    prefix = root.rstrip("/") + "/"
    from models import StoredAsset
    session = _session()
    restored = 0
    try:
        rows = (session.query(StoredAsset)
                .filter(StoredAsset.key.like(f"{prefix}%")).all())
        for row in rows:
            if os.path.isfile(absolute_path(row.key)):
                continue
            if row.data and _write_out(row.key, row.data):
                restored += 1
    except Exception:
        logger.exception("asset_store could not restore %s", root)
    finally:
        session.close()
    return restored


def restore_missing():
    """Write back every stored asset whose file is absent. Returns the count.

    Called once at start-up so a fresh container is warm before it serves
    anything. Safe to call repeatedly: files already on disk are left alone.
    """
    from models import StoredAsset
    session = _session()
    restored = 0
    try:
        rows = session.query(StoredAsset).all()
        for row in rows:
            path = absolute_path(row.key)
            if os.path.isfile(path):
                continue
            if row.data and _write_out(row.key, row.data):
                restored += 1
        if restored:
            logger.info("asset_store restored %s uploaded file(s) from the database",
                        restored)
        return restored
    except Exception:
        logger.exception("asset_store restore_missing failed")
        return restored
    finally:
        session.close()


def adopt_existing():
    """Store any on-disk upload that is not in the database yet.

    This is what makes the feature retroactive: files uploaded before the store
    existed (or committed into the repo) are picked up on the next start, so
    nothing has to be re-uploaded even once.
    """
    from models import StoredAsset
    session = _session()
    known = set()
    try:
        known = {row[0] for row in session.query(StoredAsset.key).all()}
    except Exception:
        logger.exception("asset_store could not list known assets")
        return 0
    finally:
        session.close()

    adopted = 0
    for root in DURABLE_ROOTS:
        directory = os.path.join(PROJECT_ROOT, root.replace("/", os.sep))
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            path = os.path.join(directory, entry)
            if not os.path.isfile(path):
                continue
            key = _relative_key(path)
            if not key or key in known:
                continue
            if put(path):
                adopted += 1
    if adopted:
        logger.info("asset_store adopted %s existing upload(s)", adopted)
    return adopted


def sync_on_boot():
    """Adopt anything new on disk, then refill anything missing from it."""
    adopted = adopt_existing()
    restored = restore_missing()
    return {"adopted": adopted, "restored": restored}


# ── Reporting ───────────────────────────────────────────────────────────────

def usage():
    """``{count, bytes, by_root}`` so the website can show what is being kept."""
    from models import StoredAsset
    session = _session()
    try:
        rows = session.query(StoredAsset.key, StoredAsset.byte_size).all()
    except Exception:
        logger.exception("asset_store usage lookup failed")
        return {"count": 0, "bytes": 0, "by_root": {}}
    finally:
        session.close()

    by_root = {}
    total = 0
    for key, size in rows:
        size = size or 0
        total += size
        root = key.rsplit("/", 1)[0]
        entry = by_root.setdefault(root, {"count": 0, "bytes": 0})
        entry["count"] += 1
        entry["bytes"] += size
    return {"count": len(rows), "bytes": total, "by_root": by_root}


def list_keys(prefix=None):
    """Stored keys, optionally under one prefix — used by the website listing."""
    from models import StoredAsset
    session = _session()
    try:
        query = session.query(StoredAsset.key).order_by(StoredAsset.key)
        if prefix:
            query = query.filter(StoredAsset.key.like(f"{prefix}%"))
        return [row[0] for row in query.all()]
    except Exception:
        logger.exception("asset_store key listing failed")
        return []
    finally:
        session.close()
