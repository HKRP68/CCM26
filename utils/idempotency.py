"""Shared idempotency guard for inline-button callbacks.

Rapid / duplicate taps on an inline button (fat-finger, client glitch, lag, or
a script) can otherwise apply a value-changing action more than once — e.g.
buying the same player 4-5 times, deducting coins and inserting duplicate roster
rows each time.

This module provides a thread-safe, TTL-based "claim once" guard. The bot runs
with concurrent_updates=16, so handler coroutines run across a thread pool; the
lock makes the check-and-set atomic across workers.

Usage at the very top of a callback, before any await / slow work::

    from utils.idempotency import claim_once, release

    key = f"buy_{q.message.chat_id}_{q.message.message_id}"
    if not claim_once(key):
        await q.answer("Already processing…")
        return
    ...
    # recoverable validation failure → release(key) so a legit retry works
    # terminal success → keep the claim (let it expire via TTL)

Key the guard on the *button message instance* (chat_id + message_id) for
actions that can legitimately repeat later (buy, market, packs, spins). The key
then dedups this physical button press without ever blocking a future, separate
press of a new button.
"""

import threading
import time

_LOCK = threading.Lock()
_GUARD: dict[str, float] = {}     # key -> monotonic claimed-at timestamp
_DEFAULT_TTL = 30.0               # seconds; > slowest path (pack-open render + sends)
_MAX_ENTRIES = 5000


def claim_once(key: str, ttl: float = _DEFAULT_TTL) -> bool:
    """Atomically claim ``key``.

    Returns True if this caller won the claim (no live claim younger than
    ``ttl`` existed), False if a duplicate is already in flight. Opportunistically
    evicts expired entries and hard-caps the dict at ``_MAX_ENTRIES``.
    """
    now = time.monotonic()
    with _LOCK:
        ts = _GUARD.get(key)
        if ts is not None and (now - ts) < ttl:
            return False
        _GUARD[key] = now
        if len(_GUARD) > _MAX_ENTRIES:
            cutoff = now - ttl
            expired = [k for k, t in _GUARD.items() if t < cutoff][: _MAX_ENTRIES // 2]
            for k in expired:
                _GUARD.pop(k, None)
        return True


def release(key: str) -> None:
    """Undo a claim so a legitimate retry after a recoverable failure (e.g.
    'insufficient coins') works immediately rather than waiting for the TTL."""
    with _LOCK:
        _GUARD.pop(key, None)


def in_progress(key: str, ttl: float = _DEFAULT_TTL) -> bool:
    """Read-only check: is there a live claim for ``key``?"""
    with _LOCK:
        ts = _GUARD.get(key)
    return ts is not None and (time.monotonic() - ts) < ttl
