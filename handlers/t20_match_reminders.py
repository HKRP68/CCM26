"""Preview and send date-scoped World T20 match reminders."""
import asyncio
import logging
import re
from collections import defaultdict
from datetime import date, datetime
from hashlib import blake2s

from sqlalchemy import update as db_update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from database import get_session
from handlers.t20_championship import person_names
from models import T20Championship, T20ChampionshipFixture
from services import rich_message as R, t20_championship as cup
from services.admin_ids import is_admin
from services.match_reminder_service import on_cooldown
from services.rich_message_html import inline
from utils.tournament_time import IST_OFFSET, format_ist

logger = logging.getLogger(__name__)
CALLBACK_PREFIX = "wt20r:"
MAX_FIXTURES = 12
SEND_GAP_SECONDS = 0.06
NOT_ALLOWED = "Only bot admins can send match reminders."
USAGE = "/matchremind today [CHAMPIONSHIP_PREFIX] or /matchremind YYYY-MM-DD [CHAMPIONSHIP_PREFIX]"


def _card(title, lines):
    return ([R.heading(title), *(R.paragraph(line) for line in lines)],
            "\n".join([inline(R.bold(title)), *(inline(line) for line in lines)]))


def _recipients(fixture):
    series = fixture.series
    return tuple(dict.fromkeys(uid for team in (series.home, series.away)
                               for uid in (team.owner_id, team.co_owner_id) if uid))


def _pending(db, championship, day, now):
    if not championship or not championship.started_at or championship.completed_at:
        return []
    rows = cup.series_list(db, championship, refresh=True)
    fixtures = (f for f in cup.fixtures_on_day(rows, day)
                if f.status == "scheduled" and f.deadline >= now
                and not on_cooldown(f.last_reminded_at, now))
    return sorted(fixtures, key=lambda f: (f.deadline, f.opens_at, f.id))


def _fixture_line(fixture):
    series = fixture.series
    return [R.code(cup.match_reference(fixture)),
            f" · {series.home.name} vs {series.away.name}\nDue {format_ist(fixture.deadline)}"]


def _preview_token(fixtures):
    values = [(f.id, f.opens_at, f.deadline, _recipients(f)) for f in fixtures]
    return blake2s(repr(values).encode(), digest_size=5).hexdigest()


async def matchremind_handler(update, context):
    message = update.effective_message
    user = update.effective_user
    title = "World T20 · Match reminders"
    if not user or not is_admin(user.id):
        await R.reply_rich(message, *_card(title, [NOT_ALLOWED]))
        return
    now = datetime.utcnow()
    words = context.args
    prefix = words[1] if len(words) == 2 else ""
    try:
        if not 1 <= len(words) <= 2:
            raise ValueError
        if words[0].lower() == "today":
            day = (now + IST_OFFSET).date()
        elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", words[0]):
            day = date.fromisoformat(words[0])
        else:
            raise ValueError
    except ValueError:
        await R.reply_rich(message, *_card(title, [USAGE]))
        return
    with get_session() as db:
        championships = cup.command_championships(db, prefix, active_only=True)
        if len(championships) == 1:
            championship = championships[0]
            fixtures = _pending(db, championship, day, now)
            title = f"{championship.name} · Reminder preview"
            lines = [R.italic(day.strftime("%d %b %Y") + " · IST")]
            keyboard = None
            if fixtures:
                batch = fixtures[:MAX_FIXTURES]
                people = {uid for fixture in batch for uid in _recipients(fixture)}
                names = person_names(db, people)
                lines.extend(_fixture_line(f) for f in batch)
                lines.append(f"Owners and co-owners to notify · {len(people)}")
                if people:
                    lines.append(", ".join(str(names.get(uid, uid)) for uid in sorted(people)))
                if len(fixtures) > len(batch):
                    lines.append(f"This batch includes the first {len(batch)} fixtures. Run the command again for the rest.")
                payload = f"{user.id}:{championship.id}:{day.toordinal()}:{_preview_token(batch)}"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Send reminders", callback_data=CALLBACK_PREFIX + "send:" + payload),
                    InlineKeyboardButton("Cancel", callback_data=CALLBACK_PREFIX + "cancel:" + str(user.id)),
                ]])
            else:
                lines.append("No pending fixtures on this date are due for a reminder.")
        else:
            keyboard = None
            lines = (["Choose a running championship.", *(f"/matchremind {day} {c.command} — {c.name}"
                                                          for c in championships)] if championships else
                     ["No running World T20 Championship matches that command."])
    await R.reply_rich(message, *_card(title, lines), reply_markup=keyboard)


