"""Handler for /playmatch — full match setup + ball-by-ball delivery/shot flow."""

import random
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster, Match
from services.match_constants import random_match_settings, MATCH_EXPIRE
from services.bowling_service import get_delivery_options, is_spinner, AVAILABLE_SHOTS, get_bowler_profile_key
from services.match_engine import (
    create_match_state, get_striker, get_non_striker, get_bowler,
    is_innings_over, format_score, format_overs, run_rate, get_phase,
)
from services.activity_service import log_activity

logger = logging.getLogger(__name__)


def _get_state(context, match_id):
    return context.bot_data.get(f"ms_{match_id}")


def _set_state(context, match_id, state):
    context.bot_data[f"ms_{match_id}"] = state


def _player_dict(entry, player):
    return {
        "roster_id": entry.id, "player_id": player.id,
        "name": player.name, "rating": player.rating,
        "category": player.category,
        "bat_rating": player.bat_rating, "bowl_rating": player.bowl_rating,
        "bowl_style": player.bowl_style, "bowl_hand": player.bowl_hand,
        "bat_hand": player.bat_hand,
    }


def _get_xi(session, user_id):
    rows = (
        session.query(UserRoster, Player)
        .join(Player, UserRoster.player_id == Player.id)
        .filter(UserRoster.user_id == user_id)
        .order_by(UserRoster.order_position)
        .limit(11).all()
    )
    return [_player_dict(e, p) for e, p in rows]


# ═══════════════════════════════════════════════════════════════════
# STEP 1: /playmatch @user2
# ═══════════════════════════════════════════════════════════════════

async def playmatch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    chat_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("Usage: /playmatch @username")
        return

    target_raw = context.args[0].lstrip("@").strip()
    session = get_session()
    try:
        user1 = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user1:
            await update.message.reply_text("❌ Do /debut first!")
            return
        if target_raw.lower() == (user1.username or "").lower():
            await update.message.reply_text("❌ Can't play yourself")
            return
        user2 = session.query(User).filter(User.username.ilike(target_raw)).first()
        if not user2:
            await update.message.reply_text(f"❌ @{target_raw} not found.")
            return

        r1 = session.query(UserRoster).filter(UserRoster.user_id == user1.id).count()
        r2 = session.query(UserRoster).filter(UserRoster.user_id == user2.id).count()
        if r1 < 11:
            await update.message.reply_text(f"❌ You need 11+ players ({r1}).")
            return
        if r2 < 11:
            await update.message.reply_text(f"❌ @{user2.username} needs 11+ players.")
            return

        settings = random_match_settings()
        now = datetime.utcnow()
        match = Match(
            user1_id=user1.id, user2_id=user2.id, status="pending",
            stadium=settings["stadium"], pitch_type=settings["pitch_type"],
            weather=settings["weather"], temperature=settings["temperature"],
            umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=chat_id, created_at=now,
            expires_at=now + timedelta(seconds=MATCH_EXPIRE))
        session.add(match)
        session.commit()

        t1 = user1.team_name or f"@{user1.username}'s XI"
        t2 = user2.team_name or f"@{user2.username}'s XI"

        text = (
            f"🔔 <b>NEW MATCH INVITATION!</b>\n\n"
            f"From: @{user1.username} to @{user2.username}\n\n"
            f"🏏 <b>CRICKET GURU MATCH</b>\n\n"
            f"{t1} vs {t2}\n"
            f"📍 Pitch: {settings['pitch_type']}\n"
            f"🌤️ Weather: {settings['weather']}\n"
            f"🌡️ Temperature: {settings['temperature']}°C\n"
            f"🏟️ Stadium: {settings['stadium']}\n"
            f"🎩 Umpire: {settings['umpire1']} | {settings['umpire2']}\n\n"
            f"⏳ Expires in: {MATCH_EXPIRE} seconds")

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Accept", callback_data=f"matchacc_{match.id}_{user2.id}"),
            InlineKeyboardButton("❌ Deny", callback_data=f"matchdeny_{match.id}_{user2.id}"),
        ]])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

        try:
            if context.job_queue:
                context.job_queue.run_once(
                    _auto_expire, MATCH_EXPIRE, name=f"match_{match.id}",
                    data={"match_id": match.id, "chat_id": chat_id})
        except Exception:
            pass
    except Exception:
        session.rollback()
        logger.exception("Playmatch error")
        await update.message.reply_text("⚠️ Error.")
    finally:
        session.close()


