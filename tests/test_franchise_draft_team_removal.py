"""Mid-draft removal preserves pending turns, exact money and an audit boundary."""
import json
from datetime import datetime,timedelta

import pytest

from test_franchise_draft_handlers import ADMIN,OWNER,OTHER,CHAT,room,live  # noqa: F401
from test_franchise_draft_v2 import command,accept
from test_franchise_draft import room as rotation_room  # noqa: F401


def third_team(room):
    team=room.D.add_team(room.db,room.season,'Team Three',884010,by_tg_id=ADMIN)
    room.db.commit()
    return team


def sign(room,name):
    team=room.D.turn(room.db,room.season)
    quote=room.D.preview_pick(room.db,room.season,team.owner_tg_id,name)
    room.D.confirm_pick(room.db,room.season,quote.token,team.owner_tg_id)
    room.db.commit()


def remove(room,name='Team One',reason='Franchise withdrew'):
    packet=command(room,'dremoveteam',name+' | '+reason)
    accept(room,packet)


@pytest.mark.parametrize('foreign_keys',[False,True])
def test_live_removal_previews_then_releases_players_and_shares_only_unspent_purse(room,foreign_keys):
    if foreign_keys:
        room.db.connection().exec_driver_sql('PRAGMA foreign_keys=ON')
    three=third_team(room);live(room);sign(room,'Root')
    removed_id=room.one.id
    root=room.D.find_player(room.db,room.season,'Joe Root',available_only=False)
    packet=command(room,'dremoveteam','Team One | Franchise withdrew')
    assert 'Joe Root' in packet['text'] and '77 Cr' in packet['text'] and '38.5 Cr' in packet['text']
    assert root.status==room.A.LOT_SOLD and room.two.purse_remaining_lakh==10000
    accept(room,packet)
    assert room.db.get(room.models.AuctionFranchise,removed_id) is None
    assert root.status==room.A.LOT_QUEUED and root.sold_to_id is None and root.sold_price_lakh is None
    assert room.D.price_for(room.db,room.season,root)==2300
    assert room.two.purse_remaining_lakh==three.purse_remaining_lakh==13850
    assert all(room.A.ledger_total(room.db,t.id)==t.purse_remaining_lakh for t in (room.two,three))
    assert room.D.turn(room.db,room.season).id==room.two.id
    event=room.db.query(room.models.AuctionEvent).filter_by(kind='fd_team_removed').one()
    detail=json.loads(event.detail_json)
    assert event.by_tg_id==ADMIN and detail['purse_basis']=='remaining' and detail['pot_lakh']==7700
    assert detail['players'][0]['name']=='Joe Root'
    assert detail['franchise_draft_picks'][0]['actor_tg_id']==OWNER
    assert sum(detail['shares'].values())==7700
    assert detail['auction_ledger'][-1]['amount_lakh']==-2300
    with pytest.raises(room.A.AuctionError):accept(room,packet)
    room.db.rollback()
    assert room.two.purse_remaining_lakh==13850
    sign(room,'Root')
    assert root.sold_to_id==room.two.id and room.two.purse_remaining_lakh==11550
    assert room.db.query(room.models.FranchiseDraftPick).one().overall_no==2


def test_noncurrent_removal_preserves_clock_and_all_surviving_round_slots(room):
    three=third_team(room)
    room.D.set_timer(room.db,room.season,5,by_tg_id=ADMIN)
    live(room);sign(room,'Root')
    draft=room.D.get_draft(room.db,room.season)
    deadline=draft.pick_deadline_at
    grid=[t.id for t in room.D.schedule(room.db,room.season)]
    removed=three.id
    remove(room,'Team Three')
    assert draft.pick_deadline_at==deadline
    assert room.D.turn(room.db,room.season).id==room.two.id
    assert [t.id for t in room.D.schedule(room.db,room.season)]==[fid for fid in grid if fid!=removed]
    assert draft.cursor==1
    assert room.one.squad_size==1 and room.one.purse_remaining_lakh==12700
    assert room.two.purse_remaining_lakh==15000
    with room.sessions() as restarted:
        season=restarted.get(room.models.AuctionSeason,room.season.id)
        assert room.D.turn(restarted,season).id==room.two.id
        assert [t.id for t in room.D.schedule(restarted,season)]==[fid for fid in grid if fid!=removed]


def test_current_team_removal_gives_next_owner_full_clock_and_cancels_quotes(room):
    third_team(room)
    room.D.set_timer(room.db,room.season,5,by_tg_id=ADMIN)
    live(room)
    quote=room.D.preview_pick(room.db,room.season,OWNER,'Root');room.db.commit()
    token=quote.token
    draft=room.D.get_draft(room.db,room.season)
    draft.pick_deadline_at=datetime.utcnow()+timedelta(seconds=10);room.db.commit()
    remove(room)
    assert room.D.turn(room.db,room.season).id==room.two.id
    assert 290<(draft.pick_deadline_at-datetime.utcnow()).total_seconds()<=300
    with pytest.raises(room.A.AuctionError):room.D.confirm_pick(room.db,room.season,token,OWNER)
    room.db.rollback()
    assert room.two.squad_size==0


