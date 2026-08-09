"""/cmucareer — create and manage your Career Player.

A Career Player is the one card that belongs to *you*: it can never be sold,
traded, packed or dealt to anyone else, it starts identical for everyone (78
across the board), and it only improves when you spend gems on its ten
attributes or complete its weekly quests.

Creation is a photo-and-buttons wizard. Each step edits the *same* message in
place and swaps in that step's artwork, which admins upload on the website:

    country → first-name initial → surname initial → batting hand
            → bowling hand → bowling type → face → confirm

The name is generated from the country and the two initials, and is always a
combination no existing player uses.

Wizard state lives in ``context.bot_data`` keyed by the creator's Telegram id,
and every callback carries that id, so a restart or a second user tapping the
buttons can never drive somebody else's wizard (``services.button_access``
enforces the latter automatically — this is a single-owner flow, so it must NOT
be added to ``SHARED_CALLBACK_PREFIXES``).

Callback data — all under one ``cmucareer:`` router:
    cmucareer:<step>:<owner_tg>:<value>
    cmucareer:cancel:<owner_tg>
"""

import html
import logging

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, Update)
from telegram.ext import ContextTypes

from config import MAX_ROSTER
from database import get_session
from models import User, UserRoster
from services.button_timeout import schedule_button_timeout
from services.career_service import (
    BAT_ATTRS, BOWL_ATTRS, CAREER_MAX, attribute_rows, career_stats,
    create_career_player, get_career_player, total_invested,
)
from services.flags import get_flag
from services.miniapp_buttons import miniapp_button

logger = logging.getLogger(__name__)

_STATE_KEY = "cmucareer_{}"
PROMPT_TTL = 300           # a long wizard deserves a generous button lifetime
COUNTRIES_PER_PAGE = 8

# Displayed label → the value stored on the player. "Pacer" reads better than
# "Fast" in chat, but config.BOWL_STYLES (and therefore the match engine's
# handedness/matchup logic) only knows "Fast".
BOWL_TYPES = (
    ("🔄 Off Spinner", "Off Spinner"),
    ("🌀 Leg Spinner", "Leg Spinner"),
    ("⚡ Pacer", "Fast"),
    ("🎯 Medium Pacer", "Medium Pacer"),
)
HANDS = (("🫱 Right", "Right"), ("🫲 Left", "Left"))


def _esc(value):
    return html.escape(str(value)) if value is not None else ""


# ── Wizard state ────────────────────────────────────────────────────────────

def _draft(context, tg_id):
    return context.bot_data.get(_STATE_KEY.format(tg_id))


def _start_draft(context, tg_id, user_id):
    draft = {"user_id": user_id, "country": None, "initial": None,
             "surname_letter": None, "name": None, "bat_hand": None,
             "bowl_hand": None, "bowl_style": None, "face_slot": None,
             "face_index": 0}
    context.bot_data[_STATE_KEY.format(tg_id)] = draft
    return draft


def _clear_draft(context, tg_id):
    context.bot_data.pop(_STATE_KEY.format(tg_id), None)


def _cb(step, tg_id, value=None):
    return f"cmucareer:{step}:{tg_id}" + (f":{value}" if value is not None else "")


def _cancel_row(tg_id):
    return [InlineKeyboardButton("❌ Cancel", callback_data=_cb("cancel", tg_id))]


def _grid(buttons, per_row):
    """Lay buttons out ``per_row`` at a time."""
    rows = []
    for button in buttons:
        if not rows or len(rows[-1]) >= per_row:
            rows.append([])
        rows[-1].append(button)
    return rows


# ── Step rendering ──────────────────────────────────────────────────────────

def _step_photo(session, step_key):
    from services.career_step_service import step_photo
    try:
        return step_photo(session, step_key)
    except Exception:
        logger.exception("career step photo lookup failed")
        return None


