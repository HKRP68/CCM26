"""/lastscorecard — the group's way to get a missing scorecard back.

The command exists because a card that never arrived used to be unrecoverable:
the values it is drawn from live in ``match_state``, which is cleaned up
shortly after the match ends. These pin the behaviour that matters in a busy
group — that it answers in the chat it was run in, that it refuses to read
another group's match, and that a second tap can't double the upload.

Everything below stubs ``services.scorecard_delivery`` on the handler module,
so no PIL render, no database and no Telegram call is involved.
"""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import handlers.match as hm


def _run(coro):
    return asyncio.run(coro)


def _update(chat_id=-1001, chat_type="supergroup", tg_id=777):
    message = MagicMock()
    message.reply_text = AsyncMock(return_value=MagicMock())
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=SimpleNamespace(id=tg_id),
        message=message,
    )


def _context(args=None):
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock())
    bot.send_photo = AsyncMock(return_value=MagicMock(photo=[]))
    return SimpleNamespace(args=list(args or []), bot=bot)


def _match(**overrides):
    """A Match row with every column ``_describe_scorecard_match`` reads."""
    row = dict(user1_id=1, user2_id=2, winner_id=None, margin_type=None,
               margin_value=None, inn1_runs=None, inn1_wickets=None,
               inn2_runs=None, inn2_wickets=None)
    row.update(overrides)
    return SimpleNamespace(**row)


def _card(match_id=5, chat_id=-1001, file_id="cached"):
    return {"match_id": match_id, "chat_id": chat_id, "innings": 1,
            "card_type": "batting", "caption": "bat", "payload": {},
            "file_id": file_id, "delivered": True}


