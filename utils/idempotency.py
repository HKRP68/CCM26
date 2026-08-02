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

``remember``/``recall`` are the same idea for request/response pairs: a Mini App
call that already succeeded can be replayed by its client (a dropped connection
on a phone network looks exactly like a failure to the caller) and must return
the reward it granted the first time rather than "ad required" — the ad is gone
and the reward is already in the account.
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
            # Drop expired keys first…
            cutoff = now - ttl
            for k in [k for k, t in _GUARD.items() if t < cutoff]:
                _GUARD.pop(k, None)
            # …then enforce the hard cap by evicting the oldest if still over,
            # so a burst of fresh keys can't grow the map without bound.
            overflow = len(_GUARD) - _MAX_ENTRIES
            if overflow > 0:
                oldest = sorted(_GUARD.items(), key=lambda kv: kv[1])[:overflow]
                for k, _ in oldest:
                    _GUARD.pop(k, None)
        return True


def release(key: str) -> None:
    """Undo a claim so a legitimate retry after a recoverable failure (e.g.
    'insufficient coins') works immediately rather than waiting for the TTL."""
    with _LOCK:
        _GUARD.pop(key, None)


_RESULTS: dict[str, tuple[float, object]] = {}   # key -> (stored-at, value)
_RESULT_TTL = 300.0               # 5 minutes; covers a user retrying by hand
_MAX_RESULTS = 2000


def remember(key: str, value, ttl: float = _RESULT_TTL) -> None:
    """Store the result of a completed, non-repeatable action under ``key``.

    In-process by design, like the rest of this module. Under multiple workers
    a retry can land elsewhere and miss the cache — which is why the durable
    half of the guarantee is the ad credit in the database, not this. This is
    what makes the common case (same worker, immediate retry) return the very
    same reward instead of a second one.
    """
    now = time.monotonic()
    with _LOCK:
        _RESULTS[key] = (now, value)
        if len(_RESULTS) > _MAX_RESULTS:
            cutoff = now - ttl
            for k in [k for k, (t, _v) in _RESULTS.items() if t < cutoff]:
                _RESULTS.pop(k, None)
            overflow = len(_RESULTS) - _MAX_RESULTS
            if overflow > 0:
                oldest = sorted(_RESULTS.items(), key=lambda kv: kv[1][0])[:overflow]
                for k, _ in oldest:
                    _RESULTS.pop(k, None)


def recall(key: str, ttl: float = _RESULT_TTL):
    """Return a remembered result for ``key``, or None if there isn't a live one."""
    with _LOCK:
        record = _RESULTS.get(key)
        if not record:
            return None
        stored_at, value = record
        if (time.monotonic() - stored_at) >= ttl:
            _RESULTS.pop(key, None)
            return None
        return value


def in_progress(key: str, ttl: float = _DEFAULT_TTL) -> bool:
    """Read-only check: is there a live claim for ``key``?"""
    with _LOCK:
        ts = _GUARD.get(key)
    return ts is not None and (time.monotonic() - ts) < ttl