async def _show_step(target, session, step_key, text, keyboard, *, edit=False,
                     photo=None):
    """Send or edit the wizard message, with this step's artwork when there is one.

    ``target`` is the message to reply to (first render) or the callback query
    (every later step). Steps without uploaded artwork degrade to a plain text
    message rather than failing.
    """
    photo = photo if photo is not None else _step_photo(session, step_key)
    markup = InlineKeyboardMarkup(keyboard)

    if not edit:
        if photo is not None:
            try:
                return await target.reply_photo(photo=photo, caption=text,
                                                parse_mode="HTML",
                                                reply_markup=markup)
            except Exception:
                logger.exception("career wizard photo send failed; using text")
        return await target.reply_text(text, parse_mode="HTML",
                                       reply_markup=markup)

    message = target.message
    has_photo = bool(getattr(message, "photo", None))
    try:
        if photo is not None and has_photo:
            # Telegram rejects an edit to identical media, so fall back to
            # editing just the caption when the picture hasn't changed.
            try:
                return await target.edit_message_media(
                    media=InputMediaPhoto(media=photo, caption=text,
                                          parse_mode="HTML"),
                    reply_markup=markup)
            except Exception:
                return await target.edit_message_caption(
                    caption=text, parse_mode="HTML", reply_markup=markup)
        if has_photo:
            return await target.edit_message_caption(caption=text,
                                                     parse_mode="HTML",
                                                     reply_markup=markup)
        return await target.edit_message_text(text, parse_mode="HTML",
                                              reply_markup=markup)
    except Exception:
        logger.exception("career wizard step edit failed")
        return None


def _progress(step_number):
    return f"<i>Step {step_number} of 7</i>"


def _country_step(session, tg_id, page=0):
    from services.career_name_service import available_countries
    countries = available_countries(session)
    if not countries:
        return None, None
    pages = max(1, -(-len(countries) // COUNTRIES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    chunk = countries[page * COUNTRIES_PER_PAGE:(page + 1) * COUNTRIES_PER_PAGE]

    rows = _grid([InlineKeyboardButton(f"{get_flag(c)} {c}",
                                       callback_data=_cb("country", tg_id, c))
                  for c in chunk], 2)
    if pages > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=_cb("cpage", tg_id, (page - 1) % pages)),
            InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=_cb("cpage", tg_id, (page + 1) % pages)),
        ])
    rows.append(_cancel_row(tg_id))
    text = ("🌍 <b>Where are you from?</b>\n\n"
            "Your country decides the pool your name is drawn from, and the "
            "flag on your card.\n\n" + _progress(1))
    return text, rows


def _initial_step(session, tg_id, country):
    from services.career_name_service import available_initials
    letters = available_initials(session, country)
    rows = _grid([InlineKeyboardButton(letter,
                                       callback_data=_cb("initial", tg_id, letter))
                  for letter in letters], 6)
    rows.append([InlineKeyboardButton("🔀 Surprise me",
                                      callback_data=_cb("surprise", tg_id))])
    rows.append(_cancel_row(tg_id))
    text = (f"🔠 <b>Pick your first initial</b>\n\n"
            f"{get_flag(country)} {_esc(country)}\n\n"
            f"Only letters with names behind them are shown.\n\n" + _progress(2))
    return text, rows


def _surname_step(session, tg_id, country, initial):
    from services.career_name_service import available_surname_letters
    letters = available_surname_letters(session, country, initial)
    rows = _grid([InlineKeyboardButton(letter,
                                       callback_data=_cb("surname", tg_id, letter))
                  for letter in letters], 6)
    rows.append(_cancel_row(tg_id))
    text = (f"🔡 <b>Pick your surname initial</b>\n\n"
            f"{get_flag(country)} {_esc(country)} · first initial "
            f"<b>{_esc(initial)}</b>\n\n" + _progress(3))
    return text, rows


def _hand_step(tg_id, step, title, name, subtitle, number):
    rows = _grid([InlineKeyboardButton(label, callback_data=_cb(step, tg_id, value))
                  for label, value in HANDS], 2)
    rows.append(_cancel_row(tg_id))
    text = (f"{title}\n\n🎽 <b>{_esc(name)}</b>\n{subtitle}\n\n" + _progress(number))
    return text, rows


def _bowl_type_step(tg_id, name):
    rows = _grid([InlineKeyboardButton(label, callback_data=_cb("bowltype", tg_id, value))
                  for label, value in BOWL_TYPES], 2)
    rows.append(_cancel_row(tg_id))
    text = (f"🎳 <b>What do you bowl?</b>\n\n🎽 <b>{_esc(name)}</b>\n"
            f"Every Career Player is an All-rounder, so this is what you turn "
            f"your arm over with.\n\n" + _progress(6))
    return text, rows


