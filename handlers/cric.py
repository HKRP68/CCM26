"""/cric — open group cricket-match lobby that launches the WebApp board.

This mirrors the reference UnderCover bot's `/cric` workflow exactly:

    /cric <overs>           Host opens an OPEN lobby in the group
       │                    (anyone can join, not a targeted challenge)
       ▼
    🤝 Join Match           First other user to tap joins as guest
       │
       ▼
    🪙 Auto toss            Random winner gets Bat First / Bowl First
       │
       ▼
    🏏 Match ready          Creates the match and posts the SAME
                            "🎮 Play Match" WebApp button that the rest of
                            the bot already uses — both players then play
                            inside the /cricket Mini App.

Unlike /playmatch (which challenges one named @user and lets the global
"match style" admin setting decide telegram-vs-webapp), /cric is the Mini App
game: it always launches the WebApp board, just like the reference.

The lobby itself is kept in-memory in ``context.bot_data`` (keyed by chat id),
exactly like the reference's ``activeLobbies`` map — nothing is written to the
DB until the toss decision is made and a real Match row is created.
"""

import random
import logging
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, Match
from services.match_constants import random_match_settings, MATCH_EXPIRE

logger = logging.getLogger(__name__)

# Overs range for an open lobby (reference uses 1-5, default 1).
CRIC_MIN_OVERS = 1
CRIC_MAX_OVERS = 5
CRIC_DEFAULT_OVERS = 1

# Statuses that mean a match is still "live" (blocks a new lobby in the chat).
_LIVE_MATCH_STATUSES = (
    "pending", "accepted", "toss", "selecting", "playing", "active",
)


def _lobby_key(chat_id):
    return f"cric_lobby_{chat_id}"


def _get_lobby(context, chat_id):
    return context.bot_data.get(_lobby_key(chat_id))


def _user_in_any_lobby(context, tg_id):
    """True if this telegram user is host/guest of any open lobby anywhere."""
    for k, lob in context.bot_data.items():
        if not isinstance(k, str) or not k.startswith("cric_lobby_"):
            continue
        if not isinstance(lob, dict):
            continue
        if lob.get("host_tg") == tg_id or lob.get("guest_tg") == tg_id:
            return True
    return False


def _user_in_live_match(session, user_db_id):
    """True if this user is in a not-yet-finished match in any chat."""
    if not user_db_id:
        return False
    from sqlalchemy import or_
    return (session.query(Match)
            .filter(or_(Match.user1_id == user_db_id,
                        Match.user2_id == user_db_id),
                    Match.status.in_(_LIVE_MATCH_STATUSES))
            .first()) is not None


def _chat_has_live_match(session, chat_id):
    if not chat_id:
        return False
    return (session.query(Match)
            .filter(Match.chat_id == chat_id,
                    Match.status.in_(_LIVE_MATCH_STATUSES))
            .first()) is not None


def _validate_user_xi(session, user_db_id):
    """Reuse the same XI validation /playmatch uses. Returns (ok, errors)."""
    from handlers.lineup import validate_xi, _get_ordered_roster
    roster = _get_ordered_roster(session, user_db_id)
    if len(roster) < 11:
        return False, [f"You need 11+ players in your roster (have {len(roster)})."]
    return validate_xi(roster)


# ════════════════════════════ /cric ══════════════════════════════════

