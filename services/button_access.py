"""Owner access guard for Telegram inline callback buttons.

Messages with callback buttons that are sent while handling a user update are
registered against the originating Telegram user id.  A pre-callback middleware
uses that registration to stop other users from driving someone else's personal
UI.  URL/WebApp-only keyboards are ignored, and explicitly shared callback
prefixes (join/spectate/accept-style match buttons) remain open to everyone.

Commands whose buttons must stay personal even across a restart can opt into a
stronger, stateless lock instead: see ``OWNER_RULES`` and :func:`tag_owner`,
which write the owner's id into the callback data itself and let the command
say something more useful than "this button is not for you" when somebody else
presses.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import time
from collections.abc import Iterable
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

BLOCKED_BUTTON_MESSAGE = "This button is not for you. Please use your own command."

_current_button_owner: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "current_button_owner", default=None
)

# Callback prefixes that are intentionally shared/global.  These include public
# lobby/join buttons, challenge/trade/tour invite responses intended for a
# second participant, and match-control buttons where the match state itself
# validates whose turn/action it is.
SHARED_CALLBACK_PREFIXES: tuple[str, ...] = (
    # (Tournament Draft's dr_view_ / dr_pick_ / dr_srch_ buttons used to be
    # listed here. They are now owner-locked to whoever ran the command that
    # posted them — see OWNER_RULES below — because a draft group is a busy
    # room and a board somebody else re-filters under you is worse than
    # typing /dboard again.)
    # The Franchise Auction's quick-bid buttons ride on the ONE pinned board
    # every franchise in the room is watching — a board only the person who
    # started the auction may press is not an auction. handlers/auction.py
    # authorises each press against the franchise the presser actually owns,
    # and the exact price is baked into the callback data, so a button pressed
    # after the price has moved is refused with "the price has moved" rather
    # than quietly bidding a number nobody meant. Same call as the dt_ trade
    # buttons below, for the same reason.
    "au_bid_",
    # The Right To Match prompt rides on the same pinned board. Only the
    # holding franchise can actually answer, and handlers/auction.py checks
    # that on every press — but the button has to be reachable by them, and
    # the board belongs to nobody.
    "au_rtm_",
    # The /ainfo view buttons (Sets, Next Set, My Squad, …) are read-only and
    # answer whoever presses them — "My Squad" resolves the presser's own
    # franchise — so the card belongs to the whole room.
    "au_info_",
    # The 🗂 Sets card's pages and its per-set buttons. Read-only like the
    # /ainfo views, and posted into a room where every franchise wants to look
    # at the sets — a card only the admin who typed /asets could page is a card
    # nobody else can read.
    "au_sets_",
    # A retention offer is posted by the ADMIN who ran /aretain, but it is the
    # franchise that has to answer it. handlers/auction.py lets only that
    # franchise's owner and co-owners accept or decline, on every press.
    "au_ret_",
    "cric_join",
    "cric_join_",
    "cric_join:",
    "cric_cancel_lobby",
    "cric_cancel_lobby_",
    "cric_decision:",
    # The /wpm Trait Vote: posted while handling the GUEST's join, but BOTH
    # captains have to be able to answer it. cric_traits_callback checks each
    # click against the lobby's host/guest telegram ids.
    "cric_traits:",
    "match_accept_",
    "match_deny_",
    "pm_accept_",
    "pm_deny_",
    # /playmatch (/pm) in-chat flow: invite response + the whole two-player
    # setup/gameplay handshake. Each handler validates the clicker by
    # telegram_id / live match state, so the same message is intentionally
    # driven by both players in turn (the owner guard must not block them).
    "matchacc_",
    "matchdeny_",
    "oversset_",
    "overscustom_",
    "toss_",
    "op1_",
    "op2_",
    "selbowl_",
    "bvar_",
    "blen_",
    "bspin_",
    "bshot_",
    "nbowl_",
    "newbat_",
    # /cipl (Challenge League) over-by-over flow: coin call, toss decision, and
    # the per-over bowler/approach buttons. cipl_play.py validates each click
    # against bowl_user_tg / bat_user_tg, so these are shared prompts too.
    "cipl_coin_",
    "cipl_toss_",
    "cipl_bowler_",
    "cipl_bowlapp_",
    "cipl_batapp_",
    # The Impact Player entry button rides on the over summary / innings-break
    # card, which is sent while handling ONE captain's approach tap — so the
    # registry would own it to that captain and lock the other one out of their
    # own substitution. Both must reach it; cipl_play._impact_guard checks the
    # clicker against bat_user_tg / bowl_user_tg and hands each captain their
    # own options. The picker it opens is personal, and those buttons carry an
    # owner tag instead (see OWNER_RULES below).
    "cipl_imp_",
    # Super Over (tied /cipl, /c[league], /letsplay): the player-selection and
    # ball-by-ball prompts are shared between the two captains — the bowling side
    # picks delivery/length while the batting side picks the shot, all on the same
    # message sent mid-handling of one captain's update. handlers/super_over.py
    # validates every click by telegram_id, so (like the cipl_* prompts above)
    # these must reach their handlers instead of being owner-locked to whoever
    # triggered the send.
    "so_bat_",
    "so_batok_",
    "so_bowl_",
    "so_bowlok_",
    "so_dv_",
    "so_ln_",
    "so_sh_",
    # (/wsp's join/decision/cancel prefixes lived here; the mode is turned off
    # in bot.py, so nothing handles those callbacks any more.)
    "cm_accept_",
    "cm_deny_",
    "cm_toss_",
    "cm_pick_",
    # /letsplay own-roster flow: the invite (sent while handling the HOST's
    # command, so it would otherwise be owned by the host) plus pitch, start
    # toss, coin and toss prompts are all driven by both players in turn.
    # letsplay.py validates every click by telegram_id / draft state, so these
    # must reach their handlers like the /cipl and /cm prompts above.
    "lp_accept_",
    "lp_deny_",
    "lp_pitch_",
    "lp_traits_",
    "lp_starttoss_",
    "lp_coin_",
    "lp_toss_",
    # Challenge League team and Playing XI buttons are shared prompts: the
    # same message is used by both host and guest, and challenge.py validates
    # which side may press each button based on the draft state.
    "cl_team_",
    "cl_xi_",
    "cl_pick_",
    "cl_confirm_",
    "cl_start_",
    # Pitch selection + the guest's Deny Match button live on the same prompt
    # (sent while handling the guest's team pick, so it would otherwise be owned
    # by the guest). challenge.py validates each: only the host picks the pitch,
    # only the guest may deny — so both sides must reach those handlers.
    "cl_pitch_",
    "cl_denymatch_",
    # The XI picker's own controls. These are sent while handling that captain's
    # own cl_xi_ tap, so the registry names them — until a restart empties it,
    # after which they fall through to "unregistered, anyone may press". Each
    # already checks the clicker against the side's telegram id, which is the
    # stronger, restart-proof check, so state the rule here rather than leaning
    # on process memory for it.
    "cl_useprev_",
    "cl_clear_",
    "cl_edit_",
    # /cdraft (Challenge Draft): the lobby's Join and Cancel, and the per-slot
    # pick. Every one of them is pressed by somebody OTHER than the user whose
    # update posted the card — the lobby is sent while handling the host's
    # /cdraft but it is the guest who joins, and each slot card is sent while
    # handling the previous picker's tap while the snake order hands the next
    # pick to the other captain. A slot posted by the auto-pick job has no
    # originating user at all. handlers/cdraft.py authorises every press against
    # the draft state by telegram id, which is what actually decides here.
    "cdj_",
    "cdc_",
    "cdp_",
    "pbo_accept_",
    "pbo_decline_",
    "pboacc_",
    "pbodec_",
    "bopick_",
    "taccept_",
    "treject_",
    "tac_",
    "tdc_",
    # /cltour (Challenge League Tour) setup + invite: the league/team/count wizard
    # and the Accept/Decline invite all live on one message first sent while
    # handling the HOST's command, so it would otherwise be owner-locked to the
    # host. The guest's team pick (cltset_gt_) and the invite responses
    # (clt_acc_/clt_dec_) are driven by the *guest*, so they must be shared.
    # handlers/cl_tour.py validates every click by host_tg/guest_tg/user2_id.
    "cltset_gt_",
    "clt_acc_",
    "clt_dec_",
    # /trade two-player flow: player-selection (t1p_/t2p_), confirm (tcfrm_) and
    # cancel (tcancel_) buttons live on one message that is edited between user1's
    # and user2's turns. handlers/trade.py validates every click by telegram_id,
    # so both participants must reach those handlers instead of being owner-locked
    # to user1 (who triggered the original send).
    "t1p_",
    "t2p_",
    "tcfrm_",
    "tcancel_",
    # /dtrade — the post-draft franchise trade, and shared for the same reason
    # as /trade above: one message walks from "the opening team ticks its
    # players" to "the other team ticks theirs" to "both owners confirm", but it
    # was first sent while handling the opening owner's command. The draft's
    # other buttons (dr_*) are owner-locked precisely because nobody else should
    # drive them; these are the opposite — a trade the second franchise cannot
    # touch is not a trade. handlers/draft_trade.py authorises every press
    # against the team the presser owns AND the step the offer is on, which is
    # the stronger check anyway: it survives a restart, and it lets a co-owner
    # take over mid-offer.
    "dt_",
    # /tradetrait — the trait-for-trait twin of /trade above, and shared for the
    # same reason: one message is edited from "user1 picks" (tt1_) to "user2
    # picks" (tt2_) to "both confirm" (ttcfrm_), but it was first sent while
    # handling user1's command. Without these the second captain's tap was
    # owner-blocked with "This button is not for you" the moment it became their
    # turn, so no trait trade could ever be completed. handlers/tradetrait.py
    # validates every click by telegram_id.
    "tt1_",
    "tt2_",
    "ttcfrm_",
    "ttcancel_",
    "us_join",
    "us_ans_",
    "mh_join_",
    "mh_start_",
    "mh_theme_",
    "mh_vote",
    "ct_join_",
    "ct_start_",
    "ct_theme_",
    "ct_vote",
    "bm_accept_",
    "bm_decline_",
    "bm_answer_",
    "bm_steal_",
    # Giveaway "Participate" button: a public join button broadcast to every
    # chat the bot is in. Anyone in the chat may tap it (the callback validates
    # Official GC membership + one-entry itself), so it must never be owner-locked
    # to whoever the sender happened to be.
    "gwjoin_",
    # The team-logo review keyboard is sent to the bot admins while handling the
    # *uploader's* update, so the owner lock would pin it to the uploader and
    # answer every admin with "this button is not for you". handlers/team_logo.py
    # authorises each press with is_admin() instead, and the owner's own
    # withdraw button checks the request's telegram_id.
    "tlogo:",
)

# ════════════════════════════════════════════════════════════════════
# Owner rules — locks that survive a restart
# ════════════════════════════════════════════════════════════════════
#
# The registry further down is process-local: a restart empties it, and every
# message sent before the restart then falls through to "unregistered, so let
# everybody press it".  That is a fine trade for a roster page, but not for a
# command whose whole point is that the buttons are yours — a draft board hours
# into an evening is exactly the message people are still pressing.
#
# A prefix listed here carries its owner's Telegram id *in the callback data*
# (``<prefix>u<tg id><rest>``, written by :func:`tag_owner`), so the lock is
# stateless: the button itself says who it belongs to, and a restart, a second
# process or a pruned cache changes nothing.  Each rule also carries the line a
# non-owner is shown, because "this button is not for you" tells nobody what to
# do instead.
#
# Untagged data under the same prefix (buttons sent before a deploy) reads back
# as "no owner" and falls through to the registry, so nothing is bricked.

OWNER_TAG = "u"
_MAX_OWNER_DIGITS = 16

OWNER_RULES: dict[str, str] = {
    # Tournament Draft. The pool browser re-filters *in place*, so a second
    # pair of hands on it genuinely fights the first — two owners three seconds
    # before a pick, one flipping to Gold while the other is mid-page. The
    # board's tabs post a fresh message instead, so a stranger driving yours is
    # noise in the one chat that has to stay readable. Everything they reach is
    # public draft state and the commands are free, so being told to run your
    # own costs nothing.
    "dr_view_": ("🎯 That board belongs to whoever sent /dboard. "
                 "Send /dboard for your own copy."),
    "dr_srch_": ("🔎 That pool browser belongs to whoever sent /dsearch. "
                 "Send /dsearch for your own — the filters are per-person."),
    # The disambiguation buttons under /pick: a pick is irreversible and needs
    # an admin to undo, so the one pair of hands that may finish it is the pair
    # that typed the name. handlers/draft.py re-checks the clicker against the
    # team on the clock as well.
    "dr_pick_": ("⛔ Those buttons belong to whoever typed /pick. "
                 "Type /pick <player> yourself."),
    # The Impact Player picker. Unlike the shared 🔄 button that opens it, each
    # captain's picker lists their OWN squad and spends their ONE irreversible
    # substitution, so the other captain must not be able to drive it — not even
    # after a restart has emptied the registry, which is why these are owner-
    # tagged rather than left to it. Both captains get their own via 🔄/-impact.
    "cipl_impo_": ("⛔ That Impact Player picker belongs to the other captain. "
                   "Tap 🔄 Impact Player, or send /impact, for your own."),
    "cipl_impi_": ("⛔ That Impact Player picker belongs to the other captain. "
                   "Tap 🔄 Impact Player, or send /impact, for your own."),
    "cipl_impp_": ("⛔ That Impact Player picker belongs to the other captain. "
                   "Tap 🔄 Impact Player, or send /impact, for your own."),
    "cipl_impx_": ("⛔ That Impact Player picker belongs to the other captain. "
                   "Tap 🔄 Impact Player, or send /impact, for your own."),
}


def tag_owner(prefix: str, owner_user_id: Optional[int]) -> str:
    """``("dr_view_", 111)`` → ``"dr_view_u111"``; untagged when owner is None.

    Anything that is not a positive integer comes back as the bare prefix: a
    minus sign would not read back as digits, and half-written data is worse
    than no tag at all (no tag simply falls through to the registry).
    """
    if owner_user_id is None:
        return prefix
    try:
        owner = int(owner_user_id)
    except (TypeError, ValueError):
        return prefix
    if owner <= 0 or len(str(owner)) > _MAX_OWNER_DIGITS:
        return prefix
    return f"{prefix}{OWNER_TAG}{owner}"


def split_owner(prefix: str, callback_data: Any,
                separator: str = "") -> tuple[Optional[int], str]:
    """Split tagged callback data into ``(owner id, the fields after the tag)``.

    ``separator`` is the single character the command writes between the owner
    tag and its own fields; it is consumed here when — and only when — a tag was
    actually found.  That matters for formats whose first field can be empty
    (``dr_srch_u7~~~a~~0~``): untagged data from before a deploy comes back
    byte-for-byte unchanged, so the caller parses both with one code path.
    """
    data = callback_data if isinstance(callback_data, str) else ""
    if not data.startswith(prefix):
        return None, ""
    rest = data[len(prefix):]
    if not rest.startswith(OWNER_TAG):
        return None, rest
    digits = ""
    for char in rest[len(OWNER_TAG):]:
        if not char.isdigit() or len(digits) >= _MAX_OWNER_DIGITS:
            break
        digits += char
    if not digits:
        return None, rest
    rest = rest[len(OWNER_TAG) + len(digits):]
    if separator and rest.startswith(separator):
        rest = rest[len(separator):]
    return int(digits), rest


def _matching_owner_rule(callback_data: Any) -> Optional[str]:
    """The longest OWNER_RULES prefix this callback data starts with."""
    if not isinstance(callback_data, str) or not callback_data:
        return None
    matches = [prefix for prefix in OWNER_RULES if callback_data.startswith(prefix)]
    return max(matches, key=len) if matches else None


def owner_from_callback_data(callback_data: Any) -> Optional[int]:
    """The owner a self-describing button names, or None if it names nobody."""
    prefix = _matching_owner_rule(callback_data)
    if prefix is None:
        return None
    return split_owner(prefix, callback_data)[0]


def blocked_message_for(callback_data: Any) -> str:
    """What to show the user who just pressed somebody else's button.

    Plain text, not HTML: Telegram renders a callback answer verbatim, and the
    200-character cap is enforced here so a long rule can never turn a refusal
    into a Bad Request that leaves the button looking dead instead.
    """
    prefix = _matching_owner_rule(callback_data)
    message = BLOCKED_BUTTON_MESSAGE if prefix is None else OWNER_RULES[prefix]
    return message[:200]


_SEND_METHODS = (
    "send_message",
    "send_photo",
    "send_animation",
    "send_document",
    "send_dice",
    "send_video",
    "send_audio",
    "send_sticker",
    "send_location",
    "send_venue",
    "send_poll",
)

# Process-local cache.  Enough for callback access because callback presses are
# delivered to the same running bot process that sent the message in this app.
# Entries are pruned opportunistically.
_OWNER_BY_MESSAGE: dict[tuple[int, int], tuple[int, float]] = {}
_OWNER_TTL_SECONDS = 60 * 60 * 24
_INSTALLED = False


def bind_button_owner_context(update: Any) -> None:
    """Bind outbound callback-button messages in this update to its user."""
    user = getattr(update, "effective_user", None)
    _current_button_owner.set(getattr(user, "id", None))


def clear_button_owner_context() -> None:
    _current_button_owner.set(None)


def is_shared_callback_data(callback_data: Any) -> bool:
    """Return True for callback data that is intended for shared/global use."""
    if not isinstance(callback_data, str):
        return False
    return callback_data.startswith(SHARED_CALLBACK_PREFIXES)


def _iter_callback_data(reply_markup: Any) -> Iterable[str]:
    keyboard = getattr(reply_markup, "inline_keyboard", None)
    if not keyboard:
        return []
    values: list[str] = []
    for row in keyboard:
        for button in row:
            data = getattr(button, "callback_data", None)
            if isinstance(data, str) and data:
                values.append(data)
    return values


def _should_register_markup(reply_markup: Any) -> bool:
    callback_values = list(_iter_callback_data(reply_markup))
    if not callback_values:
        return False
    return any(not is_shared_callback_data(data) for data in callback_values)


def _extract_chat_message_id(message: Any) -> tuple[Optional[int], Optional[int]]:
    chat_id = getattr(message, "chat_id", None)
    if chat_id is None:
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    try:
        chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        chat_id = None
    try:
        message_id = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        message_id = None
    return chat_id, message_id


def register_button_owner(chat_id: int, message_id: int, owner_user_id: int) -> None:
    _OWNER_BY_MESSAGE[(int(chat_id), int(message_id))] = (int(owner_user_id), time.monotonic())
    _prune_owner_cache()


def _prune_owner_cache() -> None:
    if len(_OWNER_BY_MESSAGE) < 4096:
        return
    cutoff = time.monotonic() - _OWNER_TTL_SECONDS
    stale = [key for key, (_owner, created_at) in _OWNER_BY_MESSAGE.items() if created_at < cutoff]
    for key in stale:
        _OWNER_BY_MESSAGE.pop(key, None)


def get_registered_owner(chat_id: int, message_id: int) -> Optional[int]:
    entry = _OWNER_BY_MESSAGE.get((int(chat_id), int(message_id)))
    if not entry:
        return None
    owner, created_at = entry
    if time.monotonic() - created_at > _OWNER_TTL_SECONDS:
        _OWNER_BY_MESSAGE.pop((int(chat_id), int(message_id)), None)
        return None
    return owner


def check_callback_owner(update: Any) -> bool:
    """Return True if the callback may continue for the clicking user."""
    query = getattr(update, "callback_query", None)
    if query is None:
        return True
    if is_shared_callback_data(getattr(query, "data", None)):
        return True

    clicking_user_id = getattr(getattr(query, "from_user", None), "id", None)

    # A self-describing button (OWNER_RULES) is authoritative: it names its
    # owner, so the answer does not depend on this process having been the one
    # that sent it.
    tagged_owner = owner_from_callback_data(getattr(query, "data", None))
    if tagged_owner is not None:
        return clicking_user_id == tagged_owner

    message = getattr(query, "message", None)
    chat_id, message_id = _extract_chat_message_id(message)
    if chat_id is None or message_id is None:
        return True

    owner = get_registered_owner(chat_id, message_id)
    if owner is None:
        # Legacy/unregistered messages stay usable so existing deployed buttons
        # are not bricked after a restart.
        return True

    return clicking_user_id == owner


async def button_access_guard(update: Any, context: Any) -> None:
    """PTB middleware that blocks non-owners before callback handlers run."""
    if check_callback_owner(update):
        return
    query = update.callback_query
    try:
        await query.answer(blocked_message_for(getattr(query, "data", None)),
                           show_alert=True)
    except Exception:
        logger.exception("Failed to answer blocked callback query")
    from telegram.ext import ApplicationHandlerStop

    raise ApplicationHandlerStop


def _wrap_send_method(method: Callable[..., Any]) -> Callable[..., Any]:
    try:
        parameters = set(inspect.signature(method).parameters)
    except (TypeError, ValueError):
        parameters = {"reply_markup"}

    @functools.wraps(method)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        reply_markup = kwargs.get("reply_markup") if "reply_markup" in parameters else None
        owner_user_id = _current_button_owner.get()
        sent_message = await method(self, *args, **kwargs)
        if owner_user_id is None or not _should_register_markup(reply_markup):
            return sent_message

        chat_id, message_id = _extract_chat_message_id(sent_message)
        if chat_id is not None and message_id is not None:
            register_button_owner(chat_id, message_id, owner_user_id)
        return sent_message

    return wrapped


def install_button_access_defaults() -> None:
    """Patch Telegram Bot send methods so owner-specific keyboards are tracked."""
    global _INSTALLED
    if _INSTALLED:
        return
    try:
        from telegram import Bot
        from telegram.ext import ExtBot
    except Exception:  # pragma: no cover - defensive startup guard
        logger.exception("Could not import Telegram Bot classes for button access defaults")
        return

    for bot_cls in {Bot, ExtBot}:
        for method_name in _SEND_METHODS:
            method = getattr(bot_cls, method_name, None)
            if method is None or getattr(method, "_ccm_button_access_defaults", False):
                continue
            wrapped = _wrap_send_method(method)
            setattr(wrapped, "_ccm_button_access_defaults", True)
            setattr(bot_cls, method_name, wrapped)

    _INSTALLED = True
