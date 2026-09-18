"""Challenge League Tournament rules: owned teams, fixed pitches, home sides.

What these cover:

  • a team belongs to the people who run it — one owner plus any number of
    co-owners, all equals — an owner-locked tournament refuses every other
    player, and a team nobody claimed stays open to everybody
  • a draft tournament inherits its franchise owners from the draft, so nobody
    re-types a list of Telegram ids
  • the schedule stamps each fixture with a home side and the one surface it may
    be played on, and "home" mode uses the home team's own pitch
  • the tournament's overseas-in-XI limits override the league's, and a blank
    limit really does inherit rather than reading as zero
  • the fixture card players see strikes through the matches that are done
"""

import itertools
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_TG = itertools.count(970_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.cl_tournament_view",
                 "services.league_schedule_service", "services.knockout_service")


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
    try:
        _ENGINE.dispose()
    except Exception:
        pass
    if _PREV_DATABASE_URL is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = _PREV_DATABASE_URL
    _module_swap.restore(_SAVED_MODULES)
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class TournamentCase(unittest.TestCase):
    """A Challenge League with four teams, entered into one tournament."""

    TEAM_NAMES = ("Alpha", "Bravo", "Charlie", "Delta")

    def setUp(self):
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam)
        from services import tournament_service, league_schedule_service

        self.session = get_session()
        self.ts = tournament_service
        self.lss = league_schedule_service

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                      short_code="TST", min_overseas=2,
                                      max_overseas=4)
        self.session.add(self.league)
        self.session.flush()
        self.teams = []
        for i, name in enumerate(self.TEAM_NAMES):
            ct = ChallengeTeam(league_id=self.league.id, name=name, sort_order=i)
            self.session.add(ct)
            self.teams.append(ct)
        self.session.flush()

        self.tour = Tournament(name="Test Trophy", league_id=self.league.id,
                               league_name=self.league.name, kind="challenge",
                               status="active", format="League",
                               league_format="single_rr", overs=20, max_teams=8)
        self.session.add(self.tour)
        self.session.flush()
        self.tteams = []
        for i, ct in enumerate(self.teams):
            tt = TournamentTeam(tournament_id=self.tour.id, challenge_team_id=ct.id,
                                name=ct.name, sort_order=i)
            self.session.add(tt)
            self.tteams.append(tt)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def by_name(self, name):
        return next(tt for tt in self.tteams if tt.name == name)


# ══════════════════════════════════════════════════════════════════════
# Team ownership
# ══════════════════════════════════════════════════════════════════════

class TeamOwnershipTests(TournamentCase):
    def test_unenforced_tournament_lets_anyone_use_any_team(self):
        self.by_name("Alpha").owner_tg_id = 111
        self.session.commit()
        ok, msg = self.ts.may_use_team(self.session, self.tour, "Alpha", 222)
        self.assertTrue(ok)
        self.assertIsNone(msg)

    def test_owner_locked_team_refuses_everyone_but_its_owner(self):
        self.tour.enforce_team_owner = True
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, 111, "Ana")
        self.session.commit()

        ok, _ = self.ts.may_use_team(self.session, self.tour, "Alpha", 111)
        self.assertTrue(ok, "the owner must be able to play their own team")

        ok, msg = self.ts.may_use_team(self.session, self.tour, "Alpha", 222)
        self.assertFalse(ok)
        self.assertIn("Ana", msg)

    def test_an_unclaimed_team_stays_open_even_when_locked(self):
        # A half-assigned tournament must not lock its own players out of the
        # teams an admin simply hasn't got to yet.
        self.tour.enforce_team_owner = True
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, 111)
        self.session.commit()
        ok, _ = self.ts.may_use_team(self.session, self.tour, "Bravo", 222)
        self.assertTrue(ok)

    def test_clearing_an_owner_frees_the_team(self):
        self.tour.enforce_team_owner = True
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, 111, "Ana")
        self.session.commit()
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, None)
        self.session.commit()
        ok, _ = self.ts.may_use_team(self.session, self.tour, "Alpha", 222)
        self.assertTrue(ok)
        self.assertEqual(self.ts.team_owners(self.session, self.tour.id), {})

    def test_owned_team_names_is_scoped_to_the_user(self):
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, 111)
        self.ts.set_team_owner(self.session, self.by_name("Bravo").id, 222)
        self.session.commit()
        self.assertEqual(
            self.ts.owned_team_names(self.session, self.tour.id, 111), {"Alpha"})
        self.assertEqual(
            self.ts.owned_team_names(self.session, self.tour.id, 999), set())


