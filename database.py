"""Database engine, session factory, and initialisation."""

import os

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from config import DATABASE_URL

connect_args = {}
if "sqlite" in DATABASE_URL:
    connect_args["check_same_thread"] = False

# Neon (and most managed Postgres) closes idle TCP connections after ~5 min.
# Settings tuned for low-traffic Telegram bot on Neon's free tier:
#   - pool_pre_ping: tests connection with SELECT 1 before use. Cheap, reliable.
#   - pool_recycle: proactively close connections older than 240s so we don't
#     hit the server's idle-kill (saves one "broken pipe" round-trip per kill).
#   - pool_size + max_overflow are environment-tunable so the bot can handle
#     concurrent Telegram updates without creating unbounded connections.
_is_postgres = ("postgres" in DATABASE_URL.lower() and "sqlite" not in DATABASE_URL.lower())

if _is_postgres:
    # The Telegram application handles multiple updates concurrently, so keep
    # enough database connections available that fast commands do not queue
    # behind one slow DB/image-heavy command. Values can still be tuned from
    # the host environment for small/free database plans.
    pool_size = int(os.getenv("DB_POOL_SIZE", "10"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    pool_timeout = int(os.getenv("DB_POOL_TIMEOUT", "10"))
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=240,          # < Neon's idle disconnect (~5min)
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        connect_args=connect_args,
    )
else:
    # SQLite (local dev) — keep simple
    engine = create_engine(DATABASE_URL, echo=False,
                           pool_pre_ping=True, connect_args=connect_args)

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
        ChallengeMode, ChallengeLeague, ChallengeTeam, ChallengePlayer, UserTeamLastXI,
        CLTour, CLTourMatch,
        Tournament, TournamentTeam, TournamentMatch, TournamentPlayerStats,
        FantasyLeague, FantasyMatch, FantasyPlayerScore,
        FantasyEntry, FantasyPick, FantasyLeaguePlayer, FantasyCountryRule,
        FantasyRoleRule, EventMedia,
        Giveaway, GiveawayEntry,
    )
    import logging
    import time as _time
    log = logging.getLogger(__name__)

    t0 = _time.perf_counter()
    Base.metadata.create_all(bind=engine)
    t1 = _time.perf_counter()
    _migrate_add_columns()
    t2 = _time.perf_counter()
    _seed_traits()
    _seed_competition_templates()
    t3 = _time.perf_counter()
    # Boot time is the bot's whole downtime on every restart/redeploy, so keep
    # it visible in the logs — a regression here is a regression in uptime.
    log.info(
        "init_db timings: create_all %.1fs | migrate %.1fs | seed %.1fs | total %.1fs",
        t1 - t0, t2 - t1, t3 - t2, t3 - t0,
    )


def _seed_competition_templates():
    """Idempotent seed for default competition templates. Only inserts ones
    that don't already exist (by name). Admin can edit or delete these."""
    from models import CompetitionTemplate
    DEFAULTS = [
        {
            "name": "🎁 Weekly Small",
            "description": "7-day invite sprint with modest top prizes",
            "duration_days": 7,
            "prize_top1": 50000, "prize_top2": 25000, "prize_top3": 10000,
            "prize_per_invite": 200, "prize_per_invite_gems": 0,
            "sort_order": 10,
        },
        {
            "name": "🏆 Monthly Standard",
            "description": "30-day standard competition with strong prize pool",
            "duration_days": 30,
            "prize_top1": 200000, "prize_top2": 100000, "prize_top3": 50000,
            "prize_per_invite": 500, "prize_per_invite_gems": 0,
            "sort_order": 20,
        },
        {
            "name": "💎 Premium Sprint",
            "description": "14-day high-stakes contest with gem bonuses",
            "duration_days": 14,
            "prize_top1": 500000, "prize_top2": 250000, "prize_top3": 100000,
            "prize_per_invite": 1000, "prize_per_invite_gems": 5,
            "sort_order": 30,
        },
        {
            "name": "🔥 Per-Invite Only",
            "description": "Pure participation reward — no top prizes",
            "duration_days": 30,
            "prize_top1": 0, "prize_top2": 0, "prize_top3": 0,
            "prize_per_invite": 1000, "prize_per_invite_gems": 0,
            "sort_order": 40,
        },
    ]
    session = SessionLocal()
    try:
        existing = {t.name for t in session.query(CompetitionTemplate).all()}
        added = 0
        for d in DEFAULTS:
            if d["name"] not in existing:
                session.add(CompetitionTemplate(**d))
                added += 1
        if added:
            session.commit()
            import logging
            logging.getLogger("database").info(f"Seeded {added} default competition templates")
    except Exception:
        session.rollback()
        import logging
        logging.getLogger("database").exception("Failed to seed competition templates")
    finally:
        session.close()


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


