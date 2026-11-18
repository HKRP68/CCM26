"""Account-scoped receipts, wallet serialization and Telegram launch freshness."""
import hashlib
import hmac
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Pack, PackPurchase, UnopenedPack, User
from services.pack_service import buy_pack
from services.webapp_auth import MAX_AGE_SECONDS, verify_init_data


def signed(fields):
    key = hmac.digest(b"WebAppData", b"test", "sha256")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    signature = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": signature})


def test_launch_requires_a_single_fresh_signed_timestamp():
    fields = {"user": '{"id":7}'}
    assert verify_init_data(signed(fields), "test") is None
    now = int(time.time())
    for stamp in (now - MAX_AGE_SECONDS - 5, now + 120):
        assert verify_init_data(signed({**fields, "auth_date": stamp}), "test") is None
    good = signed({**fields, "auth_date": now})
    assert verify_init_data(good, "test")["user"]["id"] == 7
    assert verify_init_data(good + f"&auth_date={now}", "test") is None


@pytest.mark.parametrize("same_order", [True, False])
def test_overlapping_orders_share_one_wallet(tmp_path, same_order):
    engine = create_engine(f"sqlite:///{tmp_path / 'orders.sqlite'}")
    for model in (User, Pack, PackPurchase, UnopenedPack):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine, autoflush=False)
    with session() as db:
        db.add_all([User(telegram_id=11, total_coins=100),
                    Pack(slot_number=1, name="Debut", cost_coins=100, is_active=True)])
        db.commit()
    ready = Barrier(2)

    def place(order):
        with session() as db:
            user, pack = db.query(User).one(), db.query(Pack).one()
            ready.wait(timeout=5)
            result = buy_pack(db, user, pack, request_id=order)
            db.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(place, ["first-signing", "first-signing" if same_order else "second-order"]))
        if same_order:
            assert all(r["success"] for r in results)
            assert sum(bool(r.get("replayed")) for r in results) == 1
            assert results[0]["inventory_id"] == results[1]["inventory_id"]
        else:
            assert sum(r["success"] for r in results) == 1
        with session() as db:
            assert db.query(PackPurchase).count() == db.query(UnopenedPack).count() == 1
            assert db.query(User).one().total_coins == 0
    finally:
        engine.dispose()


def test_ad_status_is_read_only_and_callback_requires_secret(tmp_path, monkeypatch):
    import admin
    import database
    from models import AdReward
    from services import config_service

    engine = create_engine(f"sqlite:///{tmp_path / 'ads.sqlite'}")
    database.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    with session() as db:
        db.add(User(telegram_id=7))
        db.commit()
    monkeypatch.setattr(admin, "get_session", session)
    monkeypatch.setattr(config_service, "get_config", lambda *a, **kw: {})
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setenv("WEBAPP_DEV_MODE", "1")
    monkeypatch.setitem(admin.app.config, "TESTING", False)
    secret = "s" * 32
    monkeypatch.setenv("ADSGRAM_POSTBACK_SECRET", secret)
    client = admin.app.test_client()
    auth = {"Authorization": "tma " + signed({"user": '{"id":7}', "auth_date": str(int(time.time()))})}
    url = "/api/webapp/ad-completed"
    try:
        assert client.post(url, headers={"Authorization": "tma DEV_7"}).status_code == 401
        assert client.post(url, headers=auth).get_json()["ads_saved"] == 0
        for path in ("/api/ads/reward", "/api/adsgram/reward"):
            assert client.get(path, query_string={"userid": 7}).status_code == 403
        params = {"userid": 7, "secret": secret}
        assert client.get("/api/ads/reward", query_string=params).status_code == 200
        assert client.get("/api/adsgram/reward", query_string=params).status_code == 200
        for _ in range(2):
            assert client.post(url, headers=auth).get_json() == {
                "ok": True, "confirmed": True, "ads_saved": 1}
        with session() as db:
            assert not db.query(AdReward).one().query_string
    finally:
        engine.dispose()


