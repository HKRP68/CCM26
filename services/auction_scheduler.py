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

**Why the website never sends a message.** The Flask admin panel runs in a
thread of this process. Rather than reaching across that boundary, an admin
action writes its rows plus one ``AuctionEvent`` and commits; this job drains
everything past ``AuctionSeason.announced_event_id`` and says it out loud, in
id order, whether a command or a web form produced it. Nothing but the database
crosses over.
"""

import asyncio
import logging
from datetime import datetime

from telegram.error import (
    BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut,
)

logger = logging.getLogger(__name__)

SWEEP_JOB_NAME = "auction_clock"
SWEEP_INTERVAL = 2  # seconds

# Events that are reflected on the board rather than announced. A bid is the
# whole reason the board exists; announcing each one would put forty messages
# into a room inside one lot and hit Telegram's flood limit in the process.
SILENT_KINDS = {"bid"}

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


async def pin_board(bot, session, season, message):
    """Pin the board once, best-effort.

    An auction group scrolls fast and the board is the one message that answers
    "where are we" — but pinning needs a right the bot may not have been given,
    and an auction must never stop over a pin.
    """
    message_id = getattr(message, "message_id", None)
    if message_id is None or not season.chat_id:
        return None
    try:
        await bot.unpin_chat_message(chat_id=season.chat_id)
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
    """
    from services import auction_service as A

    if not season.chat_id:
        return None
    lot = A.current_lot(session, season)
    body = A.render_board(session, season, lot, now=now)

    if season.board_message_id:
        ok = await edit_board(bot, season.chat_id, season.board_message_id, body)
        if ok:
            season.board_rendered_bid_count = int(lot.bid_count or 0) if lot else 0
            return season.board_message_id
        # The message was deleted, or is too old to edit. Post a fresh one
        # rather than leaving the room with no board at all.
        season.board_message_id = None

    sent = await _send(bot, season.chat_id, body)
    if sent is None:
        return None
    season.board_message_id = sent.message_id
    season.board_rendered_bid_count = int(lot.bid_count or 0) if lot else 0
    await pin_board(bot, session, season, sent)
    return season.board_message_id


# ── The event drain ──────────────────────────────────────────────────

async def drain_events(bot, session, season, *, limit=DRAIN_PER_TICK):
    """Say everything that has happened since the last tick, oldest first.

    The cursor advances past silent events too — they are on the board, not in
    the stream, but they have still been dealt with.
    """
    from services import auction_service as A

    if not season.chat_id:
        return 0
    pending = A.pending_events(session, season, limit=limit)
    if not pending:
        return 0
    spoken = 0
    for event in pending:
        if event.kind not in SILENT_KINDS:
            await _send(bot, season.chat_id, event.headline)
            spoken += 1
        season.announced_event_id = event.id
        # Commit the cursor per event: a crash halfway through a drain must not
        # re-announce what the room has already been told.
        session.commit()
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
        if moved_on or new_bids or spoken or not season.board_message_id:
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
