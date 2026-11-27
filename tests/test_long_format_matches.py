"""Matches of 21 to 50 overs: the ODI engine scaled, CIPL entry, tagged
opponents and the separate ODI career record.

The 50-over ODI itself is covered by tests/test_odi_approach.py. These tests
pin the generalisation: that any length from 21 to 50 overs runs on the same
measured model with its phases, quotas and par curve scaled, that a league
command can start one, and that the innings lands in ``odi_player_innings``
rather than the short-format career table.
"""

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.realism import phases
from services import bot_captain, cipl_match as cm, odi_approach
from services import odi_stats_service as odi_stats
from services.match_formats import (
    CUSTOM_OVERS_MAX, MEDIUM_OVERS_MIN, ODI_OVERS,
    ball_format_for_overs, is_medium_format)
from tools.calibrate_odi_approach import team

MEDIUM_LENGTHS = (21, 25, 30, 35, 44, 50)


def build(overs, seed=11):
    state = cm.build_cipl_state(60000 + overs, overs, 1, 2, 111, 222,
                                team(1, 0), team(2, 0), "Bat", "Bowl", -100,
                                ball_format="ODI")
    state["rng_seed"] = seed
    return state


def play_innings(state, *, cap=None):
    """Bowl the innings out with the AI captain, asserting legality each over."""
    overs = state["overs"]
    previous, guard = None, 0
    while not cm.is_innings_over(state) and guard < overs + 10:
        guard += 1
        bowler = bot_captain.pick_bowler(state)
        assert bowler, f"no legal bowler at over {state['current_over']} of {overs}"
        assert bowler["roster_id"] != previous, "a bowler was given back-to-back overs"
        previous = bowler["roster_id"]
        state["current_bowler"] = bowler
        state["batting_approach"] = state["bowling_approach"] = "balanced"
        cm.simulate_over(state)
    assert cm.is_innings_over(state), f"a {overs}-over innings never ended"
    if cap is None:
        cap = cm.max_bowler_overs(state)
    for player in state["bowl_xi"]:
        used = state["bowl_stats"][str(player["roster_id"])]["balls"] // 6
        assert used <= cm.quota_for(state, player) <= cap
    return state


# ── The over count is the only switch ────────────────────────────────
@pytest.mark.parametrize("overs", range(1, 21))
def test_short_lengths_keep_the_t20_engine(overs):
    assert ball_format_for_overs(overs) == "T20"
    assert not is_medium_format(overs)


@pytest.mark.parametrize("overs", MEDIUM_LENGTHS)
def test_medium_lengths_are_odis(overs):
    assert ball_format_for_overs(overs) == "ODI"
    assert is_medium_format(overs)


def test_the_range_boundaries_are_the_documented_ones():
    assert (MEDIUM_OVERS_MIN, ODI_OVERS, CUSTOM_OVERS_MAX) == (21, 50, 50)
    assert not is_medium_format(MEDIUM_OVERS_MIN - 1)
    assert not is_medium_format(ODI_OVERS + 1)


# ── Phases scale, and the full ODI is unchanged ──────────────────────
def test_the_full_odi_phase_split_is_still_ball_60_and_240():
    assert phases.phase_for_ball(59, 300, "ODI") == "powerplay"
    assert phases.phase_for_ball(60, 300, "ODI") == "middle"
    assert phases.phase_for_ball(239, 300, "ODI") == "middle"
    assert phases.phase_for_ball(240, 300, "ODI") == "death"


@pytest.mark.parametrize("overs", MEDIUM_LENGTHS)
def test_every_length_has_all_three_phases_in_proportion(overs):
    balls = 6 * overs
    seen = [phases.phase_for_ball(b, balls, "ODI") for b in range(balls)]
    assert set(seen) == {"powerplay", "middle", "death"}
    # A fifth of the innings each end, as in a 50-over game.
    assert seen.count("powerplay") == pytest.approx(balls * 0.2, abs=6)
    assert seen.count("death") == pytest.approx(balls * 0.2, abs=6)
    assert odi_approach.phase_windows(balls) == (round(balls * 0.2), round(balls * 0.8))


@pytest.mark.parametrize("overs", MEDIUM_LENGTHS)
def test_the_state_reports_its_own_length(overs):
    state = build(overs)
    assert odi_approach.innings_overs(state) == overs
    assert odi_approach.innings_balls(state) == 6 * overs
    assert cm.total_balls(state) == 6 * overs
    assert odi_approach.context(state).innings_balls == 6 * overs
    assert cm.approach_phase({**state, "current_over": 1}) == "powerplay"
    assert cm.approach_phase({**state, "current_over": overs}) == "death"


