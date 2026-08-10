"""Parsing of Telegram slash commands out of an update.

Two independent gates need the same answer to "is this a command, and which
one?" — maintenance mode (``services/maintenance_service.py``) and the Rookie
access gate (``services/rookie_gate.py``). Keeping the parsing here means the
two can never disagree about, say, ``/claim@OtherBot`` in a group chat.

Deliberately dependency-light: no ``telegram`` import, so both services (and
their tests) can use it against plain objects.
"""

from __future__ import annotations


def normalize_bot_username(username: str | None) -> str:
    """Normalize a Telegram bot username for case-insensitive comparison."""
    return (username or "").strip().lstrip("@").casefold()


def command_token_from_text(text: str | None) -> str:
    """Return the leading slash-command token from text, if present.

    The token is returned verbatim (``/claim@MyBot``); use
    :func:`command_name` to reduce it to a bare command name.
    """
    text = (text or "").lstrip()
    if not text.startswith("/"):
        return ""
    return text.split(maxsplit=1)[0]


def is_command_for_this_bot(command_token: str, bot_username: str | None = None) -> bool:
    """True for bare commands or commands addressed to this bot.

    Telegram group chats can contain commands explicitly addressed to another
    bot (for example, ``/help@OtherBot``). Normal CommandHandlers ignore those,
    so a gating middleware must ignore them too instead of answering on another
    bot's behalf.
    """
    import os

    if not command_token.startswith("/"):
        return False

    if "@" not in command_token:
        return True

    addressed_username = normalize_bot_username(command_token.rsplit("@", 1)[1])
    this_username = normalize_bot_username(bot_username or os.getenv("BOT_USERNAME"))
    return bool(this_username and addressed_username == this_username)


def command_name(command_token: str) -> str:
    """``/Claim@MyBot`` → ``claim``. Returns '' when the token isn't a command."""
    token = (command_token or "").strip()
    if not token.startswith("/"):
        return ""
    return token[1:].split("@", 1)[0].strip().casefold()


def command_token_from_update(update, bot_username: str | None = None) -> str:
    """Return the command token of an update's message, or '' if there is none.

    Only commands aimed at THIS bot are returned (see
    :func:`is_command_for_this_bot`). Falls back to the message's ``bot_command``
    entity when the text has not been normalized, so PTB message-like objects
    and test doubles behave the same.
    """
    message = getattr(update, "message", None)
    text = (getattr(message, "text", None) or "") if message else ""

    token = command_token_from_text(text)
    if token:
        return token if is_command_for_this_bot(token, bot_username) else ""

    lstripped_text = text.lstrip()
    leading_spaces = len(text) - len(lstripped_text)
    for entity in getattr(message, "entities", None) or []:
        entity_type = getattr(entity, "type", None)
        offset = getattr(entity, "offset", None)
        if entity_type != "bot_command" or offset not in (0, leading_spaces):
            continue
        length = getattr(entity, "length", None)
        entity_token = (
            lstripped_text[:length]
            if length
            else command_token_from_text(lstripped_text)
        )
        if entity_token:
            return (entity_token
                    if is_command_for_this_bot(entity_token, bot_username)
                    else "")
    return ""


def command_name_from_update(update, bot_username: str | None = None) -> str:
    """Bare command name of an update aimed at this bot ('claim'), or ''."""
    return command_name(command_token_from_update(update, bot_username))
