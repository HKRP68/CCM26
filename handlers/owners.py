"""/owners <player> — who owns this card in this group, and how rare is it.

Answers the question every trade starts with: "who has him?". In a group it
names the members holding the card (with the edition and how long they've had
it) so you know exactly who to make an offer to; in a DM it reports the global
picture plus a per-group tally for the groups you're in.

Group membership is learned from activity (see services/chat_tracker.py), so
the group figure is always framed as "of N known members" — members who have
never spoken since the bot joined can't be counted.
"""

import logging
from datetime import datetime
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import Player, User
from services.button_timeout import schedule_button_timeout
from services.flags import get_flag
from services import ownership_service as own

logger = logging.getLogger(__name__)

PAGE_SIZE = 10
MAX_GROUPS_LISTED = 8
GROUP_CHAT_TYPES = ("group", "supergroup")


def _display_name(user: User) -> str:
    """A tappable name: @handle when there is one, else a mention link."""
    if user.username:
        return f"@{escape(user.username)}"
    name = escape((user.first_name or user.team_name or "Manager").strip() or "Manager")
    return f'<a href="tg://user?id={user.telegram_id}">{name}</a>'


def _held_for(acquired_date) -> str:
    if not acquired_date:
        return ""
    days = (datetime.utcnow() - acquired_date).days
    if days < 1:
        return "today"
    if days < 30:
        return f"{days}d"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _version_label(player: Player) -> str:
    label = (player.version or "").strip()
    return label if label else "Base"


def _versions_line(versions, counts) -> str:
    """"🎴 Editions: Base 380 · Gold 32" — only worth printing for variants."""
    if len(versions) <= 1:
        return ""
    parts = [f"{escape(_version_label(v))} {counts.get(v.id, 0):,}" for v in versions]
    return "🎴 <b>Editions:</b> " + " · ".join(parts)


def _header(player: Player) -> str:
    flag = get_flag(player.country)
    return (f"👥 <b>Who owns {escape(player.name)}</b> {flag}\n"
            f"⭐ {player.rating} OVR · {escape(player.category or '')}")


def _global_block(session, player_ids) -> str:
    total_owners = own.global_owner_count(session, player_ids)
    managers = own.total_managers(session)
    return (f"🌍 <b>Globally:</b> {total_owners:,} owner"
            f"{'' if total_owners == 1 else 's'} "
            f"— {own.scarcity_line(total_owners, managers)}")


def _render_group_page(session, player, versions, chat_id, chat_title, page):
    """Build the group view for one page. Returns (text, total_pages, total)."""
    player_ids = [v.id for v in versions]
    rows = own.group_owner_rows(session, chat_id, player_ids)
    total = len(rows)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    window = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    title = escape(chat_title or "this group")
    lines = [_header(player), ""]

    if total == 0:
        lines.append(f"🏠 <b>{title}</b>\n"
                     f"Nobody here owns this card yet — "
                     f"you'd be the first.")
    else:
        known = own.group_member_count(session, chat_id)
        share = f" ({round(total * 100 / known)}%)" if known else ""
        lines.append(f"🏠 <b>{title}</b>\n"
                     f"Owned by <b>{total}</b> of {known} known member"
                     f"{'' if known == 1 else 's'}{share}")
        lines.append("")
        start = (page - 1) * PAGE_SIZE
        for i, (user, entry, card) in enumerate(window, start=start + 1):
            held = _held_for(entry.acquired_date)
            suffix = f" · {held}" if held else ""
            lines.append(f"{i}. {_display_name(user)} — "
                         f"{escape(_version_label(card))} ⭐{card.rating}{suffix}")

    lines.append("")
    lines.append(_global_block(session, player_ids))
    versions_line = _versions_line(versions,
                                   own.owners_per_version(session, player_ids))
    if versions_line:
        lines.append(versions_line)
    if total:
        lines.append("")
        lines.append("🤝 <i>Offer a swap with</i> <code>/trade @user</code>")

    if total_pages > 1:
        lines.insert(1, f"<i>page {page}/{total_pages}</i>")
    return "\n".join(lines), total_pages, total


