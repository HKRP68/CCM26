"""``/atrade`` — IPL-style trades between Franchise Auction franchises.

The shape is ``/dtrade``'s: one message, a tick list, the other side answers.
What is new is everything an auction adds:

* **Money.** The contract price travels with the player (the side taking him
  pays it, the side letting him go is paid it), and a cash fee can ride on top
  — so all-cash deals and player-plus-cash deals are ordinary trades. The card
  prints both purses before and after.
* **N-for-M.** Up to three players a side (``/atraderules players 4`` to
  change it), and either side may be empty.
* **Four windows.** Before the auction, **during** it (never under a live
  bid), after it, and in the middle of the season up to the deadline — see
  ``services/auction_trade_service.py``.
* **Counter-offers.** The other side can answer with its own version in one
  tap, sides swapped, instead of rejecting and starting again.
* **The bot owner / a bot admin signs it off.** With approval on (the
  default) an agreed trade waits for a bot admin's ✅ Approve or 🚫 Veto; the
  sweeper DMs them the card. Auction admins run the room, but cannot approve.

Nothing here announces a completed trade: the service records it, and
``services/auction_scheduler._trade_tick`` posts the "TRADE COMPLETED" card to
the auction group, whichever surface produced it.

Command surface
---------------
  /atrade <franchise>      open a trade with another franchise (owner/co-owner)
  /atrades                 the trade log and the open offers
  /atradehelp              the full guide and every command
  /atradecash <amount>     set the cash on your open offer (negative = they pay)
  /atradecancel            call off your open offer (or reject one made to you)
  /atradeblock [add|remove <player> [| note]]   the trade block
  /atraderules [rule value]                     read / set the rules (admin)
  /atradewindow on|off                          the master switch (admin)
  /atradedeadline <date|N|off>                  the mid-season deadline (admin)
  /atradeapprove /atradeveto /atradeundo <id>   bot owner / bot admin only

Callbacks are ``au_tr_`` and **shared**, like ``au_bid_``: both franchises
drive the same message. Every press is authorised here against the side the
presser actually owns and the step the trade is on.
"""

import html
import logging
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from services import auction_service as A
from services import auction_trade_service as T
from services.auction_service import AuctionError
from handlers.auction import (
    _arg_text, _find_franchise, _read_season, _reply,
    _require_admin,
)

logger = logging.getLogger(__name__)

CB = "au_tr_"
NOT_IN_TRADE = "You don't run a franchise in this trade."
NOT_BOT_ADMIN = "⛔ Only the bot owner or a bot admin can approve trades."
CASH_STEPS = (-100, -25, 25, 100)


# ════════════════════════════════════════════════════════════════════
# Keyboards
# ════════════════════════════════════════════════════════════════════

def _short(name, limit=14):
    name = name or ""
    return name if len(name) <= limit else name[:limit - 1] + "…"


def tag_franchise(session, team):
    """``<b>Chennai</b> (👤 @owner · @coowner)`` — everyone who can answer."""
    if team is None:
        return "<b>?</b>"
    people = []
    if int(team.owner_tg_id or 0) > 0:
        people.append(A.person_tag(session, int(team.owner_tg_id),
                                   team.owner_name or team.name))
    for tg_id in A.co_owner_ids(team):
        if tg_id and tg_id != int(team.owner_tg_id or 0):
            people.append(A.person_tag(session, tg_id, "co-owner"))
    label = f"<b>{html.escape(team.name)}</b>"
    return f"{label} (👤 {' · '.join(people)})" if people else label


