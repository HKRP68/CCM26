"""/cdraft — Challenge Draft: two players build their XIs pick by pick.

One command, a second player joins, and the bot deals eleven slots. Each slot
offers **two cards of the same role within a point of each other on OVR**: the
captain on the clock takes one and the other goes straight to their opponent.
Eleven slots later both captains hold eleven players, and from the pitch choice
onwards this is an ordinary Challenge League match — the same Playing XI
screen, the same toss, the same over-by-over flow.

Career cards are never dealt: the pool comes from ``services.player_cache``,
which already excludes them.

How it reuses the Challenge League
----------------------------------
The object that carries a league match through setup is the *draft dict* in
``context.bot_data[_challenge_team_draft_key(draft_id)]``. This module builds
one of exactly that shape, with two extra keys — ``mode: "cdraft"`` and the
``cdraft`` state from :mod:`services.cdraft_service` — so once the picking is
over, every existing callback (``cl_pitch_``, ``cl_xi_``, ``cl_confirm_``,
``cl_start_``, ``cipl_coin_``, ``cipl_toss_``) drives it unchanged. Only two
functions anywhere need to know the squad came from a draft instead of a league
team: ``handlers.challenge._query_team_players`` and
``handlers.cipl_play.build_xi_from_draft``.
"""

import logging
import random
from datetime import datetime
from html import escape as _esc

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from services import cdraft_service
from services.telegram_user_service import sync_telegram_user
from handlers.match import (
    _active_cric_match_for_user,
    _active_cric_match_in_chat,
    _active_match_in_chat,
    _active_match_for_user,
    _chat_busy_message,
    _mention,
    _user_busy_message,
    _user_label,
)
from handlers.challenge import (
    _active_draft_in_chat,
    _challenge_team_draft_key,
    _challenge_draft_chat_key,
    _expire_challenge_draft,
    _pitch_keyboard,
    _pitch_prompt,
    _release_draft_chat_lock,
    _track_setup_msg,
    _waiting_cm_lobby_in_chat,
    CHALLENGE_DRAFT_EXPIRE,
)

logger = logging.getLogger(__name__)

LEAGUE_NAME = "Challenge Draft"

_ROLE_EMOJI = {
    cdraft_service.ROLE_BATSMAN: "🏏",
    cdraft_service.ROLE_KEEPER: "🧤",
    cdraft_service.ROLE_ALLROUNDER: "🌟",
    cdraft_service.ROLE_BOWLER: "🎯",
}

# The two cards of a slot, labelled so the message and the buttons agree.
_PICK_LABELS = ("🅰️", "🅱️")


def _pick_job_name(draft_id):
    return f"cdraft_pick_{draft_id}"


def _lobby_job_name(draft_id):
    """The abandoned-setup backstop, named exactly as ``handlers.challenge``
    names its own so the two never coexist for one draft."""
    return f"cl_draft_{draft_id}"


def _cancel_lobby_expiry(ctx, draft_id):
    """Stop the abandoned-setup backstop.

    It is keyed only on ``match_launched``, so left running it would tear down a
    draft that is very much alive: eleven picks at a minute each can outlast
    ``CHALLENGE_DRAFT_EXPIRE``. The picking phase has its own safety net (the
    per-pick clock auto-picks, and a side that misses several in a row ends the
    draft), and the backstop is re-armed once picking is over.
    """
    try:
        jq = getattr(ctx, "job_queue", None)
        if jq:
            for job in jq.get_jobs_by_name(_lobby_job_name(draft_id)):
                job.schedule_removal()
    except Exception:
        logger.debug("cdraft: cancelling the setup backstop failed", exc_info=True)


def _arm_lobby_expiry(ctx, draft, message_id):
    draft_id = draft.get("draft_id")
    _cancel_lobby_expiry(ctx, draft_id)
    jq = getattr(ctx, "job_queue", None)
    if not jq:
        return
    try:
        jq.run_once(_expire_challenge_draft, CHALLENGE_DRAFT_EXPIRE,
                    name=_lobby_job_name(draft_id),
                    data={"draft_id": draft_id, "chat_id": draft.get("chat_id"),
                          "message_id": message_id})
    except Exception:
        logger.exception("cdraft: could not schedule the setup backstop")


