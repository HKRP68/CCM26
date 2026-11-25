"""Community leaderboards.

Because every awardable event writes a :class:`models.CommunityXPLedger` row
(activity + delta + UTC timestamp), almost every leaderboard — Community XP,
eligible chat messages, completed matches, successful claims, mini-game wins
(total and per game) — is one grouped query over the ledger with an IST period
bound. That keeps period handling uniform (Today / This Week / This Month /
All Time) and retains full history. Check-in streak reads the profile, and
Fastest Fingers records read the games table.

Tie-breaking is deterministic: higher value first, then lower ``user_id``
(earliest-registered) — and for "fastest" boards, lower time first.
"""

from __future__ import annotations

from sqlalchemy import func

PERIODS = ("today", "week", "month", "all")

# category key → (label, kind, params)
CATEGORIES = {
    "cxp":        ("Community XP", "ledger_sum", None),
    "messages":   ("Chat Messages", "ledger_count", ["chat"]),
    "matches":    ("Completed Matches", "ledger_count", ["pvp_match", "ai_match"]),
    "claims":     ("Successful Claims", "ledger_count", ["claim"]),
    "wins":       ("Mini-game Wins", "ledger_count",
                   ["game_mystery", "game_oddoneout", "game_wordle",
                    "game_fastestfingers", "game_rating", "game_cricketer"]),
    "mystery":    ("Mystery Card Wins", "ledger_count", ["game_mystery"]),
    "oddoneout":  ("Odd One Out Wins", "ledger_count", ["game_oddoneout"]),
    "wordle":     ("Cricket Wordle Wins", "ledger_count", ["game_wordle"]),
    "fastestfingers": ("Fastest Fingers Wins", "ledger_count", ["game_fastestfingers"]),
    "rating":     ("Guess the Rating Wins", "ledger_count", ["game_rating"]),
    "cricketer":  ("Guess the Cricketer Wins", "ledger_count", ["game_cricketer"]),
    "streak":     ("Check-in Streak", "profile_streak", None),
    "ff_records": ("Fastest Fingers Records", "ff_fastest", None),
}


def _bound(period, now=None):
    from services import community_xp_service as X
    return X.period_bound_utc(period, now)


def _name_for(user):
    if user is None:
        return "Unknown"
    if user.username:
        return f"@{user.username}"
    return user.first_name or f"Manager {user.id}"


def _rows_ledger(session, *, kind, activities, period, now):
    from models import CommunityXPLedger
    bound = _bound(period, now)
    if kind == "ledger_sum":
        value = func.coalesce(func.sum(CommunityXPLedger.delta), 0)
        q = (session.query(CommunityXPLedger.user_id, value.label("v"))
             .filter(CommunityXPLedger.delta > 0))
    else:  # ledger_count
        value = func.count(CommunityXPLedger.id)
        q = (session.query(CommunityXPLedger.user_id, value.label("v"))
             .filter(CommunityXPLedger.activity.in_(activities)))
    if bound is not None:
        q = q.filter(CommunityXPLedger.created_at >= bound)
    q = q.group_by(CommunityXPLedger.user_id).having(value > 0)
    return q


def _rows_profile_streak(session):
    from models import CommunityProfile
    return (session.query(CommunityProfile.user_id,
                          CommunityProfile.current_streak.label("v"))
            .filter(CommunityProfile.current_streak > 0))


def _rows_ff_fastest(session, period, now):
    from models import CommunityGame
    bound = _bound(period, now)
    q = (session.query(CommunityGame.winner_user_id.label("user_id"),
                       func.min(CommunityGame.winner_response_ms).label("v"))
         .filter(CommunityGame.game_type == "fastestfingers",
                 CommunityGame.status == "closed",
                 CommunityGame.winner_user_id.isnot(None),
                 CommunityGame.winner_response_ms.isnot(None)))
    if bound is not None:
        q = q.filter(CommunityGame.closed_at >= bound)
    return q.group_by(CommunityGame.winner_user_id)


def leaderboard(session, category="cxp", period="all", *, limit=10, offset=0,
                user_id=None, now=None):
    """Return a leaderboard page + the requesting user's rank.

    Result: ``{category, label, period, total, rows:[{rank,user_id,name,
    telegram_id,value}], me:{rank,value} | None}``.
    """
    from models import User
    category = category if category in CATEGORIES else "cxp"
    period = period if period in PERIODS else "all"
    label, kind, params = CATEGORIES[category]

    ascending = (kind == "ff_fastest")
    if kind in ("ledger_sum", "ledger_count"):
        base = _rows_ledger(session, kind=kind, activities=params,
                            period=period, now=now)
    elif kind == "profile_streak":
        base = _rows_profile_streak(session)
    else:
        base = _rows_ff_fastest(session, period, now)

    sub = base.subquery()
    # Deterministic ordering with user_id tiebreak.
    order_val = sub.c.v.asc() if ascending else sub.c.v.desc()
    ranked = (session.query(sub.c.user_id, sub.c.v)
              .order_by(order_val, sub.c.user_id.asc()).all())

    total = len(ranked)
    # Build id→rank map (dense by position; ties keep stable order).
    rank_of = {uid: i + 1 for i, (uid, _v) in enumerate(ranked)}

    page = ranked[offset:offset + limit]
    uids = [uid for uid, _v in page]
    users = {u.id: u for u in session.query(User).filter(User.id.in_(uids)).all()} if uids else {}
    rows = []
    for i, (uid, v) in enumerate(page):
        u = users.get(uid)
        rows.append({
            "rank": offset + i + 1,
            "user_id": uid,
            "telegram_id": u.telegram_id if u else None,
            "name": _name_for(u),
            "value": int(v or 0),
        })

    me = None
    if user_id is not None and user_id in rank_of:
        myval = next((v for uid, v in ranked if uid == user_id), 0)
        me = {"rank": rank_of[user_id], "value": int(myval or 0)}

    return {"category": category, "label": label, "period": period,
            "total": total, "rows": rows, "me": me, "ascending": ascending}
