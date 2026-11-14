"""Optional native formatting cannot delay normal rich HTML delivery indefinitely."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from services import rich_message as R


@pytest.mark.parametrize('edit',[False,True])
def test_stalled_native_request_is_cancelled_and_backed_off(monkeypatch,edit):
    R.reset_support_latch()
    monkeypatch.setattr(R.config,'RICH_TEXT_ENABLED',True)
    monkeypatch.setattr(R,'NATIVE_TIMEOUT_SECONDS',0.01)
    cancelled=[]
    async def stalled(*args):
        try:await asyncio.Event().wait()
        finally:cancelled.append(True)
    bot=SimpleNamespace(_post=AsyncMock(side_effect=stalled),send_message=AsyncMock(return_value='html'))
    async def scenario():
        if edit:
            assert not await R.edit_rich_message(bot,42,7,[R.paragraph('Hello')])
        else:
            assert await R.send_rich_message(bot,42,[R.paragraph('Hello')],'<b>Hello</b>')=='html'
        assert await R.send_rich_message(bot,42,[R.paragraph('Again')],'<b>Again</b>')=='html'
    try:
        asyncio.run(asyncio.wait_for(scenario(),1))
        assert cancelled==[True]
        assert bot._post.await_count==1
        assert bot.send_message.await_args.kwargs['parse_mode']=='HTML'
    finally:R.reset_support_latch()


def test_transport_failure_skips_repeated_native_attempts_until_cooldown(monkeypatch):
    R.reset_support_latch()
    monkeypatch.setattr(R.config,'RICH_TEXT_ENABLED',True)
    instant=[10.0]
    monkeypatch.setattr(R.time,'monotonic',lambda:instant[0])
    bot=SimpleNamespace(_post=AsyncMock(side_effect=TimedOut()),send_message=AsyncMock(return_value='html'))
    async def scenario():
        for _ in range(3):await R.send_rich_message(bot,42,[R.paragraph('Hi')],'<b>Hi</b>')
        assert bot._post.await_count==1
        instant[0]+=R.NATIVE_COOLDOWN_SECONDS+1
        await R.send_rich_message(bot,42,[R.paragraph('Retry')],'<b>Retry</b>')
        assert bot._post.await_count==2
    try:asyncio.run(scenario())
    finally:R.reset_support_latch()
