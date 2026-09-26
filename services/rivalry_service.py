"""Rivalries — the running series between two captains who keep meeting.

Every completed player-vs-player match updates the pair's series. Once a pair
has played ``RIVALRY_THRESHOLD`` matches it becomes a named rivalry, and from
then on:

  • the result card announces the series score after every match;
  • each rivalry win pays a small ``WIN_BONUS``;
  • the series is also played in rounds of ``ROUND_LENGTH`` matches, and the
    side that wins more of a round collects the ``ROUND_BONUS`` (a drawn round
    pays nobody).

The first time a pair is seen, its history is back-filled from the matches
table, so players who already have a long series start as rivals rather than
from zero. Nothing here commits — the caller owns the transaction.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

RIVALRY_THRESHOLD = 5
ROUND_LENGTH = 5
WIN_BONUS_COINS = 250
ROUND_BONUS_COINS = 3000
ROUND_BONUS_GEMS = 2


def _pair(u1, u2):
    return (u1, u2) if u1 < u2 else (u2, u1)


def _display(user):
    if user is None:
        return "Player"
    name = (user.team_name or "").strip() or (user.first_name or "").strip()
    return name or (user.username or "Player")


def rivalry_name(user_a, user_b):
    """A name for the rivalry, fixed when it forms: "A vs B Derby"."""
    return f"{_display(user_a)} vs {_display(user_b)} Derby"[:120]


def is_rivalry(row):
    return row is not None and (row.played or 0) >= RIVALRY_THRESHOLD


def _history(session, a_id, b_id, exclude_match_id=None):
    """Completed decided/tied matches between the pair, oldest first."""
    from sqlalchemy import and_, or_
    from models import Match
    from services.match_outcome import END_COMPLETED
    q = (session.query(Match)
         .filter(Match.status == "completed",
                 or_(Match.end_reason == END_COMPLETED, Match.end_reason.is_(None)),
                 or_(and_(Match.user1_id == a_id, Match.user2_id == b_id),
                     and_(Match.user1_id == b_id, Match.user2_id == a_id))))
    if exclude_match_id is not None:
        q = q.filter(Match.id != exclude_match_id)
    return q.order_by(Match.completed_at.asc().nullsfirst(), Match.id.asc()).all()


def _is_tie(match):
    return match.margin_type == "tie" or (match.winner_id is None and match.loser_id is None)


def get_rivalry(session, u1, u2):
    from models import Rivalry
    a, b = _pair(u1, u2)
    return (session.query(Rivalry)
            .filter(Rivalry.user_a_id == a, Rivalry.user_b_id == b).first())


def get_or_create(session, u1, u2, exclude_match_id=None):
    """The pair's row, back-filled from history the first time it is created."""
    from models import Rivalry
    row = get_rivalry(session, u1, u2)
    if row is not None:
        return row
    a, b = _pair(u1, u2)
    row = Rivalry(user_a_id=a, user_b_id=b, played=0, a_wins=0, b_wins=0, ties=0,
                  round_no=1, round_played=0, round_a_wins=0, round_b_wins=0,
                  rounds_a=0, rounds_b=0, streak=0)
    # Back-fill: the series so far counts, but only the running totals — the
    # bonus rounds start fresh from the moment the rivalry is recognised.
    for m in _history(session, a, b, exclude_match_id=exclude_match_id):
        row.played += 1
        if _is_tie(m):
            row.ties += 1
            row.streak_user_id, row.streak = None, 0
            continue
        if m.winner_id == a:
            row.a_wins += 1
        elif m.winner_id == b:
            row.b_wins += 1
        else:
            row.ties += 1
            continue
        if row.streak_user_id == m.winner_id:
            row.streak += 1
        else:
            row.streak_user_id, row.streak = m.winner_id, 1
    if row.played >= RIVALRY_THRESHOLD:
        from models import User
        row.became_rivalry_at = datetime.utcnow()
        row.name = rivalry_name(session.get(User, a), session.get(User, b))
    session.add(row)
    session.flush()
    return row


def _pay(session, user_id, coins, gems, why):
    from models import User
    from services.match_rewards import is_ai_user
    u = session.get(User, user_id)
    if u is None or is_ai_user(u):
        return
    u.total_coins = (u.total_coins or 0) + coins
    u.total_gems = (u.total_gems or 0) + gems
    try:
        from services.activity_service import log_activity
        log_activity(session, u.id, "rivalry_bonus", why,
                     coins_change=coins, gems_change=gems)
    except Exception:
        pass


