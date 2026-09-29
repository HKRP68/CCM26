"""Deterministic randomness.

``SimRng`` wraps ``random.Random`` seeded from a SHA-256 of the seed, so the
same seed reproduces a match byte for byte across processes (Python's own
``hash`` is salted per process and cannot be used). ``derive`` hands out an
independent child stream per purpose — weather, rain and the ball loop never
steal numbers from one another, so adding a commentary variant does not change
who wins.
"""

import hashlib
import random


def _seed_int(*parts):
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


def stable_unit(*parts):
    """A deterministic float in [0, 1) for *parts* (no RNG state involved)."""
    return _seed_int(*parts) / float(1 << 64)


class SimRng:
    def __init__(self, seed=None):
        if seed is None:
            seed = random.SystemRandom().randrange(1 << 62)
        self.seed = seed
        self._r = random.Random(_seed_int("simrng", seed))

    def derive(self, *keys):
        """An independent child stream for ``keys``."""
        return SimRng(f"{self.seed}/{'/'.join(str(k) for k in keys)}")

    def random(self):
        return self._r.random()

    def uniform(self, a, b):
        return self._r.uniform(a, b)

    def randint(self, a, b):
        return self._r.randint(a, b)

    def gauss(self, mu, sigma):
        return self._r.gauss(mu, sigma)

    def choice(self, seq):
        return self._r.choice(list(seq))

    def chance(self, p):
        return self._r.random() < p

    def weighted(self, weights):
        """Pick a key from ``{key: weight}``. Keys are visited in insertion
        order so the draw is reproducible."""
        items = [(k, max(0.0, float(w))) for k, w in weights.items()]
        total = sum(w for _, w in items)
        if total <= 0:
            return items[0][0]
        x = self._r.random() * total
        acc = 0.0
        for k, w in items:
            acc += w
            if x < acc:
                return k
        return items[-1][0]