def builder_keyboard(session, season, trade, side="a", page=0):
    """The tick list for one squad, the cash row, and Send / Cancel."""
    team = T.team_on(session, trade, side)
    chosen = set(T.selection(trade, side))
    squad = A.squad(session, team.id) if team else []
    pages = max(1, (len(squad) + T.SQUAD_PAGE_SIZE - 1) // T.SQUAD_PAGE_SIZE)
    page = max(0, min(int(page or 0), pages - 1))
    rows, line = [], []
    for lot in squad[page * T.SQUAD_PAGE_SIZE:(page + 1) * T.SQUAD_PAGE_SIZE]:
        # Seen from the side making the offer: a ticked player on its own
        # squad is going 📤 out, one on the other squad is coming 📥 in.
        mark = ("📤 " if side == "a" else "📥 ") if lot.id in chosen else ""
        line.append(InlineKeyboardButton(
            f"{mark}{_short(lot.name)} {lot.rating}",
            callback_data=f"{CB}t_{trade.id}_{lot.id}_{side}_{page}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    if not squad:
        rows.append([InlineKeyboardButton("— no players —", callback_data="noop")])
    if pages > 1:
        rows.append([
            InlineKeyboardButton("◀️", callback_data=f"{CB}v_{trade.id}_{side}_{max(0, page - 1)}"),
            InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"),
            InlineKeyboardButton("▶️", callback_data=f"{CB}v_{trade.id}_{side}_{min(pages - 1, page + 1)}"),
        ])
    team_a = T.team_on(session, trade, "a")
    team_b = T.team_on(session, trade, "b")
    rows.append([
        InlineKeyboardButton(("👉 " if side == "a" else "")
                             + f"📤 Trade out · {_short(team_a.name, 10)}",
                             callback_data=f"{CB}v_{trade.id}_a_0"),
        InlineKeyboardButton(("👉 " if side == "b" else "")
                             + f"📥 Trade in · {_short(team_b.name, 10)}",
                             callback_data=f"{CB}v_{trade.id}_b_0"),
    ])
    rules = T.trade_rules(season)
    if rules["allow_cash"]:
        cash = int(trade.cash_lakh or 0)
        label = ("💵 No cash" if not cash else
                 f"💵 {'You pay' if cash > 0 else 'They pay'} "
                 f"{A.render_money(abs(cash), season.currency_label or '₹')}")
        rows.append([InlineKeyboardButton(
            ("−" if step < 0 else "+") + A.render_money(abs(step), ""),
            callback_data=f"{CB}c_{trade.id}_{step}_{side}_{page}")
            for step in CASH_STEPS[:2]]
            + [InlineKeyboardButton(label, callback_data=f"{CB}c_{trade.id}_0_{side}_{page}")]
            + [InlineKeyboardButton(
                "+" + A.render_money(abs(step), ""),
                callback_data=f"{CB}c_{trade.id}_{step}_{side}_{page}")
               for step in CASH_STEPS[2:]])
    rows.append([
        InlineKeyboardButton("✅ Send offer", callback_data=f"{CB}s_{trade.id}"),
        InlineKeyboardButton("✖️ Cancel", callback_data=f"{CB}x_{trade.id}"),
    ])
    return InlineKeyboardMarkup(rows)


def offer_keyboard(trade):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Accept", callback_data=f"{CB}y_{trade.id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"{CB}n_{trade.id}"),
         InlineKeyboardButton("🔁 Counter", callback_data=f"{CB}k_{trade.id}")],
        [InlineKeyboardButton("✖️ Withdraw offer", callback_data=f"{CB}x_{trade.id}")],
    ])


def approval_keyboard(trade):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"{CB}ap_{trade.id}"),
        InlineKeyboardButton("🚫 Veto", callback_data=f"{CB}ve_{trade.id}"),
    ]])


def keyboard_for(session, season, trade, side="a", page=0):
    if trade.status == T.STATUS_BUILDING:
        return builder_keyboard(session, season, trade, side, page)
    if trade.status == T.STATUS_OFFERED:
        return offer_keyboard(trade)
    if trade.status == T.STATUS_PENDING:
        return approval_keyboard(trade)
    return None


def card_for(session, season, trade):
    if trade.status in (T.STATUS_COMPLETED, T.STATUS_VETOED, T.STATUS_REVERSED):
        return T.render_done(session, season, trade)
    return T.render_offer(session, season, trade)


# ════════════════════════════════════════════════════════════════════
# Shared plumbing
# ════════════════════════════════════════════════════════════════════