def _load_existing_columns():
    """Return {table_name: {column_name, ...}} for the live schema.

    Read in ONE round trip on Postgres. This is what makes a warm start fast:
    ``_try_add`` consults this map and issues no SQL at all for the ~110
    columns that already exist, instead of paying a connect + BEGIN + ALTER +
    COMMIT round trip each. Returns ``None`` if the schema can't be read, in
    which case ``_try_add`` falls back to attempting every ALTER as before.
    """
    import logging
    try:
        if _is_postgres:
            out = {}
            with engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema()"
                ))
                for table, col in rows:
                    out.setdefault(table, set()).add(col)
            return out
        # SQLite (local dev): reflect per table. Cheap — it's a local file.
        from sqlalchemy import inspect as _sa_inspect
        insp = _sa_inspect(engine)
        return {
            t: {c["name"] for c in insp.get_columns(t)}
            for t in insp.get_table_names()
        }
    except Exception:
        logging.getLogger(__name__).warning(
            "Could not read existing schema; falling back to blind ALTERs",
            exc_info=True)
        return None


_MIGRATION_STATE_TABLE = "schema_migration_state"


def _migration_signature_matches(key, statements):
    """True if this exact statement list already ran successfully before.

    The stored signature is a hash of the statements themselves, so editing or
    adding a statement automatically invalidates the marker and the batch runs
    again on the next boot. Nothing here is required for correctness — every
    statement is idempotent — it only lets a warm start skip the round trips.
    """
    import hashlib
    sig = hashlib.sha256("\n".join(statements).encode()).hexdigest()
    try:
        with engine.connect() as conn:
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {_MIGRATION_STATE_TABLE} ("
                "key VARCHAR(64) PRIMARY KEY, signature VARCHAR(64) NOT NULL)"
            ))
            conn.commit()
            row = conn.execute(
                text(f"SELECT signature FROM {_MIGRATION_STATE_TABLE} WHERE key = :k"),
                {"k": key},
            ).first()
        return (row is not None and row[0] == sig), sig
    except Exception:
        # Can't read the marker — fall back to just running the statements.
        return False, sig


def _record_migration_signature(key, sig):
    try:
        with engine.connect() as conn:
            updated = conn.execute(
                text(f"UPDATE {_MIGRATION_STATE_TABLE} SET signature = :s WHERE key = :k"),
                {"s": sig, "k": key},
            ).rowcount
            if not updated:
                conn.execute(
                    text(f"INSERT INTO {_MIGRATION_STATE_TABLE} (key, signature) "
                         "VALUES (:k, :s)"),
                    {"k": key, "s": sig},
                )
            conn.commit()
    except Exception:
        # Worst case the batch simply re-runs next boot. Never fail the start.
        pass


def _run_isolated(statements):
    """Run each statement on ONE connection, each inside its own SAVEPOINT.

    Same failure isolation as a transaction-per-statement (a failing statement
    rolls back only to its savepoint, so later ones still run), without paying
    a new connection + round trip per statement. Returns a list of
    (sql, exception) for the failures so callers can decide what to log.
    """
    import logging
    failures = []
    try:
        with engine.connect() as conn:
            for sql in statements:
                try:
                    with conn.begin_nested():
                        conn.execute(text(sql))
                except Exception as e:  # noqa: PERF203 — isolation is the point
                    failures.append((sql, e))
            conn.commit()
    except Exception:
        # Connection-level failure (dropped socket, etc). These statements are
        # all idempotent and re-run on the next boot, so never fail the start.
        logging.getLogger(__name__).warning(
            "Batched migration statements aborted", exc_info=True)
    return failures


