"""Admin-selected, round-scoped access to tournament gameplay only.

The command selects accounts explicitly: inactivity alone never changes access.
Callers commit changes, so the restriction and its audit entry are atomic.
No process cache is used; block/unblock is visible to bot and web workers on
their next gameplay request.
"""

import json
from datetime import datetime, timedelta, timezone


DEFAULT_REASON = "Please play your pending tournament matches."
IST = timezone(timedelta(hours=5, minutes=30))
OPPONENT_NOTICE = (
    "🔒 A participant has tournament-only access. They must finish their required "
    "open-round fixtures; other game modes will then unlock automatically."
)
RESTRICTION_NOTICE = (
    "🔒 Tournament-only access — ACTIVE\n"
    "Lets Play, bot matches and other casual modes are blocked.\n"
    "Official tournament fixtures remain available.\n"
    "Finish all your required open-round fixtures to unlock other modes automatically."
)


def format_ist(value):
    """Database timestamps are UTC; always label the player's local display."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")


def warning_deadline(user):
    """Only an explicit warning permits other modes before its deadline."""
    starts = getattr(user, "tournament_only_starts_at", None)
    if not getattr(user, "tournament_only_warning", False) or starts is None:
        return None
    if starts.tzinfo is not None:
        starts = starts.astimezone(timezone.utc).replace(tzinfo=None)
    return starts if starts > datetime.utcnow() else None


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
    return warning_deadline(user) is None


def casual_game_message(session, *users, actor_telegram_id=None):
    """Shared entry/launch check, including the opponent in a casual game."""
    for user in users:
        if user is None or not is_restricted(user, session=session):
            continue
        if actor_telegram_id is None or user.telegram_id == actor_telegram_id:
            return restriction_message(user, session=session)
        return OPPONENT_NOTICE
    return None


def restriction_alert(user, session):
    """Compact button popup, with progress and IST time within Telegram's limit."""
    lines = ["🔒 Tournament-only — ACTIVE"]
    if getattr(user, "tournament_only_fixture_ids_json", None):
        count = len(remaining_fixtures(session, user))
        lines.append(f"Finish {count} open-round fixture(s) to unlock.")
    active_at = (getattr(user, "tournament_only_starts_at", None)
                 if getattr(user, "tournament_only_warning", False)
                 else getattr(user, "tournament_only_at", None))
    if active_at:
        lines.append(f"Since: {format_ist(active_at)}")
    lines.append("Details: /touronly status")
    return "\n".join(lines)


def restriction_message(user_or_id=None, session=None):
    """Plain text, suitable for replies and JSON (escape when rendering HTML)."""
    user = _resolve_user(user_or_id, session)
    reason = (getattr(user, "tournament_only_reason", None) or "").strip()
    notice = RESTRICTION_NOTICE
    starts = warning_deadline(user)
    if starts:
        notice = ("⚠️ Tournament warning — GRACE PERIOD\n"
                  "Other game modes are still available until the deadline.\n"
                  f"Block starts: {format_ist(starts)}\n"
                  "Finish the required fixtures before then to avoid the block.")
    else:
        active_at = (getattr(user, "tournament_only_starts_at", None)
                     if getattr(user, "tournament_only_warning", False)
                     else getattr(user, "tournament_only_at", None))
        if active_at:
            notice += f"\nActive since: {format_ist(active_at)}"
    if session is not None and getattr(user, "tournament_only_fixture_ids_json", None):
        rows = remaining_fixtures(session, user)
        from models import TournamentTeam
        team_ids = {tid for fx in rows for tid in (fx.team1_id, fx.team2_id) if tid}
        names = {team.id: team.name for team in session.query(TournamentTeam)
                 .filter(TournamentTeam.id.in_(team_ids)).all()}
        notice += f"\n\nRemaining fixtures: {len(rows)}"
        for fx in rows[:6]:
            tour = fx.tournament
            notice += (f"\n• {tour.name[:60]} (#{fx.tournament_id}) · Round {fx.round_no} · "
                       f"M{fx.match_no or fx.id}\n  "
                       f"{names.get(fx.team1_id, 'Team')[:50]} vs {names.get(fx.team2_id, 'Team')[:50]}")
        if len(rows) > 6:
            notice += f"\n… and {len(rows) - 6} more; see your tournament fixtures."
        if any(fx.tournament.kind == "letsplay" for fx in rows):
            notice += "\nPlay official Lets Play fixtures with /lptour."
        if any(fx.tournament.kind != "letsplay" for fx in rows):
            notice += "\nUse your league's tournament command for official fixtures."
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
    user.tournament_only_warning = False
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


def schedule_restriction(session, user, *, admin_telegram_id, reason=None, grace_hours=0):
    """Bind the warning to today's released schedule, never to future rounds."""
    from models import Tournament, TournamentTeam, TournamentMatch
    from services import league_schedule_service as schedule, tournament_service as ts
    if not 0 <= grace_hours <= 168:
        raise ValueError("Grace hours must be between 0 and 168.")
    if user.tournament_only and user.tournament_only_fixture_ids_json and remaining_fixtures(session, user):
        if grace_hours:
            raise ValueError("A round restriction already exists. Use block to activate it now, or unblock before issuing a new warning.")
        # Activate an existing warning without changing its fixture snapshot.
        # Repeating block must never add next-round fixtures or extend grace.
        from models import AdminLog
        if reason and len(reason.strip()) > 500:
            raise ValueError("The reason must be 500 characters or fewer.")
        if reason:
            user.tournament_only_reason = reason.strip()
        user.tournament_only_warning = False
        user.tournament_only_starts_at = datetime.utcnow()
        user.tournament_only_by = int(admin_telegram_id)
        session.add(AdminLog(action="tournament_only_activate", target_type="user",
                             target_id=user.id, detail=f"Telegram admin {admin_telegram_id}: block active immediately; existing fixtures retained."))
        session.flush()
        return user
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
    set_restriction(session, user, enabled=True, reason=reason,
                    admin_telegram_id=admin_telegram_id)
    user.tournament_only_fixture_ids_json = json.dumps(sorted({fx.id for fx in fixtures}))
    user.tournament_only_starts_at = datetime.utcnow() + timedelta(hours=grace_hours)
    user.tournament_only_warning = grace_hours > 0
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
        user.tournament_only_warning = False
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
