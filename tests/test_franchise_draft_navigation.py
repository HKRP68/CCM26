"""Draft entry points select the existing season engine and preserve access checks."""
import asyncio

import pytest

from test_franchise_draft_handlers import ADMIN, OWNER, room  # noqa: F401
from test_franchise_draft_seasons_page import page, competition  # noqa: F401
from test_franchise_website import site, login, OWNER as WEB_OWNER, OTHER as WEB_OTHER  # noqa: F401


def test_admin_franchise_sections_separate_modes_and_create_existing_draft_rows(page):
    c=page.c
    auction=c.A.create_season(c.db,'Auction season');c.db.commit()
    response=page.client.post('/franchise-drafts',data={'action':'create','name':'Draft season','rounds':'8','purse':'125.5'})
    assert response.status_code==302
    from models import AuctionSeason,FranchiseDraft
    c.db.expire_all()
    season=c.db.query(AuctionSeason).filter_by(name='Draft season').one()
    assert c.db.get(FranchiseDraft,season.id).rounds==8
    assert season.opening_purse_lakh==12550 and season.max_squad_size==8
    assert response.location.endswith(f'/auctions/{season.id}')
    draft_page=page.client.get('/franchise-drafts').text
    auction_page=page.client.get('/auctions').text
    assert 'Draft season' in draft_page and 'Auction season' not in draft_page
    assert 'Auction season' in auction_page and 'Draft season' not in auction_page
    assert 'Franchise Drafts' in auction_page and 'Create a franchise draft' in draft_page
    assert f'/auctions/{season.id}/console' not in draft_page
    detail=page.client.get(response.location).text
    assert 'Franchise draft · 8 rounds' in detail
    assert 'href="/franchise-drafts" class="active"' in detail


@pytest.mark.parametrize('rounds,purse',[('0','100'),('101','100'),('8','-1'),('bad','100')])
def test_bad_draft_creation_rolls_back_all_shared_rows(page,rounds,purse):
    from models import AuctionSeason,FranchiseDraft
    response=page.client.post('/franchise-drafts',data={'action':'create','name':'Invalid draft','rounds':rounds,'purse':purse})
    assert response.status_code==302 and response.location.endswith('/franchise-drafts')
    assert page.c.db.query(AuctionSeason).count()==page.c.db.query(FranchiseDraft).count()==0


def test_draft_section_requires_login_and_rejects_cross_mode_deletion(page):
    auction=page.c.A.create_season(page.c.db,'Keep auction');page.c.db.commit()
    page.client.post('/franchise-drafts',data={'action':'delete','season_id':auction.id,'confirm_name':auction.name})
    page.c.db.expire_all()
    assert page.c.db.get(type(auction),auction.id) is not None
    with page.client.session_transaction() as state:state.clear()
    response=page.client.post('/franchise-drafts',data={'action':'create','name':'Forbidden'})
    assert response.status_code==302 and '/login' in response.location


def test_native_home_exposes_both_modes_and_selectors_keep_the_correct_seasons(room):
    from handlers import franchise_bot as H
    from test_franchise_bot_ui import context
    auction=room.A.create_season(room.db,'Other auction');room.db.commit()
    data=H.read_workspace(room.db,ADMIN,auction.id,1,'home')
    _,markup=H.render_workspace(data,ADMIN,auction.id,'home','',context(),1)
    labels=[b.text for row in markup.inline_keyboard for b in row]
    assert '🔨 Franchise Auction' in labels and '🏏 Franchise Draft' in labels
    drafts=H.read_workspace(room.db,ADMIN,999999,1,'draftseasons')
    auctions=H.read_workspace(room.db,ADMIN,room.season.id,1,'auctionseasons')
    assert [s['id'] for s in drafts['auctions']]==[room.season.id]
    assert [s['id'] for s in auctions['auctions']]==[auction.id]
    # Viewing either list never converts a season or copies squads.
    assert room.D.get_draft(room.db,auction) is None
    assert room.one.squad_size==0


def test_native_draft_deep_link_opens_the_existing_draft_board(room,monkeypatch):
    from handlers import franchise_bot as H
    from test_franchise_bot_ui import context,update
    monkeypatch.setattr(H,'get_session',room.sessions)
    ctx=context();ctx.args=[f'draft_{room.season.id}']
    event=update(actor=OWNER)
    asyncio.run(H.menu_handler(event,ctx))
    assert 'CMU Franchise Draft 2.0' in event.effective_message.replies[-1][0]
    assert ctx.user_data['franchise_season_id']==room.season.id


