"""Admin-selected, round-scoped access to tournament gameplay only.

The command selects accounts explicitly: inactivity alone never changes access.
Callers commit changes, so the restriction and its audit entry are atomic.
No process cache is used; block/unblock is visible to bot and web workers on
their next gameplay request.
"""

import json
from datetime import datetime, timedelta


DEFAULT_REASON = "Please play your pending tournament matches."
RESTRICTION_NOTICE = (
    "🔒 Tournament-only access. Other game modes are restricted.\n"
    "Finish all your required open-round fixtures to unlock other modes automatically."
)


def _resolve_user(user_or_id, session):
    if isinstance(user_or_id, int):
        if session is None:
            raise ValueError("An internal user ID requires a database session.")
        from models import User
        return session.get(User, user_or_id)
    return user_or_id


def is_restricted(user_or_id, session=None):
    """Check a User or an internal users.id (never a Telegram ID)."""
    user = _resolve_user(user_or_id, session)
    if user is None or not getattr(user, "tournament_only", False):
        return False
    if session is None:
        from sqlalchemy.orm import object_session
        from sqlalchemy import inspect
        if inspect(user, raiseerr=False) is not None:
            session = object_session(user)
    if session is not None and getattr(user, "tournament_only_fixture_ids_json", None):
        if not remaining_fixtures(session, user):
            return False
    starts = getattr(user, "tournament_only_starts_at", None)
    return not starts or datetime.utcnow() >= starts


def restriction_message(user_or_id=None, session=None):
    """Plain text, suitable for replies and JSON (escape when rendering HTML)."""
    user = _resolve_user(user_or_id, session)
    reason = (getattr(user, "tournament_only_reason", None) or "").strip()
    notice = RESTRICTION_NOTICE
    starts = getattr(user, "tournament_only_starts_at", None)
    if starts and starts > datetime.utcnow():
        notice = f"⚠️ Tournament warning. Other modes will lock at {starts:%Y-%m-%d %H:%M} UTC unless you finish your open-round fixtures."
    if session is not None and getattr(user, "tournament_only_fixture_ids_json", None):
        rows = remaining_fixtures(session, user)
        notice += "\nRequired fixtures: " + ", ".join(
            f"tournament #{fx.tournament_id}, round {fx.round_no}, M{fx.match_no or fx.id}"
            for fx in rows[:12])
        if len(rows) > 12:
            notice += f" (+{len(rows) - 12} more; see your tournament fixtures)"
    return notice + (f"\nReason: {reason}" if reason else "")


def set_restriction(session, user, *, enabled, reason=None,
                    admin_telegram_id):
    """Set access and append an admin audit entry; the caller commits."""
    from models import AdminLog

    reason = (reason or "").strip()
    if len(reason) > 500:
        raise ValueError("The reason must be 500 characters or fewer.")
    enabled = bool(enabled)
    user.tournament_only = enabled
    user.tournament_only_reason = (reason or DEFAULT_REASON) if enabled else None
    user.tournament_only_at = datetime.utcnow() if enabled else None
    user.tournament_only_by = int(admin_telegram_id) if enabled else None
    user.tournament_only_fixture_ids_json = None
    user.tournament_only_starts_at = None
    detail = f"Telegram admin {admin_telegram_id}: " + (
        user.tournament_only_reason if enabled else (reason or "Restriction removed")
    )
    session.add(AdminLog(
        action="tournament_only_block" if enabled else "tournament_only_unblock",
        target_type="user", target_id=user.id,
        target_name=(user.username or user.first_name or str(user.telegram_id))[:150],
        detail=detail[:500],
    ))
    session.flush()
    return user


def remaining_fixtures(session, user):
    """Snapshot requirements, excluding completed/explicitly withdrawn fixtures."""
    from models import TournamentMatch, Tournament
    ids = json.loads(user.tournament_only_fixture_ids_json or "[]")
    return (session.query(TournamentMatch).join(Tournament)
            .filter(TournamentMatch.id.in_(ids),
                    TournamentMatch.status.notin_(("completed", "cancelled")),
                    Tournament.status.notin_(("completed", "cancelled")))
            .order_by(TournamentMatch.tournament_id, TournamentMatch.round_no,
                      TournamentMatch.match_no).all())


