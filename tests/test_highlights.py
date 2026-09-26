"""Match highlights reel (services/highlights.py). Pure — no Telegram/DB."""

import unittest

from services import highlights as hl


def _state():
    return {
        "innings": 1, "bat_team_name": "Kings", "bowl_team_name": "Royals",
        "batting_order": [{"roster_id": 1, "name": "Ace"}, {"roster_id": 2, "name": "Bolt"}],
        "bowl_xi": [{"roster_id": 9, "name": "Quick"}],
        "bat_stats": {"1": {"runs": 44, "balls": 30, "out": False},
                      "2": {"runs": 3, "balls": 5, "out": False}},
        "bowl_stats": {"9": {"wickets": 2, "runs": 20}},
    }


class RecordOverTests(unittest.TestCase):
    def test_fifty_wicket_and_haul_are_logged(self):
        s = _state()
        before = hl.snapshot(s)
        s["bat_stats"]["1"]["runs"] = 58
        s["bat_stats"]["1"]["balls"] = 36
        s["bat_stats"]["2"].update(out=True, dismissal="c Ace b Quick")
        s["bowl_stats"]["9"]["wickets"] = 3
        hl.record_over(s, before, over_no=7, bowler_name="Quick", over_runs=16,
                       over_wkts=1, timeline=["6", "4", "W", "1", "4", "1"],
                       legal_balls=6)
        kinds = [m["kind"] for m in s[hl.LOG_KEY]]
        self.assertIn("fifty", kinds)
        self.assertIn("wicket", kinds)
        self.assertIn("haul", kinds)
        self.assertIn("six", kinds)
        haul = next(m for m in s[hl.LOG_KEY] if m["kind"] == "haul")
        self.assertIn("Quick takes 3 wickets", haul["text"])

    def test_maiden_and_big_over(self):
        s = _state()
        hl.record_over(s, hl.snapshot(s), over_no=2, bowler_name="Quick",
                       over_runs=0, over_wkts=0, timeline=["0"] * 6, legal_balls=6)
        hl.record_over(s, hl.snapshot(s), over_no=3, bowler_name="Quick",
                       over_runs=26, over_wkts=0,
                       timeline=["6", "6", "4", "6", "4", "0"], legal_balls=6)
        kinds = [m["kind"] for m in s[hl.LOG_KEY]]
        self.assertIn("maiden", kinds)
        self.assertIn("big_over", kinds)

    def test_chase_swing(self):
        s = _state()
        s.update(innings=2, chase_history=[{"over": 11, "chasing": 40},
                                           {"over": 12, "chasing": 71}])
        hl.record_over(s, hl.snapshot(s), over_no=12, bowler_name="Quick",
                       over_runs=9, over_wkts=0, timeline=["1"] * 6, legal_balls=6)
        swing = [m for m in s[hl.LOG_KEY] if m["kind"] == "swing"]
        self.assertEqual(len(swing), 1)
        self.assertIn("40% → 71%", swing[0]["text"])

    def test_never_raises_on_a_broken_state(self):
        hl.record_over({"bat_stats": None}, None, over_no=1, bowler_name=None,
                       over_runs=0, over_wkts=0, timeline=None, legal_balls=0)


class ReelTests(unittest.TestCase):
    def test_reel_caps_each_kind_and_keeps_match_order(self):
        s = {hl.LOG_KEY: [
            {"inn": 1, "over": o, "kind": "six", "w": 12 + o, "text": f"six {o}"}
            for o in range(1, 8)] + [
            {"inn": 2, "over": 5, "kind": "wicket", "w": 60, "text": "big wicket"},
            {"inn": 2, "over": 9, "kind": "fifty", "w": 35, "text": "fifty"}]}
        moments, top = hl.build_reel(s)
        self.assertEqual(top["text"], "big wicket")
        self.assertLessEqual(sum(1 for m in moments if m["kind"] == "six"), 2)
        order = [(m["inn"], m["over"]) for m in moments]
        self.assertEqual(order, sorted(order))
        self.assertLessEqual(len(moments), hl.REEL_SIZE)

    def test_render(self):
        s = {hl.LOG_KEY: [{"inn": 1, "over": 3, "kind": "wicket", "w": 40,
                           "text": "Ace <b>c</b> x"},
                          {"inn": 1, "over": 5, "kind": "six", "w": 12, "text": "six"},
                          {"inn": 2, "over": 2, "kind": "maiden", "w": 15, "text": "m"}]}
        text = hl.render_reel(s, title_teams="A vs B")
        self.assertIn("MATCH HIGHLIGHTS", text)
        self.assertIn("Moment of the match", text)
        self.assertIn("&lt;b&gt;", text)      # user text is escaped
        self.assertEqual(hl.render_reel({}), "")
        self.assertEqual(hl.render_reel({hl.LOG_KEY: s[hl.LOG_KEY][:2]}), "")

    def test_winning_hit(self):
        s = {"current_over": 19, "bat_team_name": "Kings",
             hl.LOG_KEY: [{"inn": 1, "over": 1, "kind": "six", "w": 12, "text": "x"}]}
        hl.mark_winning_hit(s, {"tie": False, "margin_type": "wickets", "margin": 4})
        self.assertEqual(s[hl.LOG_KEY][-1]["kind"], "winning_hit")
        empty = {}
        hl.mark_winning_hit(empty, {"tie": False, "margin_type": "wickets", "margin": 4})
        self.assertNotIn(hl.LOG_KEY, empty)
        s2 = {}
        hl.mark_winning_hit(s2, {"tie": False, "margin_type": "runs", "margin": 4})
        self.assertNotIn(hl.LOG_KEY, s2)


if __name__ == "__main__":
    unittest.main()
