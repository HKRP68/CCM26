"""Auction focus mode — while a lot is on the block, the room runs one feature.

An auction group is the loudest chat the bot has: a thirty-second clock, a
pinned board being edited every tick, forty bids inside one lot. Anything else
a player types into it lands on top of that — a ``/claim`` card, a WordChase
round, somebody's ``/pxi`` — and it pushes the board off the screen at exactly
the moment the room needs to read it. Worse, a bid that scrolls past unseen is
how a price gets disputed, which is the same reason ``AuctionSeason.chat_id``
exists at all.

So while an auction bound to a group is **live or paused**, that group answers
auction commands and nothing else. Everything else is refused with one line
naming where it still works: a DM with the bot, where the whole rest of the
feature set is untouched.

Four deliberate limits, because a gate that swallows more than it was asked to
is worse than no gate:

* **The lock is per group, never per person.** It is bound to the auction's own
  chat (``season.chat_id``), so a franchise owner's DMs — and every other group
  the bot is in — carry on exactly as before. Nobody loses the bot because an
  auction is running somewhere they are not.
* **Only commands and buttons.** Plain chatter is never touched: a room that
  cannot talk during its own auction is not a room, and the shouting is half
  of what an auction is.
* **Only while it is running.** ``setup`` is exempt on purpose, the same call
  ``STATUS_ACTIVE`` makes elsewhere: retention, the expansion picks and the
  pool all happen before a lot ever opens, and locking a group down for the
  days or weeks an auction sits in setup would be a lock nobody asked for.
* **Admins are never locked out.** Bot admins and auction admins run the room,
  and the thing they need in the middle of a disputed lot is as likely to be
  ``/grant`` or ``/userinfo`` as ``/aundobid``.

And it is a switch: ``focus_mode`` on the season, ON by default, flipped with
``/afocus off`` in the group or the checkbox on the auction's setup page.

Nothing here touches the database except :func:`locked_season_for_chat` — the
one lookup the middleware has to make, cached per chat for a few seconds — and
:func:`is_bypassed`, which is asked only about an update the gate is otherwise
about to refuse. Both fail OPEN: a broken gate must never cost a group its
commands. The rules themselves are pure, so ``tests/test_auction_focus.py`` can
pin them against plain objects.
"""

from __future__ import annotations

import logging
import time

from services.command_token import command_name_from_update

logger = logging.getLogger(__name__)

GROUP_CHAT_TYPES = ("group", "supergroup")

# Every command handlers/auction.py registers, aliases included. The gate sees
# the raw command somebody typed, not the handler it would have reached, so an
# alias missing here would be an auction command the auction locks out.
# ``tests/test_auction_focus.py`` pins this set against bot.py's own auction
# registrations in BOTH directions: a name here that bot.py does not register,
# and — the one that would actually bite — an auction command bot.py registers
# that is not here.
AUCTION_COMMANDS = frozenset({
    # Bidding, and answering a Right To Match.
    "bid", "bd", "artm", "rtm",
    # The board and the reads.
    "aboard", "auctionboard", "apurse", "apurses", "ainfo", "amenu",
    "arules", "asettings", "asets", "anextset", "anextplayer", "anextplayers",
    "asquad", "amysquad", "asoldlist", "aunsoldlist",
    # The admin surface. Registered unconditionally with the gate inside the
    # handler, so a non-admin typing one gets told so rather than silence.
    "adminhelp", "ahelp", "auction",
    "anew", "abind", "astart", "apause", "aresume", "aunpause", "anext",
    "aextend", "asold", "aunsold", "aundobid", "awithdraw", "atimer",
    "asnipe", "afocus", "afocusmode", "agrant", "aco", "apublish", "acancel",
    "aretain", "aretainforce", "aoffers", "aretcancel", "aunretain",
    "arelease", "aretlock", "aretention",
    "artmset", "artmrules", "artmcards", "artmforce", "artmundo",
    "aaccel", "arelistall", "aclone", "anextseason",
    "apick", "apicks", "apickboard", "apickset", "apickrules",
    "apickskip", "apickpass", "apickundo",
    "apool", "asetorder", "aaccelmode", "acall", "acallteams",
    "aremoveteam", "aadminadd", "aadminremove", "aadmins",
})

