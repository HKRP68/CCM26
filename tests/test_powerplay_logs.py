"""Power Play logs the wallet delta, including the stake on losing bets."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def account(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base
    from handlers import powerplay
    from models import User

    engine = create_engine(f"sqlite:///{tmp_path / 'powerplay.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    user = User(telegram_id=98765, total_coins=1000, total_gems=25)
    db.add(user)
    db.commit()
    monkeypatch.setattr(powerplay, "get_session", factory)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user.telegram_id),
        message=SimpleNamespace(reply_text=AsyncMock()),
    )
    yield SimpleNamespace(db=db, user=user, update=update, handler=powerplay)
    db.close()
    engine.dispose()


@pytest.mark.parametrize("bet,target,crash,payout,net", [
    (100, 2.5, 3.0, 250, 150),
    (100, 2.5, 1.0, 0, -100),
    (101, 1.25, 1.25, 126, 25),
])
def test_log_records_net_profit_or_loss(account, monkeypatch,
                                       bet, target, crash, payout, net):
    from models import ActivityLog

    monkeypatch.setattr(account.handler, "_crash_point", lambda: crash)
    context = SimpleNamespace(args=[str(bet), str(target)])
    asyncio.run(account.handler.powerplay_handler(account.update, context))

    account.db.expire_all()
    log = account.db.query(ActivityLog).filter_by(action="powerplay").one()
    assert account.user.total_coins == 1000 + net
    assert log.coins_change == net
    assert log.coins_balance_after == 1000 + net
    assert log.gems_change == 0
    assert log.gems_balance_after == 25
    assert f"Bet: {bet:,} coins" in log.detail
    assert f"Target: {target}×" in log.detail
    assert f"Crash: {crash}×" in log.detail
    assert f"Payout: {payout:,} coins" in log.detail
    assert f"{'Profit' if net > 0 else 'Loss'}: {net:+,} coins" in log.detail
    assert f"{1000 + net:,}" in account.update.message.reply_text.call_args.args[0]


def test_insufficient_balance_does_not_log_a_loss(account, monkeypatch):
    from models import ActivityLog

    monkeypatch.setattr(account.handler, "_crash_point", lambda: 1.0)
    context = SimpleNamespace(args=["1001", "2"])
    asyncio.run(account.handler.powerplay_handler(account.update, context))

    account.db.expire_all()
    assert account.user.total_coins == 1000
    assert account.db.query(ActivityLog).count() == 0
