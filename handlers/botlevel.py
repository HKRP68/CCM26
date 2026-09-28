"""The difficulty picker shared by /lpbot and /ciplbot.

Both commands open with the same three-button prompt, and the answer is
remembered per player in ``bot_data`` so it survives the rest of the setup flow
— team pick, pitch, XI, toss — without having to be threaded through every
draft dict on the way. ``handlers.cipl_play.mark_bot_match`` reads it back when
the match state is finally built.

The choice is also *sticky*: the button row that comes back pre-marks whatever
you picked last time, so a player who always plays Hard is one tap from a match
rather than answering the same question every time.

Difficulty changes how well the AI captain plays — never what it is allowed to
know or do. See :data:`services.bot_captain.DIFFICULTIES`.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from services.bot_captain import (
    DEFAULT_DIFFICULTY, DIFFICULTIES, DIFFICULTY_ORDER, normalize_difficulty,
)

logger = logging.getLogger(__name__)

_STORE = "bot_difficulty_by_user"

# One line each, so the prompt says what the setting actually does rather than
# leaving the player to guess what "Normal" means.
BLURBS = {
    "easy": "Plays loose and misreads the game — a fair fight while you learn.",
    "normal": "Solid captaincy. Picks sensible plans and punishes a sloppy over.",
    "hard": "Plays the optimal mix and hunts for patterns in how you captain.",
}


def remembered_level(bot_data, user_id):
    """This player's last chosen difficulty, or ``None`` if they've never picked.

    The ``None`` matters: it is what tells :func:`prompt_difficulty` not to
    pre-mark a row, so a first-time player is asked an open question instead of
    being shown a setting they never chose.
    """
    try:
        stored = (bot_data.get(_STORE) or {}).get(int(user_id))
    except Exception:
        return None
    return normalize_difficulty(stored) if stored else None


def remember_level(bot_data, user_id, level):
    """Store this player's difficulty for the match they are about to set up."""
    level = normalize_difficulty(level)
    try:
        store = bot_data.setdefault(_STORE, {})
        store[int(user_id)] = level
    except Exception:
        logger.exception("bot difficulty: could not remember the choice")
    return level


def level_for_match(bot_data, user_id, tg_id=None):
    """The difficulty a match for this player should be built with.

    The choice is stored under the player's Telegram id (the button tap is all
    the prompt knows), so pass ``tg_id`` whenever the caller has it — the
    database id alone never found the stored pick.
    """
    for key in (tg_id, user_id):
        if key is not None:
            level = remembered_level(bot_data, key)
            if level:
                return level
    return DEFAULT_DIFFICULTY


# ── Where the match is played: the chat, or the Mini App ────────────
# Also per player and sticky. ``/lpbot app`` / ``/lpbot chat`` (and the same
# words after /ciplbot) set it; with no word the last choice is kept, and the
# difficulty prompt carries a one-tap switch.
PLAY_CHAT = "chat"
PLAY_APP = "app"
PLAY_MODES = (PLAY_CHAT, PLAY_APP)
DEFAULT_PLAY_MODE = PLAY_CHAT
PLAY_LABELS = {PLAY_CHAT: "💬 Chat", PLAY_APP: "📱 Mini App"}
_MODE_STORE = "bot_play_mode_by_user"
_MODE_WORDS = {
    "chat": PLAY_CHAT, "c": PLAY_CHAT, "group": PLAY_CHAT,
    "app": PLAY_APP, "miniapp": PLAY_APP, "mini": PLAY_APP, "webapp": PLAY_APP,
}


def normalize_play_mode(mode):
    return mode if mode in PLAY_MODES else DEFAULT_PLAY_MODE


def split_play_mode(args):
    """``(mode or None, remaining args)`` — pulls "app"/"chat" out of the args."""
    mode, rest = None, []
    for a in list(args or []):
        word = str(a).strip().lower().lstrip("/")
        if word in _MODE_WORDS and mode is None:
            mode = _MODE_WORDS[word]
        else:
            rest.append(a)
    return mode, rest


def remember_play_mode(bot_data, tg_id, mode):
    mode = normalize_play_mode(mode)
    try:
        bot_data.setdefault(_MODE_STORE, {})[int(tg_id)] = mode
    except Exception:
        logger.exception("bot play mode: could not remember the choice")
    return mode


def play_mode_for(bot_data, tg_id):
    """The play mode a match for this player (by Telegram id) is built with."""
    try:
        stored = (bot_data.get(_MODE_STORE) or {}).get(int(tg_id))
    except Exception:
        stored = None
    return normalize_play_mode(stored)