async def _run(update, work, *, admin=False, card=True):
    """Run ``work(session, season, user_id)``; commit; reply with what it says.

    Works in the auction's group and — for the person's own franchise — in a
    DM, like the read-only views: building an offer is homework.
    """
    if admin and not await _require_admin(update):
        return
    user = update.effective_user
    session = get_session()
    reply, markup = None, None
    try:
        season = _read_season(session, update)
        if season is None:
            await _reply(update, "❌ No auction here. Trades live in the "
                                 "auction's group, or in a DM for your own "
                                 "franchise.")
            return
        out = work(session, season, user.id if user else None)
        if isinstance(out, tuple):
            reply, markup = out
        else:
            reply = out
        session.commit()
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("trade command failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    if reply:
        await _reply(update, reply, reply_markup=markup)


def _my_franchise(session, season, tg_id):
    team = A.franchise_for_actor(session, season.id, tg_id)
    if team is None:
        raise AuctionError("You don't run a franchise in this auction — only an "
                           "owner or co-owner can trade.")
    return team


def _require_bot_admin_id(tg_id):
    if not T.is_bot_admin(tg_id):
        raise AuctionError("Only the bot owner or a bot admin can do that.")


# ════════════════════════════════════════════════════════════════════
# Commands
# ════════════════════════════════════════════════════════════════════

def _usage(session, season, mine):
    others = [f for f in A.franchises(session, season.id)
              if mine is None or f.id != mine.id]
    lines = ["🔁 <b>Trade</b> — <code>/atrade &lt;franchise&gt;</code>",
             T.window_line(session, season), "",
             "Pick players on <b>both</b> squads, add cash if you like, and "
             "send it. The side taking a player pays his auction price; the "
             "side letting him go gets it back.", "",
             "Franchises: " + ", ".join(html.escape(f.name) for f in others),
             "", "📖 Full guide and every command: /atradehelp"]
    return "\n".join(lines)


async def atrade_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atrade &lt;franchise&gt;</code> — open a trade (owner + co-owners)."""
    name = _arg_text(context)
    chat = update.effective_chat

    def work(session, season, uid):
        mine = _my_franchise(session, season, uid)
        if not name:
            live = T.live_trade_for(session, season, mine.id)
            if live is not None:
                side = T.side_for(live, mine) or "a"
                return (T.render_offer(session, season, live),
                        keyboard_for(session, season, live, side))
            return _usage(session, season, mine)
        other = _find_franchise(session, season, name)
        trade = T.open_trade(session, season, mine, other, by_tg_id=uid,
                             chat_id=chat.id if chat else None)
        return (T.render_offer(session, season, trade),
                builder_keyboard(session, season, trade, "a", 0))
    await _run(update, work)


async def atradehelp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradehelp</code> — the full trade guide and every command.

    Answers anywhere: with this chat's auction (or, in a DM, yours) it reads
    out that auction's own window and numbers; without one, the defaults.
    """
    from services import auction_rich as AR
    user = update.effective_user
    session = get_session()
    try:
        try:
            season = _read_season(session, update)
        except Exception:
            season = None
        parts = T.render_help(session, season)
    except Exception:
        logger.exception("atradehelp failed")
        parts = T.render_help()
    finally:
        session.close()
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        await _reply(update, part,
                     reply_markup=(AR.with_close(None, user.id if user else None)
                                   if last else None))


async def atrades_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atrades</code> — every trade, and the offers still open."""
    await _run(update, lambda s, season, uid: T.render_log(s, season, limit=15))


async def atradecash_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradecash 3.5</code> — you pay ₹3.5 Cr on top; <code>-2</code> they pay."""
    raw = _arg_text(context)

    def work(session, season, uid):
        mine = _my_franchise(session, season, uid)
        trade = T.live_trade_for(session, season, mine.id)
        if trade is None or trade.status != T.STATUS_BUILDING or trade.team_a_id != mine.id:
            raise AuctionError("You have no offer being built. Start one with "
                               "/atrade <franchise>.")
        text = raw.strip()
        if not text:
            raise AuctionError("Say how much: /atradecash 2 (you pay ₹2 Cr), "
                               "/atradecash -50L (they pay ₹50 L), /atradecash 0.")
        sign = -1 if text.startswith("-") else 1
        text = text.lstrip("+-").strip()
        lakh = 0 if text in ("0", "off", "none") else A.parse_amount(text)
        T.set_cash(session, trade, sign * int(lakh))
        return (T.render_offer(session, season, trade),
                builder_keyboard(session, season, trade, "a", 0))
    await _run(update, work)


async def atradecancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradecancel</code> — call off your offer, or turn down one made to you."""
    def work(session, season, uid):
        mine = _my_franchise(session, season, uid)
        trade = T.live_trade_for(session, season, mine.id)
        if trade is None:
            return "You have no open trade."
        if trade.status == T.STATUS_PENDING:
            raise AuctionError("Both sides agreed this one — it is with the bot "
                               "admin now. Ask them to veto it.")
        if trade.team_b_id == mine.id and trade.status == T.STATUS_OFFERED:
            T.reject(session, trade, by_tg_id=uid)
            return f"❌ Trade #{trade.id} rejected."
        T.cancel(session, trade, by_tg_id=uid)
        return f"✖️ Trade #{trade.id} called off."
    await _run(update, work)


async def atradeblock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradeblock</code> · <code>add &lt;player&gt; [| note]</code> · <code>remove &lt;player&gt;</code>."""
    raw = _arg_text(context)

    def work(session, season, uid):
        if not raw:
            return T.render_block(session, season)
        verb, _, rest = raw.partition(" ")
        verb = verb.lower()
        if verb not in ("add", "remove", "rm", "del"):
            raise AuctionError("Use /atradeblock add <player> [| note] or "
                               "/atradeblock remove <player>.")
        mine = _my_franchise(session, season, uid)
        who, _, note = rest.partition("|")
        lot = A.find_one_lot(session, season, who.strip())
        if verb == "add":
            T.block_add(session, season, mine, lot, note=note, by_tg_id=uid)
            return (f"🏷 {html.escape(lot.name)} is on the trade block. "
                    f"Anyone can see him with /atradeblock.")
        T.block_remove(session, season, mine, lot)
        return f"🏷 {html.escape(lot.name)} is off the trade block."
    await _run(update, work)


async def atraderules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atraderules</code> — read; <code>/atraderules mid off</code> — set (admin)."""
    args = list(context.args or [])
    if not args:
        await _run(update, lambda s, season, uid: T.render_rules(s, season))
        return
    if len(args) < 2:
        await _reply(update, "Use <code>/atraderules &lt;rule&gt; &lt;value&gt;</code> — "
                             "e.g. <code>mid off</code>, <code>approval on</code>, "
                             "<code>players 4</code>, <code>trades 2</code>, "
                             "<code>maxcash 10</code>, <code>cash off</code>.")
        return

    def work(session, season, uid):
        changes = {}
        for index in range(0, len(args) - 1, 2):
            changes[args[index]] = " ".join(args[index + 1:index + 2])
        T.set_trade_rules(session, season, changes, by_tg_id=uid)
        return T.render_rules(session, season)
    await _run(update, work, admin=True)


async def atradewindow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradewindow on|off</code> — the master switch over every window."""
    raw = _arg_text(context).lower()

    def work(session, season, uid):
        if raw in ("on", "open", "1", "yes"):
            T.set_window(session, season, True, by_tg_id=uid)
        elif raw in ("off", "close", "closed", "0", "no"):
            T.set_window(session, season, False, by_tg_id=uid)
        elif raw:
            raise AuctionError("Use /atradewindow on or /atradewindow off.")
        return T.window_line(session, season)
    await _run(update, work, admin=bool(raw))


def parse_deadline(text, now=None):
    """``(at, matches)`` from ``off``, ``12`` (league matches), ``48h``,
    ``7d`` or ``2026-05-01 18:00`` (UTC)."""
    now = now or datetime.utcnow()
    text = (text or "").strip().lower()
    if text in ("off", "none", "clear", "0"):
        return None, None
    if text.isdigit():
        return None, int(text)
    if text[:-1].isdigit() and text[-1:] in ("h", "d"):
        n = int(text[:-1])
        return now + (timedelta(hours=n) if text.endswith("h") else timedelta(days=n)), None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%m-%Y %H:%M", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt), None
        except ValueError:
            continue
    raise AuctionError("Give the deadline as a number of league matches (12), "
                       "a time from now (48h, 7d), a UTC date "
                       "(2026-05-01 18:00), or off.")


async def atradedeadline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradedeadline 12</code> · <code>48h</code> · <code>2026-05-01 18:00</code> · <code>off</code>."""
    raw = _arg_text(context)

    def work(session, season, uid):
        if raw:
            at, matches = parse_deadline(raw)
            T.set_deadline(session, season, at=at, matches=matches, by_tg_id=uid)
        return (T.window_line(session, season) + "\n"
                "<i>The mid-season window always closes when the playoffs "
                "start.</i>")
    await _run(update, work, admin=bool(raw))


def _trade_arg(session, season, raw):
    word = (raw or "").split()[0].lstrip("#") if raw else ""
    if not word.isdigit():
        return None
    trade = T.get_trade(session, int(word))
    if trade is None or trade.season_id != season.id:
        raise AuctionError(f"There is no trade #{word} in this auction.")
    return trade


async def atradeapprove_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradeapprove &lt;id&gt;</code> — bot owner / bot admin only."""
    raw = _arg_text(context)

    def work(session, season, uid):
        _require_bot_admin_id(uid)
        trade = _trade_arg(session, season, raw)
        if trade is None:
            pending = T.pending_trades(session, season)
            if not pending:
                return "No trades are waiting for approval."
            return ("⏳ <b>Waiting for approval</b>\n" + "\n".join(
                f"#{t.id} {html.escape(T.team_on(session, t, 'a').name)} ⇄ "
                f"{html.escape(T.team_on(session, t, 'b').name)}" for t in pending)
                + "\n\n<code>/atradeapprove &lt;id&gt;</code> · "
                  "<code>/atradeveto &lt;id&gt; [reason]</code>")
        T.approve(session, trade, by_tg_id=uid)
        return f"✅ Trade #{trade.id} approved and done."
    await _run(update, work)


async def atradeveto_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradeveto &lt;id&gt; [reason]</code> — bot owner / bot admin only."""
    raw = _arg_text(context)

    def work(session, season, uid):
        _require_bot_admin_id(uid)
        trade = _trade_arg(session, season, raw)
        if trade is None:
            raise AuctionError("Name the trade: /atradeveto <id> [reason].")
        reason = raw.partition(" ")[2]
        T.veto(session, trade, by_tg_id=uid, reason=reason)
        return f"🚫 Trade #{trade.id} vetoed."
    await _run(update, work)


async def atradeundo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/atradeundo &lt;id&gt;</code> — reverse a completed trade (bot admin)."""
    raw = _arg_text(context)

    def work(session, season, uid):
        _require_bot_admin_id(uid)
        trade = _trade_arg(session, season, raw)
        if trade is None:
            raise AuctionError("Name the trade: /atradeundo <id> (see /atrades).")
        T.reverse(session, trade, by_tg_id=uid)
        return f"↩️ Trade #{trade.id} undone — players and money are back."
    await _run(update, work)


# ════════════════════════════════════════════════════════════════════
# Buttons
# ════════════════════════════════════════════════════════════════════

def _parse(data):
    parts = (data or "")[len(CB):].split("_")
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        return None, None, []
    return parts[0], int(parts[1]), parts[2:]


async def _edit(query, text, markup):
    try:
        await query.edit_message_text(text, parse_mode="HTML",
                                      reply_markup=markup,
                                      disable_web_page_preview=True)
    except Exception as exc:  # "message is not modified" and friends
        if "not modified" not in str(exc).lower():
            logger.warning("trade card edit failed: %s", exc)


async def trade_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every ``au_tr_`` press: authorised against the presser on each one."""
    query = update.callback_query
    if query is None:
        return
    action, trade_id, rest = _parse(query.data)
    if action is None:
        await query.answer()
        return
    uid = query.from_user.id if query.from_user else None
    session = get_session()
    text = markup = None
    toast = None
    # A message that must NOTIFY somebody: an edit never pings anyone, so
    # the people who have to answer get a fresh message that tags them.
    ping = None          # (chat_id, text, markup)
    try:
        trade = T.get_trade(session, trade_id)
        if trade is None:
            await query.answer("That trade no longer exists.", show_alert=True)
            return
        season = trade.season
        side = T.actor_side(session, trade, uid)
        view, page = "a", 0
        if action in ("t", "c", "v") and rest:
            view = rest[-2] if len(rest) >= 2 and rest[-2] in ("a", "b") else "a"
            page = int(rest[-1]) if rest[-1].isdigit() else 0

        if action in ("t", "v", "c", "s"):
            if side != "a":
                await query.answer(f"Only {T.team_on(session, trade, 'a').name} "
                                   f"can change this offer.", show_alert=True)
                return
            if action == "t":
                _side, lot = T.toggle_lot(session, trade, int(rest[0]))
                toast = f"{lot.name} {'in' if lot.id in T.selection(trade, _side) else 'out'}"
            elif action == "c":
                step = int(rest[0])
                if step == 0:
                    T.set_cash(session, trade, 0)
                else:
                    T.adjust_cash(session, trade, step)
            elif action == "v":
                view = rest[0] if rest and rest[0] in ("a", "b") else "a"
                page = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 0
            elif action == "s":
                T.send_offer(session, trade, by_tg_id=uid)
                toast = "Offer sent"
                here = query.message.chat.id if query.message is not None else None
                target = season.chat_id or here
                if target:
                    trade.chat_id = target
                    ping = (target, "SEND", None)
        elif action == "x":
            if side != "a" and not T.is_bot_admin(uid):
                await query.answer(NOT_IN_TRADE, show_alert=True)
                return
            T.cancel(session, trade, by_tg_id=uid)
            toast = "Called off"
        elif action in ("y", "n", "k"):
            if side != "b":
                await query.answer(f"Only {T.team_on(session, trade, 'b').name} "
                                   f"can answer this offer.", show_alert=True)
                return
            if action == "y":
                T.accept(session, trade, by_tg_id=uid)
                toast = ("Accepted — waiting for the bot admin"
                         if trade.status == T.STATUS_PENDING else "Trade done!")
            elif action == "n":
                T.reject(session, trade, by_tg_id=uid)
                toast = "Rejected"
                target = season.chat_id or (
                    query.message.chat.id if query.message is not None else None)
                if target:
                    ping = (target,
                            f"❌ {tag_franchise(session, T.team_on(session, trade, 'a'))}"
                            f" — {html.escape(T.team_on(session, trade, 'b').name)} "
                            f"rejected trade #{trade.id}.", None)
            else:
                trade = T.counter(session, trade, by_tg_id=uid)
                toast = "Your counter-offer — tick, then send"
        elif action in ("ap", "ve"):
            if not T.is_bot_admin(uid):
                await query.answer(NOT_BOT_ADMIN, show_alert=True)
                return
            if action == "ap":
                T.approve(session, trade, by_tg_id=uid)
                toast = "Approved"
            else:
                T.veto(session, trade, by_tg_id=uid)
                toast = "Vetoed"
        else:
            await query.answer()
            return

        text = card_for(session, season, trade)
        markup = keyboard_for(session, season, trade, view, page)
        # The room is shown a finished trade once. When this very message is
        # in the auction group it IS that showing.
        if (trade.status in T.ANNOUNCED_STATUSES and query.message is not None
                and season.chat_id and query.message.chat.id == season.chat_id
                and trade.status != T.STATUS_PENDING):
            trade.announced_at = datetime.utcnow()
        if ping is not None and ping[1] == "SEND":
            # The offer itself goes out as a NEW message that tags the side
            # that has to answer, buttons and all; the builder this press
            # came from becomes a pointer to it, so there is one live card.
            team_a = T.team_on(session, trade, "a")
            team_b = T.team_on(session, trade, "b")
            ping = (ping[0],
                    f"📨 {tag_franchise(session, team_b)} — "
                    f"<b>{html.escape(team_a.name)}</b> sent you a trade offer.\n\n"
                    + T.render_offer(session, season, trade),
                    offer_keyboard(trade))
            same_chat = (query.message is not None
                         and query.message.chat.id == ping[0])
            text = (f"📨 Trade offer #{trade.id} sent to "
                    f"<b>{html.escape(team_b.name)}</b> — they answer on the "
                    f"card below." if same_chat else
                    T.render_offer(session, season, trade)
                    + "\n\n📨 <i>Sent to the auction group.</i>")
            markup = None
        session.commit()
    except AuctionError as exc:
        session.rollback()
        await query.answer(str(exc)[:190], show_alert=True)
        return
    except Exception:
        session.rollback()
        logger.exception("trade button failed")
        await query.answer("Something went wrong. Try again.", show_alert=True)
        return
    finally:
        session.close()

    await query.answer(toast[:190] if toast else None)
    await _edit(query, text, markup)
    if ping is not None:
        chat_id, ping_text, ping_markup = ping
        try:
            await context.bot.send_message(chat_id, ping_text,
                                           parse_mode="HTML",
                                           reply_markup=ping_markup,
                                           disable_web_page_preview=True)
        except Exception:
            logger.warning("could not post the trade message", exc_info=True)
