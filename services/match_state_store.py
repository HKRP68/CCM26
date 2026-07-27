"""Match state persistence — single source of truth for in-progress matches.

Design:
  - State is stored in the `match_state` table as JSON. The DB stays the
    durable source of truth: every mutation commits synchronously.
  - In-memory cache (context.bot_data["ms_<mid>"]) is kept for performance.
  - A process-wide write-through cache (see "shared live-state cache" below)
    sits beneath the per-ctx tier so the Flask Mini App threads — which use a
    throwaway dummy ctx — also get memory-speed reads. The bot and Flask run
    in the SAME process (Flask is a background thread of `python bot.py`), so
    one module-level cache serves both.
  - Every state mutation goes through save_state() which writes to ALL tiers.
  - On any callback, get_state() reads from memory first, falls back to DB.
  - Bot restarts/redeploys preserve all in-progress matches: a new process
    starts with an empty cache and read-through repopulates it from the DB.
    Entries auto-revalidate against the DB after MATCH_STATE_CACHE_TTL seconds
    (default 3.0) to bound staleness during rolling-redeploy overlap; set
    MATCH_STATE_CACHE_TTL=0 to disable the shared cache entirely (every read
    goes to the DB, exactly the pre-cache behavior).

Action constants (next_action enum):
  PICK_DELIVERY        — bowler picks variation (or spinner picks delivery directly)
  PICK_LENGTH          — bowler picks length after picking variation (pacers only)
  PICK_SHOT            — batsman picks shot
  PICK_NEW_BATSMAN     — wicket fell, batting side picks next batsman
  PICK_NEW_BOWLER      — over ended, bowling side picks next bowler
  PICK_OPENERS         — innings start, batting side picks its two openers
  PICK_OPENING_BOWLER  — innings start, bowling side picks who bowls over 1
  INNINGS_BREAK        — innings ended, transition to 2nd innings
  COMPLETED            — match finished
"""

import json
import logging
import os
import threading
import time as _t
from datetime import datetime
from database import get_session
from models import MatchState
from services.perf_log import perf_timed

logger = logging.getLogger(__name__)

# Action constants (single canonical set)
A_PICK_DELIVERY = "PICK_DELIVERY"
A_PICK_LENGTH = "PICK_LENGTH"
A_PICK_SHOT = "PICK_SHOT"
A_PICK_NEW_BATSMAN = "PICK_NEW_BATSMAN"
A_PICK_NEW_BOWLER = "PICK_NEW_BOWLER"
# Innings-start selections. These exist because an innings break waits on a
# human pick that is NOT a delivery: pointing next_action at PICK_DELIVERY
# through the break made /resume (and the heartbeat) bowl a phantom ball with
# the previous innings' bowler, who by then belongs to the batting side.
A_PICK_OPENERS = "PICK_OPENERS"
A_PICK_OPENING_BOWLER = "PICK_OPENING_BOWLER"
A_INNINGS_BREAK = "INNINGS_BREAK"
A_COMPLETED = "COMPLETED"

# Challenge League over-by-over "approach" mode actions
A_PICK_CIPL_BOWLER = "PICK_CIPL_BOWLER"      # bowling side picks bowler for the over
A_PICK_BOWL_APPROACH = "PICK_BOWL_APPROACH"  # bowling side picks bowling approach
A_PICK_BAT_APPROACH = "PICK_BAT_APPROACH"    # batting side picks batting approach


def _mem_key(mid):
    return f"ms_{mid}"


# ════════════════════════════════════════════════════════════════════
# Shared live-state cache (process-wide, beneath the per-ctx tier)
# ════════════════════════════════════════════════════════════════════
#
# Stores the SERIALIZED JSON string (not the dict): readers json.loads their
# own fresh copy, which preserves both caller mutation isolation and the
# str-key normalization that a DB round-trip produces. Entries hold the row's
# version so a slower concurrent writer can never overwrite a newer commit
# (monotonic merge). Updated only AFTER a successful DB commit.

_CACHE_TTL = float(os.getenv("MATCH_STATE_CACHE_TTL", "3.0"))  # 0 disables
_CACHE_LOCK = threading.Lock()
_CACHE = {}  # mid -> {"state_json", "next_action", "ball_seq", "version", "fetched_at"}


