"""Opt-in Live Event features and transaction regressions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select

from models import User
from models_events import (
    StudioCompetition, StudioFixture, StudioGroupNotice, StudioHonourSnapshot,
    StudioLobbyPolicy, StudioQualificationJob,
)
from services import (
    event_studio as studio, event_studio_competition as play, event_studio_cricket as cricket,
    event_studio_group as group, event_studio_manager as manager,
    event_studio_predictor as predictor, event_studio_rules as rules, event_studio_schedule as schedule,
)
from tests.test_event_competitions import _users
from tests.test_event_studio import NOW, _publish, _seed, sessions  # noqa: F401


def _event(db, *, teams=2, playoffs="none", **features):
    config = _seed(db, teams)
    config.update(objective_scope="tournament", objective_preset="custom", mode=["tournament_team"],
                  objectives=[], milestones=[], tournament_objectives=[
                      {"id": "played", "metric": "played_matches", "target": 1, "stars": 4},
                  ])
    config["competition"].update(match_rewards=False, **features)
    config["schedule"]["playoffs"] = playoffs
    return _publish(db, studio.generate(studio.normalize(config), "all"))


def _finish(db, room, event, fixture, config, now):
    xis = {team: play.legacy._opponent_xi(config["snapshot"][team], config, "tournament_team")
           for team in (fixture.home, fixture.away)}
    fixture.state = cricket.build_state(config, fixture.id, fixture.home, fixture.away, xis, fixture.seed)
    for step in range(340):
        if cricket.advance(fixture.state, fixture.seed, step):
            break
    else:
        pytest.fail("The fixture did not reach a terminal result")
    play._finish(db, fixture, room, event, config, "completed", now)


def test_pass_concurrent_claim_snapshots_diamond_and_caps_xp(sessions, monkeypatch):
    tier = {"value": "free"}
    monkeypatch.setattr(play, "get_tier", lambda user: tier["value"])
    with sessions() as db:
        event = _event(db, event_pass=True, manager_profile=True, manager_xp_cap=5)
        host = _users(db)[0]
        room = play.create(db, host, event.id, "solo", "team-0", NOW)
        entry = play.membership(db, event, host.id)
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        _finish(db, room, event, fixture, play._config(db, room), NOW)
        assert play.dashboard(db, host, event.id, NOW)["event_pass"]["next_target"] == 1
        assert play.dashboard(db, host, event.id, NOW)["stars"]["unclaimed"] == 4
        user_id, event_id = host.id, event.id
        assert entry.reward_tier is None
        db.commit()

    tier["value"] = "diamond"

    def race(wallet):
        barrier = Barrier(2)

        def run(_):
            with sessions() as db:
                user = db.get(User, user_id)
                barrier.wait(timeout=15)
                receipt = play.claim(db, user, event_id, NOW, wallet=wallet)
                db.commit()
                return receipt

        with ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, range(2)))

    assert [row["claimed_stars"] for row in race(False)] == [4, 4]
    tier["value"] = "free"
    results = race(True)
    assert results[0] == results[1]
    assert len(results[0]["wallet_receipts"]) == 4
    with sessions() as db:
        user = db.get(User, user_id)
        view = play.dashboard(db, user, event_id, NOW)
        assert all(row["state"] == "claimed" and row["reward"]["tier"] == "diamond"
                   for row in view["event_pass"]["milestones"])
        assert user.total_coins == sum(row["diamond"]["coins"] for row in view["event_pass"]["milestones"])
        assert view["manager"]["xp"] == 5
        later = NOW + timedelta(days=60)
        assert play.dashboard(db, user, event_id, later)["event_pass"]["milestones"][0]["state"] == "claimed"


def test_lobby_visibility_expiry_readiness_and_consented_transfer(sessions, monkeypatch):
    from handlers import event_competitions as telegram

    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    with sessions() as db:
        event = _event(db, teams=3, lobby_v2=True)
        host, guest = _users(db)
        room = play.create(db, host, event.id, "multi", "team-0", NOW, visibility="private", chat_id=-10)
        assert room.chat_id is None
        policy = db.get(StudioLobbyPolicy, room.id)
        assert policy.visibility == "private"
        code = play.lobby(db, host, room.id, now=NOW)["invite"]
        assert room.invite != code and play._by_invite(db, code) == room.id
        text, _, _ = telegram.event_panel(db, host, event.id, NOW, private=False)
        assert "private tournament" in text and code not in text and "Team 0" not in text
        button = telegram._button("Open", "panel", host.telegram_id, event.id, "objectives", 0)
        _, _, owner, *_ = button.callback_data.split(":")
        assert owner != str(host.telegram_id)
        assert len(button.callback_data.encode()) <= 64
        assert telegram._is_owner(owner, host.telegram_id) and not telegram._is_owner(owner, guest.telegram_id)
        assert play.public_lobbies(db, guest, NOW) == []
        with pytest.raises(ValueError, match="invitation"):
            play.lobby(db, guest, room.id, now=room.join_until, invite=code)
        with pytest.raises(ValueError, match="invitation"):
            play.lobby(db, guest, room.id, now=NOW, chat_id=-10)
        play.change_lobby(db, guest, room.id, "claim", NOW, team="team-1", invite=code)
        play.change_lobby(db, guest, room.id, "ready", NOW, version=room.version)
        assert play.lobby(db, host, room.id, now=NOW)["features"]["ready_count"] == 1
        with pytest.raises(ValueError, match="pending"):
            play.change_lobby(db, guest, room.id, "accept_host", NOW, version=room.version)
        play.change_lobby(db, host, room.id, "offer_host", NOW, team="team-1", version=room.version)
        play.change_lobby(db, guest, room.id, "accept_host", NOW, version=room.version)
        assert room.host_id == guest.id
        play.change_lobby(db, host, room.id, "release", NOW)
        assert play.membership(db, event, host.id) is None
        assert play.lobby(db, guest, room.id, now=NOW)["features"]["ready_count"] == 1


def test_one_result_feeds_news_rating_and_awards_and_notices(sessions, monkeypatch):
    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    with sessions() as db:
        event = _event(db, lobby_v2=True, group_engagement=True, dressing_room=True, news=True,
                       manager_profile=True, competitive_rating=True, honour_board=True)
        first, second = _users(db)
        room = play.create(db, first, event.id, "multi", "team-0", NOW, visibility="public", chat_id=-200)
        assert play.public_lobbies(db, second, NOW)[0]["id"] == room.id
        play.change_lobby(db, second, room.id, "claim", NOW, team="team-1")
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        config = play._config(db, room)
        _finish(db, room, event, fixture, config, NOW + timedelta(minutes=2))
        play._finish(db, fixture, room, event, config, "completed", NOW + timedelta(minutes=3))
        db.flush()
        left = play.dashboard(db, first, event.id, NOW)
        right = play.dashboard(db, second, event.id, NOW)
        assert left["news"] == right["news"]
        assert sum(item["kind"] == "match_report" for item in left["news"]["items"]) == 1
        assert left["honour_board"] == right["honour_board"]
        assert db.get(StudioHonourSnapshot, (room.id, 1))
        assert len(left["honour_board"]["awards"]["runs"]) >= 1
        assert sum(p["appearances"] for p in left["dressing_room"]["players"]) == 11
        assert manager.latest(db, first.id, "T20")[0] + manager.latest(db, second.id, "T20")[0] == 3000
        assert left["manager"]["ratings"][0]["provisional"]
        # Tested without sending Telegram messages.
        assert {row.source for row in db.scalars(select(StudioGroupNotice))} >= {
            "lobby:started", "competition:finished",
        }
        delivery = group.reserve(db, NOW + timedelta(minutes=3))
        assert delivery[2] == -200
        assert db.get(StudioGroupNotice, delivery[0]).attempts == 1
        db.commit()


def test_predictor_is_reproducible_and_tracks_result_versions(sessions, monkeypatch):
    monkeypatch.setattr(predictor, "get_session", sessions)
    with sessions() as db:
        event = _event(db, teams=4, playoffs="top4", qualification=True, predictor_samples=80)
        user = _users(db)[0]
        room = play.create(db, user, event.id, "solo", "team-0", NOW)
        room_id, event_id = room.id, event.id
        assert predictor.cached(db, room) is None
        db.commit()
    predictor.sweep(NOW)
    with sessions() as db:
        room = db.get(StudioCompetition, room_id)
        computed = predictor.cached(db, room)
        assert sum(team["percent"] for team in computed["teams"].values()) == 400
        job = db.get(StudioQualificationJob, room_id)
        assert job.computed_version == job.result_version
        event = play._event(db, event_id)
        config = play._config(db, room)
        rows = play._rows(db, room)
        table_config = play._table_config(config)
        board = play.legacy.table(table_config, rows)
        estimate = predictor.estimate(table_config, rows, board, room_id=room_id, version=job.result_version)
        assert estimate["teams"] == computed["teams"]
        first = rows[0][0]
        play._finish(db, first, room, event, config, "no_result", NOW, reason="verified_technical_incident")
        assert predictor.cached(db, room) is None
        db.commit()


def test_weekend_rest_and_budget_with_locked_milestone(sessions):
    with sessions() as db:
        cfg = _seed(db, 4)
        cfg.update(objective_scope="tournament", objectives=[], mode=["tournament_team"],
                   closes_at="2026-12-04T09:00", timezone="UTC")
        cfg["competition"].update(event_pass=True, match_rewards=False)
        cfg["schedule"].update(template="weekend", window_hours=3, grace=30, rest_minutes=90)
        cfg["milestones"] = [{"id": "locked", "stars": 1, "locked": True,
                              "free": {"coins": 10}, "diamond": {"coins": 20}}]
        draft = studio.generate(studio.normalize(cfg), "all", overwrite=True)
        assert draft["milestones"][0] == cfg["milestones"][0]
        assert rules.validate_rewards(draft)["coins"] <= cfg["budget"]["coins"]
        cfg["schedule"]["window_hours"] = 24
        with pytest.raises(ValueError, match="fit"):
            studio.generate(studio.normalize(cfg), "schedule")
        assert schedule.EVENT_TEMPLATES["road_to_final"] == ("survival",)


def test_malformed_pass_rows_and_unsupported_assets(sessions):
    with sessions() as db:
        cfg = _seed(db)
        cfg["objective_scope"] = "tournament"
        cfg.update(objectives=[], mode=["tournament_team"])
        cfg["competition"]["event_pass"] = True
        cfg["milestones"] = [False]
        with pytest.raises(ValueError, match="milestone must be an object"):
            studio.normalize(cfg)
        cfg["milestones"] = [{"id": "bad", "stars": 1, "free": {"pack": 1}, "diamond": {"pack": 1}}]
        with pytest.raises(ValueError, match="Unsupported reward asset"):
            studio.normalize(cfg)


def test_group_delivery_lease_is_single_consumer(sessions, monkeypatch):
    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    with sessions() as db:
        event = _event(db, group_engagement=True)
        host = _users(db)[0]
        room = play.create(db, host, event.id, "multi", "team-0", NOW, chat_id=-300)
        play.change_lobby(db, host, room.id, "start", NOW, version=room.version)
        db.commit()
    barrier = Barrier(2)

    def claim(_):
        with sessions() as db:
            barrier.wait(timeout=15)
            job = group.reserve(db, NOW)
            db.commit()
            return job

    with ThreadPoolExecutor(max_workers=2) as pool:
        deliveries = [item for item in pool.map(claim, range(2)) if item]
    assert len(deliveries) == 1
    assert deliveries[0][2] == -300


@pytest.mark.parametrize("width", [390, 1280])
def test_miniapp_pass_and_private_lobby_layout(width):
    from pathlib import Path
    import shutil

    playwright = pytest.importorskip("playwright.sync_api")
    chromium = shutil.which("chromium")
    if not chromium:
        pytest.skip("Chromium unavailable")
    root = Path(__file__).resolve().parents[1]
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(executable_path=chromium, headless=True,
                                          args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 850})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_content('<section id="screen-events" class="events-hub"><h1 id="events-hub-title"></h1>'
                             '<div id="events-hub-status" role="status"></div><main id="events-hub-content"></main></section>')
            page.add_style_tag(path=root / "static/css/events_hub.css")
            page.add_script_tag(content="""
              const team = {key:'north', name:'North', ovr:79, eligible:true,
                            squad:[{name:'Opener', category:'Bat', rating:79}]};
              const event = {id:1,name:'City Cup',format:'T20',timezone:'UTC',fixture_count:2,
                enroll_until:'2026-12-01T08:00:00Z',close:'2026-12-02T08:00:00Z',registration_open:true,
                joined:true,lobby_v2:true,modes:['multi'],teams:[team],team:'north',room:{kind:'multi',status:'running'},
                fixtures:[],standings:[],form:[],trophy:null,my_standing:{points:2,nrr:0},remaining:1,
                stars:{earned:4,unclaimed:0,claimed:4,objectives:[]},rewards:[],maximum:10,
                event_pass:{claimed:4,next_target:10,expires_at:'2026-12-05T10:00:00Z',
                  milestones:[{id:'silver',stars:10,state:'locked',free:{coins:100},diamond:{coins:200}}]},
                milestones:[], statistics:{batting:[],bowling:[]},leaderboard:[],
                news:{items:[{title:'North won',summary:'North 120/4 · South 119/9',recorded_at:'2026-12-01T07:00:00Z'}]},
                honour_board:{champion_name:null,awards:{}},
                qualification:[{team:'north',name:'North',status:'in contention',remaining:1,max_points:4}],
                qualification_estimate:{teams:{north:{percent:50,sample_margin:5}},model:'Neutral model.',
                  samples:400,calculated_at:'2026-12-01T07:00:00Z'}};
              async function api(path, body) {
                if (path.endsWith('/2') && !body.action) return {ok:true,data:{ok:true,server_time:new Date().toISOString(),
                  competition:{...event,id:2,joined:false,event_pass:null,objectives:[]}}};
                if (body.action === 'create') window.selectedVisibility = body.visibility;
                return {ok:true,data:{ok:true,server_time:new Date().toISOString(),competition:event}};
              }
            """)
            page.add_script_tag(path=root / "static/js/events_hub.js")
            page.evaluate("EventHub.open(1)")
            page.get_by_text("Event Pass", exact=True).click()
            assert page.get_by_text("Next milestone 4/10 stars").is_visible()
            page.get_by_text("Tournament News", exact=True).click()
            assert page.get_by_text("North 120/4 · South 119/9", exact=True).is_visible()
            page.get_by_text("Playoff qualification", exact=True).click()
            assert page.get_by_text("50% ±5 pp", exact=True).is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.evaluate("EventHub.open(2)")
            page.get_by_text("Join Multi", exact=True).click()
            page.get_by_label("Lobby visibility").select_option("private")
            page.get_by_text("North", exact=True).click()
            page.get_by_role("button", name="Confirm North").click()
            page.wait_for_function("window.selectedVisibility === 'private'")
            assert not errors
        finally:
            browser.close()
