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

import asyncio
import io
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _basic_card_bytes(player) -> bytes:
    """Small always-available PNG fallback when custom/template cards fail."""
    from PIL import Image, ImageDraw, ImageFont

    width, height = 700, 420
    img = Image.new("RGB", (width, height), (15, 23, 42))
    draw = ImageDraw.Draw(img)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    regular_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    title_font = ImageFont.truetype(font_path, 46)
    rating_font = ImageFont.truetype(font_path, 96)
    body_font = ImageFont.truetype(regular_path, 26)
    small_font = ImageFont.truetype(regular_path, 22)

    rating = str(getattr(player, "rating", "?"))
    name = str(getattr(player, "name", "Player"))
    category = str(getattr(player, "category", ""))
    country = str(getattr(player, "country", ""))
    bat_rating = str(getattr(player, "bat_rating", "-"))
    bowl_rating = str(getattr(player, "bowl_rating", "-"))

    draw.rounded_rectangle((18, 18, width - 18, height - 18), radius=28,
                           outline=(250, 204, 21), width=5)
    draw.text((52, 48), rating, fill=(250, 204, 21), font=rating_font)
    draw.text((65, 145), "OVR", fill=(226, 232, 240), font=small_font)
    draw.text((220, 58), name.upper()[:22], fill=(255, 255, 255), font=title_font)
    draw.text((224, 128), category, fill=(147, 197, 253), font=body_font)
    draw.line((220, 180, 640, 180), fill=(71, 85, 105), width=2)
    draw.text((224, 215), f"Batting: {bat_rating}", fill=(226, 232, 240), font=body_font)
    draw.text((224, 260), f"Bowling: {bowl_rating}", fill=(226, 232, 240), font=body_font)
    draw.text((224, 318), country, fill=(203, 213, 225), font=small_font)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def send_player_card(
    *, bot, chat_id, player, caption=None, reply_markup=None,
    parse_mode="HTML", reply_to_message_id=None, session=None,
    allow_generated=True,
) -> Optional[object]:
    """Send a player's card to a chat. Prefers cached Telegram file_id.

    When ``allow_generated`` is False, only a genuine admin-uploaded custom
    image is sent as a photo; players without a custom card fall straight
    through to the text reply (no auto-generated or basic card image). The
    cached *generated* card (``Player.card_file_id``) is also skipped in that
    mode, since it is not a custom card.

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

    # ── Strategy 1: Cached file_id (custom image) ──
    # Check PlayerImage.tg_file_id first (admin-uploaded custom card).
    # If no session, open a short one just for the lookup.
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

    # ── Strategy 1.5: Cached file_id (generated card) ──
    # Player.card_file_id holds the file_id from the last auto-generated send.
    # Skipped when generated cards are disabled — it is not a custom card.
    if not cached_file_id and allow_generated:
        cached_file_id = getattr(player, "card_file_id", None)

    if cached_file_id:
        try:
            return await bot.send_photo(photo=cached_file_id, **common_kwargs)
        except Exception as e:
            # file_id might be stale (e.g. channel deleted, file rotated).
            # Clear whichever cache held it and fall through.
            logger.warning(f"file_id send failed ({e!r}), falling back")
            try:
                from models import PlayerImage
                if session is not None:
                    row = (session.query(PlayerImage)
                           .filter(PlayerImage.player_id == player.id).first())
                    if row:
                        row.tg_file_id = None
                    if getattr(player, "card_file_id", None) == cached_file_id:
                        player.card_file_id = None
                        session.flush()
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

    # ── No custom card available ──
    # When generated cards are disabled, stop here and fall through to the text
    # reply instead of rendering an auto-generated or basic card.
    if not allow_generated:
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=caption or f"<b>{player.name}</b> · {player.rating} OVR",
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                **({"reply_to_message_id": reply_to_message_id}
                   if reply_to_message_id is not None else {}),
            )
        except Exception:
            logger.exception("Text-only player card send failed")
            return None

    # ── Strategy 3: Auto-generated card ──
    try:
        from services.card_generator import generate_card
        # Pillow rendering is CPU-bound and synchronous — run it off the event
        # loop so a cache-miss render doesn't freeze the whole bot.
        gen_bytes = await asyncio.to_thread(generate_card, player)
    except Exception:
        gen_bytes = None

    if gen_bytes:
        try:
            gen_msg = await bot.send_photo(
                photo=io.BytesIO(gen_bytes), **common_kwargs)
            # Cache the returned file_id so future sends skip the upload.
            try:
                if gen_msg and gen_msg.photo and not getattr(player, "card_file_id", None):
                    new_fid = gen_msg.photo[-1].file_id
                    if session is not None:
                        player.card_file_id = new_fid
                        session.flush()
                    else:
                        from database import SessionLocal
                        from models import Player as _Player
                        with SessionLocal() as _s:
                            _p = _s.query(_Player).get(player.id)
                            if _p and not _p.card_file_id:
                                _p.card_file_id = new_fid
                                _s.commit()
                        player.card_file_id = new_fid  # update in-memory obj too
                    logger.info(f"Cached generated card file_id for player {player.id}")
            except Exception:
                logger.exception("Generated card file_id capture failed (non-fatal)")
            return gen_msg
        except Exception:
            logger.warning("Generated card send failed, trying basic card fallback")

    # ── Strategy 4: Minimal generated card ──
    # Keeps player-card commands image-first even if a custom/template image is
    # missing, oversized, or fails during rendering.
    try:
        basic_bytes = _basic_card_bytes(player)
        return await bot.send_photo(photo=io.BytesIO(basic_bytes), **common_kwargs)
    except Exception:
        logger.warning("Basic card fallback failed, falling back to text")

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
