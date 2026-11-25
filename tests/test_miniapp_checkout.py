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
def test_ad_rewards_require_an_authenticated_callback(tmp_path, monkeypatch, callback):
    import admin
    import database
    from services import config_service

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
    monkeypatch.setenv("ADSGRAM_REQUIRE_POSTBACK", "1")
    monkeypatch.setenv("ADSGRAM_POSTBACK_SECRET", "s" * 32 if callback else "")
    auth = {"Authorization": "tma " + signed({"user": '{"id":7}', "auth_date": str(int(time.time()))})}
    url = "/api/webapp/ad-completed"
    payload = {"view_id": "1" * 32}
    params = {"userid": 7, "secret": "s" * 32}
    try:
        with admin.app.test_client() as client:
            assert client.get("/api/ads/config").get_json()["ads"]["block_id"] == ("1234" if callback else None)
            assert client.post(url, json=payload).status_code == 401
            assert client.post(url, headers=auth, json={"view_id": "invalid"}).status_code == 400
            assert client.post("/api/webapp/spin", headers=auth, json={}).get_json()["slot_type"] == "free"
            if callback == "before":
                assert client.get("/api/ads/reward", query_string=params).status_code == 200
            for _ in range(2):
                assert client.post(url, headers=auth, json=payload).get_json()["ads_saved"] == (1 if callback == "before" else 0)
            if callback == "after":
                assert client.post("/api/webapp/spin", headers=auth, json={}).get_json()["error"] == "ad_required"
                assert client.get("/api/ads/reward", query_string=params).status_code == 200
            result = client.post("/api/webapp/spin", headers=auth, json={}).get_json()
            if callback:
                assert result["slot_type"] == "ad"
                assert result["ads_saved"] == 0
            else:
                assert result["error"] == "ad_required"
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
            assert client.post("/api/webapp/economy/quote", json={}, headers=headers).status_code == 400
        response = client.post("/api/webapp/economy/quote", json={}, headers=headers)
        assert (response.status_code, response.headers["Retry-After"]) == (429, "1")
        assert client.post("/api/webapp/roster", json={}, headers=headers).status_code == 200
        Clock.now += timedelta(seconds=1)
        assert client.post("/api/webapp/economy/quote", json={}, headers=headers).status_code == 400
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


@pytest.fixture
def miniapp_orders(tmp_path, monkeypatch):
    import admin
    import database
    from models import Player, UserRoster, GlobalPlayerMarket
    from services import config_service
    from config import MAX_ROSTER

    engine = create_engine(f"sqlite:///{tmp_path / 'economy.sqlite'}", connect_args={"timeout": 30})
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        account = User(telegram_id=7, total_coins=100_000_000, total_gems=40)
        other = User(telegram_id=8, total_coins=300)
        db.add_all([account, other, Pack(slot_number=1, name="Test Pack", cost_coins=100,
                                       main_count=1, main_min_rating=78, main_max_rating=78, bonus_count=0, is_active=True)])
        db.flush()
        for i in range(min(12, MAX_ROSTER) + 3):
            card = Player(name=f"Test player {i}", rating=78, category="Batsman", country="India",
                          bat_hand="Right", bowl_hand="Right", bowl_style="Pace", is_active=True)
            db.add(card)
            db.flush()
            if i < 12:
                db.add(UserRoster(user_id=account.id, player_id=card.id, order_position=i + 1))
            if i == 12:
                db.add(GlobalPlayerMarket(slot_index=1, player_id=card.id, base_price=10_000,
                                          final_price=10_000, quantity=1, is_active=True))
        account.roster_count = 12
        db.commit()
    monkeypatch.setattr(admin, "get_session", sessions)
    monkeypatch.setattr(admin, "post_miniapp_activity", lambda *args, **kwargs: None)
    monkeypatch.setattr(config_service, "get_config", lambda *args, **kwargs: {})
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setenv("ADSGRAM_POSTBACK_SECRET", "t" * 32)

    def post(path, body, telegram_id=7):
        headers = {"Authorization": "tma " + signed({
            "user": '{"id":%s}' % telegram_id, "auth_date": str(int(time.time())),
        })}
        with admin.app.test_client() as client:
            response = client.post("/api/webapp/" + path, json=body, headers=headers)
        return response.status_code, response.get_json()

    yield post, sessions, admin
    engine.dispose()


