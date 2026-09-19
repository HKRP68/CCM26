"""Durable delivery (and re-delivery) of match scorecard images.

Why this module exists
──────────────────────
The batting / bowling / summary cards used to be a fire-and-forget render off
live match state, sent straight from the innings-end handler:

    png = generate_batting_scorecard(...)      # CPU-bound PIL render
    await bot.send_photo(chat_id, png)         # one attempt, no retry

Three independent things could swallow a card, and all three did:

  1. **One render failure killed both cards.** The two renders ran under a
     single ``asyncio.gather`` inside one ``try``, so an exception in the
     batting card discarded the bowling card too, and the handler logged and
     moved on.
  2. **One dropped send lost the card for good.** ``send_photo`` got a single
     attempt. A ``TimedOut``, a transient ``NetworkError`` or Telegram flood
     control (``RetryAfter``) on a busy group was terminal.
  3. **Nothing could be replayed.** The values the card is drawn from live in
     ``match_state``, which is cleaned up shortly after the match ends. Once
     that row was gone the card could never be rebuilt — not by a retry, not by
     an admin, not by the group asking for it.

So the order is inverted here. The render-ready values are persisted *first*
(:class:`models.MatchScorecardImage`), then each card is rendered
independently, then each send is retried. A card that still fails has a durable
row behind it, which is what ``/lastscorecard`` redraws on demand.

What a payload holds
────────────────────
Exactly the data arguments of the generator named by ``card_type`` — team
names, rows, fall of wickets, extras. It deliberately does **not** store the
admin-tunable accent colours, the text settings, or the teams' logos: those are
all re-read live in :func:`render_card`, so a card redrawn next month follows
the theme the admins have now and the crest the team has now, rather than the
ones in force when the match was played.
"""

import asyncio
import io
import json
import logging

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

logger = logging.getLogger(__name__)

CARD_BATTING = "batting"
CARD_BOWLING = "bowling"
CARD_SUMMARY = "summary"
CARD_TYPES = (CARD_BATTING, CARD_BOWLING, CARD_SUMMARY)

# ``innings`` value for a card that describes the whole match rather than one
# innings. Kept as a constant so the sort below and the queries agree.
WHOLE_MATCH = 0

# A send gets this many attempts in total (the first one plus retries). Three
# is enough to ride out a flood-control window or a blip without holding the
# innings-end handler open for a minute when a chat is genuinely gone.
SEND_ATTEMPTS = 3


# ══════════════════════════════════════════════════════════════════════
# Retry helpers
# ══════════════════════════════════════════════════════════════════════

def _retry_after_seconds(exc):
    """Flood-control wait from a ``RetryAfter``, as float seconds.

    ``RetryAfter.retry_after`` is a number in python-telegram-bot < 22.2 and can
    be a ``datetime.timedelta`` in newer ones. This repo pins only ``>=21.3``,
    so normalise either form — the same reasoning as
    ``services.match_broadcast._retry_after_seconds``.
    """
    ra = getattr(exc, "retry_after", 1)
    if hasattr(ra, "total_seconds"):
        ra = ra.total_seconds()
    try:
        return float(ra)
    except (TypeError, ValueError):
        return 1.0


