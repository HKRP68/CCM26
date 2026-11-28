"""Additive storage for official Events Studio campaigns."""

from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint

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


class StudioCompetition(Base):
    """A versioned lobby becomes the competition atomically at launch."""
    __tablename__ = "studio_competitions"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("studio_events.id"), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    kind = Column(String(8), nullable=False)
    status = Column(String(20), nullable=False, default="lobby")
    host_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    chat_id = Column(BigInteger)
    message_id = Column(BigInteger)
    invite = Column(String(32), unique=True)
    created_at = Column(DateTime, nullable=False)
    join_until = Column(DateTime, nullable=False)
    started_at = Column(DateTime)
    closed_at = Column(DateTime)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (Index("ix_studio_competitions_due", "status", "join_until"),)


class StudioParticipant(Base):
    """Human rows reserve an event entry. AI rows have no account or wallet."""
    __tablename__ = "studio_participants"

    id = Column(Integer, primary_key=True)
    competition_id = Column(Integer, ForeignKey("studio_competitions.id"), nullable=False)
    event_id = Column(Integer, ForeignKey("studio_events.id"), nullable=False)
    revision = Column(Integer, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    team_key = Column(String(40), nullable=False)
    claimed_stars = Column(Integer, nullable=False, default=0)
    reward_tier = Column(String(15))
    joined_at = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("competition_id", "team_key"),
        UniqueConstraint("event_id", "revision", "user_id"),
    )


class StudioFixture(Base):
    __tablename__ = "studio_fixtures"

    id = Column(Integer, primary_key=True)
    competition_id = Column(Integer, ForeignKey("studio_competitions.id"), nullable=False)
    fixture_key = Column(String(40), nullable=False)
    home = Column(String(40))
    away = Column(String(40))
    status = Column(String(20), nullable=False)
    version = Column(Integer, nullable=False, default=0)
    opens_at = Column(DateTime, nullable=False)
    deadline = Column(DateTime, nullable=False)
    due_at = Column(DateTime, nullable=False)
    next_pick_at = Column(DateTime)
    started_at = Column(DateTime)
    failure_at = Column(DateTime)
    failure_type = Column(String(90))
    seed = Column(String(64))
    step = Column(Integer, nullable=False, default=0)
    setup = Column(JSON, nullable=False, default=dict)
    state = Column(JSON)

    __table_args__ = (
        UniqueConstraint("competition_id", "fixture_key"),
        Index("ix_studio_fixtures_due", "status", "due_at", "next_pick_at"),
    )


class StudioFixtureResult(Base):
    __tablename__ = "studio_fixture_results"

    fixture_id = Column(Integer, ForeignKey("studio_fixtures.id"), primary_key=True)
    kind = Column(String(20), nullable=False)
    winner = Column(String(40))
    detail = Column(JSON, nullable=False)
    recorded_at = Column(DateTime, nullable=False)


class StudioContribution(Base):
    __tablename__ = "studio_contributions"

    fixture_id = Column(Integer, ForeignKey("studio_fixture_results.fixture_id"), primary_key=True)
    participant_id = Column(Integer, ForeignKey("studio_participants.id"), primary_key=True, index=True)
    stage = Column(String(20), nullable=False)
    metrics = Column(JSON, nullable=False)
    performance = Column(JSON, nullable=False)


class StudioStarClaim(Base):
    """The entitlement is also the permanent receipt after claiming."""
    __tablename__ = "studio_star_claims"

    id = Column(Integer, primary_key=True)
    participant_id = Column(Integer, ForeignKey("studio_participants.id"), nullable=False)
    objective_id = Column(String(40), nullable=False)
    fixture_id = Column(Integer, ForeignKey("studio_fixture_results.fixture_id"), nullable=False)
    stars = Column(Integer, nullable=False)
    earned_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    claimed_at = Column(DateTime)
    total_at_claim = Column(Integer)

    __table_args__ = (UniqueConstraint("participant_id", "objective_id"),)


class StudioCompetitionReward(Base):
    __tablename__ = "studio_competition_rewards"

    id = Column(Integer, primary_key=True)
    participant_id = Column(Integer, ForeignKey("studio_participants.id"), nullable=False)
    source = Column(String(80), nullable=False)
    tier = Column(String(15), nullable=False)
    assets = Column(JSON, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    claimed_at = Column(DateTime)
    ledger_id = Column(Integer)

    __table_args__ = (UniqueConstraint("participant_id", "source"),)


class StudioCompetitionNotice(Base):
    __tablename__ = "studio_competition_notices"

    competition_id = Column(Integer, ForeignKey("studio_competitions.id"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    created_at = Column(DateTime, nullable=False)
    sent_at = Column(DateTime)


class StudioCompetitionXP(Base):
    __tablename__ = "studio_competition_xp"

    fixture_id = Column(Integer, ForeignKey("studio_fixture_results.fixture_id"), primary_key=True)
    participant_id = Column(Integer, ForeignKey("studio_participants.id"), primary_key=True)
    amount = Column(Integer, nullable=False)
    daily_limit = Column(Integer, nullable=False)
    due_at = Column(DateTime, nullable=False)
    processed_at = Column(DateTime)
    outcome = Column(String(30))
    attempts = Column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_studio_competition_xp_due", "processed_at", "due_at"),)