def _team_name_for(label):
    """A franchise name for a player: "@alice" → "@alice XI"."""
    return f"{label} XI"


# ════════════════════════════════════════════════════════════════════
# Rendering
# ════════════════════════════════════════════════════════════════════

def _card_line(card):
    """One card as it reads in the slot message."""
    bits = [f"{card['rating']} OVR",
            f"BAT {card['bat_rating']}",
            f"BWL {card['bowl_rating']}"]
    line = " · ".join(bits)
    country = card.get("country")
    if country:
        line += f" · {_esc(str(country))}"
    return line


def _squad_block(draft, state, side):
    """A side's squad so far — one line, trimmed to stay readable."""
    info = draft.get(side) or {}
    name = _esc(info.get("name") or ("Host" if side == "host" else "Guest"))
    cards = (state.get("squads") or {}).get(side, [])
    if not cards:
        return f"📋 <b>{name}</b> (0/11): <i>nothing yet</i>"
    names = ", ".join(f"{_esc(c['name'])} {c['rating']}" for c in cards)
    return f"📋 <b>{name}</b> ({len(cards)}/11): {names}"


def _slot_text(draft):
    """The card for the slot currently on the clock."""
    state = draft["cdraft"]
    slot = cdraft_service.current_slot(state)
    side = cdraft_service.current_side(state)
    index = state["index"]
    picker = draft.get(side) or {}
    waiter = draft.get(cdraft_service.other_side(side)) or {}
    role = slot["role"]

    lines = [
        f"🎯 <b>CHALLENGE DRAFT</b> · Slot {index + 1}/{cdraft_service.SLOT_COUNT}",
        "═════════════════════════════",
        f"{_ROLE_EMOJI.get(role, '🏏')} <b>{_esc(role)}</b> · ~{slot['target_rating']} OVR",
        "",
    ]
    for label, card in zip(_PICK_LABELS, slot["cards"]):
        lines.append(f"{label} <b>{_esc(card['name'])}</b>")
        lines.append(f"     {_card_line(card)}")
    lines.extend([
        "",
        f"⏱ {_mention(picker.get('tg_id'), picker.get('name') or 'Captain')} "
        f"— pick one. The other goes to "
        f"{_mention(waiter.get('tg_id'), waiter.get('name') or 'your opponent')}.",
        f"<i>{cdraft_service.PICK_SECONDS}s, or the bot picks the stronger card "
        f"for you.</i>",
        "",
        _squad_block(draft, state, "host"),
        _squad_block(draft, state, "target"),
    ])
    return "\n".join(lines)


def _slot_keyboard(draft_id, index, slot):
    rows = [[InlineKeyboardButton(
        f"{label} {card['name']} ({card['rating']})",
        callback_data=f"cdp_{draft_id}_{index}_{card['id']}")]
        for label, card in zip(_PICK_LABELS, slot["cards"])]
    # The way out of a draft in progress. Without it the only exits are one
    # captain letting three picks lapse or the setup backstop firing, so two
    # people who agree to abandon have to sit and wait for one of those.
    rows.append([InlineKeyboardButton(
        "❌ Cancel Draft", callback_data=f"cdc_{draft_id}")])
    return InlineKeyboardMarkup(rows)


def _pick_recap(draft, side, chosen, other, auto):
    """What replaces a slot's buttons once it has been picked."""
    picker = draft.get(side) or {}
    receiver = draft.get(cdraft_service.other_side(side)) or {}
    how = " <i>(time up — auto-picked)</i>" if auto else ""
    return (
        f"✅ <b>{_esc(picker.get('name') or 'Captain')}</b> took "
        f"<b>{_esc(chosen['name'])}</b> ({chosen['rating']}){how}\n"
        f"➡️ <b>{_esc(receiver.get('name') or 'Opponent')}</b> gets "
        f"<b>{_esc(other['name'])}</b> ({other['rating']})"
    )


