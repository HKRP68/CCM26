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
construction, since there is no Telegram call after the commit at all. The
two exceptions are replies by nature: ``/acall`` is itself the message (it
tags every owner), and ``/aretain`` answers with the offer card the franchise
presses Accept on.

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
from services import auction_rich as AR
from services import auction_service as A
from services.admin_ids import is_admin
from services.auction_service import AuctionError

logger = logging.getLogger(__name__)

GROUP_CHAT_TYPES = ("group", "supergroup")

NOT_ADMIN = "⛔ Only auction admins can manage an auction."
NOT_BOT_ADMIN = "⛔ Only bot admins can choose who runs auctions."
GROUP_ONLY = ("❌ Auction commands only work in the group the auction is bound "
              "to.\nAn admin binds one with <code>/abind</code>.")
NO_AUCTION = ("❌ No auction is running in this chat.\n"
              "An admin can create one with <code>/anew &lt;name&gt;</code> and "
              "bind it here with <code>/abind</code>.")
NO_AUCTION_DM = ("❌ You do not have a franchise in any auction that is "
                 "running.\nAsk in the auction's group — every one of these "
                 "views works there.")
NOT_YOURS = ("⛔ Only a franchise's owner or a co-owner can bid for it.\n"
             "Admins deliberately cannot bid on someone's behalf — an admin "
             "who must act for an absent owner uses the auction console, which "
             "records the bid as an admin's.")

BID_CB = AR.BID_CB
RTM_CB = AR.RTM_CB
INFO_CB = AR.INFO_CB
RET_CB = "au_ret_"


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


def _is_auction_admin(user_id):
    """A bot admin, or an auction admin — who may run every auction command.

    Opens its own short session: the gate runs before the command's own
    session exists, and a bot admin never needs the query at all.
    """
    if user_id is None:
        return False
    if is_admin(user_id):
        return True
    session = get_session()
    try:
        return A.is_auction_admin(session, user_id)
    finally:
        session.close()


async def _require_admin(update):
    user = update.effective_user
    if not user or not _is_auction_admin(user.id):
        await _reply(update, NOT_ADMIN)
        return False
    return True


async def _require_bot_admin(update):
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _reply(update, NOT_BOT_ADMIN)
        return False
    return True


async def _reply_rich(update, context, blocks, html_text, *, reply_markup=None):
    """Answer with a rich message, falling back to the HTML rendering.

    Quotes the command in a group, the way ``/pxi`` does, so the answer is
    attached to whoever asked for it in a busy auction chat.
    """
    chat = update.effective_chat
    bot = getattr(context, "bot", None)
    if chat is not None and bot is not None and getattr(bot, "_post", None):
        quote = None
        message = update.effective_message
        if chat.type in GROUP_CHAT_TYPES and message is not None:
            quote = getattr(message, "message_id", None)
        try:
            return await AR.send(bot, chat.id, blocks, html_text,
                                 reply_markup=reply_markup,
                                 reply_to_message_id=quote)
        except Exception:
            logger.warning("auction rich reply failed; replying in HTML",
                           exc_info=True)
    parts = AR.html_parts(html_text)
    sent = None
    for index, part in enumerate(parts):
        sent = await _reply(update, part,
                            reply_markup=reply_markup if index == len(parts) - 1 else None)
    return sent


def _no_auction(update):
    """"No auction" written for where it is being said."""
    chat = update.effective_chat
    if chat is not None and chat.type not in GROUP_CHAT_TYPES:
        return NO_AUCTION_DM
    return NO_AUCTION


def _season_for(session, update):
    chat = update.effective_chat
    if chat is None:
        return None
    return A.season_for_chat(session, chat.id)


