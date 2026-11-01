"""Verified auction roles stay distinct and follow current database authority."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ADMIN, MANAGER, OWNER, COOWNER, OTHER, BYPASS = range(719001, 719007)


@pytest.fixture
def room(tmp_path, monkeypatch):
    from database import Base
    import models  # noqa: F401 — register tables on the isolated metadata
    from services import admin_ids, auction_service as A, auction_webapp as W

    for name in (*admin_ids.ADMIN_ID_ENV_VARS, *admin_ids.OWNER_ID_ENV_VARS):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("BOT_ADMIN_IDS", str(ADMIN))
    monkeypatch.setattr(admin_ids, "get_config", lambda: {
        "maintenance_bypass_ids": str(BYPASS),
    })
    engine = create_engine("sqlite:///" + str(tmp_path / "roles.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    db = sessions()
    season = A.create_season(db, "Role test season", min_squad_size=0,
                             max_squad_size=5, max_overseas=5)
    owner = A.create_franchise(db, season, "Home", owner_tg_id=OWNER)
    A.set_co_owners(db, owner, [COOWNER])
    other = A.create_franchise(db, season, "Away", owner_tg_id=OTHER)
    A.add_auction_admin(db, MANAGER, by_tg_id=ADMIN)
    db.commit()
    yield SimpleNamespace(db=db, A=A, W=W, season=season, owner=owner, other=other)
    db.close()
    engine.dispose()


def profile(room, actor, *, season=True):
    return room.W.base_payload(room.db, room.season if season else None,
                                actor, [room.season] if season else [])


@pytest.mark.parametrize("actor,roles,labels,manage,manage_admins,bid", [
    (ADMIN, ["bot_admin"], ["Bot Admin"], True, True, False),
    (MANAGER, ["auction_manager"], ["Auction Manager"], True, False, False),
    (OWNER, ["team_owner"], ["Team Owner"], False, False, True),
    (COOWNER, ["team_co_owner"], ["Team Co-owner"], False, False, True),
    (999999, ["spectator"], ["Spectator"], False, False, False),
])
def test_roles_describe_current_verified_authority(room, actor, roles, labels,
                                                   manage, manage_admins, bid):
    data = profile(room, actor)
    assert data["roles"] == roles
    assert data["role_labels"] == labels
    assert data["primary_role"] == roles[0]
    assert data["role_label"] == labels[0]
    assert data["admin"] is manage
    assert data["bot_admin"] is manage_admins
    assert data["permissions"] == {
        "manage": manage, "manage_admins": manage_admins, "bid": bid,
    }
    assert data["role"] == ("manager" if manage else "team" if bid else "spectator")


def test_combined_roles_keep_management_and_franchise_relationships(room):
    room.owner.owner_tg_id = MANAGER
    room.db.flush()
    manager = profile(room, MANAGER)
    assert manager["roles"] == ["auction_manager", "team_owner"]
    assert manager["role_labels"] == ["Auction Manager", "Team Owner"]
    room.owner.owner_tg_id = ADMIN
    room.db.flush()
    assert profile(room, ADMIN)["roles"] == ["bot_admin", "team_owner"]
    room.owner.owner_tg_id = OWNER
    room.A.set_co_owners(room.db, room.other, [OWNER])
    room.db.flush()
    assert profile(room, OWNER)["roles"] == ["team_owner", "team_co_owner"]


def test_revoked_manager_owner_and_coowner_labels_disappear_immediately(room):
    room.owner.owner_tg_id = MANAGER
    room.db.flush()
    assert profile(room, MANAGER)["roles"] == ["auction_manager", "team_owner"]
    room.A.remove_auction_admin(room.db, MANAGER, by_tg_id=ADMIN)
    assert profile(room, MANAGER)["roles"] == ["team_owner"]
    room.owner.owner_tg_id = OTHER
    room.db.flush()
    data = profile(room, MANAGER)
    assert data["roles"] == ["spectator"]
    assert data["my_team_ids"] == []
    assert data["permissions"] == {"manage": False, "manage_admins": False, "bid": False}
    room.A.set_co_owners(room.db, room.owner, [])
    assert profile(room, COOWNER)["roles"] == ["spectator"]


def test_maintenance_bypass_does_not_create_admin_or_manager_role(room):
    data = profile(room, BYPASS)
    assert data["roles"] == ["spectator"]
    assert data["admin"] is False
    assert data["bot_admin"] is False


def test_team_role_is_specific_to_viewer_and_does_not_expose_owner_ids(room):
    owner = room.W.team_row(room.db, room.season, room.owner, OWNER)
    coowner = room.W.team_row(room.db, room.season, room.owner, COOWNER)
    other = room.W.team_row(room.db, room.season, room.owner, OTHER)
    assert (owner["my_role"], owner["my_role_label"], owner["mine"]) == (
        "team_owner", "Team Owner", True,
    )
    assert (coowner["my_role"], coowner["my_role_label"], coowner["mine"]) == (
        "team_co_owner", "Team Co-owner", True,
    )
    assert (other["my_role"], other["my_role_label"], other["mine"]) == (None, None, False)
    for row in (owner, coowner, other):
        assert not {"owner_tg_id", "co_owners", "co_owner_ids_json"} & row.keys()


def test_roles_without_selected_season_report_only_global_authority(room):
    assert profile(room, ADMIN, season=False)["roles"] == ["bot_admin"]
    assert profile(room, MANAGER, season=False)["roles"] == ["auction_manager"]
    data = profile(room, OWNER, season=False)
    assert data["roles"] == ["spectator"]
    assert data["my_team_ids"] == []
    assert data["season"] is None


def test_client_role_claims_cannot_change_labels_or_authority(room):
    data = room.W.payload(room.db, room.season, OWNER, [room.season], {
        "view": "live", "roles": ["bot_admin"], "role_labels": ["Bot Admin"],
        "admin": True, "bot_admin": True,
    })
    assert data["roles"] == ["team_owner"]
    assert data["role_labels"] == ["Team Owner"]
    assert data["admin"] is False
    assert data["bot_admin"] is False
