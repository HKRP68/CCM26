"""Read-only identity and results for the canonical Events and Challenge League stores."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os

from sqlalchemy import and_, select

from models import Match, Tournament, TournamentMatch
from models_events import (
    StudioCompetition, StudioFixture, StudioFixtureResult, StudioParticipant, StudioRevision,
)
from models_universe import CompetitionControllerEvidence
from services.event_studio_schedule import utc_text

LIVE = "live"
CHALLENGE = "challenge"
SCOPES = (LIVE, CHALLENGE)
CL_VISIBLE = ("active", "completed")
FORMATS = ("T20", "ODI")


def flag(feature=None):
    key = "COMPETITION_UNIVERSE" + (f"_{feature}" if feature else "")
    return os.getenv(key, "").strip().lower() in ("1", "true", "yes")


def enabled(source=None):
    if not flag():
        return False
    if source is None:
        return True
    return source in SCOPES and os.getenv(f"COMPETITION_UNIVERSE_{source.upper()}", "1").strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Result:
    source: str
    instance: int
    fixture: int
    revision: int
    format: str
    sides: tuple
    winner: object
    status: str
    stage: str
    recorded_at: object
    innings: tuple
    controllers: tuple
    eligibility: str
    simulated: bool = False
    telemetry: dict | None = None


def format_of(overs, explicit=None):
    if explicit == "ODI" or overs == 50:
        return "ODI"
    if explicit == "The100":
        return "The100"
    return "T20" if overs == 20 else f"{overs} overs" if overs else "Unknown"


def resolve(db, user, source, key):
    if source not in SCOPES or not enabled(source):
        raise ValueError("This competition source is unavailable.")
    if source == LIVE:
        room = db.get(StudioCompetition, key)
        if room is None or db.scalar(select(StudioParticipant.id).filter_by(competition_id=key, user_id=user.id)) is None:
            raise ValueError("This Live Event competition is unavailable.")
        return room
    tour = db.get(Tournament, key)
    if tour is None or tour.status not in CL_VISIBLE or tour.kind not in (None, CHALLENGE):
        raise ValueError("This Challenge League tournament is unavailable.")
    return tour


def live_config(db, room):
    return db.get(StudioRevision, (room.event_id, room.revision)).config


def live_result(room, config, fixture, result, humans, templates=None):
    if templates is None:
        templates = {row["key"]: row for row in config["fixtures"]}
    sides = fixture.home, fixture.away
    controllers = tuple(humans.get(side) for side in sides)
    reason = ("verified" if room.kind == "multi" and all(controllers) and controllers[0] != controllers[1]
              else "solo" if room.kind == "solo" else "ai_filled")
    if result.kind != "completed":
        reason = result.kind
    return Result(LIVE, room.id, fixture.id, room.revision, config["format"], sides, result.winner,
                  result.kind, templates[fixture.fixture_key]["stage"], result.recorded_at,
                  tuple(result.detail.get("innings", [])), controllers, reason,
                  telemetry=result.detail.get("universe_telemetry"))


def fingerprint(fixture):
    identity = [fixture.match_id, fixture.team1_id, fixture.team2_id, fixture.winner_team_id,
                fixture.result_type, fixture.inn1_runs, fixture.inn1_wickets, fixture.inn1_balls,
                fixture.inn2_runs, fixture.inn2_wickets, fixture.inn2_balls,
                utc_text(fixture.completed_at) if fixture.completed_at else None]
    return sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def capture_challenge(db, tour, fixture, state):
    """Capture actual match seats at finalization, never owner/co-owner inference."""
    if not enabled(CHALLENGE) or not any(flag(feature) for feature in ("RANKINGS", "ANALYTICS", "COLLECTION")) or tour.kind not in (None, CHALLENGE) or fixture.match_id is None:
        return
    from services.match_handover import controller_uid

    seats = fixture.host_user_id, fixture.target_user_id
    controllers = (None, None)
    match = db.get(Match, fixture.match_id)
    if state.get("is_bot_match") or state.get("is_vsbot") or state.get("bot_user_id"):
        reason = "ai_filled"
    elif state.get("_planned_winner_user_id") or state.get("_planned_super_overs") or fixture.planned_winner_team_id or fixture.planned_super_overs:
        reason = "admin_planned"
    elif state.get("captain_handovers") or any(controller_uid(state, seat) != seat for seat in seats if seat):
        reason = "multiple_controllers"
    elif (state.get("mode") == "cipl_approach" and match and match.tournament_id == tour.id
          and None not in seats and len(set(seats)) == 2 and set(seats) == {match.user1_id, match.user2_id}):
        reason, controllers = "verified", seats
    else:
        reason = "unverified_controller"
    row = db.get(CompetitionControllerEvidence, fixture.id)
    if not row:
        row = CompetitionControllerEvidence(fixture_id=fixture.id)
        db.add(row)
    row.match_id = fixture.match_id
    row.controller_a, row.controller_b = controllers
    row.fingerprint = fingerprint(fixture)
    row.recorded_at = fixture.completed_at
    row.reason = reason


def challenge_result(tour, fixture, match=None, evidence=None):
    sides = fixture.team1_id, fixture.team2_id
    kind = fixture.result_type or ("win" if fixture.winner_team_id else "tie")
    if kind not in ("win", "tie", "no_result"):
        reason = kind
    elif kind == "no_result":
        reason = "no_result"
    elif fixture.is_simulated:
        reason = "simulated"
    elif fixture.planned_winner_team_id or fixture.planned_super_overs:
        reason = "admin_planned"
    elif match is None:
        reason = "manual_or_historic"
    elif match.end_reason != "completed":
        reason = match.end_reason or "unknown_end_reason"
    elif evidence is None:
        reason = "unverified_controller"
    elif evidence.match_id != fixture.match_id or evidence.fingerprint != fingerprint(fixture):
        reason = "corrected_without_evidence"
    else:
        reason = evidence.reason
    controllers = ((evidence.controller_a, evidence.controller_b) if reason == "verified" else (None, None))
    innings = tuple({"team": team, "runs": runs, "wickets": wickets, "balls": balls}
                   for team, runs, wickets, balls in (
        (sides[0], fixture.inn1_runs, fixture.inn1_wickets, fixture.inn1_balls),
        (sides[1], fixture.inn2_runs, fixture.inn2_wickets, fixture.inn2_balls))
                   if team is not None and runs is not None)
    return Result(CHALLENGE, tour.id, fixture.id, 0, format_of(match.overs if match else tour.overs,
        match.ball_format if match else None), sides, fixture.winner_team_id,
        "completed" if kind in ("win", "tie") else kind, fixture.stage, fixture.completed_at,
        innings, controllers, reason, fixture.is_simulated)


def result_rows(db, source, instance, ids=None, *, limit=2500):
    if source == LIVE:
        config = live_config(db, instance)
        humans = dict(db.execute(select(StudioParticipant.team_key, StudioParticipant.user_id)
                                 .filter_by(competition_id=instance.id)).all())
        templates = {row["key"]: row for row in config["fixtures"]}
        query = (select(StudioFixture, StudioFixtureResult)
                 .join(StudioFixtureResult, StudioFixtureResult.fixture_id == StudioFixture.id)
                 .where(StudioFixture.competition_id == instance.id)
                 .order_by(StudioFixtureResult.recorded_at, StudioFixture.id))
        if ids:
            query = query.where(StudioFixture.id.in_(ids))
        from sqlalchemy.orm import defer
        return [(live_result(instance, config, fixture, result, humans, templates), None)
                for fixture, result in db.execute(query.options(defer(StudioFixture.state), defer(StudioFixture.seed)).limit(limit))]
    query = (select(TournamentMatch, Match, CompetitionControllerEvidence)
             .outerjoin(Match, Match.id == TournamentMatch.match_id)
             .outerjoin(CompetitionControllerEvidence, CompetitionControllerEvidence.fixture_id == TournamentMatch.id)
             .where(TournamentMatch.tournament_id == instance.id, TournamentMatch.status == "completed")
             .order_by(TournamentMatch.completed_at, TournamentMatch.id))
    if ids:
        query = query.where(TournamentMatch.id.in_(ids))
    return [(challenge_result(instance, fixture, match, evidence), fixture.scorecard_json)
            for fixture, match, evidence in db.execute(query.limit(limit))]
