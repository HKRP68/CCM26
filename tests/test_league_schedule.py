"""Unit tests for the pure round-robin schedule generator.

These exercise ``league_schedule_service.round_robin_rounds`` only (no DB), which
is the core of the League Tournament schedule feature.
"""

import unittest

from services.league_schedule_service import round_robin_rounds


def _all_pairs(rounds):
    """Flatten to a set of unordered frozenset pairs."""
    pairs = []
    for rnd in rounds:
        for a, b in rnd:
            pairs.append(frozenset((a, b)))
    return pairs


class RoundRobinTests(unittest.TestCase):
    def test_too_few_teams(self):
        self.assertEqual(round_robin_rounds([]), [])
        self.assertEqual(round_robin_rounds([1]), [])

    def test_single_rr_even_count_pairings(self):
        teams = [1, 2, 3, 4]
        rounds = round_robin_rounds(teams)
        # n teams (even) -> n-1 rounds, each n/2 matches.
        self.assertEqual(len(rounds), 3)
        for rnd in rounds:
            self.assertEqual(len(rnd), 2)
        pairs = _all_pairs(rounds)
        # Every distinct pair exactly once: C(4,2) = 6.
        self.assertEqual(len(pairs), 6)
        self.assertEqual(len(set(pairs)), 6)

    def test_single_rr_odd_count_has_byes(self):
        teams = [1, 2, 3, 4, 5]
        rounds = round_robin_rounds(teams)
        # Odd -> n rounds, each team sits out exactly once; C(5,2)=10 matches.
        self.assertEqual(len(rounds), 5)
        pairs = _all_pairs(rounds)
        self.assertEqual(len(pairs), 10)
        self.assertEqual(len(set(pairs)), 10)
        # No None ever appears in an emitted pairing.
        for rnd in rounds:
            for a, b in rnd:
                self.assertIsNotNone(a)
                self.assertIsNotNone(b)

    def test_no_team_plays_twice_in_a_round(self):
        teams = list(range(1, 7))  # 6 teams
        rounds = round_robin_rounds(teams)
        for rnd in rounds:
            seen = []
            for a, b in rnd:
                seen.extend([a, b])
            self.assertEqual(len(seen), len(set(seen)),
                             "a team appears twice in one round")

    def test_double_rr_doubles_every_pairing(self):
        teams = [1, 2, 3, 4]
        rounds = round_robin_rounds(teams, double=True)
        # Twice the rounds of a single RR.
        self.assertEqual(len(rounds), 6)
        pairs = _all_pairs(rounds)
        self.assertEqual(len(pairs), 12)  # 6 distinct pairs, each twice
        # Each distinct pair occurs exactly twice.
        counts = {}
        for p in pairs:
            counts[p] = counts.get(p, 0) + 1
        self.assertTrue(all(c == 2 for c in counts.values()))

    def test_double_rr_reverses_home_away(self):
        teams = [1, 2]
        rounds = round_robin_rounds(teams, double=True)
        self.assertEqual(rounds[0], [(1, 2)])
        self.assertEqual(rounds[1], [(2, 1)])  # mirrored second leg


if __name__ == "__main__":
    unittest.main()
