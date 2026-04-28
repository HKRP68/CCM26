"""Handlers for /searchpl and /searchovr — paginated, 15 per page."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import Player, User
from config import get_buy_value
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)

PAGE_SIZE = 15
MAX_PAGES = 30  # cap so an overly broad query doesn't paginate forever


def _format_page(players, total, page, header):
    """Format one page of results (15 players)."""
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    total_pages = min(total_pages, MAX_PAGES)
    lines = [f"{header} <i>(page {page}/{total_pages}, {total} total)</i>\n"]
    for p in players:
        buy = get_buy_value(p.rating)
        lines.append(
            f"• <b>{p.name}</b> — {p.rating} OVR | {p.category} | {p.country}\n"
            f"  💰 {buy:,} 🪙"
        )
    return "\n".join(lines), total_pages


def _build_pagination_kb(prefix, owner_tg, page, total_pages):
    """Make Previous/Next/Cancel button row.
    callback format: <prefix>_<owner_tg>_<page>
    """
    btns = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev",
                                        callback_data=f"{prefix}_{owner_tg}_{page - 1}"))
    nav.append(InlineKeyboardButton(f"Page {page}/{total_pages}",
                                    callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Next ➡️",
                                        callback_data=f"{prefix}_{owner_tg}_{page + 1}"))
    btns.append(nav)
    btns.append([InlineKeyboardButton("❌ Close",
                                      callback_data=f"searchcancel_{owner_tg}")])
    return InlineKeyboardMarkup(btns)


# ═══════════════════════════════════════════════════════════════════════
# /searchpl  — search by name
# ═══════════════════════════════════════════════════════════════════════

async def searchpl_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Usage: /searchpl <player name>\nExample: /searchpl Virat")
        return

    search = " ".join(context.args).strip()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        total = (session.query(Player)
                 .filter(Player.name.ilike(f"%{search}%"), Player.is_active == True)
                 .count())
        if total == 0:
            await update.message.reply_text(f"❌ No players found matching '{search}'")
            return

        # Save the query in bot_data for pagination callbacks
        context.bot_data[f"spl_q_{tg_user.id}"] = search

        players = (session.query(Player)
                   .filter(Player.name.ilike(f"%{search}%"), Player.is_active == True)
                   .order_by(Player.rating.desc(), Player.name)
                   .limit(PAGE_SIZE).all())
        text, total_pages = _format_page(players, total, 1,
                                         f"🔍 <b>Search: '{search}'</b>")
        kb = _build_pagination_kb("spl", tg_user.id, 1, total_pages) if total_pages > 1 else \
             InlineKeyboardMarkup([[InlineKeyboardButton(
                 "❌ Close", callback_data=f"searchcancel_{tg_user.id}")]])

        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        schedule_button_timeout(
            context, sent.chat_id, sent.message_id, delay_seconds=120,
            custom_text=text + "\n\n⏱ <i>Buttons expired. Run /searchpl again to continue.</i>",
            timeout_key=f"spl_{tg_user.id}_{sent.message_id}",
        )
    except Exception:
        logger.exception(f"SearchPl error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def searchpl_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """spl_<owner_tg>_<page>"""
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1]); page = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your search!", show_alert=True)
        return
    await q.answer()

    search = context.bot_data.get(f"spl_q_{tg.id}")
    if not search:
        await q.edit_message_text("⏱ Search expired. Run /searchpl again.")
        return

    session = get_session()
    try:
        total = (session.query(Player)
                 .filter(Player.name.ilike(f"%{search}%"), Player.is_active == True)
                 .count())
        if total == 0:
            await q.edit_message_text(f"❌ No players found.")
            return
        offset = (page - 1) * PAGE_SIZE
        players = (session.query(Player)
                   .filter(Player.name.ilike(f"%{search}%"), Player.is_active == True)
                   .order_by(Player.rating.desc(), Player.name)
                   .offset(offset).limit(PAGE_SIZE).all())
        text, total_pages = _format_page(players, total, page,
                                         f"🔍 <b>Search: '{search}'</b>")
        kb = _build_pagination_kb("spl", owner_tg, page, total_pages)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# /searchovr  — search by rating
# ═══════════════════════════════════════════════════════════════════════

CAT_MAP = {
    "batsman": "Batsman", "bat": "Batsman",
    "bowler": "Bowler", "bowl": "Bowler",
    "all-rounder": "All-rounder", "alr": "All-rounder", "allrounder": "All-rounder",
    "wicket keeper": "Wicket Keeper", "wk": "Wicket Keeper",
    "wicketkeeper": "Wicket Keeper",
}


def _parse_searchovr_args(args):
    """Returns (rating, category, country)."""
    try:
        rating = int(args[0])
    except (IndexError, ValueError):
        return None, None, None
    category = None
    country = None
    rest = args[1:]
    if rest:
        first = rest[0].lower()
        if first in CAT_MAP:
            category = CAT_MAP[first]; rest = rest[1:]
        elif len(rest) >= 2:
            two = (rest[0] + " " + rest[1]).lower()
            if two in CAT_MAP:
                category = CAT_MAP[two]; rest = rest[2:]
    if rest:
        country = " ".join(rest).strip()
    return rating, category, country


def _build_overquery(session, rating, category, country):
    q = session.query(Player).filter(Player.rating == rating, Player.is_active == True)
    if category:
        q = q.filter(Player.category == category)
    if country:
        q = q.filter(Player.country.ilike(f"%{country}%"))
    return q


def _format_ovr_header(rating, category, country):
    h = f"🔍 <b>{rating} OVR"
    if category: h += f" — {category}"
    if country: h += f" — {country}"
    h += "</b>"
    return h


async def searchovr_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "Usage:\n/searchovr 85\n/searchovr 85 Batsman\n"
            "/searchovr 85 Bowler India\n/searchovr 90 ALR\n"
            "/searchovr 79 WK England")
        return

    rating, category, country = _parse_searchovr_args(context.args)
    if rating is None:
        await update.message.reply_text("❌ First argument must be a rating number")
        return

    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        q = _build_overquery(session, rating, category, country)
        total = q.count()
        if total == 0:
            desc = _format_ovr_header(rating, category, country)
            await update.message.reply_text(f"❌ No players matching {desc}", parse_mode="HTML")
            return

        # Save params for pagination
        context.bot_data[f"sovr_q_{tg_user.id}"] = (rating, category, country)
        players = q.order_by(Player.name).limit(PAGE_SIZE).all()
        text, total_pages = _format_page(players, total, 1,
                                         _format_ovr_header(rating, category, country))
        kb = _build_pagination_kb("sovr", tg_user.id, 1, total_pages) if total_pages > 1 else \
             InlineKeyboardMarkup([[InlineKeyboardButton(
                 "❌ Close", callback_data=f"searchcancel_{tg_user.id}")]])
        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        schedule_button_timeout(
            context, sent.chat_id, sent.message_id, delay_seconds=120,
            custom_text=text + "\n\n⏱ <i>Buttons expired. Run /searchovr again to continue.</i>",
            timeout_key=f"sovr_{tg_user.id}_{sent.message_id}",
        )
    except Exception:
        logger.exception(f"SearchOVR error for {tg_user.id}")
        await update.message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


async def searchovr_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[1]); page = int(parts[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not your search!", show_alert=True)
        return
    await q.answer()

    saved = context.bot_data.get(f"sovr_q_{tg.id}")
    if not saved:
        await q.edit_message_text("⏱ Search expired. Run /searchovr again.")
        return
    rating, category, country = saved

    session = get_session()
    try:
        qq = _build_overquery(session, rating, category, country)
        total = qq.count()
        if total == 0:
            await q.edit_message_text("❌ No matching players.")
            return
        offset = (page - 1) * PAGE_SIZE
        players = qq.order_by(Player.name).offset(offset).limit(PAGE_SIZE).all()
        text, total_pages = _format_page(players, total, page,
                                         _format_ovr_header(rating, category, country))
        kb = _build_pagination_kb("sovr", owner_tg, page, total_pages)
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════
# Shared cancel + noop callbacks
# ═══════════════════════════════════════════════════════════════════════

async def search_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer("Closed")
    try:
        await q.edit_message_text("🔍 <i>Search closed.</i>", parse_mode="HTML")
    except Exception:
        pass


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """For the page-indicator button."""
    await update.callback_query.answer()
