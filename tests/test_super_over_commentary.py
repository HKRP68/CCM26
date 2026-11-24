"""Super Over ball-by-ball commentary: capture, archive and serialization.

Covers the backend recording of every resolved Super Over delivery, the
enriched summary / Mini-App innings builders (round grouping, batting/bowling,
deliveries), back-compat with totals-only archives, and the completed-match
arena payload surfacing the Super Over so a reopened match shows its
commentary.  The frontend render is covered separately (Node) in
test_super_over_miniapp_js.py.
"""

import asyncio
import os
import random
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers.super_over as so_mod  # noqa: E402

# Reuse the end-to-end Super Over driver + fakes from the existing suite.
from tests.test_super_over import (  # noqa: E402
    _patch_finalize, _tied_state, FakeContext, _play_match,
)
from handlers.super_over import start_super_over, _get  # noqa: E402

so_mod._BALL_PAUSE = 0


def _run_super_over(seed=7):
    """Drive a full Super Over and return the captured archived summary +
    extra-innings (via a stubbed save_final_scorecard)."""
    random.seed(seed)
    _patch_finalize(so_mod)
    captured = {}

    def fake_save(session, match_id, **kw):
        captured["super_over"] = kw.get("super_over")
        captured["extra_innings"] = kw.get("extra_innings")
        return True

    import services.match_webapp_service as mws
    with mock.patch.object(mws, "save_final_scorecard", fake_save):
        ctx = FakeContext()
        state = _tied_state()
        mid = state["match_id"]
        asyncio.run(start_super_over(ctx, mid, state))
        asyncio.run(_play_match(ctx, mid))
    return captured


class SuperOverCaptureTests(unittest.TestCase):

    def test_full_flow_records_ball_by_ball_commentary(self):
        cap = _run_super_over()
        so = cap.get("super_over")
        self.assertIsNotNone(so, "super_over summary was not archived")
        self.assertTrue(so["innings"], "no Super Over innings captured")

        all_deliveries = []
        for inn in so["innings"]:
            self.assertIn("deliveries", inn)
            self.assertIn("batting", inn)
            self.assertIn("bowling", inn)
            self.assertIn("round", inn)
            for d in inn["deliveries"]:
                all_deliveries.append(d)
                # Every delivery carries the real stored fields.
                for key in ("index", "ball", "legal", "runs", "batter",
                            "bowler", "commentary", "scoreRuns", "scoreWickets"):
                    self.assertIn(key, d)
                self.assertTrue(d["commentary"], "a delivery had no commentary")

        self.assertTrue(all_deliveries, "no deliveries recorded at all")
        # Unique, sequential, match-wide indices.
        idxs = [d["index"] for d in all_deliveries]
        self.assertEqual(len(idxs), len(set(idxs)), "delivery indices not unique")
        self.assertEqual(idxs, sorted(idxs), "delivery indices not in order")

    def test_ball_labels_and_wides_preserved(self):
        cap = _run_super_over(seed=3)
        so = cap["super_over"]
        for inn in so["innings"]:
            legal_seen = 0
            for d in inn["deliveries"]:
                # Label is over.ball with ball in 1..6 for a Super Over.
                over_s, ball_s = d["ball"].split(".")
                self.assertEqual(over_s, "0")
                self.assertTrue(1 <= int(ball_s) <= 6)
                if d["legal"]:
                    legal_seen += 1
            # Never more than six legal balls in a Super Over innings.
            self.assertLessEqual(legal_seen, 6)

    def test_extra_innings_also_carry_deliveries(self):
        cap = _run_super_over()
        extra = cap.get("extra_innings") or []
        self.assertTrue(extra)
        for inn in extra:
            self.assertTrue(inn.get("super_over"))
            self.assertIn("deliveries", inn)
            self.assertIn("round", inn)


