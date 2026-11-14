"""Real authenticated website requests use the same draft transactions as Telegram."""
import json
from types import SimpleNamespace

import pytest

from test_franchise_website import site,login,OWNER,OTHER,ADMIN  # noqa: F401
from test_franchise_draft_seasons_page import page,competition  # noqa: F401

COOWNER=981003


def api(client,csrf_token,sid=None,*,action=None,**values):
    body={'mode':'draft','view':'franchisedraft','season_id':sid,**values}
    if action:body['action']=action
    return client.post('/api/webapp/auction'+('/action' if action else ''),json=body,headers={'X-CSRF-Token':csrf_token})


def stage(client,token,sid,operation,values=None,reason=''):
    response=api(client,token,sid,action='draft_preview',operation=operation,values=values or {},reason=reason)
    assert response.status_code==200,response.json
    return response.json['confirmation']['token']


def approve(client,token,sid,quote):
    response=api(client,token,sid,action='draft_answer',token=quote,accept=True)
    assert response.status_code==200,response.json
    return response


def apply(client,token,sid,operation,values=None,reason=''):
    return approve(client,token,sid,stage(client,token,sid,operation,values,reason))


def identity(site,actor):
    client=site.app.test_client()
    return client,login(SimpleNamespace(client=client),actor)


def test_full_website_draft_from_empty_squads_to_next_tournament(site):
    from models import Player,AuctionSeason,FranchiseDraft,AuctionEvent,Tournament,TournamentTeam,ChallengePlayer
    # The source player is already signed elsewhere. Fresh draft eligibility
    # must still include that catalogue ID.
    old=site.A.lots(site.db,site.season.id)[0]
    source=site.db.get(Player,old.player_id)
    site.A._sign_before_auction(site.db,site.season,site.owner,source,100,
        kind=site.A.ACQ_RETAINED,counter='retained_count',ledger_kind=site.A.LEDGER_RETENTION,
        verb='retain',note='Previous season signing',event_kind='retained',mark='🔒',word='retained',quiet=True)
    second=Player(name=source.name,rating=90,category='Batsman',country='England',version='Legend',
                  bat_rating=90,bowl_rating=50,bat_hand='Right',bowl_hand='Right',bowl_style='Medium Pacer',is_active=True)
    site.db.add(second);site.db.commit()
    token=login(site,ADMIN)
    response=api(site.client,token,action='draft_create',name='Website Draft Season',rounds='1',purse='100')
    assert response.status_code==200,response.json
    sid=response.json['season_id'];season=site.db.get(AuctionSeason,sid)
    assert site.A.franchises(site.db,sid)==[] and site.db.get(FranchiseDraft,sid).rounds==1
    first=stage(site.client,token,sid,'team',{'name':'North','owner_id':str(OWNER),'co_owners':str(COOWNER),'managers':'','city':'North City'})
    assert site.A.franchises(site.db,sid)==[]
    approve(site.client,token,sid,first)
    apply(site.client,token,sid,'team',{'name':'South','owner_id':str(OTHER),'co_owners':'','managers':''})
    catalogue=api(site.client,token,sid,part='catalogue').json
    assert {p['id'] for p in catalogue['catalogue']}=={source.id,second.id}
    assert all(p['status']=='not_in_pool' for p in catalogue['catalogue'])
    apply(site.client,token,sid,'pooladd',{'text':'all'})
    assert all(t.squad_size==0 for t in site.A.franchises(site.db,sid))
    apply(site.client,token,sid,'prices',{'text':'85:20cr\n90=30cr'})
    assert api(site.client,token,sid,action='draft_preview',operation='custom',values={'text':source.name+':25.5cr'}).status_code==400
    lots=site.A.lots(site.db,sid);root=next(p for p in lots if p.player_id==source.id);legend=next(p for p in lots if p.player_id==second.id)
    apply(site.client,token,sid,'custom',{'text':f'#{root.id}:25.5cr'})
    apply(site.client,token,sid,'rules',{'minimums':'BAT=1','maximums':'BAT=1','ratings':'clear'})
    apply(site.client,token,sid,'bind',{'chat_id':'-100989900'})
    apply(site.client,token,sid,'clock',{'seconds':'0','policy':'pause'})
    apply(site.client,token,sid,'order',{'text':'North | South'})
    assert api(site.client,token,sid,part='readiness').status_code==200
    quote=stage(site.client,token,sid,'start')
    site.db.expire_all();assert season.status==site.A.STATUS_SETUP
    approve(site.client,token,sid,quote)
    assert api(site.client,token,sid,action='draft_pick',player=f'#{root.id}').status_code==400
    owner,owner_token=identity(site,OWNER);co,co_token=identity(site,COOWNER)
    quote=api(owner,owner_token,sid,action='draft_pick',player=f'#{root.id}').json['confirmation']
    assert '25.5 Cr' in quote['html'] and 'Base' in quote['html']
    competing=api(co,co_token,sid,action='draft_pick',player=f'#{legend.id}').json['confirmation']
    assert api(co,co_token,sid,action='draft_pick_answer',token=quote['token'],accept=True).status_code==400
    site.db.expire_all();north=site.A.franchises(site.db,sid)[0]
    assert north.purse_remaining_lakh==10000 and north.squad_size==0
    assert api(owner,owner_token,sid,action='draft_pick_answer',token=quote['token'],accept=True).status_code==200
    assert api(co,co_token,sid,action='draft_pick_answer',token=competing['token'],accept=True).status_code==400
    assert api(owner,owner_token,sid,action='draft_pick_answer',token=quote['token'],accept=True).status_code==400
    site.db.expire_all();assert north.purse_remaining_lakh==7450 and site.A.ledger_total(site.db,north.id)==7450
    other,other_token=identity(site,OTHER)
    quote=api(other,other_token,sid,action='draft_pick',player=f'#{legend.id}').json['confirmation']
    assert api(other,other_token,sid,action='draft_pick_answer',token=quote['token'],accept=True).status_code==200
    site.db.expire_all();assert season.status==site.A.STATUS_COMPLETED
    assert site.db.query(AuctionEvent).filter_by(season_id=sid,kind='fd_picked').count()==2
    assert site.db.query(AuctionEvent).filter_by(season_id=sid,kind='fd_completed').count()==1
    exported=api(owner,owner_token,sid,part='export').json
    assert '25.50' in exported['download'] and 'Legend' in exported['download']
    apply(site.client,token,sid,'publish')
    site.db.expire_all();assert season.league_id and site.db.query(ChallengePlayer).count()==2
    quote=stage(site.client,token,sid,'continue')
    assert site.db.query(Tournament).count()==0
    approve(site.client,token,sid,quote)
    assert site.db.query(Tournament).count()==1
    assert {t.owner_tg_id for t in site.db.query(TournamentTeam).all()}=={OWNER,OTHER}
    assert api(site.client,token,sid,action='draft_preview',operation='prices',values={'text':'85:0cr\n90:0cr'},reason='Too late').status_code==400


