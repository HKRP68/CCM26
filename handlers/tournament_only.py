"""Admin warnings and round-scoped tournament-only restrictions."""

from __future__ import annotations

import asyncio
import html
import logging
import re

from sqlalchemy import func

from database import get_session
from models import User
from services.admin_ids import is_bot_admin

logger = logging.getLogger(__name__)

PAGE_SIZE = 5
USAGE = (
    "<b>Tournament-only access</b>\n"
    "<code>/tournamentonly block @user [reason]</code>\n"
    "<code>/tournamentonly warn @user [reason]</code>\n"
    "<code>/tournamentonly unblock @user</code>\n"
    "<code>/tournamentonly status @user</code>\n"
    "<code>/tournamentonly list [page]</code>\n\n"
    "Use a Telegram user ID instead of @user, or reply to the player's "
    "message and omit the target.\n"
    "Shortcut: <code>/touronly @user [reason]</code>. "
    "Block takes effect immediately. Players unlock after finishing their "
    "required open-round fixtures.\n"
    "Use <code>warn</code> for 24 hours of grace, or add "
    "<code>--grace=12</code> to choose a deadline (0–168 hours). All times are IST."
)
_USERNAME = re.compile(r"^@[A-Za-z0-9_]{1,32}$")


def _label(user, max_name_chars=None):
    name = (f"@{user.username}" if user.username
            else user.first_name or user.team_name or "Player")
    if max_name_chars is not None and len(name) > max_name_chars:
        name = name[:max_name_chars] + "…"
    return (f'<a href="tg://user?id={int(user.telegram_id)}">'
            f'{html.escape(name)}</a> (<code>{int(user.telegram_id)}</code>)')


def _parse(args, reply_id):
    """Resolve syntax without consulting or mutating the database."""
    args = list(args)
    first = args[0].lower() if args else ""
    if first in {"help", "-h", "--help"}:
        return "help", None, ""
    actions = {"block": "block", "on": "block", "unblock": "unblock",
               "off": "unblock", "status": "status", "list": "list", "warn": "warn"}
    action = actions.get(first, "block")
    if first in actions:
        args.pop(0)
    if action == "list":
        if len(args) > 1 or (args and (not args[0].isascii()
                                      or not args[0].isdigit()
                                      or int(args[0]) < 1)):
            raise ValueError("Use /tournamentonly list [page], starting at page 1.")
        return action, int(args[0]) if args else 1, ""

    if args and (args[0].startswith("@") or args[0].isascii() and args[0].isdigit()):
        target = args.pop(0)
    elif reply_id:
        target = str(reply_id)
    else:
        if not args:
            return "help", None, ""
        raise ValueError("Choose a player by @username, Telegram user ID, or reply.")
    if not (_USERNAME.fullmatch(target)
            or target.isascii() and target.isdigit() and int(target) > 0):
        raise ValueError("Use a valid @username or a positive Telegram user ID.")
    if action not in ("block", "warn") and args:
        raise ValueError(f"Use /tournamentonly {action} @user, or reply and omit @user.")
    reason = " ".join(args).strip()
    if len(reason) > 500:
        raise ValueError("Keep the restriction reason to 500 characters or fewer.")
    return action, target, reason


def _find_user(session, target):
    if target.isdigit():
        user = session.query(User).filter(User.telegram_id == int(target)).first()
    else:
        # Equality, rather than ILIKE: '_' in a username must not be a wildcard.
        matches = (session.query(User)
                   .filter(func.lower(User.username) == target[1:].lower())
                   .limit(2).all())
        if len(matches) > 1:
            raise ValueError("More than one stored user has that username. Use a Telegram user ID.")
        user = matches[0] if matches else None
    if user is None:
        raise ValueError("Player not found. Use their Telegram user ID or reply to a player who has run /debut.")
    return user


