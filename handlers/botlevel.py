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


def level_for_match(bot_data, user_id):
    """The difficulty a match for this player should be built with."""
    return remembered_level(bot_data, user_id) or DEFAULT_DIFFICULTY


def _rows(mode, league, current, owner_id):
    buttons = []
    for key in DIFFICULTY_ORDER:
        label = DIFFICULTIES[key]["label"]
        if key == current:
            label = f"• {label} •"
        buttons.append(InlineKeyboardButton(
            label, callback_data=f"botdiff_{key}_{mode}_{league}_{owner_id}"))
    return InlineKeyboardMarkup([buttons])


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
    lines = "\n".join(
        f"{DIFFICULTIES[k]['label']} — <i>{BLURBS[k]}</i>" for k in DIFFICULTY_ORDER)
    await message.reply_text(
        "🤖 <b>How hard should the bot play?</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{lines}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "<i>Every setting plays with the same squad and the same rules — only "
        "the captaincy gets sharper.</i>",
        parse_mode="HTML", reply_markup=_rows(mode, league, current, user.id))


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
        await query.edit_message_text(
            f"🤖 <b>Bot difficulty:</b> {DIFFICULTIES[level]['label']}\n"
            f"<i>{BLURBS[level]}</i>", parse_mode="HTML")
    except Exception:
        logger.debug("bot difficulty: clearing the prompt failed", exc_info=True)

    if mode == "cipl":
        from handlers.ciplbot import ciplbot_handler
        context.args = [league] if league else []
        await ciplbot_handler(update, context)
    else:
        from handlers.lpbot import lpbot_handler
        await lpbot_handler(update, context)
