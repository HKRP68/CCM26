"""Native Telegram menus exercise current authority and real auction transactions."""
import asyncio
import importlib
import json
import time
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from test_franchise_website import ADMIN, DELEGATE, OTHER, OWNER, site  # noqa: F401


class Message:
    def __init__(self, text='', chat_id=1):
        self.text=text;self.chat_id=chat_id;self.message_id=7;self.replies=[];self.reply_to_message=None
    async def reply_text(self,text,**kwargs):
        self.replies.append((text,kwargs));return self
    async def reply_document(self,**kwargs):
        self.replies.append(('document',kwargs));return self


class Query:
    def __init__(self,actor,data,message):
        self.from_user=SimpleNamespace(id=actor);self.data=data;self.message=message;self.answers=[]
    async def answer(self,text=None,**kwargs):self.answers.append((text,kwargs))
    async def edit_message_text(self,text,**kwargs):self.message.replies.append((text,kwargs))


def update(actor=DELEGATE,chat=1,text='',callback=None):
    message=Message(text,chat)
    user=SimpleNamespace(id=actor,first_name='Operator')
    return SimpleNamespace(effective_user=user,effective_chat=SimpleNamespace(id=chat,type='private' if chat>0 else 'supergroup'),
                           effective_message=message,callback_query=Query(actor,callback,message) if callback else None)


def context():
    async def get_chat(chat):return SimpleNamespace(id=chat)
    return SimpleNamespace(user_data={},args=[],bot=SimpleNamespace(username='AuctionTestBot',get_chat=get_chat))


@pytest.fixture
def menu(site,monkeypatch):
    import handlers.franchise_bot as module
    module=importlib.reload(module)
    monkeypatch.setattr(module,'get_session',site.sessions)
    site.A.add_auction_admin(site.db,DELEGATE,by_tg_id=ADMIN)
    site.db.commit()
    return module


def run(awaitable):return asyncio.run(awaitable)


def input_step(menu,ctx,actor,text,chat=1):
    event=update(actor,chat,text)
    with pytest.raises(ApplicationHandlerStop):run(menu.prompt_handler(event,ctx))
    return event


def test_private_manager_selection_needs_no_franchise_and_multiple_roles_are_visible(site,menu,monkeypatch):
    monkeypatch.setenv('OWNER_IDS',str(DELEGATE))
    site.A.set_team_managers(site.db,site.owner,[DELEGATE]);site.db.commit()
    data=menu.read_workspace(site.db,DELEGATE,site.season.id,1,'home')
    assert {'bot_owner','auction_manager','team_manager'}<=set(data['roles'])
    assert data['admin'] and data['my_team_ids']==[site.owner.id]
    text,markup=menu.render_workspace(data,DELEGATE,site.season.id,'home','',context(),1)
    assert 'Bot Owner' in text and 'Team Manager' in text
    assert any('All settings' in b.text for row in markup.inline_keyboard for b in row)


def test_team_manager_cannot_read_or_mutate_global_settings(site,menu):
    site.A.set_team_managers(site.db,site.owner,[OTHER]);site.db.commit()
    data=menu.read_workspace(site.db,OTHER,site.season.id,1,'home')
    assert data['roles'][-1]=='team_manager'
    with pytest.raises(menu.W.AccessError):menu.read_workspace(site.db,OTHER,site.season.id,1,'settings')
    with pytest.raises(menu.W.AccessError):menu.execute_action(site.db,OTHER,site.season.id,1,'admin_settings',{'settings':{'currency_label':'NO'}})
    with pytest.raises(menu.W.AccessError):menu.read_workspace(site.db,771234,site.season.id,1,'home')


def test_group_buttons_never_select_another_groups_auction(site,menu):
    second=site.A.create_season(site.db,'Different group',min_squad_size=0,max_squad_size=5)
    site.A.bind_chat(site.db,second,-100777777);site.db.commit()
    with pytest.raises(menu.W.AccessError):menu.read_workspace(site.db,DELEGATE,second.id,site.season.chat_id,'home')
    # A cached private selection must not break opening the current group menu.
    ctx=context();ctx.user_data['franchise_season_id']=second.id
    event=update(DELEGATE,site.season.chat_id)
    run(menu.menu_handler(event,ctx))
    assert site.season.name in event.effective_message.replies[-1][0]


