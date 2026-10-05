"""The tournament watch: round deadlines that remind and alert, never settle."""

import os
import sys
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tour_fixture as F  # noqa: E402


def setUpModule():
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()


class DeadlineTests(F.FourTeamCase):
    def setUp(self):
        super().setUp()
        from services import tournament_watch
        self.tw = tournament_watch
        self.t0 = datetime(2026, 10, 5, 12, 0)
        self.tour.round_hours = 48
        self.session.commit()

    def tick(self, hours=0):
        with mock.patch.object(self.tw, "_admin_ids", return_value=[111, 222]):
            return self.tw.tick(self.session, self.tour,
                                now=self.t0 + timedelta(hours=hours))

    def test_opening_a_round_starts_its_clock(self):
        self.assertEqual(self.tick(), [])
        self.assertEqual(self.tour.round_tracked, 1)
        self.assertEqual(self.tour.round_deadline_at, self.t0 + timedelta(hours=48))

    def test_reminders_fire_once_each_then_the_admins_are_alerted(self):
        self.tick()
        self.assertEqual(self.tick(10), [])                    # 38h left
        posts = self.tick(25)                                   # 23h left
        self.assertEqual([p.kind for p in posts], ["reminder"])
        self.assertEqual(posts[0].chat_id, -100555)
        dm_ids = {tg for tg, _ in posts[0].dms}
        self.assertTrue({o.telegram_id for o in self.owners.values()} <= dm_ids)
        self.assertEqual(self.tick(26), [])                    # no repeat
        self.assertEqual([p.kind for p in self.tick(46.5)], ["reminder"])
        posts = self.tick(49)
        self.assertEqual([p.kind for p in posts], ["expired"])
        alert = posts[0]
        self.assertEqual({tg for tg, _ in alert.dms}, {111, 222})
        self.assertTrue(alert.dm_buttons)
        flat = [cb for row in alert.buttons for _l, cb in row]
        self.assertTrue(any(cb.startswith("tsim_") for cb in flat))
        self.assertTrue(any(cb.startswith(self.tw.CB_EXTEND) for cb in flat))
        self.assertEqual(self.tick(60), [])

    def test_an_expired_round_is_never_settled_automatically(self):
        self.tick()
        self.tick(100)
        self.assertTrue(all(fx.status == "scheduled" for fx in self.fixtures(1)))
        self.assertEqual(self.lss.current_round(self.session, self.tour.id), 1)

    def test_extending_moves_the_deadline_and_rearms_the_reminders(self):
        self.tick()
        self.tick(49)
        self.tw.extend_deadline(self.session, self.tour, 24,
                                now=self.t0 + timedelta(hours=49))
        self.assertEqual(self.tour.round_deadline_at,
                         self.t0 + timedelta(hours=73))
        self.assertEqual([p.kind for p in self.tick(71.5)], ["reminder"])
        self.assertEqual([p.kind for p in self.tick(74)], ["expired"])

    def test_finishing_the_round_restarts_the_clock_for_the_next(self):
        self.tick()
        self.finish_round(1)
        self.tick(30)
        self.assertEqual(self.tour.round_tracked, 2)
        self.assertEqual(self.tour.round_deadline_at, self.t0 + timedelta(hours=78))

    def test_turning_deadlines_off_clears_them(self):
        self.tick()
        self.tw.set_round_hours(self.session, self.tour, None)
        self.assertIsNone(self.tour.round_deadline_at)
        self.assertEqual(self.tick(100), [])

    def test_lengths_parse(self):
        self.assertEqual(self.tw.parse_hours("48h"), (48, False))
        self.assertEqual(self.tw.parse_hours("2d"), (48, False))
        self.assertEqual(self.tw.parse_hours("+12h"), (12, True))
        for bad in ("", "abc", "0", "-3h", "99999h"):
            with self.assertRaises(ValueError):
                self.tw.parse_hours(bad)

    def test_the_banner_shows_the_time_left(self):
        self.tour.round_deadline_at = datetime.utcnow() + timedelta(hours=28, minutes=5)
        progress = self.lss.round_progress(self.session, self.tour.id)
        self.assertIn("ends in 1d 4h", self.lss.round_banner(progress, 0, self.tour))

    def test_a_paused_tournament_is_left_alone(self):
        self.tour.status = "paused"
        self.assertEqual(self.tick(100), [])
