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
        # A refresh one minute ago cannot be due again on any interval.
        just_now = datetime.utcnow() - timedelta(minutes=1)
        self.assertFalse(_is_due(just_now, 0, 12))
        self.assertFalse(_is_due(just_now, 0, 24))
        # Never refreshed is always due.
        self.assertTrue(_is_due(None, 0, 12))
        # A refresh two days ago is due on any interval.
        self.assertTrue(_is_due(datetime.utcnow() - timedelta(days=2), 0, 12))

    def test_hourly_interval_becomes_due_after_an_hour(self):
        ninety_min_ago = datetime.utcnow() - timedelta(minutes=90)
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


class PlayerMarketUnaffectedTest(unittest.TestCase):

    def test_player_schedule_ignores_trait_interval_settings(self):
        from services.global_market import get_next_refresh_at  # noqa: F401
        # get_next_refresh_at calls _next_refresh_utc without an interval, so
        # the trait market's setting can never reach it. Pin the default here.
        last = _ist(2026, 5, 4, 0)
        daily = _next_refresh_utc(0, last)
        self.assertEqual(daily, _ist(2026, 5, 5, 0))
        self.assertNotEqual(daily, _next_refresh_utc(0, last, 12))


if __name__ == "__main__":
    unittest.main()
