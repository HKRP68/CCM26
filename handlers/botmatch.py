"""/botmatch — spectate two bot teams playing each other.

User picks bot team A and bot team B, watches the entire match unfold
automatically. No rewards (since user isn't really playing).

Reuses the vsbot infrastructure: both sides use bot_ai for picks, the same
match state machine handles the flow, but `is_spectator=True` prevents reward
distribution and skips quest/achievement tracking.
"""

import logging
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, BotTeam, BotTeamPlayer, Player, Match
from services.bot_ai import BOT_TG_ID
from handlers.vsbot import _get_or_create_bot_user as _ensure_bot_user

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# /botmatch — entry point
# ════════════════════════════════════════════════════════════════════

async def botmatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User types /botmatch [overs?] — show team picker for side A."""
    tg = update.effective_user
    args = context.args or []
    overs = 5
    if args:
        try:
            overs = int(args[0])
            if overs < 1 or overs > 20:
                overs = 5
        except (ValueError, TypeError):
            overs = 5

    cid = update.effective_chat.id
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # One match per chat
        from handlers.match import _active_match_in_chat, _chat_busy_message
        existing = _active_match_in_chat(session, cid)
        if existing:
            await update.message.reply_text(
                _chat_busy_message(existing), parse_mode="HTML")
            return

        teams = (session.query(BotTeam)
                 .filter(BotTeam.is_active == True)
                 .order_by(BotTeam.name).all())
        if len(teams) < 2:
            await update.message.reply_text(
                "⚠️ Need at least 2 active bot teams. Ask the admin to create more in the website."
            )
            return

        # Show team picker for SIDE A
        btns = []
        row = []
        for t in teams:
            label = f"{t.flag or '🏳️'} {t.name}"
            row.append(InlineKeyboardButton(label, callback_data=f"botmatch_a_{t.id}_{overs}_{tg.id}"))
            if len(row) == 2:
                btns.append(row); row = []
        if row: btns.append(row)
        btns.append([InlineKeyboardButton("❌ Cancel", callback_data=f"botmatch_cancel_{tg.id}")])

        await update.message.reply_text(
            f"🎬 <b>SPECTATOR MATCH ({overs} overs)</b>\n\n"
            "Watch two bot teams play each other.\n"
            "<i>No rewards — pure entertainment.</i>\n\n"
            "<b>Step 1:</b> Pick Team A (top of innings)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(btns),
        )
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Step 2: Team A selected → pick team B
# ════════════════════════════════════════════════════════════════════

async def botmatch_pick_a_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """botmatch_a_<team_id>_<overs>_<owner_tg>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        team_a_id = int(parts[2]); overs = int(parts[3]); owner_tg = int(parts[4])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    session = get_session()
    try:
        team_a = session.query(BotTeam).get(team_a_id)
        if not team_a:
            await q.answer("Team not found")
            return
        # List remaining teams (excluding A)
        teams = (session.query(BotTeam)
                 .filter(BotTeam.is_active == True, BotTeam.id != team_a_id)
                 .order_by(BotTeam.name).all())
        if not teams:
            await q.answer("No opponent available", show_alert=True)
            return
        await q.answer()

        btns = []
        row = []
        for t in teams:
            label = f"{t.flag or '🏳️'} {t.name}"
            row.append(InlineKeyboardButton(label, callback_data=f"botmatch_b_{team_a_id}_{t.id}_{overs}_{tg.id}"))
            if len(row) == 2:
                btns.append(row); row = []
        if row: btns.append(row)
        btns.append([InlineKeyboardButton("❌ Cancel", callback_data=f"botmatch_cancel_{tg.id}")])

        await q.edit_message_text(
            f"🎬 <b>SPECTATOR MATCH ({overs} overs)</b>\n\n"
            f"<b>Team A:</b> {team_a.flag or '🏳️'} {team_a.name}\n\n"
            "<b>Step 2:</b> Pick Team B (bowling first)",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(btns),
        )
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Step 3: Both teams chosen → start match
# ════════════════════════════════════════════════════════════════════

