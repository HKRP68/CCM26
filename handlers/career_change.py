"""Changing the name and country on a Career Player, from chat.

Reached from the ✏️ button under the card that ``/cmucareer`` sends, or with
``/cmuchange`` directly. The screen is a *draft editor*, not a wizard: the owner
sets a new country, a new name, or both, sees what the change will cost, and
applies the lot in one go. That is deliberate — one change buys both halves, so
fixing an identity picked in one sitting never costs two rungs of the ladder.

    hub ──▶ 🌍 country list ──▶ back to hub with the country pencilled in
        ├─▶ 🎲 roll a pool name ──▶ back to hub (roll again as often as you like)
        ├─▶ ✍️ type a custom name ──▶ back to hub (goes to review when applied)
        └─▶ ✅ apply ──▶ confirm (when it costs gems) ──▶ charged

Nothing is charged and nothing on the card moves until Apply is confirmed.

Pricing, gating and review live in :mod:`services.career_change_service`; this
module is only the chat surface. The one rule worth repeating here: a name the
owner *typed* goes to the website for approval and **the old name keeps running
until it is approved**, while a name *rolled from the pool* is already vetted and
lands immediately.

Draft state lives in ``context.bot_data`` keyed by the owner's Telegram id, and
every callback carries that id, so nobody can drive somebody else's change. This
is a single-owner flow, so its prefix must NOT go in
``services.button_access.SHARED_CALLBACK_PREFIXES``.

Callback data — one ``cmuchg:`` router:
    cmuchg:<step>:<owner_tg>[:<value>]
"""

import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import User
from services import career_change_service as ccs
from services.career_service import get_career_player
from services.flags import get_flag

logger = logging.getLogger(__name__)

_STATE_KEY = "cmuchg_{}"
COUNTRIES_PER_PAGE = 8
# How long a draft — and, more importantly, an armed "send me your name" prompt
# — stays live. Long enough to think of a name, short enough that the text
# handler is not quietly swallowing a message an hour later.
DRAFT_TTL = 600


def _esc(value):
    return html.escape(str(value)) if value is not None else ""


# ── Draft state ─────────────────────────────────────────────────────────────

def _draft(context, tg_id):
    draft = context.bot_data.get(_STATE_KEY.format(tg_id))
    if draft and draft.get("expires_at", 0) <= time.monotonic():
        context.bot_data.pop(_STATE_KEY.format(tg_id), None)
        return None
    return draft


def _touch(draft):
    draft["expires_at"] = time.monotonic() + DRAFT_TTL
    return draft


def _start_draft(context, tg_id, chat_id):
    draft = _touch({"country": None, "name": None, "source": None,
                    "await_name": False, "chat_id": chat_id,
                    "message_id": None})
    context.bot_data[_STATE_KEY.format(tg_id)] = draft
    return draft


def _clear_draft(context, tg_id):
    context.bot_data.pop(_STATE_KEY.format(tg_id), None)


def _cb(step, tg_id, value=None):
    return f"cmuchg:{step}:{tg_id}" + (f":{value}" if value is not None else "")


def _grid(buttons, per_row):
    rows = []
    for button in buttons:
        if not rows or len(rows[-1]) >= per_row:
            rows.append([])
        rows[-1].append(button)
    return rows


# ── Hub ─────────────────────────────────────────────────────────────────────

def _cost_line(quote):
    if quote["free_grants"]:
        return (f"🎁 You have <b>{quote['free_grants']}</b> free change"
                f"{'s' if quote['free_grants'] > 1 else ''} in hand — this one "
                f"costs nothing.")
    if quote["is_free"]:
        return "🎁 Your <b>first change is free</b>."
    return f"💎 This change costs <b>{quote['cost']:,}</b> gems."


def _future_line(quote):
    """What the next couple of changes will cost, so nobody is surprised."""
    upcoming = [f"#{i} {c:,} 💎" if c else f"#{i} free"
                for i, c in quote["next_costs"][1:]]
    return f"<i>After this: {' · '.join(upcoming)}</i>" if upcoming else ""