def test_password_authenticated_admin_workspace_is_session_bound_and_cannot_impersonate_owner(page):
    from services import franchise_draft as D
    from models import FranchiseDraftAction
    c=page.c;season=c.A.create_season(c.db,'Admin-only website draft');D.enable(c.db,season,rounds=2);c.db.commit()
    assert page.client.get(f'/franchise-drafts/{season.id}/workspace').status_code==200
    def request(client,**body):return client.post('/api/admin/franchise-draft',json={'season_id':season.id,**body})
    data=request(page.client,part='settings').json
    assert data['can_manage_draft'] and data['draft']['can_manage']
    preview=request(page.client,request_type='action',action='draft_preview',operation='team',values={'name':'New side','owner_id':'777','co_owners':'778','managers':'779'})
    assert preview.status_code==200,preview.json
    quote=preview.json['confirmation']['token']
    assert c.db.get(FranchiseDraftAction,quote).actor_tg_id<0
    another=page.client.application.test_client()
    with another.session_transaction() as state:state['admin']=True
    denied=request(another,request_type='action',action='draft_answer',token=quote,accept=True)
    assert denied.status_code==400 and c.A.franchises(c.db,season.id)==[]
    assert request(page.client,request_type='action',action='draft_answer',token=quote,accept=True).status_code==200
    c.db.expire_all();team=c.A.franchises(c.db,season.id)[0]
    assert team.owner_tg_id==777 and c.A.co_owner_ids(team)==[778] and c.A.team_manager_ids(team)==[779]
    assert request(page.client,request_type='action',action='draft_pick',player='#1').status_code==400
    with page.client.session_transaction() as state:state.clear()
    expired=request(page.client,part='settings')
    assert expired.status_code==401 and 'session expired' in expired.json['message']


