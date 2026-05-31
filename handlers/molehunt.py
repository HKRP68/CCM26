"""/mole — Mole Hunt: social deduction group game.
One player is the Mole and gets a slightly different secret word.
Civilians try to vote out the Mole; Mole tries to blend in.

Commands:
  /mole          — Start lobby (group)
  /joinmole      — Join (or tap Join button)
  /startmole     — Begin game (host only)
  /quitmole      — Leave / cancel (host cancels entire lobby)
"""
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WIN_COINS_CIVILIAN = 200
WIN_COINS_MOLE = 350

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "themes.json")
try:
    with open(_DATA_PATH) as f:
        _THEMES_DATA: dict = json.load(f).get("themes", {})
        _THEME_NAMES: List[str] = list(_THEMES_DATA.keys())
except Exception:
    _THEMES_DATA, _THEME_NAMES = {}, []
    logger.warning("molehunt: could not load themes.json")

MIN_PLAYERS = 3

# chat_id -> lobby
LOBBIES: dict = {}


def _name(user) -> str:
    return f"@{user.username}" if user.username else user.first_name or str(user.id)


def _lobby_text(lobby: dict) -> str:
    names = [_name(p) for p in lobby["players"]]
    joined = "\n".join(f"• {n}" for n in names)
    return (
        f"🕵️ <b>Mole Hunt Lobby</b>\n\n"
        f"Host: <b>{_name(lobby['host'])}</b>\n"
        f"Players ({len(lobby['players'])}/{MIN_PLAYERS} min):\n{joined}\n\n"
        f"Join, then host presses <b>Start</b>."
    )


