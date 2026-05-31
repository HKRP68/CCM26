"""Two-player /cm challenge mode built on the regular match engine."""

import random
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import Match, Player, User, UserRoster
from services.match_constants import MATCH_EXPIRE, random_match_settings
from handlers.match import _active_match_in_chat, _gxi, _mention


def _max_overs(session=None):
    """Return the website-configured challenge limit, clamped defensively."""
    from services.config_service import get_challenge_max_overs
    return get_challenge_max_overs(session)


def _state(context, match_id):
    return context.bot_data.setdefault(f"challenge_{match_id}", {"bat": [], "bowl": []})


def _picker(match_id, user, players, kind, chosen):
    title = "BATSMAN" if kind == "bat" else "BOWLER"
    rows = []
    for player in players:
        picked = player["roster_id"] in chosen
        label = f"{'✅ ' if picked else ''}{player['name']} — {player['rating']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"cm_pick_{kind}_{match_id}_{user.id}_{player['roster_id']}")])
    return (
        f"{'🏏' if kind == 'bat' else '🎳'} <b>CHALLENGE MODE — SELECT 2 {title}S</b>\n\n"
        f"{_mention(user)}, choose {2 - len(chosen)} more. A player cannot be selected twice.",
        InlineKeyboardMarkup(rows),
    )


def _xi_error(count):
    if count == 0:
        return "❌ You do not have a squad yet. Use /debut first, then accept the challenge again."
    return f"❌ You need a playing XI before accepting challenge mode. You currently have {count}/11 players. Use /autobuild after completing your squad."


async def challenge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: <code>/cm @username</code>", parse_mode="HTML")
        return
    target_name = context.args[0].lstrip("@").strip()
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
        count = session.query(UserRoster).filter(UserRoster.user_id == challenger.id).count()
        if count < 11:
            await update.message.reply_text(_xi_error(count))
            return
        existing = _active_match_in_chat(session, update.effective_chat.id)
        if existing:
            await update.message.reply_text("❌ This chat already has an active match invitation or match.")
            return
        settings = random_match_settings()
        now = datetime.utcnow()
        match = Match(
            user1_id=challenger.id, user2_id=target.id, status="pending",
            stadium=settings["stadium"], pitch_type=settings["pitch_type"], weather=settings["weather"],
            temperature=settings["temperature"], umpire1=settings["umpire1"], umpire2=settings["umpire2"],
            chat_id=update.effective_chat.id, created_at=now, expires_at=now + timedelta(seconds=MATCH_EXPIRE),
        )
        session.add(match); session.commit()
        _state(context, match.id)["mode"] = "challenge"
        await update.message.reply_text(
            f"⚔️ <b>CHALLENGE MODE</b>\n\n{_mention(challenger)} challenged {_mention(target)}.\n"
            f"🏏 2 batsmen vs 2 bowlers\n🎯 2 wickets per innings\n⏱ Maximum {_max_overs(session)} overs\n\n"
            "The invited player needs a completed XI. If they have no squad yet, they should use /debut first.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept", callback_data=f"cm_accept_{match.id}_{target.id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"cm_deny_{match.id}_{target.id}"),
            ]]),
        )
    finally:
        session.close()


async def challenge_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, match_id, invited_id = query.data.split("_")
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != int(invited_id):
            await query.answer("Only the invited player can accept.", show_alert=True); return
        match = session.query(Match).get(int(match_id))
        if not match or match.status != "pending":
            await query.answer("This invitation is no longer active.", show_alert=True); return
        count = session.query(UserRoster).filter(UserRoster.user_id == user.id).count()
        if count < 11:
            await query.answer(_xi_error(count), show_alert=True); return
        match.status = "toss"; match.overs = _max_overs(session)
        winner_id = random.choice([match.user1_id, match.user2_id]); match.toss_winner_id = winner_id
        session.commit()
        winner = session.query(User).get(winner_id)
        await query.answer("Challenge accepted!")
        await query.edit_message_text(
            f"🪙 <b>CHALLENGE TOSS</b>\n\n{_mention(winner)} won the toss. Choose your call:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏏 Bat First", callback_data=f"cm_toss_bat_{match.id}_{winner_id}"),
                InlineKeyboardButton("🎳 Bowl First", callback_data=f"cm_toss_bowl_{match.id}_{winner_id}"),
            ]]))
    finally:
        session.close()


