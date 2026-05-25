"""Daily streak tracking."""

from datetime import datetime
from config import STREAK_MISS_DAYS, STREAK_MILESTONE


def update_streak(stats) -> tuple[int, bool]:
    """Update the streak counter.

    Returns (new_streak_count, milestone_reached).

    Important: streak advances ONCE per 24-hour window, regardless of
    how many daily claims the user makes (Mini App allows 1 free + N
    ad-gated claims per cycle). Subsequent claims in same cycle return
    streak unchanged.

    To make this robust, we set `stats.daily_cycle_started_at` ourselves
    if the current claim is the first of a new cycle. This guarantees
    correctness even if the caller doesn't manage cycle state.
    """
    now = datetime.utcnow()

    # First-ever claim
    if stats.last_daily is None:
        stats.streak_count = 1
        stats.daily_cycle_started_at = now
        stats.last_daily = now  # set here too so next branch's check works
        return 1, False

    # Check if we're still inside the current cycle (multi-claim same day)
    cycle_start = getattr(stats, "daily_cycle_started_at", None)
    if cycle_start is not None:
        seconds_since_cycle_start = (now - cycle_start).total_seconds()
        if seconds_since_cycle_start < 24 * 3600:
            # Same cycle as the first claim → streak unchanged
            return stats.streak_count, False

    # New cycle — advance or reset
    days_since = (now - stats.last_daily).days
    if days_since <= STREAK_MISS_DAYS:
        stats.streak_count += 1
    else:
        stats.streak_count = 1
        stats.last_streak_reset = now

    # Start the new cycle anchor here
    stats.daily_cycle_started_at = now

    milestone = False
    if stats.streak_count >= STREAK_MILESTONE:
        milestone = True
        stats.total_streaks_completed += 1
        stats.streak_count = 0

    return stats.streak_count, milestone
