"""Canonical source identity, permissions, projections and recovery."""

from dataclasses import replace
from datetime import timedelta
import json

import pytest
from sqlalchemy import func, select

from models import Match, StatsSeason, Tournament, TournamentHonour, TournamentMatch, TournamentTeam, User
from models_events import StudioAIManager, StudioFixture, StudioFixtureResult, StudioTeamPerformance
from models_universe import (CompetitionCatalogue, CompetitionCatalogueLink, CompetitionCollectible, CompetitionRating,
                             CompetitionRatingEvent, CompetitionRotation)
from services import competition_ai as ai
from services import competition_analytics as analytics, competition_collection as collection
from services import competition_operations as operations, competition_rankings as rankings
from services import competition_sources as sources, competition_universe as universe
from services import event_studio as studio, event_studio_competition as play
from tests.test_event_competitions import _users
from tests.test_event_studio import NOW, _publish, sessions  # noqa: F401
from tests.test_live_events_phase2 import _config, _generated, _finish


@pytest.fixture
def enabled(monkeypatch):
    for flag in ("COMPETITION_UNIVERSE", "COMPETITION_UNIVERSE_RANKINGS", "COMPETITION_UNIVERSE_COLLECTION",
                 "COMPETITION_UNIVERSE_ANALYTICS", "COMPETITION_UNIVERSE_ROTATION",
                 "EVENT_STUDIO_MULTIPLAYER", "EVENT_STUDIO_AI_PERSONALITIES"):
        monkeypatch.setenv(flag, "1")


def _challenge(db, users, *, patched=False):
    first, second = users
    tour = Tournament(name="Summer Cup", kind="challenge", status="completed", overs=20)
    db.add(tour)
    db.flush()
    a = TournamentTeam(tournament_id=tour.id, name="North", owner_tg_id=second.telegram_id)
    b = TournamentTeam(tournament_id=tour.id, name="South", owner_tg_id=first.telegram_id)
    match = Match(user1_id=first.id, user2_id=second.id, status="completed", end_reason="completed",
                  tournament_id=tour.id, overs=20, ball_format="T20")
    db.add_all([a, b, match])
    db.flush()
    lines = [{"name": "Centurion", "team_name": "North", "player_id": 122, "bat_runs": 100,
              "bat_balls": 50, "bat_out": False, "batted": True, "bat_fours": 8, "bat_sixes": 3},
             {"name": "Bowler", "team_name": "South", "player_id": 123, "bowl_runs": 60,
              "bowl_balls": 110, "bowl_wickets": 5}]
    fixture = TournamentMatch(tournament_id=tour.id, match_id=match.id, status="completed", result_type="win",
                  winner_team_id=a.id, team1_id=a.id, team2_id=b.id, host_user_id=first.id, target_user_id=second.id,
                  completed_at=NOW, inn1_runs=160, inn2_runs=159, inn1_balls=110, inn2_balls=120,
                  inn1_wickets=5, inn2_wickets=7, scorecard_json=json.dumps(lines))
    db.add(fixture)
    db.flush()
    state = {"mode": "cipl_approach"}
    if patched:
        state["captain_handovers"] = [{"from": first.id}]
    sources.capture_challenge(db, tour, fixture, state)
    db.flush()
    return tour, fixture, match


def test_elo_replays_both_sources_once_and_updates_corrected_result(sessions, enabled):
    with sessions() as db:
        config = _config(db)
        config["competition"].update(world_rankings=True, lobby_v2=True)
        event = _publish(db, _generated(config))
        first, second = _users(db)
        tour, fixture, match = _challenge(db, (first, second))
        room = play.create(db, first, event.id, "multi", "team-0", NOW, visibility="public")
        play.change_lobby(db, second, room.id, "claim", NOW, team="team-1")
        live_fixture = db.scalar(select(StudioFixture).filter_by(competition_id=room.id))
        db.add(StudioFixtureResult(fixture_id=live_fixture.id, kind="completed", winner="team-1",
                                   detail={"innings": []}, recorded_at=NOW + timedelta(hours=1)))
        db.flush()
        overview = rankings.rebuild(db, NOW + timedelta(hours=2), force=True, dry_run=True)
        assert overview["verified"] == 2
        assert db.scalar(select(func.count(CompetitionRating.user_id))) == 0
        rankings.rebuild(db, NOW + timedelta(hours=2), force=True)
        db.flush()
        assert rankings.page(db, first)["own"]["matches"] == 2
        assert rankings.page(db, first, scope="challenge")["own"]["rating"] == 1516
        assert rankings.page(db, first, scope="live")["own"]["rating"] == 1484
        count = db.scalar(select(func.count(CompetitionRatingEvent.fixture_id)))
        rankings.rebuild(db, NOW + timedelta(hours=2), force=True)
        assert db.scalar(select(func.count(CompetitionRatingEvent.fixture_id))) == count
        fixture.winner_team_id = fixture.team2_id
        db.flush()
        report = rankings.rebuild(db, NOW + timedelta(hours=3), force=True)
        db.flush()
        assert report["corrected_without_evidence"] == 1
        assert rankings.page(db, first)["own"]["matches"] == 1
        assert rankings.page(db, first, scope="challenge")["own"] is None


