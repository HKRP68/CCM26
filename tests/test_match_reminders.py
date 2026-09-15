"""``/remindmatch`` — nudging two teams to play the fixture they still owe.

A tournament stalls because two people each think the other will start the
match. This feature is the message that goes the other way, and almost all of
its risk is in the same place: it tags people and lands in their DMs, so getting
it slightly wrong turns it into spam that everybody mutes.

So the tests here are mostly about restraint:

  • **who it reaches** — the owner *and* every co-owner of *both* sides, each
    person once however many teams they run, and nobody else;
  • **what it leaves alone** — matches already played, fixtures reminded about
    in the last twelve hours, and knockout slots that don't have two teams yet;
  • **who may send one** — bot admins anywhere, a team owner only for their own
    team, and nobody else at all.

Plus the card itself, because it is what people actually read.
"""

import itertools
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

_TG = itertools.count(660_001)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.tournament_service", "services.cl_tournament_view",
                 "services.match_reminder_service")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE

    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = {name: sys.modules.get(name) for name in _MODULE_NAMES}
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)

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
    for name, module in _SAVED_MODULES.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


class ReminderCase(unittest.TestCase):
    """Three franchises, owners and co-owners assigned, one round-robin."""

    def setUp(self):
        import json
        from database import get_session
        from models import (ChallengeMode, ChallengeLeague, ChallengeTeam,
                            Tournament, TournamentTeam, User)
        from services import match_reminder_service

        self.session = get_session()
        self.mrs = match_reminder_service

        mode = ChallengeMode(name=f"Mode {next(_TG)}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(mode_id=mode.id, name=f"League {next(_TG)}",
                                      short_code="RMD",
                                      tournament_command="ciplcup")
        self.session.add(self.league)
        self.session.flush()
        self.tour = Tournament(name="Summer Trophy", league_id=self.league.id,
                               kind="challenge", status="active",
                               is_active=True, format="League",
                               league_format="single_rr", overs=20, max_teams=8)
        self.session.add(self.tour)
        self.session.flush()

        # Owners: SRH is run by two people, RR by one, CSK by nobody.
        self.owner_srh = next(_TG)
        self.co_srh = next(_TG)
        self.owner_rr = next(_TG)
        self.outsider = next(_TG)

        specs = [
            ("Sunrisers Hyderabad", "SRH", "#F57C00", self.owner_srh, [self.co_srh]),
            ("Rajasthan Royals", "RR", "#EC407A", self.owner_rr, []),
            ("Chennai Super Kings", "CSK", None, None, []),
        ]
        self.teams = {}
        for i, (name, short, colour, owner, cos) in enumerate(specs):
            ct = ChallengeTeam(league_id=self.league.id, name=name,
                               short_name=short, primary_color=colour,
                               sort_order=i)
            self.session.add(ct)
            self.session.flush()
            tt = TournamentTeam(
                tournament_id=self.tour.id, challenge_team_id=ct.id, name=name,
                short_name=short, sort_order=i, owner_tg_id=owner,
                owner_name=f"Owner {owner}" if owner else None,
                co_owner_ids_json=json.dumps(cos) if cos else None)
            self.session.add(tt)
            self.session.flush()
            self.teams[short] = tt

        # The bot has seen a username for the co-owner but not for the owner:
        # both have to come out as something a person recognises.
        self.session.add(User(telegram_id=self.co_srh, username="cookinmeth",
                              first_name="Cook"))
        self.session.add(User(telegram_id=self.owner_srh, first_name="Boss"))
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def fixture(self, a, b, status="scheduled", **kwargs):
        from models import TournamentMatch
        row = TournamentMatch(
            tournament_id=self.tour.id, team1_id=self.teams[a].id,
            team2_id=self.teams[b].id, status=status, stage="league", **kwargs)
        self.session.add(row)
        self.session.commit()
        return row


# ══════════════════════════════════════════════════════════════════════
# What still has to be played
# ══════════════════════════════════════════════════════════════════════