async def botmatch_pick_b_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """botmatch_b_<a_id>_<b_id>_<overs>_<owner_tg>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        a_id = int(parts[2]); b_id = int(parts[3]); overs = int(parts[4]); owner_tg = int(parts[5])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    await q.answer()

    session = get_session()
    try:
        team_a = session.query(BotTeam).get(a_id)
        team_b = session.query(BotTeam).get(b_id)
        if not team_a or not team_b:
            await q.edit_message_text("⚠️ Team not found.")
            return

        # Build XIs from BotTeamPlayer rows
        xi_a = _build_bot_xi(session, team_a)
        xi_b = _build_bot_xi(session, team_b)

        if len(xi_a) < 11 or len(xi_b) < 11:
            await q.edit_message_text(
                f"⚠️ One of the teams doesn't have a full XI.\n"
                f"{team_a.name}: {len(xi_a)}/11\n"
                f"{team_b.name}: {len(xi_b)}/11\n\n"
                f"Ask admin to fill them out."
            )
            return

        # Get/create the singleton bot user (we use it for both sides)
        bot_user = _ensure_bot_user(session)

        # Create Match record (both users = bot user; spectator owner is the human)
        match = Match(
            user1_id=bot_user.id,
            user2_id=bot_user.id,
            overs=overs,
            status="in_progress",
            stadium=random.choice(["MCG", "Lord's", "Eden Gardens", "Wankhede", "The Oval"]),
            pitch_type=random.choice(["Flat", "Hard", "Green", "Dry", "Dusty"]),
            weather=random.choice(["Sunny", "Cloudy", "Hot", "Cool"]),
        )
        session.add(match); session.commit()
        mid = match.id

        await q.edit_message_text(
            f"🎬 <b>SPECTATOR MATCH</b>\n\n"
            f"{team_a.flag or '🏳️'} <b>{team_a.name}</b> vs {team_b.flag or '🏳️'} <b>{team_b.name}</b>\n"
            f"📍 {match.stadium} · 📍 {match.pitch_type} pitch · 🌤️ {match.weather}\n"
            f"⏱ {overs} overs each\n\n"
            f"<i>Starting in 2 seconds...</i>",
            parse_mode="HTML",
        )

        # Toss — random
        team_a_bats_first = random.random() < 0.5
        if team_a_bats_first:
            bat_team_id = team_a.id; bat_team_name = team_a.name; bat_xi = xi_a
            bowl_team_id = team_b.id; bowl_team_name = team_b.name; bowl_xi = xi_b
        else:
            bat_team_id = team_b.id; bat_team_name = team_b.name; bat_xi = xi_b
            bowl_team_id = team_a.id; bowl_team_name = team_a.name; bowl_xi = xi_a

        match.toss_winner_id = bot_user.id
        session.commit()

        # Build match state
        from handlers.match import _ss, A_PICK_DELIVERY, _send_batsman_card, _send_bowler_card
        from services.match_state_store import save_state

        chat_id = q.message.chat_id
        opener_idxs = [0, 1]
        # Pick first bowler — best non-batsman
        from services.bot_ai import pick_bot_next_bowler
        first_bowler = pick_bot_next_bowler(bowl_xi, None, {}, overs)

        state = {
            "match_id": mid,
            "chat_id": chat_id,
            "is_vsbot": True,           # reuse vsbot routing
            "is_spectator": True,       # NEW: signals no-reward mode
            "spectator_owner_tg": tg.id,
            # Both sides controlled by bot AI
            "bat_user_tg": BOT_TG_ID,
            "bowl_user_tg": BOT_TG_ID,
            "bat_team_id": bat_team_id,
            "bowl_team_id": bowl_team_id,
            "bat_team_name": bat_team_name,
            "bowl_team_name": bowl_team_name,
            "team_a_name": team_a.name,
            "team_b_name": team_b.name,
            "bat_xi": bat_xi,
            "bowl_xi": bowl_xi,
            "batting_order": list(bat_xi),
            "current_bowler": first_bowler,
            "striker_idx": 0, "non_striker_idx": 1,
            "current_over": 1, "current_ball": 0,
            "total_runs": 0, "total_wickets": 0,
            "extras_total": 0, "wides": 0, "noballs": 0, "legbyes": 0,
            "bat_stats": {}, "bowl_stats": {},
            "partnership_runs": 0, "partnership_balls": 0,
            "innings": 1, "overs": overs,
            "pitch_type": match.pitch_type,
            "weather": match.weather,
            "stadium": match.stadium,
            "timeline": [], "fow": [],
        }
        save_state(context, mid, state, next_action=A_PICK_DELIVERY)

        # Brief pause for read
        import asyncio
        await asyncio.sleep(2)

        await context.bot.send_message(
            chat_id,
            f"🪙 <b>Toss won by {bat_team_name}</b> — chose to bat\n\n"
            f"🏏 <b>1st INNINGS:</b> {bat_team_name}\n"
            f"🎳 Opening bowler: <b>{first_bowler['name']}</b>",
            parse_mode="HTML",
        )

        # Send opener cards
        op1 = bat_xi[0]; op2 = bat_xi[1]
        await _send_batsman_card(context, chat_id, op1, bat_team_id)
        await _send_batsman_card(context, chat_id, op2, bat_team_id)
        await _send_bowler_card(context, chat_id, first_bowler, bowl_team_id)

        # Kick off the auto-play loop
        from handlers.vsbot import vsbot_auto_continue
        await vsbot_auto_continue(context, mid)

    except Exception:
        logger.exception("botmatch start failed")
        try:
            await q.edit_message_text("⚠️ Failed to start spectator match.")
        except Exception:
            pass
    finally:
        session.close()


async def botmatch_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer("Cancelled")
    try:
        await q.edit_message_text("🎬 <i>Spectator match cancelled.</i>", parse_mode="HTML")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# Helper — build XI from a BotTeam
# ════════════════════════════════════════════════════════════════════

def _build_bot_xi(session, bot_team):
    """Return a list of player dicts (XI) for the bot team. Uses negative
    roster_id (-BotTeamPlayer.id) so it can't collide with real UserRoster ids."""
    rows = (session.query(BotTeamPlayer)
            .filter(BotTeamPlayer.team_id == bot_team.id)
            .order_by(BotTeamPlayer.position).limit(11).all())
    xi = []
    for btp in rows:
        p = session.query(Player).get(btp.player_id)
        if not p:
            continue
        xi.append({
            "roster_id": -btp.id,  # negative = bot player
            "player_id": p.id,
            "name": p.name,
            "country": p.country,
            "rating": p.rating,
            "category": p.category,
            "bat_rating": p.bat_rating,
            "bowl_rating": p.bowl_rating,
            "bat_hand": p.bat_hand,
            "bowl_hand": p.bowl_hand,
            "bowl_style": p.bowl_style,
            "is_bot_player": True,
        })
    return xi
