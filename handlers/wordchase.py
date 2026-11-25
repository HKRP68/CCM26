"""Group word-guessing game. Correct guesses earn a point and pass the host role."""

import json
import logging
import random
from html import escape
from pathlib import Path
from secrets import token_hex

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from services.community_answer_service import matches

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "guessWords.json"
try:
    word_data = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    _WORD_BANK = {"cricket": word_data.get("words", []), "bot": word_data.get("bot_words", [])}
except (OSError, ValueError, AttributeError):
    _WORD_BANK = {}
    logger.warning("wordchase: could not load guessWords.json", exc_info=True)

_CATEGORY_NAMES = {"cricket": "Cricket", "bot": "CCM26 Bot"}
GAMES: dict[int, dict] = {}


def _name(user) -> str:
    return escape(f"@{user.username}" if user.username else user.first_name or str(user.id))


def _draw_word(game: dict) -> tuple[str, str]:
    theme = random.choice(tuple(game["decks"]))
    deck = game["decks"][theme]
    if not deck:
        deck.extend(_WORD_BANK[theme])
        random.shuffle(deck)
        if len(deck) > 1 and deck[-1] == game.get("last_draw"):
            deck[0], deck[-1] = deck[-1], deck[0]
    word = deck.pop()
    game["last_draw"] = word
    return theme, word


async def _assign_round(bot, chat, game: dict, host_id: int, host_name: str) -> bool:
    theme, word = _draw_word(game)
    round_no = game["round"] + 1
    try:
        await bot.send_message(
            host_id,
            f"🔤 <b>Word Chase · Round {round_no}</b>\n"
            f"Group: <b>{escape(chat.title or str(chat.id))}</b>\n"
            f"Category: <b>{_CATEGORY_NAMES[theme]}</b>\n\n"
            f"Your answer: <tg-spoiler>{escape(word)}</tg-spoiler>\n\n"
            "Give clues in the group without saying or spelling the answer.",
            parse_mode="HTML",
        )
    except TelegramError:
        return False

    game.update(
        host_id=host_id, host_name=host_name, word=word, theme=theme,
        round=round_no, round_token=token_hex(8),
    )
    return True


def _end_button_data(chat_id: int, game: dict) -> str:
    return f"wc_end_{chat_id}_{game['round_token']}"


async def _announce_round(message, chat_id: int, game: dict, opening: str = ""):
    lengths = " · ".join(
        str(sum(char.isalnum() for char in part))
        for part in game["word"].split()
        if any(char.isalnum() for char in part)
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ End Game", callback_data=_end_button_data(chat_id, game)),
    ]])
    await message.reply_text(
        f"{opening}🔤 <b>Word Chase · Round {game['round']}</b>\n"
        f"🎤 Host: <a href='tg://user?id={game['host_id']}'>{game['host_name']}</a>\n"
        f"Category: <b>{_CATEGORY_NAMES[game['theme']]}</b>\n"
        f"Answer lengths: <b>{lengths}</b>\n\n"
        "Host, check your DMs and give a clue. Players, reply here with your guesses. "
        "The first correct guess wins a point and the next turn as host.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    game["guessing_enabled"] = True


def _scores_text(game: dict) -> str:
    if not game["scores"]:
        return ""
    top = sorted(game["scores"].values(), key=lambda entry: entry[1], reverse=True)[:5]
    lines = [f"{rank}. {name} · <b>{points}</b>" for rank, (name, points) in enumerate(top, 1)]
    return "\n\n🏆 <b>Top scores</b>\n" + "\n".join(lines)


def _end_error(game: dict | None, user_id: int) -> str | None:
    if not game:
        return "❌ No Word Chase running."
    if game["host_id"] != user_id:
        return "❌ Only the current host can end the game."
    if not game["guessing_enabled"]:
        return "⏳ A round is being set up. Try again in a moment."
    return None


def _end_text(game: dict) -> str:
    return (
        f"🔤 <b>Word Chase ended.</b> The answer was <b>{escape(game['word'])}</b>."
        f"{_scores_text(game)}"
    )


async def wordchase_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, message, user = update.effective_chat, update.message, update.effective_user
    if not chat or not message or not user or user.is_bot:
        return
    if chat.type not in ("group", "supergroup"):
        await message.reply_text("❌ Word Chase must be played in a group.")
        return
    if chat.id in GAMES:
        await message.reply_text("❌ A Word Chase game is already running. The current host can use /endchase.")
        return

    mode = " ".join(context.args).lower() or "mixed"
    if mode not in ("mixed", *_CATEGORY_NAMES):
        await message.reply_text("Choose /wordchase for mixed rounds, /wordchase cricket, or /wordchase bot.")
        return
    decks = {theme: [] for theme, words in _WORD_BANK.items() if words and mode in ("mixed", theme)}
    if not decks:
        await message.reply_text("❌ No answers are available for this category.")
        return

    game = {
        "host_id": user.id,
        "host_name": _name(user),
        "guessing_enabled": False,
        "round": 0,
        "decks": decks,
        "scores": {},
    }
    GAMES[chat.id] = game
    try:
        if not await _assign_round(context.bot, chat, game, user.id, game["host_name"]):
            await message.reply_text("⚠️ I couldn't send you the answer. Open the bot privately, tap Start, and try again.")
            return
        await _announce_round(message, chat.id, game)
    finally:
        if not game["guessing_enabled"]:
            GAMES.pop(chat.id, None)


async def wordchase_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, message, user = update.effective_chat, update.message, update.effective_user
    if not chat or not message or not user:
        return
    game = GAMES.get(chat.id)
    error = _end_error(game, user.id)
    if error:
        await message.reply_text(error)
        return
    del GAMES[chat.id]
    await message.reply_text(_end_text(game), parse_mode="HTML")


async def wordchase_end_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, query = update.effective_chat, update.callback_query
    game = GAMES.get(chat.id) if chat else None
    if not game or "round_token" not in game or query.data != _end_button_data(chat.id, game):
        await query.answer("This round has already ended.", show_alert=True)
        return
    error = _end_error(game, query.from_user.id)
    if error:
        await query.answer(error, show_alert=True)
        return
    del GAMES[chat.id]
    await query.answer()
    await query.edit_message_text(_end_text(game), parse_mode="HTML")


async def wordchase_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Award one point to the first correct guess and privately assign the next answer."""
    chat, message, user = update.effective_chat, update.message, update.effective_user
    if not chat or not message or not user or user.is_bot:
        return
    game = GAMES.get(chat.id)
    if not game or not game["guessing_enabled"] or user.id == game["host_id"]:
        return
    if not matches(message.text, (game["word"],)):
        return

    game["guessing_enabled"] = False
    name = _name(user)
    _, points = game["scores"].get(user.id, (name, 0))
    game["scores"][user.id] = (name, points + 1)
    result = f"✅ <b>{name}</b> guessed <b>{escape(game['word'])}</b>! (+1 point · {points + 1} total)\n\n"

    try:
        if not await _assign_round(context.bot, chat, game, user.id, name):
            result += f"📩 {name}, open the bot privately and tap Start so you can host next time.\n\n"
            if not await _assign_round(context.bot, chat, game, game["host_id"], game["host_name"]):
                await message.reply_text(
                    f"{result}🔤 <b>Word Chase ended.</b> I couldn't DM the next host.{_scores_text(game)}",
                    parse_mode="HTML",
                )
                return
        await _announce_round(message, chat.id, game, result)
    finally:
        if not game["guessing_enabled"]:
            GAMES.pop(chat.id, None)
