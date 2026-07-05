"""/vsbot — play a match against a pre-built bot team.

Flow:
  1. User: /vsbot 5  (or any 1-20)
  2. Bot: validates user has 11+ players. If yes, shows their default XI.
     User confirms or implicitly accepts.
  3. Bot: shows all active bot teams as buttons.
  4. User picks a bot team.
  5. Bot: shows toss screen → coin flip → user picks bat/bowl.
  6. Match begins using existing engine. AI controls bot decisions.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import (
    User, Player, UserRoster, Match, BotTeam, BotTeamPlayer, UserStats,
)
from services.bot_ai import BOT_TG_ID
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


def _queue_bot_action(context, mid, action_name, action):
    """Queue one AI action without blocking Telegram callback handling.

    Bot-vs-bot matches can run for hundreds of deliveries.  Running every AI
    choice by recursively awaiting the next one grows a deep coroutine chain
    and leaves the originating callback open for the whole match.  A small
    per-action task guard keeps the UI responsive and also prevents duplicate
    heartbeat renders from scheduling the same AI choice twice.
    """
    key = f"vsbot_task_{action_name}_{mid}"
    existing = context.bot_data.get(key)
    if existing and not existing.done():
        return existing

    async def _runner():
        try:
            await action(context, mid)
        except Exception:
            logger.exception("Queued vsbot %s action failed for match %s", action_name, mid)
            # Retry after this failed task has unwound. Rendering immediately
            # would see the still-running guard and incorrectly treat the bot
            # action as already queued.
            from handlers.match import _schedule_recovery
            _schedule_recovery(context, mid, f"bot {action_name}")

    task = asyncio.create_task(_runner(), name=key)
    context.bot_data[key] = task

    def _cleanup(done_task):
        if context.bot_data.get(key) is done_task:
            context.bot_data.pop(key, None)

    task.add_done_callback(_cleanup)
    return task


def _pitch_hint_vsbot(pitch_type):
    return {
        "Flat":  "Batters' paradise — high scores expected.",
        "Hard":  "Bouncy, true bounce — rewards aggressive shots.",
        "Even":  "Neutral, balanced track — bat and ball share honours.",
        "Green": "Seam movement up front — bowlers will love early overs.",
        "Dry":   "Slow and low — tough to time the ball cleanly.",
        "Dusty": "Spinners will turn it square as it wears.",
    }.get(pitch_type, "A balanced wicket.")


# ════════════════════════════════════════════════════════════════════
# Constants
# ════════════════════════════════════════════════════════════════════

VSBOT_USERNAME = "BotOpponent"  # virtual user shown in matches
VSBOT_INVITE_TIMEOUT = 120


def _get_or_create_bot_user(session):
    """Return the singleton bot user (telegram_id = BOT_TG_ID)."""
    bot_user = session.query(User).filter(User.telegram_id == BOT_TG_ID).first()
    if not bot_user:
        bot_user = User(
            telegram_id=BOT_TG_ID,
            username=VSBOT_USERNAME,
            first_name="Bot Opponent",
            total_coins=0, total_gems=0, roster_count=0,
            team_name="Bot XI",
        )
        session.add(bot_user)
        session.flush()
        session.add(UserStats(user_id=bot_user.id))
        session.flush()
    return bot_user


# ════════════════════════════════════════════════════════════════════
# /vsbot command
# ════════════════════════════════════════════════════════════════════

async def vsbot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    cid = update.effective_chat.id

    # Parse overs argument
    overs = 5  # default
    if context.args:
        try:
            overs = int(context.args[0])
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Usage: <code>/vsbot N</code> where N is overs (1-20). Default is 5.",
                parse_mode="HTML")
            return

    if overs < 1 or overs > 20:
        await update.message.reply_text("❌ Overs must be 1-20.")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # One match per chat (any game mode)
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

        # Roster check
        roster_count = session.query(UserRoster).filter(UserRoster.user_id == user.id).count()
        if roster_count < 11:
            await update.message.reply_text(
                f"❌ You need at least 11 players in your roster (have {roster_count}).")
            return

        # XI validity check
        from handlers.lineup import validate_xi, _get_ordered_roster
        user_roster = _get_ordered_roster(session, user.id)
        valid, errs = validate_xi(user_roster)
        if not valid:
            await update.message.reply_text(
                "❌ <b>Your top-11 XI is invalid:</b>\n\n" +
                "\n".join(f"• {e}" for e in errs) +
                "\n\nUse /pxi to fix lineup.",
                parse_mode="HTML")
            return

        # Show available bot teams
        active_teams = (session.query(BotTeam)
                        .filter(BotTeam.is_active == True)
                        .order_by(BotTeam.difficulty, BotTeam.name).all())

        valid_teams = []
        from services.bot_team_service import validate_team_xi
        for t in active_teams:
            ok, _ = validate_team_xi(session, t.id)
            if ok:
                valid_teams.append(t)

        if not valid_teams:
            await update.message.reply_text(
                "❌ No valid bot teams available right now.\n"
                "Admin needs to create some first via the website.")
            return

        # Build buttons
        btns = []
        for t in valid_teams:
            count = session.query(BotTeamPlayer).filter(BotTeamPlayer.bot_team_id == t.id).count()
            avg_rating = (session.query(Player.rating)
                          .join(BotTeamPlayer, BotTeamPlayer.player_id == Player.id)
                          .filter(BotTeamPlayer.bot_team_id == t.id).all())
            avg = round(sum(r[0] for r in avg_rating) / len(avg_rating), 1) if avg_rating else 0
            label = f"{t.name} ({t.difficulty}) — Avg {avg}"
            btns.append([InlineKeyboardButton(
                label, callback_data=f"vsb_pick_{tg.id}_{t.id}_{overs}",
            )])

        btns.append([InlineKeyboardButton(
            "❌ Cancel", callback_data=f"vsb_cancel_{tg.id}",
        )])

        sent = await update.message.reply_text(
            f"🤖 <b>VS BOT — {overs} OVER MATCH</b>\n\n"
            f"Choose a bot team to play against:\n",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns),
        )
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                    delay_seconds=VSBOT_INVITE_TIMEOUT)
        except Exception:
            pass

    except Exception:
        session.rollback()
        logger.exception("vsbot_handler error")
        await update.message.reply_text("⚠️ Error starting vsbot match.")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Callback: pick bot team → toss
# ════════════════════════════════════════════════════════════════════

async def vsbot_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vsb_pick_<owner_tg>_<bot_team_id>_<overs>"""
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
            await q.edit_message_text("❌ Do /debut first!")
            return

        bot_team = session.query(BotTeam).get(team_id)
        if not bot_team or not bot_team.is_active:
            await q.edit_message_text("❌ Bot team unavailable.")
            return

        # Re-check concurrency at the commit point (race with another lobby/match).
        from handlers.match import (
            _active_match_in_chat, _active_match_for_user, _chat_busy_message,
        )
        existing = _active_match_in_chat(session, q.message.chat_id)
        if existing:
            await q.edit_message_text(_chat_busy_message(existing), parse_mode="HTML")
            return
        if _active_match_for_user(session, user.id):
            await q.edit_message_text(
                "⚠️ You already have an active match (any game mode). "
                "Finish it first, then start a new one.")
            return

        # Get/create bot user
        bot_user = _get_or_create_bot_user(session)
        bot_user.team_name = bot_team.name
        session.commit()

        # Create Match record
        from services.match_constants import random_match_settings
        st = random_match_settings()
        now = datetime.utcnow()

        m = Match(
            user1_id=user.id, user2_id=bot_user.id,
            status="toss", stadium=st["stadium"],
            pitch_type=st["pitch_type"], weather=st["weather"],
            temperature=st["temperature"],
            umpire1=st["umpire1"], umpire2=st["umpire2"],
            chat_id=q.message.chat_id,
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            overs=overs,
        )
        session.add(m)
        session.commit()

        # Stash bot team id on the match (we use a context dict keyed by match id)
        context.bot_data[f"vsbot_team_{m.id}"] = bot_team.id
        context.bot_data[f"vsbot_overs_{m.id}"] = overs

        # Toss
        wid = random.choice([user.id, bot_user.id])
        m.toss_winner_id = wid
        session.commit()

        # ── Animated coin toss ──
        import asyncio as _asyncio
        try:
            await q.edit_message_text(
                "🪙 <b>TOSS</b>\n\n<i>Calling captain to the centre...</i>",
                parse_mode="HTML")
        except Exception:
            pass
        await _asyncio.sleep(0.6)

        spin_frames = [
            "🪙 <b>TOSS</b>\n\n     ⬆️\n   ╱  🪙  ╲\n\n<i>Captain flicks the coin into the air...</i>",
            "🪙 <b>TOSS</b>\n\n          🌀\n        🪙\n\n<i>It spins higher and higher...</i>",
            "🪙 <b>TOSS</b>\n\n     🌀 🪙 🌀\n\n<i>Tumbling end over end...</i>",
            "🪙 <b>TOSS</b>\n\n          ⬇️\n        🪙\n\n<i>Coming down now!</i>",
        ]
        for f in spin_frames:
            try:
                await q.edit_message_text(f, parse_mode="HTML")
            except Exception:
                pass
            await _asyncio.sleep(0.5)

        # If bot wins toss, auto-decide
        if wid == bot_user.id:
            try:
                await q.edit_message_text(
                    f"🪙 <b>TOSS RESULT</b>\n\n"
                    f"🏆 <b>{bot_team.name}</b> wins the toss!",
                    parse_mode="HTML")
            except Exception:
                pass
            await _asyncio.sleep(0.5)
            import random as _r
            bot_decision = "bowl" if _r.random() < 0.6 else "bat"
            await _vsbot_apply_toss(context, q.message.chat_id, m.id, bot_decision, bot_user.id)
            return

        # User won toss → reveal + show bat/bowl buttons
        try:
            await q.edit_message_text(
                f"🪙 <b>TOSS RESULT</b>\n\n"
                f"🏆 <b>@{user.username or user.first_name}</b> wins the toss vs <b>{bot_team.name}</b>!\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📍 Pitch: <b>{st['pitch_type']}</b> · 🌤️ {st['weather']}\n"
                f"🏟️ {st['stadium']}\n\n"
                f"<i>{_pitch_hint_vsbot(st['pitch_type'])}</i>",
                parse_mode="HTML")
        except Exception:
            pass
        await _asyncio.sleep(0.4)

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏏 Bat First", callback_data=f"vsb_toss_bat_{m.id}_{user.id}"),
            InlineKeyboardButton("🎳 Bowl First", callback_data=f"vsb_toss_bowl_{m.id}_{user.id}"),
        ]])
        sent = await context.bot.send_message(q.message.chat_id,
            f"⚖️ Choose your call:\n\n"
            f"🏏 Bat First — set a target on a {('fresh' if st['pitch_type'] in ('Flat','Hard') else 'tricky')} pitch\n"
            f"🎳 Bowl First — pitch typically {('eases' if st['pitch_type'] == 'Green' else 'wears')} later",
            parse_mode="HTML", reply_markup=kb,
        )
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id,
                                    delay_seconds=120)
        except Exception:
            pass

    except Exception:
        session.rollback()
        logger.exception("vsbot_pick_callback error")
        try:
            await q.edit_message_text("⚠️ Error setting up match.")
        except Exception:
            pass
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Cancel
# ════════════════════════════════════════════════════════════════════

