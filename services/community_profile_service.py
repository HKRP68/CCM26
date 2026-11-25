"""Read-side aggregation for Community Arena profiles.

Builds the dicts the bot's /community and /cxp commands render and the Mini App
Community Arena section consumes, so both surfaces show identical numbers.
"""

from __future__ import annotations

from services import community_game_service as G


def _display_name(user, fallback=None):
    if user is None:
        return fallback or "Manager"
    if user.username:
        return f"@{user.username}"
    return user.first_name or fallback or f"Manager {user.id}"


def cxp_summary(session, user_id, *, user=None, now=None):
    from models import User
    from services import community_xp_service as X
    user = user or session.get(User, user_id)
    prof = X.get_profile(session, user_id)
    lifetime = (prof.lifetime_xp if prof else 0) or 0
    prog = X.level_progress(session, lifetime)
    return {
        "user_id": user_id,
        "name": _display_name(user),
        "level": prog["level"],
        "title": prog["title"],
        "lifetime_xp": lifetime,
        "today": X.get_period_xp(session, user_id, "today", now),
        "week": X.get_period_xp(session, user_id, "week", now),
        "month": X.get_period_xp(session, user_id, "month", now),
        "progress": prog,
    }


def game_summary(session, user_id):
    from models import CommunityGameStat
    stats = {s.game_type: s for s in session.query(CommunityGameStat)
             .filter(CommunityGameStat.user_id == user_id).all()}
    plays = sum(s.plays for s in stats.values())
    wins = sum(s.wins for s in stats.values())
    by_type = [(G.GAME_LABEL[t], (stats[t].wins if t in stats else 0))
               for t in G.GAME_TYPES]
    fastest_ms = min((s.fastest_ms for s in stats.values()
                      if s.fastest_ms is not None), default=None)
    total_win_ms = sum(s.total_win_ms for s in stats.values())
    avg_ms = (total_win_ms / wins) if wins else None
    return {
        "plays": plays, "wins": wins,
        "win_pct": (100.0 * wins / plays) if plays else 0.0,
        "by_type": by_type,
        "fastest_s": (fastest_ms / 1000.0) if fastest_ms is not None else None,
        "avg_s": (avg_ms / 1000.0) if avg_ms is not None else None,
    }


def profile_summary(session, user_id, *, user=None, now=None):
    from models import User
    from services import community_xp_service as X
    from services import community_achievement_service as ACH
    from services import community_leaderboard_service as LB

    user = user or session.get(User, user_id)
    base = cxp_summary(session, user_id, user=user, now=now)
    prof = X.get_profile(session, user_id)

    activity = {
        "chat_messages": (prof.chat_messages if prof else 0) or 0,
        "pvp_matches": (prof.pvp_matches if prof else 0) or 0,
        "ai_matches": (prof.ai_matches if prof else 0) or 0,
        "claims": (prof.claims if prof else 0) or 0,
        "total_checkins": (prof.total_checkins if prof else 0) or 0,
        "current_streak": (prof.current_streak if prof else 0) or 0,
        "longest_streak": (prof.longest_streak if prof else 0) or 0,
        "days_active": (prof.days_active if prof else 0) or 0,
    }
    unlocked = sorted(ACH.unlocked_codes(session, user_id))
    achievements = {
        "unlocked": ACH.names(unlocked),
        "codes": unlocked,
        "count": len(unlocked),
        "total": len(ACH.CATALOG),
    }
    # All-time CXP rank.
    rank = None
    try:
        lb = LB.leaderboard(session, "cxp", "all", limit=1, user_id=user_id, now=now)
        rank = lb["me"]["rank"] if lb.get("me") else None
    except Exception:
        rank = None

    base.update({
        "rank": rank,
        "activity": activity,
        "games": game_summary(session, user_id),
        "achievements": achievements,
    })
    return base
