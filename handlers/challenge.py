"""Two-player challenge mode using the Mini App match flow."""

import logging
import random
import re
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import FantasyLeague, Match, User
from services.match_constants import MATCH_EXPIRE, random_match_settings
from services.telegram_user_service import resolve_command_target, sync_telegram_user
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

CHALLENGE_REPLY_REQUIRED_MESSAGE = "Please reply to a user’s message to challenge them."
BUILT_IN_CHALLENGE_LEAGUES = {
    "ipl": "IPL",
    "bbl": "BBL",
    "int": "INT",
}


def normalize_challenge_league(value):
    """Return a command-safe league key using only letters and digits."""
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _league_display_from_key(league_key, known_leagues=None):
    if known_leagues and league_key in known_leagues:
        return known_leagues[league_key]
    return BUILT_IN_CHALLENGE_LEAGUES.get(league_key, league_key.upper())


def _challenge_command_name(update):
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    text = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    if not text.startswith("/"):
        return ""
    token = text.split(maxsplit=1)[0][1:]
    return token.split("@", 1)[0].lower()


def _league_key_from_command(command_name):
    command = (command_name or "").lower()
    if command.startswith("challenge") and len(command) > len("challenge"):
        return normalize_challenge_league(command[len("challenge"):])
    if command.startswith("c") and len(command) > 1:
        return normalize_challenge_league(command[1:])
    return None


def _challenge_leagues(session):
    leagues = dict(BUILT_IN_CHALLENGE_LEAGUES)
    try:
        for name, in session.query(FantasyLeague.name).all():
            key = normalize_challenge_league(name)
            if key:
                leagues[key] = name.strip()
    except Exception:
        logger.exception("Failed to load dynamic challenge leagues")
    return leagues


def is_challenge_league_command(command_name, session):
    """Return ``(league_key, display_name)`` for supported challenge league commands."""
    league_key = _league_key_from_command(command_name)
    if not league_key:
        return None, None
    leagues = _challenge_leagues(session)
    if league_key not in leagues:
        return None, None
    return league_key, _league_display_from_key(league_key, leagues)


def _reply_target_telegram_user(update):
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    reply = getattr(message, "reply_to_message", None) if message is not None else None
    return getattr(reply, "from_user", None) if reply is not None else None



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