def test_distinct_opponents_provisional_and_farming_signal():
    result = sources.Result("challenge", 1, 1, 0, "T20", (22, 23), 22, "completed",
                            "league", NOW, (), (1, 2), "verified")
    boards, audit, counts = rankings.replay([result, result], {1, 2})
    assert boards["overall", "all", 0][1]["matches"] == 1
    assert boards["overall", "all", 0][1]["rating"] == 1516
    assert boards["overall", "all", 0][2]["rating"] == 1484
    assert len(audit) == 8
    series = [replace(result, fixture=i, recorded_at=NOW+timedelta(days=i)) for i in range(1, 9)]
    series.append(replace(result, fixture=20, eligibility="forfeited"))
    _, _, counts = rankings.replay(series, {1, 2})
    assert counts["flagged"] == 3 and counts["forfeited"] == 1


def test_analytics_respects_scorecard_arithmetic_and_handover(sessions, enabled):
    with sessions() as db:
        _publish(db, _generated(_config(db)))
        first, second = _users(db)
        tour, fixture, match = _challenge(db, (first, second), patched=True)
        canonical, _ = sources.result_rows(db, "challenge", tour)[0]
        assert canonical.eligibility == "multiple_controllers"
        assert canonical.controllers == (None, None)
        result = analytics.build(db, first, "challenge", tour.id, {})
        assert result["fixtures"][0]["innings"][0]["overs"] == "18.2"
        bat, bowl = result["players"]
        assert bat["fifties"] == 0 and bat["centuries"] == 1
        assert bat["average"] is None and bat["strike_rate"] == 200
        assert bowl["dot_rate"] is None and bowl["economy"] == 3.27
        assert not result["available"]["phases"]
        assert "Centurion" in analytics.export(result)
        assert analytics.build(db, first, "challenge", tour.id, {"participation": "human"})["sample"] == 0
        fixture.is_simulated = True
        db.flush()
        assert analytics.build(db, first, "challenge", tour.id, {"participation": "ai"})["sample"] == 1


def test_private_membership_directory_and_ai_identity(sessions, enabled):
    with sessions() as db:
        config = _config(db, 3)
        config["competition"].update(lobby_v2=True, analytics=True, honour_board=True)
        config["ai_managers"].update(enabled=True, allowlist=["wall"], franchises={"team-1": "wall"})
        config["schedule"]["legs"] = 2
        event = _publish(db, _generated(config))
        first, second = _users(db)
        public_tour, _, _ = _challenge(db, (first, second))
        room = play.create(db, first, event.id, "solo", "team-1", NOW)
        setup_fixture = next(row for row, _ in play._rows(db, room) if "team-1" in (row.home, row.away))
        setup = play.setup(db, first, setup_fixture.id, setup_fixture.opens_at)
        assert setup["ai_manager"]["key"] == "wall"
        pins = db.scalars(select(StudioAIManager).filter_by(competition_id=room.id)).all()
        assert {row.team_key for row in pins} == {"team-0", "team-2"}
        assert "assignment_seed" not in repr(play.dashboard(db, first, event.id, NOW))
        _finish(db, room, event, setup_fixture, play._config(db, room), NOW)
        assert analytics.build(db, first, "live", room.id, {})["available"]["tactics"]
        with pytest.raises(ValueError, match="unavailable"):
            analytics.build(db, second, "live", room.id, {})
        cards = universe.directory(db, second, NOW)["items"]
        assert not any(row["type"] == "room" for row in cards)
        assert any(row["source"] == "challenge" and row["instance"] == public_tour.id for row in cards)