def _execute(action, target, reason, admin_id):
    """Run blocking database work off the bot's event loop."""
    from services import tournament_only_service as service

    session = get_session()
    try:
        service.refresh_restrictions(session)
        session.commit()
        if action == "list":
            query = session.query(User).filter(User.tournament_only.is_(True))
            total = query.count()
            if not total:
                return "No players currently have tournament-only access."
            pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
            if target > pages:
                raise ValueError(f"There are {pages} pages. Use /tournamentonly list 1.")
            users = (query.order_by(User.tournament_only_at.desc(), User.id)
                     .offset((target - 1) * PAGE_SIZE).limit(PAGE_SIZE).all())
            lines = [f"<b>Tournament-only players · {target}/{pages}</b>"]
            for user in users:
                lines.append(_label(user, max_name_chars=40))
                starts = service.warning_deadline(user)
                lines.append(f"  ⚠️ Warning until {service.format_ist(starts)}"
                             if starts else "  🔒 Active — other game modes blocked")
                if user.tournament_only_reason:
                    # Five bounded labels/snippets stay below 4096 characters,
                    # even when every raw character expands to an HTML entity.
                    text = user.tournament_only_reason
                    lines.append(f"  {html.escape(text[:60])}{'…' if len(text) > 60 else ''}")
            if target < pages:
                lines.append(f"\nNext: /tournamentonly list {target + 1}")
            return "\n".join(lines)

        user = _find_user(session, target)
        if action == "status":
            if not user.tournament_only:
                return f"{_label(user)} has normal game access."
            lines = [f"<b>Tournament-only access</b>\n{_label(user)}"]
            if user.tournament_only_at:
                lines.append(f"Set: {service.format_ist(user.tournament_only_at)}")
            if user.tournament_only_by:
                lines.append(f"Set by admin: <code>{int(user.tournament_only_by)}</code>")
            lines.append(html.escape(service.restriction_message(user, session=session)))
            return "\n".join(lines)

        if action in ("block", "warn"):
            grace = 24 if action == "warn" else 0
            flags = [word for word in reason.split() if word.startswith("--grace=")]
            if flags:
                if len(flags) != 1 or not flags[0][8:].isdigit():
                    raise ValueError("Use --grace=0 through --grace=168.")
                grace = int(flags[0][8:])
                reason = " ".join(word for word in reason.split() if word != flags[0])
            service.schedule_restriction(session, user, reason=reason or None,
                                         admin_telegram_id=admin_id, grace_hours=grace)
        else:
            service.set_restriction(session, user, enabled=False, admin_telegram_id=admin_id)
        label = _label(user)
        notice = service.restriction_message(user, session=session)
        session.commit()
        if action == "unblock":
            return f"✅ {label} can play all game modes again."
        return (f"<b>Updated tournament-only access</b>\n{label}\n\n"
                + html.escape(notice))
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def tournamentonly_handler(update, context):
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None:
        return
    actor = getattr(update, "effective_user", None)
    args = getattr(context, "args", None) or []
    admin = is_bot_admin(actor.id if actor else None)
    # Players can inspect their own remaining fixtures, without moderation
    # privileges or visibility into somebody else's restriction.
    self_status = not args or (len(args) == 1 and str(args[0]).lower() == "status")
    if actor and not admin and self_status:
        try:
            text = await asyncio.to_thread(_execute, "status", str(actor.id), "", actor.id)
        except ValueError as exc:
            text = f"❌ {html.escape(str(exc))}"
        except Exception:
            logger.exception("tournament-only self-status failed")
            text = "⚠️ Could not check your tournament access. Please try again."
        await message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
        return
    # Maintenance bypass membership does not confer account moderation powers.
    if not admin:
        await message.reply_text("⛔ Only bot admins can change tournament-only access.")
        return
    reply = getattr(message, "reply_to_message", None)
    reply_user = getattr(reply, "from_user", None)
    reply_id = (getattr(reply_user, "id", None)
                if reply_user and not getattr(reply_user, "is_bot", False) else None)
    try:
        action, target, reason = _parse(getattr(context, "args", None) or [], reply_id)
        text = (USAGE if action == "help" else
                await asyncio.to_thread(_execute, action, target, reason, actor.id))
        if action in ("block", "warn") and getattr(context, "bot", None):
            def warning():
                from services import tournament_only_service as service
                db = get_session()
                try:
                    player = _find_user(db, target)
                    return player.telegram_id, service.restriction_message(player, session=db)
                finally:
                    db.close()
            recipient, notice = await asyncio.to_thread(warning)
            try:
                await context.bot.send_message(chat_id=recipient, text=notice, parse_mode=None)
            except Exception:
                logger.warning("Tournament warning could not be delivered to %s", recipient)
                text += "\n⚠️ DM warning could not be delivered; please relay the deadline to the player."
    except ValueError as exc:
        text = f"❌ {html.escape(str(exc))}"
    except Exception:
        logger.exception("tournamentonly command failed")
        text = "⚠️ Could not update tournament-only access. Please try again."
    await message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
