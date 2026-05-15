"""SQLAlchemy ORM models."""

from datetime import datetime, timedelta
from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Boolean, DateTime, ForeignKey, Index, Text
)
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(100), nullable=True)
    first_name = Column(String(100), nullable=True)
    team_name = Column(String(50), nullable=True)
    total_coins = Column(Integer, default=0)
    total_gems = Column(Integer, default=0)
    roster_count = Column(Integer, default=0)
    captain_roster_id = Column(Integer, nullable=True)
    matches_played = Column(Integer, default=0)
    matches_won = Column(Integer, default=0)
    matches_lost = Column(Integer, default=0)
    win_streak = Column(Integer, default=0)
    best_streak = Column(Integer, default=0)
    active_days = Column(Integer, default=0)  # days with at least 1 match
    last_match_date = Column(DateTime, nullable=True)
    quest_points = Column(Integer, default=0)  # earned from completing quests
    # Pack pity timer — increments on low rolls, resets on a max-rating roll.
    # When ≥ PITY_THRESHOLD, the next pack guarantees max-rating from the band.
    pack_pity_counter = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    roster = relationship("UserRoster", back_populates="user", cascade="all, delete-orphan")
    stats = relationship("UserStats", back_populates="user", uselist=False, cascade="all, delete-orphan")


class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # name no longer unique — multiple versions can share a name
    name = Column(String(150), nullable=False, index=True)
    version = Column(String(50), default="Base")  # 'Base', 'Gold', 'World Cup 2023', etc.
    # NULL for base cards. Set to base player's id for variant versions.
    parent_player_id = Column(Integer, ForeignKey("players.id"), nullable=True, index=True)
    rating = Column(Integer, nullable=False)
    category = Column(String(30), nullable=False)
    country = Column(String(60), nullable=False)
    bat_hand = Column(String(10), nullable=False)
    bowl_hand = Column(String(10), nullable=False)
    bowl_style = Column(String(30), nullable=False)
    bat_rating = Column(Integer, default=0)
    bowl_rating = Column(Integer, default=0)
    # Career stats kept in schema but seeded to 0 — real stats are in PlayerGameStats
    bat_avg = Column(Float, default=0.0)
    strike_rate = Column(Float, default=0.0)
    runs = Column(Integer, default=0)
    centuries = Column(Integer, default=0)
    bowl_avg = Column(Float, default=0.0)
    economy = Column(Float, default=0.0)
    wickets = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    image_url = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_players_rating", "rating"),
        Index("ix_players_parent", "parent_player_id"),
    )


class UserRoster(Base):
    __tablename__ = "user_roster"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    order_position = Column(Integer, default=99)
    acquired_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="roster")
    player = relationship("Player")

    __table_args__ = (Index("ix_user_roster_user", "user_id"),)


class UserStats(Base):
    __tablename__ = "user_stats"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_claim = Column(DateTime, nullable=True)
    last_daily = Column(DateTime, nullable=True)
    last_gspin = Column(DateTime, nullable=True)
    streak_count = Column(Integer, default=0)
    total_streaks_completed = Column(Integer, default=0)
    last_streak_reset = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="stats")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    initiator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    receiver_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    initiator_player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    receiver_player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    initiator_roster_id = Column(Integer, ForeignKey("user_roster.id"), nullable=True)
    receiver_roster_id = Column(Integer, ForeignKey("user_roster.id"), nullable=True)
    status = Column(String(20), default="pending", nullable=False)
    trade_fee = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    initiator = relationship("User", foreign_keys=[initiator_id])
    receiver = relationship("User", foreign_keys=[receiver_id])
    initiator_player = relationship("Player", foreign_keys=[initiator_player_id])
    receiver_player = relationship("Player", foreign_keys=[receiver_player_id])

    __table_args__ = (
        Index("ix_trades_status", "status"),
        Index("ix_trades_initiator", "initiator_id"),
        Index("ix_trades_receiver", "receiver_id"),
    )


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    action = Column(String(50), nullable=False)
    detail = Column(String(500), nullable=True)
    coins_change = Column(Integer, default=0)
    gems_change = Column(Integer, default=0)
    player_name = Column(String(150), nullable=True)
    player_rating = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")

    __table_args__ = (
        Index("ix_activity_user", "user_id"),
        Index("ix_activity_action", "action"),
        Index("ix_activity_time", "created_at"),
    )