def _migrate_add_columns():
    """Add any missing columns in-place. Safe to run every start.

    Columns that already exist are skipped without any SQL (see
    ``_load_existing_columns``), so a warm start costs one schema read rather
    than ~110 network round trips.

    IMPORTANT: each ALTER that *does* run gets its own transaction so a failure
    on one column does NOT abort the whole migration. Without this, Postgres
    rolls back the whole transaction on first error and subsequent columns
    silently never get added.
    """
    existing = _load_existing_columns()
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
        "last_miniapp_chat_id": "BIGINT",
        "notifications_enabled": "BOOLEAN DEFAULT TRUE",
        "subscription_tier": "VARCHAR(20) DEFAULT 'none'",
        "subscription_expires_at": "TIMESTAMP",
        "subscription_activated_at": "TIMESTAMP",
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
        # Already there? Nothing to do, and — crucially — no round trip.
        if existing is not None and col in existing.get(table, ()):
            return
        # Each attempt is independent — failure on this column doesn't poison others
        for sql in (
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}",
            f"ALTER TABLE {table} ADD COLUMN {col} {coltype}",
        ):
            try:
                with engine.begin() as conn:
                    conn.execute(text(sql))
                if existing is not None:
                    existing.setdefault(table, set()).add(col)
                return  # success
            except Exception:
                continue  # try next form, or just give up

    for col, coltype in new_user_cols.items():
        _try_add("users", col, coltype)
    for col, coltype in new_match_cols.items():
        _try_add("matches", col, coltype)

    # Subscription recurring-drop cooldown timestamps on user_stats.
    _try_add("user_stats", "last_mysterybox", "TIMESTAMP")
    _try_add("user_stats", "last_weekly", "TIMESTAMP")
    _try_add("user_stats", "last_coinchest", "TIMESTAMP")
    _try_add("user_stats", "coinchest_used", "INTEGER DEFAULT 0")

    # Challenge League Tournaments: per-league tournament command + match tagging.
    # (The tournament_* tables themselves are created by create_all above.)
    _try_add("challenge_leagues", "tournament_command", "VARCHAR(60)")
    _try_add("matches", "tournament_id", "INTEGER")

    # League Tournament Structure: league formats, groups, schedule + knockouts.
    # New table ``tournament_groups`` is created by create_all above; these add the
    # new columns on the existing tournament_* tables.
    _try_add("tournaments", "league_format", "VARCHAR(20) DEFAULT 'single_rr'")
    _try_add("tournaments", "group_points_mode", "VARCHAR(20) DEFAULT 'separate'")
    _try_add("tournaments", "schedule_generated", "BOOLEAN DEFAULT FALSE")
    _try_add("tournaments", "knockout_type", "VARCHAR(30)")
    _try_add("tournaments", "knockout_config_json", "TEXT")
    _try_add("tournaments", "knockout_generated", "BOOLEAN DEFAULT FALSE")
    _try_add("tournament_teams", "group_id", "INTEGER")
    _try_add("tournament_matches", "status", "VARCHAR(20) DEFAULT 'completed'")
    _try_add("tournament_matches", "group_id", "INTEGER")
    _try_add("tournament_matches", "round_no", "INTEGER DEFAULT 0")
    _try_add("tournament_matches", "match_no", "INTEGER DEFAULT 0")
    _try_add("tournament_matches", "feeds_winner_to_id", "INTEGER")
    _try_add("tournament_matches", "feeds_loser_to_id", "INTEGER")
    _try_add("tournament_matches", "slot1_label", "VARCHAR(60)")
    _try_add("tournament_matches", "slot2_label", "VARCHAR(60)")

    # Overseas-player rules: league home country + min/max overseas in the XI,
    # and the per-challenge-player overseas flag.
    _try_add("challenge_leagues", "home_country", "VARCHAR(60)")
    _try_add("challenge_leagues", "min_overseas", "INTEGER DEFAULT 0")
    _try_add("challenge_leagues", "max_overseas", "INTEGER DEFAULT 11")
    # Per-league match format: "T20" (20-over) or "The100" (The Hundred, 100 balls).
    _try_add("challenge_leagues", "match_format", "VARCHAR(20) DEFAULT 'T20'")
    _try_add("challenge_players", "is_overseas", "BOOLEAN DEFAULT FALSE")

    # Fantasy league configuration fields added after the original fantasy
    # launch. New eligibility/rule tables are handled by create_all above.
    _try_add("fantasy_leagues", "description", "TEXT")
    _try_add("fantasy_leagues", "broadcast_message", "TEXT")

    # EventMedia: MiniApp display timing and natural-size configuration.
    _try_add("event_media", "duration_ms", "INTEGER DEFAULT 3000")
    _try_add("event_media", "size_mode", "VARCHAR(20) DEFAULT 'original'")
    _try_add("event_media", "original_width", "INTEGER")
    _try_add("event_media", "original_height", "INTEGER")
    _try_add("event_media", "max_mobile_width", "INTEGER DEFAULT 440")
    _try_add("event_media", "media_type", "VARCHAR(10) DEFAULT 'image'")

    # GameConfig: new simulation-tuning columns
    new_gameconfig_cols = {
        "sim_dot_adjust": "FLOAT DEFAULT -10.0",
        "sim_one_adjust": "FLOAT DEFAULT 6.0",
        "sim_two_adjust": "FLOAT DEFAULT 3.0",
        "sim_four_adjust": "FLOAT DEFAULT 0.5",
        "sim_six_adjust": "FLOAT DEFAULT 0.3",
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
        "scorecard_text_settings": "TEXT",
        # Which cards /wpm and /cm post to the lobby chat on completion.
        "wpm_result_cards": "VARCHAR(120) DEFAULT 'summary'",
        # Maintenance mode
        "is_maintenance": "BOOLEAN DEFAULT FALSE",
        "maintenance_message": "TEXT",
        "maintenance_until": "TIMESTAMP",
        "maintenance_started_at": "TIMESTAMP",
        "maintenance_bypass_ids": "VARCHAR(500)",
        # Telegram IDs allowed to use the Challenge League Tournament command.
        "tournament_allowed_ids": "VARCHAR(500)",
    }
    for col, coltype in new_gameconfig_cols.items():
        _try_add("game_config", col, coltype)

    # Player versions support
    _try_add("players", "parent_player_id", "INTEGER")
    # Persistent generated-card file_id cache (survives restarts; skips re-render)
    _try_add("players", "gen_card_file_id", "VARCHAR(200)")
    # Per-player block on /buypl direct purchase
    _try_add("players", "restricted_from_buypl", "BOOLEAN DEFAULT FALSE")
    # Spin/daily quota system (1 free + N ad uses per 24h cycle)
    _try_add("user_stats", "spin_cycle_started_at", "TIMESTAMP")
    _try_add("user_stats", "spin_free_used", "BOOLEAN DEFAULT FALSE")
    _try_add("user_stats", "spin_ad_count", "INTEGER DEFAULT 0")
    _try_add("user_stats", "daily_cycle_started_at", "TIMESTAMP")
    _try_add("user_stats", "daily_free_used", "BOOLEAN DEFAULT FALSE")
    _try_add("user_stats", "daily_ad_count", "INTEGER DEFAULT 0")
    # Admin-tunable Mini App quota sizes
    _try_add("game_config", "spin_ad_quota", "INTEGER DEFAULT 5")
    _try_add("game_config", "daily_ad_quota", "INTEGER DEFAULT 5")
    # Global gameplay style: preserve the original in-chat bot flow by default.
    _try_add("game_config", "match_style", "VARCHAR(20) DEFAULT 'telegram' NOT NULL")
    _try_add("game_config", "challenge_max_overs", "INTEGER DEFAULT 2 NOT NULL")
    _try_add("game_config", "allow_same_team_challenge", "BOOLEAN DEFAULT FALSE NOT NULL")
    # Player card rendering — alternate admin-uploaded template card style
    _try_add("game_config", "card_style", "VARCHAR(20) DEFAULT 'tier' NOT NULL")
    _try_add("game_config", "card_template_image_path", "VARCHAR(300)")
    _try_add("game_config", "card_template_area_code", "TEXT")
    _try_add("game_config", "card_template_show_portrait", "BOOLEAN DEFAULT TRUE")
    _try_add("game_config", "card_template_font_path", "VARCHAR(300)")
    _try_add("game_config", "card_template_settings", "TEXT")
    # Shared caption for the /CMUshop image gallery
    _try_add("game_config", "cmushop_caption", "TEXT")
    # Telegram channel storage — cache file_ids for player images
    _try_add("player_images", "tg_file_id", "VARCHAR(200)")
    # Referral code + branding (per the invite + branding feature)
    _try_add("users", "referral_code", "VARCHAR(12)")
    _try_add("game_config", "branding_channel_username", "VARCHAR(64)")
    _try_add("game_config", "branding_channel_label", "VARCHAR(80)")
    _try_add("game_config", "branding_group_username", "VARCHAR(64)")
    _try_add("game_config", "branding_group_label", "VARCHAR(80)")
    _try_add("game_config", "branding_tagline", "VARCHAR(200)")
    # Quick Match — separate stat tracking + daily limit (per the
    # "stats not counted, daily limit 5" feature)
    _try_add("users", "quick_matches_played", "INTEGER DEFAULT 0")
    _try_add("users", "quick_matches_won", "INTEGER DEFAULT 0")
    _try_add("users", "quick_matches_lost", "INTEGER DEFAULT 0")
    _try_add("users", "quick_matches_today", "INTEGER DEFAULT 0")
    _try_add("users", "quick_matches_today_date", "VARCHAR(10)")
    _try_add("game_config", "daily_quick_match_limit", "INTEGER DEFAULT 5")
    # /ximage render cooldown (1 hour)
    _try_add("user_stats", "last_ximage", "TIMESTAMP")
    # Free Pack + cooldown-ready notifications
    _try_add("user_stats", "last_free_pack", "TIMESTAMP")
    _try_add("user_stats", "notified_daily_ready", "BOOLEAN DEFAULT FALSE")
    _try_add("user_stats", "notified_gspin_ready", "BOOLEAN DEFAULT FALSE")
    _try_add("user_stats", "notified_claim_ready", "BOOLEAN DEFAULT FALSE")
    _try_add("user_stats", "notified_free_pack_ready", "BOOLEAN DEFAULT FALSE")
    _try_add("game_config", "free_pack_cooldown_minutes", "INTEGER DEFAULT 60")
    _try_add("game_config", "free_pack_bands_json", "TEXT")
    # Pinned quests (always assigned to every user, e.g. watch N ads daily)
    _try_add("quests", "always_assign", "BOOLEAN DEFAULT FALSE")
    # Login streak ladder
    _try_add("user_stats", "login_streak", "INTEGER DEFAULT 0")
    _try_add("user_stats", "login_best_streak", "INTEGER DEFAULT 0")
    _try_add("user_stats", "last_login_date", "VARCHAR(10)")
    _try_add("user_stats", "login_reward_claimed_date", "VARCHAR(10)")
    # Monthly season
    _try_add("users", "season_points", "INTEGER DEFAULT 0")
    _try_add("users", "season_key", "VARCHAR(7)")
    _try_add("users", "season_wins", "INTEGER DEFAULT 0")
    # Clubs
    _try_add("users", "club_id", "INTEGER")
    _try_add("users", "club_joined_at", "TIMESTAMP")
    _try_add("users", "last_club_leave", "TIMESTAMP")
    # Welcome message toggle per group
    _try_add("bot_chats", "welcome_enabled", "BOOLEAN DEFAULT TRUE")
    # Optional image/document metadata for admin broadcasts
    _try_add("broadcasts", "attachment_type", "VARCHAR(20)")
    _try_add("broadcasts", "attachment_name", "VARCHAR(255)")
    # Official group restriction for buying
    _try_add("game_config", "official_group_id", "BIGINT")
    _try_add("game_config", "official_group_link", "VARCHAR(200)")
    _try_add("game_config", "welcome_message", "TEXT")

    # Pack table additions (versions filtering)
    _try_add("packs", "main_filter_mode", "VARCHAR(10) DEFAULT 'rating'")
    _try_add("packs", "main_versions_json", "VARCHAR(500)")
    _try_add("user_quest_progress", "assigned", "BOOLEAN DEFAULT TRUE")
    _try_add("users", "pack_pity_counter", "INTEGER DEFAULT 0")

    # Fantasy league auto-lock time (stored UTC; admin enters IST)
    _try_add("fantasy_leagues", "lock_at", "TIMESTAMP")
    # Generated card Telegram file_id cache — avoids re-uploading unchanged cards
    _try_add("players", "card_file_id", "VARCHAR(200)")

    # Challenge Mode hierarchy/admin UI additions.
    _try_add("challenge_leagues", "same_team_allowed", "BOOLEAN DEFAULT TRUE")
    _try_add("challenge_teams", "logo_url", "VARCHAR(500)")
    _try_add("challenge_teams", "primary_color", "VARCHAR(9)")
    _try_add("challenge_teams", "secondary_color", "VARCHAR(9)")
    _try_add("challenge_teams", "is_active", "BOOLEAN DEFAULT TRUE")
    _try_add("challenge_players", "source_player_id", "INTEGER")

    # Backfill/normalize for Postgres + SQLite: ensure non-null and true by
    # default. All of these share one connection (savepoint per statement) so
    # they stay independently fault-tolerant without a round trip each.
    backfill_sql = [
        # Normalize the legacy milestone key so it remains editable in the dashboard.
        "UPDATE event_media SET event_key = 'century' WHERE event_key = 'hundred'",
        "UPDATE user_quest_progress SET assigned = TRUE WHERE assigned IS NULL",
        "ALTER TABLE user_quest_progress ALTER COLUMN assigned SET DEFAULT TRUE",
        "ALTER TABLE user_quest_progress ALTER COLUMN assigned SET NOT NULL",
        # match_format is non-nullable in the model; backfill legacy NULLs and
        # enforce the NOT NULL invariant on already-migrated databases too.
        "UPDATE challenge_leagues SET match_format = 'T20' WHERE match_format IS NULL",
        "ALTER TABLE challenge_leagues ALTER COLUMN match_format SET DEFAULT 'T20'",
        "ALTER TABLE challenge_leagues ALTER COLUMN match_format SET NOT NULL",
        # Telegram ids outgrew INTEGER — widen the legacy column so adsgram
        # postbacks stop failing with "integer out of range".
        "ALTER TABLE adsgram_rewards ALTER COLUMN telegram_id TYPE BIGINT",
    ]
    done, sig = _migration_signature_matches("backfill", backfill_sql)
    if not done:
        # SQLite and older schemas may not support ALTER COLUMN forms; safe to
        # ignore those failures (and to keep retrying them on later boots).
        if not _run_isolated(backfill_sql):
            _record_migration_signature("backfill", sig)

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
        # Unique referral code per user (added via migration so we need the
        # index here; existing rows with NULL are OK — NULLs don't conflict)
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_referral_code "
        "ON users (referral_code)",
    ]
    done, sig = _migration_signature_matches("player_name_indexes", migration_sql)
    if not done:
        failures = _run_isolated(migration_sql)
        if not failures:
            _record_migration_signature("player_name_indexes", sig)
        for sql, e in failures:
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

    # Seed the pinned "Watch ads" daily quest (idempotent by event_key)
    try:
        from models import Quest
        sess = SessionLocal()
        try:
            existing = (sess.query(Quest)
                        .filter(Quest.event_key == "ad_watched",
                                Quest.quest_type == "daily").first())
            if not existing:
                sess.add(Quest(
                    name="Ad Enthusiast",
                    description="Watch 5 ads today",
                    quest_type="daily",
                    event_key="ad_watched",
                    target_count=5,
                    reward_points=10,
                    reward_coins=1000,
                    reward_gems=0,
                    is_active=True,
                    emoji="📺",
                    sort_order=1,
                    always_assign=True,  # given to every user daily
                ))
                sess.commit()
                import logging
                logging.getLogger(__name__).info("Seeded pinned 'Watch ads' daily quest")
        except Exception:
            sess.rollback()
        finally:
            sess.close()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Ad quest seed skipped (non-fatal)")

    # (The fantasy_* tables are registered on Base.metadata via the model
    # imports in init_db, so the create_all up there already builds them. The
    # separate create_all that used to live here was a no-op that still cost a
    # schema reflection round trip on every boot.)

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
                         amount_min=1500, amount_max=3000),
                    dict(label="Common Pull", emoji="🟨", color="FFD93D", weight=240,
                         sort_order=20, reward_type="player",
                         player_rating_min=65, player_rating_max=78),
                    dict(label="Gem Drop", emoji="🟦", color="2196F3", weight=130,
                         sort_order=30, reward_type="gems",
                         amount_min=3, amount_max=150),
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
                 dict(coin_amount=1500, gem_amount=30, player_count=1,
                      player_rating_min=80, player_rating_max=89)),
                ("claim", "/claim", "c",
                 "Pull a free random player. 1-hour cooldown.",
                 "economy", 3600, 20,
                 dict(coin_amount=0, player_count=1,
                      player_rating_min=50, player_rating_max=100)),
                ("daily", "/daily", "dl",
                 "Daily login bonus. Coins + 2 players + streak rewards.",
                 "economy", 86400, 30,
                 dict(coin_amount=1500, player_count=2,
                      player_rating_min=50, player_rating_max=100,
                      milestone_bonus_coins=3000, milestone_bonus_gems=6,
                      milestone_bonus_player_min=85, milestone_bonus_player_max=100,
                      milestone_every_n=14)),
                ("gspin", "/gspin", "gs",
                 "Spin the wheel. Rewards configured in /gspin-rewards.",
                 "economy", 28800, 40, dict()),
                ("ximage", "/ximage", "xiimg xipic",
                 "Render your Playing XI as an image. 1-hour cooldown.",
                 "utility", 3600, 45, dict()),
                ("testwpm", "/testwpm", "",
                 "Diagnostic: test /wpm and /cm completion summary delivery.",
                 "matches", 0, 90, dict()),
                ("sim", "/sim", "simmatch",
                 "Instant auto-simulated match vs a Sim XI — scorecard, result, "
                 "and ball-by-ball commentary JSON.",
                 "matches", 0, 95, dict()),
            ]
            # Two queries for the whole catalog rather than two per command —
            # every round trip here is downtime on a cold start.
            keys = [d[0] for d in defaults]
            have_cmd = {k for (k,) in sess.query(BotCommand.command_key)
                        .filter(BotCommand.command_key.in_(keys))}
            have_rwd = {k for (k,) in sess.query(CommandReward.command_key)
                        .filter(CommandReward.command_key.in_(keys))}
            n_cmd = 0; n_rwd = 0
            for key, name, aliases, desc, cat, cd, srt, rwd in defaults:
                if key not in have_cmd:
                    sess.add(BotCommand(
                        command_key=key, display_name=name, aliases=aliases,
                        description=desc, category=cat, cooldown_seconds=cd,
                        sort_order=srt, enabled=True))
                    n_cmd += 1
                if key not in have_rwd:
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
            # One query for all shot names instead of one per shot.
            have = {s for (s,) in sess.query(ShotProbability.shot_name)
                    .filter(ShotProbability.shot_name.in_(list(_SM)))}
            n = 0
            for shot, mods in _SM.items():
                if shot in have:
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