async def send_photo_with_retry(bot, chat_id, photo_factory, *, caption=None,
                                parse_mode="HTML", attempts=SEND_ATTEMPTS,
                                **kwargs):
    """``bot.send_photo`` that survives flood control and transient failures.

    ``photo_factory`` is a zero-argument callable returning what to send. It is
    a factory rather than a value because a ``BytesIO`` is consumed by a failed
    upload: re-sending the same exhausted buffer uploads zero bytes and fails
    forever. Calling the factory per attempt hands each one a fresh stream.

    Returns the sent ``Message``, or ``None`` when every attempt failed. A chat
    the bot can no longer post to (``Forbidden``) stops immediately — retrying
    a kick or a block just delays the caller.
    """
    for attempt in range(attempts):
        photo = None
        try:
            photo = photo_factory()
            if photo is None:
                return None
            return await bot.send_photo(chat_id=chat_id, photo=photo,
                                        caption=caption, parse_mode=parse_mode,
                                        **kwargs)
        except RetryAfter as exc:
            wait = _retry_after_seconds(exc) + 0.5
            logger.warning("scorecard send flood-limited on chat %s, waiting %.1fs",
                           chat_id, wait)
            await asyncio.sleep(wait)
        except Forbidden:
            # Bot removed, blocked, or the chat is gone. No retry will fix it.
            logger.warning("scorecard send forbidden for chat %s", chat_id)
            return None
        except BadRequest as exc:
            # NB: BadRequest subclasses NetworkError in PTB, so it must be
            # caught before the NetworkError clause below. A stale/rotated
            # file_id lands here — the caller re-renders instead.
            logger.warning("scorecard send rejected (%s) on chat %s", exc, chat_id)
            return None
        except (TimedOut, NetworkError):
            # The photo may in fact have landed before the client gave up; a
            # duplicate card is a far smaller cost than a missing one.
            await asyncio.sleep(0.75 * (attempt + 1))
        except Exception:
            logger.exception("scorecard send failed on chat %s", chat_id)
            await asyncio.sleep(0.75 * (attempt + 1))
    logger.error("scorecard send gave up after %s attempts on chat %s",
                 attempts, chat_id)
    return None


# ══════════════════════════════════════════════════════════════════════
# Rendering
# ══════════════════════════════════════════════════════════════════════

def _visuals(resolve):
    """Run one :mod:`services.card_identity` lookup on a session of our own.

    The identity functions take a session rather than opening one, because the
    handlers that call them directly already hold one and a second pooled
    connection mid-transaction is the exhaustion trap
    ``services/player_image_service`` documents. Rendering, though, runs on a
    worker thread with no session in hand, so this is where one is opened.

    Never raises: a card without a crest is a detail, a card that fails to
    render is the card.
    """
    from services import card_identity
    session = card_identity.open_session()
    try:
        return resolve(session)
    except Exception:
        logger.exception("card identity lookup failed — drawing without it")
        return {}
    finally:
        session.close()


def _live_style(is_first_innings, team_name=None, user_id=None):
    """The team's accent, crest and the admin text settings, at render time.

    Kept out of the stored payload on purpose: a card redrawn later should
    follow the theme the admins have configured now and the crest the team has
    now, not the ones in force when the match was played.
    """
    from services import card_identity
    style = _visuals(lambda s: card_identity.innings_visuals(
        s, team_name=team_name, user_id=user_id,
        is_first_innings=is_first_innings))
    style.setdefault("accent_hex", None)
    style.setdefault("text_settings", None)
    style.setdefault("team_logo_png", None)
    return style


def _summary_style(inn1_team=None, inn2_team=None, potm_player_id=None,
                   inn1_user_id=None, inn2_user_id=None, potm_name=None):
    """Both teams' colours and crests, the POTM portrait, and the text settings.

    The summary card used to hardcode its red/blue by innings position while
    the batting and bowling cards read the admin colours — so the three cards
    of one match could disagree. They all resolve through the same place now,
    and a team that has set its own colour gets it.
    """
    from services import card_identity
    style = _visuals(lambda s: card_identity.summary_visuals(
        s, inn1_team=inn1_team, inn2_team=inn2_team,
        inn1_user_id=inn1_user_id, inn2_user_id=inn2_user_id,
        potm_player_id=potm_player_id, potm_name=potm_name))
    for key in ("text_settings", "inn1_color", "inn2_color",
                "inn1_logo_png", "inn2_logo_png", "potm_photo_png"):
        style.setdefault(key, None)
    style.setdefault("dynamic_flourish", False)
    return style


def _accepted_kwargs(func, payload):
    """Drop payload keys the generator does not take.

    Rows outlive code. A card archived today has to still redraw after the
    generator gains or loses a parameter, and ``f(**payload)`` with one stale
    key is a ``TypeError`` that would retire every old row at once. Dropping
    what no longer fits costs at most a detail on the card; keeping it costs
    the card.
    """
    import inspect
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return dict(payload)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(payload)
    known = {name for name, p in params.items()
             if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.KEYWORD_ONLY)}
    dropped = sorted(set(payload) - known)
    if dropped:
        logger.warning("scorecard payload for %s carries keys the renderer no "
                       "longer takes: %s", func.__name__, ", ".join(dropped))
    return {k: v for k, v in payload.items() if k in known}


