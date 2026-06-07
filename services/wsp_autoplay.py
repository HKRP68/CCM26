"""Auto-simulation engine for /wsp and /wspbot matches.

/wsp and /wspbot are "watch-mode" matches — the engine drives ALL actions
(delivery, shot, new batsman, new bowler) automatically. Players and spectators
watch the match unfold in the Mini App (via polling) and receive over-by-over
commentary + final scorecard images in the Telegram chat.

The core entry point is ``run_wsp_simulation``, an async coroutine scheduled
as a background task immediately after the match is initialized.
"""

import asyncio
import logging

from database import get_session
from services import match_webapp_access as mwa
from services.match_engine import (
    get_striker, get_bowler, is_innings_over,
    transition_to_second_innings, compute_match_result,
)
from services.match_state_store import (
    A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT,
    A_PICK_NEW_BATSMAN, A_PICK_NEW_BOWLER, A_COMPLETED,
)
from services.match_webapp_service import (
    _active_players, _apply_outcome, _append_commentary_log, SETUP_DONE,
)

logger = logging.getLogger(__name__)

# Seconds between balls (controls match pace visible to spectators)
BALL_DELAY = 1.2
# Seconds pause at end-of-over before resuming
OVER_PAUSE = 2.0
# Seconds pause at innings break before 2nd innings starts
INNINGS_PAUSE = 4.0


# ──────────────────────────────────────────────────────────────────
# Synchronous step: advance one complete ball (delivery + shot → outcome)
# Handles all intermediate transitions (new batsman, new bowler) first.
# Returns a dict describing what happened, or None if match is done.
# ──────────────────────────────────────────────────────────────────

def _wsp_advance_step(match_id):
    """Advance WSP match by one legal ball + any bookkeeping transitions.

    Returns dict:
      {
        "rtxt": str,          # outcome text e.g. "SIX! 💥"
        "type": str,          # "wicket" / "wide" / "4" etc.
        "runs": int,
        "eoo": bool,          # end of over?
        "need_new_bat": bool,
        "innings_over": bool, # innings ended?
        "match_done": bool,
        "striker": dict,
        "bowler": dict,
        "state": dict,        # current state snapshot after update
      }
    or None if already completed / state missing.
    """
    import handlers.match as _bm
    from services import bot_ai
    from services.bowling_service import AVAILABLE_SHOTS

    # ── Flush intermediate transitions first ───────────────────────
    for _ in range(30):
        state = mwa.get_state(match_id)
        na = mwa.get_next_action(match_id)
        if not state or na == A_COMPLETED:
            return None

        if na == A_PICK_NEW_BOWLER:
            new_bowler = bot_ai.pick_bot_next_bowler(
                _active_players(state["bowl_xi"]), state.get("prev_bowler_rid"),
                state["bowl_stats"], state["overs"])
            state["current_bowler"] = new_bowler
            mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            continue

        if na == A_PICK_NEW_BATSMAN:
            nb = state.get("next_batsman_idx", 2)
            order = state.get("batting_order", [])
            if nb < len(order):
                state["striker_idx"] = nb
                state["next_batsman_idx"] = nb + 1
            mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            continue

        break  # reached PICK_DELIVERY or PICK_SHOT

    state = mwa.get_state(match_id)
    na = mwa.get_next_action(match_id)
    if not state or na == A_COMPLETED:
        return None

    if na not in (A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT):
        return None

    # ── Auto-pick delivery ─────────────────────────────────────────
    if na in (A_PICK_DELIVERY, A_PICK_LENGTH):
        bowler = get_bowler(state)
        over = state.get("current_over", 1)
        total = state.get("overs", 1)
        pick = bot_ai.pick_bot_delivery(
            bowler, over, total, difficulty="Medium")
        state["current_delivery"] = pick["delivery"]
        state["selected_variation"] = pick.get("variation")
        mwa.save_state(match_id, state, next_action=A_PICK_SHOT)
        # Re-read state so shot uses the just-written delivery value
        state = mwa.get_state(match_id)

    # ── Auto-pick shot ─────────────────────────────────────────────
    striker = get_striker(state)
    bowler = get_bowler(state)
    delivery = state.get("current_delivery") or "Good"

    _name, idx = bot_ai.pick_bot_shot(
        striker, bowler,
        state.get("current_over", 1), state.get("overs", 1),
        state.get("total_runs", 0), state.get("total_wickets", 0),
        target=state.get("target"),
        current_ball=state.get("current_ball", 0),
        difficulty="Medium",
    )
    shot = AVAILABLE_SHOTS[idx]

    oc = _bm._calc(state, striker, bowler, shot, delivery)
    res = _apply_outcome(state, oc, shot, delivery, striker, bowler)

    # Build commentary
    try:
        commentary = _bm._maybe_pick_commentary(oc, striker, bowler, oc.get("runs", 0))
    except Exception:
        commentary = None

    state["last_ball"] = {
        "text": res.get("rtxt"), "type": res.get("type"),
        "runs": res.get("runs", 0), "shot": shot, "delivery": delivery,
        "batsman": striker.get("name") if striker else None,
        "bowler": bowler.get("name") if bowler else None,
        "how": res.get("how"),
    }
    state["last_commentary"] = commentary or res.get("rtxt")
    _append_commentary_log(state, res, striker, bowler,
                           commentary or res.get("rtxt"))

    innings_over = False
    match_done = False

    if is_innings_over(state):
        if state.get("innings", 1) == 1:
            innings_over = True
            transition_to_second_innings(state)
            state["setup"] = SETUP_DONE
            mwa.save_state(match_id, state, next_action=A_PICK_NEW_BOWLER)
        else:
            match_done = True
            state["match_result"] = compute_match_result(state)
            mwa.save_state(match_id, state, next_action=A_COMPLETED)
            mwa.bump_ball_seq(match_id)
    elif res["need_new_bat"] and state.get("total_wickets", 0) < 10:
        nb = state.get("next_batsman_idx", 2)
        order = state.get("batting_order", [])
        if nb < len(order):
            state["striker_idx"] = nb
            state["next_batsman_idx"] = nb + 1
        mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
        mwa.bump_ball_seq(match_id)
    elif res["eoo"]:
        mwa.save_state(match_id, state, next_action=A_PICK_NEW_BOWLER)
        mwa.bump_ball_seq(match_id)
    else:
        mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
        mwa.bump_ball_seq(match_id)

    return {
        "rtxt": res["rtxt"],
        "type": res.get("type", ""),
        "runs": res.get("runs", 0),
        "eoo": res["eoo"],
        "need_new_bat": res["need_new_bat"],
        "innings_over": innings_over,
        "match_done": match_done,
        "striker": striker,
        "bowler": bowler,
        "state": mwa.get_state(match_id),
    }


