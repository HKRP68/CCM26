"""The Franchise Auction's Telegram surface — ``/bid`` and everything around it.

Shaped like ``handlers/draft.py``, because an auction is the draft's sibling and
a room that has run one should not have to learn a second set of habits: the
event is bound to one group, the commands refuse anywhere else, and the admin
gate answers the person who typed it rather than pretending the command does
not exist.

Three things here are deliberately different from the draft.

**Nothing in this module announces.** Every mutating call writes an
``AuctionEvent`` and commits; ``services/auction_scheduler`` drains that log and
says it out loud. That is what lets the website drive the same auction without
reaching across the Flask/PTB boundary — and it makes the ``/pick`` invariant
(never let a Telegram failure roll back a committed action) hold here by
construction, since there is no Telegram call after the commit at all.

**A successful ``/bid`` gets no reply.** Forty bids inside one lot would be
forty messages into a room that is already reading a live board; the bid is
acknowledged with a reaction on the bidder's own message, and the board carries
the price within a tick. A *refused* bid always answers, and always says what
would have worked instead — on a thirty-second clock, "invalid bid" is useless.

**The quick-bid buttons are shared, not owner-locked.** They ride on the pinned
board that every franchise is watching; a board the second franchise cannot
touch is not an auction. Each press is authorised against the franchise the
presser actually owns, and the exact price is baked into the callback data so a
stale button can never bid a number nobody meant.
"""

import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import get_session
from services import auction_service as A
from services.admin_ids import is_admin
from services.auction_service import AuctionError

logger = logging.getLogger(__name__)

GROUP_CHAT_TYPES = ("group", "supergroup")

NOT_ADMIN = "⛔ Only bot admins can manage an auction."
GROUP_ONLY = ("❌ Auction commands only work in the group the auction is bound "
              "to.\nAn admin binds one with <code>/abind</code>.")
NO_AUCTION = ("❌ No auction is running in this chat.\n"
              "An admin can create one with <code>/anew &lt;name&gt;</code> and "
              "bind it here with <code>/abind</code>.")
NOT_YOURS = ("⛔ Only a franchise's owner or a co-owner can bid for it.\n"
             "Admins deliberately cannot bid on someone's behalf — an admin "
             "who must act for an absent owner uses the auction console, which "
             "records the bid as an admin's.")

BID_CB = "au_bid_"


# ════════════════════════════════════════════════════════════════════
# Shared plumbing
# ════════════════════════════════════════════════════════════════════

async def _reply(update, text, **kwargs):
    msg = update.effective_message
    if msg is None:
        return None
    kwargs.setdefault("parse_mode", "HTML")
    kwargs.setdefault("disable_web_page_preview", True)
    return await msg.reply_text(text, **kwargs)


def _arg_text(context):
    return " ".join(context.args or []).strip()


async def _require_admin(update):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _reply(update, NOT_ADMIN)
        return False
    return True


def _season_for(session, update):
    chat = update.effective_chat
    if chat is None:
        return None
    return A.season_for_chat(session, chat.id)