def render_card(card_type, payload):
    """Draw one card from a stored payload. Returns PNG bytes or ``None``.

    Synchronous and CPU-bound (PIL); call it through ``asyncio.to_thread`` from
    the event loop, the way every other card render in the bot does.
    """
    try:
        payload = dict(payload or {})
        if card_type in (CARD_BATTING, CARD_BOWLING):
            from services.scorecard_card import (generate_batting_scorecard,
                                                 generate_bowling_scorecard)
            generate = (generate_batting_scorecard if card_type == CARD_BATTING
                        else generate_bowling_scorecard)
            payload.update(_live_style(payload.get("is_first_innings", True),
                                       payload.get("team_name"),
                                       payload.get("team_user_id")))
            return generate(**_accepted_kwargs(generate, payload))
        if card_type == CARD_SUMMARY:
            from services.match_summary_card import generate_match_summary
            payload.update(_summary_style(
                payload.get("inn1_team"), payload.get("inn2_team"),
                payload.get("potm_player_id"),
                inn1_user_id=payload.get("inn1_user_id"),
                inn2_user_id=payload.get("inn2_user_id"),
                potm_name=payload.get("potm_name")))
            # ``match_date`` round-trips through JSON as an ISO string.
            raw_date = payload.get("match_date")
            if isinstance(raw_date, str):
                from datetime import datetime as _dt
                try:
                    payload["match_date"] = _dt.fromisoformat(raw_date)
                except ValueError:
                    payload.pop("match_date", None)
            return generate_match_summary(
                **_accepted_kwargs(generate_match_summary, payload))
    except Exception:
        logger.exception("scorecard render failed for %s card", card_type)
        return None
    logger.error("unknown scorecard card_type %r", card_type)
    return None


async def render_card_async(card_type, payload):
    """:func:`render_card` off the event loop."""
    return await asyncio.to_thread(render_card, card_type, payload)


# ══════════════════════════════════════════════════════════════════════
# Persistence
# ══════════════════════════════════════════════════════════════════════

def _sort_key(card):
    """Delivery order: innings 1 before innings 2, batting before bowling, and
    the whole-match summary last whatever its stored ``sort_order``."""
    innings = card.get("innings", WHOLE_MATCH)
    # WHOLE_MATCH (0) has to sort *after* both innings, not before them.
    innings_rank = 99 if innings == WHOLE_MATCH else innings
    type_rank = {CARD_BATTING: 0, CARD_BOWLING: 1, CARD_SUMMARY: 2}.get(
        card.get("card_type"), 3)
    return (innings_rank, type_rank)


def record_cards(match_id, chat_id, cards):
    """Persist render-ready payloads for one or more cards of a match.

    ``cards`` is an iterable of dicts with ``card_type``, ``payload`` and
    optionally ``innings`` and ``caption``. Upserts on
    ``(match_id, innings, card_type)`` so replaying an innings end overwrites
    rather than duplicating.

    Always opens its own session and commits it. The write paths deliberately
    do not accept a caller's session: committing one would also commit whatever
    else that caller had pending, and archiving a card is not a reason to flush
    someone else's half-finished unit of work.

    Best-effort by contract: this runs on the innings-end path and must never
    be the reason a match fails to finish. Returns True when the rows were
    committed.
    """
    from database import get_session
    session = get_session()
    try:
        from models import MatchScorecardImage
        for card in cards or []:
            card_type = card.get("card_type")
            if card_type not in CARD_TYPES:
                logger.error("refusing to record unknown card_type %r", card_type)
                continue
            innings = int(card.get("innings", WHOLE_MATCH) or WHOLE_MATCH)
            row = (session.query(MatchScorecardImage)
                   .filter(MatchScorecardImage.match_id == match_id,
                           MatchScorecardImage.innings == innings,
                           MatchScorecardImage.card_type == card_type)
                   .first())
            payload_json = json.dumps(card.get("payload") or {},
                                      separators=(",", ":"), default=str)
            caption = (card.get("caption") or "")[:300] or None
            order = _sort_key(card)
            sort_order = order[0] * 10 + order[1]
            if row is None:
                row = MatchScorecardImage(
                    match_id=match_id, innings=innings, card_type=card_type)
                session.add(row)
            row.chat_id = chat_id
            row.caption = caption
            row.payload_json = payload_json
            row.sort_order = sort_order
            # A re-recorded payload invalidates the cached image: the values
            # changed, so the old file_id would re-send a stale card.
            row.file_id = None
            row.delivered = False
        session.commit()
        return True
    except Exception:
        logger.exception("recording scorecard payloads for match %s failed", match_id)
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def mark_delivered(match_id, innings, card_type, file_id=None):
    """Record that a card reached its chat, caching the ``file_id`` for re-sends.

    Owns its session for the same reason :func:`record_cards` does.
    """
    from database import get_session
    session = get_session()
    try:
        from models import MatchScorecardImage
        row = (session.query(MatchScorecardImage)
               .filter(MatchScorecardImage.match_id == match_id,
                       MatchScorecardImage.innings == innings,
                       MatchScorecardImage.card_type == card_type)
               .first())
        if row is None:
            return False
        row.delivered = True
        if file_id:
            row.file_id = file_id
        session.commit()
        return True
    except Exception:
        logger.exception("marking scorecard %s/%s/%s delivered failed",
                         match_id, innings, card_type)
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        session.close()


