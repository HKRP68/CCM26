"""The bot half of owner-locked teams and fixture-fixed pitches.

``tests/test_tournament_rules.py`` pins the rules themselves; this pins the two
places in the Challenge League setup flow that have to honour them:

  • the team picker refuses a team that isn't yours, even when the button that
    delivered the pick came from a stale card
  • a tournament fixture's surface is found from the two team *names* the picker
    works in, and announcing it says plainly that nobody gets to choose

Follows the throwaway-sqlite pattern from `tests/test_ciplbot_team_pick.py`.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "handlers.challenge",
                 "services.tournament_service", "services.league_schedule_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)

    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"

    from database import Base, engine
    import models  # noqa: F401  (registers the tables on Base)

    _ENGINE = engine
    Base.metadata.create_all(bind=engine)


def tearDownModule():
    if _ENGINE is not None:
        _ENGINE.dispose()
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


HOST_TG = 5001
GUEST_TG = 5002
TEAMS = ["Alpha", "Bravo", "Charlie"]


def _draft(**overrides):
    """A team-picker draft sitting on the host's turn."""
    draft = {
        "draft_id": 424242,
        "chat_id": -100,
        "host_tg_id": HOST_TG,
        "target_tg_id": GUEST_TG,
        "league_key": "tst",
        "league_name": "Test League",
        "is_tournament": True,
        "tournament_id": None,
        "owner_locked": True,
        "host_allowed_teams": ["Alpha"],
        "target_allowed_teams": ["Bravo"],
        "vs_bot": False,
        "teams": list(TEAMS),
        "team_codes": {t: t[:3].upper() for t in TEAMS},
        "turn": "host",
        "host": {"user_id": 1, "tg_id": HOST_TG, "name": "Ana"},
        "target": {"user_id": 2, "tg_id": GUEST_TG, "name": "Bo"},
    }
    draft.update(overrides)
    return draft


def _query(data, from_id):
    q = MagicMock()
    q.data = data
    q.from_user.id = from_id
    q.answer = AsyncMock()
    q.edit_message_caption = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    q.message.reply_text = AsyncMock()
    return q


class TeamPickLockTests(unittest.TestCase):
    def setUp(self):
        import handlers.challenge as challenge
        self.challenge = challenge
        self.draft = _draft()
        self.context = MagicMock()
        self.context.bot_data = {
            challenge._challenge_team_draft_key(self.draft["draft_id"]): self.draft}
        self.context.job_queue = None

    def _pick(self, team_name, from_id):
        idx = TEAMS.index(team_name)
        q = _query(f"cl_team_{self.draft['draft_id']}_{idx}", from_id)
        asyncio.run(self.challenge.challenge_team_callback(
            MagicMock(callback_query=q), self.context))
        return q

    def test_a_team_you_do_not_own_is_refused(self):
        q = self._pick("Charlie", HOST_TG)
        self.assertIsNone(self.draft.get("host_team"))
        self.assertEqual(self.draft["turn"], "host")
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))
        self.assertIn("isn't yours", q.answer.call_args.args[0])

    def test_your_own_team_goes_through(self):
        self._pick("Alpha", HOST_TG)
        self.assertEqual(self.draft.get("host_team"), "Alpha")
        self.assertEqual(self.draft["turn"], "target")

    def test_the_guest_is_held_to_their_own_team_too(self):
        self._pick("Alpha", HOST_TG)
        q = self._pick("Charlie", GUEST_TG)
        self.assertIsNone(self.draft.get("target_team"))
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))

    def test_an_unlocked_draft_allows_anything(self):
        self.draft.update(owner_locked=False, host_allowed_teams=None,
                          target_allowed_teams=None)
        self._pick("Charlie", HOST_TG)
        self.assertEqual(self.draft.get("host_team"), "Charlie")

    def test_a_co_owner_reaches_the_picker_through_the_same_allowed_list(self):
        # Ownership is resolved once, into the draft's allowed lists; a co-owned
        # team arrives there exactly as an owned one does.
        self.draft["host_allowed_teams"] = ["Alpha", "Charlie"]
        self._pick("Charlie", HOST_TG)
        self.assertEqual(self.draft.get("host_team"), "Charlie")

    def test_the_keyboard_hides_what_the_picker_may_not_choose(self):
        kb = self.challenge._team_keyboard_for(self.draft, self.draft["draft_id"])
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertIn("ALP", labels)
        self.assertNotIn("BRA", labels)
        self.assertNotIn("CHA", labels)


class LockedPitchTests(unittest.TestCase):
    """The surface a fixture fixes, found from the names the picker works in."""

    @classmethod
    def setUpClass(cls):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam)
        from services import league_schedule_service

        session = get_session()
        try:
            mode = ChallengeMode(name="Locked pitch mode", is_active=True)
            session.add(mode)
            session.flush()
            league = ChallengeLeague(mode_id=mode.id, name="Locked Pitch League",
                                     short_code="LPL", is_active=True)
            session.add(league)
            session.flush()
            for i, name in enumerate(TEAMS):
                session.add(ChallengeTeam(league_id=league.id, name=name,
                                          sort_order=i))
            tour = Tournament(name="Locked Cup", league_id=league.id,
                              league_name=league.name, kind="challenge",
                              status="active", league_format="single_rr",
                              pitch_mode="fixture")
            session.add(tour)
            session.flush()
            for i, name in enumerate(TEAMS):
                session.add(TournamentTeam(tournament_id=tour.id, name=name,
                                           sort_order=i))
            session.flush()
            league_schedule_service.generate_schedule(session, tour.id)
            session.commit()
            cls.tour_id = tour.id
        finally:
            session.close()

    def test_the_fixture_surface_is_found_from_the_team_names(self):
        import handlers.challenge as challenge
        draft = _draft(turn="complete", host_team="Alpha", target_team="Bravo",
                       tournament_id=self.tour_id)
        pitch, home = challenge._resolve_fixture_venue(draft)
        from services.league_schedule_service import FIXTURE_PITCHES
        self.assertIn(pitch, FIXTURE_PITCHES)
        self.assertIn(home, ("Alpha", "Bravo"))

    def test_a_non_tournament_draft_has_nothing_to_look_up(self):
        import handlers.challenge as challenge
        draft = _draft(turn="complete", host_team="Alpha", target_team="Bravo",
                       is_tournament=False, tournament_id=None)
        self.assertEqual(challenge._resolve_fixture_venue(draft), (None, None))

    def test_announcing_a_locked_pitch_says_nobody_picks_it(self):
        import handlers.challenge as challenge
        draft = _draft(turn="complete", host_team="Alpha", target_team="Bravo",
                       pitch_locked=True, home_team="Alpha")
        text = challenge._apply_pitch(draft, "Dusty")
        self.assertEqual(draft["pitch_type"], "Dusty")
        self.assertIn("Alpha", text)
        self.assertIn("Nobody picks the surface", text)

    def test_a_host_picked_pitch_carries_no_lock_notice(self):
        import handlers.challenge as challenge
        draft = _draft(turn="complete", host_team="Alpha", target_team="Bravo")
        text = challenge._apply_pitch(draft, "Flat")
        self.assertNotIn("Nobody picks the surface", text)


if __name__ == "__main__":
    unittest.main()
