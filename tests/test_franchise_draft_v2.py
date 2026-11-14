"""Integrated Draft 2.0 workflows exercise the existing squads and ledger."""
import json
from datetime import datetime,timedelta
from types import SimpleNamespace

import pytest

from test_franchise_draft_handlers import ADMIN,OWNER,COOWNER,OTHER,CHAT,room,live  # noqa: F401
from services import franchise_draft_admin as M,franchise_draft_rich as R
from test_franchise_draft_seasons import competition  # noqa: F401


def command(room,name,raw='',actor=ADMIN):
    packet=room.H.execute(room.db,actor,CHAT,room.season.id,name,raw)
    room.db.commit()
    return packet


def accept(room,packet,actor=ADMIN,yes=True):
    callback=packet['markup'].inline_keyboard[0][0 if yes else 1].callback_data
    result=room.H.execute_callback(room.db,actor,CHAT,room.season.id,callback)
    room.db.commit()
    return result


def test_price_preview_is_durable_and_only_applies_after_authorized_confirmation(room):
    before=room.D.get_draft(room.db,room.season).rating_prices_json
    packet=command(room,'dprices','96 = 25.5 Cr\n95 : 23cr')
    assert '6 eligible players priced' in packet['text']
    assert room.D.get_draft(room.db,room.season).rating_prices_json==before
    with pytest.raises(room.A.AuctionError):accept(room,packet,actor=OWNER)
    room.db.rollback()
    accept(room,packet)
    assert json.loads(room.D.get_draft(room.db,room.season).rating_prices_json)=={'96':2550,'95':2300}
    with pytest.raises(room.A.AuctionError):accept(room,packet)
    room.db.rollback()
    with room.sessions() as resumed:
        assert all(room.D.price_for(resumed,room.season,p)==2550 for p in room.A.lots(resumed,room.season.id))


def test_cancelled_price_preview_preserves_prices_and_revision(room):
    draft=room.D.get_draft(room.db,room.season)
    revision=draft.revision
    packet=command(room,'dprices','96:30cr')
    assert draft.revision==revision
    accept(room,packet,yes=False)
    assert draft.revision==revision
    assert room.D.price_for(room.db,room.season,room.A.lots(room.db,room.season.id)[0])==2300


def test_configuration_quotes_expire_and_detect_out_of_band_budget_changes(room):
    packet=command(room,'dprices','96:25cr')
    room.one.purse_remaining_lakh-=1
    room.db.commit()
    with pytest.raises(room.A.AuctionError,match='changed'):accept(room,packet)
    room.db.rollback()
    packet=command(room,'dprices','96:25cr')
    token=packet['markup'].inline_keyboard[0][0].callback_data.split('_')[-1]
    action=room.db.get(room.models.FranchiseDraftAction,token)
    action.created_at=datetime.utcnow()-timedelta(minutes=11)
    room.db.commit()
    with pytest.raises(room.A.AuctionError,match='expired'):accept(room,packet)
    room.db.rollback()


def test_price_lock_survives_pause_and_audited_correction_is_previewed(room):
    live(room)
    room.D.pause(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='reason'):command(room,'dprices','96:25cr')
    room.db.rollback()
    packet=command(room,'dcorrect','prices | Fix the published price sheet | 96:25cr')
    assert 'Fix the published price sheet' in packet['text']
    assert room.D.price_for(room.db,room.season,room.A.lots(room.db,room.season.id)[0])==2300
    accept(room,packet)
    event=room.db.query(room.models.AuctionEvent).filter_by(kind='fd_admin_action').order_by(room.models.AuctionEvent.id.desc()).first()
    assert event.by_tg_id==ADMIN and 'Fix the published price sheet' in event.detail_json


def test_custom_override_removal_restores_rating_price(room):
    lot=room.A.lots(room.db,room.season.id)[0]
    accept(room,command(room,'dcustom',f'#{lot.id}:25.5cr'))
    assert room.D.price_for(room.db,room.season,lot)==2550
    packet=command(room,'dcustom',f'remove #{lot.id}')
    assert '25.5' in packet['text'] and '23' in packet['text']
    accept(room,packet)
    assert room.D.price_for(room.db,room.season,lot)==2300


