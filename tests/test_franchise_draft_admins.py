"""Draft-only appointments, complete command help, and operator workflows."""
import asyncio
import re

import pytest

from test_franchise_draft_handlers import (ADMIN, CHAT, OWNER, COOWNER, OTHER,
    MANAGER, context, room, update)  # noqa: F401

DRAFT_ADMIN = 884050
AUCTION_ADMIN = 884051


def appoint(room):
    room.D.add_draft_admin(room.db,DRAFT_ADMIN,name='Draft organiser',by_tg_id=ADMIN)
    room.db.commit()


def test_bot_admin_appoints_and_removes_a_distinct_persistent_role(room):
    from models import FranchiseDraftAdmin
    appoint(room)
    row=room.D.draft_admins(room.db)[0]
    assert row.tg_id==DRAFT_ADMIN and row.added_by_tg_id==ADMIN
    assert room.D.is_draft_admin(room.db,DRAFT_ADMIN)
    assert not room.A.is_auction_admin(room.db,DRAFT_ADMIN)
    assert room.D.is_draft_admin(room.db,ADMIN)
    assert room.A.is_auction_admin(room.db,ADMIN)
    assert not room.D.is_draft_admin(room.db,True)
    assert not room.D.is_draft_admin(room.db,'invalid')
    with room.sessions() as restarted:
        assert restarted.query(FranchiseDraftAdmin).count()==1
        assert room.D.is_draft_admin(restarted,DRAFT_ADMIN)
    room.D.remove_draft_admin(room.db,DRAFT_ADMIN,by_tg_id=ADMIN)
    room.db.commit()
    assert not room.D.is_draft_admin(room.db,DRAFT_ADMIN)
    assert room.D.is_draft_admin(room.db,ADMIN)


@pytest.mark.parametrize('actor',[DRAFT_ADMIN,AUCTION_ADMIN,OWNER,COOWNER,MANAGER,999999])
def test_only_bot_admins_can_grant_or_revoke_draft_admins(room,actor):
    appoint(room)
    room.A.add_auction_admin(room.db,AUCTION_ADMIN,by_tg_id=ADMIN)
    room.db.commit()
    for command in ('dadminadd','dadminremove'):
        with pytest.raises(room.A.AuctionError,match='Bot Owners and Bot Admins'):
            room.H.execute(room.db,actor,CHAT,room.season.id,command,str(DRAFT_ADMIN))
        room.db.rollback()
    assert room.D.is_draft_admin(room.db,DRAFT_ADMIN)


@pytest.mark.parametrize('target',[0,-1,True,1.5,'bad',10**16])
def test_draft_admin_appointments_validate_ids(room,target):
    with pytest.raises(room.A.AuctionError):
        room.D.add_draft_admin(room.db,target,by_tg_id=ADMIN)
    assert not room.D.draft_admins(room.db)


def test_auction_and_draft_permissions_do_not_leak_into_each_other(room):
    from services import auction_webapp as W
    appoint(room)
    room.A.add_auction_admin(room.db,AUCTION_ADMIN,by_tg_id=ADMIN)
    auction=room.A.create_season(room.db,'Separate auction',min_squad_size=0,max_squad_size=5)
    room.db.commit()
    assert W.admin_for(room.db,DRAFT_ADMIN,room.season)
    assert not W.admin_for(room.db,DRAFT_ADMIN,auction)
    assert not W.admin_for(room.db,AUCTION_ADMIN,room.season)
    assert W.admin_for(room.db,AUCTION_ADMIN,auction)
    for actor,season in ((DRAFT_ADMIN,auction),(AUCTION_ADMIN,room.season)):
        with pytest.raises(W.AccessError):
            W.perform(room.db,season,actor,dict(action='admin_settings',settings={'name':'Forbidden'}))
        room.db.rollback()
    for action in ('admin_create','admin_clone','admin_add_admin','admin_remove_admin'):
        with pytest.raises(room.A.AuctionError):
            W.perform(room.db,room.season,DRAFT_ADMIN,dict(action=action,name='Forbidden',telegram_id=OWNER))
        room.db.rollback()
    with pytest.raises(room.A.AuctionError,match='Draft Admins'):
        room.D.set_timer(room.db,room.season,15,by_tg_id=AUCTION_ADMIN)
    room.db.rollback()


