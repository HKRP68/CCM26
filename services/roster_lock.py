"""Roster/XI freeze while a match is on.

Once a game is running, the team you walked out with is the team you play with.
Swapping the Playing XI, reordering the batting line-up, changing the captain or
bolting a new trait onto a player mid-match is not a tactic — it is editing the
squad sheet after the toss, and it makes every mid-match card in the chat lie
about who is actually out there.

One helper, used by every command that edits a roster or a player's traits:

    blocked = match_lock_message(session, user.id, "change your Playing XI")
    if blocked:
        await msg.reply_text(blocked, parse_mode="HTML")
        return

The lock uses the same "are you in a match?" lookup as the guards that stop a
player starting two games at once (``handlers.match._active_match_for_user``), so
"busy" means exactly the same thing everywhere — including the pre-play
invitation states, which is when a late XI edit would do the most damage. Stale
invitations are expired by that lookup itself, so an abandoned challenge can
never freeze a roster indefinitely.

The lookup failing open is deliberate: an error reading the match table must not
lock a player out of their own squad screen.
"""

import logging

logger = logging.getLogger(__name__)


def active_match_for(session, user_id):
    """The unfinished match this user is part of, or ``None``."""
    if not user_id:
        return None
    try:
        from handlers.match import _active_match_for_user
        return _active_match_for_user(session, user_id)
    except Exception:
        logger.exception("roster lock: active-match lookup failed for user %s", user_id)
        return None


def match_lock_message(session, user_id, action="change your team", *, reason=None):
    """A ready-to-send explanation when ``action`` is blocked, else ``None``.

    ``action`` completes the sentence "You can't <action> during a match" — pass
    something concrete like ``"change your Playing XI"``. ``reason`` overrides
    the one-line justification under it (the default talks about the XI, which
    doesn't fit a shop).
    """
    match = active_match_for(session, user_id)
    if not match:
        return None
    return lock_message(match, action, reason=reason)


# The squad-market wording: nothing to do with the XI, everything to do with the
# roster changing shape while a game is being played on it.
MARKET_REASON = ("the squad you started the match with is the squad you finish "
                 "it with")


def match_lock_alert(session, user_id, action="change your team"):
    """Short plain-text version for a callback-answer popup, or ``None``.

    Telegram alerts don't render HTML and are length-capped, so this is a single
    line rather than the full explanation ``match_lock_message`` gives.
    """
    if not active_match_for(session, user_id):
        return None
    return f"🔒 You can't {action} during a match. Finish the game first."


def lock_message(match, action="change your team", *, reason=None):
    """The player-facing message for a locked action."""
    status = (getattr(match, "status", "") or "").lower()
    if status in ("pending", "accepted", "toss", "selecting"):
        what = "You have a match starting"
    else:
        what = "You're in the middle of a match"
    why = reason or "the XI that took the field stays the XI"
    return (
        f"🔒 <b>{what}.</b>\n"
        f"You can't {action} until it's finished — {why}.\n\n"
        f"<i>Finish the game (or let it expire), then try again.</i>"
    )
