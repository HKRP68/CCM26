"""Tournament Draft commands — /pick and the rest of the draft floor.

A draft is bound to one group chat. Everything here is refused outside it: a
pick is a public, irreversible event that the rest of the room has to be able to
see and argue with, and one made quietly in a DM is how an order gets disputed.

Who may pick is deliberately narrow — the team's Owner Tag ID and any co-owner
an admin has added, and nobody else. Not even bot admins, who have ``/dskip``
instead: unsticking a draft is a visible admin action, recorded as an auto-pick,
rather than a pick passed off as the owner's own choice.

Command surface
---------------
Players (in the draft group)
  /pick <player>      make the pick that is on the clock (alias /pk)
  /dboard             the live board, with buttons for order / pool / squads
  /dsearch [filters]  browse the pool — 🟢 available / 🔴 taken, with filter buttons
  /dsquad [team]      a squad by tier, with slot progress
  /dqueue <player>    your wishlist, which the clock picks from if you time out
  /dtrade <team>      once the draft is over: swap players with another
                      franchise — any player for any player, no rating rule
                      (handlers/draft_trade.py)
  /dtrades            every trade that has been done

Admin
  /dadmin /dnew /dbind /dstart /dpause /dtimer /dhome /dpin /dco /dskip /dundo
  /dpublish /dtradelock
  /dadd <team> | <player>   put a player on a squad (or move him to it)
  /ddrop <player>           send a player back to the pool

Owner
  /dautopick          grant the team on the clock a random pick of its tier

Callback prefixes are all ``dr_`` (``dr_view_`` for the board's tabs,
``dr_pick_`` for the disambiguation buttons, ``dr_srch_`` for the pool
browser), and every one of them is **owner-locked to whoever ran the command
that posted it**. A draft group is a busy room: the pool browser re-filters in
place, so a second pair of hands on it fights the first; the board's tabs post a
fresh message each press, so a stranger driving yours is noise in the one chat
that has to stay readable; and a pick is irreversible. The lock is stateless — each button carries its owner's
Telegram id in its own callback data (``services.button_access.tag_owner``), so
it holds across a restart rather than lapsing the moment the process that sent
the message goes away. ``OWNER_RULES`` there also gives each prefix a refusal
that names the command to run instead.

Ownership follows the reader, not the message: pressing 🔎 Search on somebody
else's board is impossible, but a search view opened from your own board is
yours, and the filters ride in the callback data so it keeps working hours
later. None of this replaces the per-pick authorisation — ``dr_pick_`` is still
re-checked against the team on the clock on every press.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from models import DraftPlayer, DraftTeam
from services import button_access as ba
from services import draft_service as ds
from services import draft_scheduler as dsched
from services.admin_ids import is_admin, is_owner
from services.draft_service import DraftError

logger = logging.getLogger(__name__)

GROUP_CHAT_TYPES = ("group", "supergroup")

NOT_ADMIN = "⛔ Only bot admins can manage a draft."
NOT_OWNER = ("⛔ Only the bot owner can grant a pick with <code>/dautopick</code>. "
             "Admins have <code>/dskip</code>.")
GROUP_ONLY = ("❌ Draft commands only work in the group the draft is bound to.\n"
              "An admin binds one with <code>/dbind</code>.")
NO_DRAFT = ("❌ No draft is running in this chat.\n"
            "An admin can create one with <code>/dnew &lt;name&gt;</code> and "
            "bind it here with <code>/dbind</code>.")


# ════════════════════════════════════════════════════════════════════
# Shared plumbing
# ════════════════════════════════════════════════════════════════════

async def _reply(update, text, **kwargs):
    """Reply with this module's defaults: HTML, and no link previews."""
    msg = update.effective_message
    if msg is None:
        return None
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    return await msg.reply_text(text, **kwargs)


def _arg_text(context):
    return " ".join(context.args or []).strip()


def _split_pipes(text, count):
    parts = [p.strip() for p in (text or "").split("|")]
    parts += [""] * (count - len(parts))
    return parts[:count]


def _parse_tg_id(token):
    token = (token or "").strip().lstrip("@")
    try:
        value = int(token)
    except ValueError:
        return None
    return value if value > 0 else None


async def _require_admin(update):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _reply(update, NOT_ADMIN)
        return False
    return True


async def _require_owner(update):
    """Owner-only, a strictly narrower gate than ``_require_admin``.

    ``/dautopick`` hands a team a player nobody on that team chose. That is a
    heavier thing than unsticking the clock, so it sits with the bot owner
    rather than with every admin.
    """
    user = update.effective_user
    if not user or not is_owner(user.id):
        await _reply(update, NOT_OWNER)
        return False
    return True


def _load_for_chat(session, update):
    """The draft bound to this chat, or None."""
    chat = update.effective_chat
    if chat is None:
        return None
    return ds.draft_for_chat(session, chat.id)