@pytest.mark.parametrize("overs", MEDIUM_LENGTHS)
def test_the_label_names_the_length(overs):
    expected = ("ODI · 50 overs" if overs == ODI_OVERS
                else f"ODI rules · {overs} overs")
    assert cm.format_label(build(overs)) == expected


# ── Quotas and XI capacity scale with the length ─────────────────────
@pytest.mark.parametrize("overs,quota", [(21, 5), (25, 5), (30, 6), (35, 7),
                                         (44, 9), (50, 10)])
def test_the_bowler_quota_is_a_fifth_of_the_innings(overs, quota):
    state = build(overs)
    assert cm.max_bowler_overs(state) == quota
    specialists = [p for p in state["bowl_xi"] if not cm.is_part_time_bowler(p)]
    assert specialists and all(cm.overs_left(state, p) == quota for p in specialists)


@pytest.mark.parametrize("overs", MEDIUM_LENGTHS)
def test_an_xi_is_only_valid_when_it_can_bowl_the_innings_out(overs):
    assert odi_approach.valid_xi(team(1, 0), overs)
    # An XI of eleven pure batsmen only has the part-time quota each, so its
    # whole capacity is 11 x PART_TIME_MAX_OVERS: enough for a 21- or 22-over
    # innings, and short of anything longer.
    batsmen = [{**p, "category": "Batsman"} for p in team(1, 0)]
    capacity = 11 * min(cm.PART_TIME_MAX_OVERS, cm.max_bowler_overs({"overs": overs}))
    assert odi_approach.valid_xi(batsmen, overs) is (capacity >= overs)


def test_an_xi_short_of_bowling_cannot_take_a_full_odi():
    batsmen = [{**p, "category": "Batsman"} for p in team(1, 0)]
    assert not odi_approach.valid_xi(batsmen, ODI_OVERS)
    assert not odi_approach.valid_xi(team(1, 0)[:10], ODI_OVERS)


# ── A whole innings, at every length ─────────────────────────────────
@pytest.mark.parametrize("overs", MEDIUM_LENGTHS)
def test_an_innings_completes_legally_and_scores_sensibly(overs):
    state = play_innings(build(overs))
    runs = state["total_runs"]
    # Between two and nine runs an over — a real limited-overs envelope, and
    # wide enough that this never becomes a calibration test.
    assert 2 * overs <= runs <= 9 * overs, f"{runs} off {overs} overs"
    assert cm.balls_bowled(state) <= 6 * overs


@pytest.mark.parametrize("overs", (25, 40))
def test_a_chase_completes_and_the_forecast_is_a_probability(overs):
    first = play_innings(build(overs, seed=5))
    target = first["total_runs"] + 1
    second = build(overs, seed=9)
    second["innings"], second["target"] = 2, target
    # In the first innings the forecast is a projected total; chasing, it is
    # the win chance.
    assert odi_approach.forecast(build(overs)) >= 0
    assert 0.0 <= odi_approach.forecast(second) <= 1.0
    play_innings(second)
    # The chase ends because the innings ran out, the side was bowled out, or
    # the target was passed — never because the loop gave up.
    assert (cm.balls_bowled(second) == 6 * overs
            or second["total_wickets"] >= second["wicket_limit"]
            or second["total_runs"] >= target)


@pytest.mark.parametrize("overs", MEDIUM_LENGTHS)
def test_the_par_curve_follows_the_innings_length(overs):
    from engine.realism import odi
    state = build(overs)
    ctx = odi_approach.context(state)
    par = odi_approach.mean_remaining(ctx.innings_balls, 10, ctx, odi.settings(state))
    assert 2 * overs <= par <= 9 * overs


def test_par_rises_with_the_innings_length():
    from engine.realism import odi
    pars = []
    for overs in MEDIUM_LENGTHS:
        state = build(overs)
        ctx = odi_approach.context(state)
        pars.append(odi_approach.mean_remaining(
            ctx.innings_balls, 10, ctx, odi.settings(state)))
    assert pars == sorted(pars)


# ── Refusals ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("overs", (20, 51))
def test_an_odi_cannot_be_built_outside_the_range(overs):
    from engine.realism import odi
    with pytest.raises(odi.ODIUnavailable):
        cm.build_cipl_state(1, overs, 1, 2, 111, 222, team(1, 0), team(2, 0),
                            "Bat", "Bowl", -100, ball_format="ODI")


