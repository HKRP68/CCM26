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

import asyncio
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

# Challenge League / Lets Play recovery: shortest gap between two heartbeat
# resumes of the SAME match. Only reached when the match has no clock of its
# own to re-arm (no job queue) — otherwise the resume arms one and this never
# bites.
CIPL_RESUME_COOLDOWN = 300
CIPL_RESUME_TIMEOUT = 20       # seconds to wait on one match's lock
CIPL_RESUME_HARD_TIMEOUT = 60  # backstop on one whole resume (lock wait + sends)
_CIPL_RESUME_KEY = "_hb_cipl_resumed_{mid}"
# A Challenge League / Lets Play match with no inactivity clock running is not
# waiting on anyone — its flow was dropped. It doesn't need the ball-by-ball
# 90s grace before being picked back up; players were typing /rcl well before
# that. (A running clock still means "leave it alone" — see _recover_cipl.)
CIPL_STALL_THRESHOLD = 45
# One sweep shortly after boot: a restart drops every in-memory clock and
# drop_pending_updates discards the taps made while the bot was down, so every
# live over-by-over match is picked straight back up instead of sitting idle
# until the heartbeat's stall gate.
STARTUP_SWEEP_DELAY = 10
STARTUP_SWEEP_JOB_NAME = "cipl_startup_sweep"


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

            # Only act on truly idle matches. The shorter over-by-over gate is
            # checked first; the ball-by-ball one once the mode is known.
            if idle_seconds < min(CIPL_STALL_THRESHOLD, RERENDER_THRESHOLD):
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

            # Challenge League (/cipl) and Lets Play matches run their own
            # over-by-over state machine with its own inactivity clock. The
            # regular-match logic below doesn't understand their actions, so
            # they get their own recovery — it resumes them exactly the way
            # /rcl does, instead of leaving that to a captain.
            if mem.get("mode") == "cipl_approach":
                await _recover_cipl(context, mid)
                continue

            if idle_seconds < RERENDER_THRESHOLD:
                continue

            if idle_seconds >= AUTODECIDE_THRESHOLD:
                # Long idle — auto-decide
                await _auto_decide(context, mid, mem, ms.next_action)
            else:
                # Mild idle — just re-render the screen
                await _try_rerender(context, mid)

    except Exception:
        logger.exception("heartbeat_tick top-level error")