def test_audited_price_reduction_refreshes_the_shared_signing_reserve(room):
    lot=room.A.lots(room.db,room.season.id)[0]
    accept(room,command(room,'dcustom',f'#{lot.id}:100cr'))
    live(room)
    assert room.season.min_base_price_lakh==2300
    room.D.pause(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    accept(room,command(room,'dcorrect','prices | Free remaining cards per season rules | 96:0cr'))
    command(room,'dresume')
    quote=command(room,'dpick',f'#{lot.id}',actor=OWNER)
    accept(room,quote,actor=OWNER)
    assert room.one.purse_remaining_lakh==0 and room.one.squad_size==1
    assert room.season.min_base_price_lakh==0
    assert room.A.ledger_total(room.db,room.one.id)==0


def test_missing_price_and_ambiguous_versions_cannot_be_applied(room):
    with pytest.raises(room.A.AuctionError,match='No draft price'):command(room,'dprices','95:23cr')
    room.db.rollback()
    lots=room.A.lots(room.db,room.season.id)
    lots[1].name=lots[0].name;lots[1].version='Legend'
    room.db.commit()
    with pytest.raises(room.A.AuctionError,match='several player cards'):command(room,'dcustom',lots[0].name+':25cr')
    room.db.rollback()
    accept(room,command(room,'dcustom',f'p{lots[1].player_id}:25cr'))
    assert room.D.price_for(room.db,room.season,lots[1])==2500
    assert room.D.price_for(room.db,room.season,lots[0])==2300


def test_start_and_search_buttons_use_current_ownership_and_exact_ids(room):
    packet=command(room,'dstart')
    assert room.season.status==room.A.STATUS_SETUP
    accept(room,packet)
    packet=command(room,'dsearch','Joe',actor=OWNER)
    pick=packet['markup'].inline_keyboard[0][0].callback_data
    with pytest.raises(room.A.AuctionError):room.H.execute_callback(room.db,OTHER,CHAT,room.season.id,pick)
    room.db.rollback()
    quote=room.H.execute_callback(room.db,OWNER,CHAT,room.season.id,pick)
    assert 'Joe Root' in quote['text'] and 'England' in quote['text']
    assert 'Batting / bowling' in quote['text'] and 'Current purse' in quote['text']
    assert room.one.purse_remaining_lakh==10000
    accept(room,quote,actor=OWNER)
    assert room.one.purse_remaining_lakh==7700
    with pytest.raises(room.A.AuctionError,match='stale'):room.H.execute_callback(room.db,OWNER,CHAT,room.season.id,pick)
    room.db.rollback()


@pytest.mark.parametrize('policy',['pause','admin','skip','auto'])
def test_second_timers_follow_the_approved_policy_and_survive_restart(room,policy):
    accept(room,command(room,'dclock',f'30s | {policy}'))
    live(room)
    draft=room.D.get_draft(room.db,room.season)
    assert room.D.timer_seconds(draft)==30
    expired=draft.pick_deadline_at+timedelta(seconds=1)
    sid=room.season.id
    with room.sessions() as resumed:
        season=resumed.get(room.models.AuctionSeason,sid)
        room.D.resolve_timeout(resumed,season,now=expired)
        resumed.commit()
    room.db.expire_all()
    if policy in ('pause','admin'):
        assert room.season.status==room.A.STATUS_PAUSED
        assert room.one.squad_size==0 and room.one.purse_remaining_lakh==10000
    else:
        assert room.season.status==room.A.STATUS_LIVE
        assert room.D.turn(room.db,room.season).id==room.two.id
        if policy=='auto':
            assert room.one.squad_size==1 and room.one.purse_remaining_lakh==7700
            pick=room.db.query(room.models.FranchiseDraftPick).one()
            assert pick.actor_tg_id is None
        else:assert room.one.squad_size==0


def test_new_drafts_do_not_enable_automatic_skipping_without_approval(room):
    live(room)
    draft=room.D.get_draft(room.db,room.season)
    room.D.resolve_timeout(room.db,room.season,now=draft.pick_deadline_at+timedelta(seconds=1))
    room.db.commit()
    assert room.season.status==room.A.STATUS_PAUSED
    assert not room.D.skipped_turns(room.db,room.season)


def test_pool_filters_removal_and_purse_updates_are_previewed(room):
    lots=room.A.lots(room.db,room.season.id)
    remove=command(room,'dpoolremove',f'#{lots[0].id}')
    assert lots[0].status==room.A.LOT_QUEUED
    accept(room,remove)
    assert lots[0].status!=room.A.LOT_QUEUED
    assert room.db.get(room.models.Player,lots[0].player_id) is not None
    budget=command(room,'dpurse','all | 75.5cr')
    assert room.one.purse_remaining_lakh==10000
    accept(room,budget)
    assert room.one.purse_remaining_lakh==room.two.purse_remaining_lakh==7550
    from services.franchise_draft_admin import select_players
    assert len(select_players(room.db,'96-96 | BAT | *'))==6
    assert len(select_players(room.db,f'ids {lots[0].player_id},{lots[1].player_id}'))==2


def test_full_order_history_and_shared_board_buttons_are_accessible_but_cannot_pick(room):
    from handlers.franchise_draft_v2 import public_keyboard
    room.D.configure_rounds(room.db,room.season,12,by_tg_id=ADMIN);room.db.commit()
    packet=room.H._render_view(room.db,room.season,OWNER,'order','10')
    assert 'Round 12' in packet['text']
    shared=public_keyboard(room.season).inline_keyboard[0][0].callback_data
    packet=room.H.execute_callback(room.db,9999,CHAT,room.season.id,shared)
    assert 'Available players' in packet['text']
    assert not any(b.callback_data.startswith(('fd_y_','fd_p_','fd_a_')) for row in packet['markup'].inline_keyboard for b in row)
    with pytest.raises(room.A.AuctionError):room.H.execute_callback(room.db,9999,CHAT-1,room.season.id,shared)


def test_combined_last_slot_category_rating_and_affordability_checks(room):
    lots=room.A.lots(room.db,room.season.id)
    for lot in lots[:2]:lot.category='Bowler';lot.rating=96
    for lot in lots[2:]:lot.rating=80
    room.D.set_rating_prices(room.db,room.season,'96:50cr\n80:1cr',by_tg_id=ADMIN)
    room.D.update_squad_rules(room.db,room.season,minimums={'Bowler':1},ratings=[{'max_rating':80,'min_players':2}],by_tg_id=ADMIN)
    room.db.commit()
    data=M.readiness(room.db,room.season)
    assert not data['ready'] and any('together' in e for e in data['errors'])


def test_completion_emits_summary_and_history_and_publishing_reuses_existing_league(room):
    room.D.configure_rounds(room.db,room.season,1,by_tg_id=ADMIN);room.db.commit()
    live(room)
    for actor,name in ((OWNER,'Joe Root'),(OTHER,'Ben Stokes')):
        quote=command(room,'dpick',name,actor=actor)
        accept(room,quote,actor=actor)
    assert room.season.status==room.A.STATUS_COMPLETED
    assert room.db.query(room.models.AuctionEvent).filter_by(kind='fd_completed').count()==1
    history=command(room,'dhistory',actor=OWNER)
    assert 'Draft history' in history['text']
    command(room,'dpublish')
    assert room.season.league_id


def test_existing_contract_retention_counts_slots_and_keeps_previous_season_intact(competition,monkeypatch):
    from services import franchise_draft as D,admin_ids
    from models import AuctionLot,ChallengePlayer
    for key in (*admin_ids.ADMIN_ID_ENV_VARS,*admin_ids.OWNER_ID_ENV_VARS):monkeypatch.setenv(key,'')
    monkeypatch.setenv('BOT_ADMIN_IDS',str(ADMIN))
    monkeypatch.setattr(admin_ids,'get_config',lambda:{})
    c=competition
    _,season=c.LS.start_franchise_draft(c.db,c.series,purse_lakh=10000,rounds=2,by_tg_id=ADMIN)
    c.A.bind_chat(c.db,season,CHAT)
    c.A.add_players_to_pool(c.db,season,c.players)
    D.set_rating_prices(c.db,season,'90:10cr',by_tg_id=ADMIN)
    c.db.commit()
    def apply(kind,payload):
        action=M.prepare(c.db,season,ADMIN,kind,payload)
        c.db.commit()
        M.answer(c.db,season,ADMIN,action.token,accept=True)
        c.db.commit()
    apply('retention',{'maximum':1})
    teams=c.A.franchises(c.db,season.id)
    lot=c.db.query(AuctionLot).filter_by(season_id=season.id,player_id=c.players[0].id).one()
    # A stale auction floor must not override the current draft prices.
    season.min_base_price_lakh=10000;c.db.commit()
    apply('retain',{'team_id':teams[0].id,'player':'#'+str(lot.id),'price':'5cr'})
    assert teams[0].squad_size==1 and teams[0].purse_remaining_lakh==9500
    assert teams[0].draft_picks_total==1 and teams[0].retained_count==1
    assert c.db.query(ChallengePlayer).count()==2
    # Undo uses the same ledger and restores the acquisition place.
    apply('unretain',{'team_id':teams[0].id,'player':'#'+str(lot.id)})
    assert teams[0].purse_remaining_lakh==10000 and teams[0].draft_picks_total==2
    apply('retain',{'team_id':teams[0].id,'player':'#'+str(lot.id),'price':'5cr'})
    D.start(c.db,season,by_tg_id=ADMIN);c.db.commit()
    names=[c.players[2].name,c.players[1].name,c.players[3].name]
    for name in names:
        actor=D.turn(c.db,season).owner_tg_id
        quote=D.preview_pick(c.db,season,actor,name)
        D.confirm_pick(c.db,season,quote.token,actor)
        c.db.commit()
    assert season.status==c.A.STATUS_COMPLETED
    assert [t.squad_size for t in teams]==[2,2]
    assert c.A.ledger_total(c.db,teams[0].id)==teams[0].purse_remaining_lakh==8500
    assert c.db.query(ChallengePlayer).count()==2
    c.A.publish_to_league(c.db,season)
    c.db.commit()
    assert c.db.query(ChallengePlayer).count()==6


def test_role_rating_flow_matches_exhaustive_affordable_combinations():
    from collections import Counter
    from itertools import combinations
    import random
    from services import franchise_draft as D
    randomizer=random.Random(20261113)
    roles=list(D.A.SQUAD_ROLES)
    rules=[dict(max_rating=85,min_players=1),dict(max_rating=95,min_players=2)]
    for example in range(60):
        pool=[SimpleNamespace(name=str(i),category=randomizer.choice(roles),rating=randomizer.choice([80,90,96]),cost=randomizer.randint(0,80)) for i in range(9)]
        lows={role:randomizer.randint(0,1) for role in roles}
        highs={role:randomizer.randint(lows[role],4) for role in roles}
        valid=[sum(p.cost for p in choice) for choice in combinations(pool,4)
            if all(lows[r]<=sum(p.category==r for p in choice)<=highs[r] for r in roles)
            and sum(p.rating<=85 for p in choice)>=1 and sum(p.rating<=95 for p in choice)>=2]
        if not valid:
            with pytest.raises(D.AuctionError):D._completion_flow(pool,4,Counter(),lows,highs,rules,[1,2],lambda p:p.cost)
        else:
            assert D._completion_flow(pool,4,Counter(),lows,highs,rules,[1,2],lambda p:p.cost)==min(valid),example


def test_competing_owner_and_coowner_confirmations_commit_only_one_signing(room):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    live(room)
    one=room.D.preview_pick(room.db,room.season,OWNER,'Joe Root')
    two=room.D.preview_pick(room.db,room.season,COOWNER,'Ben Stokes')
    room.db.commit()
    barrier=threading.Barrier(2)
    def confirm(token,actor):
        with room.sessions() as db:
            season=db.get(room.models.AuctionSeason,room.season.id)
            room.D.get_draft(db,season)
            barrier.wait(timeout=5)
            try:
                room.D.confirm_pick(db,season,token,actor)
                db.commit()
                return True
            except room.A.AuctionError:
                db.rollback();return False
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures=[workers.submit(confirm,one.token,OWNER),workers.submit(confirm,two.token,COOWNER)]
        results=[future.result(timeout=10) for future in futures]
    assert sorted(results)==[False,True]
    room.db.expire_all()
    assert room.one.squad_size==1 and room.one.purse_remaining_lakh==7700
    assert room.db.query(room.models.FranchiseDraftPick).count()==1
    assert room.A.ledger_total(room.db,room.one.id)==7700


def test_duration_only_edits_preserve_the_approved_timeout_policy(room):
    accept(room,command(room,'dclock','30s | auto'))
    command(room,'dtimer','5')
    draft=room.D.get_draft(room.db,room.season)
    assert room.D.timer_seconds(draft)==300
    assert json.loads(draft.settings_json)['timeout_policy']=='auto'


def test_withdrawn_pool_card_can_be_restored_by_catalogue_id(room):
    lot=room.A.lots(room.db,room.season.id)[0]
    accept(room,command(room,'dpoolremove',f'#{lot.id}'))
    accept(room,command(room,'dpooladd',f'ids {lot.player_id}'))
    assert lot.status==room.A.LOT_QUEUED
    assert len(room.A.lots(room.db,room.season.id))==6


def test_revoked_administrator_cannot_apply_a_prepared_quote(room):
    manager=90091
    room.D.add_draft_admin(room.db,manager,by_tg_id=ADMIN);room.db.commit()
    packet=command(room,'dprices','96:25cr',actor=manager)
    room.D.remove_draft_admin(room.db,manager,by_tg_id=ADMIN);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='Draft Admin'):accept(room,packet,actor=manager)
    room.db.rollback()
    assert room.D.price_for(room.db,room.season,room.A.lots(room.db,room.season.id)[0])==2300


