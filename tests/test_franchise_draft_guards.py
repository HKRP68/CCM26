"""Legacy auction controls cannot bypass a franchise draft's pick journal."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from test_franchise_draft import room as full_room, start, pick  # noqa: F401


@pytest.fixture
def room(tmp_path):
    from database import Base
    from models import AuctionLot, FranchiseDraft
    from services import auction_service as A

    engine = create_engine('sqlite:///' + str(tmp_path / 'draft-guards.db'))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    season = A.create_season(db, 'Round draft', min_squad_size=2,
                             max_squad_size=2, min_overseas=0, max_overseas=2)
    team = A.create_franchise(db, season, 'One', owner_tg_id=80001)
    A.create_franchise(db, season, 'Two', owner_tg_id=80002)
    lot = AuctionLot(season_id=season.id, name='Draft player', rating=96,
                     category='Batsman', country='England', lot_no=1,
                     status=A.LOT_QUEUED, base_price_lakh=100)
    draft = FranchiseDraft(season_id=season.id, rounds=2, cursor=0, revision=0)
    db.add_all([lot, draft])
    db.commit()
    yield SimpleNamespace(db=db, season=season, team=team, lot=lot,
                          draft=draft, A=A)
    db.close()
    engine.dispose()


@pytest.mark.parametrize('operation', [
    lambda r: r.A.start(r.db, r.season),
    lambda r: r.A.pause(r.db, r.season),
    lambda r: r.A.open_lot(r.db, r.season, r.lot),
    lambda r: r.A.open_next_lot(r.db, r.season),
    lambda r: r.A.complete_if_done(r.db, r.season),
    lambda r: r.A.restart_auction(r.db, r.season),
    lambda r: r.A.draft_pick(r.db, r.season, r.team, None),
    lambda r: r.A.skip_pick(r.db, r.season, r.team),
    lambda r: r.A.undo_pick(r.db, r.season, r.lot),
    lambda r: r.A.retain(r.db, r.season, r.team, None),
    lambda r: r.A.unretain(r.db, r.season, r.team, r.lot),
    lambda r: r.A.release_by_owner(r.db, r.season, r.lot, 80001),
    lambda r: r.A.offer_retention(r.db, r.season, r.team, None),
    lambda r: r.A.answer_retention_offer(r.db, r.season, None, 80001, True),
    lambda r: r.A.sell_lot(r.db, r.season, r.lot),
    lambda r: r.A.pass_lot(r.db, r.season, r.lot),
    lambda r: r.A.undo_sale(r.db, r.season, r.lot),
    lambda r: r.A.set_expansion_picks(r.db, r.season, 10),
    lambda r: r.A.set_franchise_picks(r.db, r.season, r.team, 10),
    lambda r: r.A.set_rtm_rules(r.db, r.season, enabled=True),
    lambda r: r.A.validate_bid(r.db, r.season, r.lot, r.team, 100),
])
def test_legacy_controls_refuse_before_writing(room, operation):
    before = (room.season.status, room.team.purse_remaining_lakh,
              room.team.squad_size, room.draft.cursor, room.draft.revision)
    with pytest.raises(room.A.AuctionError, match='Franchise Draft'):
        operation(room)
    room.db.commit()
    assert (room.season.status, room.team.purse_remaining_lakh,
            room.team.squad_size, room.draft.cursor, room.draft.revision) == before
    assert room.lot.status == room.A.LOT_QUEUED


def test_simulated_finish_cannot_assign_draft_squads(room):
    from services.auction_simulator import preview, simulate_finish
    for operation in (preview, simulate_finish):
        with pytest.raises(room.A.AuctionError, match='Franchise Draft'):
            operation(room.db, room.season)
    assert room.team.squad_size == 0
    assert room.draft.cursor == 0


def test_manual_roster_edits_cannot_skip_draft_turns(room):
    from services import auction_management as M
    room.season.status = room.A.STATUS_PAUSED
    room.db.commit()
    assert '/dpick' in M.roster_block_reason(room.db, room.season)
    with pytest.raises(room.A.AuctionError, match='/dpick'):
        M.assign_player(room.db, room.season, room.team, 1,
                        price_lakh=50, note='Manual signing')
    assert room.team.squad_size == room.draft.cursor == 0


@pytest.mark.parametrize('settings', [
    {'max_squad_size': 12}, {'min_squad_size': 1}, {'max_overseas': 1},
    {'min_overseas': 1}, {'max_retentions': 3}, {'rtm_enabled': True},
    {'expansion_picks': 3}, {'base_prices': []}, {'draft_pick_seconds': 5},
])
def test_auction_settings_cannot_redefine_draft_mode(room, settings):
    from services import auction_webapp as W
    with pytest.raises(room.A.AuctionError, match='/drounds'):
        W.save_settings(room.db, room.season, 80099,
                        {'name': 'Must not be saved', **settings})
    assert room.season.name == 'Round draft'
    assert room.season.max_squad_size == room.draft.rounds == 2
    assert room.season.min_overseas == 0 and room.season.max_overseas == 2


def test_draft_role_and_rating_rules_remain_configurable(room):
    from services import auction_webapp as W
    W.save_settings(room.db, room.season, 80099, {
        'role_minimums': {'BAT': 1}, 'role_maximums': {'BOWL': 1},
        'rating_rules': [{'max_rating': 85, 'min_players': 1}],
    })
    assert room.A.role_minimums(room.season) == {'Batsman': 1}
    assert room.A.role_maximums(room.season) == {'Bowler': 1}
    assert room.A.rating_rules(room.season) == [{'max_rating': 85, 'min_players': 1}]


def test_franchise_field_locks_when_draft_starts(room):
    room.draft.cursor = 1
    room.db.commit()
    with pytest.raises(room.A.AuctionError, match='field is locked'):
        room.A.create_franchise(room.db, room.season, 'Three')
    with pytest.raises(room.A.AuctionError, match='field is locked'):
        room.A.remove_franchise(room.db, room.season, room.team)
    with pytest.raises(room.A.AuctionError, match='field is locked'):
        room.A.import_franchises(room.db, room.season, [{'name': 'Three'}])
    assert len(room.A.franchises(room.db, room.season.id)) == 2


def test_ordinary_auction_can_open_a_player_and_keep_expansion_controls(room):
    ordinary = room.A.create_season(room.db, 'Ordinary auction', min_squad_size=0,
                                   max_squad_size=3, chat_id=-10080099)
    one = room.A.create_franchise(room.db, ordinary, 'One', owner_tg_id=80001)
    room.A.create_franchise(room.db, ordinary, 'Two', owner_tg_id=80002)
    from models import AuctionLot
    lot = AuctionLot(season_id=ordinary.id, name='Auction player', rating=96,
                     category='Batsman', country='India', lot_no=1,
                     status=room.A.LOT_QUEUED, base_price_lakh=100)
    room.db.add(lot)
    room.db.flush()
    room.A.set_franchise_picks(room.db, ordinary, one, 2)
    assert one.draft_picks_total == 2
    # This ordinary auction's expansion allowance is reset before it opens.
    room.A.set_franchise_picks(room.db, ordinary, one, 0)
    room.A.start(room.db, ordinary)
    assert ordinary.status == room.A.STATUS_LIVE
    assert lot.status == room.A.LOT_ON_BLOCK


@pytest.mark.parametrize('status', ['setup', 'live', 'paused', 'completed'])
def test_unpublished_draft_trades_cannot_move_players_or_money(room, status):
    from models import AuctionTrade
    from services import auction_trade_service as T
    room.season.status = status
    room.db.commit()
    phase, reason = T.trade_phase(room.db, room.season)
    assert phase is None and 'Publish' in reason
    trade = AuctionTrade(season_id=room.season.id, season=room.season,
                         team_a_id=room.team.id,
                         team_b_id=room.A.franchises(room.db, room.season.id)[1].id,
                         phase=T.PHASE_POST, status=T.STATUS_PENDING)
    with pytest.raises(room.A.AuctionError, match='Publish'):
        T.validate(room.db, trade, check_window=False)
    with pytest.raises(room.A.AuctionError, match='Publish'):
        T.validate(room.db, trade, agreed=True)
    with pytest.raises(room.A.AuctionError, match='Publish'):
        T.admin_trade(room.db, room.season, room.team,
                      room.A.franchises(room.db, room.season.id)[1],
                      [room.lot], [], by_web=True)
    assert room.db.query(AuctionTrade).count() == 0


def test_published_draft_retains_normal_post_season_trade_window(room):
    from datetime import datetime
    from services import auction_trade_service as T
    room.season.status = room.A.STATUS_COMPLETED
    room.season.published_at = datetime.utcnow()
    assert T.trade_phase(room.db, room.season) == (T.PHASE_POST, None)


def test_publish_refuses_incomplete_draft_even_if_status_was_set_completed(room):
    room.season.status = room.A.STATUS_COMPLETED
    with pytest.raises(room.A.AuctionError, match='Complete every draft round'):
        room.A.publish_to_league(room.db, room.season)
    assert room.season.league_id is None


def test_draft_role_names_must_resolve_unambiguously_before_saving(room):
    from services import auction_webapp as W
    for roles in ({'unknown': 1}, {'BAT': 1, 'Batsman': 2}):
        with pytest.raises(room.A.AuctionError):
            W.save_settings(room.db, room.season, 80099,
                            {'name': 'Must not be saved', 'role_minimums': roles})
        assert room.season.name == 'Round draft'


@pytest.mark.parametrize('load_draft', [False, True])
def test_stale_publisher_cannot_publish_after_another_session_undoes_pick(full_room, load_draft):
    from models import AuctionSeason
    r = full_room
    r.D.configure_rounds(r.db, r.season, 1)
    r.db.commit()
    start(r)
    for player in r.players[:5]:
        pick(r, player.name)
    assert r.season.status == r.A.STATUS_COMPLETED
    stale = r.sessions()
    try:
        cached_season = stale.get(AuctionSeason, r.season.id)
        assert cached_season.status == r.A.STATUS_COMPLETED
        if load_draft:
            cached_draft = r.D.get_draft(stale, cached_season)
            assert cached_draft.cursor == 5
        r.D.undo_last(r.db, r.season)
        r.db.commit()
        with pytest.raises(r.A.AuctionError):
            r.A.publish_to_league(stale, cached_season)
        stale.rollback()
        r.db.expire_all()
        assert r.season.league_id is None
        assert r.season.status == r.A.STATUS_PAUSED
        assert r.D.get_draft(r.db, r.season).cursor == 4
    finally:
        stale.close()


def test_draft_purse_adjustment_requires_pause_and_invalidates_prior_revision(room):
    from services import auction_management as M
    room.season.status = room.A.STATUS_LIVE
    room.db.commit()
    with pytest.raises(room.A.AuctionError, match='Pause the draft'):
        room.A.correct_purse(room.db, room.season, room.team, 100)
    room.db.rollback()
    room.season.status = room.A.STATUS_PAUSED
    room.db.commit()
    revision = room.draft.revision
    balance = room.team.purse_remaining_lakh
    M.adjust_purse(room.db, room.season, room.team, mode='add', amount_lakh=100,
                   expected_purse_lakh=balance, note='Additional draft funds')
    room.db.commit()
    assert room.team.purse_remaining_lakh == balance + 100
    assert room.A.ledger_total(room.db, room.team.id) == balance + 100
    assert room.draft.revision > revision


def test_cancellation_invalidates_pending_confirmations(full_room):
    r = full_room
    start(r)
    quote = r.D.preview_pick(r.db, r.season, r.teams[0].owner_tg_id, 'Joe Root')
    r.db.commit()
    r.A.cancel(r.db, r.season)
    r.db.commit()
    assert r.season.status == r.A.STATUS_CANCELLED
    assert r.D.get_draft(r.db, r.season).revision > quote.revision
    with pytest.raises(r.A.AuctionError):
        r.D.confirm_pick(r.db, r.season, quote.token, r.teams[0].owner_tg_id)
    r.db.rollback()
    assert r.teams[0].squad_size == 0
    assert r.A.ledger_total(r.db, r.teams[0].id) == 10000
