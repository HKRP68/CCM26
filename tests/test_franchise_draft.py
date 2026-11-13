"""The franchise draft signs fixed-price players atomically in rotating rounds."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def room(tmp_path):
    from database import Base
    from models import Player
    from services import auction_service as A, franchise_draft as D

    engine = create_engine('sqlite:///' + str(tmp_path / 'franchise-draft.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    db = sessions()
    season = A.create_season(db, 'Premier draft', opening_purse_lakh=10000)
    A.bind_chat(db, season, -10045678)
    D.enable(db, season, rounds=2)
    teams = [A.create_franchise(db, season, f'Team {i + 1}', owner_tg_id=1000 + i,
                               co_owner_ids_json=json.dumps([2000 + i]),
                               team_manager_ids_json=json.dumps([3000 + i]))
             for i in range(5)]
    specs = [
        ('Joe Root', 96, 'Batsman'), ('Ben Stokes', 95, 'All-rounder'),
        ('Jos Buttler', 96, 'Wicket Keeper'), ('Jofra Archer', 95, 'Bowler'),
        ('Harry Brook', 96, 'Batsman'), ('Sam Curran', 95, 'All-rounder'),
        ('Phil Salt', 96, 'Wicket Keeper'), ('Mark Wood', 95, 'Bowler'),
        ('Dawid Malan', 96, 'Batsman'), ('Chris Woakes', 95, 'Bowler'),
        ('Alex Hales', 80, 'Batsman'), ('Adil Rashid', 80, 'Bowler'),
        ('Moeen Ali', 80, 'All-rounder'), ('Rehan Ahmed', 80, 'Bowler'),
        ('Tom Banton', 80, 'Wicket Keeper'), ('Jason Roy', 80, 'Batsman'),
        ('Will Jacks', 80, 'All-rounder'), ('Reece Topley', 80, 'Bowler'),
        ('Liam Livingstone', 80, 'All-rounder'), ('Jonny Bairstow', 80, 'Wicket Keeper'),
    ]
    players = [Player(name=name, rating=rating, category=role, country='England',
                      bat_hand='Right', bowl_hand='Right', bowl_style='Medium Pacer',
                      bat_rating=rating, bowl_rating=rating)
               for name, rating, role in specs]
    db.add_all(players)
    db.flush()
    A.add_players_to_pool(db, season, players)
    D.set_rating_prices(db, season, '96:23cr\n95:21cr\n80:1cr')
    D.set_order(db, season, [team.id for team in teams])
    db.commit()
    data = SimpleNamespace(db=db, sessions=sessions, engine=engine, season=season,
                           teams=teams, players=players, A=A, D=D)
    yield data
    data.db.close()
    engine.dispose()


def lot(room, name):
    from models import AuctionLot
    return room.db.query(AuctionLot).filter_by(season_id=room.season.id, name=name).one()


def pick(room, name, actor=None):
    actor = actor if actor is not None else room.D.turn(room.db, room.season).owner_tg_id
    confirmation = room.D.preview_pick(room.db, room.season, actor, name)
    room.db.commit()
    result = room.D.confirm_pick(room.db, room.season, confirmation.token, actor)
    room.db.commit()
    return result


def start(room):
    room.D.start(room.db, room.season)
    room.db.commit()


def test_rounds_rotate_first_team_and_last_team_as_requested(room):
    room.D.configure_rounds(room.db, room.season, 3)
    ids = [team.id for team in room.teams]
    assert [t.id for t in room.D.round_order(room.db, room.season, 1)] == ids
    assert [t.id for t in room.D.round_order(room.db, room.season, 2)] == ids[1:] + ids[:1]
    assert [t.id for t in room.D.round_order(room.db, room.season, 3)] == ids[2:] + ids[:2]
    assert room.season.min_squad_size == room.season.max_squad_size == 3


def test_custom_order_has_every_team_once(room):
    ids = [team.id for team in reversed(room.teams)]
    room.D.set_order(room.db, room.season, ids)
    assert [t.id for t in room.D.round_order(room.db, room.season, 1)] == ids
    with pytest.raises(room.A.AuctionError):
        room.D.set_order(room.db, room.season, ids[:-1])
    with pytest.raises(room.A.AuctionError):
        room.D.set_order(room.db, room.season, [ids[0]] * 5)


def test_fixed_rating_prices_and_named_override_use_exact_lakh(room):
    room.D.set_custom_prices(room.db, room.season, 'Joe Root:25.5cr')
    assert room.D.price_for(room.db, room.season, lot(room, 'Joe Root')) == 2550
    assert room.D.price_for(room.db, room.season, lot(room, 'Jos Buttler')) == 2300
    assert room.D.price_for(room.db, room.season, lot(room, 'Ben Stokes')) == 2100
    room.D.set_rating_prices(room.db, room.season, '96:0.29cr\n95:0.07cr\n80:0cr')
    assert room.D.price_for(room.db, room.season, lot(room, 'Joe Root')) == 2550
    assert room.D.price_for(room.db, room.season, lot(room, 'Jos Buttler')) == 29
    assert room.D.price_for(room.db, room.season, lot(room, 'Ben Stokes')) == 7
    assert room.D.price_for(room.db, room.season, lot(room, 'Alex Hales')) == 0


@pytest.mark.parametrize('text', ['96:-1cr', '96:nancr', '96:infcr', '96:1.001cr',
                                 'not-a-rating:1cr', '96:23cr\n96:21cr'])
def test_invalid_price_tables_are_rejected(room, text):
    before = room.D.get_draft(room.db, room.season).rating_prices_json
    with pytest.raises(room.A.AuctionError):
        room.D.set_rating_prices(room.db, room.season, text)
    assert room.D.get_draft(room.db, room.season).rating_prices_json == before


def test_unpriced_pool_cannot_start(room):
    room.D.set_rating_prices(room.db, room.season, '96:23cr')
    room.db.commit()
    with pytest.raises(room.A.AuctionError):
        room.D.start(room.db, room.season)
    room.db.rollback()
    assert room.season.status == room.A.STATUS_SETUP


def test_preview_makes_no_financial_change_and_only_owner_or_coowner_can_pick(room):
    start(room)
    for actor in (3000, 1001, 999999):
        with pytest.raises(room.A.AuctionError):
            room.D.preview_pick(room.db, room.season, actor, 'Joe Root')
        room.db.rollback()
    confirmation = room.D.preview_pick(room.db, room.season, 2000, 'Joe Root')
    room.db.commit()
    assert confirmation.price_lakh == 2300
    assert room.teams[0].purse_remaining_lakh == 10000
    assert room.teams[0].squad_size == 0
    assert lot(room, 'Joe Root').status == room.A.LOT_QUEUED
    signed = room.D.confirm_pick(room.db, room.season, confirmation.token, 2000)
    room.db.commit()
    assert signed.sold_to_id == room.teams[0].id


def test_pick_debits_once_records_ledger_and_moves_turn(room):
    start(room)
    confirmation = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    room.db.commit()
    signed = room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
    room.db.commit()
    assert signed.status == room.A.LOT_SOLD
    assert signed.acquisition == room.A.ACQ_DRAFTED
    assert signed.sold_price_lakh == 2300
    assert signed.sold_to_id == room.teams[0].id
    assert room.teams[0].purse_remaining_lakh == 7700
    assert room.teams[0].squad_size == 1
    assert room.A.ledger_total(room.db, room.teams[0].id) == 7700
    assert room.D.get_draft(room.db, room.season).cursor == 1
    assert room.D.turn(room.db, room.season).id == room.teams[1].id
    with pytest.raises(room.A.AuctionError):
        room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
    room.db.rollback()
    assert room.teams[0].purse_remaining_lakh == 7700
    assert room.A.ledger_total(room.db, room.teams[0].id) == 7700


def test_two_confirmations_for_same_turn_cannot_sign_twice(room):
    start(room)
    first = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    other = room.D.preview_pick(room.db, room.season, 2000, 'Ben Stokes')
    room.db.commit()
    room.D.confirm_pick(room.db, room.season, first.token, 1000)
    room.db.commit()
    with pytest.raises(room.A.AuctionError):
        room.D.confirm_pick(room.db, room.season, other.token, 2000)
    room.db.rollback()
    assert room.teams[0].purse_remaining_lakh == 7700
    assert room.teams[0].squad_size == 1
    assert lot(room, 'Ben Stokes').status == room.A.LOT_QUEUED


def test_cancelled_and_wrong_actor_confirmations_cannot_sign(room):
    start(room)
    confirmation = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    room.db.commit()
    with pytest.raises(room.A.AuctionError):
        room.D.confirm_pick(room.db, room.season, confirmation.token, 1001)
    room.db.rollback()
    room.D.cancel_pick(room.db, room.season, confirmation.token, 1000)
    room.db.commit()
    with pytest.raises(room.A.AuctionError):
        room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
    room.db.rollback()
    assert room.teams[0].purse_remaining_lakh == 10000


def test_revoked_owner_and_paused_draft_invalidate_pick(room):
    start(room)
    confirmation = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    room.db.commit()
    room.teams[0].owner_tg_id = 9000
    room.db.commit()
    with pytest.raises(room.A.AuctionError):
        room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
    room.db.rollback()
    room.teams[0].owner_tg_id = 1000
    room.db.commit()
    room.D.pause(room.db, room.season)
    room.db.commit()
    with pytest.raises(room.A.AuctionError):
        room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
    room.db.rollback()
    assert room.teams[0].squad_size == 0


def test_foreign_players_have_no_overseas_limit(room):
    start(room)
    for name in [p.name for p in room.players[:10]]:
        pick(room, name)
    assert room.season.status == room.A.STATUS_COMPLETED
    assert all(team.squad_size == 2 for team in room.teams)
    assert all(room.A.overseas_count(room.db, team.id) == 2 for team in room.teams)
    assert room.D.turn(room.db, room.season) is None


def test_role_maximum_and_minimum_reachability_are_enforced(room):
    room.season.role_minimums_json = json.dumps({'Bowler': 1})
    room.season.role_maximums_json = json.dumps({'Batsman': 1})
    room.db.commit()
    start(room)
    for name in ['Joe Root', 'Ben Stokes', 'Jos Buttler', 'Harry Brook', 'Sam Curran']:
        pick(room, name)
    # Team 2 opens round two and needs a bowler in its last squad slot.
    with pytest.raises(room.A.AuctionError):
        room.D.preview_pick(room.db, room.season, 1001, 'Phil Salt')
    room.db.rollback()
    pick(room, 'Jofra Archer', 1001)
    pick(room, 'Mark Wood', 1002)
    # Team 4 also already has its allowed batter.
    with pytest.raises(room.A.AuctionError):
        room.D.preview_pick(room.db, room.season, 1003, 'Dawid Malan')
    room.db.rollback()
    assert room.teams[3].squad_size == 1


def test_rating_rule_reserves_last_slot_for_lower_rating(room):
    room.A.set_rating_rules(room.db, room.season, [{'max_rating': 80, 'min_players': 1}])
    room.db.commit()
    start(room)
    for name in ['Joe Root', 'Ben Stokes', 'Jos Buttler', 'Jofra Archer', 'Harry Brook']:
        pick(room, name)
    with pytest.raises(room.A.AuctionError):
        room.D.preview_pick(room.db, room.season, 1001, 'Sam Curran')
    room.db.rollback()
    pick(room, 'Alex Hales', 1001)
    assert room.teams[1].squad_size == 2


def test_completed_draft_can_add_rounds_and_resume_rotating_order(room):
    start(room)
    for player in room.players[:10]:
        pick(room, player.name)
    room.D.configure_rounds(room.db, room.season, 3)
    room.db.commit()
    assert room.season.status == room.A.STATUS_PAUSED
    assert room.season.max_squad_size == 3
    assert room.D.turn(room.db, room.season).id == room.teams[2].id
    start(room)
    pick(room, 'Alex Hales', 1002)
    assert room.teams[2].squad_size == 3


def test_undo_refunds_only_last_pick_and_reopens_its_turn(room):
    start(room)
    pick(room, 'Joe Root', 1000)
    pick(room, 'Ben Stokes', 1001)
    room.D.pause(room.db, room.season)
    room.db.commit()
    room.D.undo_last(room.db, room.season)
    room.db.commit()
    assert room.teams[0].purse_remaining_lakh == 7700
    assert room.teams[1].purse_remaining_lakh == 10000
    assert room.teams[1].squad_size == 0
    assert room.A.ledger_total(room.db, room.teams[1].id) == 10000
    assert lot(room, 'Ben Stokes').status == room.A.LOT_QUEUED
    assert room.D.get_draft(room.db, room.season).cursor == 1
    assert room.D.turn(room.db, room.season).id == room.teams[1].id


def test_price_changes_invalidate_old_confirmation(room):
    start(room)
    confirmation = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    room.db.commit()
    room.D.pause(room.db, room.season)
    room.D.set_custom_prices(room.db, room.season, 'Joe Root:25.5cr',correction_reason='Corrected price sheet')
    room.db.commit()
    start(room)
    with pytest.raises(room.A.AuctionError):
        room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
    room.db.rollback()
    assert room.teams[0].purse_remaining_lakh == 10000


def test_restart_keeps_quote_order_purse_and_pick_history(room):
    start(room)
    confirmation = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    token = confirmation.token
    season_id = room.season.id
    room.db.commit()
    room.db.close()
    from models import AuctionSeason
    room.db = room.sessions()
    room.season = room.db.get(AuctionSeason, season_id)
    signed = room.D.confirm_pick(room.db, room.season, token, 1000)
    room.db.commit()
    assert signed.sold_price_lakh == 2300
    assert room.D.turn(room.db, room.season).owner_tg_id == 1001
    current = room.A.franchises(room.db, season_id)
    assert current[0].purse_remaining_lakh == 7700
    assert current[0].squad_size == 1


def test_stale_second_session_cannot_sign_previous_turn(room):
    from models import AuctionSeason
    start(room)
    first = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    other = room.D.preview_pick(room.db, room.season, 2000, 'Ben Stokes')
    room.db.commit()
    second_db = room.sessions()
    try:
        stale_season = second_db.get(AuctionSeason, room.season.id)
        room.D.get_draft(second_db, stale_season)
        room.D.confirm_pick(room.db, room.season, first.token, 1000)
        room.db.commit()
        with pytest.raises(room.A.AuctionError):
            room.D.confirm_pick(second_db, stale_season, other.token, 2000)
        second_db.rollback()
        assert room.teams[0].squad_size == 1
        assert room.teams[0].purse_remaining_lakh == 7700
    finally:
        second_db.close()


def test_unaffordable_pick_never_overdraws_or_claims_player(room):
    room.D.set_custom_prices(room.db, room.season, 'Joe Root:101cr')
    room.db.commit()
    start(room)
    with pytest.raises(room.A.AuctionError):
        room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    room.db.rollback()
    assert room.teams[0].purse_remaining_lakh == 10000
    assert room.teams[0].squad_size == 0
    assert lot(room, 'Joe Root').status == room.A.LOT_QUEUED


def test_pick_announcement_records_next_owners_and_updated_squad(room):
    from models import AuctionEvent
    start(room)
    pick(room, 'Joe Root', 1000)
    event = (room.db.query(AuctionEvent).filter_by(season_id=room.season.id, kind='fd_picked')
             .order_by(AuctionEvent.id.desc()).first())
    assert event is not None
    announcement = json.loads(event.detail_json)['html']
    assert 'Joe Root' in announcement
    assert 'Team 1' in announcement
    assert 'Team 2' in announcement
    assert 'tg://user?id=1001' in announcement
    assert '77' in announcement
    squad = room.D.render_squad(room.db, room.season, room.teams[0])
    assert 'Joe Root' in squad
    assert '77' in squad


def test_pick_preserves_scarce_roles_for_other_teams(room):
    room.season.role_minimums_json = json.dumps({'Bowler': 1})
    room.db.delete(lot(room, 'Reece Topley'))
    room.db.commit()
    start(room)
    for name in ['Joe Root', 'Jofra Archer', 'Jos Buttler', 'Ben Stokes', 'Harry Brook']:
        pick(room, name)
    # Team 2 already owns a bowler. The other four available bowlers are owed
    # to the other four teams; taking a second would strand one of them.
    with pytest.raises(room.A.AuctionError):
        room.D.preview_pick(room.db, room.season, 1001, 'Mark Wood')
    room.db.rollback()
    pick(room, 'Sam Curran', 1001)
    assert room.teams[1].squad_size == 2


def test_pick_preserves_scarce_low_ratings_for_other_teams(room):
    from models import AuctionLot
    room.A.set_rating_rules(room.db, room.season, [{'max_rating': 80, 'min_players': 1}])
    low = room.db.query(AuctionLot).filter_by(season_id=room.season.id, rating=80).order_by(AuctionLot.id).all()
    for player in low[5:]:
        room.db.delete(player)
    room.db.commit()
    start(room)
    for name in ['Joe Root', 'Alex Hales', 'Jos Buttler', 'Ben Stokes', 'Harry Brook']:
        pick(room, name)
    with pytest.raises(room.A.AuctionError):
        room.D.preview_pick(room.db, room.season, 1001, 'Adil Rashid')
    room.db.rollback()
    pick(room, 'Sam Curran', 1001)
    assert room.teams[1].squad_size == 2


def test_cancellation_in_second_session_overrides_cached_pending_confirmation(room):
    from models import AuctionSeason
    start(room)
    confirmation = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    room.db.commit()
    second_db = room.sessions()
    try:
        other_season = second_db.get(AuctionSeason, room.season.id)
        room.D.cancel_pick(second_db, other_season, confirmation.token, 1000)
        second_db.commit()
        assert confirmation.status == 'pending'  # Deliberately cached in the first session.
        with pytest.raises(room.A.AuctionError):
            room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
        room.db.rollback()
        assert room.teams[0].purse_remaining_lakh == 10000
        assert room.teams[0].squad_size == 0
        assert lot(room, 'Joe Root').status == room.A.LOT_QUEUED
    finally:
        second_db.close()


def test_revoked_owner_in_second_session_overrides_cached_authority(room):
    from models import AuctionFranchise
    start(room)
    confirmation = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    room.db.commit()
    second_db = room.sessions()
    try:
        other_team = second_db.get(AuctionFranchise, room.teams[0].id)
        other_team.owner_tg_id = 9000
        second_db.commit()
        assert room.teams[0].owner_tg_id == 1000
        with pytest.raises(room.A.AuctionError):
            room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
        room.db.rollback()
        assert room.teams[0].purse_remaining_lakh == 10000
        assert room.teams[0].squad_size == 0
    finally:
        second_db.close()


def test_old_confirmation_expires_without_spending_purse(room):
    from datetime import datetime, timedelta
    start(room)
    confirmation = room.D.preview_pick(room.db, room.season, 1000, 'Joe Root')
    confirmation.created_at = datetime.utcnow() - timedelta(minutes=11)
    room.db.commit()
    with pytest.raises(room.A.AuctionError, match='expired'):
        room.D.confirm_pick(room.db, room.season, confirmation.token, 1000)
    room.db.rollback()
    assert room.teams[0].purse_remaining_lakh == 10000
    assert room.teams[0].squad_size == 0


def test_deleting_season_removes_draft_config_confirmations_and_pick_rows(room):
    from models import FranchiseDraft, FranchiseDraftConfirmation, FranchiseDraftPick
    start(room)
    pick(room, 'Joe Root', 1000)
    room.D.preview_pick(room.db, room.season, 1001, 'Ben Stokes')
    room.db.commit()
    assert room.db.query(FranchiseDraft).count() == 1
    assert room.db.query(FranchiseDraftConfirmation).count() == 2
    assert room.db.query(FranchiseDraftPick).count() == 1
    room.db.delete(room.season)
    room.db.commit()
    assert room.db.query(FranchiseDraft).count() == 0
    assert room.db.query(FranchiseDraftConfirmation).count() == 0
    assert room.db.query(FranchiseDraftPick).count() == 0


def add_root_variant(room):
    from models import AuctionLot, Player
    original = lot(room, 'Joe Root')
    player = Player(name='Joe Root', version='Gold', rating=96, category='Batsman',
                    country='England', bat_hand='Right', bowl_hand='Right',
                    bowl_style='Medium Pacer', bat_rating=96, bowl_rating=96)
    room.db.add(player)
    room.db.flush()
    room.A.add_players_to_pool(room.db, room.season, [player])
    variant = room.db.query(AuctionLot).filter_by(season_id=room.season.id, player_id=player.id).one()
    room.db.commit()
    return original.id, variant.id


def test_same_team_cannot_draft_second_version_of_its_player(room):
    original_id, variant_id = add_root_variant(room)
    start(room)
    pick(room, f'#{original_id}', 1000)
    for name in ['Ben Stokes', 'Jos Buttler', 'Jofra Archer', 'Harry Brook',
                 'Sam Curran', 'Phil Salt', 'Mark Wood', 'Dawid Malan']:
        pick(room, name)
    assert room.D.turn(room.db, room.season).owner_tg_id == 1000
    with pytest.raises(room.A.AuctionError, match='one version'):
        room.D.preview_pick(room.db, room.season, 1000, f'#{variant_id}')
    room.db.rollback()
    assert room.teams[0].purse_remaining_lakh == 7700
    assert room.teams[0].squad_size == 1
    pick(room, 'Chris Woakes', 1000)
    league = room.A.publish_to_league(room.db, room.season)
    room.db.commit()
    assert league.id == room.season.league_id


def test_different_teams_can_draft_different_versions_of_same_player(room):
    original_id, variant_id = add_root_variant(room)
    start(room)
    pick(room, f'#{original_id}', 1000)
    pick(room, f'#{variant_id}', 1001)
    for name in ['Ben Stokes', 'Jos Buttler', 'Jofra Archer', 'Harry Brook',
                 'Sam Curran', 'Phil Salt', 'Mark Wood', 'Dawid Malan']:
        pick(room, name)
    assert room.season.status == room.A.STATUS_COMPLETED
    from models import AuctionLot
    roots = room.db.query(AuctionLot).filter_by(season_id=room.season.id, name='Joe Root').all()
    assert {player.sold_to_id for player in roots} == {room.teams[0].id, room.teams[1].id}
    league = room.A.publish_to_league(room.db, room.season)
    room.db.commit()
    assert league.id == room.season.league_id


@pytest.mark.parametrize('operation', ['undo', 'rounds'])
def test_stale_completed_draft_cannot_change_after_other_session_publishes(room, operation):
    from models import AuctionSeason
    start(room)
    for player in room.players[:10]:
        pick(room, player.name)
    cached = room.D.get_draft(room.db, room.season)
    assert room.season.status == room.A.STATUS_COMPLETED
    assert room.season.league_id is None
    purses = [team.purse_remaining_lakh for team in room.teams]
    second_db = room.sessions()
    try:
        other_season = second_db.get(AuctionSeason, room.season.id)
        room.A.publish_to_league(second_db, other_season)
        second_db.commit()
        assert room.season.league_id is None  # The first session retains the old state.
        with pytest.raises(room.A.AuctionError):
            if operation == 'undo':
                room.D.undo_last(room.db, room.season)
            else:
                room.D.configure_rounds(room.db, room.season, 3)
        room.db.rollback()
        assert room.season.league_id is not None
        assert room.season.status == room.A.STATUS_COMPLETED
        assert room.D.get_draft(room.db, room.season).rounds == 2
        assert room.D.get_draft(room.db, room.season).cursor == 10
        assert [team.purse_remaining_lakh for team in room.teams] == purses
        assert all(team.squad_size == 2 for team in room.teams)
    finally:
        second_db.close()


def test_enabling_draft_normalizes_inherited_role_abbreviations(room):
    season = room.A.create_season(room.db, 'Inherited rules',
                                 role_minimums_json=json.dumps({'BAT': 1, 'BOWL': 1, 'AR': 1, 'WK': 1}),
                                 role_maximums_json=json.dumps({'BAT': 2, 'BOWL': 2, 'AR': 2, 'WK': 2}))
    room.D.enable(room.db, season, rounds=4)
    room.db.commit()
    assert room.A.role_minimums(season) == {
        'Batsman': 1, 'Bowler': 1, 'All-rounder': 1, 'Wicket Keeper': 1,
    }
    assert room.A.role_maximums(season) == {
        'Batsman': 2, 'Bowler': 2, 'All-rounder': 2, 'Wicket Keeper': 2,
    }
