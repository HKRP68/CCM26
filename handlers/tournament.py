"""Tournament handlers — /newtournament, /jointournament, /pointstable,
/tournamentstats, /addtotournament.

The primary attachment path is the `Tournament` keyword on /cipl, /c<league>,
and /letsplay (wired in handlers/challenge.py and handlers/letsplay.py) —
matches started that way auto-attach with no further command needed.
/addtotournament here is the manual fallback for everything else.

Callback patterns:
  atp_<tournament_id>_<match_id>_<owner_tg> — /addtotournament: pick a tournament
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Tournament
from services.telegram_user_service import sync_telegram_user
from services.tournament_service import (
    create_tournament, join_tournament, get_open_tournaments,
    get_points_table, get_tournament_leaderboard, get_user_recent_match_id,
    attach_match,
)

logger = logging.getLogger(__name__)


def _team_label(user):
    if user.team_name:
        return user.team_name
    return f"@{user.username}'s XI" if user.username else (user.first_name or "Team")


async def newtournament_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type == "private":
        await msg.reply_text("❌ Tournaments can only be created in group chats.")
        return
    name = " ".join(context.args or []).strip()
    if not name:
        await msg.reply_text("Usage: <code>/newtournament &lt;name&gt;</code>", parse_mode="HTML")
        return

    session = get_session()
    try:
        creator = sync_telegram_user(session, update.effective_user)
        if not creator:
            await msg.reply_text("❌ Do /debut first!")
            return
        tournament, err = create_tournament(session, name, creator.id, chat_id=chat.id)
        if err:
            session.rollback()
            await msg.reply_text(f"❌ {err}")
            return
        session.commit()
        await msg.reply_text(
            f"🏆 <b>{tournament.name}</b> created!\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"Tournament ID: <code>{tournament.id}</code>\n\n"
            f"Start matches with the <code>Tournament</code> keyword to auto-attach "
            f"them, e.g. <code>/cipl Tournament</code> (reply), "
            f"<code>/letsplay Tournament</code>.\n"
            f"Use <code>/pointstable {tournament.id}</code> and "
            f"<code>/tournamentstats {tournament.id}</code> to check standings.",
            parse_mode="HTML")
    except Exception:
        session.rollback()
        logger.exception("newtournament_handler failed")
        await msg.reply_text("❌ Something went wrong creating the tournament.")
    finally:
        session.close()


async def jointournament_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    args = context.args or []
    if not args or not args[0].isdigit():
        await msg.reply_text("Usage: <code>/jointournament &lt;id&gt;</code>", parse_mode="HTML")
        return
    tournament_id = int(args[0])

    session = get_session()
    try:
        user = sync_telegram_user(session, update.effective_user)
        if not user:
            await msg.reply_text("❌ Do /debut first!")
            return
        team, err = join_tournament(session, tournament_id, user.id)
        if err:
            session.rollback()
            await msg.reply_text(f"❌ {err}")
            return
        session.commit()
        await msg.reply_text(f"✅ Joined tournament #{tournament_id}.")
    except Exception:
        session.rollback()
        logger.exception("jointournament_handler failed")
        await msg.reply_text("❌ Something went wrong joining the tournament.")
    finally:
        session.close()


async def pointstable_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    args = context.args or []
    session = get_session()
    try:
        tournament = None
        if args and args[0].isdigit():
            tournament = session.query(Tournament).get(int(args[0]))
        elif update.effective_chat:
            candidates = get_open_tournaments(session, chat_id=update.effective_chat.id)
            tournament = candidates[0] if candidates else None
        if not tournament:
            await msg.reply_text(
                "Usage: <code>/pointstable &lt;id&gt;</code> (or run it in a chat "
                "with one active tournament).", parse_mode="HTML")
            return

        rows = get_points_table(session, tournament.id)
        lines = [
            f"🏆 <b>{tournament.name}</b> — Points Table",
            "━━━━━━━━━━━━━━━━━━━",
            "<code>#  Team               P  W  L  T  Pts</code>",
        ]
        if not rows:
            lines.append("<i>No teams yet.</i>")
        for rank, team in enumerate(rows, 1):
            user = session.query(User).get(team.user_id)
            label = _team_label(user) if user else f"User #{team.user_id}"
            lines.append(
                f"<code>{rank:<2} {label[:18]:<18} {team.played:<2} {team.won:<2} "
                f"{team.lost:<2} {team.tied:<2} {team.points:<3}</code>")
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("pointstable_handler failed")
        await msg.reply_text("❌ Couldn't load the points table.")
    finally:
        session.close()


def _stats_lines(stats):
    lines = [f"Matches counted: <b>{stats['total_matches_played']}</b>"]

    def _section(title, key, unit):
        if stats[key]:
            lines.append(f"\n<b>{title}</b>")
            for rank, e in enumerate(stats[key], 1):
                medal = "🥇" if rank == 1 else ("🥈" if rank == 2 else "🥉")
                lines.append(f"{medal} {e['player_name']} — <b>{e['value']}</b>{unit} "
                             f"<i>({e['team_name']})</i>")
        else:
            lines.append(f"\n<b>{title}</b>\n<i>No data yet.</i>")

    _section("🏏 MOST RUNS", "most_runs", " runs")
    _section("🎯 MOST WICKETS", "most_wickets", " wkts")
    _section("💥 MOST SIXES", "most_sixes", " sixes")
    _section("🏃 MOST FOURS", "most_fours", " fours")
    _section("⭐ HIGHEST SCORE", "highest_score", " runs")
    _section("🔥 BEST BOWLING", "best_bowling", "")
    _section("⚡ BEST STRIKE RATE", "best_strike_rate", " SR")
    _section("🛡️ BEST ECONOMY", "best_economy", " econ")
    return lines


async def tournamentstats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    args = context.args or []
    session = get_session()
    try:
        tournament = None
        if args and args[0].isdigit():
            tournament = session.query(Tournament).get(int(args[0]))
        elif update.effective_chat:
            candidates = get_open_tournaments(session, chat_id=update.effective_chat.id)
            tournament = candidates[0] if candidates else None
        if not tournament:
            await msg.reply_text(
                "Usage: <code>/tournamentstats &lt;id&gt;</code> (or run it in a chat "
                "with one active tournament).", parse_mode="HTML")
            return

        stats = get_tournament_leaderboard(session, tournament.id, top_n=3)
        lines = [f"📈 <b>{tournament.name} — STATS</b>", "━━━━━━━━━━━━━━━━━━━"]
        lines.extend(_stats_lines(stats))
        await msg.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("tournamentstats_handler failed")
        await msg.reply_text("❌ Couldn't load tournament stats.")
    finally:
        session.close()


async def addtotournament_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual fallback only — for modes not yet wired to the `Tournament`
    keyword, or a chat with 2+ simultaneously active tournaments."""
    msg = update.effective_message
    chat = update.effective_chat
    if chat.type == "private":
        await msg.reply_text("❌ This only works in group chats.")
        return

    session = get_session()
    try:
        user = sync_telegram_user(session, update.effective_user)
        if not user:
            await msg.reply_text("❌ Do /debut first!")
            return

        candidates = get_open_tournaments(session, chat_id=chat.id)
        if not candidates:
            await msg.reply_text(
                "❌ No active tournament here — create one with "
                "<code>/newtournament &lt;name&gt;</code>.", parse_mode="HTML")
            return

        match_id = get_user_recent_match_id(session, user.id, chat.id)
        if not match_id:
            await msg.reply_text(
                "❌ No recent completed, unattached match of yours found in this "
                "chat (within the last 2 hours).")
            return

        if len(candidates) == 1:
            tm, err = attach_match(session, candidates[0].id, match_id, added_by_user_id=user.id)
            if err:
                session.rollback()
                await msg.reply_text(f"❌ {err}")
                return
            session.commit()
            await msg.reply_text(
                f"✅ Match #{match_id} attached to <b>{candidates[0].name}</b>.",
                parse_mode="HTML")
            return

        rows = [[
            InlineKeyboardButton(t.name, callback_data=f"atp_{t.id}_{match_id}_{update.effective_user.id}")
        ] for t in candidates]
        await msg.reply_text(
            "Multiple active tournaments here — pick one to attach your "
            f"match #{match_id} to:",
            reply_markup=InlineKeyboardMarkup(rows))
    except Exception:
        session.rollback()
        logger.exception("addtotournament_handler failed")
        await msg.reply_text("❌ Something went wrong.")
    finally:
        session.close()


async def addtotournament_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """atp_<tournament_id>_<match_id>_<owner_tg>"""
    q = update.callback_query
    tg = q.from_user
    try:
        _, tournament_id, match_id, owner_tg = q.data.split("_")
        tournament_id, match_id, owner_tg = int(tournament_id), int(match_id), int(owner_tg)
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer()

    session = get_session()
    try:
        user = sync_telegram_user(session, tg)
        tm, err = attach_match(session, tournament_id, match_id,
                                added_by_user_id=user.id if user else None)
        if err:
            session.rollback()
            try:
                await q.edit_message_text(f"❌ {err}")
            except Exception:
                pass
            return
        session.commit()
        tournament = session.query(Tournament).get(tournament_id)
        try:
            await q.edit_message_text(
                f"✅ Match #{match_id} attached to <b>{tournament.name}</b>.",
                parse_mode="HTML")
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("addtotournament_pick_callback failed")
    finally:
        session.close()
