"""The draft pick clock, and the announcements it and ``/pick`` both make.

**Why the clock is a sweeper.** The host redeploys often, and an in-process
``run_once`` does not survive that — the same reason
``services/giveaway_scheduler`` exists. A draft runs for hours, so losing its
timers to a deploy would strand every team on the clock forever. The deadline
therefore lives in ``PlayerDraft.pick_deadline_at`` and this job reconciles it
every few seconds; a restart picks the clock back up exactly where it was.

**Why the announcements live here too.** An auto-pick has to reach the group
looking exactly like a pick somebody typed — same card, same squad, same "next
turn" line — so there is one implementation of that message and both paths call
it. ``handlers/draft.py`` imports ``announce_pick`` / ``announce_turn`` from
here rather than growing a second copy that can drift.
"""

import logging
from datetime import datetime
from html import escape

from telegram.error import Forbidden, BadRequest, RetryAfter, TelegramError

logger = logging.getLogger(__name__)

SWEEP_JOB_NAME = "draft_clock"
SWEEP_INTERVAL = 15  # seconds

# Telegram rejects a photo caption over 1,024 characters. A squad of four fits
# comfortably; a squad of sixteen does not, so past this length the card goes
# out with a short caption and the full body follows as its own message.
CAPTION_LIMIT = 1000


# ── Mentions ─────────────────────────────────────────────────────────

def mention(session, tg_id, fallback="Owner"):
    """A clickable HTML mention, which a bare ``@handle`` would not always be.

    ``tg://user?id=`` works for someone who has no Telegram username at all —
    and an owner entered by Telegram id may well be exactly that person. The
    twin of ``handlers.match._mention``, kept local so the clock does not have
    to import the 290 KB match module to name somebody.
    """
    if not tg_id:
        return escape(fallback or "Owner")
    label = escape(fallback or "Owner")
    try:
        from models import User
        user = session.query(User).filter(User.telegram_id == int(tg_id)).first()
        if user is not None:
            label = escape(f"@{user.username}" if user.username
                           else (user.first_name or fallback or "Owner"))
    except Exception:
        logger.debug("draft mention lookup failed", exc_info=True)
    return f'<a href="tg://user?id={int(tg_id)}">{label}</a>'


def team_mention(session, team):
    if team is None:
        return "Owner"
    return mention(session, team.owner_tg_id, team.owner_name or team.name)


# ── Sending ──────────────────────────────────────────────────────────

async def _send(bot, chat_id, text, **kwargs):
    """Send one HTML message, swallowing the failures a group chat throws.

    A draft must not stop because the bot was kicked or the chat went
    read-only — the picks are in the database either way, and the next command
    will reprint the board.
    """
    try:
        return await bot.send_message(chat_id=chat_id, text=text,
                                      parse_mode="HTML",
                                      disable_web_page_preview=True, **kwargs)
    except RetryAfter as exc:
        logger.warning("draft send rate-limited: %s", exc)
    except (Forbidden, BadRequest) as exc:
        logger.warning("draft send refused by chat %s: %s", chat_id, exc)
    except TelegramError:
        logger.exception("draft send failed")
    return None


# ── The pinned pick ──────────────────────────────────────────────────

def pinning_on(draft):
    """Whether this draft pins its latest pick. Default on for an older row."""
    return getattr(draft, "pin_picks", True) is not False


async def pin_latest(bot, session, draft, message):
    """Pin ``message`` as the draft's latest pick, unpinning the one before it.

    A draft group runs for hours and scrolls fast; the pin is how somebody
    arriving late — or coming back from a nap — sees where the draft is without
    reading back through the whole room. Exactly one pick is pinned at a time,
    so the pin always answers "what just happened", not "what happened first".

    Everything here is best-effort: pinning needs a right the bot may not have
    been given, and a draft must not stop because a group refused a pin. The id
    is committed on its own because the callers commit at different points (the
    clock before announcing, ``/pick`` after) and an id nobody stored would
    leave the previous pin up forever.
    """
    message_id = getattr(message, "message_id", None)
    if message_id is None or not draft.chat_id or not pinning_on(draft):
        return None
    previous = getattr(draft, "pinned_message_id", None)
    try:
        # Silently: the room is already being told by the announcement itself,
        # and a pin notification per pick is a hundred pings in one evening.
        await bot.pin_chat_message(chat_id=draft.chat_id, message_id=message_id,
                                   disable_notification=True)
    except (Forbidden, BadRequest) as exc:
        logger.warning("draft pin refused by chat %s: %s", draft.chat_id, exc)
        return None
    except Exception:
        # A pick that is already in the database must never be undone by a pin,
        # so this catches everything, not just the Telegram errors.
        logger.warning("draft pin failed", exc_info=True)
        return None
    await _unpin(bot, draft.chat_id, previous)
    _remember_pin(session, draft, message_id)
    return message_id


