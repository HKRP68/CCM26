"""Historical identity import uses the real draft teams, rights, permissions and ledger."""
import asyncio
import json
import re

import pytest

from test_franchise_draft_seasons_page import page,competition  # noqa: F401
from test_franchise_draft_handlers import room,ADMIN,OWNER  # noqa: F401


@pytest.fixture
def draft(page,monkeypatch):
    from services import franchise_draft as D,franchise_team_import as I
    from services import admin_ids
    for key in (*admin_ids.ADMIN_ID_ENV_VARS,*admin_ids.OWNER_ID_ENV_VARS):monkeypatch.setenv(key,'')
    monkeypatch.setenv('BOT_ADMIN_IDS',str(ADMIN));monkeypatch.setattr(admin_ids,'get_config',lambda:{})
    c=page.c
    season=c.A.create_season(c.db,'Import target',opening_purse_lakh=12550)
    D.enable(c.db,season,rounds=3)
    c.db.commit()
    return c,season,D,I


def test_league_import_copies_owners_and_metadata_without_players_or_old_budgets(draft):
    from models import AuctionLot,ChallengePlayer,AuctionFranchise,TournamentTeam
    c,season,D,I=draft
    c.teams[0].short_name='NOR';c.teams[0].logo_url='https://example.test/north.png'
    entry=c.db.query(TournamentTeam).filter_by(tournament_id=c.tour.id,challenge_team_id=c.teams[0].id).one()
    entry.co_owner_ids_json='[101,201,201]'
    old=c.A.create_season(c.db,'Previous auction');old.league_id=c.league.id
    was=c.A.create_franchise(c.db,old,c.teams[0].name,owner_tg_id=101,city='North City',purse_total_lakh=50000,
        team_manager_ids_json='[401]',co_owner_ids_json='[301]')
    c.db.commit()
    plan=I.preview(c.db,season,'league',c.league.id)
    assert plan['added']==2 and c.A.franchises(c.db,season.id)==[]
    I.apply(c.db,season,'league',c.league.id,fingerprint=plan['fingerprint'],revision=plan['revision'])
    c.db.commit()
    teams=c.A.franchises(c.db,season.id)
    assert [(t.owner_tg_id,json.loads(t.co_owner_ids_json)) for t in teams]==[(101,[201]),(102,[202])]
    assert teams[0].city=='North City' and teams[0].short_name=='NOR'
    assert teams[0].logo_url.endswith('north.png') and json.loads(teams[0].team_manager_ids_json)==[401]
    assert all(t.purse_total_lakh==t.purse_remaining_lakh==12550 and t.squad_size==0 and t.draft_picks_total==3 for t in teams)
    assert all(c.A.ledger_total(c.db,t.id)==12550 for t in teams)
    assert season.previous_league_id==c.league.id and season.previous_season_id==old.id
    assert teams[0].carried_from_id==was.id and teams[0].previous_team_id==c.teams[0].id
    assert c.db.query(AuctionLot).filter_by(season_id=season.id).count()==0
    assert c.db.query(ChallengePlayer).count()==2
    assert c.db.get(AuctionFranchise,was.id).purse_remaining_lakh==50000
    assert json.loads(D.get_draft(c.db,season).order_json)==[t.id for t in teams]


def test_explicit_tournament_uses_its_owners_while_league_uses_latest_entries(draft):
    from models import Tournament,TournamentTeam,ChallengeTeam
    c,season,D,I=draft
    bench=ChallengeTeam(league_id=c.league.id,name='Unplayed franchise');c.db.add(bench)
    later=Tournament(name='Later tournament',league_id=c.league.id,kind='challenge',status='completed',format='Round Robin')
    c.db.add(later);c.db.flush()
    c.db.add(TournamentTeam(tournament_id=later.id,challenge_team_id=c.teams[0].id,name='North',owner_tg_id=901,co_owner_ids_json='[]'))
    c.db.commit()
    exact=I.preview(c.db,season,'tournament',c.tour.id)
    latest=I.preview(c.db,season,'league',c.league.id)
    assert len(exact['rows'])==2 and exact['rows'][0]['final']['owner_tg_id']==101
    assert len(latest['rows'])==3 and latest['rows'][0]['final']['owner_tg_id']==901
    assert json.loads(latest['rows'][0]['final']['co_owner_ids_json'])==[]
    assert next(r for r in latest['rows'] if r['name']=='Unplayed franchise')['warnings']==['Owner missing: assign one before starting']


