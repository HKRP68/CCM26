"""Shared, ctx-free match reward logic.

Both the bot's Telegram match flow and the Mini App use this so rewards are
identical regardless of how the match was played. No Telegram/ctx code here —
just the DB writes for coins, gems, W/L counters and the career
streak / active-day counters shown by /myprofile.
"""

import logging
from datetime import datetime, time

logger = logging.getLogger(__name__)


def is_ai_user(user) -> bool:
    """True for the AI opponent's ``users`` row (and any other system account).

    The bot side of /vsbot, /wpmbot, /lpbot and /ciplbot is stored as a real user
    so a bot match can be persisted like any other, with the sentinel
    telegram_id -1 (``services.bot_ai.BOT_TG_ID``). It plays constantly and wins
    constantly, so crediting it with matches, wins, coins or streaks puts it
    permanently on top of every leaderboard. Nothing in this module writes career
    counters for it; the leaderboards filter on the same rule
    (``User.telegram_id > 0``) so historic rows stay hidden too.
    """
    if user is None:
        return True
    tg = getattr(user, "telegram_id", None)
    return tg is None or tg <= 0


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
                              tie_user_ids=None, count_result=True) -> None:
    """Update the career streak + active-day counters after a finished match.

    This is the single place those counters move for every reward-paying flow
    (chat Challenge League, /letsplay, Super Over, Mini App), so /myprofile's
    "Current Streak / Best Streak / Active Days" stay in step with the win/loss
    totals no matter where the match was played.

    A tie touches neither streak (nobody won, nobody lost) but still counts as
    an active day for both participants. Caller commits.

    count_result: when False (a /letsplay or /wpm mismatch flagged for anti
    stat-farming) the match counts for NOTHING — no streak change and no active
    day — so this is a no-op.
    """
    from models import User

    if not count_result:
        return

    now = datetime.utcnow()
    if tie_user_ids:
        for uid in tie_user_ids:
            if not uid:
                continue
            u = session.get(User, uid)
            if is_ai_user(u):
                continue
            record_active_day(u, now)
        return

    for uid, won in ((winner_user_id, True), (loser_user_id, False)):
        if not uid:
            continue
        u = session.get(User, uid)
        if not u or is_ai_user(u):
            continue
        apply_win_streak(u, won)
        record_active_day(u, now)


def award_match_rewards_core(session, winner_user_id, loser_user_id, overs,
                             is_vsbot=False, count_result=True, match_id=None):
    """Apply match-end rewards. Returns (w_coins, w_gems, l_coins, l_gems).

    - Coins/gems scale with overs via GameConfig.
    - PvP (not vsbot) also gets the active event coin multiplier.
    - Season points are awarded separately by the ranked post-match hook.
    - The win streak and the active-day counter move here too, so every flow
      that pays match rewards keeps /myprofile's streaks honest. Flows that end
      a match WITHOUT paying rewards (a tie, a forfeit) call
      record_match_result_stats directly instead.
    - count_result: when False (a /letsplay or /wpm mismatch flagged for anti
      stat-farming), the match counts for NOTHING — no coins, no gems, no
      Win/Loss record, no matches-played, no win streak, no season points and
      no active day. Returns (0, 0, 0, 0). This is what stops players farming
      rewards/stats against a deliberately weak opponent.
    Caller commits.
    """
    from sqlalchemy import or_, update
    from config import AI_MATCH_REWARD_DAILY_CAP
    from models import Match, MatchRewardSettlement, User
    from services.config_service import get_config
    from services.activity_service import log_activity
    from services.economy_transactions import lock_user_for_update
    from services.match_outcome import END_COMPLETED, derive_end_reason

    if not count_result:
        return 0, 0, 0, 0

    settlement = None
    if match_id is not None:
        session.flush()
        if session.get_bind().dialect.name == "sqlite":
            session.execute(update(Match).where(Match.id == match_id).values(id=Match.id)
                            .execution_options(synchronize_session=False))
        match = (session.query(Match).filter_by(id=match_id)
                 .populate_existing().with_for_update().first())
        if (not match or match.status != "completed" or derive_end_reason(match)[0] != END_COMPLETED
                or match.winner_id != winner_user_id or match.loser_id != loser_user_id
                or {match.user1_id, match.user2_id} != {winner_user_id, loser_user_id}
                or winner_user_id == loser_user_id):
            return 0, 0, 0, 0
        previous = session.get(MatchRewardSettlement, match_id)
        if previous:
            return previous.amounts()
        overs = match.overs or overs
        settlement = MatchRewardSettlement(match_id=match_id, winner_id=winner_user_id,
                                           loser_id=loser_user_id, ai_match=is_vsbot)
        session.add(settlement)

    for uid in sorted({uid for uid in (winner_user_id, loser_user_id) if uid is not None}):
        user = session.get(User, uid)
        if not is_ai_user(user):
            lock_user_for_update(session, user)

    cfg = get_config(session)
    w = session.get(User, winner_user_id) if winner_user_id else None
    l = session.get(User, loser_user_id) if loser_user_id else None

    # The AI never banks anything — see is_ai_user().
    if is_ai_user(w):
        w = None
    if is_ai_user(l):
        l = None

    w_coins = int(overs * cfg["match_win_coins_per_over"])
    w_gems = max(0, int(overs * cfg["match_win_gems_per_over"]))
    l_coins = int(overs * cfg["match_loss_coins_per_over"])
    l_gems = max(0, int(overs * cfg["match_loss_gems_per_over"]))

    if settlement and is_vsbot:
        human = w or l
        if human:
            today = datetime.combine(datetime.utcnow().date(), time.min)
            paid = session.query(MatchRewardSettlement.match_id).filter(
                MatchRewardSettlement.match_id != match_id,
                MatchRewardSettlement.ai_match.is_(True),
                MatchRewardSettlement.eligibility == "eligible",
                MatchRewardSettlement.settled_at >= today,
                or_(MatchRewardSettlement.winner_id == human.id, MatchRewardSettlement.loser_id == human.id),
            ).count()
            if paid >= cfg.get("ai_match_reward_daily_cap", AI_MATCH_REWARD_DAILY_CAP):
                settlement.eligibility = "ai_daily_cap"
                w_coins = w_gems = l_coins = l_gems = 0
        if w is None:
            w_coins = w_gems = 0
        if l is None:
            l_coins = l_gems = 0

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

    if settlement:
        settlement.winner_coins, settlement.winner_gems = w_coins, w_gems
        settlement.loser_coins, settlement.loser_gems = l_coins, l_gems
    return w_coins, w_gems, l_coins, l_gems
