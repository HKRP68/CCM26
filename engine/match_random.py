"""Task-local RNG for replayable cricket. Legacy callers use Python's RNG."""

from contextlib import contextmanager
from contextvars import ContextVar
import random as _random

_current = ContextVar("match_random", default=None)


@contextmanager
def use(rng):
    token = _current.set(rng)
    try:
        yield
    finally:
        _current.reset(token)


def __getattr__(name):
    if name.startswith("_") or not hasattr(_random, name):
        raise AttributeError(name)
    default = getattr(_random, name)
    if not hasattr(_random.Random, name):
        return default

    def draw(*args, **kwargs):
        rng = _current.get()
        return (default if rng is None else getattr(rng, name))(*args, **kwargs)
    return draw
