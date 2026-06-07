"""Match heartbeat — keeps in-progress matches from getting stuck.

For every active match, runs a periodic check:
  - If state hasn't changed in 90s but match is "active" → re-render the screen
    (covers cases where the user lost buttons due to network/Telegram glitches)
  - If state hasn't changed in 5 minutes → auto-decide a sensible default
    (covers cases where one player went AFK)

This replaces the old "TIME'S UP, match forfeited" behavior with something
much more forgiving: the game just keeps going.

The heartbeat runs as a single global job that scans all matches every 30s.
That way we don't need to manage per-match jobs (lower complexity, fewer leaks).
"""

import logging
import random
from datetime import datetime, timedelta
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Tunables
HEARTBEAT_INTERVAL = 30        # seconds between scans
RERENDER_THRESHOLD = 90        # seconds idle before we re-render the screen
AUTODECIDE_THRESHOLD = 300     # seconds idle (5 min) before AI auto-decides
HEARTBEAT_JOB_NAME = "match_heartbeat_global"


async def _heartbeat_tick(context: ContextTypes.DEFAULT_TYPE):
    """Periodic scan: re-renders stuck matches + fires notifications.

    Designed to be cheap when idle (no active matches, no due notifications)
    so Neon's compute can auto-suspend. We bail out before any DB query if
    the in-memory flags say there's nothing to do.
    """
    # ── FAST PATH — skip everything if nothing to do ──
    # The MatchState table is empty when no matches are in progress. We
    # can check this with a cheap in-memory flag the match handlers
    # maintain. If both flags say idle, we don't touch the DB at all
    # → lets Neon compute suspend.
    from services.match_heartbeat_flags import (
        has_active_matches, has_due_notifications,
    )
    if not has_active_matches(context) and not has_due_notifications():
        return  # nothing to do — DB stays cold

    # Notification tick (deduped to once per minute internally)
    try:
        from services.notification_service import maybe_tick
        await maybe_tick(context.application)
    except Exception:
        logger.exception("notification tick error")

    # Active matches: only query DB if the flag says we have any
    if not has_active_matches(context):
        return

    try:
        from services.match_state_store import (
            list_active_match_ids, get_state, get_next_action,
            A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT,
            A_PICK_NEW_BATSMAN, A_PICK_NEW_BOWLER,
            A_INNINGS_BREAK, A_COMPLETED,
        )
        from database import get_session
        from models import MatchState
        now = datetime.utcnow()

        session = get_session()
        try:
            states = session.query(MatchState).all()
        finally:
            session.close()

        # Sync the flag — if DB shows no states, clear the in-memory hint
        # so the next tick can fast-skip.
        from services.match_heartbeat_flags import set_active_match_count
        set_active_match_count(context, len(states))

        for ms in states:
            mid = ms.match_id
            last_mod = ms.last_modified or now
            idle_seconds = (now - last_mod).total_seconds()

            # COMPLETED → state should have been cleaned up; skip
            if ms.next_action == A_COMPLETED:
                continue

            # Only act on truly idle matches
            if idle_seconds < RERENDER_THRESHOLD:
                continue

            # Check the match state is in memory; if not, hydrate it
            from services.match_state_store import _mem_key
            mem = context.bot_data.get(_mem_key(mid))
            if not mem:
                # Cold cache — get_state will repopulate
                mem = get_state(context, mid)
            if not mem:
                continue

            # Mini-App-driven matches are advanced by player taps via the web
            # endpoints, NOT by the bot. Don't re-render bot prompts or
            # auto-decide them here — that would double-drive the match.
            # (A separate webapp timeout sweep handles abandoned Mini-App matches.)
            if mem.get("played_via") == "webapp":
                continue

            if idle_seconds >= AUTODECIDE_THRESHOLD:
                # Long idle — auto-decide
                await _auto_decide(context, mid, mem, ms.next_action)
            else:
                # Mild idle — just re-render the screen
                await _try_rerender(context, mid)

    except Exception:
        logger.exception("heartbeat_tick top-level error")


async def _try_rerender(context, mid):
    """Re-show the current prompt to a possibly-stuck user."""
    try:
        from handlers.match import render_screen
        await render_screen(context, mid)
    except Exception:
        logger.exception(f"heartbeat re-render failed for match {mid}")


