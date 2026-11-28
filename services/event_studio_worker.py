"""Reconcile scheduled releases, personal deadlines and AI results from durable rows."""

import asyncio
from datetime import datetime, timedelta
import logging
from time import monotonic

from database import get_session
from models import User
from models_events import StudioEnrollment, StudioEvent, StudioGame, StudioXPJob
from services import event_studio as studio, event_studio_notices as notices, event_studio_play as play
from sqlalchemy import select

logger = logging.getLogger(__name__)


def sweep(clock=datetime.utcnow, limit=40):
    now = clock()
    prompts = []
    _sweep_community(now)
    with get_session() as db:
        if not studio.flag("PUBLISH") and db.scalar(
                select(StudioEvent.id).where(StudioEvent.status.in_(("scheduled", "live", "closing", "paused"))).limit(1)) is None:
            return prompts
        ids = play.due_ids(db, now, limit)
    stop_at = monotonic() + 12
    for game_id in ids:
        if monotonic() >= stop_at:
            break
        try:
            with get_session() as db:
                next_prompt = play.run_due(db, game_id, clock())
                if next_prompt:
                    user_id, event_id, version = next_prompt
                    telegram_id = db.scalar(select(User.telegram_id).where(User.id == user_id))
                db.commit()
                if next_prompt and telegram_id:
                    prompts.append((telegram_id, event_id, game_id, version))
        except Exception as exc:
            logger.exception("Events Studio job failed for game %s", game_id)
            with get_session() as db:
                game = studio.lock(db, StudioGame, game_id)
                if not game or game.status not in play.PENDING or game.due_at >= now:
                    continue
                # For an active user match the due_at value is its authoritative
                # cutoff. Delay only the worker retry using its separate field.
                game.retry_at = now + timedelta(minutes=2)
                game.failure_at = now
                game.failure_type = type(exc).__name__[:90]
                db.commit()
    prompts.extend(_sweep_competitions(clock, limit))
    with get_session() as db:
        play.sync_lifecycle(db, clock())
        db.commit()
    return prompts



def _sweep_competitions(clock, limit):
    from services import event_studio_competition as competitions

    _sweep_competition_xp(clock())
    prompts = []
    with get_session() as db:
        rooms, fixtures = competitions.due_work(db, clock(), limit=min(limit, 24))
    for room_id in rooms:
        try:
            with get_session() as db:
                competitions.expire_lobby(db, room_id, clock(), clock=clock)
                db.commit()
        except Exception:
            logger.exception("Live Event lobby %s could not start", room_id)
    stop_at = monotonic() + 10
    for fixture_id in fixtures:
        if monotonic() >= stop_at:
            break
        try:
            with get_session() as db:
                try:
                    refresh = competitions.run_fixture(db, fixture_id, clock())
                    if refresh:
                        prompts.append(("competition", refresh))
                except studio.ExecutionUnavailable:
                    logger.exception("Live Event fixture %s saved an engine failure", fixture_id)
                db.commit()
        except Exception as exc:
            logger.exception("Live Event fixture %s worker failed", fixture_id)
            with get_session() as db:
                competitions.note_failure(db, fixture_id, clock(), exc)
                db.commit()
    return prompts


def _sweep_competition_xp(now):
    from models_events import StudioCompetitionXP, StudioParticipant
    from services.community_hooks import on_event_match_completed
    from services.economy_transactions import lock_user_for_update
    from sqlalchemy import update

    with get_session() as db:
        jobs = db.execute(select(StudioCompetitionXP.fixture_id, StudioCompetitionXP.participant_id)
                          .where(StudioCompetitionXP.processed_at.is_(None), StudioCompetitionXP.due_at <= now)
                          .order_by(StudioCompetitionXP.due_at).limit(20)).all()
    for fixture, participant in jobs:
        with get_session() as db:
            conditions = (StudioCompetitionXP.fixture_id == fixture, StudioCompetitionXP.participant_id == participant)
            if db.get_bind().dialect.name == "sqlite":
                db.execute(update(StudioCompetitionXP).where(*conditions).values(attempts=StudioCompetitionXP.attempts))
            job = db.scalar(select(StudioCompetitionXP).where(*conditions).with_for_update(skip_locked=True))
            if not job or job.processed_at or job.due_at > now:
                continue
            user_id = db.get(StudioParticipant, participant).user_id
            job.attempts += 1
            try:
                with db.begin_nested():
                    account = lock_user_for_update(db, db.get(User, user_id))
                    award = ({"reason": "banned"} if account.is_banned else
                             on_event_match_completed(db, user_id, amount=job.amount, limit=job.daily_limit,
                                                      now=now, source=f"studio-competition:{fixture}"))
                    if award["reason"] in ("error", "duplicate_race"):
                        raise RuntimeError("Competition XP needs retry.")
                job.outcome, job.processed_at = award["reason"], now
            except Exception:
                logger.exception("Competition XP failed for fixture %s", fixture)
                job.due_at = now + timedelta(minutes=min(60, 2**min(job.attempts, 6)))
            db.commit()