def test_100_market_confirms_return_one_debit_and_card(miniapp_orders):
    from models import ActivityLog, GlobalPlayerMarket, MarketPurchase, UserRoster

    post, sessions, _ = miniapp_orders
    code, quote = post("economy/quote", {"action": "market", "target": 1})
    assert code == 200, quote
    assert post("market/buy/1", {})[1]["error"] == "quote_required"
    assert post("economy/status", {"quote_id": quote["quote_id"]}, 8)[0] == 404
    assert post("economy/confirm", {"quote_id": quote["quote_id"], "price": 1})[0] == 400
    with ThreadPoolExecutor(max_workers=16) as workers:
        results = list(workers.map(
            lambda _: post("economy/confirm", {"quote_id": quote["quote_id"]}), range(100)))
    assert all(code == 200 and receipt == results[0][1] for code, receipt in results), results[:3]
    assert post("economy/status", {"quote_id": quote["quote_id"]}) == results[0]
    with sessions() as db:
        assert db.query(UserRoster).count() == 13
        assert db.query(MarketPurchase).count() == 1
        assert db.query(GlobalPlayerMarket).one().purchased_count == 1
        assert db.query(ActivityLog).filter_by(action="market_buy").count() == 1
        assert db.query(User).filter_by(telegram_id=7).one().total_coins == quote["balance_after"]


def test_squad_sale_recovers_original_receipt(miniapp_orders):
    from models import UserRoster

    post, sessions, _ = miniapp_orders
    assert post("economy/quote", {"action": "sell", "target": [1]})[0] == 409
    assert post("roster/12/lock", {"locked": True})[0] == 200
    assert post("economy/quote", {"action": "sell", "target": [12]})[0] == 409
    assert post("roster/12/lock", {"locked": False})[0] == 200
    _, sale = post("economy/quote", {"action": "sell", "target": [12, 12]})
    assert len(sale["cards"]) == 1
    assert post("roster/12/lock", {"locked": True})[0] == 200
    assert post("economy/confirm", {"quote_id": sale["quote_id"]})[0] == 409
    assert post("roster/12/lock", {"locked": False})[0] == 200
    first_sale = post("economy/confirm", {"quote_id": sale["quote_id"]})
    assert first_sale[0] == 200
    assert post("economy/confirm", {"quote_id": sale["quote_id"]}) == first_sale
    with sessions() as db:
        assert db.query(UserRoster).count() == 11


def test_pack_open_recovers_original_reveal(miniapp_orders):
    from models import PackOpening, UserRoster

    post, sessions, _ = miniapp_orders
    _, quote = post("economy/quote", {"action": "pack", "target": [1, "coins"]})
    code, purchase = post("economy/confirm", {"quote_id": quote["quote_id"]})
    assert code == 200, purchase
    _, opening = post("economy/quote", {"action": "open_pack", "target": purchase["inventory_id"]})
    receipt = post("economy/confirm", {"quote_id": opening["quote_id"]})
    assert receipt[0] == 200, receipt
    assert post("economy/confirm", {"quote_id": opening["quote_id"]}) == receipt
    assert post("packs/history", {})[1]["history"][0]["cards"][0]["version"] == "Base"
    with sessions() as db:
        assert db.query(PackOpening).count() == 1
        assert db.query(UnopenedPack).count() == 0
        assert db.query(UserRoster).count() == 13