async def vsbot_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vsb_cancel_<owner_tg>"""
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
        await q.edit_message_text("🤖 <i>VS Bot match cancelled.</i>", parse_mode="HTML")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# User picks bat/bowl after winning toss
# ════════════════════════════════════════════════════════════════════

async def vsbot_toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vsb_toss_(bat|bowl)_<match_id>_<user_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        decision = parts[2]; mid = int(parts[3]); uid = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user or user.id != uid:
            await q.answer("Toss winner only!", show_alert=True)
            return
        await q.answer()
        await _vsbot_apply_toss(context, q.message.chat_id, mid, decision, user.id, q=q)
    except Exception:
        logger.exception("vsbot_toss_callback error")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Internal: apply toss decision and start match flow
# ════════════════════════════════════════════════════════════════════

async def _vsbot_apply_toss(context, chat_id, mid, decision, decider_uid, q=None):
    """Apply the toss decision and kick off the opener selection flow.

    Reuses the existing match opener/bowler selection by setting up the same
    state the existing flow expects, then triggering the right next step.
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
        m.status = "selecting"
        session.commit()

        winner = session.query(User).get(decider_uid)
        winner_name = winner.first_name or winner.username or "Bot"

        result_text = (
            f"🪙 <b>TOSS RESULT</b>\n\n"
            f"<b>{winner_name}</b> won the toss\n"
            f"and elected to <b>{'BAT' if decision == 'bat' else 'BOWL'} FIRST</b>"
        )
        if q:
            try:
                await q.edit_message_text(result_text, parse_mode="HTML")
            except Exception:
                pass
        else:
            await context.bot.send_message(chat_id, result_text, parse_mode="HTML")

        # Build XIs
        bat_uid = m.batting_first_id
        bowl_uid = m.bowling_first_id

        bot_team_id = context.bot_data.get(f"vsbot_team_{mid}")
        bot_xi = _build_bot_team_xi(session, bot_team_id) if bot_team_id else []

        # Get user XI
        from handlers.match import _gxi
        user_xi = _gxi(session, user.id)

        # Assign which is batting/bowling
        if bat_uid == user.id:
            bat_xi = user_xi; bowl_xi = bot_xi
            bat_username = user.username or user.first_name; bowl_username = "Bot"
            bat_team_name = user.team_name or f"@{user.username}'s XI"
            bowl_team_name = bot_user.team_name
        else:
            bat_xi = bot_xi; bowl_xi = user_xi
            bat_username = "Bot"; bowl_username = user.username or user.first_name
            bat_team_name = bot_user.team_name
            bowl_team_name = user.team_name or f"@{user.username}'s XI"

        # Stash for bot to know which side it controls
        context.bot_data[f"vsbot_bot_uid_{mid}"] = bot_user.id

        # Stash XIs in bot_data for the existing match flow
        context.bot_data[f"bat_xi_{mid}"] = bat_xi
        context.bot_data[f"bowl_xi_{mid}"] = bowl_xi
        context.bot_data[f"bat_uname_{mid}"] = bat_username
        context.bot_data[f"bowl_uname_{mid}"] = bowl_username
        context.bot_data[f"bat_uid_{mid}"] = bat_uid
        context.bot_data[f"bowl_uid_{mid}"] = bowl_uid

        # Show XIs
        from handlers.match import format_xi_text
        # Build batsman list to render
        if bat_uid == user.id:
            bat_r = (session.query(UserRoster, Player)
                     .join(Player).filter(UserRoster.user_id == user.id)
                     .order_by(UserRoster.order_position).limit(11).all())
            bat_render = format_xi_text(bat_r, f"🏏 {bat_team_name} (Batting)", user.captain_roster_id)
        else:
            bat_render = _format_bot_xi_text(bat_xi, f"🏏 {bat_team_name} (Batting)")

        if bowl_uid == user.id:
            bowl_r = (session.query(UserRoster, Player)
                      .join(Player).filter(UserRoster.user_id == user.id)
                      .order_by(UserRoster.order_position).limit(11).all())
            bowl_render = format_xi_text(bowl_r, f"🎳 {bowl_team_name} (Bowling)", user.captain_roster_id)
        else:
            bowl_render = _format_bot_xi_text(bowl_xi, f"🎳 {bowl_team_name} (Bowling)")

        await context.bot.send_message(chat_id, bat_render, parse_mode="HTML", disable_web_page_preview=True)
        await context.bot.send_message(chat_id, bowl_render, parse_mode="HTML", disable_web_page_preview=True)

        # Match style is global: keep the original Telegram gameplay unless
        # the admin has explicitly enabled the optional Mini App live board.
        from services.config_service import get_match_style
        if get_match_style(session) == "webapp":
            m.status = "playing"
            session.commit()
            try:
                from services.match_webapp_service import init_match_for_webapp
                # The bot's XI isn't in UserRoster — pass it explicitly.
                overrides = {bot_user.id: bot_xi}
                bt = session.query(BotTeam).get(bot_team_id) if bot_team_id else None
                init_match_for_webapp(
                    session, mid, xi_overrides=overrides,
                    difficulty=(bt.difficulty if bt else None))
            except Exception:
                logger.exception("vsbot webapp init failed")
            try:
                from services.match_broadcast import send_match_ready_message
                from handlers.match import _mention as _mm
                bat_mention = ("🤖 AI" if bat_uid == bot_user.id else _mm(user))
                bowl_mention = ("🤖 AI" if bowl_uid == bot_user.id else _mm(user))
                await send_match_ready_message(
                    context, chat_id, m, bat_team_name, bowl_team_name,
                    bat_mention, bowl_mention)
            except Exception:
                logger.exception("vsbot match-ready message failed")
        elif bat_uid == bot_user.id:
            # Original bot gameplay: AI picks its openers, then the user bowls.
            op1 = bat_xi[0]; op2 = bat_xi[1]
            context.bot_data[f"opener1_{mid}"] = op1
            context.bot_data[f"opener2_{mid}"] = op2
            await context.bot.send_message(
                chat_id,
                f"🤖 Bot openers: <b>{op1['name']}</b> & <b>{op2['name']}</b>",
                parse_mode="HTML")
            if bowl_uid == user.id:
                await _show_user_opening_bowler(context, chat_id, mid, user, bowl_xi)
            else:
                await _bot_pick_and_start(context, chat_id, mid)
        else:
            # Original bot gameplay: the user picks both openers in chat.
            await _show_user_opener_picker(context, chat_id, mid, user, bat_xi, opener_num=1)

    except Exception:
        session.rollback()
        logger.exception("_vsbot_apply_toss error")
    finally:
        session.close()


