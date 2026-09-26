"""The auction clock, and the one place the group is spoken to.

**Why a sweeper and not a job per lot.** The host redeploys often, and an
in-process ``run_once`` does not survive it — the same reason
``services/draft_scheduler`` and ``services/giveaway_scheduler`` exist. An
auction runs for hours; losing its timers to a deploy would strand every lot on
the block forever. The deadline therefore lives in ``AuctionLot.deadline_at``
and this job reconciles it, so a restart picks the clock back up exactly where
it was — and a process that was down for ten minutes resolves correctly on its
first tick back, because every bid in the table is still valid and the lot
simply sells to the standing top bid.

**Why two seconds and not fifteen.** The draft's pick clock runs for fifteen
minutes, so landing fifteen seconds late costs nothing. A lot runs for thirty
seconds. At the draft's interval an auction would sell up to half a lot-length
late, and every bid arriving in that window would have to be refused by a rule
the room cannot see. The cost is one indexed statement every two seconds, which
returns zero rows whenever no auction is live.

**The deadline is authoritative; this job only announces.** ``place_bid``
carries ``deadline_at > now`` inside its own conditional UPDATE, so a bid
arriving after the true deadline is refused by the command itself, before this
job has run. The sweeper never decides whether a bid was in time — only when
the room is told.

**One new message per lot, one small message per burst of bids.** Each lot
opens with the player's card and a fresh board, and that board is what gets
pinned — the room is notified once per player and the pin always answers
"who is on the block". Inside a lot the board is still edited in place, and
bids are announced as one short line per sweep however many landed in it, so
a bidding war costs at most one message every two seconds.

**Why the website never sends a message.** The Flask admin panel runs in a
thread of this process. Rather than reaching across that boundary, an admin
action writes its rows plus one ``AuctionEvent`` and commits; this job drains
everything past ``AuctionSeason.announced_event_id`` and says it out loud, in
id order, whether a command or a web form produced it. Nothing but the database
crosses over.
"""

import asyncio
import html
import logging
import math
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from telegram.error import (
    BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
)

logger = logging.getLogger(__name__)

SWEEP_JOB_NAME = "auction_clock"
SWEEP_INTERVAL = 2  # seconds

# Events that are reflected on the board rather than announced.
SILENT_KINDS = set()
# Bids ARE announced, but never one message each: every bid that lands inside
# one sweep is folded into a single short line (see ``drain_events``), so a
# bidding war costs at most one message per tick rather than forty.
COALESCED_KINDS = {"bid"}

# The shortest gap between two bid messages in one room. Telegram allows a
# group about twenty messages a minute; a bid line every two-second tick of a
# long bidding war would be thirty. Bids that arrive inside the gap simply wait
# for the next tick and go out together — unless something else is queued
# behind them, which is never held up for them.
BID_MESSAGE_GAP = 4.0  # seconds
_last_bid_message = {}

# The one message in the room, besides the pinned board, that carries live
# quick-bid buttons: the newest bid line, lot card or countdown header. Keyed
# by season → (chat_id, message_id). When a newer one is sent the old one's
# buttons are taken off, so the room is never offered a column of stale
# prices — the buttons are always on the message at the bottom of the chat.
_live_buttons = {}

# Send the player's card when a lot opens. A switch rather than a constant
# for the tests, which have no card renderer to spare.
SEND_LOT_CARD = True

# Latched off for the process when Telegram refuses a rich board edit, so a
# payload it does not like costs one refused call, not one every two seconds.
_board_rich_ok = True

# The hammer countdown ("Selling X to Team for ₹…", 3, 2, 1, SOLD) runs as its
# own task, because a two-second sweep cannot count in ones. Keyed by season:
# the lot and the deadline it is counting down to, so a bid that moves the
# deadline gets a fresh count rather than two overlapping ones.
_countdowns = {}
# The sweep and a countdown that brings the hammer down on time must never
# resolve the same lot at once.
_resolve_lock = None
_resolve_lock_loop = None

# How many pending events one tick will say out loud. A backlog (the bot was
# down while an admin worked through the console) is drained over several
# ticks rather than in one burst that trips flood control.
DRAIN_PER_TICK = 5


# ── Sending ──────────────────────────────────────────────────────────

