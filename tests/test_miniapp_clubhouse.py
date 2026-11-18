"""Match history, wallet and reward claims are scoped to the signed player."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def clubhouse(tmp_path, monkeypatch):
    import admin
    import models as m
    from database import Base
    from services import config_service

    engine = create_engine(f"sqlite:///{tmp_path / 'clubhouse.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(admin, "get_session", sessions)
    monkeypatch.setattr(config_service, "get_config", lambda *a, **kw: {})
    monkeypatch.setenv("BOT_TOKEN", "test")
    monkeypatch.setenv("WEBAPP_DEV_MODE", "1")
    monkeypatch.setitem(admin.app.config, "TESTING", True)

    now = datetime.utcnow()
    with sessions.begin() as db:
        db.add_all([m.User(id=1, telegram_id=11, first_name="Alice", total_coins=50),
                    m.User(id=2, telegram_id=22, first_name="Bob"),
                    m.User(id=3, telegram_id=33, first_name="Carol")])
        db.add(m.GameConfig(official_group_id=-100123))
        db.add_all([
            m.MiniappGift(id=1, user_id=1, request_key=str(uuid4()), title="Season opener", currency="coins", amount=100),
            m.MiniappGift(id=2, user_id=2, request_key=str(uuid4()), title="Private", currency="gems", amount=90),
            m.Giveaway(id=1, title="Opening day", prize_type="gems", prize_amount=100, status="running",
                       start_time=now - timedelta(hours=1), end_time=now + timedelta(hours=1)),
            m.Match(id=1, user1_id=1, user2_id=2, status="completed", winner_id=1,
                    margin_type="runs", margin_value=11, inn1_runs=150, inn2_runs=139),
            m.Match(id=2, user1_id=2, user2_id=3, status="completed"),
            m.ActivityLog(user_id=2, action="daily", coins_change=9000),
            m.ActivityLog(user_id=1, action="buy", coins_change=-100, coins_balance_after=50),
        ])
    yield admin, sessions
    engine.dispose()


def post(admin, path, body=None, user=11):
    return admin.app.test_client().post(
        "/api/webapp/clubhouse/" + path, json=body or {},
        headers={"Authorization": f"tma DEV_{user}"})


def test_match_wallet_and_inbox_are_private(clubhouse):
    admin, _ = clubhouse
    assert admin.app.test_client().post("/api/webapp/clubhouse/wallet", json={}).status_code == 401
    matches = post(admin, "matches").get_json()
    assert [(item["id"], item["result"]) for item in matches["matches"]] == [(1, "Won")]
    wallet = post(admin, "wallet").get_json()
    assert wallet["balance"]["coins"] == 50
    assert [row["coins"] for row in wallet["transactions"]] == [-100]
    assert post(admin, "wallet", {"currency": "gems"}).get_json()["transactions"] == []
    assert post(admin, "matches", {"before": -1}).status_code == 400
    rewards = post(admin, "rewards").get_json()
    assert [g["id"] for g in rewards["gifts"]] == [1]


def test_concurrent_gift_claims_credit_once(clubhouse):
    from models import ActivityLog, User
    admin, sessions = clubhouse
    assert post(admin, "gifts/2/claim").status_code == 404
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: post(admin, "gifts/1/claim", {"amount": 9999}).get_json(), range(2)))
    assert all(receipt["ok"] for receipt in receipts)
    assert sum(bool(receipt.get("already_claimed")) for receipt in receipts) == 1
    with sessions() as db:
        assert db.get(User, 1).total_coins == 150
        assert db.query(ActivityLog).filter_by(user_id=1, action="miniapp_gift").count() == 1


def test_giveaway_rechecks_membership_activity_and_cutoff(clubhouse, monkeypatch):
    from models import Giveaway, GiveawayEntry, User
    admin, sessions = clubhouse
    monkeypatch.setattr(admin, "_user_in_chat", lambda *a, **kw: False)
    assert post(admin, "giveaways/1/join").status_code == 403
    monkeypatch.setattr(admin, "_user_in_chat", lambda *a, **kw: True)
    with sessions.begin() as db:
        db.get(Giveaway, 1).require_min_activity = True
    assert post(admin, "giveaways/1/join").status_code == 403
    with sessions.begin() as db:
        db.get(User, 1).matches_played = 1
    assert post(admin, "giveaways/1/join").get_json()["joined"]
    assert post(admin, "giveaways/1/join").get_json()["joined"]
    with sessions.begin() as db:
        assert db.query(GiveawayEntry).count() == 1
        db.get(Giveaway, 1).end_time = datetime.utcnow()
    assert post(admin, "giveaways/1/join", user=22).status_code == 409


def test_admin_can_send_one_pack_per_request_and_player_claims_it(clubhouse, monkeypatch):
    from models import MiniappGift, Pack, UnopenedPack
    admin, sessions = clubhouse
    with sessions.begin() as db:
        db.add(Pack(id=1, slot_number=1, name="Season Pack"))
    key = str(uuid4())
    body = {"telegram_id": 11, "title": "Launch", "currency": "pack", "pack_id": 1, "request_key": key}
    monkeypatch.setitem(admin.app.config, "WTF_CSRF_ENABLED", False)
    client = admin.app.test_client()
    assert client.post("/giveaways/gifts", data=body).location.endswith("/login")
    with client.session_transaction() as session:
        session["admin"] = True
    for _ in range(2):
        response = client.post("/giveaways/gifts", data=body)
        assert response.status_code == 302 and response.location.endswith("/giveaways")
    with sessions() as db:
        gift = db.query(MiniappGift).filter_by(request_key=key).one()
        gift_id = gift.id
        assert gift.pack_id == 1 and gift.claimed_at is None
    assert post(admin, f"gifts/{gift_id}/claim").get_json()["gift"]["item"] == "Season Pack"
    assert post(admin, f"gifts/{gift_id}/claim").get_json()["already_claimed"]
    with sessions() as db:
        item = db.query(UnopenedPack).one()
        assert item.user_id == 1 and item.pack_id == 1


def test_profile_uses_authenticated_identity_and_photo(clubhouse, monkeypatch):
    from models import User
    from services import miniapp_avatar

    admin, sessions = clubhouse
    with sessions.begin() as db:
        user = db.get(User, 1)
        user.matches_played = 12
        user.matches_won = 7
    monkeypatch.setattr(miniapp_avatar, "profile_photo", lambda uid: b"photo" if uid == 11 else None)
    client = admin.app.test_client()
    assert client.post("/api/webapp/profile/photo").status_code == 401
    headers = {"Authorization": "tma DEV_11"}
    profile = client.post("/api/webapp/profile", headers=headers, json={"user_id": 2}).get_json()
    assert profile["name"] == "Alice" and profile["win_rate"] == 58.3
    photo = client.post("/api/webapp/profile/photo", headers=headers, json={"user_id": 2})
    assert (photo.status_code, photo.mimetype, photo.data) == (200, "image/jpeg", b"photo")
    assert photo.headers["Cache-Control"] == "private, no-store"
    assert client.post("/api/webapp/profile/photo", headers={"Authorization": "tma DEV_22"}).status_code == 204


def test_tournament_archive_shows_completed_and_keeps_drafts_private(clubhouse):
    from models import Tournament, TournamentTeam

    admin, sessions = clubhouse
    with sessions.begin() as db:
        db.add_all([
            Tournament(id=1, name="Finals", status="completed", is_active=False),
            Tournament(id=2, name="Private", status="draft", is_active=False),
            TournamentTeam(tournament_id=1, name="Alice XI", user_tg_id=11),
        ])
    client = admin.app.test_client()
    headers = {"Authorization": "tma DEV_11"}
    directory = client.post("/api/webapp/tournament/directory", headers=headers).get_json()
    assert [card["name"] for card in directory["archive"]] == ["Finals"]
    assert directory["archive"][0]["my_team"]
    opened = client.post("/api/webapp/tournament", headers=headers,
                         json={"archive": True, "tournament_id": 1}).get_json()
    assert opened["archive"] and opened["teams"][0]["name"] == "Alice XI"
    draft = client.post("/api/webapp/tournament", headers=headers,
                        json={"archive": True, "tournament_id": 2}).get_json()
    assert not draft["live"] and "tournament" not in draft


def test_captain_change_requires_a_squad_starter_and_unlocked_team(clubhouse, monkeypatch):
    from models import Player, User, UserRoster

    admin, sessions = clubhouse
    with sessions.begin() as db:
        db.add(Player(id=1, name="Opener", category="Batsman", rating=85,
                      country="India", bat_hand="Right", bowl_hand="Right", bowl_style="Pace"))
        db.add_all([UserRoster(id=1, user_id=1, player_id=1, order_position=1),
                    UserRoster(id=2, user_id=2, player_id=1, order_position=1)])
    client = admin.app.test_client()
    headers = {"Authorization": "tma DEV_11"}
    path = "/api/webapp/roster/captain"
    monkeypatch.setattr(admin, "_team_locked_response", lambda *args: ({"ok": False}, 409))
    assert client.post(path, headers=headers, json={"roster_id": 1}).status_code == 409
    monkeypatch.setattr(admin, "_team_locked_response", lambda *args: None)
    assert client.post(path, headers=headers, json={"roster_id": 2}).status_code == 404
    assert client.post(path, headers=headers, json={"roster_id": 1}).get_json()["captain_roster_id"] == 1
    with sessions() as db:
        assert db.get(User, 1).captain_roster_id == 1


def test_telegram_photo_proxy_fetches_restricted_file_and_reencodes(monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace

    import requests
    from PIL import Image
    from services import miniapp_avatar

    monkeypatch.setenv("BOT_TOKEN", "local-test-token")
    body = BytesIO()
    Image.new("RGB", (256, 256), "green").save(body, "PNG")

    class Download:
        status_code = 200

        def __enter__(self):
            self.raw = SimpleNamespace(read=lambda limit, **kw: body.getvalue()[:limit])
            return self

        def __exit__(self, *_args):
            return False

    def telegram(url, *, json, **options):
        assert options["allow_redirects"] is False
        if url.endswith("/getUserProfilePhotos"):
            assert json["user_id"] == 123
            result = {"photos": [[{"file_id": "profile-fixture", "width": 160}]]}
        else:
            assert json["file_id"] == "profile-fixture"
            result = {"file_path": "photos/file_12.png"}
        return SimpleNamespace(status_code=200, json=lambda: {"result": result})

    def download(url, **options):
        assert url == "https://api.telegram.org/file/botlocal-test-token/photos/file_12.png"
        assert options["allow_redirects"] is False and options["stream"]
        return Download()

    monkeypatch.setattr(requests, "post", telegram)
    monkeypatch.setattr(requests, "get", download)
    miniapp_avatar._cached_photo.cache_clear()
    try:
        with Image.open(BytesIO(miniapp_avatar.profile_photo(123))) as photo:
            assert photo.format == "JPEG" and photo.size == (192, 192)
    finally:
        miniapp_avatar._cached_photo.cache_clear()


def test_tournament_centre_features_only_the_latest_live_competition(clubhouse):
    from models import Tournament

    admin, sessions = clubhouse
    now = datetime.utcnow()
    with sessions.begin() as db:
        db.add_all([
            Tournament(id=1, name="Earlier Cup", kind="challenge", status="active",
                       is_active=True, activated_at=now - timedelta(days=1)),
            Tournament(id=2, name="Featured Cup", kind="letsplay", status="active",
                       is_active=True, activated_at=now),
            Tournament(id=3, name="Paused Cup", kind="challenge", status="paused",
                       is_active=True, activated_at=now + timedelta(seconds=1)),
        ])

    client = admin.app.test_client()
    headers = {"Authorization": "tma DEV_11"}
    directory = client.post("/api/webapp/tournament/directory", headers=headers).get_json()
    assert [item["id"] for item in directory["current"]] == [2]
    assert directory["career"] == {
        "played": 0, "titles": 0, "match_wins": 0, "match_losses": 0, "latest_title": None,
    }
    detail = "/api/webapp/tournament"
    featured = client.post(detail, headers=headers).get_json()
    assert featured["tournament"]["id"] == 2
    assert [item["id"] for item in featured["live_tournaments"]] == [2]
    assert client.post(detail, headers=headers, json={"tournament_id": 1}).get_json()["live"] is False
    assert client.post(detail + "/stats", headers=headers, json={"tournament_id": 1}).status_code == 404


def test_tournament_career_attributes_results_and_titles_once(clubhouse):
    from models import Tournament, TournamentMatch, TournamentTeam

    admin, sessions = clubhouse
    with sessions.begin() as db:
        db.add_all([
            Tournament(id=1, name="Crown Cup", status="completed"),
            Tournament(id=2, name="Challenge Series", status="active", is_active=True),
            Tournament(id=3, name="Draft Cup", status="draft"),
            TournamentTeam(id=1, tournament_id=1, name="Owned", owner_tg_id=11),
            TournamentTeam(id=2, tournament_id=1, name="Co-owned", co_owner_ids_json="[11]"),
            TournamentTeam(id=3, tournament_id=1, name="Rivals", owner_tg_id=22),
            TournamentTeam(id=4, tournament_id=2, name="Unowned XI"),
            TournamentTeam(id=5, tournament_id=2, name="Other XI", co_owner_ids_json="[111]"),
            TournamentTeam(id=6, tournament_id=3, name="Draft entry", user_tg_id=11),
        ])
        db.add_all([
            TournamentMatch(tournament_id=1, team1_id=1, team2_id=3, winner_team_id=1,
                            stage="final", match_no=2),
            # A rebuild produced a newer, decisive final. The title is counted once.
            TournamentMatch(tournament_id=1, team1_id=2, team2_id=3, winner_team_id=2,
                            stage="final", match_no=3, host_user_id=1, target_user_id=2),
            # A named captain gets the match result even when someone else owns the side.
            TournamentMatch(tournament_id=1, team1_id=1, team2_id=3, winner_team_id=1,
                            host_user_id=2),
            TournamentMatch(tournament_id=2, team1_id=4, team2_id=5, winner_team_id=5,
                            host_user_id=1),
            TournamentMatch(tournament_id=2, team1_id=4, team2_id=5, result_type="no_result",
                            host_user_id=1),
            TournamentMatch(tournament_id=2, team1_id=4, team2_id=5, winner_team_id=5,
                            host_user_id=1, is_simulated=True),
            TournamentMatch(tournament_id=2, team1_id=4, team2_id=5, status="scheduled",
                            host_user_id=1),
        ])

    path = "/api/webapp/tournament/directory"
    client = admin.app.test_client()
    alice = client.post(path, headers={"Authorization": "tma DEV_11"}).get_json()["career"]
    assert alice == {"played": 2, "titles": 1, "match_wins": 2, "match_losses": 1,
                     "latest_title": {"id": 1, "name": "Crown Cup"}}
    bob = client.post(path, headers={"Authorization": "tma DEV_22"}).get_json()["career"]
    assert bob["played"] == 1 and bob["titles"] == 0
