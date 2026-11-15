"""A claim is one media message, and terminal decisions edit that same card."""

import asyncio
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest
from telegram.request import RequestData
from telegram.request._requestparameter import RequestParameter

from services import rich_media as M, rich_message as R
from services.rich_message_compat import install_message_compat

PHOTO = {
    "type": "photo",
    "photo": [
        {
            "file_id": "saved-photo",
            "file_unique_id": "photo-id",
            "width": 100,
            "height": 150,
        }
    ],
}


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setattr(R.config, "RICH_TEXT_ENABLED", True)
    monkeypatch.setattr(Message, "de_json", Message.__dict__["de_json"])
    install_message_compat()
    R.reset_support_latch()
    result = {
        "message_id": 77,
        "date": 1,
        "chat": {"id": 42, "type": "private"},
        "rich_message": {"blocks": [PHOTO, R.paragraph("Claim")]},
    }
    client = SimpleNamespace(
        _post=AsyncMock(return_value=result),
        send_message=AsyncMock(),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=78)),
        edit_message_text=AsyncMock(),
        edit_message_caption=AsyncMock(),
    )
    yield client
    R.reset_support_latch()


def test_rich_photo_upload_has_real_multipart_data_and_keeps_stream_reusable(bot):
    image = BytesIO(b"jpeg-test-bytes")
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Retain", callback_data="retain_1_2_3")]]
    )
    message = asyncio.run(
        M.send(bot, 42, image, [R.heading("Joe Root")], reply_markup=markup)
    )
    endpoint, payload = bot._post.call_args.args
    assert endpoint == "sendRichMessage" and message.message_id == 77
    wire = RequestData(
        [RequestParameter.from_input(key, value) for key, value in payload.items()]
    )
    rich = json.loads(wire.json_parameters["rich_message"])
    assert rich["blocks"][0] == {
        "type": "photo",
        "photo": {"type": "photo", "media": "attach://cmu_rich_media"},
    }
    assert "cmu_rich_media" in wire.multipart_data
    assert image.tell() == 0 and image.read() == b"jpeg-test-bytes"
    assert payload["reply_markup"] == markup
    assert message.photo[-1].file_id == "saved-photo"
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_native_claim_outcome_preserves_photo_and_edits_only_its_message(bot):
    original = SimpleNamespace(
        api_kwargs={"rich_message": {"blocks": [PHOTO]}}, photo=(), message_id=77
    )
    assert asyncio.run(
        M.edit(
            bot, 42, 77, [R.heading("Retained")], "<b>Retained</b>", original=original
        )
    )
    method, payload = bot._post.call_args.args
    assert method == "editMessageText" and payload["message_id"] == 77
    assert payload["rich_message"]["blocks"][0]["photo"]["media"] == "saved-photo"
    assert payload["reply_markup"] == {"inline_keyboard": []}
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()


def test_failed_media_edit_does_not_drop_the_image_or_send_another_message(bot):
    bot._post.side_effect = BadRequest("Cannot edit this message")
    original = SimpleNamespace(
        api_kwargs={"rich_message": {"blocks": [PHOTO]}}, photo=()
    )
    assert not asyncio.run(
        M.edit(
            bot, 42, 77, [R.heading("Released")], "<b>Released</b>", original=original
        )
    )
    assert bot._post.await_count == 1
    bot.send_message.assert_not_awaited()
    bot.edit_message_text.assert_not_awaited()
    bot.edit_message_caption.assert_not_awaited()


def test_legacy_claim_photo_is_updated_by_caption_without_a_new_message(bot):
    original = SimpleNamespace(
        api_kwargs={}, photo=[SimpleNamespace(file_id="old-card")]
    )
    assert asyncio.run(
        M.edit(
            bot, 42, 11, [R.heading("Released")], "<b>Released</b>", original=original
        )
    )
    assert bot.edit_message_caption.call_args.kwargs["message_id"] == 11
    assert bot.edit_message_caption.call_args.kwargs["caption"] == "<b>Released</b>"
    bot._post.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_card_sender_embeds_a_cached_photo_in_one_native_claim(bot, monkeypatch):
    from services import card_sender, player_image_service

    monkeypatch.setattr(
        player_image_service, "get_tg_file_id", lambda *args: "cached-id"
    )
    message = asyncio.run(
        card_sender.send_player_card(
            bot=bot,
            chat_id=42,
            player=SimpleNamespace(id=1),
            caption="<b>Joe Root</b>",
            rich_blocks=[R.heading("Joe Root")],
            session=object(),
        )
    )
    assert message.message_id == 77 and bot._post.await_count == 1
    assert (
        bot._post.call_args.args[1]["rich_message"]["blocks"][0]["photo"]["media"]
        == "cached-id"
    )
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()


def test_unsupported_native_photo_sends_only_one_legacy_card(bot, monkeypatch):
    from services import card_sender, player_image_service

    monkeypatch.setattr(
        player_image_service, "get_tg_file_id", lambda *args: "cached-id"
    )
    bot._post.side_effect = BadRequest("Unknown method sendRichMessage")
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Retain", callback_data="retain_1_2_3")]]
    )
    message = asyncio.run(
        card_sender.send_player_card(
            bot=bot,
            chat_id=42,
            player=SimpleNamespace(id=1),
            caption="<b>Joe Root</b>",
            rich_blocks=[R.heading("Joe Root")],
            session=object(),
            reply_markup=markup,
        )
    )
    assert message.message_id == 78 and bot.send_photo.await_count == 1
    assert bot.send_photo.call_args.kwargs["reply_markup"] == markup
    bot.send_message.assert_not_awaited()


