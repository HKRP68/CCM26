"""Spectator predictions — the group backs a side of a live match with coins.

Anyone in the chat who is *not* playing can back one side of a live
player-vs-player match with ``/predict``. Stakes go into a pari-mutuel pool:

  • when the match is won, the backers of the winner share the whole pool in
    proportion to their stakes, plus a ``HOUSE_BONUS`` on top of their own stake
    (so a one-sided pool still pays something for being right);
  • if nobody backed the winner, or the match ends tied or without a result,
    every stake is refunded.

The pool is self-balancing — nobody sets odds — and the only coins the game
creates are the small house bonus.

Predictions close at the innings break: a stake placed once the chase is under
way would be a bet on a scoreboard everyone can already read.

Nothing here commits; callers own the transaction.
"""

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

STAKES = (100, 500, 1000, 5000)
MAX_STAKE = max(STAKES)
HOUSE_BONUS = 0.10

# Statuses a live match row can hold (anything else is finished or dead).
_LIVE_STATUSES = ("playing", "active", "in_progress", "toss", "selecting",
                  "bowling", "running", "accepted")
# How long an unsettled prediction may wait on a match before it is refunded.
STALE_AFTER = timedelta(hours=12)


class PredictionError(Exception):
    """Refusal with a message fit to show the user."""


def pool(session, match_id):
    """``{pick_user_id: {"stake": total, "count": backers}}`` for a match."""
    from sqlalchemy import func
    from models import MatchPrediction
    rows = (session.query(MatchPrediction.pick_user_id,
                          func.sum(MatchPrediction.stake),
                          func.count(MatchPrediction.id))
            .filter(MatchPrediction.match_id == match_id,
                    MatchPrediction.status != "refunded")
            .group_by(MatchPrediction.pick_user_id).all())
    return {uid: {"stake": int(total or 0), "count": int(n or 0)}
            for uid, total, n in rows}


def payout_for(stake, side_total, pool_total):
    """What a winning ``stake`` returns: its share of the pool + the bonus."""
    if side_total <= 0:
        return stake
    share = stake * pool_total / side_total
    return int(round(share + stake * HOUSE_BONUS))


def predictions_open(state):
    """True while a live match state still accepts predictions (1st innings)."""
    if not isinstance(state, dict):
        return False
    try:
        return int(state.get("innings") or 1) == 1
    except (TypeError, ValueError):
        return False


def place(session, match, user, pick_user_id, stake, state=None):
    """Stake ``stake`` coins on ``pick_user_id`` winning ``match``.

    Raises :class:`PredictionError` with a user-facing reason on refusal.
    Returns the new :class:`MatchPrediction`.
    """
    from models import MatchPrediction, User
    from services.match_rewards import is_ai_user

    if user is None:
        raise PredictionError("Use /debut first to create your account.")
    if match is None or (match.status or "") not in _LIVE_STATUSES:
        raise PredictionError("That match isn't live any more.")
    if user.id in (match.user1_id, match.user2_id):
        raise PredictionError("You can't predict your own match — go win it!")
    if pick_user_id not in (match.user1_id, match.user2_id):
        raise PredictionError("That side isn't playing this match.")
    for uid in (match.user1_id, match.user2_id):
        if is_ai_user(session.get(User, uid)):
            raise PredictionError("Predictions are only open on matches between two players.")
    # No live state means the match is over (or never started) — closed.
    if not predictions_open(state):
        raise PredictionError("Predictions closed at the innings break.")
    try:
        stake = int(stake)
    except (TypeError, ValueError):
        raise PredictionError("Pick a stake.")
    if stake <= 0 or stake > MAX_STAKE:
        raise PredictionError(f"Stakes are 1–{MAX_STAKE:,} coins.")
    existing = (session.query(MatchPrediction)
                .filter(MatchPrediction.match_id == match.id,
                        MatchPrediction.user_id == user.id).first())
    if existing is not None:
        raise PredictionError("You've already made your prediction for this match.")
    if (user.total_coins or 0) < stake:
        raise PredictionError(f"Not enough coins — you have {user.total_coins or 0:,}.")

    user.total_coins = (user.total_coins or 0) - stake
    pred = MatchPrediction(match_id=match.id, chat_id=match.chat_id, user_id=user.id,
                           pick_user_id=pick_user_id, stake=stake, status="open",
                           payout=0)
    session.add(pred)
    try:
        from services.activity_service import log_activity
        log_activity(session, user.id, "prediction_stake",
                     f"Predicted match #{match.id}", coins_change=-stake)
    except Exception:
        pass
    session.flush()
    return pred


