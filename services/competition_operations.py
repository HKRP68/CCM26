"""Read-only monitoring and approved draft creation on the existing Studio worker."""

from copy import deepcopy
from datetime import datetime, timedelta
import logging

from sqlalchemy import and_, func, select

from database import get_session
from models import Tournament, TournamentMatch
from models_events import StudioCompetition, StudioEvent, StudioFixture, StudioRevision
from models_universe import CompetitionProjection, CompetitionRatingEvent, CompetitionRotation
from services import competition_sources as sources, event_studio as studio, event_studio_schedule as schedule


logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 4


def monitor(db, now):
    statuses = {}
    if sources.enabled(sources.LIVE):
        statuses[sources.LIVE] = dict(db.execute(select(StudioEvent.status, func.count(StudioEvent.id))
                                                 .group_by(StudioEvent.status)).all())
    if sources.enabled(sources.CHALLENGE):
        statuses[sources.CHALLENGE] = dict(db.execute(select(Tournament.status, func.count(Tournament.id))
            .where((Tournament.kind == sources.CHALLENGE) | Tournament.kind.is_(None))
            .group_by(Tournament.status)).all())
    lobbies = db.execute(select(StudioCompetition.id, StudioCompetition.event_id, StudioCompetition.join_until)
                         .where(StudioCompetition.status == "lobby").order_by(StudioCompetition.join_until).limit(20)).all() if sources.enabled(sources.LIVE) else []
    incidents = db.execute(select(StudioFixture.id, StudioCompetition.event_id, StudioFixture.failure_type, StudioFixture.deadline)
                           .join(StudioCompetition).where(StudioFixture.failure_at.is_not(None))
                           .order_by(StudioFixture.failure_at.desc()).limit(30)).all() if sources.enabled(sources.LIVE) else []
    challenge = db.execute(select(Tournament.id, Tournament.name, Tournament.round_deadline_at)
                           .where(Tournament.status == "active", Tournament.round_deadline_at < now,
                                  (Tournament.kind == sources.CHALLENGE) | Tournament.kind.is_(None))
                           .order_by(Tournament.round_deadline_at).limit(20)).all() if sources.enabled(sources.CHALLENGE) else []
    jobs = db.scalars(select(CompetitionRotation).order_by(CompetitionRotation.id.desc()).limit(30)).all()
    projections = db.scalars(select(CompetitionProjection).order_by(CompetitionProjection.id)).all()
    flags = []
    rated = next((row for row in projections if row.id == 1 and row.fingerprint), None)
    if sources.flag("RANKINGS") and rated:
        query = (select(CompetitionRatingEvent).where(
            CompetitionRatingEvent.revision == rated.fingerprint,
            CompetitionRatingEvent.scope == "overall", CompetitionRatingEvent.format == "all",
            CompetitionRatingEvent.season == 0, func.json_array_length(CompetitionRatingEvent.detail["flags"]) > 0)
            .order_by(CompetitionRatingEvent.recorded_at.desc()).limit(25))
        flags = [{"source": row.source, "instance": row.instance_id, "fixture": row.fixture_id,
                  "signals": row.detail["flags"]} for row in db.scalars(query) if sources.enabled(row.source)]
    live_windows = []
    if sources.enabled(sources.LIVE):
        query = (select(StudioEvent, StudioRevision.config).join(StudioRevision, and_(
                    StudioRevision.event_id == StudioEvent.id,
                    StudioRevision.number == func.coalesce(StudioEvent.published_number, StudioEvent.draft_number)))
                 .where(StudioEvent.status.in_(("draft", *studio.OPEN_STATUSES)),
                        StudioRevision.config["kind"].as_string() == "live")
                 .order_by(StudioEvent.opens_at.desc().nulls_last(), StudioEvent.id.desc()).limit(30))
        live_windows = [{"id": event.id, "name": event.name, "status": event.status, "opens": event.opens_at,
                         "closes": event.closes_at, "format": config["format"], "capacity": config["max_users"],
                         "maximum": studio.rules.liability(config)} for event, config in db.execute(query)]
    return {"statuses": statuses, "lobbies": lobbies, "incidents": incidents,
            "challenge_deadlines": challenge, "rotation": jobs, "projections": projections,
            "live_windows": live_windows, "ranking_flags": flags}


def _template(db, template_id):
    event = db.get(StudioEvent, template_id)
    if event is None or event.published_number is None:
        raise ValueError("Select an approved, published Live Event template.")
    source = db.get(StudioRevision, (event.id, event.published_number))
    if source.config["kind"] != "live":
        raise ValueError("This rotation only builds Live Event drafts.")
    return event, source


