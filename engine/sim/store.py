"""Admin-edited stadiums and modifiers, kept in the database.

The shipped files (``data/stadiums.json``, ``config/sim_engine.json``) are the
defaults. Anything imported or edited from the admin site is saved here, in
``sim_engine_settings`` (``models.SimEngineSetting``), because the container's
filesystem is reset on every deploy and a balancing pass must survive that.

Two documents, by key:

* ``stadiums``  — the complete stadium list (replaces the file's list).
* ``modifiers`` — overrides deep-merged over ``config/sim_engine.json``.

Every save inserts a new row, so the table doubles as the history: the newest
row per key is live, ``restore`` re-saves an old one, and a row whose payload
is NULL means "back to the shipped file". Only the last ``KEEP`` versions per
key are kept.

Everything degrades to "no overrides" when the database is unavailable (the
CLI, unit tests without a DB), so the engine never depends on it.
"""

import json
import logging

logger = logging.getLogger(__name__)

KEYS = ("stadiums", "modifiers")
KEEP = 25


def _session():
    from database import get_session
    return get_session()


def _model():
    from models import SimEngineSetting
    return SimEngineSetting


def current(key, session=None):
    """The live payload for *key* (decoded JSON), or ``None`` for the file."""
    own = session is None
    try:
        s = session or _session()
    except Exception:
        return None
    try:
        M = _model()
        row = (s.query(M).filter(M.key == key)
               .order_by(M.created_at.desc(), M.id.desc()).first())
        if row is None or row.payload is None:
            return None
        return json.loads(row.payload)
    except Exception as exc:          # no table yet, no DB, bad JSON
        logger.debug("sim_engine store: %s unavailable: %s", key, exc)
        return None
    finally:
        if own:
            try:
                s.close()
            except Exception:
                pass


def save(key, payload, session, created_by=None, note=None):
    """Insert a new live version of *key*. ``payload=None`` resets to the file.

    The caller commits. Old versions beyond ``KEEP`` are pruned.
    """
    if key not in KEYS:
        raise ValueError(f"unknown settings key {key!r}")
    M = _model()
    row = M(key=key, payload=None if payload is None else json.dumps(payload, sort_keys=True),
            note=(note or "")[:300] or None, created_by=(created_by or "")[:100] or None)
    session.add(row)
    session.flush()
    old = (session.query(M).filter(M.key == key)
           .order_by(M.created_at.desc(), M.id.desc()).offset(KEEP).all())
    for r in old:
        session.delete(r)
    return row


def history(key, session, limit=KEEP):
    """``[{id, created_at, created_by, note, reset, size}]``, newest first."""
    M = _model()
    rows = (session.query(M).filter(M.key == key)
            .order_by(M.created_at.desc(), M.id.desc()).limit(limit).all())
    out = []
    for i, r in enumerate(rows):
        size = None
        if r.payload is not None:
            try:
                data = json.loads(r.payload)
                size = len(data) if isinstance(data, (list, dict)) else None
            except Exception:
                pass
        out.append({"id": r.id, "created_at": r.created_at, "created_by": r.created_by,
                    "note": r.note, "reset": r.payload is None, "size": size, "live": i == 0})
    return out


def version(version_id, session):
    """``(key, payload)`` of a stored version, or ``None``."""
    M = _model()
    r = session.get(M, version_id)
    if r is None:
        return None
    return r.key, (None if r.payload is None else json.loads(r.payload))


def restore(version_id, session, created_by=None):
    """Make an old version live again (as a new version). Returns the new row."""
    found = version(version_id, session)
    if found is None:
        raise ValueError(f"no such version {version_id}")
    key, payload = found
    return save(key, payload, session, created_by=created_by,
                note=f"restored version #{version_id}")


def reload_engine():
    """Drop the engine's caches so the next ball reads the new settings."""
    from engine.sim import config, stadium
    config.reload()
    stadium.reload()