def test_draft_admin_can_see_and_manage_drafts_without_owning_a_team(room):
    from services import auction_webapp as W
    appoint(room)
    selected,rows=W.resolve(room.db,DRAFT_ADMIN,room.season.id)
    data=W.payload(room.db,selected,DRAFT_ADMIN,rows,{'view':'admin'})
    assert data['admin'] and data['draft_admin'] and not data['auction_admin']
    assert 'draft_admin' in data['roles'] and 'Draft Admin' in data['role_labels']
    assert data['my_team_ids']==[]
    room.H.execute(room.db,DRAFT_ADMIN,CHAT,room.season.id,'dprices','96:20cr')
    room.H.execute(room.db,DRAFT_ADMIN,CHAT,room.season.id,'dtimer','8')
    room.db.commit()
    room.H.execute(room.db,DRAFT_ADMIN,CHAT,room.season.id,'dstart')
    room.db.commit()
    for command in ('dpick','dskip'):
        with pytest.raises(room.A.AuctionError,match='owner or co-owner'):
            room.H.execute(room.db,DRAFT_ADMIN,CHAT,room.season.id,command,'Joe Root' if command=='dpick' else '')
        room.db.rollback()


def test_revoking_draft_admin_blocks_old_buttons_and_stale_sessions(room):
    from models import AuctionSeason
    appoint(room)
    data=f'fd_a_{DRAFT_ADMIN}_{room.season.id}_start'
    with room.sessions() as stale:
        cached=stale.get(AuctionSeason,room.season.id)
        assert room.D.is_draft_admin(stale,DRAFT_ADMIN)
        room.D.remove_draft_admin(room.db,DRAFT_ADMIN,by_tg_id=ADMIN)
        room.db.commit()
        with pytest.raises(room.A.AuctionError,match='Draft Admins'):
            room.D.start(stale,cached,by_tg_id=DRAFT_ADMIN)
        stale.rollback()
    with pytest.raises(room.A.AuctionError,match='Draft Admins'):
        room.H.execute_callback(room.db,DRAFT_ADMIN,CHAT,room.season.id,data)
    room.db.rollback()
    assert room.season.status==room.A.STATUS_SETUP


def test_draft_admin_commands_complete_setup_through_publishing(room):
    from models import AuctionSeason,FranchiseDraftConfirmation
    from services.franchise_player_import import import_players,parse_csv
    appoint(room)
    packet=room.H.execute(room.db,DRAFT_ADMIN,DRAFT_ADMIN,None,'dnew','Admin-only draft')
    sid=packet['sid'];room.db.commit()
    season=room.db.get(AuctionSeason,sid)
    chat=CHAT-500
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dbind',str(sid))
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'drounds','1')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dteam','North | 885001')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dteam','South | 885002')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dco','North | 885003,885004')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'downer','South | 885005')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dpurse','North | 5cr')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'drolemin','BAT=1')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'drolemax','BAT=1')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dratingrule','85=1')
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dprices','85:1cr')
    rows=parse_csv(b'name,rating,role,country\nDraft Admin One,85,BAT,India\nDraft Admin Two,85,BAT,India\n')
    imported=import_players(room.db,season,rows,actor_tg_id=DRAFT_ADMIN)
    assert imported['added']==2
    room.db.commit()
    teams=room.A.franchises(room.db,sid)
    assert room.A.co_owner_ids(teams[0])==[885003,885004]
    assert teams[1].owner_tg_id==885005
    assert teams[0].purse_remaining_lakh==500
    room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dstart');room.db.commit()
    for name in ('Draft Admin One','Draft Admin Two'):
        team=room.D.turn(room.db,season)
        room.H.execute(room.db,team.owner_tg_id,chat,sid,'dpick',name);room.db.commit()
        quote=room.db.query(FranchiseDraftConfirmation).filter_by(season_id=sid,status='pending').one()
        room.H.execute_callback(room.db,team.owner_tg_id,chat,sid,'fd_y_'+quote.token);room.db.commit()
    packet=room.H.execute(room.db,DRAFT_ADMIN,chat,sid,'dpublish');room.db.commit()
    assert season.league_id and 'published' in packet['text']
    assert teams[0].purse_remaining_lakh==400