async def _with_draft(update, work, *, admin=False, allow_dm=False,
                      after=None, context=None):
    """Run ``work(session, draft)`` against this chat's draft.

    Centralises what every command has to do: the group gate, the admin check,
    finding the bound draft, turning a ``DraftError`` into a plain reply, and
    closing the session. ``work`` returns the reply text and may mutate — the
    commit happens here, only if ``work`` returns without raising.

    ``after`` is an optional coroutine ``after(bot, session, draft)`` run once
    the commit has gone through, for the commands whose effect reaches Telegram
    as well as the database — dropping the draft's pin, say. It runs before the
    session closes and its failure never un-does the commit.
    """
    chat = update.effective_chat
    if not allow_dm and (chat is None or chat.type not in GROUP_CHAT_TYPES):
        await _reply(update, GROUP_ONLY)
        return
    if admin and not await _require_admin(update):
        return
    session = get_session()
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        text = work(session, draft)
        session.commit()
        if after is not None and context is not None:
            try:
                await after(context.bot, session, draft)
            except Exception:
                logger.exception("Draft command follow-up failed")
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("Draft command failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    if text:
        await _reply(update, text)


# ════════════════════════════════════════════════════════════════════
# /pick — the whole point
# ════════════════════════════════════════════════════════════════════

async def pick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    user = update.effective_user
    if user is None:
        return
    query = _arg_text(context)

    session = get_session()
    sent = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        if draft.status != ds.STATUS_LIVE:
            await _reply(update, f"⚠️ This draft is {ds.status_label(draft)} — "
                                 f"no picks can be made right now.")
            return

        pick = ds.current_pick(session, draft)
        if pick is None:
            await _reply(update, "🏁 Every slot is filled — the draft is done.")
            return
        team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
        if not ds.may_pick_for(team, user.id):
            mine = ds.team_for_actor(session, draft.id, user.id)
            if mine is None:
                await _reply(update, "⛔ You don't own a team in this draft.")
            else:
                await _reply(update,
                             f"⛔ It's <b>{html.escape(team.name if team else '')}</b>'s "
                             f"pick, not {html.escape(mine.name)}'s. "
                             f"{dsched.team_mention(session, team)} is on the clock.")
            return

        if not query:
            await _reply(update,
                         "Usage: <code>/pick &lt;player name&gt;</code>\n"
                         f"You're picking for <b>{html.escape(team.name)}</b> — "
                         f"R{pick.round_no} P{pick.pick_no}, "
                         f"{ds.tier_badge(pick.tier)} slot or below.")
            return

        player, candidates = ds.find_available(session, draft.id, query)
        if player is None:
            await _reply(update, _ambiguous_text(query, candidates),
                         reply_markup=_ambiguous_keyboard(pick, candidates,
                                                          user.id))
            return

        done = ds.make_pick(session, draft, pick, player, by_tg_id=user.id)
        session.commit()
        sent = (draft, done, player)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/pick failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        if sent is None:
            session.close()

    # Announce outside the try/finally above so a Telegram failure can never
    # roll back a pick that is already committed.
    try:
        draft, done, player = sent
        await dsched.announce_pick(context.bot, session, draft, done, player)
        if draft.status == ds.STATUS_COMPLETED:
            await _reply(update, "🏁 <b>Draft complete.</b> Every slot is filled — "
                                 "an admin can publish the squads with /dpublish.\n"
                                 "The <b>trade window is open</b>: swap players "
                                 "with another franchise using "
                                 "<code>/dtrade &lt;team&gt;</code> — any player "
                                 "for any player, no rating rule.")
    finally:
        session.close()


def _ambiguous_text(query, candidates):
    if not candidates:
        return (f"❌ No available player matches “{html.escape(query)}”.\n"
                f"Check /dboard → <b>Available</b> for who's left.")
    if len(candidates) > 5:
        return (f"🔎 {len(candidates)} available players match "
                f"“{html.escape(query)}”. Type more of the name.")
    return (f"🔎 “{html.escape(query)}” matches more than one player — "
            f"tap the one you mean:")


PICK_CB = "dr_pick_"


def _ambiguous_keyboard(pick, candidates, owner_id):
    """The "did you mean" buttons, locked to the owner who typed the name."""
    if not candidates or len(candidates) > 5:
        return None
    head = ba.tag_owner(PICK_CB, owner_id)
    rows = [[InlineKeyboardButton(
        f"{p.name} · {p.rating} · {p.tier}",
        callback_data=f"{head}_{pick.id}_{p.id}")] for p in candidates]
    return InlineKeyboardMarkup(rows)


async def pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One of the disambiguation buttons. Re-checks everything /pick checks."""
    query = update.callback_query
    if query is None:
        return
    # The owner tag is checked by the button guard before this ever runs; the
    # team check below is the authorisation that matters and stays either way.
    _owner, rest = ba.split_owner(PICK_CB, query.data, "_")
    try:
        pick_id, player_id = (int(part) for part in rest.split("_", 1))
    except (ValueError, AttributeError):
        await query.answer("That button is out of date.", show_alert=True)
        return

    session = get_session()
    sent = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None or draft.status != ds.STATUS_LIVE:
            await query.answer("This draft isn't taking picks right now.",
                               show_alert=True)
            return
        pick = ds.current_pick(session, draft)
        if pick is None or pick.id != pick_id:
            await query.answer("That pick has already been made.", show_alert=True)
            return
        team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
        if not ds.may_pick_for(team, query.from_user.id):
            await query.answer("That's not your team's pick.", show_alert=True)
            return
        player = (session.query(DraftPlayer)
                  .filter(DraftPlayer.id == player_id,
                          DraftPlayer.draft_id == draft.id).first())
        done = ds.make_pick(session, draft, pick, player,
                            by_tg_id=query.from_user.id)
        session.commit()
        sent = (draft, done, player)
        await query.answer("Picked.")
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    except DraftError as exc:
        session.rollback()
        await query.answer(str(exc)[:190], show_alert=True)
        return
    except Exception:
        session.rollback()
        logger.exception("draft pick callback failed")
        await query.answer("Something went wrong.", show_alert=True)
        return
    finally:
        if sent is None:
            session.close()

    try:
        draft, done, player = sent
        await dsched.announce_pick(context.bot, session, draft, done, player)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Readouts
# ════════════════════════════════════════════════════════════════════

BOARD_CB = "dr_view_"


def _board_keyboard(owner_id):
    """The board's tabs, locked to the owner who asked for this board."""
    head = ba.tag_owner(BOARD_CB, owner_id)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📋 Order", callback_data=f"{head}_order"),
        InlineKeyboardButton("📦 Available", callback_data=f"{head}_pool"),
        InlineKeyboardButton("👥 Teams", callback_data=f"{head}_teams"),
    ], [
        # The pool browser, opened with no filters — the board is where people
        # already are when they wonder whether somebody is still there. It
        # arrives as its own message rather than replacing the board, because
        # the board is what the room is watching.
        InlineKeyboardButton("🔎 Search the pool",
                             callback_data=f"{head}_search"),
    ]])


