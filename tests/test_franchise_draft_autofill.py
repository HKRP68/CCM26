"""Auto draft previews and commits through the existing signing/ledger engine."""
import json
from datetime import datetime,timedelta

import pytest

from test_franchise_draft_handlers import ADMIN,OWNER,OTHER,CHAT,room,live  # noqa: F401
from test_franchise_draft_v2 import command,accept


def planned(room,packet):
    token=packet['markup'].inline_keyboard[0][0].callback_data.split('_')[-1]
    row=room.db.get(room.models.FranchiseDraftAction,token)
    return row,json.loads(row.payload_json)


@pytest.mark.parametrize('status',['setup','live','paused'])
def test_preview_is_read_only_and_all_remaining_slots_are_signed_once(room,status):
    if status!='setup':live(room)
    if status=='paused':command(room,'dpause')
    before_status=room.season.status
    packet=command(room,'dautofill')
    row,payload=planned(room,packet)
    assert row.kind=='autofill' and len(payload['picks'])==4
    assert 'all remaining slots' in packet['text'] and '92 Cr' in packet['text']
    assert room.season.status==before_status and room.one.squad_size==room.two.squad_size==0
    assert room.one.purse_remaining_lakh==room.two.purse_remaining_lakh==10000
    assert room.db.query(room.models.FranchiseDraftPick).count()==0
    accept(room,packet)
    assert room.season.status==room.A.STATUS_COMPLETED
    for team in (room.one,room.two):
        assert team.squad_size==2 and team.purse_remaining_lakh==5400
        assert room.A.ledger_total(room.db,team.id)==5400
    picks=room.db.query(room.models.FranchiseDraftPick).order_by(room.models.FranchiseDraftPick.overall_no).all()
    assert [p.franchise_id for p in picks]==[room.one.id,room.two.id,room.two.id,room.one.id]
    assert [p.lot_id for p in picks]==[p['lot_id'] for p in payload['picks']]
    assert all(p.actor_tg_id==ADMIN for p in picks)
    assert room.db.query(room.models.AuctionEvent).filter_by(kind='fd_autofill_completed').count()==1
    with pytest.raises(room.A.AuctionError):accept(room,packet)
    room.db.rollback()
    assert room.one.purse_remaining_lakh==5400


def test_cancel_preserves_purses_pool_status_clock_and_order(room):
    live(room)
    draft=room.D.get_draft(room.db,room.season)
    clock=draft.pick_deadline_at
    revision=draft.revision
    packet=command(room,'autodraft')
    accept(room,packet,yes=False)
    assert draft.pick_deadline_at==clock and draft.revision==revision
    assert room.one.squad_size==room.two.squad_size==0
    assert len(room.D._available(room.db,room.season))==6
    assert room.season.status==room.A.STATUS_LIVE


def test_owners_cannot_prepare_confirm_or_open_another_admins_full_plan(room):
    live(room)
    with pytest.raises(room.A.AuctionError):command(room,'dautofill',actor=OWNER)
    room.db.rollback()
    packet=command(room,'dautofill')
    with pytest.raises(room.A.AuctionError):accept(room,packet,actor=OWNER)
    room.db.rollback()
    button=packet['markup'].inline_keyboard[1][0].callback_data
    with pytest.raises(room.A.AuctionError):room.H.execute_callback(room.db,OWNER,CHAT,room.season.id,button)
    room.db.rollback()
    detail=room.H.execute_callback(room.db,ADMIN,CHAT,room.season.id,button)
    assert 'planned selections' in detail['text'] and 'Joe Root' in detail['text']
    assert all(len(b.callback_data.encode())<=64 for row in detail['markup'].inline_keyboard for b in row)


def test_stale_preview_is_rejected_after_an_owner_pick_or_timer_change(room):
    live(room)
    packet=command(room,'dautofill')
    quote=room.D.preview_pick(room.db,room.season,OWNER,'Root')
    room.D.confirm_pick(room.db,room.season,quote.token,OWNER);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='changed'):accept(room,packet)
    room.db.rollback()
    packet=command(room,'dautofill')
    command(room,'dtimer','15')
    with pytest.raises(room.A.AuctionError,match='changed'):accept(room,packet)
    room.db.rollback()
    assert room.one.squad_size==1 and room.two.squad_size==0


