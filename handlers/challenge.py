"""Two-player /cm challenge mode using the Mini App match flow."""

import logging
import random
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import Match, User
from services.match_constants import MATCH_EXPIRE, random_match_settings
from handlers.match import (
    _active_cric_match_for_user,
    _active_cric_match_in_chat,
    _active_match_in_chat,
    _chat_busy_message,
    _cric_lobby_for_user,
    _mention,
    _user_label,
)

logger = logging.getLogger(__name__)

CM_LOBBY_EXPIRE = 75


def _max_overs(session=None):
    """Return the website-configured challenge limit, clamped defensively."""
    from services.config_service import get_challenge_max_overs
    return get_challenge_max_overs(session)


def _cm_lobby_key(lobby_id):
    return f"cm_lobby_{lobby_id}"


def _cm_chat_key(chat_id):
    return f"cm_lobby_chat_{chat_id}"


def _cm_user_lobby(bot_data, user_id):
    return next((lobby for key, lobby in bot_data.items()
                 if key.startswith("cm_lobby_")
                 and isinstance(lobby, dict)
                 and user_id in (lobby.get("challenger_user_id"),
                                 lobby.get("target_user_id"))), None)


def _pop_lobby(context, lobby_id):
    key = _cm_lobby_key(lobby_id)
    lobby = context.bot_data.pop(key, None)
    if lobby:
        context.bot_data.pop(_cm_chat_key(lobby.get("chat_id")), None)
    return lobby


def _cancel_cm_timer(context, lobby_id):
    try:
        if context.job_queue:
            for job in context.job_queue.get_jobs_by_name(f"cm_lobby_{lobby_id}"):
                job.schedule_removal()
    except Exception:
        logger.exception("Failed to cancel /cm lobby timer")


def _xi_error(errors_or_count):
    if isinstance(errors_or_count, (list, tuple)):
        return "❌ Your playing XI is invalid. Use /xi to fix it:\n" + "\n".join(
            f"• {error}" for error in errors_or_count)
    if errors_or_count == 0:
        return "❌ You do not have a squad yet. Use /debut first, then accept the challenge again."
    return ("❌ You need a valid playing XI before accepting challenge mode. "
            f"You currently have {errors_or_count}/11 players. Use /autobuild or /xi after completing your squad.")


def _validate_user_xi(session, user_id):
    from handlers.lineup import validate_xi, _get_ordered_roster
    roster = _get_ordered_roster(session, user_id)
    valid, errors = validate_xi(roster)
    return valid, errors, len(roster)


