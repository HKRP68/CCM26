"""End to end: /selltrait and /tradetrait against real rows in a real database.

``tests/test_trait_market.py`` pins the pricing and the service rules with fakes.
This drives the shipped Telegram handlers — pickers, confirm screens, the
two-captain swap — against a throwaway sqlite file, so the wiring is covered too:
callback data that the registered patterns actually match, gems that really move,
and inventory rows that really change hands.
"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.trait_service", "services.trait_trading_service",
                 "handlers.traits", "handlers.tradetrait")


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


class _Msg:
    """Minimal telegram Message: records replies, hands back the keyboard."""

    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.markups = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        sent = MagicMock()
        sent.chat_id = 5
        sent.message_id = len(self.replies)
        return sent

    @property
    def last(self):
        return self.replies[-1] if self.replies else ""

    def buttons(self):
        markup = self.markups[-1]
        if not markup:
            return []
        return [b for row in markup.inline_keyboard for b in row]


class _Query:
    """Minimal CallbackQuery: records the edited text and its keyboard."""

    def __init__(self, data, tg_id):
        self.data = data
        self.from_user = MagicMock()
        self.from_user.id = tg_id
        self.message = MagicMock()
        self.message.chat_id = 5
        self.message.message_id = 1
        self.edits = []
        self.markups = []
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def edit_message_reply_markup(self, **kwargs):
        pass

    @property
    def last(self):
        return self.edits[-1] if self.edits else ""

    def buttons(self):
        markup = self.markups[-1] if self.markups else None
        if not markup:
            return []
        return [b for row in markup.inline_keyboard for b in row]


def _update(msg=None, query=None, tg_id=None):
    update = MagicMock()
    update.message = msg
    update.effective_message = msg
    update.callback_query = query
    update.effective_chat = MagicMock()
    update.effective_chat.id = 5
    update.effective_chat.type = "group"
    if tg_id is not None:
        update.effective_user = MagicMock()
        update.effective_user.id = tg_id
        update.effective_user.username = f"u{tg_id}"
        update.effective_user.first_name = f"U{tg_id}"
        update.effective_user.is_bot = False
    return update


def _context():
    context = MagicMock()
    context.args = []
    context.bot_data = {}
    context.bot.send_message = AsyncMock()
    context.job_queue = None       # expiry job is optional
    return context


class TraitMarketFlowTests(unittest.TestCase):
    _seq = 100

    def _next(self):
        type(self)._seq += 1
        return type(self)._seq

    def setUp(self):
        from database import get_session
        from models import Trait, TraitInventory, User
        self.get_session = get_session
        session = get_session()
        try:
            self.tg1, self.tg2 = self._next(), self._next()
            self.u1 = User(telegram_id=self.tg1, username=f"u{self.tg1}",
                           first_name="Alpha", total_gems=1000, total_coins=0,
                           roster_count=0)
            self.u2 = User(telegram_id=self.tg2, username=f"u{self.tg2}",
                           first_name="Beta", total_gems=1000, total_coins=0,
                           roster_count=0)
            session.add_all([self.u1, self.u2])
            session.flush()
            self.u1_id, self.u2_id = self.u1.id, self.u2.id

            self.t1 = Trait(name=f"Power Hitter {self._next()}", category="batting",
                            description="d", emoji="💥", effect_key="bat_power_hitter")
            self.t2 = Trait(name=f"Death Bowler {self._next()}", category="bowling",
                            description="d", emoji="🎯", effect_key="bowl_death")
            session.add_all([self.t1, self.t2])
            session.flush()

            # One Lv.3 trait each, different traits — a legal swap.
            self.inv1 = TraitInventory(user_id=self.u1_id, trait_id=self.t1.id, level=3)
            self.inv2 = TraitInventory(user_id=self.u2_id, trait_id=self.t2.id, level=3)
            session.add_all([self.inv1, self.inv2])
            session.commit()
            self.inv1_id, self.inv2_id = self.inv1.id, self.inv2.id
        finally:
            session.close()

    def _gems(self, user_id):
        from models import User
        session = self.get_session()
        try:
            return session.query(User).get(user_id).total_gems
        finally:
            session.close()

    def _owner(self, inv_id):
        from models import TraitInventory
        session = self.get_session()
        try:
            row = session.query(TraitInventory).get(inv_id)
            return row.user_id if row else None
        finally:
            session.close()

    # ── /selltrait ───────────────────────────────────────────────────

    def test_selltrait_quotes_then_sells_for_the_level_price(self):
        from config import trait_sell_value
        from handlers import traits as h
        from utils.idempotency import release as _release

        msg = _Msg("/selltrait")
        asyncio.run(h.selltrait_handler(_update(msg=msg, tg_id=self.tg1), _context()))
        self.assertIn("SELL TRAIT", msg.last)
        # The price list is on the card, and the trait is a tappable option.
        self.assertIn(f"{trait_sell_value(3):,}", msg.last)
        data = [b.callback_data for b in msg.buttons()]
        self.assertIn(f"trsell_{self.inv1_id}", data)

        confirm = _Query(f"trsell_{self.inv1_id}", self.tg1)
        asyncio.run(h.trsell_callback(_update(query=confirm), _context()))
        self.assertIn("SELL THIS TRAIT?", confirm.last)
        self.assertIn(f"{trait_sell_value(3):,}", confirm.last)
        self.assertIn(f"trsellok_{self.inv1_id}",
                      [b.callback_data for b in confirm.buttons()])

        before = self._gems(self.u1_id)
        _release(f"trsell_{self.inv1_id}")      # fresh claim for this test
        sale = _Query(f"trsellok_{self.inv1_id}", self.tg1)
        asyncio.run(h.trsellok_callback(_update(query=sale), _context()))

        self.assertIn("TRAIT SOLD", sale.last)
        self.assertEqual(self._gems(self.u1_id), before + trait_sell_value(3))
        self.assertIsNone(self._owner(self.inv1_id))   # row is gone

    def test_selltrait_refuses_someone_elses_trait(self):
        from handlers import traits as h
        from utils.idempotency import release as _release

        before = self._gems(self.u2_id)
        _release(f"trsell_{self.inv1_id}")
        theft = _Query(f"trsellok_{self.inv1_id}", self.tg2)
        asyncio.run(h.trsellok_callback(_update(query=theft), _context()))

        self.assertEqual(self._gems(self.u2_id), before)
        self.assertEqual(self._owner(self.inv1_id), self.u1_id)

    def test_selltrait_with_an_empty_inventory_says_so(self):
        from database import get_session
        from models import TraitInventory
        from handlers import traits as h

        session = get_session()
        try:
            session.query(TraitInventory).filter(
                TraitInventory.user_id == self.u1_id).delete()
            session.commit()
        finally:
            session.close()

        msg = _Msg("/selltrait")
        asyncio.run(h.selltrait_handler(_update(msg=msg, tg_id=self.tg1), _context()))
        self.assertIn("Nothing to sell", msg.last)
        self.assertIn("/removetrait", msg.last)

    # ── /tradetrait ──────────────────────────────────────────────────

    def _start_trade(self, context):
        from handlers import tradetrait as h
        msg = _Msg(f"/tradetrait @u{self.tg2}")
        update = _update(msg=msg, tg_id=self.tg1)
        context.args = [f"@u{self.tg2}"]
        asyncio.run(h.tradetrait_handler(update, context))
        return msg

    def test_the_full_swap_charges_both_and_moves_both_traits(self):
        from config import trait_trade_fee
        from handlers import tradetrait as h

        context = _context()
        msg = self._start_trade(context)
        self.assertIn("TRAIT TRADE", msg.last)
        trade_id = [b.callback_data for b in msg.buttons()][0].split("_")[1]

        pick1 = _Query(f"tt1_{trade_id}_{self.inv1_id}", self.tg1)
        asyncio.run(h.tradetrait_user1_callback(_update(query=pick1), context))
        self.assertIn("offers", pick1.last)
        self.assertIn(f"{trait_trade_fee(3):,}", pick1.last)

        pick2 = _Query(f"tt2_{trade_id}_{self.inv2_id}", self.tg2)
        asyncio.run(h.tradetrait_user2_callback(_update(query=pick2), context))
        self.assertIn("CONFIRM", pick2.last)

        before1, before2 = self._gems(self.u1_id), self._gems(self.u2_id)
        ok1 = _Query(f"ttcfrm_{trade_id}_{self.u1_id}", self.tg1)
        asyncio.run(h.tradetrait_confirm_callback(_update(query=ok1), context))
        self.assertIn("Waiting for", ok1.last)
        # One confirmation is not enough — nothing has moved yet.
        self.assertEqual(self._owner(self.inv1_id), self.u1_id)

        ok2 = _Query(f"ttcfrm_{trade_id}_{self.u2_id}", self.tg2)
        asyncio.run(h.tradetrait_confirm_callback(_update(query=ok2), context))

        fee = trait_trade_fee(3)
        self.assertEqual(self._owner(self.inv1_id), self.u2_id)
        self.assertEqual(self._owner(self.inv2_id), self.u1_id)
        self.assertEqual(self._gems(self.u1_id), before1 - fee)
        self.assertEqual(self._gems(self.u2_id), before2 - fee)
        sent = context.bot.send_message.call_args.kwargs["text"]
        self.assertIn("TRAIT TRADE COMPLETE", sent)

    def test_a_mismatched_level_leaves_nothing_to_pick(self):
        from database import get_session
        from models import TraitInventory
        from handlers import tradetrait as h

        session = get_session()
        try:
            session.query(TraitInventory).get(self.inv2_id).level = 5
            session.commit()
        finally:
            session.close()

        context = _context()
        msg = self._start_trade(context)
        trade_id = [b.callback_data for b in msg.buttons()][0].split("_")[1]
        pick1 = _Query(f"tt1_{trade_id}_{self.inv1_id}", self.tg1)
        asyncio.run(h.tradetrait_user1_callback(_update(query=pick1), context))

        self.assertIn("no other unequipped", pick1.last)
        self.assertIn("Lv.3", pick1.last)
        self.assertEqual(self._owner(self.inv1_id), self.u1_id)

    def test_a_bystander_cannot_drive_the_trade(self):
        from handlers import tradetrait as h

        context = _context()
        msg = self._start_trade(context)
        trade_id = [b.callback_data for b in msg.buttons()][0].split("_")[1]

        # Someone with no stake in the trade is told exactly that.
        outsider = _Query(f"tt1_{trade_id}_{self.inv1_id}", 999_999)
        asyncio.run(h.tradetrait_user1_callback(_update(query=outsider), context))
        self.assertEqual(outsider.edits, [])
        self.assertIn("not part of this trade", " ".join(
            a for a in outsider.answers if a))

        # The counterparty tapping the initiator's picker is refused just as
        # firmly, but told whose turn it is instead of that they're a stranger
        # to their own trade.
        wrong = _Query(f"tt1_{trade_id}_{self.inv1_id}", self.tg2)
        asyncio.run(h.tradetrait_user1_callback(_update(query=wrong), context))
        self.assertEqual(wrong.edits, [])
        answered = " ".join(a for a in wrong.answers if a)
        self.assertIn("picks first", answered)
        self.assertNotIn("not part of this trade", answered)

    def test_only_one_trait_trade_per_captain(self):
        from handlers import tradetrait as h

        context = _context()
        self._start_trade(context)
        second = _Msg(f"/tradetrait @u{self.tg2}")
        context.args = [f"@u{self.tg2}"]
        asyncio.run(h.tradetrait_handler(_update(msg=second, tg_id=self.tg1), context))
        self.assertIn("already has a trait trade open", second.last)

    def test_cancelling_frees_the_captains(self):
        from handlers import tradetrait as h

        context = _context()
        msg = self._start_trade(context)
        trade_id = [b.callback_data for b in msg.buttons()][0].split("_")[1]

        cancel = _Query(f"ttcancel_{trade_id}", self.tg1)
        asyncio.run(h.tradetrait_cancel_callback(_update(query=cancel), context))
        self.assertIn("cancelled", cancel.last)

        again = _Msg(f"/tradetrait @u{self.tg2}")
        context.args = [f"@u{self.tg2}"]
        asyncio.run(h.tradetrait_handler(_update(msg=again, tg_id=self.tg1), context))
        self.assertIn("TRAIT TRADE", again.last)

    def test_trading_with_yourself_is_refused(self):
        from handlers import tradetrait as h

        context = _context()
        msg = _Msg(f"/tradetrait @u{self.tg1}")
        context.args = [f"@u{self.tg1}"]
        asyncio.run(h.tradetrait_handler(_update(msg=msg, tg_id=self.tg1), context))
        self.assertIn("cannot trade with yourself", msg.last)


if __name__ == "__main__":
    unittest.main()
