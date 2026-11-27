"""Additive storage for official Events Studio campaigns."""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint

from database import Base


class StudioEvent(Base):
    __tablename__ = "studio_events"

    id = Column(Integer, primary_key=True)
    slug = Column(String(80), unique=True, nullable=False)
    name = Column(String(120), nullable=False)
    status = Column(String(20), nullable=False, default="draft")
    draft_number = Column(Integer, nullable=False, default=1)
    published_number = Column(Integer)
    opens_at = Column(DateTime)
    closes_at = Column(DateTime)
    paused_at = Column(DateTime)
    delay_seconds = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (Index("ix_studio_events_availability", "status", "opens_at", "closes_at"),)


class StudioRevision(Base):
    __tablename__ = "studio_event_revisions"

    event_id = Column(Integer, ForeignKey("studio_events.id"), primary_key=True)
    number = Column(Integer, primary_key=True)
    config = Column(JSON, nullable=False)
    actor = Column(String(100), nullable=False)
    action = Column(String(30), nullable=False)
    digest = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class StudioEnrollment(Base):
    __tablename__ = "studio_event_enrollments"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("studio_events.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    team_key = Column(String(40), nullable=False)
    mode = Column(String(25), nullable=False)
    roster = Column(JSON, nullable=False)
    match_setup = Column(JSON)
    joined_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("event_id", "user_id"),)


class StudioGame(Base):
    __tablename__ = "studio_event_games"

    id = Column(Integer, primary_key=True)
    enrollment_id = Column(Integer, ForeignKey("studio_event_enrollments.id"), nullable=False)
    fixture_key = Column(String(40), nullable=False)
    attempt = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="scheduled")
    version = Column(Integer, nullable=False, default=0)
    opens_at = Column(DateTime, nullable=False)
    deadline = Column(DateTime, nullable=False)
    due_at = Column(DateTime, nullable=False)
    started_at = Column(DateTime)
    retry_at = Column(DateTime)
    next_pick_at = Column(DateTime, index=True)
    failure_at = Column(DateTime)
    failure_type = Column(String(90))
    seed = Column(String(64))
    step = Column(Integer, nullable=False, default=0)
    home = Column(String(40))
    away = Column(String(40))
    tier = Column(String(15))
    state = Column(JSON)

    __table_args__ = (
        UniqueConstraint("enrollment_id", "fixture_key", "attempt"),
        Index("ix_studio_games_due", "status", "due_at", "id"),
    )


class StudioResult(Base):
    __tablename__ = "studio_event_results"

    game_id = Column(Integer, ForeignKey("studio_event_games.id"), primary_key=True)
    kind = Column(String(20), nullable=False)
    winner = Column(String(40))
    detail = Column(JSON, nullable=False)
    recorded_at = Column(DateTime, nullable=False)


class StudioEntitlement(Base):
    __tablename__ = "studio_event_entitlements"

    id = Column(Integer, primary_key=True)
    enrollment_id = Column(Integer, ForeignKey("studio_event_enrollments.id"), nullable=False)
    source = Column(String(80), nullable=False)
    game_id = Column(Integer, ForeignKey("studio_event_results.game_id"), nullable=False)
    tier = Column(String(15), nullable=False)
    assets = Column(JSON, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    claimed_at = Column(DateTime)
    # The shared ActivityLog archives old rows out of the database. The
    # entitlement is the permanent receipt and the archived entry stays linkable.
    ledger_id = Column(Integer)

    __table_args__ = (UniqueConstraint("enrollment_id", "source"),)


class StudioAudit(Base):
    __tablename__ = "studio_event_audit"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("studio_events.id"), nullable=False, index=True)
    actor = Column(String(100), nullable=False)
    action = Column(String(30), nullable=False)
    detail = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class StudioXPJob(Base):
    __tablename__ = "studio_event_xp_jobs"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("studio_event_results.game_id"), nullable=False)
    enrollment_id = Column(Integer, ForeignKey("studio_event_enrollments.id"), nullable=False)
    source = Column(String(40), nullable=False)
    amount = Column(Integer, nullable=False)
    daily_limit = Column(Integer, nullable=False)
    due_at = Column(DateTime, nullable=False)
    processed_at = Column(DateTime)
    outcome = Column(String(30))
    attempts = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("enrollment_id", "source"),
        Index("ix_studio_cxp_due", "processed_at", "due_at", "id"),
    )


class StudioNotice(Base):
    __tablename__ = "studio_event_notices"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("studio_event_games.id"), nullable=False)
    kind = Column(String(20), nullable=False)
    offset_hours = Column(Integer, nullable=False, default=0)
    due_at = Column(DateTime, nullable=False)
    lease_until = Column(DateTime)
    completed_at = Column(DateTime)
    outcome = Column(String(20))
    attempts = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("game_id", "kind", "offset_hours"),
        Index("ix_studio_notices_due", "completed_at", "due_at", "lease_until"),
    )