# ──────────────────────────────────────────────────────────────────
# Over summary text builder
# ──────────────────────────────────────────────────────────────────

def _over_summary_text(state, over_num):
    runs = state.get("total_runs", 0)
    wkts = state.get("total_wickets", 0)
    total_overs = state.get("overs", 1)
    bat_team = state.get("bat_team_name", "Batting")
    timeline = state.get("timeline", [])
    last_6 = timeline[-6:] if len(timeline) >= 6 else timeline
    over_txt = " ".join(str(x) for x in last_6) if last_6 else "-"
    return (
        f"📊 <b>End of Over {over_num}</b>\n"
        f"• {bat_team}: <b>{runs}/{wkts}</b> ({over_num}/{total_overs} overs)\n"
        f"• This over: {over_txt}"
    )


def _wicket_alert_text(state, striker, bowler, rtxt):
    runs = state.get("total_runs", 0)
    wkts = state.get("total_wickets", 0)
    over = max(0, state.get("current_over", 1) - 1)
    ball = state.get("current_ball", 0)
    bat_stats = state.get("bat_stats", {})
    s_rid = str(striker.get("roster_id", "")) if striker else ""
    s_stats = bat_stats.get(s_rid, {})
    return (
        f"⚡ <b>WICKET!</b>\n"
        f"• {rtxt}\n"
        f"• {striker.get('name', '?') if striker else '?'} — "
        f"{s_stats.get('runs', 0)} runs ({s_stats.get('balls', 0)}b)\n"
        f"• Score: {runs}/{wkts} ({over}.{ball} ov)"
    )


def _innings_break_text(state):
    runs = state.get("inn1_runs", 0)
    wkts = state.get("inn1_wickets", 0)
    # inn1_team is set by transition_to_second_innings; bowl_team_name has
    # already swapped (it now refers to the 2nd innings batting team).
    bat_team = state.get("inn1_team", "Team 1")
    chase_team = state.get("bat_team_name", "Team 2")
    target = state.get("target", runs + 1)
    return (
        f"🏏 <b>Innings 1 Complete</b>\n"
        f"• {bat_team}: {runs}/{wkts}\n\n"
        f"🎯 <b>{chase_team} need {target} to win</b>"
    )


def _match_result_text(state):
    result = state.get("match_result", {}) if state else {}
    return (
        f"🏆 <b>MATCH COMPLETE</b>\n\n"
        f"{result.get('text', 'Match finished.')}"
    )


# ──────────────────────────────────────────────────────────────────
# Async coroutine: run the full WSP match
# ──────────────────────────────────────────────────────────────────

