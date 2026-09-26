"""Custom overs (/letsplay 5, /cipl 6), the approach card's conditions line,
and buying with a full roster.
"""

import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from services.match_formats import extract_overs_arg


class ExtractOversTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(extract_overs_arg(["5", "@a"]), (5, ["@a"], None))
        self.assertEqual(extract_overs_arg(["@a", "T10"]), (10, ["@a"], None))
        self.assertEqual(extract_overs_arg(["7ov"]), (7, [], None))
        self.assertEqual(extract_overs_arg(["12overs", "@b"]), (12, ["@b"], None))
        self.assertEqual(extract_overs_arg([]), (None, [], None))
        self.assertEqual(extract_overs_arg(None), (None, [], None))

    def test_out_of_range(self):
        overs, _rest, err = extract_overs_arg(["25"])
        self.assertIsNone(overs)
        self.assertIn("between 1 and 20", err)
        self.assertIsNotNone(extract_overs_arg(["0"])[2])

    def test_a_telegram_id_is_left_for_the_target_lookup(self):
        self.assertEqual(extract_overs_arg(["123456789"]), (None, ["123456789"], None))


class LetsPlayOversTests(unittest.TestCase):
    def test_draft_overs(self):
        from handlers.letsplay import LETSPLAY_OVERS, draft_overs
        self.assertEqual(draft_overs({"overs": 5}), 5)
        self.assertEqual(draft_overs({}), LETSPLAY_OVERS)
        self.assertEqual(draft_overs(None), LETSPLAY_OVERS)

    def test_invite_card_shows_the_length(self):
        from services import letsplay_rich as lpr
        blocks = lpr.invite_blocks({"overs": 6, "host": {}, "guest": {}})
        self.assertIn("6 Overs", repr(blocks))


class ConditionsLineTests(unittest.TestCase):
    def test_live_conditions(self):
        from handlers.cipl_play import _conditions_text
        text = _conditions_text({
            "pitch_type": "Green",
            "conditions": {"weather": "Overcast", "temperature": 21,
                           "wind_strength": "Strong", "wind_direction": "Crosswind",
                           "dew": None, "dew_forecast": "Heavy"}})
        for bit in ("Green pitch", "Overcast", "21°C", "Strong crosswind",
                    "Heavy dew expected"):
            self.assertIn(bit, text)
        self.assertIn("Light dew", _conditions_text(
            {"pitch_type": "Hard", "conditions": {"dew": "Light"}}))
        self.assertEqual(_conditions_text({}), "")


class _NoOwnedSession:
    """``session.query(UserRoster)...first()`` → nothing owned."""

    def query(self, *_a):
        return self

    def filter(self, *_a):
        return self

    def first(self):
        return None


def _player(pid=7, rating=85):
    return NS(id=pid, rating=rating, parent_player_id=None, name="Ace",
              restricted_from_buypl=False)


class RosterFullKeyboardTests(unittest.TestCase):
    def _data(self, roster_count):
        from config import MAX_ROSTER
        from services.version_paginator import build_pagination_keyboard
        user = NS(id=1, roster_count=MAX_ROSTER if roster_count is None else roster_count)
        kb = build_pagination_keyboard(session=_NoOwnedSession(), user=user,
                                       versions=[_player()], current_index=0,
                                       owner_tg=99, flow="buy")
        return [b.callback_data for row in kb.inline_keyboard for b in row]

    def test_room_shows_buy(self):
        data = self._data(3)
        self.assertTrue(any(d.startswith("buypl_7_1_") for d in data))

    def test_full_shows_the_lock_not_buy(self):
        data = self._data(None)
        self.assertIn("buyfull_7_buy_99", data)
        self.assertFalse(any(d.startswith("buypl_") for d in data))
        self.assertIn("buycancel_99", data)

    def test_caption_note(self):
        from config import MAX_ROSTER
        from services.version_paginator import roster_full_note, roster_is_full
        full = NS(roster_count=MAX_ROSTER)
        self.assertTrue(roster_is_full(full))
        self.assertFalse(roster_is_full(NS(roster_count=0)))
        self.assertIn("Roster full", roster_full_note(full))


class _Q:
    def __init__(self, data, tg_id):
        self.data = data
        self.from_user = NS(id=tg_id, username="u", first_name="U")
        self.message = NS(chat_id=-5, message_id=77, reply_text=mock.AsyncMock())
        self.answer = mock.AsyncMock()
        self.edit_message_reply_markup = mock.AsyncMock()


class _GetSession:
    def __init__(self, user):
        self.user = user

    def get(self, _model, _id):
        return self.user

    def query(self, *_a):
        return self

    def filter(self, *_a):
        return self

    def first(self):
        return self.user

    def close(self):
        pass

    def rollback(self):
        pass


class BuyWithFullRosterTests(unittest.TestCase):
    def test_buy_press_with_a_full_roster_keeps_the_card(self):
        from config import MAX_ROSTER
        import handlers.buy as buy
        user = NS(id=1, telegram_id=99, roster_count=MAX_ROSTER, total_coins=10**7)
        q = _Q("buypl_7_1_99", 99)
        update = NS(callback_query=q)
        with mock.patch.object(buy, "get_session", return_value=_GetSession(user)), \
                mock.patch.object(buy, "claim_once", return_value=True), \
                mock.patch.object(buy, "release") as rel:
            asyncio.run(buy.buypl_confirm_callback(update, NS()))
        q.answer.assert_awaited_once()
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))
        self.assertIn("roster is full", q.answer.call_args.args[0])
        q.edit_message_reply_markup.assert_not_awaited()   # card keeps its buttons
        q.message.reply_text.assert_not_awaited()          # no chat spam
        rel.assert_called_once()

    def test_lock_button_while_still_full(self):
        from config import MAX_ROSTER
        import handlers.buy as buy
        user = NS(id=1, telegram_id=99, roster_count=MAX_ROSTER)
        q = _Q("buyfull_7_buy_99", 99)
        with mock.patch.object(buy, "get_session", return_value=_GetSession(user)):
            asyncio.run(buy.buy_roster_full_callback(NS(callback_query=q), NS()))
        self.assertTrue(q.answer.call_args.kwargs.get("show_alert"))
        self.assertIn("/releasepl", q.answer.call_args.args[0])

    def test_lock_button_is_owner_only(self):
        import handlers.buy as buy
        q = _Q("buyfull_7_buy_99", 12345)
        asyncio.run(buy.buy_roster_full_callback(NS(callback_query=q), NS()))
        self.assertIn("Not your card", q.answer.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