def _board_view(callback_data):
    """Which tab a board button names, owner tag stripped."""
    _owner, rest = ba.split_owner(BOARD_CB, callback_data, "_")
    return rest


async def dboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Not routed through _with_draft: the board carries a keyboard.
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        pick = ds.current_pick(session, draft)
        team = None
        if pick is not None:
            team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
        text = ds.render_board(session, draft,
                               mention=dsched.team_mention(session, team))
    except Exception:
        # There is no global PTB error handler, so an unhandled failure here
        # would leave the command looking ignored rather than broken.
        logger.exception("/dboard failed")
        await _reply(update, "⚠️ Couldn't build the board. Try again.")
        return
    finally:
        session.close()
    user = update.effective_user
    await _reply(update, text,
                 reply_markup=_board_keyboard(user.id if user else None))


async def board_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The board's tabs — only for the owner whose /dboard posted them.

    The guard in ``services.button_access`` has already turned everyone else
    away by the time this runs, so the presser here is the board's owner and
    anything opened from a tab is opened in their name.
    """
    query = update.callback_query
    if query is None:
        return
    view = _board_view(query.data)
    presser = getattr(query.from_user, "id", None)
    session = get_session()
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await query.answer("No draft is running here.", show_alert=True)
            return
        keyboard = None
        if view == "order":
            text = ds.render_order(session, draft)
        elif view == "pool":
            text = ds.render_available(session, draft)
        elif view == "teams":
            text = ds.render_teams(session, draft)
        elif view == "search":
            text, keyboard = _search_view(
                session, draft, _search_state(draft, owner=presser))
        else:
            await query.answer()
            return
    except Exception:
        logger.exception("draft board view failed")
        await query.answer("Something went wrong.", show_alert=True)
        return
    finally:
        session.close()
    await query.answer()
    await query.message.reply_text(text, parse_mode="HTML",
                                   disable_web_page_preview=True,
                                   reply_markup=keyboard)


# ════════════════════════════════════════════════════════════════════
# /dsearch — the pool browser
# ════════════════════════════════════════════════════════════════════
#
# The board's 📦 Available tab lists the best of what's left. This answers the
# other question an owner asks all evening — "is *he* still there, and if not,
# who took him" — with filters that survive a tap, so nobody has to retype
# ``/dsearch platinum bowler`` to turn one page.
#
# Callback data is ``dr_srch_u<owner>~<tier>~<role>~<avail>~<home>~<page>~<query>``.
# Indices, not names: Telegram allows 64 bytes, and the owner id and a player's
# name both have to fit in there too — hence the truncation in _encode_search.

SEARCH_CB = "dr_srch_"
_ROLE_LABEL = {"Batsman": "🏏 Bat", "Bowler": "🎯 Bowl",
               "All-rounder": "⚡ AR", "Wicket Keeper": "🧤 WK"}


def _search_state(draft, tier=None, role=None, availability=ds.AVAIL_ANY,
                  home=None, page=0, query="", owner=None):
    return {"tier": tier, "role": role, "availability": availability,
            "home": home, "page": page, "query": query, "owner": owner}


def _encode_search(draft, state, **overrides):
    """One button's callback data, short enough for Telegram's 64 bytes."""
    merged = dict(state)
    merged.update(overrides)
    tiers, roles = ds.tier_order(draft), list(ds._CATEGORIES)
    tier = str(tiers.index(merged["tier"])) if merged["tier"] in tiers else ""
    role = str(roles.index(merged["role"])) if merged["role"] in roles else ""
    home = "" if merged["home"] is None else ("1" if merged["home"] else "0")
    head = (f"{ba.tag_owner(SEARCH_CB, merged.get('owner'))}"
            f"~{tier}~{role}~{merged['availability']}~{home}"
            f"~{int(merged['page'])}~")
    # Whatever is left of the 64 bytes belongs to the name being searched for.
    room = 64 - len(head.encode())
    query = merged.get("query") or ""
    while query and len(query.encode()) > room:
        query = query[:-1]
    return head + query


def _decode_search(draft, data):
    """The state a pressed button carries, or ``None`` if it is not ours."""
    if not (data or "").startswith(SEARCH_CB):
        return None
    owner, rest = ba.split_owner(SEARCH_CB, data, "~")
    parts = rest.split("~", 5)
    if len(parts) < 6:
        return None
    tier_raw, role_raw, availability, home_raw, page_raw, query = parts
    tiers, roles = ds.tier_order(draft), list(ds._CATEGORIES)
    return _search_state(
        draft,
        owner=owner,
        tier=tiers[int(tier_raw)] if tier_raw.isdigit() and int(tier_raw) < len(tiers) else None,
        role=roles[int(role_raw)] if role_raw.isdigit() and int(role_raw) < len(roles) else None,
        availability=availability if availability in (ds.AVAIL_ANY, ds.AVAIL_FREE,
                                                      ds.AVAIL_GONE) else ds.AVAIL_ANY,
        home={"1": True, "0": False}.get(home_raw),
        page=int(page_raw) if page_raw.isdigit() else 0,
        query=query)


def _mark(label, active):
    """An active filter says so on the button — there is no other place to."""
    return f"✅ {label}" if active else label


def _search_keyboard(draft, state, pages):
    rows = []

    tier_buttons = [InlineKeyboardButton(
        _mark("All", state["tier"] is None),
        callback_data=_encode_search(draft, state, tier=None, page=0))]
    for tier in ds.tier_order(draft):
        tier_buttons.append(InlineKeyboardButton(
            _mark(f"{ds.tier_emoji(tier)} {tier}", state["tier"] == tier),
            callback_data=_encode_search(draft, state, tier=tier, page=0)))
    for start in range(0, len(tier_buttons), 3):
        rows.append(tier_buttons[start:start + 3])

    role_buttons = [InlineKeyboardButton(
        _mark("All", state["role"] is None),
        callback_data=_encode_search(draft, state, role=None, page=0))]
    for role, label in _ROLE_LABEL.items():
        role_buttons.append(InlineKeyboardButton(
            _mark(label, state["role"] == role),
            callback_data=_encode_search(draft, state, role=role, page=0)))
    for start in range(0, len(role_buttons), 3):
        rows.append(role_buttons[start:start + 3])

    rows.append([
        InlineKeyboardButton(
            _mark("🟢 Available", state["availability"] == ds.AVAIL_FREE),
            callback_data=_encode_search(draft, state,
                                         availability=ds.AVAIL_FREE, page=0)),
        InlineKeyboardButton(
            _mark("🔴 Taken", state["availability"] == ds.AVAIL_GONE),
            callback_data=_encode_search(draft, state,
                                         availability=ds.AVAIL_GONE, page=0)),
        InlineKeyboardButton(
            _mark("Both", state["availability"] == ds.AVAIL_ANY),
            callback_data=_encode_search(draft, state,
                                         availability=ds.AVAIL_ANY, page=0)),
    ])

    home_flag = ds.home_flag(draft)
    rows.append([
        InlineKeyboardButton(
            _mark(f"{home_flag} Home", state["home"] is True),
            callback_data=_encode_search(draft, state, home=True, page=0)),
        InlineKeyboardButton(
            _mark(f"{ds.OVERSEAS_FLAG} Overseas", state["home"] is False),
            callback_data=_encode_search(draft, state, home=False, page=0)),
        InlineKeyboardButton(
            _mark("Both", state["home"] is None),
            callback_data=_encode_search(draft, state, home=None, page=0)),
    ])

    if pages > 1:
        page = state["page"]
        rows.append([
            InlineKeyboardButton(
                "◀️", callback_data=_encode_search(draft, state,
                                                   page=(page - 1) % pages)),
            InlineKeyboardButton(f"{page + 1}/{pages}",
                                 callback_data=_encode_search(draft, state)),
            InlineKeyboardButton(
                "▶️", callback_data=_encode_search(draft, state,
                                                   page=(page + 1) % pages)),
        ])
    return InlineKeyboardMarkup(rows)


def _search_view(session, draft, state):
    """``(text, keyboard)`` for one browser state."""
    rows = ds.search_pool(session, draft, tier=state["tier"], role=state["role"],
                          availability=state["availability"],
                          query=state["query"], home=state["home"])
    text, page, pages = ds.render_search(
        session, draft, rows, tier=state["tier"], role=state["role"],
        availability=state["availability"], home=state["home"],
        query=state["query"], page=state["page"])
    state["page"] = page
    if _unfiltered(state):
        # What is left per tier, which is the first thing anyone opening the
        # browser wants and is noise once they have narrowed it down.
        text = _tier_summary_line(session, draft) + text
    return text, _search_keyboard(draft, state, pages)


def _unfiltered(state):
    return (not any((state["tier"], state["role"], state["query"]))
            and state["home"] is None
            and state["availability"] == ds.AVAIL_ANY)


async def dsearch_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Browse the pool: <code>/dsearch platinum bowler available</code>.

    The words can come in any order and anything the parser doesn't recognise
    is treated as the name to look for, because an owner three seconds before
    their pick types what they mean, not a query language.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        tier, role, availability, home, name = ds.parse_search_query(
            draft, _arg_text(context))
        asker = update.effective_user
        state = _search_state(draft, tier=tier, role=role,
                              availability=availability, home=home, query=name,
                              owner=asker.id if asker else None)
        rows = ds.search_pool(session, draft, tier=tier, role=role,
                              availability=availability, query=name, home=home)
        # One hit for a typed name is a question about that player, not a list.
        if name and len(rows) == 1:
            await _reply(update, ds.render_search_one(session, draft, rows[0]))
            return
        text, keyboard = _search_view(session, draft, state)
    except Exception:
        logger.exception("/dsearch failed")
        await _reply(update, "⚠️ Couldn't search the pool. Try again.")
        return
    finally:
        session.close()
    await _reply(update, text, reply_markup=keyboard)


def _tier_summary_line(session, draft):
    """``💎 4/8 · 🥇 6/12`` — what is left per tier, above an unfiltered list."""
    parts = [f"{ds.tier_emoji(tier)} {free}/{total}"
             for tier, free, total in ds.tier_summary(session, draft)]
    return f"{' · '.join(parts)}\n" if parts else ""


async def search_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A filter or page button — only for the owner whose /dsearch posted it.

    The owner rides in the callback data and is re-encoded into every button of
    the re-rendered keyboard, so the browser stays theirs for as long as the
    message lives, not just for as long as this process does.
    """
    query = update.callback_query
    if query is None:
        return
    session = get_session()
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await query.answer("No draft is running here.", show_alert=True)
            return
        state = _decode_search(draft, query.data)
        if state is None:
            await query.answer("That button is out of date.", show_alert=True)
            return
        text, keyboard = _search_view(session, draft, state)
    except Exception:
        logger.exception("draft search callback failed")
        await query.answer("Something went wrong.", show_alert=True)
        return
    finally:
        session.close()
    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode="HTML",
                                      disable_web_page_preview=True,
                                      reply_markup=keyboard)
    except Exception:
        # "Message is not modified" when a filter is pressed twice, and the
        # message can be too old to edit. Neither is worth an error to the room.
        logger.debug("draft search edit skipped", exc_info=True)