def _format_bot_xi_text(xi, header):
    """Render bot's XI roster as a text block."""
    lines = [f"<b>{header}</b>"]
    for i, p in enumerate(xi, 1):
        lines.append(f"  #{i}. {p['name']} | OVR {p['rating']} | {p['category']}")
    return "\n".join(lines)


def _build_bot_team_xi(session, bot_team_id):
    """Build XI dict list for a BotTeam."""
    rows = (session.query(BotTeamPlayer, Player)
            .join(Player, BotTeamPlayer.player_id == Player.id)
            .filter(BotTeamPlayer.bot_team_id == bot_team_id)
            .order_by(BotTeamPlayer.batting_order)
            .limit(11)
            .all())
    out = []
    for btp, p in rows:
        out.append({
            "roster_id": -btp.id,  # Negative so it doesn't collide with real UserRoster IDs
            "player_id": p.id,
            "name": p.name,
            "rating": p.rating,
            "category": p.category,
            "bat_rating": p.bat_rating,
            "bowl_rating": p.bowl_rating,
            "bowl_style": p.bowl_style,
            "bowl_hand": p.bowl_hand,
            "bat_hand": p.bat_hand,
            "is_bot_player": True,
        })
    return out


# ════════════════════════════════════════════════════════════════════
# User opener picker (for /vsbot only)
# ════════════════════════════════════════════════════════════════════