async def _recover_cipl(context, mid):
    """Resume a Challenge League / Lets Play match that has lost its clock.

    Those matches drive themselves with an inactivity timer armed for whichever
    pick is outstanding. The timer is an in-memory job, so it does not survive a
    process restart, and a send that raised mid-over could leave the match with
    neither a live picker nor a clock. The match then simply stopped until a
    captain typed /rcl — which is why players were typing it over and over.

    An armed timer means the turn is still being counted down, so this leaves
    the match alone; only a match with no clock is resumed. The resume re-arms
    the clock itself, so at most one of these runs per stall.
    """
    try:
        from handlers.cipl_play import timer_armed, cipl_resume
    except Exception:
        logger.exception("cipl recovery unavailable")
        return

    if timer_armed(context, mid):
        return  # the match's own clock is running this turn — don't interfere

    # Without a job queue there is no clock to re-arm, so the resume below is
    # the only thing moving the match on. Space those out so a chat both
    # players have walked away from isn't re-prompted every tick.
    last = context.bot_data.get(_CIPL_RESUME_KEY.format(mid=mid))
    now = datetime.utcnow()
    if last and (now - last).total_seconds() < CIPL_RESUME_COOLDOWN:
        return
    context.bot_data[_CIPL_RESUME_KEY.format(mid=mid)] = now

    # The resume waits on the match's own lock. Bound that wait: one match whose
    # lock is held by a hung task must not stop the sweep from reaching every
    # other match. only_if_stalled re-checks the clock once the lock is held:
    # the step that was holding it has usually just prompted and armed one.
    try:
        resumed = await asyncio.wait_for(
            cipl_resume(context, mid, only_if_stalled=True,
                        lock_timeout=CIPL_RESUME_TIMEOUT),
            timeout=CIPL_RESUME_HARD_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("cipl heartbeat resume timed out for match %s", mid)
        return
    except Exception:
        logger.exception("cipl heartbeat resume failed for match %s", mid)
        return
    if resumed is None:
        logger.warning("cipl heartbeat resume: match %s lock stayed busy", mid)
    elif resumed:
        logger.info("heartbeat resumed stalled Challenge League match %s", mid)


def _live_cipl_match_ids():
    """Match ids of every saved, unfinished over-by-over match (blocking)."""
    import json
    from database import get_session
    from models import MatchState
    from services.match_state_store import A_COMPLETED

    session = get_session()
    try:
        rows = (session.query(MatchState.match_id, MatchState.next_action,
                              MatchState.state_json)
                .order_by(MatchState.match_id).all())
    finally:
        session.close()
    mids = []
    for mid, next_action, state_json in rows:
        if next_action == A_COMPLETED:
            continue
        try:
            state = json.loads(state_json) if state_json else {}
        except (TypeError, ValueError):
            continue
        if (isinstance(state, dict) and state.get("mode") == "cipl_approach"
                and state.get("played_via") != "webapp"):
            mids.append(mid)
    return mids


async def _startup_cipl_sweep(context):
    """Pick every live Challenge League / Lets Play match back up after boot.

    A restart (a deploy, a crash) loses every inactivity clock with the old
    process, and the taps players made while it was down are dropped. Without
    this the match sat silent until the heartbeat's stall gate noticed it, and
    players typed /rcl first. Each match is resumed exactly as /rcl would: the
    outstanding pick is re-shown from the saved snapshot and its clock restarted.
    """
    try:
        mids = await asyncio.to_thread(_live_cipl_match_ids)
    except Exception:
        logger.exception("cipl startup sweep: could not list live matches")
        return
    if mids:
        logger.info("cipl startup sweep: resuming %d live match(es)", len(mids))
    for mid in mids:
        await _recover_cipl(context, mid)


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
        A_PICK_OPENERS, A_PICK_OPENING_BOWLER,
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
            from services.bot_ai import _stat_row
            for i, p in enumerate(state["batting_order"]):
                if i == state["striker_idx"] or i == state["non_striker_idx"]:
                    continue
                # Tolerant read: after a cold read the keys are strings, and
                # a raw int lookup would miss and report every batter not-out —
                # promoting someone already dismissed.
                bs = _stat_row(state.get("bat_stats"), p["roster_id"])
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

        elif next_act in (A_PICK_OPENERS, A_PICK_OPENING_BOWLER):
            # Nobody answered the innings-break picker. Field the top of the
            # order and the best available bowler so the chase actually starts —
            # both selections in one pass, rather than making the chat wait out
            # a second idle window just for the bowler.
            from services.bot_ai import pick_bot_next_bowler
            from services.match_engine import _active_players
            note = []
            if next_act == A_PICK_OPENERS:
                state["batting_order"] = list(state["bat_xi"])
                state["striker_idx"] = 0
                state["non_striker_idx"] = 1
                state["next_batsman_idx"] = 2
                openers = state["bat_xi"][:2]
                note.append("🤖 Auto openers: <b>"
                            + "</b> & <b>".join(p["name"] for p in openers)
                            + "</b>")
            # Over 1: no previous bowler and nobody has used any quota yet.
            bowler = pick_bot_next_bowler(
                _active_players(state["bowl_xi"]), None, {}, state["overs"])
            state["current_bowler"] = bowler
            state["prev_bowler_rid"] = None
            state["selected_variation"] = None
            note.append(f"🤖 Auto opening bowler: <b>{bowler['name']}</b>")
            save_state(context, mid, state, next_action=A_PICK_DELIVERY)
            await context.bot.send_message(chat_id, "\n".join(note),
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
            application.job_queue.run_once(
                _startup_cipl_sweep, STARTUP_SWEEP_DELAY,
                name=STARTUP_SWEEP_JOB_NAME)
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
                """A PTB-shaped context for the fallback loop.

                The tick and everything it reaches — the state store, the
                renderers, the Challenge League resume — read ``bot_data``,
                ``bot`` and ``job_queue`` off the context. Exposing only
                ``application`` made the very first state lookup raise
                AttributeError, so this fallback heartbeat never actually
                recovered anything.
                """

                def __init__(self, app):
                    self.application = app

                @property
                def bot_data(self):
                    return self.application.bot_data

                @property
                def bot(self):
                    return self.application.bot

                @property
                def job_queue(self):
                    return getattr(self.application, "job_queue", None)

            ctx = _FakeContext(app)
            await asyncio.sleep(STARTUP_SWEEP_DELAY)
            try:
                await _startup_cipl_sweep(ctx)
            except Exception:
                logger.exception("cipl startup sweep failed")
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