@pytest.mark.parametrize("overs", (20, 51))
def test_the_engine_format_refuses_an_out_of_range_odi(overs):
    from engine.realism import odi
    with pytest.raises(odi.ODIUnavailable):
        cm.approach_engine_format({"ball_format": "ODI", "overs": overs})


# ── Live surfaces read the real length ───────────────────────────────
@pytest.mark.parametrize("overs,quota", [(25, 5), (30, 6), (50, 10)])
def test_the_arena_payload_shows_the_scaled_quota(overs, quota):
    from services.crickidex_arena import _approach_payload
    from services.match_state_store import A_PICK_CIPL_BOWLER
    from services import impact_player

    state = build(overs)
    # ODI rules: no Impact Player at any length.
    assert not impact_player.cipl_available_to(state, 2)
    assert impact_player.cipl_bench_for(state, "bowl") == []
    payload = _approach_payload(state, A_PICK_CIPL_BOWLER, "bowling")
    assert payload["overBowlers"]
    assert payload["overBowlers"][0]["oversLeft"] == quota
    assert payload["overBowlers"][0]["fatigue"] == "Fresh"


@pytest.mark.parametrize("overs", (25, 50))
def test_the_scorecard_payload_carries_the_length(monkeypatch, overs):
    from services import match_webapp_service
    state = play_innings(build(overs))
    state["match_id"] = 4242
    monkeypatch.setattr(match_webapp_service.mwa, "get_state", lambda mid: state)
    card = match_webapp_service.build_scorecard(4242, 1)
    assert card["ball_format"] == "ODI"
    assert card["total_overs"] == overs
    assert card["innings"][0]["extras"] == state["extras_total"]


@pytest.mark.parametrize("overs", (25, 50))
def test_the_first_innings_projection_is_in_range(overs):
    from services import score_projection
    state = build(overs)
    ready = bot_captain.pick_bowler(state)
    assert ready
    state["current_bowler"] = ready
    state["batting_approach"] = state["bowling_approach"] = "balanced"
    cm.simulate_over(state)
    projection = score_projection.project_first_innings(state)
    assert projection["expected"] > state["total_runs"]
    assert projection["expected"] <= 9 * overs


# ── The separate ODI career record ───────────────────────────────────
def _stats_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from models import Match, ODIPlayerInnings, Player, PlayerGameStats, User

    db = create_engine("sqlite://")
    for table in (User, Player, Match, PlayerGameStats, ODIPlayerInnings):
        table.__table__.create(db)
    return Session(db)


@pytest.mark.parametrize("overs", (21, 35, 50))
def test_a_long_innings_is_recorded_as_an_odi_not_a_t20(overs):
    from sqlalchemy import select
    from models import Match, ODIPlayerInnings, Player, PlayerGameStats, User
    from services.player_stats_service import persist_player_game_stats

    state = build(overs)
    state["match_id"] = 4242
    with _stats_session() as session:
        session.add_all([User(id=uid, telegram_id=111 * uid) for uid in (1, 2)])
        player = state["bat_xi"][0]
        session.add(Player(id=player["player_id"], name=player["name"], rating=82,
                           category="Batsman", country="Test", bat_hand="Right",
                           bowl_hand="Right", bowl_style="Fast"))
        session.add(Match(id=4242, user1_id=1, user2_id=2, ball_format="ODI", overs=overs))
        session.flush()
        state["bat_stats"][str(player["roster_id"])].update(
            balls=61, runs=74, fours=6, sixes=2)
        assert persist_player_game_stats(session, state)["batting"] == 1
        # Idempotent: the same state saved twice adds nothing.
        assert persist_player_game_stats(session, state)["batting"] == 0
        row = session.scalars(select(ODIPlayerInnings)).one()
        assert json.loads(row.figures)["runs"] == 74
        # And nothing landed in the short-format career table.
        assert not session.scalars(select(PlayerGameStats)).all()


