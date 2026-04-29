"""Daily streak tracking."""

from datetime import datetime
from config import STREAK_MISS_DAYS, STREAK_MILESTONE


def update_streak(stats) -> tuple[int, bool]:
    """
    Update the streak counter.
    Returns (new_streak_count, milestone_reached).
    """
    now = datetime.utcnow()

    if stats.last_daily is None:
        stats.streak_count = 1
        return 1, False

    days_since = (now - stats.last_daily).days

    if days_since <= 1:
        stats.streak_count += 1
    elif days_since <= STREAK_MISS_DAYS:
        stats.streak_count += 1
    else:
        stats.streak_count = 1
        stats.last_streak_reset = now

    milestone = False
    if stats.streak_count >= STREAK_MILESTONE:
        milestone = True
        stats.total_streaks_completed += 1
        stats.streak_count = 0

    return stats.streak_count, milestone
