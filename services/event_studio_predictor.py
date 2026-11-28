"""Bounded, neutral, advisory playoff estimates from the canonical schedule."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import logging
from math import sqrt
from random import Random

from sqlalchemy import select, update

from database import get_session
from models_events import StudioCompetition, StudioQualificationJob
from services import event_studio_insights as insights, event_studio_play as legacy, event_studio_rules as rules
from services.event_studio_schedule import utc_text


logger = logging.getLogger(__name__)
MODEL_VERSION = 1


def invalidate(db, room, config, now):
    if not config["competition"].get("predictor_samples"):
        return
    job = db.get(StudioQualificationJob, room.id)
    if job:
        job.result_version += 1
        job.due_at = now
    else:
        db.add(StudioQualificationJob(competition_id=room.id, due_at=now))


def estimate(config, rows, board, *, room_id, version):
    statuses = insights.qualification(config, rows, board)
    if statuses is None:
        return None
    templates = rules.fixture_rows(config)
    possibilities = [fixture for fixture, result in rows if result is None and
                     templates[fixture.fixture_key]["stage"] == "league"]
    samples = config["competition"]["predictor_samples"]
    scopes = defaultdict(list)
    for row in statuses:
        scopes[row["group"]].append(row)
    chance = Counter()
    rng = Random(f"live-event:{room_id}:{version}:{MODEL_VERSION}")
    point_rules = config["points"]
    start_points = {row["team"]: row["points"] for row in board}
    start_wins = {row["team"]: row["won"] for row in board}
    for _ in range(samples):
        points, wins = start_points.copy(), start_wins.copy()
        for fixture in possibilities:
            sample = rng.random()
            if sample < .92:
                winner, loser = (fixture.home, fixture.away) if sample < .46 else (fixture.away, fixture.home)
                points[winner] += point_rules["win"]
                points[loser] += point_rules["loss"]
                wins[winner] += 1
            else:
                shared = point_rules["tie" if sample < .96 else "no_result"]
                points[fixture.home] += shared
                points[fixture.away] += shared
        for candidates in scopes.values():
            count = candidates[0]["slots"]
            # With no fixtures left, the canonical comparator already settled
            # NRR and seed. Otherwise randomly break unresolved points/wins ties.
            if all(row["remaining"] == 0 for row in candidates):
                teams = [row["team"] for row in candidates[:count]]
            else:
                teams = sorted((row["team"] for row in candidates),
                               key=lambda team: (-points[team], -wins[team], rng.random()))[:count]
            chance.update(teams)
    result = {}
    for row in statuses:
        key = row["team"]
        probability = chance[key] / samples
        result[key] = {"percent": round(probability * 100, 1),
                       "sample_margin": round(196 * sqrt(probability * (1 - probability) / samples), 1)}
    return {"version": MODEL_VERSION, "samples": samples, "teams": result,
            "model": "Neutral model (46% each side, 4% tie, 4% no result). Future NRR ties are approximated."}


def cached(db, room):
    job = db.get(StudioQualificationJob, room.id)
    if not job or job.result_version != job.computed_version or job.estimate is None:
        return None
    return {**job.estimate, "calculated_at": utc_text(job.calculated_at)}


def sweep(now=None, *, limit=4):
    from services import event_studio_competition as competitions

    now = now or datetime.utcnow()
    with get_session() as db:
        ids = db.scalars(select(StudioQualificationJob.competition_id).where(
            StudioQualificationJob.result_version != StudioQualificationJob.computed_version,
            StudioQualificationJob.due_at <= now).order_by(StudioQualificationJob.due_at).limit(limit)).all()
    for room_id in ids:
        with get_session() as db:
            job = db.get(StudioQualificationJob, room_id)
            version = job.result_version
            try:
                room = db.get(StudioCompetition, room_id)
                config = competitions._table_config(competitions._config(db, room))
                rows = competitions._rows(db, room)
                value = None if room.status == "cancelled" else estimate(
                    config, rows, legacy.table(config, rows), room_id=room_id, version=version)
                db.execute(update(StudioQualificationJob).where(
                    StudioQualificationJob.competition_id == room_id,
                    StudioQualificationJob.result_version == version).values(
                        estimate=value, computed_version=version, calculated_at=now))
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Could not update qualification estimate for competition %s", room_id)
                db.execute(update(StudioQualificationJob).where(
                    StudioQualificationJob.competition_id == room_id,
                    StudioQualificationJob.result_version == version).values(due_at=now + timedelta(minutes=5)))
                db.commit()