def test_unavailable_and_replayed_ad_callbacks_do_not_create_extra_rewards(miniapp_orders):
    from models import AdReward, MiniappRewardReceipt

    post, sessions, admin = miniapp_orders
    assert post("spin", {"request_id": "initial"})[1]["slot_type"] == "free"
    assert post("ad-unavailable", {"kind": "spin"})[0] == 409
    assert post("ad-completed", {"view_id": "a" * 32})[1]["ads_saved"] == 0
    assert post("spin", {"request_id": "reward"})[1]["error"] == "ad_required"
    with admin.app.test_client() as client:
        for _ in range(2):
            assert client.get("/api/ads/reward", query_string={
                "userid": 7, "event_id": "adsgram-reference", "secret": "t" * 32,
            }).status_code == 200
    first = post("spin", {"request_id": "reward"})[1]
    assert first["slot_type"] == "ad"
    assert post("spin", {"request_id": "reward"})[1] == dict(first, replayed=True)
    with sessions() as db:
        assert db.query(AdReward).count() == 1
        assert db.query(MiniappRewardReceipt).count() == 2


def test_adsgram_callback_survives_a_quest_tracking_rollback(miniapp_orders, monkeypatch):
    from models import AdReward
    from services import quest_service

    post, sessions, admin = miniapp_orders
    assert post("spin", {"request_id": "initial"})[1]["slot_type"] == "free"
    assert post("ad-completed", {"view_id": "a" * 32})[1]["confirmed"] is False

    def rollback_tracking(session, user_id, event, count):
        if event == "ad_watched":
            session.rollback()

    monkeypatch.setattr(quest_service, "safe_track", rollback_tracking)
    with admin.app.test_client() as client:
        params = {"userid": 7, "secret": "t" * 32}
        response = client.get("/api/ads/reward", query_string=params)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert client.get("/api/adsgram/reward", query_string={
            "telegramId": 7, "secret": "t" * 32,
        }).status_code == 200

    assert post("ad-completed", {"view_id": "a" * 32})[1]["ads_saved"] == 1
    result = post("spin", {"request_id": "watched-ad"})[1]
    assert result["slot_type"] == "ad"
    assert post("spin", {"request_id": "watched-ad"})[1] == dict(result, replayed=True)
    with sessions() as db:
        assert db.query(AdReward).one().consumed_at is not None


@pytest.mark.parametrize(("action", "callback"), [
    ("spin", None), ("spin", "missing"), ("spin", "before"), ("spin", "after"),
    ("daily", None), ("freepack/open", None),
])
def test_sdk_ad_rewards_work_with_the_existing_reward_block(miniapp_orders, monkeypatch,
                                                           action, callback):
    from models import AdReward
    from services import config_service, free_pack_service, quest_service

    post, sessions, admin = miniapp_orders
    monkeypatch.setenv("ADSGRAM_BLOCK_ID", "1234")
    monkeypatch.delenv("ADSGRAM_REQUIRE_POSTBACK", raising=False)
    if not callback:
        monkeypatch.delenv("ADSGRAM_POSTBACK_SECRET", raising=False)
    receives_callback = callback in ("before", "after")
    if action == "daily":
        monkeypatch.setattr(config_service, "get_config", lambda *a, **kw: config_service.DEFAULTS)
    if action == "freepack/open":
        monkeypatch.setattr(free_pack_service, "get_bands",
                            lambda session: [{"min": 78, "max": 78, "weight": 1}])
    else:
        assert post(action, {"request_id": "free"})[1]["slot_type"] == "free"
    assert post(action, {"request_id": "sdk-ad"})[1]["error"] == "ad_required"
    assert post("ad-completed", {})[1]["ads_saved"] == 0

    tracked = []

    def rollback_tracking(session, user_id, event, count):
        if event == "ad_watched":
            tracked.append(user_id)
            session.rollback()

    monkeypatch.setattr(quest_service, "safe_track", rollback_tracking)
    with admin.app.test_client() as client:
        assert client.get("/api/ads/config").json["ads"]["block_id"] == "1234"
        callback_args = {"userid": 7, "secret": "t" * 32, "event_id": "partner-ref"}
        if callback == "before":
            assert client.get("/api/ads/reward", query_string=callback_args).status_code == 200
        view = {"view_id": "c" * 32}
        assert post("ad-completed", view)[1]["ads_saved"] == 1
        assert post("ad-completed", view)[1]["ads_saved"] == 1
        assert post("ad-completed", {"view_id": "d" * 32})[1]["ads_saved"] == 1
        assert post("ad-completed", {}, telegram_id=8)[1]["ads_saved"] == 0
        status, result = post(action, {"request_id": "sdk-ad"})
        assert status == 200 and result["ok"]
        assert post(action, {"request_id": "sdk-ad"})[1] == dict(result, replayed=True)
        if receives_callback:
            # A callback can arrive after the SDK credit has already been spent.
            assert client.get("/api/ads/reward", query_string=callback_args).status_code == 200
            assert client.get("/api/adsgram/reward", query_string=callback_args).status_code == 200
        assert post("ad-completed", view)[1]["ads_saved"] == 0
    assert len(tracked) == 1
    with sessions() as db:
        receipt = db.query(AdReward).one()
        assert receipt.view_id == view["view_id"]
        assert receipt.provider == ("adsgram" if receives_callback else "client")
        assert receipt.consumed_at is not None


