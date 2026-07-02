"""/notifications — Let users turn the bot's push notifications on or off.

Controls three notification channels for the calling user:
  • Cooldown-ready DM nudges (daily / spin / claim / free pack)
  • Scheduled FOMO push messages
  • Mini App activity echoed into groups for this user's own actions

Usage:
  /notifications        → show current state + toggle buttons
  /notifications off     → disable
  /notifications on      → enable

Opt-out model: a missing/NULL preference is treated as ENABLED, so existing
users are unaffected until they explicitly turn notifications off.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import User

logger = logging.getLogger(__name__)


def _is_enabled(user) -> bool:
    # NULL / unset counts as enabled (opt-out model).
    return getattr(user, "notifications_enabled", True) is not False


def _status_text(enabled: bool) -> str:
    if enabled:
        return (
            "🔔 <b>Notifications are ON</b>\n\n"
            "You'll receive reminders when your rewards are ready "
            "(daily, spin, claim, free pack) plus occasional updates.\n\n"
            "Tap the button below or send <code>/notifications off</code> to "
            "stop these messages."
        )
    return (
        "🔕 <b>Notifications are OFF</b>\n\n"
        "You won't get reminder or update messages from the bot, and your "
        "Mini App actions won't be announced in groups.\n\n"
        "Tap the button below or send <code>/notifications on</code> to turn "
        "them back on."
    )


def _keyboard(enabled: bool) -> InlineKeyboardMarkup:
    if enabled:
        btn = InlineKeyboardButton("🔕 Turn OFF notifications", callback_data="notif_toggle:off")
    else:
        btn = InlineKeyboardButton("🔔 Turn ON notifications", callback_data="notif_toggle:on")
    return InlineKeyboardMarkup([[btn]])


def _set_preference(telegram_id: int, enabled: bool):
    """Persist the preference. Returns the resulting bool, or None if no user."""
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == telegram_id).first()
        if not user:
            return None
        user.notifications_enabled = enabled
        session.commit()
        return enabled
    except Exception:
        session.rollback()
        logger.exception("failed to set notifications preference for %s", telegram_id)
        return None
    finally:
        session.close()


async def notifications_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    msg = update.message
    if not tg_user or not msg:
        return

    arg = (context.args[0].lower() if context.args else "").strip()

    # Read current state.
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        current = _is_enabled(user) if user else True
    finally:
        session.close()

    if arg in ("off", "stop", "mute", "disable", "0", "no"):
        result = _set_preference(tg_user.id, False)
        if result is None:
            await msg.reply_text("Use /start first to set up your account.")
            return
        await msg.reply_text(_status_text(False), parse_mode="HTML",
                             reply_markup=_keyboard(False))
        return

    if arg in ("on", "start", "unmute", "enable", "1", "yes"):
        result = _set_preference(tg_user.id, True)
        if result is None:
            await msg.reply_text("Use /start first to set up your account.")
            return
        await msg.reply_text(_status_text(True), parse_mode="HTML",
                             reply_markup=_keyboard(True))
        return

    # No / unknown argument → show current state with a toggle button.
    await msg.reply_text(_status_text(current), parse_mode="HTML",
                         reply_markup=_keyboard(current))


async def notifications_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    want_on = data.endswith(":on")
    result = _set_preference(update.effective_user.id, want_on)
    if result is None:
        await query.answer("Use /start first.", show_alert=True)
        return
    await query.answer("Notifications turned ON ✅" if want_on else "Notifications turned OFF 🔕")
    try:
        await query.edit_message_text(
            _status_text(want_on), parse_mode="HTML",
            reply_markup=_keyboard(want_on))
    except Exception:
        # Message may be unchanged or too old to edit — ignore.
        pass
