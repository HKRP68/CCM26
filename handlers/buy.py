"""Handler for /buypl <player_name> — buy a player from the market."""

import asyncio
import io
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.card_generator import generate_card
from config import get_buy_value, get_sell_value, MAX_ROSTER
from services.activity_service import log_activity
from services.card_text import format_player_card
from services.roster_lock import MARKET_REASON, match_lock_message
from utils.idempotency import claim_once, release

logger = logging.getLogger(__name__)


def _official_group_config(session):
    """Return (official_group_id, official_group_link) from GameConfig."""
    try:
        from models import GameConfig
        cfg = session.query(GameConfig).first()
        if cfg:
            link = cfg.official_group_link
            # Fall back to building a link from the branding group username
            if not link and cfg.branding_group_username:
                link = "https://t.me/" + cfg.branding_group_username.lstrip("@")
            return cfg.official_group_id, link
    except Exception:
        logger.exception("official group config read failed")
    return None, None


def _is_buy_restricted(session, update):
    """True if /buypl is used in a group that is NOT the official group.

    DMs are never restricted. If no official group is configured, nothing is
    restricted (so existing behavior is preserved until you set it).
    Returns (restricted: bool, official_link: str|None).
    """
    chat = update.effective_chat
    official_id, official_link = _official_group_config(session)
    # Not configured → don't restrict anything
    if not official_id:
        return False, official_link
    # Private chat (DM) → always allowed
    if chat is None or chat.type == "private":
        return False, official_link
    # Group/supergroup → restricted unless it's the official group
    if chat.id == official_id:
        return False, official_link
    return True, official_link


def _join_gc_keyboard(official_link):
    """Single button that jumps to the official group."""
    if not official_link:
        # No link configured — show a disabled-looking note instead of a button
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔗 Join the GC to buy this player", url=official_link)
    ]])


