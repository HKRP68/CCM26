"""/gstats — one cricketer's record summed over every owner and match type.

``player_game_stats`` holds a row per (owner, card), written by every match
mode. /gstats sums those rows, so the things worth pinning are: totals add up,
records don't (highest score and best bowling are the best of the bests), rates
are recomputed from the sums rather than averaged, and every edition of a card
counts as the same cricketer.
"""

import asyncio
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import BotCommand, Player, PlayerGameStats, User
from services import global_stats_service as gs
import handlers.gstats as gstats_mod


# Columns the schema insists on but these tests never look at.
_HANDS = {"bat_hand": "Right", "bowl_hand": "Right", "bowl_style": "Medium"}


def _run(coro):
    """Drive one handler call. A fresh loop each time keeps this file
    independent of whatever loop the rest of the suite left behind."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Msg:
    def __init__(self, sent):
        self.sent = sent

    async def reply_text(self, text, **kwargs):
        self.sent.append(text)
        return self

    async def reply_photo(self, **kwargs):
        self.sent.append(kwargs.get("caption", ""))
        return None


class GlobalStatsTestBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.close = lambda: None

        self.base = Player(name="Virat Kohli", version="Base", rating=93,
                           category="Batsman", country="India", is_active=True,
                           **_HANDS)
        self.db.add(self.base)
        self.db.flush()
        self.gold = Player(name="Virat Kohli", version="Gold", rating=95,
                           category="Batsman", country="India", is_active=True,
                           parent_player_id=self.base.id, **_HANDS)
        self.db.add(self.gold)
        self.db.flush()
        self.db.commit()

    def tearDown(self):
        self.db.rollback()

    def _user(self, tg_id, username=None):
        user = User(telegram_id=tg_id, username=username, first_name="Manager")
        self.db.add(user)
        self.db.flush()
        return user

    def _stats(self, user, player, **fields):
        row = PlayerGameStats(user_id=user.id, player_id=player.id, **fields)
        self.db.add(row)
        self.db.flush()
        return row

    def _ids(self):
        return [self.base.id, self.gold.id]


# ════════════════════════════════════════════════════════════════════
# 1. The aggregate
# ════════════════════════════════════════════════════════════════════

class AggregateTests(GlobalStatsTestBase):
    def test_totals_add_up_across_owners_and_editions(self):
        a, b = self._user(1), self._user(2)
        self._stats(a, self.base, bat_inns=10, runs=400, balls_faced=300,
                    times_out=8, fours=40, sixes=12, fifties=3, hundreds=1,
                    ducks=1, potm=2)
        self._stats(b, self.gold, bat_inns=5, runs=200, balls_faced=100,
                    times_out=2, fours=20, sixes=8, fifties=1, hundreds=0,
                    ducks=0, potm=1)
        self.db.commit()

        totals = gs.aggregate_stats(self.db, self._ids())
        self.assertEqual(totals["owners"], 2)
        self.assertEqual(totals["bat_inns"], 15)
        self.assertEqual(totals["runs"], 600)
        self.assertEqual(totals["fifties"], 4)
        self.assertEqual(totals["potm"], 3)

    def test_a_card_nobody_has_played_reports_zeroes_not_an_error(self):
        totals = gs.aggregate_stats(self.db, self._ids())
        self.assertEqual(totals["owners"], 0)
        self.assertEqual(totals["runs"], 0)
        self.assertEqual(gs.hs_str(totals), "-")
        self.assertEqual(gs.bbf_str(totals), "-")
        self.assertEqual(gs.derived(totals)["bat_avg"], 0.0)

    def test_the_highest_score_is_the_best_of_the_bests(self):
        a, b = self._user(1), self._user(2)
        self._stats(a, self.base, bat_inns=4, highest_score=84,
                    highest_score_not_out=True)
        self._stats(b, self.base, bat_inns=4, highest_score=120,
                    highest_score_not_out=False)
        self.db.commit()
        totals = gs.aggregate_stats(self.db, self._ids())
        self.assertEqual(gs.hs_str(totals), "120")

    def test_a_not_out_knock_keeps_its_star(self):
        a, b = self._user(1), self._user(2)
        self._stats(a, self.base, bat_inns=4, highest_score=99,
                    highest_score_not_out=True)
        self._stats(b, self.base, bat_inns=4, highest_score=99,
                    highest_score_not_out=False)
        self.db.commit()
        self.assertEqual(gs.hs_str(gs.aggregate_stats(self.db, self._ids())), "99*")

    def test_best_bowling_prefers_wickets_then_cheapness(self):
        a, b, c = self._user(1), self._user(2), self._user(3)
        self._stats(a, self.base, bowl_inns=3, best_bowl_wickets=4, best_bowl_runs=12)
        self._stats(b, self.base, bowl_inns=3, best_bowl_wickets=5, best_bowl_runs=40)
        self._stats(c, self.gold, bowl_inns=3, best_bowl_wickets=5, best_bowl_runs=21)
        self.db.commit()
        self.assertEqual(gs.bbf_str(gs.aggregate_stats(self.db, self._ids())), "5/21")

    def test_a_wicketless_bowler_has_no_best_figure(self):
        a = self._user(1)
        self._stats(a, self.base, bowl_inns=2, balls_bowled=24, runs_conceded=40)
        self.db.commit()
        self.assertEqual(gs.bbf_str(gs.aggregate_stats(self.db, self._ids())), "-")

    def test_rates_come_from_the_sums_not_from_averaged_averages(self):
        """A manager with 2 innings must not weigh as much as one with 200."""
        a, b = self._user(1), self._user(2)
        self._stats(a, self.base, bat_inns=2, runs=10, balls_faced=20, times_out=2)
        self._stats(b, self.base, bat_inns=200, runs=9990, balls_faced=4980,
                    times_out=98)
        self.db.commit()
        calc = gs.derived(gs.aggregate_stats(self.db, self._ids()))
        self.assertEqual(calc["bat_avg"], 100.0)   # 10000 runs / 100 outs
        self.assertEqual(calc["bat_sr"], 200.0)    # 10000 runs off 5000 balls

    def test_bowling_rates_and_overs(self):
        a = self._user(1)
        self._stats(a, self.base, bowl_inns=10, balls_bowled=245,
                    runs_conceded=300, wickets_taken=20)
        self.db.commit()
        calc = gs.derived(gs.aggregate_stats(self.db, self._ids()))
        self.assertEqual(calc["economy"], round(300 * 6 / 245, 2))
        self.assertEqual(calc["bowl_avg"], 15.0)
        self.assertEqual(calc["bowl_sr"], 12.25)
        self.assertEqual(calc["overs"], "40.5")

    def test_never_divides_by_zero(self):
        a = self._user(1)
        self._stats(a, self.base, bat_inns=1, runs=45, balls_faced=0, times_out=0)
        self.db.commit()
        calc = gs.derived(gs.aggregate_stats(self.db, self._ids()))
        self.assertEqual((calc["bat_avg"], calc["bat_sr"], calc["economy"]),
                         (0.0, 0.0, 0.0))

    def test_another_players_rows_are_not_counted(self):
        other = Player(name="Babar Azam", version="Base", rating=90,
                       category="Batsman", country="Pakistan", is_active=True,
                       **_HANDS)
        self.db.add(other)
        self.db.flush()
        a = self._user(1)
        self._stats(a, other, bat_inns=9, runs=999)
        self.db.commit()
        self.assertEqual(gs.aggregate_stats(self.db, self._ids())["runs"], 0)

    def test_top_owners_rank_by_the_metric_and_skip_zeroes(self):
        a = self._user(1, username="alpha")
        b = self._user(2, username="bravo")
        c = self._user(3, username="charlie")
        self._stats(a, self.base, bat_inns=5, runs=300, wickets_taken=0)
        self._stats(b, self.base, bat_inns=5, runs=500, wickets_taken=2)
        self._stats(c, self.base, bat_inns=0, runs=0, wickets_taken=9)
        self.db.commit()

        top_runs = gs.top_owners(self.db, self._ids(), "runs", limit=3)
        self.assertEqual([(r["user"].username, r["value"]) for r in top_runs],
                         [("bravo", 500), ("alpha", 300)])
        top_wickets = gs.top_owners(self.db, self._ids(), "wickets_taken", limit=3)
        self.assertEqual([r["user"].username for r in top_wickets],
                         ["charlie", "bravo"])

    def test_top_owners_sum_a_managers_editions_together(self):
        a = self._user(1, username="alpha")
        self._stats(a, self.base, bat_inns=5, runs=300)
        self._stats(a, self.gold, bat_inns=5, runs=250)
        self.db.commit()
        top = gs.top_owners(self.db, self._ids(), "runs", limit=3)
        self.assertEqual([(r["user"].username, r["value"]) for r in top],
                         [("alpha", 550)])

    def test_per_edition_split(self):
        a = self._user(1)
        self._stats(a, self.base, bat_inns=5, runs=300)
        self._stats(a, self.gold, bat_inns=2, runs=150)
        self.db.commit()
        split = gs.per_version(self.db, [self.base, self.gold])
        self.assertEqual([(e["player"].version, e["totals"]["runs"]) for e in split],
                         [("Base", 300), ("Gold", 150)])

    def test_an_unknown_metric_is_refused_rather_than_guessed(self):
        self.assertEqual(gs.top_owners(self.db, self._ids(), "sixes"), [])


# ════════════════════════════════════════════════════════════════════
# 2. What /gstats says
# ════════════════════════════════════════════════════════════════════

class GStatsHandlerTests(GlobalStatsTestBase):
    def setUp(self):
        super().setUp()
        self._real = gstats_mod.get_session
        gstats_mod.get_session = lambda: self.db
        self.sent = []

    def tearDown(self):
        gstats_mod.get_session = self._real
        super().tearDown()

    def _call(self, args, tg_id=1):
        msg = _Msg(self.sent)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=tg_id, is_bot=False),
            message=msg)
        context = SimpleNamespace(args=args, bot_data={})
        _run(gstats_mod.gstats_handler(update, context))
        return "\n".join(self.sent)

    def test_it_reports_the_whole_games_record(self):
        caller = self._user(1, username="caller")
        mate = self._user(2, username="mate")
        self._stats(caller, self.base, bat_inns=10, runs=400, balls_faced=300,
                    times_out=8, highest_score=110, hundreds=1)
        self._stats(mate, self.gold, bowl_inns=6, wickets_taken=12,
                    balls_bowled=144, runs_conceded=180, best_bowl_wickets=4,
                    best_bowl_runs=20)
        self.db.commit()

        text = self._call(["Virat", "Kohli"])
        self.assertIn("GLOBAL STATS — Virat Kohli", text)
        self.assertIn("all editions", text)
        self.assertIn("2 owners with a record", text)
        self.assertIn("Runs: 400", text)
        self.assertIn("Wkts: 12", text)
        self.assertIn("HS: 110", text)
        self.assertIn("BBF: 4/20", text)
        self.assertIn("Most runs:</b> @caller", text)
        self.assertIn("Most wickets:</b> @mate", text)
        self.assertIn("By edition", text)

    def test_a_card_nobody_has_played_says_so_plainly(self):
        self._user(1)
        self.db.commit()
        text = self._call(["Virat Kohli"])
        self.assertIn("hasn't played a single match", text)
        self.assertNotIn("BATTING", text)

    def test_no_argument_explains_the_command(self):
        self._user(1)
        self.db.commit()
        self.assertIn("Usage", self._call([]))

    def test_an_unknown_player_says_so(self):
        self._user(1)
        self.db.commit()
        self.assertIn("not found", self._call(["Nobody At All"]))

    def test_an_ambiguous_name_asks_for_the_full_one(self):
        self._user(1)
        self.db.add(Player(name="Virat Singh", version="Base", rating=70,
                           category="Batsman", country="India", is_active=True,
                           **_HANDS))
        self.db.commit()
        text = self._call(["Virat"])
        self.assertIn("Multiple players found", text)
        self.assertIn("/gstats Virat Kohli", text)

    def test_an_admin_can_switch_the_command_off(self):
        """It is in the website's command catalog, so the toggle must bite."""
        self._user(1)
        self.db.add(BotCommand(command_key="gstats", display_name="/gstats",
                               enabled=False))
        self.db.commit()
        self.assertIn("temporarily disabled", self._call(["Virat Kohli"]))

    def test_you_must_have_debuted(self):
        self.assertIn("/debut", self._call(["Virat Kohli"], tg_id=404))


if __name__ == "__main__":
    unittest.main()
