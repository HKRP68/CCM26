import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from handlers.stats_website import stats_website_handler


def test_command_opens_portal_without_querying_history():
    message = SimpleNamespace(reply_text=AsyncMock())
    with patch('handlers.stats_website._webapp_host', return_value='https://cricket.example'):
        asyncio.run(stats_website_handler(SimpleNamespace(effective_message=message), None))
    markup = message.reply_text.await_args.kwargs['reply_markup']
    assert markup.inline_keyboard[0][0].url == 'https://cricket.example/stats'


def test_command_reports_unconfigured_website():
    message = SimpleNamespace(reply_text=AsyncMock())
    with patch('handlers.stats_website._webapp_host', return_value=None):
        asyncio.run(stats_website_handler(SimpleNamespace(effective_message=message), None))
    assert 'not been configured' in message.reply_text.await_args.args[0]
    assert 'reply_markup' not in message.reply_text.await_args.kwargs
