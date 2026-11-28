"""Additive CMU competition evidence and projections."""

from sqlalchemy import Column, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, JSON, String, UniqueConstraint

from database import Base


class CompetitionControllerEvidence(Base):
    __tablename__ = "competition_controller_evidence"

    fixture_id = Column(Integer, ForeignKey("tournament_matches.id", ondelete="CASCADE"), primary_key=True)
    match_id = Column(Integer, nullable=False)
    controller_a = Column(Integer, nullable=True, index=True)
    controller_b = Column(Integer, nullable=True, index=True)
    reason = Column(String(40), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    recorded_at = Column(DateTime, nullable=False)


class CompetitionArchivedResult(Base):
    __tablename__ = "competition_archived_results"

    fixture_id = Column(Integer, primary_key=True, autoincrement=False)
    tournament_id = Column(Integer, primary_key=True, autoincrement=False)
    recorded_at = Column(DateTime, nullable=False, index=True)
    detail = Column(JSON, nullable=False)


class CompetitionProjection(Base):
    __tablename__ = "competition_projections"

    id = Column(Integer, primary_key=True)
    fingerprint = Column(String(64))
    computed_at = Column(DateTime)
    reason = Column(String(120))
    counts = Column(JSON, nullable=False, default=dict)


class CompetitionRating(Base):
    __tablename__ = "competition_ratings"

    scope = Column(String(20), primary_key=True)
    format = Column(String(10), primary_key=True)
    season = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    rating = Column(Integer, nullable=False)
    matches = Column(Integer, nullable=False)
    opponents = Column(Integer, nullable=False)
    wins = Column(Integer, nullable=False)
    draws = Column(Integer, nullable=False)

    __table_args__ = (Index("ix_competition_rating_board", "scope", "format", "season", "rating"),)


class CompetitionRatingEvent(Base):
    __tablename__ = "competition_rating_events"

    revision = Column(String(64), primary_key=True)
    scope = Column(String(20), primary_key=True)
    format = Column(String(10), primary_key=True)
    season = Column(Integer, primary_key=True)
    source = Column(String(12), primary_key=True)
    instance_id = Column(Integer, primary_key=True)
    fixture_id = Column(Integer, primary_key=True)
    detail = Column(JSON, nullable=False)
    recorded_at = Column(DateTime, nullable=False)


class CompetitionCatalogue(Base):
    __tablename__ = "competition_catalogue"

    source = Column(String(12), primary_key=True)
    family_id = Column(Integer, primary_key=True)
    label = Column(String(100), nullable=False)
    category = Column(String(30), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    retired_at = Column(DateTime)


class CompetitionCatalogueLink(Base):
    __tablename__ = "competition_catalogue_links"

    source = Column(String(12), primary_key=True)
    edition_id = Column(Integer, primary_key=True)
    family_id = Column(Integer, nullable=False)

    __table_args__ = (ForeignKeyConstraint(("source", "family_id"),
                                           ("competition_catalogue.source", "competition_catalogue.family_id")),)


class CompetitionCollectible(Base):
    __tablename__ = "competition_collectibles"

    source = Column(String(12), primary_key=True)
    instance_id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    kind = Column(String(24), primary_key=True)
    evidence = Column(String(100), primary_key=True)
    family_id = Column(Integer, nullable=True)
    details = Column(JSON, nullable=False)
    earned_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime)

    __table_args__ = (Index("ix_competition_collectibles_user", "user_id", "earned_at"),)


class CompetitionRotation(Base):
    __tablename__ = "competition_rotations"

    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("studio_events.id"), nullable=False)
    revision = Column(Integer, nullable=False)
    opens_at = Column(DateTime, nullable=False)
    due_at = Column(DateTime, nullable=False, index=True)
    created_by = Column(String(100), nullable=False)
    reason = Column(String(500), nullable=False)
    status = Column(String(16), nullable=False, default="queued")
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(String(250))
    draft_id = Column(Integer, ForeignKey("studio_events.id"))

    __table_args__ = (UniqueConstraint("template_id", "opens_at"),)