async def _send(bot, chat_id, text, **kwargs):
    """Send one HTML message, swallowing the failures a group chat throws.

    An auction must not stop because the bot was kicked or the chat went
    read-only — the lots are in the database either way, and the next command
    reprints the board.
    """
    try:
        return await bot.send_message(chat_id=chat_id, text=text,
                                      parse_mode="HTML",
                                      disable_web_page_preview=True, **kwargs)
    except RetryAfter as exc:
        logger.warning("auction send rate-limited: %s", exc)
    except (Forbidden, BadRequest) as exc:
        logger.warning("auction send refused by chat %s: %s", chat_id, exc)
    except TelegramError:
        logger.exception("auction send failed")
    return None


def _retry_after_seconds(exc):
    value = getattr(exc, "retry_after", None)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 1.0


async def edit_board(bot, chat_id, message_id, text, *, attempts=3,
                     reply_markup=None):
    """Edit the pinned board, retrying the way a live message has to.

    Modelled on ``services.match_broadcast.reveal_toss_result``. Two details
    are load-bearing:

    * a ``BadRequest`` reading "not modified" is a **success** — two bids
      inside one tick can render byte-identical text, and so can a retry whose
      first attempt actually landed;
    * ``BadRequest`` subclasses ``NetworkError`` in PTB, so it must be caught
      *before* the ``NetworkError`` clause or the "not modified" branch is
      never reached.

    A failure here never rolls anything back: the bid is already committed.
    """
    for attempt in range(attempts):
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                        text=text, parse_mode="HTML",
                                        disable_web_page_preview=True,
                                        reply_markup=reply_markup)
            return True
        except RetryAfter as exc:
            await asyncio.sleep(_retry_after_seconds(exc) + 0.5)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            logger.warning("auction board edit refused: %s", exc)
            return False
        except (TimedOut, NetworkError):
            await asyncio.sleep(0.5 * (attempt + 1))
        except Exception:
            logger.exception("auction board edit failed")
            return False
    return False


async def pin_board(bot, session, season, message, previous_id=None):
    """Pin the board once, best-effort, unpinning only the board it replaces.

    An auction group scrolls fast and the board is the one message that answers
    "where are we" — but pinning needs a right the bot may not have been given,
    and an auction must never stop over a pin. The unpin names the previous
    board: without a ``message_id`` Telegram unpins the most recent pin in the
    chat, which may be somebody's unrelated announcement.
    """
    message_id = getattr(message, "message_id", None)
    if message_id is None or not season.chat_id:
        return None
    if previous_id and previous_id != message_id:
        try:
            await bot.unpin_chat_message(chat_id=season.chat_id,
                                         message_id=previous_id)
        except Exception:
            logger.debug("auction: nothing to unpin", exc_info=True)
    try:
        await bot.pin_chat_message(chat_id=season.chat_id,
                                   message_id=message_id,
                                   disable_notification=True)
    except Exception:
        logger.warning("auction: could not pin the board (missing right?)")
    return message_id


# ── The board ────────────────────────────────────────────────────────

async def refresh_board(bot, session, season, *, force=False, now=None):
    """Bring the pinned board up to date, creating it if it does not exist.

    ``force`` skips the debounce. Without it the board is only redrawn when the
    bid count has moved since the last render — which is what turns ten bids
    inside one two-second tick into one edit instead of ten.

    **A new lot gets a new board.** When the lot on the block is not the one
    the board was posted for, a fresh board is sent and pinned instead of the
    old one being edited — the room is notified of every player, and the pin
    always points at the player being sold.
    """
    global _board_rich_ok
    from services import auction_service as A
    from services import auction_rich as AR

    if not season.chat_id:
        return None
    lot = A.current_lot(session, season)
    body = AR.board_html(session, season, lot, now=now)
    blocks = (AR.board_blocks(session, season, lot, now=now)
              if _board_rich_ok else None)
    markup = AR.bid_keyboard(season, lot)
    new_lot = (lot is not None
               and int(getattr(season, "board_lot_id", 0) or 0) != lot.id)
    previous_board = season.board_message_id

    if season.board_message_id and not new_lot:
        rich = await AR.edit(bot, season.chat_id, season.board_message_id,
                             blocks, reply_markup=markup) if blocks else None
        if rich is False:
            _board_rich_ok = False
        ok = rich or await edit_board(bot, season.chat_id,
                                      season.board_message_id, body,
                                      reply_markup=markup)
        if ok:
            season.board_rendered_bid_count = int(lot.bid_count or 0) if lot else 0
            return season.board_message_id
        # The message was deleted, or is too old to edit. Post a fresh one
        # rather than leaving the room with no board at all.
        season.board_message_id = None

    sent = await _send_rich(bot, season.chat_id, blocks, body,
                            reply_markup=markup)
    if sent is None:
        return None
    season.board_message_id = sent.message_id
    season.board_lot_id = lot.id if lot is not None else None
    season.board_rendered_bid_count = int(lot.bid_count or 0) if lot else 0
    await pin_board(bot, session, season, sent, previous_id=previous_board)
    return season.board_message_id


