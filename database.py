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
        MessageTemplate, Bowlout, BowloutBall,
        UserReport, ShotProbability, BotChat, Broadcast, PendingUndo,
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
        "is_banned": "BOOLEAN DEFAULT FALSE",
        "ban_reason": "VARCHAR(500)",
        "banned_at": "TIMESTAMP",
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
        "scorecard_color_inn1": "VARCHAR(9) DEFAULT '#c41e3a'",
        "scorecard_color_inn2": "VARCHAR(9) DEFAULT '#00c9a7'",
        # Maintenance mode
        "is_maintenance": "BOOLEAN DEFAULT FALSE",
        "maintenance_message": "TEXT",
        "maintenance_until": "TIMESTAMP",
        "maintenance_started_at": "TIMESTAMP",
        "maintenance_bypass_ids": "VARCHAR(500)",
    }
    for col, coltype in new_gameconfig_cols.items():
        _try_add("game_config", col, coltype)

    # Player versions support
    _try_add("players", "parent_player_id", "INTEGER")

    # Pack table additions (versions filtering)
    _try_add("packs", "main_filter_mode", "VARCHAR(10) DEFAULT 'rating'")
    _try_add("packs", "main_versions_json", "VARCHAR(500)")
    _try_add("user_quest_progress", "assigned", "BOOLEAN DEFAULT TRUE")
    _try_add("users", "pack_pity_counter", "INTEGER DEFAULT 0")

    # Backfill/normalize for Postgres + SQLite: ensure non-null and true by default
    for sql in (
        "UPDATE user_quest_progress SET assigned = TRUE WHERE assigned IS NULL",
        "ALTER TABLE user_quest_progress ALTER COLUMN assigned SET DEFAULT TRUE",
        "ALTER TABLE user_quest_progress ALTER COLUMN assigned SET NOT NULL",
    ):
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            # SQLite and older schemas may not support ALTER COLUMN forms; safe to ignore.
            pass

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

    # Seed default GSpin rewards (idempotent — only if table is empty)
    try:
        from models import GSpinReward
        sess = SessionLocal()
        try:
            if sess.query(GSpinReward).count() == 0:
                defaults = [
                    # weight mirrors old config: 58/24/13/4.2/0.8
                    dict(label="Coin Stash", emoji="🟥", color="C41E3A", weight=580,
                         sort_order=10, reward_type="coins",
                         amount_min=5000, amount_max=10000),
                    dict(label="Common Pull", emoji="🟨", color="FFD93D", weight=240,
                         sort_order=20, reward_type="player",
                         player_rating_min=65, player_rating_max=78),
                    dict(label="Gem Drop", emoji="🟦", color="2196F3", weight=130,
                         sort_order=30, reward_type="gems",
                         amount_min=10, amount_max=500),
                    dict(label="Rare Pull", emoji="🟩", color="2ECC71", weight=42,
                         sort_order=40, reward_type="player",
                         player_rating_min=79, player_rating_max=84),
                    dict(label="Epic Pull", emoji="⭐", color="9B59B6", weight=8,
                         sort_order=50, reward_type="player",
                         player_rating_min=85, player_rating_max=90),
                ]
                for d in defaults:
                    sess.add(GSpinReward(**d))
                sess.commit()
                import logging
                logging.getLogger(__name__).info(f"Seeded {len(defaults)} gspin rewards")
        except Exception:
            sess.rollback()
        finally:
            sess.close()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("GSpin reward seed skipped (non-fatal)")

    # Seed default BotCommand + CommandReward rows (idempotent)
    try:
        from models import BotCommand, CommandReward
        sess = SessionLocal()
        try:
            # Default command catalog
            defaults = [
                # (key, name, aliases, description, category, cooldown, sort, rewards-dict)
                ("debut", "/debut", "d",
                 "Sign up for the league. One-time. Grants starter pack.",
                 "onboarding", 0, 10,
                 dict(coin_amount=5000, gem_amount=100, player_count=1,
                      player_rating_min=80, player_rating_max=89)),
                ("claim", "/claim", "c",
                 "Pull a free random player. 1-hour cooldown.",
                 "economy", 3600, 20,
                 dict(coin_amount=500, player_count=1,
                      player_rating_min=50, player_rating_max=100)),
                ("daily", "/daily", "dl",
                 "Daily login bonus. Coins + 2 players + streak rewards.",
                 "economy", 86400, 30,
                 dict(coin_amount=5000, player_count=2,
                      player_rating_min=50, player_rating_max=100,
                      milestone_bonus_coins=10000, milestone_bonus_gems=20,
                      milestone_bonus_player_min=85, milestone_bonus_player_max=100,
                      milestone_every_n=14)),
                ("gspin", "/gspin", "gs",
                 "Spin the wheel. Rewards configured in /gspin-rewards.",
                 "economy", 28800, 40, dict()),
            ]
            n_cmd = 0; n_rwd = 0
            for key, name, aliases, desc, cat, cd, srt, rwd in defaults:
                if not sess.query(BotCommand).filter(BotCommand.command_key == key).first():
                    sess.add(BotCommand(
                        command_key=key, display_name=name, aliases=aliases,
                        description=desc, category=cat, cooldown_seconds=cd,
                        sort_order=srt, enabled=True))
                    n_cmd += 1
                if not sess.query(CommandReward).filter(CommandReward.command_key == key).first():
                    sess.add(CommandReward(command_key=key, **rwd))
                    n_rwd += 1
            if n_cmd or n_rwd:
                sess.commit()
                import logging
                logging.getLogger(__name__).info(
                    f"Seeded {n_cmd} commands + {n_rwd} reward rows")
        except Exception:
            sess.rollback()
        finally:
            sess.close()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Command seed skipped (non-fatal)")

    # Seed default ShotProbability rows from SHOT_MODS (idempotent)
    try:
        from models import ShotProbability
        from services.probability_engine import SHOT_MODS as _SM
        sess = SessionLocal()
        try:
            descriptions = {
                "Drive": "Safe & productive — boundary potential, low risk",
                "Cut": "Off-side scoring shot",
                "Pull": "Cross-bat to short ball — high-risk reward",
                "Leg Glance": "Safest shot — singles & touches",
                "Flick": "Wristy leg-side scoring shot",
                "Sweep": "Spinner-killer — risky against pace",
                "Switch Hit": "All-or-nothing reverse shot",
                "Slog": "Max boundaries, max risk",
                "Loft": "Aerial big hit over the infield",
                "Defend": "Block — burn balls, very safe",
                "Leave": "Let it pass — no runs, very low risk",
            }
            n = 0
            for shot, mods in _SM.items():
                if sess.query(ShotProbability).filter(
                        ShotProbability.shot_name == shot).first():
                    continue
                row = ShotProbability(
                    shot_name=shot,
                    mod_dot=float(mods.get("dot", 0)),
                    mod_1=float(mods.get("1", 0)),
                    mod_2=float(mods.get("2", 0)),
                    mod_3=float(mods.get("3", 0)),
                    mod_4=float(mods.get("4", 0)),
                    mod_6=float(mods.get("6", 0)),
                    mod_wicket=float(mods.get("W", 0)),
                    mod_extras=float(mods.get("extras", 0)),
                    description=descriptions.get(shot, ""),
                    enabled=True,
                )
                sess.add(row)
                n += 1
            if n:
                sess.commit()
                import logging
                logging.getLogger(__name__).info(f"Seeded {n} shot probabilities")
        except Exception:
            sess.rollback()
            import logging
            logging.getLogger(__name__).exception("ShotProbability seed failed")
        finally:
            sess.close()
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "ShotProbability seed skipped (non-fatal)")


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