async def challenge_deny_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, match_id, invited_id = query.data.split("_")
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != int(invited_id):
            await query.answer("Only the invited player can deny.", show_alert=True); return
        match = session.query(Match).get(int(match_id))
        if match and match.status == "pending": match.status = "expired"; session.commit()
        await query.answer(); await query.edit_message_text("❌ Challenge denied.")
    finally:
        session.close()


async def challenge_toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, decision, match_id, winner_id = query.data.split("_")
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not user or user.id != int(winner_id):
            await query.answer("Toss winner only.", show_alert=True); return
        match = session.query(Match).get(int(match_id))
        if not match or match.status != "toss":
            await query.answer("This toss is no longer active.", show_alert=True); return
        other = match.user2_id if user.id == match.user1_id else match.user1_id
        match.toss_decision = decision
        match.batting_first_id, match.bowling_first_id = ((user.id, other) if decision == "bat" else (other, user.id))
        match.status = "selecting"; session.commit()
        batting = session.query(User).get(match.batting_first_id)
        state = _state(context, match.id); state.update({"bat_uid": batting.id, "bowl_uid": match.bowling_first_id, "bat": [], "bowl": []})
        text, keyboard = _picker(match.id, batting, _gxi(session, batting.id), "bat", [])
        await query.answer(); await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    finally:
        session.close()


async def challenge_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, _, kind, match_id, owner_id, roster_id = query.data.split("_")
    match_id, owner_id, roster_id = int(match_id), int(owner_id), int(roster_id)
    session = get_session()
    try:
        owner = session.query(User).filter(User.telegram_id == query.from_user.id).first()
        if not owner or owner.id != owner_id:
            await query.answer("That picker belongs to the other captain.", show_alert=True); return
        state = _state(context, match_id); key = "bat" if kind == "bat" else "bowl"
        xi = _gxi(session, owner.id)
        if roster_id not in {p["roster_id"] for p in xi}:
            await query.answer("That player is not in your XI.", show_alert=True); return
        if roster_id in state[key]:
            await query.answer("You already picked that player.", show_alert=True); return
        state[key].append(roster_id)
        await query.answer("Player selected.")
        if len(state[key]) < 2:
            text, keyboard = _picker(match_id, owner, xi, kind, state[key])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard); return
        if kind == "bat":
            bowler = session.query(User).get(state["bowl_uid"])
            text, keyboard = _picker(match_id, bowler, _gxi(session, bowler.id), "bowl", [])
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard); return
        batting = session.query(User).get(state["bat_uid"]); bowling = session.query(User).get(state["bowl_uid"])
        batsmen = [p for p in _gxi(session, batting.id) if p["roster_id"] in state["bat"]]
        bowlers = [p for p in _gxi(session, bowling.id) if p["roster_id"] in state["bowl"]]
        context.bot_data[f"bat_xi_{match_id}"] = batsmen; context.bot_data[f"bowl_xi_{match_id}"] = bowlers
        context.bot_data[f"bat_uid_{match_id}"] = batting.id; context.bot_data[f"bowl_uid_{match_id}"] = bowling.id
        context.bot_data[f"bat_uname_{match_id}"] = batting.username; context.bot_data[f"bowl_uname_{match_id}"] = bowling.username
        rows = [[InlineKeyboardButton(p["name"], callback_data=f"op1_{match_id}_{batting.id}_{p['roster_id']}")] for p in batsmen]
        await query.edit_message_text(
            f"✅ <b>CHALLENGE SQUADS LOCKED</b>\n\n🏏 {_mention(batting)} selected 2 batsmen.\n"
            f"🎳 {_mention(bowling)} selected 2 bowlers.\n\n{_mention(batting)}, choose the striker:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    finally:
        session.close()
