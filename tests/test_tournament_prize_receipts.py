"""Recipient receipts remain private, paginated and safe to retry."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _module_swap
import _tour_fixture as F

_SAVED = None


def setUpModule():
    global _SAVED
    _SAVED = _module_swap.save(("handlers.profile",))
    _module_swap.unload(("handlers.profile",))
    F.setup_module_db()


def tearDownModule():
    F.teardown_module_db()
    _module_swap.restore(_SAVED)


class ReceiptTests(F.FourTeamCase):
    def receipt(self, owner, **fields):
        from models import TournamentPrizePayout
        values = dict(
            tournament_id=self.tour.id, tournament_name=self.tour.name,
            award="champion", team_name="Alpha", recipient_tg_id=owner.telegram_id,
            user_id=owner.id, coins=500, gems=5, share_bps=5000, status="pending")
        values.update(fields)
        row = TournamentPrizePayout(**values)
        self.session.add(row)
        self.session.commit()
        return row

    def callback(self, owner, chat_type):
        query = SimpleNamespace(
            data=f"mp_prizes_{owner.id}", from_user=SimpleNamespace(id=owner.telegram_id),
            answer=AsyncMock(), edit_message_text=AsyncMock())
        update = SimpleNamespace(callback_query=query,
                                 effective_chat=SimpleNamespace(type=chat_type))
        from database import get_session
        from handlers import profile
        with patch.object(profile, "get_session", side_effect=get_session):
            asyncio.run(profile.myprofile_callback(update, SimpleNamespace()))
        return query

    def test_group_callback_never_exposes_or_settles_private_receipts(self):
        owner = self.owners["Alpha"]
        row = self.receipt(owner)
        query = self.callback(owner, "supergroup")
        query.edit_message_text.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.session.refresh(row)
        self.session.refresh(owner)
        self.assertEqual(row.status, "pending")
        self.assertEqual(owner.total_coins, 0)

    def test_private_callback_releases_only_own_share_and_shows_balance_receipt(self):
        owner, other = self.owners["Alpha"], self.owners["Bravo"]
        own_receipt, other_receipt = self.receipt(owner), self.receipt(other)
        for _ in range(2):
            query = self.callback(owner, "private")
            text = query.edit_message_text.await_args.args[0]
            self.assertIn(f"#{own_receipt.id} ·", text)
            self.assertNotIn(f"#{other_receipt.id} ·", text)
            self.assertIn("Balance after credit: 500 coins + 5 gems", text)
        self.session.refresh(owner)
        self.session.refresh(other)
        self.assertEqual((owner.total_coins, owner.total_gems), (500, 5))
        self.assertEqual((other.total_coins, other.total_gems), (0, 0))

    def test_long_receipt_pages_preserve_complete_rows_without_skipping(self):
        from services.tournament_payouts import recipient_receipt_page
        owner = self.owners["Alpha"]
        ids = []
        for _ in range(18):
            row = self.receipt(
                owner, tournament_name="&" * 120, team_name="<" * 120,
                status="paid", balance_coins_after=500, balance_gems_after=5)
            ids.append(row.id)
        cursor, seen = None, []
        for _ in range(20):
            text, cursor = recipient_receipt_page(self.session, owner.telegram_id,
                                                  before_id=cursor)
            self.assertLessEqual(len(text), 3500)
            page_ids = [rid for rid in reversed(ids) if f"#{rid} ·" in text]
            self.assertTrue(page_ids)
            seen.extend(page_ids)
            if cursor is None:
                break
        self.assertEqual(seen, list(reversed(ids)))
