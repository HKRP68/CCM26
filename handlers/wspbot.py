"""/wspbot — auto-simulated cricket match against a bot team (watch mode).

Like /wsp but solo: the user's team faces a configured bot team and the
engine drives every ball automatically. The user watches via the Mini App
and receives updates + scorecard in the Telegram chat.

Flow:
  1. /wspbot [overs]    → list valid bot teams
  2. User picks a team  → animated toss
  3. (If user wins) choose Bat/Bowl; if bot wins, bot picks automatically
  4. Match auto-simulates immediately.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Match, BotTeam, BotTeamPlayer, Player
from services.bot_ai import BOT_TG_ID
from services.button_timeout import schedule_button_timeout
from handlers.vsbot import _get_or_create_bot_user, _build_bot_team_xi, _pitch_hint_vsbot

logger = logging.getLogger(__name__)

WSPBOT_INVITE_TIMEOUT = 120
WSP_MAX_OVERS = 5


# ══════════════════════════════════════════════════════════════════════
# /wspbot command
# ══════════════════════════════════════════════════════════════════════

async def wspbot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    cid = update.effective_chat.id

    overs = 1
    if context.args:
        try:
            overs = int(context.args[0])
        except (ValueError, IndexError):
            await update.message.reply_text(
                f"ℹ️ <b>Usage:</b> <code>/wspbot &lt;overs (1-{WSP_MAX_OVERS})&gt;</code> "
                "to simulate a bot match.",
                parse_mode="HTML")
            return
    if overs < 1 or overs > WSP_MAX_OVERS:
        await update.message.reply_text(f"❌ Overs must be 1-{WSP_MAX_OVERS}.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Use /debut first!")
            return

        from handlers.match import _active_cric_match_in_chat, _chat_busy_message
        existing = _active_cric_match_in_chat(session, cid)
        if existing:
            await update.message.reply_text(
                _chat_busy_message(existing), parse_mode="HTML")
            return

        from models import UserRoster
        roster_count = session.query(UserRoster).filter(
            UserRoster.user_id == user.id).count()
        if roster_count < 11:
            await update.message.reply_text(
                f"❌ You need at least 11 players in your roster (have {roster_count}).")
            return

        from handlers.lineup import validate_xi, _get_ordered_roster
        valid, errs = validate_xi(_get_ordered_roster(session, user.id))
        if not valid:
            await update.message.reply_text(
                "❌ <b>Your top-11 XI is invalid:</b>\n\n"
                + "\n".join(f"• {e}" for e in errs)
                + "\n\nUse /pxi to fix lineup.", parse_mode="HTML")
            return

        active_teams = (session.query(BotTeam)
                        .filter(BotTeam.is_active == True)
                        .order_by(BotTeam.difficulty, BotTeam.name).all())
        from services.bot_team_service import validate_team_xi
        valid_teams = [t for t in active_teams if validate_team_xi(session, t.id)[0]]
        if not valid_teams:
            await update.message.reply_text(
                "❌ No valid bot teams available right now.")
            return

        btns = []
        for t in valid_teams:
            ratings = (session.query(Player.rating)
                       .join(BotTeamPlayer, BotTeamPlayer.player_id == Player.id)
                       .filter(BotTeamPlayer.bot_team_id == t.id).all())
            avg = round(sum(r[0] for r in ratings) / len(ratings), 1) if ratings else 0
            label = f"{t.name} ({t.difficulty}) — Avg {avg}"
            btns.append([InlineKeyboardButton(
                label, callback_data=f"wspb_pick_{tg.id}_{t.id}_{overs}")])
        btns.append([InlineKeyboardButton(
            "❌ Cancel", callback_data=f"wspb_cancel_{tg.id}")])

        sent = await update.message.reply_text(
            f"🤖👁 <b>WSP vs BOT — {overs} OVER MATCH</b>\n\n"
            "Auto-simulated match against a bot team.\n"
            "Choose your opponent:\n",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                    delay_seconds=WSPBOT_INVITE_TIMEOUT)
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("wspbot_handler error")
        await update.message.reply_text("⚠️ Error starting /wspbot.")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
# Pick bot team → animated toss
# ══════════════════════════════════════════════════════════════════════

async def wspbot_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """wspb_pick_<owner_tg>_<bot_team_id>_<overs>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2]); team_id = int(parts[3]); overs = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your match!", show_alert=True)
        return
    await q.answer()

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.edit_message_text("❌ Use /debut first!")
            return

        bot_team = session.query(BotTeam).get(team_id)
        if not bot_team or not bot_team.is_active:
            await q.edit_message_text("❌ Bot team unavailable.")
            return

        from handlers.match import _active_cric_match_in_chat, _chat_busy_message
        existing = _active_cric_match_in_chat(session, q.message.chat_id)
        if existing:
            await q.edit_message_text(_chat_busy_message(existing), parse_mode="HTML")
            return

        bot_user = _get_or_create_bot_user(session)
        bot_user.team_name = bot_team.name
        session.commit()

        from services.match_constants import random_match_settings
        st = random_match_settings()
        now = datetime.utcnow()
        m = Match(
            user1_id=user.id, user2_id=bot_user.id,
            status="toss", stadium=st["stadium"],
            pitch_type=st["pitch_type"], weather=st["weather"],
            temperature=st["temperature"],
            umpire1=st["umpire1"], umpire2=st["umpire2"],
            chat_id=q.message.chat_id, created_at=now,
            expires_at=now + timedelta(minutes=30), overs=overs,
        )
        session.add(m)
        session.commit()

        context.bot_data[f"wspb_team_{m.id}"] = bot_team.id
        context.bot_data[f"wspb_overs_{m.id}"] = overs

        wid = random.choice([user.id, bot_user.id])
        m.toss_winner_id = wid
        session.commit()

        # Animated coin toss
        try:
            await q.edit_message_text(
                "🪙 <b>TOSS</b>\n\n<i>Calling captain to the centre...</i>",
                parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(0.6)
        for frame in (
            "🪙 <b>TOSS</b>\n\n     ⬆️\n   ╱  🪙  ╲\n\n<i>Captain flicks the coin...</i>",
            "🪙 <b>TOSS</b>\n\n     🌀 🪙 🌀\n\n<i>Tumbling end over end...</i>",
            "🪙 <b>TOSS</b>\n\n          ⬇️\n        🪙\n\n<i>Coming down now!</i>",
        ):
            try:
                await q.edit_message_text(frame, parse_mode="HTML")
            except Exception:
                pass
            await asyncio.sleep(0.5)

        if wid == bot_user.id:
            try:
                await q.edit_message_text(
                    f"🪙 <b>TOSS RESULT</b>\n\n🏆 <b>{bot_team.name}</b> wins the toss!",
                    parse_mode="HTML")
            except Exception:
                pass
            await asyncio.sleep(0.5)
            bot_decision = "bowl" if random.random() < 0.6 else "bat"
            await _wspbot_apply_toss(context, q.message.chat_id, m.id,
                                     bot_decision, bot_user.id)
            return

        from handlers.match import _mention as _mm
        user_mention = _mm(user)
        try:
            await q.edit_message_text(
                f"🪙 <b>TOSS RESULT</b>\n\n"
                f"🏆 {user_mention} wins the toss vs <b>{bot_team.name}</b>!\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📍 Pitch: <b>{st['pitch_type']}</b> · 🌤️ {st['weather']}\n"
                f"🏟️ {st['stadium']}\n\n"
                f"<i>{_pitch_hint_vsbot(st['pitch_type'])}</i>",
                parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(0.4)

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏏 Bat First", callback_data=f"wspb_toss_bat_{m.id}_{user.id}"),
            InlineKeyboardButton("🎳 Bowl First", callback_data=f"wspb_toss_bowl_{m.id}_{user.id}"),
        ]])
        sent = await context.bot.send_message(
            q.message.chat_id, "⚖️ Choose your call:", reply_markup=kb)
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                    delay_seconds=120)
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("wspbot_pick_callback error")
        try:
            await q.edit_message_text("⚠️ Error setting up WSP bot match.")
        except Exception:
            pass
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
# Cancel
# ══════════════════════════════════════════════════════════════════════