def record_match(session, match, pay_bonuses=True):
    """Fold one finished match into the pair's series. Returns a summary dict.

    ``{"is_rivalry", "just_formed", "name", "a_id", "b_id", "a_wins", "b_wins",
    "ties", "played", "round": {...} | None, "round_result": {...} | None,
    "win_bonus": {"user_id", "coins"} | None, "streak_user_id", "streak"}``.
    Idempotent on ``match.id``. Returns None for anything that is not a
    two-human match.
    """
    from models import User
    from services.match_rewards import is_ai_user

    u1, u2 = match.user1_id, match.user2_id
    if not u1 or not u2 or u1 == u2:
        return None
    ua, ub = session.get(User, u1), session.get(User, u2)
    if is_ai_user(ua) or is_ai_user(ub):
        return None

    row = get_or_create(session, u1, u2, exclude_match_id=match.id)
    if row.last_match_id == match.id:
        return _summary(session, row, just_formed=False)

    was_rivalry = is_rivalry(row)
    tie = _is_tie(match)
    winner = None if tie else match.winner_id
    if winner not in (row.user_a_id, row.user_b_id):
        winner, tie = None, True

    row.played = (row.played or 0) + 1
    if tie:
        row.ties = (row.ties or 0) + 1
        row.streak_user_id, row.streak = None, 0
    else:
        if winner == row.user_a_id:
            row.a_wins = (row.a_wins or 0) + 1
        else:
            row.b_wins = (row.b_wins or 0) + 1
        if row.streak_user_id == winner:
            row.streak = (row.streak or 0) + 1
        else:
            row.streak_user_id, row.streak = winner, 1
    row.last_match_id = match.id

    just_formed = not was_rivalry and is_rivalry(row)
    if just_formed:
        row.became_rivalry_at = datetime.utcnow()
        row.name = rivalry_name(session.get(User, row.user_a_id),
                                session.get(User, row.user_b_id))
    elif was_rivalry and not row.name:
        row.name = rivalry_name(session.get(User, row.user_a_id),
                                session.get(User, row.user_b_id))

    win_bonus = None
    round_result = None
    # Rounds and bonuses only run once the pair is a rivalry — and the match
    # that makes them one is its first rivalry match.
    if is_rivalry(row):
        row.round_played = (row.round_played or 0) + 1
        if winner == row.user_a_id:
            row.round_a_wins = (row.round_a_wins or 0) + 1
        elif winner == row.user_b_id:
            row.round_b_wins = (row.round_b_wins or 0) + 1
        if winner and pay_bonuses and WIN_BONUS_COINS:
            _pay(session, winner, WIN_BONUS_COINS, 0,
                 f"Rivalry win bonus (match #{match.id})")
            win_bonus = {"user_id": winner, "coins": WIN_BONUS_COINS}
        if row.round_played >= ROUND_LENGTH:
            ra, rb = row.round_a_wins, row.round_b_wins
            round_winner = (row.user_a_id if ra > rb
                            else row.user_b_id if rb > ra else None)
            round_result = {"round_no": row.round_no, "a_wins": ra, "b_wins": rb,
                            "winner_id": round_winner, "coins": 0, "gems": 0}
            if round_winner == row.user_a_id:
                row.rounds_a = (row.rounds_a or 0) + 1
            elif round_winner == row.user_b_id:
                row.rounds_b = (row.rounds_b or 0) + 1
            if round_winner and pay_bonuses:
                _pay(session, round_winner, ROUND_BONUS_COINS, ROUND_BONUS_GEMS,
                     f"Rivalry round {row.round_no} won")
                round_result["coins"] = ROUND_BONUS_COINS
                round_result["gems"] = ROUND_BONUS_GEMS
            row.round_no = (row.round_no or 1) + 1
            row.round_played = row.round_a_wins = row.round_b_wins = 0

    session.flush()
    out = _summary(session, row, just_formed=just_formed)
    out["win_bonus"] = win_bonus
    out["round_result"] = round_result
    return out


def _summary(session, row, just_formed=False):
    return {
        "is_rivalry": is_rivalry(row),
        "just_formed": just_formed,
        "name": row.name,
        "a_id": row.user_a_id,
        "b_id": row.user_b_id,
        "a_wins": row.a_wins or 0,
        "b_wins": row.b_wins or 0,
        "ties": row.ties or 0,
        "played": row.played or 0,
        "round": ({"round_no": row.round_no, "played": row.round_played,
                   "a_wins": row.round_a_wins, "b_wins": row.round_b_wins}
                  if is_rivalry(row) else None),
        "rounds_a": row.rounds_a or 0,
        "rounds_b": row.rounds_b or 0,
        "streak_user_id": row.streak_user_id,
        "streak": row.streak or 0,
        "win_bonus": None,
        "round_result": None,
    }


def rivalries_for(session, user_id, limit=10):
    """The user's rivalries, most played first: ``[(Rivalry, opponent User)]``."""
    from sqlalchemy import or_
    from models import Rivalry, User
    rows = (session.query(Rivalry)
            .filter(or_(Rivalry.user_a_id == user_id, Rivalry.user_b_id == user_id),
                    Rivalry.played >= RIVALRY_THRESHOLD)
            .order_by(Rivalry.played.desc(), Rivalry.updated_at.desc())
            .limit(limit).all())
    out = []
    for r in rows:
        opp_id = r.user_b_id if r.user_a_id == user_id else r.user_a_id
        out.append((r, session.get(User, opp_id)))
    return out
