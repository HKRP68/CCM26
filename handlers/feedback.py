"""/feedback <message> — Submit feedback or a bug report.
Stored in DB and forwarded to the admin (ADMIN_CHAT_ID env var).
Also POSTs to FEEDBACK_WEBHOOK_URL if set.
"""
import logging
import os

import asyncio
import json
import urllib.request
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User

logger = logging.getLogger(__name__)

ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
FEEDBACK_WEBHOOK = os.getenv("FEEDBACK_WEBHOOK_URL", "")
MIN_LEN = 10
MAX_LEN = 1000


async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    text = " ".join(context.args) if context.args else ""
    if not text:
        await msg.reply_text(
            "💬 <b>Feedback / Bug Report</b>\n\n"
            "Usage: <code>/feedback Your message here</code>\n\n"
            "We read every submission!",
            parse_mode="HTML",
        )
        return

    if len(text) < MIN_LEN:
        await msg.reply_text(f"❌ Feedback too short (min {MIN_LEN} chars).")
        return
    if len(text) > MAX_LEN:
        await msg.reply_text(f"❌ Feedback too long (max {MAX_LEN} chars).")
        return

    chat = update.effective_chat
    chat_info = f"Group: {chat.title} ({chat.id})" if chat and chat.type != "private" else "Private"
    user_label = f"@{user.username}" if user.username else f"{user.first_name} [{user.id}]"

    # Store in activity log (lightweight, no separate table)
    session = get_session()
    try:
        u = session.query(User).filter(User.telegram_id == user.id).first()
        if u:
            from services.activity_service import log_activity
            log_activity(session, u.id, "feedback", text[:200])
            session.commit()
    except Exception:
        pass
    finally:
        session.close()

    # Forward to admin Telegram chat
    admin_msg = (
        f"💬 <b>New Feedback</b>\n\n"
        f"From: {user_label}\n"
        f"Chat: {chat_info}\n\n"
        f"<i>{text}</i>"
    )
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(int(ADMIN_CHAT_ID), admin_msg, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"feedback: failed to forward to admin: {e}")

    # POST to webhook (non-blocking, runs in thread pool)
    if FEEDBACK_WEBHOOK:
        payload = {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "chat": chat_info,
            "text": text,
        }
        def _post_webhook():
            try:
                data = json.dumps(payload).encode()
                req = urllib.request.Request(
                    FEEDBACK_WEBHOOK, data=data,
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                logger.warning(f"feedback: webhook POST failed: {e}")
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _post_webhook)

    await msg.reply_text(
        "✅ <b>Feedback received!</b> Thanks — we read every message.",
        parse_mode="HTML",
    )