def _face_step(session, tg_id, index=0):
    """Face carousel — the photo is that face's own card, flipped in place."""
    from services.career_face_service import selectable_faces
    faces = selectable_faces(session)
    if not faces:
        return None, None, None
    index = index % len(faces)
    face = faces[index]

    rows = []
    if len(faces) > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=_cb("fpage", tg_id, (index - 1) % len(faces))),
            InlineKeyboardButton(f"{index + 1}/{len(faces)}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=_cb("fpage", tg_id, (index + 1) % len(faces))),
        ])
    rows.append([InlineKeyboardButton(f"✅ Choose {face.label or f'Face {face.slot}'}",
                                      callback_data=_cb("face", tg_id, face.slot))])
    rows += _grid([InlineKeyboardButton(
        ("• " if i == index else "") + (f.label or f"Face {f.slot}"),
        callback_data=_cb("fpage", tg_id, i)) for i, f in enumerate(faces)], 3)
    rows.append(_cancel_row(tg_id))

    text = (f"🎭 <b>Choose your look</b>\n\n"
            f"Showing <b>{_esc(face.label or f'Face {face.slot}')}</b> "
            f"({index + 1} of {len(faces)}).\n"
            f"Flip through the designs, then tap Choose.\n\n" + _progress(7))
    return text, rows, face


def _face_photo(session, face):
    """The picture shown while browsing a face: its own blank card."""
    if face.preview_file_id:
        return face.preview_file_id
    from services.career_face_service import artwork_path
    path = artwork_path(face.slot)
    if path:
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            logger.exception("career face artwork read failed")
    return _step_photo(session, "face")


def _confirm_step(tg_id, draft, face_label):
    label = next((lbl for lbl, val in BOWL_TYPES if val == draft["bowl_style"]),
                 draft["bowl_style"])
    text = (
        "🎖 <b>Ready to debut?</b>\n\n"
        f"🎽 <b>{_esc(draft['name'])}</b>\n"
        f"{get_flag(draft['country'])} {_esc(draft['country'])}\n"
        f"🏷 All-rounder · <b>78 OVR</b>\n"
        f"🏏 Bats {_esc(draft['bat_hand'])}-handed\n"
        f"🎳 Bowls {_esc(draft['bowl_hand'])}-arm {_esc(label.lstrip('🔄🌀⚡🎯 '))}\n"
        f"🎭 {_esc(face_label)}\n\n"
        "Every attribute starts at <b>78</b>. This card is yours for good — "
        "it can never be sold or traded, and it grows only with gems and "
        "weekly quests.\n\n"
        "<i>Country, name and face are permanent. Check them before you confirm.</i>"
    )
    rows = [
        [InlineKeyboardButton("✅ Create my Career Player",
                              callback_data=_cb("create", tg_id))],
        [InlineKeyboardButton("🔄 Start over", callback_data=_cb("restart", tg_id))],
        _cancel_row(tg_id),
    ]
    return text, rows


# ── Existing-player view ────────────────────────────────────────────────────

def _career_overview(player, user, session):
    """The card caption shown when a user already has a Career Player."""
    from services.config_service import get_config

    rows = attribute_rows(player)
    by_key = {r["key"]: r for r in rows}

    def block(keys, heading):
        lines = [f"<b>{heading}</b>"]
        for key in keys:
            row = by_key[key]
            cost = "MAX" if row["maxed"] else f"{row['next_cost']} 💎"
            lines.append(f"  {row['label']}: <b>{row['value']}</b>  ·  next {cost}")
        return "\n".join(lines)

    invested = total_invested(player)
    streak = user.career_weekly_streak or 0
    try:
        cfg = get_config(session)
        weeks = int(cfg.get("career_streak_weeks") or 4)
        bonus = int(cfg.get("career_streak_bonus_gems") or 100)
    except Exception:
        weeks, bonus = 4, 100
    to_jackpot = (weeks - (streak % weeks)) if weeks else 0

    return (
        f"🎖 <b>{_esc(player.name)}</b> {get_flag(player.country)}\n"
        f"🏷 {_esc(player.category)} · <b>{player.rating} OVR</b> "
        f"(🏏 {player.bat_rating} · 🎳 {player.bowl_rating})\n\n"
        f"{block(BAT_ATTRS, '🏏 Batting')}\n\n"
        f"{block(BOWL_ATTRS, '🎳 Bowling')}\n\n"
        f"💎 Invested so far: <b>{invested:,}</b>\n"
        f"💰 Your balance: <b>{(user.total_gems or 0):,}</b> 💎\n"
        f"🔥 Weekly quest streak: <b>{streak}</b> "
        f"({to_jackpot} more for the {bonus} 💎 jackpot)\n\n"
        f"<i>Upgrade attributes in the Mini App — each point costs its own new "
        f"value, and the cap is {CAREER_MAX}.</i>"
    )


