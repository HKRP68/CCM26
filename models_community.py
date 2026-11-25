"""CMU Community Arena — ORM models.

Self-contained tables for the Community Arena / Engagement Engine. This module
is imported at the end of ``models.py`` so ``import models`` registers these
classes on ``database.Base.metadata``; ``database.init_db`` then creates them
exactly like every other table (new tables are auto-created on boot).

Design notes
------------
* Nothing here alters an existing table. Community Arena is additive: a failure
  in any of this must never affect core gameplay, economy or ratings.
* Community XP (CXP) is a wholly separate currency from coins, gems, quest
  points, ranked rating and season points. It lives only in these tables.
* Idempotency is enforced by a globally unique ``event_key`` on the ledger,
  mirroring ``SeasonActivityAward`` — the same replay guard the rest of the bot
  already trusts.
* All daily/weekly/monthly boundaries are computed in Asia/Kolkata (IST); the
  stored timestamps themselves stay UTC, matching the rest of the codebase.
"""

from datetime import datetime

from sqlalchemy import (
    Column, Integer, BigInteger, String, Boolean, DateTime, ForeignKey, Index,
    Text, UniqueConstraint,
)

from database import Base


# ════════════════════════════════════════════════════════════════════
# Per-manager aggregate — one row per registered user
# ════════════════════════════════════════════════════════════════════

class CommunityProfile(Base):
    """A manager's lifetime Community Arena standing.

    Lifetime XP never resets automatically. Period XP (today/week/month) is
    derived from :class:`CommunityXPLedger` against IST boundaries rather than
    stored here, so there is no reset job to get wrong and history is retained.
    The denormalized activity counters below are convenience mirrors for the
    profile screen; the ledger remains the source of truth for XP.
    """

    __tablename__ = "community_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True, index=True)

    lifetime_xp = Column(Integer, default=0, nullable=False)
    level = Column(Integer, default=1, nullable=False)

    # Daily group check-in
    current_streak = Column(Integer, default=0, nullable=False)
    longest_streak = Column(Integer, default=0, nullable=False)
    total_checkins = Column(Integer, default=0, nullable=False)
    last_checkin_date = Column(String(10), nullable=True)  # IST 'YYYY-MM-DD'

    # Admin reward-eligibility suspension (anti-abuse). When true the user earns
    # no further CXP until re-enabled.
    rewards_suspended = Column(Boolean, default=False, nullable=False)
    suspended_reason = Column(String(300), nullable=True)

    # Denormalized lifetime activity counters (profile display / achievements)
    chat_messages = Column(Integer, default=0, nullable=False)
    pvp_matches = Column(Integer, default=0, nullable=False)
    ai_matches = Column(Integer, default=0, nullable=False)
    claims = Column(Integer, default=0, nullable=False)
    days_active = Column(Integer, default=0, nullable=False)
    last_active_date = Column(String(10), nullable=True)  # IST

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow, nullable=False)


# ════════════════════════════════════════════════════════════════════
# XP ledger + daily counters
# ════════════════════════════════════════════════════════════════════

class CommunityXPLedger(Base):
    """Append-only record of every CXP credit/debit.

    The unique ``event_key`` is the duplicate-reward guard: each awardable
    event (a chat message, a completed match, a claim, a check-in, a game win,
    an admin adjustment) computes a stable key and the insert is skipped if it
    already exists. ``delta`` is the XP *actually* credited after caps, so the
    ledger always reconciles with :attr:`CommunityProfile.lifetime_xp`.
    """

    __tablename__ = "community_xp_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    activity = Column(String(40), nullable=False, index=True)
    delta = Column(Integer, nullable=False)
    event_key = Column(String(160), nullable=False)
    chat_id = Column(BigInteger, nullable=True)
    detail = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("event_key", name="uq_community_xp_event"),
        Index("ix_community_xp_user_created", "user_id", "created_at"),
        Index("ix_community_xp_activity_created", "activity", "created_at"),
    )


class CommunityDailyCounter(Base):
    """Per-user, per-IST-day, per-activity reward counters.

    Authoritative store for per-activity daily limits and the overall daily CXP
    cap. Chat XP also keeps a hot in-memory mirror for throttling, but this
    table is what survives a restart and what the limits are ultimately checked
    against inside the awarding transaction.
    """

    __tablename__ = "community_daily_counters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    day = Column(String(10), nullable=False)       # IST 'YYYY-MM-DD'
    activity = Column(String(40), nullable=False)
    count = Column(Integer, default=0, nullable=False)
    xp = Column(Integer, default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "day", "activity", name="uq_community_daily"),
        Index("ix_community_daily_user_day", "user_id", "day"),
    )


# ════════════════════════════════════════════════════════════════════
# Games
# ════════════════════════════════════════════════════════════════════

class CommunityGame(Base):
    """One launched community mini-game (restart-safe).

    All live state is persisted so the scheduler can recover active, unexpired
    games after a redeploy and expire overdue ones rather than restart them.
    ``launch_key`` is unique to collapse duplicate launches across concurrent
    workers; only one active game per chat is allowed at a time.
    """

    __tablename__ = "community_games"

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_type = Column(String(30), nullable=False, index=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    status = Column(String(16), default="active", nullable=False, index=True)
    # active | closed (won) | expired (timed out) | cancelled (admin)

    question_json = Column(Text, nullable=True)   # display payload
    answer_json = Column(Text, nullable=True)     # canonical answer + aliases + metadata

    reward_xp = Column(Integer, default=0, nullable=False)
    duration_seconds = Column(Integer, default=60, nullable=False)
    max_attempts = Column(Integer, nullable=True)
    difficulty = Column(String(12), nullable=True)

    source = Column(String(16), default="auto", nullable=False)  # auto | manual
    launched_by = Column(BigInteger, nullable=True)
    message_id = Column(BigInteger, nullable=True)

    player_id = Column(Integer, nullable=True, index=True)
    player_version = Column(String(50), nullable=True)

    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    deadline = Column(DateTime, nullable=False, index=True)
    revealed = Column(Boolean, default=False, nullable=False)
    closed_at = Column(DateTime, nullable=True)

    winner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                            nullable=True)
    winner_telegram_id = Column(BigInteger, nullable=True)
    winner_response_ms = Column(Integer, nullable=True)
    participant_count = Column(Integer, default=0, nullable=False)

    launch_key = Column(String(80), nullable=True, unique=True)

    __table_args__ = (
        Index("ix_community_game_chat_status", "chat_id", "status"),
    )


