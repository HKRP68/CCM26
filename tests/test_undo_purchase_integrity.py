"""Refunds consume one purchase and preserve the wallet/roster atomically."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import ActivityLog, PendingUndo, Player, User, UserRoster
from handlers import undo
from handlers.release import _do_release
from services import undo_service


@pytest.fixture
def account(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'undo.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = factory()
    user = User(telegram_id=98765, total_coins=550_000, total_gems=75,
                roster_count=1)
    player = Player(name="Joe Root", rating=97, category="Batsman",
                    country="England", bat_hand="Right", bowl_hand="Right",
                    bowl_style="Off spin")
    db.add_all([user, player])
    db.flush()
    entry = UserRoster(user_id=user.id, player_id=player.id, order_position=1)
    db.add(entry)
    db.flush()
    undo_service.record_buy(db, user.id, roster_id=entry.id, player_id=player.id,
                            player_name=player.name, rating=97, price=4_450_000,
                            gem_bonus=50, activity_id=123)
    db.commit()
    monkeypatch.setattr(undo, "get_session", factory)
    monkeypatch.setattr(undo, "match_lock_message", lambda *a, **k: None)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user.telegram_id),
                             message=SimpleNamespace(reply_text=AsyncMock()))
    yield SimpleNamespace(db=db, factory=factory, user=user, player=player,
                          entry=entry, update=update)
    db.close()
    engine.dispose()


def run_undo(account):
    asyncio.run(undo.cmuundo_handler(account.update, None))
    account.db.expire_all()


def test_buy_refund_reclaims_bonus_and_cannot_be_replayed(account):
    run_undo(account)
    assert account.user.total_coins == 5_000_000
    assert account.user.total_gems == 25
    assert account.user.roster_count == 0
    assert account.db.query(UserRoster).count() == 0
    assert account.db.query(PendingUndo).count() == 0
    log = account.db.query(ActivityLog).filter_by(action="undo_buy").one()
    assert (log.coins_change, log.gems_change) == (4_450_000, -50)
    assert "purchase #123" in log.detail
    run_undo(account)
    assert account.user.total_coins == 5_000_000
    assert account.user.total_gems == 25
    assert account.db.query(ActivityLog).filter_by(action="undo_buy").count() == 1


def test_concurrent_undo_requests_refund_only_once(account, monkeypatch):
    barrier = threading.Barrier(2)
    real_lock = undo.lock_user_for_update

    def synchronized_lock(session, user):
        barrier.wait(timeout=5)
        return real_lock(session, user)

    monkeypatch.setattr(undo, "lock_user_for_update", synchronized_lock)

    def request():
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=98765),
            message=SimpleNamespace(reply_text=AsyncMock()))
        asyncio.run(undo.cmuundo_handler(update, None))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(request) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    account.db.expire_all()
    assert account.user.total_coins == 5_000_000
    assert account.user.total_gems == 25
    assert account.user.roster_count == 0
    assert account.db.query(UserRoster).count() == 0
    assert account.db.query(ActivityLog).filter_by(action="undo_buy").count() == 1


def test_spent_signing_bonus_blocks_refund_and_keeps_undo(account):
    account.user.total_gems = 49
    account.db.commit()
    run_undo(account)
    assert account.user.total_coins == 550_000
    assert account.user.total_gems == 49
    assert account.db.query(UserRoster).count() == 1
    assert account.db.query(PendingUndo).count() == 1
    assert "signing bonus" in account.update.message.reply_text.call_args.args[0]


def test_trait_return_failure_rolls_back_claim_and_refund(account, monkeypatch):
    from services import trait_service

    def fail(*args):
        raise RuntimeError("trait storage unavailable")

    monkeypatch.setattr(trait_service, "return_traits_to_inventory", fail)
    run_undo(account)
    assert account.user.total_coins == 550_000
    assert account.user.total_gems == 75
    assert account.db.query(UserRoster).count() == 1
    assert account.db.query(PendingUndo).count() == 1
    assert account.db.query(ActivityLog).count() == 0


def test_no_refund_after_player_was_transferred(account):
    other = User(telegram_id=45678, total_coins=0, roster_count=1)
    account.db.add(other)
    account.db.flush()
    account.entry.user_id = other.id
    account.db.commit()
    run_undo(account)
    assert account.user.total_coins == 550_000
    assert account.db.query(UserRoster).one().user_id == other.id
    assert account.db.query(PendingUndo).count() == 0


def test_match_blocks_undo_without_consuming_it(account, monkeypatch):
    monkeypatch.setattr(undo, "match_lock_message", lambda *a, **k: "Match locked")
    run_undo(account)
    assert account.user.total_coins == 550_000
    assert account.db.query(UserRoster).count() == 1
    assert account.db.query(PendingUndo).count() == 1


def test_two_stale_readers_can_only_consume_once(account):
    second = account.factory()
    try:
        first_row, _ = undo_service.get_pending(account.db, account.user.id)
        second_row, _ = undo_service.get_pending(second, account.user.id)
        assert undo_service.consume_pending(account.db, first_row)
        account.db.commit()
        assert not undo_service.consume_pending(second, second_row)
        second.rollback()
    finally:
        second.close()


def test_old_request_cannot_consume_newer_purchase(account):
    second = account.factory()
    try:
        old_row, _ = undo_service.get_pending(second, account.user.id)
        undo_service.record_buy(account.db, account.user.id,
                                roster_id=account.entry.id,
                                player_id=account.player.id,
                                player_name=account.player.name, rating=97,
                                price=1234)
        account.db.commit()
        assert not undo_service.consume_pending(second, old_row)
        second.rollback()
        assert json.loads(account.db.query(PendingUndo).one().payload)["price"] == 1234
    finally:
        second.close()


def test_rolled_back_claim_can_be_retried(account):
    row, _ = undo_service.get_pending(account.db, account.user.id)
    assert undo_service.consume_pending(account.db, row)
    account.db.rollback()
    row, _ = undo_service.get_pending(account.db, account.user.id)
    assert row is not None
    assert undo_service.consume_pending(account.db, row)
    account.db.rollback()


def test_expired_claim_cannot_be_consumed_for_refund(account):
    row = account.db.query(PendingUndo).one()
    row.expires_at = datetime.utcnow() - timedelta(seconds=1)
    account.db.commit()
    assert not undo_service.consume_pending(account.db, row)
    account.db.rollback()
    run_undo(account)
    assert account.user.total_coins == 550_000
    assert account.db.query(PendingUndo).count() == 0


def test_malformed_pending_is_cleaned_without_refunding(account):
    account.db.query(PendingUndo).one().payload = "[]"
    account.db.commit()
    run_undo(account)
    assert account.user.total_coins == 550_000
    assert account.db.query(PendingUndo).count() == 0


@pytest.mark.parametrize("field,value", [("price", -1), ("gem_bonus", -50),
                                         ("price", True), ("price", "4450000")])
def test_invalid_refund_amount_never_changes_wallet(account, field, value):
    row = account.db.query(PendingUndo).one()
    payload = json.loads(row.payload)
    payload[field] = value
    row.payload = json.dumps(payload)
    account.db.commit()
    run_undo(account)
    assert account.user.total_coins == 550_000
    assert account.user.total_gems == 75
    assert account.db.query(UserRoster).count() == 1
    assert account.db.query(PendingUndo).count() == 0


def test_invalid_release_total_never_mints_coins(account):
    row = account.db.query(PendingUndo).one()
    row.action_type = "release"
    row.payload = json.dumps({"items": [{"player_id": account.player.id,
                                        "player_name": account.player.name,
                                        "rating": 97, "price": 10}],
                              "total_refund": -100})
    account.db.commit()
    run_undo(account)
    assert account.user.total_coins == 550_000
    assert account.db.query(UserRoster).count() == 1
    assert account.db.query(PendingUndo).count() == 0


def test_repeated_release_undo_is_a_zero_coin_loop(account):
    initial = account.user.total_coins
    for _ in range(3):
        entry = account.db.query(UserRoster).one()
        result = _do_release(account.db, account.user, [(entry, account.player)])
        assert result["success"]
        account.db.commit()
        run_undo(account)
        assert account.user.total_coins == initial
        assert account.user.total_gems == 75
        assert account.user.roster_count == 1
        assert account.db.query(UserRoster).count() == 1


def test_stale_release_cannot_credit_the_same_player_twice(account):
    second = account.factory()
    try:
        stale_user = second.get(User, account.user.id)
        stale_entry = second.get(UserRoster, account.entry.id)
        stale_player = second.get(Player, account.player.id)
        first = _do_release(account.db, account.user,
                            [(account.entry, account.player)])
        assert first["success"]
        account.db.commit()
        balance = account.user.total_coins
        repeated = _do_release(second, stale_user, [(stale_entry, stale_player)])
        assert not repeated["success"]
        assert repeated["error"] == "roster_changed"
        second.rollback()
        account.db.refresh(account.user)
        assert account.user.total_coins == balance
        assert account.db.query(ActivityLog).filter_by(action="release").count() == 1
    finally:
        second.close()


def test_duplicate_release_entries_are_rejected_before_credit(account):
    result = _do_release(account.db, account.user,
                         [(account.entry, account.player)] * 2)
    assert not result["success"]
    account.db.commit()
    assert account.user.total_coins == 550_000
    assert account.db.query(UserRoster).count() == 1


def test_stale_release_refuses_replaced_roster_row(account):
    second = account.factory()
    try:
        stale_user = second.get(User, account.user.id)
        stale_entry = second.get(UserRoster, account.entry.id)
        stale_player = second.get(Player, account.player.id)
        replacement = Player(name="Replacement", rating=85, category="Batsman",
                             country="England", bat_hand="Right",
                             bowl_hand="Right", bowl_style="Medium")
        account.db.add(replacement)
        account.db.flush()
        account.entry.player_id = replacement.id
        account.db.commit()
        result = _do_release(second, stale_user, [(stale_entry, stale_player)])
        assert not result["success"]
        second.rollback()
        account.db.expire_all()
        assert account.user.total_coins == 550_000
        assert account.db.query(UserRoster).one().player_id == replacement.id
    finally:
        second.close()


def test_undo_record_failure_rolls_back_release(account, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("undo storage unavailable")

    monkeypatch.setattr(undo_service, "record_release", fail)
    with pytest.raises(RuntimeError, match="undo storage unavailable"):
        _do_release(account.db, account.user, [(account.entry, account.player)])
    account.db.rollback()
    assert account.user.total_coins == 550_000
    assert account.db.query(UserRoster).count() == 1
    assert account.db.query(ActivityLog).count() == 0


def test_stale_release_after_buy_undo_cannot_credit_removed_player(account):
    second = account.factory()
    try:
        stale_user = second.get(User, account.user.id)
        stale_entry = second.get(UserRoster, account.entry.id)
        stale_player = second.get(Player, account.player.id)
        run_undo(account)
        result = _do_release(second, stale_user, [(stale_entry, stale_player)])
        assert not result["success"]
        second.rollback()
        account.db.refresh(account.user)
        assert account.user.total_coins == 5_000_000
        assert account.db.query(ActivityLog).filter_by(action="release").count() == 0
    finally:
        second.close()
