"""Live Event competition smoke and policy regressions."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from models import User
from models_events import (StudioCompetition, StudioCompetitionReward, StudioContribution, StudioFixture,
                           StudioFixtureResult, StudioParticipant, StudioStarClaim)
from services import cipl_picks as picks, event_studio as studio
from services import event_studio_competition as competitions, event_studio_objectives as goals
from tests.test_event_studio import NOW, _publish, _seed, sessions  # noqa: F401


def _new(db, *, teams=2, format="T20", **extra):
    config = _seed(db, teams)
    config.update(objective_scope="tournament", objective_preset="custom",
                  tournament_objectives=[{"id": "matches", "metric": "played_matches", "target": 1, "stars": 4}],
                  objectives=[], mode=["tournament_team"], format=format, **extra)
    config["competition"]["match_rewards"] = False
    config["milestones"] = [{"stars": 4, "free": {"coins": 20}, "diamond": {"coins": 40}}]
    return _publish(db, studio.generate(studio.normalize(config), "all"))


def _users(db):
    return db.scalars(select(User).order_by(User.id)).all()


@pytest.mark.parametrize("format", ["T20", "ODI"])
def test_canonical_lobby_start_and_pvp_result(sessions, monkeypatch, format):
    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER_ODI", "1")
    monkeypatch.setattr(competitions, "randbelow", lambda _: 0)
    with sessions() as db:
        event = _new(db, format=format)
        first, second = _users(db)
        room = competitions.create(db, first, event.id, "multi", "team-0", NOW, chat_id=-42)
        assert room.join_until == NOW + timedelta(hours=1)
        with pytest.raises(ValueError, match="invitation"):
            competitions.lobby(db, second, room.id, now=NOW)
        with pytest.raises(studio.Conflict, match="claimed"):
            competitions.change_lobby(db, second, room.id, "claim", NOW, team="team-0", chat_id=-42)
        competitions.change_lobby(db, second, room.id, "claim", NOW, team="team-1", invite=room.invite)
        assert room.status == "running"
        with pytest.raises(studio.Conflict, match="already"):
            competitions.create(db, second, event.id, "solo", "team-1", NOW)
        fixture = db.scalar(select(StudioFixture))
        assert fixture is not None
        now = max(NOW, fixture.opens_at)
        def setup(user, name, value=None):
            return competitions.setup(db, user, fixture.id, now, action=name, version=fixture.version, value=value)
        setup(first, "ready")
        with pytest.raises(ValueError, match="mark ready"):
            setup(first, "coin", "heads")
        setup(second, "ready")
        setup(first, "coin", "heads")
        setup(first, "elect", "bat")
        for user in (first, second):
            panel = competitions.setup(db, user, fixture.id, now)
            setup(user, "xi", panel["default_xi"])
        setup(first, "start")
        setup(second, "start")
        assert db.scalar(select(func.count(StudioFixture.id))) == 1
        first_view, second_view = (competitions.arena(db, u, fixture.id, now) for u in (first, second))
        assert first_view["id"] == second_view["id"] == f"competition-{fixture.id}"
        assert not first_view["isMyTurn"] and second_view["isMyTurn"]
        bowler = second_view["approach"]["overBowlers"][0]["roster_id"]
        with pytest.raises(ValueError, match="other captain"):
            competitions.action(db, first, fixture.id, fixture.version, picks.PICK_BOWLER, bowler, now, source="app")
        for _ in range(340):
            if fixture.status != "in_progress":
                break
            state = fixture.state
            kind = next((name for name, step in picks.PICK_ACTION.items() if step == state["event_next_action"]), "continue")
            user = first if competitions._actor(state) in ("team-0", None) else second
            val = competitions.cricket.match.eligible_bowlers(state)[0]["roster_id"] if kind == picks.PICK_BOWLER else 0
            competitions.action(db, user, fixture.id, fixture.version, kind, val, now, source="app")
        assert db.get(StudioFixtureResult, fixture.id).kind == "completed"
        assert db.scalar(select(func.count()).select_from(StudioContribution)) == 2
        assert db.scalar(select(func.count()).select_from(StudioStarClaim)) == 2
        assert competitions.dashboard(db, first, event.id, now)["standings"] == competitions.dashboard(db, second, event.id, now)["standings"]
        assert competitions.claim(db, first, event.id, now)["claimed_stars"] == 4
        assert competitions.claim(db, first, event.id, now)["claimed_stars"] == 4
        assert db.scalar(select(func.count()).select_from(StudioCompetitionReward)) == 1
        competitions.claim(db, first, event.id, now, wallet=True)
        competitions.claim(db, first, event.id, now, wallet=True)
        assert first.total_coins == 20


def test_single_human_lobby_expiry_and_budget(sessions, monkeypatch):
    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    with sessions() as db:
        event = _new(db, teams=3, max_users=1)
        host, other = _users(db)
        room = competitions.create(db, host, event.id, "multi", "team-0", NOW)
        invite, room_id, event_id = room.invite, room.id, event.id
        db.commit()
        with pytest.raises(ValueError, match="capacity"):
            competitions.change_lobby(db, other, room_id, "claim", NOW, team="team-1", invite=invite)
        db.rollback()
    with sessions() as db:
        competitions.expire_lobby(db, room_id, NOW + timedelta(minutes=59))
        assert db.get(StudioCompetition, room_id).status == "lobby"
        competitions.expire_lobby(db, room_id, NOW + timedelta(hours=1))
        assert db.get(StudioCompetition, room_id).status == "running"
        assert db.scalar(select(func.count()).select_from(StudioParticipant).where(StudioParticipant.user_id.is_(None))) == 2
        assert competitions.dashboard(db, _users(db)[0], event_id, NOW)["fixtures"]


def test_multi_is_gated_and_solo_preserves_one_entry(sessions, monkeypatch):
    monkeypatch.delenv("EVENT_STUDIO_MULTIPLAYER", raising=False)
    with sessions() as db:
        event = _new(db)
        host, _ = _users(db)
        with pytest.raises(ValueError, match="unavailable"):
            competitions.create(db, host, event.id, "multi", "team-0", NOW)
        first = competitions.create(db, host, event.id, "solo", "team-0", NOW)
        assert competitions.create(db, host, event.id, "solo", "team-1", NOW).id == first.id
        assert competitions.dashboard(db, host, event.id, NOW)["team"] == "team-0"


def test_shared_seat_race_release_and_early_start(sessions, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    with sessions() as db:
        event = _new(db, teams=3)
        db.add(User(telegram_id=2222))
        db.flush()
        host, *guests = _users(db)
        room = competitions.create(db, host, event.id, "multi", "team-0", NOW, chat_id=-101)
        room_id, event_id, guest_ids = room.id, event.id, [u.id for u in guests]
        db.commit()
    barrier = Barrier(2)

    def take(user_id):
        with sessions() as db:
            user = db.get(User, user_id)
            barrier.wait(timeout=10)
            try:
                competitions.change_lobby(db, user, room_id, "claim", NOW, team="team-1", chat_id=-101)
                db.commit()
                return user_id
            except studio.Conflict:
                db.rollback()

    with ThreadPoolExecutor(2) as pool:
        owners = [owner for owner in pool.map(take, guest_ids) if owner]
    assert len(owners) == 1
    with sessions() as db:
        host = _users(db)[0]
        guest = db.get(User, owners[0])
        room = db.get(StudioCompetition, room_id)
        with pytest.raises(ValueError, match="Only the lobby host"):
            competitions.change_lobby(db, guest, room_id, "start", NOW, version=room.version, chat_id=-101)
        competitions.change_lobby(db, guest, room_id, "release", NOW, chat_id=-101)
        assert competitions.create(db, guest, event_id, "solo", "team-1", NOW).kind == "solo"
        competitions.change_lobby(db, host, room_id, "start", NOW, version=room.version)
        assert room.status == "running"
        assert len(competitions._entries(db, room)) == 3


def test_no_show_and_ai_results_dont_grant_performance(sessions, monkeypatch):
    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    with sessions() as db:
        live = _new(db, teams=4)
        host, guest = _users(db)
        config = studio.revision(db, live, published=True).config
        first = config["fixtures"][0]
        room = competitions.create(db, host, live.id, "multi", first["home"], NOW)
        competitions.change_lobby(db, guest, room.id, "claim", NOW, team=first["away"], invite=room.invite)
        competitions.change_lobby(db, host, room.id, "start", NOW, version=room.version)
        fixtures = db.scalars(select(StudioFixture).filter_by(competition_id=room.id)).all()
        fixture = next(row for row in fixtures if row.fixture_key == first["key"])
        pure_ai = next(row for row in fixtures if row.home not in (fixture.home, fixture.away)
                       and row.away not in (fixture.home, fixture.away) and row.opens_at == fixture.opens_at)
        competitions.setup(db, host, fixture.id, max(NOW, fixture.opens_at), action="ready")
        competitions.run_fixture(db, fixture.id, fixture.deadline + timedelta(seconds=1))
        result = db.get(StudioFixtureResult, fixture.id)
        assert result.kind == "forfeited" and result.winner == first["home"]
        for _ in range(8):
            competitions.run_fixture(db, pure_ai.id, max(NOW, pure_ai.opens_at), budget=8)
        assert db.get(StudioFixtureResult, pure_ai.id).kind == "completed"
        assert db.scalar(select(func.count()).select_from(StudioContribution)) == 0
        assert db.scalar(select(func.count()).select_from(StudioStarClaim)) == 0
        board = competitions.dashboard(db, host, live.id, fixture.deadline)["my_standing"]
        assert board["won"] == 1 and board["faced"] == board["bowled"] == 0


def test_authoritative_objective_milestones_and_rate_samples():
    from types import SimpleNamespace

    bats = [50] * 5 + [99] * 5 + [100, 200]
    performance = [{"player_id": idx, "batting": {"runs": runs, "balls": idx + 20,
                                                  "fours": 8, "sixes": 1},
                    "bowling": {"wickets": 5, "balls": 18, "runs": 12} if idx < 10 else {}}
                   for idx, runs in enumerate(bats)]
    state = {"bat_team_id": "a", "inn1_wickets": 10,
             "bat_xi": [{"player_id": 0, "category": "Wicketkeeper", "roster_id": 0}],
             "record_deliveries": [
        {"innings": 1, "phase": "death", "bid": 9, "bowler_wicket": True, "wicket": True, "legal": True},
        {"innings": 1, "phase": "death", "bid": 1, "bowler_wicket": False, "wicket": True, "legal": True},
        {"innings": 1, "phase": "death", "bid": 9, "bowler_wicket": True, "wicket": True, "legal": True},
        {"innings": 1, "phase": "death", "bid": 9, "bowler_wicket": True, "wicket": True, "legal": True},
        {"innings": 2, "phase": "powerplay", "bat_runs": 6, "legal": True, "batter_id": 0},
    ]}
    metrics = goals.fixture_metrics(state, "a", "b", performance)
    assert metrics["fifties"] == 10 and metrics["centuries"] == 2
    assert metrics["boundaries"] == 108 and metrics["wickets"] == 50
    assert metrics["death_wickets"] == 3 and metrics["phases"]["death"]["team_dismissals"] == 4
    assert metrics["hat_tricks"] == 1 and not metrics["wins"]
    samples = [SimpleNamespace(stage="league", metrics=metrics)]
    for metric, target in (("boundaries", 100), ("centuries", 2), ("fifties", 10), ("wickets", 50)):
        assert goals.measure({"metric": metric, "target": target}, samples)[1]
    assert goals.measure({"metric": "runs", "role": "keeper", "phase": "powerplay", "target": 6}, samples) == (6, True)
    assert not goals.measure({"metric": "aggregate_strike_rate", "target": 40, "min_balls": 12}, samples)[1]
    assert goals.measure({"metric": "aggregate_economy", "target": 4, "min_balls": 60,
                          "comparator": "<="}, samples) == (4.0, True)


def test_weighted_claim_races_create_each_threshold_once(sessions):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with sessions() as db:
        event = _new(db)
        user = _users(db)[0]
        user.subscription_tier = "diamond"
        room = competitions.create(db, user, event.id, "solo", "team-0", NOW)
        row = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        entrant = competitions.membership(db, event, user.id)
        db.add(StudioFixtureResult(fixture_id=row.id, kind="completed", winner=None, detail={}, recorded_at=NOW))
        db.flush()
        db.add(StudioStarClaim(participant_id=entrant.id, objective_id="matches", fixture_id=row.id,
                               stars=4, earned_at=NOW, expires_at=NOW + timedelta(days=1)))
        event_id, user_id = event.id, user.id
        db.commit()
    barrier = Barrier(2)

    def claim(_):
        with sessions() as db:
            user = db.get(User, user_id)
            barrier.wait(timeout=10)
            result = competitions.claim(db, user, event_id, NOW)
            db.commit()
            return result

    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(claim, range(2)))
    assert first == second and first["claimed_stars"] == 4
    with sessions() as db:
        rewards = db.scalars(select(StudioCompetitionReward)).all()
        assert len(rewards) == 1 and rewards[0].tier == "diamond" and rewards[0].assets == {"coins": 40}


def test_migration_rerun_keeps_legacy_and_shared_rows(sessions, monkeypatch):
    import database
    from sqlalchemy import inspect
    with sessions() as db:
        event = _publish(db, _seed(db))
        user = _users(db)[0]
        entry = competitions.legacy.join(db, user, event.id, "team-0", "tournament_team", NOW)
        entry_id = entry.id
        db.commit()
        engine = db.get_bind()
        metadata = database.Base.metadata
        before = {table for table in metadata.tables.values()
                  if table.name.startswith(("studio_competition", "studio_participants", "studio_fixture",
                                            "studio_contributions", "studio_star_claims"))}
        # Later Studio tables depend on the shared schema being removed in this test.
        while True:
            children = {table for table in metadata.tables.values() if table not in before
                        and any(key.column.table in before for key in table.foreign_keys)}
            if not children:
                break
            before.update(children)
        metadata.drop_all(engine, tables=before)
        assert "studio_competitions" not in inspect(engine).get_table_names()
        monkeypatch.setattr(database, "engine", engine)
        database._create_missing_tables()
        database._create_missing_tables()
        assert db.get(competitions.StudioEnrollment, entry_id).team_key == "team-0"
        assert db.scalar(select(func.count()).select_from(StudioCompetition)) == 0


def test_odi_multiplayer_gate_and_one_canonical_over(sessions, monkeypatch):
    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    monkeypatch.setattr(competitions, "randbelow", lambda _: 0)
    with sessions() as db:
        event = _new(db, format="ODI")
        host, guest = _users(db)
        with pytest.raises(ValueError, match="unavailable"):
            competitions.create(db, host, event.id, "multi", "team-0", NOW)
        monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER_ODI", "1")
        room = competitions.create(db, host, event.id, "multi", "team-0", NOW)
        competitions.change_lobby(db, guest, room.id, "claim", NOW, team="team-1", invite=room.invite)
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        now = max(NOW, fixture.opens_at)
        for entrant, command, value in ((host, "ready", None), (guest, "ready", None),
                                        (host, "coin", "heads"), (host, "elect", "bat")):
            competitions.setup(db, entrant, fixture.id, now, action=command, version=fixture.version, value=value)
        for entrant in (host, guest):
            setup = competitions.setup(db, entrant, fixture.id, now)
            competitions.setup(db, entrant, fixture.id, now, action="xi", version=fixture.version, value=setup["default_xi"])
        competitions.setup(db, host, fixture.id, now, action="start", version=fixture.version)
        assert fixture.state["ball_format"] == "ODI" and fixture.state["overs"] == 50
        bowling = competitions.cricket.match.eligible_bowlers(fixture.state)
        assert len(bowling) == 5 and all(competitions.cricket.match.overs_left(fixture.state, player) == 10 for player in bowling)
        for entrant, command, value in ((guest, picks.PICK_BOWLER, bowling[0]["roster_id"]),
                                       (guest, picks.PICK_BOWL_APPROACH, 1),
                                       (host, picks.PICK_BAT_APPROACH, 2)):
            competitions.action(db, entrant, fixture.id, fixture.version, command, value, now, source="app")
        assert fixture.step == 1 and competitions.cricket.match.balls_bowled(fixture.state) == 6
        assert all(row["phase"] == "powerplay" for row in fixture.state["record_deliveries"])


def test_telegram_browser_is_owned_and_group_lobby_is_shared(sessions, monkeypatch):
    from datetime import datetime
    from handlers import event_competitions as chat

    class Clock(datetime):
        @classmethod
        def utcnow(cls):
            return NOW

    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    monkeypatch.setattr(chat, "get_session", sessions)
    monkeypatch.setattr(chat, "datetime", Clock)
    with sessions() as db:
        event = _new(db)
        draft = studio.create(db, "Unpublished", "unpublished", "admin")
        assert draft.status == "draft"
        host, guest = _users(db)
        event_id, host_id, guest_id = event.id, host.telegram_id, guest.telegram_id
        db.commit()
    def screen(user, *parts):
        return chat._dispatch(user, -77, False, [str(part) for part in parts], {})
    text, rows, _ = screen(host_id, "browser", host_id, 0)
    assert "Summer Cup" in text and "Unpublished" not in text
    assert [button.text for button in rows[0]] == ["Join", "Next", "Close"]
    with pytest.raises(ValueError, match="your own"):
        screen(guest_id, "browser", host_id, 1)
    text, rows, _ = screen(host_id, "open", host_id, event_id)
    assert [row[0].text for row in rows[:3]] == ["Join Solo", "Join Multi", "Join Existing Lobby"]
    text, rows, _ = screen(host_id, "create", host_id, event_id, "multi", 0)
    assert "Multiplayer lobby" in text
    data = next(b.callback_data for row in rows for b in row if b.callback_data.startswith("ec:seat:"))
    text, rows, _ = screen(guest_id, *data.split(":")[1:])
    assert "Tournament started" in text


def test_authenticated_competition_routes(tmp_path):
    import os
    import subprocess
    import sys

    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'competition-api.db'}", "BOT_TOKEN": "fixture-test",
           **{f"EVENT_STUDIO_{flag}": "1" for flag in ("PUBLISH", "LIVE", "T20", "MULTIPLAYER")}}
    subprocess.run([sys.executable, "-c",
                    "from tests.test_event_competitions import _exercise_competition_routes; _exercise_competition_routes()"],
                   check=True, env=env)


def _exercise_competition_routes():
    from datetime import datetime
    import hmac
    import json
    import os
    import time
    from urllib.parse import urlencode
    import admin
    from database import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    now = datetime.utcnow()
    with SessionLocal() as db:
        config = _seed(db)
        config.update(timezone="UTC", opens_at=(now - timedelta(hours=1)).isoformat(),
                      closes_at=(now + timedelta(days=4)).isoformat(), objective_scope="tournament",
                      mode=["tournament_team"], objectives=[])
        draft = studio.create(db, "Signed Shared Cup", "signed-cup", "tester", studio.generate(config, "all"))
        studio.publish(db, draft, 1, "tester", now)
        db.commit()
        event = draft.id

    def signed(user):
        params = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user})}
        key = hmac.digest(b"WebAppData", os.environ["BOT_TOKEN"].encode(), "sha256")
        params["hash"] = hmac.digest(key, "\n".join(f"{k}={v}" for k, v in sorted(params.items())).encode(), "sha256").hex()
        return {"Authorization": "tma " + urlencode(params)}

    client = admin.app.test_client()
    base = "/api/webapp/events-hub"
    assert client.post(f"{base}/{event}", json={}).status_code == 401
    uri = f"{base}/competitions/{event}"
    opened = client.post(uri, headers=signed(1000), json={"action": "create", "mode": "multi", "team": "team-0"})
    assert opened.status_code == 200, opened.json
    lobby = opened.json["competition"]["room"]
    assert client.post(f"{base}/lobby", headers=signed(1001), json={"room_id": lobby["id"]}).status_code == 400
    joined = client.post(f"{base}/lobby", headers=signed(1001), json={"invite": lobby["invite"], "action": "claim", "team": "team-1"})
    assert joined.json["lobby"]["status"] == "running", joined.json
    shared = client.post(f"{base}/{event}", headers=signed(1000), json={}).json["competition"]
    fixture = shared["fixtures"][0]["id"]
    ready = client.post(f"{base}/fixture/{fixture}", headers=signed(1000), json={"action": "ready"})
    assert ready.json["setup"]["ready"], ready.json
    with client.session_transaction() as state:
        state["admin"] = True
    response = client.get(f"/events/studio/{event}")
    assert response.status_code == 200 and b"Canonical fixtures and technical operations" in response.data


@pytest.mark.parametrize("width", [375, 1200])
def test_miniapp_mobile_and_desktop_join_solo(width):
    from pathlib import Path
    import shutil

    playwright = pytest.importorskip("playwright.sync_api")
    executable = shutil.which("chromium")
    if not executable:
        pytest.skip("Chromium unavailable")
    root = Path(__file__).resolve().parents[1]
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(executable_path=executable, headless=True,
                                          args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 850})
            page.set_content('<section id="screen-events" class="events-hub"><h1 id="events-hub-title"></h1>'
                             '<div id="events-hub-status"></div><main id="events-hub-content"></main></section>')
            page.add_style_tag(path=root / "static/css/events_hub.css")
            page.add_script_tag(content="""
                let joined = false;
                const team = {key: 'north', name: 'North', ovr: 79, eligible: true, venue: 'City Ground',
                              squad: [{name: 'Player One', category: 'Bat', rating: 79}]};
                async function api(path, body) {
                  if (body.action === 'create') joined = true;
                  const event = {id: 1, name: 'City Cup', format: 'T20', timezone: 'UTC', fixture_count: 1,
                    enroll_until: '2026-12-01T08:00:00Z', close: '2026-12-02T08:00:00Z',
                    joined, difficulty: 'hard', registration_open: true, modes: ['solo'],
                    objectives: [], teams: [team], maximum: 0, milestones: [],
                    room: {kind: 'solo',status: 'running'}, team: 'north',
                    my_standing:{points:0,nrr:0}, remaining:1, form: [],
                    fixtures:[{id:7,round:1,home:'North',away:'South',status:'scheduled',personal:true,
                      opens_at:'2026-12-01T07:00:00Z',deadline:'2026-12-01T08:00:00Z'}],
                    standings:[], stars:{claimed:0,earned:0,unclaimed:0,objectives:[]}, rewards:[],
                    statistics:{batting:[],bowling:[]},leaderboard:[]};
                  return {ok:true,data:{ok:true,server_time:new Date().toISOString(),competition:event}};
                }
            """)
            page.add_script_tag(path=root / "static/js/events_hub.js")
            page.evaluate("EventHub.open(1)")
            page.get_by_text("Join Solo", exact=True).click()
            page.get_by_text("North", exact=True).click()
            page.get_by_role("button", name="Confirm North").click()
            page.get_by_text("0 points · NRR 0.000 · 1 fixtures left", exact=True).wait_for()
            assert page.get_by_text("Round 1 · North v South", exact=True).first.is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


@pytest.mark.parametrize("format", ["T20", "ODI"])
def test_competition_solo_uses_shared_ai_approach_to_completion(sessions, monkeypatch, format):
    monkeypatch.setattr(competitions, "randbelow", lambda _: 0)
    with sessions() as db:
        event = _new(db, format=format)
        user = _users(db)[0]
        room = competitions.create(db, user, event.id, "solo", "team-0", NOW)
        game = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        now = max(NOW, game.opens_at)
        def setup(action, value=None):
            return competitions.setup(db, user, game.id, now, action=action, value=value, version=game.version)
        setup("ready")
        setup("coin", "heads")
        setup("elect", "bowl")
        panel = competitions.setup(db, user, game.id, now)
        setup("xi", panel["default_xi"])
        setup("start")
        assert game.state["ball_format"] == format and game.state["is_bot_match"]
        for _ in range(320):
            if game.status != "in_progress":
                break
            state = game.state
            assert competitions._actor(state) in ("team-0", None)
            kind = next((name for name, step in picks.PICK_ACTION.items() if step == state["event_next_action"]), "continue")
            value = 0
            if kind == picks.PICK_BOWLER:
                value = competitions.cricket.match.eligible_bowlers(state)[0]["roster_id"]
            elif kind == picks.PICK_NEW_BATSMAN:
                value = competitions.cricket.match.available_batsmen(state)[0]["roster_id"]
            competitions.action(db, user, game.id, game.version, kind, value, now, source="app")
        assert db.get(StudioFixtureResult, game.id).kind == "completed"
        assert db.scalar(select(func.count()).select_from(StudioContribution)) == 1
        assert db.scalar(select(func.count()).select_from(StudioStarClaim)) == 1


def test_scheduler_and_host_race_materializes_one_tournament(sessions, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    with sessions() as db:
        event = _new(db)
        user = _users(db)[0]
        room = competitions.create(db, user, event.id, "multi", "team-0", NOW)
        room_id, version, user_id, until = room.id, room.version, user.id, room.join_until
        db.commit()
    barrier = Barrier(2)

    def start(host):
        with sessions() as db:
            barrier.wait(timeout=10)
            if host:
                user = db.get(User, user_id)
                competitions.change_lobby(db, user, room_id, "start", until - timedelta(seconds=1),
                                          version=version)
            else:
                competitions.expire_lobby(db, room_id, until)
            db.commit()

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(start, [False, True]))
    with sessions() as db:
        assert db.get(StudioCompetition, room_id).status == "running"
        assert db.scalar(select(func.count()).select_from(StudioFixture)) == 1
        assert db.scalar(select(func.count()).select_from(StudioParticipant)) == 2


def test_generated_tournament_road_uses_guaranteed_games_and_caps_budget(sessions):
    from services import event_studio_rules as rules

    with sessions() as db:
        config = _seed(db, 4)
        config.update(objective_scope="tournament", mode=["tournament_team"], objectives=[],
                      tournament_objectives=[{"id": "locked-wins", "metric": "wins", "target": 1,
                                              "stars": 2, "locked": True}])
        config["schedule"]["legs"] = 2
        config["competition"]["match_rewards"] = False
        config["milestones"] = []
        generated = studio.generate(studio.normalize(config), "all")
        repeated = studio.generate(generated, "all")
        assert generated == repeated
        assert goals.guaranteed(generated) == 6 and len(generated["fixtures"]) == 12
        assert any(row["id"] == "locked-wins" and row["stars"] == 2 for row in generated["tournament_objectives"])
        assert not generated["rewards"]
        assert generated["milestones"][-1]["stars"] == goals.max_stars(generated)
        assert all(amount <= generated["budget"][item] for item, amount in rules.liability(generated).items())


def test_verified_incident_past_deadline_requires_extension_or_neutral_result(sessions):
    with sessions() as db:
        event = _new(db)
        host = _users(db)[0]
        room = competitions.create(db, host, event.id, "solo", "team-0", NOW)
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        now = fixture.deadline + timedelta(seconds=1)
        competitions.note_failure(db, fixture.id, now, RuntimeError("engine unavailable"))
        with pytest.raises(ValueError, match="Extend the fixture"):
            competitions.administer(db, event.id, "admin", "retry_fixture", fixture.id, "Verified engine failure", now)
        competitions.administer(db, event.id, "admin", "void_fixture", fixture.id, "Verified engine failure", now)
        result = db.get(StudioFixtureResult, fixture.id)
        assert result.kind == "no_result" and result.winner is None
        assert db.scalar(select(func.count()).select_from(StudioContribution)) == 0
