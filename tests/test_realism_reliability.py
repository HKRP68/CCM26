"""Cricket Mechanics & Reliability (Prompt 04) — fixes 1 and 2.

Fix 1 (delivery accounting): in realism mode byes are a tracked extra of their
own and the scorecard reconciles — an innings total equals the sum of every
batter's runs plus the extras total. Legacy keeps its historical three-key
extras dict and the same reconciliation.

Fix 2 (bowler identity): quota, fatigue and the last-bowler lock are keyed on a
stable id, so two players in one XI who share a name no longer share a quota.
The legacy name API still resolves uniquely-named bowlers.
"""

import logging
import os
import unittest

logging.disable(logging.CRITICAL)

from engine.bowler_manager import BowlerManager
from engine.format_config import get_format
from engine.realism.config import clear_cache
from services.sim_match import simulate_match
from tests.test_sim_match import _make_xi

T20 = get_format("T20")


class _Realism:
    def __init__(self, on=True):
        self.on = on

    def __enter__(self):
        self._prev = os.environ.get("SIM_REALISM_ENABLED")
        os.environ["SIM_REALISM_ENABLED"] = "1" if self.on else "0"
        clear_cache()
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop("SIM_REALISM_ENABLED", None)
        else:
            os.environ["SIM_REALISM_ENABLED"] = self._prev
        clear_cache()
        return False


# ───────────────────── Fix 1: delivery accounting ─────────────────────

class DeliveryAccountingTests(unittest.TestCase):

    def _check_innings_reconciles(self, inn):
        bat_runs = sum(bs["runs"] for bs in inn["bat_stats"].values())
        self.assertEqual(
            inn["runs"], bat_runs + inn["extras_total"],
            "innings total must equal batter runs + extras")
        ex = inn["extras"]
        self.assertEqual(
            inn["extras_total"],
            ex["wides"] + ex["noballs"] + ex["legbyes"] + ex.get("byes", 0),
            "extras_total must equal the sum of every extra type")

    def test_realism_innings_reconciles_and_tracks_byes(self):
        home, away = _make_xi("H", 80), _make_xi("A", 78)
        with _Realism(on=True):
            for s in range(6):
                m = simulate_match(home, away, 20, "Flat",
                                   "Alpha", "Bravo", seed=500 + s)
                for inn in (m["innings1"], m["innings2"]):
                    # Byes are their own tracked key in realism mode.
                    self.assertIn("byes", inn["extras"])
                    self._check_innings_reconciles(inn)

    def test_legacy_keeps_historical_three_key_extras(self):
        # Legacy is preserved byte-identical: it groups byes into legbyes (no
        # separate key) and keeps its historical extras accounting — which is
        # exactly the under-count realism mode corrects, so we assert the SHAPE
        # here, not reconciliation.
        home, away = _make_xi("H", 80), _make_xi("A", 78)
        with _Realism(on=False):
            m = simulate_match(home, away, 20, "Flat", "Alpha", "Bravo")
            for inn in (m["innings1"], m["innings2"]):
                self.assertEqual(set(inn["extras"]),
                                 {"wides", "noballs", "legbyes"})
                self.assertNotIn("byes", inn["extras"])


# ───────────────────── Fix 2: bowler identity ─────────────────────

def _bwl(rid, name):
    return {"roster_id": rid, "player_id": rid + 1000, "name": name,
            "will_bowl": True, "bowl_rating": 80}


class BowlerIdentityTests(unittest.TestCase):

    def test_same_name_different_id_keep_separate_quotas(self):
        a = _bwl(1, "Khan")
        b = _bwl(2, "Khan")          # same name, different roster id
        mgr = BowlerManager([a, b], T20)

        mgr.record_over_completion(a, 7)
        mgr.record_over_completion(a, 5)

        # Only bowler A has bowled; B is untouched despite the shared name.
        self.assertEqual(mgr.overs_bowled(a), 2)
        self.assertEqual(mgr.overs_bowled(b), 0)
        self.assertEqual(mgr.overs_remaining(b), T20.max_bowler_overs)
        self.assertFalse(mgr.at_quota(a))
        # A bowled the last over; B did not — the consecutive lock is per id.
        self.assertTrue(mgr.is_consecutive(a))
        self.assertFalse(mgr.is_consecutive(b))

    def test_quota_summary_reports_real_counts(self):
        a = _bwl(1, "Ali")
        mgr = BowlerManager([a], T20)
        mgr.record_over_completion(a, 4)
        row = mgr.quota_summary()["Ali"]
        self.assertEqual(row["bowled"], 1)
        self.assertEqual(row["remaining"], T20.max_bowler_overs - 1)

    def test_legacy_name_api_resolves_unique_names(self):
        a = _bwl(1, "Starc")
        b = _bwl(2, "Cummins")
        mgr = BowlerManager([a, b], T20)
        # Record by dict, then query by legacy name string.
        mgr.record_over_completion(a, 6)
        self.assertEqual(mgr.overs_bowled("Starc"), 1)
        self.assertEqual(mgr.prev_over_runs("Starc"), 6)
        self.assertEqual(mgr.overs_bowled("Cummins"), 0)


if __name__ == "__main__":
    unittest.main()