def test_pool_range_command_adds_catalogue_without_auction_admin_rights(room):
    appoint(room)
    other=room.A.create_season(room.db,'Pool draft')
    room.D.enable(room.db,other,rounds=1,by_tg_id=DRAFT_ADMIN)
    room.A.bind_chat(room.db,other,CHAT-600);room.db.commit()
    packet=room.H.execute(room.db,DRAFT_ADMIN,CHAT-600,other.id,'dpooladd','96-96')
    room.db.commit()
    assert '6 players added' in packet['text']
    assert len(room.A.lots(room.db,other.id))==6


def test_draft_admin_native_workspace_has_correct_actions_and_rechecks_forms(room,monkeypatch):
    from handlers import franchise_bot as N
    from telegram.ext import ApplicationHandlerStop
    appoint(room)
    monkeypatch.setattr(N,'get_session',room.sessions)
    packet=N.read_workspace(room.db,DRAFT_ADMIN,room.season.id,DRAFT_ADMIN,'home')
    text,markup=N.render_workspace(packet,DRAFT_ADMIN,room.season.id,'home','',context(),DRAFT_ADMIN)
    labels={b.text for row in markup.inline_keyboard for b in row}
    assert 'Draft Admin' in text and '📋 New draft' in labels
    assert '➕ New auction' not in labels
    ctx=context(room.season.id)
    asyncio.run(N._start_form(update(DRAFT_ADMIN,chat=DRAFT_ADMIN),ctx,'draftprices',room.season.id))
    room.D.remove_draft_admin(room.db,DRAFT_ADMIN,by_tg_id=ADMIN);room.db.commit()
    event=update(DRAFT_ADMIN,'96:30cr',chat=DRAFT_ADMIN)
    with pytest.raises(ApplicationHandlerStop):asyncio.run(N.prompt_handler(event,ctx))
    assert '✅' not in event.effective_message.replies[-1][0]
    assert N.PROMPT_KEY not in ctx.user_data
    room.db.expire_all()
    assert '2300' in room.D.get_draft(room.db,room.season).rating_prices_json


def test_native_bot_admin_can_appoint_draft_admin_without_a_selected_season(room,monkeypatch):
    from handlers import franchise_bot as N
    from telegram.ext import ApplicationHandlerStop
    monkeypatch.setattr(N,'get_session',room.sessions)
    ctx=context()
    asyncio.run(N._start_form(update(ADMIN,chat=ADMIN),ctx,'adddraftmanager',None))
    event=update(ADMIN,str(DRAFT_ADMIN),chat=ADMIN)
    with pytest.raises(ApplicationHandlerStop):asyncio.run(N.prompt_handler(event,ctx))
    assert '✅' in event.effective_message.replies[-1][0]
    assert room.D.is_draft_admin(room.db,DRAFT_ADMIN)


def test_admin_panel_and_appointment_commands_require_the_right_roles(room):
    appoint(room)
    assert 'Draft Admin' in room.H.execute(room.db,DRAFT_ADMIN,CHAT,room.season.id,'dadmin')['text']
    assert str(DRAFT_ADMIN) in room.H.execute(room.db,DRAFT_ADMIN,CHAT,room.season.id,'dadmins')['text']
    with pytest.raises(room.A.AuctionError):
        room.H.execute(room.db,OWNER,CHAT,room.season.id,'dadmin')
    with pytest.raises(room.A.AuctionError):
        room.H.execute(room.db,OWNER,CHAT,room.season.id,'dadmins')


def test_full_reference_contains_every_registered_draft_command_at_once(room):
    from services.franchise_bot_registry import COMMANDS
    from services.rich_message import html_parts
    expected={name for module,names,_ in COMMANDS if module=='draft' for name in names}
    for alias in ('draftcommands','dcommands','dcmds','dhelp'):
        packet=room.H.execute(room.db,OWNER,CHAT,None,alias)
        found=set(re.findall(r'/([a-z]+)',packet['text']))
        assert expected<=found
        assert all(len(part)<=4096 for part in html_parts(packet['text']))
        assert '<b>Quick start</b>' in packet['text']


def test_slash_menu_includes_every_draft_command_within_telegram_limits():
    from handlers.franchise_bot import BOT_COMMANDS
    from services.franchise_bot_registry import COMMANDS
    expected={name for module,names,_ in COMMANDS if module=='draft' for name in names}
    actual=[command.command for command in BOT_COMMANDS]
    assert expected<=set(actual)
    assert len(actual)==len(set(actual))<=100
    assert all(1<=len(command.description)<=256 for command in BOT_COMMANDS)