def _clear_file_id(match_id, innings, card_type):
    """Drop a ``file_id`` Telegram has stopped accepting, so the next send renders."""
    from database import get_session
    session = get_session()
    try:
        from models import MatchScorecardImage
        row = (session.query(MatchScorecardImage)
               .filter(MatchScorecardImage.match_id == match_id,
                       MatchScorecardImage.innings == innings,
                       MatchScorecardImage.card_type == card_type)
               .first())
        if row is not None and row.file_id:
            row.file_id = None
            session.commit()
    except Exception:
        logger.exception("clearing stale scorecard file_id failed")
        try:
            session.rollback()
        except Exception:
            pass
    finally:
        session.close()


def load_cards(match_id, session=None):
    """Every stored card of a match, in delivery order, as plain dicts.

    Detached dicts rather than ORM rows so callers can close the session before
    the (slow, awaitable) send — holding a connection open across a Telegram
    round trip is how a pool runs dry.
    """
    own_session = session is None
    if own_session:
        from database import get_session
        session = get_session()
    try:
        from models import MatchScorecardImage
        rows = (session.query(MatchScorecardImage)
                .filter(MatchScorecardImage.match_id == match_id)
                .order_by(MatchScorecardImage.sort_order,
                          MatchScorecardImage.id)
                .all())
        out = []
        for row in rows:
            try:
                payload = json.loads(row.payload_json or "{}")
            except (TypeError, ValueError):
                logger.exception("scorecard payload for match %s card %s is "
                                 "not valid JSON", match_id, row.id)
                payload = {}
            out.append({
                "match_id": row.match_id,
                "chat_id": row.chat_id,
                "innings": row.innings,
                "card_type": row.card_type,
                "caption": row.caption,
                "payload": payload,
                "file_id": row.file_id,
                "delivered": bool(row.delivered),
            })
        return out
    except Exception:
        logger.exception("loading scorecards for match %s failed", match_id)
        return []
    finally:
        if own_session:
            session.close()


def latest_match_id_for_chat(chat_id, session=None):
    """The most recent match played in ``chat_id`` that has stored cards."""
    own_session = session is None
    if own_session:
        from database import get_session
        session = get_session()
    try:
        from models import MatchScorecardImage
        row = (session.query(MatchScorecardImage.match_id)
               .filter(MatchScorecardImage.chat_id == chat_id)
               .order_by(MatchScorecardImage.match_id.desc())
               .first())
        return row[0] if row else None
    except Exception:
        logger.exception("last-scorecard lookup for chat %s failed", chat_id)
        return None
    finally:
        if own_session:
            session.close()