async def _show_user_opener_picker(context, chat_id, mid, user, bat_xi, opener_num=1):
    """Show the user a picker for opener 1 or 2."""
    if opener_num == 1:
        btns = [[InlineKeyboardButton(
            f"{p['name']} - {p['rating']} | {p['category']}",
            callback_data=f"vsb_op1_{mid}_{user.id}_{p['roster_id']}",
        )] for p in bat_xi]
        text = f"🏏 <b>SELECT OPENER 1</b>\n\n@{user.username}, pick:"
    else:
        op1 = context.bot_data.get(f"opener1_{mid}", {})
        rem = [p for p in bat_xi if p["roster_id"] != op1.get("roster_id")]
        btns = [[InlineKeyboardButton(
            f"{p['name']} - {p['rating']} | {p['category']}",
            callback_data=f"vsb_op2_{mid}_{user.id}_{p['roster_id']}",
        )] for p in rem]
        text = (f"✅ Opener 1: {op1.get('name')}\n\n🏏 <b>SELECT OPENER 2</b>\n\n"
                f"@{user.username}, pick:")
    sent = await context.bot.send_message(chat_id, text, parse_mode="HTML",
                                           reply_markup=InlineKeyboardMarkup(btns))
    try:
        schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
    except Exception:
        pass