async def dsquad_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        wanted = _arg_text(context)
        user = update.effective_user
        team = (ds.find_team(session, draft.id, wanted) if wanted
                else ds.team_for_actor(session, draft.id, user.id if user else None))
        if team is None:
            if wanted:
                raise DraftError(f"No team here matches “{wanted}”.")
            raise DraftError("You don't own a team in this draft — name one, "
                             "e.g. /dsquad Mumbai.")
        body = ds.render_squad(session, draft, team)
        # The squad readout is where an owner is standing when they notice they
        # have three keepers and no death bowler, so it is where the trade
        # window is worth advertising. /dtrade is not in the group slash menu —
        # that list is at Telegram's ceiling — so this pointer is how most
        # owners find it.
        from services import draft_trade_service as dts
        if draft.status == ds.STATUS_COMPLETED and dts.trades_open(draft):
            body += ("\n\n🔁 Trade window open — "
                     "<code>/dtrade &lt;team&gt;</code> swaps players with "
                     "another franchise (any player for any player). "
                     "Done so far: /dtrades")
        return body
    await _with_draft(update, work)


async def dqueue_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The owner's wishlist, which the clock picks from if their time runs out."""
    def work(session, draft):
        user = update.effective_user
        team = ds.team_for_actor(session, draft.id, user.id if user else None)
        if team is None:
            raise DraftError("You don't own a team in this draft.")
        arg = _arg_text(context)
        if arg.lower() in ("clear", "reset", "empty"):
            ds.queue_clear(team)
            return "🗑 Queue cleared."
        if not arg:
            rows = ds.queue_players(session, team)
            if not rows:
                return ("Your queue is empty. Add players with "
                        "<code>/dqueue &lt;player name&gt;</code> — if your clock "
                        "runs out, the bot picks the first one that's still "
                        "legal for your squad.")
            lines = [f"📝 <b>{html.escape(team.name)}</b> — queue"]
            for index, row in enumerate(rows, start=1):
                gone = "" if row.picked_by_team_id is None else " <i>(gone)</i>"
                lines.append(f"{index}. {ds.tier_emoji(row.tier)} "
                             f"{html.escape(row.name or '')} "
                             f"<code>{row.rating}</code>{gone}")
            return "\n".join(lines)
        player, candidates = ds.find_available(session, draft.id, arg)
        if player is None:
            if not candidates:
                raise DraftError(f"No available player matches “{arg}”.")
            raise DraftError("That matches more than one player: "
                             + ", ".join(c.name for c in candidates[:5]))
        ds.queue_add(session, draft, team, player)
        return (f"📝 Queued <b>{html.escape(player.name)}</b> "
                f"({ds.tier_badge(player.tier)}) for "
                f"<b>{html.escape(team.name)}</b>.")
    await _with_draft(update, work)