class SuperOverBuilderUnitTests(unittest.TestCase):

    def _detail(self, rnd, team, runs, wk, log):
        return {
            "team": team, "team_uid": 1, "opp_uid": 2, "runs": runs,
            "wickets": wk, "overs": "1.0", "deliveries": len(log), "round": rnd,
            "deliveries_log": log,
            "batters": [{"name": "A", "runs": runs, "balls": 6, "fours": 1,
                         "sixes": 1, "out": False, "how_out": "not out"}],
            "bowlers": [{"name": "B", "wickets": wk, "runs": runs,
                         "overs": "1.0", "balls": 6}],
        }

    def _so(self, all_innings, number=1):
        return {"so_number": number, "innings_no": 2,
                "teams": {1: {"name": "VIN"}, 2: {"name": "TAM"}},
                "so_all_innings": all_innings, "turning_points": []}

    def test_summary_groups_rounds_and_carries_detail(self):
        d1 = self._detail(1, "VIN", 15, 1, [{"index": 1, "ball": "0.1",
                                             "commentary": "x", "runs": 1,
                                             "legal": True}])
        d2 = self._detail(1, "TAM", 16, 1, [{"index": 2, "ball": "0.1",
                                             "commentary": "y", "runs": 6,
                                             "legal": True}])
        so = self._so([d1, d2])
        summ = so_mod._super_over_summary(so, "TAM", "won by 1 run")
        self.assertEqual(len(summ["innings"]), 2)
        self.assertEqual([i["round"] for i in summ["innings"]], [1, 1])
        self.assertEqual(summ["innings"][0]["deliveries"][0]["commentary"], "x")
        self.assertEqual(len(summ["innings"][0]["batting"]), 1)

    def test_multiple_rounds_kept_separate(self):
        d1 = self._detail(1, "VIN", 10, 2, [])
        d2 = self._detail(1, "TAM", 10, 2, [])
        d3 = self._detail(2, "TAM", 12, 1, [])
        d4 = self._detail(2, "VIN", 13, 0, [])
        so = self._so([d1, d2, d3, d4], number=2)
        summ = so_mod._super_over_summary(so, "VIN", "won in Super Over 2")
        rounds = sorted({i["round"] for i in summ["innings"]})
        self.assertEqual(rounds, [1, 2])
        self.assertEqual(len(summ["innings"]), 4)
        mini = so_mod._super_over_miniapp_innings(so)
        self.assertEqual([m["number"] for m in mini], [3, 4, 5, 6])

    def test_backcompat_totals_only_innings(self):
        # An innings captured before ball logging (no deliveries_log) yields an
        # empty deliveries list — never fabricated commentary.
        legacy = {"team": "OLD", "team_uid": 1, "opp_uid": 2, "runs": 12,
                  "wickets": 1, "overs": "1.0", "batters": [], "bowlers": []}
        so = {"so_number": 1, "innings_no": 2, "teams": {1: {"name": "A"},
              2: {"name": "B"}}, "so_innings": [legacy], "turning_points": []}
        summ = so_mod._super_over_summary(so, "A", "won")
        self.assertEqual(summ["innings"][0]["deliveries"], [])


class SuperOverArchiveRoundTripTests(unittest.TestCase):
    """The enriched summary survives the scorecard archive, so a completed
    match can be reopened with its Super Over commentary intact (#13)."""

    def test_load_final_scorecard_returns_super_over_with_deliveries(self):
        import json
        from types import SimpleNamespace
        from services import match_webapp_service as mws

        so_summary = {
            "winner": "TAM", "marginText": "won by 1 run",
            "innings": [{"label": "Super Over Innings 1", "round": 1,
                         "team": "VIN", "runs": 15, "wickets": 1,
                         "overs": "1.0", "batting": [], "bowling": [],
                         "deliveries": [{"index": 1, "ball": "0.1",
                                         "commentary": "SIX!", "runs": 6,
                                         "legal": True}]}],
            "turningPoints": [{"round": 1, "ball": "0.1", "team": "VIN",
                               "score": "6/0", "text": "Huge start",
                               "kind": "six"}],
        }
        stored = json.dumps({"innings": [], "super_over": so_summary,
                             "arena_state": {}})
        row = SimpleNamespace(scorecard_json=stored, result_text="TAM won")

        class _Filter:
            def first(self_inner):
                return row

        class _Query:
            def filter(self_inner, *a, **k):
                return _Filter()

        class _Session:
            def query(self_inner, *a, **k):
                return _Query()

        data = mws.load_final_scorecard(_Session(), 4242)
        self.assertIsNotNone(data)
        so = data["super_over"]
        self.assertEqual(so["winner"], "TAM")
        self.assertEqual(
            so["innings"][0]["deliveries"][0]["commentary"], "SIX!")
        self.assertEqual(so["turningPoints"][0]["kind"], "six")

    def test_summary_is_json_serializable(self):
        import json
        cap = _run_super_over()
        # The whole archived summary must be JSON-safe (it is persisted as text).
        json.dumps(cap["super_over"])
        json.dumps(cap["extra_innings"])


if __name__ == "__main__":
    unittest.main()