def _lobby_kb(lobby: dict):
    rows = [[InlineKeyboardButton("➕ Join", callback_data=f"mh_join_{lobby['chat_id']}")]]
    if len(lobby["players"]) >= MIN_PLAYERS:
        rows.append([InlineKeyboardButton("▶️ Start Game", callback_data=f"mh_start_{lobby['chat_id']}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"mh_cancel_{lobby['chat_id']}")])
    return InlineKeyboardMarkup(rows)


async def mole_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Mole Hunt must be played in a group.")
        return
    if chat.id in LOBBIES:
        await update.message.reply_text("❌ A Mole Hunt lobby already exists here.")
        return
    if not _THEME_NAMES:
        await update.message.reply_text("❌ Theme data unavailable.")
        return

    user = update.effective_user
    LOBBIES[chat.id] = {
        "chat_id": chat.id,
        "host": user,
        "players": [user],
        "state": "LOBBY",
        "word_a": None,
        "word_b": None,
        "mole_id": None,
        "theme": None,
        "clues": {},
        "votes": {},
        "pinned_id": None,
    }
    msg = await update.message.reply_text(
        _lobby_text(LOBBIES[chat.id]),
        parse_mode="HTML",
        reply_markup=_lobby_kb(LOBBIES[chat.id]),
    )
    LOBBIES[chat.id]["pinned_id"] = msg.message_id
    try:
        await context.bot.pin_chat_message(chat.id, msg.message_id, disable_notification=True)
    except Exception:
        pass


async def mh_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split("_")[2])
    lobby = LOBBIES.get(chat_id)
    if not lobby or lobby["state"] != "LOBBY":
        await query.answer("No open lobby.", show_alert=True)
        return
    uid = query.from_user.id
    if any(p.id == uid for p in lobby["players"]):
        await query.answer("Already joined.", show_alert=True)
        return
    lobby["players"].append(query.from_user)
    await query.answer(f"Joined! ({len(lobby['players'])} players)")
    await query.edit_message_text(
        _lobby_text(lobby), parse_mode="HTML", reply_markup=_lobby_kb(lobby)
    )


async def mh_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split("_")[2])
    lobby = LOBBIES.get(chat_id)
    if not lobby:
        await query.answer("Lobby gone.", show_alert=True)
        return
    if query.from_user.id != lobby["host"].id:
        await query.answer("Only the host can start.", show_alert=True)
        return
    if len(lobby["players"]) < MIN_PLAYERS:
        await query.answer(f"Need at least {MIN_PLAYERS} players.", show_alert=True)
        return
    await query.answer()

    # Theme selection keyboard
    kb_rows = []
    for i in range(0, len(_THEME_NAMES), 2):
        row = []
        for name in _THEME_NAMES[i:i+2]:
            row.append(InlineKeyboardButton(name, callback_data=f"mh_theme_{chat_id}_{name}"))
        kb_rows.append(row)
    await query.edit_message_text(
        "🎯 <b>Pick a theme:</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows)
    )


async def mh_theme_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_", 3)
    chat_id, theme = int(parts[2]), parts[3]
    lobby = LOBBIES.get(chat_id)
    if not lobby:
        await query.answer("Lobby gone.", show_alert=True)
        return
    if query.from_user.id != lobby["host"].id:
        await query.answer("Only the host can pick theme.", show_alert=True)
        return
    await query.answer()

    clusters = _THEMES_DATA.get(theme, [])
    if not clusters:
        await query.answer("No words for this theme.", show_alert=True)
        return

    cluster = random.choice(clusters)
    if len(cluster) < 2:
        await query.answer("Theme data too small.", show_alert=True)
        return

    idx_a = random.randrange(len(cluster))
    idx_b = random.randrange(len(cluster))
    while idx_b == idx_a:
        idx_b = random.randrange(len(cluster))

    lobby["word_a"] = cluster[idx_a]
    lobby["word_b"] = cluster[idx_b]
    lobby["theme"] = theme
    lobby["mole_id"] = random.choice(lobby["players"]).id
    lobby["state"] = "CLUE_PHASE"
    lobby["clues"] = {}
    lobby["votes"] = {}

    await query.edit_message_text(
        f"🕵️ <b>Mole Hunt — {theme}</b>\n\n"
        f"Words have been sent to all players privately!\n"
        f"Check your DMs, then reply here with your ONE-word clue.",
        parse_mode="HTML",
    )

    for player in lobby["players"]:
        is_mole = player.id == lobby["mole_id"]
        word = lobby["word_b"] if is_mole else lobby["word_a"]
        role_note = "🐀 <b>You are the Mole!</b> Blend in." if is_mole else "👥 You are a <b>Civilian</b>."
        try:
            await context.bot.send_message(
                player.id,
                f"🕵️ <b>Mole Hunt — {theme}</b>\n\n"
                f"{role_note}\n\n"
                f"Your secret word:\n<tg-spoiler><b>{word}</b></tg-spoiler>\n\n"
                "Reply in the group with exactly ONE word as your clue!",
                parse_mode="HTML",
            )
        except Exception:
            await context.bot.send_message(
                chat_id,
                f"⚠️ Couldn't DM <a href='tg://user?id={player.id}'>{_name(player)}</a>. "
                "Please start a private chat with the bot first! Cancelling…",
                parse_mode="HTML",
            )
            LOBBIES.pop(chat_id, None)
            return


async def mh_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split("_")[2])
    lobby = LOBBIES.get(chat_id)
    if not lobby:
        await query.answer("Already gone.", show_alert=True)
        return
    if query.from_user.id != lobby["host"].id:
        await query.answer("Only the host can cancel.", show_alert=True)
        return
    await query.answer()
    LOBBIES.pop(chat_id, None)
    await query.edit_message_text("❌ Mole Hunt cancelled.")