async def buypl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage: /buypl <player name>\nExample: /buypl Virat Kohli")
        return

    search = " ".join(context.args).strip()

    # Instant feedback while we look up the player + render the card (the render
    # runs off the event loop, so this never blocks other users' commands).
    try:
        await update.message.reply_chat_action(ChatAction.UPLOAD_PHOTO)
    except Exception:
        pass

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        locked = match_lock_message(session, user.id, "buy players",
                                    reason=MARKET_REASON)
        if locked:
            await update.message.reply_text(locked, parse_mode="HTML")
            return

        if user.roster_count >= MAX_ROSTER:
            await update.message.reply_text(
                f"❌ Roster full ({MAX_ROSTER}/{MAX_ROSTER})! Release players first.")
            return

        # Find any matching player
        from services.version_paginator import (
            find_players_for_search, format_multiple_players_message,
            get_versions_ordered, build_pagination_keyboard, page_number_for,
            _format_version_label,
        )
        matches = find_players_for_search(session, search)
        if not matches:
            await update.message.reply_text(f"❌ No player found matching '{search}'")
            return
        # Several different players match (e.g. "Sachin" / "Kumar") → ask the
        # user to disambiguate with the full name instead of guessing.
        if len(matches) > 1:
            await update.message.reply_text(
                format_multiple_players_message("buypl", search, matches),
                parse_mode="HTML")
            return
        player = matches[0]

        # Build the version list and pick which page to start on
        base_id = player.parent_player_id or player.id
        versions = get_versions_ordered(session, base_id)
        if not versions:
            versions = [player]
        # Always start on the first page (Base) — user can navigate from there
        start_player = versions[0]
        current_idx = 0

        # Official-GC restriction: in a non-official group, show the player but
        # only a "join the GC" button (no buy / nav / cancel).
        restricted, official_link = _is_buy_restricted(session, update)

        await _send_version_page(
            session=session, user=user, versions=versions,
            current_idx=current_idx, owner_tg=tg_user.id,
            send_to=update.message, context=context,
            restricted=restricted, official_link=official_link,
        )

    except Exception:
        logger.exception(f"BuyPl error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def _replace_paged_message(context, edit_query, *, photo, caption, keyboard):
    """Delete the paged message and resend it, switching between text and photo.

    A Telegram message can't be edited from text to photo (or back), so when the
    target version's custom-card availability differs from the current message we
    replace it instead of editing in place.
    """
    msg = edit_query.message
    chat_id = msg.chat_id
    try:
        await context.bot.delete_message(chat_id, msg.message_id)
    except Exception:
        pass
    if photo is not None:
        await context.bot.send_photo(
            chat_id, photo=photo, caption=caption, parse_mode="HTML", reply_markup=keyboard)
    else:
        await context.bot.send_message(
            chat_id, text=caption, parse_mode="HTML", reply_markup=keyboard)


async def _send_version_page(*, session, user, versions, current_idx, owner_tg,
                             send_to, context, edit_query=None,
                             restricted=False, official_link=None):
    """Render one version page. If edit_query is set, edit that message in
    place; otherwise send a new one via send_to.

    If restricted=True (used in a non-official group), the normal buy/nav
    buttons are replaced by a single "Join the GC to buy this player" button.
    """
    from services.version_paginator import build_pagination_keyboard, _format_version_label
    import io as _io

    player = versions[current_idx]
    n = len(versions)

    # Caption text
    caption_lines = []
    page_label = f"<b>Page {current_idx + 1}/{n}</b>" if n > 1 else ""
    if page_label and not restricted:
        caption_lines.append(page_label)
    caption_lines.append(
        format_player_card(player, value_mode="buy", session=session))
    if n > 1 and not restricted:
        version_label = _format_version_label(player)
        caption_lines.append(f"\n🎴 <i>Version: <b>{version_label}</b></i>")

    if restricted:
        # Show info but make clear buying is locked to the official group.
        caption_lines.append(
            "\n🔴 <b>Buying is restricted to the Official Group.</b>\n"
            "Tap below to join and buy this player."
        )
        caption = "\n".join(caption_lines)
        keyboard = _join_gc_keyboard(official_link)
        # send_player_card renders/caches the card itself (off-thread) — no need
        # to generate here.
        from services.card_sender import send_player_card
        chat_id = (send_to.chat_id if hasattr(send_to, 'chat_id')
                   else send_to.chat.id)
        sent = await send_player_card(
            bot=context.bot, chat_id=chat_id, player=player,
            caption=caption, reply_markup=keyboard, session=None,
        )
        if sent is None:
            await send_to.reply_text(caption, parse_mode="HTML", reply_markup=keyboard)
        return

    caption_lines.append(f"\n💳 Your Balance: <b>{user.total_coins:,}</b> 🪙")
    caption = "\n".join(caption_lines)

    keyboard = build_pagination_keyboard(
        session=session, user=user, versions=versions,
        current_index=current_idx, owner_tg=owner_tg, flow="buy",
    )

    if edit_query is not None:
        # Custom-card-only: show a photo only when this version has an admin
        # custom card; otherwise show text. Switch the message type (delete +
        # resend) when paging between custom and non-custom versions.
        from services.player_image_service import get_tg_file_id, get_custom_image_bytes
        is_photo_msg = bool(edit_query.message.photo)
        custom_fid = get_tg_file_id(session, player.id)
        custom_bytes = None
        if not custom_fid:
            try:
                custom_bytes = await asyncio.to_thread(get_custom_image_bytes, player.id)
            except Exception:
                custom_bytes = None

        # No custom card → honor an admin /setcardid manual override, else render
        # the template/generated card so paging still shows an image instead of
        # replacing a pinned card or falling back to plain text.
        manual_fid = None if (custom_fid or custom_bytes) else getattr(player, "card_file_id", None)
        # Prefer a persisted generated-card file_id (skips the Pillow render);
        # only render fresh bytes on a cache miss.
        gen_fid = None
        gen_bytes = None
        if not custom_fid and not custom_bytes and not manual_fid:
            from services.card_generator import get_generated_card_file_id
            gen_fid = get_generated_card_file_id(player.id)
            if not gen_fid:
                try:
                    from services.card_generator import generate_card
                    gen_bytes = await asyncio.to_thread(generate_card, player)
                except Exception:
                    gen_bytes = None

        if custom_fid or custom_bytes or manual_fid or gen_fid or gen_bytes:
            media_src = (custom_fid or manual_fid or gen_fid
                         or _io.BytesIO(custom_bytes or gen_bytes))
            if is_photo_msg:
                from telegram import InputMediaPhoto
                try:
                    edited = await edit_query.edit_message_media(
                        media=InputMediaPhoto(media=media_src, caption=caption, parse_mode="HTML"),
                        reply_markup=keyboard,
                    )
                    # We just uploaded freshly-rendered generated bytes — persist
                    # the returned file_id so later pages reuse it (no re-render).
                    if gen_bytes:
                        from services.card_generator import cache_sent_card_file_id
                        cache_sent_card_file_id(player.id, edited)
                except TelegramError:
                    # A stale Telegram file_id wedges paging on the old card. If we
                    # sent one, re-render to bytes and retry; if editing still fails,
                    # replace the message so paging never sticks on the old card.
                    if custom_fid or manual_fid or gen_fid:
                        logger.warning("edit_version_page media edit failed; retrying with rendered bytes")
                        if gen_fid:
                            # Persisted generated id was stale — forget it so the
                            # next send re-renders + re-caches a fresh one.
                            from services.card_generator import drop_generated_card_file_id
                            drop_generated_card_file_id(player.id)
                        retry_bytes = custom_bytes or gen_bytes
                        if retry_bytes is None:
                            try:
                                from services.card_generator import generate_card
                                retry_bytes = await asyncio.to_thread(generate_card, player)
                            except Exception:
                                retry_bytes = None
                        if retry_bytes:
                            try:
                                edited = await edit_query.edit_message_media(
                                    media=InputMediaPhoto(media=_io.BytesIO(retry_bytes),
                                                          caption=caption, parse_mode="HTML"),
                                    reply_markup=keyboard,
                                )
                                # gen_fid stale → retry_bytes is a fresh generated
                                # card; cache its file_id so paging stops re-rendering.
                                if gen_fid:
                                    from services.card_generator import cache_sent_card_file_id
                                    cache_sent_card_file_id(player.id, edited)
                            except TelegramError:
                                logger.warning("edit_version_page media retry failed; replacing message")
                                await _replace_paged_message(
                                    context, edit_query, photo=_io.BytesIO(retry_bytes),
                                    caption=caption, keyboard=keyboard)
                        else:
                            logger.warning("edit_version_page media edit failed; replacing with text")
                            await _replace_paged_message(
                                context, edit_query, photo=None,
                                caption=caption, keyboard=keyboard)
                    else:
                        # media_src was rendered bytes — re-send fresh bytes.
                        logger.warning("edit_version_page media edit failed; replacing message")
                        await _replace_paged_message(
                            context, edit_query, photo=_io.BytesIO(custom_bytes or gen_bytes),
                            caption=caption, keyboard=keyboard)
            else:
                # Text → photo isn't editable; replace the message.
                await _replace_paged_message(
                    context, edit_query, photo=media_src, caption=caption, keyboard=keyboard)
            return

        # No custom card → text.
        if is_photo_msg:
            # Photo → text isn't editable; replace the message.
            await _replace_paged_message(
                context, edit_query, photo=None, caption=caption, keyboard=keyboard)
        else:
            try:
                await edit_query.edit_message_text(
                    text=caption, parse_mode="HTML", reply_markup=keyboard,
                )
            except Exception:
                logger.exception("edit_version_page text edit failed")
        return

    # New message — pass session so file_ids can be written back after first send.
    from services.card_sender import send_player_card
    chat_id = (send_to.chat_id if hasattr(send_to, 'chat_id')
               else send_to.chat.id)
    sent = await send_player_card(
        bot=context.bot, chat_id=chat_id, player=player,
        caption=caption, reply_markup=keyboard, session=session,
    )
    if sent is None:
        sent = await send_to.reply_text(caption, parse_mode="HTML", reply_markup=keyboard)

    try:
        from services.button_timeout import schedule_button_timeout
        schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=120)
    except Exception:
        pass


