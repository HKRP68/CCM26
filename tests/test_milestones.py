"""Milestone detection for the over-by-over engine.

An over is simulated in one call, so milestones are found by diffing before and
after. Everything here is a way that diff goes wrong:

  * thresholds must fire on the CROSSING, not on equality — a batter who goes
    48 -> 52 has still made a fifty;
  * and must fire exactly once — the over after a fifty must not announce it
    again;
  * one over can carry several at once (a fifty, a three-fer and the team
    hundred), and they must come out in a sane order;
  * a partnership that FALLS resets partnership_runs to 0, which must never be
    read as a milestone;
  * counters reset at the innings break, so a diff across that boundary is
    meaningless and must be discarded rather than announced.
"""

import unittest

from services import milestones


def _state(**over):
    state = {
        "innings": 1, "overs": 20, "ball_format": "T20",
        "current_over": 10, "current_ball": 0,
        "bat_team_name": "Alpha", "bowl_team_name": "Beta",
        "total_runs": 95, "total_wickets": 2, "partnership_runs": 45,
        "batting_order": [{"roster_id": 1, "name": "Kohli"},
                          {"roster_id": 2, "name": "Rohit"}],
        "bat_xi": [{"roster_id": 1, "name": "Kohli"},
                   {"roster_id": 2, "name": "Rohit"}],
        "bowl_xi": [{"roster_id": 9, "name": "Bumrah"}],
        "bat_stats": {"1": {"runs": 47, "balls": 30},
                      "2": {"runs": 40, "balls": 28}},
        "bowl_stats": {"9": {"wickets": 2, "runs": 20, "balls": 24,
                             "hattrick": False}},
    }
    state.update(over)
    return state


def _keys(events):
    return [k for k, _ in events]


class BattingTests(unittest.TestCase):
    def test_a_fifty_fires_on_the_crossing_not_on_exactly_fifty(self):
        s = _state(); before = milestones.snapshot(s)
        s["bat_stats"]["1"] = {"runs": 52, "balls": 33}
        events = milestones.detect(s, before)
        self.assertEqual(_keys(events), ["fifty"])
        self.assertEqual(events[0][1]["player"], "Kohli")
        self.assertEqual(events[0][1]["runs"], 52)

    def test_a_fifty_is_announced_only_once(self):
        s = _state(bat_stats={"1": {"runs": 55, "balls": 34}})
        before = milestones.snapshot(s)
        s["bat_stats"]["1"] = {"runs": 71, "balls": 44}
        self.assertEqual(milestones.detect(s, before), [])

    def test_two_batters_reaching_fifty_in_one_over_both_count(self):
        s = _state(); before = milestones.snapshot(s)
        s["bat_stats"]["1"] = {"runs": 51, "balls": 32}
        s["bat_stats"]["2"] = {"runs": 50, "balls": 31}
        self.assertEqual(_keys(milestones.detect(s, before)),
                         ["fifty", "fifty"])

    def test_crossing_both_fifty_and_century_announces_the_fifty_first(self):
        s = _state(bat_stats={"1": {"runs": 40, "balls": 20}})
        before = milestones.snapshot(s)
        s["bat_stats"]["1"] = {"runs": 101, "balls": 50}
        self.assertEqual(_keys(milestones.detect(s, before)),
                         ["fifty", "century"])


class BowlingTests(unittest.TestCase):
    def test_three_and_five_wicket_hauls(self):
        s = _state(); before = milestones.snapshot(s)
        s["bowl_stats"]["9"] = {"wickets": 5, "runs": 25, "balls": 30,
                                "hattrick": False}
        events = milestones.detect(s, before)
        self.assertEqual(_keys(events), ["three_fer", "five_fer"])
        self.assertEqual(events[0][1]["figures"], "5/25 (5.0)")

    def test_a_hat_trick_is_read_off_the_flag_the_engine_already_sets(self):
        s = _state(); before = milestones.snapshot(s)
        s["bowl_stats"]["9"] = {"wickets": 3, "runs": 22, "balls": 26,
                                "hattrick": True}
        self.assertEqual(_keys(milestones.detect(s, before)),
                         ["hattrick", "three_fer"])

    def test_a_hat_trick_is_announced_only_once(self):
        s = _state(bowl_stats={"9": {"wickets": 3, "runs": 22, "balls": 26,
                                     "hattrick": True}})
        before = milestones.snapshot(s)
        s["bowl_stats"]["9"] = {"wickets": 4, "runs": 24, "balls": 30,
                                "hattrick": True}
        self.assertEqual(_keys(milestones.detect(s, before)), [])

    def test_the_bowlers_milestone_is_credited_to_the_fielding_side(self):
        s = _state(); before = milestones.snapshot(s)
        s["bowl_stats"]["9"] = {"wickets": 3, "runs": 22, "balls": 26,
                                "hattrick": False}
        fields = milestones.detect(s, before)[0][1]
        self.assertEqual(fields["team"], "Beta")
        self.assertEqual(fields["opponent"], "Alpha")


class TeamAndPartnershipTests(unittest.TestCase):
    def test_team_totals_fire_low_threshold_first(self):
        s = _state(total_runs=95); before = milestones.snapshot(s)
        s["total_runs"] = 152
        self.assertEqual(_keys(milestones.detect(s, before)),
                         ["team_100", "team_150"])

    def test_a_team_total_is_announced_only_once(self):
        s = _state(total_runs=120); before = milestones.snapshot(s)
        s["total_runs"] = 138
        self.assertEqual(milestones.detect(s, before), [])

    def test_a_stand_that_falls_is_never_a_milestone(self):
        s = _state(partnership_runs=45); before = milestones.snapshot(s)
        s["partnership_runs"] = 0          # wicket fell
        self.assertEqual(_keys(milestones.detect(s, before)), [])

    def test_a_fifty_stand_counts(self):
        s = _state(); before = milestones.snapshot(s)
        s["partnership_runs"] = 58
        self.assertEqual(_keys(milestones.detect(s, before)),
                         ["partnership_50"])


class GuardTests(unittest.TestCase):
    def test_a_diff_across_the_innings_break_is_discarded(self):
        s = _state(); before = milestones.snapshot(s)
        s["innings"] = 2
        s["total_runs"] = 200
        s["bat_stats"] = {"1": {"runs": 0, "balls": 0}}
        self.assertEqual(milestones.detect(s, before), [])

    def test_an_empty_over_produces_nothing(self):
        s = _state()
        self.assertEqual(milestones.detect(s, milestones.snapshot(s)), [])

    def test_missing_state_never_raises(self):
        self.assertEqual(milestones.detect(None, None), [])
        self.assertEqual(milestones.detect({}, {"innings": 1}), [])

    def test_one_over_can_carry_several_kinds_at_once(self):
        s = _state(); before = milestones.snapshot(s)
        s["bat_stats"]["1"] = {"runs": 55, "balls": 34}
        s["bowl_stats"]["9"] = {"wickets": 3, "runs": 25, "balls": 30,
                                "hattrick": False}
        s["total_runs"] = 103
        s["partnership_runs"] = 53
        self.assertEqual(
            _keys(milestones.detect(s, before)),
            ["fifty", "three_fer", "partnership_50", "team_100"])


if __name__ == "__main__":
    unittest.main()