def test_reimport_preserves_renames_explicit_roles_purse_and_squad_ledger(draft):
    c,season,D,I=draft
    I.apply(c.db,season,'tournament',c.tour.id);c.db.commit()
    teams=c.A.franchises(c.db,season.id)
    first=teams[0];first.name='Renamed North';first.owner_tg_id=888
    first.co_owner_ids_json='[]';first.team_manager_ids_json='[]'
    c.A.correct_purse(c.db,season,first,500,note='Approved setup budget')
    first.purse_total_lakh+=500;c.db.commit()
    ledger=len(c.A.ledger(c.db,first.id))
    I.apply(c.db,season,'tournament',c.tour.id);c.db.commit()
    assert len(c.A.franchises(c.db,season.id))==2
    assert first.name=='Renamed North' and first.owner_tg_id==888
    assert first.co_owner_ids_json==first.team_manager_ids_json=='[]'
    assert first.purse_total_lakh==first.purse_remaining_lakh==13050
    assert len(c.A.ledger(c.db,first.id))==ledger and first.squad_size==0
    # A later league import still matches the exact previous-team identity.
    I.apply(c.db,season,'league',c.league.id);c.db.commit()
    assert len(c.A.franchises(c.db,season.id))==2 and first.owner_tg_id==888


def test_import_rejects_source_changes_and_is_locked_after_any_launch(draft):
    from models import TournamentTeam
    c,season,D,I=draft
    plan=I.preview(c.db,season,'tournament',c.tour.id)
    entry=c.db.query(TournamentTeam).filter_by(tournament_id=c.tour.id).first()
    entry.owner_tg_id=777;c.db.commit()
    with pytest.raises(c.A.AuctionError,match='changed'):
        I.apply(c.db,season,'tournament',c.tour.id,fingerprint=plan['fingerprint'],revision=plan['revision'])
    c.db.rollback()
    assert c.A.franchises(c.db,season.id)==[]
    config=D.get_draft(c.db,season);from datetime import datetime
    config.started_at=datetime.utcnow();c.db.commit()
    with pytest.raises(c.A.AuctionError,match='before'):
        I.apply(c.db,season,'league',c.league.id)
    c.db.rollback()


def test_league_without_owner_records_stays_explicitly_ownerless_and_cannot_borrow_another_league(draft):
    from models import ChallengeLeague,ChallengeTeam
    c,season,D,I=draft
    other=ChallengeLeague(mode_id=c.league.mode_id,name='Unowned league',tournament_command='unownedimport')
    c.db.add(other);c.db.flush();c.db.add(ChallengeTeam(league_id=other.id,name='New identity'));c.db.commit()
    I.apply(c.db,season,'league',other.id);c.db.commit()
    assert c.A.franchises(c.db,season.id)[0].owner_tg_id is None
    with pytest.raises(c.A.AuctionError,match='different league'):I.preview(c.db,season,'league',c.league.id)


@pytest.mark.parametrize('new_owner',[901,None])
def test_latest_explicit_ownership_does_not_inherit_previous_owners_managers(draft,new_owner):
    from models import TournamentTeam
    c,season,D,I=draft
    old=c.A.create_season(c.db,'Prior published season');old.league_id=c.league.id
    c.A.create_franchise(c.db,old,c.teams[0].name,owner_tg_id=101,team_manager_ids_json='[401]',co_owner_ids_json='[301]')
    entry=c.db.query(TournamentTeam).filter_by(tournament_id=c.tour.id,challenge_team_id=c.teams[0].id).one()
    entry.owner_tg_id=new_owner;entry.co_owner_ids_json='[]';c.db.commit()
    I.apply(c.db,season,'league',c.league.id);c.db.commit()
    team=c.A.franchises(c.db,season.id)[0]
    assert team.owner_tg_id==new_owner
    assert team.co_owner_ids_json==team.team_manager_ids_json=='[]'