def test_a_real_simulated_innings_lands_in_the_odi_career():
    """End to end: bowl a 21-over innings, save it, read the record back."""
    from models import Match, Player, User
    from services.player_stats_service import persist_player_game_stats

    state = play_innings(build(21, seed=31))
    state["match_id"] = 777
    with _stats_session() as session:
        session.add_all([User(id=uid, telegram_id=111 * uid) for uid in (1, 2)])
        for player in state["bat_xi"] + state["bowl_xi"]:
            session.add(Player(
                id=player["player_id"], name=player["name"], rating=80,
                category=player.get("category") or "Batsman", country="Test",
                bat_hand="Right", bowl_hand="Right", bowl_style="Fast"))
        session.add(Match(id=777, user1_id=1, user2_id=2, ball_format="ODI", overs=21))
        session.flush()
        counts = persist_player_game_stats(session, state)
        assert counts["batting"] and counts["bowling"]

        # Every batter who faced a ball has a career innings, and the runs add
        # up to the team total minus extras.
        careers = odi_stats.squad_careers(session, 1)
        assert careers
        total = sum(c["batting"]["runs"] for c in careers.values())
        assert total == state["total_runs"] - state["extras_total"]
        bowling = odi_stats.squad_careers(session, 2)
        assert sum(c["bowling"]["wickets"] for c in bowling.values()) <= 10
        assert sum(c["bowling"]["balls"] for c in bowling.values()) == cm.balls_bowled(state)


def test_the_career_fold_reads_the_saved_innings():
    from models import ODIPlayerInnings, Player, User
    with _stats_session() as session:
        session.add_all([User(id=1, telegram_id=111),
                         Player(id=7, name="Tester", rating=80, category="All-rounder",
                                country="Test", bat_hand="Right", bowl_hand="Right",
                                bowl_style="Fast")])
        rows = [
            (1, 1, "bat", {"runs": 112, "balls": 99, "fours": 9, "sixes": 4, "out": True}),
            (2, 1, "bat", {"runs": 61, "balls": 70, "fours": 4, "sixes": 1, "out": False}),
            (3, 1, "bat", {"runs": 0, "balls": 3, "out": True}),
            (1, 2, "bowl", {"balls": 54, "runs": 41, "wickets": 4, "maidens": 1}),
            (2, 2, "bowl", {"balls": 60, "runs": 52, "wickets": 2}),
        ]
        for match_id, innings, discipline, figures in rows:
            session.add(ODIPlayerInnings(
                match_id=match_id, innings=innings, discipline=discipline,
                user_id=1, player_id=7, figures=json.dumps(figures)))
        session.flush()
        record = odi_stats.career(session, 7, 1)

    assert odi_stats.has_record(record)
    assert record["matches"] == 3
    bat = record["batting"]
    assert (bat["innings"], bat["runs"], bat["balls"]) == (3, 173, 172)
    assert (bat["hundreds"], bat["fifties"], bat["ducks"]) == (1, 1, 1)
    assert (bat["outs"], bat["not_outs"]) == (2, 1)
    assert record["highest"] == "112"
    assert record["batting_average"] == round(173 / 2, 2)
    assert record["strike_rate"] == round(100 * 173 / 172, 2)
    bowl = record["bowling"]
    assert (bowl["innings"], bowl["wickets"], bowl["runs"]) == (2, 6, 93)
    assert bowl["three_fers"] == 1 and bowl["five_fers"] == 0
    assert record["best_bowling"] == "4/41"
    assert record["overs"] == "19"
    assert record["economy"] == round(6 * 93 / 114, 2)
    assert "BATTING" in odi_stats.record_block(record)


def test_a_not_out_best_score_keeps_its_star():
    record = odi_stats.fold_batting([
        {"runs": 88, "balls": 70, "out": True},
        {"runs": 88, "balls": 64, "out": False},
    ])
    assert record["highest"] == 88 and record["highest_not_out"]
    assert odi_stats.highest_text(record) == "88*"


def test_an_empty_record_prints_dashes_rather_than_dividing_by_zero():
    bat = dict(odi_stats._EMPTY_BATTING)
    bowl = dict(odi_stats._EMPTY_BOWLING)
    assert odi_stats.batting_average(bat) is None
    assert odi_stats.strike_rate(bat) is None
    assert odi_stats.economy(bowl) is None
    assert odi_stats.bowling_average(bowl) is None
    assert odi_stats.bowling_strike_rate(bowl) is None
    assert odi_stats.highest_text(bat) == "-"
    assert odi_stats.best_bowling_text(bowl) == "-"
    assert not odi_stats.has_record({"batting": bat, "bowling": bowl})
    # A batter who has never been out still shows an average of "-", not a
    # division by zero, and the block renders.
    unbeaten = odi_stats.fold_batting([{"runs": 40, "balls": 30, "out": False}])
    assert odi_stats.batting_average(unbeaten) is None


