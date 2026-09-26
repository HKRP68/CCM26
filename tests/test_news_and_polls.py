"""CMU News and Mini App polls.

Run against a real (temporary SQLite) database, because the guarantees that
matter here — one vote per user, one payout per approval, one story per
dedupe key — are enforced by constraints and conditional writes, which a fake
session would only pretend to honour.
"""

import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.news_service",
                 "services.poll_service", "services.news_banner",
                 "services.activity_service", "services.display_name",
                 "services.tournament_service", "services.auction_service",
                 "services.retention_negotiation",
                 "services.hall_of_fame")


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE
    _PREV_DATABASE_URL = os.environ.get("DATABASE_URL")
    _SAVED_MODULES = _module_swap.save(_MODULE_NAMES)
    _module_swap.unload(_MODULE_NAMES)
    _TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    _TMP.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
    from database import Base, engine
    import models  # noqa: F401
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


def _jpeg(size=(400, 300)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(buf, "JPEG")
    return buf.getvalue()


BODY = "A long enough article body that easily passes the minimum length."


class _DbCase(unittest.TestCase):
    _tg = 1000

    def setUp(self):
        from database import get_session
        from models import (NewsArticle, NewsRead, NewsReaction, Poll, PollVote,
                            GameConfig, StoredAsset)
        self.db = get_session()
        for model in (NewsRead, NewsReaction, PollVote, NewsArticle, Poll,
                      GameConfig, StoredAsset):
            self.db.query(model).delete()
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def user(self, coins=0):
        from models import User
        _DbCase._tg += 1
        u = User(telegram_id=_DbCase._tg, first_name=f"P{_DbCase._tg}",
                 total_coins=coins)
        self.db.add(u)
        self.db.commit()
        return u


class SubmissionTests(_DbCase):
    def test_submit_is_pending_and_approve_pays_once(self):
        from services import news_service as ns
        author = self.user(coins=10)
        article = ns.submit_user_article(self.db, author, headline="Big news today!",
                                         body=BODY, image_raw=_jpeg())
        self.db.commit()
        self.assertEqual(article.status, ns.STATUS_PENDING)
        self.assertEqual(article.reward_coins, 100)
        self.assertTrue(article.image_key)
        data, ctype = ns.image_bytes(self.db, article)
        self.assertEqual(ctype, "image/jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))

        self.assertEqual(ns.approve(self.db, article, reviewer="@admin"), 100)
        self.db.commit()
        self.assertIsNone(ns.approve(self.db, article, reviewer="@other"))
        self.db.refresh(author)
        self.assertEqual(author.total_coins, 110)
        self.assertEqual(article.status, ns.STATUS_PUBLISHED)

    def test_reject_stores_note_and_cannot_then_approve(self):
        from services import news_service as ns
        author = self.user()
        article = ns.submit_user_article(self.db, author, headline="Some headline",
                                         body=BODY)
        self.assertTrue(ns.reject(self.db, article, reviewer="a",
                                  note=ns.reason_text("spam")))
        self.assertEqual(article.review_note, ns.REJECT_REASONS["spam"])
        self.assertIsNone(ns.approve(self.db, article))
        self.db.refresh(author)
        self.assertEqual(author.total_coins or 0, 0)

    def test_validation_and_daily_cap(self):
        from services import news_service as ns
        author = self.user()
        with self.assertRaises(ValueError):
            ns.submit_user_article(self.db, author, headline="short", body=BODY)
        with self.assertRaises(ValueError):
            ns.submit_user_article(self.db, author, headline="Long enough headline",
                                   body="too short")
        with self.assertRaises(ValueError):
            ns.submit_user_article(self.db, author, headline="Long enough headline",
                                   body=BODY, image_raw=b"not an image")
        for i in range(ns.DAILY_SUBMISSION_CAP):
            ns.submit_user_article(self.db, author, headline=f"Headline number {i}",
                                   body=BODY)
        with self.assertRaises(ValueError):
            ns.submit_user_article(self.db, author, headline="One too many today",
                                   body=BODY)


class FeedTests(_DbCase):
    def test_feed_order_unread_and_views(self):
        from services import news_service as ns
        reader, other = self.user(), self.user()
        made = []
        for i in range(7):
            made.append(ns.create_admin_article(self.db, headline=f"Headline story {i}",
                                                body=BODY, pinned=(i == 0)))
        self.db.commit()
        feed = ns.list_feed(self.db, reader)
        self.assertEqual(len(feed["articles"]), 5)
        self.assertEqual(feed["articles"][0]["id"], made[0].id)  # pinned first
        self.assertEqual(feed["unread_count"], 7)

        data = ns.get_article(self.db, reader, made[3].id)
        ns.get_article(self.db, reader, made[3].id)   # re-read: no second view
        ns.get_article(self.db, other, made[3].id)
        self.db.commit()
        self.assertEqual(data["unread_count"], 6)
        self.db.refresh(made[3])
        self.assertEqual(made[3].view_count, 2)

    def test_unpublished_is_invisible(self):
        from services import news_service as ns
        reader = self.user()
        article = ns.submit_user_article(self.db, self.user(), headline="Pending story",
                                         body=BODY)
        self.db.commit()
        self.assertIsNone(ns.get_article(self.db, reader, article.id))
        self.assertEqual(ns.list_feed(self.db, reader)["articles"], [])

    def test_reactions_set_change_clear(self):
        from services import news_service as ns
        reader = self.user()
        article = ns.create_admin_article(self.db, headline="React to this one",
                                          body=BODY)
        self.assertEqual(ns.react(self.db, reader, article.id, "🔥")["reactions"]["🔥"], 1)
        out = ns.react(self.db, reader, article.id, "👍")
        self.assertEqual((out["reactions"]["🔥"], out["reactions"]["👍"]), (0, 1))
        out = ns.react(self.db, reader, article.id, "👍")
        self.assertIsNone(out["my_reaction"])
        self.assertEqual(out["reactions"]["👍"], 0)
        with self.assertRaises(ValueError):
            ns.react(self.db, reader, article.id, "💩")

    def test_delete_removes_children(self):
        from models import NewsRead
        from services import news_service as ns
        reader = self.user()
        article = ns.create_admin_article(self.db, headline="To be deleted soon",
                                          body=BODY, image=_jpeg())
        ns.get_article(self.db, reader, article.id)
        ns.react(self.db, reader, article.id, "😂")
        ns.delete(self.db, article)
        self.db.commit()
        self.assertEqual(self.db.query(NewsRead).count(), 0)


class AutoStoryTests(_DbCase):
    def test_dedupe_and_publish(self):
        from models import NewsArticle
        from services import news_service as ns
        a = ns.auto_story(self.db, "tournament_champion", "tourney:1",
                          "🏆 Team wins the cup", "Body text")
        b = ns.auto_story(self.db, "tournament_champion", "tourney:1",
                          "🏆 Team wins the cup", "Body text")
        self.db.commit()
        self.assertIsNotNone(a)
        self.assertIsNone(b)
        self.assertEqual(self.db.query(NewsArticle).count(), 1)
        self.assertEqual(a.status, ns.STATUS_PUBLISHED)
        self.assertTrue(a.image_key)   # banner rendered

    def test_auto_publish_off_and_disabled_kind(self):
        from services import news_service as ns
        ns.save_settings(self.db, submit_reward_coins=50, auto_publish=False,
                         auto_announce=False, auto_kinds=["season_winners"])
        self.db.commit()
        pending = ns.auto_story(self.db, "season_winners", "season:x", "Winners here",
                                "Body", render_banner=False)
        skipped = ns.auto_story(self.db, "auction_record", "auc:1", "Record here",
                                "Body", render_banner=False)
        self.assertEqual(pending.status, ns.STATUS_PENDING)
        self.assertIsNone(skipped)

    def test_failure_leaves_caller_transaction_intact(self):
        from models import User
        from services import news_service as ns
        u = self.user(coins=5)
        u.total_coins = 999
        with mock.patch.object(ns, "attach_image", side_effect=RuntimeError("boom")):
            out = ns.auto_story(self.db, "hall_of_fame", "hof:1", "Headline", "Body",
                                image_png=b"x")
        self.assertIsNone(out)
        self.db.commit()
        self.assertEqual(self.db.get(User, u.id).total_coins, 999)

    def test_banner_renders_png(self):
        from services.news_banner import render_banner
        png = render_banner("auction_record", "💰 Record buy! A very long franchise "
                            "name signs a player for an enormous sum of money")
        self.assertTrue(png.startswith(b"\x89PNG"))


class TournamentHookTests(_DbCase):
    def test_champion_news_from_final(self):
        from models import NewsArticle, Tournament, TournamentTeam, TournamentMatch
        from services import tournament_service
        tour = Tournament(name="CMU Cup", kind="letsplay")
        self.db.add(tour)
        self.db.flush()
        a = TournamentTeam(tournament_id=tour.id, name="Mavericks")
        b = TournamentTeam(tournament_id=tour.id, name="Titans")
        self.db.add_all([a, b])
        self.db.flush()
        final = TournamentMatch(tournament_id=tour.id, team1_id=a.id, team2_id=b.id,
                                winner_team_id=a.id, stage="final", status="completed",
                                result_text="Mavericks won by 12 runs")
        self.db.add(final)
        self.db.flush()
        tournament_service._champion_news(self.db, tour, final)
        self.db.commit()
        story = self.db.query(NewsArticle).one()
        self.assertIn("Mavericks win CMU Cup", story.headline)
        self.assertIn("Titans", story.body)


class AuctionHookTests(_DbCase):
    def test_record_story_only_when_previous_best_beaten(self):
        from models import AuctionSeason, AuctionFranchise, AuctionLot, NewsArticle
        from services import auction_service as au
        season = AuctionSeason(name="CMU Auction")
        self.db.add(season)
        self.db.flush()
        franchise = AuctionFranchise(season_id=season.id, name="Kings")
        self.db.add(franchise)
        self.db.flush()

        def sold(n, price):
            lot = AuctionLot(season_id=season.id, name=f"Player {n}", lot_no=n,
                             status=au.LOT_SOLD, sold_to_id=franchise.id,
                             sold_price_lakh=price)
            self.db.add(lot)
            self.db.flush()
            return lot

        early = sold(1, 500)
        au._record_buy_news(self.db, season, early, franchise, 500)
        for n, price in ((2, 200), (3, 300)):
            sold(n, price)
        lower = sold(4, 400)
        au._record_buy_news(self.db, season, lower, franchise, 400)
        self.assertEqual(self.db.query(NewsArticle).count(), 0)
        record = sold(5, 900)
        au._record_buy_news(self.db, season, record, franchise, 900)
        self.db.commit()
        story = self.db.query(NewsArticle).one()
        self.assertEqual(story.kind, "auction_record")
        self.assertIn("Player 5", story.headline)


class ReviewFixTests(_DbCase):
    """Regression tests for the PR #465 review findings."""

    def test_decompression_bomb_is_refused_before_decoding(self):
        from PIL import Image
        from services import news_service as ns
        buf = io.BytesIO()
        Image.new("L", (6000, 6000), 0).save(buf, "PNG")
        self.assertLess(len(buf.getvalue()), ns.MAX_IMAGE_BYTES)
        with mock.patch.object(Image.Image, "load", side_effect=AssertionError("decoded")):
            with self.assertRaises(ValueError) as caught:
                ns.normalise_image(buf.getvalue())
        self.assertIn("too large", str(caught.exception))

    def _pending(self, author):
        from services import news_service as ns
        article = ns.submit_user_article(self.db, author, headline="Race condition story",
                                         body=BODY)
        self.db.commit()
        return article.id

    def test_concurrent_approvals_pay_once(self):
        from database import get_session
        from models import NewsArticle, User
        from services import news_service as ns
        author = self.user(coins=0)
        aid = self._pending(author)
        bot, web = get_session(), get_session()
        try:
            a_bot, a_web = bot.get(NewsArticle, aid), web.get(NewsArticle, aid)
            self.assertEqual((a_bot.status, a_web.status), ("pending", "pending"))
            self.assertEqual(ns.approve(bot, a_bot, reviewer="@bot"), 100)
            bot.commit()
            self.assertIsNone(ns.approve(web, a_web, reviewer="web"))
            self.assertFalse(ns.reject(web, a_web, reviewer="web", note="late"))
            web.commit()
        finally:
            bot.close(); web.close()
        self.db.expire_all()
        self.assertEqual(self.db.get(User, author.id).total_coins, 100)
        self.assertEqual(self.db.get(NewsArticle, aid).status, "published")

    def test_reject_then_approve_loses(self):
        from database import get_session
        from models import NewsArticle, User
        from services import news_service as ns
        author = self.user(coins=0)
        aid = self._pending(author)
        one, two = get_session(), get_session()
        try:
            a1, a2 = one.get(NewsArticle, aid), two.get(NewsArticle, aid)
            self.assertTrue(ns.reject(one, a1, reviewer="x", note="spam"))
            one.commit()
            self.assertIsNone(ns.approve(two, a2, reviewer="y"))
            two.commit()
        finally:
            one.close(); two.close()
        self.db.expire_all()
        self.assertEqual(self.db.get(User, author.id).total_coins or 0, 0)

    def test_read_race_does_not_error_or_double_count(self):
        from database import get_session
        from models import NewsRead
        from services import news_service as ns
        reader = self.user()
        article = ns.create_admin_article(self.db, headline="Opened twice at once",
                                          body=BODY)
        self.db.commit()
        # The session has already looked (no read row)… then a parallel request
        # writes one before this session inserts.
        other = get_session()
        other.add(NewsRead(article_id=article.id, user_id=reader.id))
        other.commit(); other.close()
        # Hide that row from this session's "already read?" check, as if it had
        # looked a moment before the other request committed.
        with mock.patch("sqlalchemy.orm.Query.first", side_effect=[None]):
            data = ns.get_article(self.db, reader, article.id)
        self.db.commit()
        self.assertIsNotNone(data)
        self.db.refresh(article)
        self.assertEqual(article.view_count or 0, 0)

    def test_hall_of_fame_posts_only_the_best_new_entry(self):
        from types import SimpleNamespace
        from models import HallOfFameEntry, NewsArticle
        from services import hall_of_fame as hof
        self.db.add(HallOfFameEntry(category="bat_score", value=100, tiebreak=0,
                                    label="100 (60)", player_name="Old Guard"))
        self.db.commit()
        rows = [dict(category="bat_score", value=110, tiebreak=0, label="110 (61)",
                     player_name="First Beater"),
                dict(category="bat_score", value=130, tiebreak=0, label="130 (70)",
                     player_name="Real Record")]
        match = SimpleNamespace(id=None, completed_at=datetime.utcnow())
        sc_row = SimpleNamespace(scorecard_json="{}", created_at=datetime.utcnow())
        with mock.patch.object(hof, "_eligible", return_value=True), \
                mock.patch.object(hof, "_owners", return_value=(None, None)), \
                mock.patch.object(hof, "entries_from_scorecard", return_value=rows):
            hof.harvest_match(self.db, match, sc_row)
        self.db.commit()
        stories = self.db.query(NewsArticle).all()
        self.assertEqual(len(stories), 1)
        self.assertIn("Real Record", stories[0].headline)
        self.assertIn("Old Guard", stories[0].body)

    def test_deleting_a_final_retracts_the_champion_story(self):
        from models import NewsArticle, Tournament, TournamentTeam, TournamentMatch
        from services import tournament_service
        tour = Tournament(name="Replay Cup", kind="letsplay")
        self.db.add(tour); self.db.flush()
        a = TournamentTeam(tournament_id=tour.id, name="Alphas")
        b = TournamentTeam(tournament_id=tour.id, name="Betas")
        self.db.add_all([a, b]); self.db.flush()
        final = TournamentMatch(tournament_id=tour.id, team1_id=a.id, team2_id=b.id,
                                winner_team_id=a.id, stage="final", status="completed",
                                match_no=1)
        self.db.add(final); self.db.flush()
        tournament_service._champion_news(self.db, tour, final)
        self.db.commit()
        tournament_service.delete_tournament_match(self.db, final.id)
        self.db.commit()
        self.assertEqual(self.db.query(NewsArticle).count(), 0)
        final.status, final.winner_team_id = "completed", b.id
        self.db.flush()
        tournament_service._champion_news(self.db, tour, final)
        self.db.commit()
        self.assertIn("Betas", self.db.query(NewsArticle).one().headline)

    def test_announce_waits_for_commit_and_skips_rollback(self):
        from services import news_service as ns
        ns.save_settings(self.db, submit_reward_coins=100, auto_publish=True,
                         auto_announce=True, auto_kinds=list(ns.AUTO_KINDS))
        self.db.commit()
        from models import NewsRead
        reader = self.user()
        with mock.patch("services.news_announce.in_background") as dispatch:
            story = ns.auto_story(self.db, "season_winners", "season:a", "Winners A",
                                  "Body", render_banner=False)
            dispatch.assert_not_called()
            # A savepoint rolling back later in the same transaction (here a
            # losing duplicate insert) must not drop the queued announcement.
            self.assertTrue(ns._insert_once(self.db, NewsRead(article_id=story.id,
                                                              user_id=reader.id)))
            self.assertFalse(ns._insert_once(self.db, NewsRead(article_id=story.id,
                                                               user_id=reader.id)))
            dispatch.assert_not_called()   # savepoint release is not the commit
            self.db.commit()
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(dispatch.call_args.args[1], story.id)

            ns.auto_story(self.db, "season_winners", "season:b", "Winners B",
                          "Body", render_banner=False)
            self.db.rollback()
            self.user()          # an unrelated later commit
            self.assertEqual(dispatch.call_count, 1)


class PollTests(_DbCase):
    def _poll(self, reward=25, **kw):
        from services import poll_service as ps
        return ps.create_poll(self.db, question="Who wins the final?",
                              option_labels=["Mavericks", "Titans", "Draw"],
                              ends_at=datetime.utcnow() + timedelta(hours=2),
                              reward_coins=reward, **kw)

    def test_vote_once_and_reward_once(self):
        from services import poll_service as ps
        voter = self.user(coins=0)
        poll = self._poll()
        self.db.commit()
        before = ps.active_poll_for(self.db, voter)
        self.assertIsNone(before["results"])            # hidden until voting
        data, paid = ps.vote(self.db, voter, poll.id, 1)
        self.db.commit()
        self.assertEqual(paid, 25)
        self.assertEqual(data["my_vote"], 1)
        self.assertEqual(data["results"]["options"][1]["pct"], 100)
        with self.assertRaises(ValueError):
            ps.vote(self.db, voter, poll.id, 0)
        self.db.refresh(voter)
        self.assertEqual(voter.total_coins, 25)

    def test_bad_input_and_closed(self):
        from services import poll_service as ps
        voter = self.user()
        with self.assertRaises(ValueError):
            ps.create_poll(self.db, question="Only one option?", option_labels=["A"],
                           ends_at=datetime.utcnow() + timedelta(hours=1))
        poll = self._poll(reward=0)
        with self.assertRaises(ValueError):
            ps.vote(self.db, voter, poll.id, 7)
        ps.end_now(self.db, poll)
        self.db.commit()
        self.assertIsNone(ps.active_poll_for(self.db, voter))
        with self.assertRaises(ValueError):
            ps.vote(self.db, voter, poll.id, 0)

    def test_scheduled_poll_not_active_yet(self):
        from services import poll_service as ps
        start = datetime.utcnow() + timedelta(hours=1)
        ps.create_poll(self.db, question="Future poll here", option_labels=["A", "B"],
                       starts_at=start, ends_at=start + timedelta(hours=1))
        self.db.commit()
        self.assertIsNone(ps.active_poll_for(self.db, self.user()))


if __name__ == "__main__":
    unittest.main()
