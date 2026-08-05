"""Fantasy league bot handlers.

Commands:
  /fantasy            — show active league + squad picker Mini App button
  /myfantasy          — show your squad and points
  /fantasyleaderboard — top-10 leaderboard
  /fantasystats       — top scoring players this week
"""

import json
import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

from database import get_session
from models import User

logger = logging.getLogger(__name__)

WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")


def _get_user(session, tg_id):
    return session.query(User).filter(User.telegram_id == tg_id).first()


# ═══════════════════════════════════════════════════════════════════════
# /fantasy — show active league
# ═══════════════════════════════════════════════════════════════════════

async def fantasy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = _get_user(session, tg.id)
        if not user:
            await update.message.reply_text("❌ Do /debut first to join the game!")
            return

        from services import fantasy_service
        league = fantasy_service.get_active_league(session)
        if not league:
            await update.message.reply_text(
                "🏏 <b>Fantasy Cricket</b>\n\n"
                "No active fantasy league right now.\n"
                "Check back soon — the admin will open one shortly!",
                parse_mode="HTML",
            )
            return

        team_info = fantasy_service.get_user_team_info(session, user.id, league.id)
        locked = fantasy_service.is_locked(league)
        # Web App inline buttons only work in private chats. In groups we must
        # use a t.me deep link instead (otherwise Telegram rejects the message
        # with "Button_type_invalid" → the old generic "something went wrong").
        is_private = update.effective_chat.type == "private"

        status_label = "LOCKED" if locked else league.status.upper()
        status_emoji = "🔒" if locked else ("🟢" if league.status == "open" else "✅")
        text = (
            f"🏏 <b>{league.name}</b>\n"
            f"Week {league.week_number}, {league.year} · {status_emoji} {status_label}\n\n"
        )
        if getattr(league, "description", None):
            text += f"📝 {league.description}\n\n"

        if team_info:
            text += (
                f"👤 <b>Your Squad:</b> {len(team_info['squad'])}/11 players\n"
                f"📊 <b>Points:</b> {team_info['total_points']}"
            )
            if team_info.get("rank"):
                text += f" · <b>Rank:</b> #{team_info['rank']}"
            text += "\n\n"
        else:
            text += "You haven't picked your squad yet.\n\n"

        def _squad_button(label):
            """Pick the right button type for the chat: a Web App button in
            private chats, a t.me deep link everywhere else."""
            if is_private and WEBAPP_URL:
                url = f"{WEBAPP_URL}/fantasy-picker?league_id={league.id}&user_id={user.id}"
                return InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
            link = fantasy_service.fantasy_deep_link(league.id)
            if link:
                return InlineKeyboardButton(label, url=link)
            return None

        buttons = []
        if locked:
            text += "🔒 Squads are locked. No more changes."
            if team_info:
                btn = _squad_button("👁 View My Squad")
                if btn:
                    buttons.append([btn])
        else:
            label = "✏️ Change Squad" if team_info else "🏏 Pick My Squad"
            btn = _squad_button(label)
            if btn:
                buttons.append([btn])
        # /fantasyguide is not listed in the group slash menu, so point at it
        # here — this is how it stays discoverable in a group.
        text += "\n\n<i>New to this? /fantasyguide explains the rules.</i>"
        buttons.append([InlineKeyboardButton(
            "🏆 Leaderboard", callback_data=f"fant_lb_{league.id}_1")])

        markup = InlineKeyboardMarkup(buttons) if buttons else None
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        logger.exception("fantasy_handler failed")
        await update.message.reply_text("❌ Something went wrong. Try again.")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /myfantasy — show squad detail
# ═══════════════════════════════════════════════════════════════════════

