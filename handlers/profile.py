"""Handler for /myprofile — user profile with tabs."""

import logging
from html import escape
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from sqlalchemy import desc, or_

from database import get_session
from models import User, Player, UserRoster, Match, ActivityLog
from config import MAX_ROSTER
from services import rich_message as R

logger = logging.getLogger(__name__)


def _build_keyboard(active, user_id):
    tabs = [("info", "👤 Overview"), ("stats", "📊 Career stats"),
            ("news", "📰 Activity"), ("results", "🏏 Results")]
    buttons = []
    for key, label in tabs:
        mark = "✓ " if key == active else ""
        buttons.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"mp_{key}_{user_id}"))
    receipts = InlineKeyboardButton(("✓ " if active == "prizes" else "") + "💰 Prize receipts",
                                   callback_data=f"mp_prizes_{user_id}")
    return InlineKeyboardMarkup([buttons[:2], buttons[2:], [receipts]])


def _profile_blocks(text):
    """Use native headings and fact tables, preserving the HTML fallback."""
    from services.rich_layout import aligned_blocks, lines_from_html, plain
    if text.startswith(("📰", "🏏", "💰")):
        # Narrative feeds contain clock times and free-form activity details.
        # Keep those intact instead of guessing that every colon starts a fact.
        visible, marker, folded = text.partition("<blockquote expandable>")
        lines = [line for line in lines_from_html(visible) if line]
        blocks = [R.heading(lines[0], 2)] + [R.paragraph(line) for line in lines[1:]]
        if marker:
            body, _, after = folded.partition("</blockquote>")
            lines = [line for line in lines_from_html(body) if line]
            if lines:
                blocks.append(R.details(plain(lines[0]), [R.paragraph(line) for line in lines[1:]]))
            blocks.extend(R.paragraph(line) for line in lines_from_html(after) if line)
        return blocks
    blocks = aligned_blocks(text)
    if not blocks:
        if "<blockquote" in text:
            return None
        lines = [line for line in lines_from_html(text) if line]
        return [R.heading(lines[0], 2)] + [R.paragraph(line) for line in lines[1:]] if lines else None
    if blocks and blocks[0].get("type") == "paragraph":
        blocks[0] = R.heading(blocks[0]["text"], 2)
    return blocks


def _owner_name(user):
    return f"@{user.username}" if user.username else (user.first_name or "Manager")


def _team_value(session, user_id):
    """Sum of the first 11 roster ratings × 1000."""
    total = (session.query(Player.rating)
             .join(UserRoster, UserRoster.player_id == Player.id)
             .filter(UserRoster.user_id == user_id)
             .order_by(UserRoster.order_position).limit(11).all())
    return sum(r[0] for r in total) * 1000


def _avg_ovr(session, user_id):
    """Average OVR of the first 11 roster positions."""
    ratings = (session.query(Player.rating)
               .join(UserRoster, UserRoster.player_id == Player.id)
               .filter(UserRoster.user_id == user_id)
               .order_by(UserRoster.order_position).limit(11).all())
    if not ratings:
        return 0
    vals = [r[0] for r in ratings]
    return round(sum(vals) / len(vals), 1)


def _format_info(session, user):
    captain_name = "Not set · use /setcaptain"
    if user.captain_roster_id:
        row = (session.query(Player).join(UserRoster, UserRoster.player_id == Player.id)
               .filter(UserRoster.id == user.captain_roster_id, UserRoster.user_id == user.id).first())
        if row:
            captain_name = row.name

    value = _team_value(session, user.id)
    avg = _avg_ovr(session, user.id)
    team = user.team_name or f"{_owner_name(user)}'s XI"

    # Featured unlocked achievements
    badges_line = ""
    try:
        from services.achievement_service import get_top_unlocked
        top = get_top_unlocked(session, user.id, limit=3)
        if top:
            emojis = " ".join(escape(str(a['emoji'])) for a in top)
            names = " · ".join(escape(a['name']) for a in top)
            badges_line = (
                f"\n\n🏆 <b>Featured badges</b>\n{emojis}\n"
                f"   <i>{names}</i>"
            )
    except Exception:
        pass

    return (
        f"👤 <b>YOUR PROFILE</b>\n\n"
        f"Manager: {escape(_owner_name(user))}\n"
        f"Team: <b>{escape(team)}</b>\n"
        f"Captain: {escape(captain_name)}\n"
        f"Roster: {user.roster_count or 0}/{MAX_ROSTER}\n"
        f"XI average OVR: {avg}\n"
        f"XI rating value: {value:,} coins\n\n"
        f"💰 <b>Your balance</b>\n"
        f"🪙 Coins: {user.total_coins or 0:,}\n"
        f"💎 Gems: {user.total_gems or 0:,}\n"
        f"🎯 Quest Points: {user.quest_points or 0:,}"
        f"{badges_line}\n\n"
        f"<i>XI figures use the first 11 roster positions. Rating value = combined ratings × 1,000.</i>\n"
        f"Use the tabs below to explore your career."
    )