# ════════════════════════════════════════════════════════════════════
# Admin
# ════════════════════════════════════════════════════════════════════

ADMIN_HELP = """🎯 <b>Tournament Draft — admin reference</b>

<b>Setting one up</b>
<code>/dnew Summer Mega Draft</code> — create it and bind it to this group
<code>/dbind</code> — bind (or re-bind) the draft to the chat you type it in
Upload the <b>player pool</b> and the <b>draft order</b> on the website:
Admin → Tournament Panel → 🎯 Player Drafts → your draft.

<b>Running it</b>
<code>/dtimer 15</code> — minutes per pick (default 15)
<code>/dhome England</code> — set the home country and re-flag the whole pool
<code>/dpin on|off</code> — pin the latest pick at the top of the group (on by default)
<code>/dco Mumbai | 123456789</code> — add a co-owner who may also /pick
<code>/dstart</code> <code>/dpause</code> <code>/dresume</code> — lifecycle
<code>/dskip</code> — resolve the pick on the clock right now, without waiting
<code>/dautopick</code> — <b>owner only</b>: grant the team on the clock a random
  player from its allotted tier (for a squad whose owner is offline)
<code>/dundo</code> — roll the last pick back onto the clock
<code>/dcancel</code> — stop the draft

<b>Editing a squad by hand</b>
<code>/dadd Mumbai | Virat Kohli</code> — put a player on a squad (moves him if
  another team has him)
<code>/ddrop Virat Kohli</code> — send a player back to the pool
These two enforce <b>no</b> squad rule — an admin has to be able to pass through
an illegal squad to fix one. Instead every edit is announced here, recorded in
<code>/dtrades</code>, and followed by the full rule state of each squad it
touched.

<b>Finishing</b>
<code>/dpublish</code> — write the squads into a Challenge League so they can play
<code>/dtradelock on|off</code> — close or reopen the post-draft trade window
  (<code>/dtrade</code>). Close it before a match day so a team sheet cannot
  change under a live fixture.

<b>The rules the bot enforces</b>
• A slot's tier is a <b>ceiling</b> — a Platinum slot takes Platinum or below.
  Picking below spends the slot; there is no refund.
• A player is <b>home</b> when their pool row's country is the draft's home
  country, and <b>overseas</b> otherwise — the sheet's country column decides.
  Check it with <code>/dhome</code>.
• The overseas cap and any role minimums are checked on every pick, and a pick
  that would make a role minimum unreachable is refused while it can still be fixed.
• Only the Owner Tag ID and co-owners may pick for a team. Admins use /dskip.
• When a clock runs out the bot picks: the owner's /dqueue first, otherwise the
  player closest to the average rating of what's legal in that tier.
• Once the draft is <b>complete</b>, owners may trade with <code>/dtrade</code>.
  There is <b>no rating rule</b> — any player for any player — but the counts
  must match and the squad rules above (tier slots, the overseas cap, role
  minimums) are re-checked on both squads before anybody moves."""