# Commands that work in the auction group whatever the auction is doing. The
# test of membership here is "somebody could need this *because* of the
# auction": the bot has stopped answering and they cannot tell whether it is
# down or locked (``/ping``), they cannot find the auction's own commands
# (``/help``), or the lock itself is the thing that is wrong and they need to
# reach a human (``/feedback``). Everything else waits.
ALWAYS_ALLOWED = frozenset({
    "start", "s",
    "help", "howto", "guide",
    "feedback", "fb",
    "botstatus", "bstatus", "ping",
})

# Every auction callback shares one prefix (``au_bid_``, ``au_rtm_``,
# ``au_info_``, ``au_sets_``, ``au_ret_``), so one entry covers the board, the
# info menu, the sets pages, the RTM answers and the retention offers — and
# covers a prefix added later for free. ``noop`` is the inert filler button.
AUCTION_CALLBACK_PREFIXES = ("au_", "noop")


# ── The switch ──────────────────────────────────────────────────────

def focus_mode_on(season) -> bool:
    """True when this auction is set to lock its group while it runs.

    Stored as 1/0 rather than a boolean for the reason
    ``database._migrate_add_columns`` gives — a non-nullable boolean added to a
    table with rows reads back NULL-as-falsy — and read through ``int`` so a
    season written before the column existed reads as ON, which is the default
    every new season gets.
    """
    if season is None:
        return False
    raw = getattr(season, "focus_mode", 1)
    if raw is None:
        return True
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return bool(raw)


def locks_the_room(season) -> bool:
    """True when this auction should be refusing everything else right now.

    Live or paused only. A pause is still an auction mid-flight — the board is
    pinned, the lot is unresolved, and the room is waiting on the admin who
    paused it — so it keeps the lock; setup and the two finished states do not.
    """
    if season is None or not focus_mode_on(season):
        return False
    from services.auction_service import STATUS_LIVE, STATUS_PAUSED
    return getattr(season, "status", None) in (STATUS_LIVE, STATUS_PAUSED)


# ── What the gate lets through ──────────────────────────────────────

def is_auction_command(name: str) -> bool:
    return (name or "").strip().casefold() in AUCTION_COMMANDS


def is_always_allowed(name: str) -> bool:
    return (name or "").strip().casefold() in ALWAYS_ALLOWED


def is_auction_callback(data: str) -> bool:
    data = data or ""
    return any(data.startswith(prefix) for prefix in AUCTION_CALLBACK_PREFIXES)


def is_group_chat(update) -> bool:
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "type", None) in GROUP_CHAT_TYPES


def is_bypassed(telegram_id) -> bool:
    """True for a bot admin or an auction admin, who are never locked out.

    Asked only about an update the gate is otherwise about to refuse, so the
    auction-admin lookup — a query — costs nothing on the ordinary path, and a
    bot admin is answered from the config list without one at all.

    A broken lookup answers "bypassed", which is this module's fail-open rule
    read from the other end: not knowing whether somebody is an admin must cost
    them a command they had before this existed, rather than cost the room
    nothing and them everything.
    """
    if telegram_id is None:
        return False
    try:
        from services.admin_ids import is_admin
        if is_admin(int(telegram_id)):
            return True
        from database import get_session
        from services.auction_service import is_auction_admin
        session = get_session()
        try:
            return bool(is_auction_admin(session, int(telegram_id)))
        finally:
            session.close()
    except Exception:
        logger.exception("Auction focus admin lookup failed — failing open")
        return True


def should_block_update(update, *, locked, bot_username=None) -> bool:
    """Decide whether focus mode blocks this update. ``locked`` is the state.

    ``locked`` is what :func:`locked_season_for_chat` found for this chat —
    truthy when an auction is bound here, running, and has focus mode on. The
    caller supplies it so these rules stay pure and free of database work.

    Rules, in order:
      - not locked, or not a group chat → never block
      - a bot admin or auction admin typed it → never block
      - a button: auction buttons through, everything else blocked
      - not a message at all (chat-member updates, edits) → not the gate's
        business
      - no command in the message (plain chatter, a photo, a service message)
        → never block; the room has to be able to talk
      - an auction command, or a doorway command → allowed
      - any other command → blocked
    """
    if not locked or not is_group_chat(update):
        return False

    user = getattr(update, "effective_user", None)
    user_id = getattr(user, "id", None)

    callback = getattr(update, "callback_query", None)
    if callback is not None:
        if is_auction_callback(getattr(callback, "data", "") or ""):
            return False
        return not is_bypassed(user_id)

    message = getattr(update, "message", None)
    if message is None:
        return False

    name = command_name_from_update(update, bot_username)
    if not name or is_auction_command(name) or is_always_allowed(name):
        return False
    return not is_bypassed(user_id)


