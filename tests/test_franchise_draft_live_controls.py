"""Live timer changes, immediate resumes and unambiguous short-name picking."""
from datetime import datetime,timedelta

import pytest

from test_franchise_draft_handlers import ADMIN,OWNER,OTHER,CHAT,room,live  # noqa: F401
from test_franchise_draft_v2 import command,accept


@pytest.mark.parametrize('name',['Joe','Root','root','  ROOT  ','Roo','Jo Ro','Joe Root'])
def test_short_name_quote_keeps_normal_confirmation_and_debit(room,name):
    live(room)
    packet=command(room,'dpick',name,actor=OWNER)
    assert 'Joe Root' in packet['text']
    assert room.one.purse_remaining_lakh==10000 and room.one.squad_size==0
    accept(room,packet,actor=OWNER)
    assert room.one.purse_remaining_lakh==7700 and room.one.squad_size==1


@pytest.mark.parametrize('version',[False,True])
def test_ambiguous_name_shows_exact_player_buttons_and_rechecks_actor(room,version):
    extra=next(p for p in room.A.lots(room.db,room.season.id) if p.name=='Ben Stokes')
    extra.name='Joe Root' if version else 'Joe Denly'
    extra.version='Legend' if version else 'Base'
    room.db.commit();live(room)
    packet=command(room,'dpick','Joe',actor=OWNER)
    picks=[b.callback_data for row in packet['markup'].inline_keyboard for b in row if b.callback_data.startswith('fd_p_')]
    assert len(picks)==2 and '2 available cards match' in packet['text']
    assert room.db.query(room.models.FranchiseDraftConfirmation).count()==0
    with pytest.raises(room.A.AuctionError):
        room.H.execute_callback(room.db,OTHER,CHAT,room.season.id,picks[0])
    room.db.rollback()
    quote=room.H.execute_callback(room.db,OWNER,CHAT,room.season.id,picks[0]);room.db.commit()
    assert quote['markup'].inline_keyboard[0][0].callback_data.startswith('fd_y_')
    accept(room,quote,actor=OWNER)
    assert room.one.purse_remaining_lakh==7700


def test_short_name_matches_available_cards_without_hiding_remaining_player(room):
    extra=next(p for p in room.A.lots(room.db,room.season.id) if p.name=='Ben Stokes')
    extra.name='Joe Denly';room.db.commit();live(room)
    quote=room.D.preview_pick(room.db,room.season,OWNER,'Root')
    room.D.confirm_pick(room.db,room.season,quote.token,OWNER);room.db.commit()
    quote=room.D.preview_pick(room.db,room.season,OTHER,'Joe')
    assert quote.lot_id==extra.id
    with pytest.raises(room.A.AuctionError,match='unavailable'):
        room.D.preview_pick(room.db,room.season,OTHER,'Joe Root')


def test_live_timer_change_resets_active_window_persists_and_invalidates_old_quotes(room):
    room.D.set_timer(room.db,room.season,5,by_tg_id=ADMIN);live(room)
    quote=room.D.preview_pick(room.db,room.season,OWNER,'Root');room.db.commit()
    now=datetime.utcnow()
    room.D.configure_clock(room.db,room.season,900,by_tg_id=ADMIN,now=now);room.db.commit()
    draft=room.D.get_draft(room.db,room.season)
    assert draft.pick_deadline_at==now+timedelta(minutes=15)
    assert room.season.status==room.A.STATUS_LIVE and room.D.turn(room.db,room.season).id==room.one.id
    with pytest.raises(room.A.AuctionError,match='stale'):
        room.D.confirm_pick(room.db,room.season,quote.token,OWNER)
    room.db.rollback()
    with room.sessions() as restarted:
        saved=room.D.get_draft(restarted,room.season.id)
        assert saved.pick_deadline_at==now+timedelta(minutes=15)
        assert room.D.timer_seconds(saved)==900
    accept(room,command(room,'dpick','Root',actor=OWNER),actor=OWNER)
    assert 895<(draft.pick_deadline_at-datetime.utcnow()).total_seconds()<=900


def test_live_timer_commands_preserve_policy_and_do_not_allow_owner_changes(room):
    room.D.set_timer(room.db,room.season,5,by_tg_id=ADMIN,timeout_policy='skip');live(room)
    command(room,'dtimer','8')
    draft=room.D.get_draft(room.db,room.season)
    assert room.D.timer_seconds(draft)==480
    assert room.D._loads(draft.settings_json,{})['timeout_policy']=='skip'
    for name,raw in [('dtimer','15'),('dclock','30s | auto')]:
        with pytest.raises(room.A.AuctionError):command(room,name,raw,actor=OWNER)
        room.db.rollback()
    preview=command(room,'dclock','30s | pause')
    assert room.D.timer_seconds(draft)==480
    accept(room,preview)
    assert room.D.timer_seconds(draft)==30 and room.season.status==room.A.STATUS_LIVE
    assert 25<(draft.pick_deadline_at-datetime.utcnow()).total_seconds()<=30
    command(room,'dtimer','0')
    assert draft.pick_deadline_at is None
    assert room.D.resolve_timeout(room.db,room.season,now=datetime.utcnow()+timedelta(days=1)) is None


