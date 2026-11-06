"""Admin-only warning commands with persistent history and player notices."""

import asyncio
import html
import logging
import re

from database import get_session
from models import UserWarning
from services.admin_ids import is_bot_admin
from services import warning_service as service

logger = logging.getLogger(__name__)
PAGE_SIZE = 3
USAGE = (
    "⚠️ <b>CMU warnings · 5 active warnings = account ban</b>\n\n"
    "<code>/cmuwarn @user [reason]</code>\n"
    "<code>/cmuunwarn @user [reason]</code>\n"
    "<code>/cmuwarns @user [page]</code>\n\n"
    "Use a Telegram user ID instead of @user, or reply to a player's message "
    "and omit the target.\n"
    "Unwarn removes the latest active warning. Dropping below five lifts a "
    "warning ban; manual bans stay in place. Warning history is kept."
)
_USERNAME = re.compile(r"^@[A-Za-z0-9_]{1,32}$")


def _parse(args, reply_id, action):
    args = list(args)
    if args and args[0].lower() in {"help", "-h", "--help"}:
        return None, None
    if args and (args[0].startswith("@") or args[0].lstrip("-").isdigit()):
        target = args.pop(0)
    elif reply_id is not None:
        target = str(reply_id)
    elif not args:
        return None, None
    else:
        raise ValueError("Choose a player by @username, Telegram user ID, or reply.")
    if not (_USERNAME.fullmatch(target) or (
            target.isascii() and target.isdigit() and 0 < int(target) <= 2**63 - 1)):
        raise ValueError("Use a valid @username or a positive Telegram user ID.")
    if action == "history":
        if len(args) > 1 or (args and (not args[0].isascii() or not args[0].isdigit()
                                      or not 1 <= int(args[0]) <= 2**31 - 1)):
            raise ValueError("Use /cmuwarns @user [page], starting at page 1.")
        return target, int(args[0]) if args else 1
    reason = " ".join(args).strip()
    if len(reason) > 500:
        raise ValueError("Keep the reason to 500 characters or fewer.")
    return target, reason


def _label(telegram_id, name):
    return (f'<a href="tg://user?id={telegram_id}">{html.escape(name[:50])}</a> '
            f'(<code>{telegram_id}</code>)')


def _snippet(value, limit):
    return html.escape(value[:limit]) + ("…" if len(value) > limit else "")


def _history(session, target, page):
    user = service.find_user(session, target)
    count = service.active_warnings(session, user.id).count()
    query = session.query(UserWarning).filter(UserWarning.user_id == user.id)
    total = query.count()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > pages:
        raise ValueError(f"There are {pages} pages. Use /cmuwarns {user.telegram_id} 1.")
    name = f"@{user.username}" if user.username else user.first_name or "Player"
    lines = [f"⚠️ <b>Warning history · {page}/{pages}</b>", _label(user.telegram_id, name),
             f"Active warnings: <b>{count}/{service.WARN_LIMIT}</b>"]
    if user.is_banned:
        lines.append("🚫 Banned (warnings)" if user.warning_ban else "🚫 Banned (manual)")
        if user.ban_reason:
            lines.append("Ban reason: " + _snippet(user.ban_reason, 80))
    else:
        lines.append("Account is not banned.")
    rows = (query.order_by(UserWarning.created_at.desc(), UserWarning.id.desc())
            .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all())
    if not rows:
        lines.append("\nNo warnings have been issued.")
    for row in rows:
        state = "Removed" if row.revoked_at else "Active"
        lines += [f"\n<b>#{row.id} · {state}</b> · {row.created_at:%Y-%m-%d %H:%M} UTC",
                  f"By admin <code>{row.issued_by}</code>: {_snippet(row.reason, 80)}"]
        if row.revoked_at:
            lines += [f"Removed {row.revoked_at:%Y-%m-%d %H:%M} UTC "
                      f"by <code>{row.revoked_by}</code>",
                      _snippet(row.revoke_reason or "Removed by admin.", 60)]
    if page < pages:
        lines.append(f"\nNext: /cmuwarns {user.telegram_id} {page + 1}")
    return "\n".join(lines)


