"""Handler for /myprofile — user profile with tabs."""

import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import desc, or_

from database import get_session
from models import User, Player, UserRoster, Match, ActivityLog

logger = logging.getLogger(__name__)


def _build_keyboard(active, user_id):
    tabs = [("info", "ℹ️ Info"), ("stats", "📊 Stats"),
            ("news", "📰 News"), ("results", "📋 Results")]
    row = []
    for key, label in tabs:
        mark = "● " if key == active else ""
        row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"mp_{key}_{user_id}"))
    return InlineKeyboardMarkup([row])


def _team_value(session, user_id):
    """Sum of all XI player ratings × 1000."""
    total = (session.query(Player.rating)
             .join(UserRoster, UserRoster.player_id == Player.id)
             .filter(UserRoster.user_id == user_id)
             .order_by(UserRoster.order_position).limit(11).all())
    return sum(r[0] for r in total) * 1000


def _avg_ovr(session, user_id):
    """Avg OVR of top 11."""
    ratings = (session.query(Player.rating)
               .join(UserRoster, UserRoster.player_id == Player.id)
               .filter(UserRoster.user_id == user_id)
               .order_by(UserRoster.order_position).limit(11).all())
    if not ratings:
        return 0
    vals = [r[0] for r in ratings]
    return round(sum(vals) / len(vals), 1)


def _format_info(session, user):
    captain_name = "Not set"
    if user.captain_roster_id:
        row = (session.query(Player).join(UserRoster, UserRoster.player_id == Player.id)
               .filter(UserRoster.id == user.captain_roster_id).first())
        if row:
            captain_name = row.name

    value = _team_value(session, user.id)
    avg = _avg_ovr(session, user.id)
    team = user.team_name or f"@{user.username or user.first_name}'s XI"

    return (
        f"👤 <b>MY PROFILE</b>\n\n"
        f"🏏 <b>Owner:</b> @{user.username or user.first_name}\n"
        f"👑 <b>Team:</b> {team}\n"
        f"⭐ <b>Captain:</b> {captain_name}\n"
        f"📈 <b>Overall:</b> {avg}\n"
        f"💎 <b>Value:</b> {value:,} Coins\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Purse</b>\n"
        f"🪙 Coins: {user.total_coins:,}\n"
        f"💎 Gems: {user.total_gems:,}"
    )


def _format_stats(session, user):
    played = user.matches_played or 0
    won = user.matches_won or 0
    lost = user.matches_lost or 0
    win_pct = round((won / played) * 100, 1) if played else 0
    streak = user.win_streak or 0
    best = user.best_streak or 0
    active = user.active_days or 0

    return (
        f"📊 <b>TEAM STATS</b>\n\n"
        f"🎮 <b>Matches Played:</b> {played}\n"
        f"🏆 <b>Won:</b> {won}\n"
        f"❌ <b>Lost:</b> {lost}\n"
        f"📈 <b>Win %:</b> {win_pct}%\n\n"
        f"🔥 <b>Current Streak:</b> {streak}\n"
        f"⭐ <b>Best Streak:</b> {best}\n"
        f"⚡ <b>Active Days:</b> {active}"
    )


def _format_news(session, user):
    news = (session.query(ActivityLog)
            .filter(ActivityLog.user_id == user.id)
            .order_by(desc(ActivityLog.timestamp)).limit(15).all())

    if not news:
        return "📰 <b>TEAM NEWS</b>\n\n<i>No activity yet.</i>"

    lines = ["📰 <b>TEAM NEWS</b>\n"]
    for n in news:
        t = n.timestamp.strftime("%b %d")
        act = n.action
        detail = n.detail or ""

        # Map action to emoji
        emoji = {
            "claim": "🎁", "release": "💸", "buy": "🛒",
            "daily": "📅", "gspin": "🎰", "trade": "🔄",
            "captain": "👑", "swap": "🔁", "debut": "🎉",
            "match_start": "🏏", "match_reward": "🎁",
            "endmatch": "🛑", "match_fine": "⚠️",
        }.get(act, "•")

        # Coins/gems info
        extras = []
        if n.coins_change:
            sign = "+" if n.coins_change > 0 else ""
            extras.append(f"{sign}{n.coins_change:,} 🪙")
        if n.gems_change:
            sign = "+" if n.gems_change > 0 else ""
            extras.append(f"{sign}{n.gems_change} 💎")
        extra_str = f" ({', '.join(extras)})" if extras else ""

        lines.append(f"{emoji} <i>{t}</i> — {detail}{extra_str}")

    return "\n".join(lines)


def _format_results(session, user):
    matches = (session.query(Match)
               .filter(Match.status == "completed",
                       or_(Match.user1_id == user.id, Match.user2_id == user.id))
               .order_by(desc(Match.completed_at)).limit(10).all())

    if not matches:
        return ("📋 <b>MATCH RESULTS</b>\n\n<i>No matches played yet.</i>", [])

    lines = ["📋 <b>LAST 10 MATCHES</b>\n"]
    jump_buttons = []

    for m in matches:
        opp_id = m.user2_id if m.user1_id == user.id else m.user1_id
        opp = session.query(User).get(opp_id)
        opp_name = opp.username or opp.first_name if opp else "Unknown"

        won = (m.winner_id == user.id)
        emoji = "🟢" if won else "🔴"
        verb = "beat" if won else "lost to"

        margin_str = ""
        if m.margin_type and m.margin_value is not None:
            margin_str = f" by {m.margin_value} {m.margin_type}"

        line = f"{emoji} vs {opp_name} — <b>{verb}{margin_str}</b>"
        lines.append(line)

        # Build jump button if we have message_id
        if m.result_message_id and m.chat_id:
            # t.me/c/CHATID/MSGID — only works for public chats with simple link
            # For supergroups: chat_id without "-100" prefix
            chat_id_str = str(m.chat_id)
            if chat_id_str.startswith("-100"):
                cid_short = chat_id_str[4:]
                link = f"https://t.me/c/{cid_short}/{m.result_message_id}"
            else:
                link = None
            if link:
                jump_buttons.append([InlineKeyboardButton(
                    f"🔗 Jump to vs {opp_name}",
                    url=link)])

    return ("\n".join(lines), jump_buttons)


async def myprofile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        text = _format_info(session, user)
        await update.message.reply_text(
            text, parse_mode="HTML",
            reply_markup=_build_keyboard("info", user.id))
    except Exception:
        logger.exception("MyProfile err")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def myprofile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split("_")
    tab, uid = parts[1], int(parts[2])

    session = get_session()
    try:
        viewer = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        if not viewer or viewer.id != uid:
            await q.answer("This is not your profile!")
            return
        await q.answer()

        user = viewer
        kb = _build_keyboard(tab, user.id)

        if tab == "info":
            text = _format_info(session, user)
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif tab == "stats":
            text = _format_stats(session, user)
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif tab == "news":
            text = _format_news(session, user)
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        elif tab == "results":
            text, jump_btns = _format_results(session, user)
            rows = kb.inline_keyboard + tuple(jump_btns)
            new_kb = InlineKeyboardMarkup(list(kb.inline_keyboard) + jump_btns)
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=new_kb)
    except Exception:
        logger.exception("MyProfile cb err")
    finally:
        session.close()