def _cache_enabled():
    return _CACHE_TTL > 0


def clear_cache():
    """Drop every cached entry (tests / ops)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _cache_fresh(mid):
    """Return a snapshot of the entry for `mid` if it is within TTL, else
    None. Copied under the lock so concurrent writers can't tear a read."""
    if not _cache_enabled():
        return None
    with _CACHE_LOCK:
        entry = _CACHE.get(mid)
        if entry and (_t.monotonic() - entry["fetched_at"]) <= _CACHE_TTL:
            return dict(entry)
    return None


def _cache_store_row(mid, ms):
    """Full refresh from a freshly-read MatchState row."""
    if not _cache_enabled():
        return
    entry = {
        "state_json": ms.state_json,
        "next_action": getattr(ms, "next_action", None),
        "ball_seq": getattr(ms, "ball_seq", 0) or 0,
        "version": getattr(ms, "version", 0) or 0,
        "fetched_at": _t.monotonic(),
    }
    with _CACHE_LOCK:
        cur = _CACHE.get(mid)
        if cur and (cur["version"] or 0) > (entry["version"] or 0):
            return  # a newer write already landed; keep it
        _CACHE[mid] = entry


def _cache_apply_write(mid, *, state_json=None, next_action=None,
                       ball_seq=None, version=None):
    """Merge a just-committed write into the cache.

    Partial payloads (e.g. set_next_action, increment_ball_seq) only merge
    into an existing entry — with no entry the next read repopulates fully.
    A write carrying a version older than the cached one is dropped so a
    slow concurrent writer can't roll the cache back.
    """
    if not _cache_enabled():
        return
    with _CACHE_LOCK:
        entry = _CACHE.get(mid)
        if entry is None:
            if state_json is None or version is None:
                return  # partial update with nothing to merge into
            _CACHE[mid] = {
                "state_json": state_json,
                "next_action": next_action,
                "ball_seq": ball_seq if ball_seq is not None else 0,
                "version": version,
                "fetched_at": _t.monotonic(),
            }
            return
        if version is not None and (entry["version"] or 0) > version:
            return  # stale writer; keep the newer cached commit
        if state_json is not None:
            entry["state_json"] = state_json
        if next_action is not None:
            entry["next_action"] = next_action
        if ball_seq is not None:
            entry["ball_seq"] = ball_seq
        if version is not None:
            entry["version"] = version
        entry["fetched_at"] = _t.monotonic()


def _cache_evict(mid):
    with _CACHE_LOCK:
        _CACHE.pop(mid, None)


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


# Per-player stat maps are keyed by ``roster_id``. JSON object keys are always
# strings, so ``str(roster_id)`` is the only key form that survives a DB
# round-trip unchanged — and it is what create_match_state / cipl_match write.
# Any int key written by a live handler therefore turns into a string on the
# next read, and ``stats[p["roster_id"]]`` starts missing: a reloaded match
# forgets who is out and shows every batter as 0(0). Worse, a caller that then
# re-creates the missing row under the int key ends up with two rows for one
# player, one of which never updates. Canonicalize to the string form on the
# way out of JSON so every tier agrees.
_ROSTER_KEYED_STATS = (
    "bat_stats", "bowl_stats", "inn1_bat_stats", "inn1_bowl_stats",
)


# Dismissal is sticky: a batter who is out stays out no matter which row of a
# duplicate pair carries the bigger tally, so these fields survive the merge
# even when they sit on the row that loses.
_STICKY_STAT_FIELDS = ("out", "how_out", "bowled_by")


def _merge_stat_rows(existing, incoming):
    """Fold a duplicate int-keyed row into its string-keyed twin.

    Only reachable for states written before keys were canonicalized, where one
    of the pair carries the real tallies and the other is a leftover zeroed row.
    Keeping the busier row avoids wiping a part-finished innings mid-match; the
    sticky fields are then carried across so the merge can't resurrect a batter
    who was dismissed without facing a ball (a non-striker run out has a
    dismissal but no balls or runs to weigh).
    """
    def _work(row):
        if not isinstance(row, dict):
            return -1
        return (row.get("balls", 0) or 0) + (row.get("runs", 0) or 0)

    winner = incoming if _work(incoming) > _work(existing) else existing
    if not isinstance(winner, dict) or winner.get("out"):
        return winner
    loser = existing if winner is incoming else incoming
    if isinstance(loser, dict) and loser.get("out"):
        for field in _STICKY_STAT_FIELDS:
            if field in loser:
                winner[field] = loser[field]
    return winner


