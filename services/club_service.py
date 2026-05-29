"""Clubs — player-formed teams that compete on combined season points.

Club score = sum of current members' season_points (live, reflects the active
season, resets naturally each month). Top clubs are paid at season finalize.

Rules:
  - One club per user. Founder + Member roles.
  - Create costs coins (anti-spam). Max 20 members.
  - Join via code or from the open list. Leaving has a cooldown before rejoin.
"""

import logging
import random
import string
from datetime import datetime, timedelta

from models import User, Club, ClubSeasonResult

logger = logging.getLogger(__name__)

CREATE_COST_COINS = 5000
MAX_MEMBERS = 20
LEAVE_REJOIN_COOLDOWN_HOURS = 6
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 6


def _gen_code(session):
    for _ in range(10):
        code = "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        if not session.query(Club).filter(Club.code == code).first():
            return code
    return "".join(random.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def _club_points(session, club_id):
    """Live sum of members' season points for the active season."""
    from services.season_service import current_season_key
    live_key = current_season_key()
    from sqlalchemy import func as _f
    total = (session.query(_f.coalesce(_f.sum(User.season_points), 0))
             .filter(User.club_id == club_id,
                     User.season_key == live_key)
             .scalar())
    return int(total or 0)


def create_club(session, user, name, description="", emoji="🛡️"):
    """Create a club. Returns result dict. Caller commits."""
    if user.club_id:
        return {"ok": False, "error": "already_in_club",
                "message": "Leave your current club first."}
    name = (name or "").strip()[:40]
    if len(name) < 3:
        return {"ok": False, "error": "bad_name",
                "message": "Club name must be at least 3 characters."}
    if (user.total_coins or 0) < CREATE_COST_COINS:
        return {"ok": False, "error": "insufficient_coins",
                "message": f"Creating a club costs {CREATE_COST_COINS:,} coins."}

    code = _gen_code(session)
    club = Club(
        name=name, code=code,
        description=(description or "").strip()[:160] or None,
        emoji=(emoji or "🛡️")[:10],
        founder_user_id=user.id, member_count=1, max_members=MAX_MEMBERS,
    )
    session.add(club)
    session.flush()

    user.total_coins = (user.total_coins or 0) - CREATE_COST_COINS
    user.club_id = club.id
    user.club_joined_at = datetime.utcnow()
    session.flush()  # ensure founder's club_id is visible to live-points query

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "club_create",
                     f"Created club '{name}' ({code})", coins_change=-CREATE_COST_COINS)
    except Exception:
        pass

    return {"ok": True, "club": _club_public(session, club, user)}


def join_club(session, user, code=None, club_id=None):
    """Join a club by code or id. Caller commits."""
    if user.club_id:
        return {"ok": False, "error": "already_in_club",
                "message": "Leave your current club first."}

    # Rejoin cooldown
    if user.last_club_leave:
        elapsed = (datetime.utcnow() - user.last_club_leave).total_seconds()
        cd = LEAVE_REJOIN_COOLDOWN_HOURS * 3600
        if elapsed < cd:
            mins = int((cd - elapsed) / 60)
            return {"ok": False, "error": "cooldown",
                    "message": f"You can join a club again in {mins} min."}

    club = None
    if code:
        club = session.query(Club).filter(Club.code == code.strip().upper()).first()
    elif club_id:
        club = session.query(Club).get(club_id)
    if not club:
        return {"ok": False, "error": "not_found", "message": "Club not found."}

    if club.member_count >= club.max_members:
        return {"ok": False, "error": "full",
                "message": f"{club.name} is full ({club.max_members} members)."}

    user.club_id = club.id
    user.club_joined_at = datetime.utcnow()
    club.member_count = (club.member_count or 0) + 1
    session.flush()  # ensure membership visible to live-points query

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "club_join", f"Joined club '{club.name}'")
    except Exception:
        pass

    return {"ok": True, "club": _club_public(session, club, user)}


