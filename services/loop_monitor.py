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

Kept deliberately tiny: one task, one deque, no I/O.
"""

import asyncio
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

# How often to take a sample, and how many samples to keep (≈ the last minute).
SAMPLE_INTERVAL = 1.0
WINDOW = 60

_samples = deque(maxlen=WINDOW)
_task = None
_started_at = None


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


def start():
    """Start the sampler on the running loop. Safe to call more than once."""
    global _task, _started_at
    if _task is not None and not _task.done():
        return _task
    try:
        _task = asyncio.get_running_loop().create_task(_sample_forever())
        _started_at = time.monotonic()
        logger.info("Event-loop lag monitor started (every %.0fs)", SAMPLE_INTERVAL)
    except RuntimeError:
        logger.warning("Event-loop lag monitor not started — no running loop")
        _task = None
    return _task


def stop():
    """Stop the sampler (used by tests)."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
