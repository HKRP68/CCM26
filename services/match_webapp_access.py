"""Flask-side access to live match state.

The bot drives matches through services.match_state_store using its PTB
`context` (which carries an in-memory cache in ctx.bot_data). Flask has no
such context, so this module provides a lightweight dummy-ctx shim whose
`bot_data` is a throwaway dict. Reads/writes therefore land on the store's
process-wide shared cache (Flask runs as a thread inside the bot process)
with the DB-backed match_state table as the durable source of truth — every
save still commits synchronously, and cache entries revalidate against the
DB after a short TTL (MATCH_STATE_CACHE_TTL, 0 = disable caching).

IMPORTANT: when Flask saves state, the bot's per-ctx cache for that match
becomes stale. The bot already guards against this by falling back to the
shared cache/DB on miss, but to be safe the bot's heartbeat / handlers
re-read from the store before acting. For Mini-App-driven turns we bump
`version` and `ball_seq` so the bot can detect external changes.
"""

import logging

from services import match_state_store as _store

logger = logging.getLogger(__name__)


class _DummyCtx:
    """Minimal stand-in for a PTB context. Only `.bot_data` is used by the
    store, and we give it a fresh dict so memory-cache writes are harmless."""
    __slots__ = ("bot_data",)

    def __init__(self):
        self.bot_data = {}


def fresh_ctx():
    return _DummyCtx()


def get_state(mid):
    """Read live match state from the DB (memory cache is irrelevant here)."""
    return _store.get_state(_DummyCtx(), mid)


def get_next_action(mid):
    return _store.get_next_action(_DummyCtx(), mid)


def save_state(mid, state, next_action=None, last_prompt_msg_id=None,
               bump_ball_seq=False):
    """Persist state to the DB + shared cache; the bot picks up changes via
    the store's shared-cache/DB fallback. Returns the new ball_seq when
    bump_ball_seq is True (single-commit alternative to bump_ball_seq())."""
    return _store.save_state(_DummyCtx(), mid, state,
                             next_action=next_action,
                             last_prompt_msg_id=last_prompt_msg_id,
                             bump_ball_seq=bump_ball_seq)


def save_autoplay_users(mid, user_id, active):
    """Persist only the Autoplay participant map against latest DB state."""
    return _store.save_autoplay_users(_DummyCtx(), mid, user_id, active)


def update_state_cas(mid, mutator, max_retries=5):
    """Atomic read-modify-write guarded by MatchState.version (see
    match_state_store.update_state_cas). Use for concurrent-submission flows
    (e.g. openers/bowler selection) where overwriting a stale whole-state
    snapshot would silently drop the other side's update."""
    return _store.update_state_cas(_DummyCtx(), mid, mutator, max_retries=max_retries)


def set_next_action(mid, next_action, last_prompt_msg_id=None):
    _store.set_next_action(_DummyCtx(), mid, next_action,
                           last_prompt_msg_id=last_prompt_msg_id)


def bump_ball_seq(mid):
    return _store.increment_ball_seq(_DummyCtx(), mid)


def get_ball_seq(mid):
    return _store.get_ball_seq(mid)
