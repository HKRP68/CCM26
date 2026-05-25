"""Unified player-card sender.

Encapsulates the "preferred-method" hierarchy for sending a player card
to Telegram:

  1. If the player has a custom image AND that image's `tg_file_id` is
     cached, send via file_id (zero disk read, zero Neon, zero PIL CPU).
  2. Else if there's a custom image but no file_id yet, read bytes from
     disk and send. After sending, capture the returned file_id and cache
     it for next time.
  3. Else generate a fresh card via card_generator.generate_card(player).
     Note: generated cards can't be channel-cached because they're
     dynamic (player ratings can change in admin → card needs regen).

All send sites in the bot should use this instead of calling reply_photo
or send_photo directly.
"""

import io
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


async def send_player_card(
    *, bot, chat_id, player, caption=None, reply_markup=None,
    parse_mode="HTML", reply_to_message_id=None, session=None,
) -> Optional[object]:
    """Send a player's card to a chat. Prefers cached Telegram file_id.

    Args:
      bot: a `telegram.Bot` (or PTB Update.message.reply_photo via context)
      chat_id: where to send
      player: a Player ORM object (must have .id, .name, .rating, etc.)
      caption: optional HTML caption
      reply_markup: optional InlineKeyboardMarkup
      parse_mode: "HTML" (default) or "Markdown"
      reply_to_message_id: optional reply target
      session: optional SQLAlchemy session — if provided, we'll cache any
        newly-returned file_id for future sends. If None, we'll skip the
        write-back (still sends successfully, just no caching).

    Returns:
      The sent Message object, or None on failure (falls back to text).
    """
    from services.player_image_service import (
        get_tg_file_id, get_custom_image_bytes,
    )

    common_kwargs = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": parse_mode,
        "reply_markup": reply_markup,
    }
    if reply_to_message_id is not None:
        common_kwargs["reply_to_message_id"] = reply_to_message_id

    # ── Strategy 1: Cached file_id ──
    # Need a session to look it up. If caller didn't pass one, we open one
    # just for the lookup (cheap, single-row select on indexed col).
    cached_file_id = None
    if session is not None:
        try:
            cached_file_id = get_tg_file_id(session, player.id)
        except Exception:
            pass
    else:
        try:
            from database import SessionLocal
            with SessionLocal() as _s:
                cached_file_id = get_tg_file_id(_s, player.id)
        except Exception:
            pass

    if cached_file_id:
        try:
            return await bot.send_photo(photo=cached_file_id, **common_kwargs)
        except Exception as e:
            # file_id might be stale (e.g. channel deleted, file rotated).
            # Clear the cache and fall through.
            logger.warning(f"file_id send failed ({e!r}), falling back")
            try:
                from models import PlayerImage
                if session is not None:
                    row = (session.query(PlayerImage)
                           .filter(PlayerImage.player_id == player.id).first())
                    if row:
                        row.tg_file_id = None
            except Exception:
                pass

    # ── Strategy 2: Custom image bytes from disk ──
    # Then opportunistically cache the returned file_id.
    try:
        custom_bytes = get_custom_image_bytes(player.id)
    except Exception:
        custom_bytes = None

    if custom_bytes:
        try:
            msg = await bot.send_photo(
                photo=io.BytesIO(custom_bytes), **common_kwargs)
            # Capture the file_id Telegram returned for future use.
            try:
                if msg and msg.photo and session is not None:
                    new_file_id = msg.photo[-1].file_id
                    from models import PlayerImage
                    row = (session.query(PlayerImage)
                           .filter(PlayerImage.player_id == player.id).first())
                    if row and not row.tg_file_id:
                        row.tg_file_id = new_file_id
                        session.flush()
                        logger.info(f"Cached file_id for player {player.id} from organic send")
            except Exception:
                logger.exception("file_id capture failed (non-fatal)")
            return msg
        except Exception:
            logger.warning("Custom image send failed, falling back")

    # ── Strategy 3: Auto-generated card ──
    try:
        from services.card_generator import generate_card
        gen_bytes = generate_card(player)
    except Exception:
        gen_bytes = None

    if gen_bytes:
        try:
            return await bot.send_photo(
                photo=io.BytesIO(gen_bytes), **common_kwargs)
        except Exception:
            logger.warning("Generated card send failed, falling back to text")

    # ── Final fallback: text only ──
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=caption or f"<b>{player.name}</b> · {player.rating} OVR",
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("Even text fallback failed")
        return None
