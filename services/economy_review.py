"""Economy reconciliation and review signals for administrators."""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_

from models import MatchRewardSettlement, User, UserRoster

logger = logging.getLogger(__name__)


def diagnostics(db):
    counts = (db.query(UserRoster.user_id, func.count(UserRoster.id).label("number"))
              .group_by(UserRoster.user_id).subquery())
    roster_count = func.coalesce(counts.c.number, 0)
    accounts = db.query(User.id, User.first_name, User.total_coins, User.total_gems,
                        User.quest_points, User.roster_count, roster_count.label("actual_roster"))
    discrepancies = (accounts.outerjoin(counts, counts.c.user_id == User.id)
                     .filter(or_(func.coalesce(User.roster_count, 0) != roster_count,
                                 User.total_coins < 0, User.total_gems < 0, User.quest_points < 0))
                     .order_by(User.id).limit(100).all())

    start = datetime.utcnow() - timedelta(days=1)
    reward = MatchRewardSettlement
    first = case((reward.winner_id < reward.loser_id, reward.winner_id), else_=reward.loser_id)
    second = case((reward.winner_id < reward.loser_id, reward.loser_id), else_=reward.winner_id)
    count = func.count(reward.match_id)
    pairs = (db.query(first.label("first_id"), second.label("second_id"), count.label("matches"),
                      func.sum(reward.winner_coins + reward.loser_coins).label("coins"))
             .filter(reward.ai_match.is_(False), reward.settled_at >= start)
             .group_by(first, second).having(count >= 5).order_by(count.desc()).limit(100).all())
    return discrepancies, pairs


def _reconcile():
    from database import get_session

    with get_session() as db:
        discrepancies, pairs = diagnostics(db)
        if discrepancies or pairs:
            logger.warning("Economy review has %s account discrepancies and %s repeat opponent pairs",
                           len(discrepancies), len(pairs))


async def reconciliation_job(_context):
    try:
        await asyncio.to_thread(_reconcile)
    except Exception:
        logger.exception("Economy reconciliation failed")
