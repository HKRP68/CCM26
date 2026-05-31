"""In-memory multiplayer /unscramble player-name game.

Games deliberately use no persistence: restarting the bot cancels every lobby.
"""

import random
import time
from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import Player

ROUND_SECONDS = 15
ROUNDS_PER_LEVEL = 4
LEVELS = (("Casual", 94, 10_000), ("Easy", 87, 93), ("Medium", 82, 86), ("Hard", 76, 81))
LOBBIES = {}


def _name(user): return f"@{user.username}" if user.username else (user.first_name or str(user.id))
def _scramble(name):
    letters = list(name.upper())
    for _ in range(8):
        random.shuffle(letters)
        candidate = "".join(letters)
        if candidate != name.upper(): return candidate
    return "".join(reversed(letters))

@dataclass
class Member:
    user_id: int; name: str; points: int = 0; strikes: int = 0; out: bool = False
    answer_times: list = field(default_factory=list)

@dataclass
class Lobby:
    chat_id: int; host_id: int; host_name: str; members: dict; message_id: int = None
    started: bool = False; level: int = 0; round_no: int = 0; token: int = 0; answer: str = ""
    opened_at: float = 0.0; answered: set = field(default_factory=set); choices: list = field(default_factory=list)


def _announcement(lobby):
    return ("🏏 <b>Unscramble Player Lobby</b>\n\n" f"Host: {lobby.host_name}\nPlayers Joined: {len(lobby.members)}\n\n"
            "<b>Rules:</b>\n• Unscramble cricket player names\n• Answer before timer ends\n• 3 strikes = out\n"
            "• Fastest correct answer gets more points\n\n/ju → Join lobby\n/eu → Exit lobby\n/su → Start game, host only\n/cu → Cancel lobby, host only")

def _leaderboard(lobby):
    ranked = sorted(lobby.members.values(), key=lambda m: (-m.points, m.strikes, sum(m.answer_times) / len(m.answer_times) if m.answer_times else 999))
    lines = ["🏆 <b>Leaderboard</b>", ""]
    for index, member in enumerate(ranked, 1):
        marks = "OUT" if member.out else "❌" * member.strikes + "⚪" * (3 - member.strikes)
        lines.append(f"{index}. {member.name} — {member.points} pts {marks}")
    return "\n".join(lines)

def _active(lobby): return [m for m in lobby.members.values() if not m.out]

def _load_players(min_rating, max_rating):
    session = get_session()
    try:
        return [p.name for p in session.query(Player).filter(Player.rating >= min_rating, Player.rating <= max_rating, Player.is_active.is_(True)).all()]
    finally: session.close()

async def _refresh_lobby(context, lobby):
    try: await context.bot.edit_message_text(chat_id=lobby.chat_id, message_id=lobby.message_id, text=_announcement(lobby), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Join Game", callback_data="us_join")]]))
    except Exception: pass

async def unscramble_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    if cid in LOBBIES:
        await update.message.reply_text("❌ This chat already has an Unscramble lobby or active game."); return
    user = update.effective_user; lobby = Lobby(cid, user.id, _name(user), {user.id: Member(user.id, _name(user))}); LOBBIES[cid] = lobby
    msg = await update.message.reply_text(_announcement(lobby), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Join Game", callback_data="us_join")]]))
    lobby.message_id = msg.message_id
    try: await context.bot.pin_chat_message(cid, msg.message_id, disable_notification=True)
    except Exception: pass

async def join_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lobby = LOBBIES.get(update.effective_chat.id)
    if not lobby or lobby.started: await update.effective_message.reply_text("❌ No open lobby in this chat."); return
    user = update.effective_user; lobby.members.setdefault(user.id, Member(user.id, _name(user))); await _refresh_lobby(context, lobby)
    if update.callback_query: await update.callback_query.answer("Joined the lobby!")
    else: await update.message.reply_text("✅ Joined the Unscramble lobby.")

async def exit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lobby = LOBBIES.get(update.effective_chat.id); user = update.effective_user
    if not lobby or lobby.started: await update.message.reply_text("❌ No open lobby to exit."); return
    if user.id == lobby.host_id: await update.message.reply_text("❌ Host cannot exit. Use /cu to cancel the lobby."); return
    lobby.members.pop(user.id, None); await _refresh_lobby(context, lobby); await update.message.reply_text("✅ Left the lobby.")

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lobby = LOBBIES.get(update.effective_chat.id)
    if not lobby or lobby.host_id != update.effective_user.id: await update.message.reply_text("❌ Only the host can cancel an active lobby."); return
    LOBBIES.pop(lobby.chat_id, None); await update.message.reply_text("🛑 Unscramble lobby cancelled.")

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lobby = LOBBIES.get(update.effective_chat.id)
    if not lobby or lobby.host_id != update.effective_user.id: await update.message.reply_text("❌ Only the host can start an open lobby."); return
    if lobby.started: await update.message.reply_text("❌ Game already started."); return
    if len(lobby.members) < 2: await update.message.reply_text("❌ At least 2 players must join before starting."); return
    lobby.started = True; await _next_round(context, lobby)

async def _next_round(context, lobby):
    if lobby.chat_id not in LOBBIES: return
    alive = _active(lobby)
    if len(alive) <= 1:
        winner = alive[0].name if alive else "Nobody"
        await context.bot.send_message(lobby.chat_id, f"🏆 <b>Unscramble Winner:</b> {winner}!", parse_mode="HTML"); LOBBIES.pop(lobby.chat_id, None); return
    if lobby.round_no >= ROUNDS_PER_LEVEL:
        await _finish_level(context, lobby); return
    level_name, low, high = LEVELS[lobby.level]; pool = _load_players(low, high)
    if len(pool) < 4:
        await context.bot.send_message(lobby.chat_id, f"❌ Not enough {level_name} players in player data to continue."); LOBBIES.pop(lobby.chat_id, None); return
    answer = random.choice(pool); choices = random.sample([p for p in pool if p != answer], 3) + [answer]; random.shuffle(choices)
    lobby.round_no += 1; lobby.token += 1; lobby.answer = answer; lobby.choices = choices; lobby.opened_at = time.time(); lobby.answered = set()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(name, callback_data=f"us_ans_{lobby.token}_{idx}")] for idx, name in enumerate(choices)])
    await context.bot.send_message(lobby.chat_id, f"🏏 <b>{level_name}</b> — Round {lobby.round_no}/{ROUNDS_PER_LEVEL}\n⏳ Time: {ROUND_SECONDS} seconds\n🔤 Unscramble: <b>{_scramble(answer)}</b>", parse_mode="HTML", reply_markup=keyboard)
    if context.job_queue: context.job_queue.run_once(_round_timeout, ROUND_SECONDS, data={"chat_id": lobby.chat_id, "token": lobby.token})