class PlayerGameStats(Base):
    """Per-player-per-owner game stats. Created when a player plays for a team."""
    __tablename__ = "player_game_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)

    # Awards
    potm = Column(Integer, default=0)  # Player of the Match count

    # Batting
    bat_inns = Column(Integer, default=0)
    runs = Column(Integer, default=0)
    fifties = Column(Integer, default=0)
    hundreds = Column(Integer, default=0)
    fours = Column(Integer, default=0)
    sixes = Column(Integer, default=0)
    balls_faced = Column(Integer, default=0)
    times_out = Column(Integer, default=0)
    ducks = Column(Integer, default=0)
    highest_score = Column(Integer, default=0)
    highest_score_not_out = Column(Boolean, default=False)

    # Bowling
    bowl_inns = Column(Integer, default=0)
    wickets_taken = Column(Integer, default=0)
    runs_conceded = Column(Integer, default=0)
    overs_bowled = Column(Float, default=0.0)
    balls_bowled = Column(Integer, default=0)
    three_fers = Column(Integer, default=0)
    five_fers = Column(Integer, default=0)
    hattricks = Column(Integer, default=0)
    best_bowl_wickets = Column(Integer, default=0)
    best_bowl_runs = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    player = relationship("Player")

    __table_args__ = (
        Index("ix_pgs_user_player", "user_id", "player_id", unique=True),
    )

    @property
    def bat_avg(self):
        return round(self.runs / self.times_out, 2) if self.times_out else 0.0

    @property
    def bat_sr(self):
        return round((self.runs / self.balls_faced) * 100, 2) if self.balls_faced else 0.0

    @property
    def bowl_avg(self):
        return round(self.runs_conceded / self.wickets_taken, 2) if self.wickets_taken else 0.0

    @property
    def bowl_economy(self):
        return round(self.runs_conceded / (self.overs_bowled or 1), 2) if self.overs_bowled else 0.0

    @property
    def bowl_sr(self):
        return round(self.balls_bowled / self.wickets_taken, 2) if self.wickets_taken else 0.0

    @property
    def hs_str(self):
        if self.highest_score == 0 and self.bat_inns == 0:
            return "-"
        no = "*" if self.highest_score_not_out else ""
        return f"{self.highest_score}{no}"

    @property
    def bbf_str(self):
        if self.best_bowl_wickets == 0 and self.bowl_inns == 0:
            return "-"
        return f"{self.best_bowl_wickets}/{self.best_bowl_runs}"

class Match(Base):
    """Tracks a match between two users."""
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user1_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user2_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(30), default="pending")
    overs = Column(Integer, default=20)
    toss_winner_id = Column(Integer, nullable=True)
    toss_decision = Column(String(10), nullable=True)
    batting_first_id = Column(Integer, nullable=True)
    bowling_first_id = Column(Integer, nullable=True)
    stadium = Column(String(100), nullable=True)
    pitch_type = Column(String(30), nullable=True)
    weather = Column(String(30), nullable=True)
    temperature = Column(Integer, nullable=True)
    umpire1 = Column(String(60), nullable=True)
    umpire2 = Column(String(60), nullable=True)
    chat_id = Column(BigInteger, nullable=True)
    # Result fields
    winner_id = Column(Integer, nullable=True)
    loser_id = Column(Integer, nullable=True)
    margin_type = Column(String(20), nullable=True)  # "runs" or "wickets"
    margin_value = Column(Integer, nullable=True)
    result_message_id = Column(BigInteger, nullable=True)  # telegram msg id for /jump
    inn1_runs = Column(Integer, nullable=True)
    inn1_wickets = Column(Integer, nullable=True)
    inn2_runs = Column(Integer, nullable=True)
    inn2_wickets = Column(Integer, nullable=True)
    potm_player_id = Column(Integer, nullable=True)
    potm_impact = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    user1 = relationship("User", foreign_keys=[user1_id])
    user2 = relationship("User", foreign_keys=[user2_id])

    __table_args__ = (
        Index("ix_matches_status", "status"),
        Index("ix_matches_winner", "winner_id"),
    )