def test_unreadable_figures_do_not_break_the_fold():
    assert odi_stats.fold_batting(["not json", None, ""]) == odi_stats._EMPTY_BATTING


def test_overs_text():
    assert [odi_stats.overs_text(n) for n in (0, 1, 6, 9, 60)] == \
        ["0", "0.1", "1", "1.3", "10"]


# ── The ODI leaderboards ─────────────────────────────────────────────
def _leaderboard_session():
    """A session holding two owners' ODI innings for four players."""
    from models import ODIPlayerInnings, Player, User
    session = _stats_session()
    session.add_all([
        User(id=1, telegram_id=111, username="skipper"),
        User(id=2, telegram_id=222, username="rival"),
    ])
    for pid, name, country in ((1, "Opener", "India"), (2, "Anchor", "England"),
                               (3, "Seamer", "Australia"), (4, "Spinner", "India")):
        session.add(Player(id=pid, name=name, rating=85, category="All-rounder",
                           country=country, bat_hand="Right", bowl_hand="Right",
                           bowl_style="Fast"))
    # (match, owner, player, discipline, figures)
    rows = [
        (1, 1, 1, "bat", {"runs": 120, "balls": 100, "out": True}),
        (2, 1, 1, "bat", {"runs": 80, "balls": 70, "out": True}),
        (3, 1, 1, "bat", {"runs": 40, "balls": 30, "out": False}),
        (1, 2, 2, "bat", {"runs": 150, "balls": 140, "out": True}),
        (2, 2, 2, "bat", {"runs": 10, "balls": 20, "out": True}),
        # One big innings, below the qualifying bar for the rate boards.
        (3, 2, 4, "bat", {"runs": 99, "balls": 40, "out": False}),
        (1, 1, 3, "bowl", {"balls": 60, "runs": 30, "wickets": 5}),
        (2, 1, 3, "bowl", {"balls": 60, "runs": 40, "wickets": 2}),
        (3, 1, 3, "bowl", {"balls": 60, "runs": 35, "wickets": 1}),
        (1, 2, 4, "bowl", {"balls": 60, "runs": 25, "wickets": 5}),
        (2, 2, 4, "bowl", {"balls": 60, "runs": 20, "wickets": 3}),
    ]
    for match_id, user_id, player_id, discipline, figures in rows:
        session.add(ODIPlayerInnings(
            match_id=match_id, innings=1 if discipline == "bat" else 2,
            discipline=discipline, user_id=user_id, player_id=player_id,
            figures=json.dumps(figures)))
    session.flush()
    odi_stats.reset_leaderboard_cache()
    return session


def _top(session, metric):
    return [(r["player_id"], r["text"])
            for r in odi_stats.leaders(session, metric, 10, fresh=True)]


def test_the_runs_board_ranks_careers_not_innings():
    with _leaderboard_session() as session:
        assert _top(session, "runs")[0] == (1, "240 (3 inns)")
        # Player 2's 160 from two innings is second; player 4's single 99 last.
        assert [pid for pid, _ in _top(session, "runs")] == [1, 2, 4]


def test_the_wickets_board_shows_overs_bowled():
    with _leaderboard_session() as session:
        board = _top(session, "wickets")
    assert board[0][0] in (3, 4)
    assert "ov" in board[0][1]
    assert {pid for pid, _ in board} == {3, 4}


def test_the_highest_score_board_is_a_single_innings():
    with _leaderboard_session() as session:
        board = _top(session, "highest")
    assert board[0] == (2, "150")
    assert (4, "99*") in board          # unbeaten, so starred


def test_best_bowling_prefers_wickets_then_fewer_runs():
    with _leaderboard_session() as session:
        board = _top(session, "best_bowling")
    assert board[0] == (4, "5/25")      # 5/25 beats 5/30
    assert board[1] == (3, "5/30")


def test_the_rate_boards_hold_a_qualifying_bar():
    with _leaderboard_session() as session:
        for metric in ("batting_average", "strike_rate"):
            pids = [pid for pid, _ in _top(session, metric)]
            # Player 4 has one innings; the bar is MIN_BATTING_INNINGS.
            assert 4 not in pids, metric
            assert 1 in pids, metric
        econ = [pid for pid, _ in _top(session, "economy")]
        # Player 4 has two bowling innings — below MIN_BOWLING_INNINGS.
        assert econ == [3]