async def myfantasy_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = _get_user(session, tg.id)
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        from services import fantasy_service
        league = fantasy_service.get_active_league(session)
        if not league:
            await update.message.reply_text("No active fantasy league right now.")
            return

        info = fantasy_service.get_user_team_info(session, user.id, league.id)
        if not info or not info["squad"]:
            await update.message.reply_text(
                f"🏏 <b>{league.name}</b>\n\nYou haven't picked your squad yet. Use /fantasy to get started!",
                parse_mode="HTML",
            )
            return

        lines = [f"🏏 <b>{league.name}</b> — Your Squad\n"]
        for pick in sorted(info["squad"], key=lambda x: (x["role"] != "captain", x["role"] != "vc", x["name"])):
            role_tag = ""
            if pick["role"] == "captain":
                role_tag = " 👑"
            elif pick["role"] == "vc":
                role_tag = " ⭐"
            lines.append(f"• {pick['name']}{role_tag} · {pick['country']} — <b>{pick['points']} pts</b>")

        lines.append(f"\n📊 <b>Total: {info['total_points']} pts</b>")
        if info.get("rank"):
            lines.append(f"🏆 <b>Rank: #{info['rank']}</b>")
        if info.get("locked"):
            lines.append("\n🔒 Squad locked")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("myfantasy_handler failed")
        await update.message.reply_text("❌ Something went wrong. Try again.")
    finally:
        session.close()


