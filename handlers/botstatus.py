"""Handler for /botstatus — bot ping, uptime and live health information."""

import logging
import time
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import Match, User

logger = logging.getLogger(__name__)

# Captured the first time this module is imported (≈ bot process start). bot.py
# overwrites bot_data["bot_start_time"] at startup for a more precise value; we
# fall back to this if that key is missing.
_IMPORT_TIME = datetime.utcnow()

# Same set the rest of the codebase treats as "an unfinished match".
ACTIVE_MATCH_STATUSES = (
    "pending", "accepted", "toss", "selecting", "playing", "active",
)


def _fmt_uptime(seconds: float) -> str:
    """Format an elapsed-seconds count as a compact 'Xd Yh Zm Ws' string."""
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _gather_health():
    """Return (active_matches, total_users, db_ok). Best-effort — never raises."""
    active_matches = total_users = None
    db_ok = False
    session = None
    try:
        session = get_session()
        active_matches = (session.query(Match)
                          .filter(Match.status.in_(ACTIVE_MATCH_STATUSES))
                          .count())
        total_users = session.query(User).count()
        db_ok = True
    except Exception:
        logger.exception("botstatus health query failed")
    finally:
        if session is not None:
            session.close()
    return active_matches, total_users, db_ok


async def botstatus_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot ping (Telegram API round-trip), uptime and live health."""
    if update.message is None:
        return

    # Measure the Telegram API round-trip by timing the first send.
    t0 = time.perf_counter()
    msg = await update.message.reply_text("🏓 <b>Pinging…</b>", parse_mode="HTML")
    ping_ms = (time.perf_counter() - t0) * 1000.0

    start_time = context.bot_data.get("bot_start_time") or _IMPORT_TIME
    try:
        uptime = (datetime.utcnow() - start_time).total_seconds()
    except Exception:
        uptime = 0.0

    active_matches, total_users, db_ok = _gather_health()

    db_line = "🟢 Connected" if db_ok else "🔴 Unreachable"
    am_line = str(active_matches) if active_matches is not None else "—"
    users_line = f"{total_users:,}" if total_users is not None else "—"

    text = (
        "🤖 <b>BOT STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🏓 <b>Ping:</b> {ping_ms:.0f} ms\n"
        f"⏱ <b>Uptime:</b> {_fmt_uptime(uptime)}\n"
        f"🗄 <b>Database:</b> {db_line}\n"
        f"🏏 <b>Active matches:</b> {am_line}\n"
        f"👥 <b>Registered users:</b> {users_line}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✅ <i>Bot is online and responding.</i>"
    )

    try:
        await msg.edit_text(text, parse_mode="HTML")
    except Exception:
        # Editing failed for some reason — send a fresh message instead.
        await update.message.reply_text(text, parse_mode="HTML")
