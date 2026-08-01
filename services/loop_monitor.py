"""Event-loop lag sampler.

The bot's asyncio loop is shared by every command, every callback, the Mini
App's Telegram sends, and the scheduled jobs. Anything that blocks it — a
synchronous database round trip inside an async handler, CPU-heavy card
rendering, a long serialization — delays *every* other user's reply by however
long it ran, without producing any error to point at.

This samples that directly: sleep for a known interval, then measure how late
the wake-up actually was. On an idle loop the overshoot is ~0ms. Sustained
double- or triple-digit lag means the loop is being held by blocking work, and
that number lands on user-visible latency (including the /botstatus ping).

The sampler is a gauge, though, not an alarm — it can only record a freeze once
the freeze is over, because recording it needs the very loop that is stuck. So
there is a second half: a plain watchdog thread that reads the sampler's
heartbeat from outside the loop and, when the loop stops ticking, logs the
stall *and the stack of the thread causing it* while it is still going on.
That is the difference between "the bot went quiet for five minutes on Saturday
and we don't know why" and a log line naming the line of code.
"""

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque

logger = logging.getLogger(__name__)

# How often to take a sample, and how many samples to keep (≈ the last minute).
SAMPLE_INTERVAL = 1.0
WINDOW = 60

# A loop that has not come back to us for this long is not "busy", it is stuck,
# and every user is looking at a bot that has stopped answering. Report it while
# it is still happening — see _watch_forever.
STALL_SECONDS = float(os.getenv("LOOP_STALL_WARN_SECONDS", "15"))

_samples = deque(maxlen=WINDOW)
_task = None
_started_at = None

_last_tick = None          # monotonic clock of the loop's most recent wake-up
_loop_thread_id = None     # so the watchdog can dump the stack that is blocking
_watchdog = None
_watchdog_stop = threading.Event()


def record(lag_ms: float) -> None:
    """Record one lag observation (exposed for tests)."""
    _samples.append(max(0.0, lag_ms))


def get_stats():
    """Return {avg_ms, max_ms, p95_ms, samples} over the recent window."""
    if not _samples:
        return {"avg_ms": 0.0, "max_ms": 0.0, "p95_ms": 0.0, "samples": 0}
    ordered = sorted(_samples)
    idx = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "avg_ms": sum(ordered) / len(ordered),
        "max_ms": ordered[-1],
        "p95_ms": ordered[idx],
        "samples": len(ordered),
    }


def describe() -> str:
    """One-word health verdict for the recent window."""
    stats = get_stats()
    if not stats["samples"]:
        return "no data"
    p95 = stats["p95_ms"]
    if p95 < 50:
        return "healthy"
    if p95 < 250:
        return "busy"
    if p95 < 1000:
        return "congested"
    return "blocked"


async def _sample_forever():
    global _last_tick
    loop = asyncio.get_running_loop()
    while True:
        before = loop.time()
        try:
            await asyncio.sleep(SAMPLE_INTERVAL)
        except asyncio.CancelledError:
            raise
        # Anything above the interval is time the loop could not get back to
        # us — i.e. time it spent blocked or backed up.
        record((loop.time() - before - SAMPLE_INTERVAL) * 1000.0)
        _last_tick = time.monotonic()


def _blocking_stack():
    """The loop thread's current stack, or None if it can't be read.

    This is the whole point of the watchdog. A frozen bot leaves no error
    behind — the blocking call eventually returns or raises minutes later, long
    after the outage, and nothing in the logs says which line it was sitting on.
    Naming that line while it is still stuck turns the next incident from an
    afternoon of log archaeology into one grep.
    """
    if _loop_thread_id is None:
        return None
    frame = sys._current_frames().get(_loop_thread_id)
    if frame is None:
        return None
    return "".join(traceback.format_stack(frame))


def _check_once(reported_at):
    """One pass of the stall check. Returns the new ``reported_at``.

    ``reported_at`` is the heartbeat value an in-progress stall was already
    logged against, or None when the loop is healthy — it keeps a five-minute
    freeze to one log line rather than one per second. Kept as a parameter
    rather than module state so the watchdog owns it privately.
    """
    tick = _last_tick
    if tick is None:
        return reported_at
    stalled_for = time.monotonic() - tick
    if stalled_for >= STALL_SECONDS:
        if reported_at != tick:
            logger.error(
                "Event loop BLOCKED for %.1fs — the bot is not answering "
                "anyone right now. Stack of the blocked thread:\n%s",
                stalled_for, _blocking_stack() or "<unavailable>")
            return tick
    elif reported_at is not None and reported_at != tick:
        logger.warning("Event loop recovered after ~%.0fs blocked",
                       tick - reported_at)
        return None
    return reported_at


def _watch_forever():
    """Off-loop stall alarm.

    The sampler above cannot report a stall, because reporting it would itself
    need the loop that is stuck: it only ever learns about the freeze once the
    freeze is over, and even then only writes to a gauge that /botstatus reads.
    So this runs in a plain thread, watches the heartbeat the sampler leaves
    behind, and logs while the bot is still down.
    """
    reported_at = None
    while not _watchdog_stop.wait(1.0):
        try:
            reported_at = _check_once(reported_at)
        except Exception:  # a watchdog that can die is not a watchdog
            logger.exception("Loop stall watchdog pass failed")


def start():
    """Start the sampler on the running loop. Safe to call more than once."""
    global _task, _started_at, _last_tick, _loop_thread_id, _watchdog
    if _task is not None and not _task.done():
        return _task
    try:
        _task = asyncio.get_running_loop().create_task(_sample_forever())
        _started_at = time.monotonic()
        _last_tick = time.monotonic()
        _loop_thread_id = threading.get_ident()
        logger.info("Event-loop lag monitor started (every %.0fs)", SAMPLE_INTERVAL)
    except RuntimeError:
        logger.warning("Event-loop lag monitor not started — no running loop")
        _task = None
        return None

    if _watchdog is None or not _watchdog.is_alive():
        _watchdog_stop.clear()
        _watchdog = threading.Thread(target=_watch_forever, daemon=True,
                                     name="loop-stall-watchdog")
        _watchdog.start()
        logger.info("Event-loop stall watchdog started (warns after %.0fs)",
                    STALL_SECONDS)
    return _task


def stop():
    """Stop the sampler and the watchdog (used by tests)."""
    global _task, _last_tick, _watchdog
    if _task is not None:
        _task.cancel()
        _task = None
    # Clear the heartbeat before releasing the thread, so a sampler cancelled
    # mid-test can't read as a permanent stall on the way out.
    _last_tick = None
    _watchdog_stop.set()
    # Join before releasing the reference, so start() cannot raise a second
    # watchdog while the old one is still unwinding. Tests cycle start/stop
    # against this module state, and two live watchdogs would race it.
    if _watchdog is not None and _watchdog is not threading.current_thread():
        _watchdog.join(timeout=2.0)
    _watchdog = None