async def _send_career_card(target, session, player, user, chat, tg_id):
    """Send the career card with its overview and the Mini App upgrade button."""
    from services.card_sender import send_player_card

    rows = []
    button = miniapp_button("⬆️ Upgrade in Mini App", "career",
                            is_private=(getattr(chat, "type", "private") == "private"),
                            origin_chat_id=getattr(chat, "id", None))
    if button is not None:
        rows.append([button])
    rows.append([InlineKeyboardButton("📊 Career Stats",
                                      callback_data=_cb("stats", tg_id))])
    # Name/country changes live in handlers/career_change.py under their own
    # cmuchg: prefix — this is just the way in from the card.
    rows.append([InlineKeyboardButton("✏️ Change name / country",
                                      callback_data=f"cmuchg:open:{tg_id}")])

    text = _career_overview(player, user, session)
    try:
        return await send_player_card(
            bot=target.get_bot(), chat_id=chat.id, player=player, caption=text,
            reply_markup=InlineKeyboardMarkup(rows), parse_mode="HTML",
            session=session)
    except Exception:
        logger.exception("career card send failed; falling back to text")
        return await target.reply_text(text, parse_mode="HTML",
                                       reply_markup=InlineKeyboardMarkup(rows))


# ── Command ─────────────────────────────────────────────────────────────────

async def cmucareer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    logger.info("/cmucareer from %s (%s)", tg_user.id, tg_user.username)

    session = get_session()
    try:
        from services.command_config_service import (is_command_enabled,
                                                     get_disabled_message)
        if not is_command_enabled(session, "cmucareer"):
            await update.message.reply_text(
                get_disabled_message(session, "cmucareer"), parse_mode="HTML")
            return

        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await update.message.reply_text("❌ Do /debut first!")
            return

        existing = get_career_player(session, user.id)
        if existing:
            await _send_career_card(update.message, session, existing, user,
                                    update.effective_chat, tg_user.id)
            return

        roster_count = (session.query(UserRoster)
                        .filter(UserRoster.user_id == user.id).count())
        if roster_count >= MAX_ROSTER:
            await update.message.reply_text(
                f"📊 Your squad is full ({roster_count}/{MAX_ROSTER}).\n\n"
                f"Your Career Player needs a slot — sell someone with "
                f"/release first, then run /cmucareer again.",
                parse_mode="HTML")
            return

        text, rows = _country_step(session, tg_user.id, 0)
        if not text:
            await update.message.reply_text(
                "🚧 Career Players aren't ready yet — no country has both a "
                "name pool and a flag configured. Please try again later.",
                parse_mode="HTML")
            return

        _start_draft(context, tg_user.id, user.id)
        sent = await _show_step(update.message, session, "country", text, rows)
        if sent:
            schedule_button_timeout(
                context, sent.chat_id, sent.message_id, delay_seconds=PROMPT_TTL,
                timeout_key=f"cmucareer_{tg_user.id}_{sent.message_id}")
    except Exception:
        logger.exception("/cmucareer failed")
        await update.message.reply_text("⚠️ Something went wrong. Try again.")
    finally:
        session.close()


# ── Callback router ─────────────────────────────────────────────────────────

async def cmucareer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = (query.data or "").split(":", 3)
    if len(parts) < 3:
        await query.answer()
        return
    _, step, owner_raw = parts[0], parts[1], parts[2]
    value = parts[3] if len(parts) > 3 else None

    try:
        owner_tg = int(owner_raw)
    except (TypeError, ValueError):
        await query.answer()
        return
    if query.from_user.id != owner_tg:
        await query.answer("This isn't your Career Player setup!", show_alert=True)
        return

    session = get_session()
    try:
        await _route(query, context, session, step, owner_tg, value)
    except Exception:
        logger.exception("cmucareer callback failed (step=%s)", step)
        try:
            await query.answer("Something went wrong. Run /cmucareer again.",
                               show_alert=True)
        except Exception:
            pass
    finally:
        session.close()