async def _auto_decide(context, mid, state, next_act):
    """User went AFK for 5+ minutes. Pick a sensible default and continue."""
    from services.match_state_store import (
        save_state, A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT,
        A_PICK_NEW_BATSMAN, A_PICK_NEW_BOWLER,
    )

    try:
        chat_id = state.get("chat_id")
        await context.bot.send_message(
            chat_id,
            "⏱ <i>Resuming idle match — auto-deciding so the game keeps going.</i>",
            parse_mode="HTML",
        )

        if next_act == A_PICK_DELIVERY:
            # Auto-pick a default delivery and route to shot
            from services.bowling_service import get_delivery_options, is_spinner
            from handlers.match import get_bowler, _ss
            bw = get_bowler(state)
            opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
            if opts["is_spinner"]:
                deliveries = [d for d in opts["deliveries"] if d != "Surprise"]
                state["current_delivery"] = random.choice(deliveries) if deliveries else "Off Break"
            else:
                variation = random.choice(opts["variations"]) if opts["variations"] else "Seam Up"
                length = "Good" if "Good" in opts["lengths"] else (opts["lengths"][0] if opts["lengths"] else "Good")
                state["current_delivery"] = f"{variation} {length}"
            state["selected_variation"] = None
            save_state(context, mid, state, next_action=A_PICK_SHOT)
            await context.bot.send_message(
                chat_id, f"🤖 Auto-pick: <b>{state['current_delivery']}</b>",
                parse_mode="HTML")
            from handlers.match import render_screen
            await render_screen(context, mid)

        elif next_act == A_PICK_LENGTH:
            from services.bowling_service import get_delivery_options
            from handlers.match import get_bowler
            bw = get_bowler(state)
            opts = get_delivery_options(bw["bowl_style"], bw["bowl_hand"])
            length = "Good" if "Good" in opts["lengths"] else (opts["lengths"][0] if opts["lengths"] else "Good")
            var = state.get("selected_variation", "Seam")
            state["current_delivery"] = f"{var} {length}"
            state["selected_variation"] = None
            save_state(context, mid, state, next_action=A_PICK_SHOT)
            await context.bot.send_message(
                chat_id, f"🤖 Auto-pick length: <b>{length}</b>",
                parse_mode="HTML")
            from handlers.match import render_screen
            await render_screen(context, mid)

        elif next_act == A_PICK_SHOT:
            # Auto-shot: a sensible "Drive" (good shot for most situations)
            from services.bowling_service import AVAILABLE_SHOTS
            from handlers.match import _bot_process_shot
            try:
                idx = AVAILABLE_SHOTS.index("Drive")
            except ValueError:
                idx = 0
            await context.bot.send_message(
                chat_id, "🤖 Auto-shot: <b>Drive</b>",
                parse_mode="HTML")
            await _bot_process_shot(context, mid, idx)

        elif next_act == A_PICK_NEW_BATSMAN:
            # Promote next available not-out batsman
            for i, p in enumerate(state["batting_order"]):
                if i == state["striker_idx"] or i == state["non_striker_idx"]:
                    continue
                bs = state["bat_stats"].get(p["roster_id"], {})
                if not bs.get("out", False):
                    state["striker_idx"] = i
                    save_state(context, mid, state, next_action=A_PICK_DELIVERY)
                    await context.bot.send_message(
                        chat_id, f"🤖 Auto next batsman: <b>{p['name']}</b>",
                        parse_mode="HTML")
                    from handlers.match import render_screen
                    await render_screen(context, mid)
                    return

        elif next_act == A_PICK_NEW_BOWLER:
            # Pick best active non-prev bowler
            from services.bot_ai import pick_bot_next_bowler
            from services.match_engine import _active_players
            new_bowler = pick_bot_next_bowler(
                _active_players(state["bowl_xi"]),
                state.get("prev_bowler_rid"),
                state["bowl_stats"],
                state["overs"],
            )
            state["current_bowler"] = new_bowler
            save_state(context, mid, state, next_action=A_PICK_DELIVERY)
            await context.bot.send_message(
                chat_id, f"🤖 Auto next bowler: <b>{new_bowler['name']}</b>",
                parse_mode="HTML")
            from handlers.match import render_screen
            await render_screen(context, mid)

    except Exception:
        logger.exception(f"auto_decide failed for match {mid}")


def start_heartbeat(application):
    """Register the global heartbeat job. Idempotent — only adds once.

    Tries job_queue first. If unavailable (e.g. python-telegram-bot was installed
    without the [job-queue] extra), falls back to a plain asyncio task that
    schedules itself in the application's event loop on startup.
    """
    if application.job_queue:
        try:
            existing = application.job_queue.get_jobs_by_name(HEARTBEAT_JOB_NAME)
            if existing:
                return
            application.job_queue.run_repeating(
                _heartbeat_tick,
                interval=HEARTBEAT_INTERVAL,
                first=HEARTBEAT_INTERVAL,
                name=HEARTBEAT_JOB_NAME,
            )
            logger.info(f"Heartbeat scheduled via JobQueue (every {HEARTBEAT_INTERVAL}s)")
            return
        except Exception:
            logger.exception("Failed to schedule via JobQueue — falling back to asyncio task")

    # Fallback: register a post_init handler that creates an asyncio loop task.
    # This works even when python-telegram-bot[job-queue] is not installed.
    # Preserve any existing startup hook, such as bot-menu registration.
    previous_post_init = application.post_init

    async def _post_init(app):
        if previous_post_init:
            await previous_post_init(app)

        import asyncio
        async def _loop():
            class _FakeContext:
                def __init__(self, app): self.application = app
            ctx = _FakeContext(app)
            while True:
                try:
                    await _heartbeat_tick(ctx)
                except Exception:
                    logger.exception("heartbeat fallback tick failed")
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        asyncio.create_task(_loop())
        logger.info(f"Heartbeat scheduled via asyncio fallback (every {HEARTBEAT_INTERVAL}s)")

    try:
        application.post_init = _post_init
    except Exception:
        logger.exception("Failed to register heartbeat fallback")
