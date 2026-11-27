import asyncio
from copy import deepcopy
from itertools import product
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.realism import odi
from engine.realism.context import chase_bucket
from handlers import botlevel, cipl_play, letsplay, lpbot
from services import bot_captain, bot_tactics, cipl_match as cm, impact_player, odi_approach
from services.match_formats import extract_approach_format, extract_overs_arg
from services.match_state_store import A_PICK_CIPL_BOWLER
from services.match_webapp_service import strip_hidden_plans
from tools.calibrate_odi_approach import team
from tools.odi_reference import classify


@pytest.fixture
def state():
    s = cm.build_cipl_state(4242, 50, 1, 2, 111, 222, team(1, 0), team(2, 0),
                            "Bat", "Bowl", -100, ball_format="ODI")
    s["rng_seed"] = 1067
    return s


def ready(state):
    state["current_bowler"] = bot_captain.pick_bowler(state)
    state["batting_approach"] = state["bowling_approach"] = "balanced"
    return state["current_bowler"]


def outcome(kind="Dot", **extras):
    return {"type": "run", "runs": 0, "is_extra": False, "batter_out": False,
            "odi_outcome": kind, **extras}


def extra(kind, count, *, runs=0, byes=0, wicket=None):
    res = outcome("Extras")
    return dict(res, type="extra", runs=runs if kind == "No Ball" else count, is_extra=True,
                extra_type=kind, odi_extras=count, odi_bat_runs=runs, odi_byes=byes,
                batter_out=bool(wicket), wicket_type=wicket)


@pytest.mark.parametrize("tokens,fmt,length,remaining", [
    ([], "T20", None, []), (["1"], "T20", 1, []), (["T20"], "T20", 20, []),
    (["ODI", "@some_user"], "ODI", 50, ["@some_user"]),
    (["@ODI", "50"], "ODI", 50, ["@ODI"]),
    (["50", "1234567"], "ODI", 50, ["1234567"]),
    (["ODI"], "ODI", 50, []),
])
def test_explicit_format_parser(monkeypatch, tokens, fmt, length, remaining):
    monkeypatch.setenv("CMU_ODI_APPROACH_ENABLED", "true")
    assert extract_approach_format(tokens) == (fmt, length, remaining, None)


@pytest.mark.parametrize("tokens", [
    ["25"], ["21"], ["49"], ["ODI", "50"], ["ODI", "20"], ["5", "3"],
    ["ODI50"], ["50.5"], ["-5"], ["t50"],
])
def test_reject_invalid_lengths(monkeypatch, tokens):
    monkeypatch.setenv("CMU_ODI_APPROACH_ENABLED", "true")
    assert extract_approach_format(tokens)[-1]
    assert extract_overs_arg(["25"])[-1]


def test_flag_defaults_closed_and_rollback_keeps_saved_matches(monkeypatch, state):
    monkeypatch.delenv("CMU_ODI_APPROACH_ENABLED", raising=False)
    assert "unavailable" in extract_approach_format(["ODI"])[-1]
    restored = json.loads(json.dumps(state))
    ready(restored)
    cm.simulate_over(restored)
    assert cm.balls_bowled(restored) == 6