async def _route(query, context, session, step, owner_tg, value):
    from services.career_name_service import generate_name, random_letters

    if step == "cancel":
        _clear_draft(context, owner_tg)
        await query.answer("Cancelled.")
        await _finish_message(query, "❌ Career Player setup cancelled. "
                                     "Run /cmucareer when you're ready.")
        return

    if step == "stats":
        await _show_stats(query, session, owner_tg)
        return

    if step == "restart":
        user = session.query(User).filter(User.telegram_id == owner_tg).first()
        if not user:
            await query.answer("Do /debut first!", show_alert=True)
            return
        _start_draft(context, owner_tg, user.id)
        text, rows = _country_step(session, owner_tg, 0)
        await query.answer("Starting over.")
        await _show_step(query, session, "country", text, rows, edit=True)
        return

    draft = _draft(context, owner_tg)
    if draft is None:
        await query.answer("This setup expired. Run /cmucareer again.",
                           show_alert=True)
        return

    if step == "cpage":
        text, rows = _country_step(session, owner_tg, int(value or 0))
        await query.answer()
        await _show_step(query, session, "country", text, rows, edit=True)
        return

    if step == "country":
        draft["country"] = value
        text, rows = _initial_step(session, owner_tg, value)
        await query.answer()
        await _show_step(query, session, "initial", text, rows, edit=True)
        return

    if step == "surprise":
        initial, surname_letter = random_letters(session, draft["country"])
        if not initial:
            await query.answer("No names available for that country.",
                               show_alert=True)
            return
        name = generate_name(session, draft["country"], initial, surname_letter)
        if not name:
            await query.answer("Couldn't find a free name — pick letters yourself.",
                               show_alert=True)
            return
        draft.update(initial=initial, surname_letter=surname_letter, name=name)
        await query.answer(f"You are {name}!")
        await _advance_to_bat_hand(query, session, owner_tg, draft)
        return

    if step == "initial":
        draft["initial"] = value
        text, rows = _surname_step(session, owner_tg, draft["country"], value)
        await query.answer()
        await _show_step(query, session, "surname", text, rows, edit=True)
        return

    if step == "surname":
        name = generate_name(session, draft["country"], draft["initial"], value)
        if not name:
            await query.answer(
                "Every name for those two letters is taken. Pick another letter.",
                show_alert=True)
            return
        draft["surname_letter"] = value
        draft["name"] = name
        await query.answer(f"You are {name}!")
        await _advance_to_bat_hand(query, session, owner_tg, draft)
        return

    if step == "bathand":
        draft["bat_hand"] = value
        text, rows = _hand_step(
            owner_tg, "bowlhand", "🎳 <b>Which arm do you bowl with?</b>",
            draft["name"], f"Bats {_esc(value)}-handed.", 5)
        await query.answer()
        await _show_step(query, session, "bowl_hand", text, rows, edit=True)
        return

    if step == "bowlhand":
        draft["bowl_hand"] = value
        text, rows = _bowl_type_step(owner_tg, draft["name"])
        await query.answer()
        await _show_step(query, session, "bowl_type", text, rows, edit=True)
        return

    if step == "bowltype":
        draft["bowl_style"] = value
        await query.answer()
        await _show_face_step(query, session, owner_tg, draft, 0)
        return

    if step == "fpage":
        await query.answer()
        await _show_face_step(query, session, owner_tg, draft, int(value or 0))
        return

    if step == "face":
        draft["face_slot"] = int(value)
        from services.career_face_service import get_face
        face = get_face(session, draft["face_slot"])
        label = (face.label if face else None) or f"Face {draft['face_slot']}"
        text, rows = _confirm_step(owner_tg, draft, label)
        await query.answer()
        await _show_step(query, session, "confirm", text, rows, edit=True)
        return

    if step == "create":
        await _create(query, context, session, owner_tg, draft)
        return

    await query.answer()


async def _advance_to_bat_hand(query, session, owner_tg, draft):
    text, rows = _hand_step(
        owner_tg, "bathand", "🏏 <b>Which hand do you bat with?</b>",
        draft["name"], f"{get_flag(draft['country'])} {_esc(draft['country'])}", 4)
    await _show_step(query, session, "bat_hand", text, rows, edit=True)


