"""Daily group check-in.

Awards the configured check-in CXP once per IST calendar day and maintains the
manager's streak. Idempotent via both the ledger ``event_key`` and the
profile's ``last_checkin_date``, so a second /checkin the same day changes
nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _yesterday_ist(today_str: str) -> str:
    d = datetime.strptime(today_str, "%Y-%m-%d").date()
    return (d - timedelta(days=1)).strftime("%Y-%m-%d")


def check_in(user_id: int, *, session=None, now=None) -> dict:
    """Perform a daily check-in. Returns a result dict.

    ``{"ok", "already", "streak", "longest", "total", "credited",
       "lifetime_xp", "level", "title", "leveled_up", "achievements",
       "milestones", "reason"}``
    """
    own = False
    if session is None:
        from database import get_session
        session = get_session()
        own = True

    from services import community_xp_service as X
    from services import community_config_service as C
    from services import community_achievement_service as ACH

    now = now or datetime.utcnow()
    today = X.ist_today_str(now)
    out = {"ok": False, "already": False, "streak": 0, "longest": 0,
           "total": 0, "credited": 0, "lifetime_xp": None, "level": None,
           "title": None, "leveled_up": False, "achievements": [],
           "milestones": [], "reason": None}
    try:
        user = X._eligible_user(session, user_id)
        if user is None:
            out["reason"] = "ineligible_user"
            return out
        prof = X.get_or_create_profile(session, user_id)
        if prof.rewards_suspended:
            out["reason"] = "suspended"
            return out

        if prof.last_checkin_date == today:
            out.update(already=True, streak=prof.current_streak or 0,
                       longest=prof.longest_streak or 0,
                       total=prof.total_checkins or 0,
                       lifetime_xp=prof.lifetime_xp or 0, level=prof.level or 1,
                       title=X.level_title(session, prof.level or 1),
                       reason="already")
            return out

        # Advance streak.
        if prof.last_checkin_date == _yesterday_ist(today):
            prof.current_streak = (prof.current_streak or 0) + 1
        else:
            prof.current_streak = 1
        prof.longest_streak = max(prof.longest_streak or 0, prof.current_streak)
        prof.total_checkins = (prof.total_checkins or 0) + 1
        prof.last_checkin_date = today

        xp = C.xp_for("checkin", session)
        award = X.award_xp(
            user_id, X.ACT_CHECKIN, xp, event_key=f"checkin:{user_id}:{today}",
            detail="daily check-in", now=now, session=session,
            activity_daily_limit=C.daily_limit_for("checkin", session) or 1)

        got = ACH.check_checkin_achievements(session, user_id)
        got += ACH.check_xp_achievements(session, user_id)

        milestones = []
        if award.get("leveled_up"):
            try:
                from services import community_level_service as L
                milestones = L.grant_due_milestones(session, user_id)
            except Exception:
                logger.exception("milestone grant failed (non-fatal)")

        if own:
            session.commit()

        out.update(
            ok=True, streak=prof.current_streak, longest=prof.longest_streak,
            total=prof.total_checkins, credited=award.get("credited", 0),
            lifetime_xp=award.get("lifetime_xp") or prof.lifetime_xp,
            level=award.get("level") or prof.level,
            title=award.get("title") or X.level_title(session, prof.level or 1),
            leveled_up=award.get("leveled_up", False),
            achievements=ACH.names(got), milestones=milestones, reason="ok")
        return out
    except Exception:
        if own:
            session.rollback()
        logger.exception("check_in failed for user=%s", user_id)
        out["reason"] = "error"
        return out
    finally:
        if own:
            session.close()