def test_random_order_preview_freezes_the_actual_order_and_untrusted_flags_do_not_grant_admin(site):
    from services import franchise_draft as D
    season=site.A.create_season(site.db,'Random draft');D.enable(site.db,season,rounds=1)
    for i in range(5):site.A.create_franchise(site.db,season,f'Side {i}',owner_tg_id=OWNER if i==0 else 8800+i)
    site.db.commit();token=login(site,ADMIN)
    quote=stage(site.client,token,season.id,'order',{'text':'random'})
    from models import FranchiseDraftAction
    row=site.db.get(FranchiseDraftAction,quote)
    expected=[int(i) for i in json.loads(row.payload_json)['text'].split('|')]
    approve(site.client,token,season.id,quote);site.db.expire_all()
    assert json.loads(D.get_draft(site.db,season).order_json)==expected
    owner,owner_token=identity(site,OWNER)
    denied=api(owner,owner_token,season.id,action='draft_preview',operation='prices',values={'text':'85:0cr'},trusted_admin=True)
    assert denied.status_code==403
    assert api(owner,owner_token,season.id,part='settings',trusted_admin=True).status_code==403


def test_website_csv_budget_timer_skip_corrections_and_undo_share_the_ledger(site):
    from models import Player,AuctionSeason,FranchiseDraftPick
    from services import franchise_draft as D
    token=login(site,ADMIN)
    sid=api(site.client,token,action='draft_create',name='Recovery on website',rounds='2',purse='100').json['season_id']
    season=site.db.get(AuctionSeason,sid)
    for name,owner in (('North',OWNER),('South',OTHER)):
        apply(site.client,token,sid,'team',{'name':name,'owner_id':str(owner),'co_owners':'','managers':''})
    text='name,rating,role,country,version,bat_rating,bowl_rating,overseas\nRoot,95,BAT,England,Base,95,30,true\nCummins,90,BOWL,Australia,Base,60,95,false\nButtler,88,WK,England,Base,88,20,true'
    quote=stage(site.client,token,sid,'csv',{'text':text})
    assert site.db.query(Player).count()==1
    approve(site.client,token,sid,quote)
    assert site.db.query(Player).count()==4
    apply(site.client,token,sid,'pooladd',{'text':'all'})
    apply(site.client,token,sid,'prices',{'text':'85:10cr\n95:20cr\n90:15cr\n88:12cr'})
    apply(site.client,token,sid,'opening',{'amount':'150.25cr'})
    apply(site.client,token,sid,'purse',{'team':'all','amount':'100cr'})
    apply(site.client,token,sid,'clock',{'seconds':'120','policy':'pause'})
    apply(site.client,token,sid,'bind',{'chat_id':'-100989901'})
    apply(site.client,token,sid,'start')
    owner,owner_token=identity(site,OWNER);other,other_token=identity(site,OTHER)
    root=next(p for p in site.A.lots(site.db,sid) if p.name=='Website Player')
    quote=api(owner,owner_token,sid,action='draft_pick',player=f'#{root.id}').json['confirmation']['token']
    assert api(owner,owner_token,sid,action='draft_pick_answer',token=quote,accept=True).status_code==200
    skip=api(other,other_token,sid,action='draft_skip').json['confirmation']['token']
    assert api(other,other_token,sid,action='draft_skip_answer',token=skip,accept=False).status_code==200
    skip=api(other,other_token,sid,action='draft_skip').json['confirmation']['token']
    assert api(other,other_token,sid,action='draft_skip_answer',token=skip,accept=True).status_code==200
    assert api(other,other_token,sid,action='draft_skip_answer',token=skip,accept=True).status_code==400
    apply(site.client,token,sid,'pause')
    apply(site.client,token,sid,'undo',reason='Restore the skipped turn')
    apply(site.client,token,sid,'undo',reason='Wrong player card selected')
    site.db.expire_all();north=site.A.franchises(site.db,sid)[0]
    assert north.purse_remaining_lakh==site.A.ledger_total(site.db,north.id)==10000 and north.squad_size==0
    assert site.db.query(FranchiseDraftPick).filter_by(season_id=sid).count()==0
    assert root.status==site.A.LOT_QUEUED
    assert api(site.client,token,sid,action='draft_preview',operation='prices',values={'text':'85:11cr\n95:20cr\n90:15cr\n88:12cr'}).status_code==400
    apply(site.client,token,sid,'prices',{'text':'85:11cr\n95:20cr\n90:15cr\n88:12cr'},reason='Approved published price correction')
    resumed=api(site.client,token,sid,action='draft_resume')
    assert resumed.status_code==200 and 'confirmation' not in resumed.json
    site.db.expire_all();assert D.turn(site.db,season).id==north.id
    assert D.price_for(site.db,season,root)==1100