async def unpin_latest(bot, session, draft):
    """Drop the draft's pin — the pick it points at is no longer the latest."""
    previous = getattr(draft, "pinned_message_id", None)
    if not previous or not draft.chat_id:
        return False
    await _unpin(bot, draft.chat_id, previous)
    _remember_pin(session, draft, None)
    return True


async def _unpin(bot, chat_id, message_id):
    if not message_id:
        return
    try:
        await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)
    except (Forbidden, BadRequest) as exc:
        # Already unpinned by hand, or the message was deleted. Not a problem:
        # the new pin is up either way.
        logger.debug("draft unpin skipped for %s: %s", chat_id, exc)
    except Exception:
        logger.warning("draft unpin failed", exc_info=True)


def _remember_pin(session, draft, message_id):
    try:
        draft.pinned_message_id = message_id
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("draft pin id not stored")


async def announce_pick(bot, session, draft, pick, player):
    """Post a completed pick to the draft group, with the player's card.

    The same message whether a person typed ``/pick`` or the clock ran out; the
    only difference is the ⏱ badge ``render_pick`` adds for an auto-pick.

    Whatever ends up carrying the pick — the card, or the text fallback — is
    then **pinned**, replacing the previous pick's pin. Every path that fills a
    slot comes through here (``/pick``, the clock, ``/dskip``, ``/dautopick``),
    which is why the pin lives here and not in four handlers.
    """
    sent = await _post_pick(bot, session, draft, pick, player)
    await pin_latest(bot, session, draft, sent)
    return sent


async def _post_pick(bot, session, draft, pick, player):
    from services import draft_service as ds

    if not draft.chat_id:
        return None
    next_pick = ds.current_pick(session, draft)
    next_team = None
    if next_pick is not None:
        from models import DraftTeam
        next_team = (session.query(DraftTeam)
                     .filter(DraftTeam.id == next_pick.team_id).first())
    body = ds.render_pick(session, draft, pick, player, next_pick=next_pick,
                          next_mention=team_mention(session, next_team))

    card = _catalogue_card(session, player)
    if card is None:
        return await _send(bot, draft.chat_id, body)

    # One message when the caption fits, card-then-body when it doesn't.
    if len(body) <= CAPTION_LIMIT:
        sent = await _send_card(bot, session, draft.chat_id, card, body)
        if sent is not None:
            return sent
        return await _send(bot, draft.chat_id, body)

    headline = "\n".join([
        f"🏏 <b>{escape((pick.team.name if pick.team else '').upper())}</b>",
        f"R{pick.round_no} P{pick.pick_no} · {ds.tier_badge(pick.tier)} slot",
        ds.player_line(player),
    ])
    await _send_card(bot, session, draft.chat_id, card, headline)
    # The body, not the card: it is the message that carries the squad and
    # whose turn it is, which is what the pin is for.
    return await _send(bot, draft.chat_id, body)


def _catalogue_card(session, player):
    """The master ``Player`` row behind a pool entry, when the names matched."""
    if player is None or not getattr(player, "source_player_id", None):
        return None
    try:
        from models import Player
        return (session.query(Player)
                .filter(Player.id == player.source_player_id).first())
    except Exception:
        logger.debug("draft card lookup failed", exc_info=True)
        return None


async def _send_card(bot, session, chat_id, card, caption):
    try:
        from services.card_sender import send_player_card
        return await send_player_card(bot=bot, chat_id=chat_id, player=card,
                                      caption=caption, parse_mode="HTML",
                                      session=session)
    except Exception:
        logger.exception("draft card send failed; falling back to text")
        return None


async def announce_turn(bot, session, draft, pick, *, prefix=""):
    """Post whose turn it is. Used at the start and after every pick."""
    from services import draft_service as ds
    from models import DraftTeam

    if not draft.chat_id:
        return None
    team = None
    if pick is not None:
        team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
    body = ds.render_turn(session, draft, pick,
                          mention=team_mention(session, team))
    if pick is not None:
        body += "\n\nType <code>/pick &lt;player name&gt;</code> to make the pick."
    return await _send(bot, draft.chat_id, (prefix + body) if prefix else body)


