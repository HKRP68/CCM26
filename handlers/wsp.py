"""/wsp — PvP cricket match with full auto-simulation (watch mode).

Workflow mirrors UnderCover's /cric + /wpm but removes interactive delivery
and shot selection — the engine drives every ball automatically. Both players
watch via the Mini App (polling) and receive over-by-over updates + scorecard
images in the Telegram chat.

Flow:
  1. /wsp [overs]       → host creates a lobby
  2. Guest clicks Join  → toss, toss winner shown
  3. Toss winner clicks Bat/Bowl first
  4. Match is initialized (openers + bowler auto-selected) and the simulation
     coroutine starts immediately.
"""

import asyncio
import logging
import random
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Match
from services.match_constants import random_match_settings
from handlers.match import (
    _active_cric_match_in_chat, _active_cric_match_for_user,
    _cric_lobby_for_user, _user_label, _mention, _user_busy_message,
)

logger = logging.getLogger(__name__)

WSP_MAX_OVERS = 5
WSP_LOBBY_EXPIRE = 120   # seconds before an unjoined lobby auto-cancels


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════

def _wsp_lobby_key(chat_id):
    return f"wsp_lobby_{chat_id}"


def _wsp_lobby_for_user(bot_data, user_id):
    return next((v for k, v in bot_data.items()
                 if k.startswith("wsp_lobby_")
                 and user_id in (v.get("host_user_id"), v.get("guest_user_id"))), None)


def _cancel_wsp_timer(ctx, chat_id):
    try:
        if ctx.job_queue:
            for j in ctx.job_queue.get_jobs_by_name(f"wsp_lobby_{chat_id}"):
                j.schedule_removal()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# /wsp command — create lobby
# ══════════════════════════════════════════════════════════════════════

async def wsp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    cid = update.effective_chat.id

    try:
        overs = int(context.args[0]) if context.args else 1
    except (ValueError, IndexError):
        overs = 0
    if overs < 1 or overs > WSP_MAX_OVERS:
        await update.message.reply_text(
            f"ℹ️ <b>Usage:</b> <code>/wsp &lt;overs (1-{WSP_MAX_OVERS})&gt;</code> "
            "to start a watch-mode match lobby.",
            parse_mode="HTML")
        return

    session = get_session()
    try:
        host = session.query(User).filter(User.telegram_id == tg.id).first()
        if not host:
            await update.message.reply_text("❌ Use /debut first!")
            return
        if _active_cric_match_in_chat(session, cid):
            await update.message.reply_text("⚠️ There is already an active match in this chat!")
            return
        busy_host = _active_cric_match_for_user(session, host.id)
        if busy_host:
            await update.message.reply_text(
                _user_busy_message(busy_host), parse_mode="HTML",
                disable_web_page_preview=True)
            return
        if context.bot_data.get(_wsp_lobby_key(cid)):
            await update.message.reply_text("⚠️ A WSP lobby is already open in this chat!")
            return
        if _wsp_lobby_for_user(context.bot_data, host.id) or _cric_lobby_for_user(context.bot_data, host.id):
            await update.message.reply_text("⚠️ You already have an active match lobby!")
            return

        from handlers.lineup import validate_xi, _get_ordered_roster
        valid, errors = validate_xi(_get_ordered_roster(session, host.id))
        if not valid:
            await update.message.reply_text(
                "❌ <b>Lobby creation failed — your XI is invalid:</b>\n"
                + "\n".join(f"• {e}" for e in errors), parse_mode="HTML")
            return

        context.bot_data[_wsp_lobby_key(cid)] = {
            "host_user_id": host.id,
            "host_tg_id": host.telegram_id,
            "host_label": _user_label(host),
            "overs": overs,
            "original_lobby_chat_id": cid,
        }
        lobby_msg = await update.message.reply_text(
            "🏏 <b>WSP MATCH LOBBY</b> 🏏\n"
            "═════════════════════════════\n"
            f"• <b>Host:</b> {_user_label(host)}\n"
            f"• <b>Length:</b> {overs} Over(s)\n"
            "• <b>Mode:</b> Auto-simulated (watch mode)\n\n"
            "Click <b>Join Match</b> to accept!\n"
            f"⏳ <i>Expires in {WSP_LOBBY_EXPIRE // 60} min if no one joins.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🤝 Join Match", callback_data="wsp_join"),
                InlineKeyboardButton("❌ Cancel", callback_data="wsp_cancel"),
            ]]),
        )
        context.bot_data[_wsp_lobby_key(cid)]["lobby_msg_id"] = lobby_msg.message_id
        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _expire_wsp_lobby, WSP_LOBBY_EXPIRE,
                    name=f"wsp_lobby_{cid}",
                    data={"chat_id": cid, "lobby_msg_id": lobby_msg.message_id})
        except Exception:
            pass
    except Exception:
        logger.exception("/wsp lobby creation failed")
        await update.message.reply_text("❌ Failed to create WSP lobby.")
    finally:
        session.close()


