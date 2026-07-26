"""Traits always come back to their owner's inventory.

A trait is bought with gems, so it must survive the player it was equipped on
leaving the squad: selling/releasing, undoing a buy, trading, replacing the
occupant of a roster slot, or unequipping via /removetrait all return the trait
to the owner's inventory at the level it had reached.
"""

import os
import sys
import tempfile
import unittest


_PREV_DATABASE_URL = None
_SAVED_MODULES = {}
_TMP = None
_ENGINE = None
_MODULE_NAMES = ("database", "models", "config",
                 "services.trait_service", "services.trading_service")


def setUpModule():
    """Point the real SQLAlchemy models at a throwaway sqlite file."""
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


class _TraitFixtures:
    """Row builders shared by the test cases below (not a TestCase itself)."""

    _next_id = 1

    def _uid(self):
        type(self)._next_id += 1
        return type(self)._next_id

    def _user(self, session, coins=0):
        from models import User
        u = User(telegram_id=self._uid(), first_name="T", roster_count=0,
                 total_coins=coins, total_gems=0)
        session.add(u)
        session.flush()
        return u

    def _player(self, session, rating=80):
        from models import Player
        p = Player(name=f"P{self._uid()}", rating=rating, category="Batsman",
                   country="India", bat_hand="Right", bowl_hand="Right",
                   bowl_style="Medium")
        session.add(p)
        session.flush()
        return p

    def _roster(self, session, user, player, pos=1):
        from models import UserRoster
        r = UserRoster(user_id=user.id, player_id=player.id, order_position=pos)
        session.add(r)
        session.flush()
        return r

    def _trait(self, session, name=None):
        from models import Trait
        t = Trait(name=name or f"Trait{self._uid()}", category="Batting",
                  emoji="🔥", description="d", effect_key="bat_finisher",
                  is_active=True)
        session.add(t)
        session.flush()
        return t

    def _equip(self, session, user, roster, trait, level=1):
        from models import PlayerTrait
        pt = PlayerTrait(user_id=user.id, roster_id=roster.id,
                         trait_id=trait.id, level=level)
        session.add(pt)
        session.flush()
        return pt

    def _inventory(self, session, user):
        from models import TraitInventory
        return (session.query(TraitInventory)
                .filter(TraitInventory.user_id == user.id).all())


