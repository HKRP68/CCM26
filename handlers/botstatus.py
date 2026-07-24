"""Handler for /botstatus — bot ping, uptime and live health information."""

import asyncio
import logging
import os
import time
from datetime import datetime

from sqlalchemy import text
from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import Match, User
from services import loop_monitor

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


# The health counts are two full-table aggregates. They change slowly, so
# cache them briefly rather than re-running them for every /ping.
_HEALTH_TTL_SECONDS = float(os.getenv("BOTSTATUS_HEALTH_TTL", "30"))
_health_cache = {"at": None, "value": None}


def _gather_health():
    """Return (active_matches, total_users, db_ok, db_ms).

    Best-effort — never raises. Runs on a worker thread (see the handler), so
    the counts and the timing probe never block the event loop.
    """
    cached = _health_cache["value"]
    if cached is not None and _health_cache["at"] is not None:
        if time.monotonic() - _health_cache["at"] < _HEALTH_TTL_SECONDS:
            return cached

    active_matches = total_users = None
    db_ms = None
    db_ok = False
    session = None
    try:
        session = get_session()
        # Time a trivial query on its own: this is the app→database round trip,
        # the unit of cost every handler pays per query.
        probe = time.perf_counter()
        session.execute(text("SELECT 1")).scalar()
        db_ms = (time.perf_counter() - probe) * 1000.0

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

    result = (active_matches, total_users, db_ok, db_ms)
    if db_ok:
        _health_cache["value"] = result
        _health_cache["at"] = time.monotonic()
    return result


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

    # Health counts are two full-table aggregates plus a timing probe. Run them
    # on a worker thread: doing that DB work inline would freeze the event loop
    # and inflate every other user's latency — the very thing being measured.
    active_matches, total_users, db_ok, db_ms = await asyncio.to_thread(_gather_health)

    lag = loop_monitor.get_stats()

    db_line = "🟢 Connected" if db_ok else "🔴 Unreachable"
    if db_ms is not None:
        db_line += f" ({db_ms:.0f} ms)"
    am_line = str(active_matches) if active_matches is not None else "—"
    users_line = f"{total_users:,}" if total_users is not None else "—"
    lag_line = (f"{lag['p95_ms']:.0f} ms p95 · {loop_monitor.describe()}"
                if lag["samples"] else "—")

    body = (
        "🤖 <b>BOT STATUS</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🏓 <b>Ping:</b> {ping_ms:.0f} ms <i>(Telegram round-trip)</i>\n"
        f"🔄 <b>Event loop:</b> {lag_line}\n"
        f"🗄 <b>Database:</b> {db_line}\n"
        f"⏱ <b>Uptime:</b> {_fmt_uptime(uptime)}\n"
        f"🏏 <b>Active matches:</b> {am_line}\n"
        f"👥 <b>Registered users:</b> {users_line}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "✅ <i>Bot is online and responding.</i>"
    )

    try:
        await msg.edit_text(body, parse_mode="HTML")
    except Exception:
        # Editing failed for some reason — send a fresh message instead.
        await update.message.reply_text(body, parse_mode="HTML")