async def _send_rich(bot, chat_id, blocks, html_text, **kwargs):
    """``auction_rich.send``, swallowing what a group chat throws like ``_send``."""
    from services import auction_rich as AR
    try:
        return await AR.send(bot, chat_id, blocks, html_text, **kwargs)
    except RetryAfter as exc:
        logger.warning("auction send rate-limited: %s", exc)
    except (Forbidden, BadRequest) as exc:
        logger.warning("auction send refused by chat %s: %s", chat_id, exc)
    except TelegramError:
        logger.exception("auction send failed")
    return None


async def strip_buttons(bot, season_id):
    """Take the quick-bid buttons off the last message that carried them.

    Best-effort: the message may be gone, too old to edit, or already bare —
    none of which matters, because a stale button is refused by the price in
    its own callback data anyway. This only keeps the room tidy.
    """
    held = _live_buttons.pop(season_id, None)
    if held is None:
        return
    chat_id, message_id = held
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id,
                                            message_id=message_id,
                                            reply_markup=None)
    except Exception:
        logger.debug("auction: could not strip old buttons", exc_info=True)


async def hand_buttons(bot, season_id, chat_id, message):
    """``message`` now carries the live buttons; the previous holder loses them."""
    message_id = getattr(message, "message_id", None)
    if not message_id:
        return
    held = _live_buttons.get(season_id)
    if held is not None and held[1] != message_id:
        await strip_buttons(bot, season_id)
    _live_buttons[season_id] = (chat_id, message_id)


async def _send_bid_line(bot, chat_id, text, reply_markup=None):
    """Send a bid line. The message (or True) when it landed or never can;
    None to retry.

    A transient failure — flood control, a timeout, a network error — must not
    advance the drain cursor, or the burst is never announced. A permanent
    refusal (kicked, read-only, a bad request) is reported as done, so one
    unreachable chat cannot stall the drain forever.
    """
    try:
        kwargs = {"reply_markup": reply_markup} if reply_markup is not None else {}
        sent = await bot.send_message(chat_id=chat_id, text=text,
                                      parse_mode="HTML",
                                      disable_web_page_preview=True, **kwargs)
        return sent if sent is not None else True
    except RetryAfter as exc:
        logger.warning("auction bid line rate-limited: %s", exc)
        return None
    except (Forbidden, BadRequest) as exc:
        # Before NetworkError: PTB's BadRequest subclasses it.
        logger.warning("auction bid line refused by chat %s: %s", chat_id, exc)
        return True
    except (TimedOut, NetworkError) as exc:
        logger.warning("auction bid line failed, will retry: %s", exc)
        return None
    except TelegramError:
        logger.exception("auction bid line failed")
        return True


async def send_lot_card(bot, session, season, lot):
    """The player's card, captioned with the lot, when he comes to the block.

    Falls back to the caption as text: the announcement matters more than the
    picture, and a card that will not render must not cost the room the news
    that a new player is up.
    """
    from services import auction_rich as AR
    caption = AR.lot_caption(session, season, lot)
    # The opening bid is one tap away on the card itself, the way TeleAuction
    # puts its buttons on the player message.
    markup = AR.bid_keyboard(season, lot)
    await strip_buttons(bot, season.id)
    sent = await _lot_card_message(bot, session, season, lot, caption, markup)
    if sent is not None and markup is not None:
        await hand_buttons(bot, season.id, season.chat_id, sent)
    return sent


