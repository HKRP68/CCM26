"""Monthly season — scoring, lazy rollover, and payouts.

Season key is 'YYYY-MM' (UTC). The live season is whichever matches the current
month. Players accumulate `season_points` (and `season_wins`) during the month.

Rollover is LAZY: any access to season data first calls `ensure_current_season`,
which detects a month change, finalizes the previous season (pays top finishers,
archives standings), and opens the new one. A background job also calls this
hourly as a safety net so rollover happens even if no one is active at midnight.

Scoring (tunable):
  - Each season point comes from gameplay; callers use add_season_points().
  - Quick Match does NOT feed the season (it's casual). Real wins, quests, etc.
    can call add_season_points() to contribute.
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

    # Pay top clubs too (combined season points)
    try:
        from services.club_service import finalize_clubs_for_season
        finalize_clubs_for_season(session, season.season_key)
    except Exception:
        logger.exception("Club season finalize failed (non-fatal)")


def _sync_user_season(session, user, live_key):
    """Reset a user's season counters if they belong to an old season."""
    if user.season_key != live_key:
        user.season_key = live_key
        user.season_points = 0
        user.season_wins = 0


def add_season_points(session, user, points=0, wins=0):
    """Add to a user's season tally, auto-resetting if they're on an old season.
    Caller commits."""
    live = ensure_current_season(session)
    _sync_user_season(session, user, live.season_key)
    if points:
        user.season_points = (user.season_points or 0) + points
    if wins:
        user.season_wins = (user.season_wins or 0) + wins


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
    if user is not None:
        _sync_user_season(session, user, live.season_key)
        info["me"] = {
            "points": user.season_points or 0,
            "wins": user.season_wins or 0,
            "rank": get_user_season_rank(session, user),
        }
    return info


def safe_add_season_points(session, user, points=0, wins=0):
    """Exception-swallowing wrapper for use inside other handlers."""
    try:
        add_season_points(session, user, points=points, wins=wins)
    except Exception:
        logger.exception("add_season_points failed")