def test_group_start_shows_bound_draft_and_unbound_group_can_use_existing_draft(room,monkeypatch):
    from handlers import franchise_bot as H
    from test_franchise_bot_ui import context,update
    from test_franchise_draft_handlers import CHAT
    monkeypatch.setattr(H,'get_session',room.sessions)
    ctx=context();ctx.user_data['franchise_season_id']=999999
    event=update(actor=OWNER,chat=CHAT,text='/start@DraftTestBot')
    asyncio.run(H.menu_handler(event,ctx))
    assert 'CMU Franchise Draft 2.0' in event.effective_message.replies[-1][0]
    new_chat=-100123450
    event=update(actor=ADMIN,chat=new_chat,text='/start')
    asyncio.run(H.menu_handler(event,ctx))
    assert f'/dbind {room.season.id}' in event.effective_message.replies[-1][0]
    packet=room.H.execute(room.db,ADMIN,new_chat,None,'dbind',str(room.season.id))
    room.db.commit()
    assert packet['sid']==room.season.id
    event=update(actor=OWNER,chat=new_chat,text='/start')
    asyncio.run(H.menu_handler(event,ctx))
    assert 'CMU Franchise Draft 2.0' in event.effective_message.replies[-1][0]
    packet=room.H.execute(room.db,ADMIN,new_chat,None,'dstart')
    from services import franchise_draft_admin as M
    quote=room.db.query(__import__('models').FranchiseDraftAction).filter_by(kind='start',status='pending').one()
    M.answer(room.db,room.season,ADMIN,quote.token,accept=True)
    assert room.season.status==room.A.STATUS_LIVE


@pytest.mark.parametrize('username_setting',['FRANCHISE_AUCTION_BOT_USERNAME','FRANCHISE_BOT_USERNAME'])
def test_authenticated_browser_draft_entry_reuses_pool_prices_and_denies_other_seasons(site,monkeypatch,username_setting):
    from services import franchise_draft as D
    from models import AuctionLot
    monkeypatch.delenv('FRANCHISE_AUCTION_BOT_USERNAME',raising=False)
    monkeypatch.delenv('FRANCHISE_BOT_USERNAME',raising=False)
    monkeypatch.setenv(username_setting,'CMUFranchiseBot')
    draft=site.A.create_season(site.db,'Browser draft')
    D.enable(site.db,draft,rounds=1)
    team=site.A.create_franchise(site.db,draft,'Draft owners',owner_tg_id=WEB_OWNER)
    old=site.A.lots(site.db,site.season.id)[0]
    from models import Player
    site.A.add_players_to_pool(site.db,draft,[site.db.get(Player,old.player_id)])
    D.set_rating_prices(site.db,draft,'85:25.5cr')
    site.db.commit()
    token=login(site)
    def request(**extra):
        return site.client.post('/api/webapp/auction',json={'mode':'draft','view':'franchisedraft',**extra},headers={'X-CSRF-Token':token})
    response=request()
    assert response.status_code==200
    data=response.json
    assert data['season']['id']==draft.id and data['season']['mode']=='draft'
    assert [s['id'] for s in data['auctions']]==[draft.id]
    assert data['draft_bot_url'].endswith(f'?start=draft_{draft.id}')
    assert data['draft_group_url'] is None
    pool=request(part='pool').json
    assert old.name in pool['draft_html'] and '25.5 Cr' in pool['draft_html']
    for part in ('board','order','squads','purses','history','rules'):
        assert request(part=part).status_code==200
    assert request(season_id=site.season.id).status_code==403
    assert request(part='squads',team_id=site.other.id).status_code==400
    site.db.expire_all()
    assert team.squad_size==0 and team.purse_remaining_lakh==team.purse_total_lakh
    assert site.db.query(AuctionLot).filter_by(season_id=draft.id,status=site.A.LOT_SOLD).count()==0
    # Another valid Telegram login still cannot see this owner's draft.
    token=login(site,WEB_OTHER)
    assert request(season_id=draft.id).status_code==403
    site.client.post('/api/franchise/auth/logout',headers={'X-CSRF-Token':token})
    assert request(season_id=draft.id).status_code==401