def latest_match_id_for_user(user_id, session=None):
    """The most recent match with stored cards that ``user_id`` played in.

    Used when ``/lastscorecard`` is run in a DM, where there is no group match
    history to read — the caller's own last match is the sensible answer.
    """
    own_session = session is None
    if own_session:
        from database import get_session
        session = get_session()
    try:
        from sqlalchemy import or_
        from models import Match, MatchScorecardImage
        row = (session.query(MatchScorecardImage.match_id)
               .join(Match, Match.id == MatchScorecardImage.match_id)
               .filter(or_(Match.user1_id == user_id, Match.user2_id == user_id))
               .order_by(MatchScorecardImage.match_id.desc())
               .first())
        return row[0] if row else None
    except Exception:
        logger.exception("last-scorecard lookup for user %s failed", user_id)
        return None
    finally:
        if own_session:
            session.close()


# ══════════════════════════════════════════════════════════════════════
# Sending
# ══════════════════════════════════════════════════════════════════════

async def deliver_card(bot, chat_id, card, *, rendered=None,
                       reply_to_message_id=None):
    """Send one stored card, preferring its cached ``file_id``.

    Order of attack:
      1. Cached ``file_id`` — no render, no upload. A rejection here clears the
         id and falls through rather than losing the card.
      2. ``rendered`` bytes if the caller already drew this card (see
         :func:`deliver_cards`), otherwise a fresh render from the stored
         payload; either way the ``file_id`` Telegram hands back is cached.

    Returns True when the photo landed.
    """
    match_id = card.get("match_id")
    innings = card.get("innings", WHOLE_MATCH)
    card_type = card.get("card_type")
    caption = card.get("caption")
    extra = ({"reply_to_message_id": reply_to_message_id}
             if reply_to_message_id is not None else {})

    file_id = card.get("file_id")
    if file_id:
        msg = await send_photo_with_retry(
            bot, chat_id, lambda: file_id, caption=caption, **extra)
        if msg is not None:
            return True
        # Stale or rotated id (channel cleaned, file expired). Forget it so the
        # next request renders instead of failing the same way forever.
        logger.warning("cached file_id rejected for match %s %s/%s — re-rendering",
                       match_id, innings, card_type)
        _clear_file_id(match_id, innings, card_type)

    png = rendered
    if not png:
        png = await render_card_async(card_type, card.get("payload"))
    if not png:
        return False

    msg = await send_photo_with_retry(
        bot, chat_id, lambda: io.BytesIO(png), caption=caption, **extra)
    if msg is None:
        return False
    new_file_id = None
    try:
        if msg.photo:
            new_file_id = msg.photo[-1].file_id
    except Exception:
        logger.debug("file_id capture failed (non-fatal)", exc_info=True)
    mark_delivered(match_id, innings, card_type, file_id=new_file_id)
    return True


async def deliver_cards(bot, chat_id, cards, *, reply_to_message_id=None):
    """Send several stored cards in order. Returns how many landed.

    Each card is independent: one that cannot be drawn or delivered never stops
    the rest, which is the whole point — a broken bowling card used to take the
    batting card down with it.

    The renders run concurrently (they are CPU-bound PIL work on worker
    threads, so one innings' cards do not have to be drawn one after the other
    while other live matches wait), and the *sends* still go out in order, so
    the chat reads batting-then-bowling as it always has. ``render_card_async``
    returns ``None`` rather than raising, so one bad card cannot poison the
    batch the way a bare ``asyncio.gather`` would.
    """
    cards = list(cards or [])
    if not cards:
        return 0

    # A card with a cached file_id needs no render at all — only draw the rest.
    # ``drawn`` is keyed by position in ``cards``: a card that is absent from it
    # was never drawn here, while one mapped to None was drawn and came back
    # empty. The two have to stay distinguishable, or a failed render would be
    # retried a second time below for nothing.
    to_draw = [i for i, c in enumerate(cards) if not c.get("file_id")]
    drawn = {}
    if to_draw:
        results = await asyncio.gather(
            *(render_card_async(cards[i].get("card_type"),
                                cards[i].get("payload")) for i in to_draw),
            return_exceptions=True)
        for index, result in zip(to_draw, results):
            if isinstance(result, BaseException):
                logger.exception("scorecard render raised for match %s %s/%s",
                                 cards[index].get("match_id"),
                                 cards[index].get("innings"),
                                 cards[index].get("card_type"),
                                 exc_info=result)
                result = None
            drawn[index] = result

    sent = 0
    threaded_reply = reply_to_message_id
    for index, card in enumerate(cards):
        if index in drawn and not drawn[index]:
            # Already drawn and it came back empty. Drawing it a second time
            # would fail identically and cost another PIL pass.
            logger.error("scorecard %s/%s of match %s could not be rendered",
                         card.get("innings"), card.get("card_type"),
                         card.get("match_id"))
            continue
        ok = await deliver_card(
            bot, chat_id, card, rendered=drawn.get(index),
            # Only the first card actually sent threads onto the request, so a
            # re-send reads as one block instead of five reply chains.
            reply_to_message_id=threaded_reply)
        threaded_reply = None
        sent += 1 if ok else 0
    return sent


