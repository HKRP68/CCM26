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
    """A Telegram message recording what the handler sent.

    ``photo_ok`` decides whether the card image "uploads": Telegram returns a
    Message on success and the handler treats a None return as "no photo went
    out, send the stats as text instead", so both settings are worth exercising.
    """

    def __init__(self, sent, photo_ok=False):
        self.sent = sent
        self.photo_ok = photo_ok
        self.chat_id = 99
        self.message_id = 1
        self.markups = []

    async def reply_text(self, text, **kwargs):
        self.sent.append(("text", text))
        self.markups.append(kwargs.get("reply_markup"))
        return self

    async def reply_photo(self, **kwargs):
        self.sent.append(("photo", kwargs.get("caption", "")))
        self.markups.append(kwargs.get("reply_markup"))
        return self if self.photo_ok else None


class _Query:
    """A tapped inline button, recording every edit the handler makes."""

    def __init__(self, data, from_id, *, is_photo=False):
        self.data = data
        self.from_user = SimpleNamespace(id=from_id)
        self.message = SimpleNamespace(
            photo=[object()] if is_photo else None,
            chat=SimpleNamespace(id=99, title="Cric Masters", type="private"))
        self.alerts = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        if text:
            self.alerts.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(("text", text, kwargs.get("reply_markup")))

    async def edit_message_caption(self, caption=None, **kwargs):
        self.edits.append(("caption", caption, kwargs.get("reply_markup")))

    async def edit_message_media(self, media=None, **kwargs):
        self.edits.append(("media", media.caption, kwargs.get("reply_markup")))

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(("markup", None, kwargs.get("reply_markup")))

    @property
    def last_body(self):
        return self.edits[-1][1] if self.edits else ""

    @property
    def buttons(self):
        markup = self.edits[-1][2] if self.edits else None
        if markup is None:
            return []
        return [b.text for row in markup.inline_keyboard for b in row]


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

    def _call(self, args, tg_id=1, photo_ok=False):
        self.msg = _Msg(self.sent, photo_ok=photo_ok)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=tg_id, is_bot=False),
            effective_chat=SimpleNamespace(id=99, type="private"),
            message=self.msg)
        context = SimpleNamespace(args=args, bot_data={})
        _run(gstats_mod.gstats_handler(update, context))
        return "\n".join(body for _kind, body in self.sent)

    def _tap(self, handler, data, tg_id=1, is_photo=False):
        query = _Query(data, tg_id, is_photo=is_photo)
        update = SimpleNamespace(callback_query=query)
        _run(handler(update, SimpleNamespace(args=[], bot_data={})))
        return query

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
        self.assertIn("Most runs:</b> caller", text)
        self.assertIn("Most wickets:</b> mate", text)
        self.assertIn("By edition", text)

    def test_the_leaders_are_named_without_tagging_them(self):
        """Reading a stat board must not notify everyone named on it."""
        caller = self._user(1, username="caller")
        self._stats(caller, self.base, bat_inns=4, runs=200)
        self.db.commit()
        text = self._call(["Virat Kohli"])
        self.assertIn("caller", text)
        self.assertNotIn("@caller", text)
        self.assertNotIn("tg://user", text)

    def test_a_short_card_travels_as_the_photo_caption_alone(self):
        """One message, not a photo followed by the same stats again."""
        caller = self._user(1, username="caller")
        self._stats(caller, self.base, bat_inns=10, runs=400, balls_faced=300,
                    times_out=8)
        self.db.commit()

        self._call(["Virat Kohli"], photo_ok=True)
        self.assertEqual([kind for kind, _ in self.sent], ["photo"])
        self.assertIn("Runs: 400", self.sent[0][1])

    def test_an_over_long_card_falls_back_to_a_short_caption_plus_text(self):
        """Telegram caps a caption at 1024 chars; the stats must survive it."""
        caller = self._user(1, username="caller")
        self._stats(caller, self.base, bat_inns=10, runs=400)
        self.db.commit()

        real = gstats_mod._caption_fits
        gstats_mod._caption_fits = lambda text, **kw: False
        try:
            self._call(["Virat Kohli"], photo_ok=True)
        finally:
            gstats_mod._caption_fits = real

        self.assertEqual([kind for kind, _ in self.sent], ["photo", "text"])
        self.assertNotIn("Runs: 400", self.sent[0][1])   # short caption
        self.assertIn("Runs: 400", self.sent[1][1])      # full block follows

    def test_a_failed_photo_still_delivers_the_stats(self):
        caller = self._user(1, username="caller")
        self._stats(caller, self.base, bat_inns=10, runs=400)
        self.db.commit()
        self._call(["Virat Kohli"], photo_ok=False)
        self.assertEqual(self.sent[-1][0], "text")
        self.assertIn("Runs: 400", self.sent[-1][1])

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


