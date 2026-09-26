"""/halloffame — the game's all-time records, one page per section.

Single-match records link to their match in the Mini App when a link can be
built; names are plain text so reading the Hall of Fame pings nobody.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from services import hall_of_fame as hof

logger = logging.getLogger(__name__)

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _match_ref(match_id, chat_id=None):
    """"#123" — as a link to the read-only match view when one can be built."""
    if not match_id:
        return ""
    try:
        from services.match_broadcast import _launch_url
        url = _launch_url(match_id, chat_id)
    except Exception:
        url = None
    if url:
        return f'<a href="{html.escape(url, quote=True)}">#{match_id}</a>'
    return f"#{match_id}"


def _keyboard(current):
    row = []
    for key, (label, _cats) in hof.SECTIONS.items():
        text = f"• {label} •" if key == current else label
        row.append(InlineKeyboardButton(text, callback_data=f"hof_{key}"))
    return InlineKeyboardMarkup([row[:2], row[2:]])


def render_section(session, key, chat_id=None, limit=5):
    label, cats = hof.SECTIONS.get(key, hof.SECTIONS["bat"])
    lines = [f"🏛️ <b>HALL OF FAME</b> — {html.escape(label)}", "━━━━━━━━━━━━━━━"]
    if key == "career":
        for title, emoji, rows in hof.career_boards(session, limit=limit):
            lines.append(f"\n{emoji} <b>{html.escape(title)}</b>")
            if not rows:
                lines.append("<i>No record yet.</i>")
            for i, (value, holder, _uid) in enumerate(rows, start=1):
                lines.append(f"{_MEDALS.get(i, f'{i}.')} {html.escape(holder)} — "
                             f"<b>{html.escape(value)}</b>")
        return "\n".join(lines)
    for cat in cats:
        title, emoji = hof.MATCH_CATEGORIES[cat]
        entries = hof.top_entries(session, cat, limit=limit)
        names = hof.holder_names(session, entries)
        lines.append(f"\n{emoji} <b>{html.escape(title)}</b>")
        if not entries:
            lines.append("<i>No record yet — go set one.</i>")
        for i, e in enumerate(entries, start=1):
            who = e.player_name or e.team_name or "—"
            extra = []
            if e.player_name and e.team_name:
                extra.append(e.team_name)
            if e.user_id and names.get(e.user_id):
                extra.append(names[e.user_id])
            tail = f" <i>({html.escape(' · '.join(extra))})</i>" if extra else ""
            when = e.achieved_at.strftime("%d %b %Y") if e.achieved_at else ""
            ref = _match_ref(e.match_id, chat_id)
            meta = " · ".join(x for x in (ref, when) if x)
            lines.append(f"{_MEDALS.get(i, f'{i}.')} <b>{html.escape(e.label)}</b> — "
                         f"{html.escape(who)}{tail}" + (f"\n     {meta}" if meta else ""))
    return "\n".join(lines)


async def halloffame_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/halloffame [bat|bowl|team|career]"""
    key = (context.args[0].lower() if context.args else "bat")
    if key not in hof.SECTIONS:
        key = "bat"
    chat = update.effective_chat
    session = get_session()
    try:
        text = render_section(session, key, chat_id=chat.id if chat else None)
        await update.message.reply_text(text, parse_mode="HTML",
                                        reply_markup=_keyboard(key),
                                        disable_web_page_preview=True)
    except Exception:
        logger.exception("/halloffame failed")
        await update.message.reply_text("❌ Couldn't open the Hall of Fame.")
    finally:
        session.close()


async def halloffame_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    key = (q.data or "").split("_", 1)[-1]
    if key not in hof.SECTIONS:
        await q.answer()
        return
    await q.answer()
    session = get_session()
    try:
        chat = update.effective_chat
        text = render_section(session, key, chat_id=chat.id if chat else None)
        try:
            await q.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=_keyboard(key),
                                      disable_web_page_preview=True)
        except Exception:
            pass
    except Exception:
        logger.exception("/halloffame callback failed")
    finally:
        session.close()


async def hall_of_fame_scan_job(context):
    """Background job: harvest newly finished matches into the Hall of Fame."""
    import asyncio

    def _run():
        session = get_session()
        try:
            n = hof.scan(session, limit=200)
            session.commit()
            return n
        except Exception:
            session.rollback()
            logger.exception("hall of fame scan failed")
            return 0
        finally:
            session.close()

    n = await asyncio.to_thread(_run)
    if n:
        logger.info("Hall of Fame: harvested %s match(es)", n)
