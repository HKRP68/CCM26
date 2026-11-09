"""Preview/confirm roster swaps and browse chemistry suggestions in Telegram."""

import logging
import secrets
from datetime import datetime
from time import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from database import get_session
from models import User
from services import chemistry_planner as planner
from services.activity_service import log_activity
from services.config_service import get_config
from services.roster_lock import match_lock_message
from services.roster_service import get_roster_ordered
from services.roster_view import build_display_order

logger = logging.getLogger(__name__)
_PENDING_KEY = "chemistry_swap_preview"


def recommendation_keyboard(user_id, suggestions):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"Preview #{index} · +{item['delta']} CHEM",
            callback_data=f"chem_pick_{user_id}_{item['outgoing']['roster_id']}_{item['incoming']['roster_id']}")
    ] for index, item in enumerate(suggestions, 1)]) if suggestions else None


def _prepare_preview(context, user, roster, first_id, second_id, config):
    preview = planner.preview_swap(roster, first_id, second_id, config)
    key = secrets.token_hex(8)
    context.user_data[_PENDING_KEY] = {
        "key": key, "owner": user.telegram_id, "expires_at": time() + 300,
        "first": first_id, "second": second_id,
        "token": preview["preview_token"],
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm swap", callback_data=f"chem_apply_{key}"),
        InlineKeyboardButton("Cancel", callback_data=f"chem_cancel_{key}"),
    ]])
    return planner.render_swap_preview(preview), keyboard


async def swap_preview_command(update, context):
    if len(context.args or ()) != 2:
        await update.message.reply_text(
            "Usage: /swap <pos1> <pos2>\nPositions match /pxi. "
            "You will see a chemistry preview before confirming.")
        return
    try:
        first, second = (int(value) for value in context.args)
    except ValueError:
        await update.message.reply_text("❌ Numbers only.")
        return
    db = get_session()
    try:
        user = db.query(User).filter(User.telegram_id == update.effective_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        roster = get_roster_ordered(db, user.id)
        display = build_display_order(roster)
        if min(first, second) < 1 or max(first, second) > len(display) or first == second:
            await update.message.reply_text(f"❌ Positions must be 1-{len(display)}, and different.")
            return
        text, keyboard = _prepare_preview(
            context, user, roster, display[first - 1][0].id,
            display[second - 1][0].id, get_config(db, fresh=True))
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        logger.exception("swap preview failed")
        await update.message.reply_text("Couldn't preview that swap. Try again shortly.")
    finally:
        db.close()


async def chemistry_swap_callback(update, context):
    q = update.callback_query
    parts = q.data.split("_")
    action = parts[1]
    pending = context.user_data.get(_PENDING_KEY)
    if action in {"apply", "cancel"}:
        if (not pending or pending["key"] != parts[2]
                or pending["owner"] != q.from_user.id or pending["expires_at"] < time()):
            await q.answer("This preview expired or belongs to another player. Open a new one.",
                           show_alert=True)
            return
        if action == "cancel":
            context.user_data.pop(_PENDING_KEY, None)
            await q.answer()
            await q.edit_message_text("Swap cancelled. Your XI is unchanged.")
            return
    db = get_session()
    try:
        user = db.query(User).filter(User.telegram_id == q.from_user.id).first()
        if not user or (action in {"pick", "recs"} and user.id != int(parts[2])):
            await q.answer("These are another player's cards.", show_alert=True)
            return
        if action == "apply":
            # Serialize confirmations, then read the current roster and rules.
            db.refresh(user, with_for_update=True)
            locked = match_lock_message(db, user.id, "swap players in your XI")
            if locked:
                await q.answer("Finish your current match before confirming this swap.", show_alert=True)
                return
        roster = get_roster_ordered(db, user.id)
        config = get_config(db, fresh=True)
        if action == "recs":
            suggestions = planner.recommend_bench_swaps(roster, config=config)
            await q.answer()
            await q.message.reply_text(
                planner.render_recommendations(suggestions), parse_mode="HTML",
                reply_markup=recommendation_keyboard(user.id, suggestions))
        elif action == "pick":
            text, keyboard = _prepare_preview(context, user, roster,
                                               int(parts[3]), int(parts[4]), config)
            await q.answer()
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        elif action == "apply":
            preview = planner.preview_swap(roster, pending["first"], pending["second"], config)
            if pending["token"] != preview["preview_token"]:
                context.user_data.pop(_PENDING_KEY, None)
                await q.answer("Your roster or chemistry rules changed. Preview this swap again.",
                               show_alert=True)
                return
            planner.apply_swap(roster, pending["first"], pending["second"])
            if preview["membership_changed"]:
                user.batting_order_set_at = datetime.utcnow()
            outgoing, incoming = preview["outgoing"], preview["incoming"]
            log_activity(db, user.id, "swap",
                         f"Swapped {outgoing['name']} ↔ {incoming['name']}")
            db.commit()
            context.user_data.pop(_PENDING_KEY, None)
            await q.answer("Swap saved")
            result = "✅ Swap saved."
            if preview["after"]:
                result += (f" Chemistry: {preview['before']['total']} → "
                           f"{preview['after']['total']}/100 ({preview['delta']:+d}).")
            from services.batting_order_service import has_custom_order
            if has_custom_order(user):
                result += "\nCheck /sbo for your batting order."
            await q.edit_message_text(result + "\n/pxi to view your updated XI.")
    except (ValueError, IndexError):
        db.rollback()
        await q.answer("Those cards are no longer available. Open a new preview.", show_alert=True)
    except Exception:
        db.rollback()
        logger.exception("chemistry swap callback failed")
        await q.answer("Couldn't complete that action. Try again shortly.", show_alert=True)
    finally:
        db.close()
