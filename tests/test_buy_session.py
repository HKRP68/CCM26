"""One buy at a time — the guard that makes /buypl finish what it started.

A buy card is a live offer against a purse: a price, a Buy button, and two
minutes of life. Five of them open at once is five prices against one balance,
and the user tapping one of them has no way to know which. So /buypl refuses to
open a second card while the first is still waiting, and these tests pin the
three things that decision rests on:

  • **the slot is per user, and answering the card frees it.** Buying, closing
    or timing out all mean "not in a buy any more" — a slot that only a purchase
    could free would strand anyone who changed their mind.
  • **it never outlives the card.** The record expires with the buttons, so an
    abandoned card locks nobody out for longer than it stays tappable.
  • **it follows the card.** Paging between a text version and a picture deletes
    and re-sends the message, and a record still pointing at the deleted one
    would name a message the user cannot find.

The /playerinfo carousel shares the Buy and Close buttons, which is why closing
is keyed on the exact message: cancelling a card the user was only *reading*
must not quietly free a buy they still have open somewhere else.
"""

import unittest

from services import buy_session


class BuySlotTests(unittest.TestCase):
    def setUp(self):
        buy_session.reset()
        self.addCleanup(buy_session.reset)

    def open(self, tg_id=501, chat_id=-100, message_id=7, **kw):
        buy_session.open_card(tg_id, chat_id=chat_id, message_id=message_id, **kw)

    def test_a_user_with_nothing_open_is_free_to_buy(self):
        self.assertIsNone(buy_session.active_card(501))

    def test_an_open_card_holds_the_slot_and_says_what_it_is(self):
        self.open(player_name="Virat Kohli", chat_title="The GC")
        card = buy_session.active_card(501)
        self.assertIsNotNone(card)
        self.assertEqual(card["player_name"], "Virat Kohli")
        self.assertEqual(card["chat_title"], "The GC")
        self.assertEqual((card["chat_id"], card["message_id"]), (-100, 7))

    def test_the_slot_is_per_user(self):
        """One manager's open card must never block another's command."""
        self.open(tg_id=501)
        self.assertIsNone(buy_session.active_card(502))

    def test_closing_the_card_frees_the_slot(self):
        self.open()
        self.assertIsNotNone(buy_session.close_card(501))
        self.assertIsNone(buy_session.active_card(501))

    def test_closing_nothing_is_not_an_error(self):
        self.assertIsNone(buy_session.close_card(501))

    def test_closing_another_message_leaves_the_open_card_alone(self):
        """The Close button is shared with the /playerinfo carousel."""
        self.open(chat_id=-100, message_id=7)
        self.assertIsNone(
            buy_session.close_card(501, chat_id=-100, message_id=99))
        self.assertIsNotNone(buy_session.active_card(501))
        self.assertIsNotNone(
            buy_session.close_card(501, chat_id=-100, message_id=7))
        self.assertIsNone(buy_session.active_card(501))

    def test_the_same_message_id_in_another_chat_is_a_different_card(self):
        self.open(chat_id=-100, message_id=7)
        self.assertIsNone(
            buy_session.close_card(501, chat_id=-200, message_id=7))
        self.assertIsNotNone(buy_session.active_card(501))

    def test_opening_a_second_card_replaces_the_record(self):
        """Belt and braces: the handler refuses first, but if a card is ever
        opened anyway the slot must track the newest one, not a ghost."""
        self.open(message_id=7, player_name="Rohit Sharma")
        self.open(message_id=8, player_name="Jasprit Bumrah")
        card = buy_session.active_card(501)
        self.assertEqual(card["message_id"], 8)
        self.assertEqual(card["player_name"], "Jasprit Bumrah")