class AdminLog(Base):
    """Audit log for admin actions in the web panel."""
    __tablename__ = "admin_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(50), nullable=False)  # player_add, player_edit, player_delete, bulk_upload, etc
    target_type = Column(String(30), nullable=True)  # player, user, roster
    target_id = Column(Integer, nullable=True)
    target_name = Column(String(150), nullable=True)
    detail = Column(String(500), nullable=True)
    ip_address = Column(String(50), nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_admin_logs_timestamp", "timestamp"),
        Index("ix_admin_logs_action", "action"),
    )


# ══════════════════════════════════════════════════════════════════════
# TRAIT SYSTEM
# ══════════════════════════════════════════════════════════════════════

class Trait(Base):
    """Master definition of a trait. 14 rows seeded at startup."""
    __tablename__ = "traits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), unique=True, nullable=False)
    category = Column(String(30), nullable=False)  # Batting / Bowling / Fielding / Mental
    description = Column(String(300), nullable=False)
    emoji = Column(String(10), default="✨")
    effect_key = Column(String(50), nullable=False)  # routed to trait_engine handlers
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PlayerTrait(Base):
    """Trait equipped on a user's roster entry. Max 3 per roster_id."""
    __tablename__ = "player_traits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    roster_id = Column(Integer, ForeignKey("user_roster.id"), nullable=False, index=True)
    trait_id = Column(Integer, ForeignKey("traits.id"), nullable=False)
    level = Column(Integer, default=1)
    acquired_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pt_user_roster", "user_id", "roster_id"),
    )


class TraitInventory(Base):
    """Unequipped traits stockpiled by user."""
    __tablename__ = "trait_inventory"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    trait_id = Column(Integer, ForeignKey("traits.id"), nullable=False)
    level = Column(Integer, default=1)
    acquired_at = Column(DateTime, default=datetime.utcnow)


class TraitMarket(Base):
    """Daily shop snapshot — 5 slots per user, refreshes every 24h."""
    __tablename__ = "trait_market"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    slot_index = Column(Integer, nullable=False)
    trait_id = Column(Integer, ForeignKey("traits.id"), nullable=False)
    base_price = Column(Integer, nullable=False)
    discount_pct = Column(Integer, default=0)
    final_price = Column(Integer, nullable=False)
    purchased = Column(Boolean, default=False)
    refreshed_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_tm_user_slot", "user_id", "slot_index"),
    )


class TraitDaily(Base):
    """Per-user per-day counters (purchases cap)."""
    __tablename__ = "trait_daily"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    day_key = Column(String(10), nullable=False)  # YYYY-MM-DD
    purchases = Column(Integer, default=0)
    rerolls = Column(Integer, default=0)
    last_refresh_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_td_user_day", "user_id", "day_key"),
    )


# ══════════════════════════════════════════════════════════════════════
# PLAYER MARKET — daily 5-slot 87+ shop
# ══════════════════════════════════════════════════════════════════════

class PlayerMarket(Base):
    """Daily player market snapshot. 5 slots per user, refreshes every 24h.
    Each slot = a high-rated (87+) player at 10% off buy price."""
    __tablename__ = "player_market"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    slot_index = Column(Integer, nullable=False)  # 1..5 (display)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    base_price = Column(Integer, nullable=False)
    final_price = Column(Integer, nullable=False)  # 10% off
    purchased = Column(Boolean, default=False)
    refreshed_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pm_user_slot", "user_id", "slot_index"),
    )


# ══════════════════════════════════════════════════════════════════════
# BOT TEAMS (for /vsbot)
# ══════════════════════════════════════════════════════════════════════

class BotTeam(Base):
    """A pre-built team users can play against via /vsbot. Admin-managed."""
    __tablename__ = "bot_teams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(String(300), nullable=True)
    difficulty = Column(String(20), default="Medium")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BotTeamPlayer(Base):
    """Members of a bot team. Players are real Player records.
    batting_order = 1..N for batting position."""
    __tablename__ = "bot_team_players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_team_id = Column(Integer, ForeignKey("bot_teams.id"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    batting_order = Column(Integer, default=1)
    is_captain = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_btp_team_order", "bot_team_id", "batting_order"),
    )


