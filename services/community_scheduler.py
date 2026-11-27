"""Automatic community engagement scheduler.

A restart-safe periodic sweeper (same architecture as
``services/giveaway_scheduler.py``): all durable state lives in
:class:`models.CommunityGame` and :class:`models.CommunitySchedulerState`, and
each tick reconciles it. It:

* reveals overdue, unwon games (backstop to the per-game run_once reveal) and
  refreshes the in-memory active-game cache — this is the restart recovery:
  active unexpired games keep running, overdue ones are expired, never
  restarted;
* launches a weighted-random eligible game in the Official GC when genuine
  human conversation has gone quiet, within active hours, under the per-day cap
  and minimum gap, avoiding consecutive game-type repeats and recently featured
  players, and never while a protected tournament match is live.

Inactivity is measured from in-memory "last genuine human message" timestamps
fed by the bot middleware — so after a restart, nothing launches until a human
speaks again, which is what stops a burst of catch-up events after downtime.
Duplicate launches across workers are prevented by the single-active-game rule
and the unique ``launch_key``.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

SWEEP_JOB_NAME = "community_sweep"
SWEEP_INTERVAL = 15  # seconds

# chat_id → datetime (UTC) of the last genuine human conversational message.
_LAST_HUMAN: dict = {}
_BOOT_AT = datetime.utcnow()


def note_human_activity(chat_id, when=None):
    """Called by the bot middleware for genuine human conversation (not bots,
    commands or game answers). Pure in-memory; no DB cost per message."""
    _LAST_HUMAN[chat_id] = when or datetime.utcnow()


def last_human_at(chat_id):
    return _LAST_HUMAN.get(chat_id)


# ── Tick ─────────────────────────────────────────────────────────────────

async def _tick(context):
    try:
        await _reveal_overdue(context)
    except Exception:
        logger.exception("community reveal sweep failed")
    try:
        _refresh_cache()
    except Exception:
        logger.exception("community cache refresh failed")
    try:
        await _maybe_launch_auto(context)
    except Exception:
        logger.exception("community auto-launch failed")


def _refresh_cache():
    from database import get_session
    from services import community_game_service as G
    s = get_session()
    try:
        G.refresh_active_cache(s)
    finally:
        s.close()


async def _reveal_overdue(context):
    from database import get_session
    from models import CommunityGame
    from handlers.community_games import reveal_if_due
    now = datetime.utcnow()
    s = get_session()
    try:
        due = (s.query(CommunityGame.id)
               .filter(CommunityGame.status == "active",
                       CommunityGame.deadline <= now).all())
        ids = [gid for (gid,) in due]
    finally:
        s.close()
    for gid in ids:
        await reveal_if_due(context, gid, now=now)


# ── Auto launch ───────────────────────────────────────────────────────

def _state_row(session, chat_id):
    from models import CommunitySchedulerState
    row = (session.query(CommunitySchedulerState)
           .filter(CommunitySchedulerState.chat_id == chat_id).first())
    if row is None:
        row = CommunitySchedulerState(chat_id=chat_id, games_today=0)
        session.add(row)
        session.flush()
    return row


def _within_active_hours(cfg, now):
    from services import community_xp_service as X
    hour = X.ist_now(now).hour
    start = int(cfg["scheduler"].get("active_start_hour", 8))
    end = int(cfg["scheduler"].get("active_end_hour", 24))
    if end >= 24:
        return hour >= start
    if start <= end:
        return start <= hour < end
    # wraps past midnight
    return hour >= start or hour < end


def _protected_match_live(session, official):
    from models import Match
    live = (session.query(Match.id)
            .filter(Match.chat_id == official,
                    Match.tournament_id.isnot(None),
                    Match.status.in_(("in_progress", "active", "live", "playing")))
            .first())
    return live is not None


def _weighted_choice(weights: dict):
    items = [(k, max(0, int(v or 0))) for k, v in weights.items()]
    items = [(k, w) for k, w in items if w > 0]
    if not items:
        return None
    total = sum(w for _k, w in items)
    r = random.uniform(0, total)
    upto = 0
    for k, w in items:
        upto += w
        if r <= upto:
            return k
    return items[-1][0]


async def _maybe_launch_auto(context):
    from database import get_session
    from services import community_config_service as C
    from services import community_game_service as G
    from services import community_xp_service as X

    cfg = C.get_config(fresh=True)
    if not (cfg.get("enabled") and cfg.get("auto_enabled")):
        return
    official = _official_group_id()
    if official is None:
        return
    now = datetime.utcnow()
    if not _within_active_hours(cfg, now):
        return

    sched = cfg["scheduler"]
    max_per_day = int(sched.get("max_games_per_day", 8))
    min_gap = timedelta(minutes=int(sched.get("min_gap_minutes", 45)))
    inactivity = timedelta(minutes=int(sched.get("inactivity_minutes", 30)))
    interval_min = int(sched.get("interval_minutes", 0) or 0)

    exclude_player_ids = []
    game_type = None
    session = get_session()
    try:
        if G.active_game_for_chat(session, official) is not None:
            return
        state = _state_row(session, official)
        today = X.ist_today_str(now)
        if state.games_today_date != today:
            state.games_today = 0
            state.games_today_date = today
        if state.games_today >= max_per_day:
            session.commit()
            return
        if state.last_game_at and (now - state.last_game_at) < min_gap:
            session.commit()
            return
        if sched.get("match_protection", True) and _protected_match_live(session, official):
            session.commit()
            return

        # Decide whether to launch now. Two independent triggers:
        #   • inactivity — the group has gone quiet for `inactivity` minutes
        #     (needs a genuine human message since boot, so a restart during a
        #     quiet spell can't fire a catch-up burst);
        #   • interval  — a random game every `interval_min` minutes regardless
        #     of activity (measured from the last game, or boot if none yet, so
        #     it also can't fire the instant the process starts).
        lh = last_human_at(official)
        inactive_trigger = (
            lh is not None and (now - lh) >= inactivity
            and (now - _BOOT_AT) >= inactivity)
        ref = state.last_game_at or _BOOT_AT
        interval_trigger = (
            interval_min > 0 and (now - ref) >= timedelta(minutes=interval_min))
        if not (inactive_trigger or interval_trigger):
            session.commit()
            return

        # Build the eligible weight map: enabled games, minus recent types.
        try:
            recent_types = json.loads(state.recent_game_types or "[]")
        except (ValueError, TypeError):
            recent_types = []
        try:
            exclude_player_ids = json.loads(state.recent_player_ids or "[]")
        except (ValueError, TypeError):
            exclude_player_ids = []

        weights = dict(sched.get("weights", {}))
        avoid = set(recent_types[-int(sched.get("recent_type_exclusion", 1)):])
        eligible = {t: w for t, w in weights.items()
                    if C.game_enabled(t, session) and t not in avoid}
        if not eligible:
            eligible = {t: w for t, w in weights.items() if C.game_enabled(t, session)}
        game_type = _weighted_choice(eligible)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("auto-launch pre-check failed")
        return
    finally:
        session.close()

    if not game_type:
        return

    from handlers.community_games import launch_and_announce
    game_id, err = await launch_and_announce(
        context, official, game_type, source="auto",
        exclude_player_ids=exclude_player_ids)
    if game_id is None:
        if err not in ("active_exists", "disabled", "game_disabled"):
            logger.info("auto-launch of %s skipped: %s", game_type, err)
        return

    # Record the launch in durable state.
    session = get_session()
    try:
        from models import CommunityGame
        state = _state_row(session, official)
        g = session.get(CommunityGame, game_id)
        state.games_today = int(state.games_today or 0) + 1
        state.last_game_at = now
        state.last_game_type = game_type
        try:
            recent_types = json.loads(state.recent_game_types or "[]")
        except (ValueError, TypeError):
            recent_types = []
        recent_types.append(game_type)
        state.recent_game_types = json.dumps(recent_types[-6:])
        if g is not None and g.player_id:
            try:
                recent_players = json.loads(state.recent_player_ids or "[]")
            except (ValueError, TypeError):
                recent_players = []
            recent_players.append(g.player_id)
            keep = int(cfg["scheduler"].get("recent_player_exclusion", 15))
            state.recent_player_ids = json.dumps(recent_players[-keep:])
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("auto-launch state update failed")
    finally:
        session.close()


def _official_group_id():
    try:
        from services.gc_gate import official_group_id
        return official_group_id()
    except Exception:
        return None


# ── Registration ──────────────────────────────────────────────────────

def start_community_scheduler(application):
    """Register the community sweep job. Idempotent."""
    if not application.job_queue:
        logger.warning("JobQueue unavailable — community scheduler not started")
        return
    try:
        if application.job_queue.get_jobs_by_name(SWEEP_JOB_NAME):
            return
        application.job_queue.run_repeating(
            _tick, interval=SWEEP_INTERVAL, first=SWEEP_INTERVAL,
            name=SWEEP_JOB_NAME)
        logger.info("Community Arena scheduler started (every %ss)", SWEEP_INTERVAL)
    except Exception:
        logger.exception("Failed to start community scheduler")