async def run_wsp_simulation(app, match_id, chat_id):
    """Run the complete WSP auto-simulation loop.

    Drives every ball from start to finish. Posts over summaries, wicket
    alerts, innings-break message, and final scorecard to ``chat_id``.
    Designed to be launched as an asyncio Task immediately after
    ``init_match_for_wsp`` succeeds.
    """
    max_balls = 2400   # hard upper bound (20 overs × 2 innings × some extras)
    over_just_ended = False
    over_num = 0

    try:
        for _ in range(max_balls):
            # Check if task was cancelled (match ended externally / /endmatch)
            if asyncio.current_task().cancelled():
                break

            result = _wsp_advance_step(match_id)
            if result is None:
                # Match already completed or state missing
                break

            state = result["state"]
            if state is None:
                break

            # --- Wicket alert (fire before end-of-over so it's timely) ---
            if result["need_new_bat"] and result["type"] == "wicket":
                try:
                    await app.bot.send_message(
                        chat_id,
                        _wicket_alert_text(state, result["striker"],
                                           result["bowler"], result["rtxt"]),
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception("wsp wicket alert failed")

            # --- End of over ---
            if result["eoo"]:
                over_num += 1
                try:
                    await app.bot.send_message(
                        chat_id,
                        _over_summary_text(state, over_num),
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception("wsp over summary failed")
                over_just_ended = True

            # --- Innings break ---
            if result["innings_over"]:
                # Capture 1st-innings state before transition mutates it
                try:
                    await app.bot.send_message(
                        chat_id,
                        _innings_break_text(state),
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception("wsp innings break message failed")
                # Send 1st-innings scorecard images
                try:
                    await _send_scorecard_images(app, match_id, chat_id, innings=1)
                except Exception:
                    logger.exception("wsp innings 1 scorecard failed (non-fatal)")
                over_num = 0
                await asyncio.sleep(INNINGS_PAUSE)
                continue

            # --- Match done ---
            if result["match_done"]:
                try:
                    await app.bot.send_message(
                        chat_id,
                        _match_result_text(result["state"]),
                        parse_mode="HTML",
                    )
                except Exception:
                    logger.exception("wsp match result message failed")
                break

            # --- Pacing delay ---
            if over_just_ended:
                await asyncio.sleep(OVER_PAUSE)
                over_just_ended = False
            else:
                await asyncio.sleep(BALL_DELAY)

        # ── Finalize & send scorecard ──────────────────────────────
        await _finalize_wsp(app, match_id, chat_id)

    except asyncio.CancelledError:
        logger.info("WSP simulation task cancelled for match %s", match_id)
    except Exception:
        logger.exception("WSP simulation crashed for match %s", match_id)


async def _finalize_wsp(app, match_id, chat_id):
    """Finalize the match DB record, send innings-2 scorecard, and post rewards."""
    # Send 2nd innings scorecard images
    try:
        await _send_scorecard_images(app, match_id, chat_id, innings=2)
    except Exception:
        logger.exception("wsp innings 2 scorecard failed (non-fatal)")

    session = get_session()
    try:
        from services.match_webapp_service import finalize_webapp_match
        result_payload = finalize_webapp_match(session, match_id)
        if not result_payload or result_payload.get("already"):
            return
        result = (result_payload.get("result") or {})
        rewards = result_payload.get("rewards") or {}

        # Post a rewards summary if there are rewards to show
        if rewards:
            from html import escape
            state = mwa.get_state(match_id)
            if not state:
                return
            winner_uid = result.get("winner_team_id")
            loser_uid = result.get("loser_team_id")
            def _team_name(uid):
                if not uid or not state:
                    return "Team"
                if uid == state.get("bat_team_id"):
                    return state.get("bat_team_name", "Batting team")
                if uid == state.get("bowl_team_id"):
                    return state.get("bowl_team_name", "Bowling team")
                # Fall back — check inn1 keys
                if uid == state.get("inn1_bat_team_id"):
                    return state.get("inn1_team", "Team 1")
                return "Team"
            winner_name = escape(_team_name(winner_uid))
            loser_name  = escape(_team_name(loser_uid))
            wc = rewards.get("winner_coins", 0)
            wg = rewards.get("winner_gems", 0)
            lc = rewards.get("loser_coins", 0)
            lg = rewards.get("loser_gems", 0)
            try:
                await app.bot.send_message(
                    chat_id,
                    f"🎁 <b>REWARDS</b>\n"
                    f"🏆 {winner_name}: +{wc:,} Coins 💰  +{wg} Gems 💎\n"
                    f"📉 {loser_name}: +{lc:,} Coins 💰  +{lg} Gems 💎",
                    parse_mode="HTML",
                )
            except Exception:
                logger.exception("wsp rewards message failed (non-fatal)")
    except Exception:
        session.rollback()
        logger.exception("_finalize_wsp error for match %s", match_id)
    finally:
        session.close()


async def _send_scorecard_images(app, match_id, chat_id, innings=1):
    """Generate and send batting + bowling scorecard images for an innings.

    Delegates to handlers.match._send_innings_scorecards which has all the
    full scorecard rendering logic. We create a lightweight ctx shim so the
    function can access bot_data and the bot object.
    """
    try:
        from handlers.match import _send_innings_scorecards

        class _Ctx:
            """Minimal shim satisfying what _send_innings_scorecards needs."""
            def __init__(self, application):
                self.bot_data = application.bot_data
                self.bot = application.bot

        await _send_innings_scorecards(_Ctx(app), match_id, innings)
    except Exception:
        logger.exception("_send_scorecard_images failed (non-fatal)")
