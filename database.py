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
    from models import (  # noqa: F401
        User, Player, UserRoster, UserStats, Trade, ActivityLog,
        PlayerGameStats, Match, AdminLog,
        Trait, PlayerTrait, TraitInventory, TraitMarket, TraitDaily,
        PlayerMarket, BotTeam, BotTeamPlayer,
        MatchState, Quest, UserQuestProgress,
        UserAchievement, PlayerFormHistory, CommentaryEntry, PlayerImage,
        NotificationSchedule, NotificationLog, ClaimRarityTier, GameConfig,
        MessageTemplate,
        GlobalPlayerMarket, GlobalTraitMarket, MarketPurchase,
    )
    Base.metadata.create_all(bind=engine)
    _migrate_add_columns()
    _seed_traits()


def _seed_traits():
    """Idempotent trait seed. Inserts missing traits only."""
    from models import Trait
    from services.trait_service import TRAIT_DEFINITIONS
    session = SessionLocal()
    try:
        existing = {t.name for t in session.query(Trait).all()}
        added = 0
        for td in TRAIT_DEFINITIONS:
            if td["name"] not in existing:
                session.add(Trait(
                    name=td["name"],
                    category=td["category"],
                    description=td["description"],
                    emoji=td["emoji"],
                    effect_key=td["effect_key"],
                    is_active=True,
                ))
                added += 1
        if added:
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _migrate_add_columns():
    """Add any missing columns in-place. Safe to run every start.

    IMPORTANT: each ALTER runs in its own transaction so a failure on one
    column (e.g. it already exists) does NOT abort the whole migration.
    Without this, Postgres rolls back the whole transaction on first error
    and subsequent columns silently never get added.
    """
    new_user_cols = {
        "matches_played": "INTEGER DEFAULT 0",
        "matches_won": "INTEGER DEFAULT 0",
        "matches_lost": "INTEGER DEFAULT 0",
        "win_streak": "INTEGER DEFAULT 0",
        "best_streak": "INTEGER DEFAULT 0",
        "active_days": "INTEGER DEFAULT 0",
        "last_match_date": "TIMESTAMP",
        "quest_points": "INTEGER DEFAULT 0",
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

    def _try_add(table, col, coltype):
        # Each attempt is independent — failure on this column doesn't poison others
        for sql in (
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}",
            f"ALTER TABLE {table} ADD COLUMN {col} {coltype}",
        ):
            try:
                with engine.begin() as conn:
                    conn.execute(text(sql))
                return  # success
            except Exception:
                continue  # try next form, or just give up

    for col, coltype in new_user_cols.items():
        _try_add("users", col, coltype)
    for col, coltype in new_match_cols.items():
        _try_add("matches", col, coltype)

    # GameConfig: new simulation-tuning columns
    new_gameconfig_cols = {
        "sim_dot_adjust": "FLOAT DEFAULT -8.0",
        "sim_one_adjust": "FLOAT DEFAULT 5.0",
        "sim_two_adjust": "FLOAT DEFAULT 2.0",
        "sim_four_adjust": "FLOAT DEFAULT 0.0",
        "sim_six_adjust": "FLOAT DEFAULT 0.0",
        "sim_wicket_adjust": "FLOAT DEFAULT 0.0",
        "sim_extras_adjust": "FLOAT DEFAULT 0.0",
        "market_min_rating": "INTEGER DEFAULT 87",
        "market_default_slots": "INTEGER DEFAULT 6",
        "market_last_refresh_at": "TIMESTAMP",
        "market_refresh_hour_ist": "INTEGER DEFAULT 0",
        "trait_market_default_slots": "INTEGER DEFAULT 5",
        "trait_market_last_refresh_at": "TIMESTAMP",
    }
    for col, coltype in new_gameconfig_cols.items():
        _try_add("game_config", col, coltype)

    # Player versions support
    _try_add("players", "parent_player_id", "INTEGER")

    # Pack table additions (versions filtering)
    _try_add("packs", "main_filter_mode", "VARCHAR(10) DEFAULT 'rating'")
    _try_add("packs", "main_versions_json", "VARCHAR(500)")

    # ─────────────────────────────────────────────────────────────
    # Player versioning: name was originally UNIQUE, but with versions
    # multiple rows legitimately share a name (one base + N variants).
    # Migration:
    #   1. Drop any unique index/constraint on players.name
    #   2. Recreate ix_players_name as a non-unique index
    #   3. Ensure (name, version) is unique so we don't dup-create variants
    # Best-effort across Postgres + SQLite. Each statement wrapped in its
    # own transaction so a failure doesn't poison the rest.
    # ─────────────────────────────────────────────────────────────
    migration_sql = [
        # Postgres: drop any legacy named unique constraints
        "ALTER TABLE players DROP CONSTRAINT IF EXISTS players_name_key",
        "ALTER TABLE players DROP CONSTRAINT IF EXISTS uq_players_name",
        # Drop the unique index variant (this is what the error message references)
        "DROP INDEX IF EXISTS ix_players_name",
        # Recreate as a NON-UNIQUE index (kept for query performance on name lookups)
        "CREATE INDEX IF NOT EXISTS ix_players_name ON players (name)",
        # Composite unique on (name, version) — prevents creating two
        # 'Abhishek Sharma' rows with version='IPL 2026' but allows
        # one with version='Base card' and one with version='IPL 2026'.
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_players_name_version "
        "ON players (name, version)",
    ]
    for sql in migration_sql:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception as e:
            # SQLite doesn't support ALTER TABLE DROP CONSTRAINT — that's expected,
            # don't spam the log. Only log unexpected errors.
            err = str(e).lower()
            if "drop constraint" in err or "syntax error" in err:
                continue
            import logging
            logging.getLogger(__name__).warning(
                f"migration step skipped ({sql[:60]}…): {e}"
            )

    # Seed default packs (idempotent)
    try:
        from services.pack_service import seed_default_packs
        sess = SessionLocal()
        try:
            n = seed_default_packs(sess)
            sess.commit()
            if n:
                import logging
                logging.getLogger(__name__).info(f"Seeded {n} default packs")
        except Exception:
            sess.rollback()
        finally:
            sess.close()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Pack seed skipped (non-fatal)")


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