async def fantasyguide_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User-facing guide for fantasy commands and squad editing."""
    text = (
        "🏏 <b>Fantasy Guide</b>\n\n"
        "<b>What is Fantasy?</b>\n"
        "Pick an XI from the current fantasy player pool. Your players earn points when admins enter match scores.\n\n"
        "<b>How to play</b>\n"
        "1) Use /fantasy to open the current league.\n"
        "2) Tap <b>Pick My Squad</b> in private chat or from the Mini App link.\n"
        "3) Search players, filter by role/country, then select exactly 11 players.\n"
        "4) Tap a selected player to set Captain (2×) and Vice-Captain (1.5×).\n"
        "5) Confirm before squads lock.\n\n"
        "<b>Editing your XI</b>\n"
        "If the league is still open, use /fantasy again and tap <b>Change Squad</b>. "
        "Locked leagues cannot be edited.\n\n"
        "<b>Useful commands</b>\n"
        "• /fantasy — open or edit your squad\n"
        "• /myfantasy — view your squad, points, and rank\n"
        "• /fantasyleaderboard — see top fantasy users\n"
        "• /fantasystats — see top scoring fantasy players\n"
        "• /fantasyguide — show this guide"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════
# /fantasyleaderboard — top users
# ═══════════════════════════════════════════════════════════════════════

async def fantasyleaderboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session()
    try:
        from services import fantasy_service
        league = fantasy_service.get_active_league(session)
        if not league:
            # Try most recent scored league
            from models import FantasyLeague
            league = (session.query(FantasyLeague)
                      .order_by(FantasyLeague.id.desc()).first())
        if not league:
            await update.message.reply_text("No fantasy league found.")
            return

        rows, total = fantasy_service.get_leaderboard(session, league.id, page=1, per_page=10)
        if not rows:
            await update.message.reply_text(
                f"🏆 <b>{league.name}</b>\n\nNo entries yet.",
                parse_mode="HTML",
            )
            return

        medal = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 <b>{league.name}</b> — Top {len(rows)}\n"]
        for i, row in enumerate(rows):
            m = medal[i] if i < 3 else f"{i+1}."
            name = row["username"]
            lines.append(f"{m} {name} — <b>{row['total_points']} pts</b>")

        lines.append(f"\n<i>{total} total entries</i>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("fantasyleaderboard_handler failed")
        await update.message.reply_text("❌ Something went wrong. Try again.")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /fantasystats — top scoring players
# ═══════════════════════════════════════════════════════════════════════

async def fantasystats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session()
    try:
        from services import fantasy_service
        league = fantasy_service.get_active_league(session)
        if not league:
            from models import FantasyLeague
            league = (session.query(FantasyLeague)
                      .order_by(FantasyLeague.id.desc()).first())
        if not league:
            await update.message.reply_text("No fantasy league found.")
            return

        top = fantasy_service.get_top_scorers(session, league.id, n=10)
        if not top:
            await update.message.reply_text(
                f"🏏 <b>{league.name}</b>\n\nNo match points entered yet.",
                parse_mode="HTML",
            )
            return

        lines = [f"🌟 <b>{league.name}</b> — Top Fantasy Scorers\n"]
        for i, row in enumerate(top, 1):
            lines.append(f"{i}. <b>{row['name']}</b> ({row['country']}) — {row['total_points']} pts")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception:
        logger.exception("fantasystats_handler failed")
        await update.message.reply_text("❌ Something went wrong. Try again.")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# Leaderboard callback (pagination)
# ═══════════════════════════════════════════════════════════════════════

async def fantasy_lb_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split("_")  # fant_lb_{league_id}_{page}
    if len(parts) < 4:
        return
    league_id = int(parts[2])
    page = int(parts[3])

    session = get_session()
    try:
        from models import FantasyLeague
        from services import fantasy_service
        league = session.query(FantasyLeague).get(league_id)
        if not league:
            await query.edit_message_text("League not found.")
            return

        rows, total = fantasy_service.get_leaderboard(session, league_id, page=page, per_page=10)
        medal = ["🥇", "🥈", "🥉"]
        lines = [f"🏆 <b>{league.name}</b> — Leaderboard\n"]
        for i, row in enumerate(rows):
            rank = (page - 1) * 10 + i + 1
            m = medal[i] if (page == 1 and i < 3) else f"{rank}."
            lines.append(f"{m} {row['username']} — <b>{row['total_points']} pts</b>")

        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀️", callback_data=f"fant_lb_{league_id}_{page-1}"))
        if len(rows) == 10:
            nav.append(InlineKeyboardButton("▶️", callback_data=f"fant_lb_{league_id}_{page+1}"))
        markup = InlineKeyboardMarkup([nav]) if nav else None

        await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)
    except Exception:
        logger.exception("fantasy_lb_callback failed")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# Web App data handler — receives squad selection from Mini App
# ═══════════════════════════════════════════════════════════════════════

async def fantasy_webapp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    web_app_data = update.effective_message.web_app_data
    if not web_app_data:
        return

    session = get_session()
    try:
        data = json.loads(web_app_data.data)
        if data.get("type") != "fantasy_picks":
            return

        user = _get_user(session, tg.id)
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        from services import fantasy_service
        league_id = data.get("league_id")
        picks = data.get("picks", [])

        league = fantasy_service.get_active_league(session)
        if not league or (league_id and league.id != int(league_id)):
            await update.message.reply_text("❌ No active league found.")
            return
        if fantasy_service.is_locked(league):
            await update.message.reply_text("🔒 Squads are locked. No changes allowed.")
            return

        entry = fantasy_service.get_or_create_entry(session, user.id, league.id)
        ok, msg = fantasy_service.set_picks(session, entry.id, picks)
        if ok:
            session.commit()
            team_info = fantasy_service.get_user_team_info(session, user.id, league.id)
            cap = next((p["name"] for p in (team_info or {}).get("squad", []) if p["role"] == "captain"), "")
            vc = next((p["name"] for p in (team_info or {}).get("squad", []) if p["role"] == "vc"), "")
            reply = (
                f"✅ <b>Squad saved for {league.name}!</b>\n\n"
                f"👑 Captain: {cap}\n⭐ VC: {vc}\n"
                f"📊 Current Points: {team_info['total_points'] if team_info else 0}\n\n"
                f"Use /myfantasy to view your full squad."
            )
            await update.message.reply_text(reply, parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ {msg}")
    except json.JSONDecodeError:
        await update.message.reply_text("❌ Invalid data received.")
    except Exception:
        logger.exception("fantasy_webapp_handler failed")
        session.rollback()
        await update.message.reply_text("❌ Something went wrong saving your squad.")
    finally:
        session.close()
