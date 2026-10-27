"""Two-group opponent frequencies, full rounds, home balance and rematch spacing."""

import copy
import unittest
from collections import Counter
from itertools import combinations

from services.cross_group_schedule import build_schedule, seeded_groups, validate_group_schedule


class CrossGroupScheduleTests(unittest.TestCase):
    def check_plan(self, a, b):
        plan = build_schedule(a, b)
        ids, m = a + b, len(a)
        counts, home, meetings = Counter(), Counter(), {}
        for rnd in plan["rounds"]:
            playing = [t for pair in rnd["pairs"] for t in pair]
            self.assertEqual(set(playing), set(ids))
            self.assertEqual(len(playing), len(ids))
            self.assertEqual(rnd["byes"], [])
            for x, y in rnd["pairs"]:
                counts.update((x, y))
                home[x] += 1
                meetings.setdefault(frozenset((x, y)), []).append((rnd["round_no"], x))
        self.assertEqual(len(meetings), len(ids) * (len(ids) - 1) // 2)
        group = {t: i for i, teams in enumerate((a, b)) for t in teams}
        for pair, legs in meetings.items():
            x, y = tuple(pair)
            cross = group[x] != group[y]
            self.assertEqual(len(legs), 2 if cross else 1)
            if cross:
                self.assertNotEqual(legs[0][1], legs[1][1])
                self.assertGreaterEqual(legs[1][0] - legs[0][0], m)
        games = 3 * m - 1
        self.assertEqual(set(counts.values()), {games})
        self.assertEqual(plan["round_count"], games)
        self.assertEqual(plan["matches"], len(ids) * games // 2)
        for row in plan["teams"]:
            self.assertEqual(row["home"], home[row["team_id"]])
            self.assertEqual(row["home"] + row["away"], games)
            self.assertLessEqual(abs(row["home"] - row["away"]), 1)
        return plan

    def test_every_even_field_from_four_to_sixty_four(self):
        for n in range(4, 66, 2):
            with self.subTest(n=n):
                self.check_plan(*seeded_groups(list(range(1, n + 1))))

    def test_ten_teams_have_seventy_fixtures_and_seven_home_games(self):
        plan = self.check_plan([1, 4, 5, 8, 9], [2, 3, 6, 7, 10])
        self.assertEqual((plan["matches"], plan["round_count"], plan["phase1_matches"], plan["phase2_matches"]),
                         (70, 14, 45, 25))
        self.assertTrue(all(row["home"] == row["away"] == 7 for row in plan["teams"]))
        self.assertGreaterEqual(plan["minimum_repeat_gap"], 5)
        self.assertLessEqual(plan["longest_streak"], 3)

    def test_every_small_group_partition(self):
        for n in (4, 6, 8, 10):
            ids = list(range(101, 101 + n))
            for rest in combinations(ids[1:], n // 2 - 1):
                a = [ids[0], *rest]
                b = [t for t in ids if t not in a]
                with self.subTest(n=n, a=a):
                    self.check_plan(a, b)

    def test_seeding_splits_each_strength_pair_and_is_reproducible(self):
        ids = list(range(1, 11))
        a, b = seeded_groups(ids)
        self.assertEqual((a, b), ([1, 4, 5, 8, 9], [2, 3, 6, 7, 10]))
        for first in range(1, 11, 2):
            self.assertNotEqual(first in a, first + 1 in a)
        self.assertEqual(build_schedule(a, b), build_schedule(a, b))
        self.assertEqual(ids, list(range(1, 11)))

    def test_rejects_odd_duplicate_unequal_and_missing_teams(self):
        for ids in ([], [1, 2], [1, 2, 3, 4, 5], [1, 2, 3, 3], [1, 2, 3, None]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                seeded_groups(ids)
        for groups in (([1, 2], [3]), ([1, 2], [2, 3]), ([1], [2])):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                build_schedule(*groups)

    def test_validation_rejects_same_home_side_for_a_rematch(self):
        a, b = seeded_groups(list(range(1, 11)))
        rounds = copy.deepcopy(build_schedule(a, b)["rounds"])
        x, y = rounds[-1]["pairs"][0]
        rounds[-1]["pairs"][0] = y, x
        with self.assertRaises(ValueError):
            validate_group_schedule(a, b, rounds)
