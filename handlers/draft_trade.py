"""``/dtrade`` — franchises swapping players once the draft is over.

The shape is ``/trade``'s, deliberately: one message in the group, the two
sides tick their players in turn, both owners confirm, and the swap happens.
What is **not** ``/trade``'s is the rule underneath it. ``/trade`` exists to
move a card between two collections without either captain gaining value, so it
insists the two cards have the exact same OVR. A franchise is not a collection:
the whole reason to trade is that one squad has a spare keeper and the other
has a spare quick, and "the numbers must match" makes that trade impossible.

So ``/dtrade`` has **no rating rule**. Any player for any player, at any OVR.
``services/draft_trade_service.py`` carries what replaces it — the draft's own
squad rules, re-checked against the squad the trade would produce — and the
offer card prints both sides' OVR totals and names the side the deal favours,
because a rule that is gone should be replaced by daylight rather than by
nothing.

Three things here are better than the flow they are modelled on:

* **N-for-N packages.** A trade is not limited to one player a side. The count
  has to match because a squad's size is fixed by the order sheet, but two-for-
  two and three-for-three are ordinary trades and the buttons are a tick list.
* **Offers survive a restart.** ``/trade`` holds its in-flight state in
  ``context.bot_data`` and loses every open trade to a redeploy. A half-built
  ``/dtrade`` is a ``DraftTrade`` row, so the buttons still work afterwards —
  the same call ``services/draft_scheduler.py`` makes for the pick clock.
* **Co-owners can act.** Whoever may ``/pick`` for a team may trade for it.

Command surface
---------------
  /dtrade <team>     open a trade with another franchise (owner + co-owners)
  /dtrades           the trade log — every completed deal in this draft
  /dtradecancel      call off your team's open offer
  /dtradelock on|off admin: close or reopen the trade window

Callbacks are all ``dt_``. Unlike the ``dr_`` buttons, these are **not**
owner-locked to whoever typed the command: the second franchise has to be able
to drive the same message, exactly as ``/trade``'s ``t1p_``/``t2p_`` buttons do.
Every press is instead authorised here, against the team the presser owns and
the step the offer is actually on — which is the stronger check anyway, because
it survives a restart and a co-owner taking over mid-offer.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from services import draft_service as ds
from services import draft_trade_service as dts
from services.draft_service import DraftError
from handlers.draft import (
    GROUP_CHAT_TYPES, GROUP_ONLY, NO_DRAFT,
    _arg_text, _load_for_chat, _reply, _with_draft,
)

logger = logging.getLogger(__name__)

CB = "dt_"
NOT_YOURS = "You don't own a team in this trade."


# ════════════════════════════════════════════════════════════════════
# Keyboards
# ════════════════════════════════════════════════════════════════════

def _build_keyboard(session, draft, trade, side, page=0):
    """The tick list for the side that is choosing, plus Done and Cancel."""
    team = dts.team_on(trade, side)
    chosen = set(dts.selection(trade, side))
    rows, page, pages = dts.squad_page(session, draft, team.id, page)

    buttons, line = [], []
    for player in rows:
        mark = "☑️ " if player.id in chosen else ""
        line.append(InlineKeyboardButton(
            f"{mark}{player.name} {player.rating}",
            callback_data=f"{CB}tog_{trade.id}_{player.id}"))
        if len(line) == 2:
            buttons.append(line)
            line = []
    if line:
        buttons.append(line)

    if pages > 1:
        buttons.append([
            InlineKeyboardButton("◀️", callback_data=(
                f"{CB}pg_{trade.id}_{(page - 1) % pages}")),
            InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=(
                f"{CB}pg_{trade.id}_{page}")),
            InlineKeyboardButton("▶️", callback_data=(
                f"{CB}pg_{trade.id}_{(page + 1) % pages}")),
        ])

    count = len(chosen)
    # Side B is told the number it has to reach; side A sets it, so it is only
    # told what it has so far.
    if side == "b":
        wanted = len(dts.selection(trade, "a"))
        done = f"✅ Done ({count}/{wanted})"
    else:
        done = f"✅ Done ({count})" if count else "✅ Done"
    buttons.append([
        InlineKeyboardButton(done, callback_data=f"{CB}done_{trade.id}"),
        InlineKeyboardButton("✖️ Cancel", callback_data=f"{CB}no_{trade.id}"),
    ])
    return InlineKeyboardMarkup(buttons), page


def _offer_keyboard(trade):
    """One confirm button per franchise, plus a cancel either side may press."""
    row = []
    for side in ("a", "b"):
        team = dts.team_on(trade, side)
        label = ("✅ " + (team.name or "")[:20]
                 + (" ✔" if dts.confirmed(trade, side) else ""))
        row.append(InlineKeyboardButton(
            label, callback_data=f"{CB}ok_{trade.id}_{side}"))
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton("✖️ Cancel trade",
                              callback_data=f"{CB}no_{trade.id}")],
    ])


# ════════════════════════════════════════════════════════════════════
# /dtrade
# ════════════════════════════════════════════════════════════════════

async def dtrade_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    user = update.effective_user
    if user is None:
        return
    wanted = _arg_text(context)

    session = get_session()
    posted = None
    try:
        draft = _load_for_chat(session, update)
        if draft is None:
            await _reply(update, NO_DRAFT)
            return
        dts.require_open(draft)

        mine = ds.team_for_actor(session, draft.id, user.id)
        if mine is None:
            await _reply(update, "⛔ You don't own a team in this draft.")
            return
        if not wanted:
            await _reply(update, _usage(session, draft, mine))
            return
        theirs = ds.find_team(session, draft.id, wanted)
        if theirs is None:
            raise DraftError(f"No team here matches “{wanted}”.")

        trade = dts.open_trade(session, draft, mine, theirs,
                               by_tg_id=user.id, chat_id=chat.id)
        text = dts.render_build(session, draft, trade, "a")
        keyboard, _page = _build_keyboard(session, draft, trade, "a")
        session.commit()
        posted = (text, keyboard)
    except DraftError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/dtrade failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    text, keyboard = posted
    await _reply(update, text, reply_markup=keyboard)


def _usage(session, draft, mine):
    names = [team.name for team in ds.teams(session, draft.id)
             if team.id != mine.id]
    listed = ", ".join(html.escape(n or "") for n in names[:8])
    return ("Usage: <code>/dtrade &lt;team&gt;</code>\n"
            f"You are trading for <b>{html.escape(mine.name or '')}</b>.\n"
            f"Any player for any player — <b>no rating rule</b>, and you can "
            f"package two-for-two or more.\n"
            + (f"\nTeams here: {listed}" if listed else ""))


# ════════════════════════════════════════════════════════════════════
# The buttons
# ════════════════════════════════════════════════════════════════════

def _parse(data):
    """``"dt_tog_12_44"`` → ``("tog", 12, "44")``; ``None`` if it is not ours."""
    if not isinstance(data, str) or not data.startswith(CB):
        return None
    parts = data[len(CB):].split("_")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1]), (parts[2] if len(parts) > 2 else "")


async def _load(session, update, query):
    """``(draft, trade)`` for a pressed button, or ``(None, None)`` after an alert.

    Everything a press has to be true about before any of the four handlers
    below does its own work: the chat still runs this draft, the offer exists,
    it belongs to this draft, and it has not expired or already been closed.
    """
    parsed = _parse(query.data)
    if parsed is None:
        await query.answer("That button is out of date.", show_alert=True)
        return None, None
    _action, trade_id, _arg = parsed
    draft = _load_for_chat(session, update)
    if draft is None:
        await query.answer("No draft is running here.", show_alert=True)
        return None, None
    trade = dts.get_trade(session, trade_id)
    if trade is None or trade.draft_id != draft.id:
        await query.answer("That trade is gone.", show_alert=True)
        return None, None
    if trade.status not in dts.LIVE_STATUSES:
        await query.answer(f"That trade is already {trade.status}.",
                           show_alert=True)
        return None, None
    if dts.is_expired(trade):
        dts.cancel(session, trade, status=dts.STATUS_EXPIRED)
        session.commit()
        await query.answer("That offer has expired. Start a new one with "
                           "/dtrade.", show_alert=True)
        try:
            await query.edit_message_text("⌛ Trade offer expired.")
        except Exception:
            pass
        return None, None
    return draft, trade


async def _redraw(query, text, keyboard):
    try:
        await query.edit_message_text(text, parse_mode="HTML",
                                      disable_web_page_preview=True,
                                      reply_markup=keyboard)
    except Exception:
        # "Message is not modified" when a tick is pressed twice on a slow
        # connection, and an old message can be too stale to edit. Neither is
        # worth an error in the group.
        logger.debug("dtrade edit skipped", exc_info=True)


async def trade_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every ``dt_`` press. One entry point, because they share their guards."""
    query = update.callback_query
    if query is None:
        return
    parsed = _parse(query.data)
    if parsed is None:
        await query.answer("That button is out of date.", show_alert=True)
        return
    action, _trade_id, arg = parsed
    presser = getattr(query.from_user, "id", None)

    session = get_session()
    announce = None
    try:
        draft, trade = await _load(session, update, query)
        if trade is None:
            return
        side = dts.side_for(trade, presser)
        if side is None:
            await query.answer(NOT_YOURS, show_alert=True)
            return

        if action in ("tog", "pg", "done"):
            turn = dts.active_side(trade)
            if turn is None:
                await query.answer(
                    "Both squads are in — confirm or cancel below.",
                    show_alert=True)
                return
            if side != turn:
                await query.answer(
                    f"It's {dts.team_on(trade, turn).name}'s turn to pick.",
                    show_alert=True)
                return

        if action == "tog":
            dts.toggle(session, trade, side, arg)
            session.commit()
            await query.answer()
            text = dts.render_build(session, draft, trade, side)
            keyboard, _page = _build_keyboard(session, draft, trade, side,
                                              _page_of(session, draft, trade,
                                                       side, arg))
            await _redraw(query, text, keyboard)
            return

        if action == "pg":
            await query.answer()
            page = int(arg) if str(arg).isdigit() else 0
            text = dts.render_build(session, draft, trade, side)
            keyboard, _page = _build_keyboard(session, draft, trade, side, page)
            await _redraw(query, text, keyboard)
            return

        if action == "done":
            dts.finish_side(session, trade, side)
            session.commit()
            await query.answer("Locked in.")
            if trade.status == dts.STATUS_BUILDING_B:
                text = dts.render_build(session, draft, trade, "b")
                keyboard, _page = _build_keyboard(session, draft, trade, "b")
            else:
                text = dts.render_offer(session, draft, trade)
                keyboard = _offer_keyboard(trade)
            await _redraw(query, text, keyboard)
            return

        if action == "no":
            dts.cancel(session, trade, by_tg_id=presser)
            session.commit()
            await query.answer("Trade cancelled.")
            await _redraw(query, "✖️ <b>Trade cancelled</b> by "
                                 f"{html.escape(dts.team_on(trade, side).name or '')}.",
                          None)
            return

        if action == "ok":
            if arg in ("a", "b") and arg != side:
                await query.answer(
                    f"That button is {dts.team_on(trade, arg).name}'s.",
                    show_alert=True)
                return
            if dts.confirmed(trade, side):
                await query.answer("You have already confirmed.")
                return
            dts.set_confirmed(trade, side, presser)
            if not dts.confirmed(trade, dts.other_side(side)):
                session.commit()
                await query.answer("Confirmed. Waiting for the other side.")
                await _redraw(query, dts.render_offer(session, draft, trade),
                              _offer_keyboard(trade))
                return
            # Both in. Re-validate at the moment the players actually move: an
            # offer can sit on screen while the other trade one of these squads
            # had open goes through underneath it.
            a_players, b_players = dts.execute(session, trade,
                                               by_tg_id=presser)
            announce = dts.render_done(session, draft, trade,
                                       a_players, b_players)
            session.commit()
            await query.answer("Done.")
            # Deliberately no ``return``: the completed card is drawn below,
            # after ``finally`` has closed the session. Returning here would
            # run the finally and skip the tail, leaving the group looking at
            # a confirmation card for a trade that has already happened.
    except DraftError as exc:
        session.rollback()
        await query.answer(str(exc)[:190], show_alert=True)
        return
    except Exception:
        session.rollback()
        logger.exception("dtrade callback failed")
        await query.answer("Something went wrong.", show_alert=True)
        return
    finally:
        session.close()

    if announce:
        await _redraw(query, announce, None)


