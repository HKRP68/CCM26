"""Real HTTP monitoring, bounded background startup and clear service status."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import ModuleType,SimpleNamespace
import sys
import threading
import time
from unittest.mock import AsyncMock,Mock
from urllib.error import HTTPError
from urllib.request import Request,urlopen

import pytest

import franchise_bot as runtime
from services.franchise_health import HealthServer


@pytest.fixture
def server():
    server=HealthServer(0,host='127.0.0.1',bot_username='ExampleAuctionBot').start()
    server.address='http://127.0.0.1:'+str(server.server.server_address[1])
    yield server
    server.close()


def request(server,path='/',method='GET'):
    try:
        response=urlopen(Request(server.address+path,method=method),timeout=2)
    except HTTPError as exc:
        response=exc
    with response:
        return response.status,response.headers,response.read()


@pytest.mark.parametrize('path',['/','/status','/status/','/franchise','/?monitor=uptime'])
def test_root_is_a_status_page_during_startup_and_has_a_real_bot_link(server,path):
    status,headers,body=request(server,path)
    assert status==200 and 'text/html' in headers['Content-Type']
    assert b'Starting the service' in body
    assert b'https://t.me/ExampleAuctionBot' in body
    assert b'uses its own website service' in body
    assert b'Error response' not in body
    assert headers['Cache-Control']=='no-store'


@pytest.mark.parametrize('path',['/healthz','/healthz/','/healthz?check=uptime','/readyz','/health'])
@pytest.mark.parametrize('method',['GET','HEAD'])
def test_health_paths_and_monitor_methods_track_readiness(server,path,method):
    status,headers,body=request(server,path,method)
    assert status==503 and headers['Retry-After']=='5'
    assert int(headers['Content-Length'])>0
    if method=='HEAD':
        assert body==b''
    else:
        assert json.loads(body)['ready'] is False
    server.ready=True
    status,headers,body=request(server,path,method)
    assert status==200
    if method=='HEAD':
        assert body==b''
    else:
        assert json.loads(body)['status']=='ready'


def test_http_liveness_and_bot_readiness_are_separate_when_heartbeat_stalls(server):
    server.ready=True
    server.heartbeat_at=time.monotonic()-61
    assert request(server,'/livez')[0]==200
    status,_,body=request(server,'/healthz')
    assert status==503 and json.loads(body)['status']=='unresponsive'
    assert b'Bot response delayed' in request(server)[2]
    server.heartbeat()
    assert request(server,'/healthz')[0]==200


def test_status_page_escapes_or_omits_untrusted_configuration(server):
    server.bot_username='bad"><script>alert(1)</script>'
    _,_,body=request(server)
    assert b'<script>alert' not in body and b'https://t.me/' not in body
    assert b'send <code>/auction</code>' in body


@pytest.mark.parametrize('path',['/api/webapp/auction','/admin','/private','/nonexistent'])
def test_unknown_and_private_paths_remain_real_404s(server,path):
    status,_,body=request(server,path)
    assert status==404 and json.loads(body)['error']=='not_found'


def test_concurrent_monitor_requests_do_not_wait_for_bot_work(server):
    server.stage='database'
    def read(index):
        return request(server,'/livez' if index%2 else '/', 'HEAD' if index%3 else 'GET')[0]
    with ThreadPoolExecutor(max_workers=12) as workers:
        assert list(workers.map(read,range(36)))==[200]*36
    assert request(server,'/healthz')[0]==503


@pytest.mark.parametrize('override,expected',[('',10000),('  ',10000),('9001',9001)])
def test_blank_optional_health_port_uses_the_render_assigned_port(override,expected):
    settings=runtime.load_settings(dict(FRANCHISE_BOT_TOKEN='123456789:'+'A'*35,
        DATABASE_URL='sqlite:///:memory:',FRANCHISE_OWNER_IDS='101',PORT='10000',FRANCHISE_HEALTH_PORT=override))
    assert settings.health_port==expected


def test_menu_api_failure_does_not_remove_readiness_and_is_retried(monkeypatch):
    from telegram.error import TimedOut
    menu=ModuleType('handlers.franchise_bot')
    menu.BOT_COMMANDS=[('auction','Open auction')]
    monkeypatch.setitem(sys.modules,'handlers.franchise_bot',menu)
    health=HealthServer(0)
    health.ready=True
    bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=TimedOut('network unavailable')))
    app=SimpleNamespace(bot_data={'franchise_health':health})
    queue=SimpleNamespace(run_once=Mock())
    ctx=SimpleNamespace(application=app,bot=bot,job=SimpleNamespace(data=0),job_queue=queue)
    asyncio.run(runtime._sync_command_menu(ctx))
    assert health.snapshot()['ready'] is True
    assert queue.run_once.call_args.kwargs['data']==1
    assert queue.run_once.call_args.args[1]>0
    bot.set_my_commands.side_effect=None
    ctx.job.data=1
    asyncio.run(runtime._sync_command_menu(ctx))
    assert app.bot_data['franchise_menu_ready'] is True


def test_rate_limited_menu_honors_telegram_retry_after(monkeypatch):
    from telegram.error import RetryAfter
    monkeypatch.setenv('PTB_TIMEDELTA','1')
    menu=ModuleType('handlers.franchise_bot')
    menu.BOT_COMMANDS=[('auction','Open auction')]
    monkeypatch.setitem(sys.modules,'handlers.franchise_bot',menu)
    ctx=SimpleNamespace(application=SimpleNamespace(bot_data={}),
        bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=RetryAfter(400))),
        job=SimpleNamespace(data=0),job_queue=SimpleNamespace(run_once=Mock()))
    asyncio.run(runtime._sync_command_menu(ctx))
    assert ctx.job_queue.run_once.call_args.args[1]>=400


def test_main_serves_status_while_database_initialization_is_blocked(monkeypatch):
    entered,release=threading.Event(),threading.Event()
    created=[]
    def start(port,**kwargs):
        server=HealthServer(0,host='127.0.0.1',**kwargs)
        server.address=''
        created.append(server)
        return server
    def init_db():
        entered.set()
        assert release.wait(5)
    db=ModuleType('database')
    db.init_db=init_db
    monkeypatch.setitem(sys.modules,'database',db)
    settings=runtime.Settings('123456789:'+'A'*35,'sqlite:///:memory:',(101,),health_port=10000)
    monkeypatch.setattr(runtime,'load_settings',lambda:settings)
    monkeypatch.setattr(runtime,'configure_environment',lambda settings:None)
    monkeypatch.setattr(runtime,'configure_logging',lambda settings:None)
    monkeypatch.setattr(runtime,'HealthServer',start)
    monkeypatch.setattr(runtime,'build_application',lambda settings:SimpleNamespace(bot_data={},run_polling=Mock()))
    with ThreadPoolExecutor(max_workers=1) as worker:
        future=worker.submit(runtime.main)
        try:
            assert entered.wait(2)
            server=created[0]
            server.address='http://127.0.0.1:'+str(server.server.server_address[1])
            status,_,body=request(server)
            assert status==200 and b'Preparing the database' in body
            assert request(server,'/healthz')[0]==503
        finally:
            release.set()
        assert future.result(timeout=5)==0
    assert created[0].server is None and created[0].thread is None


@pytest.mark.parametrize('path',['/healthz','/healthz/','/healthz?check=uptime','/livez'])
@pytest.mark.parametrize('method',['GET','HEAD'])
def test_website_monitoring_needs_neither_telegram_nor_database(monkeypatch,path,method):
    import website,database
    def unavailable():
        raise AssertionError('A health check must not access the database')
    monkeypatch.setattr(database,'get_session',unavailable)
    app=website.create_app({'TESTING':True})
    response=app.test_client().open(path,method=method)
    assert response.status_code==200
    assert response.headers['Cache-Control']=='no-store'
    assert 'Set-Cookie' not in response.headers
    if method=='HEAD':
        assert response.data==b''
    else:
        assert response.get_json()=={'service':'franchise-website','status':'ok'}


def test_website_trailing_slash_route_remains_a_real_auction_workspace():
    import website
    app=website.create_app({'TESTING':True})
    response=app.test_client().get('/franchise/')
    assert response.status_code==200
    assert b'franchise_site.js' in response.data