@pytest.mark.parametrize("callback", [None, "before", "after"])
def test_sdk_completion_can_pay_once_with_or_without_callback(tmp_path, monkeypatch, callback):
    from datetime import datetime, timedelta

    import admin
    import database
    from models import AdReward
    from services import ad_service, config_service

    engine = create_engine(f"sqlite:///{tmp_path / 'sdk.sqlite'}")
    database.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    with session() as db:
        db.add(User(telegram_id=7))
        db.commit()
    monkeypatch.setattr(admin, "get_session", session)
    monkeypatch.setattr(admin, "post_miniapp_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(config_service, "get_config", lambda *args, **kwargs: {})
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setenv("ADSGRAM_BLOCK_ID", "1234")
    monkeypatch.setenv("ADSGRAM_POSTBACK_SECRET", "s" * 32 if callback else "")
    auth = {"Authorization": "tma " + signed({"user": '{"id":7}', "auth_date": str(int(time.time()))})}
    url = "/api/webapp/ad-completed"
    payload = {"view_id": "1" * 32}
    params = {"userid": 7, "secret": "s" * 32}
    try:
        with admin.app.test_client() as client:
            assert client.get("/api/ads/config").get_json()["ads"]["block_id"] == "1234"
            assert client.post(url, json=payload).status_code == 401
            assert client.post(url, headers=auth, json={"view_id": "invalid"}).status_code == 400
            assert client.post("/api/webapp/spin", headers=auth, json={}).get_json()["slot_type"] == "free"
            if callback == "before":
                assert client.get("/api/ads/reward", query_string=params).status_code == 200
            for _ in range(2):
                assert client.post(url, headers=auth, json=payload).get_json()["ads_saved"] == 1
            if callback == "after":
                assert client.get("/api/ads/reward", query_string=params).status_code == 200
            result = client.post("/api/webapp/spin", headers=auth, json={}).get_json()
            assert result["slot_type"] == "ad"
            assert result["ads_saved"] == 0
            with session() as db:
                db.query(AdReward).update({AdReward.received_at: datetime.utcnow() - timedelta(
                    seconds=ad_service.MIN_CLIENT_AD_INTERVAL_SECONDS + 1)})
                db.commit()
            assert client.post(url, headers=auth, json=payload).get_json()["ads_saved"] == 0
            assert client.post("/api/webapp/spin", headers=auth, json={}).get_json()["error"] == "ad_required"
    finally:
        engine.dispose()


def test_rejected_orders_share_an_account_limit_and_refill(tmp_path, monkeypatch):
    import admin
    import database
    from datetime import datetime, timedelta
    from services import config_service, economy_transactions

    engine = create_engine(f"sqlite:///{tmp_path / 'limit.sqlite'}")
    database.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    with session() as db:
        db.add(User(telegram_id=7, total_coins=100))
        db.commit()
    monkeypatch.setattr(admin, "get_session", session)
    monkeypatch.setattr(config_service, "get_config", lambda **kw: {})
    monkeypatch.setenv("BOT_TOKEN", "test")
    start = datetime.utcnow()
    class Clock:
        now = start

        @classmethod
        def utcnow(cls):
            return cls.now

    monkeypatch.setattr(economy_transactions, "datetime", Clock)
    headers = {"Authorization": "tma " + signed({"user": '{"id":7}', "auth_date": str(int(time.time()))})}
    client = admin.app.test_client()
    try:
        for _ in range(8):
            assert client.post("/api/webapp/packs/buy", json={}, headers=headers).status_code == 400
        response = client.post("/api/webapp/release_multi", json={}, headers=headers)
        assert (response.status_code, response.headers["Retry-After"]) == (429, "1")
        assert client.post("/api/webapp/roster", json={}, headers=headers).status_code == 200
        Clock.now += timedelta(seconds=1)
        assert client.post("/api/webapp/packs/buy", json={}, headers=headers).status_code == 400
        with session() as db:
            assert db.query(User).one().total_coins == 100
    finally:
        engine.dispose()


def test_pack_charges_the_selected_server_price(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'currency.db'}")
    for model in (User, Pack, PackPurchase, UnopenedPack):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine)
    try:
        with session() as db:
            user = User(telegram_id=8, total_coins=0, total_gems=20)
            pack = Pack(slot_number=1, name="Duo", cost_coins=100, cost_gems=10)
            db.add_all([user, pack])
            db.commit()
            assert not buy_pack(db, user, pack, payment="free")["success"]
            assert not buy_pack(db, user, pack, payment="gems", expected_cost=1)["success"]
            assert not buy_pack(db, user, pack, payment="coins")["success"]
            result = buy_pack(db, user, pack, payment="gems", expected_cost=10)
            db.commit()
            assert result["success"] and result["currency"] == "gems"
            assert db.query(PackPurchase).one().cost_paid == 10
            assert user.total_gems == 10 and user.total_coins == 0
    finally:
        engine.dispose()


def test_two_openers_get_one_pack_roll(tmp_path):
    import database
    from models import Player, UserRoster
    from services.pack_service import open_unopened_pack

    engine = create_engine(f"sqlite:///{tmp_path / 'open.sqlite'}")
    database.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)
    with session() as db:
        user = User(telegram_id=17, total_coins=100)
        player = Player(name="Test opener", category="Batsman", country="England",
                        bat_hand="Right", bowl_hand="Right", bowl_style="Pace", rating=85)
        pack = Pack(slot_number=1, name="Rookie", cost_coins=100, main_min_rating=85,
                    main_max_rating=85, main_count=1, bonus_count=0)
        db.add_all([user, pack, player])
        db.flush()
        inventory_id = buy_pack(db, user, pack)["inventory_id"]
        db.commit()
    ready = Barrier(2)

    def open_one(_):
        with session() as db:
            user = db.query(User).one()
            ready.wait(timeout=5)
            result = open_unopened_pack(db, user, inventory_id)
            db.commit()
            return result["success"]

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            assert sorted(workers.map(open_one, range(2))) == [False, True]
        with session() as db:
            assert db.query(UnopenedPack).count() == 0
            assert db.query(UserRoster).count() == db.query(User).one().roster_count == 1
    finally:
        engine.dispose()
