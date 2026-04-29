"""Log all user actions to ActivityLog table."""

from models import ActivityLog


def log_activity(session, user_id: int, action: str, detail: str = "",
                 coins_change: int = 0, gems_change: int = 0,
                 player_name: str = None, player_rating: int = None):
    """Write one activity row. Call before session.commit()."""
    entry = ActivityLog(
        user_id=user_id,
        action=action,
        detail=detail,
        coins_change=coins_change,
        gems_change=gems_change,
        player_name=player_name,
        player_rating=player_rating,
    )
    session.add(entry)