def test_planner_reserves_cheap_cards_for_the_team_that_cannot_afford_others(room):
    room.D.configure_rounds(room.db,room.season,1,by_tg_id=ADMIN)
    lots=room.A.lots(room.db,room.season.id)
    for p in lots[2:]:room.db.delete(p)
    lots[1].rating=95
    room.D.set_rating_prices(room.db,room.season,'96:10cr\n95:90cr',by_tg_id=ADMIN)
    room.A.correct_purse(room.db,room.season,room.two,-9000,by_tg_id=ADMIN)
    room.db.commit()
    packet=command(room,'dautofill')
    _,payload=planned(room,packet)
    assert [p['price_lakh'] for p in payload['picks']]==[9000,1000]
    accept(room,packet)
    assert room.one.purse_remaining_lakh==1000 and room.two.purse_remaining_lakh==0


def test_joint_category_rating_limits_and_no_overseas_limit_are_enforced(room):
    for p in room.A.lots(room.db,room.season.id):
        p.is_overseas=True
        if p.name in ('Pat Cummins','Jasprit Bumrah'):p.category='Bowler';p.rating=80
    room.D.set_rating_prices(room.db,room.season,'96:23cr\n80:1cr',by_tg_id=ADMIN)
    room.D.update_squad_rules(room.db,room.season,minimums={'Batsman':1,'Bowler':1},
        maximums={'Batsman':1,'Bowler':1},ratings=[{'max_rating':80,'min_players':1}],by_tg_id=ADMIN)
    room.db.commit()
    packet=command(room,'dautofill')
    accept(room,packet)
    for team in (room.one,room.two):
        squad=room.A.squad(room.db,team.id)
        assert {p.category for p in squad}=={'Batsman','Bowler'}
        assert sum(p.rating<=80 for p in squad)==1
        assert all(p.is_overseas for p in squad)


def test_existing_signings_and_fifo_catch_up_turns_are_kept(room):
    live(room)
    room.D.skip_turn(room.db,room.season,OWNER);room.db.commit()
    quote=room.D.preview_pick(room.db,room.season,OTHER,'Root')
    signed=room.D.confirm_pick(room.db,room.season,quote.token,OTHER);room.db.commit()
    original_price=signed.sold_price_lakh
    packet=command(room,'dautofill')
    _,payload=planned(room,packet)
    assert len(payload['picks'])==3
    assert payload['picks'][-1]['catch_up'] and payload['picks'][-1]['team_id']==room.one.id
    accept(room,packet)
    assert signed.sold_to_id==room.two.id and signed.sold_price_lakh==original_price
    assert room.season.status==room.A.STATUS_COMPLETED and not room.D.skipped_turns(room.db,room.season)


def test_same_name_versions_cannot_both_end_up_in_one_franchise(room):
    lots=room.A.lots(room.db,room.season.id)
    lots[1].name=lots[0].name;lots[1].version='Legend';room.db.commit()
    accept(room,command(room,'dautofill'))
    for team in (room.one,room.two):
        names=[p.name.strip().casefold() for p in room.A.squad(room.db,team.id)]
        assert len(names)==len(set(names))==2


def test_failure_halfway_rolls_back_every_signing_and_can_retry_same_preview(room,monkeypatch):
    packet=command(room,'dautofill')
    original=room.D.confirm_pick
    count=[0]
    def fail(*args,**kwargs):
        count[0]+=1
        if count[0]==2:raise RuntimeError('Simulated disconnect before batch commit')
        return original(*args,**kwargs)
    monkeypatch.setattr(room.D,'confirm_pick',fail)
    with pytest.raises(RuntimeError):accept(room,packet)
    room.db.rollback()
    assert room.one.squad_size==room.two.squad_size==0
    assert room.one.purse_remaining_lakh==room.two.purse_remaining_lakh==10000
    assert room.db.query(room.models.FranchiseDraftPick).count()==0
    monkeypatch.setattr(room.D,'confirm_pick',original)
    accept(room,packet)
    assert room.season.status==room.A.STATUS_COMPLETED