async def _expire_wsp_lobby(ctx):
    d = ctx.job.data
    cid = d["chat_id"]
    key = _wsp_lobby_key(cid)
    lobby = ctx.bot_data.get(key)
    if not lobby or lobby.get("guest_user_id"):
        return
    ctx.bot_data.pop(key, None)
    try:
        msg_id = d.get("lobby_msg_id") or lobby.get("lobby_msg_id")
        if msg_id:
            await ctx.bot.edit_message_text(
                "⏰ <b>WSP Lobby expired</b> — no one joined. Start again with /wsp.",
                chat_id=cid, message_id=msg_id, parse_mode="HTML")
        else:
            await ctx.bot.send_message(cid, "⏰ WSP lobby expired — start again with /wsp.")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# Cancel lobby
# ══════════════════════════════════════════════════════════════════════

async def wsp_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat_id
    key = _wsp_lobby_key(cid)
    lobby = context.bot_data.get(key)
    if not lobby:
        await q.answer("No active WSP lobby.", show_alert=True)
        return
    is_admin = False
    try:
        member = await context.bot.get_chat_member(cid, q.from_user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        pass
    if q.from_user.id != lobby["host_tg_id"] and not is_admin:
        await q.answer("Only the host or a chat admin can cancel.", show_alert=True)
        return
    context.bot_data.pop(key, None)
    _cancel_wsp_timer(context, cid)
    await q.answer("Lobby cancelled.")
    await q.edit_message_text("❌ WSP lobby cancelled.")


# ══════════════════════════════════════════════════════════════════════
# Join callback
# ══════════════════════════════════════════════════════════════════════

async def wsp_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat_id
    key = _wsp_lobby_key(cid)
    lobby = context.bot_data.get(key)
    if not lobby:
        await q.answer("No active WSP lobby in this chat.", show_alert=True)
        return
    if q.from_user.id == lobby["host_tg_id"]:
        await q.answer("You cannot join your own lobby!", show_alert=True)
        return
    if lobby.get("guest_user_id"):
        await q.answer("Lobby is already full.", show_alert=True)
        return

    session = get_session()
    try:
        host = session.query(User).get(lobby["host_user_id"])
        guest = session.query(User).filter(User.telegram_id == q.from_user.id).first()
        if not guest:
            await q.answer("Use /debut first!", show_alert=True)
            return
        if not host:
            context.bot_data.pop(key, None)
            await q.answer("Host no longer exists.", show_alert=True)
            return
        if _active_cric_match_in_chat(session, cid):
            context.bot_data.pop(key, None)
            await q.answer("A match is already active in this chat.", show_alert=True)
            return
        if _active_cric_match_for_user(session, host.id):
            context.bot_data.pop(key, None)
            await q.answer("The host is already in another match.", show_alert=True)
            return
        if (_active_cric_match_for_user(session, guest.id)
                or _wsp_lobby_for_user(context.bot_data, guest.id)
                or _cric_lobby_for_user(context.bot_data, guest.id)):
            await q.answer("You already have an active match or lobby!", show_alert=True)
            return

        from handlers.lineup import validate_xi, _get_ordered_roster
        valid, errors = validate_xi(_get_ordered_roster(session, guest.id))
        if not valid:
            await q.answer("Join failed: your XI is invalid. Use /pxi to fix it.", show_alert=True)
            return

        winner = random.choice([host, guest])
        lobby.update({
            "guest_user_id": guest.id,
            "guest_tg_id": guest.telegram_id,
            "guest_label": _user_label(guest),
            "toss_winner_user_id": winner.id,
            "toss_winner_tg_id": winner.telegram_id,
        })
        _cancel_wsp_timer(context, cid)
        await q.answer("Joined WSP lobby!")
        await q.edit_message_text(
            "🪙 <b>TOSS COMPLETED!</b> 🪙\n"
            "═════════════════════════════\n"
            f"• Host: {_user_label(host)}\n"
            f"• Guest: {_user_label(guest)}\n\n"
            f"🎉 <b>{_user_label(winner)}</b> won the toss!\n"
            "Choose your decision:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Bat First 🏏", callback_data="wsp_decision:bat"),
                InlineKeyboardButton("Bowl First 🎳", callback_data="wsp_decision:bowl"),
            ]]),
        )
    except Exception:
        session.rollback()
        logger.exception("wsp_join_callback failed")
        await q.answer("Failed to join WSP lobby.", show_alert=True)
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
# Toss decision callback
# ══════════════════════════════════════════════════════════════════════