def _format_stats(session, user):
    played = user.matches_played or 0
    won = user.matches_won or 0
    lost = user.matches_lost or 0
    win_pct = round((won / played) * 100, 1) if played else 0
    streak = user.win_streak or 0
    best = user.best_streak or 0
    active = user.active_days or 0

    if streak:
        streak_line = f"{streak} consecutive {'win' if streak == 1 else 'wins'} 🔥"
    else:
        streak_line = "0 · Win your next match to start a new streak" if won else "0 · Your first win starts a streak"
    last_played = (user.last_match_date.strftime("%d %b %Y")
                   if user.last_match_date else "No matches recorded")

    return (
        f"📊 <b>YOUR CAREER STATS</b>\n\n"
        f"🎮 Matches played: {played:,}\n"
        f"🏆 Wins: {won:,}\n"
        f"❌ Losses: {lost:,}\n"
        f"📈 Win rate: {f'{win_pct:.1f}%' if played else '—'}\n\n"
        f"🔥 Current streak: {streak_line}\n"
        f"⭐ Best win streak: {best}\n"
        f"⚡ Active match days: {active}\n"
        f"🗓 Last match: {last_played}\n\n"
        f"<i>Win rate = wins ÷ matches played. Active days count days you played a match.</i>"
    )


def _format_news(session, user):
    """Recent activity feed (last 15 events)."""
    news = (session.query(ActivityLog)
            .filter(ActivityLog.user_id == user.id)
            .order_by(desc(ActivityLog.created_at)).limit(15).all())

    if not news:
        return ("📰 <b>YOUR RECENT ACTIVITY</b>\n\n"
                "Your activity feed is empty.\n"
                "Claim a reward with /daily or /claim, or play a match with /playmatch.\n\n"
                "Rewards, transfers and match updates will appear here.")

    lines = ["📰 <b>YOUR RECENT ACTIVITY</b>", f"Events shown: {len(news)}", "Order: Newest first", ""]
    # Comprehensive action → emoji map (covers everything that calls log_activity)
    EMOJI = {
        "claim": "🎁", "retain": "✅", "release": "💸", "auto_retain": "⏱",
        "auto_release": "🗑", "buy": "🛒", "buy_market": "🛒",
        "daily": "📅", "gspin": "🎰", "trade": "🔄",
        "captain": "👑", "swap": "🔁", "debut": "🎉",
        "batting_order": "🏏",
        "match_start": "🏏", "match_reward": "🎁", "match_won": "🏆",
        "match_lost": "💔", "endmatch": "🛑", "match_fine": "⚠️",
        "replace": "🔁", "teamname": "🏷",
        "trait_buy": "💎", "trait_apply": "✨", "trait_upgrade": "⬆",
        "trait_replace": "🔄", "trait_market": "💎",
        "buy_pack": "📦", "open_pack": "🎴", "free_pack": "🎁",
    }
    entries = []
    for n in news:
        t = n.created_at.strftime("%d %b %Y · %H:%M UTC") if n.created_at else "Time not recorded"
        emoji = EMOJI.get(n.action, "•")
        # Use detail if present, else fall back to action
        detail = (n.detail or n.action or "").strip()
        # Trim very long details for the news feed
        # Bound the escaped text too, so names with markup characters cannot
        # turn this 15-entry page into an oversized Telegram message.
        if len(detail) > 100 or len(escape(detail)) > 120:
            detail = detail[:99]
            while len(escape(detail)) > 119:
                detail = detail[:-1]
            detail += "…"
        # Coins/gems change
        extras = []
        if n.coins_change:
            sign = "+" if n.coins_change > 0 else ""
            extras.append(f"{sign}{n.coins_change:,}🪙")
        if n.gems_change:
            sign = "+" if n.gems_change > 0 else ""
            extras.append(f"{sign}{n.gems_change}💎")
        extra_str = f"  <code>{' '.join(extras)}</code>" if extras else ""
        entries.append(f"{emoji} <i>{t}</i>\n{escape(detail)}{extra_str}")

    lines.extend(entries[:5])
    if len(entries) > 5:
        lines.append("\n<blockquote expandable><b>Earlier activity</b>\n" + "\n\n".join(entries[5:]) + "</blockquote>")

    return "\n".join(lines)