def _read_season(session, update):
    """The auction a **read-only** view should answer about.

    This chat's, whenever one is bound here. Failing that — and only in a DM —
    the auction this user has a team in.

    Bidding stays in the group for the reason ``AuctionSeason.chat_id`` gives:
    a bid nobody in the room saw is how a price gets disputed. Reading carries
    none of that, and before the auction starts it is the other way round:
    working out what your purse can reach, what the sets hold and what
    retention already cost you is homework, done the night before, and making
    somebody do it in the group means either not doing it or doing it in front
    of the people they are about to bid against.
    """
    season = _season_for(session, update)
    if season is not None:
        return season
    chat = update.effective_chat
    if chat is not None and chat.type in GROUP_CHAT_TYPES:
        # A group with no auction bound to it is not somebody's DM, and
        # answering with the auction they own elsewhere would put one room's
        # numbers in another room.
        return None
    user = update.effective_user
    return A.season_for_actor(session, user.id) if user else None


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
    """Quick-bid buttons (or the RTM holder's answers) for this lot.

    Lives in ``services.auction_rich`` so the sweeper can put the same buttons
    on the pinned board without importing a handler module.
    """
    return AR.bid_keyboard(season, lot)


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
        season = _read_season(session, update)
        if season is None:
            await _reply(update, _no_auction(update))
            return
        lot = A.current_lot(session, season)
        # The quick-bid buttons only ride on a board shown *in the room*: a
        # bid is refused outside the bound group, so a button offered anywhere
        # else is one that can only ever answer "not here".
        chat = update.effective_chat
        in_room = chat is not None and season.chat_id == chat.id
        await _reply_rich(update, context, AR.board_blocks(session, season, lot),
                          AR.board_html(session, season, lot),
                          reply_markup=bid_keyboard(season, lot)
                          if in_room else None)
    except AuctionError as exc:
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
    finally:
        session.close()


async def apurse_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session()
    try:
        season = _read_season(session, update)
        if season is None:
            await _reply(update, _no_auction(update))
            return
        name = _arg_text(context)
        if name:
            franchise = _find_franchise(session, season, name)
            await _reply_rich(update, context,
                              *AR.squad_view(session, season, franchise))
            return
        await _reply_rich(update, context, *AR.purses_view(session, season))
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

async def aadmin_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/adminhelp</code> (and <code>/auction</code>) — every admin command.

    Open to auction admins as well as bot admins; the section on appointing
    auction admins is shown only to the bot admins who can use it.
    """
    if not await _require_admin(update):
        return
    user = update.effective_user
    blocks, html_text = AR.admin_help(bot_admin=bool(user and is_admin(user.id)))
    await _reply_rich(update, context, blocks, html_text)


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


def _retention_args(session, season, raw, usage):
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise AuctionError(usage)
    franchise = _find_franchise(session, season, parts[0])
    player = _find_player(session, parts[1])
    price = (A.parse_amount(parts[2])
             if len(parts) > 2 and parts[2] else None)
    return franchise, player, price


def _holder_warning(session, season, franchise, player):
    held = A.previous_squad_map(session, season).get(player.id)
    if held is not None and held.id != franchise.id:
        return (f"\n⚠️ {html.escape(player.name)} was "
                f"{html.escape(held.name)}'s last season.")
    if held is None and season.previous_league_id:
        return (f"\n⚠️ {html.escape(player.name)} was not in last "
                f"season's league.")
    return ""


async def aretain_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aretain Mumbai | Virat Kohli | 18</code> — offer a retention.

    Nothing is signed until the franchise answers: the reply carries Accept /
    Decline buttons only that franchise's owner or a co-owner can press.
    Left off, the price is the retention ladder's next slab *at the moment
    they accept*. <code>/aretainforce</code> keeps the old instant path.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    if not await _require_admin(update):
        return
    user = update.effective_user
    raw = _arg_text(context)
    session = get_session()
    try:
        season = _season_for(session, update)
        if season is None:
            await _reply(update, NO_AUCTION)
            return
        franchise, player, price = _retention_args(
            session, season, raw,
            "Usage: /aretain <franchise> | <player> | [price]\n"
            "The franchise then accepts it with a button. The price is "
            "optional — leave it off and the retention ladder decides.")
        offer = A.offer_retention(session, season, franchise, player, price,
                                  by_tg_id=user.id if user else None,
                                  chat_id=chat.id)
        session.commit()
        text = (AR.retention_offer_card(session, season, offer)
                + _holder_warning(session, season, franchise, player))
        try:
            sent = await _reply(update, text,
                                reply_markup=AR.retention_offer_keyboard(offer))
        except Exception:
            # The offer is committed but nobody can see its buttons. Withdraw
            # it rather than leave a pending offer that blocks the next one
            # for this player until somebody notices.
            session.rollback()
            A.cancel_retention_offer(session, season, offer)
            session.commit()
            raise
        message_id = getattr(sent, "message_id", None)
        if message_id:
            offer.message_id = message_id
            session.commit()
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
    except Exception:
        session.rollback()
        logger.exception("/aretain failed")
        try:
            await _reply(update, "⚠️ Something went wrong. Try again.")
        except Exception:
            # The failure may well have been the chat refusing messages.
            logger.debug("/aretain: could not report the failure",
                         exc_info=True)
    finally:
        session.close()


async def aretainforce_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aretainforce Mumbai | Virat Kohli | 18</code> — retain at once.

    The admin override: no offer, no button, exactly what /aretain did before
    franchises were asked. For the owner who has agreed in person.
    """
    user = update.effective_user
    raw = _arg_text(context)

    def work(session, season):
        franchise, player, price = _retention_args(
            session, season, raw,
            "Usage: /aretainforce <franchise> | <player> | [price]")
        lot = A.retain(session, season, franchise, player, price,
                       by_tg_id=user.id if user else None)
        kept = int(franchise.retained_count or 0)
        return (f"🔒 <b>{html.escape(franchise.name)}</b> retain "
                f"{html.escape(lot.name)} for "
                f"<b>{A.render_money(lot.sold_price_lakh, season.currency_label)}</b> "
                f"({kept}/{season.max_retentions}).\n"
                f"💰 {A.render_money(franchise.purse_remaining_lakh, season.currency_label)} "
                f"left to bid with."
                + _holder_warning(session, season, franchise, player))

    await _with_auction(update, work, admin=True, context=context)


