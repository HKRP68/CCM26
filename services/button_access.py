"""Owner access guard for Telegram inline callback buttons.

Messages with callback buttons that are sent while handling a user update are
registered against the originating Telegram user id.  A pre-callback middleware
uses that registration to stop other users from driving someone else's personal
UI.  URL/WebApp-only keyboards are ignored, and explicitly shared callback
prefixes (join/spectate/accept-style match buttons) remain open to everyone.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import time
from collections.abc import Iterable
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

BLOCKED_BUTTON_MESSAGE = "This button is not for you. Please use your own command."

_current_button_owner: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_button_owner", default=None
)

# Callback prefixes that are intentionally shared/global.  These include public
# lobby/join buttons, challenge/trade/tour invite responses intended for a
# second participant, and match-control buttons where the match state itself
# validates whose turn/action it is.
SHARED_CALLBACK_PREFIXES: tuple[str, ...] = (
    "cric_join",
    "cric_join_",
    "cric_join:",
    "cric_cancel_lobby",
    "cric_cancel_lobby_",
    "cric_decision:",
    "match_accept_",
    "match_deny_",
    "pm_accept_",
    "pm_deny_",
    # /playmatch (/pm) in-chat flow: invite response + the whole two-player
    # setup/gameplay handshake. Each handler validates the clicker by
    # telegram_id / live match state, so the same message is intentionally
    # driven by both players in turn (the owner guard must not block them).
    "matchacc_",
    "matchdeny_",
    "oversset_",
    "overscustom_",
    "toss_",
    "op1_",
    "op2_",
    "selbowl_",
    "bvar_",
    "blen_",
    "bspin_",
    "bshot_",
    "nbowl_",
    "newbat_",
    # /cipl (Challenge League) over-by-over flow: coin call, toss decision, and
    # the per-over bowler/approach buttons. cipl_play.py validates each click
    # against bowl_user_tg / bat_user_tg, so these are shared prompts too.
    "cipl_coin_",
    "cipl_toss_",
    "cipl_bowler_",
    "cipl_bowlapp_",
    "cipl_batapp_",
    "wsp_join",
    "wsp_decision:",
    "wsp_cancel",
    "cm_accept_",
    "cm_deny_",
    "cm_toss_",
    "cm_pick_",
    # /letsplay own-roster flow: the invite (sent while handling the HOST's
    # command, so it would otherwise be owned by the host) plus pitch, start
    # toss, coin and toss prompts are all driven by both players in turn.
    # letsplay.py validates every click by telegram_id / draft state, so these
    # must reach their handlers like the /cipl and /cm prompts above.
    "lp_accept_",
    "lp_deny_",
    "lp_pitch_",
    "lp_starttoss_",
    "lp_coin_",
    "lp_toss_",
    # Challenge League team and Playing XI buttons are shared prompts: the
    # same message is used by both host and guest, and challenge.py validates
    # which side may press each button based on the draft state.
    "cl_team_",
    "cl_xi_",
    "cl_pick_",
    "cl_confirm_",
    "cl_start_",
    # Pitch selection + the guest's Deny Match button live on the same prompt
    # (sent while handling the guest's team pick, so it would otherwise be owned
    # by the guest). challenge.py validates each: only the host picks the pitch,
    # only the guest may deny — so both sides must reach those handlers.
    "cl_pitch_",
    "cl_denymatch_",
    "pbo_accept_",
    "pbo_decline_",
    "pboacc_",
    "pbodec_",
    "bopick_",
    "taccept_",
    "treject_",
    "tac_",
    "tdc_",
    "us_join",
    "us_ans_",
    "mh_join_",
    "mh_start_",
    "mh_theme_",
    "mh_vote",
    "ct_join_",
    "ct_start_",
    "ct_theme_",
    "ct_vote",
    "bm_accept_",
    "bm_decline_",
    "bm_answer_",
    "bm_steal_",
)

_SEND_METHODS = (
    "send_message",
    "send_photo",
    "send_animation",
    "send_document",
    "send_dice",
    "send_video",
    "send_audio",
    "send_sticker",
    "send_location",
    "send_venue",
    "send_poll",
)

# Process-local cache.  Enough for callback access because callback presses are
# delivered to the same running bot process that sent the message in this app.
# Entries are pruned opportunistically.
_OWNER_BY_MESSAGE: dict[tuple[int, int], tuple[int, float]] = {}
_OWNER_TTL_SECONDS = 60 * 60 * 24
_INSTALLED = False


def bind_button_owner_context(update: Any) -> None:
    """Bind outbound callback-button messages in this update to its user."""
    user = getattr(update, "effective_user", None)
    _current_button_owner.set(getattr(user, "id", None))


def clear_button_owner_context() -> None:
    _current_button_owner.set(None)


def is_shared_callback_data(callback_data: Any) -> bool:
    """Return True for callback data that is intended for shared/global use."""
    if not isinstance(callback_data, str):
        return False
    return callback_data.startswith(SHARED_CALLBACK_PREFIXES)


def _iter_callback_data(reply_markup: Any) -> Iterable[str]:
    keyboard = getattr(reply_markup, "inline_keyboard", None)
    if not keyboard:
        return []
    values: list[str] = []
    for row in keyboard:
        for button in row:
            data = getattr(button, "callback_data", None)
            if isinstance(data, str) and data:
                values.append(data)
    return values


def _should_register_markup(reply_markup: Any) -> bool:
    callback_values = list(_iter_callback_data(reply_markup))
    if not callback_values:
        return False
    return any(not is_shared_callback_data(data) for data in callback_values)


def _extract_chat_message_id(message: Any) -> tuple[Optional[int], Optional[int]]:
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    try:
        chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        chat_id = None
    try:
        message_id = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        message_id = None
    return chat_id, message_id


def register_button_owner(chat_id: int, message_id: int, owner_user_id: int) -> None:
    _OWNER_BY_MESSAGE[(int(chat_id), int(message_id))] = (int(owner_user_id), time.monotonic())
    _prune_owner_cache()


def _prune_owner_cache() -> None:
    if len(_OWNER_BY_MESSAGE) < 4096:
        return
    cutoff = time.monotonic() - _OWNER_TTL_SECONDS
    stale = [key for key, (_owner, created_at) in _OWNER_BY_MESSAGE.items() if created_at < cutoff]
    for key in stale:
        _OWNER_BY_MESSAGE.pop(key, None)


def get_registered_owner(chat_id: int, message_id: int) -> Optional[int]:
    entry = _OWNER_BY_MESSAGE.get((int(chat_id), int(message_id)))
    if not entry:
        return None
    owner, created_at = entry
    if time.monotonic() - created_at > _OWNER_TTL_SECONDS:
        _OWNER_BY_MESSAGE.pop((int(chat_id), int(message_id)), None)
        return None
    return owner


def check_callback_owner(update: Any) -> bool:
    """Return True if the callback may continue for the clicking user."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return True
    if is_shared_callback_data(getattr(query, "data", None)):
        return True

    message = getattr(query, "message", None)
    chat_id, message_id = _extract_chat_message_id(message)
    if chat_id is None or message_id is None:
        return True

    owner = get_registered_owner(chat_id, message_id)
    if owner is None:
        # Legacy/unregistered messages stay usable so existing deployed buttons
        # are not bricked after a restart.
        return True

    clicking_user_id = getattr(getattr(query, "from_user", None), "id", None)
    return clicking_user_id == owner


