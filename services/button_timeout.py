"""Button auto-timeout — strips inline keyboards from messages after a delay.

Usage:
  from services.button_timeout import schedule_button_timeout
  msg = await update.message.reply_text("...", reply_markup=kb)
  schedule_button_timeout(context, msg.chat_id, msg.message_id,
                          delay_seconds=120, custom_text=None)

Optional `custom_text`: replaces the message text when buttons expire.
If None, the original text is preserved (only buttons removed).

Also enforces "owner-only buttons" via `mark_owner` + `check_owner`.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


def schedule_button_timeout(context, chat_id: int, message_id: int,
                            delay_seconds: int = 120,
                            custom_text: str | None = None,
                            timeout_key: str | None = None):
    """Schedule a task to remove inline buttons after `delay_seconds`.

    If timeout_key is given and that key is set in context.bot_data
    after the delay (meaning the button was clicked or task was cancelled),
    the timeout is skipped.
    """
    async def _expire():
        try:
            await asyncio.sleep(delay_seconds)
            # Cancellation flag check
            if timeout_key and context.bot_data.get(timeout_key) == "done":
                return
            try:
                if custom_text:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id,
                        text=custom_text, reply_markup=None, parse_mode="HTML")
                else:
                    await context.bot.edit_message_reply_markup(
                        chat_id=chat_id, message_id=message_id, reply_markup=None)
            except Exception:
                # Message may already be edited/deleted
                pass
            # Clean up bot_data state
            if timeout_key:
                context.bot_data.pop(timeout_key, None)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Button timeout expire failed")

    task = asyncio.create_task(_expire())
    if timeout_key:
        context.bot_data[f"{timeout_key}_task"] = task
    return task


def cancel_button_timeout(context, timeout_key: str):
    """Cancel a pending timeout (e.g. user clicked a button before expiry)."""
    task_key = f"{timeout_key}_task"
    task = context.bot_data.get(task_key)
    if task and not task.done():
        try:
            task.cancel()
        except Exception:
            pass
    context.bot_data.pop(task_key, None)
    context.bot_data[timeout_key] = "done"


def mark_owner(context, key: str, telegram_user_id: int):
    """Tag a UI flow so only the original user can interact with its buttons."""
    context.bot_data[f"owner_{key}"] = telegram_user_id


def check_owner(context, key: str, telegram_user_id: int) -> bool:
    """Return True if this user is the owner (or no owner is set)."""
    owner = context.bot_data.get(f"owner_{key}")
    if owner is None:
        return True
    return owner == telegram_user_id


def clear_owner(context, key: str):
    context.bot_data.pop(f"owner_{key}", None)