def leave_club(session, user):
    """Leave the current club. Founder leaving disbands if last member, else
    transfers founder role to the oldest member. Caller commits."""
    if not user.club_id:
        return {"ok": False, "error": "not_in_club", "message": "You're not in a club."}

    club = session.query(Club).get(user.club_id)
    if not club:
        user.club_id = None
        return {"ok": True, "disbanded": True}

    was_founder = (club.founder_user_id == user.id)
    user.club_id = None
    user.club_joined_at = None
    user.last_club_leave = datetime.utcnow()
    club.member_count = max(0, (club.member_count or 1) - 1)
    session.flush()  # make the departure visible to successor query

    disbanded = False
    if club.member_count <= 0:
        # Last member left → disband
        session.delete(club)
        disbanded = True
    elif was_founder:
        # Transfer founder to the oldest remaining member
        successor = (session.query(User)
                     .filter(User.club_id == club.id,
                             User.id != user.id)
                     .order_by(User.club_joined_at.asc())
                     .first())
        if successor:
            club.founder_user_id = successor.id

    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "club_leave",
                     f"Left club '{club.name}'" + (" (disbanded)" if disbanded else ""))
    except Exception:
        pass

    return {"ok": True, "disbanded": disbanded}


def kick_member(session, founder, target_user_id):
    """Founder kicks a member. Caller commits."""
    if not founder.club_id:
        return {"ok": False, "error": "not_in_club"}
    club = session.query(Club).get(founder.club_id)
    if not club or club.founder_user_id != founder.id:
        return {"ok": False, "error": "not_founder",
                "message": "Only the founder can remove members."}
    if target_user_id == founder.id:
        return {"ok": False, "error": "cant_kick_self",
                "message": "Use Leave to exit your own club."}
    target = session.query(User).get(target_user_id)
    if not target or target.club_id != club.id:
        return {"ok": False, "error": "not_member", "message": "That player isn't in your club."}

    target.club_id = None
    target.club_joined_at = None
    target.last_club_leave = datetime.utcnow()
    club.member_count = max(0, (club.member_count or 1) - 1)
    return {"ok": True}


def _club_public(session, club, viewer=None):
    """Client-friendly club dict including live points + members."""
    from services.season_service import current_season_key
    live_key = current_season_key()
    members = (session.query(User)
               .filter(User.club_id == club.id)
               .order_by(User.season_points.desc())
               .all())
    member_list = []
    for m in members:
        pts = m.season_points if m.season_key == live_key else 0
        member_list.append({
            "user_id": m.id,
            "name": m.first_name or m.username or f"Player {m.id}",
            "season_points": pts or 0,
            "is_founder": (m.id == club.founder_user_id),
            "is_me": (viewer is not None and m.id == viewer.id),
        })
    return {
        "id": club.id,
        "name": club.name,
        "code": club.code,
        "description": club.description or "",
        "emoji": club.emoji or "🛡️",
        "member_count": club.member_count or len(member_list),
        "max_members": club.max_members or MAX_MEMBERS,
        "points": _club_points(session, club.id),
        "founder_user_id": club.founder_user_id,
        "is_founder": (viewer is not None and viewer.id == club.founder_user_id),
        "members": member_list,
    }


def get_my_club(session, user):
    if not user.club_id:
        return None
    club = session.query(Club).get(user.club_id)
    if not club:
        user.club_id = None
        return None
    return _club_public(session, club, user)


def get_club_leaderboard(session, limit=25):
    """Top clubs by live combined season points."""
    clubs = session.query(Club).all()
    scored = []
    for c in clubs:
        scored.append((c, _club_points(session, c.id)))
    scored.sort(key=lambda x: -x[1])
    out = []
    for idx, (c, pts) in enumerate(scored[:limit]):
        out.append({
            "rank": idx + 1,
            "club_id": c.id,
            "name": c.name,
            "emoji": c.emoji or "🛡️",
            "member_count": c.member_count or 0,
            "points": pts,
        })
    return out


def list_open_clubs(session, limit=30):
    """Public list of joinable clubs (not full)."""
    clubs = (session.query(Club)
             .filter(Club.is_open == True,
                     Club.member_count < Club.max_members)
             .order_by(Club.member_count.desc())
             .limit(limit).all())
    return [{
        "id": c.id, "name": c.name, "emoji": c.emoji or "🛡️",
        "description": c.description or "",
        "member_count": c.member_count or 0, "max_members": c.max_members or MAX_MEMBERS,
        "points": _club_points(session, c.id),
        "code": c.code,
    } for c in clubs]


