"""Bans apply immediately to commands, silent updates and Mini App APIs."""

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram.ext import ApplicationHandlerStop

from services.ban_guard import check_ban

TG_ID = 987601
TOKEN = "123456789:" + "A" * 35
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def account(tmp_path, monkeypatch):
    import database
    from models import User
    engine = create_engine(f"sqlite:///{tmp_path / 'ban.db'}")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "get_session", factory)
    with factory() as db:
        user = User(telegram_id=TG_ID, first_name="Player", is_banned=True,
                    ban_reason="Repeated <abuse>", needs_debut=True)
        db.add(user)
        db.commit()
        user_id = user.id
    yield NS(factory=factory, user_id=user_id)
    engine.dispose()


def update(text=None, *, callback=False, caption=None):
    message = NS(text=text, caption=caption, entities=[], reply_text=AsyncMock())
    return NS(effective_user=NS(id=TG_ID),
              message=None if callback else message,
              effective_message=message,
              callback_query=NS(data="au_bid_1", answer=AsyncMock()) if callback else None)


@pytest.mark.parametrize("text", ["/start", "/claim", "/unknown", "/help@GameBot"])
def test_banned_commands_only_receive_ban_notice(account, text):
    event = update(text)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(check_ban(event, NS(bot=NS(username="GameBot"))))
    event.message.reply_text.assert_awaited_once()
    assert "Repeated &lt;abuse&gt;" in event.message.reply_text.await_args.args[0]


def test_caption_command_receives_only_ban_notice(account):
    event = update(caption="/acall player")
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(check_ban(event, NS(bot=NS(username="GameBot"))))
    event.message.reply_text.assert_awaited_once()


@pytest.mark.parametrize("event", [
    lambda: update("hello"), lambda: update(".bid 2"),
    lambda: update("/claim@OtherBot"), lambda: update(),
    lambda: update(caption="photo"), lambda: update(caption="/acall@OtherBot player"),
    lambda: update(callback=True),
])
def test_banned_non_commands_and_buttons_are_completely_silent(account, event):
    event = event()
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(check_ban(event, NS(bot=NS(username="GameBot"))))
    event.effective_message.reply_text.assert_not_awaited()
    if event.callback_query:
        event.callback_query.answer.assert_not_awaited()


def test_ban_and_unban_from_another_process_take_effect_on_next_update(account):
    from models import User
    event, context = update("/claim"), NS(bot=NS(username="GameBot"))
    for banned in (False, True, False):
        with account.factory() as db:
            db.get(User, account.user_id).is_banned = banned
            db.commit()
        if banned:
            with pytest.raises(ApplicationHandlerStop):
                asyncio.run(check_ban(event, context))
        else:
            assert asyncio.run(check_ban(event, context)) is True  # reset gate still applies


def test_lookup_failure_stops_handlers_silently(monkeypatch):
    import database
    monkeypatch.setattr(database, "get_session", Mock(side_effect=RuntimeError("offline")))
    event = update("/claim")
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(check_ban(event, NS(bot=NS(username="GameBot"))))
    event.message.reply_text.assert_not_awaited()


def test_main_and_auction_bans_run_before_every_other_guard(monkeypatch):
    source = (ROOT / "bot.py").read_text()
    groups = {name: int(group) for name, group in re.findall(
        r"TypeHandler\(_TGUpdate,\s*(\w+)\),\s*group=(-?\d+)", source)}
    for name in ("button_access_guard", "_bind_reply_context", "_track_chat",
                 "_maintenance_check", "_rookie_check"):
        assert groups["_ban_check"] < groups[name]
    from services import auction_bot_handoff, button_access
    from services.franchise_bot_registry import register_auction_handlers
    monkeypatch.setattr(button_access, "install_button_access_defaults", lambda: None)
    app = NS(handlers=[])
    app.add_handler = lambda handler, group=0: app.handlers.append((group, handler))
    auction_bot_handoff.register_main_auction_handoff(app)
    assert groups["_ban_check"] < min(group for group, _ in app.handlers)
    app.handlers.clear()
    register_auction_handlers(app)
    first = min(app.handlers, key=lambda item: item[0])[1]
    assert first.callback is check_ban


def signed_header():
    data = {"user": json.dumps({"id": TG_ID}), "auth_date": str(int(time.time()))}
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": "tma " + urlencode(data)}


