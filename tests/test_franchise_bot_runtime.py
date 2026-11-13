"""Dedicated runtime isolation, command completeness, clock and health behavior."""
import asyncio
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import franchise_bot as runtime

TOKEN = '123456789:' + 'A' * 35
OTHER_TOKEN = '987654321:' + 'B' * 35
DB = 'sqlite:////tmp/franchise-runtime-test.db'


def valid(**extra):
    return dict(FRANCHISE_BOT_TOKEN=TOKEN, FRANCHISE_DATABASE_URL=DB,
                FRANCHISE_OWNER_IDS='101, 102', **extra)


@pytest.mark.parametrize('missing', ['FRANCHISE_BOT_TOKEN', 'FRANCHISE_OWNER_IDS'])
def test_dedicated_credentials_are_required_without_legacy_fallback(missing):
    values = valid()
    values.pop(missing)
    values.update(BOT_TOKEN=OTHER_TOKEN, DATABASE_URL='sqlite:///old.db', OWNER_IDS='999')
    with pytest.raises(ValueError):
        runtime.load_settings(values)


@pytest.mark.parametrize('change', [
    {'FRANCHISE_OWNER_IDS':'0'}, {'FRANCHISE_OWNER_IDS':'-12'},
    {'FRANCHISE_OWNER_IDS':'true'}, {'FRANCHISE_ADMIN_IDS':'-100123'},
    {'FRANCHISE_DATABASE_URL':'not-a-database'}, {'FRANCHISE_HEALTH_PORT':'0'},
    {'FRANCHISE_HEALTH_PORT':'abc'}, {'FRANCHISE_BOT_TOKEN':'placeholder'},
])
def test_invalid_dedicated_configuration_fails_closed(change):
    values = valid()
    values.update(change)
    with pytest.raises(ValueError):
        runtime.load_settings(values)


def test_new_bot_cannot_poll_old_identity_but_can_share_auction_database():
    with pytest.raises(ValueError, match='new bot token'):
        runtime.load_settings(valid(BOT_TOKEN=TOKEN))
    assert runtime.load_settings(valid(DATABASE_URL=DB)).database_url == DB


def test_existing_database_url_is_supported_and_explicit_override_wins():
    values=valid(DATABASE_URL='postgresql://example:example@localhost/shared')
    assert runtime.load_settings(values).database_url==DB
    values['FRANCHISE_DATABASE_URL']=''
    assert runtime.load_settings(values).database_url==values['DATABASE_URL']
    values.pop('FRANCHISE_DATABASE_URL')
    assert runtime.load_settings(values).database_url==values['DATABASE_URL']
    values.pop('DATABASE_URL')
    with pytest.raises(ValueError,match='DATABASE_URL'):
        runtime.load_settings(values)


def test_dedicated_owner_admin_and_website_configuration_is_isolated(monkeypatch):
    monkeypatch.setitem(sys.modules, 'config', SimpleNamespace(DATABASE_URL=DB))
    monkeypatch.setitem(sys.modules, 'database', SimpleNamespace(DATABASE_URL=DB))
    values = valid(FRANCHISE_ADMIN_IDS='103;104', FRANCHISE_BOT_USERNAME='@NewFranchiseBot')
    settings = runtime.load_settings(values)
    with patch.dict(os.environ, {**dict(os.environ), 'ADMIN_IDS':'999', 'SUDO_USERS':'888',
                                'ADMIN_USER_IDS':'777','OWNER_IDS':'666','ADMIN_CHAT_ID':'555',
                                'FRANCHISE_WEBSITE_URL':'https://old.example', 'WEBAPP_URL':'https://old.example/webapp',
                                'MINIAPP_NAME':'oldapp'}, clear=True):
        runtime.configure_environment(settings)
        assert os.environ['BOT_TOKEN'] == TOKEN
        assert os.environ['DATABASE_URL'] == DB
        assert os.environ['OWNER_IDS'] == '101,102'
        assert os.environ['BOT_OWNER_IDS'] == '101,102'
        assert os.environ['BOT_ADMIN_IDS'] == '103,104'
        assert os.environ['BOT_USERNAME'] == 'NewFranchiseBot'
        for key in ['ADMIN_IDS','ADMIN_USER_IDS','SUDO_USERS','ADMIN_CHAT_ID','FRANCHISE_WEBSITE_URL','WEBAPP_URL','MINIAPP_NAME']:
            assert os.environ[key] == ''
    assert TOKEN not in repr(settings) and DB not in repr(settings)


def test_cannot_remap_an_already_imported_old_database(monkeypatch):
    monkeypatch.setitem(sys.modules, 'config', SimpleNamespace(DATABASE_URL='sqlite:///old.db'))
    with pytest.raises(RuntimeError, match='own process'):
        runtime.configure_environment(runtime.load_settings(valid()))


