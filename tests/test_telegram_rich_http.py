"""Website/news text takes the native path without losing delivery semantics."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import rich_message as R, telegram_rich_http as H


def response(**body):
    return SimpleNamespace(json=lambda: body)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", True)
    H._unavailable.clear()
    yield
    H._unavailable.clear()


def test_website_message_keeps_rich_html_privacy_and_controls():
    payload = {
        "chat_id": 42,
        "text": "<b>Match won</b>",
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[{"text": "Details", "callback_data": "details"}]]
        },
        "ephemeral_message_parameters": {"callback_query_id": "private"},
        "protect_content": True,
    }
    original = dict(payload)
    result = response(ok=True)
    post = Mock(return_value=result)
    assert H.post_message("test-token", payload, timeout=7, post=post) is result
    assert post.call_args.args[0].endswith("/sendRichMessage")
    sent = post.call_args.kwargs["json"]
    assert sent["rich_message"] == {"html": "<b>Match won</b>"}
    assert sent["reply_markup"] == payload["reply_markup"]
    assert sent["ephemeral_message_parameters"] == {"callback_query_id": "private"}
    assert sent["protect_content"] and payload == original
    assert post.call_args.kwargs["timeout"] == 7


def test_missing_method_is_cached_per_token_then_recovers(monkeypatch):
    instant = [100.0]
    monkeypatch.setattr(H.time, "monotonic", lambda: instant[0])
    unavailable = response(ok=False, error_code=404, description="Unknown method")
    post = Mock(
        side_effect=[
            unavailable,
            response(ok=True),
            response(ok=True),
            response(ok=True),
            response(ok=True),
        ]
    )
    payload = {"chat_id": 42, "text": "Root <Captain>"}
    H.post_message("old-token", payload, post=post)
    H.post_message("old-token", payload, post=post)
    H.post_message("new-token", payload, post=post)
    instant[0] += R.UNSUPPORTED_RETRY_SECONDS + 1
    H.post_message("old-token", payload, post=post)
    assert [call.args[0].rsplit("/", 1)[-1] for call in post.call_args_list] == [
        "sendRichMessage",
        "sendMessage",
        "sendMessage",
        "sendRichMessage",
        "sendRichMessage",
    ]
    assert post.call_args.kwargs["json"]["rich_message"] == {
        "blocks": [R.paragraph("Root <Captain>")]
    }


def test_rejected_native_html_falls_back_with_original_formatting():
    payload = {"chat_id": 42, "text": "<b>Ready</b>", "parse_mode": "HTML"}
    post = Mock(
        side_effect=[
            response(ok=False, error_code=400, description="Unsupported rich content"),
            response(ok=True),
        ]
    )
    H.post_message("token", payload, post=post)
    assert post.call_count == 2
    assert post.call_args.args[0].endswith("/sendMessage")
    assert post.call_args.kwargs["json"] == payload


@pytest.mark.parametrize("code", [429, 500])
def test_flood_and_server_errors_do_not_trigger_an_extra_send(code):
    post = Mock(return_value=response(ok=False, error_code=code))
    H.post_message("token", {"chat_id": 42, "text": "Next over"}, post=post)
    assert post.call_count == 1


def test_news_notifications_use_the_shared_rich_path(monkeypatch):
    import requests
    from services import news_announce

    monkeypatch.setenv("BOT_TOKEN", "test-token")
    post = Mock(return_value=response(ok=True, result={"message_id": 1}))
    monkeypatch.setattr(requests, "post", post)
    assert news_announce._send(42, "<b>News approved</b>")["ok"]
    assert post.call_args.args[0].endswith("/sendRichMessage")
    assert post.call_args.kwargs["json"]["rich_message"] == {
        "html": "<b>News approved</b>"
    }