async def cric_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    cid = update.effective_chat.id

    # Parse overs (default 1, range 1-5) — reference parity.
    overs = CRIC_DEFAULT_OVERS
    if context.args:
        try:
            overs = int(context.args[0])
        except (ValueError, IndexError):
            await update.message.reply_text(
                f"ℹ️ <b>Usage:</b> <code>/cric &lt;overs ({CRIC_MIN_OVERS}-{CRIC_MAX_OVERS})&gt;</code> "
                f"to start a match lobby.", parse_mode="HTML")
            return
    if overs < CRIC_MIN_OVERS or overs > CRIC_MAX_OVERS:
        await update.message.reply_text(
            f"ℹ️ <b>Usage:</b> <code>/cric &lt;overs ({CRIC_MIN_OVERS}-{CRIC_MAX_OVERS})&gt;</code> "
            f"to start a match lobby.", parse_mode="HTML")
        return

    session = get_session()
    try:
        host = session.query(User).filter(User.telegram_id == tg.id).first()
        if not host:
            await update.message.reply_text("❌ Use /debut first to create your team!")
            return

        # One lobby per chat; one match/lobby per user.
        if _get_lobby(context, cid):
            await update.message.reply_text(
                "⚠️ There is already a match lobby waiting to be joined in this chat!")
            return
        if _chat_has_live_match(session, cid):
            await update.message.reply_text(
                "⚠️ A match is already going on in this chat. Wait for it to finish.")
            return
        if _user_in_any_lobby(context, tg.id) or _user_in_live_match(session, host.id):
            await update.message.reply_text(
                "⚠️ You already have an active match or lobby running!")
            return

        # Validate the host's Playing XI up-front (just like the reference).
        ok, errs = _validate_user_xi(session, host.id)
        if not ok:
            await update.message.reply_text(
                "❌ <b>Lobby Creation Failed — your XI is invalid:</b>\n"
                + "\n".join(f"• {e}" for e in errs),
                parse_mode="HTML")
            return

        host_name = tg.username or tg.first_name or "Host"
        context.bot_data[_lobby_key(cid)] = {
            "chat_id": cid,
            "host_tg": tg.id,
            "host_uid": host.id,
            "host_name": host_name,
            "overs": overs,
            "guest_tg": None,
            "status": "waiting_join",
            "created_at": datetime.utcnow(),
        }

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🤝 Join Match", callback_data="cric_join"),
            InlineKeyboardButton("❌ Cancel Lobby", callback_data="cric_cancel"),
        ]])
        await update.message.reply_text(
            "🏏 <b>CRICKET MATCH LOBBY CREATED!</b> 🏏\n"
            "═════════════════════════════\n"
            f"• <b>Host:</b> @{host_name}\n"
            f"• <b>Length:</b> {overs} Over(s)\n\n"
            "Click the button below to join the match!",
            parse_mode="HTML", reply_markup=kb)
    except Exception:
        session.rollback()
        logger.exception("cric lobby creation error")
        await update.message.reply_text("❌ Failed to create lobby.")
    finally:
        session.close()


# ═══════════════════════════ Cancel ══════════════════════════════════

async def cric_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    cid = q.message.chat_id
    lobby = _get_lobby(context, cid)
    if not lobby:
        await q.answer("❌ No active lobby in this chat.", show_alert=True)
        return

    # Host can always cancel; group admins may too.
    is_host = lobby.get("host_tg") == q.from_user.id
    is_admin = False
    if not is_host:
        try:
            member = await context.bot.get_chat_member(cid, q.from_user.id)
            is_admin = member.status in ("administrator", "creator")
        except Exception:
            is_admin = False
    if not is_host and not is_admin:
        await q.answer("❌ Only the host or a group admin can cancel this lobby.",
                       show_alert=True)
        return

    context.bot_data.pop(_lobby_key(cid), None)
    await q.answer("Lobby cancelled.")
    try:
        await q.edit_message_text("❌ Match lobby has been cancelled.")
    except Exception:
        pass


# ════════════════════════════ Join ═══════════════════════════════════

async def cric_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    cid = q.message.chat_id

    lobby = _get_lobby(context, cid)
    if not lobby:
        await q.answer("❌ No active lobby in this chat.", show_alert=True)
        return
    if lobby.get("status") != "waiting_join":
        await q.answer("❌ This lobby is no longer open.", show_alert=True)
        return
    if lobby.get("host_tg") == tg.id:
        await q.answer("❌ You cannot join your own lobby!", show_alert=True)
        return
    if lobby.get("guest_tg"):
        await q.answer("❌ Lobby is already full.", show_alert=True)
        return

    session = get_session()
    try:
        guest = session.query(User).filter(User.telegram_id == tg.id).first()
        if not guest:
            await q.answer("❌ Use /debut first to create your team!", show_alert=True)
            return
        if _user_in_any_lobby(context, tg.id) or _user_in_live_match(session, guest.id):
            await q.answer("❌ You are already in an active match or lobby!",
                           show_alert=True)
            return

        ok, errs = _validate_user_xi(session, guest.id)
        if not ok:
            msg = "❌ Join Failed — your XI is invalid:\n" + "\n".join(f"• {e}" for e in errs)
            await q.answer(msg[:200], show_alert=True)
            return

        host = session.query(User).get(lobby["host_uid"])
        if not host:
            context.bot_data.pop(_lobby_key(cid), None)
            await q.answer("❌ Host no longer available.", show_alert=True)
            return

        guest_name = tg.username or tg.first_name or "Guest"
        lobby["guest_tg"] = tg.id
        lobby["guest_uid"] = guest.id
        lobby["guest_name"] = guest_name
        lobby["status"] = "toss_decision"

        # Random toss winner (store the telegram id of whoever won).
        toss_winner_tg = random.choice([lobby["host_tg"], lobby["guest_tg"]])
        toss_winner_name = (lobby["host_name"] if toss_winner_tg == lobby["host_tg"]
                            else guest_name)
        lobby["toss_winner_tg"] = toss_winner_tg
        lobby["toss_winner_name"] = toss_winner_name

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏏 Bat First", callback_data="cricdec_bat"),
            InlineKeyboardButton("🎳 Bowl First", callback_data="cricdec_bowl"),
        ]])
        await q.answer("✅ Joined match lobby!")
        await q.edit_message_text(
            "🪙 <b>TOSS COMPLETED!</b> 🪙\n"
            "═════════════════════════════\n"
            f"• Host: @{lobby['host_name']}\n"
            f"• Guest: @{guest_name}\n\n"
            f"🎉 <b>@{toss_winner_name}</b> won the toss!\n"
            "Choose your decision:",
            parse_mode="HTML", reply_markup=kb)
    except Exception:
        session.rollback()
        logger.exception("cric join error")
        await q.answer("❌ Error joining lobby.", show_alert=True)
    finally:
        session.close()


