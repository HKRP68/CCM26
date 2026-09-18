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
RTM_CB = "au_rtm_"


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
    if lot is not None and lot.status == A.LOT_RTM_OFFERED:
        # The holder's two answers. The top bidder's final raise is an ordinary
        # /bid, so it needs no button of its own.
        if lot.rtm_stage in (A.RTM_INTENT, A.RTM_DECISION):
            # The lot AND the stage ride in the callback data, for the same
            # reason the quick-bid buttons carry the exact price: a "yes" from
            # the *intent* prompt, pressed thirty seconds late, would otherwise
            # land as a MATCH at the decision stage — signing a player for the
            # raised number when the franchise only ever agreed to the old one.
            tag = f"{RTM_CB}{lot.id}_{lot.rtm_stage}_"
            label = ("🪪 Use RTM" if lot.rtm_stage == A.RTM_INTENT
                     else f"🪪 Match {A.render_money(A.rtm_price(season, lot), season.currency_label)}")
            return InlineKeyboardMarkup([[
                InlineKeyboardButton(label, callback_data=f"{tag}yes"),
                InlineKeyboardButton("Pass", callback_data=f"{tag}no"),
            ]])
        return None
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


async def artm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/artm yes</code> / <code>/artm no</code> — the RTM holder answers.

    One command for both questions the rule asks a franchise, because from
    where they are sitting it is the same question twice: *do you want him at
    this price?* The bot has already said which stage it is on and what the
    number is; making them remember two verbs under a thirty-second clock
    would be a way to lose the lot to a typo.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    user = update.effective_user
    raw = _arg_text(context).strip().lower()

    session = get_session()
    try:
        season = A.season_for_chat(session, chat.id)
        if season is None:
            await _reply(update, NO_AUCTION)
            return
        lot = A.current_lot(session, season)
        if lot is None or lot.status != A.LOT_RTM_OFFERED:
            await _reply(update, "⚠️ There is no Right To Match to answer "
                                 "right now.")
            return
        franchise = A.franchise_for_actor(session, season.id, user.id)
        if franchise is None:
            await _reply(update, NOT_YOURS)
            return

        if raw in ("yes", "y", "use", "match", "rtm"):
            wants = True
        elif raw in ("no", "n", "pass", "decline"):
            wants = False
        else:
            holder = A.rtm_holder(session, lot)
            price = A.render_money(A.rtm_price(season, lot),
                                   season.currency_label)
            question = ("use your Right To Match?"
                        if lot.rtm_stage == A.RTM_INTENT
                        else f"match at {price}?")
            await _reply(update,
                         f"🪪 {html.escape(holder.name) if holder else ''} — "
                         f"{question}\nAnswer <code>/artm yes</code> or "
                         f"<code>/artm no</code>.")
            return

        if lot.rtm_stage == A.RTM_INTENT:
            A.rtm_intent(session, season, lot, franchise, wants,
                         by_tg_id=user.id)
        elif lot.rtm_stage == A.RTM_DECISION:
            A.rtm_decide(session, season, lot, franchise, wants,
                         by_tg_id=user.id)
        else:
            await _reply(update, "⚠️ It is the top bidder's turn — they have "
                                 "one final raise.")
            return
        session.commit()
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
        return
    except Exception:
        session.rollback()
        logger.exception("/artm failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
        return
    finally:
        session.close()
    # The sweeper announces the outcome within a tick, like every other
    # auction action — nothing here talks to the room directly.
    await _react(context, update)


async def rtm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The board's Use it / Pass buttons. Shared, authorised per press."""
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    try:
        lot_id, stage, answer = (query.data or "")[len(RTM_CB):].split("_", 2)
        lot_id = int(lot_id)
    except (ValueError, AttributeError):
        await query.answer("That button is from an older auction.",
                           show_alert=True)
        return

    session = get_session()
    try:
        season = A.season_for_chat(session, update.effective_chat.id)
        if season is None:
            await query.answer("No auction is running here.", show_alert=True)
            return
        lot = A.current_lot(session, season)
        if lot is None or lot.status != A.LOT_RTM_OFFERED or lot.id != lot_id:
            await query.answer("That Right To Match has been answered.",
                               show_alert=True)
            return
        if lot.rtm_stage != stage:
            # The window moved on under them. Refusing beats guessing: the two
            # stages ask different questions about different numbers.
            await query.answer("That window has closed — check the board for "
                               "what is being asked now.", show_alert=True)
            return
        franchise = A.franchise_for_actor(session, season.id, user.id)
        if franchise is None:
            await query.answer("Only the franchise that held this player can "
                               "answer.", show_alert=True)
            return
        wants = answer == "yes"
        if lot.rtm_stage == A.RTM_INTENT:
            A.rtm_intent(session, season, lot, franchise, wants,
                         by_tg_id=user.id)
        elif lot.rtm_stage == A.RTM_DECISION:
            A.rtm_decide(session, season, lot, franchise, wants,
                         by_tg_id=user.id)
        else:
            await query.answer("It is the top bidder's turn.", show_alert=True)
            return
        session.commit()
        await query.answer("Noted." if wants else "Passed.")
    except AuctionError as exc:
        session.rollback()
        await query.answer(str(exc)[:190], show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("auction rtm button failed")
        await query.answer("That did not land — try again.", show_alert=True)
    finally:
        session.close()


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

<b>Retention</b> — before the auction opens
<code>/aretlock</code> — the state of retention, and every franchise's keeps
<code>/aretain &lt;franchise&gt; | &lt;player&gt; | [price]</code> — leave the price off and the ladder decides
<code>/aunretain &lt;player&gt;</code> — release one, back into the pool
<code>/aretlock on</code> — close the window (<code>/astart</code> closes it too)

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


async def aretain_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aretain Mumbai | Virat Kohli | 18</code> — the price is optional.

    Left off, it takes the season's retention ladder for that franchise's next
    slab, which is the number the admin almost always wants and the one the
    reply prints back so they can see what they just spent.
    """
    user = update.effective_user
    raw = _arg_text(context)

    def work(session, season):
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise AuctionError(
                "Usage: /aretain <franchise> | <player> | [price]\n"
                "The price is optional — leave it off and the retention "
                "ladder decides.")
        franchise = _find_franchise(session, season, parts[0])
        player = _find_player(session, parts[1])
        price = (A.parse_amount(parts[2])
                 if len(parts) > 2 and parts[2] else None)
        lot = A.retain(session, season, franchise, player, price,
                       by_tg_id=user.id if user else None)
        kept = int(franchise.retained_count or 0)
        warning = ""
        held = A.previous_squad_map(session, season).get(player.id)
        if held is not None and held.id != franchise.id:
            warning = (f"\n⚠️ {html.escape(player.name)} was "
                       f"{html.escape(held.name)}'s last season — retained "
                       f"anyway.")
        elif held is None and season.previous_league_id:
            warning = (f"\n⚠️ {html.escape(player.name)} was not in last "
                       f"season's league — retained anyway.")
        return (f"🔒 <b>{html.escape(franchise.name)}</b> retain "
                f"{html.escape(lot.name)} for "
                f"<b>{A.render_money(lot.sold_price_lakh, season.currency_label)}</b> "
                f"({kept}/{season.max_retentions}).\n"
                f"💰 {A.render_money(franchise.purse_remaining_lakh, season.currency_label)} "
                f"left to bid with." + warning)

    await _with_auction(update, work, admin=True, context=context)


def _find_player(session, name):
    """A master card by name. Never guesses between two.

    Retaining the wrong card costs a franchise real money and an admin a
    release to undo, so an ambiguous name asks for more of it rather than
    picking the best match — the same call ``/pick`` makes in the draft.
    """
    from services import player_query
    wanted = (name or "").strip().lower()
    query = player_query.master_player_query(session, {"q": wanted})
    rows = player_query.ordered(query).limit(30).all()
    exact = [p for p in rows if (p.name or "").lower() == wanted]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        editions = sorted({p.version or "Base" for p in exact})
        if len(editions) > 1:
            raise AuctionError(f"There are {len(exact)} cards called “{name}” — "
                               f"say which edition: " + ", ".join(editions))
        raise AuctionError(
            f"There are {len(exact)} cards called “{name}”, all {editions[0]}. "
            f"Retain from the auction's setup page, where they can be told "
            f"apart.")
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        raise AuctionError("That could be " +
                           ", ".join(p.name for p in rows[:5]) +
                           " — type more of the name.")
    raise AuctionError(f"No player called “{name}”.")


async def aunretain_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Release a retained player back into the auction pool."""
    user = update.effective_user
    name = _arg_text(context)

    def work(session, season):
        if not name:
            raise AuctionError("Usage: /aunretain <player name>")
        lot = _find_lot(session, season, name)
        if (lot.acquisition or A.ACQ_AUCTION) != A.ACQ_RETAINED:
            raise AuctionError(f"{lot.name} is not retained.")
        from models import AuctionFranchise
        franchise = (session.query(AuctionFranchise)
                     .filter(AuctionFranchise.id == lot.sold_to_id).first())
        A.unretain(session, season, franchise, lot,
                   by_tg_id=user.id if user else None)
        return (f"🔓 {html.escape(lot.name)} released — back in the pool, and "
                f"{html.escape(franchise.name)} has "
                f"<b>{A.render_money(franchise.purse_remaining_lakh, season.currency_label)}</b>.")

    await _with_auction(update, work, admin=True, context=context)


async def aretlock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """With no arguments, the state of retention. With ``on``, close it.

    The readout matters more than the switch: the deadline is enforced lazily
    (nothing sweeps while an auction is in setup), so an admin who cannot see
    how long is left only finds out when it refuses them.
    """
    user = update.effective_user
    arg = _arg_text(context).strip().lower()

    def work(session, season):
        if arg in ("on", "close", "lock"):
            A.lock_retention(session, season,
                             by_tg_id=user.id if user else None)
            return "🔒 Retention is closed."

        symbol = season.currency_label
        if not A.retention_configured(season):
            return ("🔓 This auction allows no retentions. Set a maximum on "
                    "the auction's setup page to turn retention on.")
        left = A.retention_seconds_left(season)
        if A.retention_locked(season):
            window = "🔒 <b>Closed</b>"
        elif left is None:
            window = "🔓 <b>Open</b> — no deadline set"
        elif left > 0:
            window = f"🔓 <b>Open</b> — closes in {A.format_clock(left)}"
        else:
            window = f"🔒 <b>Closed</b> — the deadline passed {A.format_clock(-left)} ago"

        lines = [f"🔒 <b>Retention</b> — {window}",
                 f"Up to <b>{season.max_retentions}</b> per franchise"
                 + (f", at least <b>{season.min_retentions}</b>"
                    if season.min_retentions else "")]
        if season.retention_max_spend_lakh is not None:
            lines.append(f"Budget: "
                         f"{A.render_money(season.retention_max_spend_lakh, symbol)}")
        ladder = ", ".join(A.render_money(r["price_lakh"], symbol)
                           for r in A.retention_price_rules(season))
        lines.append(f"Ladder: {ladder}")
        lines.append("")
        for f in A.franchises(session, season.id):
            kept = A.retained(session, f.id)
            lines.append(
                f"<b>{html.escape(f.name)}</b> — {len(kept)}"
                f"/{season.max_retentions} · "
                f"{A.render_money(A.retention_spent(session, f.id), symbol)}")
            for lot in kept:
                lines.append(f"   🔒 {html.escape(lot.name)} — "
                             f"{A.render_money(lot.sold_price_lakh, symbol)}")
        return "\n".join(lines)

    await _with_auction(update, work, admin=True, context=context)


async def aclone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aclone Season 3</code> — start the next season from this one.

    Every rule and every franchise, owners included, carried over. Deliberately
    does <b>not</b> bind the new auction to this group: the old season's pinned
    board is still here, and stealing the group out from under it would leave
    that board answering for an auction nobody is running. /abind when the
    current one is finished.
    """
    user = update.effective_user
    raw = _arg_text(context).strip()

    def work(session, season):
        if not raw:
            raise AuctionError("Usage: /aclone <name>, e.g. /aclone Season 3")
        fresh = A.clone_season(session, season, raw,
                               by_tg_id=user.id if user else None)
        field = A.franchises(session, fresh.id)
        lines = [f"🌱 <b>{html.escape(fresh.name)}</b> starts from "
                 f"<b>{html.escape(season.name)}</b> — {len(field)} franchises "
                 f"and every rule carried over."]
        if fresh.previous_league_id:
            lines.append("It follows last season's published league, so "
                         "retention and Right To Match already know who held "
                         "whom.")
        else:
            lines.append(f"⚠️ {html.escape(season.name)} was never published, "
                         f"so there is no record of last season's squads yet. "
                         f"Publish it, then link the league on the new "
                         f"auction's setup page.")
        lines.append("")
        lines.append("Next: build the pool, then <code>/abind</code> in this "
                     "group once this auction is done.")
        return "\n".join(lines)

    await _with_auction(update, work, admin=True, context=context)


async def aaccel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aaccel</code> — every unsold player back into the queue at once.

    With no arguments it reads out who would come back, because "re-list 43
    players" is not a thing to do from muscle memory at the end of a long
    auction. <code>/aaccel go</code> does it.
    """
    user = update.effective_user
    arg = _arg_text(context).strip().lower()

    def work(session, season):
        rows = A.unsold(session, season.id)
        if not rows:
            return "⚡ Nothing went unsold — there is nothing to re-list."
        if arg not in ("go", "yes", "confirm", "do"):
            symbol = season.currency_label
            names = "\n".join(
                f"   • {html.escape(lot.name)} · {lot.rating} · "
                f"{A.render_money(lot.base_price_lakh, symbol)}"
                for lot in rows[:15])
            more = (f"\n   …and {len(rows) - 15} more" if len(rows) > 15 else "")
            return (f"⚡ <b>Accelerated round</b> — {len(rows)} unsold "
                    f"{'player' if len(rows) == 1 else 'players'} would go back "
                    f"into the pool at the same base price:\n{names}{more}\n\n"
                    f"Confirm with <code>/aaccel go</code>.")
        back = A.relist_all(session, season,
                            by_tg_id=user.id if user else None)
        return (f"⚡ {len(back)} "
                f"{'player is' if len(back) == 1 else 'players are'} back in "
                f"the pool.")

    await _with_auction(update, work, admin=True, context=context)


async def artmset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The Right To Match rules: read them, or set them in one line.

    <code>/artmset 2</code> — two cards each, the default 30s window, no
    premium. <code>/artmset 2 45 200</code> — the same with a 45s window and
    the proposal's optional premium on top of the final bid.
    <code>/artmset off</code> turns it off without touching the numbers, so
    turning it back on does not mean typing them again.
    """
    args = list(context.args or [])

    def work(session, season):
        symbol = season.currency_label
        if args and args[0].lower() in ("off", "no", "none"):
            A.set_rtm_rules(session, season, enabled=False)
            return "🪪 Right To Match is off."
        if args:
            if len(args) > 3:
                raise AuctionError("Usage: /artmset <cards> [seconds] "
                                   "[premium], e.g. /artmset 2 30")
            seconds = args[1] if len(args) > 1 else None
            # The premium is money, so it is read the way every other amount
            # in this feature is: "2" means two crore, not two lakh.
            extra = A.parse_amount(args[2]) if len(args) > 2 else None
            A.set_rtm_rules(session, season, enabled=True, per_team=args[0],
                            window_seconds=seconds, extra_lakh=extra)

        if not A.rtm_configured(season):
            return ("🪪 Right To Match is <b>off</b>.\n"
                    "Turn it on with <code>/artmset 2</code> — two cards each.")
        lines = [f"🪪 <b>Right To Match</b> — on",
                 f"{season.rtm_per_team} card(s) each · "
                 f"{season.rtm_window_seconds}s to answer each question"
                 + (f" · premium {A.render_money(season.rtm_extra_lakh, symbol)}"
                    if season.rtm_extra_lakh else "")]
        lines.append("")
        for franchise in A.franchises(session, season.id):
            lines.append(f"<b>{html.escape(franchise.name)}</b> — "
                         f"{A.rtm_cards_left(franchise)} left of "
                         f"{int(franchise.rtm_cards_total or 0)}")
        lines.append("")
        lines.append("<i>Give one franchise a different number with "
                     "<code>/artmcards &lt;franchise&gt; &lt;n&gt;</code>.</i>")
        return "\n".join(lines)

    await _with_auction(update, work, admin=True, context=context)


async def artmcards_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/artmcards Mumbai 3</code> — one franchise's own card count."""
    args = list(context.args or [])

    def work(session, season):
        if len(args) < 2:
            raise AuctionError("Usage: /artmcards <franchise> <cards>, "
                               "e.g. /artmcards Mumbai 2")
        franchise = _find_franchise(session, season, " ".join(args[:-1]))
        A.set_rtm_cards(session, season, franchise, args[-1])
        return (f"🪪 {html.escape(franchise.name)} now holds "
                f"<b>{A.rtm_cards_left(franchise)}</b> Right To Match of "
                f"{int(franchise.rtm_cards_total or 0)}.")

    await _with_auction(update, work, admin=True, context=context)


async def artmforce_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Answer the open RTM window on the franchise's behalf.

    For the owner whose phone died mid-window. It is the same three
    transitions the franchise itself would drive, so the announcement, the
    card and the ledger all read exactly as they would have — the audit log
    is where it shows that an admin did it.
    """
    user = update.effective_user
    raw = _arg_text(context).strip().lower()

    def work(session, season):
        lot = A.current_lot(session, season)
        if lot is None or lot.status != A.LOT_RTM_OFFERED:
            raise AuctionError("There is no Right To Match open right now.")
        by = user.id if user else None
        if lot.rtm_stage == A.RTM_FINAL_OFFER:
            if raw and raw not in ("stand", "no", "n"):
                raise AuctionError("At the final offer the only thing to "
                                   "force is standing pat: /artmforce stand. "
                                   "A raise has to be a real bid.")
            A.rtm_to_decision(session, season, lot)
            return "🪪 The final-offer window is closed — over to the holder."
        if raw in ("yes", "y", "use", "match"):
            wants = True
        elif raw in ("no", "n", "pass", "decline"):
            wants = False
        else:
            raise AuctionError("Usage: /artmforce yes|no")
        holder = A.rtm_holder(session, lot)
        if lot.rtm_stage == A.RTM_INTENT:
            A.rtm_intent(session, season, lot, holder, wants, by_tg_id=by)
        else:
            A.rtm_decide(session, season, lot, holder, wants, by_tg_id=by)
        return ("🪪 Answered on their behalf." if wants
                else "🪪 Declined on their behalf.")

    await _with_auction(update, work, admin=True, context=context)


async def artmundo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/artmundo Ashwin</code> — undo a match, card and money and all."""
    raw = _arg_text(context).strip()

    def work(session, season):
        if not raw:
            raise AuctionError("Usage: /artmundo <player>")
        lot = _find_lot(session, season, raw)
        if lot.acquisition != A.ACQ_RTM:
            raise AuctionError(f"{lot.name} was not signed with a Right To "
                               f"Match. Use /aundo for an auction sale.")
        A.undo_rtm(session, season, lot,
                   by_tg_id=update.effective_user.id if update.effective_user
                   else None)
        return (f"↩️ The Right To Match on {html.escape(lot.name)} is undone — "
                f"the money and the card are both back, and he is on the "
                f"block again.")

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
