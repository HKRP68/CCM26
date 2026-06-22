"""Unit tests for the pure knockout bracket helpers (no DB)."""

import unittest

from services.knockout_service import _seed_order, _stage_for


class SeedOrderTests(unittest.TestCase):
    def test_size_2(self):
        self.assertEqual(_seed_order(2), [0, 1])

    def test_size_4(self):
        # Standard bracket: 1v4, 2v3 -> positions [0,3,1,2].
        self.assertEqual(_seed_order(4), [0, 3, 1, 2])

    def test_size_8_is_permutation(self):
        order = _seed_order(8)
        self.assertEqual(sorted(order), list(range(8)))
        # Round-1 pairs keep the top seed away from the #2 seed.
        pairs = [(order[i], order[i + 1]) for i in range(0, 8, 2)]
        # Seed 0 (best) pairs with seed 7 (worst).
        self.assertIn((0, 7), pairs)
        # Seeds 0 and 1 are in different round-1 pairs.
        p0 = next(p for p in pairs if 0 in p)
        self.assertNotIn(1, p0)

    def test_top4_pairs_are_1v4_2v3(self):
        order = _seed_order(4)
        pairs = [(order[0], order[1]), (order[2], order[3])]
        # 0-based: (0,3) and (1,2) i.e. #1v#4 and #2v#3.
        self.assertEqual(pairs, [(0, 3), (1, 2)])


class StageNameTests(unittest.TestCase):
    def test_named_rounds(self):
        self.assertEqual(_stage_for(1), "final")
        self.assertEqual(_stage_for(2), "semifinal")
        self.assertEqual(_stage_for(4), "quarterfinal")

    def test_large_round_name(self):
        self.assertEqual(_stage_for(8), "round_of_16")


if __name__ == "__main__":
    unittest.main()
