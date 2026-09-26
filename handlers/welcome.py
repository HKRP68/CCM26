"""Welcome new members in groups + /ewm /dwm toggles.

When a new member joins a group where the bot is present (and welcome is
enabled for that chat), the bot posts a short intro and a deep-link button
that opens the bot in DM and runs /debut.

When the bot itself is added to a group, it posts an intro too.

Admins toggle the welcome with /ewm (enable) and /dwm (disable), per group.
"""

import asyncio
import logging
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import get_session
from models import BotChat, User

logger = logging.getLogger(__name__)


def _bot_username():
    return (os.getenv("BOT_USERNAME") or "").lstrip("@").strip()


def _welcome_enabled(session, chat_id):
    row = session.query(BotChat).filter(BotChat.chat_id == chat_id).first()
    if row is None:
        return True  # default on
    return bool(row.welcome_enabled)


def _set_welcome(session, chat_id, enabled, chat=None):
    row = session.query(BotChat).filter(BotChat.chat_id == chat_id).first()
    if row is None:
        row = BotChat(
            chat_id=chat_id,
            chat_type=(chat.type if chat else "group"),
            title=(chat.title if chat else None),
            welcome_enabled=enabled,
        )
        session.add(row)
    else:
        row.welcome_enabled = enabled
    session.commit()


async def _is_group_admin(update, context):
    """True if the command issuer is an admin/owner of the group."""
    try:
        chat = update.effective_chat
        user = update.effective_user
        if not chat or not user:
            return False
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _default_intro_text():
    return (
        "🏏 <b>Welcome, @User!</b>\n\n"
        "This is <b>CricMaster Ultra</b> — collect real cricketers, build your "
        "dream XI, play matches, climb the season leaderboard, and join a club.\n\n"
        "👉 To get started, tap <b>/debut</b> to create your account and claim "
        "your starting squad.\n\n"
        "🎁 Even better — start the bot in DM for a welcome surprise:"
    )


def _get_welcome_template(session):
    try:
        from models import GameConfig
        cfg = session.query(GameConfig).first()
        if cfg and cfg.welcome_message and cfg.welcome_message.strip():
            return cfg.welcome_message
    except Exception:
        logger.exception("welcome template read failed")
    return _default_intro_text()


def _mention_html(member):
    """An HTML mention that pings the user."""
    name = member.first_name or member.username or "player"
    # escape minimal HTML in the name
    safe = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<a href="tg://user?id={member.id}">{safe}</a>'


def _render_welcome(template, member):
    """Substitute @User / {name} / {mention} with a real mention of the member."""
    mention = _mention_html(member)
    out = template
    # @User → mention (word-boundary-ish; simple replace is fine here)
    out = out.replace("@User", mention).replace("@user", mention)
    out = out.replace("{mention}", mention).replace("{name}", mention)
    return out


def _intro_text(new_name=None):
    # Back-compat shim (unused by new flow but kept for safety)
    who = f"Welcome, {new_name}! " if new_name else "Welcome! "
    return (
        f"🏏 <b>{who}</b>\n\n"
        "This is <b>CricMaster Ultra</b> — collect cricketers, build your XI, "
        "play matches, and compete in seasons & clubs.\n\n"
        "👉 Tap <b>/debut</b> to begin.\n\n"
        "🎁 Start the bot in DM for a welcome surprise:"
    )


def _welcome_keyboard(chat_id=None):
    """Deep-link to /debut in DM, plus a Join button for the Official GC
    (unless this *is* the Official GC)."""
    uname = _bot_username()
    rows = []
    if uname:
        rows.append([InlineKeyboardButton(
            "🎁 A welcome surprise for you — tap here!",
            url=f"https://t.me/{uname}?start=debut")])
    try:
        from services import gc_gate
        gid = gc_gate.official_group_id()
        url = gc_gate.join_url()
        if url and gid and chat_id != gid:
            rows.append([InlineKeyboardButton("💬 Join the Official GC", url=url)])
    except Exception:
        logger.debug("welcome GC button failed", exc_info=True)
    return InlineKeyboardMarkup(rows) if rows else None


async def _note_official_gc_join(context, chat_id, members):
    """Someone joined the Official GC: unlock the gate for them straight away
    and tick the journey step (the player gets their card from the job)."""
    try:
        from services import gc_gate, onboarding_service
        if chat_id != gc_gate.official_group_id():
            return
        cache = context.bot_data.get("gc_member_cache")
        for m in members:
            if m.is_bot:
                continue
            if isinstance(cache, dict):
                cache.pop(m.id, None)
            await asyncio.to_thread(onboarding_service.complete_gc_step_for, m.id)
    except Exception:
        logger.exception("Official GC join bookkeeping failed (non-fatal)")


async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires on new_chat_members. Greets humans; intro when bot itself joins."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or not msg.new_chat_members:
        return

    bot_id = context.bot.id
    await _note_official_gc_join(context, chat.id, msg.new_chat_members)
    session = get_session()
    try:
        if not _welcome_enabled(session, chat.id):
            return
    finally:
        session.close()

    kb = _welcome_keyboard(chat.id)

    # Load the (editable) welcome template once per batch
    session = get_session()
    try:
        template = _get_welcome_template(session)
    finally:
        session.close()

    for member in msg.new_chat_members:
        if member.id == bot_id:
            # The bot itself was added to the group
            try:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        "🏏 <b>Thanks for adding CricMaster Ultra!</b>\n\n"
                        "Collect cricketers, build your XI, play matches, and "
                        "compete in monthly seasons & clubs.\n\n"
                        "New here? Tap <b>/debut</b> to begin. Group admins can "
                        "turn these welcomes off with /dwm (and back on with /ewm)."
                    ),
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("bot-added welcome failed")
            continue

        if member.is_bot:
            continue  # don't greet other bots

        # Greet the human using the editable template (with @User mention)
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=_render_welcome(template, member),
                parse_mode="HTML",
                reply_markup=kb,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("new-member welcome failed")


async def enable_welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ewm — enable welcome messages (group admins only)."""
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("This command is for groups.")
        return
    if not await _is_group_admin(update, context):
        await update.message.reply_text("⚠️ Only group admins can do that.")
        return
    session = get_session()
    try:
        _set_welcome(session, chat.id, True, chat=chat)
    finally:
        session.close()
    await update.message.reply_text("✅ Welcome messages <b>enabled</b> for this group.",
                                    parse_mode="HTML")


async def disable_welcome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/dwm — disable welcome messages (group admins only)."""
    chat = update.effective_chat
    if not chat or chat.type == "private":
        await update.message.reply_text("This command is for groups.")
        return
    if not await _is_group_admin(update, context):
        await update.message.reply_text("⚠️ Only group admins can do that.")
        return
    session = get_session()
    try:
        _set_welcome(session, chat.id, False, chat=chat)
    finally:
        session.close()
    await update.message.reply_text("🔕 Welcome messages <b>disabled</b> for this group.",
                                    parse_mode="HTML")
