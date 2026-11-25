"""Admin-side aggregation + settings mutation for the Community Arena.

Pure data access (no Flask), so it is unit-testable and reusable by both the
admin dashboard and any reporting endpoint. All analytics come from real
database rows; where a metric cannot be derived from stored data it is reported
as ``None`` and the UI shows "n/a" rather than inventing a number.

Note on "human messages": by design the bot stores only short-lived message
fingerprints, never group conversation text, so the only message figure that
can be reported is *eligible rewarded* messages (the ledger 'chat' rows).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from sqlalchemy import func

logger = logging.getLogger(__name__)

GAME_LABELS = {
    "mystery": "Mystery Card", "oddoneout": "Odd One Out", "wordle": "Cricket Wordle",
    "fastestfingers": "Fastest Fingers", "rating": "Guess the Rating",
    "cricketer": "Guess the Cricketer",
}

MATCH_ACTIVITIES = ("pvp_match", "ai_match")
GAME_ACTIVITIES = ("game_mystery", "game_oddoneout", "game_wordle",
                   "game_fastestfingers", "game_rating", "game_cricketer")


def _bounds(period, now=None):
    """Return (start, prev_start) UTC bounds for a period and the one before."""
    from services import community_xp_service as X
    start = X.period_bound_utc(period, now)
    if start is None:
        return None, None
    span = {
        "today": timedelta(days=1), "day": timedelta(days=1),
        "week": timedelta(days=7), "month": timedelta(days=30),
    }.get(period, timedelta(days=1))
    return start, start - span


def analytics(session, period="today", now=None):
    from models import (CommunityXPLedger as L, CommunityGame as Gm,
                        CommunityGameParticipant as GP, CommunityProfile as Pr)
    start, prev_start = _bounds(period, now)

    def ledger_q(*filters):
        q = session.query(*filters)
        if start is not None:
            q = q.filter(L.created_at >= start)
        return q

    out = {"period": period}

    # Active members + CXP
    out["active_members"] = ledger_q(func.count(func.distinct(L.user_id))).scalar() or 0
    out["cxp_awarded"] = int(ledger_q(func.coalesce(func.sum(L.delta), 0))
                             .filter(L.delta > 0).scalar() or 0)
    by_activity = (ledger_q(L.activity, func.coalesce(func.sum(L.delta), 0))
                   .filter(L.delta > 0).group_by(L.activity).all())
    out["cxp_by_activity"] = sorted(
        [{"activity": a, "cxp": int(v or 0)} for a, v in by_activity],
        key=lambda r: -r["cxp"])

    def act_count(acts):
        q = session.query(func.count(L.id)).filter(L.activity.in_(acts))
        if start is not None:
            q = q.filter(L.created_at >= start)
        return int(q.scalar() or 0)

    out["rewarded_messages"] = act_count(["chat"])
    out["completed_matches"] = act_count(list(MATCH_ACTIVITIES))
    out["claims"] = act_count(["claim"])
    out["checkins"] = act_count(["checkin"])
    out["game_wins"] = act_count(list(GAME_ACTIVITIES))

    # Peak activity hours (IST), bucketed in Python from a capped sample.
    try:
        from services import community_xp_service as X
        rows = ledger_q(L.created_at).limit(20000).all()
        hours = Counter()
        for (ts,) in rows:
            if ts is not None:
                hours[X.ist_now(ts).hour] += 1
        out["peak_hours"] = [{"hour": h, "count": c}
                             for h, c in sorted(hours.items(), key=lambda kv: -kv[1])[:5]]
    except Exception:
        out["peak_hours"] = []

    # Games
    def game_q(*cols):
        q = session.query(*cols)
        if start is not None:
            q = q.filter(Gm.started_at >= start)
        return q

    launched = game_q(func.count(Gm.id)).scalar() or 0
    closed = game_q(func.count(Gm.id)).filter(Gm.status == "closed").scalar() or 0
    expired = game_q(func.count(Gm.id)).filter(Gm.status == "expired").scalar() or 0
    out["games_launched"] = int(launched)
    out["games_won"] = int(closed)
    out["games_unanswered"] = int(expired)
    out["completion_rate"] = (round(100.0 * closed / (closed + expired), 1)
                              if (closed + expired) else None)
    avg_ms = (game_q(func.avg(Gm.winner_response_ms))
              .filter(Gm.status == "closed",
                      Gm.winner_response_ms.isnot(None)).scalar())
    out["avg_winning_response_s"] = round(float(avg_ms) / 1000.0, 2) if avg_ms else None
    avg_part = game_q(func.avg(Gm.participant_count)).scalar()
    out["avg_participants"] = round(float(avg_part), 1) if avg_part else None

    by_type = game_q(Gm.game_type, func.count(Gm.id)).group_by(Gm.game_type).all()
    out["games_by_type"] = sorted(
        [{"type": t, "label": GAME_LABELS.get(t, t), "count": int(c)}
         for t, c in by_type], key=lambda r: -r["count"])
    out["most_popular_game"] = out["games_by_type"][0]["label"] if out["games_by_type"] else None

    # Unique participants across games in the window.
    pq = (session.query(func.count(func.distinct(GP.user_id)))
          .join(Gm, Gm.id == GP.game_id))
    if start is not None:
        pq = pq.filter(Gm.started_at >= start)
    out["unique_game_participants"] = int(pq.scalar() or 0)

    # New members + returning members (explicit definitions).
    nq = session.query(func.count(Pr.id))
    if start is not None:
        nq = nq.filter(Pr.created_at >= start)
    out["new_members"] = int(nq.scalar() or 0)

    if start is not None and prev_start is not None:
        cur = {u for (u,) in session.query(func.distinct(L.user_id))
               .filter(L.created_at >= start).all()}
        prev = {u for (u,) in session.query(func.distinct(L.user_id))
                .filter(L.created_at >= prev_start, L.created_at < start).all()}
        returning = cur & prev
        out["returning_members"] = len(returning)
        out["retention_pct"] = (round(100.0 * len(returning) / len(prev), 1)
                                if prev else None)
        out["retention_note"] = ("Members active in the previous %s who were also "
                                  "active in this one." % period)
    else:
        out["returning_members"] = None
        out["retention_pct"] = None
        out["retention_note"] = "Retention is reported for fixed periods only."

    return out


def active_games(session):
    from models import CommunityGame
    rows = (session.query(CommunityGame)
            .filter(CommunityGame.status == "active")
            .order_by(CommunityGame.started_at.desc()).all())
    return rows


def recent_games(session, limit=20):
    from models import CommunityGame
    return (session.query(CommunityGame)
            .order_by(CommunityGame.started_at.desc()).limit(limit).all())


# ── Settings mutation ───────────────────────────────────────────────────

def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_settings_form(form):
    """Turn the admin HTML form into a nested settings-updates dict."""
    from services import community_game_service as G

    def gi(name, default):
        return _to_int(form.get(name), default)

    updates = {
        "max_daily_xp": gi("max_daily_xp", 400),
        "xp": {
            "chat": {"xp": gi("xp_chat", 1), "daily_limit": gi("limit_chat", 50)},
            "pvp_match": {"xp": gi("xp_pvp", 15), "daily_limit": gi("limit_pvp", 10)},
            "ai_match": {"xp": gi("xp_ai", 5), "daily_limit": gi("limit_ai", 5)},
            "claim": {"xp": gi("xp_claim", 5), "daily_limit": gi("limit_claim", 5)},
            "checkin": {"xp": gi("xp_checkin", 20), "daily_limit": 1},
        },
        "chat": {
            "min_chars": gi("chat_min_chars", 8),
            "cooldown_seconds": gi("chat_cooldown", 45),
            "daily_limit": gi("limit_chat", 50),
        },
        "scheduler": {
            "active_start_hour": gi("sched_start", 8),
            "active_end_hour": gi("sched_end", 24),
            "max_games_per_day": gi("sched_max", 8),
            "min_gap_minutes": gi("sched_gap", 45),
            "inactivity_minutes": gi("sched_inactivity", 30),
            "match_protection": bool(form.get("sched_match_protection")),
            "recent_player_exclusion": gi("sched_recent_players", 15),
            "weights": {t: gi(f"weight_{t}", 10) for t in G.GAME_TYPES},
        },
        "eligible": {
            "rating_min": gi("elig_rating_min", 0),
            "rating_max": gi("elig_rating_max", 100),
        },
        "fuzzy_matching": bool(form.get("fuzzy_matching")),
        "games": {},
    }
    for t in G.GAME_TYPES:
        updates["games"][t] = {
            "enabled": bool(form.get(f"game_{t}_enabled")),
            "duration": gi(f"game_{t}_duration", 60),
            "reward": gi(f"game_{t}_reward", 20),
            "daily_win_limit": gi(f"game_{t}_winlimit", 0),
        }
        if t == "wordle":
            updates["games"][t]["max_attempts"] = gi("game_wordle_attempts", 6)
        if t == "rating":
            updates["games"][t]["max_attempts"] = gi("game_rating_attempts", 3)
    return updates


def adjust_user_xp(session, user_id, delta, reason, admin_id):
    from services import community_xp_service as X
    res = X.admin_adjust_xp(session, user_id, delta, reason=reason, admin_id=admin_id)
    session.commit()
    return res


def set_user_suspension(session, user_id, suspended, reason, admin_id):
    from models import CommunityAuditLog
    from services import community_xp_service as X
    prof = X.get_or_create_profile(session, user_id)
    prof.rewards_suspended = bool(suspended)
    prof.suspended_reason = (reason or "")[:300] if suspended else None
    session.add(CommunityAuditLog(
        admin_id=admin_id, user_id=user_id,
        action="suspend" if suspended else "unsuspend",
        reason=(reason or "")[:400]))
    session.commit()
    return prof


def reset_user_game_stats(session, user_id, admin_id, reason=""):
    from models import CommunityGameStat, CommunityAuditLog
    (session.query(CommunityGameStat)
     .filter(CommunityGameStat.user_id == user_id).delete())
    session.add(CommunityAuditLog(
        admin_id=admin_id, user_id=user_id, action="reset_game_stats",
        reason=(reason or "")[:400]))
    session.commit()


def user_ledger(session, user_id, limit=50):
    from models import CommunityXPLedger
    return (session.query(CommunityXPLedger)
            .filter(CommunityXPLedger.user_id == user_id)
            .order_by(CommunityXPLedger.created_at.desc()).limit(limit).all())


def find_users(session, query, limit=25):
    from models import User, CommunityProfile
    q = (session.query(CommunityProfile, User)
         .join(User, User.id == CommunityProfile.user_id))
    query = (query or "").strip()
    if query:
        if query.isdigit():
            q = q.filter((User.telegram_id == int(query)) | (User.id == int(query)))
        else:
            q = q.filter(User.username.ilike(f"%{query.lstrip('@')}%"))
    q = q.order_by(CommunityProfile.lifetime_xp.desc()).limit(limit)
    return q.all()
