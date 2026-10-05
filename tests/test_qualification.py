"""The qualification tracker's arithmetic — cautious Q/E marks."""

import unittest

from services.qualification import E, Q, status, need_line


class StatusTests(unittest.TestCase):
    def test_clinched_eliminated_and_alive(self):
        # Five teams, top 2 go through, 2 points a win.
        pts = {1: 10, 2: 8, 3: 4, 4: 2, 5: 0}
        rem = {1: 1, 2: 1, 3: 1, 4: 1, 5: 1}
        st = status(pts, rem, spots=2, points_win=2)
        self.assertEqual(st[1].mark, Q)        # nobody else can reach 10
        self.assertEqual(st[2].mark, Q)        # only team 1 can reach 8
        self.assertEqual(st[3].mark, E)        # max 6, two teams already above
        self.assertEqual(st[5].mark, E)

    def test_still_alive(self):
        pts = {1: 6, 2: 4, 3: 4, 4: 2}
        rem = {1: 2, 2: 2, 3: 2, 4: 2}
        st = status(pts, rem, spots=2, points_win=2)
        self.assertTrue(all(s.mark is None for s in st.values()))

    def test_a_tie_on_points_counts_as_a_threat(self):
        pts = {1: 6, 2: 4, 3: 4}
        rem = {1: 0, 2: 1, 3: 1}
        st = status(pts, rem, spots=1, points_win=2)
        self.assertIsNone(st[1].mark)          # 2 or 3 could reach 6
        self.assertIsNone(st[2].mark)

    def test_wins_to_be_sure(self):
        pts = {1: 4, 2: 4, 3: 4, 4: 0}
        rem = {1: 3, 2: 3, 3: 3, 4: 3}
        st = status(pts, rem, spots=2, points_win=2)
        # Rivals can reach 10, 10, 6 → the 2nd-best rival max is 10; 4+2w > 10 → w=4 > 3
        self.assertIsNone(st[1].wins_to_be_sure)
        self.assertIn("needs other results", need_line("A", st[1], 2))

    def test_need_line_counts_wins(self):
        pts = {1: 6, 2: 2, 3: 2, 4: 0}
        rem = {1: 2, 2: 2, 3: 2, 4: 2}
        st = status(pts, rem, spots=2, points_win=2)
        # Rivals' maxes 6, 6, 4 → bar is 6; 6 + 2w > 6 → 1 win
        self.assertEqual(st[1].wins_to_be_sure, 1)
        self.assertEqual(need_line("Alpha", st[1], 2),
                         "Alpha: 1 more win to be sure of the top 2")

    def test_no_playoffs_no_marks(self):
        st = status({1: 4, 2: 0}, {1: 0, 2: 0}, spots=0, points_win=2)
        self.assertIsNone(st[1].mark)
        self.assertIsNone(st[2].mark)