# ════════════════════════════════════════════════════════════════════
# 3. The edition browser: ◀ Prev / Next ▶, 👥 Owners, and who may tap
# ════════════════════════════════════════════════════════════════════

class GStatsBrowserTests(GlobalStatsTestBase):
    def setUp(self):
        super().setUp()
        self._real = gstats_mod.get_session
        gstats_mod.get_session = lambda: self.db
        self.sent = []
        self.caller = self._user(1, username="caller")
        self._stats(self.caller, self.base, bat_inns=10, runs=400,
                    balls_faced=300, times_out=8)
        self._stats(self.caller, self.gold, bowl_inns=4, wickets_taken=9,
                    balls_bowled=96, runs_conceded=100)
        self.db.commit()

    def tearDown(self):
        gstats_mod.get_session = self._real
        super().tearDown()

    def _tap(self, handler, data, tg_id=1, is_photo=False):
        query = _Query(data, tg_id, is_photo=is_photo)
        update = SimpleNamespace(callback_query=query)
        _run(handler(update, SimpleNamespace(args=[], bot_data={})))
        return query

    def _pages(self):
        from services.version_paginator import get_versions_ordered
        return gstats_mod._build_pages(get_versions_ordered(self.db, self.base.id))

    # ── pages ────────────────────────────────────────────────────────
    def test_a_two_edition_card_pages_combined_then_each_edition(self):
        labels = [p["label"] for p in self._pages()]
        self.assertEqual(labels, ["All editions", "Base", "Gold"])

    def test_a_card_with_one_edition_has_a_single_page(self):
        solo = Player(name="Solo Man", version="Base", rating=80,
                      category="Bowler", country="India", is_active=True,
                      **_HANDS)
        self.db.add(solo)
        self.db.commit()
        self.assertEqual(len(gstats_mod._build_pages([solo])), 1)

    def test_paging_to_an_edition_reports_only_that_editions_numbers(self):
        gold_page = self._tap(gstats_mod.gstats_page_callback,
                              f"gstpg_1_{self.base.id}_2")
        self.assertIn("Wkts: 9", gold_page.last_body)
        self.assertIn("Runs: 0", gold_page.last_body)   # Gold never batted
        self.assertIn("Edition 3/3", gold_page.last_body)

    def test_the_combined_page_still_sums_every_edition(self):
        combined = self._tap(gstats_mod.gstats_page_callback,
                             f"gstpg_1_{self.base.id}_0")
        self.assertIn("Runs: 400", combined.last_body)
        self.assertIn("Wkts: 9", combined.last_body)
        self.assertIn("By edition", combined.last_body)

    def test_the_per_edition_breakdown_stays_on_the_combined_page(self):
        """On one edition's own page it would just repeat the block above it."""
        page = self._tap(gstats_mod.gstats_page_callback,
                         f"gstpg_1_{self.base.id}_1")
        self.assertNotIn("By edition", page.last_body)

    def test_an_out_of_range_page_lands_on_a_real_one(self):
        page = self._tap(gstats_mod.gstats_page_callback,
                         f"gstpg_1_{self.base.id}_99")
        self.assertIn("Edition 3/3", page.last_body)

    # ── the owners flip ──────────────────────────────────────────────
    def test_the_owners_button_answers_in_the_same_message(self):
        owners = self._tap(gstats_mod.gstats_owners_callback,
                           f"gstow_1_{self.base.id}_0")
        self.assertIn("Who owns Virat Kohli", owners.last_body)
        self.assertIn("◀ Back to stats", owners.buttons)

    def test_the_owners_view_of_one_edition_says_which(self):
        owners = self._tap(gstats_mod.gstats_owners_callback,
                           f"gstow_1_{self.base.id}_2")
        self.assertIn("Gold edition only", owners.last_body)

    def test_back_from_owners_returns_to_that_same_edition(self):
        back = self._tap(gstats_mod.gstats_page_callback,
                         f"gstpg_1_{self.base.id}_2")
        self.assertIn("Edition 3/3", back.last_body)
        self.assertIn("👥 Owners", back.buttons)

    # ── ownership of the buttons ─────────────────────────────────────
    def test_only_the_manager_who_asked_may_drive_the_card(self):
        for handler, data in (
                (gstats_mod.gstats_page_callback, f"gstpg_1_{self.base.id}_1"),
                (gstats_mod.gstats_owners_callback, f"gstow_1_{self.base.id}_0"),
                (gstats_mod.gstats_close_callback, f"gstcl_1_{self.base.id}_0")):
            query = self._tap(handler, data, tg_id=2)
            self.assertEqual(query.edits, [])
            self.assertTrue(any("Not your stats" in a for a in query.alerts))

    def test_close_drops_the_buttons_and_keeps_the_card(self):
        query = self._tap(gstats_mod.gstats_close_callback,
                          f"gstcl_1_{self.base.id}_0")
        self.assertEqual(query.edits, [("markup", None, None)])

    def test_malformed_callback_data_is_refused_not_crashed(self):
        query = self._tap(gstats_mod.gstats_page_callback, "gstpg_nope")
        self.assertEqual(query.edits, [])

    # ── how the edit reaches Telegram ────────────────────────────────
    def test_a_text_card_is_edited_as_text(self):
        query = self._tap(gstats_mod.gstats_page_callback,
                          f"gstpg_1_{self.base.id}_1", is_photo=False)
        self.assertEqual(query.edits[-1][0], "text")

    def test_a_photo_card_is_edited_in_place_never_reposted(self):
        query = self._tap(gstats_mod.gstats_page_callback,
                          f"gstpg_1_{self.base.id}_1", is_photo=True)
        self.assertIn(query.edits[-1][0], ("media", "caption"))

    def test_an_owners_view_too_long_for_a_caption_is_trimmed_not_dropped(self):
        long_text = "\n".join(f"line {i} " + "x" * 60 for i in range(60))
        trimmed = gstats_mod._trim_to_caption(long_text)
        self.assertTrue(gstats_mod._caption_fits(trimmed))
        self.assertTrue(trimmed.endswith("…"))
        self.assertIn("line 0", trimmed)

    # ── the landing message ──────────────────────────────────────────
    def test_the_first_message_carries_the_buttons(self):
        msg = _Msg(self.sent, photo_ok=True)
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1, is_bot=False),
            effective_chat=SimpleNamespace(id=99, type="private"),
            message=msg)
        _run(gstats_mod.gstats_handler(
            update, SimpleNamespace(args=["Virat Kohli"], bot_data={})))
        markup = next(m for m in msg.markups if m is not None)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        self.assertIn("👥 Owners", labels)
        self.assertIn("◀ Prev", labels)
        self.assertIn("Next ▶", labels)


if __name__ == "__main__":
    unittest.main()
