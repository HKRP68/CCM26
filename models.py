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
    total_coins = Column(Integer, default=0)
    total_gems = Column(Integer, default=0)
    roster_count = Column(Integer, default=0)
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
    status = Column(String(20), default="pending", nullable=False)  # pending/accepted/rejected/expired
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
