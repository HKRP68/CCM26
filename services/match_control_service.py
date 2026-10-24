"""Persistent, global rolling allowance and audit for manual match controls.

Pending reservations count too, so concurrent commands cannot overspend. A
database write on the per-user account serializes checks on SQLite/Postgres.
Successful mutations call finish_in_session in their own transaction, making
the Elo debit and the audit record atomic with the match row change.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import update, or_
from sqlalchemy.exc import IntegrityError

LIMIT = 5
COOLDOWN = 60
PENALTY = 5
UNDO_TTL = timedelta(minutes=10)


class ControlDenied(ValueError):
    pass


def _lock_account(session, tg_id):
    from models import MatchControlAccount
    if session.get(MatchControlAccount, tg_id) is None:
        try:
            with session.begin_nested():
                session.add(MatchControlAccount(telegram_id=tg_id, blocked=False))
                session.flush()
        except IntegrityError:
            pass  # Another worker created the same account.
    session.execute(update(MatchControlAccount)
                    .where(MatchControlAccount.telegram_id == tg_id)
                    .values(telegram_id=tg_id))
    return session.get(MatchControlAccount, tg_id, populate_existing=True)


def reserve(tg_id, chat_id, command, *, now=None):
    from database import get_session
    from models import MatchControlAction, User
    now = now or datetime.utcnow()
    session = get_session()
    try:
        account = _lock_account(session, tg_id)
        if account.blocked:
            raise ControlDenied("Your pause/clear access has been revoked. Contact a bot admin.")
        recent = (session.query(MatchControlAction)
                  .filter(MatchControlAction.telegram_id == tg_id,
                          MatchControlAction.created_at > now - timedelta(hours=24),
                          or_(MatchControlAction.outcome == "success",
                              MatchControlAction.outcome == "pending"))
                  .order_by(MatchControlAction.created_at).all())
        if len(recent) >= LIMIT:
            ready = recent[0].created_at + timedelta(hours=24)
            raise ControlDenied(f"You have used all {LIMIT} pause + clear actions in 24 hours. "
                                f"Next use: {ready:%d %b %H:%M} UTC.")
        if recent and (now - recent[-1].created_at).total_seconds() < COOLDOWN:
            raise ControlDenied("Wait 60 seconds between pause/clear actions.")
        user = session.query(User).filter(User.telegram_id == tg_id).first()
        if user is None:
            raise ControlDenied("Run /debut first so this action can be recorded against your rating.")
        row = MatchControlAction(telegram_id=tg_id, user_id=user.id,
                                 chat_id=chat_id, command=command,
                                 outcome="pending", created_at=now)
        session.add(row)
        session.commit()
        return row.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def finish_in_session(session, action_id, details, backup=None):
    from models import MatchControlAction, RankedRating
    from services.ranked_service import get_rating, _live_key
    row = session.query(MatchControlAction).filter_by(id=action_id).with_for_update().one()
    if row.outcome != "pending":
        raise ControlDenied("This action has already been processed.")
    # Lock an existing ladder row before reading/changing it.
    session.query(RankedRating).filter_by(user_id=row.user_id).with_for_update().first()
    live_key = _live_key(session)
    rating = get_rating(session, row.user_id, create=True, live_key=live_key)
    if rating.season_key != live_key:
        raise ControlDenied("The ranked season is still updating. Please try again shortly.")
    rating.rating -= PENALTY
    row.rating_delta = -PENALTY
    row.outcome = "success"
    row.finished_at = datetime.utcnow()
    row.details_json = json.dumps(details)
    if backup is not None:
        row.backup_json = json.dumps(backup)


def fail(action_id, reason):
    from database import get_session
    from models import MatchControlAction
    session = get_session()
    try:
        session.query(MatchControlAction).filter_by(id=action_id, outcome="pending").update({
            "outcome": "failed", "details_json": json.dumps({"reason": str(reason)}),
            "finished_at": datetime.utcnow()}, synchronize_session=False)
        session.commit()
    finally:
        session.close()


def history(tg_id, limit=10):
    from database import get_session
    from models import MatchControlAction
    session = get_session()
    try:
        return [{"id": r.id, "command": r.command, "outcome": r.outcome,
                 "created_at": r.created_at, "chat_id": r.chat_id,
                 "rating_delta": r.rating_delta, "undone_at": r.undone_at,
                 "details": json.loads(r.details_json or "{}")}
                for r in session.query(MatchControlAction).filter_by(telegram_id=tg_id)
                .order_by(MatchControlAction.id.desc()).limit(limit)]
    finally:
        session.close()


def set_blocked(tg_id, blocked):
    from database import get_session
    session = get_session()
    try:
        _lock_account(session, tg_id).blocked = blocked
        session.commit()
    finally:
        session.close()