class PendingFixtureTests(ReminderCase):
    def test_only_unplayed_fixtures_are_chased(self):
        self.fixture("SRH", "RR", match_no=1)
        self.fixture("SRH", "CSK", status="completed", match_no=2)
        self.fixture("RR", "CSK", status="live", match_no=3)
        rows = self.mrs.pending_fixtures(self.session, self.tour.id)
        self.assertEqual([f.match_no for f in rows], [1])

    def test_a_knockout_slot_with_nobody_in_it_is_not_chased(self):
        """"Winner of Qualifier 1" has nobody to remind yet."""
        from models import TournamentMatch
        self.session.add(TournamentMatch(
            tournament_id=self.tour.id, status="scheduled", stage="final",
            match_no=9, slot1_label="Winner Q1", slot2_label="Winner Q2"))
        self.session.commit()
        self.assertEqual(self.mrs.pending_fixtures(self.session, self.tour.id), [])

    def test_one_team_can_be_singled_out(self):
        self.fixture("SRH", "RR", match_no=1)
        self.fixture("RR", "CSK", match_no=2)
        rows = self.mrs.pending_fixtures(self.session, self.tour.id,
                                         team_id=self.teams["SRH"].id)
        self.assertEqual([f.match_no for f in rows], [1])

    def test_so_can_one_pair(self):
        self.fixture("SRH", "RR", match_no=1)
        self.fixture("SRH", "CSK", match_no=2)
        rows = self.mrs.pending_fixtures(self.session, self.tour.id,
                                         team_id=self.teams["SRH"].id,
                                         opponent_id=self.teams["CSK"].id)
        self.assertEqual([f.match_no for f in rows], [2])


# ══════════════════════════════════════════════════════════════════════
# Who hears about it
# ══════════════════════════════════════════════════════════════════════

class ContactTests(ReminderCase):
    def test_co_owners_are_reached_as_well_as_the_owner(self):
        """A franchise run by two people is stalled by whichever of them didn't
        see it."""
        ids = [tg for tg, _m in self.mrs.team_contacts(self.session,
                                                       self.teams["SRH"])]
        self.assertEqual(ids, [self.owner_srh, self.co_srh])

    def test_a_known_username_is_used_because_that_is_what_pings_them(self):
        contacts = dict(self.mrs.team_contacts(self.session, self.teams["SRH"]))
        self.assertEqual(contacts[self.co_srh], "@cookinmeth")

    def test_somebody_with_no_username_still_reads_as_a_person(self):
        """A raw Telegram id in a chat tells nobody who is being asked."""
        contacts = dict(self.mrs.team_contacts(self.session, self.teams["SRH"]))
        self.assertIn(f'tg://user?id={self.owner_srh}', contacts[self.owner_srh])
        self.assertIn("Boss", contacts[self.owner_srh])

    def test_a_team_nobody_runs_has_nobody_to_tell(self):
        self.assertEqual(
            self.mrs.team_contacts(self.session, self.teams["CSK"]), [])

    def test_the_same_person_listed_twice_is_still_one_person(self):
        import json
        self.teams["RR"].co_owner_ids_json = json.dumps([self.owner_rr])
        self.session.commit()
        ids = [tg for tg, _m in self.mrs.team_contacts(self.session,
                                                       self.teams["RR"])]
        self.assertEqual(ids, [self.owner_rr])


# ══════════════════════════════════════════════════════════════════════
# The card
# ══════════════════════════════════════════════════════════════════════