def test_reimport_preserves_existing_retained_player_payment_and_slot(draft):
    c,season,D,I=draft
    I.apply(c.db,season,'league',c.league.id)
    c.A.add_players_to_pool(c.db,season,c.players)
    D.set_rating_prices(c.db,season,'90:5cr')
    team=c.A.franchises(c.db,season.id)[0]
    signed=c.A._sign_before_auction(c.db,season,team,c.players[0],500,
        kind=c.A.ACQ_RETAINED,counter='retained_count',ledger_kind=c.A.LEDGER_RETENTION,
        verb='retain',note='Existing approved retained contract',event_kind='fd_retained',mark='🔒',word='retained',quiet=True)
    c.db.commit()
    ledger=len(c.A.ledger(c.db,team.id))
    I.apply(c.db,season,'league',c.league.id);c.db.commit()
    assert team.squad_size==team.retained_count==1 and team.draft_picks_total==2
    assert team.purse_remaining_lakh==12050 and c.A.ledger_total(c.db,team.id)==12050
    assert c.A.squad(c.db,team.id)[0].id==signed.id and len(c.A.ledger(c.db,team.id))==ledger


def test_invalid_sources_are_rejected_and_owners_cannot_apply_an_import(draft):
    c,season,D,I=draft
    for kind,source_id in (('unknown',1),('league',2**80),('league',True),('league',999999)):
        with pytest.raises(c.A.AuctionError):I.preview(c.db,season,kind,source_id)
    with pytest.raises(c.A.AuctionError):I.apply(c.db,season,'league',c.league.id,actor=OWNER)
    assert c.A.franchises(c.db,season.id)==[]


def test_tournament_without_league_uses_recorded_team_user_and_keeps_import_aliases(draft):
    from models import Tournament,TournamentTeam
    c,season,D,I=draft
    personal=Tournament(name='Previous personal tournament',kind='letsplay',status='completed',format='Round Robin')
    c.db.add(personal);c.db.flush()
    c.db.add(TournamentTeam(tournament_id=personal.id,name='Original franchise',user_tg_id=501,co_owner_ids_json='[]'))
    c.db.commit()
    I.apply(c.db,season,'tournament',personal.id);c.db.commit()
    team=c.A.franchises(c.db,season.id)[0]
    assert team.owner_tg_id==501 and season.previous_league_id is None
    team.name='Renamed franchise'
    another=Tournament(name='Another historical tournament',kind='letsplay',status='completed',format='Round Robin')
    c.db.add(another);c.db.flush()
    c.db.add(TournamentTeam(tournament_id=another.id,name=team.name,user_tg_id=501,co_owner_ids_json='[]'))
    c.db.commit()
    I.apply(c.db,season,'tournament',another.id);c.db.commit()
    I.apply(c.db,season,'tournament',personal.id);c.db.commit()
    assert len(c.A.franchises(c.db,season.id))==1 and team.name=='Renamed franchise'
    origins=json.loads(D.get_draft(c.db,season).settings_json)['team_imports'][str(team.id)]
    assert len(origins)==2


