"""The /letsplay Impact Player bench — the pool the substitution draws from.

The regression this file exists for: every /letsplay match answered the Impact
Player button with "No substitutes available outside your Playing XI", however
deep the captain's roster was.

``_launch_match`` rebuilds each side's XI from ``{side}_xi_roster_ids`` — the
exact eleven that side confirmed, snapshotted so nobody can /swap between
confirmation and the toss. The bench was then taken as ``pairs[11:]`` of that
same list, which is empty **by construction**: a snapshot holds eleven ids and
nothing else. The bench is whatever the live roster holds beyond those eleven,
so it has to be read fresh rather than sliced off the XI.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import handlers.letsplay as lp


def _pair(rid, name):
    return (SimpleNamespace(id=rid, player_id=rid),
            SimpleNamespace(id=rid, name=name, rating=80))


def _roster(n):
    return [_pair(i, f"P{i}") for i in range(1, n + 1)]


class BenchForLaunchTests(unittest.TestCase):
    def _bench(self, roster, xi_pairs):
        with patch.object(lp, "_get_ordered_roster", lambda s, u: roster):
            return lp._bench_pairs_for_launch(None, 1, xi_pairs)

    def test_a_deep_roster_has_a_bench_even_from_a_pinned_xi(self):
        """The bug: an XI snapshot is exactly eleven, so slicing it gives nothing."""
        roster = _roster(15)
        xi = roster[:11]                     # what _pairs_from_roster_ids returns
        self.assertEqual(xi[11:], [], "the snapshot really is eleven long")
        bench = self._bench(roster, xi)
        self.assertEqual([e.id for e, _p in bench], [12, 13, 14, 15])

    def test_the_bench_is_whoever_is_not_in_the_xi_not_whoever_is_after_it(self):
        # A captain who moved a deep squad member into their XI leaves the
        # player they dropped on the bench, wherever either sits in the roster.
        roster = _roster(13)
        xi = [p for p in roster if p[0].id != 3][:11]   # 3 dropped, 12 promoted
        bench = self._bench(roster, xi)
        self.assertEqual(sorted(e.id for e, _p in bench), [3, 13])

    def test_exactly_eleven_players_means_no_bench(self):
        roster = _roster(11)
        self.assertEqual(self._bench(roster, roster), [])

    def test_a_roster_read_that_fails_is_no_bench_not_a_failed_launch(self):
        def _boom(_s, _u):
            raise RuntimeError("db gone")
        with patch.object(lp, "_get_ordered_roster", _boom):
            self.assertEqual(lp._bench_pairs_for_launch(None, 1, _roster(11)), [])

    def test_no_xi_pairs_leaves_the_whole_roster_available(self):
        roster = _roster(12)
        self.assertEqual(len(self._bench(roster, None)), 12)


class LaunchWiringTests(unittest.TestCase):
    """The launch must not go back to slicing the snapshot."""

    def test_the_bench_is_not_sliced_off_the_confirmed_xi(self):
        import inspect
        source = inspect.getsource(lp._launch_match)
        for dead in ("host_pairs[11:]", "guest_pairs[11:]"):
            self.assertNotIn(
                dead, source,
                f"{dead} is always empty when the XI came from a snapshot")
        self.assertIn("_bench_pairs_for_launch(session, host.id", source)
        self.assertIn("_bench_pairs_for_launch(session, guest.id", source)


if __name__ == "__main__":
    unittest.main()
