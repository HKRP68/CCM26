"""Preferred pitches, driven through the Flask admin app.

What is pinned here: a tournament can be created with a preferred-pitch mode,
share and list; the Manage page saves them (and refuses an over-long list
instead of trimming it); each team saves its own list; and the pages that show
all of this still render.
"""

import json
import itertools
import os
import re
import sys
import tempfile
import unittest
import asyncio
from types import SimpleNamespace
from unittest import mock

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None

# Everything that caches a reference to ``database`` / ``models``, for the reason
# tests/test_auction_bidding.py gives at length. ``admin`` is in the list because
# it binds ``get_session`` at import time.
_MODULE_NAMES = ("database", "models", "config", "admin",
                 "handlers.tournament_admin",
                 "services.tournament_service",
                 "services.league_schedule_service")

_PID = itertools.count(1)


def _unload(names):
    for name in names:
        sys.modules.pop(name, None)
        parent, _, child = name.rpartition(".")
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            try:
                delattr(package, child)
            except AttributeError:
                pass


def _restore(saved):
    for name, module in saved.items():
        parent, _, child = name.rpartition(".")
        if module is None:
            _unload([name])
            continue
        sys.modules[name] = module
        package = sys.modules.get(parent) if parent else None
        if package is not None:
            setattr(package, child, module)


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    _unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    os.environ.setdefault("BOT_TOKEN", "test-token")
    os.environ.setdefault("ADMIN_PASSWORD", "test")
    os.environ.setdefault("ADMIN_USERNAME", "admin")

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class PreferredPitchAdminTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            import admin
        except Exception as exc:            # pragma: no cover - env-dependent
            raise unittest.SkipTest(f"admin app unavailable: {exc}")
        admin.app.config["TESTING"] = True
        admin.app.config["WTF_CSRF_ENABLED"] = False
        cls.admin = admin

    def setUp(self):
        from database import get_session
        from models import ChallengeMode, ChallengeLeague, ChallengeTeam
        self.session = get_session()
        tag = next(_PID)
        mode = ChallengeMode(name=f"Mode {tag}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"League {tag}",
                                      short_code="PP")
        self.session.add(self.league)
        self.session.flush()
        self.cteams = []
        for i, name in enumerate(("Alpha", "Bravo", "Charlie", "Delta")):
            ct = ChallengeTeam(league_id=self.league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.cteams.append(ct)
        self.session.commit()
        self.client = self.admin.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["admin"] = True

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def create(self, **extra):
        from models import Tournament
        data = {"action": "create_tournament", "name": f"Cup {next(_PID)}",
                "league_id": str(self.league.id), "tour_format": "double_round_robin",
                "team_ids": [str(ct.id) for ct in self.cteams]}
        data.update(extra)
        response = self.client.post("/tournaments", data=data,
                                    follow_redirects=True)
        self.assertEqual(200, response.status_code)
        self.session.expire_all()
        return (self.session.query(Tournament).filter_by(name=data["name"]).one())

    def detail(self, t, data=None):
        url = f"/tournaments/{t.id}"
        response = (self.client.post(url, data=data, follow_redirects=True)
                    if data else self.client.get(url))
        self.assertEqual(200, response.status_code)
        self.session.expire_all()
        return response.get_data(as_text=True)

    def test_create_with_a_tournament_list(self):
        t = self.create(pitch_mode="tour_preferred", preferred_pitch_pct="70",
                        preferred_pitch_limit="3",
                        preferred_pitches=["Green", "Dusty"])
        self.assertEqual(t.pitch_mode, "tour_preferred")
        self.assertEqual(t.preferred_pitch_pct, 70)
        self.assertEqual(t.preferred_pitch_limit, 3)
        self.assertEqual(json.loads(t.preferred_pitches_json), ["Green", "Dusty"])
        self.assertIn("Tournament preferred pitches", self.detail(t))

    def test_the_create_page_offers_the_new_modes(self):
        body = self.client.get("/tournaments").get_data(as_text=True)
        self.assertIn('value="team_preferred"', body)
        self.assertIn('name="preferred_pitches"', body)

    def test_settings_save_and_re_stamp_the_schedule(self):
        from services import league_schedule_service as lss
        t = self.create()
        lss.generate_schedule(self.session, t.id)
        self.session.commit()
        self.detail(t, {"action": "save_settings", "name": t.name,
                        "pitch_mode": "tour_preferred",
                        "preferred_pitch_pct": "100", "preferred_pitch_limit": "4",
                        "preferred_pitches": ["Flat"]})
        self.assertEqual(t.pitch_mode, "tour_preferred")
        pitches = {fx.pitch_type for fx in lss.list_fixtures(self.session, t.id)}
        self.assertEqual(pitches, {"Flat"})

    def test_an_over_long_list_is_refused_not_trimmed(self):
        t = self.create(preferred_pitches=["Green"])
        body = self.detail(t, {"action": "save_settings", "name": t.name,
                               "pitch_mode": "tour_preferred",
                               "preferred_pitch_pct": "80",
                               "preferred_pitch_limit": "2",
                               "preferred_pitches": ["Green", "Dusty", "Flat"]})
        self.assertIn("at most 2", body)
        self.assertEqual(json.loads(t.preferred_pitches_json), ["Green"])

    def test_a_team_saves_its_own_list(self):
        from models import TournamentTeam
        t = self.create(pitch_mode="team_preferred")
        tt = self.session.query(TournamentTeam).filter_by(tournament_id=t.id).first()
        tt.home_pitch = "Bouncy"
        self.session.commit()
        body = self.detail(t, {"action": "set_team_preferred_pitches",
                               "team_id": str(tt.id),
                               "preferred_pitches": ["Green", "Hard"]})
        self.assertIn("Green, Hard", body)
        self.session.refresh(tt)
        self.assertEqual(json.loads(tt.preferred_pitches_json), ["Green", "Hard"])
        self.assertIsNone(tt.home_pitch)

    def test_playoff_conditions_and_private_fixture_setting_from_website(self):
        from models import TournamentMatch, TournamentTeam
        from services import league_schedule_service as lss
        t = self.create()
        lss.generate_schedule(self.session, t.id)
        self.session.commit()
        body = self.detail(t, {'action': 'save_playoff_conditions', 'playoff_pitches': ['Green'],
                               'playoff_stadiums': ['Wankhede Stadium']})
        self.assertIn('Playoff pitches and stadiums', body)
        self.assertEqual(json.loads(t.playoff_conditions_json)['pitches'], ['Green'])
        fx = lss.list_fixtures(self.session, t.id)[0]
        self.detail(t, {'action': 'set_fixture_super_overs', 'fixture_id': str(fx.id), 'planned_super_overs': '2'})
        self.assertEqual(fx.planned_super_overs, 2)
        schedule = self.client.get(f'/tournaments/{t.id}/schedule')
        self.assertEqual(schedule.status_code, 200)
        self.assertIn('Two Super Overs', schedule.get_data(as_text=True))
        dashboard = self.client.get(f'/tournaments/{t.id}/dashboard')
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn('League stage', dashboard.get_data(as_text=True))
        team = self.session.query(TournamentTeam).filter_by(tournament_id=t.id).first()
        self.detail(t, {'action': 'set_team_award_photo', 'team_id': str(team.id),
                        'award_photo_url': 'https://example.com/team.png'})
        self.assertEqual(team.award_photo_url, 'https://example.com/team.png')
        fx.status = 'live'
        self.session.commit()
        self.detail(t, {'action': 'set_fixture_super_overs', 'fixture_id': str(fx.id), 'planned_super_overs': '1'})
        self.assertEqual(fx.planned_super_overs, 2)

    def test_channel_configuration_and_automatic_most_catches_award(self):
        t = self.create()
        body = self.detail(t, {'action':'save_tournament_channels', 'channels':'@FirstChannel\n-1001234567890\n@firstchannel'})
        self.assertEqual(json.loads(t.channels_json), ['@firstchannel', '-1001234567890'])
        self.assertIn('Most Catches', body)
        self.assertNotIn('Best Catch', body)
        self.assertNotIn('save_best_catch', body)
        self.assertIn('-1001234567890', body)
        self.assertIn('Most Dot Balls', body)
        self.assertIn('Winning Captain', body)
        self.detail(t, {'action':'save_tournament_channels', 'channels':'invalid'})
        self.assertEqual(json.loads(t.channels_json), ['@firstchannel', '-1001234567890'])

    def test_shared_round_pitch_can_be_created_and_edited_until_play_starts(self):
        from services import league_schedule_service as lss
        t = self.create(pitch_mode="round")
        lss.generate_schedule(self.session, t.id)
        self.session.commit()
        first = lss.list_fixtures(self.session, t.id)[0]
        self.detail(t, {"action": "set_round_pitch", "round_no": str(first.round_no), "pitch_type": "Green"})
        rows = [fx for fx in lss.list_fixtures(self.session, t.id) if fx.round_no == first.round_no]
        self.assertEqual({fx.pitch_type for fx in rows}, {"Green"})
        first.status = "live"
        self.session.commit()
        body = self.detail(t, {"action": "set_round_pitch", "round_no": str(first.round_no), "pitch_type": "Flat"})
        self.assertIn("pitch is locked", body)
        self.assertEqual({fx.pitch_type for fx in rows}, {"Green"})
        self.detail(t, {"action": "save_settings", "name": t.name, "pitch_mode": "host"})
        self.assertEqual(t.pitch_mode, "round")

    def test_batch_release_controls_save_on_create_manage_and_schedule(self):
        from services import league_schedule_service as lss
        t = self.create(fixture_release_present="1", fixture_release_mode="manual",
                        fixture_round_window="2", fixture_batch_hours="24")
        lss.generate_schedule(self.session, t.id)
        self.session.commit()
        self.assertEqual(lss.release_mode(t), "manual")
        self.assertEqual(lss.open_round(self.session, t.id), 2)
        body = self.client.get(f"/tournaments/{t.id}/schedule").get_data(as_text=True)
        self.assertIn("Round opening controls", body)
        self.assertIn("Round 1 · Open", body)
        self.assertIn("Round 2 · Open", body)
        self.assertIn("Round 3 · Locked", body)
        self.detail(t, {"action": "open_next_rounds", "round_count": "1"})
        self.assertEqual(lss.open_round(self.session, t.id), 3)
        self.detail(t, {"action": "save_fixture_release", "fixture_release_mode": "batch",
                        "fixture_round_window": "2", "fixture_batch_hours": "48"})
        self.assertEqual(lss.release_mode(t), "batch")
        self.assertEqual(lss.open_round(self.session, t.id), 3)
        self.detail(t, {"action": "save_settings", "name": t.name,
                        "fixture_release_present": "1", "fixture_release_mode": "scheduled",
                        "fixture_round_window": "3", "fixture_batch_hours": "72"})
        self.assertEqual(lss.release_mode(t), "scheduled")
        self.assertEqual(t.fixture_batch_hours, 72)

    def test_invalid_release_input_rolls_back(self):
        t = self.create()
        self.detail(t, {"action": "save_fixture_release", "fixture_release_mode": "manual",
                        "fixture_round_window": "0", "fixture_batch_hours": "48"})
        self.assertEqual(t.fixture_release_mode, "rolling")

    def test_bot_release_commands_and_legacy_modes(self):
        from handlers import tournament_admin as handler
        from services import league_schedule_service as lss
        t = self.create()
        lss.generate_schedule(self.session, t.id)
        self.session.commit()
        model, tid = type(t), t.id
        with mock.patch.object(handler, "get_session", return_value=self.session), \
             mock.patch.object(handler, "_resolve_tournament", side_effect=lambda db, args: db.get(model, tid)), \
             mock.patch.object(handler, "_require_admin", new=mock.AsyncMock(return_value=True)), \
             mock.patch.object(handler, "_reply", new=mock.AsyncMock()) as reply:
            for args, mode, end in ((["manual", "2"], "manual", 2),
                                    (["next", "1"], "manual", 3),
                                    (["round"], "rolling", 1),
                                    (["batch", "2"], "batch", 2),
                                    (["scheduled", "2", "24h"], "scheduled", 2),
                                    (["3"], "rolling", 3),
                                    (["all"], "all", None)):
                asyncio.run(handler.tfixturemode_handler(None, SimpleNamespace(args=args)))
                t = self.session.get(model, tid)
                self.assertEqual(lss.release_mode(t), mode)
                self.assertEqual(lss.open_round(self.session, t.id), end)
            asyncio.run(handler.tfixturemode_handler(None, SimpleNamespace(args=["batch", "0"])))
            t = self.session.get(model, tid)
            self.assertEqual(lss.release_mode(t), "all")
            self.assertIn("Choose between", reply.call_args.args[1])



if __name__ == "__main__":
    unittest.main()
