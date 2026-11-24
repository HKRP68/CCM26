"""Over-volatility /sim + /cipl integration (Historical Cricket Intelligence).

With the flag on, the simulated over-to-over spread widens while the innings
mean stays put; with it off the path is byte-identical; and no player data is
touched.
"""

import hashlib
import logging
import os
import statistics as S
import unittest

logging.disable(logging.CRITICAL)

from engine.realism.config import clear_cache
from services.sim_match import simulate_match
from tests.test_sim_match import _make_xi


class _Env:
    def __init__(self, over_vol):
        self.over_vol = over_vol

    def __enter__(self):
        self._prev = {k: os.environ.get(k) for k in
                      ("SIM_REALISM_ENABLED", "SIM_REALISM_OVER_VOL")}
        os.environ["SIM_REALISM_ENABLED"] = "1"
        os.environ["SIM_REALISM_OVER_VOL"] = "1" if self.over_vol else "0"
        clear_cache()
        return self

    def __exit__(self, *exc):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        clear_cache()
        return False


def _over_stats(n, seed):
    oruns, totals = [], []
    for s in range(n):
        m = simulate_match(_make_xi("H", 80), _make_xi("A", 80), 20, "Even",
                           "A", "B", seed=seed + s)
        totals.append(m["innings1"]["runs"])
        for inn in ("innings1", "innings2"):
            pr = 0
            for ov in m[inn]["over_summaries"]:
                try:
                    cr = int(ov["team_score"].split("/")[0])
                except Exception:
                    continue
                oruns.append(cr - pr)
                pr = cr
    return S.pstdev(oruns), S.mean(totals), len(oruns)


class OverVolatilityIntegrationTests(unittest.TestCase):

    def test_widens_over_spread_without_moving_the_mean(self):
        with _Env(over_vol=False):
            sd_off, mean_off, n = _over_stats(30, 5000)
        with _Env(over_vol=True):
            sd_on, mean_on, _ = _over_stats(30, 5000)
        self.assertGreater(n, 800)
        # The over-to-over SD must increase.
        self.assertGreater(sd_on, sd_off)
        # The innings mean must stay within noise (SE ~ 40/sqrt(30) ~ 7).
        self.assertLess(abs(mean_on - mean_off), 8.0)

    def test_reproducible_from_seed(self):
        with _Env(over_vol=True):
            a = simulate_match(_make_xi("H", 80), _make_xi("A", 78), 20, "Even",
                               "A", "B", seed=314)
            b = simulate_match(_make_xi("H", 80), _make_xi("A", 78), 20, "Even",
                               "A", "B", seed=314)
        self.assertEqual((a["innings1"]["runs"], a["innings2"]["runs"]),
                         (b["innings1"]["runs"], b["innings2"]["runs"]))

    def test_default_off_is_untouched(self):
        # SIM_REALISM_OVER_VOL defaults off -> the result matches a run with it
        # explicitly off (no over-volatility effect leaks in).
        os.environ["SIM_REALISM_ENABLED"] = "1"
        os.environ.pop("SIM_REALISM_OVER_VOL", None)
        clear_cache()
        default = simulate_match(_make_xi("H", 80), _make_xi("A", 78), 20,
                                 "Even", "A", "B", seed=77)
        with _Env(over_vol=False):
            off = simulate_match(_make_xi("H", 80), _make_xi("A", 78), 20,
                                 "Even", "A", "B", seed=77)
        self.assertEqual(default["innings1"]["runs"], off["innings1"]["runs"])
        self.assertEqual(default["innings2"]["runs"], off["innings2"]["runs"])

    def test_no_player_data_touched(self):
        players = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "players.json")
        before = hashlib.sha256(open(players, "rb").read()).hexdigest()
        with _Env(over_vol=True):
            simulate_match(_make_xi("H", 80), _make_xi("A", 80), 20, "Even",
                           "A", "B", seed=5)
        after = hashlib.sha256(open(players, "rb").read()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
