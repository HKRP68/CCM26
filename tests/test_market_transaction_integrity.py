"""Purchases and releases use the persisted wallet, with one receipt per sale."""

import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from config import MAX_ROSTER, get_buy_value, get_sell_value
from models import (
    ActivityLog, GlobalPlayerMarket, MarketPurchase, PendingUndo,
    Player, User, UserRoster,
)
from services import global_market as gm


def endpoint(name, namespace):
    """Run the real endpoint body without booting the unrelated admin app."""
    tree = ast.parse((Path(__file__).parents[1] / "admin.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), "admin.py", "exec"),
         namespace)
    return namespace[name]


class MarketTransactionIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        for model in (User, Player, UserRoster, GlobalPlayerMarket,
                      MarketPurchase, ActivityLog, PendingUndo):
            model.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False)
        self.session = self.Session()
        self.user = User(telegram_id=971, total_coins=10_000_000,
                         total_gems=0, roster_count=0)
        self.player = Player(name="Joe Root", rating=97, category="Batsman",
                             country="England", bat_hand="Right",
                             bowl_hand="Right", bowl_style="Offbreak")
        self.session.add_all([self.user, self.player])
        self.session.flush()
        self.slot = GlobalPlayerMarket(
            slot_index=0, player_id=self.player.id,
            base_price=get_buy_value(97), final_price=get_buy_value(97),
            quantity=1, purchased_count=0, is_active=True)
        self.session.add(self.slot)
        self.session.commit()
        self.user_id, self.player_id, self.slot_id = (
            self.user.id, self.player.id, self.slot.id)
        self.bonus = patch("services.buy_bonus.award_buy_gem_bonus", return_value=0)
        self.bonus.start()
        self.addCleanup(self.bonus.stop)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)

    def persisted(self, model):
        with self.Session() as check:
            return check.query(model).all()

    def external_balance(self, amount):
        # The original session deliberately keeps its previously loaded User.
        with self.Session() as other:
            other.execute(update(User).where(User.id == self.user_id)
                          .values(total_coins=amount))
            other.commit()

    def route(self, name, data=None):
        namespace = {
            "_webapp_auth": lambda: ((self.session, self.user, 971), 971, None),
            "_team_locked_response": lambda *a, **kw: None,
            "_release_prompt_open": lambda *a: None,
            "post_miniapp_activity": lambda *a, **kw: None,
            "Player": Player, "MAX_ROSTER": MAX_ROSTER,
            "logger": logging.getLogger(__name__),
            "request": SimpleNamespace(get_json=lambda **kw: data or {}),
        }
        return endpoint(name, namespace)

    def add_roster(self):
        row = UserRoster(user_id=self.user_id, player_id=self.player_id,
                         order_position=1)
        self.session.add(row)
        self.user.roster_count = 1
        self.session.commit()
        return row.id

    def test_global_market_rejects_stale_affordability_without_using_stock(self):
        self.assertEqual(self.user.total_coins, 10_000_000)
        self.external_balance(2_011_842)
        ok, _message, bonus = gm.buy_player(self.session, self.user, 0)
        self.assertFalse(ok)
        self.assertEqual(bonus, 0)
        self.session.commit()
        self.assertEqual(self.persisted(User)[0].total_coins, 2_011_842)
        self.assertEqual(self.persisted(GlobalPlayerMarket)[0].purchased_count, 0)
        self.assertEqual(self.persisted(UserRoster), [])
        self.assertEqual(self.persisted(MarketPurchase), [])

    def test_market_success_charges_once_and_writes_stock_and_receipt(self):
        ok, _message, _bonus = gm.buy_player(self.session, self.user, 0)
        self.assertTrue(ok)
        self.session.commit()
        self.assertEqual(self.persisted(User)[0].total_coins,
                         10_000_000 - get_buy_value(97))
        self.assertEqual(len(self.persisted(UserRoster)), 1)
        self.assertEqual(self.persisted(GlobalPlayerMarket)[0].purchased_count, 1)
        self.assertEqual(self.persisted(MarketPurchase)[0].price_paid,
                         get_buy_value(97))
        self.assertFalse(gm.buy_player(self.session, self.user, 0)[0])

    def test_failed_final_debit_rolls_back_stock_reservation(self):
        with patch.object(gm, "debit_coins", return_value=False):
            self.assertFalse(gm.buy_player(self.session, self.user, 0)[0])
        self.session.commit()
        self.assertEqual(self.persisted(GlobalPlayerMarket)[0].purchased_count, 0)
        self.assertEqual(self.persisted(MarketPurchase), [])
        self.assertEqual(self.persisted(UserRoster), [])

    def test_market_reroll_cannot_substitute_a_different_slot_id(self):
        self.assertFalse(gm.buy_player(
            self.session, self.user, 0, expected_slot_id=self.slot_id + 1)[0])
        self.session.commit()
        self.assertEqual(self.persisted(GlobalPlayerMarket)[0].purchased_count, 0)
        self.assertEqual(self.persisted(MarketPurchase), [])

    def test_ownership_lookup_failure_does_not_allow_purchase(self):
        with patch("services.version_service.user_owns_any_version",
                   side_effect=RuntimeError("ownership lookup failed")):
            with self.assertRaises(RuntimeError):
                gm.buy_player(self.session, self.user, 0)
        self.session.rollback()
        self.assertEqual(self.persisted(UserRoster), [])
        self.assertEqual(self.persisted(MarketPurchase), [])

    def test_miniapp_direct_buy_rechecks_persisted_balance(self):
        self.assertEqual(self.user.total_coins, 10_000_000)
        self.external_balance(2_011_842)
        body, status = self.route("webapp_buy")(self.player_id)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "insufficient_coins")
        self.assertEqual(self.persisted(User)[0].total_coins, 2_011_842)
        self.assertEqual(self.persisted(UserRoster), [])

    def test_miniapp_buy_and_undo_receipt_commit_together(self):
        with patch("services.undo_service.record_buy",
                   side_effect=RuntimeError("undo unavailable")):
            body, status = self.route("webapp_buy")(self.player_id)
        self.assertEqual(status, 500)
        self.assertFalse(body["ok"])
        self.assertEqual(self.persisted(User)[0].total_coins, 10_000_000)
        self.assertEqual(self.persisted(UserRoster), [])
        self.assertEqual(self.persisted(ActivityLog), [])

    def test_miniapp_success_links_undo_to_exact_purchase_log(self):
        body = self.route("webapp_buy")(self.player_id)
        self.assertTrue(body["ok"], body)
        log = self.persisted(ActivityLog)[0]
        undo = json.loads(self.persisted(PendingUndo)[0].payload)
        self.assertEqual(undo["activity_id"], log.id)
        self.assertEqual(undo["roster_id"], self.persisted(UserRoster)[0].id)
        self.assertEqual(log.coins_balance_after,
                         10_000_000 - get_buy_value(97))

    def test_miniapp_market_uses_atomic_service_and_exact_history_delta(self):
        body = self.route("webapp_market_buy")(self.slot_id)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["spent"], get_buy_value(97))
        self.assertEqual(len(self.persisted(MarketPurchase)), 1)
        log = self.persisted(ActivityLog)[0]
        self.assertEqual(log.coins_change, -get_buy_value(97))
        self.assertEqual(log.coins_balance_after, 10_000_000 - get_buy_value(97))

    def test_miniapp_release_records_valid_single_undo_and_no_double_refund(self):
        roster_id = self.add_roster()
        route = self.route("webapp_release")
        with patch("services.trait_service.return_traits_to_inventory", return_value=0):
            body = route(roster_id)
            self.assertTrue(body["ok"], body)
            again, status = route(roster_id)
        self.assertEqual(status, 404)
        self.assertFalse(again["ok"])
        self.assertEqual(self.persisted(User)[0].total_coins,
                         10_000_000 + get_sell_value(97))
        undo = self.persisted(PendingUndo)[0]
        self.assertEqual(undo.action_type, "release")
        payload = json.loads(undo.payload)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["player_id"], self.player_id)

    def test_miniapp_multi_release_records_whole_batch_once(self):
        first_id = self.add_roster()
        second = Player(name="Charlie Dean", rating=85, category="Bowler",
                        country="England", bat_hand="Right",
                        bowl_hand="Right", bowl_style="Offbreak")
        self.session.add(second)
        self.session.flush()
        row = UserRoster(user_id=self.user_id, player_id=second.id, order_position=2)
        self.session.add(row)
        self.user.roster_count = 2
        self.session.commit()
        second_id = row.id
        with patch("services.trait_service.return_traits_to_inventory", return_value=0):
            body = self.route("webapp_release_multi", {
                "roster_ids": [first_id, second_id, first_id],
            })()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["released_count"], 2)
        payload = json.loads(self.persisted(PendingUndo)[0].payload)
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["total_refund"],
                         get_sell_value(97) + get_sell_value(85))


if __name__ == "__main__":
    unittest.main()