async def player_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for plpg_<flow>_<owner_tg>_<player_id> — navigate between pages."""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        # plpg_<flow>_<owner_tg>_<player_id>
        flow = parts[1]
        owner_tg = int(parts[2])
        target_player_id = int(parts[3])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your card!", show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("Do /debut first")
            return

        target = session.query(Player).get(target_player_id)
        if not target:
            await q.answer("Player not found")
            return

        from services.version_paginator import get_versions_ordered, page_number_for
        base_id = target.parent_player_id or target.id
        versions = get_versions_ordered(session, base_id)
        if not versions:
            versions = [target]
        idx = page_number_for(versions, target.id)

        await q.answer()
        await _send_version_page(
            session=session, user=user, versions=versions,
            current_idx=idx, owner_tg=tg.id,
            send_to=None, context=context, edit_query=q,
        )
    except Exception:
        logger.exception("player_page_callback failed")
        try: await q.answer("⚠️ Error", show_alert=True)
        except Exception: pass
    finally:
        session.close()


async def player_page_noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback for plpgnoop_ — page indicator / owned-marker. Just acknowledge."""
    q = update.callback_query
    if "owned" in (q.data or "").lower() or True:
        await q.answer()


async def buypl_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user

    # Idempotency: dedup rapid/duplicate taps on THIS Buy button instance.
    # Keyed on the message so a future /buypl (a new button) is never blocked.
    key = f"buy_{query.message.chat_id}_{query.message.message_id}"
    if not claim_once(key):
        await query.answer("Already processing…")
        return
    await query.answer()

    parts = query.data.split("_")  # buypl_{player_id}_{user_id}
    player_id = int(parts[1])
    owner_user_id = int(parts[2])

    session = get_session()
    try:
        user = session.query(User).get(owner_user_id)
        if not user or user.telegram_id != tg_user.id:
            # Not the owner — don't strip their button; just drop the claim.
            release(key)
            return

        # Owner confirmed — now remove the keyboard so the client can't re-fire it.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # The card may have been opened before the match started — re-check at
        # the moment the coins would actually move.
        locked = match_lock_message(session, user.id, "buy players",
                                    reason=MARKET_REASON)
        if locked:
            release(key)
            await query.message.reply_text(locked, parse_mode="HTML")
            return

        # Official-GC restriction: block purchase if the callback fires from a
        # non-official group (e.g. a stale/forwarded button).
        restricted, official_link = _is_buy_restricted(session, update)
        if restricted:
            release(key)
            kb = _join_gc_keyboard(official_link)
            try:
                await query.edit_message_reply_markup(reply_markup=kb)
            except Exception:
                pass
            await query.message.reply_text("🔴 Buying is restricted to the Official Group.")
            return

        player = session.query(Player).get(player_id)
        if not player:
            release(key)
            await query.message.reply_text("❌ Player no longer available")
            return

        # Defense in depth: this callback only fires from /buypl flow, so
        # honor the restricted_from_buypl flag even if a stale button slipped
        # through (e.g. admin enabled the flag after the buttons were sent).
        if getattr(player, "restricted_from_buypl", False):
            release(key)
            await query.message.reply_text(
                f"🚫 <b>{player.name}</b> is <b>Not available to Buy</b> via /buypl.\n\n"
                f"<i>Try the player market (/playermarket), packs, or trades.</i>",
                parse_mode="HTML",
            )
            return

        buy_val = get_buy_value(player.rating)
        username = tg_user.username or tg_user.first_name

        if user.total_coins < buy_val:
            release(key)
            shortage = buy_val - user.total_coins
            await query.message.reply_text(
                f"❌ @{username} needs {shortage:,} more coins to buy {player.name}\n"
                f"💰 Balance: {user.total_coins:,} | Price: {buy_val:,}"
            )
            return

        if user.roster_count >= MAX_ROSTER:
            release(key)
            await query.message.reply_text("❌ Roster full! Release players first.")
            return

        # Re-check version ownership (user might have bought another version since)
        from services.version_service import user_owns_any_version
        if user_owns_any_version(session, user.id, player.id):
            release(key)
            await query.message.reply_text(
                f"❌ You already own a version of <b>{player.name}</b>.",
                parse_mode="HTML",
            )
            return

        # Buy the player
        user.total_coins -= buy_val
        entry = UserRoster(
            user_id=user.id, player_id=player.id,
            order_position=user.roster_count + 1,
            acquired_date=datetime.utcnow(),
        )
        session.add(entry)
        session.flush()  # populate entry.id for undo record
        user.roster_count += 1

        log_activity(session, user.id, "buy",
                     f"Bought {player.name} ({player.rating} OVR) for {buy_val:,}",
                     coins_change=-buy_val,
                     player_name=player.name, player_rating=player.rating)

        # Elite signing rebate, while that offer is running. Read once so the
        # receipt's countdown is the same offer the gems were paid from.
        from services.buy_bonus import (
            award_buy_gem_bonus, bonus_line, current_offer)
        offer = current_offer(session)
        gem_bonus = award_buy_gem_bonus(session, user, player, buy_val,
                                        source="buypl", offer=offer)

        # Record for /cmuundo (60-second window)
        try:
            from services.undo_service import record_buy
            record_buy(session, user.id,
                       roster_id=entry.id, player_id=player.id,
                       player_name=player.name, rating=player.rating,
                       price=buy_val, gem_bonus=gem_bonus)
        except Exception:
            logger.exception("record_buy failed (non-fatal)")

        session.commit()

        # Terminal success — keep the claim (TTL expires it) so the dup-click
        # window stays closed. Keyboard was already removed up top.
        await query.message.reply_text(
            f"✅ <b>PURCHASED!</b>\n\n"
            f"📛 {player.name} - {player.rating} OVR\n"
            f"💰 Paid: {buy_val:,} 🪙"
            f"{bonus_line(gem_bonus, offer=offer)}\n"
            f"💳 Balance: {user.total_coins:,} 🪙"
            f"{f' · {user.total_gems:,} 💎' if gem_bonus else ''}\n"
            f"📊 Roster: {user.roster_count}/{MAX_ROSTER}\n\n"
            f"<i>↩️ Made a mistake? /cmuundo within 60 seconds to reverse.</i>",
            parse_mode="HTML",
        )

    except Exception:
        # Don't release here: the error can be post-commit, and freeing the key
        # would let a stale button replay an already-applied buy. TTL clears it.
        session.rollback()
        logger.exception(f"BuyPl confirm error")
    finally:
        session.close()


async def buypl_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    tg_user = query.from_user
    parts = query.data.split("_")
    if len(parts) >= 2:
        try:
            owner_tg = int(parts[1])
            if tg_user.id != owner_tg:
                await query.answer("This isn't your purchase!", show_alert=True)
                return
        except ValueError:
            pass
    await query.answer("Cancelled")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
