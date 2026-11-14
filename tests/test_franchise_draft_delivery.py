"""Draft event retries, final-pick delivery, board recovery and clock isolation."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from telegram.error import BadRequest, Forbidden


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def room(tmp_path, monkeypatch):
    from database import Base
    import models
    from services import auction_service as A, franchise_draft as D
    from services import franchise_draft_delivery as DD, franchise_draft_rich as DR

    engine = create_engine('sqlite:///' + str(tmp_path / 'delivery.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    db = sessions()
    season = A.create_season(db, 'Rotating draft', min_squad_size=1, max_squad_size=1)
    A.bind_chat(db, season, -10077222)
    draft = models.FranchiseDraft(season_id=season.id, rounds=1, cursor=0, revision=1)
    db.add(draft)
    db.commit()
    text = '<b>Draft status</b>\nRemaining purse: ₹10 Cr'
    monkeypatch.setattr(DR, 'board', lambda *_args: (DR.blocks_for_html(text), text))
    counter = iter(range(100, 1000))

    async def send(**_kwargs):
        return SimpleNamespace(message_id=next(counter))

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send),
                          edit_message_text=AsyncMock(),
                          pin_chat_message=AsyncMock(),
                          unpin_chat_message=AsyncMock())
    yield SimpleNamespace(db=db, engine=engine, sessions=sessions, season=season,
                          draft=draft, bot=bot, A=A, D=D, DD=DD)
    db.close()
    engine.dispose()


def event(room, kind='fd_picked', text=None):
    row = room.A.log_event(room.db, room.season, kind, 'Draft update',
                           detail={'html': text or '<b>Joe Root signed</b>\nNext: <a href="tg://user?id=42">Team 2 owner</a>'})
    room.db.commit()
    return row


def test_rich_html_content_preserves_price_formatting_and_next_owner_link():
    from services.franchise_draft_delivery import blocks_for_html
    blocks = blocks_for_html('<b>Root &amp; Co.</b> · <code>₹25.5 Cr</code>\n'
                             '<a href="tg://user?id=123">Next owner</a>')
    assert blocks[0]['type'] == 'heading'
    nodes = blocks[0]['text']
    assert {'type': 'bold', 'text': 'Root & Co.'} in nodes
    assert {'type': 'code', 'text': '₹25.5 Cr'} in nodes
    assert {'type': 'url', 'text': 'Next owner', 'url': 'tg://user?id=123'} in blocks[1]['text']
    json.dumps(blocks)


def test_announcements_are_ordered_and_non_draft_setup_is_silent(room):
    event(room, kind='franchise_added')
    first = event(room, text='<b>Pick 1</b> · ₹25.5 Cr')
    second = event(room, text='<b>Pick 2</b> · ₹23 Cr')
    assert run(room.DD.drain_events(room.bot, room.db, room.season, limit=1)) == 1
    assert room.season.announced_event_id == first.id
    assert room.bot.send_message.call_args.kwargs['text'] == '<b>Pick 1</b> · ₹25.5 Cr'
    assert run(room.DD.drain_events(room.bot, room.db, room.season, limit=1)) == 1
    assert room.season.announced_event_id == second.id
    assert room.bot.send_message.call_count == 2


def test_bulk_pick_audits_are_silent_and_completion_summary_is_delivered(room):
    for i in range(4):
        room.A.log_event(room.db,room.season,'fd_picked',f'Batch player {i}',detail={'html':f'<b>Player {i}</b>','silent':True})
    summary=room.A.log_event(room.db,room.season,'fd_autofill_completed','Auto draft completed',detail={'html':'<b>All squads complete</b>'})
    room.db.commit()
    assert run(room.DD.drain_events(room.bot,room.db,room.season))==1
    assert room.bot.send_message.call_count==1
    assert room.season.announced_event_id==summary.id
    assert room.bot.send_message.call_args.kwargs['text']=='<b>All squads complete</b>'
    assert run(room.DD.drain_events(room.bot,room.db,room.season))==0


def test_failed_send_retains_cursor_and_retries_without_losing_next_pick(room):
    row = event(room)
    room.bot.send_message.side_effect = Forbidden('Bot temporarily cannot send')
    assert run(room.DD.drain_events(room.bot, room.db, room.season)) == 0
    assert int(room.season.announced_event_id or 0) == 0
    room.bot.send_message.side_effect = None
    room.bot.send_message.return_value = SimpleNamespace(message_id=777)
    assert run(room.DD.drain_events(room.bot, room.db, room.season)) == 1
    assert room.season.announced_event_id == row.id
    assert run(room.DD.drain_events(room.bot, room.db, room.season)) == 0


def test_completed_final_pick_is_delivered_after_restart_and_only_once(room):
    from models import AuctionSeason
    row = event(room, text='<b>Final pick confirmed. Draft complete.</b>')
    room.season.status = room.A.STATUS_COMPLETED
    room.db.commit()
    for _ in range(2):
        with room.sessions() as resumed:
            season = resumed.get(AuctionSeason, room.season.id)
            run(room.DD.sweep(SimpleNamespace(bot=room.bot), resumed))
            assert season.announced_event_id == row.id
    texts = [call.kwargs['text'] for call in room.bot.send_message.call_args_list]
    assert texts.count('<b>Final pick confirmed. Draft complete.</b>') == 1
    assert texts.count('<b>Draft status</b>\nRemaining purse: ₹10 Cr') == 1


def test_deleted_pinned_board_is_replaced_and_revision_is_persisted(room):
    room.season.board_message_id = 5
    room.season.board_rendered_bid_count = 0
    room.db.commit()
    room.bot.edit_message_text.side_effect = BadRequest('Message to edit not found')
    result = run(room.DD.refresh_board(room.bot, room.db, room.season))
    room.db.commit()
    assert result == room.season.board_message_id == 100
    assert room.season.board_rendered_bid_count == room.draft.revision
    room.bot.unpin_chat_message.assert_awaited_once_with(chat_id=room.season.chat_id, message_id=5)
    room.bot.pin_chat_message.assert_awaited_once()


def test_stale_board_failure_is_retried_on_a_later_tick(room):
    room.bot.send_message.side_effect = Forbidden('Read-only')
    run(room.DD.tick(room.bot, room.db, room.season))
    assert room.season.board_message_id is None
    room.bot.send_message.side_effect = None
    room.bot.send_message.return_value = SimpleNamespace(message_id=888)
    run(room.DD.tick(room.bot, room.db, room.season))
    assert room.season.board_message_id == 888


def test_live_draft_sweep_never_opens_auction_lots_or_hammer_countdown(room, monkeypatch):
    import database
    from services import auction_scheduler as S

    room.season.status = room.A.STATUS_LIVE
    room.db.commit()
    monkeypatch.setattr(database, 'get_session', room.sessions)
    open_lot = AsyncMock(side_effect=AssertionError('Draft cannot open auction lot'))
    monkeypatch.setattr(room.A, 'open_next_lot', open_lot)
    monkeypatch.setattr(S, '_trade_tick', AsyncMock())
    monkeypatch.setattr(S, 'maybe_start_countdown', AsyncMock(side_effect=AssertionError('No countdown')))
    run(S._auction_tick(SimpleNamespace(bot=room.bot)))
    open_lot.assert_not_called()
    S.maybe_start_countdown.assert_not_called()
    assert any('Draft status' in call.kwargs['text'] for call in room.bot.send_message.call_args_list)
