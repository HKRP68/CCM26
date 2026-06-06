"""Match state persistence — single source of truth for in-progress matches.

Design:
  - State is stored in the `match_state` table as JSON.
  - In-memory cache (context.bot_data["ms_<mid>"]) is kept for performance.
  - Every state mutation goes through save_state() which writes to BOTH.
  - On any callback, get_state() reads from memory first, falls back to DB.
  - Bot restarts/redeploys preserve all in-progress matches.

Action constants (next_action enum):
  PICK_DELIVERY      — bowler picks variation (or spinner picks delivery directly)
  PICK_LENGTH        — bowler picks length after picking variation (pacers only)
  PICK_SHOT          — batsman picks shot
  PICK_NEW_BATSMAN   — wicket fell, batting side picks next batsman
  PICK_NEW_BOWLER    — over ended, bowling side picks next bowler
  INNINGS_BREAK      — innings ended, transition to 2nd innings
  COMPLETED          — match finished
"""

import json
import logging
from datetime import datetime
from database import get_session
from models import MatchState

logger = logging.getLogger(__name__)

# Action constants (single canonical set)
A_PICK_DELIVERY = "PICK_DELIVERY"
A_PICK_LENGTH = "PICK_LENGTH"
A_PICK_SHOT = "PICK_SHOT"
A_PICK_NEW_BATSMAN = "PICK_NEW_BATSMAN"
A_PICK_NEW_BOWLER = "PICK_NEW_BOWLER"
A_INNINGS_BREAK = "INNINGS_BREAK"
A_COMPLETED = "COMPLETED"


def _mem_key(mid):
    return f"ms_{mid}"


def _serialize(state):
    """Convert state dict to JSON-safe form. Some fields (like full XI dicts)
    contain only primitives, so this is mostly a passthrough.
    """
    try:
        return json.dumps(state, default=str)
    except (TypeError, ValueError):
        logger.exception("Failed to serialize state — falling back to repr")
        # Last resort: repr-coerce the unserialiable parts
        return json.dumps({k: (v if isinstance(v, (int, float, str, bool, list, dict, type(None))) else repr(v))
                           for k, v in state.items()}, default=str)


def _deserialize(json_str):
    return json.loads(json_str) if json_str else {}


def get_state(ctx, mid):
    """Get match state. Checks memory first, falls back to DB.

    On DB-fallback hit, repopulates memory cache for future reads.
    """
    # Memory check
    mem = ctx.bot_data.get(_mem_key(mid))
    if mem:
        return mem

    # DB fallback
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if not ms:
            return None
        state = _deserialize(ms.state_json)
        # Re-cache in memory
        ctx.bot_data[_mem_key(mid)] = state
        return state
    except Exception:
        logger.exception(f"get_state DB fallback failed for match {mid}")
        return None
    finally:
        session.close()


def get_next_action(ctx, mid):
    """Read the canonical next_action pointer. Returns None if no record."""
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        return ms.next_action if ms else None
    except Exception:
        return None
    finally:
        session.close()


def save_state(ctx, mid, state, next_action=None, last_prompt_msg_id=None):
    """Save state to memory + DB atomically.

    Args:
      ctx: PTB context
      mid: match id
      state: state dict
      next_action: optional update to state-machine pointer
      last_prompt_msg_id: optional id of the message showing current buttons
    """
    # Update memory immediately (fast path)
    ctx.bot_data[_mem_key(mid)] = state

    # Persist to DB
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if not ms:
            ms = MatchState(
                match_id=mid,
                state_json=_serialize(state),
                next_action=next_action or A_PICK_DELIVERY,
                version=1, ball_seq=0,
                last_modified=datetime.utcnow(),
                last_prompt_msg_id=last_prompt_msg_id,
            )
            session.add(ms)
            # Bump active-match flag so heartbeat knows to do work
            try:
                from services.match_heartbeat_flags import increment_active_matches
                increment_active_matches(ctx)
            except Exception:
                pass
        else:
            ms.state_json = _serialize(state)
            if next_action is not None:
                ms.next_action = next_action
            if last_prompt_msg_id is not None:
                ms.last_prompt_msg_id = last_prompt_msg_id
            ms.version = (ms.version or 0) + 1
            ms.last_modified = datetime.utcnow()
        session.commit()
    except Exception:
        session.rollback()
        logger.exception(f"save_state DB write failed for match {mid}")
    finally:
        session.close()