def _str_keys(mapping):
    """Re-key a stat dict by ``str(roster_id)``, merging any int/str duplicates."""
    out = {}
    for k, v in mapping.items():
        key = str(k)
        if key in out:
            v = _merge_stat_rows(out[key], v)
        out[key] = v
    return out


def _normalize_state(state):
    """Canonicalize roster-keyed stat maps to the JSON-stable string form."""
    if not isinstance(state, dict):
        return state
    for key in _ROSTER_KEYED_STATS:
        stats = state.get(key)
        if isinstance(stats, dict) and stats:
            state[key] = _str_keys(stats)
    return state


def _deserialize(json_str):
    return _normalize_state(json.loads(json_str)) if json_str else {}


@perf_timed("store.get_state")
def get_state(ctx, mid):
    """Get match state. Checks ctx memory, then the shared cache, then the DB.

    On a fallback hit, repopulates the faster tiers for future reads.
    """
    # Memory check
    mem = ctx.bot_data.get(_mem_key(mid))
    if mem:
        return mem

    # Shared-cache check — parse a fresh dict so callers mutate in isolation,
    # exactly like a DB read would give them.
    entry = _cache_fresh(mid)
    if entry:
        state = _deserialize(entry["state_json"])
        ctx.bot_data[_mem_key(mid)] = state
        return state

    # DB fallback
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if not ms:
            _cache_evict(mid)  # never cache misses
            return None
        state = _deserialize(ms.state_json)
        _cache_store_row(mid, ms)
        # Re-cache in memory
        ctx.bot_data[_mem_key(mid)] = state
        return state
    except Exception:
        logger.exception(f"get_state DB fallback failed for match {mid}")
        return None
    finally:
        session.close()


@perf_timed("store.get_next_action")
def get_next_action(ctx, mid):
    """Read the canonical next_action pointer. Returns None if no record."""
    entry = _cache_fresh(mid)
    if entry:
        return entry["next_action"]
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if not ms:
            _cache_evict(mid)
            return None
        # Cache the whole row so the same request's get_state is a hit too.
        _cache_store_row(mid, ms)
        return ms.next_action
    except Exception:
        return None
    finally:
        session.close()


