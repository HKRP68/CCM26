"""Coverage for the website-editable trait market refresh schedule.

The trait market used to share one daily reroll hour with the player market.
It now runs on its own interval + start hour (GameConfig), so an admin can set
"every 12 hours starting 12 AM" and get midnight and noon IST every day.

These tests pin the anchor maths, the back-compat fallback for a DB migrated
but not yet re-saved, and that the player market's schedule is untouched.
"""

import unittest
from datetime import datetime, timedelta

from config import (clamp_refresh_interval, refresh_anchor_hours,
                    format_hour_ist, TRAIT_MARKET_REFRESH_INTERVALS)
from services.global_market import _next_refresh_utc, _is_due, _trait_schedule

IST_OFFSET = timedelta(hours=5, minutes=30)


def _ist(y, m, d, hour):
    """A naive UTC datetime for the given IST wall-clock hour."""
    return datetime(y, m, d, hour, 0, 0) - IST_OFFSET


def _frozen_now(utc_dt):
    """Pin ``global_market._now_utc`` so due-checks don't race the real clock."""
    from unittest.mock import patch
    return patch("services.global_market._now_utc", lambda: utc_dt)


class AnchorHoursTest(unittest.TestCase):

    def test_twelve_hourly_from_midnight_is_midnight_and_noon(self):
        self.assertEqual(refresh_anchor_hours(0, 12), [0, 12])

    def test_start_hour_shifts_every_anchor(self):
        self.assertEqual(refresh_anchor_hours(6, 12), [6, 18])
        self.assertEqual(refresh_anchor_hours(9, 8), [1, 9, 17])

    def test_default_interval_is_a_single_daily_anchor(self):
        self.assertEqual(refresh_anchor_hours(9, 24), [9])

    def test_every_allowed_interval_tiles_the_day_evenly(self):
        for interval in TRAIT_MARKET_REFRESH_INTERVALS:
            hours = refresh_anchor_hours(0, interval)
            self.assertEqual(len(hours), 24 // interval, interval)
            self.assertEqual(len(set(hours)), len(hours), interval)

    def test_bad_intervals_snap_to_an_allowed_value(self):
        self.assertEqual(clamp_refresh_interval(5), 4)
        # Ties break toward the shorter interval — err toward more refreshes.
        self.assertEqual(clamp_refresh_interval(7), 6)
        self.assertEqual(clamp_refresh_interval(0), 1)
        self.assertEqual(clamp_refresh_interval(99), 24)
        self.assertEqual(clamp_refresh_interval(None), 24)
        self.assertEqual(clamp_refresh_interval("nonsense"), 24)

    def test_hour_labels_read_as_a_12_hour_clock(self):
        self.assertEqual(format_hour_ist(0), "12:00 AM")
        self.assertEqual(format_hour_ist(12), "12:00 PM")
        self.assertEqual(format_hour_ist(9), "9:00 AM")
        self.assertEqual(format_hour_ist(21), "9:00 PM")


class NextRefreshTest(unittest.TestCase):

    def test_twelve_hour_interval_advances_to_the_next_half_day(self):
        # Last refreshed at midnight IST → next is noon IST the same day.
        last = _ist(2026, 5, 4, 0)
        self.assertEqual(_next_refresh_utc(0, last, 12), _ist(2026, 5, 4, 12))
        # …and from noon, midnight the following day.
        self.assertEqual(_next_refresh_utc(0, _ist(2026, 5, 4, 12), 12),
                         _ist(2026, 5, 5, 0))

    def test_default_interval_reproduces_the_old_daily_behaviour(self):
        last = _ist(2026, 5, 4, 9)
        self.assertEqual(_next_refresh_utc(9, last), _ist(2026, 5, 5, 9))
        self.assertEqual(_next_refresh_utc(9, last, 24), _ist(2026, 5, 5, 9))

    def test_a_stale_last_refresh_lands_on_the_next_anchor_not_far_future(self):
        # Refreshed days ago — the answer is the first anchor after it, which
        # is already in the past, so _is_due will fire.
        last = _ist(2026, 5, 1, 0)
        self.assertEqual(_next_refresh_utc(0, last, 12), _ist(2026, 5, 1, 12))

    def test_is_due_only_after_an_anchor_has_passed(self):
        # Frozen clock: against a live utcnow() these assertions flip for a
        # minute either side of every anchor (18:30 UTC is midnight IST), which
        # is a genuine once-a-day flake rather than a real failure.
        now = _ist(2026, 5, 4, 6)
        with _frozen_now(now):
            # A refresh one minute ago cannot be due again on any interval.
            just_now = now - timedelta(minutes=1)
            self.assertFalse(_is_due(just_now, 0, 12))
            self.assertFalse(_is_due(just_now, 0, 24))
            # Never refreshed is always due.
            self.assertTrue(_is_due(None, 0, 12))
            # A refresh two days ago is due on any interval.
            self.assertTrue(_is_due(now - timedelta(days=2), 0, 12))

    def test_hourly_interval_becomes_due_after_an_hour(self):
        now = _ist(2026, 5, 4, 6)
        with _frozen_now(now):
            ninety_min_ago = now - timedelta(minutes=90)
            self.assertTrue(_is_due(ninety_min_ago, 0, 1))
            self.assertFalse(_is_due(ninety_min_ago, 0, 24))


class TraitScheduleFallbackTest(unittest.TestCase):

    class _Cfg:
        def __init__(self, start, interval, player_hour=0):
            self.trait_market_refresh_start_hour_ist = start
            self.trait_market_refresh_interval_hours = interval
            self.market_refresh_hour_ist = player_hour

    def test_reads_the_configured_schedule(self):
        self.assertEqual(_trait_schedule(self._Cfg(0, 12)), (0, 12))
        self.assertEqual(_trait_schedule(self._Cfg(6, 8)), (6, 8))

    def test_null_start_hour_falls_back_to_the_player_market_hour(self):
        # A DB migrated but never re-saved keeps the time it already had,
        # rather than silently jumping to midnight.
        self.assertEqual(_trait_schedule(self._Cfg(None, 24, player_hour=9)),
                         (9, 24))

    def test_missing_config_row_defaults_to_daily_midnight(self):
        self.assertEqual(_trait_schedule(None), (0, 24))

    def test_a_bad_stored_interval_is_snapped_not_trusted(self):
        self.assertEqual(_trait_schedule(self._Cfg(0, 7)), (0, 6))
        self.assertEqual(_trait_schedule(self._Cfg(0, None)), (0, 24))

    def test_a_stored_zero_interval_is_a_value_not_an_absence(self):
        # Only NULL means "unset". A stored 0 must reach clamp_refresh_interval
        # and snap to the 1-hour minimum, not read as "once a day".
        self.assertEqual(_trait_schedule(self._Cfg(0, 0)),
                         (0, clamp_refresh_interval(0)))
        self.assertEqual(_trait_schedule(self._Cfg(0, 0))[1], 1)

    def test_a_stored_zero_start_hour_is_midnight_not_a_fallback(self):
        # 0 is a legitimate start hour; it must not fall back to the player
        # market's hour the way NULL does.
        self.assertEqual(_trait_schedule(self._Cfg(0, 12, player_hour=9)),
                         (0, 12))


class _FakeSession:
    """Minimal stand-in: ``session.query(GameConfig).first()`` → the row."""

    def __init__(self, cfg_row):
        self._cfg = cfg_row

    def query(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._cfg


class _FakeCfg:
    def __init__(self, *, player_hour, player_last,
                 trait_start, trait_interval, trait_last=None):
        self.market_refresh_hour_ist = player_hour
        self.market_last_refresh_at = player_last
        self.trait_market_refresh_start_hour_ist = trait_start
        self.trait_market_refresh_interval_hours = trait_interval
        self.trait_market_last_refresh_at = trait_last


class PlayerMarketUnaffectedTest(unittest.TestCase):
    """The two markets must not be able to reach each other's schedule."""

    def setUp(self):
        # Several service tests here stub ``models`` (and ``sqlalchemy``) in
        # sys.modules with lightweight fakes. If one leaks its stub, the
        # ``from models import GameConfig`` inside global_market fails and
        # these tests error out purely on test ordering — and the real module
        # can't be re-imported, because sqlalchemy is stubbed too.
        #
        # The functions under test only use GameConfig as a query argument that
        # the fake session ignores, so supplying the name is enough. Whatever
        # was in sys.modules is restored afterwards, leaving the leaker's own
        # state exactly as it was.
        import sys
        import types
        saved = sys.modules.get("models")
        self.addCleanup(self._restore_models, saved)
        try:
            from models import GameConfig  # noqa: F401
        except ImportError:
            stub = types.ModuleType("models")
            if saved is not None:
                stub.__dict__.update(saved.__dict__)
            stub.GameConfig = type("GameConfig", (), {})
            sys.modules["models"] = stub

    @staticmethod
    def _restore_models(saved):
        import sys
        if saved is None:
            sys.modules.pop("models", None)
        else:
            sys.modules["models"] = saved

    def _session(self):
        # Player market: daily at midnight IST, last rolled at midnight.
        # Trait market: every 12 hours from 6 AM IST, same last-roll time.
        return _FakeSession(_FakeCfg(
            player_hour=0, player_last=_ist(2026, 5, 4, 0),
            trait_start=6, trait_interval=12, trait_last=_ist(2026, 5, 4, 0),
        ))

    def test_player_next_refresh_is_the_daily_anchor(self):
        from services.global_market import get_next_refresh_at
        # Aggressive trait settings sit in the same config row and must not
        # pull the player market off its once-a-day schedule.
        self.assertEqual(get_next_refresh_at(self._session()),
                         _ist(2026, 5, 5, 0))

    def test_trait_next_refresh_follows_its_own_anchors(self):
        from services.global_market import get_next_trait_refresh_at
        # 12 h from 6 AM → 6 AM and 6 PM; from midnight the next is 6 AM.
        self.assertEqual(get_next_trait_refresh_at(self._session()),
                         _ist(2026, 5, 4, 6))

    def test_the_two_markets_disagree_on_purpose(self):
        from services.global_market import (get_next_refresh_at,
                                            get_next_trait_refresh_at)
        session = self._session()
        self.assertNotEqual(get_next_refresh_at(session),
                            get_next_trait_refresh_at(session))

    def test_a_missing_config_row_yields_no_schedule(self):
        from services.global_market import (get_next_refresh_at,
                                            get_next_trait_refresh_at)
        empty = _FakeSession(None)
        self.assertIsNone(get_next_refresh_at(empty))
        self.assertIsNone(get_next_trait_refresh_at(empty))


if __name__ == "__main__":
    unittest.main()