async def _with_auction(update, work, *, admin=False, allow_dm=False,
                        context=None):
    """Run ``work(session, season)`` against this chat's auction.

    Centralises the group gate, the admin check, finding the bound auction,
    turning an ``AuctionError`` into a plain reply, and closing the session.
    ``work`` returns the reply text and may mutate; the commit happens here,
    and only if ``work`` returns without raising.
    """
    chat = update.effective_chat
    if not allow_dm and (chat is None or chat.type not in GROUP_CHAT_TYPES):
        await _reply(update, GROUP_ONLY)
        return
    if admin and not await _require_admin(update):
        return
    session = get_session()
    text = None
    try:
        season = _season_for(session, update)
        if season is None:
            await _reply(update, NO_AUCTION)
            return
        text = work(session, season)
        session.commit()
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("Auction command failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    if text:
        await _reply(update, text)


def bid_keyboard(season, lot):
    """Quick-bid buttons: the minimum, and one step above it.

    The exact amount rides in the callback data, so a button pressed after the
    price has moved bids a number that is no longer legal and is refused with
    "the price has moved" rather than quietly bidding the wrong thing.
    """
    if lot is None or lot.status != A.LOT_ON_BLOCK:
        return None
    minimum = A.next_min_bid(season, lot)
    step = A.increment_for(season, minimum)
    symbol = season.currency_label or "₹"
    row = [InlineKeyboardButton(f"Bid {A.render_money(minimum, symbol)}",
                                callback_data=f"{BID_CB}{lot.id}_{minimum}")]
    if minimum + step <= 1_000_000:
        row.append(InlineKeyboardButton(
            f"Bid {A.render_money(minimum + step, symbol)}",
            callback_data=f"{BID_CB}{lot.id}_{minimum + step}"))
    return InlineKeyboardMarkup([row])


# ════════════════════════════════════════════════════════════════════
# /bid — the whole point
# ════════════════════════════════════════════════════════════════════

async def bid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    user = update.effective_user
    if user is None:
        return

    session = get_session()
    landed = None
    try:
        season = A.season_for_chat(session, chat.id)
        if season is None:
            await _reply(update, NO_AUCTION)
            return
        franchise = A.franchise_for_actor(session, season.id, user.id)
        if franchise is None:
            await _reply(update, NOT_YOURS)
            return
        lot = A.current_lot(session, season)
        if lot is None:
            await _reply(update, "⚠️ Nothing is on the block right now.")
            return

        raw = _arg_text(context)
        # A bare /bid means "the next minimum" — the commonest action in the
        # room, and the one form that cannot be fat-fingered into a number
        # nobody meant with ten seconds on the clock.
        amount = A.next_min_bid(season, lot) if not raw else A.parse_amount(raw)

        A.place_bid(session, season, lot, franchise, amount,
                    by_tg_id=user.id, source="tg")
        session.commit()
        landed = amount
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/bid failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()

    # Acknowledge with a reaction rather than a message: the board carries the
    # price a tick later, and forty replies inside one lot is the flood this
    # design exists to avoid. Entirely best-effort — the bid is committed.
    if landed is not None:
        await _react(context, update)


async def _react(context, update):
    try:
        from telegram import ReactionTypeEmoji
        message = update.effective_message
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id, message_id=message.message_id,
            reaction=[ReactionTypeEmoji("👍")])
    except Exception:
        logger.debug("auction: could not react to the bid", exc_info=True)


async def bid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A quick-bid button. Shared by the room, authorised per press."""
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    try:
        rest = (query.data or "")[len(BID_CB):]
        lot_id, amount = rest.split("_", 1)
        lot_id, amount = int(lot_id), int(amount)
    except (ValueError, AttributeError):
        await query.answer("That button is from an older auction.", show_alert=True)
        return

    session = get_session()
    try:
        from models import AuctionLot
        season = A.season_for_chat(session, update.effective_chat.id)
        if season is None:
            await query.answer("No auction is running here.", show_alert=True)
            return
        franchise = A.franchise_for_actor(session, season.id, user.id)
        if franchise is None:
            await query.answer("Only a franchise owner or co-owner can bid.",
                               show_alert=True)
            return
        lot = session.query(AuctionLot).filter(AuctionLot.id == lot_id).first()
        A.place_bid(session, season, lot, franchise, amount,
                    by_tg_id=user.id, source="button")
        session.commit()
        await query.answer(f"Bid {A.render_money(amount, season.currency_label)}")
    except AuctionError as exc:
        session.rollback()
        await query.answer(str(exc)[:190], show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("auction quick-bid failed")
        await query.answer("That did not land — try again.", show_alert=True)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Reading the room — open to anyone
# ════════════════════════════════════════════════════════════════════

async def aboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A personal copy of the board, with the quick-bid buttons under it."""
    session = get_session()
    try:
        season = _season_for(session, update)
        if season is None:
            await _reply(update, NO_AUCTION)
            return
        lot = A.current_lot(session, season)
        await _reply(update, A.render_board(session, season, lot),
                     reply_markup=bid_keyboard(season, lot))
    finally:
        session.close()


async def apurse_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session()
    try:
        season = _season_for(session, update)
        if season is None:
            await _reply(update, NO_AUCTION)
            return
        name = _arg_text(context)
        if name:
            franchise = _find_franchise(session, season, name)
            await _reply(update, A.render_squad(session, season, franchise))
            return
        await _reply(update, A.render_purses(session, season))
    except AuctionError as exc:
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
    finally:
        session.close()


