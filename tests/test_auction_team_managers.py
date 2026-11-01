"""Team Managers retain team-scoped authority across auction persistence paths."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

OWNER, COOWNER, MANAGER, OTHER = 771001, 771002, 771003, 771004


@pytest.fixture
def room(tmp_path, monkeypatch):
    from database import Base
    import models
    from services import admin_ids, auction_service as A

    for key in (*admin_ids.ADMIN_ID_ENV_VARS, *admin_ids.OWNER_ID_ENV_VARS):
        monkeypatch.setenv(key, '')
    monkeypatch.setattr(admin_ids, 'get_config', lambda: {})
    engine = create_engine('sqlite:///' + str(tmp_path / 'team-managers.db'))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = sessions()
    season = A.create_season(db, 'Team manager season', min_squad_size=0,
                             max_squad_size=5, max_overseas=5, auto_accelerated=0)
    A.bind_chat(db, season, -100771001)
    team = A.create_franchise(db, season, 'Home', owner_tg_id=OWNER)
    A.set_co_owners(db, team, [COOWNER])
    away = A.create_franchise(db, season, 'Away', owner_tg_id=OTHER)
    player = models.Player(name='Manager test player', rating=85, category='Batsman',
                           country='India', version='Base', bat_hand='Right',
                           bowl_hand='Right', bowl_style='Medium Pacer',
                           bat_rating=85, bowl_rating=80, is_active=True)
    db.add(player)
    db.flush()
    A.add_players_to_pool(db, season, [player], set_name='Main')
    db.commit()
    yield SimpleNamespace(db=db, engine=engine, sessions=sessions, A=A,
                          season=season, team=team, away=away, player=player)
    db.close()
    engine.dispose()


def test_manager_assignment_is_distinct_and_can_overlap_other_team_roles(room):
    A, team = room.A, room.team
    assert A.team_manager_ids(team) == []
    assert team.team_manager_ids_json is None
    assert A.set_team_managers(room.db, team, [MANAGER, str(MANAGER), OWNER, COOWNER]) == [MANAGER, OWNER, COOWNER]
    assert A.co_owner_ids(team) == [COOWNER]
    assert team.owner_tg_id == OWNER
    assert A.owner_ids(team) == [OWNER, COOWNER, MANAGER]
    assert A.may_bid_for(team, MANAGER)
    assert not A.may_bid_for(room.away, MANAGER)
    assert not A.is_auction_admin(room.db, MANAGER), 'A team assignment never grants global auction settings'
    room.db.commit()
    room.db.expire_all()
    assert A.team_manager_ids(room.db.get(type(team), team.id)) == [MANAGER, OWNER, COOWNER]


@pytest.mark.parametrize('invalid', [True, 0, -1, 1.5, '1.5', '-1', 'hello', 2**63, [], {}])
def test_invalid_assignment_cannot_partially_replace_valid_managers(invalid):
    from services import auction_service as A
    team = SimpleNamespace(team_manager_ids_json=json.dumps([MANAGER]))
    with pytest.raises(A.AuctionError, match='positive Telegram'):
        A.set_team_managers(None, team, [OTHER, invalid])
    assert A.team_manager_ids(team) == [MANAGER]


@pytest.mark.parametrize('stored', [None, '{}', 'not json', 'null', 'true', '123'])
def test_malformed_stored_manager_lists_grant_nobody(stored):
    from services import auction_service as A
    team = SimpleNamespace(owner_tg_id=None, co_owner_ids_json='[]', team_manager_ids_json=stored)
    assert A.team_manager_ids(team) == []
    assert not A.may_bid_for(team, MANAGER)


def test_manager_can_bid_only_for_assigned_franchise_and_revocation_is_current(room):
    from services import auction_webapp as W
    A, db = room.A, room.db
    A.set_team_managers(db, room.team, [MANAGER])
    A.start(db, room.season)
    db.commit()
    lot = A.current_lot(db, room.season)
    command = dict(action='bid', team_id=room.away.id, lot_id=lot.id,
                   stage=lot.rtm_stage, expected_bids=lot.bid_count,
                   amount_lakh=A.next_min_bid(room.season, lot))
    with pytest.raises(W.AccessError):
        W.perform(db, room.season, MANAGER, command)
    command['team_id'] = room.team.id
    W.perform(db, room.season, MANAGER, command)
    db.commit()
    from models import AuctionBid
    assert lot.current_bidder_id == room.team.id
    assert db.query(AuctionBid).filter_by(lot_id=lot.id).order_by(AuctionBid.id.desc()).first().by_tg_id == MANAGER
    assert A.franchise_for_actor(db, room.season.id, MANAGER).id == room.team.id
    A.set_team_managers(db, room.team, [])
    db.commit()
    db.expire_all()
    assert A.franchise_for_actor(db, room.season.id, MANAGER) is None
    with pytest.raises(W.AccessError):
        W.perform(db, room.season, MANAGER, command)
    assert A.may_bid_for(room.team, OWNER)
    assert A.may_bid_for(room.team, COOWNER)


def test_manager_assignment_rollback_keeps_previous_assignment(room):
    A, db = room.A, room.db
    A.set_team_managers(db, room.team, [MANAGER])
    db.commit()
    A.set_team_managers(db, room.team, [])
    db.flush()
    db.rollback()
    assert A.team_manager_ids(room.team) == [MANAGER]


def test_clone_and_export_import_preserve_manager_lists_without_purse_results(room):
    A, db = room.A, room.db
    A.set_team_managers(db, room.team, [MANAGER, OWNER])
    A.set_team_managers(db, room.away, [])
    db.commit()
    clone = A.clone_season(db, room.season, 'Next season')
    cloned = {t.name:t for t in A.franchises(db, clone.id)}
    assert A.team_manager_ids(cloned['Home']) == [MANAGER, OWNER]
    assert cloned['Away'].team_manager_ids_json == '[]'
    assert cloned['Home'].squad_size == 0
    exported = A.export_franchises(db, room.season)
    exported_home = next(t for t in exported['franchises'] if t['name'] == 'Home')
    assert exported_home['team_manager_tg_ids'] == [MANAGER, OWNER]
    imported = A.create_season(db, 'Imported season', min_squad_size=0)
    A.import_franchises(db, imported, exported)
    imported_home = next(t for t in A.franchises(db, imported.id) if t.name == 'Home')
    assert A.team_manager_ids(imported_home) == [MANAGER, OWNER]
    A.import_franchises(db, imported, {'franchises':[{'name':'Home','team_manager_tg_ids':[], 'team_managers':[MANAGER]}]})
    assert A.team_manager_ids(imported_home) == [], 'Canonical explicit [] wins over an alias'
    A.import_franchises(db, imported, {'franchises':[{'name':'Home','team_managers':[OTHER]}]})
    assert A.team_manager_ids(imported_home) == [OTHER]
    A.import_franchises(db, imported, {'franchises':[{'name':'Home'}]})
    assert A.team_manager_ids(imported_home) == [OTHER], 'Omitting assignments preserves the existing field'


def previous_link(room):
    from models import ChallengeMode, ChallengeLeague
    A, db = room.A, room.db
    mode = ChallengeMode(name='Previous league mode')
    db.add(mode); db.flush()
    league = ChallengeLeague(mode_id=mode.id, name='Previous league')
    db.add(league); db.flush()
    room.season.league_id = league.id
    A.set_team_managers(db, room.team, [MANAGER])
    following = A.create_season(db, 'Following season', min_squad_size=0)
    following.previous_season_id = room.season.id
    following.previous_league_id = league.id
    target = A.create_franchise(db, following, 'Renamed Home', carried_from_id=room.team.id)
    db.flush()
    return following, target


def test_exact_previous_link_restores_unconfigured_managers_and_respects_revocation(room):
    following, target = previous_link(room)
    room.A.restore_retention_owners(room.db, following, target)
    assert target.owner_tg_id == OWNER
    assert room.A.team_manager_ids(target) == [MANAGER]
    room.A.set_team_managers(room.db, target, [])
    room.A.restore_retention_owners(room.db, following, target)
    assert room.A.team_manager_ids(target) == [], 'An explicit revocation cannot be restored from last season'


def test_previous_link_never_reassigns_a_changed_or_explicitly_empty_owner(room):
    following, target = previous_link(room)
    target.owner_tg_id = OTHER
    room.A.restore_retention_owners(room.db, following, target)
    assert room.A.team_manager_ids(target) == []
    target.owner_tg_id = None
    room.A.set_co_owners(room.db, target, [])
    room.A.restore_retention_owners(room.db, following, target)
    assert target.owner_tg_id is None
    assert target.co_owner_ids_json == '[]'
    assert target.team_manager_ids_json is None


def test_additive_schema_migration_preserves_legacy_rows_and_is_idempotent(room, monkeypatch):
    import database
    room.db.close()
    with room.engine.begin() as conn:
        conn.execute(text('ALTER TABLE auction_franchises DROP COLUMN team_manager_ids_json'))
    assert 'team_manager_ids_json' not in {c['name'] for c in inspect(room.engine).get_columns('auction_franchises')}
    monkeypatch.setattr(database, 'engine', room.engine)
    monkeypatch.setattr(database, '_is_postgres', False)
    database._migrate_add_columns()
    columns = {c['name'] for c in inspect(room.engine).get_columns('auction_franchises')}
    assert 'team_manager_ids_json' in columns
    with room.engine.connect() as conn:
        row = conn.execute(text('SELECT owner_tg_id, co_owner_ids_json, team_manager_ids_json FROM auction_franchises WHERE id=:id'), {'id':room.team.id}).one()
    assert row == (OWNER, json.dumps([COOWNER], separators=(',',':')), None)
    alterations = []
    def observed(conn, cursor, statement, parameters, context, executemany):
        if 'ALTER TABLE auction_franchises ADD COLUMN' in statement and 'team_manager_ids_json' in statement:
            alterations.append(statement)
    event.listen(room.engine, 'before_cursor_execute', observed)
    try:
        database._migrate_add_columns()
    finally:
        event.remove(room.engine, 'before_cursor_execute', observed)
    assert alterations == [], 'A warm startup does not attempt the same migration again'
