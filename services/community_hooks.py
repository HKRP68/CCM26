"""Integration hooks that feed real gameplay into Community XP.

These are called from existing gameplay flows (match completion, successful
claims). They are **best-effort and isolated**: each opens its own session and
swallows its own errors, so a Community Arena problem can never roll back or
block a match or a claim.

Eligibility (per spec):
* only activity originating in the configured Official CMU Group qualifies —
  validated from the match's own ``chat_id`` / the claim's chat, i.e. trusted
  server-side data, never a client-supplied value;
* only registered, non-banned managers; the AI opponent never earns;
* both human participants earn for a completed PvP match regardless of result;
* abandoned / invalid / not-counted matches award nothing (``counts`` gate);
* every award is idempotent via a stable ledger ``event_key``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _official_group_id():
    try:
        from services.gc_gate import official_group_id
        return official_group_id()
    except Exception:
        return None


def on_match_completed(match_id, *, counts: bool = True):
    """Award Community XP for a completed match.

    Reads the trusted :class:`models.Match` row for the origin chat and
    participants. Awards PvP XP to both humans, or AI XP to the lone human in a
    vs-AI match. No-op unless Community Arena is enabled, the match counted, and
    it originated in the Official GC.
    """
    if not counts or not match_id:
        return
    try:
        from database import get_session
        from models import Match
        from services import community_config_service as C
        from services import community_xp_service as X
        from services import community_achievement_service as ACH
        from services.match_rewards import is_ai_user

        if not C.is_enabled():
            return
        official = _official_group_id()
        if official is None:
            return

        session = get_session()
        try:
            m = session.get(Match, match_id)
            if m is None:
                return
            if (m.chat_id or 0) != official:
                return  # not the Official GC — ineligible
            if (m.status or "").lower() not in ("completed", "complete"):
                return

            from models import User
            u1 = session.get(User, m.user1_id) if m.user1_id else None
            u2 = session.get(User, m.user2_id) if m.user2_id else None
            humans = [u for u in (u1, u2) if u is not None and not is_ai_user(u)]
            if not humans:
                return
            is_pvp = len(humans) >= 2

            for u in humans:
                if is_pvp:
                    activity, label = X.ACT_PVP, "pvp_match"
                    xp = C.xp_for("pvp_match")
                    limit = C.daily_limit_for("pvp_match")
                else:
                    activity, label = X.ACT_AI, "ai_match"
                    xp = C.xp_for("ai_match")
                    limit = C.daily_limit_for("ai_match")
                res = X.award_xp(
                    u.id, activity, xp,
                    event_key=f"match:{match_id}:{u.id}",
                    chat_id=m.chat_id,
                    detail=("PvP match" if is_pvp else "AI match"),
                    session=session, activity_daily_limit=limit)
                if res.get("reason") == "ok" or res.get("credited"):
                    field = "pvp_matches" if is_pvp else "ai_matches"
                    X.bump_profile_counter(session, u.id, field,
                                           day_ist=X.ist_today_str())
                    ACH.check_match_achievements(session, u.id)
                    ACH.check_xp_achievements(session, u.id)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("on_match_completed hook failed (match=%s)", match_id)
        finally:
            session.close()
    except Exception:
        logger.debug("on_match_completed outer failed (non-fatal)", exc_info=True)


def on_claim_success(user_id, chat_id, *, claim_seq=None):
    """Award Community XP for a successful player claim in the Official GC.

    ``claim_seq`` (e.g. the user's new roster count) makes the event key unique
    per claim while staying idempotent across retries of the same claim.
    """
    try:
        from database import get_session
        from services import community_config_service as C
        from services import community_xp_service as X
        from services import community_achievement_service as ACH

        if not C.is_enabled():
            return
        official = _official_group_id()
        if official is None or (chat_id or 0) != official:
            return

        session = get_session()
        try:
            seq = claim_seq if claim_seq is not None else X.ist_today_str()
            res = X.award_xp(
                user_id, X.ACT_CLAIM, C.xp_for("claim"),
                event_key=f"claim:{user_id}:{seq}",
                chat_id=chat_id, detail="player claim", session=session,
                activity_daily_limit=C.daily_limit_for("claim"))
            if res.get("reason") == "ok" or res.get("credited"):
                X.bump_profile_counter(session, user_id, "claims",
                                       day_ist=X.ist_today_str())
                ACH.check_claim_achievements(session, user_id)
                ACH.check_xp_achievements(session, user_id)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("on_claim_success hook failed (user=%s)", user_id)
        finally:
            session.close()
    except Exception:
        logger.debug("on_claim_success outer failed (non-fatal)", exc_info=True)