def _config(config, opens):
    authored = deepcopy({key: value for key, value in config.items() if key in studio.defaults()})
    zone = authored["timezone"]
    original = schedule.utc_instant(authored["opens_at"], zone)
    shift = opens - original
    authored["opens_at"] = schedule.utc_text(opens)
    authored["closes_at"] = schedule.utc_text(schedule.utc_instant(authored["closes_at"], zone) + shift)
    for fixture in authored["fixtures"]:
        for key in ("opens_at", "deadline"):
            fixture[key] = schedule.utc_text(schedule.utc_instant(fixture[key], zone) + shift)
    # Conditions, manually locked times and the official template stay pinned
    # to the same UTC offsets. Record derived inputs against the shifted draft.
    authored["generation"] = {}
    clean = studio.normalize(authored)
    schedule.validate(clean, clean["fixtures"])
    return clean


def preview(db, template_id, first, weeks, count, now):
    if not sources.flag("ROTATION") or not sources.enabled(sources.LIVE):
        raise ValueError("Competition draft rotation is disabled.")
    if type(weeks) is not int or not 1 <= weeks <= 26 or type(count) is not int or not 1 <= count <= 12:
        raise ValueError("Choose 1–12 editions with 1–26 weeks between them.")
    event, source = _template(db, template_id)
    zone = source.config["timezone"]
    from zoneinfo import ZoneInfo
    from datetime import timezone
    first_utc = schedule.utc_instant(first, zone)
    if first_utc <= now:
        raise ValueError("Choose a future opening time.")
    first_local = first_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))
    plan = []
    windows = db.execute(select(StudioEvent.id, StudioEvent.name, StudioEvent.opens_at, StudioEvent.closes_at)
                         .where(StudioEvent.status.in_(studio.OPEN_STATUSES), StudioEvent.closes_at > first_utc)).all()
    for slot in range(count):
        wall = first_local.replace(tzinfo=None) + timedelta(weeks=slot * weeks)
        opens = first_utc if not slot else schedule.utc_instant(wall.isoformat(), zone)
        config = _config(source.config, opens)
        studio.validate_publish(db, config, now)
        if not studio.allowed("live", config["format"]):
            raise ValueError("Publication for this format is disabled.")
        close = schedule.utc_instant(config["closes_at"], zone)
        overlaps = [{"id": key, "name": label} for key, label, start, end in windows if start < close and end > opens]
        plan.append({"opens": opens, "close": close, "timezone": zone, "capacity": config["max_users"],
                     "overlapping_published_events": overlaps, "liability": studio.rules.liability(config)})
    return event, source, plan


def queue(db, template_id, first, weeks, count, actor, reason, now):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise ValueError("Enter a short approval reason.")
    event = studio.lock(db, StudioEvent, template_id)
    if event is None:
        raise ValueError("Template unavailable.")
    _, source, plan = preview(db, template_id, first, weeks, count, now)
    existing = set(db.scalars(select(CompetitionRotation.opens_at).filter_by(template_id=event.id)))
    for row in plan:
        if row["opens"] not in existing:
            db.add(CompetitionRotation(template_id=event.id, revision=source.number, opens_at=row["opens"],
                                       due_at=now, created_by=actor[:100], reason=reason))
    return len(plan)


def retry(db, job_id, now):
    if not sources.enabled(sources.LIVE) or not sources.flag("ROTATION"):
        raise ValueError("Competition draft rotation is disabled.")
    job = studio.lock(db, CompetitionRotation, job_id)
    if job is None or job.status != "failed" or job.draft_id is not None or job.opens_at <= now:
        raise ValueError("Choose a failed rotation with a future opening date.")
    job.status, job.due_at, job.attempts = "queued", now, 0
    return job


def sweep(now=None):
    if not sources.enabled(sources.LIVE) or not sources.flag("ROTATION"):
        return
    now = now or datetime.utcnow()
    with get_session() as db:
        ids = db.scalars(select(CompetitionRotation.id).where(CompetitionRotation.status == "queued",
                         CompetitionRotation.due_at <= now).order_by(CompetitionRotation.due_at).limit(2)).all()
    for key in ids:
        with get_session() as db:
            job = studio.lock(db, CompetitionRotation, key, skip_locked=True)
            if not job or job.status != "queued" or job.due_at > now:
                continue
            try:
                with db.begin_nested():
                    event = db.get(StudioEvent, job.template_id)
                    source = db.get(StudioRevision, (job.template_id, job.revision))
                    config = _config(source.config, job.opens_at)
                    if not studio.allowed("live", config["format"]):
                        raise ValueError("The format rollout is paused.")
                    studio.validate_publish(db, config, now)
                    stamp = job.opens_at.strftime("%Y%m%d-%H%M%S")
                    slug = f"rotation-{event.id}-{stamp}"
                    draft = studio.create(db, f"{event.name[:93]} · {job.opens_at:%Y-%m-%d}", slug, job.created_by, config)
                    job.draft_id = draft.id
                    job.status, job.error = "prepared", None
            except Exception as exc:
                job.attempts += 1
                job.error = str(exc)[:250] if isinstance(exc, ValueError) else type(exc).__name__
                job.due_at = now + timedelta(minutes=2**job.attempts)
                if job.attempts >= MAX_ATTEMPTS:
                    job.status = "failed"
                logger.exception("Competition rotation job %s failed", key)
            db.commit()
