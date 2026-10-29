"""Gender entry updates exact card IDs and rejects invalid batches atomically."""

import csv
import io
import zipfile

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import admin
import database
from models import AdminLog, Player
from services import player_cache, player_gender


@pytest.fixture
def catalogue(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(admin, "get_session", factory)
    monkeypatch.setitem(admin.app.config, "TESTING", True)
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", False)
    monkeypatch.setitem(admin.app.config, "SECRET_KEY", "gender-test-secret")
    with factory() as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(catalogue):
    client = admin.app.test_client()
    with client.session_transaction() as flask_session:
        flask_session["admin"] = True
    return client


def card(session, name="Player", **kwargs):
    player = Player(name=name, rating=90, category="Batsman", country="India",
                    bat_hand="Right", bowl_hand="Right", bowl_style="Medium",
                    **kwargs)
    session.add(player)
    session.flush()
    return player


@pytest.mark.parametrize("content, expected", [
    ("Player ID,Gender\n1,m\n2,FEMALE", {1: "Male", 2: "Female"}),
    ("\ufeffplayer_id,gender\r\n1,f\r\n2,M\r\n", {1: "Female", 2: "Male"}),
    ("\nPlayer ID\tGender\n1\tFemale\n2\t", {1: "Female"}),
    ("1,Other\n2,Non-binary\n3,NB", {1: "Other", 2: "Non-binary", 3: "Non-binary"}),
    ("1,\n2,Not set", {2: None}),
])
def test_csv_and_spreadsheet_inputs(content, expected):
    assert player_gender.parse_gender_rows(content) == expected


@pytest.mark.parametrize("content", [
    "", "Player ID,Gender", "1,", "0,Male", "-1,Female", "1.5,Female",
    "2147483648,Male", "abc,Male", "1,Typo", "1,Male,extra",
    "1,Male\n1,Female", '1,"Female', "Player ID,Name\n1,Male",
])
def test_invalid_input_is_rejected(content):
    with pytest.raises(ValueError):
        player_gender.parse_gender_rows(content)


def test_bulk_changes_only_listed_ids_and_invalidates_cache(catalogue, client, monkeypatch):
    base = card(catalogue, version="Base")
    variant = card(catalogue, version="Legend", parent_player_id=base.id)
    inactive = card(catalogue, name="Inactive", is_active=False)
    catalogue.commit()
    invalidations = []
    monkeypatch.setattr(player_cache, "invalidate", lambda: invalidations.append(True))
    content = f"Player ID,Gender\n{base.id},F\n{inactive.id},male"
    response = client.post("/players/bulk-gender", data={"csv_text": content})
    assert response.status_code == 302
    catalogue.expire_all()
    assert base.gender == "Female"
    assert inactive.gender == "Male"
    assert variant.gender is None
    assert (base.name, base.rating, base.parent_player_id) == ("Player", 90, None)
    assert variant.parent_player_id == base.id
    assert invalidations == [True]
    assert catalogue.query(AdminLog).filter_by(action="players_bulk_gender").count() == 1
    # Re-importing the same data preserves the values and reports no changes.
    response = client.post("/players/bulk-gender", data={"csv_text": content})
    assert response.status_code == 302
    with client.session_transaction() as flask_session:
        assert "0 player(s); 2 unchanged" in str(flask_session["_flashes"])
    assert invalidations == [True]


@pytest.mark.parametrize("bad_row, error", [
    ("999999,Female", "Player IDs not found"),
    ("2,Typo", "Gender must be"),
    ("1,Female", "appears more than once"),
])
def test_invalid_batch_saves_nothing(catalogue, client, bad_row, error):
    player = card(catalogue, gender="Other")
    catalogue.commit()
    content = f"{player.id},Male\n{bad_row}"
    response = client.post("/players/bulk-gender", data={"csv_text": content})
    assert response.status_code == 400
    assert error in response.text
    assert "No changes were saved" in response.text
    assert content in response.text
    catalogue.expire_all()
    assert player.gender == "Other"
    assert catalogue.query(AdminLog).count() == 0


def test_utf8_csv_upload_preserves_blanks_and_can_clear_gender(catalogue, client):
    keep = card(catalogue, gender="Female")
    clear = card(catalogue, name="Clear", gender="Other")
    catalogue.commit()
    content = f"\ufeffPlayer ID,Gender\r\n{keep.id},\r\n{clear.id},Not set\r\n"
    response = client.post("/players/bulk-gender", data={
        "csv_file": (io.BytesIO(content.encode("utf-8")), "genders.csv"),
    })
    assert response.status_code == 302
    catalogue.expire_all()
    assert keep.gender == "Female"
    assert clear.gender is None


def test_template_round_trip_includes_inactive_players(catalogue, client):
    player = card(catalogue, gender="Male")
    inactive = card(catalogue, name="Inactive", is_active=False)
    catalogue.commit()
    response = client.get("/players/gender-template")
    rows = list(csv.reader(io.StringIO(response.text)))
    assert rows == [["Player ID", "Gender"], [str(player.id), "Male"], [str(inactive.id), ""]]
    rows[2][1] = "Female"
    content = io.StringIO()
    csv.writer(content).writerows(rows)
    assert client.post("/players/bulk-gender", data={"csv_text": content.getvalue()}).status_code == 302
    catalogue.expire_all()
    assert (player.gender, inactive.gender) == ("Male", "Female")


def test_manual_entry_validates_gender_and_preserves_it_for_old_forms(catalogue, client):
    form = {"name": "Manual", "rating": "90", "category": "Batsman", "country": "India",
            "gender": "female"}
    assert client.post("/players/add", data=form).status_code == 302
    player = catalogue.query(Player).one()
    assert player.gender == "Female"
    assert 'value="Female" selected' in client.get(f"/players/{player.id}/edit").text
    form.pop("gender")
    assert client.post(f"/players/{player.id}/edit", data=form).status_code == 302
    catalogue.expire_all()
    assert player.gender == "Female"
    form["gender"] = "Typo"
    assert client.post(f"/players/{player.id}/edit", data=form).status_code == 302
    catalogue.expire_all()
    assert player.gender == "Female"
    form["gender"] = ""
    assert client.post(f"/players/{player.id}/edit", data=form).status_code == 302
    catalogue.expire_all()
    assert player.gender is None


def test_list_and_downloads_respect_gender_filter(catalogue, client):
    female = card(catalogue, name="Female player", gender="Female")
    card(catalogue, name="Male player", gender="Male")
    unset = card(catalogue, name="Unset player")
    catalogue.commit()
    response = client.get("/players?gender=Female")
    assert response.status_code == 200
    assert "Female player" in response.text
    assert "Male player" not in response.text
    assert "Player ID" in response.text
    assert "bulk-gender" in response.text
    assert "gender=Female" in response.text
    response = client.get("/players?gender=unset")
    assert response.status_code == 200
    assert "Unset player" in response.text
    assert "Female player" not in response.text
    rows = client.get("/players/download?format=json&gender=Female").get_json()
    assert [(row["id"], row["gender"]) for row in rows] == [(female.id, "Female")]
    csv_rows = list(csv.DictReader(io.StringIO(client.get("/players/download?gender=unset").text)))
    assert [(row["id"], row["gender"]) for row in csv_rows] == [(str(unset.id), "")]
    response = client.get("/players/download?format=xlsx&gender=Female")
    with zipfile.ZipFile(io.BytesIO(response.data)) as workbook:
        sheet = workbook.read("xl/worksheets/sheet1.xml").decode()
    assert "gender" in sheet and "Female" in sheet


@pytest.mark.parametrize("path", ["/players/bulk-gender", "/players/gender-template"])
def test_gender_routes_require_admin_login(catalogue, path):
    response = admin.app.test_client().get(path)
    assert response.status_code == 302
    assert "/login" in response.location


def test_gender_import_keeps_csrf_protection(catalogue, client, monkeypatch):
    player = card(catalogue)
    catalogue.commit()
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", True)
    response = client.post("/players/bulk-gender", data={"csv_text": f"{player.id},Female"})
    assert response.status_code == 302
    catalogue.expire_all()
    assert player.gender is None


def test_existing_database_migration_is_idempotent_and_preserves_players(catalogue, monkeypatch):
    player = card(catalogue)
    catalogue.commit()
    engine = catalogue.get_bind()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE players DROP COLUMN gender"))
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(database, "_is_postgres", False)
    database._migrate_add_columns()
    database._migrate_add_columns()
    assert "gender" in {column["name"] for column in inspect(engine).get_columns("players")}
    catalogue.expire_all()
    assert (player.name, player.rating, player.gender) == ("Player", 90, None)
