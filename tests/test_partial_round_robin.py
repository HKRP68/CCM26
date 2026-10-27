"""Dynamic 1.5-RR construction, checked independently of its validator."""

import copy
import unittest
from collections import Counter
from itertools import combinations
from types import SimpleNamespace

from services.partial_round_robin import build_schedule, parse_match_target, validate_schedule
from services import standings


class PartialRoundRobinTests(unittest.TestCase):
    def check_plan(self, n, target=None):
        ids = list(range(101, 101 + n))
        plan = build_schedule(ids, target)
        phases = {p: [r for r in plan["rounds"] if r["phase"] == p] for p in (1, 2)}
        first = Counter(frozenset(pair) for r in phases[1] for pair in r["pairs"])
        repeats = Counter(frozenset(pair) for r in phases[2] for pair in r["pairs"])
        self.assertEqual(set(first), {frozenset(p) for p in combinations(ids, 2)})
        self.assertEqual(set(first.values()), {1})
        self.assertTrue(all(c == 1 for c in repeats.values()))
        self.assertTrue(set(repeats) <= set(first))
        counts, byes = Counter(), Counter()
        home = {frozenset(pair): pair for r in phases[1] for pair in r["pairs"]}
        for rnd in plan["rounds"]:
            playing = [t for pair in rnd["pairs"] for t in pair]
            self.assertEqual(len(playing), len(set(playing)))
            self.assertEqual(set(rnd["byes"]), set(ids) - set(playing))
            counts.update(playing)
            byes.update(rnd["byes"])
            if rnd["phase"] == 2:
                for pair in rnd["pairs"]:
                    self.assertEqual(pair, tuple(reversed(home[frozenset(pair)])))
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
        self.assertEqual(sum(counts.values()), 2 * plan["matches"])
        self.assertEqual(len(phases[1]), n if n % 2 else n - 1)
        self.assertEqual(len(phases[2]), (sum(repeats.values()) + n // 2 - 1) // (n // 2))
        for row in plan["teams"]:
            self.assertEqual(row["matches"], counts[row["team_id"]])
            self.assertEqual(row["byes"], byes[row["team_id"]])
            self.assertEqual(row["matches"] + row["byes"], plan["round_count"])
        return plan

    def test_default_odd_and_even_counts(self):
        for n in range(2, 66):
            with self.subTest(n=n):
                self.check_plan(n)

    def test_every_feasible_fixture_total_in_small_fields(self):
        for n in range(2, 18):
            for repeats in range(n * (n - 1) // 2 + 1):
                with self.subTest(n=n, repeats=repeats):
                    plan = self.check_plan(n, n - 1 + 2 * repeats / n)
                    self.assertEqual(plan["phase2_matches"], repeats)

    def test_pdf_examples_are_calculated(self):
        for n, matches, low, high, rounds in (
                (8, 42, 10, 11, 11), (10, 68, 13, 14, 14),
                (11, 83, 15, 16, 17), (12, 99, 16, 17, 17),
                (16, 180, 22, 23, 23)):
            with self.subTest(n=n):
                p = self.check_plan(n)
                self.assertEqual((p["matches"], p["min_matches"], p["max_matches"], p["round_count"]),
                                 (matches, low, high, rounds))

    def test_deterministic_and_non_contiguous_team_ids(self):
        ids = [42, 9001, 7, 62, 12000, 101, 5, 8, 125, 99, 222]
        p = build_schedule(ids)
        self.assertEqual(p, build_schedule(ids))
        self.assertEqual(len(p["fingerprint"]), 64)
        self.assertNotEqual(p["fingerprint"], build_schedule(ids, 16)["fingerprint"])

    def test_exact_and_rounded_targets_are_explained(self):
        exact = self.check_plan(8, 11)
        self.assertTrue(exact["exact"])
        self.assertEqual((exact["min_matches"], exact["max_matches"]), (11, 11))
        rounded = self.check_plan(11)
        self.assertFalse(rounded["exact"])
        self.assertIn("10 teams play 15, 1 team plays 16", rounded["explanation"])
        self.assertIn("round up", rounded["explanation"])

    def test_invalid_participants_and_targets(self):
        for ids in ([], [1], [1, 1], [1, None]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                build_schedule(ids)
        for raw in ("NaN", "Infinity", "-Infinity", "abc", 0, -3, "1e1000"):
            with self.subTest(target=raw), self.assertRaises(ValueError):
                parse_match_target(raw)
        for target in (1, 6.9, 14.1):
            with self.subTest(target=target), self.assertRaises(ValueError):
                build_schedule(range(8), target)

    def test_validation_rejects_corrupt_rounds_byes_and_pairs(self):
        ids = list(range(5))
        good = build_schedule(ids)
        bad = copy.deepcopy(good["rounds"])
        bad[0]["pairs"][0] = (0, 0)
        with self.assertRaises(ValueError):
            validate_schedule(ids, bad)
        bad = copy.deepcopy(good["rounds"])
        bad[0]["byes"] = []
        with self.assertRaises(ValueError):
            validate_schedule(ids, bad)
        bad = copy.deepcopy(good["rounds"])
        bad[-1]["pairs"] = bad[-2]["pairs"]
        bad[-1]["byes"] = bad[-2]["byes"]
        with self.assertRaises(ValueError):
            validate_schedule(ids, bad)


class RequestedStandingsOrderTests(unittest.TestCase):
    def team(self, tid, *, points=6, wins=3, runs=120, priority=0):
        return SimpleNamespace(id=tid, points=points, won=wins, runs_for=runs,
                               balls_for=120, runs_against=100, balls_against=120,
                               sort_order=priority)

    def ranked(self, rows, matches=()):
        return [r.id for r in standings.order(rows, "nrr_wins_h2h", matches)]

    def test_points_then_nrr_then_wins(self):
        rows = [self.team(1, wins=5), self.team(2, wins=2, runs=140),
                self.team(3, points=8, wins=1), self.team(4, wins=4, runs=140)]
        self.assertEqual(self.ranked(rows), [3, 4, 2, 1])

    def test_head_to_head_then_admin_priority(self):
        rows = [self.team(1, priority=1), self.team(2, priority=0), self.team(3, priority=2)]
        match = SimpleNamespace(team1_id=1, team2_id=2, winner_team_id=1)
        self.assertEqual(self.ranked(rows, [match]), [1, 2, 3])
        self.assertEqual(self.ranked(rows), [2, 1, 3])

    def test_legacy_standings_order_is_preserved(self):
        rows = [self.team(1, wins=4), self.team(2, wins=3, runs=140)]
        self.assertEqual([r.id for r in standings.order(rows)], [1, 2])