async def button_access_guard(update: Any, context: Any) -> None:
    """PTB middleware that blocks non-owners before callback handlers run."""
    if check_callback_owner(update):
        return
    query = update.callback_query
    try:
        await query.answer(BLOCKED_BUTTON_MESSAGE, show_alert=True)
    except Exception:
        logger.exception("Failed to answer blocked callback query")
    from telegram.ext import ApplicationHandlerStop

    raise ApplicationHandlerStop


def _wrap_send_method(method: Callable[..., Any]) -> Callable[..., Any]:
    try:
        parameters = set(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        parameters = {"reply_markup"}

    @functools.wraps(method)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        reply_markup = kwargs.get("reply_markup") if "reply_markup" in parameters else None
        owner_user_id = _current_button_owner.get()
        sent_message = await method(self, *args, **kwargs)
        if owner_user_id is None or not _should_register_markup(reply_markup):
            return sent_message

        chat_id, message_id = _extract_chat_message_id(sent_message)
        if chat_id is not None and message_id is not None:
            register_button_owner(chat_id, message_id, owner_user_id)
        return sent_message

    return wrapped


def install_button_access_defaults() -> None:
    """Patch Telegram Bot send methods so owner-specific keyboards are tracked."""
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        from telegram import Bot
        from telegram.ext import ExtBot
    except Exception:  # pragma: no cover - defensive startup guard
        logger.exception("Could not import Telegram Bot classes for button access defaults")
        return

    for bot_cls in {Bot, ExtBot}:
        for method_name in _SEND_METHODS:
            method = getattr(bot_cls, method_name, None)
            if method is None or getattr(method, "_ccm_button_access_defaults", False):
                continue
            wrapped = _wrap_send_method(method)
            setattr(wrapped, "_ccm_button_access_defaults", True)
            setattr(bot_cls, method_name, wrapped)

    _INSTALLED = True