def test_website_admin_pick_skip_and_pause_recheck_real_authority(site):
    from services import franchise_draft as D
    from models import Player,FranchiseDraftPick
    season=site.season
    D.enable(site.db,season,rounds=1,by_tg_id=ADMIN)
    player=Player(name='Second Player',rating=85,category='Batsman',country='India',version='Base',
                  bat_rating=85,bowl_rating=50,bat_hand='Right',bowl_hand='Right',bowl_style='Medium Pacer',is_active=True)
    site.db.add(player);site.db.flush();site.A.add_players_to_pool(site.db,season,[player])
    D.set_rating_prices(site.db,season,'85:10cr',by_tg_id=ADMIN)
    D.set_order(site.db,season,[site.owner.id,site.other.id],by_tg_id=ADMIN)
    D.start(site.db,season,by_tg_id=ADMIN);site.db.commit()
    token=login(site,ADMIN)
    values={'team_id':site.owner.id,'player':'Website Player'}
    quote=stage(site.client,token,season.id,'adminpick',values,'Owner unavailable')
    owner,owner_token=identity(site,OWNER)
    assert api(owner,owner_token,season.id,action='draft_answer',token=quote,accept=True,trusted_admin=True).status_code==403
    assert api(owner,owner_token,season.id,action='draft_preview',operation='adminskip',values={'team_id':site.owner.id},reason='Forbidden',trusted_admin=True).status_code==403
    site.db.expire_all();assert site.owner.squad_size==0
    approve(site.client,token,season.id,quote)
    site.db.expire_all();assert site.owner.squad_size==1
    assert site.db.query(FranchiseDraftPick).one().actor_tg_id==ADMIN
    apply(site.client,token,season.id,'pause')
    apply(site.client,token,season.id,'adminskip',{'team_id':site.other.id},'Owner unavailable')
    site.db.expire_all()
    assert season.status==site.A.STATUS_PAUSED
    assert D.skipped_turns(site.db,season)[0]['franchise_id']==site.other.id


