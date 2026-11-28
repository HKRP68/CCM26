"""Events Studio generator, engine and service regressions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from database import Base
from models import ActivityLog, Player, User, UserRoster
from models_events import StudioEnrollment, StudioEntitlement, StudioEvent, StudioGame, StudioResult
from services import cipl_picks as picks, event_studio as studio, event_studio_cricket as cricket
from services.match_state_store import A_INNINGS_BREAK
from services import event_studio_play as play, event_studio_rules as rules
from services import event_studio_schedule as schedule

NOW = datetime(2026, 12, 1, 6)
CLOCK = lambda: NOW


def _play_over(db, user, game_id, version, approach, bowler, clock=None):
    """Drive one over with defaults, using the production pick actions."""
    game, entry, _ = play._ownership(db, user.id, game_id)
    if game.status != "in_progress":
        return game
    now = play._time(clock)
    source = game.state.get("play_mode", "chat")
    state = play._checkpoint(game, entry, user)
    kind = picks.PICK_BAT_APPROACH if state["bat_team_id"] == entry.team_key else picks.PICK_BOWL_APPROACH
    options = cricket.BATTING_APPROACHES if kind == picks.PICK_BAT_APPROACH else cricket.BOWLING_APPROACHES
    value = next((index for index, item in enumerate(options) if item[0] == approach), None)
    if value is None:
        raise ValueError("Select a legal approach for this over.")
    if state.get("event_next_action") == A_INNINGS_BREAK and now <= game.due_at:
        play.submit_pick(db, user, game_id, version, "continue", 0, source=source, clock=clock)
        version = game.version
        state = game.state
        kind = picks.PICK_BAT_APPROACH if state["bat_team_id"] == entry.team_key else picks.PICK_BOWL_APPROACH
        options = cricket.BATTING_APPROACHES if kind == picks.PICK_BAT_APPROACH else cricket.BOWLING_APPROACHES
        value = next((idx for idx, item in enumerate(options) if item[0] == approach), None)
    if kind == picks.PICK_BOWL_APPROACH and now <= game.due_at:
        if state.get("event_next_action") == picks.PICK_ACTION[picks.PICK_BOWLER]:
            play.submit_pick(db, user, game_id, version, picks.PICK_BOWLER, bowler, source=source, clock=clock)
            version = game.version
    game = play.submit_pick(db, user, game_id, version, kind, value, source=source, clock=clock)
    while game.status == "in_progress" and game.state.get("event_next_action") == picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]:
        player = cricket.match.available_batsmen(game.state)[0]
        game = play.submit_pick(db, user, game_id, game.version, picks.PICK_NEW_BATSMAN,
                           player["roster_id"], source=source, clock=clock)
    return game


def _schedule_config(count, kind="live"):
    config = studio.defaults()
    config.update(kind=kind, opens_at="2026-12-01T09:00", closes_at="2026-12-24T07:00",
                  teams=[{"key": f"team-{i}", "name": f"Team {i}", "players": []} for i in range(count)])
    config["schedule"]["playoffs"] = "none"
    return config


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    postgres = os.getenv("CCM26_EVENT_TEST_POSTGRES")
    schema = f"test_studio_{uuid4().hex}"
    root = create_engine(postgres) if postgres else None
    if root:
        from sqlalchemy.schema import CreateSchema
        with root.begin() as conn:
            conn.execute(CreateSchema(schema))
        engine = create_engine(postgres, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'events.db'}")
    Base.metadata.create_all(engine)
    for flag in ("PUBLISH", "T20", "ODI", "LIVE", "MOMENTS", "BOSSES", "HARD_PLUS"):
        monkeypatch.setenv("EVENT_STUDIO_" + flag, "1")
    factory = sessionmaker(bind=engine, autoflush=False)
    yield factory
    engine.dispose()
    if root:
        from sqlalchemy.schema import DropSchema
        with root.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        root.dispose()


def _seed(db, count=2, kind="live"):
    config = _schedule_config(count, kind)
    config["budget"] = {"coins": 100_000, "gems": 10_000, "quest_points": 10_000}
    config["max_users"] = 4
    config["schedule"]["legs"] = 1
    config["mode"] = ["tournament_team"] if kind != "live" else list(studio.TRACKS)
    for i in range(2):
        db.add(User(telegram_id=1000 + i, total_coins=0, total_gems=0, quest_points=0))
    for team in config["teams"]:
        for index in range(11):
            role = "Wicketkeeper" if index == 0 else "Batsman" if index < 6 else "Bowler"
            card = Player(name=f'{team["name"]} Player {index + 1}', category=role, country="India",
                          bowl_style="Fast", bat_hand="Right", bowl_hand="Right",
                          rating=75, bat_rating=76 if index < 6 else 45, bowl_rating=80 if index >= 6 else 35)
            db.add(card)
            db.flush()
            team["players"].append(card.id)
    return studio.generate(studio.normalize(config), "all")


def _publish(db, config):
    event = studio.create(db, "Summer Cup", "summer-cup", "operator", config)
    studio.publish(db, event, 1, "operator", NOW)
    db.commit()
    return event


def _join(db, event, user, team="team-0", mode="tournament_team"):
    enrollment = play.join(db, user, event.id, team, mode, NOW)
    db.commit()
    return enrollment


@pytest.mark.parametrize("count", [2, 3, 7, 8, 14])
@pytest.mark.parametrize("legs", [1, 2, 3])
def test_round_robin_counts_and_home_away(count, legs):
    keys = [str(n) for n in range(count)]
    rounds = schedule.round_robin(keys, legs)
    totals = {}
    for designs in rounds:
        participants = [team for game in designs for team in game]
        assert len(set(participants)) == len(participants)
        for home, away in designs:
            totals[home, away] = totals.get((home, away), 0) + 1
    assert sum(totals.values()) == count * (count - 1) * legs // 2
    for home, away in totals:
        assert totals[home, away] + totals.get((away, home), 0) == legs
        assert abs(totals[home, away] - totals.get((away, home), 0)) <= 1


def test_double_round_robin_bracket_and_locked_manual_schedule():
    config = _schedule_config(8)
    config["schedule"]["playoffs"] = "top4"
    first = studio.generate(config, "schedule")
    assert len(first["fixtures"]) == 60
    assert first["fixtures"][-1]["away"] == "winner:qualifier-2"
    assert studio.generate(first, "schedule") == first
    first["fixtures"][0].update(grace=10, locks=["grace"])
    assert studio.generate(first, "schedule")["fixtures"][0]["grace"] == 10
    first["closes_at"] = "2026-12-03T10:00"
    assert studio.stale(first) == ["schedule"]
    with pytest.raises(ValueError, match="fit"):
        studio.generate(first, "schedule")


def test_knockout_byes_and_dst():
    config = _schedule_config(7)
    config["schedule"].update(preset="knockout", third_place=True)
    assert len(studio.generate(config, "schedule")["fixtures"]) == 7
    with pytest.raises(ValueError, match="explicit UTC offset"):
        schedule.utc_instant("2026-11-01T01:30", "America/New_York")


def test_auto_rewards_caps_and_preserves_locked_fields():
    config = _schedule_config(8)
    config["budget"] = {"coins": 400_000, "gems": 100_000, "quest_points": 200_000}
    first = studio.generate(config, "all")
    assert all(value <= first["budget"][asset] for asset, value in rules.liability(first).items())
    first["rewards"]["diamond"]["win"]["coins"] = 7
    first["reward_locks"] = ["diamond.win.coins"]
    with pytest.raises(ValueError, match="Diamond"):
        studio.generate(first, "rewards")


def test_personal_games_are_independent_and_unstarted_fixtures_forfeit(sessions):
    with sessions() as db:
        event = _publish(db, _seed(db, 3))
        a, b = db.scalars(select(User).order_by(User.id)).all()
        ea = _join(db, event, a)
        eb = _join(db, event, b)
        games = lambda entry: db.scalars(select(StudioGame).filter_by(enrollment_id=entry.id).order_by(StudioGame.id)).all()
        one = next(game for game in games(ea) if ea.team_key in (game.home, game.away))
        other = next(game for game in games(eb) if eb.team_key in (game.home, game.away))
        with pytest.raises(ValueError, match="unavailable"):
            play.start_fixture(db, b, one.id, None, lambda: one.opens_at)
        play.run_due(db, one.id, one.deadline + timedelta(seconds=1))
        db.commit()
        assert db.get(StudioResult, one.id).detail == {"reason": "deadline"}
        assert other.status == "scheduled"
        assert not play.detail(db, a, event.id, NOW)["rewards"]
        table = play.detail(db, a, event.id, NOW)["table"]
        assert next(row for row in table if row["team"] == ea.team_key)["points"] == 0
        assert all(row["faced"] == 0 for row in table)


def test_concurrent_start_and_replay_preserve_one_match(sessions):
    with sessions() as db:
        event = _publish(db, _seed(db))
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game_id = db.scalar(select(StudioGame.id).filter_by(enrollment_id=entry.id))
        user_id = user.id

    def start(_):
        with sessions() as db:
            game = play.start_fixture(db, db.get(User, user_id), game_id, None, CLOCK)
            result = game.id, game.seed, game.version
            db.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(start, range(2))
    assert first == second
    with sessions() as db:
        state = db.get(StudioGame, game_id).state
        user = db.get(User, user_id)
        choice = cricket.public_choices(state, "team-0")
        bowler = choice["bowlers"][0]["id"] if choice["bowlers"] else None
        _play_over(db, user, game_id, first[2], "balanced", bowler, CLOCK)
        db.commit()
    with sessions() as db:
        game = db.get(StudioGame, game_id)
        assert game.step == 1
        with pytest.raises(studio.Conflict):
            _play_over(db, db.get(User, user_id), game_id, first[2], "balanced", bowler, CLOCK)
        assert play.scorecard(db, db.get(User, user_id), game_id)["innings"][0]["overs"] == "1.0"


def test_own_roster_locks_identity_and_uses_published_opponent(sessions, monkeypatch):
    from models import PlayerTrait, Trait
    from services import match_broadcast

    monkeypatch.setattr(match_broadcast, "flip_coin", lambda call: (call, True))
    with sessions() as db:
        config = _seed(db)
        user = db.scalar(select(User).order_by(User.id))
        roster = [UserRoster(user_id=user.id, player_id=card) for card in config["teams"][0]["players"]]
        trait = Trait(name="Front Foot", category="Batting", description="Fixture test trait", effect_key="bat_anchor")
        db.add_all([*roster, trait])
        db.flush()
        db.add(PlayerTrait(user_id=user.id, roster_id=roster[0].id, trait_id=trait.id, level=2))
        event = _publish(db, config)
        entry = _join(db, event, user, mode="own_roster")
        player_id = config["teams"][1]["players"][0]
        db.get(Player, player_id).rating = 99
        db.commit()
        game = db.scalar(select(StudioGame).filter_by(enrollment_id=entry.id))
        with pytest.raises(ValueError, match="outside"):
            play.start_fixture(db, user, game.id, [-101] * 11, CLOCK)
        setup = play.setup_fixture(db, user, event.id, game.id, 0, NOW, call="heads")
        game = play.start_fixture(db, user, game.id, setup["default_xi"], CLOCK,
                                  decision="bat", require_toss=True)
        assert next(row["rating"] for row in game.state["bowl_xi"] if row["player_id"] == player_id) == 75
        assert game.state["event_ai_policy"] == "hard-plus-v1"
        own = next(row for row in game.state["bat_xi"] if row["roster_id"] == roster[0].id)
        assert own["traits"][0]["level"] == 2
        assert game.state["event_context"]["track"] == "own_roster"


def test_finish_grace_boundary_and_claim_double_tap(sessions, monkeypatch):
    with sessions() as db:
        config = _seed(db)
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game_id = db.scalar(select(StudioGame.id).filter_by(enrollment_id=entry.id))
        game = play.start_fixture(db, user, game_id, None, CLOCK)
        cutoff = game.due_at
        choices = cricket.public_choices(game.state, entry.team_key)
        bowler = choices["bowlers"][0]["id"] if choices["bowlers"] else None
        version, userid, eventid = game.version, user.id, event.id
        db.commit()
    monkeypatch.setattr(cricket, "resolve_over", lambda *args, **kwargs: "completed")
    monkeypatch.setattr(cricket, "match_result", lambda state: ("team-0", {"innings": []}))
    monkeypatch.setattr(cricket, "objectives", lambda *args: {"stars": 1})
    monkeypatch.setattr(play.progress, "contribution", lambda *args: [])
    with sessions() as db:
        game = _play_over(db, db.get(User, userid), game_id, version, "balanced", bowler, lambda: cutoff)
        assert game.status == "completed"
        db.commit()

    def claim(_):
        with sessions() as db:
            rows = play.claim(db, db.get(User, userid), eventid, None, cutoff + timedelta(seconds=1))
            receipt = rows[0].ledger_id
            db.commit()
            return receipt

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.map(claim, range(2))
    assert a == b
    with sessions() as db:
        assert db.scalar(select(func.count(ActivityLog.id))) == 1
        assert db.scalar(select(func.count(StudioEntitlement.id))) == 1
        assert db.get(User, userid).total_coins == config["rewards"]["free"]["win"]["coins"]
        assert play.progression(db, db.get(User, userid), eventid)["leaderboard"] == [
            {"rank": 1, "name": "You", "wins": 1, "stars": 1, "played": 1}]



def test_unsteered_engine_replays_all_overs_with_identical_input(sessions, monkeypatch):
    from services import cipl_match, match_drama

    def disallowed(*args, **kwargs):
        pytest.fail("Official play reached an outcome steering layer")

    for name in ("_load_scenario_engine", "_maybe_enable_scenario", "chase_model_active",
                 "_make_floor_hook", "_make_no_collapse_hook", "_make_chase_hook"):
        monkeypatch.setattr(cipl_match, name, disallowed)
    monkeypatch.setattr(match_drama, "make_contest_hook", disallowed)
    with sessions() as db:
        event = _publish(db, _seed(db))
        config = studio.revision(db, event, published=True).config
    seed = "ab" * 32
    x = {key: team["default_xi"] for key, team in config["snapshot"].items()}
    state = cricket.build_state(config, 1, "team-0", "team-1", x, seed, human="team-0")
    recovered = deepcopy(state)
    from services import cipl_picks as picks
    from services.match_state_store import A_INNINGS_BREAK
    cricket.prepare_pick(state, seed, 0, "team-0")
    recovered = deepcopy(state)
    step = 0
    for _ in range(130):
        stage = state["event_next_action"]
        if stage == A_INNINGS_BREAK:
            for checkpoint in (state, recovered):
                cricket.prepare_pick(checkpoint, seed, step, "team-0")
            continue
        kind = next(name for name, action in picks.PICK_ACTION.items() if action == stage)
        value = 2
        if kind == picks.PICK_BOWLER:
            value = cipl_match.eligible_bowlers(state)[0]["roster_id"]
        elif kind == picks.PICK_NEW_BATSMAN:
            value = cipl_match.available_batsmen(state)[-1]["roster_id"]
        end = cricket.submit(state, seed, step, "team-0", kind, value)
        recovered = json.loads(json.dumps(recovered))
        assert end == cricket.submit(recovered, seed, step, "team-0", kind, value)
        assert cricket.view(state, "team-0") == cricket.view(recovered, "team-0")
        if end:
            break
        if kind != picks.PICK_BOWLER and state["event_next_action"] != picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]:
            step += 1
    assert end == "completed"
    winner, score = cricket.match_result(recovered)
    assert all(0 < innings["balls"] <= 120 for innings in score["innings"])


def test_moment_prefix_is_real_and_attempt_counters_are_idempotent(sessions):
    with sessions() as db:
        config = _seed(db, kind="moment")
        config["moment"].update(start_over=2, overs=1, goal=1, seed=2026)
        config = studio.generate(config, "all")
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        _join(db, event, user)
        game = play.start_attempt(db, user, event.id, 0, CLOCK)
        assert game.state["event_moment"]["start_ball"] == 12
        assert play.start_attempt(db, user, event.id, 0, CLOCK).id == game.id
        _play_over(db, user, game.id, game.version, "ultra", None, CLOCK)
        db.commit()
        assert game.status in ("success", "failed")
        assert game.step == 1
        assert play.start_attempt(db, user, event.id, 0, CLOCK).id == game.id
        assert play.start_attempt(db, user, event.id, 1, CLOCK).attempt == 2


def test_publish_is_disabled_and_revisions_are_optimistic(sessions, monkeypatch):
    with sessions() as db:
        config = _seed(db)
        event = studio.create(db, "Disabled cup", "disabled", "operator", config)
        studio.save(db, event, 1, config, "operator")
        with pytest.raises(studio.Conflict):
            studio.save(db, event, 1, config, "operator")
        monkeypatch.delenv("EVENT_STUDIO_PUBLISH")
        with pytest.raises(ValueError, match="disabled"):
            studio.publish(db, event, 2, "operator", NOW)
        assert event.published_number is None


def test_pause_shifts_only_unresolved_games_and_keeps_the_seed(sessions):
    with sessions() as db:
        event = _publish(db, _seed(db))
        u1, u2 = db.scalars(select(User).order_by(User.id)).all()
        e1 = _join(db, event, u1)
        e2 = _join(db, event, u2)
        g1 = db.scalar(select(StudioGame).where(StudioGame.enrollment_id == e1.id))
        g2 = db.scalar(select(StudioGame).where(StudioGame.enrollment_id == e2.id))
        play.start_fixture(db, u1, g1.id, None, CLOCK)
        play.start_fixture(db, u2, g2.id, None, CLOCK)
        play.abandon(db, u2, g2.id, g2.version, CLOCK)
        db.commit()
        first_due, second_due, first_seed = g1.due_at, g2.due_at, g1.seed
        event = studio.lock(db, StudioEvent, event.id)
        play.operate(db, event, "pause", "operator", "Maintenance", NOW)
        db.commit()
        assert play.detail(db, u1, event.id, NOW)["games"][0]["status"] == "paused"
        with pytest.raises(ValueError, match="paused"):
            _play_over(db, u1, g1.id, g1.version, "balanced", None, CLOCK)
        db.rollback()
        event = studio.lock(db, StudioEvent, event.id)
        play.operate(db, event, "resume", "operator", "Ready", NOW + timedelta(hours=1))
        db.commit()
        assert g1.due_at == first_due + timedelta(hours=1)
        assert g1.seed == first_seed
        assert g2.due_at == second_due
        assert g2.status == "forfeited"


def test_group_schedule_is_stable_and_uses_group_ranks():
    config = _schedule_config(8)
    config["schedule"].update(preset="groups", legs=1,
                              groups={"b": [f"team-{i}" for i in range(4, 8)],
                                      "a": [f"team-{i}" for i in range(4)]})
    first = studio.generate(config, "schedule")
    assert len(first["fixtures"]) == 15
    playoffs = [row for row in first["fixtures"] if row["stage"] == "playoff"]
    assert {row["home"] for row in playoffs[:2]} == {"group_rank:a:1", "group_rank:b:1"}
    reordered = deepcopy(config)
    reordered["schedule"]["groups"] = dict(reversed(list(config["schedule"]["groups"].items())))
    assert studio.generate(reordered, "schedule")["fixtures"] == first["fixtures"]


def test_survival_forfeit_cancels_remaining_rounds_without_a_table(sessions):
    with sessions() as db:
        config = _seed(db, 4)
        config["schedule"]["preset"] = "survival"
        config = studio.generate(config, "all")
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        games = db.scalars(select(StudioGame).filter_by(enrollment_id=entry.id).order_by(StudioGame.opens_at)).all()
        play.run_due(db, games[0].id, games[0].deadline + timedelta(seconds=1))
        db.commit()
        assert [game.status for game in games] == ["forfeited", "cancelled", "cancelled"]
        assert play.detail(db, user, event.id, NOW)["table"] == []
        assert db.scalar(select(func.count(StudioResult.game_id))) == 3


def test_boss_supports_a_verified_chase_prefix(sessions):
    with sessions() as db:
        config = _seed(db, kind="boss")
        config["challenge_style"] = "moment"
        config["moment"].update(goal_type="chase", start_over=2, overs=18, seed=101)
        config = studio.generate(config, "all")
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        _join(db, event, user)
        game = play.start_attempt(db, user, event.id, 0, CLOCK)
        assert game.state["innings"] == 2
        shot = game.state["event_moment"]
        assert shot["target"] == game.state["inn1_runs"] + 1
        assert shot["goal"] == shot["target"] - shot["start_runs"]
        assert game.state["bat_team_id"] == "team-0"
        assert play.detail(db, user, event.id, NOW)["moment"]["goal_type"] == "chase"


def test_locked_optional_goals_and_reward_edited_values_survive_regeneration():
    config = _schedule_config(8)
    config["budget"] = {"coins": 30_000_000, "gems": 0, "quest_points": 0}
    config = studio.generate(config, "all")
    config["objectives"][1]["threshold"] = 7
    config["rewards"]["diamond"]["win"]["coins"] = 1000
    config["rewards"]["free"]["win"]["coins"] = 800
    config = studio.generate(config, "all")
    assert config["objectives"][1]["threshold"] == 7
    assert config["rewards"]["diamond"]["win"]["coins"] == 1000
    rule = {"key": "boundaries", "threshold": 4, "min_balls": 12}
    assert rules.evaluate([rule], {"boundaries": 1}, {"boundaries": 6})[0]["status"] == "not_applicable"


def test_star_milestone_is_budgeted_and_pays_once(sessions, monkeypatch):
    with sessions() as db:
        config = _seed(db, kind="moment")
        config["moment"].update(start_over=0, overs=2, goal=5)
        config["milestones"] = [{"stars": 1, "free": {"gems": 1}, "diamond": {"gems": 3}}]
        config = studio.generate(config, "all")
        assert rules.liability(config)["gems"] == 4 * (config["rewards"]["diamond"]["success"]["gems"] + 3)
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        _join(db, event, user)
        monkeypatch.setattr(cricket, "resolve_over", lambda *args, **kwargs: "success")
        for attempt in (0, 1):
            game = play.start_attempt(db, user, event.id, attempt, CLOCK)
            _play_over(db, user, game.id, game.version, "balanced", None, CLOCK)
            db.commit()
        got = play.claim(db, user, event.id, None, NOW)
        db.commit()
        assert {row.source for row in got} == {"challenge", "milestone:1"}
        assert user.total_gems == config["rewards"]["free"]["success"]["gems"] + 1


def test_squad_import_rejects_ambiguity_and_resolves_known_card(sessions):
    from services.event_studio_import import squad_import, teams_import
    with sessions() as db:
        config = _seed(db)
        teams = teams_import('{"teams":[{"team_name":"Team 0","primary_color":"#1d4ed8"}]}', config["teams"])
        assert teams[0]["primary_color"] == "#1d4ed8"
        card = db.get(Player, teams[0]["players"][0])
        dupe = Player(name=card.name, version="Gold", rating=80, category=card.category, country=card.country,
                      bat_hand=card.bat_hand, bowl_hand=card.bowl_hand, bowl_style=card.bowl_style)
        db.add(dupe)
        db.flush()
        with pytest.raises(ValueError, match="Row 2: ambiguous"):
            squad_import(db, f"TEAM NAME,PLAYER NAME,VERSION\nTeam 0,{card.name},\n", teams)
        imported = squad_import(db, f"TEAM NAME,PLAYER NAME,VERSION\nTeam 0,{card.name},Gold\n", teams)
        assert imported[0]["players"] == [dupe.id]


def test_signed_api_and_admin_csrf_share_real_routes(tmp_path):
    # The Flask entry point caches model classes at import. Run its HTTP
    # smoke in a fresh process so the repository's module-swap suites can
    # import their own isolated SQLAlchemy models regardless of test order.
    import subprocess
    import sys
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'event-http.db'}",
           "BOT_TOKEN": "event-auth-test", "EVENT_STUDIO_PUBLISH": "1",
           "EVENT_STUDIO_T20": "1", "EVENT_STUDIO_LIVE": "1", "EVENT_STUDIO_HARD_PLUS": "1"}
    subprocess.run([sys.executable, "-c",
                    "from tests.test_event_studio import _check_signed_api; _check_signed_api()"],
                   check=True, env=env)


def _check_signed_api():
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    import admin
    from database import SessionLocal, engine

    Base.metadata.create_all(engine)
    sessions = SessionLocal
    token = os.environ["BOT_TOKEN"]
    admin.app.testing = True

    def signed(telegram_id):
        data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": telegram_id})}
        check = "\n".join(f"{key}={data[key]}" for key in sorted(data))
        key = hmac.digest(b"WebAppData", token.encode(), "sha256")
        data["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        return {"Authorization": "tma " + urlencode(data)}

    with sessions() as db:
        config = _seed(db)
        now = datetime.utcnow()
        config.update(timezone="UTC", opens_at=(now - timedelta(hours=1)).isoformat(),
                      closes_at=(now + timedelta(days=3)).isoformat())
        config = studio.generate(config, "all")
        event = studio.create(db, "Local signed cup", "signed", "operator", config)
        studio.publish(db, event, 1, "operator", now)
        db.commit()
        event_id = event.id

    client = admin.app.test_client()
    base = "/api/webapp/events-hub"
    assert client.post(base, json={}).status_code == 401
    assert client.post(base, json={}, headers={"Authorization": "bad"}).status_code == 401
    assert client.post(base, json={}, headers=signed(1000)).json["events"][0]["id"] == event_id
    joined = client.post(base + "/action", headers=signed(1000), json={
        "action": "join", "event_id": event_id, "team": "team-0", "mode": "tournament_team",
        "tier": "diamond",
    })
    assert joined.status_code == 200
    detail = client.post(base + f"/{event_id}", headers=signed(1000), json={}).json
    game_id = detail["event"]["games"][0]["id"]
    assert client.post(base + "/action", headers=signed(1001), json={
        "action": "start", "game_id": game_id,
    }).status_code == 400
    setup = {"action": "setup", "event_id": event_id, "game_id": game_id, "previous_attempt": 0}
    assert client.post(base + "/action", headers=signed(1000), json=setup).json["setup"]["toss"] is None
    with sessions() as db:
        assert db.get(StudioGame, game_id).status == "scheduled"
    first = client.post(base + "/action", headers=signed(1000), json={**setup, "call": "heads"}).json["setup"]
    assert client.post(base + "/action", headers=signed(1000), json={**setup, "call": "tails"}).json["setup"]["toss"] == first["toss"]
    assert client.post(base + "/action", headers=signed(1000), json={
        "action": "start", "game_id": game_id, "decision": "bat", "tier": "diamond", "result": "win",
    }).status_code == 200
    arena = base + f"/arena/{game_id}"
    assert client.post(arena, headers=signed(1001), json={}).status_code == 400
    match = client.post(arena, headers=signed(1000), json={}).json
    assert match["id"] == f"event-{game_id}" and match["approachMode"] and match["isMyTurn"], match
    assert match["officialEvent"]["play_mode"] == "app"
    assert "bot_memory" not in match and "bowling_approach" not in match
    revision = match["officialEvent"]["version"]
    kind, action = ("cipl_bat_approach", {"key": "balanced"})
    if match["turnState"] == "selecting_over_bowler":
        kind, action = ("cipl_bowler", {"rosterId": match["approach"]["overBowlers"][0]["roster_id"]})
    pick = {"type": kind, "action": action, "version": revision}
    chosen = client.post(arena, headers=signed(1000), json=pick)
    assert chosen.status_code == 200 and chosen.json["ballSeq"] > revision, chosen.json
    assert client.post(arena, headers=signed(1000), json=pick).status_code == 409
    with sessions() as db:
        game = db.get(StudioGame, game_id)
        assert game.tier == "free" and game.status == "in_progress"
        assert db.get(StudioResult, game_id) is None
    assert client.get("/events/studio").status_code == 302
    with client.session_transaction() as state:
        state["admin"] = True
    assert client.get("/events/studio").status_code == 200
    denied = client.post("/events/studio", data={"name": "Blocked by CSRF", "slug": "blocked"})
    assert denied.status_code in (302, 400)
    with sessions() as db:
        assert db.scalar(select(StudioEvent.id).filter_by(slug="blocked")) is None


def test_official_odi_pins_reference_and_finishes_fifty_over_state(sessions, monkeypatch):
    from engine.realism import odi
    from services import cipl_match

    with sessions() as db:
        config = _seed(db)
        config["format"] = "ODI"
        config = studio.generate(config, "all")
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        fixture = db.scalar(select(StudioGame).filter_by(enrollment_id=entry.id))
        game = play.start_fixture(db, user, fixture.id, None, CLOCK)
        assert game.state["engine_version"] == "odi-approach-1"
        assert game.state["odi_settings"]["reference_sha256"] == game.state["reference_hash"]
        assert game.state["overs"] == 50 and cipl_match.total_balls(game.state) == 300
        db.commit()
        # Suspending new ordinary ODIs must leave the pinned official session playable.
        monkeypatch.setenv("CMU_ODI_APPROACH_ENABLED", "0")
        options = cricket.public_choices(game.state, entry.team_key)
        selected = options["bowlers"][0]["id"] if options["bowlers"] else None
        _play_over(db, user, game.id, game.version, "balanced", selected, CLOCK)
        db.commit()
        assert game.step == 1
        original = deepcopy(game.state)
        restored = json.loads(json.dumps(game.state))
        for number in range(1, 100):
            outcome = cricket.advance(original, game.seed, number)
            assert cricket.advance(restored, game.seed, number) == outcome
            assert cricket.view(original, entry.team_key) == cricket.view(restored, entry.team_key)
            if outcome:
                break
        assert outcome == "completed"
        assert odi.settings(original)["reference_sha256"] == game.state["reference_hash"]
        _, result = cricket.match_result(original)
        assert all(0 < innings["balls"] <= 300 for innings in result["innings"])


def test_official_community_xp_worker_credits_once(sessions, monkeypatch):
    from models import CommunityXPLedger
    from models_events import StudioXPJob
    from services import community_config_service, event_studio_worker

    monkeypatch.setattr(community_config_service, "is_enabled", lambda session: True)
    monkeypatch.setattr(event_studio_worker, "get_session", sessions)
    with sessions() as db:
        config = _seed(db, kind="moment")
        config["community"]["completion_xp"] = 5
        config["moment"].update(start_over=0, overs=2, goal=1)
        event = _publish(db, studio.generate(config, "all"))
        user = db.scalar(select(User).order_by(User.id))
        _join(db, event, user)
        monkeypatch.setattr(cricket, "resolve_over", lambda *args, **kwargs: "success")
        for attempt in range(2):
            game = play.start_attempt(db, user, event.id, attempt, CLOCK)
            _play_over(db, user, game.id, game.version, "balanced", None, CLOCK)
            db.commit()
        assert db.scalar(select(func.count(StudioXPJob.id))) == 1
    event_studio_worker._sweep_community(NOW)
    event_studio_worker._sweep_community(NOW)
    with sessions() as db:
        rows = db.scalars(select(CommunityXPLedger)).all()
        assert len(rows) == 1 and rows[0].delta == 5
        assert db.scalar(select(StudioXPJob)).processed_at == NOW


def test_fixture_reward_override_is_reserved_before_auto_rewards():
    config = _schedule_config(2)
    config["schedule"]["legs"] = 1
    config["max_users"] = 5
    config["budget"]["coins"] = 5000
    config = studio.generate(config, "schedule")
    fixture = config["fixtures"][0]["key"]
    config["fixture_overrides"][fixture] = {
        "rewards": {"free": {"win": {"coins": 800}}, "diamond": {"win": {"coins": 1000}}},
    }
    generated = studio.generate(studio.normalize(config), "all")
    assert rules.liability(generated)["coins"] == 5000
    assert rules.fixture_rules(generated, fixture)["rewards"]["free"]["win"]["coins"] == 800
    config["budget"]["coins"] -= 1
    with pytest.raises(ValueError, match="locked fixture"):
        studio.generate(config, "rewards")


def test_deadline_extension_keeps_official_seed_and_cannot_reopen_a_result(sessions):
    with sessions() as db:
        event = _publish(db, _seed(db))
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game = db.scalar(select(StudioGame).filter_by(enrollment_id=entry.id))
        play.start_fixture(db, user, game.id, None, CLOCK)
        db.commit()
        seed = game.seed
        version = game.version
        next_deadline = game.deadline + timedelta(hours=1)
        play.extend_deadline(db, studio.lock(db, StudioEvent, event.id), game.id, next_deadline,
                             "operator", "Observed outage", NOW)
        db.commit()
        assert game.seed == seed and game.version == version + 1
        assert game.deadline == next_deadline and game.due_at == next_deadline + timedelta(minutes=30)
        play.abandon(db, user, game.id, game.version, CLOCK)
        db.commit()
        with pytest.raises(ValueError, match="unresolved"):
            play.extend_deadline(db, studio.lock(db, StudioEvent, event.id), game.id,
                                 next_deadline + timedelta(hours=1), "operator", "Try again", NOW)


def test_rotation_publishes_distinct_prevalidated_editions_with_a_combined_cap(sessions):
    with sessions() as db:
        config = _seed(db, kind="moment")
        config.update(opens_at="2026-12-01T09:00", closes_at="2026-12-01T19:00")
        config["moment"].update(start_over=0, overs=2, goal=5)
        config["rotation"].update(cadence="daily", periods=3)
        config = studio.generate(config, "all")
        config["rotation"]["series_budget"] = {asset: cost * 3 for asset, cost in rules.liability(config).items()}
        event = _publish(db, config)
        rows = db.scalars(select(StudioEvent).order_by(StudioEvent.opens_at)).all()
        assert [row.slug for row in rows] == [event.slug, event.slug + "-edition-2", event.slug + "-edition-3"]
        assert rows[2].opens_at - rows[0].opens_at == timedelta(days=2)
        assert rows[1].status == "scheduled"
        configs = [studio.revision(db, row, published=True).config for row in rows]
        assert len({row["moment"]["seed"] for row in configs}) == 3
        config["rotation"]["series_budget"]["coins"] = 0
        frozen, opens, closes, maximum = studio.validate_publish(db, config, NOW)
        with pytest.raises(ValueError, match="series budget"):
            studio.rotation_editions(frozen, opens, closes, maximum)


def test_reminder_has_durable_lease_receipt_and_respects_opt_out(sessions, monkeypatch):
    from models_events import StudioNotice
    from services import event_studio_notices as notices

    monkeypatch.setattr(notices, "get_session", sessions)
    with sessions() as db:
        config = _seed(db)
        config["notifications"]["enabled"] = True
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        _join(db, event, user)
        rows = db.scalars(select(StudioNotice).order_by(StudioNotice.due_at)).all()
        assert len(rows) == 2
        a, b = [(row.id, row.due_at) for row in rows]
        user_id = user.id
    due = notices.reserve(*a)
    assert due["user_id"] == user_id
    assert notices.reserve(*a) is None
    notices.settle(due, "sent", a[1])
    assert notices.reserve(*a) is None
    with sessions() as db:
        db.get(User, user_id).notifications_enabled = False
        db.commit()
    assert notices.reserve(*b) is None
    with sessions() as db:
        assert db.get(StudioNotice, a[0]).outcome == "sent"
        assert db.get(StudioNotice, b[0]).outcome == "obsolete"


def test_ai_worker_keeps_seed_and_committed_overs_across_batches(sessions):
    with sessions() as db:
        config = _seed(db, 3)
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        fixture = db.scalar(select(StudioGame).filter(
            StudioGame.enrollment_id == entry.id, StudioGame.home != entry.team_key,
            StudioGame.away != entry.team_key).order_by(StudioGame.opens_at))
        fixture_id = fixture.id
        play.run_due(db, fixture_id, NOW, over_budget=1)
        db.commit()
        assert fixture.step == 1 and fixture.status == "simulating"
        seed = fixture.seed
    with sessions() as db:
        fixture = db.get(StudioGame, fixture_id)
        play.run_due(db, fixture_id, NOW + timedelta(seconds=1), over_budget=1)
        db.commit()
        assert fixture.step == 2 and fixture.seed == seed
        for attempt in range(2, 15):
            play.run_due(db, fixture_id, NOW + timedelta(seconds=attempt))
            db.commit()
            if fixture.status == "completed":
                break
        assert fixture.status == "completed" and fixture.seed == seed
        assert db.get(StudioResult, fixture.id)
        assert not db.scalar(select(StudioEntitlement.id).filter_by(game_id=fixture.id))


def test_chapter_enrollment_requires_recorded_prerequisite_stars(sessions, monkeypatch):
    with sessions() as db:
        first_config = _seed(db, kind="moment")
        first_config["moment"].update(start_over=0, overs=2, goal=5)
        first_config = studio.generate(first_config, "all")
        first = _publish(db, first_config)
        later_config = deepcopy(first_config)
        later_config["chapter"].update(name="Opening chapter", position=2,
                                       prerequisite_slug=first.slug, min_stars=1)
        later = studio.create(db, "Next checkpoint", "next-checkpoint", "operator", later_config)
        studio.publish(db, later, 1, "operator", NOW)
        db.commit()
        player = db.scalar(select(User).order_by(User.id))
        assert not play.detail(db, player, later.id, NOW)["can_join"]
        with pytest.raises(ValueError, match="Complete Summer Cup"):
            play.join(db, player, later.id, "team-0", "tournament_team", NOW)
        db.rollback()
        _join(db, first, player)
        game = play.start_attempt(db, player, first.id, 0, CLOCK)
        monkeypatch.setattr(cricket, "resolve_over", lambda *args, **kwargs: "success")
        _play_over(db, player, game.id, game.version, "balanced", None, CLOCK)
        db.commit()
        assert play.detail(db, player, later.id, NOW)["can_join"]
        assert _join(db, later, player).event_id == later.id


def test_boss_goal_counts_distinct_winners(sessions, monkeypatch):
    with sessions() as db:
        config = _seed(db, kind="boss")
        config["challenge_style"] = "moment"
        config["moment"].update(start_over=0, overs=2, goal=5)
        config["community"]["boss_goal"] = 2
        event = _publish(db, studio.generate(config, "all"))
        user = db.scalar(select(User).order_by(User.id))
        _join(db, event, user)
        monkeypatch.setattr(cricket, "resolve_over", lambda *args, **kwargs: "success")
        for attempt in (0, 1):
            game = play.start_attempt(db, user, event.id, attempt, CLOCK)
            _play_over(db, user, game.id, game.version, "balanced", None, CLOCK)
            db.commit()
        assert play.detail(db, user, event.id, NOW)["boss_goal"] == {"target": 2, "winners": 1}


def test_verified_engine_failure_can_restore_a_moment_attempt(sessions, monkeypatch):
    with sessions() as db:
        config = _seed(db, kind="moment")
        config["attempts"] = 1
        config["moment"].update(start_over=0, overs=2, goal=5)
        event = _publish(db, studio.generate(config, "all"))
        user = db.scalar(select(User).order_by(User.id))
        _join(db, event, user)
        game = play.start_attempt(db, user, event.id, 0, CLOCK)
        db.commit()
        with pytest.raises(ValueError, match="no verified"):
            play.resolve_technical_failure(db, studio.lock(db, StudioEvent, event.id), game.id,
                                           "operator", "Unverified", NOW)
        db.rollback()

        def fail(*args, **kwargs):
            raise RuntimeError("simulated engine failure")

        with monkeypatch.context() as failure:
            failure.setattr(cricket, "resolve_over", fail)
            with pytest.raises(studio.ExecutionUnavailable):
                _play_over(db, user, game.id, game.version, "balanced", None, CLOCK)
            db.commit()
        assert game.status == "in_progress" and game.step == 0
        assert game.failure_type == "RuntimeError"
        play.run_due(db, game.id, game.due_at + timedelta(seconds=1))
        db.commit()
        assert game.status == "in_progress"
        play.resolve_technical_failure(db, studio.lock(db, StudioEvent, event.id), game.id,
                                       "operator", "Verified engine failure", NOW)
        db.commit()
        assert game.status == "technical_void"
        assert play.detail(db, user, event.id, NOW)["attempts_used"] == 0
        assert play.start_attempt(db, user, event.id, 1, CLOCK).attempt == 2


def test_event_cancellation_preserves_completed_results(sessions):
    with sessions() as db:
        event = _publish(db, _seed(db))
        first, second = db.scalars(select(User).order_by(User.id)).all()
        ea = _join(db, event, first)
        eb = _join(db, event, second)
        a = db.scalar(select(StudioGame).filter_by(enrollment_id=ea.id))
        b = db.scalar(select(StudioGame).filter_by(enrollment_id=eb.id))
        play.start_fixture(db, first, a.id, None, CLOCK)
        play.abandon(db, first, a.id, a.version, CLOCK)
        db.commit()
        play.operate(db, studio.lock(db, StudioEvent, event.id), "cancel", "operator", "Operational cancellation", NOW)
        db.commit()
        assert a.status == "forfeited"
        assert b.status == "cancelled"
        assert play.detail(db, second, event.id, NOW)["table"][0]["points"] == 0
        assert not play.detail(db, second, event.id, NOW)["can_join"]


def test_chat_batting_and_bowling_use_committed_cipl_stages(sessions, monkeypatch):
    from handlers import event_studio as chat
    from services import match_broadcast
    from services.cipl_match import eligible_bowlers

    monkeypatch.setattr(match_broadcast, "flip_coin", lambda call: (call, True))
    with sessions() as db:
        config = _seed(db)
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game_id = db.scalar(select(StudioGame.id).filter_by(enrollment_id=entry.id))
        setup = play.setup_fixture(db, user, event.id, game_id, 0, NOW, call="heads")
        assert setup["toss"]["human_won"] and db.get(StudioGame, game_id).started_at is None
        game = play.start_fixture(db, user, game_id, setup["default_xi"], CLOCK, decision="bowl",
                                  captain_id=setup["default_xi"][2], require_toss=True)
        assert game.state["event_context"]["squad_keys"] == ["team-0", "team-1"]
        bowler = eligible_bowlers(game.state)[0]
        game = play.submit_pick(db, user, game.id, game.version, picks.PICK_BOWLER,
                                bowler["roster_id"], source="chat", clock=CLOCK)
        ai_pick = game.state["batting_approach"]
        assert ai_pick and game.state["current_bowler"]["roster_id"] == bowler["roster_id"]
        assert game.state["total_runs"] == game.state["current_ball"] == 0
        text, _, _ = chat._match_panel(play.detail(db, user, event.id, NOW),
            play.game_view(game, None, entry.team_key, NOW, studio.revision(db, event, published=True).config),
            game.state)
        assert "Choose your bowling approach." in text
        assert "batting_approach" not in text
        with pytest.raises(ValueError, match="other surface"):
            play.submit_pick(db, user, game.id, game.version, picks.PICK_BOWL_APPROACH,
                             0, source="app", clock=CLOCK)
        play.submit_pick(db, user, game.id, game.version, picks.PICK_BOWL_APPROACH,
                         0, source="chat", clock=CLOCK)
        assert game.state["approach_log"][0]["bat"] == ai_pick
        db.commit()


def test_wicket_checkpoint_timeout_and_surface_switch_preserve_resolved_balls(sessions, monkeypatch):
    from services import match_broadcast
    from services.cipl_match import available_batsmen

    monkeypatch.setattr(match_broadcast, "flip_coin", lambda call: (call, True))
    with sessions() as db:
        event = _publish(db, _seed(db))
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game_id = db.scalar(select(StudioGame.id).filter_by(enrollment_id=entry.id))
        play.setup_fixture(db, user, event.id, game_id, 0, NOW, call="heads")
        game = play.start_fixture(db, user, game_id, None, CLOCK, decision="bat")
        human = entry.team_key
        # Find an actual seeded wicket, then commit that seed with one normal pick.
        seed = None
        for index in range(300):
            candidate = f"{index:064x}"
            state = deepcopy(game.state)
            state["rng_seed"] = candidate
            cricket.prepare_pick(state, candidate, 0, human)
            trial = deepcopy(state)
            cricket.submit(trial, candidate, 0, human, picks.PICK_BAT_APPROACH, 4)
            if trial.get("event_next_action") == picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]:
                seed = candidate
                break
        assert seed is not None
        game.seed, game.state = seed, state
        play.submit_pick(db, user, game.id, game.version, picks.PICK_BAT_APPROACH, 4, source="chat", clock=CLOCK)
        checkpoint = json.loads(json.dumps(game.state["over_in_progress"]))
        assert len(available_batsmen(game.state)) >= 2 and game.next_pick_at is not None
        gameid, userid = game.id, user.id
        old_version = game.version
        db.commit()

    with sessions() as db:
        user = db.get(User, userid)
        game = db.get(StudioGame, gameid)
        play.set_play_state(db, user, game.id, game.version, "pause", "chat", CLOCK)
        assert game.next_pick_at is None and game.state["over_in_progress"] == checkpoint
        play.set_play_state(db, user, game.id, game.version, "resume", "app", CLOCK)
        assert game.next_pick_at and game.state["over_in_progress"] == checkpoint
        with pytest.raises(studio.Conflict):
            play.submit_pick(db, user, game.id, old_version, picks.PICK_NEW_BATSMAN, 0, source="chat", clock=CLOCK)
        db.commit()

    with sessions() as db:
        game = db.get(StudioGame, gameid)
        due = game.next_pick_at
        first = available_batsmen(game.state)[0]
        assert gameid in play.due_ids(db, due)
        play.run_due(db, gameid, due)
        assert game.status == "in_progress"
        assert game.state["batting_order"][2]["roster_id"] == first["roster_id"]
        timeline = (game.state.get("over_in_progress") or {}).get("over_timeline") or game.state["last_over_timeline"]
        assert timeline[:len(checkpoint["over_timeline"])] == checkpoint["over_timeline"]
        db.commit()


def test_recorded_moment_validates_scorecards_and_objective_baselines(sessions):
    with sessions() as db:
        config = _seed(db, kind="moment")
        scorecard = {
            "runs": 8, "wickets": 0, "extras": 0, "crease": [1, 2],
            "partnership": {"runs": 8, "balls": 6},
            "batting": [
                {"slot": 1, "runs": 8, "balls": 6, "fours": 2, "sixes": 0, "dots": 4,
                 "dismissal": "", "bowler": None},
                {"slot": 2, "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "dots": 0,
                 "dismissal": "", "bowler": None},
            ],
            "bowling": [{"slot": 7, "runs": 8, "balls": 6, "wickets": 0, "dots": 4, "maidens": 0}],
            "overs": [{"bowler": 7, "balls": 6, "runs": 8, "wickets": 0, "credited_wickets": 0, "maidens": 0}],
        }
        config["moment"].update(start_over=1, overs=2, goal=10, checkpoint={"current": scorecard})
        config = studio.generate(config, "all")
        event = _publish(db, config)
        frozen = studio.revision(db, event, published=True).config
        state = frozen["moment_state"]
        assert state["total_runs"] == 8 and cricket.match.balls_bowled(state) == 6
        assert state["prev_bowler_rid"] == state["bowl_xi"][6]["roster_id"]
        assert cricket.objective_progress(frozen, state, "team-0")[0]["measured"] == 0
        inclusive = deepcopy(state)
        inclusive["event_moment"]["objective_baseline"] = "inclusive"
        assert cricket.objective_progress(frozen, inclusive, "team-0")[0]["measured"] == 2
        for field, bad in (("runs", 9), ("crease", [1, 1]),
                           ("overs", [*scorecard["overs"], *scorecard["overs"]])):
            broken = deepcopy(frozen)
            broken["moment"]["checkpoint"]["current"][field] = bad
            with pytest.raises(ValueError):
                cricket.build_moment(broken)


def test_hard_plus_outcome_probabilities_equal_hard_for_identical_inputs(sessions):
    from services import bot_tactics

    with sessions() as db:
        event = _publish(db, _seed(db))
        config = studio.revision(db, event, published=True).config
    xis = {key: team["default_xi"] for key, team in config["snapshot"].items()}
    seed = "a1" * 32
    state = cricket.build_state(config, 1, "team-0", "team-1", xis, seed, human="team-0", batting="team-0")
    harder = deepcopy(state)
    cricket.pin_difficulty(harder, config, "own_roster")
    bowler = cricket.match.eligible_bowlers(state)[0]
    assert bot_tactics.ball_probabilities(state, bowler) == bot_tactics.ball_probabilities(harder, bowler)
    for checkpoint in (state, harder):
        checkpoint.update(current_bowler=bowler, batting_approach="ultra", bowling_approach="defensive")
        cricket.resolve_over(checkpoint, seed, 0)
    assert state["timeline"] == harder["timeline"]
    assert state["bat_stats"] == harder["bat_stats"] and state["bowl_stats"] == harder["bowl_stats"]


def test_expiry_and_a_completed_over_can_only_settle_once(sessions, monkeypatch):
    from threading import Barrier

    with sessions() as db:
        event = _publish(db, _seed(db))
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game = play.start_fixture(db, user, db.scalar(
            select(StudioGame.id).filter_by(enrollment_id=entry.id)), None, CLOCK)
        stage = game.state["event_next_action"]
        if stage == picks.PICK_ACTION[picks.PICK_BOWLER]:
            bowler = cricket.match.eligible_bowlers(game.state)[0]["roster_id"]
            play.submit_pick(db, user, game.id, game.version, picks.PICK_BOWLER, bowler,
                             source="chat", clock=CLOCK)
        kind = next(key for key, stage in picks.PICK_ACTION.items() if stage == game.state["event_next_action"])
        game_id, user_id, version, cutoff = game.id, user.id, game.version, game.due_at
        db.commit()

    monkeypatch.setattr(cricket, "resolve_over", lambda *args, **kwargs: "completed")
    monkeypatch.setattr(cricket, "match_result", lambda state: ("team-0", {"innings": []}))
    monkeypatch.setattr(play.progress, "contribution", lambda *args: [])
    simultaneous = Barrier(2)

    def complete():
        with sessions() as db:
            user = db.get(User, user_id)
            simultaneous.wait(timeout=5)
            try:
                play.submit_pick(db, user, game_id, version, kind, 1,
                                 source="chat", clock=lambda: cutoff)
                db.commit()
            except studio.Conflict:
                db.rollback()

    def expiry():
        with sessions() as db:
            simultaneous.wait(timeout=5)
            play.run_due(db, game_id, cutoff + timedelta(seconds=1))
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        completions = [pool.submit(task) for task in (complete, expiry)]
        for pending in completions:
            pending.result(timeout=20)

    with sessions() as db:
        game = db.get(StudioGame, game_id)
        result = db.get(StudioResult, game_id)
        assert game.status == result.kind and result.kind in ("completed", "forfeited")
        assert db.scalar(select(func.count(StudioResult.game_id)).where(StudioResult.game_id == game_id)) == 1
        assert db.scalar(select(func.count(StudioEntitlement.id)).where(
            StudioEntitlement.game_id == game_id)) == int(result.kind == "completed")
        play.run_due(db, game_id, cutoff + timedelta(days=1))
        assert db.get(StudioResult, game_id).kind == result.kind


def test_unstarted_forfeit_has_no_score_nrr_or_reward(sessions):
    with sessions() as db:
        event = _publish(db, _seed(db))
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game = db.scalar(select(StudioGame).filter_by(enrollment_id=entry.id))
        play.run_due(db, game.id, game.deadline + timedelta(seconds=1))
        db.flush()
        assert game.status == "forfeited" and game.state is None
        result = db.get(StudioResult, game.id)
        assert result.detail == {"reason": "deadline"}
        config = studio.revision(db, event, published=True).config
        for row in play.table(config, [(game, result)]):
            assert row["nrr"] == row["scored"] == row["faced"] == 0
        assert not db.scalar(select(StudioEntitlement.id))
        assert play.start_fixture(db, user, game.id, None, CLOCK).status == "forfeited"


def test_tied_odi_and_t20_use_competition_points(sessions):
    with sessions() as db:
        event = _publish(db, _seed(db))
        config = studio.revision(db, event, published=True).config
        xis = {key: team["default_xi"] for key, team in config["snapshot"].items()}
        from engine.realism import odi

        for format_key in ("T20", "ODI"):
            current = {**config, "format": format_key, "engine": rules.FORMATS[format_key]["engine"]}
            if format_key == "ODI":
                current["odi_settings"] = odi.new_settings()
            state = cricket.build_state(current, 1, "team-0", "team-1", xis, "12" * 32,
                                        human="team-0", batting="team-0")
            state.update(total_runs=100, current_over=state["overs"] + 1)
            cricket.match.end_first_innings(state)
            state.update(total_runs=100, current_over=state["overs"] + 1)
            winner, detail = cricket.match_result(state)
            assert winner is None and detail["margin_type"] == "tie"
            assert [row["runs"] for row in detail["innings"]] == [100, 100]


def test_t20_event_pins_enabled_measured_cipl_model(sessions, monkeypatch):
    monkeypatch.setenv("SIM_REALISM_ENABLED", "1")
    monkeypatch.setenv("SIM_REALISM_SHADOW_MODE", "0")
    monkeypatch.setenv("SIM_REALISM_FORMATS", "T20")
    with sessions() as db:
        event = _publish(db, _seed(db))
        config = studio.revision(db, event, published=True).config
        profile = config["realism_profile"]
        assert profile["reference"]["format"] == "T20"
        assert config["realism_profile_hash"] == studio.digest(profile)
    xis = {key: team["default_xi"] for key, team in config["snapshot"].items()}
    seed = "24" * 32
    state = cricket.build_state(config, 1, "team-0", "team-1", xis, seed,
                                human="team-0", batting="team-0")
    assert cricket.match.uses_t20_realism(state, cricket.match.approach_engine_format(state))
    cricket.prepare_pick(state, seed, 0, "team-0")
    copy = json.loads(json.dumps(state))
    cricket.submit(state, seed, 0, "team-0", picks.PICK_BAT_APPROACH, 2)
    monkeypatch.setenv("SIM_REALISM_ENABLED", "0")
    assert cricket.match.uses_t20_realism(copy, cricket.match.approach_engine_format(copy))
    cricket.submit(copy, seed, 0, "team-0", picks.PICK_BAT_APPROACH, 2)
    assert cricket.view(copy, "team-0") == cricket.view(state, "team-0")


def test_scheduled_conditions_and_impact_use_the_shared_t20_rules(sessions, monkeypatch):
    from services import impact_player, match_broadcast

    monkeypatch.setattr(match_broadcast, "flip_coin", lambda call: (call, True))
    with sessions() as db:
        config = _seed(db)
        reserve = Player(name="Team 0 Reserve", category="Bowler", country="India",
                         rating=60, bat_rating=50, bowl_rating=66, is_active=True,
                         gender="Male", bat_hand="Right", bowl_hand="Right", bowl_style="Fast")
        db.add(reserve)
        db.flush()
        config["teams"][0]["players"].append(reserve.id)
        config["impact_player"] = True
        config = studio.generate(config, "all")
        key = config["fixtures"][0]["key"]
        config["fixture_overrides"][key] = {"weather": "Overcast", "pitch": "Flat",
                                             "venue": "Reserve Ground", "difficulty": "normal"}
        event = _publish(db, config)
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game_id = db.scalar(select(StudioGame.id).filter_by(enrollment_id=entry.id))
        setup = play.setup_fixture(db, user, event.id, game_id, 0, NOW, call="heads")
        assert (setup["venue"], setup["weather"], setup["difficulty"]) == ("Reserve Ground", "Overcast", "normal")
        game = play.start_fixture(db, user, game_id, None, CLOCK, decision="bowl")
        assert game.state["pitch_type"] == "Flat" and game.state["conditions"]["timeline"] == []
        assert game.state["event_context"]["difficulty"] == "normal"
        impact = impact_player.cipl_options(game.state, entry.team_key, game.state["event_next_action"])
        assert impact["can_use"]
        leaving = next(p for p in impact["replaceable_players"] if p["category"] == "Bowler")
        incoming = impact["incoming_options"][0]
        play.use_impact(db, user, game.id, game.version, incoming["roster_id"], leaving["roster_id"], None,
                        source="chat", clock=CLOCK)
        assert impact_player.summary(game.state)[0]["in_player"] == reserve.name
        assert not impact_player.cipl_options(game.state, entry.team_key, game.state["event_next_action"])["can_use"]
        with pytest.raises(ValueError, match="full T20"):
            studio.normalize({**config, "format": "ODI"})


def test_official_result_does_not_create_ordinary_stats_or_match_rewards(sessions, monkeypatch):
    from models import Match, MatchRewardSettlement, PlayerGameStats

    with sessions() as db:
        event = _publish(db, _seed(db))
        user = db.scalar(select(User).order_by(User.id))
        entry = _join(db, event, user)
        game = play.start_fixture(db, user, db.scalar(
            select(StudioGame.id).filter_by(enrollment_id=entry.id)), None, CLOCK)
        monkeypatch.setattr(cricket, "resolve_over", lambda *args, **kwargs: "completed")
        monkeypatch.setattr(cricket, "match_result", lambda state: (entry.team_key, {"innings": []}))
        choices = cricket.public_choices(game.state, entry.team_key)
        bowler = choices["bowlers"][0]["id"] if choices["bowlers"] else None
        _play_over(db, user, game.id, game.version, "balanced", bowler, CLOCK)
        db.flush()
        assert game.status == "completed"
        assert not db.scalar(select(Match.id)) and not db.scalar(select(PlayerGameStats.id))
        assert not db.scalar(select(MatchRewardSettlement.match_id))
        assert user.matches_played == user.total_coins == 0
        assert db.scalar(select(StudioEntitlement.id).where(StudioEntitlement.game_id == game.id))


def test_each_fixture_draws_its_own_reproducible_ground():
    config = studio.generate(_schedule_config(8), "schedule")
    venues, pitches = studio.condition_pools(config)
    from services.pitch_report import WEATHERS

    rows = config["fixtures"]
    assert all(row["pitch"] in pitches and row["venue"] in venues and row["weather"] in WEATHERS
               for row in rows)
    # Not one ground for the whole event any more.
    assert len({(row["pitch"], row["venue"]) for row in rows}) > 1
    # "Dead" is never drawn, and the mistaken "Wicket" surface never was one.
    assert not {row["pitch"] for row in rows} & {"Dead", "Wicket"}
    # Regenerating an unchanged draft redraws the identical grounds; a new seed re-rolls.
    assert studio.generate(config, "schedule")["fixtures"] == rows
    reseeded = deepcopy(config)
    reseeded["schedule"]["seed"] = 99
    assert {row["venue"] for row in studio.generate(reseeded, "schedule")["fixtures"]} != \
        {row["venue"] for row in rows} or len(venues) < 4


def test_venue_countries_narrow_the_draw_and_odi_keeps_its_reference_pitches():
    from services.draft_service import country_key
    from services.tournament_stadiums import all_rows

    config = _schedule_config(8)
    config["schedule"]["venue_countries"] = ["india"]
    indian = {row["name"] for row in all_rows() if country_key(row.get("country")) == "india"}
    drawn = {row["venue"] for row in studio.generate(studio.normalize(config), "schedule")["fixtures"]}
    assert drawn and drawn <= indian

    config["schedule"]["venue_countries"] = ["Narnia"]
    with pytest.raises(ValueError, match="matches the chosen venue countries"):
        studio.generate(studio.normalize(config), "schedule")

    odi = _schedule_config(8)
    odi["format"] = "ODI"
    from engine.realism import odi as odi_reference
    supported = set(odi_reference.new_settings()["pitch"])
    assert {row["pitch"] for row in studio.generate(studio.normalize(odi), "schedule")["fixtures"]} <= supported


def test_manual_fixture_carries_its_own_pitch_and_venue():
    config = _schedule_config(2)
    config["schedule"].update(preset="manual", legs=1)
    config["fixtures"] = [{
        "key": "r1-1", "home": "team-0", "away": "team-1", "round": 1, "stage": "league",
        "opens_at": "2026-12-01T09:00", "deadline": "2026-12-01T20:00", "grace": 30,
        "source": "manual", "pitch": "Flat", "venue": "Wankhede Stadium", "weather": "Humid",
    }]
    config = studio.generate(studio.normalize(config), "schedule")
    ruling = rules.fixture_rules(config, "r1-1")
    assert (ruling["pitch"], ruling["venue"], ruling["weather"]) == ("Flat", "Wankhede Stadium", "Humid")

    # An explicit fixture override still beats the row.
    config["fixture_overrides"]["r1-1"] = {"venue": "Eden Gardens"}
    assert rules.fixture_rules(studio.normalize(config), "r1-1")["venue"] == "Eden Gardens"

    with pytest.raises(ValueError, match="supported pitch"):
        studio.normalize({**config, "fixtures": [{**config["fixtures"][0], "pitch": "Trampoline"}]})


def test_a_drawn_ground_survives_a_hand_edit_and_a_regeneration():
    config = studio.generate(_schedule_config(8), "schedule")
    key = config["fixtures"][0]["key"]
    config["fixtures"][0]["venue"] = "Reserve Ground"
    config["fixtures"][1].update(pitch="Dead", locks=["pitch"])
    regenerated = studio.generate(studio.normalize(config), "schedule")
    assert regenerated["fixtures"][0]["venue"] == "Reserve Ground"
    assert regenerated["fixtures"][1]["pitch"] == "Dead"
    # Replacing unlocked manual edits puts the drawn venue back.
    replaced = studio.generate(studio.normalize(config), "schedule", overwrite=True)
    assert replaced["fixtures"][0]["venue"] != "Reserve Ground"
    assert next(row for row in replaced["fixtures"] if row["key"] == key)["venue"] in \
        studio.condition_pools(config)[0]


def test_a_standalone_challenge_draws_one_ground():
    config = studio.generate(_schedule_config(2, kind="moment"), "schedule")
    venues, pitches = studio.condition_pools(config)
    assert not config["fixtures"]
    assert config["pitch"] in pitches and config["venue"] in venues and config["weather"]
    assert rules.fixture_rules(config, "challenge")["venue"] == config["venue"]


def test_anyone_may_enrol_past_the_budget_figure(sessions):
    with sessions() as db:
        config = _seed(db)
        config["max_users"] = 1
        # max_users only scales the reward budget now, so regenerate against the new figure.
        event = _publish(db, studio.generate(studio.normalize(config), "all"))
        users = db.scalars(select(User).order_by(User.id)).all()
        assert len(users) >= 2
        for user in users:
            _join(db, event, user)
        assert db.scalar(select(func.count(StudioEnrollment.id)).filter_by(event_id=event.id)) == len(users)


def test_enrollment_closes_is_derived_and_the_old_key_still_loads():
    # A Live Event takes players until its first fixture deadline, because a campaign's
    # games are written at join time with the published deadlines.
    config = studio.generate(_schedule_config(4), "schedule")
    first = min(schedule.utc_instant(row["deadline"], config["timezone"])
                for row in config["fixtures"])
    assert studio.enrollment_closes(config) == first

    # A standalone challenge has no fixtures, so it takes players right up to Closes.
    moment = studio.normalize(_schedule_config(2, kind="moment"))
    assert studio.enrollment_closes(moment) == schedule.utc_instant(
        moment["closes_at"], moment["timezone"])

    # The field is gone, but a draft saved before that still opens — the key is dropped.
    assert "enrollment_cutoff" not in studio.defaults()
    revived = studio.normalize({**_schedule_config(4), "enrollment_cutoff": "2026-12-01T22:00"})
    assert "enrollment_cutoff" not in revived
    with pytest.raises(ValueError, match="unsupported fields"):
        studio.normalize({**_schedule_config(4), "not_a_real_key": 1})


def test_a_late_joiner_is_turned_away_rather_than_given_dead_fixtures(sessions):
    with sessions() as db:
        event = _publish(db, _seed(db))
        config = studio.revision(db, event, published=True).config
        user = db.scalar(select(User).order_by(User.id))
        closes = play._cutoff(event, config)
        team = config["teams"][0]["key"]
        # One second before enrollment closes, joining still works, and every fixture
        # the campaign is given is still ahead of the player.
        entry = play.join(db, user, event.id, team, "tournament_team", closes - timedelta(seconds=1))
        db.flush()
        games = db.scalars(select(StudioGame).filter_by(enrollment_id=entry.id)).all()
        assert games and all(game.deadline >= closes for game in games)

        # After it, the event reports itself closed to new players.
        other = db.scalars(select(User).order_by(User.id)).all()[1]
        with pytest.raises(ValueError, match="Enrollment is closed"):
            play.join(db, other, event.id, team, "tournament_team", closes + timedelta(seconds=1))