async def _lot_card_message(bot, session, season, lot, caption, markup):
    extra = {"reply_markup": markup} if markup is not None else {}
    if SEND_LOT_CARD and lot.player_id:
        try:
            from models import Player
            from services.card_sender import send_player_card
            player = (session.query(Player)
                      .filter(Player.id == lot.player_id).first())
            if player is not None:
                sent = await send_player_card(bot=bot, chat_id=season.chat_id,
                                              player=player, caption=caption,
                                              parse_mode="HTML",
                                              session=session, **extra)
                if sent is not None:
                    return sent
        except Exception:
            logger.exception("auction lot card failed; sending text")
    return await _send(bot, season.chat_id, caption, **extra)


def last_lot_id(run):
    """The lot a run of bid events was on (the last one's, if they differ)."""
    return run[-1].lot_id if run else None


# ── The event drain ──────────────────────────────────────────────────

async def drain_events(bot, session, season, *, limit=DRAIN_PER_TICK):
    """Say everything that has happened since the last tick, oldest first.

    ``limit`` caps the **messages** sent, not the events read: a run of bids
    is one message however many it holds. The cursor advances past silent
    events too — they are on the board, not in the stream, but they have still
    been dealt with.
    """
    from services import auction_service as A
    from services import auction_rich as AR

    if not season.chat_id:
        return 0
    pending = A.pending_events(session, season, limit=limit * 10)
    if not pending:
        return 0
    spoken = 0
    index = 0
    while index < len(pending) and spoken < limit:
        event = pending[index]
        if event.kind in COALESCED_KINDS:
            run = [event]
            while (index + 1 < len(pending)
                   and pending[index + 1].kind == event.kind):
                index += 1
                run.append(pending[index])
            waiting_behind = index + 1 < len(pending)
            since = time.monotonic() - _last_bid_message.get(season.id, 0.0)
            if since < BID_MESSAGE_GAP and not waiting_behind:
                # Hold the burst for a later tick; the board already shows
                # the price, so nothing the room needs is late.
                break
            # The newest bid carries the quick-bid buttons, TeleAuction-style,
            # so the next raise is one tap at the bottom of the chat rather
            # than a scroll up to the pinned board.
            current = A.current_lot(session, season)
            markup = (AR.bid_keyboard(season, current)
                      if current is not None and current.id == last_lot_id(run)
                      else None)
            delivered = await _send_bid_line(
                bot, season.chat_id, AR.bid_burst_html(session, season, run),
                reply_markup=markup)
            if delivered is None:
                # Rate-limited or a network blip: keep the cursor where it
                # is and send the whole burst again on a later tick.
                break
            if markup is not None and delivered is not True:
                await hand_buttons(bot, season.id, season.chat_id, delivered)
            _last_bid_message[season.id] = time.monotonic()
            spoken += 1
            last = run[-1]
        else:
            last = event
            if event.kind not in SILENT_KINDS:
                # Anything else that happens — a sale, a pause, a Right To
                # Match — changes what the buttons would bid on, so the last
                # set comes off. The next bid line (or lot card) brings them
                # back at the right price.
                await strip_buttons(bot, season.id)
                if event.kind == "lot_opened":
                    lot = A.current_lot(session, season)
                    if lot is None or lot.id != event.lot_id:
                        from models import AuctionLot
                        lot = (session.query(AuctionLot)
                               .filter(AuctionLot.id == event.lot_id).first())
                    if lot is not None:
                        await send_lot_card(bot, session, season, lot)
                    else:
                        await _send(bot, season.chat_id, event.headline)
                else:
                    await _send(bot, season.chat_id,
                                AR.event_html(session, season, event))
                spoken += 1
        season.announced_event_id = last.id
        # Commit the cursor per message: a crash halfway through a drain must
        # not re-announce what the room has already been told.
        session.commit()
        index += 1
    return spoken


# ── The staged clock's warnings ──────────────────────────────────────

async def send_warning(bot, session, season, lot, stage, left):
    """The 1st or 2nd warning, carrying the quick-bid buttons."""
    from services import auction_rich as AR
    text = AR.warning_html(session, season, lot, stage, left)
    markup = AR.bid_keyboard(season, lot)
    extra = {"reply_markup": markup} if markup is not None else {}
    sent = await _send(bot, season.chat_id, text, **extra)
    if sent is not None and markup is not None:
        await hand_buttons(bot, season.id, season.chat_id, sent)
    return sent


# ── The hammer countdown ─────────────────────────────────────────────