def _format_results(session, user):
    matches = (session.query(Match)
               .filter(Match.status == "completed",
                       or_(Match.user1_id == user.id, Match.user2_id == user.id))
               .order_by(desc(Match.completed_at)).limit(10).all())

    if not matches:
        return ("🏏 <b>YOUR RECENT RESULTS</b>\n\n"
                "No completed matches yet.\n"
                "Start a match with /playmatch. Your latest 10 completed matches will appear here.", [])

    lines = ["🏏 <b>YOUR RECENT RESULTS</b>", f"Matches shown: {len(matches)}", "Order: Newest first", ""]
    jump_buttons = []

    for m in matches:
        opp_id = m.user2_id if m.user1_id == user.id else m.user1_id
        opp = session.get(User, opp_id)
        opp_name = _owner_name(opp) if opp else "Unknown opponent"
        if m.winner_id == user.id:
            emoji, outcome, detail = "🟢", "WIN", "You won"
        elif m.winner_id is not None:
            emoji, outcome, detail = "🔴", "LOSS", "You lost"
        elif (m.margin_type or "").lower() in {"tie", "tied"}:
            emoji, outcome, detail = "🤝", "TIE", "Scores finished level"
        else:
            emoji, outcome, detail = "⚪", "NO RESULT", "No winner recorded"

        if m.winner_id is not None:
            if m.margin_type in {"runs", "wickets"} and m.margin_value is not None:
                unit = m.margin_type[:-1] if m.margin_value == 1 else m.margin_type
                detail += f" by {m.margin_value} {unit}"
            elif m.margin_type:
                detail += f" · {m.margin_type.replace('_', ' ')}"

        played = m.completed_at.strftime("%d %b %Y") if m.completed_at else "Date not recorded"
        lines.extend([f"{emoji} <b>{outcome} · #{m.id} · vs {escape(opp_name)}</b>",
                      f"{escape(detail)} · {m.overs} overs · {played}", ""])

        # Build jump button if we have message_id
        if m.result_message_id and m.chat_id:
            # Supergroup result links retain Telegram's membership checks.
            chat_id_str = str(m.chat_id)
            if chat_id_str.startswith("-100"):
                cid_short = chat_id_str[4:]
                link = f"https://t.me/c/{cid_short}/{m.result_message_id}"
            else:
                link = None
            if link:
                jump_buttons.append([InlineKeyboardButton(
                    f"🔗 #{m.id} · vs {opp_name}",
                    url=link)])

    lines.append("Open a match below to view its original result. Group access may be required." if jump_buttons
                 else "New results appear here after a match finishes.")
    return ("\n".join(lines), jump_buttons)


async def myprofile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("👋 Create your team with /debut, then open /myprofile.")
            return

        text = _format_info(session, user)
        await R.reply_rich(
            update.message, _profile_blocks(text), text,
            reply_markup=_build_keyboard("info", user.id))
    except Exception:
        logger.exception("MyProfile err")
        await update.message.reply_text("⚠️ Could not load your profile. Try /myprofile again.")
    finally:
        session.close()


async def myprofile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        parts = q.data.split("_")
        tab, uid = parts[1], int(parts[2])
        if tab not in {"info", "stats", "news", "results", "prizes"}:
            raise ValueError("Unknown profile page")
        before_id = int(parts[3]) if len(parts) > 3 else None
        if before_id is not None and (tab != "prizes" or before_id <= 0):
            raise ValueError("Invalid receipt page")
    except (AttributeError, IndexError, TypeError, ValueError):
        await q.answer("This profile page is unavailable. Open /myprofile again.", show_alert=True)
        return

    session = get_session()
    try:
        viewer = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        if not viewer or viewer.id != uid:
            await q.answer("These tabs belong to another manager. Open /myprofile to view yours.", show_alert=True)
            return
        if tab == "prizes" and (not update.effective_chat or update.effective_chat.type != "private"):
            await q.answer("Open /myprofile in a private chat with the bot to view prize receipts.", show_alert=True)
            return
        await q.answer()

        user = viewer
        kb = _build_keyboard(tab, user.id)

        if tab == "info":
            text = _format_info(session, user)
        elif tab == "stats":
            text = _format_stats(session, user)
        elif tab == "news":
            text = _format_news(session, user)
        elif tab == "results":
            text, jump_btns = _format_results(session, user)
            kb = InlineKeyboardMarkup(list(kb.inline_keyboard) + jump_btns)
        elif tab == "prizes":
            from services.tournament_payouts import recipient_receipt_page, retry_for_recipient
            retry_for_recipient(session, user.telegram_id)
            session.commit()
            text, older = recipient_receipt_page(session, user.telegram_id, before_id=before_id)
            text = text.replace("💰 <b>TOURNAMENT PRIZE RECEIPTS</b>",
                                "💰 <b>YOUR PRIZE RECEIPTS</b>\n"
                                "Your tournament rewards and payment status.", 1)
            if "No tournament prize receipts yet." in text:
                text = text.replace("No tournament prize receipts yet.",
                                    "No prize receipts yet. Tournament rewards will appear here when they are assigned to you.")
            navigation = []
            if before_id:
                navigation.append(InlineKeyboardButton("⏮ Newest receipts", callback_data=f"mp_prizes_{user.id}"))
            if older:
                navigation.append(InlineKeyboardButton("Older receipts →", callback_data=f"mp_prizes_{user.id}_{older}"))
            if navigation:
                kb = InlineKeyboardMarkup(list(kb.inline_keyboard) + [navigation])
        await R.edit_rich(q, _profile_blocks(text), text, reply_markup=kb)
    except Exception:
        logger.exception("MyProfile cb err")
    finally:
        session.close()