async def _auto_expire(context):
    d = context.job.data
    session = get_session()
    try:
        m = session.query(Match).get(d["match_id"])
        if m and m.status == "pending":
            m.status = "expired"
            session.commit()
            await context.bot.send_message(d["chat_id"], "⏰ Match invitation expired.")
    except Exception:
        session.rollback()
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════
# STEP 2: Accept / Deny
# ═══════════════════════════════════════════════════════════════════

async def match_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    match_id, allowed_uid = int(parts[1]), int(parts[2])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user or user.id != allowed_uid:
            await query.answer("Only the invited player can accept!")
            return
        await query.answer()
        match = session.query(Match).get(match_id)
        if not match or match.status != "pending":
            await query.edit_message_text("❌ Match no longer available.")
            return
        match.status = "accepted"
        session.commit()

        try:
            for j in context.job_queue.get_jobs_by_name(f"match_{match_id}"):
                j.schedule_removal()
        except Exception:
            pass

        u1 = session.query(User).get(match.user1_id)
        u2 = session.query(User).get(match.user2_id)
        t1 = u1.team_name or f"@{u1.username}'s XI"
        t2 = u2.team_name or f"@{u2.username}'s XI"

        await query.edit_message_text(
            f"✅ <b>MATCH ACCEPTED!</b>\n\n"
            f"🏟️ Cricket Guru Match\n{t1} vs {t2}\n\n"
            f"@{u2.username}, select overs (1-20):\n📝 Reply with: <code>20</code>",
            parse_mode="HTML")
        context.bot_data[f"awaiting_overs_{u2.telegram_id}"] = match_id
    except Exception:
        session.rollback()
        logger.exception("Match accept error")
    finally:
        session.close()


async def match_deny_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    match_id, allowed_uid = int(parts[1]), int(parts[2])
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user or user.id != allowed_uid:
            await query.answer("Only the invited player can deny!")
            return
        await query.answer()
        m = session.query(Match).get(match_id)
        if m and m.status == "pending":
            m.status = "expired"
            session.commit()
        await query.edit_message_text("❌ Match denied.")
    except Exception:
        session.rollback()
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════
# STEP 3: Overs selection (text)
# ═══════════════════════════════════════════════════════════════════

