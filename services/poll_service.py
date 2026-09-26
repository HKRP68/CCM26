"""Mini App polls, run by admins from the website.

One poll shows at a time: the newest active poll whose window contains now.
Each user votes once — the ``uq_poll_vote`` constraint is the guarantee, the
pre-check here only produces a friendly message. Results are shown to a user
after they vote, and to everyone once the poll has closed.

Services never commit; the caller does.
"""

import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MIN_OPTIONS, MAX_OPTIONS = 2, 6
QUESTION_MAX = 200
OPTION_MAX = 80


def options(poll):
    try:
        data = json.loads(poll.options_json or "[]")
        return [str(o) for o in data]
    except Exception:
        return []


def create_poll(db, *, question, option_labels, ends_at, starts_at=None,
                reward_coins=0, created_by="admin"):
    from models import Poll
    question = " ".join((question or "").split())
    labels = [" ".join(str(o).split())[:OPTION_MAX] for o in option_labels or []]
    labels = [o for o in labels if o]
    if len(question) < 5:
        raise ValueError("Question needs at least 5 characters.")
    if len(question) > QUESTION_MAX:
        raise ValueError(f"Question is too long (max {QUESTION_MAX}).")
    if not MIN_OPTIONS <= len(labels) <= MAX_OPTIONS:
        raise ValueError(f"A poll needs {MIN_OPTIONS}–{MAX_OPTIONS} options.")
    if len({o.lower() for o in labels}) != len(labels):
        raise ValueError("Options must be different from each other.")
    starts_at = starts_at or datetime.utcnow()
    if ends_at is None or ends_at <= starts_at:
        raise ValueError("The end time must be after the start time.")
    poll = Poll(question=question, options_json=json.dumps(labels),
                starts_at=starts_at, ends_at=ends_at, is_active=True,
                reward_coins=max(0, int(reward_coins or 0)),
                created_by=(created_by or "admin")[:80])
    db.add(poll)
    db.flush()
    return poll


def is_open(poll, now=None):
    now = now or datetime.utcnow()
    return bool(poll.is_active) and poll.starts_at <= now < poll.ends_at


def results(db, poll):
    from sqlalchemy import func
    from models import PollVote
    counts = dict(db.query(PollVote.option_index, func.count(PollVote.id))
                  .filter(PollVote.poll_id == poll.id)
                  .group_by(PollVote.option_index).all())
    labels = options(poll)
    total = sum(int(counts.get(i, 0)) for i in range(len(labels)))
    rows = []
    for i, label in enumerate(labels):
        n = int(counts.get(i, 0))
        rows.append({"label": label, "votes": n,
                     "pct": round(100 * n / total) if total else 0})
    return {"total": total, "options": rows}


def _my_vote(db, poll, user):
    from models import PollVote
    return (db.query(PollVote.option_index)
            .filter(PollVote.poll_id == poll.id, PollVote.user_id == user.id)
            .scalar())


def serialize(db, poll, user, now=None):
    now = now or datetime.utcnow()
    mine = _my_vote(db, poll, user)
    open_ = is_open(poll, now)
    data = {
        "id": poll.id,
        "question": poll.question,
        "options": options(poll),
        "ends_at": poll.ends_at.isoformat() + "Z",
        "ends_in_seconds": max(0, int((poll.ends_at - now).total_seconds())),
        "reward_coins": int(poll.reward_coins or 0),
        "is_open": open_,
        "my_vote": mine,
        "results": None,
    }
    if mine is not None or not open_:
        data["results"] = results(db, poll)
    return data


def active_poll(db, now=None):
    from models import Poll
    now = now or datetime.utcnow()
    return (db.query(Poll)
            .filter(Poll.is_active.is_(True), Poll.starts_at <= now, Poll.ends_at > now)
            .order_by(Poll.starts_at.desc(), Poll.id.desc())
            .first())


def active_poll_for(db, user, now=None):
    poll = active_poll(db, now)
    return serialize(db, poll, user, now) if poll else None


def vote(db, user, poll_id, option_index, now=None):
    """Record a vote and pay the reward once. Raises ValueError for the user.

    Returns ``(serialized_poll, coins_paid)``.
    """
    from sqlalchemy.exc import IntegrityError
    from models import Poll, PollVote
    now = now or datetime.utcnow()
    poll = db.get(Poll, int(poll_id))
    if poll is None or not is_open(poll, now):
        raise ValueError("This poll is closed.")
    labels = options(poll)
    try:
        idx = int(option_index)
    except (TypeError, ValueError):
        raise ValueError("Pick one of the options.")
    if not 0 <= idx < len(labels):
        raise ValueError("Pick one of the options.")
    if _my_vote(db, poll, user) is not None:
        raise ValueError("You have already voted in this poll.")
    try:
        with db.begin_nested():
            db.add(PollVote(poll_id=poll.id, user_id=user.id, option_index=idx))
            db.flush()
    except IntegrityError:
        raise ValueError("You have already voted in this poll.")
    paid = int(poll.reward_coins or 0)
    if paid > 0:
        user.total_coins = (user.total_coins or 0) + paid
        try:
            from services.activity_service import log_activity
            log_activity(db, user.id, "poll_reward",
                         f"Voted: {poll.question[:80]}", coins_change=paid)
        except Exception:
            logger.exception("poll: activity log failed")
    return serialize(db, poll, user, now), paid


def end_now(db, poll):
    now = datetime.utcnow()
    if poll.ends_at > now:
        poll.ends_at = now
    poll.is_active = False


def delete_poll(db, poll):
    from models import PollVote
    (db.query(PollVote).filter(PollVote.poll_id == poll.id)
     .delete(synchronize_session=False))
    db.delete(poll)