async def announce_skip(bot, session, draft, pick):
    from services import draft_service as ds
    from models import DraftTeam

    if not draft.chat_id:
        return None
    team = session.query(DraftTeam).filter(DraftTeam.id == pick.team_id).first()
    next_pick = ds.current_pick(session, draft)
    next_team = None
    if next_pick is not None:
        next_team = (session.query(DraftTeam)
                     .filter(DraftTeam.id == next_pick.team_id).first())
    body = (f"⏭ <b>R{pick.round_no} P{pick.pick_no}</b> · "
            f"{escape(team.name if team else '')} — <b>slot passed</b>\n"
            f"<i>No player left in the pool could legally join that squad.</i>\n\n"
            + ds.render_turn(session, draft, next_pick,
                             mention=team_mention(session, next_team)))
    sent = await _send(bot, draft.chat_id, body)
    # Pinned like a pick: the pin answers "where is the draft", and a passed
    # slot is as much an answer to that as a filled one.
    await pin_latest(bot, session, draft, sent)
    return sent


# ── The sweep ────────────────────────────────────────────────────────

async def _draft_tick(context):
    """One sweep: warn the teams running short, auto-pick the ones who ran out."""
    from database import get_session
    from models import PlayerDraft
    from services import draft_service as ds

    session = get_session()
    try:
        now = datetime.utcnow()
        live = (session.query(PlayerDraft)
                .filter(PlayerDraft.status == ds.STATUS_LIVE,
                        PlayerDraft.chat_id.isnot(None),
                        PlayerDraft.pick_deadline_at.isnot(None)).all())
        for draft in live:
            # One broken draft must not stall the others, so each is isolated —
            # the same shape giveaway_scheduler uses for the same reason.
            try:
                await _tick_one(context, session, draft, now)
            except Exception:
                session.rollback()
                logger.exception("draft #%s tick failed", draft.id)
    finally:
        session.close()


async def _tick_one(context, session, draft, now):
    from services import draft_service as ds

    pick = ds.current_pick(session, draft)
    if pick is None:
        # Every slot is filled but the draft never got marked done (a crash
        # between the last pick and the commit). Close it out.
        ds.advance(session, draft)
        session.commit()
        return

    # The pointer can lag the row when a pick was made elsewhere in this
    # process; realign before judging the deadline.
    if draft.current_pick_id != pick.id:
        draft.current_pick_id = pick.id
        session.commit()
        return

    left = (draft.pick_deadline_at - now).total_seconds()

    if left > 0:
        warn_at = max(0, int(draft.warn_seconds or 0))
        if warn_at and left <= warn_at and not draft.warn_sent:
            draft.warn_sent = True
            session.commit()
            from models import DraftTeam
            team = (session.query(DraftTeam)
                    .filter(DraftTeam.id == pick.team_id).first())
            await _send(context.bot, draft.chat_id,
                        f"⏰ <b>{ds.format_clock(int(left))} left</b> — "
                        f"R{pick.round_no} P{pick.pick_no}, "
                        f"{escape(team.name if team else '')}.\n"
                        f"{team_mention(session, team)}, you're on the clock.")
        return

    resolved, player = ds.resolve_expired(session, draft, pick)
    session.commit()
    if player is None:
        await announce_skip(context.bot, session, draft, resolved)
    else:
        await announce_pick(context.bot, session, draft, resolved, player)
    if draft.status == ds.STATUS_COMPLETED:
        await _send(context.bot, draft.chat_id,
                    "🏁 <b>Draft complete.</b> Every slot is filled — "
                    "an admin can now publish the squads with /dpublish.")


# ── Registration ─────────────────────────────────────────────────────

def start_draft_scheduler(application):
    """Register the pick-clock sweep. Idempotent — adds at most once."""
    if not application.job_queue:
        logger.warning("JobQueue unavailable — draft clock not started")
        return
    try:
        if application.job_queue.get_jobs_by_name(SWEEP_JOB_NAME):
            return
        application.job_queue.run_repeating(
            _draft_tick,
            interval=SWEEP_INTERVAL,
            first=SWEEP_INTERVAL,
            name=SWEEP_JOB_NAME,
        )
        logger.info("Draft clock started (every %ss)", SWEEP_INTERVAL)
    except Exception:
        logger.exception("Failed to start draft clock")