class ExpiryTests(unittest.TestCase):
    """The slot frees itself the moment the buttons stop working."""

    def setUp(self):
        buy_session.reset()
        self.addCleanup(buy_session.reset)
        self._real_clock = buy_session.time.monotonic
        self.now = 1000.0
        buy_session.time.monotonic = lambda: self.now
        self.addCleanup(self._restore)

    def _restore(self):
        buy_session.time.monotonic = self._real_clock

    def test_an_abandoned_card_stops_holding_the_slot(self):
        buy_session.open_card(501, chat_id=-100, message_id=7)
        self.now += buy_session.BUY_CARD_TTL - 1
        self.assertIsNotNone(buy_session.active_card(501))
        self.now += 2
        self.assertIsNone(buy_session.active_card(501))

    def test_the_ttl_matches_the_cards_own_button_timeout(self):
        """Both are 120s on purpose: the slot frees exactly when the Buy button
        stops working, so there is never a window where one is live and the
        other is not."""
        self.assertIn("delay_seconds=120", _read(_handler_path()))
        self.assertEqual(buy_session.BUY_CARD_TTL, 120.0)


class MoveTests(unittest.TestCase):
    """Paging a card can delete and re-send it; the record has to follow."""

    def setUp(self):
        buy_session.reset()
        self.addCleanup(buy_session.reset)

    def test_a_replaced_card_keeps_the_slot_under_its_new_message(self):
        buy_session.open_card(501, chat_id=-100, message_id=7)
        buy_session.move_card(501, from_chat_id=-100, from_message_id=7,
                              to_chat_id=-100, to_message_id=11)
        card = buy_session.active_card(501)
        self.assertEqual(card["message_id"], 11)
        # And the card is now closable at the message the user can actually see.
        self.assertIsNotNone(
            buy_session.close_card(501, chat_id=-100, message_id=11))

    def test_paging_an_untracked_card_never_steals_the_slot(self):
        """/playerinfo renders the same carousel without opening a buy."""
        buy_session.open_card(501, chat_id=-100, message_id=7)
        buy_session.move_card(501, from_chat_id=-100, from_message_id=404,
                              to_chat_id=-100, to_message_id=405)
        self.assertEqual(buy_session.active_card(501)["message_id"], 7)

    def test_moving_a_card_nobody_has_open_is_a_no_op(self):
        buy_session.move_card(501, from_chat_id=-100, from_message_id=7,
                              to_chat_id=-100, to_message_id=11)
        self.assertIsNone(buy_session.active_card(501))


class HandlerWiringTests(unittest.TestCase):
    """The guard is only worth anything if /buypl actually consults it.

    Source-level, like the other handler-wiring tests here: importing
    ``handlers.buy`` pulls in the whole bot (database, config, Telegram) for
    assertions that are about which calls exist, not what they return.
    """

    def setUp(self):
        self.source = _read(_handler_path())

    def test_buypl_refuses_a_second_card(self):
        self.assertIn("pending = buy_session.active_card(tg_user.id)", self.source)
        self.assertIn("_refuse_second_buy(update, pending, tg_user.id)", self.source)

    def test_only_the_buy_command_takes_the_slot(self):
        """/playerinfo shares the carousel; browsing is not being in a buy."""
        self.assertIn("track=not restricted", self.source)
        self.assertIn("if track and sent is not None:", self.source)

    def test_both_answers_to_the_card_free_the_slot(self):
        confirm = self.source.split("async def buypl_confirm_callback")[1]
        self.assertIn("buy_session.close_card(tg_user.id,", confirm.split(
            "async def buypl_cancel_callback")[0])
        cancel = self.source.split("async def buypl_cancel_callback")[1]
        self.assertIn("buy_session.close_card(tg_user.id,", cancel)

    def test_the_refusal_offers_a_way_out(self):
        self.assertIn("buyclose_", self.source)
        self.assertIn("async def buypl_close_callback", self.source)

    def test_the_close_button_is_registered(self):
        self.assertIn(r'pattern=r"^buyclose_"', _read(_repo_path("bot.py")))


def _repo_path(*parts):
    import os
    return os.path.join(os.path.dirname(__file__), "..", *parts)


def _handler_path():
    return _repo_path("handlers", "buy.py")


def _read(path):
    with open(path) as fh:
        return fh.read()


if __name__ == "__main__":
    unittest.main()
