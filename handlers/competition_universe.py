"""Telegram entry points for the shared competition read models."""

import asyncio
from datetime import datetime
from html import escape
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, CommandHandler

from database import get_session
from services import competition_analytics, competition_collection, competition_rankings, competition_sources
from services import competition_universe
from services.miniapp_buttons import miniapp_button


logger = logging.getLogger(__name__)
ALIASES = {"competitions": "directory", "competition": "directory", "worldrank": "rankings",
           "worldrankings": "rankings", "collection": "collection", "ctanalytics": "challenge",
           "eventanalytics": "live"}


def render(telegram_id, section, index=0, instance=None, *, private=True):
    from handlers.event_competitions import _owner_token
    from handlers.event_studio import _user

    if not competition_sources.enabled():
        raise ValueError("Competition Universe is unavailable.")
    if not private and section == "collection":
        raise ValueError("Open your Collection Book in a private chat or the MiniApp.")
    sections = {"directory": ("Competition Universe", "universe"),
                "rankings": ("World Rankings", "worldrank"),
                "collection": ("Collection Book", "collection"),
                "live": ("Live Event Analytics", "universe"),
                "challenge": ("Challenge League Analytics", "universe")}
    title, target = sections[section]
    lines = [f"<b>{title}</b>"]
    rows = []
    with get_session() as db:
        user = _user(db, telegram_id)
        if section == "directory":
            data = competition_universe.directory(db, user, datetime.utcnow(), page=index)
            visible = [row for row in data["items"] if private or row["type"] != "room"]
            for row in visible:
                upcoming = (row["next"] or {}) if private else {}
                deadline = (upcoming.get("grace") if upcoming.get("in_grace") else upcoming.get("deadline")) or row.get("deadline")
                label = f'{row["name"]} · {row["format"]} · {row["status"].replace("_", " ").title()}'
                if private and row["teams"]:
                    label += " · " + ", ".join(row["teams"])
                lines.append(escape(label))
                if deadline:
                    lines.append("  " + escape(deadline[:16].replace("T", " ")) + " UTC")
            if not visible:
                lines.append("No competitions available on this page.")
            if private:
                for feature, label in (("rankings", "World Rankings"), ("collection", "Collection Book")):
                    if data["capabilities"][feature]:
                        rows.append([InlineKeyboardButton(label, callback_data=f"cu:{_owner_token(telegram_id)}:{feature}:0")])
        elif section == "rankings":
            data = competition_rankings.page(db, user, index=index)
            for row in data["rows"]:
                lines.append(f'{row["rank"]}. {escape(row["name"][:64])} · {row["rating"]} · {row["matches"]} matches')
            if not data["rows"]:
                lines.append("No public ranks meet the verified-match requirement.")
            own = data.get("own")
            if own and private:
                lines.append(f'Your rating {own["rating"]} over {own["matches"]} verified matches.')
            lines.append(escape(data.get("explanation", "Verified PvP results only.")))
        elif section == "collection":
            data = competition_collection.page(db, user, index)
            for item in data["rows"]:
                lines.append(f'{escape(item["name"])} · {escape(item["kind"].replace("_", " ").title())}'
                             + (" · Superseded" if item["revoked"] else ""))
            if not data["rows"]:
                lines.append("Your verified honours will appear here.")
        else:
            if not instance:
                lines.append(f"Use /{'ctanalytics' if section == 'challenge' else 'eventanalytics'} followed by the instance ID, or open Analytics on a competition card in the MiniApp.")
                data = {}
            elif not private and section == "live":
                raise ValueError("Open private Live Event analytics in your private chat.")
            else:
                data = competition_analytics.build(db, user, section, instance, {})
                lines.append(f'{data["sample"]} recorded fixtures')
                for row in data["teams"][:12]:
                    lines.append(f'{escape(row["team"])} · {row["wins"]}W {row["losses"]}L · {row["format"]}')
                for row in data["players"][:5]:
                    if row.get("bat_runs") is not None:
                        lines.append(f'{escape(row["name"] or "Player")} · {row["bat_runs"]} runs')
    pager = []
    if index:
        pager.append(InlineKeyboardButton("Previous", callback_data=f"cu:{_owner_token(telegram_id)}:{section}:{index-1}"))
    if data.get("more") and index < 80:
        pager.append(InlineKeyboardButton("Next", callback_data=f"cu:{_owner_token(telegram_id)}:{section}:{index+1}"))
    if pager:
        rows.append(pager)
    launch = miniapp_button("Open MiniApp", target, is_private=private)
    if launch:
        rows.append([launch])
    return "\n".join(lines), InlineKeyboardMarkup(rows) if rows else None


async def command(update, context):
    name = update.effective_message.text.split()[0].lower().split("@")[0].removeprefix("/")
    args = context.args or []
    instance = int(args[0]) if args and args[0].isascii() and args[0].isdigit() and len(args[0]) <= 10 else None
    try:
        message, markup = await asyncio.to_thread(render, update.effective_user.id, ALIASES[name], 0, instance,
                                                  private=update.effective_chat.type == "private")
        await update.effective_message.reply_text(message, parse_mode="HTML", reply_markup=markup)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
    except Exception:
        logger.exception("Could not render competition view %s", name)
        await update.effective_message.reply_text("This competition view could not load. Try again.")


async def callback(update, context):
    from handlers.event_competitions import _is_owner

    query = update.callback_query
    parts = query.data.split(":")
    if (len(parts) != 4 or not _is_owner(parts[1], update.effective_user.id)
            or parts[2] not in ("directory", "rankings", "collection") or not parts[3].isdigit()):
        await query.answer("Open your own /competitions menu.", show_alert=True)
        return
    try:
        message, markup = await asyncio.to_thread(render, update.effective_user.id, parts[2], int(parts[3]),
                                                  private=update.effective_chat.type == "private")
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    except Exception:
        logger.exception("Could not render competition callback %s", parts[2])
        await query.answer("Try again or reopen /competitions.", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_text(message, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def register(app):
    app.add_handler(CommandHandler(list(ALIASES), command))
    app.add_handler(CallbackQueryHandler(callback, pattern=r"^cu:"))
