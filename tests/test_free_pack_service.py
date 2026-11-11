"""Free Pack weights and sparse player pools must respect rating bands."""

import json
import random
from types import SimpleNamespace

import pytest


@pytest.fixture
def account(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base
    from models import User
    from services import free_pack_service

    engine = create_engine(f"sqlite:///{tmp_path / 'freepack.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    user = User(telegram_id=98765, total_coins=1000, roster_count=0)
    db.add(user)
    db.commit()
    yield SimpleNamespace(db=db, user=user, service=free_pack_service)
    db.close()
    engine.dispose()


def add_player(account, rating):
    from models import Player

    player = Player(name=f"Player {rating}", rating=rating, category="Batsman",
                    country="India", version="Base", is_active=True,
                    is_career=False, bat_hand="Right", bowl_hand="Right",
                    bowl_style="Off spin")
    account.db.add(player)
    account.db.flush()
    return player


def test_default_draws_favor_70_to_80_and_make_87_plus_rare(account, monkeypatch):
    bands = account.service.get_bands(account.db)
    rng = random.Random(12345)
    monkeypatch.setattr(account.service.random, "uniform", rng.uniform)

    draws = [account.service._roll_band(bands) for _ in range(100_000)]
    standard = sum(b["min"] == 70 and b["max"] == 80 for b in draws)
    high = sum(b["min"] > 86 for b in draws)
    assert 94_500 <= standard <= 95_500
    assert 50 <= high <= 150


def test_custom_odds_remain_configurable(account):
    from models import GameConfig

    bands = [{"min": 70, "max": 80, "weight": 99.5, "label": "Standard"},
             {"min": 87, "max": 90, "weight": 0.5, "label": "Elite"}]
    account.db.add(GameConfig(free_pack_bands_json=json.dumps(bands)))
    account.db.flush()
    assert account.service.get_bands(account.db) == bands


@pytest.mark.parametrize("roll,expected_rating", [
    (50, 70), (97, 81), (99.5, 84), (99.95, 87),
])
def test_open_awards_one_player_from_the_rolled_band(account, monkeypatch,
                                                  roll, expected_rating):
    from models import ActivityLog, UserRoster, UserStats

    for rating in (70, 81, 84, 87):
        add_player(account, rating)
    monkeypatch.setattr(account.service.random, "uniform", lambda *_: roll)
    result = account.service.open_free_pack(account.db, account.user)
    account.db.commit()

    assert result["ok"]
    assert result["player"]["rating"] == expected_rating
    assert account.user.roster_count == 1
    assert account.db.query(UserRoster).count() == 1
    assert account.db.query(ActivityLog).filter_by(action="free_pack").count() == 1
    assert account.db.get(UserStats, account.user.id).last_free_pack is not None
    assert account.user.total_coins == 1000
    assert account.service.open_free_pack(account.db, account.user)["error"] == "cooldown"


def test_missing_86_does_not_widen_into_87(account, monkeypatch):
    add_player(account, 85)
    add_player(account, 87)
    monkeypatch.setattr(account.service.random, "uniform", lambda *_: 99.5)
    monkeypatch.setattr(account.service.random, "shuffle", lambda ratings: ratings.reverse())
    monkeypatch.setattr(account.service.random, "choice", lambda pool: max(pool, key=lambda p: p.rating))
    result = account.service.open_free_pack(account.db, account.user)

    assert result["ok"]
    assert result["player"]["rating"] == 85


def test_empty_band_does_not_award_a_higher_card_or_start_cooldown(account, monkeypatch):
    from models import UserRoster, UserStats

    add_player(account, 87)
    monkeypatch.setattr(account.service.random, "uniform", lambda *_: 99.5)
    result = account.service.open_free_pack(account.db, account.user)

    assert result["error"] == "no_player"
    assert account.db.get(UserStats, account.user.id).last_free_pack is None
    assert account.db.query(UserRoster).count() == 0
    assert account.user.roster_count == 0