class CardTests(ReminderCase):
    def render(self, fixtures, a="SRH", b="RR", **kwargs):
        return self.mrs.render_reminder(
            self.session, self.tour, fixtures, self.teams[a], self.teams[b],
            **kwargs)

    def test_the_card_names_both_sides_the_match_and_the_command(self):
        fx = self.fixture("SRH", "RR", match_no=14, pitch_type="Dusty",
                          home_team_id=self.teams["SRH"].id)
        text = self.render([fx], command="/ciplcup")
        self.assertIn("PENDING MATCH", text)
        self.assertIn("Sunrisers Hyderabad", text)
        self.assertIn("Rajasthan Royals", text)
        self.assertIn("M14", text)
        self.assertIn("Dusty", text)
        self.assertIn("/ciplcup", text)

    def test_it_says_how_many_are_left_and_counts_them_properly(self):
        one = self.fixture("SRH", "RR", match_no=1)
        self.assertIn("<b>1</b> league match left", self.render([one]))
        two = self.fixture("SRH", "RR", match_no=2)
        self.assertIn("<b>2</b> league matches left", self.render([one, two]))

    def test_it_points_each_side_at_its_own_schedule(self):
        fx = self.fixture("SRH", "RR", match_no=1)
        text = self.render([fx])
        self.assertIn("/clsd SRH", text)
        self.assertIn("/clsd RR", text)

    def test_everyone_who_runs_either_side_is_tagged(self):
        fx = self.fixture("SRH", "RR", match_no=1)
        text = self.render([fx])
        self.assertIn("@cookinmeth", text)
        self.assertIn(str(self.owner_srh), text)
        self.assertIn(str(self.owner_rr), text)

    def test_a_team_nobody_runs_says_so_rather_than_showing_a_gap(self):
        fx = self.fixture("SRH", "CSK", match_no=1)
        text = self.render([fx], b="CSK")
        self.assertIn("nobody is assigned", text)

    def test_the_dm_tells_the_reader_which_side_is_theirs(self):
        """A co-owner reading a card full of franchise names needs to know
        instantly that this one is on them."""
        fx = self.fixture("SRH", "RR", match_no=1)
        text = self.render([fx], for_tg_id=self.co_srh)
        self.assertIn("You run <b>Sunrisers Hyderabad</b>", text)
        self.assertNotIn("You run", self.render([fx], for_tg_id=self.outsider))

    def test_the_two_sides_get_different_badges(self):
        """A chat full of text needs the sides telling apart at a glance."""
        srh = self.mrs.team_badge(self.session, self.teams["SRH"])
        rr = self.mrs.team_badge(self.session, self.teams["RR"])
        self.assertEqual(srh, "🟠")   # the franchise's own orange
        self.assertEqual(rr, "🩷")    # …and pink
        self.assertNotEqual(srh, rr)

    def test_a_team_with_no_colour_still_gets_the_same_badge_every_time(self):
        first = self.mrs.team_badge(self.session, self.teams["CSK"])
        self.assertTrue(first)
        self.assertEqual(first, self.mrs.team_badge(self.session, self.teams["CSK"]))


# ══════════════════════════════════════════════════════════════════════
# Grouping, and the cooldown that keeps it from becoming spam
# ══════════════════════════════════════════════════════════════════════

class NudgeTests(ReminderCase):
    def build(self, **kwargs):
        fixtures = self.mrs.pending_fixtures(self.session, self.tour.id)
        return self.mrs.build_nudges(self.session, self.tour, fixtures, **kwargs)

    def test_both_legs_of_a_pair_are_one_conversation(self):
        """Two teams with two legs left owe each other two matches — that is one
        notification, not two."""
        self.fixture("SRH", "RR", match_no=1)
        self.fixture("RR", "SRH", match_no=7)
        nudges = self.build()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(len(nudges[0].fixtures), 2)

    def test_each_person_appears_once_per_nudge(self):
        self.fixture("SRH", "RR", match_no=1)
        recipients = [tg for tg, _m in self.build()[0].recipients]
        self.assertEqual(sorted(recipients),
                         sorted([self.owner_srh, self.co_srh, self.owner_rr]))

    def test_a_pair_reminded_this_morning_is_held_back_not_sent_again(self):
        fx = self.fixture("SRH", "RR", match_no=1)
        nudge = self.build()[0]
        self.mrs.record_send(self.session, self.tour, nudge, delivered=3)
        self.session.commit()

        held = self.build()[0]
        self.assertIsNotNone(held.skipped_until)
        self.assertEqual(held.recipients, [])
        self.assertIn(fx.id, held.fixture_ids)

    def test_a_held_back_pair_can_be_overridden_on_purpose(self):
        self.fixture("SRH", "RR", match_no=1)
        self.mrs.record_send(self.session, self.tour, self.build()[0])
        self.session.commit()
        forced = self.build(force=True)[0]
        self.assertIsNone(forced.skipped_until)
        self.assertTrue(forced.recipients)

    def test_the_cooldown_expires(self):
        self.fixture("SRH", "RR", match_no=1)
        self.mrs.record_send(self.session, self.tour, self.build()[0],
                             now=datetime.utcnow() - timedelta(days=2))
        self.session.commit()
        self.assertIsNone(self.build()[0].skipped_until)

    def test_a_newly_scheduled_leg_is_worth_a_nudge_even_if_the_first_was_not(self):
        """The pair is quiet only while EVERY fixture in it is quiet."""
        self.fixture("SRH", "RR", match_no=1)
        self.mrs.record_send(self.session, self.tour, self.build()[0])
        self.session.commit()
        self.fixture("RR", "SRH", match_no=7)
        self.assertIsNone(self.build()[0].skipped_until)

    def test_the_send_is_written_down_against_every_fixture_it_covered(self):
        from models import TournamentMatchReminder
        self.fixture("SRH", "RR", match_no=1)
        self.fixture("RR", "SRH", match_no=7)
        nudge = self.build()[0]
        self.mrs.record_send(self.session, self.tour, nudge, sent_by_tg_id=42,
                             chat_id=-100, delivered=3)
        self.session.commit()
        rows = (self.session.query(TournamentMatchReminder)
                .filter_by(tournament_id=self.tour.id).all())
        self.assertEqual(len(rows), 2)
        self.assertEqual({r.sent_by_tg_id for r in rows}, {42})
        self.assertEqual({r.chat_id for r in rows}, {-100})

    def test_the_roundup_names_every_stalled_pair(self):
        self.fixture("SRH", "RR", match_no=1)
        self.fixture("SRH", "CSK", match_no=2)
        text = self.mrs.render_roundup(self.session, self.tour, self.build(),
                                       command="/ciplcup")
        self.assertIn("Rajasthan Royals", text)
        self.assertIn("Chennai Super Kings", text)
        self.assertIn("/ciplcup", text)


