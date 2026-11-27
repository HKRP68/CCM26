"""/odirecords — the ODI leaderboards.

The short formats have ``/leaderboard``; this is its long-format twin, read
from ``odi_player_innings`` (every innings of 21 overs or more). Seven boards
share one message and a row of buttons, so a reader can move between most runs,
most wickets, the best individual innings and spell, and the rate metrics,
without the bot posting seven messages.

Rate boards carry a qualifying bar (``services.odi_stats_service``'s
``MIN_BATTING_INNINGS`` / ``MIN_BOWLING_INNINGS``) so one good night does not
outrank a career.
"""

from __future__ import annotations

import logging
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import Player, User
from services import odi_stats_service as odi_stats
from services.flags import get_flag

logger = logging.getLogger(__name__)

COMMAND = "odirecords"
#: How many careers each board shows.
TOP_N = 10
#: Buttons per row.
_ROW = 2


def _display_name(user):
    if user is None:
        return "—"
    if getattr(user, "username", None):
        return f"@{user.username}"
    return getattr(user, "first_name", None) or "Player"


def _resolve(session, rows):
    """Attach the Player and User rows a board needs, in two queries."""
    player_ids = {r["player_id"] for r in rows}
    user_ids = {r["user_id"] for r in rows}
    players = {p.id: p for p in session.query(Player)
               .filter(Player.id.in_(player_ids)).all()} if player_ids else {}
    users = {u.id: u for u in session.query(User)
             .filter(User.id.in_(user_ids)).all()} if user_ids else {}
    return players, users


def board_text(session, metric):
    """The whole message body for one board."""
    rows = odi_stats.leaders(session, metric, TOP_N)
    title = odi_stats.metric_title(metric)
    lines = [f"🥇 <b>ODI RECORDS</b> — {title}",
             "<i>Every innings of 21 overs or more.</i>", ""]
    if not rows:
        lines.append("<i>No qualifying ODI innings yet. Start one with "
                     "<code>/lp 50 @username</code>.</i>")
        return "\n".join(lines)
    players, users = _resolve(session, rows)
    for rank, row in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"{rank}.")
        player = players.get(row["player_id"])
        name = escape(getattr(player, "name", None) or "Unknown")
        flag = get_flag(getattr(player, "country", None)) if player else ""
        owner = escape(_display_name(users.get(row["user_id"])))
        lines.append(f"{medal} {name} {flag} | {owner} | <b>{escape(row['text'])}</b>")
    if metric in ("batting_average", "strike_rate"):
        lines += ["", f"<i>Qualifying: {odi_stats.MIN_BATTING_INNINGS}+ innings.</i>"]
    elif metric == "economy":
        lines += ["", f"<i>Qualifying: {odi_stats.MIN_BOWLING_INNINGS}+ innings.</i>"]
    return "\n".join(lines)


def board_keyboard(metric):
    """The metric switch. The board being shown is marked rather than linked."""
    buttons = []
    for key in odi_stats.METRIC_ORDER:
        label = odi_stats.metric_title(key)
        if key == metric:
            label = f"• {label}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"odirec_{key}"))
    rows = [buttons[i:i + _ROW] for i in range(0, len(buttons), _ROW)]
    return InlineKeyboardMarkup(rows)


async def odirecords_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message is None:
        return
    args = [str(a).lower() for a in (getattr(context, "args", None) or [])]
    metric = next((a for a in args if a in odi_stats.METRICS), "runs")
    session = get_session()
    try:
        from services.command_config_service import (
            get_disabled_message, is_command_enabled)
        if not is_command_enabled(session, COMMAND):
            await message.reply_text(get_disabled_message(session, COMMAND),
                                     parse_mode="HTML")
            return
        await message.reply_text(board_text(session, metric), parse_mode="HTML",
                                 reply_markup=board_keyboard(metric))
    except Exception:
        logger.exception("/odirecords failed")
        await message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def odirecords_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """``odirec_<metric>`` — switch board in place."""
    query = update.callback_query
    metric = (query.data or "").split("_", 1)[-1]
    if metric not in odi_stats.METRICS:
        await query.answer("Unknown board.", show_alert=True)
        return
    session = get_session()
    try:
        text = board_text(session, metric)
    except Exception:
        logger.exception("/odirecords board switch failed")
        await query.answer("Could not load that board.", show_alert=True)
        return
    finally:
        session.close()
    await query.answer(odi_stats.metric_title(metric))
    try:
        await query.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=board_keyboard(metric))
    except Exception:
        # An unchanged message, a deleted one, a network blip — the answer
        # above already told the tapper which board they asked for.
        logger.debug("odirecords edit failed", exc_info=True)