# ══════════════════════════════════════════════════════════════════════
# MATCH STATE — persistent state for in-progress matches
# Source of truth for the match flow; survives bot restarts/redeploys.
# ══════════════════════════════════════════════════════════════════════

class MatchState(Base):
    __tablename__ = "match_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id"), unique=True, nullable=False, index=True)
    # Full game state serialized as JSON
    state_json = Column(Text, nullable=False)
    # Explicit state-machine pointer: PICK_DELIVERY / PICK_LENGTH / PICK_SHOT /
    # PICK_NEW_BATSMAN / PICK_NEW_BOWLER / INNINGS_BREAK / COMPLETED
    next_action = Column(String(40), nullable=False, default="PICK_DELIVERY")
    # Optimistic concurrency token — incremented on every save
    version = Column(Integer, default=0, nullable=False)
    # Sequential ball number — used for callback idempotency
    ball_seq = Column(Integer, default=0, nullable=False)
    last_modified = Column(DateTime, default=datetime.utcnow)
    # ID of the message currently showing buttons (for re-rendering)
    last_prompt_msg_id = Column(Integer, nullable=True)


# ══════════════════════════════════════════════════════════════════════
# QUESTS — daily/monthly engagement objectives
# ══════════════════════════════════════════════════════════════════════

class Quest(Base):
    """Master definition of a quest. Admin-managed."""
    __tablename__ = "quests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(String(300), nullable=False)
    quest_type = Column(String(20), nullable=False)   # 'daily' or 'monthly'
    event_key = Column(String(50), nullable=False)
    target_count = Column(Integer, default=1, nullable=False)
    reward_points = Column(Integer, default=5, nullable=False)
    reward_coins = Column(Integer, default=0)
    reward_gems = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    emoji = Column(String(10), default="🎯")
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserQuestProgress(Base):
    """Per-user progress on a single quest for a single period."""
    __tablename__ = "user_quest_progress"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    quest_id = Column(Integer, ForeignKey("quests.id"), nullable=False)
    period_key = Column(String(10), nullable=False)
    progress = Column(Integer, default=0, nullable=False)
    completed = Column(Boolean, default=False, nullable=False)
    claimed = Column(Boolean, default=False, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow)
    # Whether this quest is one of the user's randomly-selected quests for the
    # current period. Only assigned quests show in /mq, count for tracking,
    # and are eligible for auto-claim at period end.
    assigned = Column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_uqp_user_period", "user_id", "period_key"),
        Index("ix_uqp_user_quest_period", "user_id", "quest_id", "period_key", unique=True),
    )


# ══════════════════════════════════════════════════════════════════════
# ACHIEVEMENTS — permanent unlockable badges
# ══════════════════════════════════════════════════════════════════════

class UserAchievement(Base):
    """Achievements unlocked by a user. The achievement key is hardcoded
    in services/achievement_service.py (CATALOG)."""
    __tablename__ = "user_achievements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    achievement_key = Column(String(50), nullable=False)
    unlocked_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_uach_user_key", "user_id", "achievement_key", unique=True),
    )


# ══════════════════════════════════════════════════════════════════════
# PLAYER FORM — last-5 match performances, drives in-match modifier
# ══════════════════════════════════════════════════════════════════════

class PlayerFormHistory(Base):
    """Recent match performance for a player owned by a user.
    Used to compute current 'form' which slightly modifies in-match outcomes.
    """
    __tablename__ = "player_form_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, index=True)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=True)
    # Batting performance (if batted)
    runs = Column(Integer, default=0)
    balls = Column(Integer, default=0)
    out = Column(Boolean, default=False)
    # Bowling performance (if bowled)
    wickets = Column(Integer, default=0)
    runs_conceded = Column(Integer, default=0)
    overs_bowled = Column(Float, default=0.0)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pfh_user_player", "user_id", "player_id"),
    )