def finalize_clubs_for_season(session, season_key, top_n=3, pool_per_rank=None):
    """Pay top clubs at season end + archive. Called from season finalize.
    Caller commits."""
    if pool_per_rank is None:
        # coins split among members for ranks 1/2/3
        pool_per_rank = {1: 100000, 2: 50000, 3: 25000}

    lb = get_club_leaderboard(session, limit=10)
    for entry in lb:
        rank = entry["rank"]
        if rank not in pool_per_rank:
            continue
        club = session.query(Club).get(entry["club_id"])
        if not club:
            continue
        members = session.query(User).filter(User.club_id == club.id).all()
        if not members:
            continue
        each = pool_per_rank[rank] // max(1, len(members))
        for m in members:
            m.total_coins = (m.total_coins or 0) + each
        if rank == 1:
            club.total_seasons_won = (club.total_seasons_won or 0) + 1
        session.add(ClubSeasonResult(
            season_key=season_key, club_id=club.id, club_name=club.name,
            rank=rank, points=entry["points"], member_count=len(members),
            prize_coins_each=each,
        ))
        logger.info(f"Club '{club.name}' rank {rank} season {season_key}: "
                    f"{each} coins each to {len(members)} members")


# ── Admin operations ─────────────────────────────────────────────────

def admin_list_clubs(session):
    """All clubs with live points + founder name, sorted by points desc."""
    clubs = session.query(Club).all()
    out = []
    for c in clubs:
        founder = session.query(User).get(c.founder_user_id) if c.founder_user_id else None
        out.append({
            "club": c,
            "points": _club_points(session, c.id),
            "founder_name": (founder.first_name or founder.username or f"User {founder.id}") if founder else "—",
        })
    out.sort(key=lambda x: -x["points"])
    return out


def admin_get_club(session, club_id):
    """Full club detail incl. members for the admin view."""
    club = session.query(Club).get(club_id)
    if not club:
        return None
    members = (session.query(User)
               .filter(User.club_id == club.id)
               .order_by(User.club_joined_at.asc())
               .all())
    from services.season_service import current_season_key
    live_key = current_season_key()
    mem = []
    for m in members:
        pts = m.season_points if m.season_key == live_key else 0
        mem.append({
            "user_id": m.id,
            "name": m.first_name or m.username or f"User {m.id}",
            "telegram_id": m.telegram_id,
            "season_points": pts or 0,
            "is_founder": (m.id == club.founder_user_id),
            "joined_at": m.club_joined_at,
        })
    return {"club": club, "points": _club_points(session, club.id), "members": mem}


def admin_rename_club(session, club_id, name=None, description=None, emoji=None, max_members=None):
    club = session.query(Club).get(club_id)
    if not club:
        return {"ok": False, "error": "not_found"}
    if name is not None:
        name = name.strip()[:40]
        if len(name) >= 3:
            club.name = name
    if description is not None:
        club.description = description.strip()[:160] or None
    if emoji is not None and emoji.strip():
        club.emoji = emoji.strip()[:10]
    if max_members is not None:
        try:
            mm = int(max_members)
            if 2 <= mm <= 100:
                club.max_members = mm
        except (ValueError, TypeError):
            pass
    return {"ok": True}


def admin_transfer_founder(session, club_id, new_founder_user_id):
    club = session.query(Club).get(club_id)
    if not club:
        return {"ok": False, "error": "not_found"}
    target = session.query(User).get(new_founder_user_id)
    if not target or target.club_id != club.id:
        return {"ok": False, "error": "not_member",
                "message": "That user isn't a member of this club."}
    club.founder_user_id = target.id
    return {"ok": True}


def admin_disband_club(session, club_id):
    club = session.query(Club).get(club_id)
    if not club:
        return {"ok": False, "error": "not_found"}
    # Clear all members
    members = session.query(User).filter(User.club_id == club.id).all()
    for m in members:
        m.club_id = None
        m.club_joined_at = None
    session.delete(club)
    return {"ok": True, "removed_members": len(members)}


def admin_kick(session, club_id, user_id):
    club = session.query(Club).get(club_id)
    target = session.query(User).get(user_id)
    if not club or not target or target.club_id != club.id:
        return {"ok": False, "error": "not_member"}
    target.club_id = None
    target.club_joined_at = None
    target.last_club_leave = datetime.utcnow()
    club.member_count = max(0, (club.member_count or 1) - 1)
    # If we removed the founder, promote oldest remaining
    if club.founder_user_id == user_id:
        session.flush()
        successor = (session.query(User)
                     .filter(User.club_id == club.id)
                     .order_by(User.club_joined_at.asc())
                     .first())
        if successor:
            club.founder_user_id = successor.id
        elif club.member_count <= 0:
            session.delete(club)
    return {"ok": True}