def test_personal_callback_lock_and_group_private_control_redirect(site,menu):
    ctx=context();sid=site.season.id
    forged=update(OWNER,callback=menu._callback(DELEGATE,sid,'v:settings'))
    run(menu.callback_handler(forged,ctx))
    assert forged.callback_query.answers[-1][1]['show_alert'] is True
    assert not forged.effective_message.replies
    event=update(DELEGATE,site.season.chat_id,callback=menu._callback(DELEGATE,sid,'v:settings'))
    run(menu.callback_handler(event,ctx))
    text,kwargs=event.effective_message.replies[-1]
    assert 'private chat' in text and str(OWNER) not in text
    assert kwargs['reply_markup'].inline_keyboard[0][0].url.startswith('https://t.me/AuctionTestBot?start=auction_')


def test_settings_reply_rechecks_revoked_manager_and_preserves_the_database(site,menu):
    ctx=context();sid=site.season.id
    event=update(callback=menu._callback(DELEGATE,sid,'s:currency_label'))
    run(menu.callback_handler(event,ctx));assert menu.PROMPT_KEY in ctx.user_data
    site.A.remove_auction_admin(site.db,DELEGATE,by_tg_id=ADMIN);site.db.commit()
    response=input_step(menu,ctx,DELEGATE,'BAD')
    assert '✅' not in response.effective_message.replies[-1][0]
    assert menu.PROMPT_KEY not in ctx.user_data
    site.db.expire_all();assert site.season.currency_label!='BAD'


def test_guided_franchise_creation_and_independent_team_roles(site,menu):
    ctx=context();sid=site.season.id
    run(menu._start_form(update(),ctx,'teamadd',sid))
    for value in ['New Franchise','771001','skip','771002,771003']:
        result=input_step(menu,ctx,DELEGATE,value)
    assert '✅' in result.effective_message.replies[-1][0]
    from models import AuctionFranchise
    created=site.db.query(AuctionFranchise).filter_by(season_id=sid,name='New Franchise').one()
    assert created.owner_tg_id==771001 and site.A.team_manager_ids(created)==[771002,771003]
    run(menu._start_form(update(),ctx,'role',sid,created.id,'team_managers'))
    input_step(menu,ctx,DELEGATE,'clear')
    site.db.expire_all();assert site.A.team_manager_ids(created)==[] and created.owner_tg_id==771001


def test_same_chat_input_and_duplicate_reply_cannot_double_adjust_purse(site,menu):
    ctx=context();sid=site.season.id
    run(menu._start_form(update(),ctx,'purse',sid,site.owner.id))
    # Other-chat messages leave the wizard untouched.
    run(menu.prompt_handler(update(DELEGATE,2,'deduct'),ctx))
    assert ctx.user_data[menu.PROMPT_KEY]['step']==0
    input_step(menu,ctx,DELEGATE,'deduct');input_step(menu,ctx,DELEGATE,'100')
    async def two_replies():
        return await asyncio.gather(menu.prompt_handler(update(text='Penalty'),ctx),menu.prompt_handler(update(text='Penalty twice'),ctx),return_exceptions=True)
    results=run(two_replies())
    assert sum(isinstance(value,ApplicationHandlerStop) for value in results)==1
    site.db.expire_all();assert site.owner.purse_remaining_lakh==9900
    assert menu.PROMPT_KEY not in ctx.user_data