class CommunityGameParticipant(Base):
    """One manager's participation in a single game.

    ``data_json`` holds per-game, per-user state that does not deserve its own
    table — notably each player's independent Cricket Wordle guess history.
    """

    __tablename__ = "community_game_participants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    game_id = Column(Integer, ForeignKey("community_games.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    first_attempt_at = Column(DateTime, nullable=True)
    is_winner = Column(Boolean, default=False, nullable=False)
    response_ms = Column(Integer, nullable=True)
    data_json = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("game_id", "user_id", name="uq_community_game_participant"),
    )


class CommunityGameStat(Base):
    """Per-user, per-game-type aggregate for leaderboards and profiles."""

    __tablename__ = "community_game_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    game_type = Column(String(30), nullable=False)
    plays = Column(Integer, default=0, nullable=False)
    wins = Column(Integer, default=0, nullable=False)
    fastest_ms = Column(Integer, nullable=True)
    total_win_ms = Column(BigInteger, default=0, nullable=False)
    last_win_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "game_type", name="uq_community_game_stat"),
    )


# ════════════════════════════════════════════════════════════════════
# Levels + milestone rewards
# ════════════════════════════════════════════════════════════════════

class CommunityLevel(Base):
    """Configurable lifetime-XP level ladder. Seeded on first boot."""

    __tablename__ = "community_levels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(Integer, nullable=False, unique=True)
    title = Column(String(80), nullable=False)
    required_xp = Column(Integer, nullable=False)


class CommunityMilestoneReward(Base):
    """Optional reward granted once when a manager first reaches a level.

    Monetary rewards (coins/gems/packs) are ``enabled`` False by default until
    an administrator turns them on, per spec. Badges/titles are cosmetic and
    always granted. Each level's reward is claimable exactly once per user
    (enforced by :class:`CommunityMilestoneClaim`).
    """

    __tablename__ = "community_milestone_rewards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(Integer, nullable=False, unique=True)
    badge = Column(String(80), nullable=True)
    title = Column(String(80), nullable=True)
    coins = Column(Integer, default=0, nullable=False)
    gems = Column(Integer, default=0, nullable=False)
    pack_json = Column(Text, nullable=True)
    cosmetic = Column(String(120), nullable=True)
    enabled = Column(Boolean, default=False, nullable=False)


class CommunityMilestoneClaim(Base):
    """One manager's one-time claim of a level milestone reward."""

    __tablename__ = "community_milestone_claims"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    level = Column(Integer, nullable=False)
    claimed_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    detail = Column(String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "level", name="uq_community_milestone_claim"),
    )


# ════════════════════════════════════════════════════════════════════
# Achievements (community eligibility kept separate from the core framework)
# ════════════════════════════════════════════════════════════════════

class CommunityAchievement(Base):
    """A Community Arena achievement a manager has unlocked."""

    __tablename__ = "community_achievements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    code = Column(String(40), nullable=False)
    unlocked_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_community_achievement"),
    )


# ════════════════════════════════════════════════════════════════════
# Config + scheduler state + admin audit
# ════════════════════════════════════════════════════════════════════

class CommunityConfig(Base):
    """Single-row Community Arena configuration.

    ``settings_json`` holds the full admin-editable config (XP values, limits,
    cooldowns, per-game settings, scheduler weights, active hours …) merged over
    the code defaults in ``services/community_config_service.py``. The two
    master switches are promoted to real columns so they can be toggled and
    queried cheaply.
    """

    __tablename__ = "community_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    enabled = Column(Boolean, default=False, nullable=False)
    auto_enabled = Column(Boolean, default=False, nullable=False)
    settings_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow, nullable=True)
    updated_by = Column(BigInteger, nullable=True)


class CommunitySchedulerState(Base):
    """Restart-safe scheduler bookkeeping, one row per chat (the Official GC).

    Tracks genuine-human inactivity, per-IST-day launch counts, and the recent
    game-type / player history used to avoid consecutive repeats.
    """

    __tablename__ = "community_scheduler_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, nullable=False, unique=True, index=True)
    last_human_at = Column(DateTime, nullable=True)
    last_game_at = Column(DateTime, nullable=True)
    last_game_type = Column(String(30), nullable=True)
    games_today = Column(Integer, default=0, nullable=False)
    games_today_date = Column(String(10), nullable=True)  # IST
    recent_player_ids = Column(Text, nullable=True)       # JSON list
    recent_game_types = Column(Text, nullable=True)       # JSON list
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow, nullable=True)


class CommunityAuditLog(Base):
    """Audit record for every administrator action on Community Arena data."""

    __tablename__ = "community_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    admin_id = Column(BigInteger, nullable=True)
    user_id = Column(Integer, nullable=True, index=True)
    action = Column(String(40), nullable=False)
    reason = Column(String(400), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