# ══════════════════════════════════════════════════════════════════════
# The Player of the Match's collectible card
# ══════════════════════════════════════════════════════════════════════

def _potm_card_enabled():
    """Whether the extra photo is switched on.

    Assumes on for anything that is not an explicit "off": a config that cannot
    be read, and a column that is there but was never written — a row that
    predates the migration reads ``None``, and ``bool(None)`` would quietly ship
    the feature off to exactly the installs that already existed.
    """
    try:
        from services.config_service import get_config
        value = get_config().get("scorecard_potm_card")
    except Exception:
        logger.exception("POTM card toggle lookup failed — assuming on")
        return True
    return True if value is None else bool(value)


def potm_card_bytes(player_id=None, name=None):
    """Draw the award winner's card. Image bytes or ``None``, never raises.

    Synchronous and CPU-bound, like :func:`render_card` — call it through
    ``asyncio.to_thread`` from the event loop. Owns a session for the same
    reason :func:`_visuals` does: this runs well away from whatever handler
    started the match, with no session in hand.

    Honours the ``scorecard_potm_card`` switch, so every mode gets the toggle
    by using this rather than reaching for ``card_identity`` directly.
    """
    if not player_id and not name:
        return None
    if not _potm_card_enabled():
        return None
    from services import card_identity
    session = card_identity.open_session()
    try:
        return card_identity.potm_card_png(session, player_id=player_id,
                                           name=name)
    except Exception:
        logger.exception("POTM card render failed (player=%s name=%r)",
                         player_id, name)
        return None
    finally:
        session.close()


def potm_card_caption(name=None, team=None):
    """The caption that goes under the player card, in one place so the modes
    that send it themselves (the Mini App album) read the same as the rest."""
    import html
    label = html.escape(str(name).strip()) if name else "Player of the Match"
    caption = f"🏅 <b>{label}</b>"
    if team:
        caption += f" — {html.escape(str(team).strip())}"
    return caption


async def send_potm_card(bot, chat_id, *, player_id=None, name=None, team=None,
                         reply_to_message_id=None):
    """Post the Player of the Match's collectible card after the summary card.

    The summary's POTM strip is 125px tall and the card is 1536×1024, so it
    cannot be composited in and still be readable — it goes out as its own
    photo, which also means every mode gets it rather than only the ones whose
    strip has room.

    Best-effort from end to end. The summary card has already landed by the time
    this runs, and a player without art, a stale id or a render failure must all
    read as "no second photo" rather than as an error on a finished match.
    Returns True when a photo was sent.
    """
    if not player_id and not name:
        return False
    try:
        png = await asyncio.to_thread(potm_card_bytes, player_id, name)
    except Exception:
        logger.exception("POTM card render thread failed (player=%s name=%r)",
                         player_id, name)
        return False
    if not png:
        return False

    caption = potm_card_caption(name, team)
    extra = ({"reply_to_message_id": reply_to_message_id}
             if reply_to_message_id is not None else {})
    msg = await send_photo_with_retry(
        bot, chat_id, lambda: io.BytesIO(png), caption=caption, **extra)
    return msg is not None


# ══════════════════════════════════════════════════════════════════════
# Text fallback
# ══════════════════════════════════════════════════════════════════════

def _fmt_row_name(row):
    return str(row.get("name", "?"))