def _rows(mode, league, current, owner_id, play_mode=DEFAULT_PLAY_MODE):
    """The three difficulty buttons, with the player's last pick marked, and a
    second row that switches where the match is played."""
    buttons = []
    for key in DIFFICULTY_ORDER:
        label = DIFFICULTIES[key]["label"]
        if key == current:
            label = f"• {label} •"
        buttons.append(InlineKeyboardButton(
            label, callback_data=f"botdiff_{key}_{mode}_{league}_{owner_id}"))
    where = []
    for pm in PLAY_MODES:
        label = PLAY_LABELS[pm]
        if pm == play_mode:
            label = f"✅ {label}"
        where.append(InlineKeyboardButton(
            label, callback_data=f"botplay_{pm}_{mode}_{league}_{owner_id}"))
    return InlineKeyboardMarkup([buttons, where])


def _prompt_text(play_mode):
    lines = "\n".join(
        f"{DIFFICULTIES[k]['label']} — <i>{BLURBS[k]}</i>" for k in DIFFICULTY_ORDER)
    where = ("📱 <b>Play in:</b> Mini App — every pick in the app, the chat "
             "just follows the score." if play_mode == PLAY_APP else
             "💬 <b>Play in:</b> Chat — every pick with the buttons here.")
    return (
        "🤖 <b>How hard should the bot play?</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{lines}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{where}\n"
        "<i>Every setting plays with the same squad and the same rules — only "
        "the captaincy gets sharper.</i>")


async def prompt_difficulty(update, context, mode, league=""):
    """Ask which difficulty to play, then stop — the button resumes the command.

    ``mode`` is ``"lp"`` or ``"cipl"``; ``league`` is the league key /ciplbot
    resolved (empty for /lpbot), so the callback can restart the exact same
    invocation the player made.
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    current = remembered_level(context.bot_data, user.id)
    play_mode = play_mode_for(context.bot_data, user.id)
    await message.reply_text(
        _prompt_text(play_mode), parse_mode="HTML",
        reply_markup=_rows(mode, league, current, user.id, play_mode))


async def play_mode_callback(update, context):
    """``botplay_<chat|app>_<mode>_<league>_<owner>`` — switch where the match
    is played, keeping the difficulty prompt open."""
    query = update.callback_query
    parts = (query.data or "").split("_")   # botplay, pm, mode, league, owner
    pm = normalize_play_mode(parts[1] if len(parts) > 1 else "")
    mode = parts[2] if len(parts) > 2 else "lp"
    league = parts[3] if len(parts) > 3 else ""
    owner = parts[4] if len(parts) > 4 else ""
    user = update.effective_user
    if owner and user is not None and str(user.id) != owner:
        await query.answer("This isn't your match — send /lpbot or /ciplbot "
                           "to start your own.", show_alert=True)
        return
    remember_play_mode(context.bot_data, user.id, pm)
    await query.answer(f"Playing in: {PLAY_LABELS[pm]}")
    try:
        await query.edit_message_text(
            _prompt_text(pm), parse_mode="HTML",
            reply_markup=_rows(mode, league,
                               remembered_level(context.bot_data, user.id),
                               user.id, pm))
    except Exception:
        logger.debug("bot play mode: prompt refresh failed", exc_info=True)


def take_pending_choice(context):
    """True once, right after the player answered the prompt.

    Lets the command handler tell "the player just picked Hard, carry on" apart
    from "the player typed /lpbot", without a second prompt in between.
    """
    try:
        return bool(context.user_data.pop("_bot_difficulty_answered", False))
    except Exception:
        return False


async def difficulty_callback(update, context):
    """``botdiff_<level>_<mode>_<league>`` — remember the pick and start the match."""
    query = update.callback_query
    parts = (query.data or "").split("_")   # botdiff, level, mode, league, owner
    level = normalize_difficulty(parts[1] if len(parts) > 1 else "")
    mode = parts[2] if len(parts) > 2 else "lp"
    league = parts[3] if len(parts) > 3 else ""
    owner = parts[4] if len(parts) > 4 else ""
    user = update.effective_user

    # In a group the prompt is visible to everyone; only the player who asked
    # for the match may answer it, or a bystander's tap would quietly start a
    # match in their own name instead.
    if owner and user is not None and str(user.id) != owner:
        await query.answer("This isn't your match — send /lpbot or /ciplbot "
                           "to start your own.", show_alert=True)
        return

    await query.answer()
    if user is not None:
        remember_level(context.bot_data, user.id, level)
    try:
        context.user_data["_bot_difficulty_answered"] = True
    except Exception:
        logger.debug("bot difficulty: no user_data to flag", exc_info=True)

    try:
        pm = play_mode_for(context.bot_data, user.id) if user else DEFAULT_PLAY_MODE
        await query.edit_message_text(
            f"🤖 <b>Bot difficulty:</b> {DIFFICULTIES[level]['label']}\n"
            f"<i>{BLURBS[level]}</i>\n"
            f"<b>Play in:</b> {PLAY_LABELS[pm]}", parse_mode="HTML")
    except Exception:
        logger.debug("bot difficulty: clearing the prompt failed", exc_info=True)

    if mode == "cipl":
        from handlers.ciplbot import ciplbot_handler
        context.args = [league] if league else []
        await ciplbot_handler(update, context)
    else:
        from handlers.lpbot import lpbot_handler
        await lpbot_handler(update, context)