def _draft_complete_text(draft):
    state = draft["cdraft"]
    lines = [
        "🏁 <b>DRAFT COMPLETE</b>",
        "═════════════════════════════",
    ]
    for side in ("host", "target"):
        info = draft.get(side) or {}
        shape = cdraft_service.squad_shape(state, side)
        lines.append("")
        lines.append(
            f"👤 <b>{_esc(info.get('name') or side.title())}</b> — "
            f"{_esc(draft.get('host_team') if side == 'host' else draft.get('target_team'))}")
        for pid in cdraft_service.squad_in_batting_order(state, side):
            card = next(c for c in state["squads"][side] if int(c["id"]) == pid)
            lines.append(
                f"   {_ROLE_EMOJI.get(cdraft_service.normalise_role(card['category']), '🏏')} "
                f"{_esc(card['name'])} · {card['rating']}")
        lines.append(
            f"   <i>{shape[cdraft_service.ROLE_BATSMAN]} bat · "
            f"{shape[cdraft_service.ROLE_KEEPER]} wk · "
            f"{shape[cdraft_service.ROLE_ALLROUNDER]} all · "
            f"{shape[cdraft_service.ROLE_BOWLER]} bowl · "
            f"{cdraft_service.squad_strength(state, side)} total OVR</i>")
    lines.extend([
        "",
        "Both squads are legal Playing XIs by construction — 4 batsmen, "
        "a keeper, 2 all-rounders and 4 bowlers each.",
    ])
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
# The pick clock
# ════════════════════════════════════════════════════════════════════

def _cancel_pick_timer(ctx, draft_id):
    try:
        jq = getattr(ctx, "job_queue", None)
        if jq:
            for job in jq.get_jobs_by_name(_pick_job_name(draft_id)):
                job.schedule_removal()
    except Exception:
        logger.debug("cdraft: cancelling the pick timer failed", exc_info=True)


def _arm_pick_timer(ctx, draft):
    draft_id = draft.get("draft_id")
    _cancel_pick_timer(ctx, draft_id)
    jq = getattr(ctx, "job_queue", None)
    if not jq:
        return
    try:
        jq.run_once(_cdraft_pick_timeout, cdraft_service.PICK_SECONDS,
                    name=_pick_job_name(draft_id), data={"draft_id": draft_id})
    except Exception:
        logger.exception("cdraft: could not schedule the pick timer")


async def _cdraft_pick_timeout(ctx):
    """Nobody picked in time: take the stronger card for them and move on.

    A draft is eleven turns; cancelling the whole thing over one lapse is far
    harsher than it needs to be. Only a side that has let ``MAX_AUTO_STREAK``
    picks in a row go by is treated as having walked away.
    """
    draft_id = ctx.job.data["draft_id"]
    draft = ctx.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("mode") != "cdraft":
        return
    state = draft.get("cdraft") or {}
    if draft.get("turn") != "draft" or cdraft_service.is_complete(state):
        return
    slot = cdraft_service.current_slot(state)
    side = cdraft_service.current_side(state)
    try:
        chosen, other = cdraft_service.apply_pick(
            state, state["index"], side, cdraft_service.auto_pick_id(slot),
            auto=True)
    except ValueError:
        logger.debug("cdraft: auto-pick raced a real pick", exc_info=True)
        return
    await _close_slot_message(ctx, draft, side, chosen, other, auto=True)

    idle = cdraft_service.walked_away(state)
    if idle:
        info = draft.get(idle) or {}
        await _abandon(
            ctx, draft,
            f"⌛ <b>Draft cancelled</b> — "
            f"{_esc(info.get('name') or 'a captain')} missed "
            f"{cdraft_service.MAX_AUTO_STREAK} picks in a row.")
        return
    await _advance(ctx, draft)


# ════════════════════════════════════════════════════════════════════
# Driving the draft
# ════════════════════════════════════════════════════════════════════

