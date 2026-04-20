"""Handler for /cmuleaderboard — leaderboard with multiple views."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import func, desc

from database import get_session
from models import User, Player, UserRoster, Match, PlayerGameStats

logger = logging.getLogger(__name__)


def _build_keyboard(active):
    """Build leaderboard tab buttons."""
    tabs = [
        ("matches", "🎮 Matches"),
        ("wins", "🏆 Wins"),
        ("value", "💎 Value"),
        ("streak", "🔥 Streak"),
        ("gamer", "⚡ Active"),
        ("batsman", "🏏 Runs"),
    ]
    rows = []
    row = []
    for key, label in tabs:
        mark = "● " if key == active else ""
        row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"lb_{key}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _get_leaderboard_data(session, metric, viewer_id):
    """Return (top_10, viewer_rank, viewer_value) for the given metric."""
    if metric == "matches":
        q = session.query(User).filter(User.matches_played > 0).order_by(desc(User.matches_played), User.id)
        value_fn = lambda u: u.matches_played
        unit = "matches"
    elif metric == "wins":
        q = session.query(User).filter(User.matches_won > 0).order_by(desc(User.matches_won), User.id)
        value_fn = lambda u: u.matches_won
        unit = "wins"
    elif metric == "streak":
        q = session.query(User).filter(User.best_streak > 0).order_by(desc(User.best_streak), User.id)
        value_fn = lambda u: u.best_streak
        unit = "streak"
    elif metric == "gamer":
        q = session.query(User).filter(User.active_days > 0).order_by(desc(User.active_days), User.id)
        value_fn = lambda u: u.active_days
        unit = "days"
    elif metric == "value":
        # Team value = sum of top 11 player ratings × 1000 (rough valuation)
        value_subq = (session.query(
            UserRoster.user_id,
            func.sum(Player.rating).label("team_val")
        ).join(Player, UserRoster.player_id == Player.id)
          .group_by(UserRoster.user_id).subquery())
        q = (session.query(User, value_subq.c.team_val)
             .join(value_subq, User.id == value_subq.c.user_id)
             .order_by(desc(value_subq.c.team_val)))
        rows = q.limit(100).all()
        top_10 = [(u, v * 1000) for u, v in rows[:10]]
        viewer_rank = None; viewer_val = 0
        for i, (u, v) in enumerate(rows, 1):
            if u.id == viewer_id:
                viewer_rank = i; viewer_val = v * 1000; break
        return top_10, viewer_rank, viewer_val, "coins"
    elif metric == "batsman":
        # Top run-scorers: join PlayerGameStats.runs + owner + player
        rows = (session.query(PlayerGameStats, Player, User)
                .join(Player, PlayerGameStats.player_id == Player.id)
                .join(User, PlayerGameStats.user_id == User.id)
                .filter(PlayerGameStats.runs > 0)
                .order_by(desc(PlayerGameStats.runs))
                .limit(100).all())
        top_10 = rows[:10]
        viewer_rank = None; viewer_val = 0
        for i, (gs, p, u) in enumerate(rows, 1):
            if u.id == viewer_id:
                viewer_rank = i; viewer_val = gs.runs
                break
        return top_10, viewer_rank, viewer_val, "runs"

    # Default list-based (users)
    all_rows = q.limit(100).all()
    top_10 = [(u, value_fn(u)) for u in all_rows[:10]]
    viewer_rank = None; viewer_val = 0
    for i, u in enumerate(all_rows, 1):
        if u.id == viewer_id:
            viewer_rank = i; viewer_val = value_fn(u); break
    return top_10, viewer_rank, viewer_val, unit


def _format_leaderboard(metric, top_10, viewer_rank, viewer_val, unit, viewer):
    titles = {
        "matches": "🎮 MOST MATCHES",
        "wins": "🏆 MOST WINS",
        "value": "💎 TEAM VALUE",
        "streak": "🔥 LONGEST STREAK",
        "gamer": "⚡ ACTIVE DAYS",
        "batsman": "🏏 TOP BATSMEN (Runs)",
    }
    lines = [f"<b>{titles.get(metric, 'LEADERBOARD')}</b>\n"]

    if not top_10:
        lines.append("<i>No data yet.</i>")
    else:
        for i, row in enumerate(top_10, 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            if metric == "batsman":
                gs, player, user = row
                lines.append(f"{medal} {player.name} | @{user.username or user.first_name} | <b>{gs.runs}</b>")
            else:
                user, val = row
                val_str = f"{val:,}" if unit == "coins" else f"{val:,}"
                lines.append(f"{medal} @{user.username or user.first_name} | <b>{val_str}</b>")

    lines.append("\n━━━━━━━━━━━━━━━━━━━")
    if viewer_rank:
        val_str = f"{viewer_val:,}"
        lines.append(f"📍 <b>Your Rank:</b> #{viewer_rank} — {val_str} {unit}")
    else:
        lines.append(f"📍 <b>Your Rank:</b> Unranked")

    return "\n".join(lines)


async def leaderboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    session = get_session()
    try:
        viewer = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not viewer:
            await update.message.reply_text("❌ Do /debut first!")
            return
        top_10, rank, val, unit = _get_leaderboard_data(session, "matches", viewer.id)
        text = _format_leaderboard("matches", top_10, rank, val, unit, viewer)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=_build_keyboard("matches"))
    except Exception:
        logger.exception("Leaderboard err")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    metric = q.data.replace("lb_", "")
    tg_user = q.from_user

    session = get_session()
    try:
        viewer = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not viewer:
            await q.edit_message_text("❌ Do /debut first!")
            return
        top_10, rank, val, unit = _get_leaderboard_data(session, metric, viewer.id)
        text = _format_leaderboard(metric, top_10, rank, val, unit, viewer)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=_build_keyboard(metric))
    except Exception:
        logger.exception("Leaderboard cb err")
    finally:
        session.close()