def test_collection_uses_honour_recipient_and_retains_revoked_row(sessions, enabled):
    with sessions() as db:
        _publish(db, _generated(_config(db)))
        first, second = _users(db)
        tour, fixture, _ = _challenge(db, (first, second))
        honour = TournamentHonour(tournament_id=tour.id, tournament_name=tour.name, tournament_team_id=fixture.team1_id,
                                  team_name="North", user_id=first.id, award="champion", awarded_at=NOW)
        db.add(honour)
        db.flush()
        assert collection.sync(db, NOW, dry_run=True)["new"] == 3
        assert db.scalar(select(func.count(CompetitionCollectible.evidence))) == 0
        collection.sync(db, NOW)
        db.flush()
        assert collection.page(db, first)["rows"][0]["kind"] == "champion"
        assert [row["kind"] for row in collection.page(db, second, mode="challenge", fmt="T20", season=NOW.year)["rows"]] == ["participation"]
        assert collection.sync(db, NOW)["retained"] == 3
        db.delete(honour)
        db.flush()
        assert collection.sync(db, NOW + timedelta(minutes=2))["revoked"] == 1
        assert collection.page(db, first)["rows"][0]["revoked"]


def test_collection_keeps_two_edition_trophies_in_one_retired_family(sessions, enabled):
    with sessions() as db:
        _publish(db, _generated(_config(db)))
        first, second = _users(db)
        tours = [_challenge(db, (first, second))[:2] for _ in range(2)]
        db.add(CompetitionCatalogue(source="challenge", family_id=100, label="Summer Series", category="challenge"))
        db.flush()
        for tour, fixture in tours:
            db.add(CompetitionCatalogueLink(source="challenge", edition_id=tour.id, family_id=100))
            db.add(TournamentHonour(tournament_id=tour.id, tournament_name=tour.name,
                                    tournament_team_id=fixture.team1_id, team_name="North", user_id=first.id,
                                    award="champion", awarded_at=NOW))
        db.flush()
        collection.sync(db, NOW)
        db.flush()
        book = collection.page(db, first)
        assert book["sets"][0]["earned"] == 2
        assert book["progress"] == {"titles": 2, "families": 1, "available_families": 1}
        slot = db.get(CompetitionCatalogue, ("challenge", 100))
        slot.retired_at = NOW
        db.flush()
        book = collection.page(db, first)
        assert book["sets"][0]["retired"]
        assert len([row for row in book["rows"] if row["kind"] == "champion" and not row["revoked"]]) == 2


def test_collection_preserves_official_honour_after_archival(sessions, enabled):
    with sessions() as db:
        db.add_all([User(telegram_id=1000), User(telegram_id=1001)])
        db.flush()
        first, second = _users(db)
        tour, fixture, _ = _challenge(db, (first, second))
        honour = TournamentHonour(tournament_id=tour.id, tournament_name=tour.name, award="champion",
                                  tournament_team_id=fixture.team1_id, team_name="North", user_id=first.id,
                                  awarded_at=NOW)
        db.add(honour)
        db.flush()
        collection.sync(db, NOW)
        db.flush()
        db.add(StatsSeason(source_tournament_id=tour.id, name=tour.name, kind="challenge"))
        rankings.archive_challenge(db, tour)
        honour.tournament_id = None
        db.delete(tour)
        db.flush()
        assert collection.sync(db, NOW)["revoked"] == 0
        assert rankings.rebuild(db, NOW, dry_run=True)["verified"] == 1
        book = collection.page(db, first)
        assert all(row["archived"] and not row["revoked"] for row in book["rows"])


def test_approved_rotation_recovers_without_duplicate_drafts(sessions, enabled, monkeypatch):
    with sessions() as db:
        event = _publish(db, _generated(_config(db)))
        event_id = event.id
        opening = (NOW + timedelta(days=35)).isoformat()
        _, _, editions = operations.preview(db, event_id, opening, 1, 2, NOW)
        assert len(editions) == 2
        assert not db.scalar(select(CompetitionRotation.id))
        operations.queue(db, event_id, opening, 1, 2, "moderator", "Calendar approved", NOW)
        db.commit()
    monkeypatch.setattr(operations, "get_session", sessions)
    operations.sweep(NOW)
    operations.sweep(NOW)
    with sessions() as db:
        jobs = db.scalars(select(CompetitionRotation)).all()
        assert len(jobs) == 2 and all(job.status == "prepared" for job in jobs)
        assert set(job.draft_id for job in jobs) == {event_id + 1, event_id + 2}
        operations.queue(db, event_id, opening, 1, 2, "moderator", "Retry same window", NOW)
        db.commit()
        assert db.scalar(select(func.count(CompetitionRotation.id))) == 2


