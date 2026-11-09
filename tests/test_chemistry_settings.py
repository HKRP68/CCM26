"""Card Variety edition counts, configurable scoring and real website saves."""

import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services import chemistry


def cards(*versions):
    return [dict(name=f"Private player {i}", version=version, country="India",
                 category="Batsman") for i, version in enumerate(versions)]


def settings(target=4, versions=None):
    return {"chemistry_variety_target": target,
            "chemistry_variety_versions_json": json.dumps(versions)
            if versions is not None else None}


def test_requested_summary_counts_duplicates_without_listing_players():
    players = cards("IPL2026", "IPL2026", "T20WC", "BBL2025", "Base")
    report = chemistry.calculate_role_report(players, config={})
    assert (report["variety"], report["variety_types"]) == (7, 3)
    text = chemistry.render_chemistry_card(iter(players), config={})
    assert "You have <b>3 special card types</b>" in text
    assert "2× IPL2026, 1× BBL2025, 1× T20WC" in text
    assert "Private player" not in text


def test_years_and_unrelated_events_are_distinct_but_cosmetic_suffixes_are_not():
    detail = chemistry.card_variety_details(cards(
        "IPL2026", "  ipl2026  card ", "IPL2025", "T20WC", "Ashes",
        "Base   card", "BASE card edition", None, "Common"), config={})
    assert detail["types"] == 4
    assert detail["score"] == 10
    assert detail["versions"][0] == {"version": "IPL2026", "count": 2}
    assert sum(item["count"] for item in detail["versions"]) == 5


def test_allowlist_controls_the_counts_score_match_boosts_and_summary():
    players = cards("IPL2026", "IPL2026", "T20WC", "BBL2025", "Icon")
    cfg = settings(target=2, versions=["IPL2026", "T20WC"])
    report = chemistry.calculate_role_report(players, config=cfg)
    assert (report["variety"], report["variety_types"], report["variety_target"]) == (10, 2, 2)
    text = chemistry.render_chemistry_card(players, config=cfg)
    assert "2× IPL2026, 1× T20WC" in text
    assert "BBL2025" not in text
    assert "1× Icon" not in text
    disabled = settings(target=2, versions=[])
    assert chemistry.card_variety(players, config=disabled) == (0, 0)
    assert chemistry.match_boosts(players, config=cfg)["Batsman"] > chemistry.match_boosts(
        players, config=disabled)["Batsman"]
    assert not any("Card Variety" in tip for _, tip in
                   chemistry.improvement_tips(players, limit=10, config=disabled))
    assert chemistry.is_icon(players[-1]), "Eligibility must not disable Icon effects"


@pytest.mark.parametrize("target", range(1, 12))
def test_all_supported_targets_reach_ten_without_duplicate_card_exploits(target):
    cfg = settings(target)
    scores = [chemistry.card_variety(cards(*(f"Drop {i}" for i in range(n))), cfg)[0]
              for n in range(13)]
    assert scores == sorted(scores)
    assert scores[0] == 0 and scores[target] == 10 and max(scores) == 10
    assert scores[target - 1] < 10
    score, count = chemistry.card_variety(cards(*(["IPL2026"] * 11)), cfg)
    assert count == 1
    assert score == (10 if target == 1 else 0)


def test_missing_edition_advice_uses_the_selected_target_and_escapes_labels():
    cfg = settings(3, ["IPL2026", "T20WC", "BBL & <2025>"])
    tips = chemistry.improvement_tips(cards("IPL2026", "T20WC"), limit=10, config=cfg)
    tip = next(text for _, text in tips if "Card Variety" in text)
    assert "Reach 3 for 10/10" in tip
    assert "BBL &amp; &lt;2025&gt;" in tip


def test_advice_never_asks_for_more_types_than_the_allowlist_contains():
    cfg = settings(4, ["IPL2026"])
    assert not any("Card Variety" in tip for _, tip in
                   chemistry.improvement_tips(cards("Base"), limit=10, config=cfg))


def test_large_edition_selections_keep_the_telegram_guide_readable(monkeypatch):
    from handlers import chemhelp
    from services import config_service

    cfg = settings(11, [f"Edition {i:03d}" for i in range(100)])
    monkeypatch.setattr(config_service, "get_chemistry_config", lambda: cfg)
    text = chemhelp.SECTIONS["bonus"]["body"]()
    assert "plus 92 more eligible editions" in text
    assert len(text) < 4096


@pytest.fixture
def chemistry_site(tmp_path, monkeypatch):
    import admin
    import database
    from models import Base, Player
    from services import config_service

    engine = create_engine("sqlite:///" + str(tmp_path / "chemistry.db"))
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(admin, "get_session", sessions)
    monkeypatch.setattr(database, "get_session", sessions)
    monkeypatch.setattr(config_service, "_CACHE", {"data": None, "loaded_at": None})
    monkeypatch.setattr(config_service, "_CHEMISTRY_CACHE", {"data": None, "loaded_at": 0.0})
    monkeypatch.setitem(admin.app.config, "TESTING", True)
    monkeypatch.setitem(admin.app.config, "SESSION_COOKIE_SECURE", False)
    with sessions() as db:
        for i, version in enumerate(["Base", "IPL2026", "IPL2026 card", "T20WC",
                                     "BBL2025", "IPL2025"]):
            db.add(Player(name=f"Player {i}", version=version, rating=85,
                          category="Batsman", country="India", bat_hand="Right",
                          bowl_hand="Right", bowl_style="Medium Pacer"))
        db.commit()
    client = admin.app.test_client()
    with client.session_transaction() as session:
        session["admin"] = True
        session["admin_user"] = "chemistry-test"
    yield SimpleNamespace(client=client, sessions=sessions, config_service=config_service)
    engine.dispose()