async def wspbot_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """wspb_cancel_<owner_tg>"""
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your match!", show_alert=True)
        return
    await q.answer("Cancelled")
    try:
        await q.edit_message_text("🤖 <i>WSP vs Bot match cancelled.</i>", parse_mode="HTML")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════
# User picks bat/bowl after winning toss
# ══════════════════════════════════════════════════════════════════════

async def wspbot_toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """wspb_toss_(bat|bowl)_<match_id>_<user_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        decision = parts[2]; mid = int(parts[3]); uid = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if decision not in ("bat", "bowl"):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user or user.id != uid:
            await q.answer("Toss winner only!", show_alert=True)
            return
        await q.answer()
        await _wspbot_apply_toss(context, q.message.chat_id, mid,
                                 decision, user.id, q=q)
    except Exception:
        logger.exception("wspbot_toss_callback error")
    finally:
        session.close()


# ══════════════════════════════════════════════════════════════════════
# Internal: apply toss → init WSP match → launch simulation
# ══════════════════════════════════════════════════════════════════════

async def _wspbot_apply_toss(context, chat_id, mid, decision, decider_uid, q=None):
    """Apply toss decision, initialize the WSP match, launch auto-simulation."""
    session = get_session()
    try:
        m = session.query(Match).get(mid)
        if not m:
            return
        bot_user = session.query(User).filter(User.telegram_id == BOT_TG_ID).first()
        user = session.query(User).get(m.user1_id)

        m.toss_decision = decision
        if decision == "bat":
            m.batting_first_id = decider_uid
            m.bowling_first_id = bot_user.id if decider_uid == user.id else user.id
        else:
            m.bowling_first_id = decider_uid
            m.batting_first_id = bot_user.id if decider_uid == user.id else user.id

        bot_team_id = context.bot_data.get(f"wspb_team_{mid}")
        bot_xi = _build_bot_team_xi(session, bot_team_id) if bot_team_id else []
        if not bot_xi:
            err_msg = "❌ Bot team unavailable."
            if q:
                try:
                    await q.edit_message_text(err_msg)
                except Exception:
                    pass
            else:
                await context.bot.send_message(chat_id, err_msg)
            return

        bot_team = session.query(BotTeam).get(bot_team_id) if bot_team_id else None
        user_team_name = user.team_name or f"@{user.username}'s XI"
        bot_team_name = bot_user.team_name or "Bot XI"
        bat_uid = m.batting_first_id
        if bat_uid == user.id:
            bat_team_name, bowl_team_name = user_team_name, bot_team_name
        else:
            bat_team_name, bowl_team_name = bot_team_name, user_team_name

        m.status = "playing"
        session.commit()

        winner = session.query(User).get(decider_uid)
        winner_name = (winner.first_name or winner.username or "Bot") if winner else "Bot"
        result_text = (
            f"🪙 <b>TOSS RESULT</b>\n\n"
            f"<b>{winner_name}</b> won the toss\n"
            f"and elected to <b>{'BAT' if decision == 'bat' else 'BOWL'} FIRST</b>")
        if q:
            try:
                await q.edit_message_text(result_text, parse_mode="HTML")
            except Exception:
                pass
        else:
            await context.bot.send_message(chat_id, result_text, parse_mode="HTML")

        from services.match_webapp_service import init_match_for_wsp
        ok, msg = init_match_for_wsp(
            session, mid, xi_overrides={bot_user.id: bot_xi})
        if not ok:
            logger.error("wspbot init_match_for_wsp failed: %s", msg)
            await context.bot.send_message(chat_id, f"❌ Failed to launch match: {msg}")
            return

        # Post match-ready card with spectate button
        try:
            from services.match_broadcast import send_match_ready_message
            from handlers.match import _mention as _mm
            bat_mention = "🤖 AI" if m.batting_first_id == bot_user.id else _mm(user)
            bowl_mention = "🤖 AI" if m.bowling_first_id == bot_user.id else _mm(user)
            await send_match_ready_message(
                context, chat_id, m, bat_team_name, bowl_team_name,
                bat_mention, bowl_mention,
                rules_note="Auto-simulated — watch live in the Mini App")
        except Exception:
            logger.exception("wspbot match-ready message failed")

        # Launch auto-simulation
        from services.wsp_autoplay import run_wsp_simulation
        task = asyncio.create_task(
            run_wsp_simulation(context.application, mid, chat_id))
        context.bot_data[f"wsp_task_{mid}"] = task

    except Exception:
        session.rollback()
        logger.exception("_wspbot_apply_toss error")
    finally:
        session.close()