def test_telegram_preview_is_actor_bound_and_detects_changed_source(draft,monkeypatch):
    from services import admin_ids,franchise_draft_admin as M
    from handlers.franchise_draft_v2 import route
    from models import TournamentTeam
    c,season,D,I=draft
    for key in (*admin_ids.ADMIN_ID_ENV_VARS,*admin_ids.OWNER_ID_ENV_VARS):monkeypatch.setenv(key,'')
    monkeypatch.setenv('BOT_ADMIN_IDS',str(ADMIN));monkeypatch.setattr(admin_ids,'get_config',lambda:{})
    packet=route(c.db,season,ADMIN,'dimportteams',f'tournament {c.tour.id}')
    c.db.commit()
    assert '101' in packet['text'] and '201' in packet['text'] and 'Nothing has been applied' in packet['text']
    assert c.A.franchises(c.db,season.id)==[]
    token=packet['markup'].inline_keyboard[0][0].callback_data.split('_')[-1]
    with pytest.raises(c.A.AuctionError):M.answer(c.db,season,OWNER,token,accept=True)
    c.db.rollback()
    entry=c.db.query(TournamentTeam).filter_by(tournament_id=c.tour.id).first();entry.owner_tg_id=778;c.db.commit()
    with pytest.raises(c.A.AuctionError,match='changed'):M.answer(c.db,season,ADMIN,token,accept=True)
    c.db.rollback()
    packet=route(c.db,season,ADMIN,'dimportteams',f'league {c.league.id}');c.db.commit()
    token=packet['markup'].inline_keyboard[0][0].callback_data.split('_')[-1]
    M.answer(c.db,season,ADMIN,token,accept=True);c.db.commit()
    assert len(c.A.franchises(c.db,season.id))==2
    with pytest.raises(c.A.AuctionError):M.answer(c.db,season,ADMIN,token,accept=True)
    c.db.rollback()


def test_admin_creation_can_import_previous_teams_and_existing_draft_can_preview(page):
    from models import AuctionSeason
    from services import franchise_draft as D
    c=page.c
    response=page.client.post('/franchise-drafts',data={'action':'create','name':'Imported at creation','rounds':'5','purse':'150','team_source':f'tournament:{c.tour.id}'})
    assert response.status_code==302
    season=c.db.query(AuctionSeason).filter_by(name='Imported at creation').one()
    assert [t.owner_tg_id for t in c.A.franchises(c.db,season.id)]==[101,102]
    assert all(t.purse_remaining_lakh==15000 for t in c.A.franchises(c.db,season.id))
    fresh=c.A.create_season(c.db,'Existing empty draft');D.enable(c.db,fresh,rounds=3);c.db.commit()
    url=f'/auctions/{fresh.id}'
    assert 'Preview teams &amp; owners' in page.client.get(url).text
    response=page.client.post(url,data={'action':'source_import_preview','team_source':f'league:{c.league.id}'})
    assert response.status_code==200 and '101' in response.text
    assert c.A.franchises(c.db,fresh.id)==[]
    fields={'action':'source_import_apply','team_source':f'league:{c.league.id}'}
    for field in ('import_fingerprint','import_revision'):
        fields[field]=re.search(fr'name="{field}" value="([^"]+)"',response.text)[1]
    assert page.client.post(url,data=fields).status_code==302
    c.db.expire_all()
    assert len(c.A.franchises(c.db,fresh.id))==2
    assert page.client.post(url,data=fields).status_code==302
    c.db.expire_all()
    assert len(c.A.franchises(c.db,fresh.id))==2


def test_private_source_picker_is_admin_only_and_callbacks_open_durable_preview(draft,monkeypatch):
    from handlers import franchise_bot as H
    from services import admin_ids
    from test_franchise_bot_ui import update,context
    c,season,D,I=draft
    for key in (*admin_ids.ADMIN_ID_ENV_VARS,*admin_ids.OWNER_ID_ENV_VARS):monkeypatch.setenv(key,'')
    monkeypatch.setenv('BOT_ADMIN_IDS',str(ADMIN));monkeypatch.setattr(admin_ids,'get_config',lambda:{})
    monkeypatch.setattr(H,'get_session',lambda:c.db)
    with pytest.raises(c.A.AuctionError):H.read_workspace(c.db,OWNER,season.id,1,'draftsources')
    data=H.read_workspace(c.db,ADMIN,season.id,1,'draftsources')
    text,markup=H.render_workspace(data,ADMIN,season.id,'draftsources','1',context(),1)
    assert 'Import franchises' in text
    callback=next(b.callback_data for row in markup.inline_keyboard for b in row if f'timp:tournament:{c.tour.id}' in (b.callback_data or ''))
    event=update(actor=ADMIN,callback=callback)
    asyncio.run(H.callback_handler(event,context()))
    assert 'Confirm draft configuration' in event.effective_message.replies[-1][0]
    assert c.A.franchises(c.db,season.id)==[]