def save(site, **fields):
    response = site.client.get("/settings/chemistry")
    assert response.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))[1]
    return site.client.post("/settings/chemistry", data={"csrf_token": token, **fields})


def test_website_saves_only_selected_editions_and_bot_reads_them(chemistry_site):
    from models import AdminLog, GameConfig

    site = chemistry_site
    # Warm this worker's cache first: a save must invalidate it.
    assert site.config_service.get_chemistry_config()["chemistry_variety_target"] == 4
    response = save(site, chemistry_variety_target="2", chemistry_version_mode="selected",
                    chemistry_versions=["ipl2026 card", "T20WC"])
    assert response.status_code == 302
    with site.sessions() as db:
        row = db.query(GameConfig).one()
        assert row.chemistry_variety_target == 2
        assert json.loads(row.chemistry_variety_versions_json) == ["IPL2026", "T20WC"]
        assert row.updated_by == "chemistry-test"
        assert db.query(AdminLog).filter_by(action="chemistry_settings_save").count() == 1
    assert chemistry.card_variety(cards("IPL2026", "T20WC", "BBL2025")) == (10, 2)
    html = site.client.get("/settings/chemistry").get_data(as_text=True)
    assert 'value="IPL2026"' in html and "(2 cards)" in html
    assert 'value="Base"' not in html
    from handlers import chemhelp
    guide = chemhelp.SECTIONS["bonus"]["body"]()
    assert "2 different eligible editions" in guide and "T20WC" in guide
    assert "BBL2025" not in guide


def test_none_and_all_are_different_saved_choices(chemistry_site):
    site = chemistry_site
    assert save(site, chemistry_variety_target="1", chemistry_version_mode="selected").status_code == 302
    assert chemistry.card_variety(cards("IPL2026")) == (0, 0)
    from handlers import chemhelp
    assert "currently disabled" in chemhelp.SECTIONS["bonus"]["body"]()
    assert save(site, chemistry_variety_target="1", chemistry_version_mode="all").status_code == 302
    assert chemistry.card_variety(cards("Future edition")) == (10, 1)
    with site.sessions() as db:
        from models import GameConfig
        assert db.query(GameConfig).one().chemistry_variety_versions_json is None


@pytest.mark.parametrize("values", [
    {"chemistry_variety_target": "0"}, {"chemistry_variety_target": "12"},
    {"chemistry_variety_target": "3.5"}, {"chemistry_variety_target": "bad"},
    {"chemistry_version_mode": "unknown"},
    {"chemistry_version_mode": "selected", "chemistry_versions": ["Not in catalogue"]},
    {"chemistry_version_mode": "selected", "chemistry_versions": ["Base"]},
])
def test_invalid_website_settings_do_not_partially_save(chemistry_site, values):
    from models import GameConfig

    response = save(chemistry_site, **dict(
        {"chemistry_variety_target": "3", "chemistry_version_mode": "all"}, **values))
    assert response.status_code == 400
    with chemistry_site.sessions() as db:
        assert db.query(GameConfig).count() == 0


def test_another_bot_worker_refreshes_website_rules_within_five_seconds(chemistry_site, monkeypatch):
    from models import GameConfig

    site = chemistry_site
    assert site.config_service.get_chemistry_config()["chemistry_variety_target"] == 4
    with site.sessions() as db:
        db.add(GameConfig(chemistry_variety_target=2,
                          chemistry_variety_versions_json='["IPL2026", "T20WC"]'))
        db.commit()
    # Another process cannot invalidate this worker's memory; its TTL must expire.
    loaded = site.config_service._CHEMISTRY_CACHE["loaded_at"]
    monkeypatch.setattr(site.config_service, "monotonic", lambda: loaded + 5.1)
    assert chemistry.card_variety(cards("IPL2026", "T20WC", "BBL2025")) == (10, 2)


def test_settings_require_admin_login_and_csrf(chemistry_site):
    site = chemistry_site
    assert site.client.post("/settings/chemistry", data={
        "chemistry_variety_target": "1", "chemistry_version_mode": "all"
    }).status_code == 302
    # The normal CSRF handler clears the session on a missing token.
    response = site.client.get("/settings/chemistry")
    assert response.status_code == 302 and response.location.endswith("/login")


def test_startup_migrates_existing_settings_without_changing_other_rules(chemistry_site, monkeypatch):
    import database
    from models import GameConfig
    from sqlalchemy import text

    site = chemistry_site
    engine = site.sessions.kw["bind"]
    with site.sessions() as db:
        db.add(GameConfig(match_style="webapp"))
        db.commit()
    # Reproduce the previously deployed schema with an existing config row.
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE game_config DROP COLUMN chemistry_variety_target"))
        connection.execute(text("ALTER TABLE game_config DROP COLUMN chemistry_variety_versions_json"))
    monkeypatch.setattr(database, "engine", engine)
    database._migrate_add_columns()
    database._migrate_add_columns()  # Startup migrations must be repeatable.
    with site.sessions() as db:
        row = db.query(GameConfig).one()
        assert row.chemistry_variety_target == 4
        assert row.chemistry_variety_versions_json is None
        assert row.match_style == "webapp"
