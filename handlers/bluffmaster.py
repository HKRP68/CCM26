"""/bluff @user [rounds] — Bluff Master: 1v1 cricket trivia with a steal mechanic.
Each round both players choose: Answer (normal) or Steal (risky).
Steal from a correct answer: +2 pts for stealer, 0 for answerer.
Steal from wrong answer: −2 pts for stealer.
"""
import json
import logging
import os
import random
import unicodedata

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

WIN_COINS = 300
LOSE_COINS = 0
DEFAULT_ROUNDS = 5

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "quiz.json")
try:
    with open(_DATA_PATH) as f:
        _QUIZ = json.load(f).get("categories", {})
        _CATEGORIES = list(_QUIZ.keys())
except Exception:
    _QUIZ, _CATEGORIES = {}, []
    logger.warning("bluffmaster: could not load quiz.json")

# chat_id -> lobby dict
LOBBIES: dict = {}
# user_id -> chat_id for reverse lookup
USER_CHAT: dict = {}


def _name(user) -> str:
    return f"@{user.username}" if user.username else user.first_name or str(user.id)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.strip().lower().replace("-", " ").replace(",", "")


def _check_answer(inp: str, q: dict) -> bool:
    inp = _norm(inp)
    for v in q.get("v", []):
        nv = _norm(v)
        if inp == nv:
            return True
        if nv in inp or inp in nv:
            if len(inp) >= 4:
                return True
    return False


def _scoreboard(lobby: dict) -> str:
    p1, p2 = lobby["players"]
    s1, s2 = lobby["scores"][p1.id], lobby["scores"][p2.id]
    return f"<b>{_name(p1)}</b>: {s1} pts  |  <b>{_name(p2)}</b>: {s2} pts"


def _pick_question(lobby: dict):
    cat = lobby["category"]
    pool = _QUIZ.get(cat, [])
    asked = lobby.get("asked", [])
    available = [q for i, q in enumerate(pool) if i not in asked]
    if not available:
        return None, None
    idx = random.randrange(len(available))
    real_idx = [i for i in range(len(pool)) if i not in asked][idx]
    asked.append(real_idx)
    lobby["asked"] = asked
    return pool[real_idx], real_idx


