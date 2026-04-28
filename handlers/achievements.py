"""/achievements — view all achievements and their unlock status."""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services.achievement_service import (
    get_user_achievements_with_progress, CATALOG,
)
from services.button_timeout import schedule_button_timeout

logger = logging.getLogger(__name__)


def _render_for_category(items, category):
    """Render list of achievements for a single category."""
    lines = []
    cat_items = [a for a in items if a["category"] == category]
    for a in cat_items:
        if a["unlocked"]:
            lines.append(f"  {a['emoji']} <b>{a['name']}</b>  ✅")
            lines.append(f"     <i>{a['desc']}</i>")
        else:
            # Locked — grey emoji style
            lines.append(f"  ⬜ <b>{a['name']}</b>")
            lines.append(f"     <i>{a['desc']}</i>")
    return "\n".join(lines)


def _render_full_text(items, user, owner_tg, mode="all"):
    """Build the achievement display.
    mode: 'all' / 'unlocked' / 'locked'
    """
    total = len(items)
    unlocked_n = sum(1 for a in items if a["unlocked"])
    pct = int(unlocked_n * 100 / total) if total else 0
    bar_filled = int(pct / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    header = (
        f"🏆 <b>{user.first_name or user.username}'s Achievements</b>\n"
        f"<b>{unlocked_n}/{total}</b> unlocked  ({pct}%)\n"
        f"{bar}\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )

    # Filter by mode
    if mode == "unlocked":
        items = [a for a in items if a["unlocked"]]
    elif mode == "locked":
        items = [a for a in items if not a["unlocked"]]

    if not items:
        body = "\n<i>No achievements in this view.</i>"
    else:
        # Group by category
        cats = {}
        for a in items:
            cats.setdefault(a["category"], []).append(a)
        # Display each category
        category_order = ["Match", "Streak", "Batting", "Bowling",
                          "Collection", "Traits", "Economy", "Engagement", "Social"]
        body_parts = []
        for c in category_order:
            if c in cats:
                body_parts.append(f"\n<b>━ {c} ━</b>")
                for a in cats[c]:
                    if a["unlocked"]:
                        body_parts.append(f"  {a['emoji']} <b>{a['name']}</b>  ✅")
                    else:
                        body_parts.append(f"  ⬜ <b>{a['name']}</b>")
                    body_parts.append(f"     <i>{a['desc']}</i>")
        body = "\n".join(body_parts)

    # Tab buttons
    btns = [[
        InlineKeyboardButton(("• " if mode == "all" else "") + "All",
                             callback_data=f"ach_tab_{owner_tg}_all"),
        InlineKeyboardButton(("• " if mode == "unlocked" else "") + "Unlocked",
                             callback_data=f"ach_tab_{owner_tg}_unlocked"),
        InlineKeyboardButton(("• " if mode == "locked" else "") + "Locked",
                             callback_data=f"ach_tab_{owner_tg}_locked"),
    ], [InlineKeyboardButton("❌ Close", callback_data=f"ach_close_{owner_tg}")]]

    return header + "\n" + body, InlineKeyboardMarkup(btns)


# ════════════════════════════════════════════════════════════════════
# /achievements
# ════════════════════════════════════════════════════════════════════

async def achievements_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        items = get_user_achievements_with_progress(session, user.id)
        text, kb = _render_full_text(items, user, tg.id, mode="all")

        # Telegram message limit 4096 — split if needed
        if len(text) > 4000:
            text = text[:3990] + "\n\n<i>...truncated</i>"

        sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        try:
            schedule_button_timeout(context, sent.chat_id, sent.message_id, delay_seconds=180)
        except Exception:
            pass
    except Exception:
        logger.exception("achievements_handler error")
        await update.message.reply_text("⚠️ Error loading achievements.")
    finally:
        session.close()


# Tab switcher
async def achievements_tab_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        parts = q.data.split("_")
        owner_tg = int(parts[2])
        mode = parts[3]
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return

    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return

    if mode not in ("all", "unlocked", "locked"):
        await q.answer("Unknown")
        return

    await q.answer()
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg.id).first()
        if not user:
            return
        items = get_user_achievements_with_progress(session, user.id)
        text, kb = _render_full_text(items, user, tg.id, mode=mode)
        if len(text) > 4000:
            text = text[:3990] + "\n\n<i>...truncated</i>"
        try:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
    except Exception:
        logger.exception("achievements_tab_callback error")
    finally:
        session.close()


async def achievements_close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tg = q.from_user
    try:
        owner_tg = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        await q.answer("Invalid")
        return
    if tg.id != owner_tg:
        await q.answer("Not yours!", show_alert=True)
        return
    await q.answer("Closed")
    try:
        await q.edit_message_text("🏆 <i>Achievements closed.</i>", parse_mode="HTML")
    except Exception:
        pass