async def overs_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    chat_id = update.effective_chat.id
    key = f"awaiting_overs_{tg_user.id}"
    match_id = context.bot_data.get(key)
    if not match_id:
        return

    text = update.message.text.strip().lower().replace("overs", "").replace("over", "").strip()
    try:
        overs = int(text)
    except ValueError:
        await update.message.reply_text("❌ Enter a number (1-20)")
        return
    if overs < 1 or overs > 20:
        await update.message.reply_text("❌ Overs: 1-20")
        return

    del context.bot_data[key]

    session = get_session()
    try:
        match = session.query(Match).get(match_id)
        if not match or match.status != "accepted":
            return
        match.overs = overs
        match.status = "toss"
        session.commit()

        u1 = session.query(User).get(match.user1_id)
        u2 = session.query(User).get(match.user2_id)
        t1 = u1.team_name or f"@{u1.username}'s XI"
        t2 = u2.team_name or f"@{u2.username}'s XI"

        await update.message.reply_text(
            f"✅ <b>MATCH CONFIRMED!</b>\n\n"
            f"🏏 {t1} vs {t2}\n"
            f"📍 {overs} Overs | {match.stadium}\n"
            f"📍 {match.pitch_type} | {match.weather} {match.temperature}°C\n"
            f"🎩 {match.umpire1} | {match.umpire2}\n\n"
            f"🔄 Getting ready for toss...", parse_mode="HTML")

        # Toss
        winner_id = random.choice([match.user1_id, match.user2_id])
        match.toss_winner_id = winner_id
        session.commit()
        winner = session.query(User).get(winner_id)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏏 Bat First", callback_data=f"toss_bat_{match_id}_{winner_id}"),
            InlineKeyboardButton("🎳 Bowl First", callback_data=f"toss_bowl_{match_id}_{winner_id}"),
        ]])
        await context.bot.send_message(chat_id,
            f"🪙 <b>TOSS TIME!</b>\n\n🏆 @{winner.username} wins the toss!\n\n"
            f"@{winner.username}, what will you do?",
            parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        session.rollback()
        logger.exception("Overs error")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════
# STEP 4: Toss decision
# ═══════════════════════════════════════════════════════════════════

async def toss_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    decision, match_id, winner_id = parts[1], int(parts[2]), int(parts[3])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user or user.id != winner_id:
            await query.answer("Only the toss winner can decide!")
            return
        await query.answer()

        match = session.query(Match).get(match_id)
        if not match or match.status != "toss":
            await query.edit_message_text("❌ Match error.")
            return

        match.toss_decision = decision
        if decision == "bat":
            match.batting_first_id = winner_id
            match.bowling_first_id = match.user2_id if winner_id == match.user1_id else match.user1_id
        else:
            match.bowling_first_id = winner_id
            match.batting_first_id = match.user2_id if winner_id == match.user1_id else match.user1_id
        match.status = "selecting"
        session.commit()

        choice = "BAT FIRST" if decision == "bat" else "BOWL FIRST"
        await query.edit_message_text(f"✅ @{user.username} elected to {choice}\n\n⏳ Loading XIs...")

        chat_id = query.message.chat_id
        bat_user = session.query(User).get(match.batting_first_id)
        bowl_user = session.query(User).get(match.bowling_first_id)
        bat_xi = _get_xi(session, bat_user.id)
        bowl_xi = _get_xi(session, bowl_user.id)
        bt = bat_user.team_name or f"@{bat_user.username}'s XI"
        bwt = bowl_user.team_name or f"@{bowl_user.username}'s XI"

        lines = [f"🏏 <b>{bt} XI</b> (Batting):"]
        for i, p in enumerate(bat_xi, 1):
            lines.append(f"  {i}. {p['name']} - {p['rating']} | {p['category']}")
        lines.append(f"\n🎳 <b>{bwt} XI</b> (Bowling):")
        for i, p in enumerate(bowl_xi, 1):
            lines.append(f"  {i}. {p['name']} - {p['rating']} | {p['category']}")

        await context.bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")

        # Store XIs for later
        context.bot_data[f"bat_xi_{match_id}"] = bat_xi
        context.bot_data[f"bowl_xi_{match_id}"] = bowl_xi

        # Opener 1 selection
        batsmen = [p for p in bat_xi if p["category"] in ("Batsman", "Wicket Keeper", "All-rounder")]
        if len(batsmen) < 2:
            batsmen = bat_xi[:6]

        buttons = [[InlineKeyboardButton(
            f"{p['name']} - {p['rating']}",
            callback_data=f"op1_{match_id}_{bat_user.id}_{p['roster_id']}"
        )] for p in batsmen[:8]]

        await context.bot.send_message(chat_id,
            f"🏏 <b>SELECT OPENER 1</b>\n\n@{bat_user.username}, pick first opener:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        session.rollback()
        logger.exception("Toss error")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════
# STEP 5-6: Opener selection
# ═══════════════════════════════════════════════════════════════════

async def opener1_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    match_id, bat_uid, rid = int(parts[1]), int(parts[2]), int(parts[3])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user or user.id != bat_uid:
            await query.answer("Not your pick!")
            return
        await query.answer()

        bat_xi = context.bot_data.get(f"bat_xi_{match_id}", [])
        picked = next((p for p in bat_xi if p["roster_id"] == rid), None)
        if not picked:
            return
        context.bot_data[f"opener1_{match_id}"] = picked

        remaining = [p for p in bat_xi if p["roster_id"] != rid and
                     p["category"] in ("Batsman", "Wicket Keeper", "All-rounder")]
        if not remaining:
            remaining = [p for p in bat_xi if p["roster_id"] != rid]

        buttons = [[InlineKeyboardButton(
            f"{p['name']} - {p['rating']}",
            callback_data=f"op2_{match_id}_{bat_uid}_{p['roster_id']}"
        )] for p in remaining[:8]]

        await query.edit_message_text(
            f"✅ Opener 1: {picked['name']} - {picked['rating']}\n\n"
            f"🏏 <b>SELECT OPENER 2</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        logger.exception("Opener1 error")
    finally:
        session.close()


async def opener2_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    chat_id = query.message.chat_id
    parts = query.data.split("_")
    match_id, bat_uid, rid = int(parts[1]), int(parts[2]), int(parts[3])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user or user.id != bat_uid:
            await query.answer("Not your pick!")
            return
        await query.answer()

        bat_xi = context.bot_data.get(f"bat_xi_{match_id}", [])
        picked = next((p for p in bat_xi if p["roster_id"] == rid), None)
        if not picked:
            return
        context.bot_data[f"opener2_{match_id}"] = picked

        op1 = context.bot_data.get(f"opener1_{match_id}", {})
        await query.edit_message_text(
            f"✅ <b>Opening Partnership:</b>\n"
            f"1. {op1.get('name')} - {op1.get('rating')}\n"
            f"2. {picked['name']} - {picked['rating']}\n\n"
            f"⏳ Waiting for bowler...", parse_mode="HTML")

        # Bowler selection
        match = session.query(Match).get(match_id)
        bowl_user = session.query(User).get(match.bowling_first_id)
        bowl_xi = context.bot_data.get(f"bowl_xi_{match_id}", [])
        bowlers = sorted(
            [p for p in bowl_xi if p["category"] in ("Bowler", "All-rounder")],
            key=lambda x: x["bowl_rating"], reverse=True)
        if not bowlers:
            bowlers = sorted(bowl_xi, key=lambda x: x["bowl_rating"], reverse=True)

        buttons = [[InlineKeyboardButton(
            f"{p['name']} - {p['bowl_rating']} BWL | {p['bowl_style']}",
            callback_data=f"selbowl_{match_id}_{bowl_user.id}_{p['roster_id']}"
        )] for p in bowlers[:8]]

        await context.bot.send_message(chat_id,
            f"🎳 <b>SELECT OPENING BOWLER</b>\n\n@{bowl_user.username}, pick your bowler:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        logger.exception("Opener2 error")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════
# STEP 7: Bowler selected → create match state → first delivery
# ═══════════════════════════════════════════════════════════════════

async def select_bowler_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    chat_id = query.message.chat_id
    parts = query.data.split("_")
    match_id, bowl_uid, rid = int(parts[1]), int(parts[2]), int(parts[3])

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user or user.id != bowl_uid:
            await query.answer("Not your pick!")
            return
        await query.answer()

        match = session.query(Match).get(match_id)
        match.status = "playing"
        session.commit()

        bowl_xi = context.bot_data.get(f"bowl_xi_{match_id}", [])
        bowler = next((p for p in bowl_xi if p["roster_id"] == rid), None)
        if not bowler:
            return

        bat_xi = context.bot_data.get(f"bat_xi_{match_id}", [])
        op1 = context.bot_data.get(f"opener1_{match_id}", {})
        op2 = context.bot_data.get(f"opener2_{match_id}", {})

        bat_user = session.query(User).get(match.batting_first_id)
        bowl_user = session.query(User).get(match.bowling_first_id)
        bt = bat_user.team_name or f"@{bat_user.username}'s XI"
        bwt = bowl_user.team_name or f"@{bowl_user.username}'s XI"

        await query.edit_message_text(
            f"✅ Opening Bowler: {bowler['name']}\n\n⏳ Match Starting...",
            parse_mode="HTML")

        # Create match state
        state = create_match_state(
            match_id, match.overs, match.batting_first_id, match.bowling_first_id,
            bat_xi, bowl_xi, op1, op2, bowler)
        state["chat_id"] = chat_id
        state["bat_user_tg"] = bat_user.telegram_id
        state["bowl_user_tg"] = bowl_user.telegram_id
        state["bat_team_name"] = bt
        state["bowl_team_name"] = bwt
        _set_state(context, match_id, state)

        await context.bot.send_message(chat_id,
            f"🏏 <b>MATCH STARTING!</b>\n\n"
            f"🏟️ {match.stadium}\n"
            f"{bt} vs {bwt} | {match.overs} Overs\n\n"
            f"🏏 {op1['name']} & {op2['name']}\n"
            f"🎳 {bowler['name']}\n\n"
            f"━━━━━━━━━━━━━━━━━━━", parse_mode="HTML")

        # Show first delivery selection
        await _show_delivery_selection(context, chat_id, match_id)

    except Exception:
        session.rollback()
        logger.exception("Select bowler error")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════
# DELIVERY SELECTION
# ═══════════════════════════════════════════════════════════════════

async def _show_delivery_selection(context, chat_id, match_id):
    """Show delivery options to the bowling user."""
    state = _get_state(context, match_id)
    if not state:
        return

    bowler = get_bowler(state)
    striker = get_striker(state)
    ov = state["current_over"]
    ball = state["current_ball"] + 1
    phase = get_phase(state)

    opts = get_delivery_options(bowler["bowl_style"], bowler["bowl_hand"])

    header = (
        f"🎳 <b>OVER {ov} • BALL {ball}</b>\n\n"
        f"📊 Score: {format_score(state)} | Overs: {format_overs(state)} | RR: {run_rate(state)}\n\n"
        f"🎳 {bowler['name']} ({bowler['bowl_rating']} BWL) | {bowler['bowl_style']}\n"
        f"🏏 vs {striker['name']} ({striker['bat_rating']} BAT)\n"
        f"📍 Phase: {phase}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
    )

    if opts["is_spinner"]:
        # Single step — show deliveries directly
        deliveries = opts["deliveries"]
        buttons = []
        row = []
        for i, d in enumerate(deliveries):
            row.append(InlineKeyboardButton(d, callback_data=f"bspin_{match_id}_{i}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        await context.bot.send_message(chat_id,
            header + "🎯 <b>SELECT DELIVERY</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        # Two-step — show variations first
        variations = opts["variations"]
        buttons = []
        row = []
        for i, v in enumerate(variations):
            row.append(InlineKeyboardButton(v, callback_data=f"bvar_{match_id}_{i}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        await context.bot.send_message(chat_id,
            header + "🎯 <b>SELECT LINE / VARIATION</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# Pacer step 1: variation selected → show lengths

async def variation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    match_id, var_idx = int(parts[1]), int(parts[2])

    state = _get_state(context, match_id)
    if not state:
        return
    if tg_user.id != state["bowl_user_tg"]:
        await query.answer("Not your turn to bowl!")
        return
    await query.answer()

    bowler = get_bowler(state)
    opts = get_delivery_options(bowler["bowl_style"], bowler["bowl_hand"])
    variation = opts["variations"][var_idx]

    state["selected_variation"] = variation
    _set_state(context, match_id, state)

    lengths = opts["lengths"]
    buttons = []
    row = []
    for i, l in enumerate(lengths):
        row.append(InlineKeyboardButton(l, callback_data=f"blen_{match_id}_{i}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await query.edit_message_text(
        f"🎳 <b>SELECT LENGTH</b> ({variation})",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


# Pacer step 2: length selected → finalize delivery

async def length_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    match_id, len_idx = int(parts[1]), int(parts[2])

    state = _get_state(context, match_id)
    if not state:
        return
    if tg_user.id != state["bowl_user_tg"]:
        await query.answer("Not your turn!")
        return
    await query.answer()

    bowler = get_bowler(state)
    opts = get_delivery_options(bowler["bowl_style"], bowler["bowl_hand"])
    length = opts["lengths"][len_idx]
    variation = state.get("selected_variation", "Seam Up")

    delivery_text = f"{variation} {length}"
    state["current_delivery"] = delivery_text
    state["selected_variation"] = None
    _set_state(context, match_id, state)

    await query.edit_message_text(
        f"✅ <b>DELIVERY SELECTED</b>\n\n"
        f"🎳 {bowler['name']}: {delivery_text}\n\n"
        f"⏳ Waiting for batsman...", parse_mode="HTML")

    await _show_shot_selection(context, state["chat_id"], match_id)


# Spinner: delivery selected directly

async def spinner_delivery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    match_id, del_idx = int(parts[1]), int(parts[2])

    state = _get_state(context, match_id)
    if not state:
        return
    if tg_user.id != state["bowl_user_tg"]:
        await query.answer("Not your turn!")
        return
    await query.answer()

    bowler = get_bowler(state)
    opts = get_delivery_options(bowler["bowl_style"], bowler["bowl_hand"])
    delivery = opts["deliveries"][del_idx]

    # Handle "Surprise" — random from rest
    if delivery == "Surprise":
        non_surprise = [d for d in opts["deliveries"] if d != "Surprise"]
        delivery = random.choice(non_surprise) + " (Surprise)"

    state["current_delivery"] = delivery
    _set_state(context, match_id, state)

    await query.edit_message_text(
        f"✅ <b>DELIVERY SELECTED</b>\n\n"
        f"🎳 {bowler['name']}: {delivery}\n\n"
        f"⏳ Waiting for batsman...", parse_mode="HTML")

    await _show_shot_selection(context, state["chat_id"], match_id)


# ═══════════════════════════════════════════════════════════════════
# SHOT SELECTION
# ═══════════════════════════════════════════════════════════════════

async def _show_shot_selection(context, chat_id, match_id):
    state = _get_state(context, match_id)
    if not state:
        return

    striker = get_striker(state)
    bowler = get_bowler(state)
    delivery = state.get("current_delivery", "?")
    bs = state["bat_stats"].get(striker["roster_id"], {})

    text = (
        f"🏏 <b>OVER {state['current_over']} • BALL {state['current_ball'] + 1}</b>\n\n"
        f"📊 {format_score(state)} | Overs: {format_overs(state)} | RR: {run_rate(state)}\n\n"
        f"🎳 {bowler['name']}: {delivery}\n"
        f"🏏 {striker['name']} ({striker['bat_rating']} BAT) — "
        f"{bs.get('runs', 0)}({bs.get('balls', 0)})\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 <b>SELECT YOUR SHOT</b>"
    )

    buttons = []
    row = []
    for i, shot in enumerate(AVAILABLE_SHOTS):
        row.append(InlineKeyboardButton(shot, callback_data=f"bshot_{match_id}_{i}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await context.bot.send_message(chat_id, text, parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(buttons))


async def shot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    match_id, shot_idx = int(parts[1]), int(parts[2])

    state = _get_state(context, match_id)
    if not state:
        return
    if tg_user.id != state["bat_user_tg"]:
        await query.answer("Not your turn to bat!")
        return
    await query.answer()

    shot = AVAILABLE_SHOTS[shot_idx]
    delivery = state.get("current_delivery", "?")
    striker = get_striker(state)
    bowler = get_bowler(state)

    # ── Calculate outcome (simplified — will be refined in Part 3) ──
    outcome = _calculate_outcome(state, striker, bowler, shot, delivery)

    # Update state
    state["current_ball"] += 1
    bs = state["bat_stats"][striker["roster_id"]]
    bws = state["bowl_stats"].get(bowler["roster_id"], {"balls": 0, "runs": 0, "wickets": 0})
    state["bowl_stats"][bowler["roster_id"]] = bws

    bs["balls"] += 1
    bws["balls"] += 1

    result_text = ""
    if outcome["type"] == "runs":
        runs = outcome["runs"]
        state["total_runs"] += runs
        bs["runs"] += runs
        bws["runs"] += runs
        if runs == 4:
            bs["fours"] += 1
        elif runs == 6:
            bs["sixes"] += 1
        state["over_balls"].append(str(runs))

        emojis = {0: "⚫", 1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "🟢", 6: "🔴"}
        emoji = emojis.get(runs, "🔵")
        commentary = outcome.get("commentary", "")
        result_text = f"{emoji} <b>{runs} RUN{'S' if runs != 1 else ''}!</b> {commentary}"

        # Rotate strike on odd runs
        if runs % 2 == 1:
            state["striker_idx"], state["non_striker_idx"] = state["non_striker_idx"], state["striker_idx"]

    elif outcome["type"] == "wicket":
        state["total_wickets"] += 1
        bws["wickets"] += 1
        bws["runs"] += outcome.get("runs", 0)
        state["total_runs"] += outcome.get("runs", 0)
        bs["out"] = True
        bs["how_out"] = outcome.get("how", "Bowled")
        state["over_balls"].append("W")
        result_text = f"🔴 <b>WICKET!</b> {striker['name']} {outcome.get('how', 'OUT')}!"

        # Next batsman
        if state["next_batsman_idx"] < len(state["batting_order"]):
            state["striker_idx"] = state["next_batsman_idx"]
            state["next_batsman_idx"] += 1

    # End of over?
    if state["current_ball"] >= 6:
        state["current_over"] += 1
        state["current_ball"] = 0
        state["over_balls"] = []
        # Swap strike
        state["striker_idx"], state["non_striker_idx"] = state["non_striker_idx"], state["striker_idx"]
        # Need new bowler next over
        state["prev_bowler_rid"] = bowler["roster_id"]

    _set_state(context, match_id, state)

    # Build result message
    new_striker = get_striker(state)
    over_display = " ".join(state["over_balls"]) if state["over_balls"] else "New Over"
    msg = (
        f"🎳 {bowler['name']} → {delivery}\n"
        f"🏏 {striker['name']} played {shot}\n\n"
        f"{result_text}\n\n"
        f"📊 <b>{format_score(state)}</b> | {format_overs(state)} ov | RR {run_rate(state)}\n"
        f"This over: {over_display}"
    )

    await query.edit_message_text(msg, parse_mode="HTML")

    # Check innings end
    if is_innings_over(state):
        await _end_innings(context, match_id)
        return

    # If new over, ask for new bowler
    if state["current_ball"] == 0:
        await _show_bowler_selection_new_over(context, match_id)
    else:
        await _show_delivery_selection(context, state["chat_id"], match_id)


def _calculate_outcome(state, striker, bowler, shot, delivery):
    """Simple outcome calculator based on ratings."""
    bat_r = striker["bat_rating"]
    bowl_r = bowler["bowl_rating"]
    diff = bat_r - bowl_r  # positive = batsman advantage

    r = random.random() * 100

    # Wicket chance: higher if bowler is better
    wicket_base = 8 - (diff * 0.08)
    wicket_base = max(3, min(18, wicket_base))

    if r < wicket_base:
        hows = ["Bowled", "Caught", "LBW", "Caught Behind", "Caught & Bowled", "Stumped"]
        return {"type": "wicket", "runs": 0, "how": random.choice(hows)}

    # Dot ball
    dot_base = 35 - diff * 0.2
    dot_base = max(15, min(50, dot_base))
    if r < wicket_base + dot_base:
        return {"type": "runs", "runs": 0, "commentary": "Dot ball"}

    # Singles/doubles
    if r < wicket_base + dot_base + 25:
        runs = random.choice([1, 1, 1, 2, 2, 3])
        return {"type": "runs", "runs": runs, "commentary": ""}

    # Boundaries
    if r < wicket_base + dot_base + 25 + 18:
        return {"type": "runs", "runs": 4, "commentary": "FOUR! 🔥"}

    return {"type": "runs", "runs": 6, "commentary": "SIX! 💥"}


# ═══════════════════════════════════════════════════════════════════
# NEW OVER → bowler selection
# ═══════════════════════════════════════════════════════════════════

async def _show_bowler_selection_new_over(context, match_id):
    state = _get_state(context, match_id)
    if not state:
        return

    bowl_xi = state["bowl_xi"]
    prev_rid = state.get("prev_bowler_rid")

    # Can't bowl same bowler consecutively
    available = [p for p in bowl_xi
                 if p["roster_id"] != prev_rid and
                 p["category"] in ("Bowler", "All-rounder")]
    if not available:
        available = [p for p in bowl_xi if p["roster_id"] != prev_rid]

    available = sorted(available, key=lambda x: x["bowl_rating"], reverse=True)

    buttons = [[InlineKeyboardButton(
        f"{p['name']} - {p['bowl_rating']} BWL",
        callback_data=f"nbowl_{match_id}_{p['roster_id']}"
    )] for p in available[:8]]

    await context.bot.send_message(state["chat_id"],
        f"🎳 <b>OVER {state['current_over']}</b> — Select bowler:\n\n"
        f"📊 {format_score(state)} | {format_overs(state)} ov",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def new_over_bowler_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    match_id, rid = int(parts[1]), int(parts[2])

    state = _get_state(context, match_id)
    if not state:
        return
    if tg_user.id != state["bowl_user_tg"]:
        await query.answer("Not your pick!")
        return
    await query.answer()

    bowler = next((p for p in state["bowl_xi"] if p["roster_id"] == rid), None)
    if not bowler:
        return

    state["current_bowler"] = bowler
    _set_state(context, match_id, state)

    await query.edit_message_text(
        f"🎳 Over {state['current_over']}: {bowler['name']}",
        parse_mode="HTML")

    await _show_delivery_selection(context, state["chat_id"], match_id)


# ═══════════════════════════════════════════════════════════════════
# END OF INNINGS
# ═══════════════════════════════════════════════════════════════════

async def _end_innings(context, match_id):
    state = _get_state(context, match_id)
    if not state:
        return

    chat_id = state["chat_id"]

    if state["innings"] == 1:
        target = state["total_runs"] + 1
        await context.bot.send_message(chat_id,
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>END OF 1ST INNINGS</b>\n\n"
            f"🏏 {state['bat_team_name']}: {format_score(state)} ({format_overs(state)} ov)\n\n"
            f"🎯 Target: {target}\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"⏳ 2nd Innings starting...\n"
            f"🏏 {state['bowl_team_name']} needs {target} to win",
            parse_mode="HTML")

        # Swap innings
        state["innings"] = 2
        state["target"] = target
        first_inn_score = state["total_runs"]
        first_inn_wickets = state["total_wickets"]
        state["total_runs"] = 0
        state["total_wickets"] = 0
        state["current_over"] = 1
        state["current_ball"] = 0
        state["over_balls"] = []
        state["bat_team_id"], state["bowl_team_id"] = state["bowl_team_id"], state["bat_team_id"]
        state["bat_user_tg"], state["bowl_user_tg"] = state["bowl_user_tg"], state["bat_user_tg"]
        state["bat_team_name"], state["bowl_team_name"] = state["bowl_team_name"], state["bat_team_name"]
        state["bat_xi"], state["bowl_xi"] = state["bowl_xi"], state["bat_xi"]
        state["batting_order"] = list(state["bat_xi"])
        state["striker_idx"] = 0
        state["non_striker_idx"] = 1
        state["next_batsman_idx"] = 2
        state["prev_bowler_rid"] = None
        state["selected_variation"] = None

        # Reset bat/bowl stats for 2nd innings
        state["bat_stats"] = {p["roster_id"]: {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "out": False} for p in state["bat_xi"]}
        state["bowl_stats"] = {p["roster_id"]: {"balls": 0, "runs": 0, "wickets": 0} for p in state["bowl_xi"]}

        _set_state(context, match_id, state)

        # Ask for openers and bowler for 2nd innings
        batsmen = [p for p in state["bat_xi"] if p["category"] in ("Batsman", "Wicket Keeper", "All-rounder")]
        if len(batsmen) < 2:
            batsmen = state["bat_xi"][:6]
        bat_uid = state["bat_team_id"]
        buttons = [[InlineKeyboardButton(
            f"{p['name']} - {p['rating']}",
            callback_data=f"op1_{match_id}_{bat_uid}_{p['roster_id']}"
        )] for p in batsmen[:8]]

        await context.bot.send_message(chat_id,
            f"🏏 <b>2ND INNINGS — SELECT OPENER 1</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

    else:
        # Match complete
        session = get_session()
        try:
            match = session.query(Match).get(match_id)
            if match:
                match.status = "completed"
                match.completed_at = datetime.utcnow()
                session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

        target = state["target"]
        chasing_runs = state["total_runs"]
        chasing_wickets = state["total_wickets"]

        if chasing_runs >= target:
            winner_name = state["bat_team_name"]
            margin = f"by {10 - chasing_wickets} wickets"
        else:
            winner_name = state["bowl_team_name"]
            margin = f"by {target - 1 - chasing_runs} runs"

        await context.bot.send_message(chat_id,
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏆 <b>MATCH RESULT</b>\n\n"
            f"🏏 {state['bowl_team_name']}: {target - 1}/{10 - (10 - state.get('first_wickets', 0))} (1st inn)\n"
            f"🏏 {state['bat_team_name']}: {format_score(state)} ({format_overs(state)} ov)\n\n"
            f"🏆 <b>{winner_name} wins {margin}!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━",
            parse_mode="HTML")

        # Cleanup
        for k in list(context.bot_data.keys()):
            if str(match_id) in k:
                del context.bot_data[k]