class CoOwnershipTests(TournamentCase):
    """A franchise can be run by several people; all of them are equals."""

    def setUp(self):
        super().setUp()
        self.tour.enforce_team_owner = True
        self.alpha = self.by_name("Alpha")
        self.ts.set_team_owner(self.session, self.alpha.id, 111, "Ana")
        self.ts.set_co_owners(self.session, self.alpha.id, [222, 333])
        self.session.commit()

    def test_a_co_owner_may_play_the_team(self):
        for who in (111, 222, 333):
            ok, msg = self.ts.may_use_team(self.session, self.tour, "Alpha", who)
            self.assertTrue(ok, f"{who} should be able to play Alpha: {msg}")

    def test_an_outsider_still_cannot(self):
        ok, msg = self.ts.may_use_team(self.session, self.tour, "Alpha", 444)
        self.assertFalse(ok)
        self.assertIn("co-own", msg)

    def test_a_co_owner_sees_the_team_as_theirs(self):
        self.assertEqual(
            self.ts.owned_team_names(self.session, self.tour.id, 222), {"Alpha"})

    def test_members_list_puts_the_owner_first(self):
        self.assertEqual(self.ts.team_member_ids(self.alpha), [111, 222, 333])

    def test_the_form_box_is_cleaned_of_junk_and_duplicates(self):
        ids = self.ts.set_co_owners(
            self.session, self.alpha.id,
            [" 222 ", "222", "abc", "", "-5", "0", "111", "444"])
        self.session.commit()
        # 111 is the owner and 222 is repeated; the rest of the noise is dropped.
        self.assertEqual(ids, [222, 444])

    def test_clearing_the_co_owners_stores_null_not_an_empty_list(self):
        self.ts.set_co_owners(self.session, self.alpha.id, [])
        self.session.commit()
        self.assertIsNone(self.alpha.co_owner_ids_json)
        self.assertEqual(self.ts.co_owner_ids(self.alpha), [])

    def test_add_and_remove_one_at_a_time(self):
        self.ts.add_co_owner(self.session, self.alpha.id, 444)
        self.session.commit()
        self.assertIn(444, self.ts.co_owner_ids(self.alpha))
        self.ts.remove_co_owner(self.session, self.alpha.id, 222)
        self.session.commit()
        self.assertEqual(self.ts.co_owner_ids(self.alpha), [333, 444])

    def test_adding_the_owner_or_a_duplicate_is_refused(self):
        with self.assertRaises(ValueError):
            self.ts.add_co_owner(self.session, self.alpha.id, 111)
        with self.assertRaises(ValueError):
            self.ts.add_co_owner(self.session, self.alpha.id, 222)

    def test_promoting_a_co_owner_does_not_leave_them_listed_twice(self):
        self.ts.set_team_owner(self.session, self.alpha.id, 222, "Bo")
        self.session.commit()
        self.assertEqual(self.ts.co_owner_ids(self.alpha), [333])
        self.assertEqual(self.ts.team_member_ids(self.alpha), [222, 333])

    def test_clearing_the_owner_leaves_the_co_owners_running_the_team(self):
        # Dropping them too would hand a claimed team back to the whole chat.
        self.ts.set_team_owner(self.session, self.alpha.id, None)
        self.session.commit()
        self.assertTrue(self.ts.team_is_claimed(self.alpha))
        ok, _ = self.ts.may_use_team(self.session, self.tour, "Alpha", 333)
        self.assertTrue(ok)
        ok, _ = self.ts.may_use_team(self.session, self.tour, "Alpha", 444)
        self.assertFalse(ok)

    def test_a_broken_json_column_reads_as_no_co_owners(self):
        # A hand-edited column must not take the team picker down mid-match.
        self.alpha.co_owner_ids_json = "{not json"
        self.session.commit()
        self.assertEqual(self.ts.co_owner_ids(self.alpha), [])
        self.assertEqual(self.ts.team_member_ids(self.alpha), [111])

    def test_team_owners_lists_everyone_who_runs_each_team(self):
        owners = self.ts.team_owners(self.session, self.tour.id)
        self.assertEqual(owners["Alpha"], [111, 222, 333])
        self.assertNotIn("Bravo", owners, "an unclaimed team is not listed")