async def answer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; _, _, token, index = query.data.split("_"); lobby = LOBBIES.get(query.message.chat_id); now = time.time()
    if not lobby or not lobby.started or int(token) != lobby.token: await query.answer("Old button: this round has ended.", show_alert=True); return
    member = lobby.members.get(query.from_user.id)
    if not member: await query.answer("Only joined users can answer.", show_alert=True); return
    if member.out: await query.answer("You are OUT and cannot answer.", show_alert=True); return
    if query.from_user.id in lobby.answered: await query.answer("Only one answer per round.", show_alert=True); return
    elapsed = now - lobby.opened_at
    if elapsed > ROUND_SECONDS: await query.answer("⏳ Time Over!", show_alert=True); return
    lobby.answered.add(query.from_user.id)
    if lobby.choices[int(index)] == lobby.answer:
        member.points += 10 if elapsed <= 5 else 7 if elapsed <= 10 else 5; member.answer_times.append(elapsed)
        await query.answer("✅ Correct!", show_alert=True)
    else:
        member.strikes += 1; member.out = member.strikes >= 3
        await query.answer("❌ Wrong! +1 strike", show_alert=True)

async def _round_timeout(context):
    data = context.job.data; lobby = LOBBIES.get(data["chat_id"])
    if not lobby or lobby.token != data["token"]: return
    for member in _active(lobby):
        if member.user_id not in lobby.answered:
            member.strikes += 1; member.out = member.strikes >= 3
    await context.bot.send_message(lobby.chat_id, f"⏳ <b>Time Over!</b> Answer: <b>{lobby.answer}</b>\n\n{_leaderboard(lobby)}", parse_mode="HTML")
    await _next_round(context, lobby)

async def _finish_level(context, lobby):
    alive = _active(lobby)
    if len(alive) <= 1: await _next_round(context, lobby); return
    low_points = min(m.points for m in alive); tied = [m for m in alive if m.points == low_points]
    max_strikes = max(m.strikes for m in tied); tied = [m for m in tied if m.strikes == max_strikes]
    if len(tied) > 1:
        slowest = max(sum(m.answer_times) / len(m.answer_times) if m.answer_times else 999 for m in tied)
        tied = [m for m in tied if (sum(m.answer_times) / len(m.answer_times) if m.answer_times else 999) == slowest]
    eliminated = None
    if len(tied) == 1: eliminated = tied[0]; eliminated.out = True
    if lobby.level >= len(LEVELS) - 1:
        remaining = sorted(_active(lobby), key=lambda m: (-m.points, m.strikes))
        winner = remaining[0].name if remaining else "Nobody"
        await context.bot.send_message(lobby.chat_id, f"🏆 <b>Unscramble Winner:</b> {winner}!\n\n{_leaderboard(lobby)}", parse_mode="HTML"); LOBBIES.pop(lobby.chat_id, None); return
    lobby.level += 1; lobby.round_no = 0
    note = f"\n\n🚪 Eliminated: {eliminated.name}" if eliminated else "\n\n🤝 Lowest-score tie remains unresolved, so nobody is eliminated."
    await context.bot.send_message(lobby.chat_id, f"✅ <b>Difficulty complete!</b>{note}\n\n{_leaderboard(lobby)}\n\n⬆️ Next: <b>{LEVELS[lobby.level][0]}</b>", parse_mode="HTML")
    await _next_round(context, lobby)