def _find_franchise(session, season, name):
    """Find a franchise the way somebody types it — never guess between two."""
    wanted = (name or "").strip().lower()
    field = A.franchises(session, season.id)
    exact = [f for f in field if (f.name or "").lower() == wanted
             or (f.short_name or "").lower() == wanted]
    if exact:
        return exact[0]
    hits = [f for f in field if (f.name or "").lower().startswith(wanted)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise AuctionError("That could be " +
                           ", ".join(f.name for f in hits[:5]) +
                           " — type more of the name.")
    raise AuctionError(f"No franchise here is called “{name}”.")


# ════════════════════════════════════════════════════════════════════
# Admin
# ════════════════════════════════════════════════════════════════════

ADMIN_CARD = """🔨 <b>Franchise Auction — admin</b>

<b>Setting up</b>
<code>/anew &lt;name&gt;</code> — create an auction and bind it to this group
<code>/abind</code> — bind an existing auction to this group
<code>/atimer &lt;seconds&gt;</code> — seconds per lot (default 30)
<code>/asnipe &lt;window&gt; &lt;extend&gt; &lt;max&gt;</code> — anti-snipe, e.g. <code>/asnipe 10 10 5</code>
<code>/aco &lt;franchise&gt; | &lt;telegram id&gt;</code> — let one more person bid for a franchise

<b>Running it</b>
<code>/astart</code> · <code>/apause</code> · <code>/aresume</code>
<code>/anext</code> — put the next lot on the block
<code>/aextend [seconds]</code> — add time to the lot on the block
<code>/asold</code> — sell at the standing bid
<code>/aunsold</code> — pass the lot (refused while a bid stands)
<code>/aundobid</code> — void the standing bid and fall back
<code>/awithdraw &lt;player&gt;</code> — pull a player out of the auction

<b>Money and the end</b>
<code>/agrant &lt;franchise&gt; | &lt;amount&gt;</code> — correct a purse, e.g. <code>| -2</code> or <code>| 5</code>
<code>/apublish</code> — publish the bought squads as a Challenge League
<code>/acancel</code> — cancel the auction

<b>Everyone</b>
<code>/bid [amount]</code> — bare <code>/bid</code> bids the next minimum
<code>/aboard</code> — the live board · <code>/apurse [franchise]</code> — purses, or one squad

Amounts are in <b>crore</b> unless you say lakh: <code>/bid 15</code> is ₹15 Cr,
<code>/bid 75L</code> is ₹75 lakh."""


async def aadmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _require_admin(update):
        return
    await _reply(update, ADMIN_CARD)


async def anew_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    if not await _require_admin(update):
        return
    name = _arg_text(context)
    if not name:
        await _reply(update, "Usage: <code>/anew Season 2</code>")
        return
    session = get_session()
    try:
        season = A.create_season(session, name)
        A.bind_chat(session, season, chat.id)
        session.commit()
        await _reply(update,
                     f"🔨 <b>{html.escape(season.name)}</b> created and bound "
                     f"to this group.\nBuild the pool and the franchises on the "
                     f"website, then <code>/astart</code>. "
                     f"<code>/auction</code> lists every command.")
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
    except Exception:
        session.rollback()
        logger.exception("/anew failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
    finally:
        session.close()


async def abind_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    if not await _require_admin(update):
        return
    name = _arg_text(context)
    if not name:
        await _reply(update, "Usage: <code>/abind Season 2</code>")
        return
    session = get_session()
    try:
        from models import AuctionSeason
        from sqlalchemy import func as _func
        season = (session.query(AuctionSeason)
                  .filter(_func.lower(AuctionSeason.name) == name.lower()).first())
        if season is None:
            await _reply(update, f"⚠️ No auction called “{html.escape(name)}”.")
            return
        A.bind_chat(session, season, chat.id)
        session.commit()
        await _reply(update, f"🔗 <b>{html.escape(season.name)}</b> is now bound "
                             f"to this group.")
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
    finally:
        session.close()


async def astart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    def work(session, season):
        A.start(session, season, by_tg_id=user.id if user else None)
        return None      # the sweeper announces it, within a tick

    await _with_auction(update, work, admin=True, context=context)


async def apause_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    def work(session, season):
        A.pause(session, season, by_tg_id=user.id if user else None)
        return None

    await _with_auction(update, work, admin=True, context=context)


async def anext_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, season):
        A.open_next_lot(session, season)
        return None

    await _with_auction(update, work, admin=True, context=context)


async def aextend_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    seconds = _arg_text(context)

    def work(session, season):
        lot = A.current_lot(session, season)
        A.extend_timer(session, season, lot, seconds,
                       by_tg_id=user.id if user else None)
        return None

    await _with_auction(update, work, admin=True, context=context)


async def asold_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    def work(session, season):
        lot = A.current_lot(session, season)
        A.sell_lot(session, season, lot, by_tg_id=user.id if user else None,
                   by_admin=True)
        return None

    await _with_auction(update, work, admin=True, context=context)


async def aunsold_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    def work(session, season):
        lot = A.current_lot(session, season)
        A.pass_lot(session, season, lot, by_tg_id=user.id if user else None,
                   by_admin=True)
        return None

    await _with_auction(update, work, admin=True, context=context)


async def aundobid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    def work(session, season):
        lot = A.current_lot(session, season)
        A.undo_last_bid(session, season, lot,
                        by_tg_id=user.id if user else None)
        return None

    await _with_auction(update, work, admin=True, context=context)


async def awithdraw_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = _arg_text(context)

    def work(session, season):
        if not name:
            raise AuctionError("Usage: /awithdraw <player name>")
        lot = _find_lot(session, season, name)
        A.withdraw_lot(session, season, lot,
                       by_tg_id=user.id if user else None)
        return None

    await _with_auction(update, work, admin=True, context=context)


def _find_lot(session, season, name):
    """Find a pooled player by name. Never guesses between two."""
    from models import AuctionLot
    wanted = (name or "").strip().lower()
    rows = (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season.id).all())
    exact = [lot for lot in rows if (lot.name or "").lower() == wanted]
    if exact:
        return exact[0]
    hits = [lot for lot in rows if wanted in (lot.name or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise AuctionError("That could be " +
                           ", ".join(lot.name for lot in hits[:5]) +
                           " — type more of the name.")
    raise AuctionError(f"“{name}” is not in this auction's pool.")


async def atimer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = _arg_text(context)

    def work(session, season):
        if not raw:
            return (f"⏱ A lot runs for <b>{season.bid_seconds}s</b>.\n"
                    f"Change it with <code>/atimer 45</code>.")
        seconds = A.set_timer(session, season, raw)
        return f"⏱ A lot now runs for <b>{seconds}s</b>."

    await _with_auction(update, work, admin=True, context=context)


async def asnipe_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = list(context.args or [])

    def work(session, season):
        if not args:
            return (f"🛡 <b>Anti-snipe</b>\n"
                    f"A bid inside the last <b>{season.snipe_window_seconds}s</b> "
                    f"pushes the clock to <b>{season.snipe_extend_seconds}s</b>, "
                    f"up to <b>{season.max_extensions}</b> times per lot.\n"
                    f"Change it with <code>/asnipe 10 10 5</code>.\n"
                    f"<i>Setting the window equal to the extension gives the "
                    f"rule most rooms want: any bid in the last N seconds gives "
                    f"everyone N more.</i>")
        if len(args) != 3:
            raise AuctionError("Usage: /asnipe <window> <extend> <max>, "
                               "e.g. /asnipe 10 10 5")
        window, extend, cap = A.set_anti_snipe(session, season, *args)
        return (f"🛡 A bid inside the last <b>{window}s</b> now pushes the clock "
                f"to <b>{extend}s</b>, up to <b>{cap}</b> times per lot.")

    await _with_auction(update, work, admin=True, context=context)


async def agrant_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    raw = _arg_text(context)

    def work(session, season):
        if "|" not in raw:
            raise AuctionError("Usage: /agrant <franchise> | <amount>  — for "
                               "example  /agrant Mumbai | -2  (take ₹2 Cr back) "
                               "or  /agrant Mumbai | 5  (add ₹5 Cr)")
        name, amount_text = [part.strip() for part in raw.split("|", 1)]
        franchise = _find_franchise(session, season, name)
        negative = amount_text.lstrip().startswith("-")
        amount = A.parse_amount(amount_text.lstrip().lstrip("-"))
        new_balance = A.correct_purse(session, season, franchise,
                                      -amount if negative else amount,
                                      note="Admin correction",
                                      by_tg_id=user.id if user else None)
        return (f"🧾 {html.escape(franchise.name)} now has "
                f"<b>{A.render_money(new_balance, season.currency_label)}</b>.")

    await _with_auction(update, work, admin=True, context=context)


async def aco_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = _arg_text(context)

    def work(session, season):
        if "|" not in raw:
            raise AuctionError("Usage: /aco <franchise> | <telegram id>")
        name, tg_id = [part.strip() for part in raw.split("|", 1)]
        franchise = _find_franchise(session, season, name)
        ids = A.add_co_owner(session, franchise, tg_id)
        return (f"🤝 {html.escape(franchise.name)} now has "
                f"<b>{len(ids)}</b> co-owner(s) who may bid.")

    await _with_auction(update, work, admin=True, context=context)


async def apublish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def work(session, season):
        league = A.publish_to_league(session, season)
        return (f"📤 Squads published to <b>{html.escape(league.name)}</b>. "
                f"They can play immediately.")

    await _with_auction(update, work, admin=True, context=context)


async def acancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    def work(session, season):
        A.cancel(session, season, by_tg_id=user.id if user else None)
        return None

    await _with_auction(update, work, admin=True, context=context)