async def dadmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    await _reply(update, ADMIN_HELP)


async def dnew_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a draft and bind it to this group in one step."""
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, "❌ Run /dnew in the group the draft should live in.")
        return
    name = _arg_text(context)
    if not name:
        await _reply(update, "Usage: <code>/dnew &lt;draft name&gt;</code>")
        return
    session = get_session()
    try:
        existing = ds.draft_for_chat(session, chat.id)
        if existing is not None:
            await _reply(update, f"⚠️ This chat already runs the draft "
                                 f"“{html.escape(existing.name)}”. Unbind it first "
                                 f"from the website, or use that one.")
            return
        draft = ds.create_draft(session, name)
        ds.bind_chat(session, draft, chat.id)
        session.commit()
        text = (f"🎯 Created <b>{html.escape(draft.name)}</b> and bound it to this "
                f"chat.\n\nNext: upload the player pool and the draft order on the "
                f"website (Tournament Panel → 🎯 Player Drafts), then /dstart.\n"
                f"Pick clock is {draft.pick_seconds // 60} min — change it with "
                f"/dtimer.")
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/dnew failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    await _reply(update, text)


async def dbind_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Point an existing draft (by id) at this chat."""
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, "❌ Run /dbind in the group the draft should live in.")
        return
    draft_id = _parse_tg_id(_arg_text(context))
    session = get_session()
    try:
        if draft_id:
            draft = ds.get_draft(session, draft_id)
        else:
            draft = ds.draft_for_chat(session, chat.id)
        if draft is None:
            await _reply(update, "❌ No such draft. <code>/dbind &lt;id&gt;</code> — "
                                 "the id is on the website's Player Drafts page.")
            return
        ds.bind_chat(session, draft, chat.id)
        session.commit()
        text = (f"🔗 <b>{html.escape(draft.name)}</b> is now bound to this chat. "
                f"Picks happen here.")
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    finally:
        session.close()
    await _reply(update, text)


async def dtimer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        arg = _arg_text(context)
        if not arg:
            return (f"⏱ Pick clock is <b>{draft.pick_seconds // 60} min</b> "
                    f"({draft.pick_seconds}s). Change it with "
                    f"<code>/dtimer &lt;minutes&gt;</code>.")
        try:
            minutes = float(arg)
        except ValueError:
            raise DraftError("Give the clock in minutes, e.g. /dtimer 15.")
        draft.pick_seconds = ds.clamp_pick_seconds(int(minutes * 60))
        # Re-arm the running clock so the change takes effect on the pick that
        # is on it, rather than only on the next one.
        if draft.status == ds.STATUS_LIVE and ds.current_pick(session, draft):
            ds.advance(session, draft)
        return (f"⏱ Pick clock set to <b>{draft.pick_seconds // 60} min</b> "
                f"({draft.pick_seconds}s).")
    await _with_draft(update, work, admin=True)


