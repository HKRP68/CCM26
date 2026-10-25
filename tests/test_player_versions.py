"""Version families must survive manual entry, old data and both bulk formats."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import admin
from database import Base
from models import Player, User, UserRoster
from services import version_service as versions
from services.version_paginator import find_players_for_search, get_versions_ordered


@pytest.fixture
def catalogue(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(admin, "get_session", factory)
    monkeypatch.setitem(admin.app.config, "TESTING", True)
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", False)
    monkeypatch.setitem(admin.app.config, "SECRET_KEY", "versions-test-secret")
    with factory() as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(catalogue):
    client = admin.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["admin"] = True
    return client


def card(session, name="Virat Kohli", version="Base", **kwargs):
    row = Player(name=name, version=version, rating=kwargs.pop("rating", 90),
                 category="Batsman", country="India", bat_hand="Right",
                 bowl_hand="Right", bowl_style="Medium", **kwargs)
    session.add(row)
    session.flush()
    return row


def csv_row(name, version):
    return f"{name},90,Bat,India,R,R,Medium,90,30,{version},1"


@pytest.mark.parametrize("main_label", ["Base card", "Legend Player", "Star Card"])
def test_imported_specials_resolve_without_links(catalogue, main_label):
    special = card(catalogue, version="World Cup")
    main = card(catalogue, name=" VIRAT KOHLI ", version=main_label)
    career = card(catalogue, version="Career", is_career=True)
    catalogue.commit()
    assert versions.get_base_id(special.id, catalogue) == main.id
    assert [c.id for c in versions.get_all_versions(catalogue, special.id)] == [main.id, special.id]
    assert [c.id for c in get_versions_ordered(catalogue, special.id)] == [main.id, special.id]
    assert versions.get_all_versions(catalogue, career.id) == [career]


def test_repair_is_idempotent_and_preserves_card_data(catalogue):
    special = card(catalogue, version="World Cup", rating=99, is_active=False,
                   image_url="card-image.png")
    star = card(catalogue, version="Star")
    main = card(catalogue, version="Base card")
    legend = card(catalogue, version="Legend", parent_player_id=star.id)
    career = card(catalogue, version="Career", is_career=True)
    assert versions.sync_version_links(catalogue) == 3
    assert all(c.parent_player_id == main.id for c in (special, star, legend))
    assert main.parent_player_id is None
    assert career.parent_player_id is None
    assert (special.rating, special.image_url, special.is_active) == (99, "card-image.png", False)
    assert versions.sync_version_links(catalogue) == 0


def test_owned_inactive_edition_still_blocks_second_version(catalogue):
    main = card(catalogue)
    special = card(catalogue, version="Gold", is_active=False)
    user = User(telegram_id=1001, username="versions")
    catalogue.add(user)
    catalogue.flush()
    catalogue.add(UserRoster(user_id=user.id, player_id=special.id))
    catalogue.flush()
    assert versions.get_all_versions(catalogue, main.id) == [main]
    assert versions.user_owns_any_version(catalogue, user.id, main.id)


@pytest.mark.parametrize("main_label", ["Base card", "Legend card", "Star Player"])
def test_csv_bulk_links_every_order_and_skips_batch_duplicates(catalogue, client, main_label):
    rows = [csv_row("Virat Kohli", "World Cup"), csv_row("VIRAT KOHLI", main_label),
            csv_row("virat kohli", "world cup"), csv_row("Virat Kohli", "Gold")]
    response = client.post("/players/bulk-upload", data={"csv_text": "\n".join(rows)})
    assert response.status_code == 302
    cards = catalogue.query(Player).order_by(Player.id).all()
    assert len(cards) == 3
    main = next(c for c in cards if c.version == versions.normalize_version(main_label))
    assert main.parent_player_id is None
    assert all(c.parent_player_id == main.id for c in cards if c.id != main.id)
    response = client.post("/players/bulk-upload", data={"csv_text": "\n".join(rows)})
    assert response.status_code == 302
    assert catalogue.query(Player).count() == 3


def test_later_base_upload_reparents_existing_legend_family(catalogue, client):
    legend = card(catalogue, version="Legend")
    special = card(catalogue, version="Gold", parent_player_id=legend.id)
    catalogue.commit()
    client.post("/players/bulk-upload", data={"csv_text": csv_row("Virat Kohli", "Base")})
    catalogue.expire_all()
    main = catalogue.query(Player).filter_by(version="Base").one()
    assert legend.parent_player_id == special.parent_player_id == main.id


def test_json_bulk_uses_same_family_and_duplicate_rules(catalogue):
    card(catalogue, name="Virat Kohli", version="Star Card")
    catalogue.commit()
    rows = [{"Name": "Virat Kohli", "Version": "World Cup", "Rating": 99},
            {"Name": "VIRAT KOHLI", "Version": "legend player", "Rating": 95},
            {"Name": "virat kohli", "Version": "WORLD CUP", "Rating": 99}]
    assert admin._seed_from_json(rows) == 2
    catalogue.expire_all()
    cards = catalogue.query(Player).all()
    main = next(c for c in cards if c.version == "Legend")
    assert all(c.parent_player_id == main.id for c in cards if c.id != main.id)
    assert admin._seed_from_json(rows) == 0


def test_duplicate_only_upload_repairs_legacy_links(catalogue, client):
    main = card(catalogue, version="Base card")
    special = card(catalogue, version="Gold")
    catalogue.commit()
    client.post("/players/bulk-upload", data={"csv_text": csv_row("virat kohli", "base")})
    catalogue.expire_all()
    assert catalogue.query(Player).count() == 2
    assert special.parent_player_id == main.id


def test_manager_only_offers_families_and_lists_detected_versions(catalogue, client):
    main = card(catalogue, version="Legend")
    card(catalogue, version="Gold")
    solo = card(catalogue, name="Solo Player")
    card(catalogue, name="Career Only", version="Base")
    card(catalogue, name="Career Only", version="Career", is_career=True)
    catalogue.commit()
    response = client.get(f"/players/versions?main_player_id={main.id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'name="main_player_id"' in html and 'name="version_ids"' in html
    assert "Gold" in html and "Detected by name" in html
    assert "Solo Player" not in html and "Career Only" not in html
    assert f'value="{solo.id}"' not in html


def test_manager_adds_multiple_versions_and_keeps_existing_links(catalogue, client):
    main = card(catalogue)
    old = card(catalogue, version="Star", parent_player_id=main.id)
    gold = card(catalogue, version="Gold")
    cup = card(catalogue, version="World Cup")
    catalogue.commit()
    response = client.post("/players/versions", data={"action": "add", "main_player_id": main.id,
                                                    "version_ids": [gold.id, cup.id]})
    assert response.status_code == 302
    catalogue.expire_all()
    assert all(c.parent_player_id == main.id for c in (old, gold, cup))


@pytest.mark.parametrize("bad_selection", ["foreign", "self", "empty", "invalid"])
def test_manager_rejects_invalid_selection_atomically(catalogue, client, bad_selection):
    main = card(catalogue)
    gold = card(catalogue, version="Gold")
    foreign = card(catalogue, name="Other Player")
    catalogue.commit()
    selected = {"foreign": [gold.id, foreign.id], "self": [main.id],
                "empty": [], "invalid": ["bad-id"]}[bad_selection]
    client.post("/players/versions", data={"action": "add", "main_player_id": main.id,
                                          "version_ids": selected})
    catalogue.expire_all()
    assert gold.parent_player_id is None and foreign.parent_player_id is None


def test_manager_repair_fixes_old_uploads(catalogue, client):
    main = card(catalogue, version="Star Card")
    gold = card(catalogue, version="Gold")
    catalogue.commit()
    response = client.post("/players/versions", data={"action": "repair"})
    assert response.status_code == 302
    catalogue.expire_all()
    assert gold.parent_player_id == main.id


def test_manual_add_accepts_another_edition_and_rejects_duplicate(catalogue, client):
    main = card(catalogue, version="Legend")
    catalogue.commit()
    form = {"name": "Virat Kohli", "rating": "98", "category": "Batsman",
            "country": "India", "version": "World Cup"}
    assert client.post("/players/add", data=form).status_code == 302
    special = catalogue.query(Player).filter_by(version="World Cup").one()
    assert special.parent_player_id == main.id
    assert client.post("/players/add", data=form).status_code == 302
    assert catalogue.query(Player).count() == 2


def test_manager_requires_login(catalogue):
    response = admin.app.test_client().get("/players/versions")
    assert response.status_code == 302
    assert "/login" in response.location


def test_main_version_filter_uses_label_instead_of_missing_parent(catalogue):
    base = card(catalogue, version="Base card")
    legend = card(catalogue, name="Legend Only", version="Legend Player")
    special = card(catalogue, name="Unlinked Special", version="Gold")
    assert versions.filter_version(catalogue.query(Player), "Base").all() == [base]
    assert versions.filter_version(catalogue.query(Player), "Legend").all() == [legend]
    assert versions.filter_version(catalogue.query(Player), "Gold").all() == [special]


def test_data_change_matches_legacy_version_labels(catalogue, client):
    main = card(catalogue, version="Star Card", rating=70)
    catalogue.commit()
    response = client.post("/players/data-change", data={"csv_text": csv_row("virat kohli", "Star")})
    assert response.status_code == 302
    catalogue.expire_all()
    assert main.rating == 90


def test_manager_has_csrf_protection(catalogue, client, monkeypatch):
    main = card(catalogue)
    gold = card(catalogue, version="Gold")
    catalogue.commit()
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", True)
    # The admin panel's shared CSRF handler redirects back with an error.
    response = client.post("/players/versions", data={"action": "repair"})
    assert response.status_code == 302
    catalogue.expire_all()
    assert gold.parent_player_id is None


def test_search_collapses_unlinked_versions_before_repair(catalogue):
    card(catalogue, version="World Cup", rating=99)
    main = card(catalogue, version="Legend", rating=95)
    other = card(catalogue, name="Virat Singh", rating=85)
    assert find_players_for_search(catalogue, "Virat Kohli") == [main]
    assert find_players_for_search(catalogue, "Virat") == [main, other]