async def molehunt_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect clues and votes in group chat."""
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return
    lobby = LOBBIES.get(chat.id)
    if not lobby:
        return
    user = update.effective_user
    if not user:
        return
    text = (update.message.text or "").strip()

    if lobby["state"] == "CLUE_PHASE":
        if not any(p.id == user.id for p in lobby["players"]):
            return
        if user.id in lobby["clues"]:
            return
        words = text.split()
        if len(words) != 1:
            return  # Only count single-word messages as clues
        lobby["clues"][user.id] = text

        if len(lobby["clues"]) >= len(lobby["players"]):
            await _show_clues_and_vote(context, chat.id, lobby)

    elif lobby["state"] == "VOTING":
        # Vote via buttons only — ignore text in voting phase
        pass


async def _show_clues_and_vote(context, chat_id: int, lobby: dict):
    lobby["state"] = "VOTING"
    clue_lines = []
    for player in lobby["players"]:
        clue = lobby["clues"].get(player.id, "—")
        clue_lines.append(f"• <b>{_name(player)}</b>: {clue}")

    kb_rows = []
    for player in lobby["players"]:
        kb_rows.append([InlineKeyboardButton(
            f"Vote: {_name(player)}",
            callback_data=f"mh_vote_{chat_id}_{player.id}",
        )])
    kb_rows.append([InlineKeyboardButton("Skip Vote", callback_data=f"mh_voteskip_{chat_id}")])

    await context.bot.send_message(
        chat_id,
        "🕵️ <b>All clues in!</b>\n\n"
        + "\n".join(clue_lines)
        + "\n\n<b>Vote for the Mole:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def mh_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    action = parts[1]
    chat_id = int(parts[2])
    lobby = LOBBIES.get(chat_id)
    if not lobby or lobby["state"] != "VOTING":
        await query.answer("Voting not active.", show_alert=True)
        return
    uid = query.from_user.id
    if not any(p.id == uid for p in lobby["players"]):
        await query.answer("You're not in this game.", show_alert=True)
        return
    if uid in lobby["votes"]:
        await query.answer("Already voted.", show_alert=True)
        return
    await query.answer()

    if action == "voteskip":
        lobby["votes"][uid] = None
    else:
        target_id = int(parts[3])
        lobby["votes"][uid] = target_id

    if len(lobby["votes"]) >= len(lobby["players"]):
        await _reveal(context, chat_id, lobby, query)


async def _reveal(context, chat_id: int, lobby: dict, query=None):
    # Tally
    tallies: Dict[int, int] = {}
    for target in lobby["votes"].values():
        if target is not None:
            tallies[target] = tallies.get(target, 0) + 1

    voted_out_id: Optional[int] = None
    max_v = 0
    tie = False
    for tid, cnt in tallies.items():
        if cnt > max_v:
            max_v = cnt; voted_out_id = tid; tie = False
        elif cnt == max_v:
            tie = True

    mole_id = lobby["mole_id"]
    mole_player = next((p for p in lobby["players"] if p.id == mole_id), None)
    word_a, word_b = lobby["word_a"], lobby["word_b"]

    if tie or voted_out_id is None:
        mole_wins = True
        outcome = "🤝 Tie vote — Mole escapes!"
    elif voted_out_id == mole_id:
        mole_wins = False
        outcome = f"✅ Correct! The Mole was <b>{_name(mole_player)}</b>!"
    else:
        mole_wins = True
        voted_name = next((_name(p) for p in lobby["players"] if p.id == voted_out_id), "someone")
        outcome = f"🎭 Wrong! <b>{voted_name}</b> was innocent. The Mole wins!"

    reveal_text = (
        f"🕵️ <b>Mole Hunt — Reveal</b>\n\n"
        f"{outcome}\n\n"
        f"🐀 Mole: <b>{_name(mole_player) if mole_player else '?'}</b>\n"
        f"📝 Civilian word: <b>{word_a}</b>\n"
        f"🐀 Mole word: <b>{word_b}</b>"
    )

    # Award coins
    from database import get_session
    from models import User
    from services.activity_service import log_activity
    session = get_session()
    try:
        if mole_wins and mole_player:
            u = session.query(User).filter(User.telegram_id == mole_player.id).first()
            if u:
                u.total_coins += WIN_COINS_MOLE
                log_activity(session, u.id, "molehunt", f"mole wins +{WIN_COINS_MOLE}")
        elif not mole_wins:
            for player in lobby["players"]:
                if player.id != mole_id:
                    u = session.query(User).filter(User.telegram_id == player.id).first()
                    if u:
                        u.total_coins += WIN_COINS_CIVILIAN
                        log_activity(session, u.id, "molehunt", f"civilian wins +{WIN_COINS_CIVILIAN}")
        session.commit()
    finally:
        session.close()

    LOBBIES.pop(chat_id, None)
    if query:
        try:
            await query.edit_message_text(reveal_text, parse_mode="HTML")
        except Exception:
            await context.bot.send_message(chat_id, reveal_text, parse_mode="HTML")
    else:
        await context.bot.send_message(chat_id, reveal_text, parse_mode="HTML")
