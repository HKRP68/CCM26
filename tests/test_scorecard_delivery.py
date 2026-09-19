"""Durable scorecard delivery — ``services.scorecard_delivery``.

Three failures used to eat a scorecard silently, and each one has a test here:

  • a render that raised took the *other* card down with it
  • a single dropped ``send_photo`` lost the card for good
  • nothing survived the live match state, so nothing could be replayed

Follows the throwaway-sqlite pattern from ``tests/test_tournament_team_lock.py``
for the persistence half; the send/render half runs against fakes so no PIL
render or Telegram call is needed.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap  # noqa: E402  (sibling helper; see its docstring)

from telegram.error import Forbidden, NetworkError, RetryAfter, TimedOut  # noqa: E402

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config", "services.scorecard_delivery")

sd = None  # services.scorecard_delivery, bound in setUpModule


def setUpModule():
    global _PREV_DATABASE_URL, _SAVED_MODULES, _TMP, _ENGINE, sd

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

    from services import scorecard_delivery
    sd = scorecard_delivery


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


def _run(coro):
    return asyncio.run(coro)


def _bat_card(innings=1, match_id=1):
    return {
        "match_id": match_id,
        "card_type": "batting",
        "innings": innings,
        "caption": "batting",
        "payload": {
            "team_name": "Alpha", "opponent_name": "Bravo",
            "total_runs": 156, "total_wickets": 4, "overs_str": "20.0",
            "batsmen_rows": [
                {"name": "Opener", "runs": 78, "balls": 51,
                 "dismissal": "c slip b spinner", "status": "out"},
                {"name": "Reserve", "runs": 0, "balls": 0,
                 "dismissal": "did not bat", "status": "dnb"},
            ],
            "fall_of_wickets": [[1, 12, "1.3"]],
            "extras_dict": {"wd": 4, "nb": 1, "b": 0, "lb": 2, "total": 7},
            "is_first_innings": True,
        },
    }


def _bowl_card(innings=1, match_id=1):
    return {
        "match_id": match_id,
        "card_type": "bowling",
        "innings": innings,
        "caption": "bowling",
        "payload": {
            "team_name": "Bravo",
            "bowlers_rows": [
                {"name": "Quick", "overs": "4.0", "maidens": 1,
                 "runs_conceded": 25, "wickets": 4},
            ],
            "fall_of_wickets": [[1, 12, "1.3"]],
            "is_first_innings": True,
        },
    }


class SortOrderTests(unittest.TestCase):
    """Delivery order is batting, bowling, then the whole-match summary.

    The summary carries ``innings == 0``, which sorts *before* both innings on a
    naive numeric key — so it would lead the re-send instead of closing it.
    """

    def test_batting_precedes_bowling_within_an_innings(self):
        cards = [_bowl_card(1), _bat_card(1)]
        ordered = sorted(cards, key=sd._sort_key)
        self.assertEqual([c["card_type"] for c in ordered],
                         ["batting", "bowling"])

    def test_first_innings_precedes_second(self):
        cards = [_bat_card(2), _bat_card(1)]
        ordered = sorted(cards, key=sd._sort_key)
        self.assertEqual([c["innings"] for c in ordered], [1, 2])

    def test_whole_match_summary_sorts_last(self):
        cards = [
            {"card_type": "summary", "innings": sd.WHOLE_MATCH, "payload": {}},
            _bat_card(2), _bat_card(1),
        ]
        ordered = sorted(cards, key=sd._sort_key)
        self.assertEqual([c["card_type"] for c in ordered],
                         ["batting", "batting", "summary"])


class RetryAfterSecondsTests(unittest.TestCase):
    def test_reads_a_plain_number(self):
        self.assertEqual(sd._retry_after_seconds(RetryAfter(7)), 7.0)

    def test_reads_a_timedelta(self):
        """PTB >= 22.2 can hand back a timedelta; both forms must work."""
        from datetime import timedelta
        exc = MagicMock()
        exc.retry_after = timedelta(seconds=12)
        self.assertEqual(sd._retry_after_seconds(exc), 12.0)

    def test_falls_back_when_the_value_is_nonsense(self):
        exc = MagicMock()
        exc.retry_after = "soon"
        self.assertEqual(sd._retry_after_seconds(exc), 1.0)


class SendWithRetryTests(unittest.TestCase):
    """A single dropped send used to be terminal. It is not any more."""

    def test_a_transient_failure_is_retried_and_succeeds(self):
        bot = MagicMock()
        sent = MagicMock()
        bot.send_photo = AsyncMock(side_effect=[TimedOut(), sent])
        result = _run(sd.send_photo_with_retry(
            bot, -100, lambda: b"png", caption="c"))
        self.assertIs(result, sent)
        self.assertEqual(bot.send_photo.await_count, 2)

    def test_every_attempt_gets_a_fresh_photo(self):
        """A BytesIO consumed by a failed upload re-sends zero bytes.

        That is why the photo is passed as a factory: if the same exhausted
        buffer were reused, every retry after the first would fail forever.
        """
        made = []

        def factory():
            buf = MagicMock()
            made.append(buf)
            return buf

        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=[NetworkError("x"), MagicMock()])
        _run(sd.send_photo_with_retry(bot, -100, factory))
        self.assertEqual(len(made), 2)
        self.assertIsNot(made[0], made[1])

    def test_it_gives_up_after_the_attempt_budget(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=TimedOut())
        result = _run(sd.send_photo_with_retry(
            bot, -100, lambda: b"png", attempts=3))
        self.assertIsNone(result)
        self.assertEqual(bot.send_photo.await_count, 3)

    def test_a_forbidden_chat_is_not_retried(self):
        """Kicked, blocked or deleted — no number of retries fixes it, and
        hammering a dead chat only delays the caller."""
        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=Forbidden("bot was kicked"))
        result = _run(sd.send_photo_with_retry(bot, -100, lambda: b"png"))
        self.assertIsNone(result)
        self.assertEqual(bot.send_photo.await_count, 1)

    def test_flood_control_waits_and_retries(self):
        bot = MagicMock()
        sent = MagicMock()
        bot.send_photo = AsyncMock(side_effect=[RetryAfter(0), sent])
        with unittest.mock.patch.object(sd.asyncio, "sleep",
                                        new=AsyncMock()) as slept:
            result = _run(sd.send_photo_with_retry(bot, -100, lambda: b"png"))
        self.assertIs(result, sent)
        slept.assert_awaited()


class RenderCardTests(unittest.TestCase):
    def test_an_unknown_card_type_is_refused_not_guessed(self):
        self.assertIsNone(sd.render_card("mystery", {}))

    def test_a_render_that_raises_returns_none_rather_than_propagating(self):
        """One card must never be able to take another down with it."""
        import services.scorecard_card as card_module
        with unittest.mock.patch.object(
                card_module, "generate_batting_scorecard",
                side_effect=RuntimeError("font missing")):
            self.assertIsNone(sd.render_card("batting", _bat_card()["payload"]))

    def test_style_comes_from_live_config_not_the_stored_payload(self):
        """A card redrawn later follows the theme admins have *now*.

        Storing the accent would freeze last season's colours into every
        replay, so the payload deliberately has no style keys and render_card
        supplies them.
        """
        import services.scorecard_card as card_module
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return b"png"

        with unittest.mock.patch.object(
                card_module, "generate_batting_scorecard", _capture), \
             unittest.mock.patch.object(
                sd, "_live_style", return_value={"accent_hex": "#123456",
                                                 "text_settings": {"a": 1}}):
            sd.render_card("batting", _bat_card()["payload"])
        self.assertEqual(seen["accent_hex"], "#123456")
        self.assertEqual(seen["text_settings"], {"a": 1})

    def test_the_summary_match_date_survives_the_json_round_trip(self):
        """``match_date`` is stored as an ISO string; the generator wants a
        datetime, so the string has to be parsed back on the way in."""
        from datetime import datetime
        import services.match_summary_card as summary_module
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return b"png"

        with unittest.mock.patch.object(
                summary_module, "generate_match_summary", _capture), \
             unittest.mock.patch.object(sd, "_summary_style", return_value={}):
            sd.render_card("summary", {"match_date": "2026-01-02T03:04:05"})
        self.assertEqual(seen["match_date"], datetime(2026, 1, 2, 3, 4, 5))

    def test_an_unparseable_match_date_is_dropped_not_passed_through(self):
        """A bad stored value must not become a TypeError inside the renderer,
        which would cost the card that the fallback is meant to save."""
        import services.match_summary_card as summary_module
        seen = {}

        def _capture(**kwargs):
            seen.update(kwargs)
            return b"png"

        with unittest.mock.patch.object(
                summary_module, "generate_match_summary", _capture), \
             unittest.mock.patch.object(sd, "_summary_style", return_value={}):
            sd.render_card("summary", {"match_date": "not a date"})
        self.assertNotIn("match_date", seen)


class AcceptedKwargsTests(unittest.TestCase):
    """Archived rows outlive the code that drew them."""

    def test_a_key_the_renderer_no_longer_takes_is_dropped(self):
        def renderer(team_name, runs=0):
            return None

        self.assertEqual(
            sd._accepted_kwargs(renderer,
                                {"team_name": "A", "runs": 1, "gone": 2}),
            {"team_name": "A", "runs": 1})

    def test_a_renderer_taking_kwargs_keeps_everything(self):
        def renderer(**kwargs):
            return None

        payload = {"anything": 1, "at": "all"}
        self.assertEqual(sd._accepted_kwargs(renderer, payload), payload)

    def test_a_stale_key_costs_a_detail_not_the_card(self):
        """A TypeError here would retire every previously archived row at
        once, which is the failure this guards."""
        import services.scorecard_card as card_module
        calls = {}

        def renderer(team_name=None, accent_hex=None, text_settings=None):
            calls.update(team_name=team_name)
            return b"png"

        with unittest.mock.patch.object(
                card_module, "generate_batting_scorecard", renderer), \
             unittest.mock.patch.object(
                sd, "_live_style", return_value={}):
            out = sd.render_card("batting",
                                 {"team_name": "Alpha", "retired_key": 1})
        self.assertEqual(out, b"png")
        self.assertEqual(calls["team_name"], "Alpha")


class PersistenceTests(unittest.TestCase):
    """The values outlive the live match state, which is the whole point."""

    def setUp(self):
        from database import get_session
        from models import MatchScorecardImage
        session = get_session()
        try:
            session.query(MatchScorecardImage).delete()
            session.commit()
        finally:
            session.close()

    def test_cards_round_trip_in_delivery_order(self):
        sd.record_cards(77, -1001, [_bowl_card(2, 77), _bat_card(1, 77),
                                    _bowl_card(1, 77), _bat_card(2, 77)])
        loaded = sd.load_cards(77)
        self.assertEqual(
            [(c["innings"], c["card_type"]) for c in loaded],
            [(1, "batting"), (1, "bowling"), (2, "batting"), (2, "bowling")])
        self.assertEqual(loaded[0]["payload"]["total_runs"], 156)
        self.assertEqual(loaded[0]["chat_id"], -1001)

    def test_recording_the_same_card_twice_updates_rather_than_duplicates(self):
        sd.record_cards(78, -1001, [_bat_card(1, 78)])
        second = _bat_card(1, 78)
        second["payload"]["total_runs"] = 200
        sd.record_cards(78, -1001, [second])
        loaded = sd.load_cards(78)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["payload"]["total_runs"], 200)

    def test_re_recording_drops_the_cached_file_id(self):
        """The image no longer matches the values, so re-sending the old
        file_id would put a stale card in the chat."""
        sd.record_cards(79, -1001, [_bat_card(1, 79)])
        sd.mark_delivered(79, 1, "batting", file_id="abc123")
        self.assertEqual(sd.load_cards(79)[0]["file_id"], "abc123")
        sd.record_cards(79, -1001, [_bat_card(1, 79)])
        replayed = sd.load_cards(79)[0]
        self.assertIsNone(replayed["file_id"])
        self.assertFalse(replayed["delivered"])

    def test_an_unknown_card_type_is_refused_at_the_door(self):
        sd.record_cards(80, -1001, [{"card_type": "bogus", "payload": {}}])
        self.assertEqual(sd.load_cards(80), [])

    def test_the_latest_match_for_a_chat_is_the_highest_match_id(self):
        sd.record_cards(81, -2002, [_bat_card(1, 81)])
        sd.record_cards(95, -2002, [_bat_card(1, 95)])
        sd.record_cards(99, -3003, [_bat_card(1, 99)])
        self.assertEqual(sd.latest_match_id_for_chat(-2002), 95)
        self.assertEqual(sd.latest_match_id_for_chat(-3003), 99)

    def test_a_chat_with_no_matches_reports_none(self):
        self.assertIsNone(sd.latest_match_id_for_chat(-4004))

    def test_a_payload_that_is_not_valid_json_yields_an_empty_dict(self):
        """A corrupted row must not take the whole replay down — the other
        cards of that match are still deliverable."""
        from database import get_session
        from models import MatchScorecardImage
        sd.record_cards(82, -1001, [_bat_card(1, 82)])
        session = get_session()
        try:
            row = (session.query(MatchScorecardImage)
                   .filter(MatchScorecardImage.match_id == 82).first())
            row.payload_json = "{not json"
            session.commit()
        finally:
            session.close()
        self.assertEqual(sd.load_cards(82)[0]["payload"], {})


class DeliverCardTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import MatchScorecardImage
        session = get_session()
        try:
            session.query(MatchScorecardImage).delete()
            session.commit()
        finally:
            session.close()

    def test_a_cached_file_id_is_sent_without_rerendering(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
        card = dict(_bat_card(1, 90), file_id="cached-id", chat_id=-100)
        with unittest.mock.patch.object(
                sd, "render_card_async", new=AsyncMock()) as render:
            ok = _run(sd.deliver_card(bot, -100, card))
        self.assertTrue(ok)
        render.assert_not_awaited()
        self.assertEqual(bot.send_photo.await_args.kwargs["photo"], "cached-id")

    def test_a_stale_file_id_is_forgotten_and_the_card_is_redrawn(self):
        """Telegram rotates and expires file ids. Without clearing one that is
        rejected, the card would fail the same way forever."""
        from telegram.error import BadRequest
        sd.record_cards(91, -100, [_bat_card(1, 91)])
        sd.mark_delivered(91, 1, "batting", file_id="stale-id")

        photo_msg = MagicMock()
        photo_msg.photo = [MagicMock(file_id="fresh-id")]
        bot = MagicMock()
        bot.send_photo = AsyncMock(
            side_effect=[BadRequest("wrong file identifier"), photo_msg])

        card = sd.load_cards(91)[0]
        with unittest.mock.patch.object(
                sd, "render_card_async", new=AsyncMock(return_value=b"png")):
            ok = _run(sd.deliver_card(bot, -100, card))

        self.assertTrue(ok)
        self.assertEqual(sd.load_cards(91)[0]["file_id"], "fresh-id")

    def test_a_card_that_cannot_be_rendered_reports_failure(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        card = dict(_bat_card(1, 92), chat_id=-100)
        with unittest.mock.patch.object(
                sd, "render_card_async", new=AsyncMock(return_value=None)):
            ok = _run(sd.deliver_card(bot, -100, card))
        self.assertFalse(ok)
        bot.send_photo.assert_not_awaited()

    def test_renders_run_concurrently_and_sends_stay_in_order(self):
        """The two cards used to be drawn under one gather inside one try. The
        parallel render is worth keeping; the shared failure mode is not."""
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
        cards = [dict(_bat_card(1, 95), chat_id=-100, caption="bat"),
                 dict(_bowl_card(1, 95), chat_id=-100, caption="bowl")]
        renders = AsyncMock(return_value=b"png")
        with unittest.mock.patch.object(sd, "render_card_async", new=renders):
            sent = _run(sd.deliver_cards(bot, -100, cards))
        self.assertEqual(sent, 2)
        self.assertEqual([c.kwargs["caption"]
                          for c in bot.send_photo.await_args_list],
                         ["bat", "bowl"])

    def test_a_card_drawn_empty_is_not_drawn_a_second_time(self):
        """A re-render would fail identically and cost another PIL pass."""
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
        cards = [dict(_bat_card(1, 100), chat_id=-100)]
        renders = AsyncMock(return_value=None)
        with unittest.mock.patch.object(sd, "render_card_async", new=renders):
            sent = _run(sd.deliver_cards(bot, -100, cards))
        self.assertEqual(sent, 0)
        self.assertEqual(renders.await_count, 1)

    def test_a_cached_card_is_not_rendered_even_alongside_one_that_is(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
        cards = [dict(_bat_card(1, 101), chat_id=-100, file_id="cached"),
                 dict(_bowl_card(1, 101), chat_id=-100, file_id=None)]
        renders = AsyncMock(return_value=b"png")
        with unittest.mock.patch.object(sd, "render_card_async", new=renders):
            sent = _run(sd.deliver_cards(bot, -100, cards))
        self.assertEqual(sent, 2)
        self.assertEqual(renders.await_count, 1)

    def test_a_render_that_raises_does_not_poison_the_batch(self):
        """``render_card`` swallows its own errors, but the gather must survive
        one escaping anyway — otherwise every card in the batch is lost to a
        single bad one, which is the bug this module was written for."""
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
        cards = [dict(_bat_card(1, 102), chat_id=-100),
                 dict(_bowl_card(1, 102), chat_id=-100)]
        renders = AsyncMock(side_effect=[RuntimeError("boom"), b"png"])
        with unittest.mock.patch.object(sd, "render_card_async", new=renders):
            sent = _run(sd.deliver_cards(bot, -100, cards))
        self.assertEqual(sent, 1)

    def test_one_broken_card_does_not_stop_the_others(self):
        """The original bug: both renders shared a try block, so a failure in
        the batting card discarded the bowling card too."""
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
        cards = [dict(_bat_card(1, 93), chat_id=-100),
                 dict(_bowl_card(1, 93), chat_id=-100)]
        renders = AsyncMock(side_effect=[None, b"png"])
        with unittest.mock.patch.object(sd, "render_card_async", new=renders):
            sent = _run(sd.deliver_cards(bot, -100, cards))
        self.assertEqual(sent, 1)
        self.assertEqual(bot.send_photo.await_count, 1)


class PotmCardTests(unittest.TestCase):
    """The Player of the Match's collectible card, sent as a second photo.

    It goes out *after* the summary card has already landed, so every failure
    mode here — no identity, a player with no card, a render that raises — has
    to read as "no second photo" and never as an error on a finished match.
    """

    def _bot(self, send=None):
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=send or MagicMock())
        return bot

    def test_it_sends_the_rendered_card(self):
        bot = self._bot()
        with unittest.mock.patch.object(sd, "potm_card_bytes",
                                        return_value=b"card-bytes") as render:
            ok = _run(sd.send_potm_card(bot, -100, player_id=7, name="Ace",
                                        team="Alpha"))
        self.assertTrue(ok)
        render.assert_called_once_with(7, "Ace")
        self.assertEqual(bot.send_photo.await_count, 1)
        caption = bot.send_photo.await_args.kwargs["caption"]
        self.assertIn("Ace", caption)
        self.assertIn("Alpha", caption)

    def test_no_identity_sends_nothing_and_does_not_render(self):
        bot = self._bot()
        with unittest.mock.patch.object(sd, "potm_card_bytes") as render:
            ok = _run(sd.send_potm_card(bot, -100))
        self.assertFalse(ok)
        render.assert_not_called()
        bot.send_photo.assert_not_awaited()

    def test_a_player_with_no_card_sends_nothing(self):
        bot = self._bot()
        with unittest.mock.patch.object(sd, "potm_card_bytes",
                                        return_value=None):
            ok = _run(sd.send_potm_card(bot, -100, name="Ghost"))
        self.assertFalse(ok)
        bot.send_photo.assert_not_awaited()

    def test_a_render_that_raises_does_not_propagate(self):
        bot = self._bot()
        with unittest.mock.patch.object(sd, "potm_card_bytes",
                                        side_effect=RuntimeError("boom")):
            ok = _run(sd.send_potm_card(bot, -100, player_id=7, name="Ace"))
        self.assertFalse(ok)
        bot.send_photo.assert_not_awaited()

    def test_a_send_that_never_lands_is_reported_not_raised(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=Forbidden("kicked"))
        with unittest.mock.patch.object(sd, "potm_card_bytes",
                                        return_value=b"card-bytes"):
            ok = _run(sd.send_potm_card(bot, -100, player_id=7, name="Ace"))
        self.assertFalse(ok)

    def test_the_caption_escapes_a_name_that_looks_like_markup(self):
        caption = sd.potm_card_caption("<b>Ace</b>", "Alpha & Co")
        self.assertIn("&lt;b&gt;Ace&lt;/b&gt;", caption)
        self.assertIn("Alpha &amp; Co", caption)

    def test_the_caption_survives_a_nameless_award(self):
        self.assertIn("Player of the Match", sd.potm_card_caption(None, None))

    def test_the_switch_turns_the_render_off(self):
        """One config key, so an admin can stop the extra photo without a
        deploy — and it is read here rather than at each of the four call
        sites, so no mode can miss it."""
        with unittest.mock.patch.object(sd, "_potm_card_enabled",
                                        return_value=False):
            self.assertIsNone(sd.potm_card_bytes(player_id=7, name="Ace"))

    def test_it_defaults_to_on_when_the_config_cannot_be_read(self):
        import services.config_service as config_service
        with unittest.mock.patch.object(config_service, "get_config",
                                        side_effect=RuntimeError("db down")):
            self.assertTrue(sd._potm_card_enabled())

    def test_a_row_that_predates_the_migration_still_gets_the_card(self):
        """The column is added with a default, but a NULL would read as False
        and ship the feature off to precisely the installs that already had
        matches to show it on."""
        import services.config_service as config_service
        with unittest.mock.patch.object(
                config_service, "get_config",
                return_value={"scorecard_potm_card": None}):
            self.assertTrue(sd._potm_card_enabled())

    def test_an_explicit_off_is_honoured(self):
        import services.config_service as config_service
        with unittest.mock.patch.object(
                config_service, "get_config",
                return_value={"scorecard_potm_card": False}):
            self.assertFalse(sd._potm_card_enabled())

    def test_the_switch_ships_on(self):
        from services.config_service import DEFAULTS
        self.assertIs(DEFAULTS["scorecard_potm_card"], True)

    def test_nothing_to_resolve_skips_the_session_entirely(self):
        """No id and no name cannot resolve to anybody, so it must not open a
        connection to find that out."""
        import services.card_identity as card_identity
        with unittest.mock.patch.object(card_identity, "open_session") as opened:
            self.assertIsNone(sd.potm_card_bytes())
        opened.assert_not_called()

    def test_the_session_is_closed_even_when_the_lookup_raises(self):
        import services.card_identity as card_identity
        session = MagicMock()
        with unittest.mock.patch.object(card_identity, "open_session",
                                        return_value=session), \
             unittest.mock.patch.object(card_identity, "potm_card_png",
                                        side_effect=RuntimeError("boom")):
            self.assertIsNone(sd.potm_card_bytes(player_id=7))
        session.close.assert_called_once()


class TextFallbackTests(unittest.TestCase):
    """When every image fails the group still gets the numbers."""

    def test_it_reports_the_score_and_the_batters_who_batted(self):
        text = sd.text_scorecard([_bat_card(1), _bowl_card(1)])
        self.assertIn("Alpha", text)
        self.assertIn("156/4", text)
        self.assertIn("Opener 78(51)", text)
        self.assertIn("Quick 4.0-1-25-4", text)

    def test_a_player_who_did_not_bat_is_left_out(self):
        text = sd.text_scorecard([_bat_card(1)])
        self.assertNotIn("Reserve", text)

    def test_extras_are_shown_only_when_there_are_any(self):
        self.assertIn("Extras 7", sd.text_scorecard([_bat_card(1)]))
        no_extras = _bat_card(1)
        no_extras["payload"]["extras_dict"] = {"total": 0}
        self.assertNotIn("Extras", sd.text_scorecard([no_extras]))

    def test_the_summary_card_contributes_the_result(self):
        card = {"card_type": "summary", "innings": 0, "payload": {
            "inn1_team": "Alpha", "inn1_runs": 156, "inn1_wickets": 4,
            "inn1_overs": "20.0", "inn2_team": "Bravo", "inn2_runs": 157,
            "inn2_wickets": 6, "inn2_overs": "19.2",
            "winner_name": "Bravo", "win_margin_text": "by 4 wickets",
            "potm_name": "Quick", "potm_stats": "4/25"}}
        text = sd.text_scorecard([card])
        self.assertIn("Bravo won by 4 wickets", text)
        self.assertIn("POTM Quick — 4/25", text)

    def test_the_result_closes_the_text_the_way_it_closes_the_images(self):
        text = sd.text_scorecard([
            {"card_type": "summary", "innings": 0,
             "payload": {"winner_name": "Bravo"}},
            _bat_card(1),
        ])
        self.assertLess(text.index("Alpha"), text.index("Result"))

    def test_nothing_to_show_returns_none(self):
        self.assertIsNone(sd.text_scorecard([]))
        self.assertIsNone(sd.text_scorecard(
            [{"card_type": "summary", "innings": 0, "payload": {}}]))


class RecordAndSendTests(unittest.TestCase):
    def setUp(self):
        from database import get_session
        from models import MatchScorecardImage
        session = get_session()
        try:
            session.query(MatchScorecardImage).delete()
            session.commit()
        finally:
            session.close()

    def test_the_payloads_are_persisted_before_anything_can_fail(self):
        """This is what makes a lost card recoverable: even when every render
        and every send dies, /lastscorecard still has something to redraw."""
        bot = MagicMock()
        bot.send_photo = AsyncMock(side_effect=TimedOut())
        bot.send_message = AsyncMock()
        with unittest.mock.patch.object(
                sd, "render_card_async", new=AsyncMock(return_value=None)):
            sent = _run(sd.record_and_send(
                bot, -5005, 94, [_bat_card(1, 94), _bowl_card(1, 94)]))
        self.assertEqual(sent, 0)
        stored = sd.load_cards(94)
        self.assertEqual(len(stored), 2)
        self.assertEqual(stored[0]["chat_id"], -5005)

    def test_total_image_failure_falls_back_to_text(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        with unittest.mock.patch.object(
                sd, "render_card_async", new=AsyncMock(return_value=None)):
            _run(sd.record_and_send(bot, -5005, 96, [_bat_card(1, 96)]))
        bot.send_message.assert_awaited_once()
        self.assertIn("156/4", bot.send_message.await_args.kwargs["text"])

    def test_a_successful_send_does_not_also_post_the_text_fallback(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
        bot.send_message = AsyncMock()
        with unittest.mock.patch.object(
                sd, "render_card_async", new=AsyncMock(return_value=b"png")):
            sent = _run(sd.record_and_send(bot, -5005, 97, [_bat_card(1, 97)]))
        self.assertEqual(sent, 1)
        bot.send_message.assert_not_awaited()

    def test_no_cards_is_a_no_op(self):
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        bot.send_message = AsyncMock()
        self.assertEqual(_run(sd.record_and_send(bot, -5005, 98, [])), 0)
        bot.send_photo.assert_not_awaited()
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
