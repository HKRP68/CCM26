"""Measure tactical coverage, SQLite checkpoints and the Telegram turn loop."""

import argparse
import asyncio
from collections import Counter
from itertools import product
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from engine.realism import odi
from handlers import cipl_play
from models import MatchState
from services import bot_tactics, cipl_match as cm, match_state_store, odi_approach
from tools.calibrate_odi_approach import team
from tools.odi_reference import quantiles


def make_state(mid, settings, pitch="Hard", gap=0):
    state = cm.build_cipl_state(mid, 50, 1, 2, 111, 222, team(1, max(0, gap)),
                                team(2, max(0, -gap)), "Side 1", "Side 2", -100,
                                ball_format="ODI", pitch_type=pitch, odi_settings=settings)
    state["rng_seed"] = 261923
    return state


def tactical_sweep(settings):
    batting, bowling = Counter(), Counter()
    fatigue = {}
    for over, pitch, gap in product((5, 25, 45), ("Green", "Hard", "Dead"), (-14, 0, 14)):
        state = make_state(610001, settings, pitch, gap)
        state["current_over"] = over
        state["current_bowler"] = cm.eligible_bowlers(state)[0]
        matrix = bot_tactics.payoff_matrix(state)
        for row in matrix:
            bowling[bot_tactics.BOWL_KEYS[min(range(5), key=row.__getitem__)]] += 1
        for col in range(5):
            batting[bot_tactics.BAT_KEYS[max(range(5), key=lambda row: matrix[row][col])]] += 1
    state = make_state(610002, settings)
    bowler = state["current_bowler"] = cm.eligible_bowlers(state)[0]
    rid = bowler["roster_id"]
    state["current_over"] = 27
    for label, start in (("fresh", None), ("extended_spell", 13), ("after_rest", 5)):
        state["approach_log"] = ([] if start is None else
                                 [{"over": o, "innings": 1, "bowler_rid": rid, "balls": 6}
                                  for o in range(start, start + 14, 2)])
        state["bowl_stats"][str(rid)]["balls"] = 6 * len(state["approach_log"])
        model = odi_approach.delivery_model(state)
        runs, wickets = odi.expectations(odi_approach.probabilities(model), model[0], model[1])
        fatigue[label] = {"workload": vars(model[0].extra["workload"]),
                          "expected_runs_per_legal_ball": runs, "wickets_per_legal_ball": wickets}
    return {"contexts": 27, "responses_per_side": 135,
            "batting_best_responses": batting, "bowling_best_responses": bowling,
            "dominant_bowling_plan": any(n == 135 for n in bowling.values()), "fatigue": fatigue}


async def telegram_turns(settings, mode, engine):
    from contextlib import ExitStack

    mid = 620001 if mode == "chat" else 620002
    state = make_state(mid, settings)
    state["play_mode"] = mode
    counts = Counter()
    commits = []
    mem = {"action": match_state_store.A_PICK_CIPL_BOWLER}
    async def send(*args, **kwargs):
        counts["send"] += 1
        return SimpleNamespace(message_id=counts["send"])
    async def edit(*args, **kwargs):
        counts["edit"] += 1
    async def delete(*args, **kwargs):
        counts["delete"] += 1
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=send, edit_message_text=edit,
                                                delete_message=delete), bot_data={}, job_queue=None)
    async def save(_context, match_id, s, next_action=None, **kwargs):
        if next_action:
            mem["action"] = next_action
        start = perf_counter()
        match_state_store.save_state(ctx, match_id, s, next_action=next_action)
        commits.append((perf_counter() - start) * 1000)
    async def delivered(_context, match_id, s, _delivered, action):
        await save(ctx, match_id, s, next_action=action)
    async def finish(_context, _mid, s):
        if s["innings"] == 1:
            cm.end_first_innings(s)
            await cipl_play._prompt_bowler(ctx, mid, s)
    with ExitStack() as stack:
        def replace(module, name, value):
            stack.enter_context(patch.object(module, name, value))
        replace(match_state_store, "get_session", lambda: Session(engine))
        replace(cipl_play, "_gs", AsyncMock(return_value=state))
        replace(cipl_play, "_get_next_action", AsyncMock(side_effect=lambda *a: mem["action"]))
        replace(cipl_play, "_ss", save)
        replace(cipl_play, "_mark_prompt_delivered", delivered)
        replace(cipl_play, "_finish_innings", finish)
        replace(cipl_play, "_fire_milestones_async", lambda *a: None)
        replace(cipl_play, "_announce_drama_async", lambda *a: None)
        replace(cipl_play, "_with_match_controls", lambda *_args, **_kw: (None, _args[1]))
        replace(cipl_play.R, "rich_text_enabled", lambda: False)
        from services import league_records
        replace(league_records, "announce", AsyncMock())
        await cipl_play._prompt_bowler(ctx, mid, state)
        for _ in range(100):
            if cm.is_innings_over(state):
                break
            bowler = cm.eligible_bowlers(state)[0]
            for actor, kind, value in (
                    (state["bowl_user_tg"], cipl_play.PICK_BOWLER, bowler["roster_id"]),
                    (state["bowl_user_tg"], cipl_play.PICK_BOWL_APPROACH, 1),
                    (state["bat_user_tg"], cipl_play.PICK_BAT_APPROACH, 2)):
                accepted, error = await cipl_play.submit_pick(ctx, mid, actor, kind, value, source=mode)
                if not accepted:
                    raise RuntimeError(error)
    with Session(engine) as session:
        serialized = session.query(MatchState.state_json).filter_by(match_id=mid).scalar()
    return {
        "overs_started": len(state["inn1_approach_log"]) + len(state["approach_log"]),
        "telegram_operations": dict(counts), "sqlite_checkpoints": len(commits),
        "checkpoint_latency_ms": quantiles(commits),
        "final_checkpoint_bytes": len(serialized.encode()) if serialized else 0,
        "terminal": cm.is_innings_over(state),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    settings = odi.new_settings()
    with TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'checkpoints.db'}")
        MatchState.__table__.create(engine)
        report = {"tactics": tactical_sweep(settings),
                  "turn_loop": {mode: asyncio.run(telegram_turns(settings, mode, engine))
                                for mode in ("chat", "app")}}
        engine.dispose()
    report["limits"] = ("One seeded human-v-human match per surface. The real turn loop and local "
                        "SQLite checkpointing ran with a mock Telegram transport. Setup, milestones, "
                        "end-of-innings announcements and result posts are excluded from message counts.")
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