class DraftOwnerInheritanceTests(TournamentCase):
    """A league published from a Tournament Draft already knows its owners."""

    def _make_draft(self):
        from models import PlayerDraft, DraftTeam
        draft = PlayerDraft(name="Test Draft", status="completed",
                            league_id=self.league.id)
        self.session.add(draft)
        self.session.flush()
        self.session.add(DraftTeam(draft_id=draft.id, name="Alpha",
                                   owner_tg_id=4242, owner_name="Ana",
                                   co_owner_ids_json="[61, 62]"))
        self.session.add(DraftTeam(draft_id=draft.id, name="Bravo",
                                   owner_tg_id=5353, owner_name="Bo"))
        self.session.commit()
        return draft

    def test_owner_is_read_back_from_the_draft(self):
        self._make_draft()
        tg_id, name, extras = self.ts.draft_owner_for_team(
            self.session, self.league.id, "Alpha")
        self.assertEqual((tg_id, name), (4242, "Ana"))
        self.assertEqual(extras, [61, 62])

    def test_a_league_with_no_draft_has_no_owners_to_inherit(self):
        self.assertEqual(
            self.ts.draft_owner_for_team(self.session, self.league.id, "Alpha"),
            (None, None, []))

    def test_sync_fills_the_blanks_and_never_overwrites(self):
        self._make_draft()
        # Bravo was assigned by hand; the draft must not take it back.
        self.ts.set_team_owner(self.session, self.by_name("Bravo").id, 999, "Hand")
        self.session.commit()

        changed = self.ts.sync_owners_from_draft(self.session, self.tour.id)
        self.session.commit()

        self.assertEqual(changed, 1)
        self.assertEqual(self.by_name("Alpha").owner_tg_id, 4242)
        self.assertEqual(self.by_name("Bravo").owner_tg_id, 999)
        self.assertIsNone(self.by_name("Charlie").owner_tg_id)

    def test_co_owners_come_across_with_the_owner(self):
        self._make_draft()
        self.ts.sync_owners_from_draft(self.session, self.tour.id)
        self.session.commit()
        self.assertEqual(self.ts.co_owner_ids(self.by_name("Alpha")), [61, 62])

    def test_a_team_run_only_by_co_owners_is_left_alone_by_sync(self):
        # Somebody already runs Charlie, even though it has no owner — the draft
        # must not quietly take it over.
        self._make_draft()
        self.ts.set_co_owners(self.session, self.by_name("Charlie").id, [31337])
        self.session.commit()
        self.ts.sync_owners_from_draft(self.session, self.tour.id)
        self.session.commit()
        self.assertIsNone(self.by_name("Charlie").owner_tg_id)
        self.assertEqual(self.ts.co_owner_ids(self.by_name("Charlie")), [31337])


# ══════════════════════════════════════════════════════════════════════
# Per-fixture pitch + home side
# ══════════════════════════════════════════════════════════════════════

