"""Past seasons: delete a tournament but keep its stats, and link seasons.

  • **Delete, keep stats.** The tournament is gone; its player rows live on as
    a saved season, and Total Season Stats still counts them.
  • **Delete everything.** Nothing is kept.
  • **Links.** Explicit links replace the league default, and a linked
    tournament that is later deleted becomes a link to its saved season — the
    total does not move.
"""

import itertools
import unittest
from datetime import datetime

from tests.test_tournament_rating_rule import (  # noqa: F401 — module fixtures
    setUpModule, tearDownModule)

_SEQ = itertools.count(500)


class SeasonCase(unittest.TestCase):

    def setUp(self):
        from database import get_session
        from models import (ChallengeLeague, ChallengeMode, Player, Tournament,
                            TournamentPlayerStats, User)
        from services import season_archive, tournament_service

        self.SA = season_archive
        self.TS = tournament_service
        self.session = get_session()
        tag = next(_SEQ)
        mode = ChallengeMode(name=f"Archive mode {tag}")
        self.session.add(mode)
        self.session.flush()
        self.league = ChallengeLeague(name=f"Archive league {tag}", mode_id=mode.id)
        self.other_league = ChallengeLeague(name=f"Elsewhere {tag}", mode_id=mode.id)
        self.session.add_all([self.league, self.other_league])
        self.session.flush()
        self.user = User(telegram_id=990_000 + tag, first_name="Owner")
        self.player = Player(name=f"Kohli {tag}", rating=90, category="Batsman",
                             country="India", version="Base", is_active=True,
                             bat_hand="Right", bowl_hand="Right",
                             bowl_style="Medium Pacer")
        self.session.add_all([self.user, self.player])
        self.session.flush()

        def tour(league, name, year, status="completed"):
            t = Tournament(name=name, league_id=league.id,
                           league_name=league.name, status=status,
                           created_at=datetime(year, 1, 1))
            self.session.add(t)
            self.session.flush()
            return t

        def stats(t, runs, wickets):
            row = TournamentPlayerStats(
                tournament_id=t.id, user_id=self.user.id,
                player_id=self.player.id, name=self.player.name,
                team_name="RCB", matches=5, bat_runs=runs, bat_balls=runs,
                bat_outs=1, highest_score=runs // 3, bowl_wickets=wickets,
                bowl_runs=40, bowl_balls=30, best_bowl_wickets=wickets,
                best_bowl_runs=20)
            self.session.add(row)
            self.session.flush()
            return row

        self.s1 = tour(self.league, "Season 1", 2024)
        self.s2 = tour(self.league, "Season 2", 2025)
        self.s3 = tour(self.league, "Season 3", 2026, status="active")
        self.cup = tour(self.other_league, "Other Cup", 2025)
        stats(self.s1, 100, 1)
        stats(self.s2, 200, 2)
        self.row = stats(self.s3, 300, 3)
        stats(self.cup, 900, 9)
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def total(self):
        return self.TS.career_player_stats(self.session, self.s3, self.row)


class DeleteKeepsStatsTests(SeasonCase):

    def test_default_total_is_every_season_in_the_league(self):
        total = self.total()
        self.assertEqual(3, total.seasons)
        self.assertEqual(600, total.bat_runs)
        self.assertEqual(["Season 1", "Season 2", "Season 3"],
                         [label for label, _s in total.breakdown])

    def test_deleting_with_keep_stats_saves_the_season(self):
        from models import StatsSeason, Tournament
        s1_id = self.s1.id
        season = self.SA.delete_tournament(self.session, self.s1, keep_stats=True)
        self.session.commit()
        self.assertIsNone(self.session.get(Tournament, s1_id))
        self.assertEqual(1, season.player_count)
        saved = self.session.get(StatsSeason, season.id)
        self.assertEqual("Season 1", saved.name)
        self.assertEqual(self.league.id, saved.league_id)
        total = self.total()
        self.assertEqual(600, total.bat_runs, "the deleted season still counts")
        self.assertEqual(3, total.seasons)

    def test_deleting_everything_keeps_nothing(self):
        self.assertIsNone(
            self.SA.delete_tournament(self.session, self.s1, keep_stats=False))
        self.session.commit()
        self.assertEqual(500, self.total().bat_runs)

    def test_a_saved_season_can_be_removed_for_good(self):
        season = self.SA.delete_tournament(self.session, self.s1)
        self.session.commit()
        self.assertEqual("Season 1",
                         self.SA.delete_saved_season(self.session, season.id))
        self.session.commit()
        self.assertEqual(500, self.total().bat_runs)


