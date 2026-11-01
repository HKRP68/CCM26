"""Monthly season — scoring, lazy rollover, and payouts.

Season key is 'YYYY-MM' (UTC). The live season is whichever matches the current
month. Players accumulate `season_points` (and `season_wins`) during the month.

Rollover is LAZY: any access to season data first calls `ensure_current_season`,
which detects a month change, finalizes the previous season (pays top finishers,
archives standings), and opens the new one. A background job also calls this
hourly as a safety net so rollover happens even if no one is active at midnight.

Eligible ranked wins and capped game activity bonuses feed the season. The
ranked service calculates opponent-strength and repeat-opponent adjustments
before points are applied inside the idempotent post-match transaction.
Activity bonuses have their own
daily limits and never increment season wins or change ranked rating. Casual
match results, losses and draws award no season points.
"""

import logging
from datetime import datetime

from models import User, Season, SeasonStanding

logger = logging.getLogger(__name__)


def current_season_key():
    return datetime.utcnow().strftime("%Y-%m")


def _month_name(season_key):
    try:
        dt = datetime.strptime(season_key, "%Y-%m")
        return dt.strftime("%B %Y") + " Season"
    except Exception:
        return season_key + " Season"


def ensure_current_season(session):
    """Return the live Season row, creating + finalizing previous as needed.

    Caller commits.
    """
    live_key = current_season_key()
    live = session.query(Season).filter(Season.season_key == live_key).first()
    if live:
        return live

    # The live season row doesn't exist yet. Finalize any prior un-finalized
    # seasons, then create the live one.
    prior = (session.query(Season)
             .filter(Season.season_key != live_key,
                     Season.finalized == False)
             .all())
    for s in prior:
        try:
            _finalize_season(session, s)
        except Exception:
            logger.exception(f"Failed to finalize season {s.season_key}")

    live = Season(
        season_key=live_key,
        name=_month_name(live_key),
        started_at=datetime.utcnow(),
    )
    session.add(live)
    session.flush()
    return live


def _season_news(session, season, podium):
    """CMU News for the month's top three managers. Never raises."""
    try:
        from services.display_name import manager_name
        from services.news_service import auto_story
        medals = ("🥇", "🥈", "🥉")
        lines = [f"{medals[i]} {manager_name(u)} — {u.season_points or 0} pts, "
                 f"{u.season_wins or 0} wins" for i, u in enumerate(podium)]
        label = getattr(season, "name", None) or season.season_key
        auto_story(session, "season_winners", f"season:{season.season_key}",
                   f"👑 {manager_name(podium[0])} tops the {label} season!",
                   f"The {label} season is over. Here is the podium:\n\n"
                   + "\n".join(lines)
                   + "\n\nPrizes have been paid. A new season starts now — good luck!",
                   kicker="Season winners")
    except Exception:
        logger.exception("season news failed for %s", season.season_key)


def _finalize_season(session, season):
    """Pay top finishers + archive standings for a season being closed."""
    if season.finalized:
        return

    # Rank users by season_points (only those whose season_key matches this
    # season — others have stale/zero points for it).
    rows = (session.query(User)
            .filter(User.season_key == season.season_key,
                    User.season_points > 0)
            .order_by(User.season_points.desc(),
                      User.season_wins.desc())
            .limit(50)
            .all())

    for idx, u in enumerate(rows):
        rank = idx + 1
        prize_coins = 0
        prize_gems = 0
        if rank == 1:
            prize_coins, prize_gems = season.prize_top1, season.prize_gems_top1
        elif rank == 2:
            prize_coins, prize_gems = season.prize_top2, season.prize_gems_top2
        elif rank == 3:
            prize_coins, prize_gems = season.prize_top3, season.prize_gems_top3
        elif rank <= 10:
            prize_coins = season.prize_top10

        if prize_coins:
            u.total_coins = (u.total_coins or 0) + prize_coins
        if prize_gems:
            u.total_gems = (u.total_gems or 0) + prize_gems

        session.add(SeasonStanding(
            season_key=season.season_key, user_id=u.id, rank=rank,
            points=u.season_points or 0, wins=u.season_wins or 0,
            prize_coins=prize_coins, prize_gems=prize_gems,
        ))

        # Notify-worthy activity log
        try:
            from services.activity_service import log_activity
            if prize_coins or prize_gems:
                log_activity(session, u.id, "season_reward",
                             f"Season {season.season_key} rank #{rank}: "
                             f"+{prize_coins} coins, +{prize_gems} gems",
                             coins_change=prize_coins)
        except Exception:
            pass

    season.finalized = True
    season.ended_at = datetime.utcnow()
    session.flush()
    logger.info(f"Finalized season {season.season_key}: {len(rows)} ranked")

    if rows:
        _season_news(session, season, rows[:3])

    # Pay top clubs too (combined season points)
    try:
        from services.club_service import finalize_clubs_for_season
        finalize_clubs_for_season(session, season.season_key)
    except Exception:
        logger.exception("Club season finalize failed (non-fatal)")

    # Archive + pay the ranked ladder by division.
    try:
        from services.ranked_service import finalize_season as _finalize_ranked
        with session.begin_nested():
            _finalize_ranked(session, season.season_key)
    except Exception:
        logger.exception("Ranked season finalize failed (non-fatal)")