def test_pool_shortage_and_search_budget_fail_without_mutations(room,monkeypatch):
    for p in room.A.lots(room.db,room.season.id)[2:]:room.db.delete(p)
    room.db.commit()
    with pytest.raises(room.A.AuctionError,match='needs 4 players'):command(room,'dautofill')
    room.db.rollback()
    assert room.db.query(room.models.FranchiseDraftAction).count()==0
    from services import franchise_draft_autofill as F
    monkeypatch.setattr(F,'MAX_SEARCH_NODES',0)
    # Restore enough pool cards for the bounded-search path.
    room.D.configure_rounds(room.db,room.season,1,by_tg_id=ADMIN);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='search limit'):command(room,'dautofill')
    room.db.rollback()
    assert room.one.purse_remaining_lakh==10000 and room.one.squad_size==0


def test_frozen_plan_is_applied_after_restart_without_replanning(room,monkeypatch):
    from services import franchise_draft_autofill as F
    packet=command(room,'dautofill')
    row,payload=planned(room,packet)
    token=row.token
    def no_replan(*args):raise AssertionError('Confirmation must use the reviewed player IDs')
    monkeypatch.setattr(F,'plan',no_replan)
    with room.sessions() as restarted:
        season=restarted.get(room.models.AuctionSeason,room.season.id)
        from services import franchise_draft_admin as M
        M.answer(restarted,season,ADMIN,token,accept=True);restarted.commit()
    room.db.expire_all()
    assert {p.lot_id for p in room.db.query(room.models.FranchiseDraftPick).all()}=={p['lot_id'] for p in payload['picks']}


def test_expired_preview_or_revoked_admin_cannot_auto_draft(room):
    room.D.add_draft_admin(room.db,OWNER,by_tg_id=ADMIN);room.db.commit()
    packet=command(room,'dautofill',actor=OWNER)
    room.D.remove_draft_admin(room.db,OWNER,by_tg_id=ADMIN);room.db.commit()
    with pytest.raises(room.A.AuctionError):accept(room,packet,actor=OWNER)
    room.db.rollback()
    packet=command(room,'dautofill')
    row,_=planned(room,packet)
    row.created_at=datetime.utcnow()-timedelta(minutes=11);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='expired'):accept(room,packet)
    room.db.rollback()
    assert room.one.squad_size==room.two.squad_size==0


def test_website_admin_and_owner_permissions_use_same_plan_and_transaction(room):
    from services import franchise_draft_web as W
    from services.auction_webapp import AccessError
    body=dict(action='draft_preview',operation='autofill',values={})
    with pytest.raises(AccessError):W.perform(room.db,room.season,OWNER,body)
    room.db.rollback()
    quote=W.perform(room.db,room.season,-555,body,trusted_admin=True)['confirmation'];room.db.commit()
    assert 'Joe Root' in quote['html'] and 'Jos Buttler' in quote['html']
    W.perform(room.db,room.season,-555,dict(action='draft_answer',token=quote['token'],accept=True),trusted_admin=True)
    room.db.commit()
    assert all(p.actor_tg_id==-555 for p in room.db.query(room.models.FranchiseDraftPick).all())
    assert room.season.status==room.A.STATUS_COMPLETED


def test_optional_slots_are_filled_to_maximum_and_retained_contracts_keep_their_price(room):
    room.D.configure_rounds(room.db,room.season,2,min_squad_size=1,by_tg_id=ADMIN)
    lot=room.A.lots(room.db,room.season.id)[0]
    from types import SimpleNamespace
    room.A._sign_before_auction(room.db,room.season,room.one,SimpleNamespace(id=lot.player_id),1000,
        kind=room.A.ACQ_RETAINED,counter='retained_count',ledger_kind=room.A.LEDGER_RETENTION,verb='retain',
        note='Previous contract',event_kind='retained',mark='🔒',word='retained',quiet=True)
    room.db.commit()
    packet=command(room,'dautofill')
    _,payload=planned(room,packet)
    assert len(payload['picks'])==3 and lot.id not in {p['lot_id'] for p in payload['picks']}
    accept(room,packet)
    assert lot.sold_price_lakh==1000 and lot.acquisition==room.A.ACQ_RETAINED
    assert room.one.squad_size==room.two.squad_size==2
    assert room.one.purse_remaining_lakh==6700 and room.two.purse_remaining_lakh==5400
    assert room.db.query(room.models.FranchiseDraftPick).count()==3


