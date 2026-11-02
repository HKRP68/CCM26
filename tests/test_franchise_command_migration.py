"""Historical command parity, retired-bot separation and usable native help."""
import ast
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT=Path(__file__).resolve().parents[1]
LEGACY=json.loads((ROOT/'tests/fixtures/franchise_legacy_commands.json').read_text())


def test_original_bot_has_no_franchise_handlers_menus_clock_or_focus_lock():
    tree=ast.parse((ROOT/'bot.py').read_text())
    commands=set()
    for node in ast.walk(tree):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='CommandHandler':
            names=ast.literal_eval(node.args[0])
            commands.update([names] if isinstance(names,str) else names)
        if isinstance(node,ast.ImportFrom):
            assert node.module not in ('handlers.auction','handlers.auction_trade','services.auction_scheduler','services.auction_focus')
        if isinstance(node,ast.AsyncFunctionDef):
            assert node.name!='_auction_focus_check'
    assert not commands.intersection(LEGACY['commands'])
    assert {'claim','debut','dnew','dpublish','pick','dtrade','unscramble'}<=commands
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('BOT_MENU_COMMANDS','ADMIN_MENU_COMMANDS') for t in node.targets):
            assert not {name for name,_ in ast.literal_eval(node.value)}.intersection(LEGACY['commands'])


def test_actual_new_bot_preserves_all_historical_handlers_and_guides_every_command(tmp_path):
    target=tmp_path/'not-opened.db'
    script=r'''
import json,os,sys
from pathlib import Path
os.environ['FRANCHISE_BOT_TOKEN']='123456789:'+'A'*35
os.environ['BOT_TOKEN']='987654321:'+'B'*35
os.environ['DATABASE_URL']='sqlite:///'+sys.argv[1]
os.environ['FRANCHISE_DATABASE_URL']=''
os.environ['FRANCHISE_OWNER_IDS']='101'
os.environ['FRANCHISE_SOLO_LEAGUE']='false'
os.environ.pop('FRANCHISE_DEDICATED_RUNTIME',None)
import franchise_bot
application=franchise_bot.build_application()
from telegram.ext import CommandHandler,CallbackQueryHandler,MessageHandler
handlers=[h for group in application.handlers.values() for h in group]
commands={name:h.callback for h in handlers if isinstance(h,CommandHandler) for name in h.commands}
legacy=json.loads(Path('tests/fixtures/franchise_legacy_commands.json').read_text())
assert len(legacy['commands'])==142 and len(legacy['callbacks'])==12
for name,callback in legacy['commands'].items():
 assert name in commands,name
 expected='handlers.franchise_bot.menu_handler' if name=='auction' else callback
 assert commands[name].__module__+'.'+commands[name].__name__==expected,name
callbacks={h.pattern.pattern:h.callback for h in handlers if isinstance(h,CallbackQueryHandler)}
for pattern,callback in legacy['callbacks'].items():
 assert pattern in callbacks,pattern
 assert callbacks[pattern].__module__+'.'+callbacks[pattern].__name__==callback,pattern
from handlers.auction import DOT_COMMANDS,dot_command_handler,acall_handler
from services.franchise_bot_registry import DOT_ALIASES
assert set(legacy['dot_aliases'])<=DOT_ALIASES
assert all(DOT_COMMANDS.get(key)==value for key,value in legacy['dot_aliases'].items())
assert any(isinstance(h,MessageHandler) and h.callback is dot_command_handler for h in handlers)
assert any(isinstance(h,MessageHandler) and h.callback is acall_handler for h in handlers)
from handlers.franchise_commands import command_catalogue
documented={name for row in command_catalogue() for name in row['names']}
assert documented==set(commands),(documented-set(commands),set(commands)-documented)
assert not Path(sys.argv[1]).exists()
assert not application.job_queue.jobs()
assert 'bot' not in sys.modules and 'admin' not in sys.modules
print('parity verified')
'''
    result=subprocess.run([sys.executable,'-c',script,str(target)],cwd=ROOT,capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr


def test_original_bot_help_hides_auctions_without_removing_shared_reference():
    from services import onboarding_rich as R
    from services.command_catalog import COMMAND_CATEGORIES
    assert any(title=='🔨 Franchise Auction' for title,_ in COMMAND_CATEGORIES)
    assert '🔨 Franchise Auction' in R.commands_html()
    _,retired_help,_=R.commands_card(include_franchise_auction=False)
    assert '🔨 Franchise Auction' not in retired_help
    assert '/bid [amount]' not in retired_help
    assert '/claim' in retired_help and '/dboard' in retired_help


@pytest.mark.parametrize('query,expected',[('/apickpass','apickskip'),('retention','retain'),('trade','atrade'),('ASQUAD','asquad')])
def test_guide_finds_aliases_and_workflow_topics(query,expected):
    from handlers.franchise_commands import command_catalogue
    assert any(expected in row['names'] for row in command_catalogue(query))


def test_every_guide_page_fits_telegram_and_unknown_search_can_be_cleared():
    from handlers.franchise_commands import command_catalogue,render_commands,PAGE_SIZE
    count=len(command_catalogue())
    for page in range(1,(count+PAGE_SIZE-1)//PAGE_SIZE+1):
        text,markup=render_commands(999999999999999,page)
        assert len(text)<=4096
        assert all(len(button.callback_data.encode())<=64 for row in markup.inline_keyboard for button in row)
    text,_=render_commands(1,query='<missing>')
    assert 'No matching commands' in text and '&lt;missing&gt;' in text


def test_command_guide_needs_no_database_and_locks_personal_pagination():
    from handlers.franchise_commands import commands_handler,commands_callback
    message=SimpleNamespace(reply_text=AsyncMock())
    update=SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(id=-100),effective_message=message)
    context=SimpleNamespace(args=['retention'],user_data={})
    asyncio.run(commands_handler(update,context))
    assert 'retention' in message.reply_text.call_args.args[0]
    query=SimpleNamespace(data='fc:1:2',from_user=SimpleNamespace(id=2),answer=AsyncMock(),edit_message_text=AsyncMock())
    update.callback_query=query
    asyncio.run(commands_callback(update,context))
    assert query.answer.call_args.kwargs['show_alert']
    query.edit_message_text.assert_not_awaited()