async def _start_challenge_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                 target: User, league_key=None, league_name=None):
    """Create a targeted challenge lobby once host/guest validation has passed."""
    cid = update.effective_chat.id
    session = get_session()
    try:
        challenger = sync_telegram_user(session, update.effective_user)
        if not challenger:
            await update.message.reply_text("❌ Use /debut first.")
            return
        target = session.merge(target)
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
            await update.message.reply_text("⚠️ There is already a challenge waiting in this chat!")
            return

        league_key = normalize_challenge_league(league_key) if league_key else None
        league_name = (league_name or _league_display_from_key(league_key) if league_key else "Challenge Mode")
        lobby_title = f"{league_name} CHALLENGE MODE" if league_key else "CHALLENGE MODE LOBBY"
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
            "league_key": league_key,
            "league_name": league_name,
            "overs": min(_max_overs(session), 2),
            "created_at": datetime.utcnow().isoformat(),
        }
        context.bot_data[_cm_chat_key(cid)] = lobby_id
        msg = await update.message.reply_text(
            f"⚔️ <b>{lobby_title}</b>\n"
            "═════════════════════════════\n"
            f"• <b>Host:</b> {_user_label(challenger)}\n"
            f"• <b>Guest:</b> {_user_label(target)}\n"
            f"• <b>Rules:</b> 2 wickets per innings · up to {min(_max_overs(session), 2)} over(s)\n"
            "• <b>Flow:</b> fast /wpm-style Mini App gameplay with live spectating\n\n"
            "The guest accepts, toss winner chooses, then everyone opens the same live board.\n"
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
            logger.exception("Failed to schedule challenge lobby expiry")
    finally:
        session.close()


async def challenge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a targeted /cm lobby, preserving legacy mention/reply targeting."""
    session = get_session()
    try:
        challenger = sync_telegram_user(session, update.effective_user)
        if not challenger:
            await update.message.reply_text("❌ Use /debut first.")
            return
        target, target_source = resolve_command_target(session, update, context, "cm")
        if not target:
            if target_source == "missing":
                await update.message.reply_text(
                    "Usage: <code>/cm @username</code>\n"
                    "Tip: for users without @username, reply to their message and run /cm.",
                    parse_mode="HTML")
            elif target_source == "not_mention":
                await update.message.reply_text(
                    "❌ Please reply to the user's message or use a real @username mention.",
                    parse_mode="HTML")
            else:
                await update.message.reply_text(
                    "❌ User not found. They need to use /debut first; if they changed or "
                    "don't have a username, reply to their message and run /cm.")
            return
        await _start_challenge_lobby(update, context, target)
    finally:
        session.close()


async def challenge_league_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start built-in or admin-created league challenge commands from replies."""
    command_name = _challenge_command_name(update)
    session = get_session()
    try:
        league_key, league_name = is_challenge_league_command(command_name, session)
        if not league_key:
            return

        target_tg = _reply_target_telegram_user(update)
        if not target_tg:
            await update.message.reply_text(CHALLENGE_REPLY_REQUIRED_MESSAGE)
            return
        if getattr(target_tg, "is_bot", False):
            await update.message.reply_text("❌ Bot accounts cannot be challenged.")
            return
        if update.effective_user and target_tg.id == update.effective_user.id:
            await update.message.reply_text("❌ You cannot challenge yourself.")
            return

        target = sync_telegram_user(session, target_tg)
        if not target:
            await update.message.reply_text("❌ User not found. They need to use /debut first.")
            return
        await _start_challenge_lobby(update, context, target, league_key, league_name)
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
        # The invited player (acceptor) calls the toss.
        lobby["caller_user_id"] = user.id
        lobby["caller_tg_id"] = user.telegram_id
        _cancel_cm_timer(context, lobby_id)
        await query.answer("Challenge accepted!")
        from services.match_broadcast import coin_call_keyboard
        await query.edit_message_text(
            "🪙 <b>CHALLENGE TOSS</b>\n"
            "═════════════════════════════\n"
            f"{_mention(user)}, call it in the air!\n"
            "<b>Heads</b> or <b>Tails?</b>",
            parse_mode="HTML", reply_markup=coin_call_keyboard(
                f"cm_coin_heads_{lobby_id}_{user.id}",
                f"cm_coin_tails_{lobby_id}_{user.id}"))
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


async def challenge_coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cm_coin_(heads|tails)_<lobby_id>_<caller_uid> — invited player calls the
    toss; the coin is flipped and the winner then chooses bat or bowl."""
    query = update.callback_query
    _, _, call, lobby_id, caller_id = query.data.split("_")
    lobby_id = int(lobby_id)
    caller_id = int(caller_id)
    lobby = context.bot_data.get(_cm_lobby_key(lobby_id))
    if not lobby or not lobby.get("accepted"):
        await query.answer("This toss is no longer active.", show_alert=True)
        return
    if call not in ("heads", "tails"):
        await query.answer("Invalid call.", show_alert=True)
        return
    if lobby.get("toss_winner_id"):
        await query.answer("Toss already done — pick bat or bowl.", show_alert=True)
        return
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != caller_id or user.id != lobby.get("caller_user_id"):
            await query.answer("Only the calling player can toss!", show_alert=True)
            return
        await query.answer()
        from services.match_broadcast import run_coin_toss
        coin, won = await run_coin_toss(
            lambda t: query.edit_message_text(t, parse_mode="HTML"), call)

        challenger = session.query(User).get(lobby["challenger_user_id"])
        target = session.query(User).get(lobby["target_user_id"])
        if not challenger or not target:
            _pop_lobby(context, lobby_id)
            await query.edit_message_text("Challenge players no longer exist.")
            return
        # The target called; they win if the coin matches their call.
        winner = target if won else challenger
        lobby["toss_winner_id"] = winner.id
        lobby["toss_winner_tg_id"] = winner.telegram_id
        await query.edit_message_text(
            "🪙 <b>CHALLENGE TOSS</b>\n"
            "═════════════════════════════\n"
            f"The coin lands on <b>{coin.upper()}</b> — "
            f"{_mention(target)} called <b>{call.upper()}</b>.\n\n"
            f"🏆 {_mention(winner)} won the toss. Choose your decision:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏏 Bat First", callback_data=f"cm_toss_bat_{lobby_id}_{winner.id}"),
                InlineKeyboardButton("🎳 Bowl First", callback_data=f"cm_toss_bowl_{lobby_id}_{winner.id}"),
            ]]))
    except Exception:
        logger.exception("/cm coin toss failed")
        await query.answer("Toss failed — start again with /cm.", show_alert=True)
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
        toss_note = (f"{_user_label(user)} won & chose to "
                     f"{'bat' if decision == 'bat' else 'bowl'}")
        from services.match_broadcast import send_match_ready_message
        await send_match_ready_message(
            context, lobby["chat_id"], match, bat_team, bowl_team,
            _mention(bat_user), _mention(bowl_user),
            rules_note="Challenge Mode · 2 wickets per innings",
            toss_note=toss_note)
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