def test_odd_lakh_is_distributed_exactly_once_in_display_order(room):
    three=third_team(room)
    room.A.correct_purse(room.db,room.season,room.one,1,by_tg_id=ADMIN)
    room.db.commit();live(room)
    before=room.one.purse_remaining_lakh+room.two.purse_remaining_lakh+three.purse_remaining_lakh
    recipients=[t.id for t in room.A.franchises(room.db,room.season.id) if t.id!=room.one.id]
    remove(room)
    assert room.db.get(room.models.AuctionFranchise,recipients[0]).purse_remaining_lakh==15001
    assert room.db.get(room.models.AuctionFranchise,recipients[1]).purse_remaining_lakh==15000
    assert room.two.purse_remaining_lakh+three.purse_remaining_lakh==before


def test_paused_removal_preserves_pause_and_new_team_gets_full_saved_window(room):
    third_team(room)
    room.D.set_timer(room.db,room.season,5,by_tg_id=ADMIN);live(room)
    command(room,'dpause')
    draft=room.D.get_draft(room.db,room.season)
    draft.paused_seconds_left=10;room.db.commit()
    remove(room)
    assert room.season.status==room.A.STATUS_PAUSED
    assert draft.pick_deadline_at is None and draft.paused_seconds_left==300
    command(room,'dresume')
    assert room.season.status==room.A.STATUS_LIVE


def test_removal_cleans_skipped_queue_without_reordering_survivors(room):
    three=third_team(room);live(room)
    for actor in (OWNER,OTHER,three.owner_tg_id,OTHER,three.owner_tg_id,OWNER):
        room.D.skip_turn(room.db,room.season,actor);room.db.commit()
    before=room.D.skipped_turns(room.db,room.season)
    assert room.D.turn_info(room.db,room.season)['catch_up']
    removed=room.two.id
    remove(room,'Team Two')
    assert room.D.skipped_turns(room.db,room.season)==[r for r in before if r['franchise_id']!=removed]
    assert room.D.turn(room.db,room.season).id==room.one.id
    assert room.D.get_draft(room.db,room.season).cursor==4


def test_remaining_single_team_can_resume_finish_and_cannot_be_removed(room):
    live(room)
    remove(room)
    assert len(room.A.franchises(room.db,room.season.id))==1
    command(room,'dpause');command(room,'dresume')
    sign(room,'Root');sign(room,'Stokes')
    assert room.season.status==room.A.STATUS_COMPLETED
    assert room.two.squad_size==2


