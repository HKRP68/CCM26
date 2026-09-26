"""Bot-admin review of player-submitted CMU News stories.

A player submits a story from the Mini App; the website DMs every bot admin a
review card (services/news_announce.send_review_to_admins) with ✅ Approve and
❌ Reject. This module answers those buttons and ``/newsqueue``, which re-sends
cards for anything still pending — the recovery path when a DM was missed.

The service refuses a decision on anything no longer pending, so two admins
pressing at once (or one here and one on the website) publish once.
"""

import html
import io
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import NewsArticle
from services import news_service
from services import news_announce
from services.admin_ids import is_admin

logger = logging.getLogger(__name__)

CB = "news"
_NOT_ADMIN = "These buttons are for bot admins."


def _review_keyboard(article_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"{CB}:ap:{article_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"{CB}:rj:{article_id}"),
    ]])


def _reason_keyboard(article_id):
    labels = {"spam": "🚫 Spam", "rude": "⚠️ Inappropriate",
              "dup": "♻️ Duplicate", "low": "✏️ Low quality"}
    keys = list(news_service.REJECT_REASONS)
    rows = [[InlineKeyboardButton(labels[k], callback_data=f"{CB}:rr:{article_id}:{k}")
             for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
    rows.append([InlineKeyboardButton("↩️ Back", callback_data=f"{CB}:bk:{article_id}")])
    return InlineKeyboardMarkup(rows)


def _name_of(tg_user):
    if tg_user is None:
        return "admin"
    return (tg_user.username and f"@{tg_user.username}") or \
        (tg_user.first_name or str(tg_user.id))


async def _close_card(query, footer):
    """Replace the buttons with who decided, on whichever card was pressed."""
    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(
                caption=f"📰 News #{footer}", parse_mode="HTML", reply_markup=None)
        else:
            await query.edit_message_text(
                f"📰 News #{footer}", parse_mode="HTML", reply_markup=None)
    except Exception:
        pass


async def news_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":", 3)
    if len(parts) < 3:
        await query.answer()
        return
    action = parts[1]
    try:
        article_id = int(parts[2])
    except (TypeError, ValueError):
        await query.answer()
        return
    value = parts[3] if len(parts) > 3 else None
    if not is_admin(query.from_user.id):
        await query.answer(_NOT_ADMIN, show_alert=True)
        return

    db = get_session()
    try:
        article = db.get(NewsArticle, article_id)
        if article is None:
            await query.answer("That story is gone.", show_alert=True)
            return
        reviewer = _name_of(query.from_user)
        if action == "ap":
            paid = news_service.approve(db, article, reviewer=reviewer)
            if paid is None:
                await query.answer(f"Already decided ({article.status}).", show_alert=True)
                await _close_card(query, f"{article.id} — already {article.status}.")
                return
            db.commit()
            await query.answer("Published.")
            news_announce.in_background(news_announce.notify_author_by_id, article.id, True, paid)
            await _close_card(query, f"{article.id} ✅ approved by {html.escape(reviewer)}"
                              + (f" · {paid} coins paid" if paid else ""))
        elif action == "rj":
            await query.answer()
            try:
                await query.edit_message_reply_markup(reply_markup=_reason_keyboard(article.id))
            except Exception:
                pass
        elif action == "bk":
            await query.answer()
            try:
                await query.edit_message_reply_markup(reply_markup=_review_keyboard(article.id))
            except Exception:
                pass
        elif action == "rr":
            note = news_service.reason_text(value or "")
            if not news_service.reject(db, article, reviewer=reviewer, note=note):
                await query.answer(f"Already decided ({article.status}).", show_alert=True)
                await _close_card(query, f"{article.id} — already {article.status}.")
                return
            db.commit()
            await query.answer("Rejected.")
            news_announce.in_background(news_announce.notify_author_by_id, article.id, False)
            await _close_card(query, f"{article.id} ❌ rejected by {html.escape(reviewer)}")
        else:
            await query.answer()
    except Exception:
        db.rollback()
        logger.exception("news callback failed (%s)", action)
        try:
            await query.answer("Something went wrong.", show_alert=True)
        except Exception:
            pass
    finally:
        db.close()



async def newsqueue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message is None:
        return
    if not is_admin(update.effective_user.id):
        await message.reply_text("That command is bot-admin only.")
        return
    db = get_session()
    try:
        rows = (db.query(NewsArticle)
                .filter(NewsArticle.status == news_service.STATUS_PENDING)
                .order_by(NewsArticle.created_at.asc()).limit(10).all())
        if not rows:
            await message.reply_text("✅ Nothing waiting — the news queue is empty.")
            return
        total = news_service.pending_count(db)
        await message.reply_text(
            f"📰 <b>{total} stor{'ies' if total != 1 else 'y'} waiting.</b>"
            + ("\nShowing the oldest 10." if total > len(rows) else ""),
            parse_mode="HTML")
        for article in rows:
            caption = news_announce.review_caption(article)
            photo, _ptype = news_service.image_bytes(db, article)
            try:
                if photo:
                    await context.bot.send_photo(
                        chat_id=message.chat_id, photo=io.BytesIO(photo),
                        caption=caption[:1024], parse_mode="HTML",
                        reply_markup=_review_keyboard(article.id))
                else:
                    await message.reply_text(caption[:4096], parse_mode="HTML",
                                             reply_markup=_review_keyboard(article.id))
            except Exception:
                logger.exception("newsqueue could not show #%s", article.id)
    except Exception:
        logger.exception("newsqueue failed")
        await message.reply_text("⚠️ Something went wrong.")
    finally:
        db.close()