def test_only_global_admins_can_appoint_draft_admins_from_the_website(site):
    from services import franchise_draft as D
    season=site.A.create_season(site.db,'Appointments');D.enable(site.db,season,rounds=1);site.db.commit()
    token=login(site,ADMIN)
    apply(site.client,token,season.id,'draftadmin_add',{'telegram_id':'981010','name':'New Draft Organiser'})
    client,delegate_token=identity(site,981010)
    assert api(client,delegate_token,season.id,part='settings').json['draft']['can_manage']
    denied=api(client,delegate_token,season.id,action='draft_preview',operation='draftadmin_add',values={'telegram_id':'981011'})
    assert denied.status_code==400 and not D.is_draft_admin(site.db,981011)
    apply(site.client,token,season.id,'draftadmin_remove',{'telegram_id':'981010'})
    assert api(client,delegate_token,season.id,part='settings').status_code==403


def test_admin_website_continues_registered_tournament_with_empty_squads_and_all_prior_players(page):
    from models import AuctionSeason,AuctionLot
    c=page.c
    response=page.client.post('/api/admin/franchise-draft',json={'request_type':'action','action':'draft_create',
        'name':'Next CMU draft','rounds':'2','purse':'125.5','source':f'tournament:{c.tour.id}'})
    assert response.status_code==200,response.json
    sid=response.json['season_id'];season=c.db.get(AuctionSeason,sid)
    linked=c.LS.season_for_auction(c.db,sid)
    assert linked.series_id==c.series.id and linked.season_no==2 and linked.squad_mode==c.LS.MODE_DRAFT
    teams=c.A.franchises(c.db,sid)
    assert [t.owner_tg_id for t in teams]==[101,102]
    assert all(t.squad_size==t.retained_count==0 and t.purse_remaining_lakh==12550 for t in teams)
    assert c.db.query(AuctionLot).filter_by(season_id=sid).count()==0
    catalogue=page.client.post('/api/admin/franchise-draft',json={'season_id':sid,'part':'catalogue'}).json
    assert {p['id'] for p in catalogue['catalogue']}=={p.id for p in c.players}
    assert all(p['status']=='not_in_pool' for p in catalogue['catalogue'])


def test_website_recovers_an_expired_approved_timer_once_without_the_bot_worker(site):
    from datetime import datetime,timedelta
    from models import Player
    from services import franchise_draft as D
    season=site.A.create_season(site.db,'Website clock recovery');D.enable(site.db,season,rounds=1)
    one=site.A.create_franchise(site.db,season,'Clock North',owner_tg_id=OWNER)
    two=site.A.create_franchise(site.db,season,'Clock South',owner_tg_id=OTHER)
    player=Player(name='Second clock player',rating=85,category='Batsman',country='India',version='Base',is_active=True,
                  bat_hand='Right',bowl_hand='Right',bowl_style='Medium Pacer',bat_rating=85,bowl_rating=80)
    site.db.add(player);site.db.flush()
    original=site.A.lots(site.db,site.season.id)[0]
    site.A.add_players_to_pool(site.db,season,[player,site.db.get(Player,original.player_id)])
    D.set_rating_prices(site.db,season,'85:10cr')
    D.set_timer(site.db,season,1,timeout_policy='skip')
    site.A.bind_chat(site.db,season,-100989902)
    D.start(site.db,season,now=datetime.utcnow()-timedelta(minutes=2));site.db.commit()
    owner,token=identity(site,OWNER)
    assert api(owner,token,season.id).status_code==200
    site.db.expire_all()
    assert D.turn(site.db,season).id==two.id and len(D.skipped_turns(site.db,season))==1
    assert one.purse_remaining_lakh==two.purse_remaining_lakh==10000
    assert api(owner,token,season.id).status_code==200
    site.db.expire_all();assert len(D.skipped_turns(site.db,season))==1