async def _close_slot_message(ctx, draft, side, chosen, other, auto):
    """Replace the finished slot's buttons with what happened."""
    state = draft["cdraft"]
    chat_id = draft.get("chat_id")
    msg_id = state.pop("msg_id", None)
    if chat_id is None or msg_id is None:
        return
    try:
        await ctx.bot.edit_message_text(
            _pick_recap(draft, side, chosen, other, auto),
            chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    except Exception:
        logger.debug("cdraft: could not edit the finished slot message",
                     exc_info=True)


async def _advance(ctx, draft):
    """Post the next slot, or finish the draft when all eleven are filled."""
    state = draft["cdraft"]
    if cdraft_service.is_complete(state):
        await _finish_draft(ctx, draft)
        return
    chat_id = draft.get("chat_id")
    slot = cdraft_service.current_slot(state)
    try:
        sent = await ctx.bot.send_message(
            chat_id, _slot_text(draft), parse_mode="HTML",
            reply_markup=_slot_keyboard(draft.get("draft_id"),
                                        state["index"], slot))
    except Exception:
        logger.exception("cdraft: could not post slot %s", state["index"] + 1)
        await _abandon(ctx, draft,
                       "⚠️ <b>Draft cancelled</b> — the next pick could not be "
                       "posted. Try /cdraft again.")
        return
    state["msg_id"] = sent.message_id
    _track_setup_msg(draft, sent)
    _arm_pick_timer(ctx, draft)


async def _finish_draft(ctx, draft):
    """Hand the finished squads to the ordinary Challenge League setup flow."""
    _cancel_pick_timer(ctx, draft.get("draft_id"))
    # From here the draft dict is an ordinary league draft waiting on its pitch,
    # which is what every downstream callback checks for.
    draft["turn"] = "complete"
    chat_id = draft.get("chat_id")
    try:
        sent = await ctx.bot.send_message(chat_id, _draft_complete_text(draft),
                                          parse_mode="HTML")
        _track_setup_msg(draft, sent)
    except Exception:
        logger.exception("cdraft: could not post the draft recap")
    # The host picks the surface, exactly as in /cipl. No Deny button: the guest
    # agreed to this match by joining and has just spent eleven picks on it.
    try:
        sent = await ctx.bot.send_message(
            chat_id, _pitch_prompt(draft), parse_mode="HTML",
            reply_markup=_pitch_keyboard(draft.get("draft_id"), allow_deny=False))
        _track_setup_msg(draft, sent)
    except Exception:
        logger.exception("cdraft: could not send the pitch prompt")
        return
    # Back under the league flow's own clocks: the per-turn selection timer for
    # pitch and Playing XI, and the backstop for a setup abandoned outright.
    _arm_lobby_expiry(ctx, draft, sent.message_id)
    from handlers.challenge import _arm_selection_timer
    await _arm_selection_timer(ctx, draft, [draft.get("host_tg_id")], "pitch")


async def _abandon(ctx, draft, message=None):
    """Tear a draft down and free the chat.

    ``message`` is posted to the chat when given. It is omitted by the caller
    that has already said so by editing the card the button was on — two
    messages for one cancellation is noise.
    """
    draft_id = draft.get("draft_id")
    _cancel_pick_timer(ctx, draft_id)
    _cancel_lobby_expiry(ctx, draft_id)
    _release_draft_chat_lock(ctx.bot_data, draft)
    ctx.bot_data.pop(_challenge_team_draft_key(draft_id), None)
    chat_id = draft.get("chat_id")
    if chat_id is None or message is None:
        return
    try:
        await ctx.bot.send_message(chat_id, message, parse_mode="HTML")
    except Exception:
        logger.exception("cdraft: could not announce the cancellation")


# ════════════════════════════════════════════════════════════════════
# /cdraft
# ════════════════════════════════════════════════════════════════════

def _lobby_text(draft, target_label=None):
    host = draft.get("host") or {}
    who = (f"Only {_esc(target_label)} can join."
           if target_label else "Anyone in this chat can join.")
    settings = draft.get("cdraft_settings") or {}
    return "\n".join([
        "🎯 <b>CHALLENGE DRAFT</b>",
        "═════════════════════════════",
        f"👑 <b>Host:</b> {_mention(host.get('tg_id'), host.get('name') or 'Host')}",
        "",
        f"Eleven slots. Each one offers <b>two players of the same role, within "
        f"{cdraft_service.PAIR_SPREAD} OVR of each other</b> — the captain on the "
        f"clock takes one, the other goes to their opponent.",
        "",
        "• No squads, no career cards — straight from the player pool.",
        # Both captains should know what they are drafting from before either
        # of them commits to eleven picks.
        f"• 🎱 Pool: {_esc(cdraft_service.settings_summary(settings))}",
        "• Both XIs end up 4 batsmen, a keeper, 2 all-rounders, 4 bowlers.",
        "• Then pitch, toss, and a normal Challenge League match.",
        "",
        f"🙋 {who}",
    ])


def _lobby_keyboard(draft_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🙋 Join Draft", callback_data=f"cdj_{draft_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"cdc_{draft_id}"),
    ]])


