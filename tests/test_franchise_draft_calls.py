"""Draft calls mirror auction broadcasts with draft-specific scope and authority."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden

from test_franchise_draft_handlers import room, ADMIN, OWNER, COOWNER, OTHER, MANAGER, CHAT, update  # noqa: F401


def call(room, *, actor=ADMIN, text='Draft in 5 minutes', chat=CHAT, caption=None, photo=None, reply_to=None, bot=None):
    event=update(actor=actor,text='/dcall '+text,chat=chat)
    event.effective_message.caption=caption
    event.effective_message.photo=photo
    event.effective_message.reply_to_message=reply_to
    bot=bot or SimpleNamespace(send_message=AsyncMock(),send_photo=AsyncMock(),send_document=AsyncMock(),username='DraftTestBot')
    ctx=SimpleNamespace(args=text.split(),bot=bot,user_data={})
    asyncio.run(room.H.dcall_handler(event,ctx))
    return bot,event.effective_message.replies


def test_delegated_draft_admin_can_call_unique_contacts_without_auction_permission(room):
    delegate=884055
    room.D.add_draft_admin(room.db,delegate,by_tg_id=ADMIN)
    room.two.co_owner_ids_json=json.dumps([OWNER,COOWNER])
    room.db.commit()
    assert not room.A.is_auction_admin(room.db,delegate)
    bot,replies=call(room,actor=delegate,text='Please join <now>')
    calls=bot.send_message.call_args_list
    assert {c.kwargs['chat_id'] for c in calls}=={CHAT,OWNER,COOWNER,OTHER,MANAGER}
    assert len(calls)==5
    group=next(c.kwargs['text'] for c in calls if c.kwargs['chat_id']==CHAT)
    assert 'Please join &lt;now&gt;' in group
    for uid in (OWNER,COOWNER,OTHER,MANAGER):assert f'tg://user?id={uid}' in group
    assert '4 delivered, 0 unavailable' in replies[-1][0]


@pytest.mark.parametrize('actor',[OWNER,COOWNER,MANAGER,884056])
def test_only_draft_admins_can_broadcast(room,actor):
    if actor==884056:
        room.A.add_auction_admin(room.db,actor,by_tg_id=ADMIN);room.db.commit()
    bot,replies=call(room,actor=actor)
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    assert 'Only Draft Admins' in replies[-1][0]


@pytest.mark.parametrize('chat',[1,-100998877])
def test_call_requires_the_bound_draft_group(room,chat):
    bot,replies=call(room,chat=chat)
    bot.send_message.assert_not_awaited()
    assert '/dbind' in replies[-1][0]


def test_call_rejects_an_auction_group(room):
    season=room.A.create_season(room.db,'Separate auction')
    room.A.bind_chat(room.db,season,-100998877);room.db.commit()
    bot,replies=call(room,chat=-100998877)
    bot.send_message.assert_not_awaited()
    assert 'Use /acall' in replies[-1][0]


def test_caption_alias_uses_full_size_image_and_custom_text(room):
    bot,_=call(room,text='',caption='/dcallteams@DraftTestBot Join the draft',
        photo=[SimpleNamespace(file_id='small'),SimpleNamespace(file_id='large')])
    assert bot.send_photo.await_count==5
    for item in bot.send_photo.call_args_list:
        assert item.kwargs['photo']=='large' and 'Join the draft' in item.kwargs['caption']
    bot.send_message.assert_not_awaited()


def test_caption_for_another_bot_is_ignored(room):
    bot,replies=call(room,caption='/dcall@AnotherBot Ignore this',photo=[SimpleNamespace(file_id='image')])
    bot.send_photo.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert not replies


@pytest.mark.parametrize('text,expected',[('Custom reminder','Custom reminder'),('','Original caption')])
def test_replied_image_document_and_caption_are_preserved(room,text,expected):
    source=SimpleNamespace(caption='Original caption',document=SimpleNamespace(file_id='image-doc',mime_type='image/png'))
    bot,_=call(room,text=text,reply_to=source)
    assert bot.send_document.await_count==5
    assert all(c.kwargs['document']=='image-doc' and expected in c.kwargs['caption'] for c in bot.send_document.call_args_list)


def test_blocked_dm_does_not_stop_the_other_contacts(room):
    async def send(**kwargs):
        if kwargs['chat_id']==OWNER:raise Forbidden('Bot blocked')
    bot=SimpleNamespace(send_message=AsyncMock(side_effect=send))
    bot,replies=call(room,bot=bot)
    assert bot.send_message.await_count==5
    assert '3 delivered, 1 unavailable' in replies[-1][0]


def test_long_image_call_uses_draft_caption_and_preserves_all_text(room):
    bot,_=call(room,text='Long & message '*400,photo=[SimpleNamespace(file_id='image')])
    assert bot.send_photo.await_count==5
    assert all(c.kwargs['caption']=='📣 Draft announcement' for c in bot.send_photo.call_args_list)
    group=''.join(c.kwargs['text'] for c in bot.send_message.call_args_list if c.kwargs['chat_id']==CHAT)
    assert group.count('&amp;')==400
    assert f'tg://user?id={OWNER}' in group