def _pending_block(pending):
    lines = ["⏳ <b>Waiting for approval</b>"]
    if pending["new_name"]:
        lines.append(f"Name: {_esc(pending['old_name'])} → "
                     f"<b>{_esc(pending['new_name'])}</b>")
    if pending["new_country"]:
        lines.append(f"Country: {_esc(pending['old_country'])} → "
                     f"<b>{_esc(pending['new_country'])}</b>")
    lines.append("Your card keeps its current name and country until an admin "
                 "approves it. Withdraw it to get your gems back.")
    return "\n".join(lines)


def _hub_screen(session, tg_id, user, player, draft):
    quote = ccs.quote(session, user, player)

    head = (f"✏️ <b>Change your Career Player</b>\n\n"
            f"🎽 <b>{_esc(player.name)}</b> {get_flag(player.country)} "
            f"{_esc(player.country)}\n")

    if quote["pending"]:
        rows = [[InlineKeyboardButton("🚫 Withdraw the request",
                                      callback_data=_cb("withdraw", tg_id))],
                [InlineKeyboardButton("✖️ Close", callback_data=_cb("close", tg_id))]]
        return f"{head}\n{_pending_block(quote['pending'])}", rows

    lines = [head]
    pencilled = []
    if draft.get("country"):
        pencilled.append(f"🌍 Country → <b>{_esc(draft['country'])}</b>")
    if draft.get("name"):
        tag = ("from your country's name pool — applied instantly"
               if draft.get("source") == ccs.SOURCE_POOL
               else "your own name — goes to an admin for approval")
        pencilled.append(f"🔤 Name → <b>{_esc(draft['name'])}</b>\n"
                         f"      <i>{tag}</i>")
    if pencilled:
        lines.append("<b>Your draft</b>\n" + "\n".join(pencilled) + "\n")
    else:
        lines.append("Pick a new country, a new name, or both — nothing is "
                     "charged until you apply.\n")

    lines.append(_cost_line(quote))
    future = _future_line(quote)
    if future:
        lines.append(future)
    lines.append(f"\n💰 Balance: <b>{quote['balance']:,}</b> 💎")
    if not quote["can_change"] and quote["message"]:
        lines.append(f"\n⚠️ {_esc(quote['message'])}")

    rows = [[InlineKeyboardButton("🌍 Change country",
                                  callback_data=_cb("clist", tg_id, 0)),
             InlineKeyboardButton("🎲 Roll a name",
                                  callback_data=_cb("roll", tg_id))]]
    if quote["custom_open"]:
        rows.append([InlineKeyboardButton("✍️ Type my own name",
                                          callback_data=_cb("custom", tg_id))])
    if draft.get("country") or draft.get("name"):
        apply_label = ("✅ Apply — free" if quote["cost"] == 0
                       else f"✅ Apply — {quote['cost']:,} 💎")
        rows.append([InlineKeyboardButton(apply_label,
                                          callback_data=_cb("apply", tg_id))])
        rows.append([InlineKeyboardButton("♻️ Clear draft",
                                          callback_data=_cb("clear", tg_id))])
    rows.append([InlineKeyboardButton("✖️ Close", callback_data=_cb("close", tg_id))])
    return "\n".join(lines), rows


def _country_screen(session, tg_id, player, page=0):
    countries = [c for c in ccs.available_countries(session) if c != player.country]
    if not countries:
        return None, None
    pages = max(1, -(-len(countries) // COUNTRIES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = countries[page * COUNTRIES_PER_PAGE:(page + 1) * COUNTRIES_PER_PAGE]

    rows = _grid([InlineKeyboardButton(f"{get_flag(c)} {c}",
                                       callback_data=_cb("setc", tg_id, c))
                  for c in chunk], 2)
    if pages > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=_cb("clist", tg_id, (page - 1) % pages)),
            InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=_cb("clist", tg_id, (page + 1) % pages)),
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=_cb("hub", tg_id))])
    text = (f"🌍 <b>Where are you from now?</b>\n\n"
            f"Currently {get_flag(player.country)} <b>{_esc(player.country)}</b>. "
            f"Your country sets the flag on your card and the name pool you can "
            f"roll from.")
    return text, rows