async def bluff_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _CATEGORIES:
        await update.message.reply_text("❌ Quiz data unavailable.")
        return
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Use /bluff in a group, tagging your opponent.")
        return
    if chat.id in LOBBIES:
        await update.message.reply_text("❌ A Bluff Master game is already running here.")
        return

    host = update.effective_user
    msg = update.message

    # Parse target from mention or reply
    target = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    elif context.args:
        for ent in (msg.entities or []):
            if ent.type == "mention":
                username = msg.text[ent.offset + 1:ent.offset + ent.length]
                # Can't resolve username to user object without bot.get_chat — skip
                await update.message.reply_text("❌ Please reply to your opponent's message to challenge them.")
                return

    if not target:
        await update.message.reply_text(
            "Usage: Reply to your opponent's message and send <code>/bluff</code>",
            parse_mode="HTML",
        )
        return
    if target.id == host.id or target.is_bot:
        await update.message.reply_text("❌ You can't challenge yourself or a bot.")
        return

    # Parse rounds
    rounds = DEFAULT_ROUNDS
    if context.args:
        try:
            rounds = max(3, min(10, int(context.args[0])))
        except ValueError:
            pass

    category = random.choice(_CATEGORIES)
    LOBBIES[chat.id] = {
        "players": [host, target],
        "host_id": host.id,
        "challenger_id": target.id,
        "scores": {host.id: 0, target.id: 0},
        "round": 0,
        "total_rounds": rounds,
        "category": category,
        "state": "WAITING",
        "current_q": None,
        "submissions": {},
        "asked": [],
    }
    USER_CHAT[host.id] = chat.id
    USER_CHAT[target.id] = chat.id

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"bm_accept_{chat.id}_{target.id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"bm_decline_{chat.id}_{target.id}"),
    ]])
    await update.message.reply_text(
        f"🎭 <b>Bluff Master Challenge!</b>\n\n"
        f"<b>{_name(host)}</b> challenges <b>{_name(target)}</b>!\n"
        f"Category: <b>{category}</b>  |  Rounds: <b>{rounds}</b>\n\n"
        f"<a href='tg://user?id={target.id}'>{_name(target)}</a>, do you accept?",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def bluff_accept_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    chat_id, target_id = int(parts[2]), int(parts[3])

    if query.from_user.id != target_id:
        await query.answer("Not your challenge.", show_alert=True)
        return
    await query.answer()

    lobby = LOBBIES.get(chat_id)
    if not lobby:
        await query.edit_message_text("❌ Challenge expired.")
        return

    lobby["state"] = "ACTIVE"
    await query.edit_message_text(
        f"🎭 <b>Bluff Master</b> — {lobby['category']}\n"
        f"{_scoreboard(lobby)}\n\n"
        "Game starting…"
    )
    await _run_round(context, chat_id, lobby)


async def bluff_decline_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    chat_id, target_id = int(parts[2]), int(parts[3])
    if query.from_user.id != target_id:
        await query.answer("Not your challenge.", show_alert=True)
        return
    await query.answer()
    lobby = LOBBIES.pop(chat_id, None)
    if lobby:
        for pid in (lobby["host_id"], lobby["challenger_id"]):
            USER_CHAT.pop(pid, None)
    await query.edit_message_text("❌ Challenge declined.")


async def _run_round(context, chat_id: int, lobby: dict):
    lobby["round"] += 1
    if lobby["round"] > lobby["total_rounds"]:
        await _end_game(context, chat_id, lobby)
        return

    q, _ = _pick_question(lobby)
    if q is None:
        await _end_game(context, chat_id, lobby)
        return

    lobby["current_q"] = q
    lobby["submissions"] = {}
    lobby["state"] = "QUIZ_PHASE"

    p1, p2 = lobby["players"]
    round_text = (
        f"🎭 <b>Round {lobby['round']}/{lobby['total_rounds']}</b>\n"
        f"{_scoreboard(lobby)}\n\n"
        f"❓ <b>{q['q']}</b>\n\n"
        "Both players choose in their DMs:"
    )

    # DM each player action options
    for player in (p1, p2):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Answer", callback_data=f"bm_answer_{chat_id}_{player.id}"),
            InlineKeyboardButton("🎭 Steal", callback_data=f"bm_steal_{chat_id}_{player.id}"),
        ]])
        try:
            await context.bot.send_message(
                player.id,
                f"🎭 <b>Bluff Master — Round {lobby['round']}</b>\n\n"
                f"❓ {q['q']}\n\n"
                "<b>Answer</b>: Reply with your answer\n"
                "<b>Steal</b>: Steal opponent's answer (risky!)",
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            pass

    await context.bot.send_message(chat_id, round_text, parse_mode="HTML")


async def bluff_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Answer/Steal button presses from DMs."""
    query = update.callback_query
    parts = query.data.split("_")
    action, chat_id, uid = parts[1], int(parts[2]), int(parts[3])

    if query.from_user.id != uid:
        await query.answer("Not your game.", show_alert=True)
        return

    lobby = LOBBIES.get(chat_id)
    if not lobby or lobby["state"] != "QUIZ_PHASE":
        await query.answer("Round already resolved.", show_alert=True)
        return
    if uid in lobby["submissions"]:
        await query.answer("Already submitted.", show_alert=True)
        return
    await query.answer()

    if action == "steal":
        lobby["submissions"][uid] = {"type": "steal", "value": ""}
        await query.edit_message_text("🎭 You chose to <b>Steal</b>! Waiting for opponent…", parse_mode="HTML")
    else:
        # Prompt for answer text
        lobby["submissions"][uid] = {"type": "answer_pending", "value": ""}
        await query.edit_message_text(
            f"✏️ Type your answer for:\n<b>{lobby['current_q']['q']}</b>",
            parse_mode="HTML",
        )


async def bluff_answer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch answer text messages in DMs for Bluff Master."""
    if update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    chat_id = USER_CHAT.get(uid)
    if not chat_id:
        return
    lobby = LOBBIES.get(chat_id)
    if not lobby or lobby["state"] != "QUIZ_PHASE":
        return
    sub = lobby["submissions"].get(uid)
    if sub and sub["type"] == "answer_pending":
        sub["type"] = "answer"
        sub["value"] = update.message.text.strip()
        await update.message.reply_text(f"✅ Answer submitted: <b>{sub['value']}</b>. Waiting for opponent…", parse_mode="HTML")
        await _check_round_complete(context, chat_id, lobby)


async def _check_round_complete(context, chat_id: int, lobby: dict):
    subs = lobby["submissions"]
    players = lobby["players"]
    if len(subs) < 2:
        return
    if any(s["type"] == "answer_pending" for s in subs.values()):
        return
    await _resolve_round(context, chat_id, lobby)


async def _resolve_round(context, chat_id: int, lobby: dict):
    lobby["state"] = "RESOLVING"
    p1, p2 = lobby["players"]
    s1 = lobby["submissions"].get(p1.id, {"type": "steal", "value": ""})
    s2 = lobby["submissions"].get(p2.id, {"type": "steal", "value": ""})
    q = lobby["current_q"]

    c1 = s1["type"] == "answer" and _check_answer(s1["value"], q)
    c2 = s2["type"] == "answer" and _check_answer(s2["value"], q)

    pts = {p1.id: 0, p2.id: 0}

    if s1["type"] == "answer" and s2["type"] == "answer":
        if c1: pts[p1.id] += 1
        if c2: pts[p2.id] += 1
    elif s1["type"] == "steal" and s2["type"] == "steal":
        pts[p1.id] -= 2; pts[p2.id] -= 2
    elif s1["type"] == "steal":
        if c2: pts[p1.id] += 2
        else: pts[p1.id] -= 2
    elif s2["type"] == "steal":
        if c1: pts[p2.id] += 2
        else: pts[p2.id] -= 2

    lobby["scores"][p1.id] += pts[p1.id]
    lobby["scores"][p2.id] += pts[p2.id]

    a1 = s1["value"] if s1["type"] == "answer" else "🎭 Stole"
    a2 = s2["value"] if s2["type"] == "answer" else "🎭 Stole"
    correct_ans = q.get("a", "?")

    result_text = (
        f"🎭 <b>Round {lobby['round']} Results</b>\n\n"
        f"❓ {q['q']}\n"
        f"✅ Answer: <b>{correct_ans}</b>\n\n"
        f"<b>{_name(p1)}</b>: {a1}  {'✅' if c1 else '❌'}  ({pts[p1.id]:+d} pts)\n"
        f"<b>{_name(p2)}</b>: {a2}  {'✅' if c2 else '❌'}  ({pts[p2.id]:+d} pts)\n\n"
        f"🏆 {_scoreboard(lobby)}"
    )
    await context.bot.send_message(chat_id, result_text, parse_mode="HTML")
    await _run_round(context, chat_id, lobby)


async def _end_game(context, chat_id: int, lobby: dict):
    p1, p2 = lobby["players"]
    s1, s2 = lobby["scores"][p1.id], lobby["scores"][p2.id]

    if s1 > s2:
        winner, loser = p1, p2
    elif s2 > s1:
        winner, loser = p2, p1
    else:
        winner = None

    LOBBIES.pop(chat_id, None)
    for pid in (lobby["host_id"], lobby["challenger_id"]):
        USER_CHAT.pop(pid, None)

    if winner:
        from database import get_session
        from models import User
        from services.activity_service import log_activity
        session = get_session()
        try:
            u = session.query(User).filter(User.telegram_id == winner.id).first()
            if u:
                u.total_coins += WIN_COINS
                log_activity(session, u.id, "bluffmaster", f"win +{WIN_COINS} coins")
                session.commit()
        finally:
            session.close()
        outcome = f"🏆 <b>{_name(winner)}</b> wins! +{WIN_COINS} coins"
    else:
        outcome = "🤝 It's a tie!"

    await context.bot.send_message(
        chat_id,
        f"🎭 <b>Bluff Master — Final Scores</b>\n\n"
        f"<b>{_name(p1)}</b>: {s1} pts\n"
        f"<b>{_name(p2)}</b>: {s2} pts\n\n"
        f"{outcome}",
        parse_mode="HTML",
    )
