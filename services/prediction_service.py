"""Spectator predictions — retired.

``/predict`` let spectators stake coins on a live match. Players didn't take to
it, so the command, its buttons and its settlement are gone. What remains is
the one thing still owed: any stake that was open when the feature was removed
is handed back, by :func:`refund_all_open`, which the bot runs once at start-up.
The ``match_predictions`` rows are kept as history.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def refund_all_open(session):
    """Refund every prediction still open. Returns how many. Caller commits.

    Idempotent: a refunded row is no longer ``open``, so a second run finds
    nothing.
    """
    from models import MatchPrediction, User
    preds = (session.query(MatchPrediction)
             .filter(MatchPrediction.status == "open").all())
    now = datetime.utcnow()
    for p in preds:
        u = session.get(User, p.user_id)
        if u is not None:
            u.total_coins = (u.total_coins or 0) + (p.stake or 0)
            try:
                from services.activity_service import log_activity
                log_activity(session, u.id, "prediction_refund",
                             f"Prediction on match #{p.match_id} refunded "
                             f"(predictions were removed)",
                             coins_change=p.stake or 0)
            except Exception:
                pass
        p.status = "refunded"
        p.payout = p.stake or 0
        p.settled_at = now
    session.flush()
    if preds:
        logger.info("Refunded %s open prediction(s) after /predict was removed",
                    len(preds))
    return len(preds)
