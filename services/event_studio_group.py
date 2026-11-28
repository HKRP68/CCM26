"""Durable, rate-limited notices for explicitly enabled hosting groups."""

import asyncio
from datetime import datetime, timedelta
import logging

from sqlalchemy import func, or_, select, text, update
from telegram import InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter

from database import get_session
from models_events import StudioCompetition, StudioEvent, StudioFixture, StudioGroupNotice
from services.miniapp_buttons import miniapp_button


logger = logging.getLogger(__name__)
LEASE = timedelta(minutes=2)
MAX_ATTEMPTS = 5
HOURLY_LIMIT = 3
DAILY_LIMIT = 12


def enqueue(db, room, config, source, message, now, *, fixture=None, lead_hours=None):
    if room.kind != "multi" or room.chat_id is None or not config["competition"].get("group_engagement"):
        return
    due = max(now, fixture.deadline - timedelta(hours=lead_hours)) if fixture else now
    expires = fixture.deadline if fixture else now + timedelta(hours=12)
    if due < expires:
        db.add(StudioGroupNotice(competition_id=room.id, chat_id=room.chat_id, source=source,
                                  text=message[:500], due_at=due, expires_at=expires,
                                  fixture_id=fixture.id if fixture else None, lead_hours=lead_hours))


def schedule_fixture(db, room, fixture, config, event, now):
    if not config["competition"].get("group_engagement"):
        return
    sides = config["snapshot"]
    text = f'{event.name}: {sides[fixture.home]["name"]} v {sides[fixture.away]["name"]} is approaching its deadline.'
    for hour in config["notifications"]["reminders_hours"]:
        enqueue(db, room, config, f"fixture:{fixture.id}:deadline:{hour}", text, now, fixture=fixture, lead_hours=hour)


def reserve(db, now):
    # SQLite serializes writers; PostgreSQL locks each recipient's rate counter
    # with a transaction-scoped advisory lock shared across event rooms.
    ids = db.execute(select(StudioGroupNotice.id, StudioGroupNotice.chat_id)
                     .where(StudioGroupNotice.due_at <= now, StudioGroupNotice.sent_at.is_(None),
                            StudioGroupNotice.failed_at.is_(None),
                            or_(StudioGroupNotice.leased_at.is_(None), StudioGroupNotice.leased_at <= now - LEASE))
                     .order_by(StudioGroupNotice.id).limit(24)).all()
    for key, chat_id in ids:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:chat)"), {"chat": chat_id})
        elif db.get_bind().dialect.name == "sqlite":
            db.execute(update(StudioGroupNotice).where(StudioGroupNotice.id == key)
                       .values(attempts=StudioGroupNotice.attempts))
        row = db.get(StudioGroupNotice, key, populate_existing=True)
        if row.sent_at or row.failed_at or row.due_at > now or row.leased_at and row.leased_at > now - LEASE:
            continue
        room = db.get(StudioCompetition, row.competition_id)
        event = db.get(StudioEvent, room.event_id)
        if row.fixture_id:
            fixture = db.get(StudioFixture, row.fixture_id)
            if room.status != "running" or fixture.status not in ("scheduled", "in_progress") or fixture.deadline <= now:
                row.failed_at = now
                return None
            revised = fixture.deadline - timedelta(hours=row.lead_hours)
            row.expires_at = fixture.deadline
            if revised > now:
                row.due_at = revised
                return None
        if row.expires_at <= now or room.chat_id != row.chat_id or (event.status == "cancelled"
                and row.source != "lobby:cancelled") or row.attempts >= MAX_ATTEMPTS:
            row.failed_at = now
            return None
        if event.status == "paused":
            row.due_at = now + timedelta(minutes=5)
            return None
        for period, cap in ((timedelta(hours=1), HOURLY_LIMIT), (timedelta(days=1), DAILY_LIMIT)):
            count, earliest = db.execute(select(func.count(), func.min(StudioGroupNotice.leased_at))
                                         .where(StudioGroupNotice.chat_id == row.chat_id,
                                                StudioGroupNotice.leased_at > now - period)).one()
            if count >= cap:
                row.due_at = earliest + period
                return None
        row.leased_at = now
        row.attempts += 1
        return row.id, row.attempts, row.chat_id, row.text, event.id
    return None


def _reserve():
    with get_session() as db:
        item = reserve(db, datetime.utcnow())
        db.commit()
        return item


def _record(key, attempt, retry=None, *, failed=False, message_id=None):
    now = datetime.utcnow()
    with get_session() as db:
        values = {"failed_at": now} if failed else {"due_at": now + retry} if retry else {"sent_at": now, "telegram_message_id": message_id}
        db.execute(update(StudioGroupNotice).where(StudioGroupNotice.id == key,
                   StudioGroupNotice.attempts == attempt, StudioGroupNotice.sent_at.is_(None)).values(**values))
        db.commit()


async def send_due(bot):
    for _ in range(8):
        item = await asyncio.to_thread(_reserve)
        if not item:
            return
        key, attempt, chat, message, event_id = item
        link = miniapp_button("Open event", f"events_{event_id}", is_private=False)
        try:
            sent = await bot.send_message(chat, message, reply_markup=InlineKeyboardMarkup([[link]]) if link else None)
        except Forbidden:
            await asyncio.to_thread(_record, key, attempt, failed=True)
        except RetryAfter as exc:
            retry = exc.retry_after if isinstance(exc.retry_after, timedelta) else timedelta(seconds=exc.retry_after)
            await asyncio.to_thread(_record, key, attempt, retry)
        except Exception:
            logger.warning("Live Event group notice %s failed", key, exc_info=True)
            await asyncio.to_thread(_record, key, attempt, timedelta(minutes=2 ** attempt))
        else:
            await asyncio.to_thread(_record, key, attempt, message_id=sent.message_id)