def _confirm_screen(tg_id, player, draft, quote):
    lines = ["🧾 <b>Confirm the change</b>\n"]
    if draft.get("name"):
        lines.append(f"🔤 {_esc(player.name)} → <b>{_esc(draft['name'])}</b>")
    if draft.get("country"):
        lines.append(f"🌍 {_esc(player.country)} → <b>{_esc(draft['country'])}</b>")
    lines.append("")
    lines.append(_cost_line(quote))
    if draft.get("source") == ccs.SOURCE_CUSTOM and quote["needs_approval"]:
        lines.append("\n⏳ A name you typed yourself is checked by an admin "
                     "first. The gems are taken now and refunded in full if it "
                     "is refused — and <b>your card keeps its current name "
                     "until it's approved</b>.")
    else:
        lines.append("\nThis is applied to your card straight away.")
    rows = [[InlineKeyboardButton("✅ Yes, do it", callback_data=_cb("applyok", tg_id))],
            [InlineKeyboardButton("⬅️ Back", callback_data=_cb("hub", tg_id))]]
    return "\n".join(lines), rows


# ── Rendering ───────────────────────────────────────────────────────────────

async def _render(query, text, rows):
    """Draw a screen on the message the button lives on.

    The hub is opened from the ✏️ button under the career *card*, which is a
    photo: editing that message would blow away the card and its caption, so the
    first screen is posted as a new text message underneath it instead. Every
    screen after that edits the hub's own message in place, which is what makes
    it read as one panel rather than a wall of replies.
    """
    markup = InlineKeyboardMarkup(rows)
    if getattr(query.message, "photo", None):
        try:
            return await query.message.reply_text(text, parse_mode="HTML",
                                                  reply_markup=markup)
        except Exception:
            logger.exception("career change hub send failed")
            return None
    try:
        return await query.edit_message_text(text, parse_mode="HTML",
                                             reply_markup=markup)
    except Exception:
        # An edit that changes nothing is an error in Telegram's eyes — not
        # worth failing the tap over.
        logger.debug("career change render skipped", exc_info=True)
        return None


async def _show_hub(query, context, session, tg_id, user, player, draft,
                    answer=None):
    draft["await_name"] = False
    _touch(draft)
    text, rows = _hub_screen(session, tg_id, user, player, draft)
    await query.answer(answer or "")
    await _render(query, text, rows)


# ── Entry points ────────────────────────────────────────────────────────────