# ══════════════════════════════════════════════════════════════════════
# LIVE COMMENTARY — admin-managed text bank for in-match flavour
# ══════════════════════════════════════════════════════════════════════

class CommentaryEntry(Base):
    """A single commentary line. event_key buckets like 'dot', 'four', 'six',
    'wicket_bowled', etc. Text supports placeholders: {batsman}, {bowler},
    {fielder}, {keeper}, {runs}.
    """
    __tablename__ = "commentary_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(40), nullable=False, index=True)
    text = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    weight = Column(Integer, default=1)  # higher = more likely to be chosen
    created_at = Column(DateTime, default=datetime.utcnow)


# ══════════════════════════════════════════════════════════════════════
# CUSTOM PLAYER CARD IMAGES — admin can upload custom card art per player
# ══════════════════════════════════════════════════════════════════════

class PlayerImage(Base):
    """Custom card image for a player. If active, replaces the auto-generated
    card in /claim, /buy, /myroster previews, and match in-play cards.
    Falls back to the default generator if no row exists or is_active=False.
    """
    __tablename__ = "player_images"

    id = Column(Integer, primary_key=True, autoincrement=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False, unique=True, index=True)
    # Image kind: 'default' (regular card), 'batsman' (in-match), 'bowler' (in-match)
    # v1 supports just 'default' which overrides all card displays.
    image_kind = Column(String(20), default="default")
    # Path on disk relative to project root (e.g. data/player_images/123.png)
    image_path = Column(String(300), nullable=False)
    # Optional admin-set caption / variant name
    label = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    uploaded_by = Column(String(100), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# NOTIFICATIONS — scheduled FOMO-style push messages from the bot
# ══════════════════════════════════════════════════════════════════════

class NotificationSchedule(Base):
    """A notification rule. The cron-like job fires it when conditions match."""
    __tablename__ = "notification_schedules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)            # admin label
    message = Column(Text, nullable=False)                # template — supports {first_name}, {coins}, {gems}, {streak}
    # Timing
    schedule_type = Column(String(20), default="daily")   # 'daily' | 'interval' | 'one_off'
    # For 'daily': fires once per day at hour:minute IST
    fire_hour = Column(Integer, default=18)               # 0-23 IST
    fire_minute = Column(Integer, default=0)              # 0-59 IST
    # For 'interval': repeats every N hours after last_fired_at
    interval_hours = Column(Integer, default=24)
    # Time window — only fire if current IST hour is between these two
    window_start_hour = Column(Integer, default=10)       # 0-23 IST
    window_end_hour = Column(Integer, default=22)         # 0-23 IST (exclusive)
    # Targeting filters
    target_filter = Column(String(20), default="all")     # 'all' | 'inactive_24h' | 'active' | 'low_coins' | 'has_streak'
    # State
    is_active = Column(Boolean, default=True)
    last_fired_at = Column(DateTime, nullable=True)       # last time the job actually fired
    sent_count = Column(Integer, default=0)               # cumulative recipients
    created_at = Column(DateTime, default=datetime.utcnow)


class NotificationLog(Base):
    """Per-user delivery record for a single notification fire."""
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    schedule_id = Column(Integer, ForeignKey("notification_schedules.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    sent_at = Column(DateTime, default=datetime.utcnow, index=True)
    delivered = Column(Boolean, default=True)
    error_text = Column(String(500), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# CLAIM RARITY TIERS — admin-configurable distribution for /claim
# ══════════════════════════════════════════════════════════════════════

class ClaimRarityTier(Base):
    """Rating tier definition for /claim pulls. Sum of probabilities should be ~100.
    If empty (no rows), code falls back to CLAIM_RARITY in config.py.
    """
    __tablename__ = "claim_rarity_tiers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(40), nullable=False)               # 'Bronze', 'Legendary', etc
    rating_min = Column(Integer, nullable=False)             # inclusive
    rating_max = Column(Integer, nullable=False)             # inclusive
    probability = Column(Float, nullable=False)              # 0.0 to 100.0 (percent)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    emoji = Column(String(10), default="🃏")


# ══════════════════════════════════════════════════════════════════════
# GAME CONFIG — admin-tunable economy values
# ══════════════════════════════════════════════════════════════════════

class GameConfig(Base):
    """Single-row configuration for tunable game values.
    Admin-managed via /economy. Code reads via config_service.get_config()
    which falls back to baked defaults if row missing.
    """
    __tablename__ = "game_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Match rewards
    match_win_coins_per_over = Column(Integer, default=300)
    match_win_gems_per_over = Column(Float, default=1.0)       # Float to allow 0.5
    match_loss_coins_per_over = Column(Integer, default=150)
    match_loss_gems_per_over = Column(Float, default=0.5)
    # GSpin gem range (for blue outcome)
    gspin_gem_min = Column(Integer, default=5)
    gspin_gem_max = Column(Integer, default=50)
    # Daily reward
    daily_coins = Column(Integer, default=1000)
    daily_gems = Column(Integer, default=0)
    daily_streak_bonus_coins = Column(Integer, default=200)    # extra per day of streak
    daily_streak_bonus_gems = Column(Integer, default=0)
    # Debut bonus
    debut_coins = Column(Integer, default=100000)
    debut_gems = Column(Integer, default=20)                    # was 100

    # ── Simulation tuning (additive percentage points applied at end) ──
    # These nudge final probabilities to fix systemic biases (e.g. too many dots).
    # Applied AFTER all per-ball modifiers but BEFORE normalization.
    # Default 0 = no adjustment (matches old behavior).
    sim_dot_adjust = Column(Float, default=-8.0)        # -8 = fewer dot balls
    sim_one_adjust = Column(Float, default=+5.0)        # +5 = more singles
    sim_two_adjust = Column(Float, default=+2.0)        # +2 = more twos
    sim_four_adjust = Column(Float, default=0.0)
    sim_six_adjust = Column(Float, default=0.0)
    sim_wicket_adjust = Column(Float, default=0.0)
    sim_extras_adjust = Column(Float, default=0.0)
    # ── Player market settings ──
    market_min_rating = Column(Integer, default=87)
    market_default_slots = Column(Integer, default=6)
    market_last_refresh_at = Column(DateTime, nullable=True)
    # Refresh schedule (IST). 0 = midnight IST, 9 = 9 AM IST, etc.
    market_refresh_hour_ist = Column(Integer, default=0)
    # Trait market settings
    trait_market_default_slots = Column(Integer, default=5)
    trait_market_last_refresh_at = Column(DateTime, nullable=True)
    # ── Scorecard color customization ──
    # Hex strings like "#c41e3a". Used as accent (header border, table-header
    # bottom border, RTG color, FoW labels, target line) on the per-innings
    # scorecard. Defaults match the original PRIMARY (red) / SECONDARY (teal).
    scorecard_color_inn1 = Column(String(9), default="#c41e3a")
    scorecard_color_inn2 = Column(String(9), default="#00c9a7")
    # Updated tracking (existing)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(80), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# (Player versioning is implemented at the Player table level via
# parent_player_id — see services/version_service.py for behavior.)
# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# MESSAGE TEMPLATES — admin-editable fixed strings used by the bot
# ══════════════════════════════════════════════════════════════════════

class MessageTemplate(Base):
    """Editable bot message. Bot code reads via message_service.get(key, ...).
    If a row exists with this key, it's used; otherwise the hardcoded default.
    """
    __tablename__ = "message_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(80), unique=True, nullable=False, index=True)
    label = Column(String(120), nullable=False)        # Human-readable name
    description = Column(String(400), nullable=True)   # explains where it shows + placeholders
    body = Column(Text, nullable=False)                # the message itself
    category = Column(String(40), default="general")   # group in admin UI
    is_active = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(80), nullable=True)


# ══════════════════════════════════════════════════════════════════════
# GLOBAL MARKETS — single shared market for ALL users (admin-managed)
# ══════════════════════════════════════════════════════════════════════
# These replace the per-user PlayerMarket / TraitMarket with a single
# market shared by all users. Admin controls slots, prices, quantities,
# and refreshes via the website.

class GlobalPlayerMarket(Base):
    """Single shared player market. Admin manages slots."""
    __tablename__ = "global_player_market"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slot_index = Column(Integer, nullable=False, unique=True, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    base_price = Column(Integer, nullable=False)        # price before discount
    final_price = Column(Integer, nullable=False)       # actual sell price
    quantity = Column(Integer, default=1)               # how many can be bought
    purchased_count = Column(Integer, default=0)        # how many already sold
    listed_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)


class GlobalTraitMarket(Base):
    """Single shared trait market. Admin manages slots."""
    __tablename__ = "global_trait_market"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slot_index = Column(Integer, nullable=False, unique=True, index=True)
    trait_id = Column(Integer, ForeignKey("traits.id"), nullable=False)
    base_price = Column(Integer, nullable=False)
    discount_pct = Column(Integer, default=0)
    final_price = Column(Integer, nullable=False)
    quantity = Column(Integer, default=10)              # traits can be re-bought
    purchased_count = Column(Integer, default=0)
    listed_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)


class MarketPurchase(Base):
    """Audit log of all global market purchases."""
    __tablename__ = "market_purchases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    market_type = Column(String(20), nullable=False)    # 'player' or 'trait'
    slot_index = Column(Integer, nullable=False)
    item_id = Column(Integer, nullable=False)            # player_id or trait_id
    item_name = Column(String(200), nullable=True)
    price_paid = Column(Integer, nullable=False)
    purchased_at = Column(DateTime, default=datetime.utcnow, index=True)


# ══════════════════════════════════════════════════════════════════════
# Pack system — buyable bundles that give 1 main + N bonus players
# ══════════════════════════════════════════════════════════════════════

class Pack(Base):
    """Admin-configurable pack definitions for /buypack."""
    __tablename__ = "packs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slot_number = Column(Integer, nullable=False, unique=True)  # display order 1, 2, 3...
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    emoji = Column(String(10), default="📦")

    # Cost — at least one of these should be > 0
    cost_coins = Column(Integer, default=0)
    cost_quest_points = Column(Integer, default=0)
    cost_gems = Column(Integer, default=0)

    # Filter mode for the MAIN player slot:
    #   'rating'  — match by rating range only (any version)
    #   'version' — match by version name only (any rating)
    #   'both'    — match BOTH (rating range AND version name)
    main_filter_mode = Column(String(10), default="rating")

    # Main player(s) — guaranteed
    main_min_rating = Column(Integer, default=85)
    main_max_rating = Column(Integer, default=87)
    main_count = Column(Integer, default=1)
    # JSON list of weights, one per integer rating in [min, max]. If null,
    # we use uniform distribution. Example for 85-87: "[60, 30, 10]"
    main_weights_json = Column(String(500), nullable=True)
    # JSON list of acceptable version names (case-insensitive match).
    # e.g. ["Star", "Star Card", "Star Player"]. Used when filter_mode is
    # 'version' or 'both'.
    main_versions_json = Column(String(500), nullable=True)

    # Bonus player(s) — extras included with the pack. Always rating-based,
    # any version (we don't restrict bonus pulls).
    bonus_min_rating = Column(Integer, default=74)
    bonus_max_rating = Column(Integer, default=80)
    bonus_count = Column(Integer, default=2)
    bonus_weights_json = Column(String(500), nullable=True)

    # Limits
    daily_limit = Column(Integer, default=0)  # 0 = unlimited

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PackPurchase(Base):
    """Audit log + daily-limit enforcement source."""
    __tablename__ = "pack_purchases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pack_id = Column(Integer, ForeignKey("packs.id"), nullable=False, index=True)
    cost_paid = Column(Integer, nullable=False)
    currency = Column(String(20), nullable=False)   # 'coins' / 'quest_points' / 'gems'
    players_json = Column(Text, nullable=True)      # serialized list of player_ids/names received
    purchased_at = Column(DateTime, default=datetime.utcnow, index=True)


class UnopenedPack(Base):
    """A pack the user has bought but not yet opened.
    Created on /buypack purchase, removed when /openpack opens it."""
    __tablename__ = "unopened_packs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pack_id = Column(Integer, ForeignKey("packs.id"), nullable=False)
    acquired_at = Column(DateTime, default=datetime.utcnow, index=True)
    source = Column(String(40), default="buypack")  # 'buypack' / 'admin' / 'reward' etc.


class Tour(Base):
    """A tour: a multi-match challenge between two users."""
    __tablename__ = "tours"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user1_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # creator
    user2_id = Column(Integer, ForeignKey("users.id"), nullable=False)  # invitee
    chat_id = Column(BigInteger, nullable=True)  # group chat where tour was created

    match_count = Column(Integer, nullable=False)        # 3, 4, 5, or 6
    overs_per_match = Column(Integer, nullable=False)    # 5+

    # Status flow:
    #   pending  → invite sent, awaiting user2's accept
    #   active   → user2 accepted; matches being played
    #   completed→ all matches played, winner decided
    #   expired  → either: user2 didn't accept in 30s,
    #              OR tour lifetime ended with unfinished matches
    #   declined → user2 explicitly declined
    status = Column(String(20), default="pending", index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)        # invite expiry (30s after create)
    lifetime_expires_at = Column(DateTime, nullable=True)  # tour expiry (matches*2 days)
    completed_at = Column(DateTime, nullable=True)

    # Score after every completed match (denormalized for fast leaderboard)
    user1_wins = Column(Integer, default=0)
    user2_wins = Column(Integer, default=0)

    # Winner once tour is decided
    winner_id = Column(Integer, nullable=True)  # null = ongoing OR drawn

    user1 = relationship("User", foreign_keys=[user1_id])
    user2 = relationship("User", foreign_keys=[user2_id])

    __table_args__ = (
        Index("ix_tours_user1_status", "user1_id", "status"),
        Index("ix_tours_user2_status", "user2_id", "status"),
    )


class TourMatch(Base):
    """A single match within a tour."""
    __tablename__ = "tour_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tour_id = Column(Integer, ForeignKey("tours.id"), nullable=False, index=True)
    match_number = Column(Integer, nullable=False)  # 1, 2, 3, ...
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=True)  # null until played

    # Pre-decided settings (so each tour match has its own venue)
    stadium = Column(String(100), nullable=True)
    pitch_type = Column(String(30), nullable=True)

    # Status:
    #   pending → not started yet
    #   playing → match_id set, match in progress
    #   done    → match completed
    #   forfeit → one side forfeited (idle)
    status = Column(String(20), default="pending")

    winner_id = Column(Integer, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_tour_matches_tour", "tour_id", "match_number"),
    )


class PlayerMatchStats(Base):
    """Per-player per-match stats — written at match-end so we can compute
    tour aggregates (most runs in a tour, etc.).

    This is in addition to the cumulative PlayerGameStats. PlayerGameStats
    is the lifetime total; PlayerMatchStats is the single-match snapshot.
    """
    __tablename__ = "player_match_stats"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, ForeignKey("matches.id", ondelete="CASCADE"), nullable=False, index=True)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    # Batting (this match)
    bat_runs = Column(Integer, default=0)
    bat_balls = Column(Integer, default=0)
    bat_fours = Column(Integer, default=0)
    bat_sixes = Column(Integer, default=0)
    bat_out = Column(Boolean, default=False)

    # Bowling (this match)
    bowl_wickets = Column(Integer, default=0)
    bowl_runs = Column(Integer, default=0)
    bowl_balls = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_pms_match_user", "match_id", "user_id"),
    )


class EventMedia(Base):
    """Media (GIF/animation) shown during matches for specific events.

    Admin uploads or links GIFs for each event_key. When the matching in-match
    event fires, a random enabled item (weighted) is picked and sent via
    Telegram's send_animation API.

    event_key values: see services.event_media_service.EVENT_KEYS
    source_type:
      'url'  → source is a public URL (Telegram fetches it directly)
      'file' → source is a path/filename under data/event_media/
    """
    __tablename__ = "event_media"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(30), nullable=False, index=True)
    source_type = Column(String(10), default="url")
    source = Column(Text, nullable=False)
    label = Column(String(120), nullable=True)
    weight = Column(Integer, default=1)
    enabled = Column(Boolean, default=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    uploaded_by = Column(String(80), nullable=True)