def test_command_button_sends_all_sections_instead_of_trimming_them(room):
    from services.franchise_bot_registry import COMMANDS
    event=update(OWNER,callback=f'fd_v_{OWNER}_{room.season.id}_commands_0')
    asyncio.run(room.H.callback_handler(event,context(room.season.id)))
    text='\n'.join(part for part,_ in event.effective_message.replies)
    expected={name for module,names,_ in COMMANDS if module=='draft' for name in names}
    assert expected<=set(re.findall(r'/([a-z]+)',text))
    assert len(event.effective_message.replies)>1


def test_draft_admin_badge_and_event_snapshot_notice_grant_and_revocation(room):
    from services.auction_updates import snapshot
    initial=snapshot(room.db,room.season.id,DRAFT_ADMIN)
    appoint(room)
    granted=snapshot(room.db,room.season.id,DRAFT_ADMIN)
    assert initial.revision!=granted.revision
    assert 'draft_admin' in granted.authority[1]
    room.D.remove_draft_admin(room.db,DRAFT_ADMIN,by_tg_id=ADMIN);room.db.commit()
    revoked=snapshot(room.db,room.season.id,DRAFT_ADMIN)
    assert granted.revision!=revoked.revision and 'draft_admin' not in revoked.authority[1]


def test_auction_conversion_requires_authority_for_both_modes(room):
    appoint(room)
    auction=room.A.create_season(room.db,'Prepared auction',min_squad_size=0,max_squad_size=5)
    room.A.bind_chat(room.db,auction,CHAT-700);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='Auction Managers'):
        room.H.execute(room.db,DRAFT_ADMIN,CHAT-700,auction.id,'dsetup','2')
    room.db.rollback()
    assert room.D.get_draft(room.db,auction) is None
    room.A.add_auction_admin(room.db,DRAFT_ADMIN,by_tg_id=ADMIN);room.db.commit()
    room.H.execute(room.db,DRAFT_ADMIN,CHAT-700,auction.id,'dsetup','2');room.db.commit()
    assert room.D.get_draft(room.db,auction).rounds==2


def test_revoked_admin_who_still_owns_a_team_loses_pending_admin_form(room,monkeypatch):
    from handlers import franchise_bot as N
    from telegram.ext import ApplicationHandlerStop
    monkeypatch.setattr(N,'get_session',room.sessions)
    room.D.add_draft_admin(room.db,OWNER,by_tg_id=ADMIN);room.db.commit()
    ctx=context(room.season.id)
    asyncio.run(N._start_form(update(OWNER,chat=OWNER),ctx,'draftprices',room.season.id))
    room.D.remove_draft_admin(room.db,OWNER,by_tg_id=ADMIN);room.db.commit()
    event=update(OWNER,'96:30cr',chat=OWNER)
    with pytest.raises(ApplicationHandlerStop):asyncio.run(N.prompt_handler(event,ctx))
    assert N.PROMPT_KEY not in ctx.user_data
    assert 'Draft Admins' in event.effective_message.replies[-1][0]
    assert '✅' not in event.effective_message.replies[-1][0]


def test_draft_admin_next_season_form_can_create_from_previous_league(room):
    from handlers import franchise_bot as N
    from models import AuctionSeason,ChallengeLeague,ChallengeTeam
    appoint(room)
    mode=room.A._ensure_default_mode(room.db)
    league=ChallengeLeague(mode_id=mode.id,name='Previous draft competition',is_active=True)
    room.db.add(league);room.db.flush()
    room.db.add_all([ChallengeTeam(league_id=league.id,name=name) for name in ('North','South')])
    room.db.commit()
    result=N.execute_action(room.db,DRAFT_ADMIN,None,DRAFT_ADMIN,'native_draft_from_league',
                            {'league_id':league.id,'name':'Next draft','rounds':2,'purse_lakh':20000})
    room.db.commit()
    season=room.db.get(AuctionSeason,result['season_id'])
    assert season.previous_league_id==league.id
    assert season.opening_purse_lakh==20000 and room.D.get_draft(room.db,season).rounds==2
    assert all(t.purse_remaining_lakh==20000 for t in room.A.franchises(room.db,season.id))