def test_impossible_purse_and_out_of_band_changes_do_not_sign_any_player(room):
    room.A.correct_purse(room.db,room.season,room.two,-9999,by_tg_id=ADMIN);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='purse'):command(room,'dautofill')
    room.db.rollback()
    assert room.one.squad_size==room.two.squad_size==0
    room.A.correct_purse(room.db,room.season,room.two,9999,by_tg_id=ADMIN);room.db.commit()
    packet=command(room,'dautofill')
    room.one.purse_remaining_lakh-=1;room.db.commit()
    with pytest.raises(room.A.AuctionError,match='changed'):accept(room,packet)
    room.db.rollback()
    assert room.one.squad_size==room.two.squad_size==0


def test_autofill_works_after_team_removal_and_respects_the_filtered_rotation(room):
    room.D.add_team(room.db,room.season,'Team Three',884010,by_tg_id=ADMIN);room.db.commit();live(room)
    accept(room,command(room,'dremoveteam','Team One | Withdrawn'))
    original=[t.id for t in room.D.schedule(room.db,room.season)]
    accept(room,command(room,'dautofill'))
    picks=room.db.query(room.models.FranchiseDraftPick).order_by(room.models.FranchiseDraftPick.overall_no).all()
    assert [p.franchise_id for p in picks]==original
    assert room.season.status==room.A.STATUS_COMPLETED


def test_admin_buttons_open_preview_and_owners_cannot_forward_them(room):
    packet=room.H.execute(room.db,ADMIN,CHAT,room.season.id,'draft')
    button=next(b for row in packet['markup'].inline_keyboard for b in row if b.callback_data.endswith('_autofill'))
    with pytest.raises(room.A.AuctionError):room.H.execute_callback(room.db,OWNER,CHAT,room.season.id,button.callback_data)
    room.db.rollback()
    preview=room.H.execute_callback(room.db,ADMIN,CHAT,room.season.id,button.callback_data);room.db.commit()
    assert preview['markup'].inline_keyboard[0][0].text=='🤖 Confirm Auto Draft'
    assert room.one.squad_size==0


def test_native_controls_prepare_same_plan_without_signing(room):
    from handlers import franchise_bot as H
    result=H.execute_action(room.db,ADMIN,room.season.id,CHAT,'native_fd_autofill',{})
    room.db.commit()
    assert 'draft_packet' in result and room.one.squad_size==room.two.squad_size==0


def test_paused_clock_settings_survive_batch_and_later_undo(room):
    room.D.set_timer(room.db,room.season,1,timeout_policy='skip',by_tg_id=ADMIN)
    live(room);command(room,'dpause')
    settings=room.D.get_draft(room.db,room.season).settings_json
    accept(room,command(room,'dautofill'))
    draft=room.D.get_draft(room.db,room.season)
    assert draft.settings_json==settings and draft.pick_deadline_at is None
    team=room.one
    purse=team.purse_remaining_lakh
    room.D.undo_last(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    assert room.season.status==room.A.STATUS_PAUSED
    assert team.squad_size==1 and team.purse_remaining_lakh==purse+2300
    assert draft.paused_seconds_left==60


def test_custom_decimal_prices_are_charged_without_discount_or_rounding(room):
    lots=room.A.lots(room.db,room.season.id)
    for p in lots[4:]:room.db.delete(p)
    room.D.set_custom_prices(room.db,room.season,'Joe Root:25.5cr',by_tg_id=ADMIN);room.db.commit()
    packet=command(room,'dautofill')
    _,payload=planned(room,packet)
    assert payload['total_lakh']==9450
    accept(room,packet)
    root=room.D.find_player(room.db,room.season,'Joe Root',available_only=False)
    assert root.sold_price_lakh==2550
    assert room.one.purse_remaining_lakh+room.two.purse_remaining_lakh==10550
