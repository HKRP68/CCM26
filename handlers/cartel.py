"""/cartel — Cricket Cartel: multi-role Mole Hunt variant.
Roles:
  CIVILIAN — majority, get word A
  IMPOSTOR — minority, get word B (like mole)
  JOKER    — no word; wins by getting voted out first
Role counts:
  3-5 players : 1 Impostor
  6-7 players : 2 Impostors
  8-10 players: 2 Impostors + 1 Joker
  11-13       : 3 Impostors + 1 Joker
  14+         : 4 Impostors + 1 Joker

Commands: /cartel, /joincartel, /startcartel, /quitcartel
"""
import json
import logging
import os
import random
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WIN_COINS_CIVILIAN = 200
WIN_COINS_IMPOSTOR = 400
WIN_COINS_JOKER = 500

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "themes.json")
try:
    with open(_DATA_PATH) as f:
        _THEMES_DATA: dict = json.load(f).get("themes", {})
        _THEME_NAMES: List[str] = list(_THEMES_DATA.keys())
except Exception:
    _THEMES_DATA, _THEME_NAMES = {}, []
    logger.warning("cartel: could not load themes.json")

MIN_PLAYERS = 3

LOBBIES: dict = {}


def _name(user) -> str:
    return f"@{user.username}" if user.username else user.first_name or str(user.id)


def _role_dist(n: int):
    if n <= 5:   return {"impostor": 1, "joker": 0}
    if n <= 7:   return {"impostor": 2, "joker": 0}
    if n <= 10:  return {"impostor": 2, "joker": 1}
    if n <= 13:  return {"impostor": 3, "joker": 1}
    return {"impostor": 4, "joker": 1}


def _lobby_text(lobby: dict) -> str:
    names = [_name(p) for p in lobby["players"]]
    joined = "\n".join(f"• {n}" for n in names)
    dist = _role_dist(len(lobby["players"]))
    return (
        f"🃏 <b>Cricket Cartel Lobby</b>\n\n"
        f"Host: <b>{_name(lobby['host'])}</b>\n"
        f"Players ({len(lobby['players'])}/{MIN_PLAYERS} min):\n{joined}\n\n"
        f"At current size: {dist['impostor']} Impostor(s)"
        + (f", 1 Joker" if dist["joker"] else "")
        + "\n\nJoin, then host presses Start."
    )


