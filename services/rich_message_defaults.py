"""Promote ordinary bot text sends/edits to native rich messages.

Installed once at startup below PTB's send/reply methods, so commands, jobs,
match prompts, DMs and broadcasts share the same behavior. Existing keyboards,
reply targets, topics, privacy parameters, rate limiting and timeouts survive.
Media captions stay on their media with their original formatting.
"""

from functools import wraps
import logging
from threading import Lock

from telegram import Message

from services import rich_message as R

logger = logging.getLogger(__name__)
_install_lock = Lock()


def content_for(data):
    """Keep HTML and explicit Telegram entities; plain text is never HTML."""
    text = data.get("text")
    if not isinstance(text, str) or not text:
        return None
    entities = data.get("entities")
    if entities:
        from datetime import datetime, timezone
        from telegram import Chat, MessageEntity

        entities = [
            MessageEntity.de_json(item, None) if isinstance(item, dict) else item
            for item in entities
        ]
        message = Message(
            0,
            datetime.fromtimestamp(0, timezone.utc),
            Chat(0, "private"),
            text=text,
            entities=entities,
        )
        return {"html": message.text_html}
    mode = data.get("parse_mode")
    if mode == "HTML":
        from services.rich_layout import aligned_blocks

        aligned = aligned_blocks(text)
        return {"blocks": aligned} if aligned else {"html": text}
    if mode is None:
        from html import escape
        from services.rich_layout import aligned_blocks

        aligned = aligned_blocks(escape(text))
        if aligned:
            return {"blocks": aligned}
        return {"blocks": [R.paragraph(text)]}
    # Legacy Markdown has different semantics from rich Markdown. Preserve its
    # original renderer rather than silently changing bold/code/link content.
    return None


def wrap_post(post):
    @wraps(post)
    async def rich_post(self, endpoint, data=None, **kwargs):
        native_endpoint = {
            "sendMessage": "sendRichMessage",
            "editMessageText": "editMessageText",
        }.get(endpoint)
        if (
            native_endpoint is None
            or R.legacy_delivery_active()
            or not R.rich_text_enabled(self, native_endpoint)
            or not data
            or "rich_message" in data
        ):
            return await post(self, endpoint, data, **kwargs)

        prepared = dict(data)
        prepared.update(kwargs.get("api_kwargs") or {})
        if "rich_message" in prepared:
            return await post(self, endpoint, data, **kwargs)
        self._insert_defaults(prepared)
        prepared = {key: value for key, value in prepared.items() if value is not None}
        try:
            content = content_for(prepared)
        except (TypeError, ValueError):
            logger.warning(
                "Unable to prepare rich text; preserving original message",
                exc_info=True,
            )
            content = None
        if content is None:
            return await post(self, endpoint, data, **kwargs)

        payload = {
            key: value
            for key, value in prepared.items()
            if key not in ("text", "parse_mode", "entities", "link_preview_options")
        }
        payload["rich_message"] = content
        transport_options = {
            key: value for key, value in kwargs.items() if key != "api_kwargs"
        }

        async def native_post(method, body):
            return await post(self, method, body, **transport_options)

        result = await R._request_rich(self, native_endpoint, payload, post=native_post)
        if result is not None:
            return result
        # The original payload includes all privacy/options and uses the same
        # authenticated PTB transport. Never recursively promote this fallback.
        return await post(self, endpoint, data, **kwargs)

    rich_post._cmu_rich_defaults = True
    return rich_post


def install_rich_defaults():
    from telegram import Bot
    from services.rich_message_compat import install_message_compat

    with _install_lock:
        if not getattr(Bot._post, "_cmu_rich_defaults", False):
            Bot._post = wrap_post(Bot._post)
        install_message_compat()
