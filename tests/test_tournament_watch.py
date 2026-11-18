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

    def test_batch_deadline_covers_every_open_round_without_restarting(self):
        self.lss.configure_release(self.session, self.tour, "batch", 2)
        self.tick()
        deadline = self.tour.round_deadline_at
        self.assertEqual(self.tour.round_tracked, 2)
        _, pending = self.tw.open_round_pending(self.session, self.tour)
        self.assertEqual({fx.round_no for fx in pending}, {1, 2})
        self.finish_round(1)
        self.tick(10)
        self.assertEqual(self.tour.round_deadline_at, deadline)
        self.finish_round(2)
        self.tick(30)
        self.assertEqual(self.tour.round_tracked, 3)
        self.assertEqual(self.tour.round_deadline_at, self.t0 + timedelta(hours=78))

    def test_manual_batch_waiting_for_release_has_no_deadline(self):
        self.lss.configure_release(self.session, self.tour, "manual", 2)
        self.tick()
        self.finish_round(1)
        self.finish_round(2)
        posts = self.tick(49)
        self.assertIsNone(self.tour.round_deadline_at)
        self.assertFalse(any(p.kind == "expired" for p in posts))
        self.assertIsNone(self.lss.deadline_round(self.session, self.tour))

    def test_scheduled_watch_opens_and_announces_once(self):
        self.lss.configure_release(self.session, self.tour, "scheduled", 1, 24, now=self.t0)
        self.tick()
        posts = self.tick(25)
        release = [p for p in posts if p.kind == "round_release"]
        self.assertEqual(len(release), 1)
        self.assertIn("through 2", release[0].text)
        self.assertFalse(any(p.kind == "round_release" for p in self.tick(26)))


class PhasedDeadlineTests(F.FourTeamCase):
    KNOCKOUT = None

    def setUp(self):
        super().setUp()
        from services import tournament_watch
        self.tw = tournament_watch
        self.t0 = datetime(2026, 10, 5, 12)
        self.tour.league_format = "double_rr"
        self.lss.generate_schedule(self.session, self.tour.id)
        self.configure()

    def configure(self, size=2, hours=18, now=None):
        self.lss.configure_phased_release(self.session, self.tour, 1, 2, "2026-10-07 00:15", size, hours,
                                          now=now or self.t0)
        self.session.commit()

    def finish_at(self, round_no, when):
        with mock.patch.object(self.ts, "datetime", wraps=datetime) as clock:
            clock.utcnow.return_value = when
            self.finish_round(round_no)

    def test_telegram_fixtures_switch_from_the_fixed_deadline_to_relative_batches(self):
        from services import cl_tournament_view, lp_tournament_service

        self.assertEqual(self.tour.round_deadline_at, datetime(2026, 10, 6, 18, 45))
        _, pending = self.tw.open_round_pending(self.session, self.tour)
        self.assertEqual({fx.round_no for fx in pending}, {1, 2})
        for render in (cl_tournament_view.render_fixtures, lp_tournament_service.render_fixtures):
            self.assertIn("07 Oct 2026, 00:15 IST", render(self.session, self.tour))
        self.assertFalse(self.lss.fixture_in_open_round(self.session, self.fixtures(3)[0]))
        self.finish_at(2, self.t0 + timedelta(hours=10))
        self.assertEqual(self.lss.open_round(self.session, self.tour.id), 2)

        opened = self.t0 + timedelta(hours=12)
        self.finish_at(1, opened)
        self.assertEqual(self.lss.open_round(self.session, self.tour.id), 4)
        self.assertTrue(self.lss.fixture_in_open_round(self.session, self.fixtures(4)[0]))
        self.assertFalse(self.lss.fixture_in_open_round(self.session, self.fixtures(5)[0]))
        for render in (cl_tournament_view.render_fixtures, lp_tournament_service.render_fixtures):
            self.assertIn("06 Oct 2026, 23:30 IST", render(self.session, self.tour))

        later = opened + timedelta(hours=8)
        posts = self.tw.tick(self.session, self.tour, now=later)
        release = [post for post in posts if post.kind == "round_release"]
        self.assertEqual(len(release), 1)
        self.assertIn("06 Oct 2026, 23:30 IST", release[0].text)
        self.assertEqual(self.tour.round_deadline_at, opened + timedelta(hours=18))
        self.assertFalse(any(p.kind == "round_release" for p in self.tw.tick(self.session, self.tour, now=later)))

    def test_settings_update_future_batches_and_expiry_buttons_honor_their_batch(self):
        expired_at = self.t0 + timedelta(hours=32)
        with mock.patch.object(self.tw, "_admin_ids", return_value=[111]):
            alert, = self.tw.tick(self.session, self.tour, now=expired_at)
        self.assertEqual(alert.kind, "expired")
        self.assertIn("07 Oct 2026, 00:15 IST", alert.text)
        self.assertIn(f"tdl_{self.tour.id}_24_2", [code for row in alert.buttons for _, code in row])
        self.assertTrue(all(fx.status == "scheduled" for fx in self.fixtures()))
        self.assertEqual(self.lss.open_round(self.session, self.tour.id), 2)
        self.finish_at(1, expired_at)
        self.finish_at(2, expired_at)
        with self.assertRaisesRegex(ValueError, "batch has finished"):
            self.tw.extend_deadline(self.session, self.tour, 24, now=expired_at, expected_round=2)

        self.configure(size=1, hours=30, now=expired_at)
        self.assertEqual(self.lss.open_round(self.session, self.tour.id), 4)
        self.assertEqual(self.tour.round_deadline_at, expired_at + timedelta(hours=18))
        self.finish_at(3, expired_at + timedelta(hours=1))
        self.finish_at(4, expired_at + timedelta(hours=2))
        self.tw.tick(self.session, self.tour, now=expired_at + timedelta(hours=3))
        self.assertEqual(self.lss.open_round(self.session, self.tour.id), 5)
        self.assertEqual(self.tour.round_deadline_at, expired_at + timedelta(hours=32))

    def test_initial_range_can_start_later_after_earlier_rounds_finish(self):
        configure = self.lss.configure_phased_release
        with self.assertRaisesRegex(ValueError, "Finish the earlier league rounds"):
            configure(self.session, self.tour, 2, 3, "2026-10-07 00:15", 1, 24, now=self.t0)
        self.finish_at(1, self.t0)
        configure(self.session, self.tour, 2, 3, "2026-10-07 00:15", 1, 24, now=self.t0)
        self.assertEqual(self.lss.open_round(self.session, self.tour.id), 3)
        self.tw.extend_deadline(self.session, self.tour, 3, now=self.t0)
        self.assertEqual(self.tour.fixture_initial_deadline_at, datetime(2026, 10, 6, 21, 45))
        self.assertEqual(self.tour.round_hours, 24)