def _refund_all(session, preds, why):
    from models import User
    now = datetime.utcnow()
    for p in preds:
        u = session.get(User, p.user_id)
        if u is not None:
            u.total_coins = (u.total_coins or 0) + p.stake
        p.status = "refunded"
        p.payout = p.stake
        p.settled_at = now
    session.flush()
    return {"settled": len(preds), "refunded": len(preds), "reason": why,
            "winners": 0, "pool": sum(p.stake for p in preds), "paid": 0,
            "top_payout": 0}


def settle(session, match):
    """Settle every open prediction on a finished ``match``. Idempotent.

    Returns a summary dict (``None`` when there was nothing to settle).
    """
    from models import MatchPrediction, User
    # Re-check in Python too: a session without autoflush can hand back rows
    # this same transaction has already settled.
    preds = [p for p in (session.query(MatchPrediction)
                         .filter(MatchPrediction.match_id == match.id,
                                 MatchPrediction.status == "open").all())
             if p.status == "open"]
    if not preds:
        return None
    winner = match.winner_id
    tie = match.margin_type == "tie" or not winner
    if tie or winner not in (match.user1_id, match.user2_id):
        return _refund_all(session, preds, "no result")

    total = sum(p.stake for p in preds)
    side = sum(p.stake for p in preds if p.pick_user_id == winner)
    if side <= 0:
        return _refund_all(session, preds, "nobody backed the winner")

    now = datetime.utcnow()
    paid = winners = top = 0
    for p in preds:
        p.settled_at = now
        if p.pick_user_id == winner:
            amount = payout_for(p.stake, side, total)
            u = session.get(User, p.user_id)
            if u is not None:
                u.total_coins = (u.total_coins or 0) + amount
                try:
                    from services.activity_service import log_activity
                    log_activity(session, u.id, "prediction_win",
                                 f"Called match #{match.id} right",
                                 coins_change=amount)
                except Exception:
                    pass
            p.status, p.payout = "won", amount
            paid += amount
            winners += 1
            top = max(top, amount)
        else:
            p.status, p.payout = "lost", 0
    session.flush()
    return {"settled": len(preds), "refunded": 0, "reason": None,
            "winners": winners, "pool": total, "paid": paid, "top_payout": top}


def refund_match(session, match_id, why="match abandoned"):
    """Refund every open prediction on ``match_id`` (abandoned/cleared match)."""
    from models import MatchPrediction
    preds = [p for p in (session.query(MatchPrediction)
                         .filter(MatchPrediction.match_id == match_id,
                                 MatchPrediction.status == "open").all())
             if p.status == "open"]
    if not preds:
        return None
    return _refund_all(session, preds, why)


def sweep(session, now=None):
    """Safety net: settle or refund predictions whose match is over.

    Flows that finish a match through the post-match hook settle there; this
    catches every other ending (a forfeit, /clearmatches, an admin force-end, a
    crash between the result and the hook). Returns ``[(match, summary)]``.
    """
    from models import Match, MatchPrediction
    from services.match_outcome import END_COMPLETED
    now = now or datetime.utcnow()
    mids = [mid for (mid,) in (session.query(MatchPrediction.match_id)
                               .filter(MatchPrediction.status == "open")
                               .distinct().all())]
    out = []
    for mid in mids:
        m = session.get(Match, mid)
        if m is None:
            continue
        status = (m.status or "")
        if status == "completed":
            if m.end_reason in (END_COMPLETED, None) and m.winner_id:
                summary = settle(session, m)
            else:
                summary = refund_match(session, mid, "no result")
        elif status in _LIVE_STATUSES and (now - (m.created_at or now)) < STALE_AFTER:
            continue
        else:
            summary = refund_match(session, mid, "match abandoned")
        if summary:
            out.append((m, summary))
    return out