@pytest.mark.parametrize("format_name", ["T20", "ODI"])
def test_profiles_stay_normalized_and_reproducible(format_name):
    profiles = []
    for key in ai.PROFILES:
        state = {"ball_format": format_name, "bot_user_id": "a",
                 "event_ai_managers": {"a": [key, ai.VERSION]}, "event_ai_blend": .2}
        before = dict.fromkeys(ai.BAT, .2)
        profiles.append(ai.mix(state, before, batting=True, phase="middle"))
        assert sum(profiles[-1].values()) == pytest.approx(1)
        assert profiles[-1] == ai.mix(json.loads(json.dumps(state)), before, batting=True, phase="middle")
        assert sum(abs(profiles[-1][option] - before[option]) for option in ai.BAT) <= .4
    assert len({tuple(profile.values()) for profile in profiles}) == 10


def test_challenge_finalizer_captures_actual_controller_not_owner(sessions, enabled):
    from models_universe import CompetitionControllerEvidence
    from services import tournament_service

    with sessions() as db:
        _publish(db, _generated(_config(db)))
        first, second = _users(db)
        tour = Tournament(name="Control Cup", kind="challenge", status="active", overs=20)
        db.add(tour)
        db.flush()
        a = TournamentTeam(tournament_id=tour.id, name="North", owner_tg_id=second.telegram_id)
        b = TournamentTeam(tournament_id=tour.id, name="South", owner_tg_id=first.telegram_id)
        match = Match(user1_id=first.id, user2_id=second.id, status="completed", end_reason="completed",
                      tournament_id=tour.id, overs=20, ball_format="T20")
        db.add_all([a, b, match])
        db.flush()
        fixture = tournament_service.record_tournament_match(db, {
            "mode": "cipl_approach", "tournament_id": tour.id, "match_id": match.id,
            "tournament_tteam_by_user": {first.id: a.id, second.id: b.id},
            "inn1_bat_team_id": first.id, "bat_team_id": second.id, "inn1_bowl_team_id": second.id,
            "inn1_runs": 130, "total_runs": 126, "inn1_wickets": 6, "total_wickets": 8,
            "inn1_bowl_stats": {"34": {"balls": 120}}, "bowl_stats": {"12": {"balls": 120}},
        })
        db.flush()
        evidence = db.get(CompetitionControllerEvidence, fixture.id)
        assert evidence.controller_a == first.id and evidence.controller_b == second.id
        assert rankings.rebuild(db, NOW, dry_run=True)["verified"] == 1


@pytest.mark.parametrize("width", [390, 1280])
def test_miniapp_universe_card_analytics_filter_and_collection(width):
    from pathlib import Path
    import shutil

    playwright = pytest.importorskip("playwright.sync_api")
    chromium = shutil.which("chromium")
    if not chromium:
        pytest.skip("Chromium unavailable")
    root = Path(__file__).resolve().parents[1]
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium, headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            page = browser.new_page(viewport={"width": width, "height": 850})
            page.set_content('<main class="events-hub"><h1 id="universe-title"></h1>'
                             '<div id="universe-status" role="status"></div><div id="universe-content"></div></main>')
            page.add_style_tag(path=root / "static/css/events_hub.css")
            page.add_script_tag(content="""
              function navigate() {}
              async function api(path, body) {
                window.lastRequest = body;
                if (body.action === "analytics") return {ok:true,data:{ok:true,team_options:[{key:"north",name:"North"}],
                  format_options:["T20"],teams:[{team:"North",format:"T20",played:1,wins:1}],
                  standings:[],players:[],phases:[{team:"North",format:"T20",phase:"powerplay",balls:36,rpo:6}],
                  tactics:[],fixtures:[],notice:"Recorded overs only"}};
                if (body.action === "collection") return {ok:true,data:{ok:true,available:true,rows:[],
                  progress:{titles:1,families:1,available_families:2},
                  sets:[{label:"City League",category:"domestic",retired:false,earned:1}]}};
                return {ok:true,data:{ok:true,next:[],capabilities:{collection:true},items:[{
                  source:"live",instance:4,event_id:2,type:"room",name:"City Cup",mode:"solo",
                  format:"T20",status:"running",access:"participants",teams:["North"],action:"Open event",
                  played:1,total:2,capabilities:{analytics:true}}]}};
              }
            """)
            page.add_script_tag(path=root / "static/js/competition_universe.js")
            page.evaluate("CompetitionUniverse.open()")
            assert page.get_by_text("City Cup", exact=True).is_visible()
            page.get_by_role("button", name="Analytics", exact=True).click()
            page.get_by_label("Team", exact=True).select_option("north")
            page.wait_for_function("window.lastRequest.team === 'north'")
            assert page.get_by_text("Recorded overs only").is_visible()
            assert page.get_by_text("North · Powerplay · 6 runs per over").is_visible()
            assert page.get_by_role("cell", name="36", exact=True).is_visible()
            page.get_by_role("button", name="Competitions", exact=True).click()
            page.get_by_role("button", name="Collection Book", exact=True).click()
            assert page.get_by_text("City League", exact=True).is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            browser.close()


