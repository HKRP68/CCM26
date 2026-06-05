"""Telegram reply-context helpers.

This module installs a small python-telegram-bot monkey patch so outbound bot
messages sent while handling a user update default to replying to that user's
message.  Existing explicit reply targets are preserved.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_current_reply_chat_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_reply_chat_id", default=None
)
_current_reply_message_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_reply_message_id", default=None
)
_installed = False

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


def bind_reply_context(update: Any) -> None:
    """Bind the current update's user message as the default reply target.

    Callback-query updates do not include a new user chat message to reply to, so
    they are intentionally ignored unless PTB exposes a real ``update.message``.
    """
    _current_reply_chat_id.set(None)
    _current_reply_message_id.set(None)

    message = getattr(update, "message", None)
    if message is None:
        return

    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return

    _current_reply_chat_id.set(chat_id)
    _current_reply_message_id.set(message_id)


def _chat_ids_match(target_chat_id: Any, context_chat_id: int) -> bool:
    if target_chat_id == context_chat_id:
        return True
    try:
        return int(target_chat_id) == int(context_chat_id)
    except (TypeError, ValueError):
        return False


def _already_has_reply_target(kwargs: dict[str, Any]) -> bool:
    return (
        kwargs.get("reply_to_message_id") is not None
        or kwargs.get("reply_parameters") is not None
    )


def _wrap_send_method(method: Callable[..., Any]) -> Callable[..., Any]:
    try:
        parameters = set(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        parameters = {"reply_to_message_id", "reply_parameters", "allow_sending_without_reply"}

    @functools.wraps(method)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        context_chat_id = _current_reply_chat_id.get()
        context_message_id = _current_reply_message_id.get()
        if (
            context_chat_id is not None
            and context_message_id is not None
            and not _already_has_reply_target(kwargs)
        ):
            target_chat_id = kwargs.get("chat_id")
            if target_chat_id is None and args:
                target_chat_id = args[0]
            if target_chat_id is not None and _chat_ids_match(target_chat_id, context_chat_id):
                if "reply_to_message_id" in parameters:
                    kwargs["reply_to_message_id"] = context_message_id
                elif "reply_parameters" in parameters:
                    try:
                        from telegram import ReplyParameters

                        kwargs["reply_parameters"] = ReplyParameters(
                            message_id=context_message_id,
                            allow_sending_without_reply=True,
                        )
                    except Exception:
                        logger.exception("Could not build Telegram reply parameters")
                if "allow_sending_without_reply" in parameters:
                    kwargs.setdefault("allow_sending_without_reply", True)
        return await method(self, *args, **kwargs)

    return wrapped


def install_reply_defaults() -> None:
    """Install default reply behavior for common Telegram send methods."""
    global _installed
    if _installed:
        return

    try:
        from telegram import Bot
        from telegram.ext import ExtBot
    except Exception:  # pragma: no cover - defensive startup guard
        logger.exception("Could not import Telegram Bot classes for reply defaults")
        return

    for bot_cls in {Bot, ExtBot}:
        for method_name in _SEND_METHODS:
            method = getattr(bot_cls, method_name, None)
            if method is None or getattr(method, "_ccm_reply_defaults", False):
                continue
            wrapped = _wrap_send_method(method)
            setattr(wrapped, "_ccm_reply_defaults", True)
            setattr(bot_cls, method_name, wrapped)

    _installed = True
