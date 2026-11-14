"""Administrator turns use the same atomic signing and deferred-pick ledger."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from test_franchise_draft_handlers import ADMIN, OWNER, OTHER, CHAT, room, live  # noqa: F401
from test_franchise_draft_v2 import command, accept


def test_admin_signing_is_previewed_durable_audited_and_debited_once(room):
    live(room)
    packet=command(room,'dapick','Team One | Joe Root | Owner unavailable')
    assert 'Joe Root' in packet['text'] and '23 Cr' in packet['text']
    assert room.one.squad_size==0 and room.one.purse_remaining_lakh==10000
    with room.sessions() as restarted:
        token=packet['markup'].inline_keyboard[0][0].callback_data.split('_')[-1]
        assert restarted.get(room.models.FranchiseDraftAction,token).status=='pending'
    accept(room,packet)
    assert room.one.squad_size==1 and room.one.purse_remaining_lakh==7700
    assert room.A.ledger_total(room.db,room.one.id)==7700
    assert room.D.turn(room.db,room.season).id==room.two.id
    pick=room.db.query(room.models.FranchiseDraftPick).one()
    assert pick.actor_tg_id==ADMIN
    record=room.db.query(room.models.AuctionEvent).filter_by(kind='fd_picked').one()
    assert record.by_tg_id==ADMIN
    detail=json.loads(record.detail_json)
    assert detail['admin_override'] and detail['reason']=='Owner unavailable'
    assert 'Administrator signing' in detail['html']
    with pytest.raises(room.A.AuctionError):accept(room,packet)
    room.db.rollback()
    assert room.one.purse_remaining_lakh==7700


@pytest.mark.parametrize('name,raw',[
    ('dapick','Team One | Joe Root | Helping owner'),
    ('daskip','Team One | Owner unavailable'),
])
def test_admin_turn_controls_reject_owners_and_copied_confirmations(room,name,raw):
    live(room)
    with pytest.raises(room.A.AuctionError):command(room,name,raw,actor=OWNER)
    room.db.rollback()
    packet=command(room,name,raw)
    with pytest.raises(room.A.AuctionError):accept(room,packet,actor=OWNER)
    room.db.rollback()
    accept(room,packet,yes=False)
    assert room.one.squad_size==0 and room.one.purse_remaining_lakh==10000
    assert room.D.turn(room.db,room.season).id==room.one.id


def test_revoked_admin_cannot_confirm_even_when_they_own_the_team(room):
    room.D.add_draft_admin(room.db,OWNER,by_tg_id=ADMIN);room.db.commit()
    live(room)
    packet=command(room,'dapick','Team One | Joe Root | Assistance',actor=OWNER)
    room.D.remove_draft_admin(room.db,OWNER,by_tg_id=ADMIN);room.db.commit()
    with pytest.raises(room.A.AuctionError):accept(room,packet,actor=OWNER)
    room.db.rollback()
    assert room.one.squad_size==0 and room.one.purse_remaining_lakh==10000


@pytest.mark.parametrize('name,raw',[
    ('dapick','Team Two | Joe Root | Wrong team'),
    ('daskip','Team Two | Wrong team'),
    ('dapick','Team One | Joe Root'),
    ('daskip','Team One |'),
])
def test_override_requires_current_team_and_reason(room,name,raw):
    live(room)
    with pytest.raises(room.A.AuctionError):command(room,name,raw)
    room.db.rollback()
    assert room.D.turn(room.db,room.season).id==room.one.id


def test_owner_pick_invalidates_competing_admin_action(room):
    live(room)
    packet=command(room,'dapick','Team One | Joe Root | Assistance')
    quote=room.D.preview_pick(room.db,room.season,OWNER,'Ben Stokes');room.db.commit()
    room.D.confirm_pick(room.db,room.season,quote.token,OWNER);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='changed'):accept(room,packet)
    room.db.rollback()
    assert room.one.purse_remaining_lakh==7700 and room.one.squad_size==1
    assert room.db.query(room.models.FranchiseDraftPick).count()==1


@pytest.mark.parametrize('restriction',['category','rating'])
def test_admin_signing_cannot_bypass_mandatory_squad_slots(room,restriction):
    for lot in room.A.lots(room.db,room.season.id):
        if lot.name in ('Pat Cummins','Jasprit Bumrah'):
            if restriction=='category':lot.category='Bowler'
            else:lot.rating=85
    if restriction=='category':
        room.D.update_squad_rules(room.db,room.season,minimums={'Bowler':1},by_tg_id=ADMIN)
    else:
        room.D.set_rating_prices(room.db,room.season,'96:23cr\n85:10cr',by_tg_id=ADMIN)
        room.D.update_squad_rules(room.db,room.season,ratings=[{'max_rating':85,'min_players':1}],by_tg_id=ADMIN)
    room.db.commit();live(room)
    for actor,name in ((OWNER,'Joe Root'),(OTHER,'Ben Stokes')):
        quote=room.D.preview_pick(room.db,room.season,actor,name)
        room.D.confirm_pick(room.db,room.season,quote.token,actor);room.db.commit()
    assert room.D.turn(room.db,room.season).id==room.two.id
    with pytest.raises(room.A.AuctionError):command(room,'dapick','Team Two | Virat Kohli | Owner requested it')
    room.db.rollback()
    assert room.two.squad_size==1 and room.two.purse_remaining_lakh==7700


def test_admin_pick_still_obeys_budget_and_expired_clock(room):
    room.D.set_timer(room.db,room.season,5,by_tg_id=ADMIN)
    live(room)
    packet=command(room,'dapick','Team One | Joe Root | Assistance')
    draft=room.D.get_draft(room.db,room.season)
    draft.pick_deadline_at=datetime.utcnow()-timedelta(seconds=1);room.db.commit()
    with pytest.raises(room.A.AuctionError):accept(room,packet)
    room.db.rollback()
    draft.pick_deadline_at=datetime.utcnow()+timedelta(minutes=5)
    room.one.purse_remaining_lakh=1;room.db.commit()
    with pytest.raises(room.A.AuctionError):command(room,'dapick','Team One | Joe Root | Assistance')
    room.db.rollback()
    assert room.one.squad_size==0 and room.one.purse_remaining_lakh==1


def test_admin_skip_keeps_pause_and_gives_next_team_full_window(room):
    room.D.set_timer(room.db,room.season,5,by_tg_id=ADMIN)
    live(room)
    command(room,'draftpause')
    with pytest.raises(room.A.AuctionError):command(room,'dapick','Team One | Joe Root | Assistance')
    room.db.rollback()
    packet=command(room,'daskip','Team One | Owner unavailable')
    accept(room,packet)
    draft=room.D.get_draft(room.db,room.season)
    assert room.season.status==room.A.STATUS_PAUSED
    assert draft.pick_deadline_at is None and draft.paused_seconds_left==300
    assert room.D.turn(room.db,room.season).id==room.two.id
    assert room.D.skipped_turns(room.db,room.season)[0]['franchise_id']==room.one.id
    assert room.one.purse_remaining_lakh==10000 and room.one.squad_size==0
    event=room.db.query(room.models.AuctionEvent).filter_by(kind='fd_skipped').one()
    assert event.by_tg_id==ADMIN and json.loads(event.detail_json)['reason']=='Owner unavailable'
    accept(room,command(room,'dresume'))
    assert 295 < (draft.pick_deadline_at-datetime.utcnow()).total_seconds() <= 300


def test_expired_admin_confirmation_cannot_sign(room):
    live(room)
    packet=command(room,'dapick','Team One | Joe Root | Assistance')
    token=packet['markup'].inline_keyboard[0][0].callback_data.split('_')[-1]
    room.db.get(room.models.FranchiseDraftAction,token).created_at=datetime.utcnow()-timedelta(minutes=11)
    room.db.commit()
    with pytest.raises(room.A.AuctionError,match='expired'):accept(room,packet)
    room.db.rollback()
    assert room.one.squad_size==0


def test_full_admin_reason_is_audited_without_overflowing_ledger_note(room):
    live(room)
    reason='R'*300
    accept(room,command(room,'dapick','Team One | Joe Root | '+reason))
    entry=room.db.query(room.models.AuctionLedgerEntry).filter_by(kind=room.A.LEDGER_DRAFT).one()
    assert len(entry.note)<=200
    record=room.db.query(room.models.AuctionEvent).filter_by(kind='fd_picked').one()
    assert json.loads(record.detail_json)['reason']==reason


def test_draft_schedule_and_board_queries_do_not_grow_with_rounds(room):
    counts=[]
    for rounds in (2,100):
        room.D.configure_rounds(room.db,room.season,rounds,by_tg_id=ADMIN);room.db.commit()
        statements=[]
        def trace(*args):statements.append(args[2])
        event.listen(room.engine,'before_cursor_execute',trace)
        try:
            with room.sessions() as db:
                season=db.get(room.models.AuctionSeason,room.season.id)
                grid=room.D.schedule(db,season)
                assert [t.id for t in grid[:4]]==[room.one.id,room.two.id,room.two.id,room.one.id]
                room.H.execute(db,OWNER,CHAT,season.id,'dboard')
        finally:event.remove(room.engine,'before_cursor_execute',trace)
        counts.append(len(statements))
    assert counts[0]==counts[1]


def test_native_draft_home_skips_auction_arena_work(room,monkeypatch):
    from handlers import franchise_bot as H
    from services import auction_webapp as W
    def unexpected(*args,**kwargs):raise AssertionError('Draft home should not build auction arena')
    monkeypatch.setattr(W,'payload',unexpected)
    result=H.read_workspace(room.db,OWNER,room.season.id,CHAT,'home')
    assert result['franchise_draft']['rounds']==2
    assert result['counts']['queued']==6 and len(result['teams'])==2


def test_website_admin_uses_same_preview_and_can_not_promote_owner(room):
    from services import franchise_draft_web as W
    live(room)
    body=dict(action='draft_preview',operation='adminpick',values={'team_id':room.one.id,'player':'Joe Root'},reason='Owner unavailable')
    from services.auction_webapp import AccessError
    with pytest.raises(AccessError):W.perform(room.db,room.season,OWNER,body)
    room.db.rollback()
    # A negative actor is supplied only by the authenticated password-admin route.
    quote=W.perform(room.db,room.season,-98765,body,trusted_admin=True)['confirmation']
    room.db.commit()
    W.perform(room.db,room.season,-98765,dict(action='draft_answer',token=quote['token'],accept=True),trusted_admin=True)
    room.db.commit()
    assert room.one.purse_remaining_lakh==7700
    assert room.db.query(room.models.FranchiseDraftPick).one().actor_tg_id==-98765