def is_command_or_button(update, bot_username=None) -> bool:
    """True for a slash command aimed at this bot, or a button press.

    The gate's own pre-filter, and the reason a locked group costs nothing on
    ordinary traffic: everything else — chatter, photos, joins, edits — is
    answered here without a database lookup, because the lock does not apply
    to it.
    """
    if getattr(update, "callback_query", None) is not None:
        return True
    return bool(command_name_from_update(update, bot_username))


def should_reply(update, bot_username=None) -> bool:
    """True when a blocked update deserves a line back rather than silence.

    Everything this gate blocks is a command or a button — somebody's
    deliberate action, waiting for an answer — so it always answers. Plain
    chatter never reaches here, which is what keeps a locked group from
    replying to every message in it.
    """
    return is_command_or_button(update, bot_username)


# ── The one database lookup ─────────────────────────────────────────
# Cached per chat for a few seconds. The middleware asks on every command and
# every button press in every group the bot is in, and the answer changes only
# when an admin starts, pauses, finishes or unlocks an auction — so a short TTL
# costs one query per chat per window and makes /astart take effect within it.

_CACHE_TTL_SECONDS = 10.0
_cache: dict[int, tuple[float, str | None]] = {}


def invalidate(chat_id=None) -> None:
    """Drop the cached lock state — the whole cache, or one chat's entry.

    Called by ``auction_service`` whenever an auction's status or focus switch
    moves, so ``/astart`` and ``/afocus off`` land immediately instead of at
    the end of a TTL. Cheap enough to call on any auction write.
    """
    if chat_id is None:
        _cache.clear()
        return
    try:
        _cache.pop(int(chat_id), None)
    except (TypeError, ValueError):
        pass


def locked_season_for_chat(chat_id, *, now=None):
    """The name of the auction locking this chat, or None. Blocking; cached.

    Returns the name rather than the row because the caller only ever prints
    it, and a detached ORM object outliving its session is how a middleware
    ends up raising in a place with nothing to catch it.
    """
    if chat_id is None:
        return None
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None

    now = now if now is not None else time.monotonic()
    cached = _cache.get(chat_id)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    name = None
    try:
        from database import get_session
        from services import auction_service as A
        session = get_session()
        try:
            season = A.season_for_chat(session, chat_id)
            if locks_the_room(season):
                name = season.name
        finally:
            session.close()
    except Exception:
        # A broken lookup must never take the group's commands down with it:
        # fail OPEN, which is the state the group was in before this existed.
        logger.exception("Auction focus lookup failed (non-fatal)")
        return None

    _cache[chat_id] = (now, name)
    if len(_cache) > 5000:
        for key in [k for k, entry in _cache.items()
                    if now - entry[0] > _CACHE_TTL_SECONDS]:
            _cache.pop(key, None)
    return name


# ── Messaging ───────────────────────────────────────────────────────

def locked_message(season_name=None) -> str:
    """The one line a blocked command gets back, in Telegram HTML."""
    import html as _html
    title = (f"<b>{_html.escape(str(season_name))}</b> is on the block"
             if season_name else "An auction is on the block")
    return (f"🔨 {title}.\n\n"
            f"Only auction commands work in this group while it runs — "
            f"<code>/bid</code>, <code>/ainfo</code>, <code>/aboard</code>, "
            f"<code>/apurse</code> and the rest of the a-commands.\n\n"
            f"<i>Everything else still works in a DM with me.</i>")


def locked_alert(season_name=None) -> str:
    """Short plain text for a callback answer — Telegram alerts render no HTML."""
    title = f"{season_name} is on the block" if season_name else "Auction in progress"
    return (f"🔨 {title} — only auction buttons work in this group right now. "
            f"Everything else works in a DM.")
