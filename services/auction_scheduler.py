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
import logging
import time
from datetime import datetime

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

# Send the player's card when a lot opens. A switch rather than a constant
# for the tests, which have no card renderer to spare.
SEND_LOT_CARD = True

# Latched off for the process when Telegram refuses a rich board edit, so a
# payload it does not like costs one refused call, not one every two seconds.
_board_rich_ok = True

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


async def _send_bid_line(bot, chat_id, text):
    """Send a bid line. True when it landed or never can; None to retry.

    A transient failure — flood control, a timeout, a network error — must not
    advance the drain cursor, or the burst is never announced. A permanent
    refusal (kicked, read-only, a bad request) is reported as done, so one
    unreachable chat cannot stall the drain forever.
    """
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                               disable_web_page_preview=True)
        return True
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
                                              session=session)
                if sent is not None:
                    return sent
        except Exception:
            logger.exception("auction lot card failed; sending text")
    return await _send(bot, season.chat_id, caption)


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
            delivered = await _send_bid_line(
                bot, season.chat_id, AR.bid_burst_html(session, season, run))
            if delivered is None:
                # Rate-limited or a network blip: keep the cursor where it
                # is and send the whole burst again on a later tick.
                break
            _last_bid_message[season.id] = time.monotonic()
            spoken += 1
            last = run[-1]
        else:
            last = event
            if event.kind not in SILENT_KINDS:
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
                await _tick_one(context, session, season, now)
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
                 else A.going_stage_for(left))
        moved_on = stage > int(lot.going_stage or 0)
        new_bids = int(lot.bid_count or 0) != int(season.board_rendered_bid_count or 0)
        if moved_on:
            lot.going_stage = stage
            session.commit()
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
