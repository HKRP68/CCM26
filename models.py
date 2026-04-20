"""SQLAlchemy ORM models."""

from datetime import datetime, timedelta
from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, Boolean, DateTime, ForeignKey, Index
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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    roster = relationship("UserRoster", back_populates="user", cascade="all, delete-orphan")
    stats = relationship("UserStats", back_populates="user", uselist=False, cascade="all, delete-orphan")


class Player(Base):
    __tablename__ = "players"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(150), unique=True, nullable=False, index=True)
    version = Column(String(50), default="Base")
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

    __table_args__ = (Index("ix_players_rating", "rating"),)


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