def test_native_creation_and_settings_are_attributed_without_website_copy(site,menu):
    ctx=context()
    run(menu._start_form(update(),ctx,'new',None))
    result=input_step(menu,ctx,DELEGATE,'New independent season')
    assert '✅' in result.effective_message.replies[-1][0]
    sid=ctx.user_data['franchise_season_id']
    from models import AuctionSeason,AuctionEvent
    created=site.db.get(AuctionSeason,sid);assert created.name=='New independent season'
    event=update(callback=menu._callback(DELEGATE,sid,'s:opening_purse_lakh'))
    run(menu.callback_handler(event,ctx));input_step(menu,ctx,DELEGATE,'15000')
    site.db.expire_all();assert created.opening_purse_lakh==15000
    records=site.db.query(AuctionEvent).filter_by(season_id=sid).all()
    assert not any('Miniapp:' in (record.headline or '') for record in records)


def test_live_confirmation_is_stale_after_someone_else_bids(site,menu):
    site.A.start(site.db,site.season);site.db.commit()
    lot=site.A.current_lot(site.db,site.season)
    values=dict(lot_id=lot.id,stage=lot.rtm_stage,expected_bids=lot.bid_count)
    site.A.place_bid(site.db,site.season,lot,site.owner,site.A.next_min_bid(site.season,lot),by_tg_id=OWNER)
    site.db.commit()
    with pytest.raises(menu.A.PriceMoved):menu.execute_action(site.db,DELEGATE,site.season.id,1,'admin_sold',values)
    site.db.rollback();assert site.A.current_lot(site.db,site.season).status==site.A.LOT_ON_BLOCK


def test_menus_and_settings_buttons_fit_telegram_limits(site,menu):
    sid=site.season.id;ctx=context()
    for view,arg in [('home',''),('settings',''),('fields','retention-1'),('control',''),('finish',''),('preseason',''),('pool','1'),('team',str(site.owner.id)),('help','')]:
        data=menu.read_workspace(site.db,DELEGATE,sid,1,view,arg)
        text,markup=menu.render_workspace(data,DELEGATE,sid,view,arg,ctx,1)
        assert len(text)<=4096
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data:assert len(button.callback_data.encode())<=64
    for key in menu.SETTINGS:assert len(menu._callback(999999999999999,sid,'s:'+key).encode())<=64


def test_sample_download_has_only_an_explicit_example(site,menu):
    event=update()
    run(menu.sample_handler(event,context()))
    kind,payload=event.effective_message.replies[-1]
    assert kind=='document' and 'Example rows only' in payload['caption']
    assert payload['document'].getvalue().startswith(b'name,rating,role,country')


@pytest.mark.parametrize('key,raw,expected',[
    ('opening_purse_lakh','100',100),('direct_bids','off',False),('retention_max_spend_lakh','clear',None),
    ('role_minimums','BAT=4,BOWL=3',{'BAT':4,'BOWL':3}),
    ('rating_rules','83=4',[{'max_rating':83,'min_players':4}]),
    ('base_prices','0-83=20;84+=50',[{'min_rating':0,'max_rating':83,'base_lakh':20},{'min_rating':84,'max_rating':None,'base_lakh':50}]),
    ('increments','200=10,*=50',[{'upto_lakh':200,'step_lakh':10},{'upto_lakh':0,'step_lakh':50}]),
    ('retention_ranges','70-83,0,2,100',[{'min_rating':70,'max_rating':83,'min_count':0,'max_count':2,'price_lakh':100}]),
    ('staged_clock','45 20 12 8 5',[45,20,12,8,5]),
])
def test_setting_prompts_use_explicit_integer_lakh_formats(key,raw,expected):
    from services.franchise_bot_settings import parse_value
    assert parse_value(key,raw)==expected


def test_invalid_reply_retries_the_same_wizard_step(site,menu):
    ctx=context();sid=site.season.id
    run(menu._start_form(update(),ctx,'teamadd',sid))
    input_step(menu,ctx,DELEGATE,'Kept Name')
    failed=input_step(menu,ctx,DELEGATE,'not-an-id')
    assert '✅' not in failed.effective_message.replies[-1][0]
    assert ctx.user_data[menu.PROMPT_KEY]['step']==1
    assert ctx.user_data[menu.PROMPT_KEY]['payload']['name']=='Kept Name'
    input_step(menu,ctx,DELEGATE,'771001')
    assert ctx.user_data[menu.PROMPT_KEY]['step']==2
