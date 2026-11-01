"""Real Telegram browser login and shared-auction authorization boundaries."""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

TOKEN = "123456789:" + "A" * 35
OWNER, OTHER, ADMIN, DELEGATE = 981001, 981002, 981099, 981098


def widget(user_id=OWNER, **extra):
    data = {"id": user_id, "first_name": "Owner", "username": "owner_test",
            "auth_date": int(time.time()), **extra}
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    data["hash"] = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return data


def miniapp(user_id=OWNER):
    data = {"user": json.dumps({"id": user_id, "first_name": "Owner"}),
            "auth_date": str(int(time.time()))}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(data)


@pytest.fixture
def site(tmp_path, monkeypatch):
    from database import Base
    import database
    import models
    import website
    from services import admin_ids, auction_service as A, config_service

    engine = create_engine("sqlite:///" + str(tmp_path / "website.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "get_session", sessions)
    cfg = {"maintenance_bypass_ids": "", "is_maintenance": False, "rookie_mode": False}
    monkeypatch.setattr(config_service, "get_config", lambda session=None: cfg)
    monkeypatch.setattr(admin_ids, "get_config", lambda: cfg)
    for key in (*admin_ids.ADMIN_ID_ENV_VARS, *admin_ids.OWNER_ID_ENV_VARS):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("BOT_ADMIN_IDS", str(ADMIN))
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(ok=True, json=lambda: {"ok": False}))
    app = website.create_app({"TESTING": True, "WEBSITE_SECRET": "website-tests-secret-" + "x" * 32,
                              "BOT_TOKEN": TOKEN, "BOT_USERNAME": "CCMTestBot",
                              "SESSION_COOKIE_SECURE": False})
    db = sessions()
    season = A.create_season(db, "Website test auction", min_squad_size=0,
                              max_squad_size=5, max_overseas=5, auto_accelerated=0)
    A.bind_chat(db, season, -100981000)
    team = A.create_franchise(db, season, "Owners", owner_tg_id=OWNER)
    other = A.create_franchise(db, season, "Others", owner_tg_id=OTHER)
    player = models.Player(name="Website Player", rating=85, category="Batsman", country="India",
                           version="Base", bat_hand="Right", bowl_hand="Right",
                           bowl_style="Medium Pacer", bat_rating=85, bowl_rating=80, is_active=True)
    db.add(player)
    db.flush()
    A.add_players_to_pool(db, season, [player], set_name="Main")
    db.commit()
    yield SimpleNamespace(app=app, client=app.test_client(), db=db, A=A, cfg=cfg,
                          sessions=sessions, season=season, owner=team, other=other)
    db.close()
    engine.dispose()


def login(site, user_id=OWNER):
    before = site.client.get("/api/franchise/auth/session").get_json()["csrf_token"]
    response = site.client.post("/api/franchise/auth/login", json=widget(user_id),
                                headers={"X-CSRF-Token": before})
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["csrf_token"] != before
    return result["csrf_token"]


def auction(site, token=None, suffix="", **body):
    return site.client.post("/api/webapp/auction" + suffix,
                            json={"season_id": site.season.id, "view": "arena", **body},
                            headers={"X-CSRF-Token": token} if token is not None else {})


@pytest.mark.parametrize("change", [
    {"hash": "0" * 64}, {"auth_date": int(time.time()) - 301},
    {"auth_date": int(time.time()) + 60}, {"auth_date": "tomorrow"},
    {"id": 0}, {"id": -1}, {"id": True}, {"id": "981001.0"},
])
def test_widget_rejects_forged_stale_future_and_invalid_ids(change):
    from services.website_auth import verify_widget_login
    payload = widget(**{key: value for key, value in change.items() if key != "hash"})
    payload.update({key: value for key, value in change.items() if key == "hash"})
    assert verify_widget_login(payload, TOKEN) is None


def test_widget_uses_telegram_widget_key_and_positive_id():
    from services.website_auth import verify_widget_login
    assert verify_widget_login(widget(), TOKEN) == {"id": OWNER, "first_name": "Owner", "username": "owner_test"}
    assert verify_widget_login(widget(), "your_bot_token_here") is None
    assert verify_widget_login(widget(), "") is None
    payload = widget()
    payload["id"] = OTHER
    assert verify_widget_login(payload, TOKEN) is None


def test_login_requires_prelogin_csrf_and_rotates_cookie_token(site):
    response = site.client.post("/api/franchise/auth/login", json=widget())
    assert response.status_code == 403
    initial = site.client.get("/api/franchise/auth/session").get_json()["csrf_token"]
    token = login(site)
    assert auction(site, initial).status_code == 403
    assert auction(site, token).status_code == 200
    session_response = site.client.get("/api/franchise/auth/session")
    assert session_response.headers["Cache-Control"] == "no-store"
    assert session_response.get_json()["user"]["id"] == OWNER
    with site.client.session_transaction() as state:
        assert "admin" not in state


def test_every_cookie_auction_endpoint_requires_csrf_and_unicode_is_denied(site):
    token = login(site)
    for suffix in ("", "/action", "/catalogue", "/export"):
        for invalid in (None, "wrong", "é" * len(token)):
            response = auction(site, invalid, suffix=suffix, action="admin_pause")
            assert response.status_code == 403, (suffix, response.get_json())
            assert response.get_json()["error"] == "csrf"


def test_invalid_explicit_authorization_cannot_fall_back_to_cookie(site, monkeypatch):
    token = login(site)
    monkeypatch.setenv("WEBAPP_DEV_MODE", "1")
    for identity in ("", "bad", f"tma DEV_{ADMIN}"):
        response = site.client.post("/api/webapp/auction", json={"season_id": site.season.id},
                                    headers={"Authorization": identity, "X-CSRF-Token": token})
        assert response.status_code == 401
    valid = site.client.post("/api/webapp/auction", json={"season_id": site.season.id},
                             headers={"Authorization": miniapp()})
    assert valid.status_code == 200


def test_owner_can_bid_only_for_own_team_and_cannot_manage(site):
    token = login(site)
    response = auction(site, token)
    assert response.get_json()["role"] == "team"
    assert response.get_json()["my_team_ids"] == [site.owner.id]
    assert auction(site, token, suffix="/action", action="admin_pause", admin=True).status_code == 403
    site.A.start(site.db, site.season)
    site.db.commit()
    lot = auction(site, token).get_json()["lot"]
    context = {"lot_id": lot["id"], "stage": lot["rtm_stage"], "expected_bids": lot["bids"]}
    denied = auction(site, token, suffix="/action", action="bid", team_id=site.other.id,
                     amount_lakh=lot["next_lakh"], **context)
    assert denied.status_code == 403
    accepted = auction(site, token, suffix="/action", action="bid", team_id=site.owner.id,
                       amount_lakh=lot["next_lakh"], **context)
    assert accepted.status_code == 200, accepted.get_json()


def test_manager_permissions_are_checked_again_after_revocation(site):
    site.A.add_auction_admin(site.db, DELEGATE, by_tg_id=ADMIN)
    site.db.commit()
    token = login(site, DELEGATE)
    assert auction(site, token, view="admin").get_json()["role"] == "manager"
    site.A.remove_auction_admin(site.db, DELEGATE, by_tg_id=ADMIN)
    site.db.commit()
    response = auction(site, token, suffix="/action", action="admin_pause", bot_admin=True)
    assert response.status_code == 403


def test_nonmember_cannot_open_auction_and_telegram_login_cannot_open_admin(site):
    token = login(site, 981004)
    assert auction(site, token).status_code == 403
    for path in ("/login", "/players", "/auctions", "/api/webapp/init"):
        assert site.client.get(path).status_code == 404
    with site.client.session_transaction() as state:
        state["admin"] = True
    assert auction(site, token, suffix="/action", action="admin_create", name="Forbidden").status_code == 403


def test_logout_requires_csrf_and_invalidates_browser_identity(site):
    token = login(site)
    assert site.client.post("/api/franchise/auth/logout").status_code == 403
    response = site.client.post("/api/franchise/auth/logout", headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert response.get_json()["user"] is None
    assert response.get_json()["csrf_token"] != token
    assert auction(site, token).status_code == 401


def test_browser_expiry_and_missing_credentials_fail_closed(site):
    token = login(site)
    with site.client.session_transaction() as state:
        state["website_authenticated_at"] = time.time() - 86401
    assert auction(site, token).status_code == 401
    for key, value in (("BOT_TOKEN", ""), ("BOT_TOKEN", "placeholder"), ("WEBSITE_SECRET", "")):
        site.app.config[key] = value
        csrf = site.client.get("/api/franchise/auth/session").get_json()["csrf_token"]
        response = site.client.post("/api/franchise/auth/login", json=widget(), headers={"X-CSRF-Token": csrf})
        assert response.status_code == 503


def test_banned_accounts_and_maintenance_are_enforced(site):
    from models import User
    token = login(site)
    user = User(telegram_id=OWNER, first_name="Owner", is_banned=True)
    site.db.add(user)
    site.db.commit()
    assert auction(site, token).get_json()["error"] == "banned"
    user.is_banned = False
    site.db.commit()
    site.cfg["is_maintenance"] = True
    assert auction(site, token).get_json()["error"] == "maintenance"
    site.cfg["maintenance_bypass_ids"] = str(OWNER)
    response = auction(site, token)
    assert response.status_code == 200
    assert response.get_json()["role"] == "team"


def test_group_spectator_can_watch_but_cannot_bid(site, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: SimpleNamespace(
        ok=True, json=lambda: {"ok": True, "result": {"status": "member"}}))
    token = login(site, 981004)
    response = auction(site, token)
    assert response.status_code == 200
    assert response.get_json()["role"] == "spectator"
    assert response.get_json()["permissions"] == {"manage": False, "manage_admins": False, "bid": False}
    assert auction(site, token, suffix="/action", action="bid", team_id=site.owner.id,
                    amount_lakh=100).status_code == 403


def test_owner_revocation_takes_effect_without_logging_out(site):
    token = login(site)
    assert auction(site, token).status_code == 200
    site.owner.owner_tg_id = OTHER
    site.db.commit()
    assert auction(site, token).status_code == 403


def test_rookie_membership_gate_is_preserved(site):
    token = login(site)
    site.cfg["rookie_mode"] = True
    response = auction(site, token)
    assert response.status_code == 403
    assert response.get_json()["error"] == "rookie_required"


def test_manager_can_create_without_existing_season_and_cookie_is_hardened(site):
    site.app.config["SESSION_COOKIE_SECURE"] = True
    token = login(site, ADMIN)
    response = site.client.post("/api/webapp/auction/action", headers={"X-CSRF-Token": token},
                                json={"action": "admin_create", "name": "Website-created season"})
    assert response.status_code == 200, response.get_json()
    assert site.db.query(__import__("models").AuctionSeason).filter_by(name="Website-created season").count() == 1
    site.client.post("/api/franchise/auth/logout", headers={"X-CSRF-Token": token})
    # The session bootstrap has already issued the cookie; inspect a fresh
    # browser to see all flags rather than relying on an unchanged response.
    cookie = site.app.test_client().get("/api/franchise/auth/session").headers["Set-Cookie"]
    assert "ccm26_franchise_session=" in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Lax" in cookie