async def wsp_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat_id
    key = _wsp_lobby_key(cid)
    lobby = context.bot_data.get(key)
    if not lobby or not lobby.get("guest_user_id"):
        await q.answer("No joined WSP lobby in this chat.", show_alert=True)
        return
    if q.from_user.id != lobby.get("toss_winner_tg_id"):
        await q.answer("Only the toss winner can make the decision!", show_alert=True)
        return

    decision = q.data.split(":", 1)[1]
    if decision not in ("bat", "bowl"):
        await q.answer("Invalid toss decision.", show_alert=True)
        return

    session = get_session()
    try:
        host = session.query(User).get(lobby["host_user_id"])
        guest = session.query(User).get(lobby["guest_user_id"])
        if not host or not guest:
            context.bot_data.pop(key, None)
            await q.answer("Players no longer exist.", show_alert=True)
            return
        if _active_cric_match_in_chat(session, cid):
            context.bot_data.pop(key, None)
            await q.answer("A match is already active in this chat.", show_alert=True)
            return

        winner_id = lobby["toss_winner_user_id"]
        opponent_id = guest.id if winner_id == host.id else host.id
        settings = random_match_settings()

        match = Match(
            user1_id=host.id, user2_id=guest.id, status="toss",
            overs=lobby["overs"], toss_winner_id=winner_id,
            toss_decision=decision,
            batting_first_id=winner_id if decision == "bat" else opponent_id,
            bowling_first_id=opponent_id if decision == "bat" else winner_id,
            stadium=settings["stadium"], pitch_type=settings["pitch_type"],
            weather=settings["weather"], temperature=settings["temperature"],
            umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=cid, created_at=datetime.utcnow(),
        )
        session.add(match)
        session.commit()

        from services.match_webapp_service import init_match_for_wsp
        ok, msg = init_match_for_wsp(session, match.id)
        if not ok:
            session.delete(match)
            session.commit()
            await q.answer(f"Failed to launch match: {msg}", show_alert=True)
            return

        context.bot_data.pop(key, None)
        winner = host if winner_id == host.id else guest
        await q.answer()
        await q.edit_message_text(
            f"✅ {_user_label(winner)} elected to "
            f"{'BAT' if decision == 'bat' else 'BOWL'} FIRST\n\n"
            "🤖 <i>Match simulation starting…</i>",
            parse_mode="HTML",
        )

        bat_user = session.query(User).get(match.batting_first_id)
        bowl_user = session.query(User).get(match.bowling_first_id)
        bat_team = bat_user.team_name or f"@{bat_user.username}'s XI"
        bowl_team = bowl_user.team_name or f"@{bowl_user.username}'s XI"

        # Post match-ready card with spectate button
        from services.match_broadcast import send_match_ready_message
        await send_match_ready_message(
            context, cid, match, bat_team, bowl_team,
            _mention(bat_user), _mention(bowl_user),
            rules_note="Auto-simulated — watch live in the Mini App")

        # Launch simulation as background task
        from services.wsp_autoplay import run_wsp_simulation
        task = asyncio.create_task(
            run_wsp_simulation(context.application, match.id, cid))
        context.bot_data[f"wsp_task_{match.id}"] = task

    except Exception:
        session.rollback()
        logger.exception("wsp_decision_callback failed")
        try:
            await q.answer("Failed to launch WSP match.", show_alert=True)
        except Exception:
            pass
    finally:
        session.close()
