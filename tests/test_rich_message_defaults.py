"""Real PTB send/reply/edit paths use native rich delivery throughout the bot."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import (
    Bot,
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    Update,
    User,
)
from telegram.error import BadRequest, InvalidToken
from telegram.ext import Defaults, ExtBot

from services import button_access as B, rich_message as R
from services import rich_message_defaults as D
from services.rich_message_compat import adapt_message

TOKEN = "123456:" + "a" * 35


@pytest.fixture
def delivery(monkeypatch):
    calls = []

    async def wire(self, endpoint, data, **kwargs):
        calls.append((endpoint, deepcopy(data), kwargs))
        if getattr(wire, "fail_native", False) and "rich_message" in data:
            raise BadRequest("Invalid rich content")
        if getattr(wire, "missing", False) and "rich_message" in data:
            raise InvalidToken("Unknown method sendRichMessage")
        message = {"message_id": 77, "date": 1, "chat": {"id": 42, "type": "private"}}
        if "rich_message" in data:
            rich = data["rich_message"]
            message["rich_message"] = {
                "blocks": rich.get("blocks", [R.paragraph("Rendered HTML")])
            }
        elif endpoint == "sendPhoto":
            message["photo"] = [
                {
                    "file_id": "cached-photo",
                    "file_unique_id": "photo-1",
                    "width": 100,
                    "height": 100,
                }
            ]
            message["caption"] = data.get("caption")
        else:
            message["text"] = data.get("text", "")
        if data.get("reply_markup"):
            markup = data["reply_markup"]
            message["reply_markup"] = (
                markup.to_dict() if hasattr(markup, "to_dict") else markup
            )
        return message

    monkeypatch.setattr(Bot, "_do_post", wire)
    monkeypatch.setattr(Bot, "_post", Bot._post)
    monkeypatch.setattr(Message, "de_json", Message.__dict__["de_json"])
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", True)
    R.reset_support_latch()
    D.install_rich_defaults()
    yield SimpleNamespace(calls=calls, wire=wire)
    R.reset_support_latch()
    B.clear_button_owner_context()
    B._OWNER_BY_MESSAGE.clear()


@pytest.mark.parametrize("client_type", [Bot, ExtBot])
def test_direct_sends_keep_html_buttons_topics_and_reply_context(delivery, client_type):
    client = client_type(TOKEN)
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open", callback_data="profile_open")]]
    )
    message = asyncio.run(
        client.send_message(
            42,
            "<b>Your squad</b>\n<b>Joe Root</b>",
            parse_mode="HTML",
            reply_markup=markup,
            message_thread_id=123,
            reply_to_message_id=9,
            protect_content=True,
            disable_notification=True,
            read_timeout=12,
        )
    )
    endpoint, data, options = delivery.calls[0]
    assert endpoint == "sendRichMessage"
    assert data["rich_message"] == {"html": "<b>Your squad</b>\n<b>Joe Root</b>"}
    assert data["reply_markup"] == markup and data["reply_parameters"].message_id == 9
    assert (
        data["message_thread_id"] == 123
        and data["protect_content"]
        and data["disable_notification"]
    )
    assert options["read_timeout"] == 12
    assert "parse_mode" not in data and "text" not in data
    assert message.message_id == 77


def test_plain_text_never_interprets_player_names_as_html(delivery):
    asyncio.run(Bot(TOKEN).send_message(42, "Root <Captain> & Co"))
    assert delivery.calls[0][1]["rich_message"] == {
        "blocks": [R.paragraph("Root <Captain> & Co")]
    }


def test_extbot_defaults_are_resolved_before_native_rendering(delivery):
    asyncio.run(
        ExtBot(TOKEN, defaults=Defaults(parse_mode="HTML")).send_message(
            42, "<b>Daily reward</b>"
        )
    )
    assert delivery.calls[0][1]["rich_message"] == {"html": "<b>Daily reward</b>"}


def test_native_requests_still_use_sdk_rate_limiter(delivery):
    from telegram.ext import BaseRateLimiter

    seen = []

    class Limiter(BaseRateLimiter):
        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def process_request(
            self, callback, args, kwargs, endpoint, data, rate_limit_args
        ):
            seen.append((endpoint, rate_limit_args))
            return await callback(*args, **kwargs)

    client = ExtBot(TOKEN, rate_limiter=Limiter())
    asyncio.run(client.send_message(42, "Next over", rate_limit_args={"priority": 1}))
    assert seen == [("sendRichMessage", {"priority": 1})]
    assert len(delivery.calls) == 1


def test_compatibility_bypass_is_local_to_one_async_task(delivery):
    client = Bot(TOKEN)
    ready, sent = asyncio.Event(), asyncio.Event()

    async def legacy():
        with R.legacy_delivery():
            ready.set()
            await sent.wait()
            await client.send_message(42, "Legacy fallback")

    async def native():
        await ready.wait()
        await client.send_message(42, "Live match")
        sent.set()

    async def scenario():
        await asyncio.gather(legacy(), native())

    asyncio.run(scenario())
    assert [call[0] for call in delivery.calls] == ["sendRichMessage", "sendMessage"]


def test_over_summary_keeps_old_html_keyboard_and_tracking_with_rich_enabled(delivery):
    from handlers import cipl_play

    client = Bot(TOKEN)
    state = {"chat_id": 42}
    text = "<b>End of Over 6</b> — Quick\nTimeline: 1 0 4 W 1 0\n🏏 Kings 49/1"
    keyboard = [[InlineKeyboardButton("Impact Player", callback_data="impact_42")]]

    async def scenario():
        await cipl_play._post_tracked(SimpleNamespace(bot=client), state, text,
                                     keyboard=keyboard, legacy=True)
        await client.send_message(42, "Next prompt")

    asyncio.run(scenario())
    assert [call[0] for call in delivery.calls] == ["sendMessage", "sendRichMessage"]
    payload = delivery.calls[0][1]
    assert payload["text"] == text and payload["parse_mode"] == "HTML"
    assert "rich_message" not in payload
    assert payload["reply_markup"].inline_keyboard[0][0].callback_data == "impact_42"
    assert state["over_msg_ids"] == [77]


def test_explicit_entities_preserve_utf16_links_and_bold(delivery):
    asyncio.run(
        Bot(TOKEN).send_message(42, "🏏 Root", entities=[MessageEntity("bold", 3, 4)])
    )
    assert delivery.calls[0][1]["rich_message"] == {"html": "🏏 <b>Root</b>"}


def test_message_reply_and_callback_edit_use_the_same_delivery_layer(delivery):
    client = Bot(TOKEN)
    original = Message(10, datetime.now(timezone.utc), Chat(42, "private"), text="Menu")
    original.set_bot(client)
    query = CallbackQuery(
        "query", User(12, "Owner", False), "instance", message=original, data="next"
    )
    query.set_bot(client)

    async def scenario():
        await original.reply_text("<b>Choose your bowler</b>", parse_mode="HTML")
        await query.edit_message_text("<b>Next over</b>", parse_mode="HTML")

    asyncio.run(scenario())
    assert [call[0] for call in delivery.calls] == [
        "sendRichMessage",
        "editMessageText",
    ]
    assert delivery.calls[1][1]["rich_message"] == {"html": "<b>Next over</b>"}
    assert delivery.calls[1][1]["message_id"] == 10


def test_inline_message_edits_preserve_inline_id(delivery):
    asyncio.run(Bot(TOKEN).edit_message_text("Ready", inline_message_id="inline-123"))
    assert delivery.calls[0][1]["inline_message_id"] == "inline-123"
    assert delivery.calls[0][1]["rich_message"] == {"blocks": [R.paragraph("Ready")]}


def test_missing_endpoint_falls_back_once_and_keeps_private_parameters(delivery):
    delivery.wire.missing = True
    client = Bot(TOKEN)

    async def scenario():
        for _ in range(2):
            await client.send_message(
                42,
                "<b>Private</b>",
                parse_mode="HTML",
                api_kwargs={
                    "ephemeral_message_parameters": {"callback_query_id": "secret"}
                },
            )

    asyncio.run(scenario())
    assert [call[0] for call in delivery.calls] == [
        "sendRichMessage",
        "sendMessage",
        "sendMessage",
    ]
    assert all(
        call[1]["ephemeral_message_parameters"] == {"callback_query_id": "secret"}
        for call in delivery.calls
    )
    assert delivery.calls[-1][1]["parse_mode"] == "HTML"


def test_explicit_rich_helpers_do_not_promote_failed_fallbacks_again(delivery):
    delivery.wire.fail_native = True
    message = asyncio.run(
        R.send_rich_message(Bot(TOKEN), 42, [R.heading("Claim")], "<b>Claim</b>")
    )
    assert message.message_id == 77
    assert [call[0] for call in delivery.calls] == [
        "sendRichMessage",
        "sendRichMessage",
        "sendMessage",
    ]


def test_personal_buttons_remain_guarded_when_sending_natively(delivery):
    B.bind_button_owner_context(SimpleNamespace(effective_user=SimpleNamespace(id=11)))
    client = Bot(TOKEN)
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open", callback_data="personal_profile")]]
    )
    asyncio.run(
        R.send_rich_message(
            client, 42, [R.paragraph("Profile")], "Profile", reply_markup=markup
        )
    )
    assert B.get_registered_owner(42, 77) == 11
    query = SimpleNamespace(
        data="personal_profile",
        from_user=SimpleNamespace(id=22),
        message=SimpleNamespace(chat_id=42, message_id=77),
    )
    assert not B.check_callback_owner(SimpleNamespace(callback_query=query))


def test_shared_match_buttons_stay_available_to_both_captains(delivery):
    B.bind_button_owner_context(SimpleNamespace(effective_user=SimpleNamespace(id=11)))
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Play", callback_data="cipl_batapp_attack")]]
    )
    asyncio.run(Bot(TOKEN).send_message(42, "Next over", reply_markup=markup))
    assert B.get_registered_owner(42, 77) is None


def test_media_captions_and_keyboard_removal_keep_their_original_protocol(delivery):
    async def scenario():
        client = Bot(TOKEN)
        image = await client.send_photo(
            42, "file-id", caption="<b>Player card</b>", parse_mode="HTML"
        )
        await client.edit_message_reply_markup(42, image.message_id, reply_markup=None)
        return image

    assert asyncio.run(scenario()).photo[0].file_id == "cached-photo"
    assert [call[0] for call in delivery.calls] == [
        "sendPhoto",
        "editMessageReplyMarkup",
    ]
    assert delivery.calls[0][1]["caption"] == "<b>Player card</b>"


def test_feature_switch_keeps_original_formatted_delivery(delivery, monkeypatch):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", False)
    asyncio.run(Bot(TOKEN).send_message(42, "<b>Ready</b>", parse_mode="HTML"))
    assert (
        delivery.calls[0][0] == "sendMessage"
        and delivery.calls[0][1]["parse_mode"] == "HTML"
    )


def test_incoming_rich_callbacks_keep_text_html_and_mentions_on_old_sdk(delivery):
    native = {
        "message_id": 15,
        "date": 1,
        "chat": {"id": 42, "type": "private"},
        "rich_message": {
            "blocks": [
                R.heading("🏏 Match"),
                R.paragraph(["Captain: ", R.mention("Root", 7)]),
                R.table([[R.cell("Score"), R.cell(R.bold("125/3"))]]),
                R.details("Bench", [R.paragraph("Stokes")]),
            ]
        },
    }
    saved = deepcopy(native)
    message = Message.de_json(native, Bot(TOKEN))
    assert native == saved
    assert "125/3" in message.text and "Bench\nStokes" in message.text
    assert "<b>🏏 Match</b>" in message.text_html
    assert '<a href="tg://user?id=7">Root</a>' in message.text_html
    assert message.api_kwargs["rich_message"] == native["rich_message"]
    event = Update.de_json(
        {
            "update_id": 1,
            "callback_query": {
                "id": "q",
                "from": {"id": 7, "first_name": "Root", "is_bot": False},
                "chat_instance": "instance",
                "data": "refresh",
                "message": native,
            },
        },
        Bot(TOKEN),
    )
    assert event.callback_query.message.text_html == message.text_html


def test_adaptation_leaves_legacy_messages_unchanged():
    message = {"text": "Original", "rich_message": {"blocks": [R.paragraph("Other")]}}
    assert adapt_message(message) is message


def test_initial_claim_has_rich_details_and_removable_controls(delivery, monkeypatch):
    from handlers import claim
    from services import card_sender

    p = dict(
        id=1,
        name="Joe <Root>",
        rating=95,
        bat_rating=96,
        bowl_rating=50,
        country="England",
        category="Batsman",
        version="Prime",
        bat_hand="Right",
        bowl_style="Off Spin",
    )
    picture = AsyncMock(return_value=SimpleNamespace(message_id=66))
    monkeypatch.setattr(card_sender, "send_player_card", picture)
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Retain", callback_data="retain_1_2_3")]]
    )
    blocks = claim._claim_blocks(p, 15, 0, "@Owner", 12)
    message = asyncio.run(
        claim._send_claim_card(
            bot=Bot(TOKEN),
            chat_id=42,
            player=SimpleNamespace(**p),
            p=p,
            text="<b>Joe &lt;Root&gt;</b>",
            blocks=blocks,
            keyboard=markup,
            reply_to_message_id=5,
            session=None,
        )
    )
    assert picture.call_args.kwargs['reply_markup']==markup
    assert picture.call_args.kwargs['rich_blocks']==blocks
    assert picture.call_args.kwargs['caption']=='<b>Joe &lt;Root&gt;</b>'
    assert message.message_id==66
    assert not delivery.calls  # No second decision message after the image.