# ═════════════════════════ Toss decision ═════════════════════════════

async def cric_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    cid = q.message.chat_id
    decision = q.data.split("_", 1)[1]  # "bat" | "bowl"

    lobby = _get_lobby(context, cid)
    if not lobby or lobby.get("status") != "toss_decision":
        await q.answer("❌ No lobby awaiting a toss decision here.", show_alert=True)
        return
    if tg.id != lobby.get("toss_winner_tg"):
        await q.answer("⚠️ Only the toss winner can make the decision!", show_alert=True)
        return

    await q.answer()
    session = get_session()
    try:
        host = session.query(User).get(lobby["host_uid"])
        guest = session.query(User).get(lobby["guest_uid"])
        if not host or not guest:
            context.bot_data.pop(_lobby_key(cid), None)
            await q.edit_message_text("❌ A player is no longer available. Lobby closed.")
            return

        # Decide batting / bowling sides from the toss winner's choice.
        winner_uid = host.id if lobby["toss_winner_tg"] == lobby["host_tg"] else guest.id
        loser_uid = guest.id if winner_uid == host.id else host.id
        if decision == "bat":
            batting_uid, bowling_uid = winner_uid, loser_uid
        else:
            batting_uid, bowling_uid = loser_uid, winner_uid

        # Create the real Match row that the existing engine understands.
        st = random_match_settings()
        now = datetime.utcnow()
        m = Match(
            user1_id=host.id, user2_id=guest.id,
            overs=lobby["overs"], status="playing",
            toss_winner_id=winner_uid, toss_decision=decision,
            batting_first_id=batting_uid, bowling_first_id=bowling_uid,
            stadium=st["stadium"], pitch_type=st["pitch_type"], weather=st["weather"],
            temperature=st["temperature"], umpire1=st["umpire1"], umpire2=st["umpire2"],
            chat_id=cid, created_at=now,
            expires_at=now + timedelta(seconds=MATCH_EXPIRE),
        )
        session.add(m)
        session.commit()
        mid = m.id

        # Lobby's job is done.
        context.bot_data.pop(_lobby_key(cid), None)

        try:
            winner_name = lobby.get("toss_winner_name") or (tg.username or tg.first_name or "Player")
            await q.edit_message_text(
                f"✅ @{winner_name} elected to "
                f"<b>{'BAT' if decision == 'bat' else 'BOWL'} FIRST</b>",
                parse_mode="HTML")
        except Exception:
            pass

        # Initialise the WebApp board state, then post the SAME "Play Match"
        # button the rest of the bot uses. This is the Mini App game.
        bu = session.query(User).get(batting_uid)
        bwu = session.query(User).get(bowling_uid)
        bt = bu.team_name or f"@{bu.username}'s XI"
        bwt = bwu.team_name or f"@{bwu.username}'s XI"

        from services.match_webapp_service import init_match_for_webapp
        ok, msg = init_match_for_webapp(session, mid)
        if not ok:
            logger.error("cric: init_match_for_webapp failed: %s", msg)
            m.status = "abandoned"
            session.commit()
            await context.bot.send_message(
                cid, "❌ Could not start the match board. Please try /cric again.")
            return

        from handlers.match import _mention
        from services.match_broadcast import send_match_ready_message
        await send_match_ready_message(
            context, cid, m, bt, bwt, _mention(bu), _mention(bwu))
    except Exception:
        session.rollback()
        logger.exception("cric decision error")
        await context.bot.send_message(cid, "❌ Error starting match.")
    finally:
        session.close()