async def challenge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a targeted /cm lobby, mirroring /wpm until launch."""
    if not context.args:
        await update.message.reply_text("Usage: <code>/cm @username</code>", parse_mode="HTML")
        return
    target_name = context.args[0].lstrip("@").strip()
    cid = update.effective_chat.id
    session = get_session()
    try:
        challenger = session.query(User).filter(User.telegram_id == update.effective_user.id).first()
        if not challenger:
            await update.message.reply_text("❌ Use /debut first.")
            return
        target = session.query(User).filter(User.username.ilike(target_name)).first()
        if not target:
            await update.message.reply_text(f"❌ @{target_name} not found. They need to use /debut first.")
            return
        if target.id == challenger.id:
            await update.message.reply_text("❌ You cannot challenge yourself.")
            return

        valid, errors, count = _validate_user_xi(session, challenger.id)
        if not valid:
            await update.message.reply_text(_xi_error(errors if errors else count), parse_mode="HTML")
            return

        existing = _active_match_in_chat(session, cid) or _active_cric_match_in_chat(session, cid)
        if existing:
            await update.message.reply_text(_chat_busy_message(existing), parse_mode="HTML")
            return
        if (_active_cric_match_for_user(session, challenger.id)
                or _cric_lobby_for_user(context.bot_data, challenger.id)
                or _cm_user_lobby(context.bot_data, challenger.id)):
            await update.message.reply_text("⚠️ You already have an active match or lobby!")
            return
        if context.bot_data.get(_cm_chat_key(cid)):
            await update.message.reply_text("⚠️ There is already a /cm challenge waiting in this chat!")
            return

        lobby_id = random.randint(100000, 999999)
        while context.bot_data.get(_cm_lobby_key(lobby_id)):
            lobby_id = random.randint(100000, 999999)
        context.bot_data[_cm_lobby_key(lobby_id)] = {
            "lobby_id": lobby_id,
            "chat_id": cid,
            "original_lobby_chat_id": cid,
            "challenger_user_id": challenger.id,
            "challenger_tg_id": challenger.telegram_id,
            "target_user_id": target.id,
            "target_tg_id": target.telegram_id,
            "overs": min(_max_overs(session), 2),
            "created_at": datetime.utcnow().isoformat(),
        }
        context.bot_data[_cm_chat_key(cid)] = lobby_id
        msg = await update.message.reply_text(
            f"⚔️ <b>CHALLENGE MODE LOBBY</b>\n"
            "═════════════════════════════\n"
            f"• <b>Challenger:</b> {_user_label(challenger)}\n"
            f"• <b>Invited:</b> {_user_label(target)}\n"
            f"• <b>Rules:</b> 2 wickets per innings · up to {min(_max_overs(session), 2)} over(s)\n"
            "• <b>Flow:</b> fast /wpm-style Mini App gameplay with live spectating\n\n"
            "The invited player accepts, toss winner chooses, then everyone opens the same live board.\n"
            f"⏳ <i>Expires in {CM_LOBBY_EXPIRE} seconds if unanswered.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept", callback_data=f"cm_accept_{lobby_id}_{target.id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"cm_deny_{lobby_id}_{target.id}"),
            ], [
                InlineKeyboardButton("❌ Cancel Lobby", callback_data=f"cm_cancel_{lobby_id}_{challenger.id}"),
            ]]),
        )
        context.bot_data[_cm_lobby_key(lobby_id)]["lobby_msg_id"] = msg.message_id
        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _expire_cm_lobby, CM_LOBBY_EXPIRE,
                    name=f"cm_lobby_{lobby_id}",
                    data={"lobby_id": lobby_id, "chat_id": cid, "message_id": msg.message_id},
                )
        except Exception:
            logger.exception("Failed to schedule /cm lobby expiry")
    finally:
        session.close()


async def _expire_cm_lobby(ctx):
    lobby_id = ctx.job.data["lobby_id"]
    lobby = _pop_lobby(ctx, lobby_id)
    if not lobby or lobby.get("accepted"):
        return
    try:
        await ctx.bot.edit_message_text(
            "⏰ <b>Challenge expired</b> — no response.\nStart again with /cm @username.",
            chat_id=ctx.job.data["chat_id"], message_id=ctx.job.data["message_id"],
            parse_mode="HTML")
    except Exception:
        logger.exception("/cm lobby expiry message failed")


async def challenge_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, lobby_id, invited_id = query.data.split("_")
    lobby_id = int(lobby_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby:
        await query.answer("This challenge is no longer active.", show_alert=True)
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != int(invited_id) or user.id != lobby.get("target_user_id"):
            await query.answer("Only the invited player can accept.", show_alert=True)
            return
        if lobby.get("accepted"):
            await query.answer("Challenge already accepted — toss winner must choose.", show_alert=True)
            return
        valid, errors, count = _validate_user_xi(session, user.id)
        if not valid:
            await query.answer(_xi_error(errors if errors else count), show_alert=True)
            return
        challenger = session.query(User).get(lobby["challenger_user_id"])
        if not challenger:
            _pop_lobby(context, lobby_id)
            await query.answer("The challenger no longer exists.", show_alert=True)
            return
        if (_active_cric_match_for_user(session, user.id)
                or _active_cric_match_for_user(session, challenger.id)
                or _cric_lobby_for_user(context.bot_data, user.id)
                or _cric_lobby_for_user(context.bot_data, challenger.id)
                or (_cm_user_lobby(context.bot_data, user.id) not in (None, lobby))):
            await query.answer("A challenge player already has an active match or lobby!", show_alert=True)
            return
        lobby["accepted"] = True
        winner_id = random.choice([lobby["challenger_user_id"], lobby["target_user_id"]])
        winner = session.query(User).get(winner_id)
        lobby["toss_winner_id"] = winner_id
        lobby["toss_winner_tg_id"] = winner.telegram_id
        _cancel_cm_timer(context, lobby_id)
        await query.answer("Challenge accepted!")
        await query.edit_message_text(
            "🪙 <b>CHALLENGE TOSS</b>\n"
            "═════════════════════════════\n"
            f"{_mention(winner)} won the toss. Choose your decision:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏏 Bat First", callback_data=f"cm_toss_bat_{lobby_id}_{winner_id}"),
                InlineKeyboardButton("🎳 Bowl First", callback_data=f"cm_toss_bowl_{lobby_id}_{winner_id}"),
            ]]))
    finally:
        session.close()


async def challenge_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow the /cm challenger or a chat admin to cancel a waiting lobby."""
    query = update.callback_query
    _, _, lobby_id, _challenger_id = query.data.split("_")
    lobby_id = int(lobby_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby:
        await query.answer("This challenge is no longer active.", show_alert=True)
        return
    if lobby.get("accepted"):
        await query.answer("This challenge has already reached the toss.", show_alert=True)
        return
    is_admin = False
    try:
        member = await context.bot.get_chat_member(lobby["chat_id"], query.from_user.id)
        is_admin = member.status in ("administrator", "creator")
    except Exception:
        pass
    if query.from_user.id != lobby.get("challenger_tg_id") and not is_admin:
        await query.answer("Only the challenger or a chat admin can cancel this lobby.", show_alert=True)
        return
    _pop_lobby(context, lobby_id)
    _cancel_cm_timer(context, lobby_id)
    await query.answer("Challenge cancelled.")
    await query.edit_message_text("❌ /cm challenge lobby has been cancelled.")


async def challenge_deny_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, lobby_id, invited_id = query.data.split("_")
    lobby_id = int(lobby_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby:
        await query.answer("This challenge is no longer active.", show_alert=True)
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != int(invited_id) or user.id != lobby.get("target_user_id"):
            await query.answer("Only the invited player can deny.", show_alert=True)
            return
        _pop_lobby(context, lobby_id)
        _cancel_cm_timer(context, lobby_id)
        await query.answer()
        await query.edit_message_text("❌ Challenge denied.")
    finally:
        session.close()


async def challenge_toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, decision, lobby_id, winner_id = query.data.split("_")
    lobby_id = int(lobby_id)
    winner_id = int(winner_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby or not lobby.get("accepted"):
        await query.answer("This toss is no longer active.", show_alert=True)
        return
    if decision not in ("bat", "bowl"):
        await query.answer("Invalid toss decision.", show_alert=True)
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != winner_id or winner_id != lobby.get("toss_winner_id"):
            await query.answer("Toss winner only.", show_alert=True)
            return
        if _active_cric_match_in_chat(session, lobby["chat_id"]):
            _pop_lobby(context, lobby_id)
            await query.answer("A match is already active in this chat.", show_alert=True)
            return

        challenger = session.query(User).get(lobby["challenger_user_id"])
        target = session.query(User).get(lobby["target_user_id"])
        if not challenger or not target:
            _pop_lobby(context, lobby_id)
            await query.answer("Challenge players no longer exist.", show_alert=True)
            return
        if (_active_cric_match_for_user(session, challenger.id)
                or _active_cric_match_for_user(session, target.id)):
            _pop_lobby(context, lobby_id)
            await query.answer("A challenge player is already in another active match.", show_alert=True)
            return
        opponent_id = target.id if winner_id == challenger.id else challenger.id
        settings = random_match_settings()
        match = Match(
            user1_id=challenger.id, user2_id=target.id, status="toss",
            overs=lobby["overs"], toss_winner_id=winner_id,
            toss_decision=decision,
            batting_first_id=winner_id if decision == "bat" else opponent_id,
            bowling_first_id=opponent_id if decision == "bat" else winner_id,
            stadium=settings["stadium"], pitch_type=settings["pitch_type"],
            weather=settings["weather"], temperature=settings["temperature"],
            umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=lobby["chat_id"], created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(seconds=MATCH_EXPIRE),
        )
        session.add(match)
        session.commit()

        from services.match_webapp_service import init_match_for_webapp
        ok, message = init_match_for_webapp(session, match.id, challenge_rules=True)
        if not ok:
            session.delete(match)
            session.commit()
            await query.answer(f"Failed to launch challenge: {message}", show_alert=True)
            return

        _pop_lobby(context, lobby_id)
        await query.answer()
        await query.edit_message_text(
            f"✅ {_user_label(user)} elected to {'BAT' if decision == 'bat' else 'BOWL'} FIRST.\n"
            "Opening the Challenge Mode Mini App…")
        bat_user = session.query(User).get(match.batting_first_id)
        bowl_user = session.query(User).get(match.bowling_first_id)
        bat_team = bat_user.team_name or f"@{bat_user.username}'s XI"
        bowl_team = bowl_user.team_name or f"@{bowl_user.username}'s XI"
        from services.match_broadcast import send_match_ready_message
        await send_match_ready_message(
            context, lobby["chat_id"], match, bat_team, bowl_team,
            _mention(bat_user), _mention(bowl_user),
            rules_note="Challenge Mode · 2 wickets per innings")
    except Exception:
        session.rollback()
        logger.exception("/cm toss decision failed")
        await query.answer("Failed to launch challenge match.", show_alert=True)
    finally:
        session.close()


# Legacy callback kept for safety if old inline buttons are still delivered.
async def challenge_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "This /cm challenge now opens in the Mini App after the toss. Start a fresh /cm if needed.",
        show_alert=True)