def test_the_economy_board_puts_the_cheapest_first():
    from models import ODIPlayerInnings
    with _leaderboard_session() as session:
        session.add(ODIPlayerInnings(
            match_id=9, innings=2, discipline="bowl", user_id=2, player_id=4,
            figures=json.dumps({"balls": 60, "runs": 60, "wickets": 0})))
        session.flush()
        board = odi_stats.leaders(session, "economy", 10, fresh=True)
    # Player 3 concedes 105 off 30 overs (3.50); player 4, 105 off 30 (3.50)…
    # both qualify now, and whichever is cheaper leads.
    assert len(board) == 2
    economies = [row["record"]["economy"] for row in board]
    assert economies == sorted(economies)


def test_an_unknown_metric_is_an_empty_board_not_an_error():
    with _leaderboard_session() as session:
        assert odi_stats.leaders(session, "catches", 10) == []


def test_every_metric_has_a_title_and_a_place_in_the_order():
    assert set(odi_stats.METRIC_ORDER) == set(odi_stats.METRICS)
    for metric in odi_stats.METRIC_ORDER:
        assert odi_stats.metric_title(metric)
    assert odi_stats.metric_title("nope") == "ODI records"


def test_the_fold_is_cached_and_can_be_dropped():
    from models import ODIPlayerInnings
    with _leaderboard_session() as session:
        before = _top(session, "runs")[0]
        session.add(ODIPlayerInnings(
            match_id=8, innings=1, discipline="bat", user_id=2, player_id=2,
            figures=json.dumps({"runs": 300, "balls": 200, "out": True})))
        session.flush()
        # The cached fold has not seen it.
        assert odi_stats.leaders(session, "runs", 10)[0]["player_id"] == before[0]
        odi_stats.reset_leaderboard_cache()
        assert odi_stats.leaders(session, "runs", 10)[0]["player_id"] == 2


def test_the_board_renders_with_names_owners_and_flags():
    from handlers import odirecords
    with _leaderboard_session() as session:
        text = odirecords.board_text(session, "runs")
    assert "ODI RECORDS" in text and "Most runs" in text
    assert "Opener" in text and "@skipper" in text
    assert "21 overs or more" in text


def test_an_empty_board_says_how_to_fill_it():
    from handlers import odirecords
    odi_stats.reset_leaderboard_cache()
    with _stats_session() as session:
        text = odirecords.board_text(session, "runs")
    assert "No qualifying ODI innings yet" in text and "/lp 50" in text


def test_the_hall_of_fame_gains_odi_career_boards():
    """A 50-over career is invisible to the short-format boards, so it gets
    boards of its own rather than being merged into them."""
    from services import hall_of_fame
    with _leaderboard_session() as session:
        boards = hall_of_fame._odi_career_boards(session, 5)
    titles = [title for title, _emoji, _rows in boards]
    assert titles == ["Most ODI runs by one card", "Most ODI wickets by one card"]
    runs_rows = boards[0][2]
    assert runs_rows and "Opener" in runs_rows[0][1]
    assert "240" in runs_rows[0][0]


def test_the_odi_hall_of_fame_boards_are_absent_with_no_odis():
    from services import hall_of_fame
    odi_stats.reset_leaderboard_cache()
    with _stats_session() as session:
        assert hall_of_fame._odi_career_boards(session, 5) == []


def test_the_odi_hall_of_fame_boards_skip_non_humans():
    """The short-format boards are humans only; so are these."""
    from models import User
    from services import hall_of_fame
    with _leaderboard_session() as session:
        # telegram_id is unique, so give each a distinct non-human id.
        for offset, user in enumerate(session.query(User).order_by(User.id).all()):
            user.telegram_id = -1 - offset
        session.flush()
        odi_stats.reset_leaderboard_cache()
        assert hall_of_fame._odi_career_boards(session, 5) == []


def test_the_board_keyboard_marks_the_one_being_shown():
    from handlers import odirecords
    markup = odirecords.board_keyboard("wickets")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert sum(1 for label in labels if label.startswith("• ")) == 1
    assert any(label.startswith("• ") and "wickets" in label.lower()
               for label in labels)
    assert len(labels) == len(odi_stats.METRIC_ORDER)
    data = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert data == {f"odirec_{m}" for m in odi_stats.METRIC_ORDER}