@perf_timed("store.save_state")
def save_state(ctx, mid, state, next_action=None, last_prompt_msg_id=None,
               bump_ball_seq=False):
    """Save state to memory + DB atomically.

    Args:
      ctx: PTB context
      mid: match id
      state: state dict
      next_action: optional update to state-machine pointer
      last_prompt_msg_id: optional id of the message showing current buttons
      bump_ball_seq: also increment ball_seq in the SAME commit (saves the
        separate increment_ball_seq round-trip on the ball hot path)

    Returns the new ball_seq when bump_ball_seq is True (else None).
    """
    # Canonicalize roster-keyed stat maps in place before anything caches them,
    # so the in-memory tier and the DB agree on key form. Without this, a live
    # handler's int keys survive in bot_data and only turn into strings on the
    # next cold read — a difference the readers would trip over.
    _normalize_state(state)

    # Update memory immediately (fast path)
    ctx.bot_data[_mem_key(mid)] = state

    serialized = _serialize(state)
    new_seq = None

    # Persist to DB
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if not ms:
            effective_na = next_action or A_PICK_DELIVERY
            new_version = 1
            new_seq = 1 if bump_ball_seq else 0
            ms = MatchState(
                match_id=mid,
                state_json=serialized,
                next_action=effective_na,
                version=new_version, ball_seq=new_seq,
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
            ms.state_json = serialized
            if next_action is not None:
                ms.next_action = next_action
            effective_na = ms.next_action
            if last_prompt_msg_id is not None:
                ms.last_prompt_msg_id = last_prompt_msg_id
            new_version = (ms.version or 0) + 1
            ms.version = new_version
            if bump_ball_seq:
                new_seq = (ms.ball_seq or 0) + 1
                ms.ball_seq = new_seq
            ms.last_modified = datetime.utcnow()
        session.commit()
        _cache_apply_write(mid, state_json=serialized, next_action=effective_na,
                           ball_seq=new_seq, version=new_version)
    except Exception:
        session.rollback()
        logger.exception(f"save_state DB write failed for match {mid}")
    finally:
        session.close()
    return new_seq if bump_ball_seq else None


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

            serialized = _serialize(state)
            updated = (session.query(MatchState)
                       .filter(MatchState.match_id == mid,
                               MatchState.version == version)
                       .update({
                           MatchState.state_json: serialized,
                           MatchState.version: version + 1,
                           MatchState.last_modified: datetime.utcnow(),
                       }, synchronize_session=False))
            if updated:
                session.commit()
                _cache_apply_write(mid, state_json=serialized,
                                   version=version + 1)
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


class CasAbort(Exception):
    """Raise from an `update_state_cas` mutator to bail out without writing —
    e.g. validation failed against the freshly-read state, so nothing changed
    and the row should be left untouched. Carries the result to return as-is."""

    def __init__(self, result):
        super().__init__("update_state_cas aborted")
        self.result = result


def update_state_cas(ctx, mid, mutator, max_retries=5):
    """Read-modify-write match state under optimistic concurrency control.

    Generalizes the version-guarded retry pattern used by save_autoplay_users
    so independent submissions (e.g. the batting side picking openers and the
    bowling side picking a bowler at the same time) can't silently clobber
    each other's flags by overwriting a stale whole-state snapshot.

    `mutator(state)` is called with a freshly-read, mutable state dict. It
    should validate against THIS state (not any older copy), mutate it in
    place, and return `(result, next_action_or_None)`. Raise `CasAbort(result)`
    to signal "validation failed — persist nothing" and return `result` as-is.

    Retries up to `max_retries` times if another writer commits between our
    read and write. Returns the mutator's `result`, or `None` if the match
    state row is missing or all retries were exhausted on version conflicts.
    """
    for attempt in range(max_retries):
        session = get_session()
        try:
            ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
            if not ms:
                return None

            version = ms.version or 0
            state = _deserialize(ms.state_json)

            try:
                result, next_action = mutator(state)
            except CasAbort as abort:
                return abort.result

            serialized = _serialize(state)
            values = {
                MatchState.state_json: serialized,
                MatchState.version: version + 1,
                MatchState.last_modified: datetime.utcnow(),
            }
            if next_action is not None:
                values[MatchState.next_action] = next_action

            updated = (session.query(MatchState)
                       .filter(MatchState.match_id == mid,
                               MatchState.version == version)
                       .update(values, synchronize_session=False))
            if updated:
                session.commit()
                # next_action=None merges as "leave cached pointer untouched".
                _cache_apply_write(mid, state_json=serialized,
                                   next_action=next_action,
                                   version=version + 1)
                ctx.bot_data[_mem_key(mid)] = state
                return result

            session.rollback()
            logger.info(
                "update_state_cas retrying for match %s after version conflict (%s/%s)",
                mid, attempt + 1, max_retries)
        except Exception:
            session.rollback()
            logger.exception("update_state_cas failed for match %s", mid)
            return None
        finally:
            session.close()

    logger.warning("update_state_cas gave up for match %s after %s retries",
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
            new_version = (ms.version or 0) + 1
            ms.version = new_version
            ms.last_modified = datetime.utcnow()
            session.commit()
            _cache_apply_write(mid, next_action=next_action,
                               version=new_version)
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
            # Capture pre-commit: expire_on_commit would otherwise refetch.
            new_seq = (ms.ball_seq or 0) + 1
            ms.ball_seq = new_seq
            ms.last_modified = datetime.utcnow()
            session.commit()
            _cache_apply_write(mid, ball_seq=new_seq)
            return new_seq
    except Exception:
        session.rollback()
    finally:
        session.close()
    return 0


def get_ball_seq(mid):
    entry = _cache_fresh(mid)
    if entry:
        return entry["ball_seq"]
    session = get_session()
    try:
        ms = session.query(MatchState).filter(MatchState.match_id == mid).first()
        if not ms:
            _cache_evict(mid)
            return 0
        _cache_store_row(mid, ms)
        return ms.ball_seq or 0
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
    _cache_evict(mid)
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