async def cmuchange_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cmuchange — open the change screen for your own Career Player."""
    tg_user = update.effective_user
    session = get_session()
    try:
        # Same website kill switch /cmucareer honours, so the whole feature can
        # be turned off from the Commands page without a deploy.
        from services.command_config_service import (is_command_enabled,
                                                     get_disabled_message)
        if not is_command_enabled(session, "cmuchange"):
            await update.message.reply_text(
                get_disabled_message(session, "cmuchange"), parse_mode="HTML")
            return

        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return
        player = get_career_player(session, user.id)
        if not player:
            await update.message.reply_text(
                "🎖 You don't have a Career Player yet — create one with "
                "/cmucareer, then come back to change its name or country.",
                parse_mode="HTML")
            return
        draft = _start_draft(context, tg_user.id, update.effective_chat.id)
        text, rows = _hub_screen(session, tg_user.id, user, player, draft)
        sent = await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        if sent:
            draft["message_id"] = sent.message_id
    except Exception:
        logger.exception("/cmuchange failed")
        await update.message.reply_text("⚠️ Something went wrong. Try again.")
    finally:
        session.close()


async def cmuchange_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":", 3)
    if len(parts) < 3:
        await query.answer()
        return
    step, owner_raw = parts[1], parts[2]
    value = parts[3] if len(parts) > 3 else None

    try:
        owner_tg = int(owner_raw)
    except (TypeError, ValueError):
        await query.answer()
        return
    if query.from_user.id != owner_tg:
        await query.answer("This isn't your Career Player!", show_alert=True)
        return

    session = get_session()
    try:
        await _route(query, context, session, step, owner_tg, value)
    except Exception:
        logger.exception("cmuchg callback failed (step=%s)", step)
        session.rollback()
        try:
            await query.answer("Something went wrong. Try /cmuchange again.",
                               show_alert=True)
        except Exception:
            pass
    finally:
        session.close()


async def _route(query, context, session, step, owner_tg, value):
    if step == "close":
        _clear_draft(context, owner_tg)
        await query.answer("Closed.")
        try:
            await query.edit_message_text(
                "✖️ Closed. Run /cmuchange whenever you want to change your "
                "Career Player's name or country.", parse_mode="HTML")
        except Exception:
            pass
        return

    user = session.query(User).filter(User.telegram_id == owner_tg).first()
    player = get_career_player(session, user.id) if user else None
    if not player:
        await query.answer("You don't have a Career Player yet.", show_alert=True)
        return

    draft = _draft(context, owner_tg)
    if draft is None:
        # An expired draft is not a dead end — reopen the hub with a clean one
        # rather than making the owner go and find the command again.
        draft = _start_draft(context, owner_tg, query.message.chat.id)

    if step in ("hub", "open"):
        await _show_hub(query, context, session, owner_tg, user, player, draft)
        return

    if step == "clear":
        draft.update(country=None, name=None, source=None)
        await _show_hub(query, context, session, owner_tg, user, player, draft,
                        answer="Draft cleared.")
        return

    if step == "clist":
        text, rows = _country_screen(session, owner_tg, player, int(value or 0))
        if not text:
            await query.answer("No other country is available right now.",
                               show_alert=True)
            return
        _touch(draft)
        draft["await_name"] = False
        await query.answer()
        await _render(query, text, rows)
        return

    if step == "setc":
        draft["country"] = value
        note = f"Country set to {value}."
        if draft.get("source") == ccs.SOURCE_POOL:
            # A rolled name belongs to the pool of the country it came from.
            # Keeping it here would quietly turn an instant change into one
            # that waits for review, so drop it and let them roll again.
            draft.update(name=None, source=None)
            note = f"Country set to {value} — roll a {value} name."
        await _show_hub(query, context, session, owner_tg, user, player, draft,
                        answer=note)
        return

    if step == "roll":
        country = draft.get("country") or player.country
        name = ccs.roll_pool_name(session, country)
        if not name:
            await query.answer(
                f"No free names left in the {country} pool — type your own "
                f"instead.", show_alert=True)
            return
        draft.update(name=name, source=ccs.SOURCE_POOL)
        await _show_hub(query, context, session, owner_tg, user, player, draft,
                        answer=f"How about {name}?")
        return

    if step == "custom":
        conf = ccs.settings(session)
        if not conf["custom_open"]:
            await query.answer("Custom names are closed right now.",
                               show_alert=True)
            return
        _touch(draft)
        draft["await_name"] = True
        draft["chat_id"] = query.message.chat.id
        await query.answer()
        await _render(query, _custom_prompt(conf), [
            [InlineKeyboardButton("⬅️ Back", callback_data=_cb("hub", owner_tg))]])
        return

    if step == "apply":
        quote = ccs.quote(session, user, player)
        if not draft.get("name") and not draft.get("country"):
            await query.answer("Pick a new name or country first.",
                               show_alert=True)
            return
        if not quote["can_change"]:
            await query.answer(quote["message"], show_alert=True)
            return
        _touch(draft)
        text, rows = _confirm_screen(owner_tg, player, draft, quote)
        await query.answer()
        await _render(query, text, rows)
        return

    if step == "applyok":
        await _apply(query, context, session, owner_tg, user, player, draft)
        return

    if step == "withdraw":
        request = ccs.pending_request(session, user.id)
        result = ccs.cancel_request(session, request, by_owner=True)
        if not result["ok"]:
            session.rollback()
            await query.answer(result["message"], show_alert=True)
            return
        session.commit()
        await _show_hub(query, context, session, owner_tg, user, player, draft,
                        answer=result["message"][:200])
        return

    await query.answer()


def _custom_prompt(conf):
    review = ("An admin checks it before it goes on your card — until then your "
              "current name keeps running. If it's refused you get every gem "
              "back."
              if conf["needs_approval"] else
              "It goes on your card as soon as you apply.")
    return ("✍️ <b>Send me the name you want.</b>\n\n"
            "Just type it as your next message.\n\n"
            "• Letters, spaces, apostrophes, hyphens and full stops only\n"
            "• 2–48 characters, and no other player may already have it\n"
            f"• {review}\n\n"
            "<i>Tap Back to pick a pool name instead.</i>")


async def _apply(query, context, session, owner_tg, user, player, draft):
    result = ccs.submit_change(session, user, player=player,
                               name=draft.get("name"),
                               country=draft.get("country"),
                               source=draft.get("source"))
    if not result["ok"]:
        session.rollback()
        await query.answer(result["message"], show_alert=True)
        return
    session.commit()
    _clear_draft(context, owner_tg)

    await query.answer("Done!" if result["applied"] else "Sent for review.")
    if result["applied"]:
        text = (f"✅ <b>Your Career Player is now</b>\n\n"
                f"🎽 <b>{_esc(player.name)}</b> {get_flag(player.country)} "
                f"{_esc(player.country)}\n\n"
                f"The new card shows up everywhere from your next match.\n"
                f"💰 Balance: <b>{result['balance']:,}</b> 💎")
    else:
        text = (f"⏳ <b>Sent for approval</b>\n\n"
                f"🔤 {_esc(result['request'].old_name)} → "
                f"<b>{_esc(result['name'])}</b>\n"
                + (f"🌍 {_esc(result['request'].old_country)} → "
                   f"<b>{_esc(result['country'])}</b>\n" if result["country"] else "")
                + f"\nYour card keeps its current name and country until an "
                  f"admin approves it. You'll get a message either way, and a "
                  f"refused name refunds every gem.\n"
                  f"💰 Balance: <b>{result['balance']:,}</b> 💎")
    await _render(query, text, [])


# ── The typed name ──────────────────────────────────────────────────────────

async def career_name_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch the next text message from an owner who asked to type a name.

    Registered on its own handler group for every text message, so the very
    first thing it does is check whether this user has an armed prompt — for
    everybody else it is a dictionary lookup and a return.
    """
    message = update.effective_message
    tg_user = update.effective_user
    if not message or not tg_user or not (message.text or "").strip():
        return
    draft = _draft(context, tg_user.id)
    if not draft or not draft.get("await_name"):
        return
    if draft.get("chat_id") and message.chat.id != draft["chat_id"]:
        return

    typed = " ".join(message.text.split())
    session = get_session()
    try:
        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        player = get_career_player(session, user.id) if user else None
        if not player:
            _clear_draft(context, tg_user.id)
            return

        conf = ccs.settings(session)
        clean, error = ccs.validate_name(session, player, typed,
                                         source=ccs.SOURCE_CUSTOM, conf=conf)
        if error:
            await message.reply_text(
                f"❌ {_esc(error)}\n\n<i>Send another name, or run /cmuchange "
                f"to start again.</i>", parse_mode="HTML")
            return

        draft["await_name"] = False
        draft.update(name=clean, source=ccs.SOURCE_CUSTOM)
        _touch(draft)
        text, rows = _hub_screen(session, tg_user.id, user, player, draft)
        sent = await message.reply_text(text, parse_mode="HTML",
                                        reply_markup=InlineKeyboardMarkup(rows))
        if sent:
            draft["message_id"] = sent.message_id
    except Exception:
        logger.exception("career custom name capture failed")
    finally:
        session.close()