def test_expired_turn_admin_resolution_is_audited_and_preserves_the_slot(room):
    live(room)
    room.D.pause(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    packet=command(room,'dcorrect','resolve | Owner unavailable, pick deferred | skip')
    assert room.D.turn(room.db,room.season).id==room.one.id
    accept(room,packet)
    assert room.season.status==room.A.STATUS_PAUSED
    assert room.D.turn(room.db,room.season).id==room.two.id
    assert room.D.skipped_turns(room.db,room.season)[0]['franchise_id']==room.one.id
    assert room.one.squad_size==0 and room.one.purse_remaining_lakh==10000


def test_undo_preview_refunds_once_and_preserves_audit_history(room):
    live(room)
    quote=command(room,'dpick','Joe Root',actor=OWNER)
    accept(room,quote,actor=OWNER)
    room.D.pause(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    packet=command(room,'dcorrect','undo | Wrong card selected |')
    assert room.one.purse_remaining_lakh==7700
    accept(room,packet)
    assert room.one.purse_remaining_lakh==10000 and room.one.squad_size==0
    assert room.D.turn(room.db,room.season).id==room.one.id
    assert 'Joe Root' in command(room,'dhistory',actor=OWNER)['text']
    with pytest.raises(room.A.AuctionError):accept(room,packet)
    room.db.rollback()
    assert room.A.ledger_total(room.db,room.one.id)==10000


def test_season_deletion_also_removes_pending_configuration_quotes(room):
    command(room,'dprices','96:25cr')
    assert room.db.query(room.models.FranchiseDraftAction).count()==1
    room.db.delete(room.season);room.db.commit()
    assert room.db.query(room.models.FranchiseDraftAction).count()==0


def test_pending_administrator_preview_can_be_confirmed_after_process_restart(room):
    packet=command(room,'dprices','96:25.5cr')
    callback=packet['markup'].inline_keyboard[0][0].callback_data
    with room.sessions() as resumed:
        room.H.execute_callback(resumed,ADMIN,CHAT,room.season.id,callback)
        resumed.commit()
    room.db.expire_all()
    assert room.D.price_for(room.db,room.season,room.A.lots(room.db,room.season.id)[0])==2550


@pytest.mark.parametrize('value',[-1,23.5,True])
def test_invalid_stored_prices_cannot_credit_or_silently_round_a_signing(room,value):
    live(room)
    lot=room.A.lots(room.db,room.season.id)[0]
    draft=room.D.get_draft(room.db,room.season)
    draft.custom_prices_json=json.dumps({str(lot.id):value})
    room.db.commit()
    with pytest.raises(room.A.AuctionError,match='Invalid stored price'):
        room.D.preview_pick(room.db,room.season,OWNER,'#'+str(lot.id))
    room.db.rollback()
    assert room.one.squad_size==0 and room.one.purse_remaining_lakh==10000


def test_all_retained_squads_finish_without_announcing_a_fake_live_turn(competition,monkeypatch):
    from services import franchise_draft as D,admin_ids
    from models import AuctionEvent
    for key in (*admin_ids.ADMIN_ID_ENV_VARS,*admin_ids.OWNER_ID_ENV_VARS):monkeypatch.setenv(key,'')
    monkeypatch.setenv('BOT_ADMIN_IDS',str(ADMIN))
    monkeypatch.setattr(admin_ids,'get_config',lambda:{})
    c=competition
    _,season=c.LS.start_franchise_draft(c.db,c.series,purse_lakh=10000,rounds=1,by_tg_id=ADMIN)
    c.A.bind_chat(c.db,season,CHAT)
    c.A.add_players_to_pool(c.db,season,c.players)
    D.set_rating_prices(c.db,season,'90:10cr',by_tg_id=ADMIN)
    c.db.commit()
    payloads=[('retention',{'maximum':1})]
    payloads += [('retain',{'team_id':t.id,'player':'p'+str(p.id),'price':'5cr'})
                 for t,p in zip(c.A.franchises(c.db,season.id),c.players)]
    for kind,payload in payloads:
        action=M.prepare(c.db,season,ADMIN,kind,payload);c.db.commit()
        M.answer(c.db,season,ADMIN,action.token,accept=True);c.db.commit()
    D.start(c.db,season,by_tg_id=ADMIN);c.db.commit()
    assert season.status==c.A.STATUS_COMPLETED
    assert c.db.query(AuctionEvent).filter_by(season_id=season.id,kind='fd_started').count()==0
    assert c.db.query(AuctionEvent).filter_by(season_id=season.id,kind='fd_completed').count()==1


def test_native_undo_collects_a_reason_and_shows_a_preview_before_refunding(room,monkeypatch):
    import asyncio
    from telegram.ext import ApplicationHandlerStop
    from handlers import franchise_bot as N
    from test_franchise_draft_handlers import update,context
    monkeypatch.setattr(N,'get_session',room.sessions)
    live(room)
    quote=command(room,'dpick','Joe Root',actor=OWNER);accept(room,quote,actor=OWNER)
    room.D.pause(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    ctx=context(room.season.id)
    event=update(ADMIN,chat=1)
    asyncio.run(N._start_form(event,ctx,'draftundo',room.season.id))
    reply=update(ADMIN,text='Wrong card selected',chat=1)
    with pytest.raises(ApplicationHandlerStop):asyncio.run(N.prompt_handler(reply,ctx))
    _,options=reply.effective_message.replies[-1]
    callback=options['reply_markup'].inline_keyboard[0][0].callback_data
    assert callback.startswith('fd_c_')
    room.db.expire_all()
    assert room.one.purse_remaining_lakh==7700
    room.H.execute_callback(room.db,ADMIN,1,room.season.id,callback);room.db.commit()
    assert room.one.purse_remaining_lakh==10000