def save_autoplay_users(ctx, mid, user_id, active, max_retries=5):
    """Merge one participant's Autoplay status into the latest persisted state.

    Unlike save_state(), this intentionally updates only state["autoplay_users"]
    against a freshly-read DB row.  The Mini App can toggle Autoplay while a
    delivery/shot/selection request is still processing; rewriting the whole
    stale snapshot from the status request can otherwise roll back score, ball,
    wicket, or innings changes.  We use the MatchState.version token as an
    optimistic guard and retry if another writer commits between our read and
    write.

    Returns the updated state dict, or None if the match state row is missing.
    """
    user_key = str(user_id)

    for attempt in range(max_retries):
        session = get_session()
        try:
            ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
            if not ms:
                return None

            version = ms.version or 0
            state = _deserialize(ms.state_json)
            autoplay_users = dict(state.get("autoplay_users") or {})
            autoplay_users[user_key] = bool(active)
            state["autoplay_users"] = autoplay_users

            updated = (session.query(MatchState)
                       .filter(MatchState.match_id == mid,
                               MatchState.version == version)
                       .update({
                           MatchState.state_json: _serialize(state),
                           MatchState.version: version + 1,
                           MatchState.last_modified: datetime.utcnow(),
                       }, synchronize_session=False))
            if updated:
                session.commit()
                ctx.bot_data[_mem_key(mid)] = state
                return state

            session.rollback()
            logger.info(
                "save_autoplay_users retrying for match %s after version conflict (%s/%s)",
                mid, attempt + 1, max_retries)
        except Exception:
            session.rollback()
            logger.exception("save_autoplay_users DB write failed for match %s", mid)
            return None
        finally:
            session.close()

    logger.warning("save_autoplay_users gave up for match %s after %s retries",
                   mid, max_retries)
    return None


def set_next_action(ctx, mid, next_action, last_prompt_msg_id=None):
    """Lightweight: update only the next_action pointer (and optionally msg id).
    Use when the state dict hasn't changed but the pointer has."""
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if ms:
            ms.next_action = next_action
            if last_prompt_msg_id is not None:
                ms.last_prompt_msg_id = last_prompt_msg_id
            ms.version = (ms.version or 0) + 1
            ms.last_modified = datetime.utcnow()
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def increment_ball_seq(ctx, mid):
    """Bump the ball sequence counter. Used for callback idempotency."""
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if ms:
            ms.ball_seq = (ms.ball_seq or 0) + 1
            ms.last_modified = datetime.utcnow()
            session.commit()
            return ms.ball_seq
    except Exception:
        session.rollback()
    finally:
        session.close()
    return 0


def get_ball_seq(mid):
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        return ms.ball_seq if ms else 0
    except Exception:
        return 0
    finally:
        session.close()


def get_last_prompt_msg_id(mid):
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        return ms.last_prompt_msg_id if ms else None
    except Exception:
        return None
    finally:
        session.close()


def cleanup_state(ctx, mid):
    """Remove state from memory + DB. Called on match end / forfeit."""
    ctx.bot_data.pop(_mem_key(mid), None)
    # Clear other match-keyed entries
    for k in list(ctx.bot_data.keys()):
        if isinstance(k, str) and k.endswith(f"_{mid}"):
            ctx.bot_data.pop(k, None)
        if isinstance(k, str) and k == f"processing_{mid}":
            ctx.bot_data.pop(k, None)

    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if ms:
            session.delete(ms)
            session.commit()
            # Decrement active-match flag for heartbeat fast-path
            try:
                from services.match_heartbeat_flags import decrement_active_matches
                decrement_active_matches(ctx)
            except Exception:
                pass
    except Exception:
        session.rollback()
    finally:
        session.close()


def list_active_match_ids():
    """Return all match ids that still have state records (for recovery on startup)."""
    session = get_session()
    try:
        return [r[0] for r in session.query(MatchState.match_id).all()]
    except Exception:
        return []
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Concurrency: per-match async lock
# ════════════════════════════════════════════════════════════════════

# Single asyncio lock per match. Prevents concurrent shot processing
# from corrupting state. Cheaper and more correct than the bot_data flag.

import asyncio
_match_locks = {}


def get_match_lock(mid):
    if mid not in _match_locks:
        _match_locks[mid] = asyncio.Lock()
    return _match_locks[mid]


def release_match_lock(mid):
    """Drop the lock object when the match ends."""
    _match_locks.pop(mid, None)
