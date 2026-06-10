"""Optional hot-path timing logs for the /wpm Mini App endpoints.

Enable with MATCH_PERF_LOG=1. When disabled (the default) the helpers are
no-ops: `perf_timed` returns the function unwrapped and `perf_span` hands back
a shared do-nothing context manager, so production pays nothing beyond one
module-level env check at import time.

Log lines use time.monotonic() and look like:
    PERF match_rest_poll mid=123 4.2ms
"""

import logging
import os
import time

logger = logging.getLogger("match_perf")

PERF_ENABLED = os.getenv("MATCH_PERF_LOG", "").strip() == "1"


if PERF_ENABLED:
    from contextlib import contextmanager
    from functools import wraps

    @contextmanager
    def perf_span(label, mid=None):
        t0 = time.monotonic()
        try:
            yield
        finally:
            logger.info("PERF %s mid=%s %.1fms",
                        label, mid, (time.monotonic() - t0) * 1000)

    def perf_timed(label):
        def deco(fn):
            @wraps(fn)
            def wrapper(*args, **kwargs):
                t0 = time.monotonic()
                try:
                    return fn(*args, **kwargs)
                finally:
                    logger.info("PERF %s %.1fms",
                                label, (time.monotonic() - t0) * 1000)
            return wrapper
        return deco
else:
    class _NoopSpan:
        __slots__ = ()

        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    _NOOP_SPAN = _NoopSpan()

    def perf_span(label, mid=None):
        return _NOOP_SPAN

    def perf_timed(label):
        return lambda fn: fn