class LinkTests(SeasonCase):

    def test_explicit_links_replace_the_league_default(self):
        # Link only Season 2 — and a tournament from another league.
        self.SA.set_linked_refs(self.session, self.s3,
                                [self.SA.tour_ref(self.s2.id),
                                 self.SA.tour_ref(self.cup.id),
                                 self.SA.tour_ref(self.s3.id)])   # self: ignored
        self.session.commit()
        self.assertEqual([self.SA.tour_ref(self.s2.id),
                          self.SA.tour_ref(self.cup.id)],
                         self.SA.linked_refs(self.s3))
        total = self.total()
        self.assertEqual(200 + 900 + 300, total.bat_runs)
        self.assertEqual(3, total.seasons)

    def test_a_linked_tournament_deleted_later_stays_linked_via_its_saved_season(self):
        self.SA.set_linked_refs(self.session, self.s3,
                                [self.SA.tour_ref(self.s2.id)])
        self.session.commit()
        season = self.SA.delete_tournament(self.session, self.s2)
        self.session.commit()
        self.assertEqual([self.SA.saved_ref(season.id)],
                         self.SA.linked_refs(self.s3))
        self.assertEqual(500, self.total().bat_runs)

    def test_choices_list_both_kinds_and_mark_the_linked_ones(self):
        season = self.SA.delete_tournament(self.session, self.s1)
        self.SA.set_linked_refs(self.session, self.s3,
                                [self.SA.saved_ref(season.id)])
        self.session.commit()
        choices = {c["ref"]: c for c in
                   self.SA.season_choices(self.session, self.s3)}
        saved = choices[self.SA.saved_ref(season.id)]
        self.assertTrue(saved["saved"])
        self.assertTrue(saved["linked"])
        self.assertFalse(choices[self.SA.tour_ref(self.s2.id)]["linked"])
        self.assertNotIn(self.SA.tour_ref(self.s3.id), choices)

    def test_clearing_links_goes_back_to_the_default(self):
        self.SA.set_linked_refs(self.session, self.s3,
                                [self.SA.tour_ref(self.cup.id)])
        self.SA.set_linked_refs(self.session, self.s3, [])
        self.session.commit()
        self.assertIsNone(self.s3.linked_seasons_json)
        self.assertEqual(600, self.total().bat_runs)


class TseasonsCommandTests(SeasonCase):
    """/tseasons, driven the way Telegram drives it: two numbered lists,
    then ``running | completed``."""

    ADMIN = 4242

    def run_cmd(self, *args):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch
        from handlers import tournament_admin as H

        replies = []

        async def reply_text(text, **kwargs):
            replies.append(text)

        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=self.ADMIN),
            effective_message=SimpleNamespace(reply_text=reply_text))
        context = SimpleNamespace(args=list(args))
        self.session.commit()
        with patch.object(H, "is_admin", lambda uid: uid == self.ADMIN):
            asyncio.run(H.tseasons_handler(update, context))
        self.session.expire_all()
        return "\n".join(replies)

    def running_no(self, tour):
        ids = [t.id for t in self.SA.running_tournaments(self.session)]
        return str(ids.index(tour.id) + 1)

    def completed_no(self, ref):
        refs = [c["ref"] for c in self.SA.completed_seasons(self.session)]
        return str(refs.index(ref) + 1)

    def test_bare_lists_running_and_completed(self):
        text = self.run_cmd()
        self.assertIn("Running tournaments", text)
        self.assertIn("Completed tournaments", text)
        running, completed = text.split("Completed tournaments", 1)
        # Tournaments from other tests share this database, so check by
        # this test's own league name and the service's own lists.
        self.assertIn(f"Season 3 <i>({self.league.name})</i>", running)
        self.assertIn(f"Season 1 <i>({self.league.name}", completed)
        running_ids = [t.id for t in self.SA.running_tournaments(self.session)]
        self.assertIn(self.s3.id, running_ids)
        self.assertNotIn(self.s1.id, running_ids)

    def test_pipe_links_unlinks_and_resets(self):
        r = self.running_no(self.s3)
        c1 = self.completed_no(self.SA.tour_ref(self.s1.id))
        c2 = self.completed_no(self.SA.tour_ref(self.s2.id))
        text = self.run_cmd(r, "|", c1, c2)
        self.assertIn("Linked", text)
        self.assertEqual({self.SA.tour_ref(self.s1.id), self.SA.tour_ref(self.s2.id)},
                         set(self.SA.linked_refs(self.s3)))
        self.run_cmd(f"{r}|-{c1}")          # no spaces works too
        self.assertEqual([self.SA.tour_ref(self.s2.id)],
                         self.SA.linked_refs(self.s3))
        self.assertIn("Season 2", self.run_cmd(r))
        self.run_cmd(r, "|", "auto")
        self.assertEqual([], self.SA.linked_refs(self.s3))

    def test_a_bad_number_is_refused(self):
        r = self.running_no(self.s3)
        self.assertIn("aren't all on the completed list",
                      self.run_cmd(r, "|", "999"))
        self.assertIn("no running tournament #999", self.run_cmd("999", "|", "1"))
        self.assertEqual([], self.SA.linked_refs(self.s3))


class ParseTests(unittest.TestCase):

    def test_parse_link_command(self):
        from services.season_archive import parse_link_command as parse
        self.assertEqual(("list", None, None), parse(""))
        self.assertEqual(("show", 2, None), parse("2"))
        self.assertEqual(("link", 2, [3]), parse("2 | 3"))
        self.assertEqual(("link", 2, [3, 5]), parse("2|3,5"))
        self.assertEqual(("unlink", 2, [3]), parse("2 | -3"))
        self.assertEqual(("auto", 2, None), parse("2 | auto"))
        for bad in ("x | 3", "2 |", "2 | 3 -5", "2 | abc"):
            with self.assertRaises(ValueError):
                parse(bad)


if __name__ == "__main__":
    unittest.main()
