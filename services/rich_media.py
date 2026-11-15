"""Native rich media with real multipart uploads through PTB's transport."""

from copy import deepcopy
from telegram import InputFile, Message
from services import rich_message as R


def media_from_message(message):
    rich = getattr(message, "api_kwargs", {}).get("rich_message", {})
    for block in rich.get("blocks", []):
        kind = block.get("type")
        if kind not in ("photo", "animation", "video"):
            continue
        media = block.get(kind)
        if isinstance(media, list):
            media = media[-1] if media else None
        if isinstance(media, dict) and media.get("file_id"):
            return {"type": kind, kind: {"type": kind, "media": media["file_id"]}}
    return None


async def send(
    bot,
    chat_id,
    source,
    blocks,
    *,
    kind="photo",
    reply_markup=None,
    reply_to_message_id=None,
):
    """Return None on native refusal so the caller can send one legacy card."""
    if not R.rich_text_enabled(bot) or not getattr(bot, "_post", None):
        return None
    uploads = {}
    if isinstance(source, str):
        reference = source
    else:
        reference = "attach://cmu_rich_media"
        position = source.tell() if hasattr(source, "tell") else None
        uploads["cmu_rich_media"] = InputFile(
            source, filename=f'card.{"jpg" if kind=="photo" else "mp4"}'
        )
        if position is not None and hasattr(source, "seek"):
            source.seek(position)
    media = {"type": kind, kind: {"type": kind, "media": reference}}
    payload = {
        "chat_id": chat_id,
        "rich_message": {"blocks": [media] + R.normalize_blocks(blocks)},
        **uploads,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    result = await R._request_rich(bot, "sendRichMessage", payload, retry_html=False)
    return Message.de_json(result, bot) if isinstance(result, dict) else None


async def edit(
    bot,
    chat_id,
    message_id,
    blocks,
    text,
    *,
    original=None,
    media=None,
    legacy_photo=False,
):
    """Replace this claim card, preserving its photo and removing all controls."""
    media = media or (media_from_message(original) if original is not None else None)
    rich = (
        getattr(original, "api_kwargs", {}).get("rich_message")
        if original is not None
        else None
    )
    legacy_photo = legacy_photo or (bool(getattr(original, "photo", None)) and not rich)
    content = ([deepcopy(media)] if media else []) + list(blocks or [R.paragraph(text)])
    if not legacy_photo and await R.edit_rich_message(
        bot,
        chat_id,
        message_id,
        content,
        reply_markup={"inline_keyboard": []},
        fallback_text=text,
    ):
        return True
    # A failed rich media edit must not flatten the photo or send another card.
    if media:
        return False
    try:
        with R.legacy_delivery():
            if legacy_photo:
                from services.telegram_text import utf16_length

                caption = (
                    text
                    if utf16_length(text) <= 1024
                    else "<b>Claim completed.</b> Open /myroster for your squad."
                )
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=None,
                )
        return True
    except Exception as exc:
        if "not modified" in str(exc).lower():
            return True
        return False