def _sweep_community(now):
    from services.community_hooks import on_event_match_completed
    from services.economy_transactions import lock_user_for_update

    with get_session() as db:
        jobs = db.scalars(select(StudioXPJob.id).where(StudioXPJob.processed_at.is_(None),
                          StudioXPJob.due_at <= now).order_by(StudioXPJob.due_at, StudioXPJob.id).limit(20)).all()
    for job_id in jobs:
        with get_session() as db:
            job = studio.lock(db, StudioXPJob, job_id, skip_locked=True)
            if job is None or job.processed_at or job.due_at > now:
                continue
            entry = db.get(StudioEnrollment, job.enrollment_id)
            job.attempts += 1
            try:
                with db.begin_nested():
                    user = db.get(User, entry.user_id)
                    lock_user_for_update(db, user)
                    outcome = on_event_match_completed(
                        db, user.id, amount=job.amount, limit=job.daily_limit, now=now,
                        source=f"studio:{entry.id}:{job.source}")
                    if outcome["reason"] in ("error", "duplicate_race"):
                        raise RuntimeError("Community XP transaction needs retry.")
                job.outcome = outcome["reason"]
                job.processed_at = now
            except Exception:
                logger.exception("Events Studio Community XP job failed for job %s", job_id)
                job.due_at = now + timedelta(minutes=min(60, 2**min(job.attempts, 6)))
            db.commit()


async def _tick(context):
    from handlers.event_studio import send_timeout_prompt

    prompts = await asyncio.to_thread(sweep)
    from services import event_studio_award_media, event_studio_predictor
    await asyncio.to_thread(event_studio_predictor.sweep)
    await asyncio.to_thread(event_studio_award_media.sweep)
    for prompt in prompts:
        if prompt[0] == "competition":
            from handlers.event_competitions import send_timeout_prompt as send_shared_prompt
            await send_shared_prompt(context.bot, prompt[1])
        else:
            await send_timeout_prompt(context.bot, *prompt)
    await notices.send_due(context.bot)
    await _send_competition_notices(context.bot)
    from services.event_studio_group import send_due
    await send_due(context.bot)
    from services import competition_collection, competition_operations, competition_rankings
    await asyncio.to_thread(competition_rankings.sweep)
    await asyncio.to_thread(competition_collection.sweep)
    await asyncio.to_thread(competition_operations.sweep)



async def _send_competition_notices(bot):
    from telegram import InlineKeyboardMarkup
    from services import event_studio_competition as competitions
    from services.miniapp_buttons import miniapp_button

    def reserve():
        with get_session() as db:
            output = competitions.take_start_notices(db, datetime.utcnow())
            db.commit()
            return output

    for telegram_id, event_id, name in await asyncio.to_thread(reserve):
        link = miniapp_button("Open tournament", f"events_{event_id}")
        try:
            await bot.send_message(telegram_id, f"{name} has started. Open /myevents to see your fixtures.",
                                   reply_markup=InlineKeyboardMarkup([[link]]) if link else None)
        except Exception:
            logger.info("Could not deliver competition start notice for event %s", event_id, exc_info=True)

def register(app):
    if app.job_queue:
        app.job_queue.run_repeating(_tick, interval=20, first=5, name="event_studio")
