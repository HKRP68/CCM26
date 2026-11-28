"""Canonical Event Pass grants, format progression and Phase 2 rule coverage."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from config import MAX_ROSTER
from models import Pack, UnopenedPack, User, UserRoster
from models_events import StudioAwardMedia, StudioFixture, StudioFixtureResult, StudioRewardItem
from services import event_studio as studio, event_studio_competition as play
from services import event_studio_brackets as brackets, event_studio_items as items, pack_service
from tests.test_event_studio import NOW, _publish, _seed, sessions  # noqa: F401
from tests.test_live_event_expansion import _finish, _users


def _config(db, teams=2):
    config = _seed(db, teams)
    config.update(objective_scope="tournament", objective_preset="custom", mode=["tournament_team"],
                  objectives=[], milestones=[], tournament_objectives=[
                      {"id": "played", "metric": "played_matches", "target": 1, "stars": 4},
                  ])
    config["competition"]["match_rewards"] = False
    return config


def _generated(config):
    return studio.generate(studio.normalize(config), "all")


def test_reward_claim_grants_pack_and_versioned_card_once(sessions, monkeypatch):
    monkeypatch.setattr(play, "get_tier", lambda _: "diamond")
    with sessions() as db:
        config = _config(db)
        config["competition"]["event_pass"] = True
        pack = Pack(slot_number=1, name="Tournament pack", main_min_rating=75, main_max_rating=75,
                    main_count=1, bonus_count=0, is_active=True)
        db.add(pack)
        db.flush()
        pack_item = {"kind": "pack", "pack_id": pack.id}
        config["pass_inventory"].update(packs=4, cards=4)
        config["milestones"] = [
            {"id": "chest", "stars": 4, "free_items": [pack_item], "diamond_items": [
                pack_item, {"kind": "random_player", "min_rating": 75, "max_rating": 75, "version": "Base"},
            ]}
        ]
        assert items.liability(config) == {"packs": 4, "cards": 4}
        event = _publish(db, _generated(config))
        user = _users(db)[0]
        room = play.create(db, user, event.id, "solo", "team-0", NOW)
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        _finish(db, room, event, fixture, play._config(db, room), NOW)
        play.claim(db, user, event.id, NOW)
        db.flush()
        user_id, event_id, pack_id = user.id, event.id, pack.id
        assert play.dashboard(db, user, event.id, NOW)["event_pass"]["milestones"][0]["state"] == "claimable"
        db.commit()
    with sessions() as db:
        user = db.get(User, user_id)
        user.roster_count = MAX_ROSTER
        db.flush()
        with pytest.raises(ValueError, match="roster slots"):
            play.claim(db, user, event_id, NOW, wallet=True)
        db.rollback()
    barrier = Barrier(2)

    def claim(_):
        with sessions() as db:
            user = db.get(User, user_id)
            barrier.wait(10)
            result = play.claim(db, user, event_id, NOW, wallet=True)
            db.commit()
            return result

    with ThreadPoolExecutor(2) as pool:
        first, second = list(pool.map(claim, range(2)))
    assert first == second
    assert [item["kind"] for item in first["items"]] == ["pack", "player"]
    with sessions() as db:
        user = db.get(User, user_id)
        assert db.scalar(select(func.count(StudioRewardItem.reward_id))) == 2
        assert db.scalar(select(func.count(UserRoster.id)).filter_by(user_id=user_id)) == 1
        inv = db.scalar(select(UnopenedPack).filter_by(user_id=user_id))
        assert inv.pack_id == pack_id
        opened = pack_service.open_unopened_pack(db, user, inv.id)
        assert opened["success"] and opened["history_id"]
        assert len(opened["added"]) == 1


@pytest.mark.parametrize("force_reset", [False, True])
def test_double_elimination_materializes_reset_only_after_upper_loss(sessions, force_reset):
    with sessions() as db:
        config = _config(db, 5)
        config["competition"]["modes"] = ["solo"]
        config["competition"]["honour_board"] = True
        config["schedule"].update(template="double_elimination", preset="double_elimination", grand_final_reset=True)
        config["tournament_objectives"][0]["stages"] = ["playoff"]
        event = _publish(db, _generated(config))
        user = _users(db)[0]
        room = play.create(db, user, event.id, "solo", "team-0", NOW)
        config = play._config(db, room)
        defeats = {team: 0 for team in config["snapshot"]}
        for fixture, result in play._rows(db, room):
            if fixture.fixture_key == brackets.RESET and not force_reset:
                assert db.get(StudioFixtureResult, fixture.id).detail["reason"] == "grand_final_reset_not_needed"
                continue
            sides = play.legacy._resolve(config, play.rules.fixture_rows(config)[fixture.fixture_key], play._rows(db, room))
            assert len(set(sides)) == 2
            fixture.home, fixture.away = sides
            assert all(defeats[team] < 2 for team in sides)
            drop = 0 if force_reset and fixture.fixture_key == brackets.FINAL else 1
            defeats[sides[drop]] += 1
            play._finish(db, fixture, room, event, config, "forfeited", fixture.deadline,
                         winner=sides[1-drop], reason="deadline", offender=sides[drop])
        assert room.status == "completed"
        assert sorted(defeats.values()) == [int(force_reset), 2, 2, 2, 2]
        db.flush()
        view = play.dashboard(db, user, event.id, NOW)
        assert view["honour_board"]["champion_name"]
        assert view["honour_board"]["runner_up"]


def test_mystery_registration_deterministic_and_multiplayer_no_seat_selection(sessions, monkeypatch):
    monkeypatch.setenv("EVENT_STUDIO_MULTIPLAYER", "1")
    with sessions() as db:
        config = _config(db, 3)
        config["schedule"]["template"] = "mystery"
        config["competition"]["lobby_v2"] = True
        event = _publish(db, _generated(config))
        host, guest = _users(db)
        with pytest.raises(ValueError, match="assign"), db.begin_nested():
            play.create(db, host, event.id, "multi", "team-0", NOW, consent=True)
        room = play.create(db, host, event.id, "multi", None, NOW, consent=True, visibility="public")
        first_team = play.membership(db, event, host.id).team_key
        assert "mystery_seed" not in str(play.dashboard(db, host, event.id, NOW))
        with pytest.raises(ValueError, match="assign"):
            play.change_lobby(db, guest, room.id, "claim", NOW)
        play.change_lobby(db, guest, room.id, "claim", NOW, consent=True)
        assert play.membership(db, event, guest.id).team_key != first_team
        room_id, event_id, guest_id = room.id, event.id, guest.id
        db.commit()
    with sessions() as db:
        guest = db.get(User, guest_id)
        assigned = play.lobby(db, guest, room_id, now=NOW)["my_team"]
        assert play.change_lobby(db, guest, room_id, "claim", NOW, consent=True)["my_team"] == assigned
        with pytest.raises(ValueError, match="stay fixed"):
            play.change_lobby(db, guest, room_id, "release", NOW)


def test_news_awards_graphics_worker_recovers_from_committed_snapshot(sessions, monkeypatch):
    from base64 import b64decode
    from services import event_studio_award_media as media

    monkeypatch.setattr(media, "get_session", sessions)
    with sessions() as db:
        config = _config(db)
        config["competition"].update(news=True, honour_board=True)
        event = _publish(db, _generated(config))
        user = _users(db)[0]
        room = play.create(db, user, event.id, "solo", "team-0", NOW)
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        _finish(db, room, event, fixture, play._config(db, room), NOW)
        db.flush()
        view = play.dashboard(db, user, event.id, NOW)
        assert "player_of_tournament" in view["honour_board"]["awards"]
        assert "team_of_tournament" in view["honour_board"]["awards"]
        assert any(item["kind"] == "match_report" for item in view["news"]["items"])
        room_id = room.id
        db.commit()
    media.sweep(NOW)
    media.sweep(NOW + timedelta(minutes=10))
    with sessions() as db:
        job = db.get(StudioAwardMedia, room_id)
        assert job.generated_at == NOW and job.attempts == 1
        assert b64decode(job.images["player_of_tournament"]).startswith(b"\x89PNG")


def test_ladder_entitlements_created_from_completed_official_fixture(sessions):
    with sessions() as db:
        config = _config(db)
        config["objective_ladders"] = {"metric_version": 1, "objectives": [{
            "id": "team_runs", "metric": "runs",
            "stages": [{"id": "bronze_runs", "target": 5, "stars": 2},
                       {"id": "silver_runs", "target": 10, "stars": 3}],
        }]}
        event = _publish(db, _generated(config))
        user = _users(db)[0]
        room = play.create(db, user, event.id, "solo", "team-0", NOW)
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        _finish(db, room, event, fixture, play._config(db, room), NOW)
        db.flush()
        view = play.dashboard(db, user, event.id, NOW)
        assert {row["id"] for row in view["stars"]["objectives"] if row["status"] == "ready"} == {
            "played", "bronze_runs", "silver_runs",
        }
        assert play.claim(db, user, event.id, NOW)["claimed_stars"] == 9


@pytest.mark.parametrize("format_name", ["T20", "ODI"])
def test_dressing_changes_solo_engine_copy_but_not_published_ratings(sessions, format_name):
    with sessions() as db:
        config = _config(db)
        config["format"] = format_name
        config["competition"].update(modes=["solo"], dressing_room=True, management_cap=1,
                                     management_weights={"morale": 1, "unity": 1, "form": 1, "fatigue": 1, "captain": 1})
        config["schedule"]["legs"] = 2
        event = _publish(db, _generated(config))
        user = _users(db)[0]
        room = play.create(db, user, event.id, "solo", "team-0", NOW)
        first, second = [row for row, _ in play._rows(db, room)]
        frozen = play._config(db, room)
        _finish(db, room, event, first, frozen, first.deadline)
        caller = play.membership(db, event, user.id)
        published_xi = frozen["snapshot"][caller.team_key]["default_xi"]
        second.setup = {"toss": {"winner": caller.team_key, "decision": "bat"},
                        "xis": {caller.team_key: [card["roster_id"] for card in published_xi]}}
        play._launch(db, second, room, event, frozen, {caller.team_key: caller}, second.opens_at, "app")
        assert second.state["event_management"]["cap"] == 1
        differences = [card["bat_rating"] - frozen_card["bat_rating"]
                       for card, frozen_card in zip(second.state["bat_xi"], published_xi)]
        assert any(delta for delta in differences)
        assert all(-1 <= delta <= 1 for delta in differences)
        assert all(card["card_bat_rating"] == frozen_card["bat_rating"]
                   for card, frozen_card in zip(second.state["bat_xi"], published_xi))


def test_world_tour_and_auto_ladders_use_the_authored_series(sessions):
    with sessions() as db:
        config = _config(db)
        config["competition"]["modes"] = ["solo"]
        config["schedule"].update(template="world_tour", preset="series",
                                  tour=[{"country": "India", "games": 1}, {"country": "Australia", "games": 1}])
        config["ladder_generator"] = {"metrics": ["runs", "wickets"], "stage_count": 2,
                                      "star_cap": 20, "preset": "Balanced"}
        draft = _generated(config)
        assert len(draft["fixtures"]) == 2
        assert draft["generation"]["objectives"]["ladder_version"] == 1
        assert draft["objective_ladders"]["metric_version"] == 1
        event = _publish(db, draft)
        user = _users(db)[0]
        play.create(db, user, event.id, "solo", "team-0", NOW)
        view = play.dashboard(db, user, event.id, NOW)
        assert [stop["country"] for stop in view["tour_series"]] == ["India", "Australia"]
        assert all(not stop["complete"] for stop in view["tour_series"])


def test_auto_item_only_pass_enforces_global_budget(sessions):
    with sessions() as db:
        config = _config(db)
        card_id = config["teams"][0]["players"][0]
        definition = {"kind": "player", "player_id": card_id}
        config["budget"] = dict.fromkeys(config["budget"], 0)
        config["competition"]["event_pass"] = True
        config["pass_inventory"].update(auto_free=[definition], auto_diamond=[definition], cards=3)
        with pytest.raises(ValueError, match="inventory budget"):
            _generated(config)
        config["pass_inventory"]["cards"] = 4
        draft = _generated(config)
        assert len(draft["milestones"]) == 1
        assert draft["milestones"][0]["free_items"] == [definition]
        assert _publish(db, draft).published_number


@pytest.mark.parametrize("width", [390, 1280])
def test_miniapp_claim_inventory_and_navigate_to_existing_pack_screen(width):
    from pathlib import Path
    import shutil

    playwright = pytest.importorskip("playwright.sync_api")
    chromium = shutil.which("chromium")
    if not chromium:
        pytest.skip("Chromium unavailable")
    repo = Path(__file__).resolve().parents[1]
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium, headless=True,
                                     args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 850})
            page.set_content('<section id="screen-events" class="events-hub"><h1 id="events-hub-title"></h1>'
                             '<div id="events-hub-status" role="status"></div><main id="events-hub-content"></main></section>')
            page.add_style_tag(path=repo / "static/css/events_hub.css")
            page.add_script_tag(content="""
              let paid = false;
              function navigate(screen) { window.destination = screen; }
              function view() {
                const reward = {id: 7, tier: 'free', assets: {}, items: ['Prize pack', 'Opener'], source: 'pass:chest',
                  status: paid ? 'claimed' : 'claimable', expires_at: '2026-12-15T10:00:00Z'};
                return {id:1, name:'Weekend Cup', format:'T20', timezone:'UTC', fixture_count:1,
                  enroll_until:'2026-12-13T10:00:00Z', close:'2026-12-14T10:00:00Z', description:'',
                  joined:true, room:{kind:'solo',status:'completed'}, teams:[{key:'north',name:'North',squad:[]}],
                  team:'north', my_standing:{points:2,nrr:0}, remaining:0, form:[], fixtures:[], standings:[],
                  stars:{earned:4,unclaimed:0,claimed:4,objectives:[]}, maximum:4, rewards:[reward],
                  event_pass:{claimed:4,next_target:null,expires_at:reward.expires_at,
                    milestones:[{id:'chest',stars:4,state:reward.status,reward}]}, milestones:[],
                  statistics:{batting:[],bowling:[]}, leaderboard:[]};
              }
              async function api(path, body) {
                const response = {ok:true,server_time:new Date().toISOString()};
                if (body.action === 'claim_rewards') {
                  paid = true;
                  window.rewardClaim = body.reward_id;
                  Object.assign(response,{wallet_receipts:[88],balances:{coins:0},
                    items:[{kind:'pack',name:'Prize pack'},{kind:'player',name:'Opener'}]});
                } else response.competition = view();
                return {ok:true,data:response};
              }
            """)
            page.add_script_tag(path=repo / "static/js/events_hub.js")
            page.evaluate("EventHub.open(1)")
            page.get_by_text("Event Pass", exact=True).click()
            page.get_by_role("button", name="Claim Reward", exact=True).click()
            page.get_by_text("Open pack inventory", exact=True).click()
            assert page.evaluate("window.rewardClaim") == 7
            assert page.evaluate("window.destination") == "packs"
            assert page.get_by_role("status").get_by_text("Prize pack", exact=False).is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


def test_signed_rewards_and_awards_across_process_restart(tmp_path):
    import os
    import subprocess
    import sys

    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'phase2.db'}", "BOT_TOKEN": "phase2-local-test",
           **{f"EVENT_STUDIO_{flag}": "1" for flag in ("PUBLISH", "LIVE", "T20")}}
    for stage in ("seed", "verify"):
        subprocess.run([sys.executable, "-c",
                        f"from tests.test_live_events_phase2 import _signed_{stage}; _signed_{stage}()"],
                       env=env, check=True)


def _signed_seed():
    from datetime import datetime

    from database import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    now = datetime.utcnow()
    with SessionLocal() as db:
        config = _config(db)
        config.update(timezone="UTC", opens_at=(now - timedelta(hours=1)).isoformat(),
                      closes_at=(now + timedelta(days=4)).isoformat())
        config["competition"].update(modes=["solo"], event_pass=True, honour_board=True)
        player = config["teams"][0]["players"][0]
        config["pass_inventory"]["cards"] = 4
        config["milestones"] = [{"id": "first", "stars": 4,
                                "free_items": [{"kind": "player", "player_id": player}],
                                "diamond_items": [{"kind": "player", "player_id": player}]}]
        event = studio.create(db, "Persistent Cup", "persistent-cup", "tester", _generated(config))
        studio.publish(db, event, 1, "tester", now)
        user = _users(db)[0]
        room = play.create(db, user, event.id, "solo", "team-0", now)
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        fixture.seed = "24" * 32
        _finish(db, room, event, fixture, play._config(db, room), now)
        db.commit()


def _signed_verify():
    import hmac
    import json
    import os
    import time
    from urllib.parse import urlencode

    import admin
    from services import event_studio_award_media as media

    def signed(telegram_id):
        values = {"auth_date": str(int(time.time())), "user": json.dumps({"id": telegram_id})}
        key = hmac.digest(b"WebAppData", os.environ["BOT_TOKEN"].encode(), "sha256")
        content = "\n".join(f"{name}={value}" for name, value in sorted(values.items()))
        values["hash"] = hmac.digest(key, content.encode(), "sha256").hex()
        return {"Authorization": "tma " + urlencode(values)}

    media.sweep()
    client = admin.app.test_client()
    api = "/api/webapp/events-hub"
    awards = f"{api}/honours/1"
    assert client.post(awards, json={}).status_code == 401
    assert client.post(awards, headers=signed(1001), json={}).status_code == 400
    authorized = client.post(awards, headers=signed(1000), json={})
    assert authorized.status_code == 200
    assert authorized.json["images"]["player_of_tournament"].startswith("iVBOR")
    route = f"{api}/competitions/1"
    assert client.post(route, headers=signed(1000), json={"action": "claim_stars"}).json["claimed_stars"] == 4
    first = client.post(route, headers=signed(1000), json={"action": "claim_rewards"}).json
    second = client.post(route, headers=signed(1000), json={"action": "claim_rewards"}).json
    assert first["items"] == second["items"] and first["items"][0]["kind"] == "player"
    with client.session_transaction() as session:
        session["admin"] = True
    assert client.get("/events/studio/1").status_code == 200
    graphic = client.get("/events/studio/1/awards/1/player_of_tournament")
    assert graphic.status_code == 200 and graphic.data.startswith(b"\x89PNG")


def test_dynasty_links_verified_same_franchise_history_without_carrying_stars(sessions):
    from copy import deepcopy

    with sessions() as db:
        base = _config(db)
        base["competition"].update(modes=["solo"], honour_board=True)
        first = _publish(db, _generated(base))
        user = _users(db)[0]
        room = play.create(db, user, first.id, "solo", "team-0", NOW)
        fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        _finish(db, room, first, fixture, play._config(db, room), NOW)
        claim = play.claim(db, user, first.id, NOW)
        assert claim["claimed_stars"] == 4
        db.flush()

        config = deepcopy(base)
        start = first.closes_at + timedelta(hours=1)
        config.update(timezone="UTC", opens_at=start.isoformat(),
                      closes_at=(start + timedelta(days=5)).isoformat())
        config["schedule"].update(template="dynasty", previous_event_id=first.id)
        second = studio.create(db, "Second Edition", "second-edition", "operator", _generated(config))
        studio.publish(db, second, 1, "operator", NOW)
        play.create(db, user, second.id, "solo", "team-0", start)
        view = play.dashboard(db, user, second.id, start)
        assert view["dynasty_history"][0]["played"] == 1
        assert view["dynasty_history"][0]["event_id"] == first.id
        assert view["stars"]["claimed"] == 0