def _lock():
    """The resolve lock for the running loop (tests run one loop per case)."""
    global _resolve_lock, _resolve_lock_loop
    loop = asyncio.get_running_loop()
    if _resolve_lock is None or _resolve_lock_loop is not loop:
        _resolve_lock = asyncio.Lock()
        _resolve_lock_loop = loop
    return _resolve_lock


def countdown_header(session, season, lot):
    """What is about to happen to the lot, said before the count starts."""
    from services import auction_service as A
    name = html.escape(lot.name or "?")
    if lot.current_bidder_id is None:
        return f"⏳ <b>{name}</b> is going <b>UNSOLD</b> — no bids yet"
    from models import AuctionFranchise
    team = (session.query(AuctionFranchise)
            .filter(AuctionFranchise.id == lot.current_bidder_id).first())
    team_name = html.escape(team.name if team else "?")
    price = A.render_money(lot.current_bid_lakh, season.currency_label)
    holder, _ = A.rtm_available(session, season, lot)
    if holder is not None:
        # The hammer is not the end of this one: the old franchise is asked
        # first, so "selling to" would be a promise the room cannot keep.
        return (f"⏳ Bidding closes on <b>{name}</b>\n"
                f"<b>{team_name}</b> lead at <b>{price}</b> — then "
                f"<b>{html.escape(holder.name)}</b> may use a Right To Match")
    return (f"⏳ Selling <b>{name}</b>\n"
            f"to <b>{team_name}</b> for <b>{price}</b>")


def maybe_start_countdown(bot, session, season, *, now=None):
    """Arm the hammer countdown for the lot on the block, once per deadline.

    Armed a little early — up to one sweep before the count is due — so the
    task can land its first number on time; it sleeps the rest itself.
    """
    from services import auction_service as A
    seconds = A.countdown_seconds(season)
    if seconds <= 0 or season.status != A.STATUS_LIVE or not season.chat_id:
        return None
    lot = A.current_lot(session, season)
    if lot is None or lot.status != A.LOT_ON_BLOCK or lot.deadline_at is None:
        return None
    left = A.seconds_left(lot, now)
    if left is None or left <= 0 or left > seconds + SWEEP_INTERVAL + 0.5:
        return None
    key = (lot.id, lot.deadline_at)
    running = _countdowns.get(season.id)
    if running is not None and running[0] == key and not running[1].done():
        return None
    task = asyncio.get_running_loop().create_task(
        run_countdown(bot, season.id, lot.id, lot.deadline_at, seconds))
    _countdowns[season.id] = (key, task)
    return task


async def _edit_count(bot, chat_id, message_id, text, markup=None):
    """Edit the countdown message in place. False when Telegram refused."""
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                    text=text, parse_mode="HTML",
                                    reply_markup=markup,
                                    disable_web_page_preview=True)
        return True
    except BadRequest as exc:
        return "not modified" in str(exc).lower()
    except Exception:
        logger.debug("auction countdown edit failed", exc_info=True)
        return False


def _count_text(header, number):
    return f"{header}\n\n⏳ <b>{number}</b>"


