"""Bounded response caching for historical website reads, off the match path."""
from collections import OrderedDict
import copy
import threading
import time

_LOCK = threading.Lock()
_CACHE = OrderedDict()
_BUILD_LOCKS = {}
_GENERATION = 0
MAX_ENTRIES = 128
TTL = 20


def invalidate():
    global _GENERATION
    with _LOCK:
        _GENERATION += 1
        _CACHE.clear()


def read(session, series, season, kind, filters, build):
    from services.live_records import _source_signature
    source = _source_signature(session, series, season)
    scope = (str(session.get_bind().url), series.id, season.id if season else None,
             kind, tuple(sorted(filters.items())))
    with _LOCK:
        generation = _GENERATION
        key = (generation, scope)
        cached = _CACHE.get(key)
        if cached and time.monotonic() - cached[0] < TTL and cached[1] == source:
            _CACHE.move_to_end(key)
            return copy.deepcopy(cached[2])
        # In-flight keys are retained only for the duration of their readers.
        item = _BUILD_LOCKS.setdefault(key, [threading.Lock(), 0])
        item[1] += 1
    try:
        with item[0]:
            with _LOCK:
                cached = _CACHE.get(key)
                if cached and time.monotonic() - cached[0] < TTL and cached[1] == source:
                    return copy.deepcopy(cached[2])
            result = build()
            with _LOCK:
                if _GENERATION == generation:
                    _CACHE[key] = (time.monotonic(), source, copy.deepcopy(result))
                    _CACHE.move_to_end(key)
                    while len(_CACHE) > MAX_ENTRIES:
                        _CACHE.popitem(last=False)
            return result
    finally:
        with _LOCK:
            item[1] -= 1
            if not item[1]:
                _BUILD_LOCKS.pop(key, None)