# ── The /odistats command ────────────────────────────────────────────
def test_the_record_sums_every_edition_of_a_card():
    """A re-released card's ODI record is the sum over its editions."""
    from models import ODIPlayerInnings, Player, User
    from handlers.odistats import edition_ids
    with _stats_session() as session:
        session.add(User(id=1, telegram_id=111))
        base = Player(id=7, name="Tester", rating=80, category="Batsman",
                      country="Test", bat_hand="Right", bowl_hand="Right",
                      bowl_style="Fast")
        rerelease = Player(id=8, name="Tester", rating=86, category="Batsman",
                           country="Test", bat_hand="Right", bowl_hand="Right",
                           bowl_style="Fast", parent_player_id=7, version="ODI 2026")
        session.add_all([base, rerelease])
        for pid, runs in ((7, 40), (8, 75)):
            session.add(ODIPlayerInnings(
                match_id=pid, innings=1, discipline="bat", user_id=1,
                player_id=pid, figures=json.dumps(
                    {"runs": runs, "balls": runs, "out": True})))
        session.flush()
        assert sorted(edition_ids(session, rerelease)) == [7, 8]
        merged = odi_stats.careers(session, edition_ids(session, rerelease), 1)
        only_base = odi_stats.career(session, 7, 1)
    assert merged["batting"]["runs"] == 115 and merged["batting"]["innings"] == 2
    assert merged["highest"] == "75"
    assert only_base["batting"]["runs"] == 40


def test_an_empty_edition_list_reads_as_no_record():
    blank = odi_stats.record(dict(odi_stats._EMPTY_BATTING),
                             dict(odi_stats._EMPTY_BOWLING))
    assert not odi_stats.has_record(blank)
    assert odi_stats.record_block(blank)


def test_odistats_scope_parsing():
    from handlers.odistats import split_scope
    assert split_scope(["Virat", "Kohli"]) == ("Virat Kohli", False)
    assert split_scope(["Virat", "Kohli", "global"]) == ("Virat Kohli", True)
    assert split_scope(["Rohit", "ALL"]) == ("Rohit", True)
    assert split_scope([]) == ("", False)


def test_odistats_asks_for_a_name_when_given_none():
    from handlers import odistats
    message = MagicMock(reply_text=AsyncMock())
    update = MagicMock(effective_message=message, effective_user=MagicMock(id=1))
    asyncio.run(odistats.odistats_handler(update, MagicMock(args=[])))
    assert "odistats" in message.reply_text.await_args.args[0]


def test_odistats_renders_a_record():
    from handlers.odistats import record_text
    player = NS(name="Tester", country="India", rating=84, category="All-rounder")
    record = {
        "matches": 4,
        "batting": {**odi_stats._EMPTY_BATTING, "innings": 4, "runs": 210,
                    "balls": 198, "fours": 18, "sixes": 5, "outs": 3,
                    "not_outs": 1, "fifties": 2, "highest": 96},
        "bowling": {**odi_stats._EMPTY_BOWLING, "innings": 3, "balls": 150,
                    "runs": 120, "wickets": 5, "best_wickets": 3, "best_runs": 28},
        "batting_average": 70.0, "strike_rate": 106.06, "economy": 4.8,
        "bowling_average": 24.0, "bowling_strike_rate": 30.0,
        "highest": "96", "best_bowling": "3/28", "overs": "25",
    }
    text = record_text(player, record, owner_label="@tester")
    assert "ODI career" in text and "@tester" in text
    assert "Matches: 4" in text and "3/28" in text


# ── The 🥇 ODI flip on the career cards ──────────────────────────────
def test_the_stats_card_offers_the_other_format():
    from handlers.team import odi_flip_keyboard
    short = odi_flip_keyboard(777, 5, "short")
    assert [b.text for r in short.inline_keyboard for b in r] == ["🥇 ODI record"]
    assert short.inline_keyboard[0][0].callback_data == "stfmt_777_5_odi"
    odi = odi_flip_keyboard(777, 5, "odi")
    assert [b.text for r in odi.inline_keyboard for b in r] == ["◀ Short formats"]
    assert odi.inline_keyboard[0][0].callback_data == "stfmt_777_5_short"


def test_the_short_format_record_builder_is_shared():
    """One layout, used by /stats and by the flip back to it."""
    from handlers.team import _blank_career_row, career_record_text
    player = NS(name="Tester", rating=84, category="All-rounder")
    gs = _blank_career_row(1, 1)
    gs.bat_inns, gs.runs, gs.fifties = 4, 200, 2
    gs.fours, gs.sixes, gs.balls_faced, gs.times_out = 18, 6, 150, 3
    gs.highest_score, gs.potm = 88, 1
    text = career_record_text(player, "🇮🇳", "@owner", 1000, gs)
    assert "BATTING" in text and "BOWLING" in text
    assert "@owner" in text and "Tester" in text
    assert "Runs: 200" in text


