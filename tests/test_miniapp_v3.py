"""Compatibility and reconciliation checks for the Elite Mini App surfaces."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def local_app(monkeypatch, tmp_path):
    import admin
    import database
    from models import User

    engine = create_engine(f"sqlite:///{tmp_path / 'miniapp.sqlite'}")
    database.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    monkeypatch.setenv("BOT_TOKEN", "miniapp-v3-test-token")
    monkeypatch.setenv("WEBAPP_DEV_MODE", "1")
    monkeypatch.setitem(admin.app.config, "TESTING", True)
    with sessions() as db:
        db.add_all([User(id=71, telegram_id=71701), User(id=72, telegram_id=71702)])
        db.commit()
    yield admin.app.test_client(), sessions
    engine.dispose()


def post(client, path, body=None, account=71701):
    return client.post("/api/webapp/" + path, json=body or {},
                       headers={"Authorization": f"DEV_{account}"})


def test_shell_uses_legacy_and_new_route_targets_once(local_app):
    client, _ = local_app
    html = client.get("/webapp").get_data(as_text=True)
    assert html.count('id="bottom-nav"') == 1
    assert all(html.count(f'id="screen-{route}"') == 1 for route in (
        "home", "roster", "matchcentre", "compete", "rewards", "wallet", "match",
        "xi", "market", "tournament", "freepack", "explore", "appearance",
    ))
    assert 'name="appearance"' in html
    assert "Community Arena and XP" in html
    assert '<script src="/static/js/auction_miniapp.js?' not in html
    assert client.post("/api/webapp/quickmatch/quota", json={}).status_code == 401


def test_roster_player_detail_serializes_acquisition_for_its_owner(local_app):
    from models import Player, UserRoster

    client, sessions = local_app
    with sessions() as db:
        player = Player(name="Test All-rounder", rating=91, category="All-rounder",
                        country="India", bat_hand="Right", bowl_hand="Right",
                        bowl_style="Fast", bat_rating=91, bowl_rating=85)
        db.add(player)
        db.flush()
        entry = UserRoster(user_id=71, player_id=player.id,
                           acquired_date=datetime(2025, 8, 14, 9, 30))
        db.add(entry)
        db.flush()
        roster_id = entry.id
        db.commit()

    response = post(client, "player-detail", {"roster_id": roster_id})
    assert response.status_code == 200
    player = response.json["player"]
    assert player["acquired_at"] == "2025-08-14T09:30:00+00:00"
    assert (player["rating"], player["bat_rating"], player["bowl_rating"]) == (91, 91, 85)
    assert post(client, "player-detail", {"roster_id": roster_id}, account=71702).status_code == 404


def test_market_listings_include_batting_and_bowling_ratings(local_app, monkeypatch):
    from models import GlobalPlayerMarket, Player
    from services import global_market

    client, sessions = local_app
    monkeypatch.setattr(global_market, "ensure_player_market_fresh", lambda db: None)
    with sessions() as db:
        player = Player(name="Test Bowler", rating=82, category="Bowler",
                        country="India", bat_hand="Right", bowl_hand="Right",
                        bowl_style="Fast", bat_rating=0, bowl_rating=82)
        db.add(player)
        db.flush()
        db.add(GlobalPlayerMarket(slot_index=1, player_id=player.id,
                                  base_price=100_000, final_price=100_000))
        db.commit()

    response = post(client, "market")
    assert response.status_code == 200
    listing, = response.json["slots"]
    assert (listing["rating"], listing["bat_rating"], listing["bowl_rating"]) == (82, 0, 82)


def test_quick_match_start_and_replayed_phase_use_current_snapshot(local_app, monkeypatch):
    from services import quick_match_service as quick

    client, _ = local_app
    xi = [
        {"id": i, "name": f"Player {i}", "rating": 80, "bat_rating": 80, "bowl_rating": 80,
         "category": category, "country": "India", "bat_hand": "Right", "bowl_hand": "Right",
         "bowl_style": "Fast"}
        for i, category in enumerate(["Batsman"] * 4 + ["Wicket Keeper"] +
                                     ["All-rounder"] * 2 + ["Bowler"] * 4, 1)
    ]
    monkeypatch.setattr(quick, "get_user_xi", lambda *_: xi)
    monkeypatch.setattr(quick, "_generate_bot_xi", lambda *_: xi)
    quick.drop_phase_match(71701)
    try:
        body = {"toss_choice": "bat", "difficulty": "medium", "resume_existing": True}
        started = post(client, "quickmatch/start", body).get_json()["state"]
        repeated = post(client, "quickmatch/start", body).get_json()["state"]
        assert repeated["match_id"] == started["match_id"]
        assert post(client, "quickmatch/quota", account=71702).get_json()["match"] is None
        phase = {"choice": "balanced", "expected": [started["match_id"], 1, 0]}
        updated = post(client, "quickmatch/phase", phase).get_json()["state"]
        duplicate = post(client, "quickmatch/phase", phase)
        assert duplicate.status_code == 409
        assert duplicate.get_json()["error"] == "stale_phase"
        assert post(client, "quickmatch/quota").get_json()["match"] == updated
    finally:
        quick.drop_phase_match(71701)


def test_catalog_pages_versions_and_sort(local_app):
    from models import Player

    client, sessions = local_app
    with sessions() as db:
        for i, (version, bat_rating) in enumerate([("Gold", 93), ("Gold", 97), ("Base", 99)]):
            db.add(Player(name=f"Card {i}", version=version, rating=93,
                          category="Batsman", country="India", bat_hand="Right",
                          bowl_hand="Right", bowl_style="Fast", bat_rating=bat_rating))
        db.commit()
    filters = {"version": "Gold", "sort": "batting", "per_page": 1}
    first = post(client, "players", filters).get_json()
    second = post(client, "players", {**filters, "page": 2}).get_json()
    assert first["total"] == 2 and first["has_more"] is True
    assert first["results"][0]["bat_rating"] == 97
    assert second["results"][0]["bat_rating"] == 93 and not second["has_more"]
    assert post(client, "countries").get_json()["versions"] == ["Base", "Gold"]
    assert post(client, "players", {"q": "Gold", "per_page": "all"}).get_json()["total"] == 2


def test_timed_fixture_only_for_owner_in_open_window(local_app):
    from models import Tournament, TournamentMatch, TournamentTeam
    from services.tournament_webapp import clubhouse_fixture

    _, sessions = local_app
    with sessions() as db:
        now = datetime.utcnow()
        tour = Tournament(name="Elite Fixture Test", status="active", is_active=True,
                          round_tracked=1, round_deadline_at=now + timedelta(hours=2),
                          fixture_release_mode="rolling")
        db.add(tour)
        db.flush()
        home = TournamentTeam(name="Harbour", tournament_id=tour.id, user_tg_id=71701)
        away = TournamentTeam(name="City", tournament_id=tour.id, user_tg_id=71702)
        db.add_all([home, away])
        db.flush()
        db.add(TournamentMatch(tournament_id=tour.id, team1_id=home.id, team2_id=away.id,
                               status="scheduled", round_no=1, match_no=1, stage="league"))
        db.flush()
        fixture = clubhouse_fixture(db, 71701)
        assert fixture["teams"] == ["Harbour", "City"]
        assert fixture["deadline"].endswith("+00:00")
        assert clubhouse_fixture(db, 71703) is None
        tour.round_tracked = 2
        assert clubhouse_fixture(db, 71701) is None
        tour.round_tracked = 1
        tour.round_deadline_at = now - timedelta(minutes=1)
        assert clubhouse_fixture(db, 71701) is None


def test_pack_openings_still_require_a_confirmed_economy_quote(local_app):
    client, _ = local_app
    response = post(client, "packs/open", {"inventory_id": 3})
    assert response.status_code == 409
    assert response.get_json()["error"] == "quote_required"


def test_arena_uses_persisted_outcome_and_normalizes_recorded_wickets(local_app):
    from models import Match
    from services.crickidex_arena import _build_result, _fall_of_wickets

    _, sessions = local_app
    with sessions() as db:
        match = Match(user1_id=71, user2_id=72, status="completed", winner_id=72,
                      margin_type="super_over")
        host = {"telegramId": 71701, "teamName": "Harbour"}
        guest = {"telegramId": 71702, "teamName": "City"}
        state = {"bat_team_id": 72, "inn1_bat_team_id": 71, "innings": 2,
                 "total_runs": 140, "inn1_runs": 140, "target": 141,
                 "_completed_rewards": {"winner_coins": 0, "loser_coins": 0}}
        outcome = _build_result(db, state, match, host, guest)
        assert outcome["winner"]["teamName"] == "City"
        assert outcome["outcome"] == "super_over"
        assert _fall_of_wickets([(24, "3.2"), [41, 2, "Batter", "6.4"]]) == [
            {"runs": 24, "wicket": 1, "batter": None, "progress": "3.2"},
            {"runs": 41, "wicket": 2, "batter": "Batter", "progress": "6.4"},
        ]