@pytest.fixture
def claim_room(bot, monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base
    import models
    from handlers import claim
    from services import achievement_service, quest_service

    monkeypatch.setattr(achievement_service, "check_and_notify", AsyncMock())
    monkeypatch.setattr(quest_service, "safe_track", lambda *args: None)
    engine = create_engine("sqlite:///" + str(tmp_path / "claims.db"))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    owner = models.User(
        telegram_id=111, first_name="Owner", total_coins=100, roster_count=0
    )
    player = models.Player(
        name="Joe Root",
        rating=95,
        category="Batsman",
        country="England",
        bat_hand="Right",
        bowl_hand="Right",
        bowl_style="Off Spin",
        bat_rating=96,
        bowl_rating=50,
    )
    db.add_all([owner, player])
    db.commit()
    guard = set()

    def done(key):
        duplicate = key in guard
        guard.add(key)
        return duplicate

    monkeypatch.setattr(claim, "_claim_done", done)
    monkeypatch.setattr(claim, "release", lambda key: guard.discard(key))
    monkeypatch.setattr(claim, "get_session", Session)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=111, username="owner", first_name="Owner"),
        message=SimpleNamespace(
            chat_id=42,
            message_id=77,
            api_kwargs={"rich_message": {"blocks": [PHOTO]}},
            photo=(),
        ),
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    context = SimpleNamespace(bot=bot, job_queue=None)
    yield SimpleNamespace(
        db=db,
        models=models,
        claim=claim,
        owner=owner,
        player=player,
        query=query,
        context=context,
        guard=guard,
        Session=Session,
    )
    db.close()
    engine.dispose()


@pytest.mark.parametrize("action", ["retain", "release"])
def test_claim_confirmation_edits_same_card_and_never_pays_twice(
    claim_room, bot, action
):
    room = claim_room
    room.query.data = f"{action}_{room.player.id}_{room.owner.id}_500"
    handler = getattr(room.claim, action + "_callback")
    event = SimpleNamespace(callback_query=room.query)
    asyncio.run(handler(event, room.context))
    asyncio.run(handler(event, room.context))
    room.db.expire_all()
    user = room.db.get(room.models.User, room.owner.id)
    assert user.roster_count == (1 if action == "retain" else 0)
    assert user.total_coins == (100 if action == "retain" else 600)
    assert bot._post.await_count == 1
    assert bot._post.call_args.args[1]["message_id"] == 77
    bot.send_message.assert_not_awaited()


def test_stranger_cannot_consume_or_change_another_owners_claim(claim_room, bot):
    room = claim_room
    room.query.data = f"retain_{room.player.id}_{room.owner.id}_500"
    room.query.from_user.id = 222
    asyncio.run(
        room.claim.retain_callback(
            SimpleNamespace(callback_query=room.query), room.context
        )
    )
    assert not room.guard
    room.query.edit_message_reply_markup.assert_not_awaited()
    bot._post.assert_not_awaited()


def test_timeout_edits_the_original_photo_card(claim_room, bot):
    room = claim_room
    room.context.job = SimpleNamespace(
        data={
            "player_id": room.player.id,
            "user_id": room.owner.id,
            "sell_val": 500,
            "chat_id": 42,
            "message_id": 77,
            "media": {
                "type": "photo",
                "photo": {"type": "photo", "media": "saved-photo"},
            },
        }
    )
    asyncio.run(room.claim._auto_decide(room.context))
    assert bot._post.call_args.args[1]["message_id"] == 77
    assert (
        bot._post.call_args.args[1]["rich_message"]["blocks"][0]["photo"]["media"]
        == "saved-photo"
    )
    bot.send_message.assert_not_awaited()


def test_full_squad_keeps_claim_controls_and_does_not_consume_the_turn(claim_room, bot):
    room = claim_room
    room.owner.roster_count = room.claim.MAX_ROSTER
    room.db.commit()
    room.query.data = f"retain_{room.player.id}_{room.owner.id}_500"
    asyncio.run(
        room.claim.retain_callback(
            SimpleNamespace(callback_query=room.query), room.context
        )
    )
    assert not room.guard
    room.query.edit_message_reply_markup.assert_not_awaited()
    bot._post.assert_not_awaited()
    assert "Replace" in room.query.answer.call_args.args[0]


def test_replacement_checks_owner_before_consuming_claim_and_edits_same_photo(
    claim_room, bot, monkeypatch
):
    from services import trait_service

    monkeypatch.setattr(trait_service, "return_traits_to_inventory", lambda *args: 0)
    room = claim_room
    old = room.models.Player(
        name="Old player",
        rating=80,
        category="Batsman",
        country="England",
        bat_hand="Right",
        bowl_hand="Right",
        bowl_style="Fast",
    )
    room.db.add(old)
    room.db.flush()
    entry = room.models.UserRoster(
        user_id=room.owner.id, player_id=old.id, order_position=1
    )
    room.db.add(entry)
    room.owner.roster_count = 1
    room.db.commit()
    room.query.data = f"replc_{room.player.id}_{entry.id}_{room.owner.id}"
    room.query.from_user.id = 222
    event = SimpleNamespace(callback_query=room.query)
    asyncio.run(room.claim.replace_confirm_callback(event, room.context))
    assert not room.guard
    room.query.edit_message_reply_markup.assert_not_awaited()
    room.query.from_user.id = 111
    asyncio.run(room.claim.replace_confirm_callback(event, room.context))
    room.db.expire_all()
    assert room.db.get(room.models.UserRoster, entry.id).player_id == room.player.id
    assert bot._post.call_args.args[1]["message_id"] == 77
    bot.send_message.assert_not_awaited()