def test_undo_cannot_cross_removal_but_later_picks_undo_with_refunds(room):
    third_team(room);live(room);sign(room,'Root')
    remove(room,'Team Three')
    sign(room,'Stokes')
    command(room,'dpause')
    before=room.two.purse_remaining_lakh
    room.D.undo_last(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    assert room.two.purse_remaining_lakh==before+2300
    with pytest.raises(room.A.AuctionError,match='audit boundary'):
        room.D.undo_last(room.db,room.season,by_tg_id=ADMIN)
    room.db.rollback()
    assert room.one.squad_size==1


def test_removal_rejects_owner_copied_expired_and_changed_previews(room):
    third_team(room);live(room)
    with pytest.raises(room.A.AuctionError):command(room,'dremoveteam','Team One | Leaving',actor=OWNER)
    room.db.rollback()
    packet=command(room,'dremoveteam','Team One | Leaving')
    with pytest.raises(room.A.AuctionError):accept(room,packet,actor=OWNER)
    room.db.rollback()
    sign(room,'Root')
    with pytest.raises(room.A.AuctionError,match='changed'):accept(room,packet)
    room.db.rollback()
    assert len(room.A.franchises(room.db,room.season.id))==3
    packet=command(room,'dremoveteam','Team One | Leaving')
    accept(room,packet,yes=False)
    assert len(room.A.franchises(room.db,room.season.id))==3
    assert room.one.purse_remaining_lakh==7700


def test_website_trusted_admin_removal_uses_current_session_and_releases_players(room):
    from services import franchise_draft_web as W
    third_team(room);live(room);sign(room,'Root')
    body=dict(action='draft_preview',operation='removeteam',values={'team_id':room.one.id,'name':room.one.name},reason='Withdrawal')
    from services.auction_webapp import AccessError
    with pytest.raises(AccessError):W.perform(room.db,room.season,OWNER,body)
    room.db.rollback()
    quote=W.perform(room.db,room.season,-777,body,trusted_admin=True)['confirmation'];room.db.commit()
    with pytest.raises(room.A.AuctionError):W.perform(room.db,room.season,-778,dict(action='draft_answer',token=quote['token'],accept=True),trusted_admin=True)
    room.db.rollback()
    W.perform(room.db,room.season,-777,dict(action='draft_answer',token=quote['token'],accept=True),trusted_admin=True)
    room.db.commit()
    event=room.db.query(room.models.AuctionEvent).filter_by(kind='fd_team_removed').one()
    assert event.by_tg_id==-777
    assert room.D.find_player(room.db,room.season,'Joe Root').status==room.A.LOT_QUEUED


def test_repeated_removal_preserves_rotation_seed_and_custom_prices(room):
    three=third_team(room)
    room.D.set_custom_prices(room.db,room.season,'Joe Root:25.5cr',by_tg_id=ADMIN)
    room.db.commit();live(room);sign(room,'Root')
    ids=[t.id for t in room.D._ordered_teams(room.db,room.season)]
    remove(room)
    root=room.D.find_player(room.db,room.season,'Joe Root')
    assert room.D.price_for(room.db,room.season,root)==2550
    assert room.two.purse_remaining_lakh==three.purse_remaining_lakh==13725
    remove(room,'Team Three')
    assert room.D._loads(room.D.get_draft(room.db,room.season).settings_json,{})['rotation_seed']==ids
    assert room.two.purse_remaining_lakh==27450
    assert [t.id for t in room.D.schedule(room.db,room.season)]==[room.two.id,room.two.id]
    with pytest.raises(room.A.AuctionError,match='last franchise'):
        command(room,'dremoveteam','Team Two | Also leaving')
    room.db.rollback()


def test_removing_last_incomplete_team_finishes_survivors_and_releases_all_players(room):
    third_team(room);live(room)
    for name in ('Root','Stokes','Cummins','Buttler','Bumrah'):
        sign(room,name)
    assert room.D.turn(room.db,room.season).id==room.one.id
    remove(room)
    assert room.season.status==room.A.STATUS_COMPLETED
    assert len(room.D._available(room.db,room.season))==2
    assert room.db.query(room.models.AuctionEvent).filter_by(kind='fd_completed').count()==1


def test_expired_removal_preview_cannot_remove_a_team(room):
    third_team(room);live(room)
    packet=command(room,'dremoveteam','Team One | Leaving')
    token=packet['markup'].inline_keyboard[0][0].callback_data.split('_')[-1]
    room.db.get(room.models.FranchiseDraftAction,token).created_at=datetime.utcnow()-timedelta(minutes=11)
    room.db.commit()
    with pytest.raises(room.A.AuctionError,match='expired'):accept(room,packet)
    room.db.rollback()
    assert len(room.A.franchises(room.db,room.season.id))==3


def test_mid_release_failure_rolls_back_players_purses_and_membership(room,monkeypatch):
    third_team(room);live(room);sign(room,'Root')
    packet=command(room,'dremoveteam','Team One | Leaving')
    original=room.A._remove_franchise
    def fail(*args,**kwargs):
        original(*args,**kwargs)
        raise RuntimeError('Simulated disconnect before transaction commit')
    monkeypatch.setattr(room.A,'_remove_franchise',fail)
    with pytest.raises(RuntimeError):accept(room,packet)
    room.db.rollback()
    assert len(room.A.franchises(room.db,room.season.id))==3
    assert room.one.squad_size==1 and room.one.purse_remaining_lakh==7700
    assert room.two.purse_remaining_lakh==10000
    assert room.D.find_player(room.db,room.season,'Joe Root',available_only=False).sold_to_id==room.one.id


@pytest.mark.parametrize('completed_turns',[2,7,12])
def test_removal_filters_existing_rotation_across_rounds_and_extensions(rotation_room,completed_turns):
    r=rotation_room
    r.D.configure_rounds(r.db,r.season,3)
    r.D.start(r.db,r.season);r.db.commit()
    grid=[t.id for t in r.D.schedule(r.db,r.season)]
    for _ in range(completed_turns):
        current=r.D.turn(r.db,r.season)
        r.D.skip_turn(r.db,r.season,current.owner_tg_id);r.db.commit()
    removed=r.teams[2].id
    r.D.remove_team(r.db,r.season,removed,reason='Withdrawn');r.db.commit()
    assert [t.id for t in r.D.schedule(r.db,r.season)]==[fid for fid in grid if fid!=removed]
    draft=r.D.get_draft(r.db,r.season)
    assert draft.cursor==sum(fid!=removed for fid in grid[:completed_turns])
    r.D.pause(r.db,r.season)
    r.D.configure_rounds(r.db,r.season,4);r.db.commit()
    assert len(r.D.schedule(r.db,r.season))==16
    assert [t.id for t in r.D.schedule(r.db,r.season)[:12]]==[fid for fid in grid if fid!=removed]