def _lobby_kb(lobby: dict):
    rows = [[InlineKeyboardButton("➕ Join", callback_data=f"ct_join_{lobby['chat_id']}")]]
    if len(lobby["players"]) >= MIN_PLAYERS:
        rows.append([InlineKeyboardButton("▶️ Start Game", callback_data=f"ct_start_{lobby['chat_id']}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"ct_cancel_{lobby['chat_id']}")])
    return InlineKeyboardMarkup(rows)


async def cartel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Cricket Cartel must be played in a group.")
        return
    if chat.id in LOBBIES:
        await update.message.reply_text("❌ A Cricket Cartel lobby already exists here.")
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
        "roles": {},
        "word_a": None,
        "word_b": None,
        "theme": None,
        "round": 0,
        "alive": [],
        "clues": {},
        "votes": {},
        "joker_id": None,
        "eliminated": [],
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


async def ct_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = int(query.data.split("_")[2])
    lobby = LOBBIES.get(chat_id)
    if not lobby or lobby["state"] != "LOBBY":
        await query.answer("No open lobby.", show_alert=True)
        return
    uid = query.from_user.id
    if any(p.id == uid for p in lobby["players"]):
        await query.answer("Already in.", show_alert=True)
        return
    lobby["players"].append(query.from_user)
    await query.answer(f"Joined! ({len(lobby['players'])} players)")
    await query.edit_message_text(
        _lobby_text(lobby), parse_mode="HTML", reply_markup=_lobby_kb(lobby)
    )


async def ct_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    kb_rows = []
    for i in range(0, len(_THEME_NAMES), 2):
        row = []
        for name in _THEME_NAMES[i:i+2]:
            row.append(InlineKeyboardButton(name, callback_data=f"ct_theme_{chat_id}_{name}"))
        kb_rows.append(row)
    await query.edit_message_text(
        "🎯 <b>Pick a theme:</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def ct_theme_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_", 3)
    chat_id, theme = int(parts[2]), parts[3]
    lobby = LOBBIES.get(chat_id)
    if not lobby:
        await query.answer("Lobby gone.", show_alert=True)
        return
    if query.from_user.id != lobby["host"].id:
        await query.answer("Only the host picks theme.", show_alert=True)
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

    # Assign roles
    n = len(lobby["players"])
    dist = _role_dist(n)
    shuffled = lobby["players"][:]
    random.shuffle(shuffled)

    roles: Dict[int, str] = {}
    idx = 0
    for _ in range(dist["impostor"]):
        roles[shuffled[idx].id] = "IMPOSTOR"; idx += 1
    for _ in range(dist["joker"]):
        roles[shuffled[idx].id] = "JOKER"; idx += 1
    while idx < n:
        roles[shuffled[idx].id] = "CIVILIAN"; idx += 1

    lobby["roles"] = roles
    lobby["word_a"] = cluster[idx_a]
    lobby["word_b"] = cluster[idx_b]
    lobby["theme"] = theme
    lobby["alive"] = list(lobby["players"])
    lobby["joker_id"] = next((p.id for p in lobby["players"] if roles.get(p.id) == "JOKER"), None)
    lobby["round"] = 1
    lobby["state"] = "CLUE_PHASE"
    lobby["clues"] = {}
    lobby["votes"] = {}

    await query.edit_message_text(
        f"🃏 <b>Cricket Cartel — {theme}</b>\n\n"
        f"Round 1 starting! Check your DMs for your role and word.",
        parse_mode="HTML",
    )

    for player in lobby["players"]:
        role = roles.get(player.id, "CIVILIAN")
        if role == "CIVILIAN":
            word_text = f"Your secret word:\n<tg-spoiler><b>{lobby['word_a']}</b></tg-spoiler>"
            role_note = "👥 Role: <b>Civilian</b> — identify the Impostors!"
        elif role == "IMPOSTOR":
            word_text = f"Your secret word:\n<tg-spoiler><b>{lobby['word_b']}</b></tg-spoiler>"
            role_note = "🕵️ Role: <b>Impostor</b> — blend in, don't get caught!"
        else:
            word_text = "You have <b>no word</b> — be vague."
            role_note = "🃏 Role: <b>Joker</b> — get voted out first to WIN!"

        try:
            await context.bot.send_message(
                player.id,
                f"🃏 <b>Cricket Cartel — {theme}</b>\n\n"
                f"{role_note}\n\n{word_text}\n\n"
                "Reply in the group with ONE word as your clue!",
                parse_mode="HTML",
            )
        except Exception:
            await context.bot.send_message(
                chat_id,
                f"⚠️ Couldn't DM {_name(player)}. Please DM the bot first! Game cancelled.",
                parse_mode="HTML",
            )
            LOBBIES.pop(chat_id, None)
            return


async def ct_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await query.edit_message_text("❌ Cricket Cartel cancelled.")


async def cartel_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return
    lobby = LOBBIES.get(chat.id)
    if not lobby or lobby["state"] != "CLUE_PHASE":
        return
    user = update.effective_user
    if not user:
        return
    if not any(p.id == user.id for p in lobby["alive"]):
        return
    if user.id in lobby["clues"]:
        return

    text = (update.message.text or "").strip()
    if len(text.split()) != 1:
        return  # Only single-word clues

    lobby["clues"][user.id] = text

    if len(lobby["clues"]) >= len(lobby["alive"]):
        await _voting_round(context, chat.id, lobby)


async def _voting_round(context, chat_id: int, lobby: dict):
    lobby["state"] = "VOTING"
    clue_lines = [
        f"• <b>{_name(p)}</b>: {lobby['clues'].get(p.id, '—')}"
        for p in lobby["alive"]
    ]
    kb_rows = []
    for player in lobby["alive"]:
        kb_rows.append([InlineKeyboardButton(
            f"Vote: {_name(player)}",
            callback_data=f"ct_vote_{chat_id}_{player.id}",
        )])
    kb_rows.append([InlineKeyboardButton("Skip Vote", callback_data=f"ct_voteskip_{chat_id}")])

    await context.bot.send_message(
        chat_id,
        f"🃏 <b>Round {lobby['round']} — Clues</b>\n\n"
        + "\n".join(clue_lines)
        + "\n\n<b>Vote to eliminate:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def ct_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    action, chat_id = parts[1], int(parts[2])
    lobby = LOBBIES.get(chat_id)
    if not lobby or lobby["state"] != "VOTING":
        await query.answer("Voting not active.", show_alert=True)
        return
    uid = query.from_user.id
    if not any(p.id == uid for p in lobby["alive"]):
        await query.answer("Not in this game.", show_alert=True)
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

    if len(lobby["votes"]) >= len(lobby["alive"]):
        await _process_vote(context, chat_id, lobby, query)


async def _process_vote(context, chat_id: int, lobby: dict, query=None):
    tallies: Dict[int, int] = {}
    for t in lobby["votes"].values():
        if t is not None:
            tallies[t] = tallies.get(t, 0) + 1

    voted_out_id: Optional[int] = None
    max_v, tie = 0, False
    for tid, cnt in tallies.items():
        if cnt > max_v:
            max_v = cnt; voted_out_id = tid; tie = False
        elif cnt == max_v:
            tie = True

    if tie or voted_out_id is None:
        result_msg = "🤝 Tie — no one eliminated this round."
        lobby["clues"] = {}
        lobby["votes"] = {}
        lobby["round"] += 1
        lobby["state"] = "CLUE_PHASE"
        await context.bot.send_message(chat_id, result_msg + f"\nNext round {lobby['round']}!", parse_mode="HTML")
        return

    voted_player = next((p for p in lobby["alive"] if p.id == voted_out_id), None)

    # Check Joker win condition FIRST
    if voted_out_id == lobby.get("joker_id") and voted_player:
        joker = voted_player
        await context.bot.send_message(
            chat_id,
            f"🃏 <b>JOKER WINS!</b>\n\n"
            f"<b>{_name(joker)}</b> was the Joker and got voted out!\n"
            f"🎉 Joker collects <b>{WIN_COINS_JOKER}</b> coins.",
            parse_mode="HTML",
        )
        _award(joker.id, WIN_COINS_JOKER, "cartel joker win")
        LOBBIES.pop(chat_id, None)
        return

    # Eliminate voted player
    lobby["alive"] = [p for p in lobby["alive"] if p.id != voted_out_id]
    lobby["eliminated"].append(voted_out_id)

    if voted_player:
        role = lobby["roles"].get(voted_out_id, "CIVILIAN")
        await context.bot.send_message(
            chat_id,
            f"🚫 <b>{_name(voted_player)}</b> was eliminated! (Role: {role})",
            parse_mode="HTML",
        )

    # Check win conditions
    n_alive = len(lobby["alive"])
    impostors_alive = [p for p in lobby["alive"] if lobby["roles"].get(p.id) == "IMPOSTOR"]
    civilians_alive = [p for p in lobby["alive"] if lobby["roles"].get(p.id) == "CIVILIAN"]

    # Impostors win if they match or outnumber civilians
    if len(impostors_alive) >= len(civilians_alive) and impostors_alive:
        winner_ids = [p.id for p in lobby["players"] if lobby["roles"].get(p.id) == "IMPOSTOR"]
        await _end_cartel(context, chat_id, lobby, "IMPOSTOR_WIN", winner_ids)
        return

    # All impostors gone → civilians win
    if not impostors_alive:
        winner_ids = [p.id for p in lobby["players"] if lobby["roles"].get(p.id) == "CIVILIAN"]
        await _end_cartel(context, chat_id, lobby, "CIVILIAN_WIN", winner_ids)
        return

    # Continue
    lobby["clues"] = {}
    lobby["votes"] = {}
    lobby["round"] += 1
    lobby["state"] = "CLUE_PHASE"

    kb_rows = [[InlineKeyboardButton(
        f"Vote: {_name(p)}", callback_data=f"ct_vote_{chat_id}_{p.id}"
    )] for p in lobby["alive"]]
    kb_rows.append([InlineKeyboardButton("Skip Vote", callback_data=f"ct_voteskip_{chat_id}")])

    await context.bot.send_message(
        chat_id,
        f"🃏 <b>Round {lobby['round']} continues!</b>\n"
        f"{n_alive} players remain. Each send ONE clue word.",
        parse_mode="HTML",
    )


def _award(user_tg_id: int, coins: int, reason: str):
    try:
        from database import get_session
        from models import User
        from services.activity_service import log_activity
        session = get_session()
        try:
            u = session.query(User).filter(User.telegram_id == user_tg_id).first()
            if u:
                u.total_coins += coins
                log_activity(session, u.id, "cartel", f"{reason} +{coins}")
                session.commit()
        finally:
            session.close()
    except Exception as e:
        logger.error(f"cartel _award failed: {e}")


async def _end_cartel(context, chat_id: int, lobby: dict, result: str, winner_ids: List[int]):
    word_a, word_b = lobby["word_a"], lobby["word_b"]
    impostors = [p for p in lobby["players"] if lobby["roles"].get(p.id) == "IMPOSTOR"]
    impostor_names = ", ".join(_name(p) for p in impostors) or "none"

    if result == "CIVILIAN_WIN":
        header = "✅ <b>Civilians Win!</b> All Impostors eliminated."
        for pid in winner_ids:
            _award(pid, WIN_COINS_CIVILIAN, "cartel civilian win")
    else:
        header = "🕵️ <b>Impostors Win!</b> They outnumber the civilians."
        for pid in winner_ids:
            _award(pid, WIN_COINS_IMPOSTOR, "cartel impostor win")

    LOBBIES.pop(chat_id, None)
    await context.bot.send_message(
        chat_id,
        f"🃏 <b>Cricket Cartel — Game Over</b>\n\n"
        f"{header}\n\n"
        f"🕵️ Impostor(s): <b>{impostor_names}</b>\n"
        f"📝 Civilian word: <b>{word_a}</b>\n"
        f"🕵️ Impostor word: <b>{word_b}</b>",
        parse_mode="HTML",
    )