def test_projection_refresh_is_idempotent_across_concurrent_workers(sessions, enabled):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with sessions() as db:
        db.add_all([User(telegram_id=1000), User(telegram_id=1001)])
        db.flush()
        tour, fixture, _ = _challenge(db, _users(db))
        fixture.inn1_balls = fixture.inn2_balls = 6
        sources.capture_challenge(db, tour, fixture, {"mode": "cipl_approach"})
        db.commit()
    barrier = Barrier(2)

    def refresh():
        with sessions() as db:
            barrier.wait(timeout=10)
            report = rankings.rebuild(db, NOW, force=True)
            db.commit()
            return report

    with ThreadPoolExecutor(max_workers=2) as workers:
        reports = list(workers.map(lambda _: refresh(), range(2)))
    assert [report["verified"] for report in reports] == [1, 1]
    with sessions() as db:
        assert db.scalar(select(func.count(CompetitionRatingEvent.fixture_id))) == 8
        assert db.scalar(select(func.count(CompetitionRating.user_id))) == 16
        flags = operations.monitor(db, NOW)["ranking_flags"]
        assert len(flags) == 1 and flags[0]["signals"] == ["short_match"]


def test_signed_routes_and_telegram_render_after_process_restart(tmp_path):
    import os
    import subprocess
    import sys

    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'universe.db'}",
           "CCM26_EVENT_TEST_POSTGRES": "",
           "BOT_TOKEN": "local-universe-token",
           **{key: "1" for key in ("COMPETITION_UNIVERSE", "COMPETITION_UNIVERSE_ANALYTICS",
                                   "COMPETITION_UNIVERSE_COLLECTION", "COMPETITION_UNIVERSE_RANKINGS",
                                   "EVENT_STUDIO_PUBLISH", "EVENT_STUDIO_LIVE", "EVENT_STUDIO_T20")}}
    subprocess.run([sys.executable, "-c", "from tests.test_live_events_phase2 import _signed_seed; _signed_seed()"],
                   env=env, check=True)
    subprocess.run([sys.executable, "-c",
                    "from tests.test_competition_universe import _verify_signed_routes; _verify_signed_routes()"],
                   env=env, check=True)


def _verify_signed_routes():
    import hmac
    import os
    import re
    import time
    from urllib.parse import urlencode

    import admin
    from database import init_db
    from handlers.competition_universe import render
    init_db()

    def signed(telegram_id):
        values = {"auth_date": str(int(time.time())), "user": json.dumps({"id": telegram_id})}
        key = hmac.digest(b"WebAppData", os.environ["BOT_TOKEN"].encode(), "sha256")
        content = "\n".join(f"{name}={value}" for name, value in sorted(values.items()))
        values["hash"] = hmac.digest(key, content.encode(), "sha256").hex()
        return {"Authorization": "tma " + urlencode(values)}

    client = admin.app.test_client()
    route = "/api/webapp/competition-universe"
    assert client.post(route, json={}).status_code == 401
    first = signed(1000)
    directory = client.post(route, headers=first, json={})
    assert directory.status_code == 200
    assert any(row["type"] == "room" for row in directory.json["items"])
    request = {"action": "analytics", "source": "live", "instance": 1}
    view = client.post(route, headers=first, json=request)
    assert view.status_code == 200
    assert view.json["sample"] > 0
    csv = client.post(route, headers=first, json={**request, "action": "csv"})
    assert csv.status_code == 200 and csv.json["csv"].startswith("name,team,format,edition,")
    assert client.post(route, headers=signed(1001), json=request).status_code == 400
    assert client.post(route, headers=first, json={**request, "source": None}).status_code == 400
    assert client.get("/competitions/operations").status_code == 302
    with client.session_transaction() as session:
        session["admin"] = True
    operations = client.get("/competitions/operations")
    assert operations.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', operations.text)[1]
    preview = client.post("/competitions/operations", data={"action": "dry_run", "csrf_token": csrf})
    assert preview.status_code == 200 and "historic_unverified" in preview.text
    rejected = client.post("/competitions/operations", data={"action": "dry_run"})
    assert rejected.status_code == 302 and rejected.location.endswith("/login")
    assert "Persistent Cup" in render(1000, "directory")[0]
    assert "recorded fixtures" in render(1000, "live", instance=1)[0]
    with pytest.raises(ValueError, match="private"):
        render(1000, "collection", private=False)