def schedule_restriction(session, user, *, admin_telegram_id, reason=None, grace_hours=24):
    """Bind the warning to today's released schedule, never to future rounds."""
    from models import Tournament, TournamentTeam, TournamentMatch
    from services import league_schedule_service as schedule, tournament_service as ts
    if not 0 <= grace_hours <= 168:
        raise ValueError("Grace hours must be between 0 and 168.")
    fixtures = []
    for tour in session.query(Tournament).filter(Tournament.status == "active").all():
        teams = [team.id for team in session.query(TournamentTeam)
                 .filter_by(tournament_id=tour.id).all()
                 if user.telegram_id in ({team.user_tg_id, team.owner_tg_id}
                                         | set(ts.co_owner_ids(team)))]
        if not teams:
            continue
        first = schedule.current_round(session, tour.id)
        boundary = schedule.open_round(session, tour.id)
        if boundary is None:
            boundary = first
        from sqlalchemy import or_
        rows = (session.query(TournamentMatch)
                .filter(TournamentMatch.tournament_id == tour.id,
                        or_(TournamentMatch.team1_id.in_(teams), TournamentMatch.team2_id.in_(teams)),
                        TournamentMatch.team1_id.isnot(None), TournamentMatch.team2_id.isnot(None),
                        TournamentMatch.status.in_(("scheduled", "live"))).all())
        fixtures.extend(fx for fx in rows if (
            fx.stage in ("league", "group") and boundary is not None
            and fx.round_no <= boundary
            or first is None and fx.stage not in ("league", "group")))
    if not fixtures:
        raise ValueError("This player has no unfinished fixtures in a currently open round. No restriction applied.")
    if user.tournament_only and user.tournament_only_fixture_ids_json and remaining_fixtures(session, user):
        raise ValueError("A round restriction already exists. Use status, or unblock before issuing a new warning.")
    set_restriction(session, user, enabled=True, reason=reason,
                    admin_telegram_id=admin_telegram_id)
    user.tournament_only_fixture_ids_json = json.dumps(sorted({fx.id for fx in fixtures}))
    user.tournament_only_starts_at = datetime.utcnow() + timedelta(hours=grace_hours)
    session.flush()
    return user


def refresh_restrictions(session, *, upgrade_legacy=False):
    """Clear completed scopes atomically with results; caller commits."""
    from models import User, AdminLog
    query = session.query(User).filter(User.tournament_only.is_(True))
    if not upgrade_legacy:
        query = query.filter(User.tournament_only_fixture_ids_json.isnot(None))
    for user in query.all():
        if not user.tournament_only_fixture_ids_json:
            try:
                schedule_restriction(session, user,
                    admin_telegram_id=user.tournament_only_by or 0,
                    reason=user.tournament_only_reason, grace_hours=0)
            except ValueError as exc:
                if "no unfinished fixtures" not in str(exc):
                    raise
        if remaining_fixtures(session, user):
            continue
        user.tournament_only = False
        user.tournament_only_reason = None
        user.tournament_only_at = None
        user.tournament_only_by = None
        user.tournament_only_starts_at = None
        user.tournament_only_fixture_ids_json = None
        session.add(AdminLog(action="tournament_only_auto_unblock", target_type="user",
                            target_id=user.id, detail="Open-round requirements finished or withdrawn."))


async def refresh_job(context):
    import asyncio
    def run():
        from database import get_session
        session = get_session()
        try:
            refresh_restrictions(session, upgrade_legacy=True)
            session.commit()
        finally:
            session.close()
    await asyncio.to_thread(run)


def is_tournament_match(session, match_or_id):
    """Use server-owned match/fixture records to recognize official games.

    Friendly tours, standalone super overs and drafts are not official
    tournaments merely because they use the same match engine.
    """
    from models import Match, TournamentMatch

    match = (session.get(Match, match_or_id)
             if isinstance(match_or_id, int) else match_or_id)
    if match is None:
        return False
    if getattr(match, "tournament_id", None):
        return True
    match_id = getattr(match, "id", None)
    return bool(match_id and session.query(TournamentMatch.id)
                .filter(TournamentMatch.match_id == match_id).first())