# ══════════════════════════════════════════════════════════════════════
# Who is allowed to make the bot ping people
# ══════════════════════════════════════════════════════════════════════

class PermissionTests(ReminderCase):
    def setUp(self):
        super().setUp()
        from handlers import match_reminders
        self.handler = match_reminders
        self._prev_admins = os.environ.get("BOT_ADMIN_IDS")
        self.admin = next(_TG)
        os.environ["BOT_ADMIN_IDS"] = str(self.admin)
        self.fixture("SRH", "RR", match_no=1)
        self.fixture("SRH", "CSK", match_no=2)

    def tearDown(self):
        if self._prev_admins is None:
            os.environ.pop("BOT_ADMIN_IDS", None)
        else:
            os.environ["BOT_ADMIN_IDS"] = self._prev_admins
        super().tearDown()

    def plan(self, tg_id, **kwargs):
        return self.handler.build_plan(self.session, tg_id,
                                       tour_id=self.tour.id, **kwargs)

    def test_an_admin_chases_the_whole_tournament(self):
        plan = self.plan(self.admin)
        self.assertIsNone(plan.error)
        self.assertEqual(len(plan.sending), 2)

    def test_a_team_owner_chases_their_own_fixtures(self):
        plan = self.plan(self.owner_srh)
        self.assertIsNone(plan.error)
        self.assertEqual(len(plan.sending), 2)   # SRH plays both of them

    def test_a_co_owner_is_the_owners_equal_here_too(self):
        self.assertIsNone(self.plan(self.co_srh).error)

    def test_somebody_who_runs_nothing_cannot_make_the_bot_ping_anyone(self):
        plan = self.plan(self.outsider)
        self.assertIn("own or co-own", plan.error)
        self.assertEqual(plan.sending, [])

    def test_an_owner_cannot_chase_a_team_that_is_not_theirs(self):
        plan = self.plan(self.owner_rr, team_id=self.teams["CSK"].id)
        self.assertIn("own or co-own", plan.error)

    def test_an_owner_cannot_force_past_the_cooldown(self):
        """A non-admin's cooldown is not negotiable."""
        self.assertFalse(self.plan(self.owner_srh, force=True).force)
        self.assertTrue(self.plan(self.admin, force=True).force)

    def test_the_preview_says_who_would_be_pinged_before_anything_is_sent(self):
        plan = self.plan(self.admin)
        text = self.handler.render_preview(self.session, plan)
        self.assertIn("@cookinmeth", text)
        self.assertIn("people to notify", text)
        self.assertIn("Tap Send", text)

    def test_a_tournament_with_nothing_left_says_so(self):
        for fx in self.mrs.pending_fixtures(self.session, self.tour.id):
            fx.status = "completed"
        self.session.commit()
        text = self.handler.render_preview(self.session, self.plan(self.admin))
        self.assertIn("Nothing is waiting", text)

    def test_vs_splits_two_team_names_and_force_is_not_part_of_one(self):
        self.assertEqual(
            self.handler._split_args(["Sunrisers", "Hyderabad", "vs", "RR"]),
            ("Sunrisers Hyderabad", "RR", False))
        self.assertEqual(self.handler._split_args(["SRH", "force"]),
                         ("SRH", "", True))
        self.assertEqual(self.handler._split_args([]), ("", "", False))


if __name__ == "__main__":
    unittest.main()