class TraitReturnTest(_TraitFixtures, unittest.TestCase):
    # ── return_traits_to_inventory ───────────────────────────────────

    def test_returned_trait_keeps_its_level_and_owner(self):
        from database import get_session
        from models import PlayerTrait
        from services.trait_service import return_traits_to_inventory
        session = get_session()
        try:
            user = self._user(session)
            roster = self._roster(session, user, self._player(session))
            trait = self._trait(session)
            self._equip(session, user, roster, trait, level=4)

            self.assertEqual(return_traits_to_inventory(session, roster.id), 1)

            inv = self._inventory(session, user)
            self.assertEqual(len(inv), 1)
            self.assertEqual(inv[0].trait_id, trait.id)
            self.assertEqual(inv[0].level, 4)  # a Lv.4 trait comes back Lv.4
            self.assertEqual(
                session.query(PlayerTrait)
                .filter(PlayerTrait.roster_id == roster.id).count(), 0)
        finally:
            session.rollback()
            session.close()

    def test_handles_many_roster_ids_and_no_ids(self):
        from database import get_session
        from services.trait_service import return_traits_to_inventory
        session = get_session()
        try:
            user = self._user(session)
            r1 = self._roster(session, user, self._player(session), pos=1)
            r2 = self._roster(session, user, self._player(session), pos=2)
            self._equip(session, user, r1, self._trait(session), level=2)
            self._equip(session, user, r1, self._trait(session), level=1)
            self._equip(session, user, r2, self._trait(session), level=5)

            self.assertEqual(
                return_traits_to_inventory(session, [r1.id, r2.id]), 3)
            self.assertEqual(len(self._inventory(session, user)), 3)

            # Idempotent, and tolerant of empty/None input.
            self.assertEqual(return_traits_to_inventory(session, [r1.id]), 0)
            self.assertEqual(return_traits_to_inventory(session, []), 0)
            self.assertEqual(return_traits_to_inventory(session, None), 0)
        finally:
            session.rollback()
            session.close()

    # ── /removetrait ─────────────────────────────────────────────────

    def test_remove_trait_from_player_returns_it_to_inventory(self):
        from database import get_session
        from models import PlayerTrait
        from services.trait_service import remove_trait_from_player
        session = get_session()
        try:
            user = self._user(session)
            roster = self._roster(session, user, self._player(session))
            pt = self._equip(session, user, roster, self._trait(session), level=3)

            ok, msg = remove_trait_from_player(session, user, pt.id)
            self.assertTrue(ok, msg)

            inv = self._inventory(session, user)
            self.assertEqual(len(inv), 1)
            self.assertEqual(inv[0].level, 3)  # removing never costs progress
            self.assertIsNone(session.query(PlayerTrait).get(pt.id))
        finally:
            session.rollback()
            session.close()

    def test_remove_trait_rejects_someone_elses_trait(self):
        from database import get_session
        from services.trait_service import remove_trait_from_player
        session = get_session()
        try:
            owner = self._user(session)
            other = self._user(session)
            roster = self._roster(session, owner, self._player(session))
            pt = self._equip(session, owner, roster, self._trait(session))

            ok, msg = remove_trait_from_player(session, other, pt.id)
            self.assertFalse(ok)
            self.assertIn("isn't equipped", msg)
            self.assertEqual(self._inventory(session, other), [])
        finally:
            session.rollback()
            session.close()

    def test_remove_trait_rejects_unknown_id(self):
        from database import get_session
        from services.trait_service import remove_trait_from_player
        session = get_session()
        try:
            user = self._user(session)
            ok, _ = remove_trait_from_player(session, user, 999999)
            self.assertFalse(ok)
        finally:
            session.rollback()
            session.close()

    # ── trade ────────────────────────────────────────────────────────

    def test_trade_returns_each_sides_traits_to_their_own_owner(self):
        from database import get_session
        from models import PlayerTrait, TraitInventory, Trade
        from datetime import datetime, timedelta
        from services.trading_service import complete_trade, trade_fee_for
        session = get_session()
        try:
            # A completed trade charges both captains the trade fee, so both
            # need enough coins for the swap to go through at all.
            fee = trade_fee_for(85)
            a = self._user(session, coins=fee * 2)
            b = self._user(session, coins=fee * 2)
            pa = self._player(session, rating=85)
            pb = self._player(session, rating=85)
            ra = self._roster(session, a, pa)
            rb = self._roster(session, b, pb)
            ta = self._trait(session)
            tb = self._trait(session)
            self._equip(session, a, ra, ta, level=2)
            self._equip(session, b, rb, tb, level=5)

            now = datetime.utcnow()
            trade = Trade(initiator_id=a.id, receiver_id=b.id,
                          initiator_player_id=pa.id, receiver_player_id=pb.id,
                          initiator_roster_id=ra.id, receiver_roster_id=rb.id,
                          status="pending", trade_fee=0, created_at=now,
                          expires_at=now + timedelta(minutes=5), updated_at=now)
            session.add(trade)
            session.flush()

            result = complete_trade(session, trade.id)
            self.assertTrue(result["success"], result["message"])
            self.assertEqual(result["traits_returned"], 2)

            # Players changed hands…
            self.assertEqual(ra.user_id, b.id)
            self.assertEqual(rb.user_id, a.id)
            # …but each trait went back to the captain who applied it.
            self.assertEqual(session.query(PlayerTrait).count(), 0)
            inv_a = self._inventory(session, a)
            inv_b = self._inventory(session, b)
            self.assertEqual([(i.trait_id, i.level) for i in inv_a],
                             [(ta.id, 2)])
            self.assertEqual([(i.trait_id, i.level) for i in inv_b],
                             [(tb.id, 5)])
            self.assertEqual(
                session.query(TraitInventory)
                .filter(TraitInventory.user_id.in_([a.id, b.id])).count(), 2)
        finally:
            session.rollback()
            session.close()

    # ── release ──────────────────────────────────────────────────────

    def test_release_returns_traits_and_reports_the_count(self):
        from database import get_session
        from handlers.release import _do_release
        from models import Player, UserRoster
        session = get_session()
        try:
            user = self._user(session, coins=0)
            player = self._player(session, rating=90)
            roster = self._roster(session, user, player)
            user.roster_count = 1
            self._equip(session, user, roster, self._trait(session), level=2)
            self._equip(session, user, roster, self._trait(session), level=1)

            entry = session.query(UserRoster).get(roster.id)
            pl = session.query(Player).get(player.id)
            result = _do_release(session, user, [(entry, pl)])

            self.assertTrue(result["success"])
            self.assertEqual(result["traits_returned"], 2)
            self.assertEqual(len(self._inventory(session, user)), 2)
            self.assertIsNone(session.query(UserRoster).get(roster.id))
        finally:
            session.rollback()
            session.close()