def _render_private(session, user, player, versions) -> str:
    """The DM view: global rarity first, then a tally per group you're in."""
    player_ids = [v.id for v in versions]
    lines = [_header(player), "", _global_block(session, player_ids)]

    versions_line = _versions_line(versions,
                                   own.owners_per_version(session, player_ids))
    if versions_line:
        lines.append(versions_line)

    chat_ids = own.group_ids_for_user(session, user.id)
    counts = own.owner_counts_by_group(session, player_ids, chat_ids)
    titles = own.group_titles(session, chat_ids)
    ranked = sorted(chat_ids, key=lambda c: counts.get(c, 0), reverse=True)

    lines.append("")
    if not ranked:
        lines.append("🏠 <i>I haven't seen you in any group yet — run this "
                     "command inside your group to see who there owns him.</i>")
        return "\n".join(lines)

    lines.append("🏠 <b>Your groups</b>")
    for chat_id in ranked[:MAX_GROUPS_LISTED]:
        title = escape(titles.get(chat_id) or "Unnamed group")
        count = counts.get(chat_id, 0)
        lines.append(f"• {title} — <b>{count}</b> owner{'' if count == 1 else 's'}")
    if len(ranked) > MAX_GROUPS_LISTED:
        lines.append(f"<i>…and {len(ranked) - MAX_GROUPS_LISTED} more</i>")

    lines.append("")
    lines.append("💡 <i>Run</i> <code>/owners " + escape(player.name) +
                 "</code> <i>inside a group to see exactly who owns him there.</i>")
    return "\n".join(lines)


def _pagination_kb(owner_tg, base_id, page, total_pages):
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(
            "⬅️ Prev", callback_data=f"own_{owner_tg}_{base_id}_{page - 1}"))
    nav.append(InlineKeyboardButton(f"Page {page}/{total_pages}",
                                    callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(
            "Next ➡️", callback_data=f"own_{owner_tg}_{base_id}_{page + 1}"))
    return InlineKeyboardMarkup([nav])


def _resolve_player(session, search_name):
    """(player, error_text). Mirrors /playerinfo's disambiguation."""
    from services.version_paginator import (
        find_players_for_search, format_multiple_players_message)
    matches = find_players_for_search(session, search_name)
    if not matches:
        return None, f"❌ Player not found: {escape(search_name)}"
    if len(matches) > 1:
        return None, format_multiple_players_message("owners", search_name, matches)
    return matches[0], None


async def owners_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    chat = update.effective_chat

    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/owners &lt;player name&gt;</code>\n"
            "Example: <code>/owners Rohit Sharma</code>\n\n"
            "<i>In a group it names the members who own him — "
            "in a DM it shows how rare he is everywhere.</i>",
            parse_mode="HTML")
        return

    search_name = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        player, error = _resolve_player(session, search_name)
        if error:
            await update.message.reply_text(error, parse_mode="HTML")
            return

        from services.version_paginator import get_versions_ordered
        base_id = player.parent_player_id or player.id
        versions = get_versions_ordered(session, base_id) or [player]

        if chat and (chat.type or "") in GROUP_CHAT_TYPES:
            text, total_pages, total = _render_group_page(
                session, player, versions, chat.id, chat.title, 1)
            kb = (_pagination_kb(tg_user.id, base_id, 1, total_pages)
                  if total_pages > 1 else None)
            sent = await update.message.reply_text(
                text, parse_mode="HTML", reply_markup=kb,
                disable_web_page_preview=True)
            if kb is not None:
                schedule_button_timeout(
                    context, sent.chat_id, sent.message_id, delay_seconds=180,
                    custom_text=text + "\n\n⏱ <i>Buttons expired. Run /owners "
                                       "again to continue.</i>",
                    timeout_key=f"own_{tg_user.id}_{sent.message_id}")
            return

        await update.message.reply_text(
            _render_private(session, user, player, versions),
            parse_mode="HTML", disable_web_page_preview=True)

    except Exception:
        logger.exception("Owners error for %s", tg_user.id)
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def owners_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """own_<owner_tg>_<base_id>_<page>"""
    q = update.callback_query
    tg = q.from_user
    try:
        _, owner_tg, base_id, page = q.data.split("_")
        owner_tg, base_id, page = int(owner_tg), int(base_id), int(page)
    except (AttributeError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your list!", show_alert=True)
        return
    await q.answer()

    session = get_session()
    try:
        player = session.query(Player).get(base_id)
        if not player:
            await q.answer("Player not found", show_alert=True)
            return
        from services.version_paginator import get_versions_ordered
        versions = get_versions_ordered(session, base_id) or [player]
        chat = q.message.chat
        text, total_pages, _total = _render_group_page(
            session, player, versions, chat.id, chat.title, page)
        page = max(1, min(page, total_pages))
        kb = (_pagination_kb(owner_tg, base_id, page, total_pages)
              if total_pages > 1 else None)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb,
                                  disable_web_page_preview=True)
    except Exception:
        logger.exception("owners_page_callback error")
        try:
            await q.answer("⚠️ Error", show_alert=True)
        except Exception:
            pass
    finally:
        session.close()