class FixtureVenueTests(TournamentCase):
    def test_host_mode_leaves_the_pitch_open(self):
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fixtures = self.lss.list_fixtures(self.session, self.tour.id)
        self.assertTrue(fixtures)
        self.assertTrue(all(fx.pitch_type is None for fx in fixtures))
        # A home side is still recorded — it costs nothing and reads well.
        self.assertTrue(all(fx.home_team_id == fx.team1_id for fx in fixtures))

    def test_fixture_mode_stamps_every_fixture_with_a_real_surface(self):
        self.tour.pitch_mode = "fixture"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fixtures = self.lss.list_fixtures(self.session, self.tour.id)
        self.assertEqual(len(fixtures), 6)  # C(4,2)
        for fx in fixtures:
            self.assertIn(fx.pitch_type, self.lss.FIXTURE_PITCHES)

    def test_home_mode_uses_the_home_team_declared_pitch(self):
        self.tour.pitch_mode = "home"
        for tt in self.tteams:
            tt.home_pitch = "Dusty"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        for fx in self.lss.list_fixtures(self.session, self.tour.id):
            self.assertEqual(fx.pitch_type, "Dusty")

    def test_home_mode_falls_back_when_a_team_declared_nothing(self):
        # A half-configured tournament must still produce a playable schedule.
        self.tour.pitch_mode = "home"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        for fx in self.lss.list_fixtures(self.session, self.tour.id):
            self.assertIn(fx.pitch_type, self.lss.FIXTURE_PITCHES)

    def test_a_second_pass_does_not_reshuffle_settled_fixtures(self):
        self.tour.pitch_mode = "fixture"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        before = {fx.id: fx.pitch_type
                  for fx in self.lss.list_fixtures(self.session, self.tour.id)}
        self.assertEqual(self.lss.assign_fixture_venues(self.session, self.tour.id), 0)
        after = {fx.id: fx.pitch_type
                 for fx in self.lss.list_fixtures(self.session, self.tour.id)}
        self.assertEqual(before, after)

    def test_switching_back_to_host_clears_the_stamps(self):
        self.tour.pitch_mode = "fixture"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        self.tour.pitch_mode = "host"
        self.lss.assign_fixture_venues(self.session, self.tour.id, overwrite=True)
        self.session.commit()
        for fx in self.lss.list_fixtures(self.session, self.tour.id):
            self.assertIsNone(fx.pitch_type)

    def test_re_rolling_pitches_leaves_the_chosen_home_side_alone(self):
        # "Re-roll pitches" must not quietly undo an admin's home/away edits.
        self.tour.pitch_mode = "fixture"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fx = self.lss.list_fixtures(self.session, self.tour.id)[0]
        self.lss.set_fixture_home(self.session, fx.id, fx.team2_id)
        other = self.lss.list_fixtures(self.session, self.tour.id)[1]
        self.lss.set_fixture_home(self.session, other.id, None)  # neutral venue
        self.session.commit()

        self.lss.assign_fixture_venues(self.session, self.tour.id, overwrite=True)
        self.session.commit()

        self.assertEqual(fx.home_team_id, fx.team2_id)
        self.assertIsNone(other.home_team_id)

    def test_a_dangling_home_reference_is_repaired(self):
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fx = self.lss.list_fixtures(self.session, self.tour.id)[0]
        outsider = next(tt for tt in self.tteams
                        if tt.id not in (fx.team1_id, fx.team2_id))
        fx.home_team_id = outsider.id  # e.g. left behind by an older edit
        self.session.commit()
        self.lss.assign_fixture_venues(self.session, self.tour.id)
        self.session.commit()
        self.assertIn(fx.home_team_id, (fx.team1_id, fx.team2_id))

    def test_an_unknown_pitch_is_refused(self):
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fx = self.lss.list_fixtures(self.session, self.tour.id)[0]
        with self.assertRaises(ValueError):
            self.lss.set_fixture_pitch(self.session, fx.id, "Concrete")

    def test_a_played_match_keeps_the_pitch_it_was_played_on(self):
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fx = self.lss.list_fixtures(self.session, self.tour.id)[0]
        fx.status = "completed"
        fx.pitch_type = "Green"
        self.session.commit()
        with self.assertRaises(ValueError):
            self.lss.set_fixture_pitch(self.session, fx.id, "Flat")
        self.lss.assign_fixture_venues(self.session, self.tour.id, overwrite=True)
        self.session.commit()
        self.session.refresh(fx)
        self.assertEqual(fx.pitch_type, "Green")

    def test_home_team_must_be_one_of_the_two_sides(self):
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fx = self.lss.list_fixtures(self.session, self.tour.id)[0]
        outsider = next(tt for tt in self.tteams
                        if tt.id not in (fx.team1_id, fx.team2_id))
        with self.assertRaises(ValueError):
            self.lss.set_fixture_home(self.session, fx.id, outsider.id)
        self.lss.set_fixture_home(self.session, fx.id, fx.team2_id)
        self.session.commit()
        self.assertEqual(fx.home_team_id, fx.team2_id)

    def test_swapping_a_team_out_moves_the_home_flag_with_it(self):
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fx = self.lss.list_fixtures(self.session, self.tour.id)[0]
        self.assertEqual(fx.home_team_id, fx.team1_id)
        replacement = next(tt for tt in self.tteams
                           if tt.id not in (fx.team1_id, fx.team2_id))
        self.lss.swap_fixture_team(self.session, fx.id, 1, replacement.id)
        self.session.commit()
        self.assertEqual(fx.home_team_id, replacement.id)
        self.assertIn(fx.home_team_id, (fx.team1_id, fx.team2_id))

    def test_the_locked_pitch_is_readable_by_team_name(self):
        self.tour.pitch_mode = "fixture"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fx = self.lss.fixture_for_pair(self.session, self.tour.id, "Alpha", "Bravo")
        self.assertIsNotNone(fx)
        pitch, home = self.lss.locked_pitch_for_pair(
            self.session, self.tour, "Alpha", "Bravo")
        self.assertEqual(pitch, fx.pitch_type)
        self.assertIn(home, ("Alpha", "Bravo"))

    def test_host_mode_reports_no_locked_pitch(self):
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        self.assertEqual(
            self.lss.locked_pitch_for_pair(self.session, self.tour, "Alpha", "Bravo"),
            (None, None))

    def test_no_schedule_means_no_locked_pitch(self):
        self.tour.pitch_mode = "fixture"
        self.session.commit()
        self.assertEqual(
            self.lss.locked_pitch_for_pair(self.session, self.tour, "Alpha", "Bravo"),
            (None, None))