@pytest.mark.parametrize("args", [["ODI", "app"], ["chat", "50"], ["50", "chat"], ["app", "ODI"]])
def test_bot_difficulty_carries_format_and_remembers_mode(monkeypatch, args):
    monkeypatch.setenv("CMU_ODI_APPROACH_ENABLED", "1")
    user = MagicMock(id=123456)
    msg = MagicMock(reply_text=AsyncMock())
    update = MagicMock(effective_user=user, effective_chat=MagicMock(id=-100), effective_message=msg)
    ctx = MagicMock(args=args, bot_data={}, user_data={})
    asyncio.run(lpbot.lpbot_handler(update, ctx))
    markup = msg.reply_text.await_args.kwargs["reply_markup"]
    assert "ODI" in msg.reply_text.await_args.args[0]
    assert botlevel.play_mode_for(ctx.bot_data, user.id) == ("app" if "app" in args else "chat")
    data = markup.inline_keyboard[0][0].callback_data
    assert "_ODI_" in data
    update.callback_query = MagicMock(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
    called = AsyncMock()
    monkeypatch.setattr(lpbot, "lpbot_handler", called)
    asyncio.run(botlevel.difficulty_callback(update, ctx))
    called.assert_awaited_once()
    assert ctx.args == ["ODI"]
    assert ctx.user_data["_bot_difficulty_answered"]


@pytest.mark.parametrize("args,target_args", [(["50", "@captain"], ["@captain"]), (["ODI"], [])])
def test_letsplay_passes_target_arguments_unchanged(monkeypatch, args, target_args):
    monkeypatch.setenv("CMU_ODI_APPROACH_ENABLED", "1")
    monkeypatch.setattr(letsplay, "get_session", MagicMock())
    monkeypatch.setattr(letsplay, "sync_telegram_user", MagicMock())
    monkeypatch.setattr(letsplay, "_active_letsplay_in_chat", lambda *a: False)
    monkeypatch.setattr(letsplay, "_active_match_in_chat", lambda *a: None)
    resolve = MagicMock(return_value=(None, "test target lookup"))
    monkeypatch.setattr(letsplay, "resolve_command_target", resolve)
    message = MagicMock(reply_text=AsyncMock())
    update = MagicMock(effective_message=message, effective_user=MagicMock(), effective_chat=MagicMock())
    ctx = MagicMock(args=args, bot_data={})
    asyncio.run(letsplay.letsplay_handler(update, ctx))
    resolve.assert_called_once()
    assert ctx.args == target_args


def test_toss_shows_odi_and_saved_settings_survive(monkeypatch, state):
    send = AsyncMock()
    monkeypatch.setattr(letsplay.R, "send_rich_message", send)
    draft = {"ball_format": "ODI", "overs": 50, "chat_id": -100, "invite_id": 1,
             "guest": {"name": "Peer", "tg_id": 321}, "odi_settings": state["odi_settings"]}
    asyncio.run(letsplay._start_toss(MagicMock(), json.loads(json.dumps(draft))))
    assert "ODI · 50 overs" in send.await_args.args[3]
    assert draft["odi_settings"]["reference_sha256"] == state["odi_settings"]["reference_sha256"]


def test_canonical_odi_phase_and_reference(state):
    for over, phase in ((1, "powerplay"), (10, "powerplay"), (11, "middle"),
                        (40, "middle"), (41, "death"), (50, "death")):
        state["current_over"] = over
        ready(state)
        context, _, _, node, metadata, _ = odi_approach.delivery_model(state)
        assert cm.approach_engine_format(state).name == "ListA"
        assert cm.approach_engine_format(state).get_phase(over - 1).name.lower() == (
            "pp1" if phase == "powerplay" else phase)
        assert context.format_name == "ODI" and context.phase == phase == cm.approach_phase(state)
        assert f"/{phase}/" in node
        assert metadata["count_kind"] == "observed"
        assert metadata["n"] >= 200
    assert [chase_bucket(rate, "ODI") for rate in (3, 4, 6, 8, 10)] == [
        "rrr_lt4", "rrr_4_6", "rrr_6_8", "rrr_8_10", "rrr_10_plus"]
    assert chase_bucket(5, "T20") == "rrr_lt6"
    assert cm.approach_phase({"ball_format": "T20", "overs": 50, "current_over": 10}) == "middle"


def test_reference_fails_closed_and_rejects_prior(state):
    ready(state)
    state["odi_settings"]["reference_version"] = "v1"
    with pytest.raises(odi.ODIUnavailable):
        cm.simulate_over(state)
    assert cm.balls_bowled(state) == 0
    assert not state.get("_rng_tick")


def test_ai_weights_are_live_weights_with_hidden_picks_excluded(state):
    ready(state)
    model = odi_approach.delivery_model(state)
    fingerprint = bot_tactics._fingerprint(state, None)
    before = bot_tactics.payoff_matrix(state)
    assert bot_tactics._realism_context(state) == model[0]
    assert bot_tactics.ball_probabilities(state) == odi_approach.probabilities(model)
    state["bowling_approach"], state["batting_approach"] = "variation", "ultra"
    assert bot_tactics._fingerprint(state, None) == fingerprint
    assert bot_tactics.payoff_matrix(state) == before
    public = strip_hidden_plans(state)
    for key in ("bowling_approach", "batting_approach", "rng_seed", "odi_settings"):
        assert key not in public


def test_workload_recovery_and_innings_reset(state):
    config = odi.settings(state)
    h = [{"over": i, "innings": 1, "bowler_rid": 5, "balls": 6} for i in (1, 3, 5, 7, 9)]
    current = odi.workload(h, 1, 11, 5, 30, False, config)
    rested = odi.workload(h, 1, 18, 5, 30, False, config)
    spinner = odi.workload(h, 1, 11, 5, 30, True, config)
    assert current.spell_overs == 5 and current.rest_overs == 1 and current.legal_balls == 30
    assert rested.penalty < current.penalty and rested.spell_overs == 0
    assert spinner.penalty < current.penalty
    assert odi.workload(h, 2, 1, 5, 0, False, config).fatigue == 0


def test_feasibility_bound_matches_exhaustive_small_schedules():
    def possible(quotas, last, overs):
        if not overs:
            return True
        for i, quota in enumerate(quotas):
            if quota and i != last:
                remainder = quotas[:i] + (quota - 1,) + quotas[i + 1:]
                if possible(remainder, i, overs - 1):
                    return True
        return False
    for quotas in product(range(4), repeat=3):
        for last in range(-1, 3):
            for overs in range(1, 7):
                assert odi_approach.can_allocate(quotas, last, overs) == possible(quotas, last, overs)


def test_no_illegal_fallback_or_overcommit(state):
    state["current_over"] = 50
    pool = state["bowl_xi"]
    for p in pool:
        state["bowl_stats"][str(p["roster_id"])]["balls"] = 6 * cm.quota_for(state, p)
    last = pool[-1]
    state["prev_bowler_rid"] = last["roster_id"]
    state["bowl_stats"][str(last["roster_id"])]["balls"] = 54
    assert cm.eligible_bowlers(state) == []
    state["current_bowler"] = last
    with pytest.raises(odi.ODIUnavailable):
        cm.simulate_over(state)


def test_max_duration_two_innings_tie(monkeypatch, state):
    monkeypatch.setattr(odi_approach, "bowl_delivery", lambda *a, **k: outcome())
    monkeypatch.setattr(cm, "_load_scenario_engine", lambda *a: pytest.fail("scenario ran"))
    monkeypatch.setattr(cm.match_drama, "make_score_ceiling_hook", lambda *a: pytest.fail("score ceiling ran"))
    monkeypatch.setattr(cm.match_drama, "make_pitch_character_hook", lambda *a: pytest.fail("short pitch hook ran"))
    for innings in (1, 2):
        for over in range(1, 51):
            bowler = ready(state)
            assert bowler["roster_id"] != state["prev_bowler_rid"]
            assert cm.overs_left(state, bowler) > 0
            cm.simulate_over(state)
        assert cm.is_innings_over(state)
        assert cm.balls_bowled(state) == 300
        assert sum(x["balls"] for x in state["bowl_stats"].values()) == 300
        assert max(x["balls"] for x in state["bowl_stats"].values()) <= 60
        assert state["scenario"] is None
        with pytest.raises(odi.ODIUnavailable):
            cm.simulate_over(state)
        if innings == 1:
            cm.end_first_innings(state)
    assert cm.compute_result(state)["tie"]
    assert state["inn1_balls"] == 300


def test_all_out_on_tenth_wicket(monkeypatch, state):
    res = dict(outcome("Wicket"), type="wicket", batter_out=True, wicket_type="Bowled")
    monkeypatch.setattr(odi_approach, "bowl_delivery", lambda *a, **k: res)
    for _ in range(2):
        ready(state)
        cm.simulate_over(state)
    assert state["total_wickets"] == 10
    assert cm.balls_bowled(state) == 10


def test_wides_no_balls_and_joint_dismissals_preserve_scorecard(monkeypatch, state):
    script = iter((extra("Wide", 1, wicket="Stumped"),
                   extra("No Ball", 3, byes=2, wicket="Run Out"),
                   extra("Wide", 1), outcome()))
    free_hits = []

    def simulate(*_, free_hit, **kwargs):
        free_hits.append(free_hit)
        return deepcopy(next(script, outcome()))

    monkeypatch.setattr(odi_approach, "bowl_delivery", simulate)
    bowler = ready(state)
    summary = cm.simulate_over(state)
    stats = state["bowl_stats"][str(bowler["roster_id"])]
    assert free_hits[:5] == [False, False, True, True, False]
    assert stats["balls"] == cm.balls_bowled(state) == 6
    assert state["total_runs"] == state["extras_total"] == 5
    assert stats["runs"] == 3 and stats["wickets"] == 1 and state["total_wickets"] == 2
    assert summary["over_wickets"] == 2


def test_target_reached_on_wide_stops_at_zero_legal_balls(monkeypatch, state):
    monkeypatch.setattr(odi_approach, "bowl_delivery", lambda *a, **k: extra("Wide", 1))
    cm.end_first_innings(state)
    ready(state)
    cm.simulate_over(state)
    assert state["total_runs"] == 1 and cm.balls_bowled(state) == 0
    assert state["total_wickets"] == 0 and cm.compute_result(state)["winner"] == "Bowl"


def test_safety_pause_never_advances_a_partial_over(monkeypatch, state):
    script = iter(extra("Wide", 1) for _ in range(32))
    monkeypatch.setattr(odi_approach, "bowl_delivery", lambda *a, **k: next(script, outcome()))
    ready(state)
    result = cm.simulate_over(state)
    assert result["delivery_paused"] and cm.balls_bowled(state) == 0 and state["current_over"] == 1
    persisted = json.loads(json.dumps(state))
    cm.simulate_over(persisted)
    assert persisted["extras_total"] == 32 and cm.balls_bowled(persisted) == 6


def test_pause_resume_same_delivery_rng_and_fatigue(state):
    ready(state)
    saved = deepcopy(state)
    cm.simulate_over(state)
    while True:
        result = cm.simulate_over(saved, pause_after_delivery=True)
        saved = json.loads(json.dumps(saved))
        if not result.get("delivery_paused"):
            break
    for key in ("approach_log", "total_runs", "total_wickets", "bat_stats", "bowl_stats", "free_hit", "_rng_tick"):
        assert state[key] == saved[key]


def test_impact_is_disabled_and_bowler_options_are_private(state):
    from services.crickidex_arena import _approach_payload
    state["bowl_bench"] = [dict(state["bowl_xi"][0], roster_id=912345)]
    assert not impact_player.cipl_available_to(state, 2)
    own = _approach_payload(state, A_PICK_CIPL_BOWLER, "bowling")
    assert own["overBowlers"] and own["overBowlers"][0]["oversLeft"] == 10
    assert own["overBowlers"][0]["fatigue"] == "Fresh"
    spectator = _approach_payload(state, A_PICK_CIPL_BOWLER, None)
    assert spectator["overBowlers"] == []


def test_importer_dismissal_rules_and_precedence():
    ball = {"runs": {"batter": 0, "extras": 1, "total": 1},
            "extras": {"wides": 1}, "wickets": [{"kind": "stumped", "player_out": "Bat"}]}
    assert classify(ball)[0:3] == ("Extras", False, "wides")
    with pytest.raises(ValueError):
        classify(ball, True)
    ball["wickets"][0]["kind"] = "run out"
    assert classify(ball, True)[3][0]["kind"] == "run out"
    ball["wickets"][0]["kind"] = "retired hurt"
    assert classify(ball)[3] == []


def test_submit_pick_enforces_odi_role_surface_and_quota(monkeypatch, state):
    state["play_mode"] = "app"
    bowler = cm.eligible_bowlers(state)[0]["roster_id"]
    state["prev_bowler_rid"] = bowler
    monkeypatch.setattr(cipl_play, "_gs", AsyncMock(return_value=state))
    monkeypatch.setattr(cipl_play, "_get_next_action", AsyncMock(return_value=A_PICK_CIPL_BOWLER))
    saved = AsyncMock()
    monkeypatch.setattr(cipl_play, "_ss", saved)
    ctx = MagicMock(bot_data={})
    for user, source in ((999, "app"), (222, "chat"), (222, "app")):
        ok, reason = asyncio.run(cipl_play.submit_pick(
            ctx, 4242, user, cipl_play.PICK_BOWLER, bowler, source=source))
        assert not ok and reason
    saved.assert_not_called()


def test_odis_keep_separate_idempotent_stats_and_practice_skips(state):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from models import Match, ODIPlayerInnings, Player, PlayerGameStats, User
    from services.player_stats_service import persist_player_game_stats

    db = create_engine("sqlite://")
    for table in (User, Player, Match, PlayerGameStats, ODIPlayerInnings):
        table.__table__.create(db)
    with Session(db) as session:
        session.add_all([User(id=uid, telegram_id=111 * uid) for uid in (1, 2)])
        p = state["bat_xi"][0]
        session.add(Player(id=p["player_id"], name=p["name"], rating=82, category="Batsman",
                           country="Test", bat_hand="Right", bowl_hand="Right", bowl_style="Fast"))
        session.add(Match(id=4242, user1_id=1, user2_id=2, ball_format="ODI", overs=50))
        session.flush()
        state["bat_stats"][str(p["roster_id"])].update(balls=11, runs=18)
        assert persist_player_game_stats(session, {**state, "unranked": True})["skipped"] == "unranked"
        assert persist_player_game_stats(session, state)["batting"] == 1
        assert persist_player_game_stats(session, state)["batting"] == 0
        saved = session.scalars(select(ODIPlayerInnings)).one()
        assert saved.innings == 1 and saved.user_id == 1
        assert json.loads(saved.figures)["runs"] == 18
        assert not session.scalars(select(PlayerGameStats)).all()


def test_odi_first_innings_envelope_and_scorecard_label(monkeypatch, state):
    from services import match_webapp_service, score_projection
    ready(state)
    cm.simulate_over(state)
    projection = score_projection.project_first_innings(state)
    assert projection["expected"] > state["total_runs"]
    monkeypatch.setattr(match_webapp_service.mwa, "get_state", lambda mid: state)
    sc = match_webapp_service.build_scorecard(4242, 1)
    assert sc["ball_format"] == "ODI"
    assert sc["innings"][0]["extras"] == state["extras_total"]


def test_approach_wicket_and_run_intents_increase_across_rating_grid(state):
    for over, offset in product((5, 25, 45), (-15, 0, 15)):
        probe = deepcopy(state)
        probe["current_over"] = over
        ready(probe)
        probe["batting_order"][0]["bat_rating"] = 75 + offset
        model = odi_approach.delivery_model(probe)
        rates = [odi.expectations(odi_approach.probabilities(model, intent), model[0], model[1])
                 for intent in ("defensive", "rotate", "balanced", "aggressive", "ultra")]
        assert all(all(a < b for a, b in zip(left, right)) for left, right in zip(rates, rates[1:]))


def test_new_batsman_after_illegal_wicket_resumes_without_replaying(monkeypatch, state):
    script = iter((extra("No Ball", 1, wicket="Run Out"), outcome()))
    monkeypatch.setattr(odi_approach, "bowl_delivery", lambda *a, **k: deepcopy(next(script, outcome())))
    ready(state)
    result = cm.simulate_over(state, pause_on_wicket=True)
    assert result["paused"] and cm.needs_new_batsman(state)
    restored = json.loads(json.dumps(state))
    cm.bring_in_batsman(restored, cm.available_batsmen(restored)[-1]["roster_id"])
    cm.simulate_over(restored, pause_on_wicket=True)
    assert restored["total_wickets"] == 1
    assert restored["total_runs"] == 1 and cm.balls_bowled(restored) == 6
    assert restored["_rng_tick"] == 7


def test_bot_difficulty_reentry_creates_a_real_odi_draft(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("CMU_ODI_APPROACH_ENABLED", "1")
    monkeypatch.setattr(lpbot, "get_session", MagicMock())
    host = SimpleNamespace(id=1, telegram_id=111, username="Captain", first_name="Captain")
    guest = SimpleNamespace(id=2)
    monkeypatch.setattr(lpbot, "sync_telegram_user", lambda *args: host)
    monkeypatch.setattr(lpbot, "_active_letsplay_in_chat", lambda *args: False)
    monkeypatch.setattr(lpbot, "_active_match_in_chat", lambda *args: None)
    monkeypatch.setattr(lpbot, "_active_match_for_user", lambda *args: None)
    monkeypatch.setattr(lpbot, "_cric_lobby_for_user", lambda *args: None)
    monkeypatch.setattr(lpbot, "_get_ordered_roster", lambda *args: [object()] * 11)
    monkeypatch.setattr(lpbot, "validate_xi", lambda pairs: (True, []))
    monkeypatch.setattr(lpbot, "_bot_user", lambda session: guest)
    monkeypatch.setattr(lpbot, "_rearm_setup_timeout", lambda *args: None)
    pitch = AsyncMock()
    monkeypatch.setattr(lpbot, "_prompt_pitch", pitch)

    user = SimpleNamespace(id=111)
    msg = MagicMock(reply_text=AsyncMock())
    update = SimpleNamespace(effective_user=user, effective_chat=SimpleNamespace(id=-100),
                             effective_message=msg, callback_query=None)
    ctx = SimpleNamespace(args=["app", "50"], bot_data={}, user_data={})
    asyncio.run(lpbot.lpbot_handler(update, ctx))
    data = msg.reply_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    update.callback_query = MagicMock(data=data, answer=AsyncMock(), edit_message_text=AsyncMock())
    asyncio.run(botlevel.difficulty_callback(update, ctx))
    draft = pitch.await_args.args[1]
    assert draft["ball_format"] == "ODI" and draft["overs"] == 50
    assert draft["odi_settings"]["reference_sha256"] == odi.new_settings()["reference_sha256"]
    assert ctx.bot_data[letsplay._dkey(draft["invite_id"])] is draft
    assert botlevel.play_mode_for(ctx.bot_data, user.id) == "app"


def test_odi_bowling_candidate_requires_six_legal_balls_left(state):
    bowler = cm.eligible_bowlers(state)[0]
    state["current_over"] = 25
    state["bowl_stats"][str(bowler["roster_id"])]["balls"] = 59
    assert cm.overs_left(state, bowler) == 0
    assert bowler not in cm.eligible_bowlers(state)
