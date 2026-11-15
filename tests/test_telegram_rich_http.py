"""Website notifications preserve compact text without automatic rich wrappers."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import telegram_rich_http as H


def response(**body):
    return SimpleNamespace(json=lambda: body)


def test_website_text_keeps_html_privacy_and_controls_without_native_probe():
    payload = dict(chat_id=42, text="<b>Match won</b>\nBalance: 100", parse_mode="HTML",
                   reply_markup={"inline_keyboard": [[{"text": "Details", "callback_data": "details"}]]},
                   ephemeral_message_parameters={"callback_query_id": "private"}, protect_content=True)
    post = Mock(return_value=response(ok=True))
    H.post_message("token", payload, timeout=7, post=post)
    post.assert_called_once_with("https://api.telegram.org/bottoken/sendMessage", json=payload, timeout=7)
    assert "rich_message" not in post.call_args.kwargs["json"]


@pytest.mark.parametrize("code", [400, 404, 429, 500])
def test_errors_do_not_repeat_notifications(code):
    post = Mock(return_value=response(ok=False, error_code=code))
    H.post_message("token", {"chat_id": 42, "text": "Next over"}, post=post)
    assert post.call_count == 1


def test_explicit_rich_payloads_still_use_the_native_endpoint():
    payload = dict(chat_id=42, rich_message={"blocks": [{"type": "heading", "size": 2, "text": "Match"}]})
    post = Mock(return_value=response(ok=True))
    H.post_message("token", payload, post=post)
    assert post.call_args.args[0].endswith("/sendRichMessage")


def test_news_notifications_keep_the_original_formatted_text(monkeypatch):
    import requests
    from services import news_announce
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    post = Mock(return_value=response(ok=True, result={"message_id": 1}))
    monkeypatch.setattr(requests, "post", post)
    assert news_announce._send(42, "<b>News approved</b>")["ok"]
    assert post.call_args.args[0].endswith("/sendMessage")
    assert post.call_args.kwargs["json"]["text"] == "<b>News approved</b>"