async def retention_offer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Accept / Decline on a retention offer — that franchise's people only."""
    query = update.callback_query
    if query is None:
        return
    user = update.effective_user
    try:
        offer_id, answer = (query.data or "")[len(RET_CB):].split("_", 1)
        offer_id = int(offer_id)
    except (ValueError, AttributeError):
        await query.answer("That button is from an older auction.",
                           show_alert=True)
        return

    session = get_session()
    try:
        offer = A.retention_offer(session, offer_id)
        season = None
        if offer is not None:
            from models import AuctionSeason
            season = (session.query(AuctionSeason)
                      .filter(AuctionSeason.id == offer.season_id).first())
        if offer is None or season is None:
            await query.answer("That offer no longer exists.", show_alert=True)
            return
        accept = answer == "yes"
        lot = A.answer_retention_offer(session, season, offer,
                                       user.id if user else None, accept)
        session.commit()
        if lot is not None:
            await query.answer(f"Retained for "
                               f"{A.render_money(lot.sold_price_lakh, season.currency_label)}.")
        else:
            await query.answer("Declined.")
        try:
            await query.edit_message_text(
                AR.retention_offer_card(session, season, offer),
                parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            logger.debug("auction: could not update the offer card",
                         exc_info=True)
    except AuctionError as exc:
        session.rollback()
        await query.answer(str(exc)[:190], show_alert=True)
    except Exception:
        session.rollback()
        logger.exception("auction retention offer button failed")
        await query.answer("That did not land — try again.", show_alert=True)
    finally:
        session.close()


async def aoffers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aoffers</code> — retention offers still waiting on a franchise."""
    def work(session, season):
        offers = A.pending_retention_offers(session, season.id)
        if not offers:
            return "🔒 No retention offers are waiting."
        from models import AuctionFranchise
        lines = [f"🔒 <b>Retention offers waiting — {len(offers)}</b>"]
        for offer in offers:
            franchise = (session.query(AuctionFranchise)
                         .filter(AuctionFranchise.id == offer.franchise_id).first())
            price = A.offer_price(season, franchise, offer) if franchise else 0
            lines.append(f"· <b>{html.escape(offer.player_name)}</b> → "
                         f"{html.escape(franchise.name if franchise else '?')} · "
                         f"{A.render_money(price, season.currency_label)}")
        lines.append("\n<i>Withdraw one with</i> <code>/aretcancel &lt;player&gt;</code>")
        return "\n".join(lines)

    await _with_auction(update, work, admin=True, context=context)


async def aretcancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aretcancel Virat Kohli</code> — withdraw a waiting offer."""
    raw = _arg_text(context).strip().lower()

    def work(session, season):
        if not raw:
            raise AuctionError("Usage: /aretcancel <player>")
        offers = [o for o in A.pending_retention_offers(session, season.id)
                  if raw in (o.player_name or "").lower()]
        if not offers:
            raise AuctionError(f"No waiting offer matches “{raw}”.")
        if len(offers) > 1:
            raise AuctionError("That could be " + ", ".join(
                o.player_name for o in offers[:5]) + " — type more of the name.")
        A.cancel_retention_offer(session, season, offers[0])
        return (f"🚫 The retention offer for "
                f"{html.escape(offers[0].player_name)} is withdrawn.")

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
    """The state of retention for anyone; ``on`` closes it, for an admin.

    The readout matters more than the switch, and to more people. The deadline
    is enforced lazily — nothing sweeps while an auction is in setup — so a
    window nobody can see only ever announces itself by refusing somebody; and
    what the rest of the room kept is what every franchise is about to bid
    against. So the readout is open, and only the switch takes an admin.
    """
    user = update.effective_user
    arg = _arg_text(context).strip().lower()

    if arg not in ("on", "close", "lock"):
        await _view(update, context,
                    lambda s, season: AR.retention_view(s, season))
        return

    def work(session, season):
        A.lock_retention(session, season, by_tg_id=user.id if user else None)
        return "🔒 Retention is closed."

    await _with_auction(update, work, admin=True, context=context)


async def apick_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/apick Gujarat | Hardik Pandya | 15</code> — the price is optional.

    An expansion side taking a player before the auction opens. Left off, the
    price is the retention ladder's rung for that side's next pick, which is
    the number an admin almost always wants and the one the reply prints back.
    """
    user = update.effective_user
    raw = _arg_text(context)

    def work(session, season):
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            turn = A.pick_turn(session, season)
            whose = (f"\nIt is <b>{html.escape(turn.name)}</b>'s pick."
                     if turn is not None else "")
            raise AuctionError(
                "Usage: /apick <franchise> | <player> | [price]\n"
                "The price is optional — leave it off and the retention "
                "ladder decides." + whose)
        franchise = _find_franchise(session, season, parts[0])
        player = _find_player(session, parts[1])
        price = (A.parse_amount(parts[2])
                 if len(parts) > 2 and parts[2] else None)
        lot = A.draft_pick(session, season, franchise, player, price,
                           by_tg_id=user.id if user else None)
        used = int(franchise.draft_picks_used or 0)
        following = A.pick_turn(session, season)
        nxt = (f"\n➡️ Next: <b>{html.escape(following.name)}</b>."
               if following is not None
               else "\n✅ That is every pick used — the auction can open.")
        return (f"🆕 <b>{html.escape(franchise.name)}</b> draft "
                f"{html.escape(lot.name)} for "
                f"<b>{A.render_money(lot.sold_price_lakh, season.currency_label)}</b> "
                f"({used}/{int(franchise.draft_picks_total or 0)}).\n"
                f"💰 {A.render_money(franchise.purse_remaining_lakh, season.currency_label)} "
                f"left to bid with." + nxt)

    await _with_auction(update, work, admin=True, context=context)


async def apicks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/apicks</code> — the running order, whose turn, and what is taken.

    Open to the room. It is the *new* side that needs the order — their turn
    is the one coming up — and an auction where only the admin can see whose
    pick it is makes everybody else ask.
    """
    await _view(update, context, lambda s, season: AR.picks_view(s, season))


async def apickset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/apickset 3</code> — picks for every new side.

    <code>/apickset Gujarat | 2</code> gives one side its own number, never
    below what it has already used.
    """
    raw = _arg_text(context).strip()

    def work(session, season):
        if not raw:
            raise AuctionError(
                "Usage: /apickset <n>, or /apickset <franchise> | <n>")
        if "|" in raw:
            name, _, count = raw.partition("|")
            franchise = _find_franchise(session, season, name.strip())
            A.set_franchise_picks(session, season, franchise, count.strip())
            return (f"🆕 {html.escape(franchise.name)} now holds "
                    f"<b>{A.picks_left(franchise)}</b> picks of "
                    f"{int(franchise.draft_picks_total or 0)}.")
        dealt = A.set_expansion_picks(session, season, raw)
        if not dealt:
            return ("🆕 Saved, but nobody is new this season so nobody got "
                    "picks.\n<i>A side is 'new' when last season's league "
                    "records nobody as theirs — check the auction follows "
                    "the right league.</i>")
        return (f"🆕 <b>{season.expansion_picks}</b> picks each to: "
                + ", ".join(html.escape(f.name) for f in dealt)
                + ".\nSee the order with <code>/apicks</code>.")

    await _with_auction(update, work, admin=True, context=context)


async def apickskip_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/apickskip</code> — burn the current turn without signing anyone."""
    user = update.effective_user
    raw = _arg_text(context).strip()

    def work(session, season):
        franchise = (_find_franchise(session, season, raw) if raw
                     else A.pick_turn(session, season))
        if franchise is None:
            raise AuctionError("There is no pick waiting to be made.")
        A.skip_pick(session, season, franchise,
                    by_tg_id=user.id if user else None)
        following = A.pick_turn(session, season)
        return (f"⏭ {html.escape(franchise.name)} pass."
                + (f"\n➡️ Next: <b>{html.escape(following.name)}</b>."
                   if following is not None
                   else "\n✅ That is every pick used."))

    await _with_auction(update, work, admin=True, context=context)


async def apickundo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/apickundo Hardik Pandya</code> — money and the pick both back."""
    user = update.effective_user
    raw = _arg_text(context).strip()

    def work(session, season):
        if not raw:
            raise AuctionError("Usage: /apickundo <player>")
        lot = _find_lot(session, season, raw)
        A.undo_pick(session, season, lot,
                    by_tg_id=user.id if user else None)
        return (f"↩️ The expansion pick on {html.escape(lot.name)} is undone — "
                f"the money and the pick are both back, and he is in the pool "
                f"again.")

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


# ════════════════════════════════════════════════════════════════════
# Team views — sets, the next set and player, squads, sold and unsold
#
# Open to everyone in the auction's group (and in a DM, where there is no
# auction to find, they say so). None is in the group slash menu — both player
# scopes sit at Telegram's 100-command ceiling — so /ainfo puts every one of
# them behind a button, and the board's footer names them.
# ════════════════════════════════════════════════════════════════════

async def _view(update, context, build):
    """Run a read-only view against this chat's auction and send it."""
    session = get_session()
    try:
        season = _read_season(session, update)
        if season is None:
            await _reply(update, _no_auction(update))
            return
        result = build(session, season)
        if isinstance(result, str):
            await _reply(update, result)
            return
        blocks, html_text, *markup = result
        await _reply_rich(update, context, blocks, html_text,
                          reply_markup=markup[0] if markup else None)
    except AuctionError as exc:
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
    except Exception:
        logger.exception("auction view failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
    finally:
        session.close()


def _own_or_named_franchise(session, season, update, name):
    if name:
        return _find_franchise(session, season, name)
    user = update.effective_user
    franchise = (A.franchise_for_actor(session, season.id, user.id)
                 if user else None)
    if franchise is None:
        raise AuctionError("You do not own a franchise here — name one, e.g. "
                           "/asquad Mumbai")
    return franchise


async def ainfo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/ainfo</code> — where the auction stands, and a button per view."""
    user = update.effective_user

    def build(session, season):
        franchise = (A.franchise_for_actor(session, season.id, user.id)
                     if user else None)
        blocks, html_text = AR.info_menu(session, season, franchise)
        return blocks, html_text, AR.info_keyboard(season)

    await _view(update, context, build)


async def arules_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/arules</code> — every number the auction will run by.

    Open to the room, and written for the hour before it starts: the purse, the
    squad and overseas caps, the reserve the max-bid rule holds back, the base
    prices, the bid ladder, the clock, retention, RTM and the picks. All of it
    lived on the admin's setup page, which is no use at all to the people who
    have to bid against it.
    """
    await _view(update, context, lambda s, season: AR.rules_view(s, season))


async def asets_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/asets [page]</code> — every set, in running order, with its players.

    Each set on the page opens with a tap for its first few, and a button per set
    shows the whole thing. <code>/asets 2</code> starts on the second page.
    """
    page = A._as_int(_arg_text(context), 1) or 1
    await _view(update, context,
                lambda s, season: AR.sets_view(s, season, page=page))


async def anextset_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/anextset</code> — the next set's players.

    For an auction admin, <code>/anextset Marquee</code> or
    <code>/anextset 85-90</code> makes that set — or every queued player in
    that rating range — come next instead.
    """
    raw = _arg_text(context)
    if not raw:
        await _view(update, context, lambda s, season: AR.next_set_view(s, season))
        return
    user = update.effective_user

    def work(session, season):
        label, moved = A.bring_forward(session, season, raw,
                                       by_tg_id=user.id if user else None)
        return (f"⏭ <b>{html.escape(label)}</b> comes next — {len(moved)} "
                f"{'player' if len(moved) == 1 else 'players'}, starting with "
                f"<b>{html.escape(moved[0].name)}</b>."
                + ("\n<i>The lot on the block finishes first.</i>"
                   if A.current_lot(session, season) is not None else ""))

    await _with_auction(update, work, admin=True, context=context)


async def anextplayer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/anextplayer [n]</code> — who comes to the block next."""
    raw = _arg_text(context)
    count = max(1, min(15, A._as_int(raw, 5))) if raw else 5
    await _view(update, context,
                lambda s, season: AR.next_players_view(s, season, count))


async def asquad_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/asquad [franchise]</code> — your squad, or any franchise's."""
    name = _arg_text(context)
    await _view(update, context, lambda s, season: AR.squad_view(
        s, season, _own_or_named_franchise(s, season, update, name)))


async def asoldlist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/asoldlist</code> — every player sold, set by set."""
    await _view(update, context, lambda s, season: AR.sold_view(s, season))


async def aunsoldlist_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aunsoldlist</code> — the ⚡ Unsold / Accelerated set."""
    await _view(update, context, lambda s, season: AR.unsold_view(s, season))


async def info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A /ainfo button. Shared by the room; each press answers the presser."""
    query = update.callback_query
    if query is None:
        return
    key = (query.data or "")[len(INFO_CB):]
    views = {
        "sets": lambda s, season: AR.sets_view(s, season),
        "nextset": lambda s, season: AR.next_set_view(s, season),
        "next": lambda s, season: AR.next_players_view(s, season),
        "squad": lambda s, season: AR.squad_view(
            s, season, _own_or_named_franchise(s, season, update, "")),
        "sold": lambda s, season: AR.sold_view(s, season),
        "unsold": lambda s, season: AR.unsold_view(s, season),
        "purse": lambda s, season: AR.purses_view(s, season),
        "rules": lambda s, season: AR.rules_view(s, season),
        "retention": lambda s, season: AR.retention_view(s, season),
        "picks": lambda s, season: AR.picks_view(s, season),
    }
    build = views.get(key)
    if build is None:
        await query.answer("That button is from an older auction.",
                           show_alert=True)
        return
    session = get_session()
    try:
        season = _read_season(session, update)
        if season is None:
            await query.answer("No auction is running here.", show_alert=True)
            return
        # A view may hand back a keyboard of its own — the Sets card does, so
        # its pages and its per-set buttons work when reached from /ainfo too.
        blocks, html_text, *markup = build(session, season)
        await query.answer()
        await AR.send(context.bot, update.effective_chat.id, blocks, html_text,
                      reply_markup=markup[0] if markup else None)
    except AuctionError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
    except Exception:
        logger.exception("auction info button failed")
        await query.answer("That did not work — try the command instead.",
                           show_alert=True)
    finally:
        session.close()


async def sets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A 🗂 Sets button: turn a page, or open a set.

    Edits the card in place rather than posting another one — a pool of thirty
    sets is a lot of paging, and a room that gets a fresh message per press
    cannot follow the auction it is there for. Read-only, so it answers whoever
    presses, like the /ainfo buttons it sits beside.
    """
    query = update.callback_query
    if query is None:
        return
    raw = (query.data or "")[len(AR.SETS_CB):]
    if raw == "noop":
        await query.answer()
        return

    kind, _, rest = raw.partition("_")
    parts = rest.split("_")
    if kind == "p":
        page, expand, lot_page = A._as_int(parts[0], 1) or 1, None, 1
    elif kind == "x":
        expand = A._as_int(parts[0], 0)
        lot_page = A._as_int(parts[1], 1) if len(parts) > 1 else 1
        page = 1
    else:
        await query.answer("That button is from an older auction.",
                           show_alert=True)
        return

    session = get_session()
    try:
        season = _read_season(session, update)
        if season is None:
            await query.answer("No auction is running here.", show_alert=True)
            return
        result = AR.sets_view(session, season, page=page, expand=expand,
                              lot_page=lot_page)
        if result is None:
            # The set finished and the numbers moved under the open card. Saying
            # so beats opening whichever set now holds that number.
            await query.answer("The sets have moved — press 🗂 Sets again.",
                               show_alert=True)
            return
        blocks, html_text, keyboard = result
        await query.answer()
        await AR.edit(context.bot, query.message.chat_id, query.message.message_id,
                      blocks, reply_markup=keyboard, html_text=html_text)
    except AuctionError as exc:
        await query.answer(str(exc)[:190], show_alert=True)
    except Exception:
        logger.exception("auction sets button failed")
        await query.answer("That did not work — try /asets instead.",
                           show_alert=True)
    finally:
        session.close()


# ════════════════════════════════════════════════════════════════════
# Admin — the pool by rating range, and the order sets run in
# ════════════════════════════════════════════════════════════════════

async def apool_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/apool 85-90 | Marquee</code> — a rating range into the pool as a set.

    The set name is optional (it defaults to the range, "85-90 OVR"). Base
    cards only unless a third part says <code>all</code>, because two editions
    of one cricketer in a pool is a squad with the same player twice. Sets run
    in the order they are added; /anextset and /asetorder change it.
    """
    raw = _arg_text(context)

    def work(session, season):
        parts = [p.strip() for p in raw.split("|")]
        band = A.parse_rating_range(parts[0]) if parts and parts[0] else None
        if band is None:
            raise AuctionError("Usage: /apool <min>-<max> | [set name] | [all]"
                               " — e.g. /apool 85-90 | Marquee")
        name = parts[1] if len(parts) > 1 and parts[1] else None
        editions = len(parts) > 2 and parts[2].lower() in ("all", "editions")
        added, skipped, label = A.add_rating_range_to_pool(
            session, season, *band, set_name=name, editions=editions)
        # The set's own number, not a count of queued sets: those two agree only
        # until one set finishes, and then every other surface says something
        # different from this message.
        entry = next((e for e in A.list_sets(session, season)
                      if e["name"] == label), None)
        position = entry["set_no"] if entry else "?"
        return (f"🗂 <b>{html.escape(label)}</b> — {added} "
                f"{'player' if added == 1 else 'players'} added"
                + (f", {skipped} already in the auction" if skipped else "")
                + f".\nIt is set <b>#{position}</b> in the queue — "
                  f"<code>/anextset {html.escape(label)}</code> brings it "
                  f"forward.")

    await _with_auction(update, work, admin=True, context=context)


async def asetorder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/asetorder Marquee, 85-90 OVR, Bowlers</code> — order the queue."""
    raw = _arg_text(context)
    user = update.effective_user

    def work(session, season):
        if not raw:
            entries = A.queued_sets(session, season)
            # Numbered, so the order named here and the Set No on the website
            # and in /asets are plainly the same thing.
            names = ", ".join(f"#{e['set_no']} {e['name']}"
                              for e in entries) or "none"
            raise AuctionError(f"Usage: /asetorder <set>, <set>, … — queued "
                               f"sets now: {names}")
        labels = A.set_order(session, season, raw.split(","),
                             by_tg_id=user.id if user else None)
        return ("🗂 The queue now runs: "
                + " → ".join(f"<b>{html.escape(l)}</b>" for l in labels)
                + ", then everything else as it was.")

    await _with_auction(update, work, admin=True, context=context)


async def aaccelmode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aaccelmode on|off</code> — the automatic accelerated round."""
    arg = _arg_text(context).strip().lower()

    def work(session, season):
        if arg in ("on", "yes", "1"):
            season.auto_accelerated = 1
        elif arg in ("off", "no", "0"):
            season.auto_accelerated = 0
        elif arg:
            raise AuctionError("Usage: /aaccelmode on|off")
        on = bool(A._as_int(season.auto_accelerated, 1))
        done = bool(A._as_int(season.accelerated_done, 0))
        return ("⚡ Automatic accelerated round: <b>"
                + ("on" if on else "off") + "</b>"
                + (" — already run this auction." if on and done else
                   " — unsold players come back once, as the ⚡ Accelerated "
                   "set, when the main pool is done." if on else
                   " — unsold players stay unsold unless you run /aaccel go, "
                   "and short squads are only auto-filled after a round of "
                   "it."))

    await _with_auction(update, work, admin=True, context=context)


# ════════════════════════════════════════════════════════════════════
# Admin — the teams
# ════════════════════════════════════════════════════════════════════

async def acall_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/acall [message]</code> — tag every owner and co-owner."""
    chat = update.effective_chat
    if chat is None or chat.type not in GROUP_CHAT_TYPES:
        await _reply(update, GROUP_ONLY)
        return
    if not await _require_admin(update):
        return
    message = _arg_text(context)
    session = get_session()
    try:
        season = _season_for(session, update)
        if season is None:
            await _reply(update, NO_AUCTION)
            return
        parts = AR.call_html(session, season, message or None)
    finally:
        session.close()
    for part in parts:
        await context.bot.send_message(chat_id=chat.id, text=part,
                                       parse_mode="HTML",
                                       disable_web_page_preview=True)


async def aremoveteam_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aremoveteam Delhi</code> — what removing a team would do.

    <code>/aremoveteam Delhi | confirm</code> does it: the team's players go
    back into the pool as a set of their own, and its opening purse is shared
    equally among the teams that remain.
    """
    raw = _arg_text(context)
    user = update.effective_user

    def work(session, season):
        parts = [p.strip() for p in raw.split("|")]
        if not parts or not parts[0]:
            raise AuctionError("Usage: /aremoveteam <franchise> — then "
                               "/aremoveteam <franchise> | confirm")
        franchise = _find_franchise(session, season, parts[0])
        symbol = season.currency_label
        confirm = len(parts) > 1 and parts[1].lower() in ("confirm", "yes", "go")
        if not confirm:
            preview = A.removal_preview(session, season, franchise)
            names = ", ".join(html.escape(lot.name) for lot in preview["players"][:12])
            more = (f" …and {len(preview['players']) - 12} more"
                    if len(preview["players"]) > 12 else "")
            return (f"🚪 <b>Remove {html.escape(franchise.name)}?</b>\n"
                    f"<blockquote>👥 {len(preview['players'])} "
                    f"{'player goes' if len(preview['players']) == 1 else 'players go'} "
                    f"back into the auction"
                    + (f": {names}{more}" if names else "") + "\n"
                    f"💰 Their {A.render_money(preview['pot'], symbol)} purse is "
                    f"shared out — about "
                    f"{A.render_money(preview['share'], symbol)} to each of "
                    f"{len(preview['others'])} teams</blockquote>\n"
                    f"Confirm with <code>/aremoveteam "
                    f"{html.escape(franchise.name)} | confirm</code>")
        name = franchise.name
        released, shares = A.remove_franchise(
            session, season, franchise, by_tg_id=user.id if user else None)
        return (f"🚪 <b>{html.escape(name)}</b> removed. {len(released)} "
                f"{'player is' if len(released) == 1 else 'players are'} back "
                f"in the pool and {len(shares)} purses went up.")

    await _with_auction(update, work, admin=True, context=context)


# ════════════════════════════════════════════════════════════════════
# Auction admins — appointed by bot admins
# ════════════════════════════════════════════════════════════════════

def _admin_target(session, update, context):
    """``(tg_id, name)`` from a reply, a numeric id, or an @username."""
    message = update.effective_message
    replied = getattr(message, "reply_to_message", None)
    person = getattr(replied, "from_user", None) if replied is not None else None
    if person is not None and not context.args:
        name = (f"@{person.username}" if getattr(person, "username", None)
                else getattr(person, "first_name", None))
        return int(person.id), name
    raw = _arg_text(context).strip()
    if raw.lstrip("-").isdigit():
        return int(raw), None
    if raw:
        from services.telegram_user_service import resolve_command_target
        user, _reason = resolve_command_target(session, update, context,
                                               "aadminadd")
        if user is not None and getattr(user, "telegram_id", None):
            name = (f"@{user.username}" if user.username
                    else user.first_name)
            return int(user.telegram_id), name
        raise AuctionError(f"I do not know {raw} — use their numeric Telegram "
                           f"id, or reply to one of their messages.")
    raise AuctionError("Reply to their message, or give their Telegram id or "
                       "@username.")


async def aadminadd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aadminadd 123456789</code> (or reply) — let someone run auctions."""
    if not await _require_bot_admin(update):
        return
    user = update.effective_user
    session = get_session()
    try:
        tg_id, name = _admin_target(session, update, context)
        row = A.add_auction_admin(session, tg_id, name=name,
                                  by_tg_id=user.id if user else None)
        session.commit()
        label = html.escape(row.name or str(row.tg_id))
        await _reply(update,
                     f"👮 <a href=\"tg://user?id={row.tg_id}\">{label}</a> is now "
                     f"an <b>auction admin</b> — every auction command, and no "
                     f"other admin command. <code>/adminhelp</code> lists them.")
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
    except Exception:
        session.rollback()
        logger.exception("/aadminadd failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
    finally:
        session.close()


async def aadminremove_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aadminremove 123456789</code> (or reply) — take it away again."""
    if not await _require_bot_admin(update):
        return
    session = get_session()
    try:
        tg_id, _name = _admin_target(session, update, context)
        row = A.remove_auction_admin(session, tg_id)
        session.commit()
        await _reply(update, f"👮 {html.escape(row.name or str(row.tg_id))} is "
                             f"no longer an auction admin.")
    except AuctionError as exc:
        session.rollback()
        await _reply(update, f"⚠️ {html.escape(str(exc))}")
    except Exception:
        session.rollback()
        logger.exception("/aadminremove failed")
        await _reply(update, "⚠️ Something went wrong. Try again.")
    finally:
        session.close()


async def aadmins_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """<code>/aadmins</code> — everyone who may run auctions."""
    if not await _require_admin(update):
        return
    session = get_session()
    try:
        rows = A.auction_admins(session)
    finally:
        session.close()
    if not rows:
        await _reply(update, "👮 <b>Auction admins</b>\n<i>None yet — bot admins "
                             "can always run auctions. Add one with "
                             "</i><code>/aadminadd</code>.")
        return
    lines = [f"👮 <b>Auction admins — {len(rows)}</b>",
             "<i>Plus every bot admin.</i>", ""]
    for row in rows:
        lines.append(f"· {html.escape(row.name or 'Admin')} — "
                     f"<code>{row.tg_id}</code>")
    await _reply(update, "\n".join(lines))
