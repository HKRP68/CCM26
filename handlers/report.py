"""/report command — user sends feedback/bug report to admins.

Usage:
    /report I think the bowl-out probabilities feel off.

The text is stored in user_reports. Admins triage from /reports admin page
and can reply (which sends a message back through the bot).
"""

import logging
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserReport

logger = logging.getLogger(__name__)

# Anti-spam: max reports per user per day
MAX_REPORTS_PER_DAY = 5
# Min message length to filter joke "test" submissions
MIN_MESSAGE_LENGTH = 5


async def report_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg_text = " ".join(ctx.args).strip()
    if not msg_text:
        await update.message.reply_text(
            "📨 <b>/report</b> — send feedback or report a bug.\n\n"
            "Usage: <code>/report your message here</code>\n\n"
            "Examples:\n"
            "  • <code>/report bowl-out feels too random</code>\n"
            "  • <code>/report match got stuck at over 12</code>\n"
            "  • <code>/report can we get auto-buy filters?</code>\n\n"
            "Admins read every report.",
            parse_mode="HTML"
        )
        return

    if len(msg_text) < MIN_MESSAGE_LENGTH:
        await update.message.reply_text(
            "⚠️ Please write a more detailed message (at least 5 characters)."
        )
        return
    if len(msg_text) > 2000:
        msg_text = msg_text[:2000] + "…"

    tg_user = update.effective_user
    session = get_session()
    try:
        user = (session.query(User)
                .filter(User.telegram_id == tg_user.id).first())
        if not user:
            from services.message_service import get_msg
            await update.message.reply_text(get_msg("not_debuted"))
            return

        # Anti-spam: count reports in last 24h
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=1)
        recent_count = (session.query(UserReport)
                              .filter(UserReport.user_id == user.id,
                                      UserReport.created_at >= cutoff)
                              .count())
        if recent_count >= MAX_REPORTS_PER_DAY:
            await update.message.reply_text(
                f"⚠️ You've sent {recent_count} reports in the last 24 hours. "
                f"Limit is {MAX_REPORTS_PER_DAY}/day. Please wait before sending more."
            )
            return

        report = UserReport(
            user_id=user.id,
            message=msg_text,
            is_read=False,
            is_resolved=False,
        )
        session.add(report)
        session.commit()

        await update.message.reply_text(
            "✅ <b>Report received!</b>\n\n"
            "Thanks — your feedback has been logged. Admins will review it soon. "
            "If a reply is needed, you'll get a message back from the bot.",
            parse_mode="HTML"
        )
        logger.info(f"User {user.id} (@{user.username}) submitted report #{report.id}")
    except Exception:
        session.rollback()
        logger.exception("Report submit failed")
        await update.message.reply_text("⚠️ Couldn't save your report. Please try again.")
    finally:
        session.close()