async def run_countdown(bot, season_id, lot_id, deadline, seconds, *,
                        sleep=None, clock=None):
    """Count the room down to the hammer in ONE message, then bring it down.

    The first number is sent with the header that says what is about to
    happen (and the bid buttons); every second after that EDITS the same
    message — 5, 4, 3, 2, 1 — so the whole count costs the room one message
    rather than five, well inside Telegram's twenty-a-minute group limit. An
    edit Telegram refuses falls back to a new message.

    Before every number the lot is read again. A bid that did not move the
    deadline rewrites the header with the new leader. A bid that DID move it
    (anti-snipe, or the staged clock's reset) ends this count: the message is
    turned into "🔄 New bid — clock back to 40s" and loses its buttons, so a
    stale "3" is never left hanging, and the sweep arms a fresh count for the
    new deadline. At the deadline the lot is resolved here rather than up to a
    sweep later, so "1" is followed by SOLD, not by two seconds of silence.
    """
    from database import get_session
    from models import AuctionFranchise, AuctionLot, AuctionSeason
    from services import auction_service as A
    from services import auction_rich as AR

    sleep = sleep or asyncio.sleep
    clock = clock or datetime.utcnow

    async def until(moment):
        wait = (moment - clock()).total_seconds()
        if wait > 0:
            await sleep(wait)

    said_for = None
    header = ""
    message_id = None
    chat_id = None
    try:
        left = (deadline - clock()).total_seconds()
        first = max(1, min(int(seconds), int(math.ceil(left))))
        for number in range(first, 0, -1):
            await until(deadline - timedelta(seconds=number))
            session = get_session()
            try:
                season = (session.query(AuctionSeason)
                          .filter(AuctionSeason.id == season_id).first())
                lot = (session.query(AuctionLot)
                       .filter(AuctionLot.id == lot_id).first())
                if (season is None or lot is None
                        or season.status != A.STATUS_LIVE
                        or lot.status != A.LOT_ON_BLOCK
                        or lot.deadline_at != deadline):
                    if (message_id and season is not None and lot is not None
                            and lot.status == A.LOT_ON_BLOCK
                            and lot.deadline_at is not None
                            and lot.deadline_at > deadline):
                        team = (session.query(AuctionFranchise)
                                .filter(AuctionFranchise.id == lot.current_bidder_id)
                                .first())
                        back = int(math.ceil((lot.deadline_at - clock())
                                             .total_seconds()))
                        await _edit_count(
                            bot, chat_id, message_id,
                            f"🔄 <b>New bid</b> — "
                            f"{html.escape(team.name if team else '?')} "
                            f"<b>{A.render_money(lot.current_bid_lakh, season.currency_label)}</b>"
                            f" on {html.escape(lot.name or '?')} · clock back to "
                            f"<b>{back}s</b>")
                        if _live_buttons.get(season_id, (None, None))[1] == message_id:
                            _live_buttons.pop(season_id, None)
                    return False
                standing = (lot.current_bidder_id, lot.current_bid_lakh)
                markup = AR.bid_keyboard(season, lot)
                if standing != said_for:
                    header = countdown_header(session, season, lot)
                    said_for = standing
                text = _count_text(header, number)
                edited = False
                if message_id is not None:
                    edited = await _edit_count(bot, chat_id, message_id, text,
                                               markup)
                if not edited:
                    extra = {"reply_markup": markup} if markup is not None else {}
                    sent = await _send(bot, season.chat_id, text, **extra)
                    if sent is not None:
                        message_id = getattr(sent, "message_id", None)
                        chat_id = season.chat_id
                        if markup is not None:
                            await hand_buttons(bot, season.id, season.chat_id,
                                               sent)
            finally:
                session.close()
        await until(deadline + timedelta(milliseconds=50))
        if message_id is not None:
            # The hammer is coming down: the count keeps its last number but
            # loses its buttons, so nobody taps into a lot that just closed.
            await strip_buttons(bot, season_id)
        await _resolve_now(bot, season_id, lot_id, deadline, clock=clock)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("auction countdown failed; the sweep will resolve")
        return False


async def _resolve_now(bot, season_id, lot_id, deadline, *, clock=None):
    """One sweep of one auction, taken the moment its lot's clock runs out."""
    from database import get_session
    from models import AuctionLot, AuctionSeason
    from services import auction_service as A

    clock = clock or datetime.utcnow
    async with _lock():
        session = get_session()
        try:
            season = (session.query(AuctionSeason)
                      .filter(AuctionSeason.id == season_id).first())
            lot = (session.query(AuctionLot)
                   .filter(AuctionLot.id == lot_id).first())
            if (season is None or lot is None
                    or season.status != A.STATUS_LIVE or not season.chat_id
                    or lot.status != A.LOT_ON_BLOCK
                    or lot.deadline_at != deadline):
                return  # the sweep, a bid or an admin got there first
            await _tick_one(SimpleNamespace(bot=bot), session, season, clock())
        except Exception:
            session.rollback()
            logger.exception("auction #%s countdown resolve failed", season_id)
        finally:
            session.close()


def cancel_countdown(season_id):
    """Drop a running countdown — the auction under it has been reset."""
    _live_buttons.pop(season_id, None)
    running = _countdowns.pop(season_id, None)
    if running is not None and not running[1].done():
        running[1].cancel()


# ── The sweep ────────────────────────────────────────────────────────