class RemoveTraitCommandTest(_TraitFixtures, unittest.TestCase):
    """/removetrait end-to-end: picker lists equipped traits, tap unequips."""

    def test_removetrait_lists_equipped_traits_then_unequips(self):
        import asyncio
        from unittest import mock
        from database import get_session
        import handlers.traits as ht
        from models import PlayerTrait, TraitInventory

        session = get_session()
        try:
            user = self._user(session)
            roster = self._roster(session, user, self._player(session))
            trait = self._trait(session, name="Finisher")
            pt = self._equip(session, user, roster, trait, level=3)
            tg_id, pt_id, user_id = user.telegram_id, pt.id, user.id
            session.commit()
        finally:
            session.close()

        # ── /removetrait shows one button per equipped trait ──────────
        update = mock.MagicMock()
        update.effective_user.id = tg_id
        replies = []

        async def _reply(text, **kwargs):
            replies.append((text, kwargs.get("reply_markup")))

        update.message.reply_text = _reply
        with mock.patch.object(ht, "get_session", get_session):
            asyncio.run(ht.removetrait_handler(update, mock.MagicMock()))

        self.assertEqual(len(replies), 1)
        text, markup = replies[0]
        self.assertIn("REMOVE TRAIT", text)
        rows = markup.inline_keyboard
        self.assertEqual(rows[0][0].callback_data, f"trrem_pt_{pt_id}")
        self.assertIn("Finisher", rows[0][0].text)

        # ── tapping it returns the trait to inventory at the same level ──
        q = mock.MagicMock()
        q.data = f"trrem_pt_{pt_id}"
        q.from_user.id = tg_id
        answers, edits = [], []

        async def _answer(text=None, **kwargs):
            answers.append(text)

        async def _edit(text, **kwargs):
            edits.append(text)

        q.answer = _answer
        q.edit_message_text = _edit
        cb_update = mock.MagicMock()
        cb_update.callback_query = q

        with mock.patch.object(ht, "get_session", get_session):
            asyncio.run(ht.trrem_pt_callback(cb_update, mock.MagicMock()))

        check = get_session()
        try:
            self.assertIsNone(check.query(PlayerTrait).get(pt_id))
            inv = (check.query(TraitInventory)
                   .filter(TraitInventory.user_id == user_id).all())
            self.assertEqual([i.level for i in inv], [3])
        finally:
            check.close()
        self.assertTrue(any("back in your inventory" in e for e in edits))


if __name__ == "__main__":
    unittest.main()