def _claim_reminders(db, championship_id, day, now, token, in_group):
    championship = cup.lock(db, championship_id)
    fixtures = _pending(db, championship, day, now)[:MAX_FIXTURES]
    if not fixtures or _preview_token(fixtures) != token:
        return "", {}, []
    title = f"{championship.name} · Match reminder"
    by_person = defaultdict(list)
    cards = []
    names = person_names(db, {uid for fixture in fixtures for uid in _recipients(fixture)}) if in_group else {}
    for fixture in fixtures:
        fixture.last_reminded_at = now
        recipients = _recipients(fixture)
        line = _fixture_line(fixture)
        group_line = list(line)
        if in_group and recipients:
            group_line.append("\n")
            for uid in recipients:
                group_line.extend([R.mention(str(names.get(uid, uid)), uid), " "])
        cards.append((fixture.id, recipients, group_line))
        for uid in recipients:
            by_person[uid].append(line)
    play = f"Reply to your opponent with /{championship.command}play and the fixture ID."
    db.commit()
    return title, {uid: _card(title, [*lines, play]) for uid, lines in by_person.items()}, cards


async def _deliver(bot, chat_id, card, **options):
    try:
        await R.send_rich_message(bot, chat_id, *card, **options)
        return True
    except TelegramError as exc:
        logger.info("World T20 reminder to %s failed: %s", chat_id, exc)
        return False


async def matchremind_callback(update, context):
    query = update.callback_query
    try:
        action, owner, *selection = query.data[len(CALLBACK_PREFIX):].split(":")
        owner = int(owner)
        if action == "send" and len(selection) == 3:
            championship_id, ordinal = map(int, selection[:2])
            day = date.fromordinal(ordinal)
            token = selection[2]
        elif action != "cancel" or selection:
            raise ValueError
    except (ValueError, OverflowError):
        await query.answer("This reminder preview is unavailable.", show_alert=True)
        return
    if not is_admin(query.from_user.id) or query.from_user.id != owner:
        await query.answer("Only the bot admin who opened this preview can use its buttons.", show_alert=True)
        return
    if action == "cancel":
        await query.answer()
        await R.edit_rich(query, *_card("Match reminders", ["Reminder cancelled."]))
        return

    await query.answer("Sending match reminders…")
    now = datetime.utcnow()
    chat = update.effective_chat
    in_group = chat is not None and chat.type in ("group", "supergroup")
    with get_session() as db:
        title, direct, cards = _claim_reminders(db, championship_id, day, now, token, in_group)
    if not cards:
        await R.edit_rich(query, *_card("Match reminders", ["The pending fixtures have changed. Run /matchremind again to preview them."]))
        return
    group_ok = False
    if in_group:
        thread = getattr(query.message, "message_thread_id", None)
        options = {"message_thread_id": thread} if thread is not None else {}
        group_ok = await _deliver(context.bot, chat.id, _card(title, [line for _, _, line in cards]), **options)
    delivered = set()
    for uid, card in direct.items():
        if await _deliver(context.bot, uid, card):
            delivered.add(uid)
        await asyncio.sleep(SEND_GAP_SECONDS)
    missed = [fixture_id for fixture_id, recipients, _ in cards
              if not group_ok and delivered.isdisjoint(recipients)]
    if missed:
        with get_session() as db:
            db.execute(db_update(T20ChampionshipFixture)
                       .where(T20ChampionshipFixture.id.in_(missed), T20ChampionshipFixture.last_reminded_at == now)
                       .values(last_reminded_at=None))
            db.commit()
    report = [f"Direct messages delivered · {len(delivered)} of {len(direct)}",
              f"Fixtures reached · {len(cards) - len(missed)} of {len(cards)}"]
    if in_group:
        report.append(f"Group post · {'Sent' if group_ok else 'Delivery failed'}")
    await R.edit_rich(query, *_card(title, report))
