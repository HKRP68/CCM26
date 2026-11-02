"""A shared database must not leave two bots accepting bids or running clocks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop

from services import auction_bot_handoff as H


def event(text='', *, chat='private', callback=None, caption=None):
    message=SimpleNamespace(text=text,caption=caption,reply_text=AsyncMock())
    return SimpleNamespace(effective_message=message,effective_chat=SimpleNamespace(type=chat),
                           callback_query=SimpleNamespace(data=callback,answer=AsyncMock()) if callback else None)


def ctx():
    return SimpleNamespace(application=SimpleNamespace(bot_data={}),bot=SimpleNamespace(username='OriginalBot'))


@pytest.fixture
def handed_off(monkeypatch):
    monkeypatch.setenv('AUCTION_BOT_ENABLED','false')
    monkeypatch.setenv('FRANCHISE_AUCTION_BOT_USERNAME','NewAuctionBot')


@pytest.mark.parametrize('text',['/bid 1.5','/retain 12','/apick 23','/apublish','/atradeapprove 2'])
def test_original_bot_blocks_all_workflow_mutations_after_handoff(handed_off,text):
    update=event(text)
    with pytest.raises(ApplicationHandlerStop):asyncio.run(H.auction_handoff_guard(update,ctx()))
    assert '@NewAuctionBot' in update.effective_message.reply_text.call_args.args[0]


def test_original_group_bot_does_not_answer_each_bid_a_second_time(handed_off):
    for text in ('/bid 1.5','.bid 1.5'):
        update=event(text,chat='supergroup')
        with pytest.raises(ApplicationHandlerStop):asyncio.run(H.auction_handoff_guard(update,ctx()))
        update.effective_message.reply_text.assert_not_awaited()
    update=event('/bid@OriginalBot 1.5',chat='supergroup')
    with pytest.raises(ApplicationHandlerStop):asyncio.run(H.auction_handoff_guard(update,ctx()))
    update.effective_message.reply_text.assert_awaited_once()


def test_old_retention_buttons_and_media_commands_cannot_mutate_shared_records(handed_off):
    update=event(callback='au_rtn_accept_7')
    with pytest.raises(ApplicationHandlerStop):asyncio.run(H.auction_handoff_guard(update,ctx()))
    assert update.callback_query.answer.call_args.kwargs['show_alert']
    update=event(caption='/acall hello')
    with pytest.raises(ApplicationHandlerStop):asyncio.run(H.auction_handoff_guard(update,ctx()))


def test_other_games_and_commands_addressed_to_the_new_bot_are_untouched(handed_off):
    for update in [event('/claim'),event('/bid@NewAuctionBot 1.5',chat='supergroup'),
                   event(callback='claim_1'),event('.'),event('.unrelated')]:
        asyncio.run(H.auction_handoff_guard(update,ctx()))
        update.effective_message.reply_text.assert_not_awaited()


def test_existing_bot_keeps_auctions_until_explicitly_handed_off(monkeypatch):
    monkeypatch.delenv('AUCTION_BOT_ENABLED',raising=False)
    update=event('/apublish')
    asyncio.run(H.auction_handoff_guard(update,ctx()))
    update.effective_message.reply_text.assert_not_awaited()


def test_only_dedicated_bot_starts_a_clock_after_handoff(handed_off):
    from services.auction_scheduler import start_auction_scheduler,SWEEP_JOB_NAME
    jobs=[]
    queue=SimpleNamespace(get_jobs_by_name=lambda name:jobs,
                          run_repeating=lambda callback,**kw:jobs.append((callback,kw)))
    app=SimpleNamespace(bot_data={},job_queue=queue)
    start_auction_scheduler(app)
    assert not jobs
    app.bot_data['franchise_dedicated']=True
    start_auction_scheduler(app);start_auction_scheduler(app)
    assert len(jobs)==1 and jobs[0][1]['name']==SWEEP_JOB_NAME