def test_duration_change_preserves_legacy_timeout_policy(room):
    live(room)
    draft=room.D.get_draft(room.db,room.season)
    settings=room.D._loads(draft.settings_json,{})
    settings.pop('timeout_policy',None)
    draft.settings_json=room.D._dump(settings);room.db.commit()
    command(room,'dtimer','10')
    assert room.D._loads(draft.settings_json,{})['timeout_policy']=='skip'


def test_timer_change_expires_ambiguous_player_buttons_without_signing(room):
    extra=next(p for p in room.A.lots(room.db,room.season.id) if p.name=='Ben Stokes')
    extra.name='Joe Denly';room.db.commit();live(room)
    packet=command(room,'dpick','Joe',actor=OWNER)
    choice=packet['markup'].inline_keyboard[0][0].callback_data
    command(room,'dtimer','8')
    with pytest.raises(room.A.AuctionError,match='stale'):
        room.H.execute_callback(room.db,OWNER,CHAT,room.season.id,choice)
    room.db.rollback()
    assert room.one.squad_size==0 and room.one.purse_remaining_lakh==10000


def test_resume_is_immediate_owner_protected_and_only_emits_one_announcement(room):
    room.D.set_timer(room.db,room.season,5,by_tg_id=ADMIN);live(room)
    command(room,'dpause')
    preview=command(room,'dclock','120s | pause');accept(room,preview)
    draft=room.D.get_draft(room.db,room.season)
    assert room.season.status==room.A.STATUS_PAUSED and draft.paused_seconds_left==120
    with pytest.raises(room.A.AuctionError):command(room,'dresume',actor=OWNER)
    room.db.rollback()
    before=room.db.query(room.models.FranchiseDraftAction).count()
    packet=command(room,'dresume')
    assert room.season.status==room.A.STATUS_LIVE
    assert room.db.query(room.models.FranchiseDraftAction).count()==before
    assert not any(b.callback_data.startswith('fd_c_') for row in packet['markup'].inline_keyboard for b in row)
    assert 115<(draft.pick_deadline_at-datetime.utcnow()).total_seconds()<=120
    assert room.db.query(room.models.AuctionEvent).filter_by(kind='fd_resumed').count()==1
    with pytest.raises(room.A.AuctionError):command(room,'dresume')
    room.db.rollback()


def test_resume_button_and_native_start_skip_launch_preview_when_paused(room):
    from handlers import franchise_bot as H
    live(room);command(room,'dpause')
    packet=room.H.execute_callback(room.db,ADMIN,CHAT,room.season.id,f'fd_a_{ADMIN}_{room.season.id}_start')
    room.db.commit()
    assert room.season.status==room.A.STATUS_LIVE and 'blocks' in packet
    command(room,'dpause')
    result=H.execute_action(room.db,ADMIN,room.season.id,CHAT,'native_fd_start',{})
    room.db.commit()
    assert room.season.status==room.A.STATUS_LIVE and 'draft_packet' not in result


def test_native_resume_callback_renders_live_board_without_another_confirmation(room,monkeypatch):
    import asyncio
    from handlers import franchise_bot as H
    from test_franchise_draft_handlers import update,context
    monkeypatch.setattr(H,'get_session',room.sessions)
    live(room);command(room,'dpause')
    event=update(ADMIN,chat=1,callback=f'fa:{ADMIN}:{room.season.id}:dgo:start')
    asyncio.run(H.callback_handler(event,context(room.season.id)))
    room.db.expire_all()
    assert room.season.status==room.A.STATUS_LIVE
    text=event.effective_message.replies[-1][0]
    assert 'Live' in text and 'That input is invalid' not in text


def test_initial_launch_still_needs_confirmation_and_resume_does_not_launch_setup(room):
    with pytest.raises(room.A.AuctionError):command(room,'dresume')
    room.db.rollback()
    packet=command(room,'dstart')
    assert room.season.status==room.A.STATUS_SETUP
    assert packet['markup'].inline_keyboard[0][0].callback_data.startswith('fd_c_')


def test_website_short_name_choices_and_immediate_resume_use_shared_rules(room):
    from services import franchise_draft_web as W
    extra=next(p for p in room.A.lots(room.db,room.season.id) if p.name=='Ben Stokes')
    extra.name='Joe Denly';room.db.commit();live(room)
    result=W.perform(room.db,room.season,OWNER,{'action':'draft_pick','player':'Joe'})
    assert len(result['selection']['players'])==2 and room.one.squad_size==0
    command(room,'dpause')
    from services.auction_webapp import AccessError
    with pytest.raises(AccessError):W.perform(room.db,room.season,OWNER,{'action':'draft_resume'})
    room.db.rollback()
    result=W.perform(room.db,room.season,-999,{'action':'draft_resume'},trusted_admin=True)
    room.db.commit()
    assert room.season.status==room.A.STATUS_LIVE and 'confirmation' not in result
    event=room.db.query(room.models.AuctionEvent).filter_by(kind='fd_resumed').one()
    assert event.by_tg_id==-999
