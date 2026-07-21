"""Giveaway scheduler — a restart-safe periodic sweeper.

Because the host redeploys frequently, in-process one-shot jobs (run_once) don't
survive restarts. Instead all giveaway state lives in the DB and this sweeper —
registered like ``services.match_heartbeat.start_heartbeat`` — reconciles it on
every tick:

  * ``scheduled`` giveaways whose ``start_time`` has passed → announced to every
    active chat the bot is in, then flipped to ``running``.
  * ``running`` giveaways whose ``end_time`` has passed → winners drawn (with a
    live Official-GC membership re-check), prizes granted, results posted in the
    Official GC + DMed to winners, then flipped to ``ended``.
"""

import asyncio
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError

logger = logging.getLogger(__name__)

SWEEP_JOB_NAME = "giveaway_sweep"
SWEEP_INTERVAL = 30  # seconds
GROUP_CHAT_TYPES = ("group", "supergroup")
# Small gap between broadcast sends to stay under Telegram's flood limits.
SEND_DELAY_SECONDS = 0.05

# Telegram member statuses that count as "in the group".
_MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


async def _giveaway_tick(context):
    """One sweep: start due giveaways, end expired ones."""
    try:
        await _start_due(context)
    except Exception:
        logger.exception("giveaway _start_due failed")
    try:
        await _end_due(context)
    except Exception:
        logger.exception("giveaway _end_due failed")


# ── Start (announce) ─────────────────────────────────────────────────

async def _start_due(context):
    from database import get_session
    from models import Giveaway

    session = get_session()
    try:
        now = datetime.utcnow()
        due = (session.query(Giveaway)
               .filter(Giveaway.status == "scheduled",
                       Giveaway.start_time <= now)
               .all())
        for g in due:
            # End time already passed while still scheduled → skip announcing,
            # let _end_due void it.
            if g.end_time <= now:
                continue
            # Isolate failures so one broken giveaway can't block the others.
            try:
                await _announce_start(context, session, g)
            except Exception:
                session.rollback()
                logger.exception("Giveaway #%s announce failed", g.id)
    finally:
        session.close()


async def _announce_start(context, session, giveaway):
    from models import BotChat, GameConfig
    from services.giveaway_service import (
        announcement_text, group_join_url, group_handle)

    q = session.query(BotChat).filter(BotChat.is_active == True)  # noqa: E712
    if (giveaway.announce_target or "groups") == "groups":
        q = q.filter(BotChat.chat_type.in_(GROUP_CHAT_TYPES))
    chat_ids = [c.chat_id for c in q.all()]

    text = announcement_text(giveaway)
    rows = [[InlineKeyboardButton("🎉 Participate",
                                  callback_data=f"gwjoin_{giveaway.id}")]]
    # A one-tap join button so non-members can join the Official Group right from
    # the announcement (they still tap Participate afterwards to enter).
    cfg = session.query(GameConfig).first()
    join_url = group_join_url(cfg)
    if join_url:
        handle = group_handle(cfg)
        rows.append([InlineKeyboardButton(
            f"🔗 Join {handle}" if handle else "🔗 Join Official Group",
            url=join_url)])
    markup = InlineKeyboardMarkup(rows)

    # Flip to running and commit BEFORE the (potentially slow) fan-out so users
    # in the first chats who tap immediately aren't rejected as "not open", and
    # a concurrent tick won't re-announce.
    giveaway.announced_at = datetime.utcnow()
    giveaway.status = "running"
    session.commit()

    sent = 0
    for cid in chat_ids:
        try:
            msg = await _send_announcement(context, cid, giveaway, text, markup)
            await _pin_announcement(context, cid, msg)
            sent += 1
        except Forbidden:
            await _mark_chat_inactive(cid)
        except BadRequest as exc:
            logger.info("Giveaway announce to %s rejected: %s", cid, exc)
        except RetryAfter as exc:
            # Flood control — wait the requested interval and retry this chat once
            # so its audience still sees the announcement.
            await asyncio.sleep(getattr(exc, "retry_after", 1) or 1)
            try:
                msg = await _send_announcement(context, cid, giveaway, text, markup)
                await _pin_announcement(context, cid, msg)
                sent += 1
            except TelegramError as exc2:
                logger.warning("Giveaway announce retry to %s failed: %s", cid, exc2)
        except TelegramError as exc:
            logger.warning("Giveaway announce to %s failed: %s", cid, exc)

        if SEND_DELAY_SECONDS:
            await asyncio.sleep(SEND_DELAY_SECONDS)

    logger.info("Giveaway #%s announced to %s/%s chats",
                giveaway.id, sent, len(chat_ids))


async def _send_announcement(context, chat_id, giveaway, text, markup):
    if giveaway.image_file_id:
        return await context.bot.send_photo(
            chat_id=chat_id, photo=giveaway.image_file_id,
            caption=text, parse_mode="HTML", reply_markup=markup)
    return await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode="HTML",
        reply_markup=markup, disable_web_page_preview=True)


