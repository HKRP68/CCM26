"""/botvsbot — pick two bot teams and watch them play.

Pure spectator mode:
  - User selects two distinct bot teams from the registry
  - Both teams use AI for everything (toss, openers, deliveries, shots, batsmen, bowlers)
  - User just watches scorecards stream in
  - NO rewards — this is for entertainment / testing only

Implementation notes:
  - Both "users" in the match are the bot user (telegram_id = -1)
  - bat_user_tg = bowl_user_tg = BOT_TG_ID throughout
  - Match ends without coin/gem rewards (handled by checking is_bot_vs_bot flag)
"""

import logging
import random
import time
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Match, BotTeam, BotTeamPlayer, Player
from services.button_timeout import schedule_button_timeout
from services.match_outcome import TYPE_BOTVSBOT

logger = logging.getLogger(__name__)

BOT_TG_ID = -1   # match.py constant


async def botvsbot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: pick overs."""
    args = context.args or []
    overs = 5
    if args:
        try:
            overs = max(1, min(20, int(args[0])))
        except ValueError:
            pass

    cid = update.effective_chat.id
    session = get_session()
    try:
        # One match per chat
        from handlers.match import _active_match_in_chat, _chat_busy_message
        existing = _active_match_in_chat(session, cid)
        if existing:
            await update.message.reply_text(
                _chat_busy_message(existing), parse_mode="HTML")
            return

        # Make sure we have at least 2 active bot teams
        teams = (session.query(BotTeam)
                 .filter(BotTeam.is_active == True)
                 .order_by(BotTeam.name).all())
        if len(teams) < 2:
            await update.message.reply_text(
                "⚠️ Need at least 2 active bot teams. Ask admin to create more via /admin.",
                parse_mode="HTML")
            return

        # Verify each team has 11 players
        viable = []
        for t in teams:
            cnt = (session.query(BotTeamPlayer)
                   .filter(BotTeamPlayer.bot_team_id == t.id).count())
            if cnt >= 11:
                viable.append(t)
        if len(viable) < 2:
            await update.message.reply_text(
                "⚠️ Need at least 2 bot teams with 11+ players each.",
                parse_mode="HTML")
            return

        # Show team picker for Team A
        rows = []
        cur = []
        for t in viable:
            cur.append(InlineKeyboardButton(
                t.name,
                callback_data=f"bvb_pickA_{t.id}_{overs}_{update.effective_user.id}"))
            if len(cur) >= 2:
                rows.append(cur); cur = []
        if cur: rows.append(cur)
        rows.append([InlineKeyboardButton("❌ Cancel",
                    callback_data=f"bvb_cancel_{update.effective_user.id}")])

        sent = await update.message.reply_text(
            f"🤖 <b>BOT vs BOT</b> — {overs} overs\n\n"
            f"Pick <b>Team A</b> (will bat first if it wins toss):\n\n"
            f"<i>Pure spectator mode — no rewards earned.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=180)
        except Exception:
            pass
    except Exception:
        logger.exception("botvsbot_handler error")
        await update.message.reply_text("⚠️ Error starting bot vs bot.")
    finally:
        session.close()


async def bvb_cancel_callback(update, context):
    q = update.callback_query
    try:
        owner_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer()
        return
    if q.from_user.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer("Cancelled")
    try: await q.edit_message_text("❌ <i>Cancelled.</i>", parse_mode="HTML")
    except Exception: pass


async def bvb_pickA_callback(update, context):
    """Step 2: A picked. Show picker for Team B."""
    q = update.callback_query
    try:
        parts = q.data.split("_")
        team_a_id = int(parts[2])
        overs = int(parts[3])
        owner_tg = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer()

    session = get_session()
    try:
        team_a = session.query(BotTeam).get(team_a_id)
        if not team_a:
            await q.edit_message_text("⚠️ Team not found.")
            return
        # Show all OTHER active teams
        teams = (session.query(BotTeam)
                 .filter(BotTeam.is_active == True,
                         BotTeam.id != team_a_id)
                 .order_by(BotTeam.name).all())
        viable = []
        for t in teams:
            cnt = (session.query(BotTeamPlayer)
                   .filter(BotTeamPlayer.bot_team_id == t.id).count())
            if cnt >= 11:
                viable.append(t)

        rows = []
        cur = []
        for t in viable:
            cur.append(InlineKeyboardButton(
                t.name,
                callback_data=f"bvb_pickB_{team_a_id}_{t.id}_{overs}_{owner_tg}"))
            if len(cur) >= 2:
                rows.append(cur); cur = []
        if cur: rows.append(cur)
        rows.append([InlineKeyboardButton("❌ Cancel",
                    callback_data=f"bvb_cancel_{owner_tg}")])

        await q.edit_message_text(
            f"🤖 <b>BOT vs BOT</b> — {overs} overs\n\n"
            f"<b>Team A:</b> {team_a.name} ✓\n\n"
            f"Pick <b>Team B</b>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        logger.exception("bvb_pickA_callback error")
        await q.edit_message_text("⚠️ Error picking team.")
    finally:
        session.close()


async def bvb_pickB_callback(update, context):
    """Step 3: B picked. Run the match."""
    q = update.callback_query
    try:
        parts = q.data.split("_")
        team_a_id = int(parts[2])
        team_b_id = int(parts[3])
        overs = int(parts[4])
        owner_tg = int(parts[5])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if q.from_user.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer("Starting...")

    session = get_session()
    try:
        team_a = session.query(BotTeam).get(team_a_id)
        team_b = session.query(BotTeam).get(team_b_id)
        if not team_a or not team_b:
            await q.edit_message_text("⚠️ Team(s) not found.")
            return

        # Get bot user
        bot_user = session.query(User).filter(User.telegram_id == BOT_TG_ID).first()
        if not bot_user:
            await q.edit_message_text("⚠️ Internal: bot user missing. Run /vsbot once first.")
            return

        # Build XI for each team via existing vsbot helpers
        from handlers.vsbot import _build_bot_team_xi
        xi_a = _build_bot_team_xi(session, team_a_id)
        xi_b = _build_bot_team_xi(session, team_b_id)
        if len(xi_a) < 11 or len(xi_b) < 11:
            await q.edit_message_text("⚠️ One or both teams don't have 11 players.")
            return

        # Create match: both user1 and user2 are the bot user
        m = Match(
            user1_id=bot_user.id, user2_id=bot_user.id,
            match_type=TYPE_BOTVSBOT,
            overs=overs,
            stadium=random.choice([
                "M.A. Chidambaram Stadium", "Wankhede Stadium",
                "Eden Gardens", "Lord's Cricket Ground", "MCG",
            ]),
            pitch_type=random.choice(["Flat", "Hard", "Green", "Dry", "Dusty"]),
            weather=random.choice(["Clear", "Hazy", "Cloudy"]),
            status="active",
            started_at=__import__("datetime").datetime.utcnow(),
        )
        session.add(m); session.flush()

        # Toss
        winner_team_id = random.choice([team_a_id, team_b_id])
        winner_team = team_a if winner_team_id == team_a_id else team_b
        m.toss_winner_id = bot_user.id
        # Bot toss decision: 60% bowl, 40% bat
        bat_first_team_id = winner_team_id if random.random() > 0.6 else (team_b_id if winner_team_id == team_a_id else team_a_id)

        # Set up state — both sides are bot
        bat_team = team_a if bat_first_team_id == team_a_id else team_b
        bowl_team = team_b if bat_team == team_a else team_a
        bat_xi = xi_a if bat_team == team_a else xi_b
        bowl_xi = xi_b if bowl_team == team_b else xi_a

        # Show result message + start
        await q.edit_message_text(
            f"🤖 <b>BOT vs BOT</b>\n\n"
            f"<b>{team_a.name}</b> 🆚 <b>{team_b.name}</b>\n"
            f"📍 {m.pitch_type} pitch · 🌤 {m.weather}\n"
            f"🏟 {m.stadium}\n"
            f"⏱ {overs} overs\n\n"
            f"🪙 <b>{winner_team.name}</b> won the toss and chose to "
            f"{'bat' if bat_first_team_id == winner_team_id else 'bowl'} first.\n\n"
            f"🏏 <b>Batting first:</b> {bat_team.name}\n"
            f"🎳 <b>Bowling first:</b> {bowl_team.name}",
            parse_mode="HTML",
        )

        # Pick openers & opening bowler via simple AI heuristics
        # (matches what /vsbot does for the bot side)
        striker_idx = 0
        non_striker_idx = 1
        # Opening bowler = highest bowl_rating in the bowling XI
        opening_bowler = max(bowl_xi, key=lambda p: p.get("bowl_rating", 0))

        # Build the match state used by match.py engine
        from handlers.match import _ss, A_PICK_DELIVERY
        from services.match_state_store import save_state

        s = {
            "match_id": m.id,
            "chat_id": q.message.chat_id,
            "is_vsbot": True,
            "is_bot_vs_bot": True,           # NEW flag — engine uses this to skip rewards
            "bat_user_tg": BOT_TG_ID,
            "bowl_user_tg": BOT_TG_ID,
            "bat_team_id": bat_team.id,
            "bowl_team_id": bowl_team.id,
            "bat_team_name": bat_team.name,
            "bowl_team_name": bowl_team.name,
            "bat_xi": bat_xi,
            "bowl_xi": bowl_xi,
            "batting_order": list(bat_xi),
            "bowling_order": list(bowl_xi),
            "striker_idx": striker_idx,
            "non_striker_idx": non_striker_idx,
            "current_bowler": opening_bowler,
            "prev_bowler_rid": None,
            "innings": 1,
            "current_over": 1,
            "current_ball": 0,
            "total_runs": 0,
            "total_wickets": 0,
            "extras_total": 0, "wides": 0, "noballs": 0, "legbyes": 0,
            "partnership_runs": 0, "partnership_balls": 0,
            "bat_stats": {}, "bowl_stats": {},
            "fow": [],
            "timeline": [],
            "overs": overs,
            "target": None,
            "current_delivery": None,
            "selected_variation": None,
            "pitch_type": m.pitch_type,
            "stadium": m.stadium,
            "weather": m.weather,
        }
        save_state(context, m.id, s, next_action=A_PICK_DELIVERY)
        session.commit()

        # Show opener cards
        from handlers.match import _send_batsman_card, _send_bowler_card
        await _send_batsman_card(context, q.message.chat_id, bat_xi[striker_idx], bat_team.id)
        await _send_batsman_card(context, q.message.chat_id, bat_xi[non_striker_idx], bat_team.id)
        await _send_bowler_card(context, q.message.chat_id, opening_bowler, bowl_team.id)

        # Kick off the bot loop — just call vsbot_auto_continue, it'll handle everything
        from handlers.vsbot import vsbot_auto_continue
        # tiny delay so all initial messages land in order
        await asyncio.sleep(1.0)
        await vsbot_auto_continue(context, m.id)

    except Exception:
        logger.exception("bvb_pickB_callback error")
        try:
            await q.edit_message_text("⚠️ Error starting bot vs bot match.")
        except Exception:
            pass
    finally:
        session.close()
