"""Handler for /playerinfo [name] — shows player card + version selector."""

import io
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User, Player, UserRoster
from services.card_generator import generate_card
from services.card_text import format_player_card
from services.version_service import get_all_versions, user_owns_any_version

logger = logging.getLogger(__name__)


def _build_version_keyboard(versions, current_id, owner_tg, base_id):
    """Build the version selector keyboard. Highlights the currently-displayed version."""
    if len(versions) <= 1:
        return None  # No keyboard if only one version
    rows = [[]]
    for v in versions:
        is_current = v.id == current_id
        label = ("• " if is_current else "") + (v.version or "Base")
        if len(rows[-1]) >= 3:
            rows.append([])
        rows[-1].append(InlineKeyboardButton(
            label, callback_data=f"plv_{owner_tg}_{base_id}_{v.id}"
        ))
    return InlineKeyboardMarkup(rows)


async def _send_player_card(session, user, player, target, owner_tg):
    """Render and send the player card with version selector."""
    base_id = player.parent_player_id or player.id
    versions = get_all_versions(session, base_id)

    # Roster check across all versions
    user_owns = user_owns_any_version(session, user.id, player.id)
    owned_row = None
    if user_owns:
        version_ids = [v.id for v in versions]
        owned_row = (session.query(UserRoster)
                     .filter(UserRoster.user_id == user.id,
                             UserRoster.player_id.in_(version_ids)).first())
    acq_date = owned_row.acquired_date if owned_row else None

    text = format_player_card(player, acq_date)

    # Versions footer
    if len(versions) > 1:
        text += "\n\n🎴 <b>Versions:</b> " + " · ".join(
            ("<b>" + (v.version or "Base") + "</b>" if v.id == player.id else (v.version or "Base"))
            for v in versions
        )
        if user_owns and owned_row:
            owned_v = next((v for v in versions if v.id == owned_row.player_id), None)
            text += f"\n✅ <i>You own:</i> <b>{owned_v.version if owned_v else 'Base'}</b>"

    # Equipped traits (only if user owns this exact version)
    if owned_row and owned_row.player_id == player.id:
        from models import PlayerTrait, Trait
        traits = (session.query(PlayerTrait, Trait)
                  .join(Trait, PlayerTrait.trait_id == Trait.id)
                  .filter(PlayerTrait.roster_id == owned_row.id)
                  .all())
        if traits:
            text += "\n\n💎 <b>Traits:</b>"
            for pt, t in traits:
                badge = " 🌟" if pt.level == 5 else ""
                text += f"\n  {t.emoji} {t.name} Lv.{pt.level}{badge}"

    kb = _build_version_keyboard(versions, player.id, owner_tg, base_id)
    card_bytes = generate_card(player)

    if card_bytes:
        await target.reply_photo(
            photo=io.BytesIO(card_bytes), caption=text, parse_mode="HTML", reply_markup=kb,
        )
    else:
        await target.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def playerinfo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    # If invoked as /info with no args AND user is currently in a match,
    # show match info (striker/non-striker/bowler) instead of asking for a name.
    if not context.args:
        try:
            for k, v in context.bot_data.items():
                if k.startswith("ms_") and isinstance(v, dict):
                    if (v.get("bat_user_tg") == tg_user.id
                            or v.get("bowl_user_tg") == tg_user.id):
                        from handlers.match import info_handler
                        await info_handler(update, context)
                        return
        except Exception:
            pass
        await update.message.reply_text(
            "Usage: <code>/playerinfo &lt;player name&gt;</code>\n"
            "Example: <code>/playerinfo Virat Kohli</code>\n\n"
            "<i>Tip: while in a match, /info shows the live state.</i>",
            parse_mode="HTML",
        )
        return

    search_name = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        # Find the player; if multiple versions exist, use the paginator UI
        from services.version_paginator import (
            find_player_for_search, get_versions_ordered,
        )
        player = find_player_for_search(session, search_name)
        if not player:
            await update.message.reply_text(f"❌ Player not found: {search_name}")
            return

        base_id = player.parent_player_id or player.id
        versions = get_versions_ordered(session, base_id)

        # If only one version exists, use the simpler legacy view (with traits etc.)
        if len(versions) <= 1:
            await _send_player_card(session, user, player, update.message, tg_user.id)
            return

        # Multiple versions → paginated carousel
        from handlers.buy import _send_version_page
        await _send_version_page(
            session=session, user=user, versions=versions,
            current_idx=0, owner_tg=tg_user.id,
            send_to=update.message, context=context,
        )

    except Exception:
        logger.exception(f"PlayerInfo error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Version switcher — plv_<owner_tg>_<base_id>_<version_id>
# ════════════════════════════════════════════════════════════════════

async def player_version_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1]); base_id = int(parts[2]); ver_id = int(parts[3])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await q.answer("User not found", show_alert=True)
            return
        version = session.query(Player).get(ver_id)
        if not version:
            await q.answer("Version not found", show_alert=True)
            return
        await q.answer(f"→ {version.version or 'Base'}")
        # Delete old card, post new one — different image, can't be edited in one go
        try:
            await q.message.delete()
        except Exception:
            pass

        class _ReplyTarget:
            def __init__(self, bot, chat_id):
                self._bot = bot; self._chat_id = chat_id
            async def reply_photo(self, **kw):
                return await self._bot.send_photo(chat_id=self._chat_id, **kw)
            async def reply_text(self, *a, **kw):
                return await self._bot.send_message(chat_id=self._chat_id, *a, **kw)

        target = _ReplyTarget(context.bot, q.message.chat_id)
        await _send_player_card(session, user, version, target, tg.id)
    except Exception:
        logger.exception("player_version_callback error")
        try: await q.answer("⚠️ Error", show_alert=True)
        except Exception: pass
    finally:
        session.close()
