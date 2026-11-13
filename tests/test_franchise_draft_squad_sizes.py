"""Independent minimums, maximum-sized rounds and validated optional places."""
import json

import pytest

from test_franchise_draft_handlers import room, ADMIN, OWNER, OTHER, CHAT  # noqa: F401
from test_franchise_draft_seasons_page import page, competition  # noqa: F401
from services import franchise_draft_admin as M


def configure(room, minimum=1, maximum=3):
    row=M.prepare(room.db,room.season,ADMIN,'rounds',{'min_squad_size':minimum,'rounds':maximum})
    room.db.commit()
    assert room.season.min_squad_size==2
    M.answer(room.db,room.season,ADMIN,row.token,accept=True)
    room.db.commit()
    assert room.season.min_squad_size==minimum
    assert room.season.max_squad_size==room.D.get_draft(room.db,room.season).rounds==maximum


def pick(room, actor, lot):
    quote=room.D.preview_pick(room.db,room.season,actor,f'#{lot.id}')
    room.D.confirm_pick(room.db,room.season,quote.token,actor)
    room.db.commit()


def test_minimum_survives_start_picks_and_restart_and_optional_places_can_finish(room):
    configure(room)
    room.D.start(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    with pytest.raises(room.A.AuctionError,match='incomplete'):
        M.prepare(room.db,room.season,ADMIN,'finish',{})
    lots=room.A.lots(room.db,room.season.id)
    pick(room,OWNER,lots[0]);pick(room,OTHER,lots[1])
    assert room.season.min_squad_size==1 and room.season.max_squad_size==3
    balances=[t.purse_remaining_lakh for t in (room.one,room.two)]
    row=M.prepare(room.db,room.season,ADMIN,'finish',{})
    assert room.season.status==room.A.STATUS_LIVE
    M.answer(room.db,room.season,ADMIN,row.token,accept=True);room.db.commit()
    assert room.season.status==room.A.STATUS_COMPLETED
    assert [t.purse_remaining_lakh for t in (room.one,room.two)]==balances
    # Undoing a finish reopens the unfilled places without refunding a signing.
    room.D.undo_last(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    assert room.season.status==room.A.STATUS_PAUSED and room.D.get_draft(room.db,room.season).cursor==2
    assert [t.squad_size for t in (room.one,room.two)]==[1,1]
    room.D.finish(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    league=room.A.publish_to_league(room.db,room.season);room.db.commit()
    assert league.id==room.season.league_id
    with room.sessions() as resumed:
        season=resumed.get(type(room.season),room.season.id)
        assert season.min_squad_size==1 and season.max_squad_size==3


@pytest.mark.parametrize('minimum,maximum',[(0,3),(4,3),('bad',3),(1,0)])
def test_bad_limits_do_not_change_the_existing_draft(room,minimum,maximum):
    before=M.fingerprint(room.db,room.season)
    with pytest.raises(room.A.AuctionError):
        M.prepare(room.db,room.season,ADMIN,'rounds',{'min_squad_size':minimum,'rounds':maximum})
    assert before==M.fingerprint(room.db,room.season)


def test_small_pool_can_cover_minimum_without_filling_every_optional_place(room):
    configure(room,minimum=1,maximum=5)
    room.D.start(room.db,room.season,by_tg_id=ADMIN)
    assert room.season.status==room.A.STATUS_LIVE  # Six cards cover two mandatory places.


def test_minimum_does_not_reset_when_maximum_rounds_change(room):
    configure(room)
    room.D.configure_rounds(room.db,room.season,4,by_tg_id=ADMIN)
    assert room.season.min_squad_size==1 and room.season.max_squad_size==4
    room.db.commit()
    packet=room.H.execute(room.db,ADMIN,CHAT,room.season.id,'drounds','2 | 5')
    assert 'Minimum squad' in packet['text']
    row=room.db.query(room.models.FranchiseDraftAction).filter_by(kind='rounds',status='pending').one()
    assert json.loads(row.payload_json)=={'min_squad_size':2,'rounds':5}


def test_optional_places_still_reserve_for_combined_role_and_rating_rules(room):
    configure(room)
    lots=room.A.lots(room.db,room.season.id)
    for lot in lots[:2]:lot.category='Bowler'
    for lot in lots[2:4]:lot.rating=80
    room.D.set_rating_prices(room.db,room.season,'96:23cr\n80:1cr',by_tg_id=ADMIN)
    room.D.update_squad_rules(room.db,room.season,minimums={'BOWL':1},
        ratings=[{'max_rating':80,'min_players':1}],by_tg_id=ADMIN)
    room.db.commit()
    # A cheap low-rated batter and a bowler are both required, even though min=1.
    assert room.D.completion_reserve(room.db,room.season,room.one,lots)==2400
    room.D.start(room.db,room.season,by_tg_id=ADMIN);room.db.commit()
    pick(room,OWNER,lots[2]);pick(room,OTHER,lots[3])
    with pytest.raises(room.A.AuctionError,match='rules'):
        M.prepare(room.db,room.season,ADMIN,'finish',{})
    pick(room,OTHER,lots[0]);pick(room,OWNER,lots[1])
    room.D.finish(room.db,room.season,by_tg_id=ADMIN)
    assert room.season.status==room.A.STATUS_COMPLETED


def test_admin_can_create_and_read_independent_limits(page):
    response=page.client.post('/api/admin/franchise-draft',json={
        'request_type':'action','action':'draft_create','name':'Two to five',
        'min_squad_size':2,'rounds':5,'purse':'100'})
    assert response.status_code==200,response.json
    sid=response.json['season_id']
    response=page.client.post('/api/admin/franchise-draft',json={'season_id':sid,'part':'settings'})
    assert response.status_code==200,response.json
    assert (response.json['draft']['min_squad_size'],response.json['draft']['max_squad_size'],response.json['draft']['rounds'])==(2,5,5)