@pytest.fixture
def miniapp(account, monkeypatch):
    import admin
    from services import config_service
    monkeypatch.setattr(admin, "get_session", account.factory)
    monkeypatch.setattr(config_service, "get_config", lambda *a, **kw: {})
    monkeypatch.setenv("BOT_TOKEN", TOKEN)
    monkeypatch.delenv("WEBAPP_DEV_MODE", raising=False)
    return admin


@pytest.mark.parametrize("path", [
    "/api/webapp/init", "/api/webapp/daily", "/api/webapp/spin",
    "/api/webapp/match/deliver", "/api/webapp/match/abandon",
    "/api/webapp/auction", "/api/fantasy/save", "/api/ipl16/share",
])
def test_signed_miniapp_requests_cannot_access_features(miniapp, path):
    response = miniapp.app.test_client().post(path, json={}, headers=signed_header())
    assert response.status_code == 403
    assert response.get_json()["error"] == "banned"


def test_ban_wins_over_reset_onboarding_and_membership_bypasses(miniapp, monkeypatch):
    from services import config_service
    monkeypatch.setattr(config_service, "get_config", lambda *a, **kw: {
        "is_maintenance": True, "rookie_mode": True, "maintenance_bypass_ids": str(TG_ID)})
    for allow in (False, True):
        with miniapp.app.test_request_context("/api/webapp/init", method="POST", headers=signed_header()):
            auth, _, error = miniapp._webapp_auth(allow_not_debuted=allow)
        assert auth is None
        assert error[1] == 403 and error[0]["error"] == "banned"


def test_ban_is_rechecked_after_miniapp_is_already_open(miniapp, account):
    from models import User
    headers = signed_header()
    for banned in (False, True, False):
        with account.factory() as db:
            db.get(User, account.user_id).is_banned = banned
            db.commit()
        with miniapp.app.test_request_context("/api/webapp/init", headers=headers):
            error = miniapp._block_banned_miniapp_user()
        if banned:
            assert error[0]["error"] == "banned"
        else:
            assert error is None


@pytest.mark.parametrize("path", ["/api/match/state", "/api/match/action", "/api/match/autoplay-status"])
def test_legacy_match_aliases_require_signed_header(miniapp, account, path):
    client = miniapp.app.test_client()
    if path.endswith("state"):
        response = client.get(path, query_string={"userId": account.user_id})
    else:
        response = client.post(path, json={"userId": account.user_id})
    assert response.status_code == 401
    assert response.get_json()["error"] == "bad_init_data"


def test_legacy_poll_cannot_bypass_ban(miniapp, monkeypatch):
    # These imports normally load live-match engines; ensure a denied poll
    # returns before looking up or advancing a match.
    from services import crickidex_arena
    lookup = Mock(side_effect=AssertionError("must not look up a match"))
    monkeypatch.setattr(crickidex_arena, "find_active_match", lookup)
    response = miniapp.app.test_client().get("/api/match", query_string={"userId": "spectator"},
                                            headers=signed_header())
    assert response.status_code == 403
    assert response.get_json()["error"] == "banned"
    lookup.assert_not_called()


@pytest.mark.parametrize("path", ["/api/match", "/api/match/state", "/api/match/action"])
def test_banned_signed_sender_cannot_impersonate_another_player(miniapp, path):
    client = miniapp.app.test_client()
    if path.endswith("action"):
        response = client.post(path, json={"userId": 999999}, headers=signed_header())
    else:
        response = client.get(path, query_string={"userId": 999999}, headers=signed_header())
    assert response.status_code == 403
    assert response.get_json()["error"] == "banned"


def test_match_actions_use_verified_sender_instead_of_supplied_user_id(miniapp, account):
    from models import User
    with account.factory() as db:
        db.get(User, account.user_id).is_banned = False
        other = User(telegram_id=TG_ID + 1, first_name="Other")
        db.add(other)
        db.commit()
        other_id = other.id
    with miniapp.app.test_request_context("/api/match/action", method="POST", headers=signed_header()):
        assert miniapp._block_banned_miniapp_user() is None
        with account.factory() as db:
            user, _, error = miniapp._match_rest_user_and_match(db, other_id)
            assert user.id == account.user_id
            assert error[0]["error"] == "no_match"


def test_forged_match_authentication_is_rejected(miniapp):
    response = miniapp.app.test_client().get("/api/match", query_string={"userId": TG_ID},
        headers={"Authorization": "tma user=forged&hash=" + "0" * 64})
    assert response.status_code == 401
    assert response.get_json()["error"] == "bad_init_data"
