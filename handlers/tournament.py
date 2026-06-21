"""Bot commands for Challenge League Tournament stats.

- ``/statstour <player name>`` — a player's stats in the active tournament.
- ``/tournamentstats`` — Top-10 stat leaderboards with category buttons (only the
  user who opened the command can switch categories).
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import func
import html

from database import get_session
from models import TournamentPlayerStats
from services import tournament_service

logger = logging.getLogger(__name__)

NO_ACTIVE = "❌ No Challenge League Tournament is currently active."

# (callback key, button label). "runs" is the default view.
_CATEGORIES = [
    ("runs", "🏏 Most Runs"),
    ("wkts", "🎯 Most Wickets"),
    ("sixes", "6️⃣ Most Sixes"),
    ("fours", "4️⃣ Most Fours"),
    ("hs", "⭐ Highest Score"),
    ("fig", "💥 Best Figure"),
    ("sr", "⚡ Best Strike Rate"),
    ("econ", "🛡️ Best Economy"),
]
_CAT_LABELS = dict(_CATEGORIES)


def _sr(r):
    return ((r.bat_runs or 0) / r.bat_balls * 100.0) if r.bat_balls else 0.0


def _econ(r):
    overs = (r.bowl_balls or 0) / 6.0
    return ((r.bowl_runs or 0) / overs) if overs else 0.0


def _leaders_for(session, tour, category):
    """Return a Top-10 list of (name, team, value_str) for the given category."""
    leaders = tournament_service.stat_leaders(session, tour.id, limit=10)
    out = []
    if category == "runs":
        out = [(r.name, r.team_name, str(r.bat_runs)) for r in leaders["most_runs"]]
    elif category == "wkts":
        out = [(r.name, r.team_name, str(r.bowl_wickets)) for r in leaders["most_wickets"]]
    elif category == "sixes":
        out = [(r.name, r.team_name, str(r.bat_sixes)) for r in leaders["most_sixes"]]
    elif category == "fours":
        out = [(r.name, r.team_name, str(r.bat_fours)) for r in leaders["most_fours"]]
    elif category == "hs":
        out = [(r.name, r.team_name, str(r.highest_score)) for r in leaders["highest_score"]]
    elif category == "fig":
        out = [(r.name, r.team_name, f"{r.best_bowl_wickets}/{r.best_bowl_runs}")
               for r in leaders["best_figure"]]
    elif category == "sr":
        out = [(r.name, r.team_name, f"{v:.1f}") for r, v in leaders["best_strike_rate"]]
    elif category == "econ":
        out = [(r.name, r.team_name, f"{v:.2f}") for r, v in leaders["best_economy"]]
    return out


def _render(tour, category, rows):
    label = _CAT_LABELS.get(category, "Stats")
    lines = [f"🏆 <b>{html.escape(tour.name)}</b> — Tournament Stats",
             f"<b>{label}</b> · Top 10", ""]
    if not rows:
        lines.append("<i>No qualifying players yet.</i>")
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, (name, team, val) in enumerate(rows, 1):
            rank = medals.get(i, f"{i}.")
            team_s = f" · {html.escape(team)}" if team else ""
            lines.append(f"{rank} {html.escape(name or 'Player')}{team_s} — <b>{val}</b>")
    return "\n".join(lines)


def _keyboard(active, opener_tg):
    rows, row = [], []
    for key, label in _CATEGORIES:
        mark = "● " if key == active else ""
        row.append(InlineKeyboardButton(f"{mark}{label}",
                                        callback_data=f"tstat_{key}_{opener_tg}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def tournamentstats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tournamentstats — Top-10 tournament leaderboards with category buttons."""
    session = get_session()
    try:
        tour = tournament_service.get_active_tournament(session)
        if not tour:
            await update.message.reply_text(NO_ACTIVE)
            return
        opener = update.effective_user.id
        rows = _leaders_for(session, tour, "runs")
        await update.message.reply_text(
            _render(tour, "runs", rows), parse_mode="HTML",
            reply_markup=_keyboard("runs", opener))
    except Exception:
        logger.exception("/tournamentstats failed")
        await update.message.reply_text("⚠️ Could not load tournament stats.")
    finally:
        session.close()


async def tournamentstats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch the /tournamentstats view — restricted to the user who opened it."""
    q = update.callback_query
    try:
        _, cat, opener = q.data.split("_", 2)
        opener = int(opener)
    except Exception:
        await q.answer("Invalid selection.", show_alert=True)
        return
    if q.from_user.id != opener:
        await q.answer("Only the person who opened this can use these buttons.",
                       show_alert=True)
        return
    if cat not in _CAT_LABELS:
        await q.answer("Unknown category.", show_alert=True)
        return
    await q.answer()
    session = get_session()
    try:
        tour = tournament_service.get_active_tournament(session)
        if not tour:
            await q.edit_message_text(NO_ACTIVE)
            return
        rows = _leaders_for(session, tour, cat)
        await q.edit_message_text(
            _render(tour, cat, rows), parse_mode="HTML",
            reply_markup=_keyboard(cat, opener))
    except Exception:
        logger.exception("/tournamentstats callback failed")
    finally:
        session.close()


async def statstour_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/statstour <player name> — a player's stats in the active tournament."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /statstour <player name>\nExample: /statstour Virat Kohli")
        return
    search = " ".join(context.args).strip()
    session = get_session()
    try:
        tour = tournament_service.get_active_tournament(session)
        if not tour:
            await update.message.reply_text(NO_ACTIVE)
            return
        rows = (session.query(TournamentPlayerStats)
                .filter(TournamentPlayerStats.tournament_id == tour.id,
                        func.lower(TournamentPlayerStats.name).like(f"%{search.lower()}%"))
                .all())
        if not rows:
            await update.message.reply_text(
                f"❌ No tournament stats found for “{search}” in {tour.name}.")
            return
        # Prefer an exact name match; a player may appear for more than one team.
        exact = [r for r in rows if (r.name or "").strip().lower() == search.lower()]
        rows = exact or rows
        rows = sorted(rows, key=lambda r: (r.bat_runs or 0), reverse=True)[:3]

        blocks = [f"🏆 <b>{html.escape(tour.name)}</b> — Player Tournament Stats"]
        for r in rows:
            fig = (f"{r.best_bowl_wickets}/{r.best_bowl_runs}"
                   if r.best_bowl_wickets is not None and r.best_bowl_runs is not None
                   and r.best_bowl_runs >= 0 else "—")
            team = f" · {html.escape(r.team_name)}" if r.team_name else ""
            blocks.append(
                f"\n👤 <b>{html.escape(r.name or 'Player')}</b>{team}\n"
                f"🎮 Matches: {r.matches}\n"
                f"🏏 Runs: {r.bat_runs} ({r.bat_balls}b) · SR {_sr(r):.1f}\n"
                f"   4s: {r.bat_fours} · 6s: {r.bat_sixes} · HS: {r.highest_score}\n"
                f"🎯 Wickets: {r.bowl_wickets} · Runs: {r.bowl_runs} · Econ {_econ(r):.2f}\n"
                f"   Best: {fig}")
        await update.message.reply_text("\n".join(blocks), parse_mode="HTML")
    except Exception:
        logger.exception("/statstour failed")
        await update.message.reply_text("⚠️ Could not load player tournament stats.")
    finally:
        session.close()
