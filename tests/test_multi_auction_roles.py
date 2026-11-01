"""Explicit bot and franchise assignments can coexist without role escalation."""

import pytest

from test_auction_roles import (ADMIN, BYPASS, COOWNER, MANAGER, OWNER, OTHER,
                                profile, room)  # noqa: F401

BOT_OWNER, TEAM_MANAGER = 718888, 718889


def test_explicit_bot_owner_is_distinct_from_inherited_admin_authority(room, monkeypatch):
    monkeypatch.setenv('OWNER_IDS', str(BOT_OWNER))
    data = profile(room, BOT_OWNER)
    assert data['roles'] == ['bot_owner']
    assert data['role_labels'] == ['Bot Owner']
    assert data['bot_owner'] is True
    assert data['bot_admin'] is True
    assert data['admin'] is True
    assert data['permissions']['manage_admins'] is True

    room.A.add_auction_admin(room.db, BOT_OWNER, by_tg_id=ADMIN)
    assert profile(room, BOT_OWNER)['roles'] == ['bot_owner', 'auction_manager']
    monkeypatch.setenv('BOT_ADMIN_IDS', f'{ADMIN},{BOT_OWNER}')
    assert profile(room, BOT_OWNER)['roles'] == ['bot_owner', 'bot_admin', 'auction_manager']


def test_owner_fallback_and_maintenance_exemptions_do_not_assign_bot_owner(room):
    from services.admin_ids import is_owner
    # The generic owner helper has a legacy admin fallback; role badges must
    # describe explicit assignments instead of inheriting that fallback.
    assert is_owner(ADMIN) is True
    assert profile(room, ADMIN)['bot_owner'] is False
    assert profile(room, ADMIN)['roles'] == ['bot_admin']
    assert profile(room, BYPASS)['roles'] == ['spectator']


def test_owner_role_revocation_keeps_independent_admin_assignment(room, monkeypatch):
    monkeypatch.setenv('BOT_OWNER_IDS', str(ADMIN))
    before = profile(room, ADMIN)
    assert before['roles'] == ['bot_owner', 'bot_admin']
    monkeypatch.setenv('BOT_OWNER_IDS', '')
    after = profile(room, ADMIN)
    assert after['roles'] == ['bot_admin']
    assert after['bot_owner'] is False
    assert after['permissions'] == before['permissions']


def test_same_team_can_have_multiple_assignments_for_one_viewer(room):
    room.A.set_team_managers(room.db, room.owner, [OWNER, COOWNER])
    room.db.flush()
    assert profile(room, OWNER)['roles'] == ['team_owner', 'team_manager']
    assert profile(room, COOWNER)['roles'] == ['team_co_owner', 'team_manager']
    owner = room.W.team_row(room.db, room.season, room.owner, OWNER)
    assert owner['my_roles'] == ['team_owner', 'team_manager']
    assert owner['my_role_labels'] == ['Team Owner', 'Team Manager']
    assert owner['my_role'] == 'team_owner'
    assert owner['my_role_label'] == 'Team Owner'
    assert owner['mine'] is True
    assert not {'owner_tg_id', 'co_owner_ids_json', 'team_manager_ids_json'} & owner.keys()


def test_different_team_assignments_are_combined_without_losing_scope(room):
    room.A.set_team_managers(room.db, room.other, [OWNER])
    room.db.flush()
    data = profile(room, OWNER)
    assert data['roles'] == ['team_owner', 'team_manager']
    assert set(data['my_team_ids']) == {room.owner.id, room.other.id}
    home = room.W.team_row(room.db, room.season, room.owner, OWNER)
    away = room.W.team_row(room.db, room.season, room.other, OWNER)
    assert home['my_roles'] == ['team_owner']
    assert away['my_roles'] == ['team_manager']


def test_team_manager_has_team_access_without_auction_management(room):
    room.A.set_team_managers(room.db, room.owner, [TEAM_MANAGER])
    room.db.flush()
    data = profile(room, TEAM_MANAGER)
    assert data['roles'] == ['team_manager']
    assert data['role_labels'] == ['Team Manager']
    assert data['permissions'] == {'manage': False, 'manage_admins': False, 'bid': True}
    assert room.W.own_team(room.db, room.season, TEAM_MANAGER, room.owner.id).id == room.owner.id
    with pytest.raises(room.W.AccessError):
        room.W.own_team(room.db, room.season, TEAM_MANAGER, room.other.id)
    with pytest.raises(room.W.AccessError):
        room.W.payload(room.db, room.season, TEAM_MANAGER, [room.season], {'view': 'admin'})
    with pytest.raises(room.W.AccessError):
        room.W.perform(room.db, room.season, TEAM_MANAGER, {
            'action': 'admin_settings', 'settings': {'max_squad_size': 10},
            'roles': ['bot_owner', 'auction_manager'], 'admin': True,
        })


def test_explicit_team_manager_revocation_removes_role_and_access(room):
    room.A.set_team_managers(room.db, room.owner, [TEAM_MANAGER])
    room.db.flush()
    assert profile(room, TEAM_MANAGER)['roles'] == ['team_manager']
    room.A.set_team_managers(room.db, room.owner, [])
    room.db.flush()
    after = profile(room, TEAM_MANAGER)
    assert after['roles'] == ['spectator']
    assert after['my_team_ids'] == []
    assert after['permissions']['bid'] is False
    with pytest.raises(room.W.AccessError):
        room.W.own_team(room.db, room.season, TEAM_MANAGER, room.owner.id)


def test_snapshot_changes_when_manager_assignment_removed_but_owner_remains(room):
    from services.auction_updates import snapshot
    room.A.set_team_managers(room.db, room.owner, [OWNER])
    room.db.flush()
    before = snapshot(room.db, room.season.id, OWNER)
    room.A.set_team_managers(room.db, room.owner, [])
    room.db.flush()
    after = snapshot(room.db, room.season.id, OWNER)
    assert before.authority != after.authority
    assert before.revision != after.revision
    assert profile(room, OWNER)['roles'] == ['team_owner']


def test_snapshot_observes_explicit_global_role_changes(room, monkeypatch):
    from services.auction_updates import snapshot
    before = snapshot(room.db, room.season.id, ADMIN)
    monkeypatch.setenv('OWNER_IDS', str(ADMIN))
    after = snapshot(room.db, room.season.id, ADMIN)
    assert before.authority != after.authority
    assert before.revision != after.revision
    assert set(after.public()) == {'revision', 'season_id'}
