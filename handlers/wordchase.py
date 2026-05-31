"""/wordchase — Word Chase: host picks a word, group guesses it.
The first correct guesser becomes the next host.
Commands: /wordchase (host/start), /endchase (stop)
"""
import json
import logging
import os
import random
import unicodedata

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, MessageHandler, filters

logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "guessWords.json")
try:
    with open(_DATA_PATH) as f:
        _WORDS: list = json.load(f).get("words", [])
except Exception:
    _WORDS = []
    logger.warning("wordchase: could not load guessWords.json")

# chat_id -> {host_id, host_name, word, guessing_enabled, round}
GAMES: dict = {}


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.strip().lower().replace(" ", "")


def _random_word() -> str:
    return random.choice(_WORDS) if _WORDS else "cricket"


def _name(user) -> str:
    return f"@{user.username}" if user.username else user.first_name or str(user.id)


def _host_text(word: str, round_no: int) -> str:
    return (
        f"💬 <b>Word Chase</b> — Round {round_no}\n\n"
        f"🎤 You are the host! Your word is:\n"
        f"<tg-spoiler>{word}</tg-spoiler>\n\n"
        "Describe it in the group chat so players can guess. "
        "Don't say the word directly!"
    )


async def wordchase_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("❌ Word Chase must be played in a group.")
        return
    if chat.id in GAMES:
        await update.message.reply_text("❌ A Word Chase game is already running. Use /endchase to stop it.")
        return
    if not _WORDS:
        await update.message.reply_text("❌ Word bank unavailable.")
        return

    user = update.effective_user
    word = _random_word()
    GAMES[chat.id] = {
        "host_id": user.id,
        "host_name": _name(user),
        "word": word,
        "guessing_enabled": True,
        "round": 1,
    }

    # DM the word to the host
    try:
        await context.bot.send_message(user.id, _host_text(word, 1), parse_mode="HTML")
        dm_note = f"📩 <a href='tg://user?id={user.id}'>{_name(user)}</a> — check your DMs for your word!"
    except Exception:
        dm_note = f"⚠️ Couldn't DM <a href='tg://user?id={user.id}'>{_name(user)}</a>. Please start a chat with the bot first!"
        GAMES.pop(chat.id, None)
        await update.message.reply_text(dm_note, parse_mode="HTML")
        return

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ End Game", callback_data=f"wc_end_{chat.id}_{user.id}"),
    ]])
    await update.message.reply_text(
        f"🔤 <b>Word Chase Started!</b>\n\n"
        f"🎤 Host: <b>{_name(user)}</b>\n\n"
        f"{dm_note}\n\n"
        "Type your guesses in chat. First to guess correctly becomes the next host!",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def wordchase_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    game = GAMES.get(chat.id)
    if not game:
        await update.message.reply_text("❌ No Word Chase running.")
        return
    if update.effective_user.id != game["host_id"]:
        await update.message.reply_text("❌ Only the current host can end the game.")
        return
    GAMES.pop(chat.id, None)
    await update.message.reply_text(
        f"🔤 <b>Word Chase ended.</b>  The word was: <b>{game['word']}</b>",
        parse_mode="HTML",
    )


async def wordchase_end_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("_")
    chat_id, host_id = int(parts[2]), int(parts[3])

    if query.from_user.id != host_id:
        await query.answer("Only the host can end the game.", show_alert=True)
        return
    await query.answer()
    game = GAMES.pop(chat_id, None)
    word = game["word"] if game else "?"
    await query.edit_message_text(
        f"🔤 <b>Word Chase ended.</b>  The word was: <b>{word}</b>",
        parse_mode="HTML",
    )


async def wordchase_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check every group message for a correct guess."""
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return
    game = GAMES.get(chat.id)
    if not game or not game.get("guessing_enabled"):
        return

    user = update.effective_user
    if not user or user.id == game["host_id"]:
        return  # Host can't guess their own word

    msg_text = update.message.text or ""
    if _normalize(msg_text) == _normalize(game["word"]):
        correct_word = game["word"]
        game["guessing_enabled"] = False  # Pause while transitioning

        # Winner becomes next host
        new_word = _random_word()
        next_round = game["round"] + 1
        game.update({
            "host_id": user.id,
            "host_name": _name(user),
            "word": new_word,
            "guessing_enabled": True,
            "round": next_round,
        })

        # DM new word to winner
        try:
            await context.bot.send_message(user.id, _host_text(new_word, next_round), parse_mode="HTML")
            dm_note = f"📩 <a href='tg://user?id={user.id}'>{_name(user)}</a> — check your DMs!"
        except Exception:
            dm_note = f"⚠️ Couldn't DM {_name(user)}. Please start a private chat with the bot."
            game["guessing_enabled"] = False
            GAMES.pop(chat.id, None)
            await update.message.reply_text(
                f"🎉 <b>{_name(user)}</b> guessed correctly! Word: <b>{correct_word}</b>\n\n"
                f"{dm_note} Game stopped.",
                parse_mode="HTML",
            )
            return

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ End Game", callback_data=f"wc_end_{chat.id}_{user.id}"),
        ]])
        await update.message.reply_text(
            f"✅ <b>{_name(user)}</b> guessed it! The word was <b>{correct_word}</b>.\n\n"
            f"🎤 New host: <b>{_name(user)}</b>  (Round {next_round})\n"
            f"{dm_note}",
            parse_mode="HTML",
            reply_markup=kb,
        )
