"""Flask-side access to live match state.

The bot drives matches through services.match_state_store using its PTB
`context` (which carries an in-memory cache in ctx.bot_data). Flask has no
such context, so this module provides a lightweight dummy-ctx shim whose
`bot_data` is a throwaway dict. All reads/writes therefore go straight to the
DB-backed match_state table — which is exactly what we want for the Mini App,
since the DB is the single source of truth shared by both processes.

IMPORTANT: when Flask saves state, the bot's in-memory cache for that match
becomes stale. The bot already guards against this by falling back to the DB
on cache miss, but to be safe the bot's heartbeat / handlers re-read from the
store before acting. For Mini-App-driven turns we bump `version` and
`ball_seq` so the bot can detect external changes.
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


def save_state(mid, state, next_action=None, last_prompt_msg_id=None):
    """Persist state to the DB. A throwaway ctx means only the DB is updated;
    the bot will pick up changes via its DB fallback."""
    _store.save_state(_DummyCtx(), mid, state,
                      next_action=next_action,
                      last_prompt_msg_id=last_prompt_msg_id)


def set_next_action(mid, next_action, last_prompt_msg_id=None):
    _store.set_next_action(_DummyCtx(), mid, next_action,
                           last_prompt_msg_id=last_prompt_msg_id)


def bump_ball_seq(mid):
    return _store.increment_ball_seq(_DummyCtx(), mid)


def get_ball_seq(mid):
    return _store.get_ball_seq(mid)