def _execute(action, target, value, admin_id, message_key):
    session = get_session()
    try:
        if action == "history":
            return _history(session, target, value)
        result = service.change_warning(session, target, action=action, reason=value,
                                        admin_telegram_id=admin_id, message_key=message_key)
        session.commit()
        return result
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _confirmation(result):
    label = _label(result.telegram_id, result.name)
    if not result.changed:
        return (f"ℹ️ This command was already processed; no warning changed.\n{label}\n"
                f"Active warnings: <b>{result.count}/{service.WARN_LIMIT}</b>")
    verb = "Warning issued" if result.action == "warn" else "Warning removed"
    lines = [f"⚠️ <b>{verb} · #{result.warning_id}</b>", label,
             f"Active warnings: <b>{result.count}/{service.WARN_LIMIT}</b>",
             f"Reason: {html.escape(result.reason)}"]
    if result.is_banned:
        lines.append("🚫 Banned from CricMaster Ultra at five warnings."
                     if result.warning_ban else "🚫 The manual ban remains in place.")
    elif result.unbanned:
        lines.append("✅ Warning ban lifted. Account is no longer banned.")
    elif result.count == service.WARN_LIMIT - 1:
        lines.append("❗ Final warning: one more warning triggers an account ban.")
    else:
        lines.append(f"{service.WARN_LIMIT - result.count} more warnings will trigger an account ban.")
    return "\n".join(lines)


def _notice(result):
    verb = "received a warning" if result.action == "warn" else "had your latest warning removed"
    lines = [f"⚠️ You {verb} in CricMaster Ultra.",
             f"Active warnings: {result.count}/{service.WARN_LIMIT}", f"Reason: {result.reason}"]
    if result.is_banned:
        lines.append("You are banned from using the bot. Contact a bot admin to appeal."
                     if result.warning_ban else "Your manual ban remains in place.")
    elif result.unbanned:
        lines.append("Your warning ban has been lifted. Your account is no longer banned.")
    else:
        lines.append(f"{service.WARN_LIMIT - result.count} more warnings will trigger an account ban.")
    return "\n".join(lines)


async def _handle(update, context, action):
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    actor = getattr(update, "effective_user", None)
    if not is_bot_admin(actor.id if actor else None):
        await message.reply_text("⛔ Only bot admins can manage or view warnings.")
        return
    reply = getattr(message, "reply_to_message", None)
    reply_user = getattr(reply, "from_user", None)
    reply_id = (getattr(reply_user, "id", None)
                if reply_user and not getattr(reply_user, "is_bot", False)
                and not getattr(reply, "sender_chat", None) else None)
    try:
        target, value = _parse(getattr(context, "args", None) or [], reply_id, action)
        if target is None:
            text = USAGE
        else:
            chat_id = getattr(message, "chat_id", None)
            message_id = getattr(message, "message_id", None)
            key = (chat_id, message_id) if chat_id is not None and message_id is not None else None
            result = await asyncio.to_thread(_execute, action, target, value, actor.id, key)
            if action == "history":
                text = result
            else:
                text = _confirmation(result)
                # Invalidate after commit: the very next command/button sees
                # the new ban status rather than the middleware's old TTL.
                (getattr(context, "bot_data", None) or {}).get("_ban_cache", {}).pop(
                    result.telegram_id, None)
                if result.changed and getattr(context, "bot", None):
                    try:
                        await context.bot.send_message(chat_id=result.telegram_id,
                                                       text=_notice(result), parse_mode=None)
                    except Exception:
                        logger.warning("Warning notice could not be delivered to %s", result.telegram_id)
                        text += "\n⚠️ Could not DM the player; please relay this notice."
    except ValueError as exc:
        text = "❌ " + html.escape(str(exc))
    except Exception:
        logger.exception("CMU warning command failed")
        text = "⚠️ Could not process this warning command. Please try again."
    await message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def cmuwarn_handler(update, context):
    await _handle(update, context, "warn")


async def cmuunwarn_handler(update, context):
    await _handle(update, context, "unwarn")


async def cmuwarns_handler(update, context):
    await _handle(update, context, "history")