async def cdraft_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cdraft — open a Challenge Draft lobby for someone to join.

    Reply to a player's message to invite them specifically; otherwise the first
    person in the chat to tap Join takes the seat.
    """
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    if getattr(chat, "type", "private") == "private":
        await message.reply_text(
            "🎯 /cdraft is a two-player mode — run it in a group where somebody "
            "can join you.")
        return

    session = get_session()
    try:
        host = sync_telegram_user(session, update.effective_user)
        if not host:
            await message.reply_text("❌ Use /debut first.")
            return

        busy = (_active_match_in_chat(session, chat.id)
                or _active_cric_match_in_chat(session, chat.id))
        if busy:
            await message.reply_text(_chat_busy_message(busy), parse_mode="HTML")
            return
        if _active_draft_in_chat(context.bot_data, chat.id) \
                or _waiting_cm_lobby_in_chat(context.bot_data, chat.id):
            await message.reply_text(
                "⚠️ A challenge is already being set up in this chat. Finish or "
                "cancel it first.")
            return
        host_busy = (_active_match_for_user(session, host.id)
                     or _active_cric_match_for_user(session, host.id))
        if host_busy:
            await message.reply_text(_user_busy_message(host_busy),
                                     parse_mode="HTML",
                                     disable_web_page_preview=True)
            return

        # An invited guest, when /cdraft was sent as a reply. Resolved now so a
        # nonexistent target is reported before the lobby is posted.
        invited_id = None
        invited_label = None
        reply_to = getattr(message, "reply_to_message", None)
        target_tg = getattr(reply_to, "from_user", None) if reply_to else None
        if target_tg is not None and not getattr(target_tg, "is_bot", False):
            if target_tg.id == update.effective_user.id:
                await message.reply_text("❌ You cannot draft against yourself.")
                return
            invited = sync_telegram_user(session, target_tg)
            if not invited:
                await message.reply_text(
                    "❌ That player needs to use /debut first.")
                return
            invited_id = target_tg.id
            invited_label = _user_label(invited)

        # Deal the slots up front: a pool that cannot supply eleven legal pairs
        # should say so now, not after somebody has joined. The rating band and
        # the allowed editions come from the admin's settings (the Match
        # Gameplay page / /cdraftset), falling back to the CDRAFT_* defaults.
        settings = cdraft_service.load_settings()
        try:
            slots = cdraft_service.build_slots(
                seed=random.randrange(1 << 30),
                top=settings["rating_max"], bottom=settings["rating_min"],
                versions=settings["versions"])
        except cdraft_service.CdraftPoolError as exc:
            logger.warning("cdraft: could not deal a draft: %s", exc)
            await message.reply_text(
                f"❌ {_esc(str(exc))}\n\n"
                "An admin can widen the pool with /cdraftset or on the Match "
                "Gameplay settings page.")
            return

        draft_id = random.randint(100000, 999999)
        while context.bot_data.get(_challenge_team_draft_key(draft_id)):
            draft_id = random.randint(100000, 999999)

        draft = {
            "draft_id": draft_id,
            "chat_id": chat.id,
            "mode": "cdraft",
            "host_user_id": host.id,
            "host_tg_id": host.telegram_id,
            # Filled in when somebody joins.
            "target_user_id": None,
            "target_tg_id": None,
            "invited_tg_id": invited_id,
            # No league backs a drafted squad, so there is no league record, no
            # franchise list and no overseas rule to inherit.
            "league_key": None,
            "league_name": LEAGUE_NAME,
            "is_tournament": False,
            "tournament_id": None,
            "owner_locked": False,
            "host_allowed_teams": None,
            "target_allowed_teams": None,
            "vs_bot": False,
            "teams": [],
            "team_codes": {},
            "overseas_min": 0,
            "overseas_max": 11,
            "ball_format": "T20",
            "turn": "join",
            "host": {"user_id": host.id, "tg_id": host.telegram_id,
                     "name": _user_label(host)},
            "target": {},
            "host_team": _team_name_for(_user_label(host)),
            "target_team": None,
            "cdraft": cdraft_service.new_state(slots),
            # Kept so the lobby card can name the pool this draft was dealt
            # from, even if an admin changes the settings mid-draft.
            "cdraft_settings": settings,
            "created_at": datetime.utcnow().isoformat(),
        }
        context.bot_data[_challenge_team_draft_key(draft_id)] = draft
        # Take the per-chat lock now: every other challenge command already
        # checks it, so the lobby blocks and is blocked like any league setup.
        context.bot_data[_challenge_draft_chat_key(chat.id)] = draft_id

        try:
            sent = await message.reply_text(
                _lobby_text(draft, invited_label), parse_mode="HTML",
                reply_markup=_lobby_keyboard(draft_id))
        except Exception:
            logger.exception("cdraft: could not post the lobby; releasing the lock")
            _release_draft_chat_lock(context.bot_data, draft)
            context.bot_data.pop(_challenge_team_draft_key(draft_id), None)
            return
        _track_setup_msg(draft, sent)
        draft["lobby_msg_id"] = sent.message_id

        # Free an abandoned lobby (and its chat lock) the same way a league
        # team-selection draft is freed.
        _arm_lobby_expiry(context, draft, sent.message_id)
    finally:
        session.close()


async def cdraft_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cdc_{draft_id} — call the draft off.

    In the lobby that is the host's call alone: nobody has invested anything
    yet, and the host is the one holding the chat. Once picking has started both
    captains have, so either may end it. Nobody else in the group can, at either
    stage.
    """
    query = update.callback_query
    try:
        draft_id = int(query.data.split("_")[1])
    except Exception:
        await query.answer("Invalid button.", show_alert=True)
        return
    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("mode") != "cdraft":
        await query.answer("This draft is no longer active.", show_alert=True)
        return

    turn = draft.get("turn")
    if turn not in ("join", "draft"):
        # From the pitch step on, the Challenge League flow owns this draft and
        # its own selection timers handle an abandoned setup.
        await query.answer("The draft is done — this match is already being set up.",
                           show_alert=True)
        return

    presser = query.from_user.id
    allowed = [draft.get("host_tg_id")]
    if turn == "draft":
        allowed.append(draft.get("target_tg_id"))
    if presser not in [tg for tg in allowed if tg]:
        await query.answer(
            "Only the host can cancel this draft." if turn == "join"
            else "Only the two captains can cancel this draft.", show_alert=True)
        return

    if turn == "join":
        _cancel_pick_timer(context, draft_id)
        _cancel_lobby_expiry(context, draft_id)
        _release_draft_chat_lock(context.bot_data, draft)
        context.bot_data.pop(_challenge_team_draft_key(draft_id), None)
        await query.answer("Draft cancelled.")
        try:
            await query.edit_message_text("❌ <b>Challenge Draft cancelled.</b>",
                                          parse_mode="HTML")
        except Exception:
            logger.debug("cdraft: could not edit the cancelled lobby", exc_info=True)
        return

    # Mid-draft. _abandon stops the pick clock, releases the per-chat lock and
    # drops the draft; the slot card is edited here so its buttons go with it.
    side = "host" if presser == draft.get("host_tg_id") else "target"
    who = (draft.get(side) or {}).get("name") or "A captain"
    await query.answer("Draft cancelled.")
    # The card this button is on IS the live slot, so editing it is what removes
    # the pick buttons; _abandon then cleans up without posting a second notice.
    draft.get("cdraft", {}).pop("msg_id", None)
    try:
        await query.edit_message_text(
            f"❌ <b>Challenge Draft cancelled</b> by {_esc(who)}.\n"
            f"<i>The chat is free for a new /cdraft.</i>",
            parse_mode="HTML")
    except Exception:
        logger.debug("cdraft: could not edit the cancelled slot card", exc_info=True)
    await _abandon(context, draft)


