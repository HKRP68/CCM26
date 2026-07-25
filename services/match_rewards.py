"""Shared, ctx-free match reward logic.

Both the bot's Telegram match flow and the Mini App use this so rewards are
identical regardless of how the match was played. No Telegram/ctx code here —
just the DB writes for coins, gems, season points, W/L counters and the career
streak / active-day counters shown by /myprofile.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def record_active_day(user, when=None) -> bool:
    """Count today as an active day for ``user`` (once per UTC day).

    ``active_days`` is "days with at least 1 match", so it may only advance when
    the user's last recorded match was on an earlier date. Always refreshes
    ``last_match_date``. Returns True when the counter advanced.
    """
    if user is None:
        return False
    now = when or datetime.utcnow()
    last = getattr(user, "last_match_date", None)
    advanced = False
    if not last or last.date() != now.date():
        user.active_days = (user.active_days or 0) + 1
        advanced = True
    user.last_match_date = now
    return advanced


def apply_win_streak(user, won: bool) -> None:
    """Advance or reset a user's match-winning streak and keep the best one.

    ``win_streak`` is the CURRENT run of wins (reset to 0 by any defeat);
    ``best_streak`` is the highest it has ever reached.
    """
    if user is None:
        return
    if won:
        user.win_streak = (user.win_streak or 0) + 1
        user.best_streak = max(user.best_streak or 0, user.win_streak)
    else:
        user.win_streak = 0


def record_match_result_stats(session, winner_user_id, loser_user_id,
                              tie_user_ids=None) -> None:
    """Update the career streak + active-day counters after a finished match.

    This is the single place those counters move for every reward-paying flow
    (chat Challenge League, /letsplay, Super Over, Mini App), so /myprofile's
    "Current Streak / Best Streak / Active Days" stay in step with the win/loss
    totals no matter where the match was played.

    A tie touches neither streak (nobody won, nobody lost) but still counts as
    an active day for both participants. Caller commits.
    """
    from models import User

    now = datetime.utcnow()
    if tie_user_ids:
        for uid in tie_user_ids:
            if not uid:
                continue
            u = session.query(User).get(uid)
            record_active_day(u, now)
        return

    for uid, won in ((winner_user_id, True), (loser_user_id, False)):
        if not uid:
            continue
        u = session.query(User).get(uid)
        if not u:
            continue
        apply_win_streak(u, won)
        record_active_day(u, now)


def award_match_rewards_core(session, winner_user_id, loser_user_id, overs,
                             is_vsbot=False):
    """Apply match-end rewards. Returns (w_coins, w_gems, l_coins, l_gems).

    - Coins/gems scale with overs via GameConfig.
    - PvP (not vsbot) also gets the active event coin multiplier + season points.
    - vsbot gives the same coin/gem rewards but NO season points (matches the
      bot's existing policy).
    - The win streak and the active-day counter move here too, so every flow
      that pays match rewards keeps /myprofile's streaks honest. Flows that end
      a match WITHOUT paying rewards (a tie, a forfeit) call
      record_match_result_stats directly instead.
    Caller commits.
    """
    from models import User
    from services.config_service import get_config
    from services.activity_service import log_activity

    cfg = get_config(session)
    w = session.query(User).get(winner_user_id) if winner_user_id else None
    l = session.query(User).get(loser_user_id) if loser_user_id else None

    w_coins = int(overs * cfg["match_win_coins_per_over"])
    w_gems = max(0, int(overs * cfg["match_win_gems_per_over"]))
    l_coins = int(overs * cfg["match_loss_coins_per_over"])
    l_gems = max(0, int(overs * cfg["match_loss_gems_per_over"]))

    if not is_vsbot:
        try:
            from services.event_service import apply_coin_multiplier
            w_coins, _ = apply_coin_multiplier(session, w_coins)
            l_coins, _ = apply_coin_multiplier(session, l_coins)
        except Exception:
            logger.exception("event multiplier failed (non-fatal)")

    if w:
        w.total_coins = (w.total_coins or 0) + w_coins
        w.total_gems = (w.total_gems or 0) + w_gems
        w.matches_played = (w.matches_played or 0) + 1
        w.matches_won = (w.matches_won or 0) + 1
        apply_win_streak(w, True)
        record_active_day(w)
        try:
            log_activity(session, w.id, "match_reward",
                         f"Win reward: +{w_coins} coins, +{w_gems} gems",
                         coins_change=w_coins, gems_change=w_gems)
        except Exception:
            pass
    if l:
        l.total_coins = (l.total_coins or 0) + l_coins
        l.total_gems = (l.total_gems or 0) + l_gems
        l.matches_played = (l.matches_played or 0) + 1
        l.matches_lost = (l.matches_lost or 0) + 1
        apply_win_streak(l, False)
        record_active_day(l)
        try:
            log_activity(session, l.id, "match_reward",
                         f"Loss reward: +{l_coins} coins, +{l_gems} gems",
                         coins_change=l_coins, gems_change=l_gems)
        except Exception:
            pass

    # Season points — PvP only (matches existing policy)
    if not is_vsbot:
        try:
            from services.season_service import safe_add_season_points
            if w:
                safe_add_season_points(session, w, points=25, wins=1)
            if l:
                safe_add_season_points(session, l, points=5)
        except Exception:
            logger.exception("season points failed (non-fatal)")

    return w_coins, w_gems, l_coins, l_gems