def test_finalized_match_pays_once_and_applies_ai_daily_cap(miniapp_orders, monkeypatch):
    from models import Match, MatchRewardSettlement
    from services import config_service
    from services.match_rewards import award_match_rewards_core

    _, sessions, _ = miniapp_orders
    monkeypatch.setattr(config_service, "get_config", lambda *args, **kwargs: {
        **config_service.DEFAULTS, "ai_match_reward_daily_cap": 1,
    })
    with sessions() as db:
        human = db.query(User).filter_by(telegram_id=7).one()
        ai = User(telegram_id=-1)
        db.add(ai)
        db.flush()
        start = human.total_coins
        for status in ("completed", "completed", "playing"):
            match = Match(user1_id=human.id, user2_id=ai.id, winner_id=human.id,
                          loser_id=ai.id, status=status, overs=5, end_reason="completed")
            db.add(match)
            db.flush()
            first = award_match_rewards_core(db, human.id, ai.id, 5, is_vsbot=True, match_id=match.id)
            assert award_match_rewards_core(db, human.id, ai.id, 5, is_vsbot=True, match_id=match.id) == first
        db.commit()
        assert human.total_coins - start == 5 * config_service.DEFAULTS["match_win_coins_per_over"]
        assert {row.eligibility for row in db.query(MatchRewardSettlement)} == {"eligible", "ai_daily_cap"}
        assert human.matches_played == 2


def test_eligibility_and_repeat_tournament_registration(miniapp_orders):
    from datetime import datetime, timedelta
    from models import Player, Tournament, TournamentTeam

    post, sessions, _ = miniapp_orders
    with sessions() as db:
        tournament = Tournament(name="Registration Cup", kind="letsplay", status="scheduled",
                                registration_closes_at=datetime.utcnow() + timedelta(hours=1))
        db.add(tournament)
        for player in db.query(Player).order_by(Player.id).limit(11):
            player.category = (["Batsman"] * 4 + ["Wicket Keeper"] + ["All-rounder"] * 2 +
                               ["Bowler"] * 4)[player.id - 1]
        db.commit()
        tournament_id = tournament.id
    assert post("tournament/join", {"tournament_id": tournament_id}, 8)[0] == 409
    first = post("tournament/join", {"tournament_id": tournament_id})
    assert first[0] == 200, first
    assert post("tournament/join", {"tournament_id": tournament_id}) == first
    with sessions() as db:
        assert db.query(TournamentTeam).filter_by(tournament_id=tournament_id).count() == 1