def test_a_never_played_card_renders_rather_than_raising():
    """An ORM row that was never inserted has None in every defaulted column,
    and the record layout formats those with a width."""
    from handlers.team import _blank_career_row, career_record_text
    player = NS(name="Rookie", rating=60, category="Batsman")
    text = career_record_text(player, "", "@owner", 500, _blank_career_row(1, 2))
    assert "Inns: 0" in text and "Runs: 0" in text


def test_the_stats_flip_renders_an_owners_odi_record():
    from handlers.team import odi_record_text
    from models import ODIPlayerInnings, Player, User
    with _stats_session() as session:
        session.add_all([
            User(id=1, telegram_id=777, username="skip"),
            Player(id=5, name="Tester", rating=89, category="Wicket Keeper",
                   country="England", bat_hand="Right", bowl_hand="Right",
                   bowl_style="Medium"),
        ])
        for match_id, figures in ((1, {"runs": 101, "balls": 92, "out": True}),
                                  (2, {"runs": 55, "balls": 48, "out": False})):
            session.add(ODIPlayerInnings(
                match_id=match_id, innings=1, discipline="bat", user_id=1,
                player_id=5, figures=json.dumps(figures)))
        session.flush()
        user = session.get(User, 1)
        text = odi_record_text(session, user, session.get(Player, 5), "@skip")
    assert "ODI record" in text and "@skip" in text
    assert "Matches: 2" in text
    assert "Runs: 156" in text and "HS: 101" in text


def test_the_stats_flip_says_so_when_there_is_no_odi_record():
    from handlers.team import odi_record_text
    from models import Player, User
    with _stats_session() as session:
        session.add_all([
            User(id=1, telegram_id=777, username="skip"),
            Player(id=5, name="Tester", rating=70, category="Batsman",
                   country="India", bat_hand="Right", bowl_hand="Right",
                   bowl_style="Medium"),
        ])
        session.flush()
        text = odi_record_text(session, session.get(User, 1),
                               session.get(Player, 5), "@skip")
    assert "No ODI innings yet" in text and "/lp 50" in text


def test_the_gstats_card_offers_the_odi_view():
    from handlers import gstats
    markup = gstats._keyboard(777, 5, 0, 1, "stats")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "🥇 ODI" in labels and "👥 Owners" in labels
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "gstodi_777_5_0" in data


def test_the_gstats_odi_view_sums_every_owner():
    from handlers import gstats
    from models import ODIPlayerInnings, Player
    with _stats_session() as session:
        session.add(Player(id=5, name="Tester", rating=88, category="All-rounder",
                           country="India", bat_hand="Right", bowl_hand="Right",
                           bowl_style="Fast"))
        for user_id, runs in ((1, 70), (2, 90)):
            session.add(ODIPlayerInnings(
                match_id=user_id, innings=1, discipline="bat", user_id=user_id,
                player_id=5, figures=json.dumps({"runs": runs, "balls": runs,
                                                 "out": True})))
        session.flush()
        player = session.get(Player, 5)
        pages = gstats._build_pages([player])
        text = gstats._render_odi(session, player, [player], pages, 0)
    assert "ODI STATS" in text and "21 overs or more" in text
    # Both owners' innings, summed — the long-format twin of /gstats.
    assert "Runs: 160" in text
    assert "/odirecords" in text


def test_the_gstats_odi_view_is_honest_when_empty():
    from handlers import gstats
    from models import Player
    with _stats_session() as session:
        session.add(Player(id=5, name="Tester", rating=60, category="Batsman",
                           country="India", bat_hand="Right", bowl_hand="Right",
                           bowl_style="Medium"))
        session.flush()
        player = session.get(Player, 5)
        text = gstats._render_odi(session, player, [player],
                                  gstats._build_pages([player]), 0)
    assert "No ODI has been played" in text


# ── The difficulty prompt carries a shortened ODI's length ───────────
@pytest.mark.parametrize("token,expected", [
    ("ODI", "🏏 ODI · 50 overs\n"), ("35", "🏏 ODI rules · 35 overs\n"),
    ("21", "🏏 ODI rules · 21 overs\n"), ("6", ""), ("", ""),
])
def test_bot_prompt_banner(token, expected):
    from handlers.botlevel import length_banner
    assert length_banner("lp", token) == expected
    assert length_banner("cipl", token) == ""