async def _show_face_step(query, session, owner_tg, draft, index):
    text, rows, face = _face_step(session, owner_tg, index)
    if not text:
        await query.answer()
        await _finish_message(
            query, "🚧 No Career Player faces are available yet. "
                   "Ask an admin to upload one, then run /cmucareer again.")
        return
    draft["face_index"] = index
    await _show_step(query, session, "face", text, rows, edit=True,
                     photo=_face_photo(session, face))


async def _create(query, context, session, owner_tg, draft):
    missing = [k for k in ("country", "name", "bat_hand", "bowl_hand",
                           "bowl_style", "face_slot") if not draft.get(k)]
    if missing:
        await query.answer("Setup is incomplete. Run /cmucareer again.",
                           show_alert=True)
        return

    user = session.query(User).filter(User.telegram_id == owner_tg).first()
    if not user:
        await query.answer("Do /debut first!", show_alert=True)
        return

    result = create_career_player(
        session, user, name=draft["name"], country=draft["country"],
        bat_hand=draft["bat_hand"], bowl_hand=draft["bowl_hand"],
        bowl_style=draft["bowl_style"], face_slot=draft["face_slot"])
    if not result["ok"]:
        session.rollback()
        await query.answer(result["message"], show_alert=True)
        return

    session.commit()
    _clear_draft(context, owner_tg)
    player = result["player"]
    await query.answer("Welcome to the squad! 🎉")

    try:
        await query.edit_message_caption(
            caption=f"🎉 <b>{_esc(player.name)}</b> has signed!",
            parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_text(
                f"🎉 <b>{_esc(player.name)}</b> has signed!", parse_mode="HTML")
        except Exception:
            pass

    await _send_career_card(query.message, session, player, user,
                            query.message.chat, owner_tg)


async def _show_stats(query, session, owner_tg):
    """Send the career player's batting and bowling stats cards."""
    user = session.query(User).filter(User.telegram_id == owner_tg).first()
    player = get_career_player(session, user.id) if user else None
    if not player:
        await query.answer("You don't have a Career Player yet.", show_alert=True)
        return

    stats = career_stats(session, user.id, player) or {}
    await query.answer()

    header = (f"📊 <b>{_esc(player.name)}</b> — career record\n"
              f"🏏 {stats.get('bat_inns', 0)} inns · {stats.get('runs', 0)} runs · "
              f"HS {stats.get('hs_str', '-')} · avg {stats.get('bat_avg', 0)}\n"
              f"🎳 {stats.get('bowl_inns', 0)} inns · "
              f"{stats.get('wickets_taken', 0)} wickets · "
              f"BBF {stats.get('bbf_str', '-')} · econ {stats.get('bowl_economy', 0)}\n"
              f"🏅 Player of the Match: {stats.get('potm', 0)}")

    if not stats.get("played"):
        await query.message.reply_text(
            f"{header}\n\n<i>No matches yet — pick your Career Player in "
            f"/playingxi and get them out there.</i>", parse_mode="HTML")
        return

    sent_any = False
    try:
        from services.batsman_card import generate_batsman_card
        from services.bowler_card import generate_bowler_card
        import io

        batting = generate_batsman_card(
            name=player.name, rating=player.rating,
            bat_rating=player.bat_rating, stats=stats,
            bat_hand=player.bat_hand, bowl_hand=player.bowl_hand,
            bowl_style=player.bowl_style)
        if batting:
            await query.message.reply_photo(photo=io.BytesIO(batting),
                                            caption=header, parse_mode="HTML")
            sent_any = True
        bowling = generate_bowler_card(
            name=player.name, rating=player.rating,
            bowl_rating=player.bowl_rating, stats=stats,
            bat_hand=player.bat_hand, bowl_hand=player.bowl_hand,
            bowl_style=player.bowl_style)
        if bowling:
            await query.message.reply_photo(photo=io.BytesIO(bowling))
            sent_any = True
    except Exception:
        logger.exception("career stats card render failed")

    if not sent_any:
        await query.message.reply_text(header, parse_mode="HTML")


async def _finish_message(query, text):
    try:
        await query.edit_message_caption(caption=text, parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_text(text, parse_mode="HTML")
        except Exception:
            pass