async def vsbot_op1_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vsb_op1_<mid>_<user_id>_<roster_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        mid = int(parts[2]); uid = int(parts[3]); rid = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user or user.id != uid:
            await q.answer("Not yours!")
            return
        await q.answer()

        bat_xi = context.bot_data.get(f"bat_xi_{mid}", [])
        chosen = next((p for p in bat_xi if p["roster_id"] == rid), None)
        if not chosen:
            return
        context.bot_data[f"opener1_{mid}"] = chosen

        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await _show_user_opener_picker(context, q.message.chat_id, mid, user, bat_xi, opener_num=2)
    except Exception:
        logger.exception("vsbot_op1_callback error")
    finally:
        session.close()


async def vsbot_op2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vsb_op2_<mid>_<user_id>_<roster_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        mid = int(parts[2]); uid = int(parts[3]); rid = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user or user.id != uid:
            await q.answer("Not yours!")
            return
        await q.answer()

        bat_xi = context.bot_data.get(f"bat_xi_{mid}", [])
        chosen = next((p for p in bat_xi if p["roster_id"] == rid), None)
        if not chosen:
            return
        context.bot_data[f"opener2_{mid}"] = chosen
        op1 = context.bot_data.get(f"opener1_{mid}", {})

        try:
            await q.edit_message_text(
                f"✅ Openers: <b>{op1.get('name')}</b> & <b>{chosen['name']}</b>\n\n⏳ Bowler...",
                parse_mode="HTML")
        except Exception:
            pass

        # Now bowling side picks opening bowler
        bowl_uid = context.bot_data.get(f"bowl_uid_{mid}")
        bot_uid = context.bot_data.get(f"vsbot_bot_uid_{mid}")
        bowl_xi = context.bot_data.get(f"bowl_xi_{mid}", [])

        if bowl_uid == bot_uid:
            # Bot picks bowler
            await _bot_pick_and_start(context, q.message.chat_id, mid)
        else:
            # Show user bowler picker
            bwu = session.query(User).get(bowl_uid)
            await _show_user_opening_bowler(context, q.message.chat_id, mid, bwu, bowl_xi)
    except Exception:
        logger.exception("vsbot_op2_callback error")
    finally:
        session.close()


async def _show_user_opening_bowler(context, chat_id, mid, bwu, bowl_xi):
    """Show the user a picker for opening bowler."""
    sorted_xi = sorted(bowl_xi, key=lambda x: x["bowl_rating"], reverse=True)
    btns = [[InlineKeyboardButton(
        f"{p['name']} | {p.get('bowl_hand','R')[:1]}-{p.get('bowl_style','Medium')} | BWL {p['bowl_rating']}",
        callback_data=f"vsb_selbowl_{mid}_{bwu.id}_{p['roster_id']}",
    )] for p in sorted_xi]
    sent = await context.bot.send_message(
        chat_id,
        f"🎳 <b>SELECT OPENING BOWLER</b>\n\n@{bwu.username}, pick:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns),
    )
    try:
        schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
    except Exception:
        pass