async def cdraft_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cdj_{draft_id} — a second player takes the seat and the draft begins."""
    query = update.callback_query
    try:
        draft_id = int(query.data.split("_")[1])
    except Exception:
        await query.answer("Invalid button.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("mode") != "cdraft":
        await query.answer("This draft is no longer active.", show_alert=True)
        return
    if draft.get("turn") != "join":
        await query.answer("Somebody has already joined this draft.",
                           show_alert=True)
        return
    if query.from_user.id == draft.get("host_tg_id"):
        await query.answer("You started this draft — wait for someone to join.",
                           show_alert=True)
        return
    invited = draft.get("invited_tg_id")
    if invited and query.from_user.id != invited:
        await query.answer("This draft was opened for somebody else.",
                           show_alert=True)
        return

    session = get_session()
    try:
        guest = sync_telegram_user(session, query.from_user)
        if not guest:
            await query.answer("Use /debut first.", show_alert=True)
            return
        guest_busy = (_active_match_for_user(session, guest.id)
                      or _active_cric_match_for_user(session, guest.id))
        if guest_busy:
            await query.answer(
                f"You're already in an active match (#{guest_busy.id}). "
                "Finish it first.", show_alert=True)
            return
        guest_label = _user_label(guest)
        draft["target_user_id"] = guest.id
        draft["target_tg_id"] = guest.telegram_id
        draft["target"] = {"user_id": guest.id, "tg_id": guest.telegram_id,
                           "name": guest_label}
        draft["target_team"] = _team_name_for(guest_label)
    finally:
        session.close()

    # Claim the seat before any await that could let a second tap through.
    draft["turn"] = "draft"
    # Picking runs on its own per-slot clock from here; the setup backstop is
    # re-armed when the draft hands over to the pitch step.
    _cancel_lobby_expiry(context, draft_id)
    await query.answer("You're in — let's draft!")
    try:
        await query.edit_message_text(
            f"🎯 <b>CHALLENGE DRAFT</b>\n"
            "═════════════════════════════\n"
            f"👑 {_mention(draft['host'].get('tg_id'), draft['host'].get('name'))}"
            f" — {_esc(draft.get('host_team'))}\n"
            f"⚔️ {_mention(draft['target'].get('tg_id'), draft['target'].get('name'))}"
            f" — {_esc(draft.get('target_team'))}\n\n"
            f"{cdraft_service.SLOT_COUNT} slots, "
            f"{cdraft_service.PICK_SECONDS}s a pick. Here we go.",
            parse_mode="HTML")
    except Exception:
        logger.debug("cdraft: could not edit the lobby into a header",
                     exc_info=True)
    await _advance(context, draft)


async def cdraft_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """cdp_{draft_id}_{slot}_{player_id} — the captain on the clock picks."""
    query = update.callback_query
    try:
        _, draft_id, slot_index, player_id = query.data.split("_")
        draft_id, slot_index, player_id = (int(draft_id), int(slot_index),
                                           int(player_id))
    except Exception:
        await query.answer("Invalid pick button.", show_alert=True)
        return

    draft = context.bot_data.get(_challenge_team_draft_key(draft_id))
    if not draft or draft.get("mode") != "cdraft" or draft.get("turn") != "draft":
        await query.answer("This draft is no longer active.", show_alert=True)
        return
    state = draft["cdraft"]
    side = cdraft_service.current_side(state)
    if side is None:
        await query.answer("This draft is already complete.", show_alert=True)
        return
    if query.from_user.id != (draft.get(side) or {}).get("tg_id"):
        picker = draft.get(side) or {}
        await query.answer(
            f"It's {picker.get('name') or 'the other captain'}'s pick, not yours.",
            show_alert=True)
        return

    try:
        chosen, other = cdraft_service.apply_pick(state, slot_index, side,
                                                  player_id)
    except ValueError as exc:
        # A stale button from an earlier slot, or a race with the auto-pick.
        # Nothing is cancelled on this path: the live slot's clock belongs to
        # whoever is genuinely on it, and a stray tap must not reset it.
        await query.answer(str(exc), show_alert=True)
        return
    # The pick landed, so this slot's clock is spent. Stopping it only now is
    # safe because everything from the guards above to here is synchronous —
    # the timeout job cannot run in between and double-pick the slot.
    _cancel_pick_timer(context, draft_id)

    await query.answer(f"{chosen['name']} — yours.")
    await _close_slot_message(context, draft, side, chosen, other, auto=False)
    await _advance(context, draft)
