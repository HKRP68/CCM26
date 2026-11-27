"""Opt-in fixture reminders. Claims and delivery receipts are durable."""

import asyncio
from datetime import datetime, timedelta
import logging

from sqlalchemy import or_, select
from telegram import InlineKeyboardMarkup
from telegram.error import Forbidden, TelegramError
from zoneinfo import ZoneInfo

from database import get_session
from models import User
from models_events import StudioEnrollment, StudioEvent, StudioGame, StudioNotice
from services import event_studio as studio
from services.cooldown_notifier import _in_quiet_hours
from services.miniapp_buttons import miniapp_button

logger = logging.getLogger(__name__)


def enqueue(db, game, config, now):
    settings = config["notifications"]
    if not settings["enabled"]:
        return
    existing = {row.offset_hours: row for row in db.scalars(
        select(StudioNotice).filter_by(game_id=game.id, kind="deadline").with_for_update())}
    for hours in settings["reminders_hours"]:
        reminder = existing.get(hours)
        if reminder and (reminder.completed_at or reminder.lease_until and reminder.lease_until > now):
            continue
        due = game.deadline - timedelta(hours=hours)
        live = game.opens_at <= due and due >= now
        if reminder:
            if live:
                reminder.due_at = due
                reminder.lease_until = None
            else:
                reminder.completed_at = now
                reminder.outcome = "obsolete"
        elif live:
            db.add(StudioNotice(game_id=game.id, kind="deadline", offset_hours=hours, due_at=due))


def _ready_ids(now):
    with get_session() as db:
        return db.scalars(select(StudioNotice.id).where(
            StudioNotice.completed_at.is_(None), StudioNotice.due_at <= now,
            or_(StudioNotice.lease_until.is_(None), StudioNotice.lease_until <= now),
        ).order_by(StudioNotice.due_at, StudioNotice.id).limit(12)).all()


def _reserve(db, notice_id, now):
    row = studio.lock(db, StudioNotice, notice_id, skip_locked=True)
    if not row or row.completed_at or row.due_at > now or row.lease_until and row.lease_until > now:
        return None
    game = db.get(StudioGame, row.game_id)
    entry = db.get(StudioEnrollment, game.enrollment_id)
    event = db.get(StudioEvent, entry.event_id)
    user = db.get(User, entry.user_id)
    if event.status == "paused":
        row.due_at = now + timedelta(minutes=2)
        return None
    if (not user or not user.telegram_id or user.is_banned or user.dm_blocked
            or not user.notifications_enabled or game.status != "scheduled"
            or now >= game.deadline):
        row.completed_at = now
        row.outcome = "obsolete"
        return None
    cfg = studio.revision(db, event, published=True).config
    name = cfg["snapshot"][game.away if game.home == entry.team_key else game.home]["name"]
    local = game.deadline.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(cfg["timezone"]))
    row.lease_until = now + timedelta(minutes=2)
    row.attempts += 1
    return {
        "notice_id": row.id, "attempt": row.attempts, "user_id": user.id,
        "telegram_id": user.telegram_id, "event_id": event.id,
        "text": f"{event.name}\nYour fixture against {name} must start by {local:%d %b, %H:%M %Z}.",
    }


def reserve(notice_id, now):
    with get_session() as db:
        result = _reserve(db, notice_id, now)
        db.commit()
        return result


def settle(delivery, outcome, now):
    with get_session() as db:
        row = studio.lock(db, StudioNotice, delivery["notice_id"])
        if not row or row.completed_at or row.attempts != delivery["attempt"]:
            return
        if outcome == "retry" and row.attempts < 3:
            row.due_at = now + timedelta(minutes=2**row.attempts)
        else:
            row.completed_at = now
            row.outcome = "failed" if outcome == "retry" else outcome
        if outcome == "blocked":
            user = db.get(User, delivery["user_id"])
            if user:
                user.dm_blocked = True
        row.lease_until = None
        db.commit()


async def send_due(bot):
    if _in_quiet_hours():
        return
    ids = await asyncio.to_thread(_ready_ids, datetime.utcnow())
    for notice_id in ids:
        delivery = await asyncio.to_thread(reserve, notice_id, datetime.utcnow())
        if delivery is None:
            continue
        button = miniapp_button("Open event", f'events_{delivery["event_id"]}')
        keyboard = InlineKeyboardMarkup([[button]]) if button else None
        try:
            await bot.send_message(delivery["telegram_id"], delivery["text"], reply_markup=keyboard)
            outcome = "sent"
        except Forbidden:
            outcome = "blocked"
        except TelegramError:
            outcome = "retry"
            logger.warning("Events Studio reminder %s will retry", notice_id)
        await asyncio.to_thread(settle, delivery, outcome, datetime.utcnow())