def _sync_user_season(session, user, live_key):
    """Reset a user's season counters if they belong to an old season."""
    if user.season_key != live_key:
        _credit_season_points(session, user, live_key, points=0)


def _credit_season_points(session, user, live_key, *, points, wins=0):
    """Atomically credit a current-season balance, resetting stale counters.

    SQL arithmetic preserves simultaneous ranked and activity awards even when
    another request loaded its User object before the first request committed.
    """
    from sqlalchemy import case, func
    session.query(User).filter(User.id == user.id).update({
        User.season_points: case(
            (User.season_key == live_key, func.coalesce(User.season_points, 0)),
            else_=0) + points,
        User.season_wins: case(
            (User.season_key == live_key, func.coalesce(User.season_wins, 0)),
            else_=0) + wins,
        User.season_key: live_key,
    }, synchronize_session="fetch")


def add_ranked_season_points(session, user, *, ranked_result):
    """Credit this player's points from an eligible ranked result.

    Called only by the idempotent post-match hook, in its savepoint. Keeping
    the calculated award in the ranked result prevents independent reward
    flows from granting fixed points or crediting an unrated match.
    """
    if not ranked_result or not ranked_result.get("rated"):
        return 0
    player = ranked_result.get("players", {}).get(user.id, {})
    points = player.get("season_points", 0)
    if points <= 0:
        return 0
    live = ensure_current_season(session)
    _credit_season_points(session, user, live.season_key, points=points, wins=1)
    return points


def get_season_leaderboard(session, limit=20):
    """Return (season, [ {rank, user_id, name, points, wins, is_me?} ]).

    Only includes users whose season_key matches the live season.
    """
    live = ensure_current_season(session)
    rows = (session.query(User)
            .filter(User.season_key == live.season_key,
                    User.season_points > 0)
            .order_by(User.season_points.desc(),
                      User.season_wins.desc())
            .limit(limit)
            .all())
    out = []
    for idx, u in enumerate(rows):
        out.append({
            "rank": idx + 1,
            "user_id": u.id,
            "name": u.first_name or u.username or f"Player {u.id}",
            "points": u.season_points or 0,
            "wins": u.season_wins or 0,
        })
    return live, out


def get_user_season_rank(session, user):
    """Return the user's current rank (1-based) or None if unranked."""
    live = ensure_current_season(session)
    if user.season_key != live.season_key or (user.season_points or 0) <= 0:
        return None
    higher = (session.query(User)
              .filter(User.season_key == live.season_key,
                      User.season_points > (user.season_points or 0))
              .count())
    return higher + 1


def get_season_info(session, user=None):
    """Return live season meta + (optionally) the user's standing."""
    live = ensure_current_season(session)
    info = {
        "season_key": live.season_key,
        "name": live.name,
        "started_at": live.started_at.isoformat() if live.started_at else None,
        "prizes": {
            "top1": {"coins": live.prize_top1, "gems": live.prize_gems_top1},
            "top2": {"coins": live.prize_top2, "gems": live.prize_gems_top2},
            "top3": {"coins": live.prize_top3, "gems": live.prize_gems_top3},
            "top10": {"coins": live.prize_top10, "gems": 0},
        },
    }
    from services.season_activity_service import get_activity_progress
    info["activity"] = get_activity_progress(session, user.id if user is not None else None)
    if user is not None:
        _sync_user_season(session, user, live.season_key)
        info["me"] = {
            "points": user.season_points or 0,
            "wins": user.season_wins or 0,
            "rank": get_user_season_rank(session, user),
        }
    return info
