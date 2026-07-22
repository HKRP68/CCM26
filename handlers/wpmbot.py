"""/wpmbot — play a match against a pre-built bot team, INSIDE the Mini App.

This is `/vsbot` (human vs an AI bot team) but the game is played in the
Crickidex Arena Mini App like `/wpm`, instead of the Telegram ball-by-ball
flow. The human plays their own side in the Mini App; the bot side is driven
automatically by the server-side AI (services.match_webapp_service.
auto_play_bot_turns). The human can also flip the in-app "autoplay" toggle to
hand their own side to the AI too — for a fully automatic match.

Flow:
  1. /wpmbot [overs]  (1-20, like /wpm; default 1)
  2. Validate roster / XI, list active bot teams as buttons.
  3. User picks a team → animated toss → bot or user chooses bat/bowl.
  4. The match is initialized for the Mini App and the "Play Match" card is
     posted. From there everything happens in the Mini App.

Reuses the pure /vsbot helpers (bot user, bot XI) and the existing Mini App
plumbing (init_match_for_webapp auto-detects the bot user → is_vsbot).
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, UserRoster, Match
from services.bot_ai import BOT_TG_ID
from services.button_timeout import schedule_button_timeout
from services.match_broadcast import coin_call_keyboard, run_coin_toss
from handlers.vsbot import (
    _get_or_create_bot_user, build_adaptive_bot_xi, _pitch_hint_vsbot,
    ADAPTIVE_AI_DIFFICULTY,
)

# Name shown for the auto-built adaptive opponent.
ADAPTIVE_BOT_TEAM_NAME = "Challenger XI"

logger = logging.getLogger(__name__)

WPMBOT_INVITE_TIMEOUT = 120
# Longest bot match the Mini App supports — full T20 length, matching /wpm.
WPMBOT_MAX_OVERS = 20


# ════════════════════════════════════════════════════════════════════
# /wpmbot command
# ════════════════════════════════════════════════════════════════════

async def wpmbot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    cid = update.effective_chat.id

    overs = 1  # default, matching /wpm
    if context.args:
        try:
            overs = int(context.args[0])
        except (ValueError, IndexError):
            await update.message.reply_text(
                f"ℹ️ <b>Usage:</b> <code>/wpmbot &lt;overs (1-{WPMBOT_MAX_OVERS})&gt;</code> "
                "to play the bot in the Mini App.", parse_mode="HTML")
            return
    if overs < 1 or overs > WPMBOT_MAX_OVERS:
        await update.message.reply_text(f"❌ Overs must be 1-{WPMBOT_MAX_OVERS}.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        from services import subscription_service
        if not subscription_service.has_premium_commands(user):
            await update.message.reply_text(
                subscription_service.premium_required_message("/wpmbot"),
                parse_mode="HTML")
            return

        from handlers.match import (
            _active_match_in_chat, _active_match_for_user,
            _chat_busy_message, _user_busy_message,
        )
        existing = _active_match_in_chat(session, cid)
        if existing:
            await update.message.reply_text(
                _chat_busy_message(existing), parse_mode="HTML")
            return

        # One match per player (any game mode)
        busy = _active_match_for_user(session, user.id)
        if busy:
            await update.message.reply_text(
                _user_busy_message(busy), parse_mode="HTML",
                disable_web_page_preview=True)
            return

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
                "❌ <b>Your top-11 XI is invalid:</b>\n\n" +
                "\n".join(f"• {e}" for e in errs) +
                "\n\nUse /pxi to fix lineup.", parse_mode="HTML")
            return

        # Redesigned flow: no team picker. The opponent is auto-built to be
        # ADAPTIVE_RATING_DELTA points stronger than the user's XI, with Level 5
        # traits (see handlers.vsbot.build_adaptive_bot_xi). Go straight to toss.
        # NB: the bot User row is a shared singleton (also used by /vsbot,
        # /wspbot), so we do NOT stamp the opponent name onto it — that would
        # race across concurrent matches. The label is a constant here and every
        # display site uses ADAPTIVE_BOT_TEAM_NAME directly.
        bot_user = _get_or_create_bot_user(session)

        from services.match_constants import random_match_settings
        st = random_match_settings()
        now = datetime.utcnow()
        m = Match(
            user1_id=user.id, user2_id=bot_user.id,
            status="toss", stadium=st["stadium"],
            pitch_type=st["pitch_type"], weather=st["weather"],
            temperature=st["temperature"],
            umpire1=st["umpire1"], umpire2=st["umpire2"],
            chat_id=cid, created_at=now,
            # Short pre-play expiry: if the user closes the prompt or the toss
            # buttons time out, the stale toss row is swept quickly (see
            # _expire_stale_pending_matches) instead of blocking them for long.
            expires_at=now + timedelta(minutes=10), overs=overs,
        )
        session.add(m)
        session.commit()

        context.bot_data[f"wpmb_overs_{m.id}"] = overs
        context.bot_data[f"wpmb_pitch_{m.id}"] = st

        # ── Toss: the human calls heads or tails ──
        kb = coin_call_keyboard(
            f"wpmb_coin_heads_{m.id}_{user.id}",
            f"wpmb_coin_tails_{m.id}_{user.id}")
        sent = await update.message.reply_text(
            f"🤖📱 <b>WPM vs BOT — {overs} OVER MATCH</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"An <b>adaptive Challenger XI</b> — tuned just above your team, "
            f"with Level 5 traits — is warming up.\n"
            f"📍 Pitch: <b>{st['pitch_type']}</b> · 🌤️ {st['weather']}\n"
            f"🏟️ {st['stadium']}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Call it in the air, captain!</i>\n"
            f"<b>Heads</b> or <b>Tails?</b>",
            parse_mode="HTML", reply_markup=kb)
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                    delay_seconds=WPMBOT_INVITE_TIMEOUT)
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("wpmbot_handler error")
        await update.message.reply_text("⚠️ Error starting wpmbot match.")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Cancel
# ════════════════════════════════════════════════════════════════════

async def wpmbot_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """wpmb_cancel_<owner_tg>"""
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
        await q.edit_message_text("🤖 <i>WPM vs Bot match cancelled.</i>", parse_mode="HTML")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# Coin call: human calls heads/tails → flip → winner
# ════════════════════════════════════════════════════════════════════

async def wpmbot_coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """wpmb_coin_(heads|tails)_<match_id>_<user_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        call = parts[2]; mid = int(parts[3]); uid = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if call not in ("heads", "tails"):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user or user.id != uid:
            await q.answer("Only the calling captain can toss!", show_alert=True)
            return
        m = session.query(Match).get(mid)
        if not m or m.status != "toss":
            await q.answer("This toss is no longer active.", show_alert=True)
            return
        bot_user = session.query(User).filter(User.telegram_id == BOT_TG_ID).first()
        bot_name = ADAPTIVE_BOT_TEAM_NAME
        await q.answer()

        coin, won = await run_coin_toss(
            lambda t: q.edit_message_text(t, parse_mode="HTML"), call)

        st = context.bot_data.get(f"wpmb_pitch_{mid}") or {}
        coin_label = coin.upper()
        call_label = call.upper()

        if won:
            m.toss_winner_id = user.id
            session.commit()
            try:
                await q.edit_message_text(
                    f"🪙 <b>TOSS RESULT</b>\n\n"
                    f"The coin lands on <b>{coin_label}</b> — you called <b>{call_label}</b>.\n"
                    f"🏆 <b>@{user.username or user.first_name}</b> wins the toss "
                    f"vs <b>{bot_name}</b>!\n\n"
                    + (f"<i>{_pitch_hint_vsbot(st['pitch_type'])}</i>"
                       if st.get('pitch_type') else ""),
                    parse_mode="HTML")
            except Exception:
                pass
            await asyncio.sleep(0.4)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🏏 Bat First", callback_data=f"wpmb_toss_bat_{m.id}_{user.id}"),
                InlineKeyboardButton("🎳 Bowl First", callback_data=f"wpmb_toss_bowl_{m.id}_{user.id}"),
            ]])
            sent = await context.bot.send_message(
                q.message.chat_id, "⚖️ Choose your call:", reply_markup=kb)
            try:
                schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                        delay_seconds=120)
            except Exception:
                pass
            return

        # Human lost the toss → bot decides.
        m.toss_winner_id = bot_user.id
        session.commit()
        try:
            await q.edit_message_text(
                f"🪙 <b>TOSS RESULT</b>\n\n"
                f"The coin lands on <b>{coin_label}</b> — you called <b>{call_label}</b>.\n"
                f"🏆 <b>{bot_name}</b> wins the toss!",
                parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(0.5)
        bot_decision = "bowl" if random.random() < 0.6 else "bat"
        await _wpmbot_apply_toss(context, q.message.chat_id, mid,
                                 bot_decision, bot_user.id)
    except Exception:
        session.rollback()
        logger.exception("wpmbot_coin_callback error")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# User picks bat/bowl after winning toss
# ════════════════════════════════════════════════════════════════════

async def wpmbot_toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """wpmb_toss_(bat|bowl)_<match_id>_<user_id>"""
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
        await _wpmbot_apply_toss(context, q.message.chat_id, mid, decision, user.id, q=q)
    except Exception:
        logger.exception("wpmbot_toss_callback error")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Internal: apply toss decision → init Mini App match → launch
# ════════════════════════════════════════════════════════════════════

async def _wpmbot_apply_toss(context, chat_id, mid, decision, decider_uid, q=None):
    """Apply the toss decision and launch the match in the Mini App.

    Unlike /vsbot (which honours the global match style), /wpmbot ALWAYS plays
    in the Mini App. The bot's XI isn't in UserRoster, so it's passed via
    xi_overrides; init_match_for_webapp auto-detects the bot user and flags the
    match is_vsbot, so the bot side auto-plays.
    """
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

        bat_uid = m.batting_first_id
        bowl_uid = m.bowling_first_id

        # Auto-build the adaptive opponent (+1 stronger, Level 5 traits).
        bot_xi = build_adaptive_bot_xi(session, user.id)
        if not bot_xi or len(bot_xi) < 11:
            if q:
                try:
                    await q.edit_message_text(
                        "❌ Couldn't assemble an opponent (not enough players "
                        "in the pool). Try again later.")
                except Exception:
                    pass
            return

        # Team-name labels (independent of which side bats first).
        user_team_name = user.team_name or f"@{user.username}'s XI"
        bot_team_name = ADAPTIVE_BOT_TEAM_NAME
        if bat_uid == user.id:
            bat_team_name, bowl_team_name = user_team_name, bot_team_name
        else:
            bat_team_name, bowl_team_name = bot_team_name, user_team_name

        m.status = "playing"
        session.commit()

        # Toss result message.
        winner = session.query(User).get(decider_uid)
        winner_name = winner.first_name or winner.username or "Bot"
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

        # Initialize the Mini App match (bot XI injected; difficulty threaded).
        try:
            from services.match_webapp_service import init_match_for_webapp
            ok, message = init_match_for_webapp(
                session, mid, xi_overrides={bot_user.id: bot_xi},
                difficulty=ADAPTIVE_AI_DIFFICULTY)
            if not ok:
                logger.error("wpmbot init_match_for_webapp failed: %s", message)
                await context.bot.send_message(
                    chat_id, f"❌ Failed to launch match: {message}")
                return
        except Exception:
            logger.exception("wpmbot webapp init failed")
            await context.bot.send_message(chat_id, "❌ Failed to launch match.")
            return

        # Post the "Play Match" card that opens the Mini App.
        try:
            from services.match_broadcast import send_match_ready_message
            from handlers.match import _mention as _mm
            bat_mention = "🤖 AI" if bat_uid == bot_user.id else _mm(user)
            bowl_mention = "🤖 AI" if bowl_uid == bot_user.id else _mm(user)
            winner = session.query(User).get(decider_uid)
            winner_label = ("🤖 " + ADAPTIVE_BOT_TEAM_NAME
                            if decider_uid == bot_user.id
                            else f"@{winner.username or winner.first_name}")
            toss_note = f"{winner_label} won & chose to {'bat' if decision == 'bat' else 'bowl'}"
            await send_match_ready_message(
                context, chat_id, m, bat_team_name, bowl_team_name,
                bat_mention, bowl_mention, toss_note=toss_note)
        except Exception:
            logger.exception("wpmbot match-ready message failed")
    except Exception:
        session.rollback()
        logger.exception("_wpmbot_apply_toss error")
    finally:
        session.close()