async def _pin_announcement(context, chat_id, message):
    """Pin the giveaway announcement so it stays visible. Best-effort: a missing
    pin permission (bot isn't admin) must never fail the broadcast itself."""
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return
    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True)
    except TelegramError as exc:
        logger.info("Could not pin giveaway announcement in %s: %s", chat_id, exc)


# ── End (draw + results) ─────────────────────────────────────────────

async def _end_due(context):
    from database import get_session
    from models import Giveaway

    session = get_session()
    try:
        now = datetime.utcnow()
        due = (session.query(Giveaway)
               .filter(Giveaway.status == "running",
                       Giveaway.end_time <= now)
               .all())
        # Also void giveaways that were never announced but are already past end.
        stale = (session.query(Giveaway)
                 .filter(Giveaway.status == "scheduled",
                         Giveaway.end_time <= now)
                 .all())
        for g in due:
            # Isolate failures so one broken giveaway can't block the rest — or
            # the stale void-loop below.
            try:
                await _finalize(context, session, g)
            except Exception:
                session.rollback()
                logger.exception("Giveaway #%s finalize failed", g.id)
        for g in stale:
            try:
                g.status = "ended"
                g.winners_drawn_at = datetime.utcnow()
                session.commit()
            except Exception:
                session.rollback()
                logger.exception("Giveaway #%s void failed", g.id)
    finally:
        session.close()


async def _finalize(context, session, giveaway):
    from models import GiveawayEntry
    from services.giveaway_service import draw_winners, winners_text

    official_group_id = _official_group_id(session)

    # Re-verify live Official GC membership for each entrant (anti-cheat: dropping
    # anyone who left the GC after entering). Fail closed: if the Official GC is
    # unconfigured we can't verify anyone, so no one is eligible (empty set →
    # giveaway ends void) rather than paying out an unverifiable audience.
    eligible_user_ids = set()
    if official_group_id:
        entries = (session.query(GiveawayEntry)
                   .filter(GiveawayEntry.giveaway_id == giveaway.id).all())
        for e in entries:
            if await _is_group_member(context, official_group_id, e.telegram_id):
                eligible_user_ids.add(e.user_id)
    else:
        logger.warning("Giveaway #%s finalized with no Official GC configured — "
                       "voiding (no verifiable participants)", giveaway.id)

    winners = draw_winners(session, giveaway, eligible_user_ids=eligible_user_ids)
    session.commit()

    # Post results in the Official GC (fallback: skip if unconfigured).
    text = winners_text(giveaway, winners)
    if official_group_id:
        try:
            await context.bot.send_message(
                chat_id=official_group_id, text=text, parse_mode="HTML",
                disable_web_page_preview=True)
        except TelegramError as exc:
            logger.warning("Giveaway #%s results post to GC failed: %s",
                          giveaway.id, exc)

    # DM each winner (best-effort).
    for w in winners:
        try:
            await context.bot.send_message(
                chat_id=w["telegram_id"],
                text=(f"🎉 You won the giveaway <b>{_esc(giveaway.title)}</b>!\n\n"
                      f"🎁 Prize: <b>{_esc(w['prize_detail'])}</b>\n"
                      "It has been credited to your account."),
                parse_mode="HTML")
        except (Forbidden, BadRequest):
            pass
        except TelegramError as exc:
            logger.info("Giveaway winner DM to %s failed: %s",
                        w["telegram_id"], exc)

    logger.info("Giveaway #%s finalized with %s winner(s)",
                giveaway.id, len(winners))


# ── Helpers ──────────────────────────────────────────────────────────

def _official_group_id(session):
    from models import GameConfig
    try:
        cfg = session.query(GameConfig).first()
        return cfg.official_group_id if cfg else None
    except Exception:
        logger.exception("official group config read failed")
        return None


async def _is_group_member(context, group_id, user_id) -> bool:
    try:
        member = await context.bot.get_chat_member(group_id, user_id)
    except (BadRequest, Forbidden):
        return False
    except TelegramError:
        # Transient Telegram error — be lenient rather than disqualify unfairly.
        return True
    status = getattr(member, "status", None)
    if status not in _MEMBER_STATUSES:
        return False
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return True


async def _mark_chat_inactive(chat_id):
    from database import get_session
    from models import BotChat
    session = get_session()
    try:
        row = session.query(BotChat).filter(BotChat.chat_id == chat_id).first()
        if row:
            row.is_active = False
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _esc(text) -> str:
    return (str(text or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ── Registration ─────────────────────────────────────────────────────

def start_giveaway_scheduler(application):
    """Register the giveaway sweep job. Idempotent — adds at most once."""
    if not application.job_queue:
        logger.warning("JobQueue unavailable — giveaway scheduler not started")
        return
    try:
        if application.job_queue.get_jobs_by_name(SWEEP_JOB_NAME):
            return
        application.job_queue.run_repeating(
            _giveaway_tick,
            interval=SWEEP_INTERVAL,
            first=SWEEP_INTERVAL,
            name=SWEEP_JOB_NAME,
        )
        logger.info("Giveaway scheduler started (every %ss)", SWEEP_INTERVAL)
    except Exception:
        logger.exception("Failed to start giveaway scheduler")