class _Session:
    """Just enough SQLAlchemy surface for the handler's lookups."""

    def __init__(self, match=None, user=None):
        self._match = match
        self._user = user

    def get(self, model, pk):
        return self._match if getattr(model, "__name__", "") == "Match" else None

    def query(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._user

    def close(self):
        pass


class _Patched:
    """Stub scorecard_delivery + get_session on handlers.match for one test."""

    def __init__(self, cards=None, latest_chat=None, latest_user=None,
                 session=None, sent=None):
        self.cards = cards if cards is not None else []
        self.latest_chat = latest_chat
        self.latest_user = latest_user
        self.session = session or _Session()
        self.delivered = AsyncMock(
            return_value=len(self.cards) if sent is None else sent)
        self.text_fallback = AsyncMock(return_value=True)

    def __enter__(self):
        delivery = MagicMock()
        delivery.load_cards.return_value = self.cards
        delivery.latest_match_id_for_chat.return_value = self.latest_chat
        delivery.latest_match_id_for_user.return_value = self.latest_user
        delivery.deliver_cards = self.delivered
        delivery.send_text_fallback = self.text_fallback
        delivery.ensure_summary_card.side_effect = lambda mid, cards, **k: cards
        self.delivery = delivery
        self._patches = [
            mock.patch.object(hm, "scorecard_delivery", delivery),
            mock.patch.object(hm, "get_session", lambda: self.session),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False


class ArgumentTests(unittest.TestCase):
    def test_a_non_numeric_argument_gets_the_usage_line(self):
        update, context = _update(), _context(["yesterday"])
        with _Patched() as p:
            _run(hm.lastscorecard_handler(update, context))
        update.message.reply_text.assert_awaited_once()
        self.assertIn("Usage", update.message.reply_text.await_args.args[0])
        p.delivered.assert_not_awaited()

    def test_a_leading_hash_is_accepted(self):
        """People copy the match number off the result card, where it reads
        '#1234'."""
        update, context = _update(), _context(["#1234"])
        with _Patched(cards=[_card(match_id=1234)]) as p:
            _run(hm.lastscorecard_handler(update, context))
        p.delivery.load_cards.assert_called_once()
        self.assertEqual(p.delivery.load_cards.call_args.args[0], 1234)


class GroupLookupTests(unittest.TestCase):
    def test_a_group_with_no_archived_match_is_told_so(self):
        update, context = _update(), _context()
        with _Patched(latest_chat=None) as p:
            _run(hm.lastscorecard_handler(update, context))
        p.delivered.assert_not_awaited()
        self.assertIn("No scorecard yet",
                      update.message.reply_text.await_args.args[0])

    def test_the_groups_last_match_is_replayed_into_that_group(self):
        update, context = _update(chat_id=-4242), _context()
        cards = [_card(match_id=9, chat_id=-4242)]
        with _Patched(cards=cards, latest_chat=9) as p:
            _run(hm.lastscorecard_handler(update, context))
        p.delivery.latest_match_id_for_chat.assert_called_once()
        self.assertEqual(p.delivery.latest_match_id_for_chat.call_args.args[0],
                         -4242)
        p.delivered.assert_awaited_once()
        self.assertEqual(p.delivered.await_args.args[1], -4242)

    def test_a_private_chat_falls_back_to_the_callers_own_last_match(self):
        """There is no group history in a DM, so the caller's own match is the
        only sensible answer."""
        update = _update(chat_id=777, chat_type="private")
        context = _context()
        session = _Session(user=SimpleNamespace(id=42))
        with _Patched(cards=[_card()], latest_user=11, session=session) as p:
            _run(hm.lastscorecard_handler(update, context))
        p.delivery.latest_match_id_for_user.assert_called_once()
        self.assertEqual(p.delivery.latest_match_id_for_user.call_args.args[0], 42)

    def test_a_dm_from_someone_with_no_account_is_told_to_play_first(self):
        update = _update(chat_id=777, chat_type="private")
        context = _context()
        with _Patched(session=_Session(user=None)) as p:
            _run(hm.lastscorecard_handler(update, context))
        p.delivered.assert_not_awaited()
        self.assertIn("No scorecard yet",
                      update.message.reply_text.await_args.args[0])


class CrossChatAccessTests(unittest.TestCase):
    """A match belongs to the chat it was played in."""

    def test_another_groups_match_is_refused(self):
        update, context = _update(chat_id=-1001), _context(["5"])
        session = _Session(match=_match(user1_id=1, user2_id=2),
                           user=SimpleNamespace(id=99))
        with _Patched(cards=[_card(chat_id=-9999)], session=session) as p, \
             mock.patch("services.admin_ids.is_admin", return_value=False):
            _run(hm.lastscorecard_handler(update, context))
        p.delivered.assert_not_awaited()
        self.assertIn("wasn't played here",
                      update.message.reply_text.await_args.args[0])

    def test_someone_who_played_the_match_may_pull_it_up_anywhere(self):
        update, context = _update(chat_id=-1001), _context(["5"])
        session = _Session(match=_match(user1_id=99, user2_id=2),
                           user=SimpleNamespace(id=99))
        with _Patched(cards=[_card(chat_id=-9999)], session=session) as p, \
             mock.patch("services.admin_ids.is_admin", return_value=False):
            _run(hm.lastscorecard_handler(update, context))
        p.delivered.assert_awaited_once()

    def test_an_unknown_match_id_says_there_is_nothing_stored(self):
        update, context = _update(), _context(["123456"])
        with _Patched(cards=[]) as p:
            _run(hm.lastscorecard_handler(update, context))
        p.delivered.assert_not_awaited()
        self.assertIn("No stored scorecard",
                      update.message.reply_text.await_args.args[0])


class ConcurrencyTests(unittest.TestCase):
    def test_a_second_request_while_one_is_in_flight_is_turned_away(self):
        """Five images is a lot of upload; two overlapping replays double the
        flood-control pressure that loses cards in the first place."""
        update, context = _update(chat_id=-7007), _context()
        hm._SCORECARD_REPLAYS_IN_FLIGHT.add(-7007)
        try:
            with _Patched(cards=[_card()], latest_chat=5) as p:
                _run(hm.lastscorecard_handler(update, context))
            p.delivered.assert_not_awaited()
            self.assertIn("Already sending",
                          update.message.reply_text.await_args.args[0])
        finally:
            hm._SCORECARD_REPLAYS_IN_FLIGHT.discard(-7007)

    def test_the_in_flight_marker_is_cleared_even_when_delivery_raises(self):
        """A marker left behind would wedge the command for that chat until the
        next restart."""
        update, context = _update(chat_id=-7008), _context()
        with _Patched(cards=[_card()], latest_chat=5) as p:
            p.delivered.side_effect = RuntimeError("telegram is down")
            _run(hm.lastscorecard_handler(update, context))
        self.assertNotIn(-7008, hm._SCORECARD_REPLAYS_IN_FLIGHT)


class NoticeTests(unittest.TestCase):
    """The rebuild notice names the case this command exists for."""

    def test_a_card_the_chat_never_received_is_called_out(self):
        update, context = _update(), _context()
        cards = [dict(_card(), file_id=None, delivered=False)]
        with _Patched(cards=cards, latest_chat=5):
            _run(hm.lastscorecard_handler(update, context))
        texts = [c.kwargs.get("text", "")
                 for c in context.bot.send_message.await_args_list]
        self.assertTrue(any("never received" in t for t in texts), texts)

    def test_a_delivered_card_that_only_needs_redrawing_says_so_plainly(self):
        update, context = _update(), _context()
        cards = [dict(_card(), file_id=None, delivered=True)]
        with _Patched(cards=cards, latest_chat=5):
            _run(hm.lastscorecard_handler(update, context))
        texts = [c.kwargs.get("text", "")
                 for c in context.bot.send_message.await_args_list]
        self.assertTrue(any("Rebuilding the scorecard" in t for t in texts), texts)
        self.assertFalse(any("never received" in t for t in texts), texts)

    def test_fully_cached_cards_need_no_notice_at_all(self):
        update, context = _update(), _context()
        with _Patched(cards=[_card()], latest_chat=5):
            _run(hm.lastscorecard_handler(update, context))
        texts = [c.kwargs.get("text", "")
                 for c in context.bot.send_message.await_args_list]
        self.assertFalse(any("Rebuilding" in t for t in texts), texts)


class FallbackTests(unittest.TestCase):
    def test_when_no_image_lands_the_numbers_are_posted_as_text(self):
        update, context = _update(), _context()
        with _Patched(cards=[_card()], latest_chat=5, sent=0) as p:
            _run(hm.lastscorecard_handler(update, context))
        p.text_fallback.assert_awaited_once()

    def test_a_partial_replay_says_how_many_cards_were_lost(self):
        update, context = _update(), _context()
        cards = [_card(), dict(_card(), card_type="bowling")]
        with _Patched(cards=cards, latest_chat=5, sent=1):
            _run(hm.lastscorecard_handler(update, context))
        texts = [c.kwargs.get("text", "")
                 for c in context.bot.send_message.await_args_list]
        self.assertTrue(any("1 of 2 cards" in t for t in texts), texts)

    def test_a_full_replay_adds_no_warning(self):
        update, context = _update(), _context()
        cards = [_card(), dict(_card(), card_type="bowling")]
        with _Patched(cards=cards, latest_chat=5, sent=2) as p:
            _run(hm.lastscorecard_handler(update, context))
        p.text_fallback.assert_not_awaited()
        texts = [c.kwargs.get("text", "")
                 for c in context.bot.send_message.await_args_list]
        self.assertFalse(any("couldn't be rebuilt" in t for t in texts), texts)


class InningsSenderRecoveryTests(unittest.TestCase):
    """The innings sender must never leave the chat empty when it can replay."""

    def test_a_missing_live_state_replays_the_stored_cards(self):
        ctx = SimpleNamespace(bot=MagicMock(), bot_data={})
        with mock.patch.object(hm, "_gs", return_value=None), \
             mock.patch.object(hm, "_replay_stored_scorecards",
                               new=AsyncMock(return_value=2)) as replay:
            sent = _run(hm._send_innings_scorecards(ctx, 31, innings_num=1))
        self.assertEqual(sent, 2)
        replay.assert_awaited_once()

    def test_a_build_failure_also_falls_back_to_the_stored_cards(self):
        """The old code logged and returned here, and the card was gone."""
        ctx = SimpleNamespace(bot=MagicMock(), bot_data={})
        # ``inn1_bat_xi`` of None makes the row build raise, which is the
        # shape of every real build failure: a state that is present but not
        # the shape the card reader expects.
        with mock.patch.object(hm, "_gs",
                               return_value={"chat_id": -1,
                                             "inn1_bat_xi": None}), \
             mock.patch.object(hm, "_replay_stored_scorecards",
                               new=AsyncMock(return_value=1)) as replay:
            sent = _run(hm._send_innings_scorecards(ctx, 32, innings_num=1))
        self.assertEqual(sent, 1)
        replay.assert_awaited_once()

    def test_replay_targets_the_chat_the_cards_were_recorded_for(self):
        ctx = SimpleNamespace(bot=MagicMock(), bot_data={})
        cards = [_card(match_id=33, chat_id=-808)]
        with _Patched(cards=cards) as p:
            sent = _run(hm._replay_stored_scorecards(ctx, 33, innings_num=1))
        self.assertEqual(sent, len(cards))
        self.assertEqual(p.delivered.await_args.args[1], -808)

    def test_replay_with_nothing_stored_is_a_no_op(self):
        ctx = SimpleNamespace(bot=MagicMock(), bot_data={})
        with _Patched(cards=[]) as p:
            self.assertEqual(_run(hm._replay_stored_scorecards(ctx, 34)), 0)
        p.delivered.assert_not_awaited()

    def test_replay_filters_to_the_innings_asked_for(self):
        ctx = SimpleNamespace(bot=MagicMock(), bot_data={})
        cards = [_card(match_id=35, chat_id=-808),
                 dict(_card(match_id=35, chat_id=-808), innings=2)]
        with _Patched(cards=cards) as p:
            _run(hm._replay_stored_scorecards(ctx, 35, innings_num=2))
        replayed = p.delivered.await_args.args[2]
        self.assertEqual([c["innings"] for c in replayed], [2])


if __name__ == "__main__":
    unittest.main()
