"""Shared, ctx-free match reward logic.

Both the bot's Telegram match flow and the Mini App use this so rewards are
identical regardless of how the match was played. No Telegram/ctx code here —
just the DB writes for coins, gems, season points, and W/L counters.
"""

import logging

logger = logging.getLogger(__name__)


def award_match_rewards_core(session, winner_user_id, loser_user_id, overs,
                             is_vsbot=False):
    """Apply match-end rewards. Returns (w_coins, w_gems, l_coins, l_gems).

    - Coins/gems scale with overs via GameConfig.
    - PvP (not vsbot) also gets the active event coin multiplier + season points.
    - vsbot gives the same coin/gem rewards but NO season points (matches the
      bot's existing policy).
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