def test_import_has_no_database_network_or_scheduler_side_effects():
    root = Path(__file__).resolve().parents[1]
    script = "import sys; import franchise_bot; assert 'database' not in sys.modules; assert 'config' not in sys.modules; assert 'telegram' not in sys.modules; assert 'admin' not in sys.modules; assert 'services.auction_scheduler' not in sys.modules; print('ok')"
    result = subprocess.run([sys.executable,'-c',script], cwd=root, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_logging_redacts_bot_token_and_database_credentials():
    settings = runtime.load_settings(valid())
    formatter = runtime._RedactingFormatter(settings)
    record = logging.LogRecord('runtime', logging.ERROR, __file__, 1,
                               'failed https://api.telegram.org/bot%s and %s', (TOKEN,DB), None)
    output = formatter.format(record)
    assert TOKEN not in output and DB not in output
    assert '[redacted]' in output


def test_health_is_read_only_and_tracks_readiness():
    server = runtime.HealthServer(0,host='127.0.0.1').start()
    address = 'http://127.0.0.1:' + str(server.server.server_address[1])
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(address+'/healthz',timeout=3)
        assert error.value.code == 503
        assert json.loads(error.value.read())['status'] == 'starting'
        server.ready = True
        with urlopen(address+'/healthz',timeout=3) as response:
            assert response.status == 200
            data=json.load(response)
            assert data['service']=='franchise-auction-bot' and data['status']=='ready'
            assert data['ready'] is True
        with urlopen(address+'/franchise',timeout=3) as response:
            assert response.status==200 and b'Franchise HQ' in response.read()
    finally:
        server.close()
    assert server.server is None and server.thread is None


def test_registry_has_all_group_auction_controls_and_owner_guard(monkeypatch):
    from services import button_access
    from services.franchise_bot_registry import register_auction_handlers
    monkeypatch.setattr(button_access, 'install_button_access_defaults', lambda:None)
    app = SimpleNamespace(handlers=[])
    app.add_handler = lambda handler,group=0:app.handlers.append((group,handler))
    register_auction_handlers(app)
    from telegram.ext import CommandHandler, CallbackQueryHandler
    commands = {cmd for _,handler in app.handlers if isinstance(handler,CommandHandler) for cmd in handler.commands}
    for required in ['bid','anew','abind','asetsimport','asquad','aretain','retain','retainlock','apick','apickclock',
                     'ashortlist','artm','aadminadd','aadminremove','atrade','atradeapprove','atradedeadline','aclone','arestart']:
        assert required in commands
    assert not commands & {'claim','debut','buy','unscramble','auctionleague','simset'}
    patterns = {handler.pattern.pattern for _,handler in app.handlers if isinstance(handler,CallbackQueryHandler)}
    for pattern in ['^au_bid_','^au_rtm_','^au_info_','^au_sets_','^au_x_','^au_ret_','^au_rfinal_','^au_rpk_','^au_rel_','^au_tr_']:
        assert pattern in patterns
    assert any(group==-60 and handler.callback is button_access.button_access_guard for group,handler in app.handlers)
    assert any(group==-50 for group,_ in app.handlers)


def test_post_init_registers_one_clock_and_the_bot_menu(monkeypatch):
    starts=[]
    jobs=[]
    scheduler=ModuleType('services.auction_scheduler')
    scheduler.SWEEP_JOB_NAME='auction_clock'
    def start(app):
        starts.append(app)
        jobs.append('clock')
    scheduler.start_auction_scheduler=start
    monkeypatch.setitem(sys.modules,'services.auction_scheduler',scheduler)
    menu=ModuleType('handlers.franchise_bot')
    menu.BOT_COMMANDS=[('start','Open Franchise Auction'),('auction','Auction workspace')]
    monkeypatch.setitem(sys.modules,'handlers.franchise_bot',menu)
    app=SimpleNamespace(bot_data={},bot=SimpleNamespace(username='NewFranchiseBot',set_my_commands=AsyncMock()),
                        job_queue=SimpleNamespace(get_jobs_by_name=lambda name:jobs,
                                                  run_once=Mock(),run_repeating=Mock()))
    # Job registration is synchronous; records let the test run the deferred
    # menu callback after startup instead of pretending a network call ran.
    health=runtime.HealthServer(0,host='127.0.0.1')
    app.bot_data['franchise_health']=health
    with patch.dict(os.environ,{},clear=False):
        asyncio.run(runtime._post_init(app))
        asyncio.run(runtime._post_init(app))
        assert os.environ['BOT_USERNAME']=='NewFranchiseBot'
    assert len(starts)==1
    assert health.ready
    assert health.snapshot()['ready']
    app.bot.set_my_commands.assert_not_awaited()
    app.job_queue.run_once.assert_called_once()
    app.job_queue.run_repeating.assert_called_once()
    menu_job=app.job_queue.run_once.call_args.args[0]
    ctx=SimpleNamespace(bot=app.bot,application=app,job=SimpleNamespace(data=0),job_queue=app.job_queue)
    asyncio.run(menu_job(ctx))
    assert [command.command for command in app.bot.set_my_commands.call_args.args[0]]==['start','auction']
    asyncio.run(runtime._post_shutdown(app))
    assert not health.ready


def test_clock_unavailable_never_reports_ready(monkeypatch):
    app=SimpleNamespace(bot_data={},job_queue=None)
    with pytest.raises(RuntimeError,match='JobQueue'):
        asyncio.run(runtime._post_init(app))


def test_polling_conflict_stops_the_extra_process():
    from telegram.error import Conflict
    app=SimpleNamespace(bot_data={},stop_running=lambda:None)
    called=[]
    app.stop_running=lambda:called.append(True)
    context=SimpleNamespace(error=Conflict('another poller'),application=app)
    asyncio.run(runtime._error_handler(None,context))
    assert called==[True]


def test_render_runs_the_dedicated_bot_with_shared_database_and_separate_identity():
    import yaml
    root=Path(__file__).resolve().parents[1]
    service=yaml.safe_load((root/'render.franchise-bot.yaml').read_text())['services'][0]
    assert service['startCommand']=='python franchise_bot.py'
    assert service['healthCheckPath']=='/healthz'
    keys={entry['key'] for entry in service['envVars']}
    assert {'FRANCHISE_BOT_TOKEN','DATABASE_URL','FRANCHISE_OWNER_IDS'}<=keys
    assert not {'BOT_TOKEN','WEBSITE_SECRET'} & keys


def test_real_builder_registers_only_auction_runtime_without_touching_database(tmp_path):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / 'never-opened.db'
    script = r'''
import os,sys,types
from pathlib import Path
os.environ['FRANCHISE_BOT_TOKEN']='123456789:'+'A'*35
os.environ['FRANCHISE_DATABASE_URL']='sqlite:///'+sys.argv[1]
os.environ['FRANCHISE_OWNER_IDS']='101'
os.environ['BOT_TOKEN']='987654321:'+'B'*35
os.environ['DATABASE_URL']='sqlite:///old-main-bot.db'
os.environ.pop('FRANCHISE_DEDICATED_RUNTIME',None)
menu=types.ModuleType('handlers.franchise_bot')
menu.BOT_COMMANDS=[('start','Open Franchise Auction')]
menu.register=lambda app:app.bot_data.update(franchise_menu_registered=True)
sys.modules['handlers.franchise_bot']=menu
import franchise_bot
app=franchise_bot.build_application()
assert app.bot_data['franchise_dedicated'] is True
assert app.bot_data['franchise_menu_registered'] is True
assert app.update_processor.max_concurrent_updates==16
assert app.job_queue is not None
assert not app.job_queue.jobs(), 'Building does not start the auction scheduler'
assert not Path(sys.argv[1]).exists(), 'Building does not initialize or query the database'
assert 'admin' not in sys.modules and 'bot' not in sys.modules
from telegram.ext import CommandHandler
commands={cmd for handlers in app.handlers.values() for handler in handlers if isinstance(handler,CommandHandler) for cmd in handler.commands}
assert {'bid','anew','atrade','importplayers'}<=commands
assert not {'claim','debut','unscramble','auctionleague'}&commands
print('ready')
'''
    result = subprocess.run([sys.executable,'-c',script,str(path)], cwd=root,
                            capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_native_menus_are_registered_in_a_clean_dedicated_process(tmp_path):
    root=Path(__file__).resolve().parents[1]
    target=tmp_path/'fresh-native.db'
    script=r'''
import os,sys
from pathlib import Path
os.environ['FRANCHISE_BOT_TOKEN']='123456789:'+'A'*35
os.environ['FRANCHISE_DATABASE_URL']='sqlite:///'+sys.argv[1]
os.environ['FRANCHISE_OWNER_IDS']='101'
os.environ['BOT_TOKEN']='987654321:'+'B'*35
os.environ['DATABASE_URL']='sqlite:///old-main-bot.db'
import franchise_bot
application=franchise_bot.build_application()
from telegram.ext import CommandHandler,CallbackQueryHandler
commands={command for handlers in application.handlers.values() for handler in handlers if isinstance(handler,CommandHandler) for command in handler.commands}
assert {'auction','start','settings','newauction','bind','roles','id','importplayers','sampleplayers','bid','anew','atrade'}<=commands
assert any(isinstance(handler,CallbackQueryHandler) and handler.pattern.pattern=='^fa:' for handlers in application.handlers.values() for handler in handlers)
assert not {'debut','claim','spin','auctionleague'}&commands
assert not Path(sys.argv[1]).exists()
assert not application.job_queue.jobs()
assert 'admin' not in sys.modules and 'bot' not in sys.modules
print('ready')
'''
    result=subprocess.run([sys.executable,'-c',script,str(target)],cwd=root,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