# ══════════════════════════════════════════════════════════════════════
# Overseas limits
# ══════════════════════════════════════════════════════════════════════

class OverseasLimitTests(TournamentCase):
    def test_unset_limits_inherit_the_league(self):
        self.assertEqual(
            self.ts.overseas_limits(self.session, self.tour, self.league), (2, 4))

    def test_tournament_limits_win(self):
        self.tour.min_overseas = 1
        self.tour.max_overseas = 3
        self.session.commit()
        self.assertEqual(
            self.ts.overseas_limits(self.session, self.tour, self.league), (1, 3))

    def test_each_bound_inherits_independently(self):
        self.tour.max_overseas = 6
        self.session.commit()
        self.assertEqual(
            self.ts.overseas_limits(self.session, self.tour, self.league), (2, 6))

    def test_zero_is_a_real_limit_not_an_empty_box(self):
        self.tour.min_overseas = 0
        self.tour.max_overseas = 0
        self.session.commit()
        self.assertEqual(
            self.ts.overseas_limits(self.session, self.tour, self.league), (0, 0))

    def test_values_are_clamped_and_never_inverted(self):
        self.tour.min_overseas = 9
        self.tour.max_overseas = 2
        self.session.commit()
        lo, hi = self.ts.overseas_limits(self.session, self.tour, self.league)
        self.assertLessEqual(lo, hi)

    def test_the_league_is_looked_up_when_not_passed(self):
        self.assertEqual(self.ts.overseas_limits(self.session, self.tour), (2, 4))


# ══════════════════════════════════════════════════════════════════════
# The card players actually read
# ══════════════════════════════════════════════════════════════════════

class FixtureCardTests(TournamentCase):
    def _render(self, viewer=None):
        from services import cl_tournament_view as ctv
        return ctv.render_fixtures(self.session, self.tour, viewer_tg_id=viewer)

    def test_a_played_match_is_struck_through_with_its_result(self):
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        fx = self.lss.list_fixtures(self.session, self.tour.id)[0]
        fx.status = "completed"
        fx.result_text = "Alpha won by 12 runs"
        self.session.commit()

        text = self._render()
        self.assertIn("<s>", text)
        self.assertIn("Alpha won by 12 runs", text)

    def test_an_unplayed_match_shows_its_pitch_and_is_not_struck_through(self):
        self.tour.pitch_mode = "fixture"
        self.session.commit()
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        text = self._render()
        self.assertNotIn("<s>", text)
        self.assertIn("🌱", text)

    def test_an_owner_sees_their_own_next_matches_pulled_out(self):
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, 777, "Ana")
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        self.assertIn("Your next matches", self._render(viewer=777))
        self.assertNotIn("Your next matches", self._render(viewer=888))

    def test_a_co_owner_sees_the_teams_matches_as_their_own(self):
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, 777, "Ana")
        self.ts.set_co_owners(self.session, self.by_name("Alpha").id, [888])
        self.lss.generate_schedule(self.session, self.tour.id)
        self.session.commit()
        self.assertIn("Your next matches", self._render(viewer=888))

    def test_no_schedule_says_so_rather_than_showing_an_empty_list(self):
        self.assertIn("free-play", self._render())

    def test_the_team_card_names_the_owner_and_flags_the_lock(self):
        from services import cl_tournament_view as ctv
        self.tour.enforce_team_owner = True
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, 777, "Ana")
        self.session.commit()
        text = ctv.render_teams(self.session, self.tour)
        self.assertIn("Ana", text)
        self.assertIn("unowned", text)
        self.assertIn("Owner-locked", text)

    def test_the_team_card_counts_the_co_owners(self):
        from services import cl_tournament_view as ctv
        self.ts.set_team_owner(self.session, self.by_name("Alpha").id, 777, "Ana")
        self.ts.set_co_owners(self.session, self.by_name("Alpha").id, [888, 999])
        self.session.commit()
        self.assertIn("🤝 +2", ctv.render_teams(self.session, self.tour))


if __name__ == "__main__":
    unittest.main()