def _page_of(session, draft, trade, side, player_id):
    """The page the player just ticked is on, so a tick does not jump pages."""
    try:
        wanted = int(player_id)
    except (TypeError, ValueError):
        return 0
    rows = dts.squad_in_page_order(session, draft, dts.team_on(trade, side).id)
    for index, row in enumerate(rows):
        if row.id == wanted:
            return index // dts.SQUAD_PAGE_SIZE
    return 0


# ════════════════════════════════════════════════════════════════════
# /dtrades, /dtradecancel, /dtradelock
# ════════════════════════════════════════════════════════════════════

async def dtrades_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The trade log. The pick rows are never rewritten by a trade, so this is
    the only place a squad change after the draft is on the record."""
    def work(session, draft):
        dts.expire_stale(session, draft)
        return dts.render_log(session, draft)
    await _with_draft(update, work)


async def dtradecancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, draft):
        user = update.effective_user
        mine = ds.team_for_actor(session, draft.id, user.id if user else None)
        if mine is None:
            raise DraftError("You don't own a team in this draft.")
        trade = dts.live_trade_for_team(session, draft, mine.id)
        if trade is None:
            raise DraftError(f"{mine.name} has no open trade.")
        other = (trade.team_b if trade.team_a_id == mine.id else trade.team_a)
        dts.cancel(session, trade, by_tg_id=user.id if user else None)
        return (f"✖️ Called off the trade between "
                f"<b>{html.escape(mine.name or '')}</b> and "
                f"<b>{html.escape((other.name if other else '') or '')}</b>.")
    await _with_draft(update, work)


async def dtradelock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close or reopen the trade window — admin only.

    A trade moves players between two published squads, and a league fixture
    reads those squads. Closing the window before a match day is how an admin
    makes sure a team sheet cannot change under a game in progress.
    """
    def work(session, draft):
        arg = _arg_text(context).lower()
        if not arg:
            state = "open" if dts.trades_open(draft) else "closed"
            return (f"🔁 The trade window is <b>{state}</b>.\n"
                    f"<code>/dtradelock on</code> closes it, "
                    f"<code>/dtradelock off</code> reopens it.")
        if arg in ("on", "lock", "close", "closed", "1", "true", "yes"):
            dts.set_window(session, draft, False)
            return ("🔒 <b>Trade window closed.</b> Any offer still being built "
                    "has been called off. Reopen it with "
                    "<code>/dtradelock off</code>.")
        if arg in ("off", "unlock", "open", "0", "false", "no"):
            if draft.status != ds.STATUS_COMPLETED:
                dts.set_window(session, draft, True)
                return ("🔓 <b>Trade window open</b> — it takes effect once the "
                        "draft finishes. This draft is "
                        f"{ds.status_label(draft)}.")
            dts.set_window(session, draft, True)
            return ("🔓 <b>Trade window open.</b> Owners can swap players with "
                    "<code>/dtrade &lt;team&gt;</code> — any player for any "
                    "player, package deals included.")
        raise DraftError("Say /dtradelock on or /dtradelock off.")

    await _with_draft(update, work, admin=True)