def text_scorecard(cards):
    """A plain-text rendering of a set of innings cards.

    The last line of defence: when every image fails — a render bug, a font
    that went missing in a deploy, a chat that rejects photos — the group still
    gets the numbers instead of silence. Reads the same payloads the images are
    drawn from, so it can never disagree with them.

    Returns an HTML string, or ``None`` when there is nothing to show.
    """
    blocks = []
    for card in sorted(cards or [], key=_sort_key):
        payload = card.get("payload") or {}
        card_type = card.get("card_type")
        if card_type == CARD_BATTING:
            team = payload.get("team_name", "Team")
            head = (f"🏏 <b>{team}</b> — "
                    f"{payload.get('total_runs', 0)}/{payload.get('total_wickets', 0)} "
                    f"({payload.get('overs_str', '0.0')})")
            lines = [head]
            for row in (payload.get("batsmen_rows") or []):
                if row.get("status") == "dnb":
                    continue
                lines.append(
                    f"• {_fmt_row_name(row)} {row.get('runs', 0)}"
                    f"({row.get('balls', 0)}) — {row.get('dismissal', '')}".strip())
            extras = payload.get("extras_dict") or {}
            if extras.get("total"):
                lines.append(f"• Extras {extras['total']}")
            blocks.append("\n".join(lines))
        elif card_type == CARD_BOWLING:
            team = payload.get("team_name", "Team")
            lines = [f"🎳 <b>{team}</b> — Bowling"]
            for row in (payload.get("bowlers_rows") or []):
                lines.append(
                    f"• {_fmt_row_name(row)} {row.get('overs', '0')}-"
                    f"{row.get('maidens', 0)}-{row.get('runs_conceded', 0)}-"
                    f"{row.get('wickets', 0)}")
            blocks.append("\n".join(lines))
        elif card_type == CARD_SUMMARY:
            if not payload.get("winner_name"):
                continue
            lines = [
                "🏆 <b>Result</b>",
                f"• {payload.get('inn1_team', 'Team 1')} "
                f"{payload.get('inn1_runs', 0)}/{payload.get('inn1_wickets', 0)}"
                f" ({payload.get('inn1_overs', '0')})",
                f"• {payload.get('inn2_team', 'Team 2')} "
                f"{payload.get('inn2_runs', 0)}/{payload.get('inn2_wickets', 0)}"
                f" ({payload.get('inn2_overs', '0')})",
                f"• {payload['winner_name']} won "
                f"{payload.get('win_margin_text', '')}".rstrip(),
            ]
            if payload.get("potm_name"):
                lines.append(f"• POTM {payload['potm_name']}"
                             f" — {payload.get('potm_stats', '')}".rstrip(" —"))
            blocks.append("\n".join(lines))
    if not blocks:
        return None
    return "\n\n".join(blocks)


async def send_text_fallback(bot, chat_id, cards):
    """Post :func:`text_scorecard` when no image could be delivered."""
    text = text_scorecard(cards)
    if not text:
        return False
    try:
        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ <i>Scorecard images couldn't be generated — here are the "
                 "numbers.</i>\n\n" + text,
            parse_mode="HTML")
        return True
    except Exception:
        logger.exception("scorecard text fallback failed for chat %s", chat_id)
        return False


async def record_and_send(bot, chat_id, match_id, cards,
                          *, reply_to_message_id=None):
    """The innings-end path: persist first, then render and send each card.

    Persisting before rendering is what makes the cards recoverable — if the
    render or the send dies, ``/lastscorecard`` can still redraw them from the
    row written here. When no image at all reaches the chat, the text fallback
    takes over so the group is never left with nothing.

    Returns the number of images delivered.
    """
    cards = sorted(list(cards or []), key=_sort_key)
    if not cards:
        return 0
    record_cards(match_id, chat_id, cards)

    # ``record_cards`` stamps match_id/chat_id onto the stored rows; the
    # in-memory copies need them too so deliver_card can cache file_ids back.
    for card in cards:
        card.setdefault("match_id", match_id)
        card.setdefault("chat_id", chat_id)
        card.setdefault("innings", WHOLE_MATCH)

    sent = await deliver_cards(bot, chat_id, cards,
                               reply_to_message_id=reply_to_message_id)
    if sent == 0:
        await send_text_fallback(bot, chat_id, cards)
    elif sent < len(cards):
        logger.error("match %s: only %s of %s scorecard images reached chat %s",
                     match_id, sent, len(cards), chat_id)
    return sent