async def _auction_tick(context):
    """One sweep: move the going-once clock on, and resolve what has expired."""
    from database import get_session
    from models import AuctionSeason
    from services import auction_service as A

    session = get_session()
    try:
        now = datetime.utcnow()
        live = (session.query(AuctionSeason)
                .filter(AuctionSeason.status == A.STATUS_LIVE,
                        AuctionSeason.chat_id.isnot(None)).all())
        for season in live:
            # One broken auction must not stall the others — the same
            # isolation ``_draft_tick`` uses, for the same reason.
            try:
                async with _lock():
                    await _tick_one(context, session, season, now)
                maybe_start_countdown(context.bot, session, season,
                                      now=datetime.utcnow())
            except Exception:
                session.rollback()
                logger.exception("auction #%s tick failed", season.id)
    finally:
        session.close()


async def _tick_one(context, session, season, now):
    from services import auction_service as A

    lot = A.current_lot(session, season)

    # Realign a pointer left stale by a crash between two commits, and say
    # nothing else this tick.
    if lot is not None and season.current_lot_id != lot.id:
        season.current_lot_id = lot.id
        session.commit()
        return

    if lot is None:
        # Nothing on the block: either the auction just finished, or an admin
        # resolved a lot from the website and the next one is waiting.
        if A.next_queued(session, season.id) is not None:
            A.open_next_lot(session, season, now=now)
        else:
            A.complete_if_done(session, season)
        session.commit()
        await drain_events(context.bot, session, season)
        await refresh_board(context.bot, session, season, force=True, now=now)
        session.commit()
        return

    left = A.seconds_left(lot, now)

    if left is not None and left > 0:
        spoken = await drain_events(context.bot, session, season)
        # "Going once / going twice" is the language of a contest between
        # bidders. An RTM window is one franchise answering one question, so it
        # counts down without the auctioneer's patter.
        stage = (0 if lot.status == A.LOT_RTM_OFFERED
                 else A.going_stage_for(left, season))
        moved_on = stage > int(lot.going_stage or 0)
        new_bids = int(lot.bid_count or 0) != int(season.board_rendered_bid_count or 0)
        if moved_on:
            lot.going_stage = stage
            session.commit()
            # The staged clock says its warnings out loud: "Selling X to
            # Team for ₹…", with the bid buttons on it. Once per stage per
            # deadline — a bid resets going_stage, so it re-arms both. Inside
            # the final count the count itself is the warning.
            if (A.staged_clock(season) is not None
                    and left > A.countdown_seconds(season)):
                await send_warning(context.bot, session, season, lot, stage,
                                   left)
        # Redraw when something the board says has actually changed — a new
        # leading price, going once/twice, or anything just announced — and
        # whenever there is no board yet, which is the case on the first tick
        # of a lot. Without that last clause the room would be told a lot had
        # opened and then have nothing to watch it on until somebody bid.
        stale_board = int(getattr(season, "board_lot_id", 0) or 0) != lot.id
        if (moved_on or new_bids or spoken or stale_board
                or not season.board_message_id):
            await refresh_board(context.bot, session, season, force=True,
                                now=now)
            session.commit()
        return

    # Expired. A lot mid-Right-To-Match resolves the stage it is on rather
    # than the lot — each stage taking the SAFE default, so a franchise nobody
    # is running can never wedge an auction.
    if lot.status == A.LOT_RTM_OFFERED:
        A.resolve_rtm_stage(session, season, lot, now=now)
    else:
        A.resolve_expired(session, season, lot, now=now)
    session.commit()
    await drain_events(context.bot, session, season, limit=DRAIN_PER_TICK + 3)
    # Only reach for the next lot once nothing is in front of the room. An RTM
    # window that has just opened, or moved on a stage, still is.
    if season.status == A.STATUS_LIVE and A.current_lot(session, season) is None:
        A.open_next_lot(session, season, now=now)
        session.commit()
        await drain_events(context.bot, session, season)
    await refresh_board(context.bot, session, season, force=True, now=now)
    session.commit()


# ── Registration ─────────────────────────────────────────────────────

def start_auction_scheduler(application):
    """Register the auction clock sweep. Idempotent — adds at most once."""
    if not application.job_queue:
        logger.warning("JobQueue unavailable — auction clock not started")
        return
    try:
        if application.job_queue.get_jobs_by_name(SWEEP_JOB_NAME):
            return
        application.job_queue.run_repeating(
            _auction_tick,
            interval=SWEEP_INTERVAL,
            first=SWEEP_INTERVAL,
            name=SWEEP_JOB_NAME,
        )
        logger.info("Auction clock started (every %ss)", SWEEP_INTERVAL)
    except Exception:
        logger.exception("Failed to start auction clock")