async def vsbot_selbowl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """vsb_selbowl_<mid>_<bwuid>_<roster_id>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        mid = int(parts[2]); bwuid = int(parts[3]); rid = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user or user.id != bwuid:
            await q.answer("Not yours!")
            return
        await q.answer()

        bowl_xi = context.bot_data.get(f"bowl_xi_{mid}", [])
        bowler = next((p for p in bowl_xi if p["roster_id"] == rid), None)
        if not bowler:
            return

        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # ── INNINGS 2 path: state already exists, just plug in the bowler ──
        # If we ALSO build a fresh state here, we'd reset innings, target, and
        # all the inn1_* snapshots — sending the user back to bat from scratch.
        from handlers.match import _gs, _ss, render_screen, _send_batsman_card, _send_bowler_card
        from services.match_state_store import A_PICK_DELIVERY
        existing = _gs(context, mid)
        if existing and existing.get("innings") == 2:
            await q.edit_message_text(
                f"✅ Opening bowler: <b>{bowler['name']}</b>\n\n⏳ 2nd Innings starting…",
                parse_mode="HTML",
            )
            existing["current_bowler"] = bowler
            existing["prev_bowler_rid"] = None
            existing["selected_variation"] = None
            existing["current_delivery"] = None
            _ss(context, mid, existing, next_action=A_PICK_DELIVERY)

            # Show the 2nd-innings opener cards + bowler card so the user
            # has the same experience as innings 1.
            try:
                op1 = existing["bat_xi"][existing.get("striker_idx", 0)]
                op2 = existing["bat_xi"][existing.get("non_striker_idx", 1)]
                bat_team_id = existing.get("bat_team_id")
                bowl_team_id = existing.get("bowl_team_id")
                target = existing.get("target", "?")
                await context.bot.send_message(
                    q.message.chat_id,
                    f"🏏 <b>2ND INNINGS!</b>\n\n"
                    f"🟢 {existing.get('bat_team_name','')} needs {target} to win\n"
                    f"🏏 {op1.get('name','?')} & {op2.get('name','?')}\n"
                    f"🎳 {bowler['name']}\n━━━━━━━━━━━━━━━━━━━",
                    parse_mode="HTML",
                )
                await _send_batsman_card(context, q.message.chat_id, op1, bat_team_id)
                await _send_batsman_card(context, q.message.chat_id, op2, bat_team_id)
                await _send_bowler_card(context, q.message.chat_id, bowler, bowl_team_id)
            except Exception:
                logger.exception("inn2 cards failed (non-fatal)")

            # Hand off to render_screen — this fires _bot_bowl_delivery if bot
            # is bowling, or shows the user the picker if user is bowling.
            await render_screen(context, mid)
            return

        # ── INNINGS 1 path: build fresh state from scratch (existing flow) ──
        await q.edit_message_text(
            f"✅ Opening bowler: <b>{bowler['name']}</b>\n\n⏳ Starting match...",
            parse_mode="HTML")
        await _vsbot_start_match(context, q.message.chat_id, mid, bowler)

    except Exception:
        logger.exception("vsbot_selbowl_callback error")
    finally:
        session.close()


async def _bot_pick_and_start(context, chat_id, mid):
    """Bot picks opening bowler and starts match."""
    from services.bot_ai import pick_bot_next_bowler
    bowl_xi = context.bot_data.get(f"bowl_xi_{mid}", [])
    if not bowl_xi:
        return
    # First over: no prev_bowler restriction
    bowler = pick_bot_next_bowler(bowl_xi, prev_bowler_rid=None,
                                   bowl_stats={}, total_overs=context.bot_data.get(f"vsbot_overs_{mid}", 5))

    await context.bot.send_message(
        chat_id,
        f"🤖 Bot opening bowler: <b>{bowler['name']}</b>",
        parse_mode="HTML",
    )
    await _vsbot_start_match(context, chat_id, mid, bowler)


async def _vsbot_start_match(context, chat_id, mid, opening_bowler):
    """Initialize match state and start the first delivery."""
    from handlers.match import (
        _ss, _gs, _show_delivery, _send_batsman_card, _send_bowler_card,
    )

    session = get_session()
    try:
        m = session.query(Match).get(mid)
        if not m:
            return
        m.status = "in_progress"
        session.commit()

        # Build state
        bat_xi = context.bot_data.get(f"bat_xi_{mid}", [])
        bowl_xi = context.bot_data.get(f"bowl_xi_{mid}", [])
        op1 = context.bot_data.get(f"opener1_{mid}")
        op2 = context.bot_data.get(f"opener2_{mid}")
        overs = context.bot_data.get(f"vsbot_overs_{mid}", 5)

        # Determine indexes
        striker_idx = next((i for i, p in enumerate(bat_xi) if p["roster_id"] == op1["roster_id"]), 0)
        non_striker_idx = next((i for i, p in enumerate(bat_xi) if p["roster_id"] == op2["roster_id"]), 1)

        bat_user_tg = m.user1_id if context.bot_data.get(f"bat_uid_{mid}") == m.user1_id else BOT_TG_ID
        bowl_user_tg = m.user1_id if context.bot_data.get(f"bowl_uid_{mid}") == m.user1_id else BOT_TG_ID

        # Map to actual telegram IDs
        bat_uid_real = context.bot_data.get(f"bat_uid_{mid}")
        bowl_uid_real = context.bot_data.get(f"bowl_uid_{mid}")
        bat_user = session.query(User).get(bat_uid_real)
        bowl_user = session.query(User).get(bowl_uid_real)

        state = {
            "match_id": mid,
            "chat_id": chat_id,
            "innings": 1,
            "overs": overs,
            "current_over": 1,
            "current_ball": 0,
            "total_runs": 0,
            "total_wickets": 0,
            "extras_total": 0, "wides": 0, "noballs": 0, "legbyes": 0,
            "partnership_runs": 0, "partnership_balls": 0,
            "timeline": [], "fow": [], "target": None,

            "bat_user_tg": bat_user.telegram_id,
            "bowl_user_tg": bowl_user.telegram_id,
            "bat_username": bat_user.username or bat_user.first_name or "Player",
            "bowl_username": bowl_user.username or bowl_user.first_name or "Player",
            "bat_team_name": context.bot_data.get(f"bat_uname_{mid}", "Bat XI"),
            "bowl_team_name": context.bot_data.get(f"bowl_uname_{mid}", "Bowl XI"),
            "bat_team_id": bat_user.id,
            "bowl_team_id": bowl_user.id,

            "bat_xi": bat_xi,
            "bowl_xi": bowl_xi,
            "batting_order": bat_xi[:],
            "striker_idx": striker_idx,
            "non_striker_idx": non_striker_idx,

            "current_bowler": opening_bowler,
            "prev_bowler_rid": None,

            "bat_stats": {p["roster_id"]: {
                "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
                "out": False, "how_out": "", "bowled_by": "",
            } for p in bat_xi},
            "bowl_stats": {p["roster_id"]: {
                "balls": 0, "runs": 0, "wickets": 0,
                "overs_done": 0, "this_over_balls": 0,
                "maidens": 0, "this_over_runs": 0,
            } for p in bowl_xi},

            "pitch_type": m.pitch_type or "Flat",
            "stadium": m.stadium or "Stadium",
            "weather": m.weather or "Sunny",
            "is_vsbot": True,
            "vsbot_difficulty": _get_vsbot_difficulty(session, context, mid),
        }
        # Persist initial state with PICK_DELIVERY action
        from services.match_state_store import save_state, A_PICK_DELIVERY
        save_state(context, mid, state, next_action=A_PICK_DELIVERY)

        # Show opening cards
        striker = bat_xi[striker_idx]
        non_striker = bat_xi[non_striker_idx]

        # For bot players, just send a text intro instead of card image
        if not striker.get("is_bot_player"):
            try:
                await _send_batsman_card(context, chat_id, striker, bat_user.id)
            except Exception:
                pass
        if not non_striker.get("is_bot_player"):
            try:
                await _send_batsman_card(context, chat_id, non_striker, bat_user.id)
            except Exception:
                pass
        if not opening_bowler.get("is_bot_player"):
            try:
                await _send_bowler_card(context, chat_id, opening_bowler, bowl_user.id)
            except Exception:
                pass

        # First delivery prompt. Route through the dispatcher so bot actions
        # use the same responsive queue as every later delivery.
        from handlers.match import render_screen
        await render_screen(context, mid)

    except Exception:
        session.rollback()
        logger.exception("_vsbot_start_match error")
    finally:
        session.close()


def _get_vsbot_difficulty(session, context, mid):
    """Look up bot team difficulty for AI decision-making."""
    bot_team_id = context.bot_data.get(f"vsbot_team_{mid}")
    if bot_team_id:
        bt = session.query(BotTeam).get(bot_team_id)
        if bt:
            return bt.difficulty
    return "Medium"


# ════════════════════════════════════════════════════════════════════
# Bot bowling: auto-pick delivery
# ════════════════════════════════════════════════════════════════════

async def _bot_bowl_delivery(context, mid):
    """Bot is bowling — auto-pick variation/length and proceed to shot."""
    import asyncio
    await asyncio.sleep(1.5)  # Pacing

    from handlers.match import _gs, _ss, render_screen, _show_shot
    from services.bot_ai import pick_bot_delivery

    s = _gs(context, mid)
    if not s:
        return
    bowler = s["current_bowler"]
    over = s["current_over"]
    total_overs = s["overs"]
    difficulty = s.get("vsbot_difficulty", "Medium")

    pick = pick_bot_delivery(bowler, over, total_overs, difficulty)
    s["current_delivery"] = pick["delivery"]
    s["selected_variation"] = None
    # Persist the shot step before rendering it.  Recovery must never replay
    # a bot delivery after the AI has already selected one.
    from services.match_state_store import A_PICK_SHOT
    _ss(context, mid, s, next_action=A_PICK_SHOT)

    # Announce
    try:
        await context.bot.send_message(
            s["chat_id"],
            f"🤖 {bowler['name']}: <b>{pick['delivery']}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Now show user the shot picker (since user is batting if bot is bowling)
    if s.get("bat_user_tg") == BOT_TG_ID:
        # Spectator bot-vs-bot mode: dispatch the next AI choice separately
        # instead of recursively awaiting the entire match.
        await render_screen(context, mid)
    else:
        await _show_shot(context, s["chat_id"], mid)


# ════════════════════════════════════════════════════════════════════
# Bot batting: auto-pick shot and trigger shot_callback flow
# ════════════════════════════════════════════════════════════════════

async def _bot_play_shot(context, mid):
    """Bot is batting — auto-pick a shot and route through shot processing."""
    import asyncio
    await asyncio.sleep(1.2)  # pacing

    from handlers.match import _gs, _bot_process_shot
    from services.bot_ai import pick_bot_shot

    s = _gs(context, mid)
    if not s:
        return

    striker = s["bat_xi"][s["striker_idx"]]
    bowler = s["current_bowler"]
    over = s["current_over"]
    total_overs = s["overs"]
    target = s.get("target")
    difficulty = s.get("vsbot_difficulty", "Medium")

    shot_name, shot_idx = pick_bot_shot(
        striker, bowler, over, total_overs,
        s["total_runs"], s["total_wickets"], target=target,
        current_ball=s["current_ball"], difficulty=difficulty,
    )

    try:
        await context.bot.send_message(
            s["chat_id"],
            f"🤖 {striker['name']} plays: <b>{shot_name}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass

    # Process the shot using a slim version of shot_callback logic
    await _bot_process_shot(context, mid, shot_idx)


# ════════════════════════════════════════════════════════════════════
# Hook into existing match callbacks: detect bot turn and auto-decide
# ════════════════════════════════════════════════════════════════════

async def vsbot_auto_continue(context, mid):
    """Called by render_screen when /vsbot match needs the next action.

    Returns True if the bot took an action (and downstream handler should
    NOT also try to render). Returns False only when it's the user's turn.

    The bot side handles ALL its own selections automatically:
      - When bot bats: picks new batsmen, picks shots
      - When bot bowls: picks delivery (variation+length OR spinner-line)
      - End of over with bot bowling: picks new bowler (excluding prev)
    """
    from handlers.match import (
        _gs, _ss, A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT,
        A_PICK_NEW_BATSMAN, A_PICK_NEW_BOWLER, A_INNINGS_BREAK,
    )
    from services.match_state_store import get_next_action, set_next_action

    s = _gs(context, mid)
    if not s or not s.get("is_vsbot"):
        return False

    bat_is_bot = s.get("bat_user_tg") == BOT_TG_ID
    bowl_is_bot = s.get("bowl_user_tg") == BOT_TG_ID
    next_act = get_next_action(context, mid) or A_PICK_DELIVERY

    # ── New batsman needed?
    if next_act == A_PICK_NEW_BATSMAN:
        if bat_is_bot:
            from services.bot_ai import pick_bot_next_batsman
            idx, p = pick_bot_next_batsman(
                s["batting_order"], s["striker_idx"], s["non_striker_idx"], s["bat_stats"],
            )
            if p:
                s["striker_idx"] = idx
                # A wicket on the last ball of an over still needs a bowler
                # selection after the replacement batsman is chosen.
                next_action = (A_PICK_NEW_BOWLER
                               if s["current_ball"] == 0 and s["current_over"] > 1
                               else A_PICK_DELIVERY)
                _ss(context, mid, s, next_action=next_action)
                try:
                    await context.bot.send_message(
                        s["chat_id"],
                        f"🤖 New batsman: <b>{p['name']}</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                # We handled the batsman selection. Always route the next
                # state through the dispatcher: it may be a fresh delivery or,
                # after a last-ball wicket, the next-over bowler picker.
                from handlers.match import render_screen
                await render_screen(context, mid)
                return True
            return False
        else:
            # User batting — let normal UI handle
            return False

    # ── New bowler needed (end of over)?
    if next_act == A_PICK_NEW_BOWLER:
        if bowl_is_bot:
            from services.bot_ai import pick_bot_next_bowler
            new_bowler = pick_bot_next_bowler(
                s["bowl_xi"],
                s.get("prev_bowler_rid"),
                s["bowl_stats"],
                s["overs"],
            )
            s["current_bowler"] = new_bowler
            _ss(context, mid, s, next_action=A_PICK_DELIVERY)
            try:
                await context.bot.send_message(
                    s["chat_id"],
                    f"🤖 New bowler: <b>{new_bowler['name']}</b> | {new_bowler.get('bowl_hand','R')[:1]}-{new_bowler['bowl_style']}",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            # Continue through the dispatcher.  AI work is queued below so
            # a full spectator match does not become one recursive await chain.
            from handlers.match import render_screen
            await render_screen(context, mid)
            return True
        else:
            # User bowling — let UI handle
            return False

    # ── Fresh delivery to be picked
    if next_act == A_PICK_DELIVERY:
        if bowl_is_bot:
            _queue_bot_action(context, mid, "delivery", _bot_bowl_delivery)
            return True
        else:
            return False

    # ── Length awaiting
    if next_act == A_PICK_LENGTH:
        # Only happens when bowler is human + chose pace variation.
        # If bowler is bot, _bot_bowl_delivery sets full delivery directly,
        # so we never enter A_PICK_LENGTH for bot.
        return False

    # ── Shot awaiting
    if next_act == A_PICK_SHOT:
        if bat_is_bot:
            _queue_bot_action(context, mid, "shot", _bot_play_shot)
            return True
        else:
            return False

    return False
