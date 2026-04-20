"""Database engine, session factory, and initialisation."""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from config import DATABASE_URL

connect_args = {}
if "sqlite" in DATABASE_URL:
    connect_args["check_same_thread"] = False

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db():
    from models import User, Player, UserRoster, UserStats, Trade, ActivityLog, PlayerGameStats, Match, AdminLog  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _migrate_add_columns()


def _migrate_add_columns():
    """Add any missing columns in-place. Safe to run every start."""
    new_user_cols = {
        "matches_played": "INTEGER DEFAULT 0",
        "matches_won": "INTEGER DEFAULT 0",
        "matches_lost": "INTEGER DEFAULT 0",
        "win_streak": "INTEGER DEFAULT 0",
        "best_streak": "INTEGER DEFAULT 0",
        "active_days": "INTEGER DEFAULT 0",
        "last_match_date": "TIMESTAMP",
    }
    new_match_cols = {
        "winner_id": "INTEGER",
        "loser_id": "INTEGER",
        "margin_type": "VARCHAR(20)",
        "margin_value": "INTEGER",
        "result_message_id": "BIGINT",
        "inn1_runs": "INTEGER",
        "inn1_wickets": "INTEGER",
        "inn2_runs": "INTEGER",
        "inn2_wickets": "INTEGER",
        "potm_player_id": "INTEGER",
        "potm_impact": "INTEGER",
    }

    try:
        with engine.begin() as conn:
            for col, coltype in new_user_cols.items():
                try:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {coltype}"))
                except Exception:
                    # SQLite doesn't support IF NOT EXISTS on ADD COLUMN, try without
                    try:
                        conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {coltype}"))
                    except Exception:
                        pass  # column already exists
            for col, coltype in new_match_cols.items():
                try:
                    conn.execute(text(f"ALTER TABLE matches ADD COLUMN IF NOT EXISTS {col} {coltype}"))
                except Exception:
                    try:
                        conn.execute(text(f"ALTER TABLE matches ADD COLUMN {col} {coltype}"))
                    except Exception:
                        pass
    except Exception:
        pass  # migration is best-effort, don't crash startup


def reset_db():
    """Drop ALL tables and recreate. Destroys all data."""
    from models import User, Player, UserRoster, UserStats, Trade, ActivityLog, PlayerGameStats, Match  # noqa: F401
    from sqlalchemy import text
    tables = ["matches", "player_game_stats", "activity_log", "trades", "user_roster", "user_rosters", "user_stats", "users", "players"]
    with engine.begin() as conn:
        if "postgresql" in DATABASE_URL:
            for t in tables:
                conn.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
        else:
            # SQLite: disable FK checks, drop normally
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            for t in tables:
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
            conn.execute(text("PRAGMA foreign_keys = ON"))
    Base.metadata.create_all(bind=engine)


def get_session():
    return SessionLocal()