async def dhome_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show or set the draft's home country, re-flagging the pool against it.

    A player is home when their **pool row's country** is this country, and
    overseas otherwise. Running it on a live draft is the supported way to fix a
    pool that was flagged under a different home country — the pool itself can't
    be re-uploaded once picking has started.
    """
    def work(session, draft):
        arg = _arg_text(context)
        if arg.lower() in ("sync", "resync", "refresh"):
            arg = draft.home_country or ds.DEFAULT_HOME_COUNTRY
        if not arg:
            home, overseas, unknown, wrong = ds.home_status_counts(session, draft)
            lines = [f"{ds.home_flag(draft)} Home country: "
                     f"<b>{html.escape(draft.home_country or ds.DEFAULT_HOME_COUNTRY)}</b>",
                     f"Pool: {ds.home_flag(draft)} {home} home · "
                     f"{ds.OVERSEAS_FLAG} {overseas} overseas "
                     f"· max {draft.max_overseas} overseas per squad"]
            if unknown:
                lines.append(f"⚠️ {unknown} player(s) have no country the bot can "
                             f"read — their flag is left as it is.")
            if wrong:
                lines.append(f"⚠️ {wrong} player(s) are flagged against a different "
                             f"country than their pool row says. "
                             f"<code>/dhome sync</code> fixes them.")
            lines.append("\nChange it with <code>/dhome &lt;country&gt;</code> — "
                         "the whole pool is re-flagged from its country column.")
            return "\n".join(lines)

        country, changed, unknown = ds.set_home_country(session, draft, arg)
        home, overseas, _unknown, _wrong = ds.home_status_counts(session, draft)
        out = [f"{ds.home_flag(draft)} Home country set to "
               f"<b>{html.escape(country)}</b>.",
               f"Re-flagged <b>{changed}</b> player(s) — the pool is now "
               f"{home} home · {ds.OVERSEAS_FLAG} {overseas} overseas."]
        if unknown:
            out.append(f"⚠️ {unknown} player(s) have no readable country and were "
                       f"left alone.")
        out.append("<i>Squads already picked keep their players; the overseas cap "
                   "is counted from the new flags on every pick from here.</i>")
        return "\n".join(out)
    await _with_draft(update, work, admin=True)


async def dco_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add (or list) the co-owners allowed to pick for a team."""
    def work(session, draft):
        team_name, token = _split_pipes(_arg_text(context), 2)
        if not team_name:
            lines = ["👥 <b>Co-owners</b>"]
            for team in ds.teams(session, draft.id):
                ids = ds.co_owner_ids(team)
                lines.append(f"• {html.escape(team.name)} — owner "
                             f"<code>{team.owner_tg_id or '—'}</code>"
                             + (f", co: {', '.join(str(i) for i in ids)}" if ids else ""))
            lines.append("\nAdd one with "
                         "<code>/dco &lt;team&gt; | &lt;telegram id&gt;</code>")
            return "\n".join(lines)
        team = ds.find_team(session, draft.id, team_name)
        if team is None:
            raise DraftError(f"No team here matches “{team_name}”.")
        tg_id = _parse_tg_id(token)
        if not tg_id:
            raise DraftError("Give the co-owner's numeric Telegram id, e.g. "
                             "/dco Mumbai | 123456789.")
        ds.add_co_owner(session, team, tg_id)
        return (f"✅ <code>{tg_id}</code> can now pick for "
                f"<b>{html.escape(team.name)}</b>.")
    await _with_draft(update, work, admin=True)


async def dstart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    opened = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        pick = ds.resume(session, draft) if draft.status == ds.STATUS_PAUSED \
            else ds.start(session, draft)
        session.commit()
        opened = (draft, pick)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/dstart failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        if opened is None:
            session.close()
    try:
        draft, pick = opened
        await dsched.announce_turn(
            context.bot, session, draft, pick,
            prefix=f"🎯 <b>{html.escape(draft.name)}</b> is under way!\n"
                   f"Pick clock: {draft.pick_seconds // 60} min.\n\n")
    finally:
        session.close()


async def dpause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        ds.pause(session, draft)
        return ("⏸ <b>Draft paused.</b> The clock is stopped and no picks can be "
                "made until an admin runs /dstart.")
    await _with_draft(update, work, admin=True)


async def dcancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        ds.set_status(session, draft, ds.STATUS_CANCELLED)
        return "🚫 <b>Draft cancelled.</b>"
    # The pinned pick outlives the draft otherwise, still sitting at the top of
    # a group whose draft is over.
    await _with_draft(update, work, admin=True, context=context,
                      after=dsched.unpin_latest)


async def dpin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Turn the auto-pinned latest pick on or off for this draft."""
    wanted = None

    def work(session, draft):
        nonlocal wanted
        arg = _arg_text(context).lower()
        if not arg:
            state = "on" if dsched.pinning_on(draft) else "off"
            return (f"📌 Pinning the latest pick is <b>{state}</b>.\n"
                    f"Every pick replaces the previous pin, so the top of the "
                    f"chat always shows where the draft is.\n"
                    f"Change it with <code>/dpin on</code> or "
                    f"<code>/dpin off</code>.")
        if arg in ("on", "yes", "enable", "enabled", "1", "true"):
            draft.pin_picks = True
            return ("📌 The latest pick will be pinned from now on. The bot needs "
                    "the <b>Pin Messages</b> admin right in this group.")
        if arg in ("off", "no", "disable", "disabled", "0", "false"):
            draft.pin_picks = False
            wanted = "unpin"
            return "📌 Picks will no longer be pinned. The current pin is cleared."
        raise DraftError("Say /dpin on or /dpin off.")

    async def after(bot, session, draft):
        if wanted == "unpin":
            await dsched.unpin_latest(bot, session, draft)

    await _with_draft(update, work, admin=True, context=context, after=after)


async def _resolve_on_the_clock(update, context, resolve, *, command):
    """Resolve the slot on the clock with ``resolve(session, draft, pick, ...)``.

    Shared by ``/dskip`` and ``/dautopick``: the two differ only in who may run
    them and in which player the slot ends up with. Both announce exactly like a
    typed pick — the ⏱ badge ``render_pick`` adds is the only difference, and it
    is the whole point: the board must never read as the owner's own choice.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    resolved = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        if draft.status != ds.STATUS_LIVE:
            await _reply(update, f"⚠️ This draft is {ds.status_label(draft)}.")
            return
        pick = ds.current_pick(session, draft)
        if pick is None:
            await _reply(update, "🏁 Every slot is already filled.")
            return
        done, player = resolve(session, draft, pick,
                               by_tg_id=update.effective_user.id)
        session.commit()
        resolved = (draft, done, player)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("%s failed", command)
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        if resolved is None:
            session.close()
    try:
        draft, done, player = resolved
        if player is None:
            await dsched.announce_skip(context.bot, session, draft, done)
        else:
            await dsched.announce_pick(context.bot, session, draft, done, player)
    finally:
        session.close()


async def dskip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve the pick on the clock now, exactly as the timer would.

    The admin's way to unstick a draft. It is recorded as an auto-pick, because
    that is what it is — it must never read as the owner's own choice.
    """
    if not await _require_admin(update):
        return
    await _resolve_on_the_clock(update, context, ds.resolve_expired,
                                command="/dskip")


async def dautopick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant the team on the clock a random player from its allotted tier.

    For a squad whose owner isn't in the room: they still get a player of the
    tier their slot is worth, drawn at random from what is legal for them rather
    than from ``auto_pick``'s deterministic middle-of-the-band — run that over
    every absent team and they all end up with the same shape of squad.

    Owner-only, and one slot per command: granting picks is not something to do
    by accident, and the room sees each one land.
    """
    if not await _require_owner(update):
        return
    await _resolve_on_the_clock(update, context, ds.resolve_random,
                                command="/dautopick")


async def dundo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    session = get_session()
    undone = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        last, player = ds.undo_last(session, draft)
        session.commit()
        undone = (draft, last, player)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/dundo failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        if undone is None:
            session.close()
    try:
        draft, last, player = undone
        # The pin is the pick that just stopped existing.
        await dsched.unpin_latest(context.bot, session, draft)
        freed = (f"<b>{html.escape(player.name)}</b> is back in the pool."
                 if player is not None else "That slot had been passed.")
        await dsched.announce_turn(
            context.bot, session, draft, ds.current_pick(session, draft),
            prefix=f"↩️ <b>Undone:</b> R{last.round_no} P{last.pick_no}. "
                   f"{freed}\n\n")
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# /dadd and /ddrop — the admin's hands on a squad
# ════════════════════════════════════════════════════════════════════
#
# Every other way a player joins a squad is gated: /pick obeys the tier
# ceiling, the overseas cap and the role minimums; /dtrade re-checks all three
# against the squad the trade would produce. These two obey **nothing**, and
# that is the point — an admin untangling a mess has to be able to pass through
# an illegal squad to reach a legal one (drop the extra keeper, then add the
# quick), and a gate that refuses the first half makes the tool useless at
# exactly the moment it is needed.
#
# What stands in for the gate is the report. Each command announces itself in
# the draft group (a squad nobody remembers agreeing to is how a league gets
# disputed), writes a DraftSquadEdit row, and prints the full rule state of
# every squad it touched — so an admin sees what they have just broken, or
# fixed, without running /dsquad twice.

async def _squad_edit(update, context, work, *, command):
    """Run one admin squad edit and announce it, with both squads' rule state.

    ``work(session, draft)`` returns ``(edit, teams)``. Shared by /dadd and
    /ddrop because the whole of the difference between them is that one line.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    if not await _require_admin(update):
        return
    user = update.effective_user
    session = get_session()
    done = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        edit, teams = work(session, draft)
        from services import draft_trade_service as dts
        body = dts.render_edit_result(
            session, draft, edit, teams,
            by=dsched.mention(session, user.id if user else None,
                              (user.first_name if user else None) or "Admin"))
        session.commit()
        done = body
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("%s failed", command)
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    await _reply(update, done)


def _pool_choice(candidates, query):
    """The refusal for a name that matched nothing, or more than one thing."""
    if not candidates:
        return f"No player in this draft's pool matches “{query}”."
    names = ", ".join(c.name or "" for c in candidates[:6])
    more = "" if len(candidates) <= 6 else f" (+{len(candidates) - 6} more)"
    return f"“{query}” matches more than one player: {names}{more}. Be exact."


async def dadd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/dadd &lt;team&gt; | &lt;player&gt;</code> — put a player on a squad.

    One verb for *add* and *move*, because from the admin's side they are the
    same instruction — "this player belongs to that team now" — and making them
    choose a different command based on a state they may not have checked is a
    way to get the wrong one.
    """
    from services import draft_trade_service as dts

    def work(session, draft):
        team_name, player_name = _split_pipes(_arg_text(context), 2)
        if not team_name or not player_name:
            raise DraftError("Usage: /dadd <team> | <player name>")
        team = ds.find_team(session, draft.id, team_name)
        if team is None:
            raise DraftError(f"No team here matches “{team_name}”.")
        player, candidates = ds.find_in_pool(session, draft.id, player_name)
        if player is None:
            raise DraftError(_pool_choice(candidates, player_name))
        return dts.admin_assign(session, draft, team, player,
                                by_tg_id=update.effective_user.id)
    await _squad_edit(update, context, work, command="/dadd")


async def ddrop_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/ddrop &lt;player&gt;</code> — send a player back to the pool.

    No team argument: a player is on exactly one squad, so naming it would only
    be a second thing to get wrong.
    """
    from services import draft_trade_service as dts

    def work(session, draft):
        player_name = _arg_text(context)
        if not player_name:
            raise DraftError("Usage: /ddrop <player name>")
        player, candidates = ds.find_in_pool(session, draft.id, player_name,
                                             drafted=True)
        if player is None:
            if not candidates:
                raise DraftError(f"No drafted player matches “{player_name}”. "
                                 f"Only a player on a squad can be released.")
            raise DraftError(_pool_choice(candidates, player_name))
        return dts.admin_release(session, draft, player,
                                 by_tg_id=update.effective_user.id)
    await _squad_edit(update, context, work, command="/ddrop")


async def dpublish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        league = ds.publish_to_league(session, draft)
        teams = len(ds.teams(session, draft.id))
        return (f"✅ Published <b>{html.escape(draft.name)}</b> as the Challenge "
                f"League <b>{html.escape(league.name)}</b> "
                f"(id <code>{league.id}</code>) — {teams} squads, ready to play.\n"
                f"Enter them in a tournament from the admin site's Tournament "
                f"Panel.")
    await _with_draft(update, work, admin=True)
