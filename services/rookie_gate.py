"""Rookie mode — the bot-wide membership gate.

Rookie mode is an admin switch (Website → Maintenance → "Rookie mode"). While
it is OFF nothing changes: the bot behaves exactly as it always has, and this
module costs one dict lookup per update. While it is ON, using the bot at all
requires an ACTIVE subscription of at least the ``rookie`` tier (₹15/month):

* every command, every button and every Mini App API is locked;
* a brand-new player can still walk through the door — ``/debut`` creates the
  account, and the handful of other doorway commands below (help, membership,
  the post-debut referral prompt, feedback) keep working so a locked user can
  find out what to buy and reach a human;
* every higher tier outranks Rookie, so Bronze → Diamond members are covered
  automatically (see ``subscription_service.has_tier_at_least``);
* admins in the bypass list are never locked out.

Nothing here touches the database. Callers resolve the user (bot middleware,
Flask before-request hook) and pass the answer in, which keeps the rules pure
and unit-testable — and keeps the "mode is off" fast path free of DB work.
"""

from __future__ import annotations

import logging
import re

from services.command_token import command_name_from_update

logger = logging.getLogger(__name__)

# The tier that buys access. Everything ranked at or above it is allowed in.
ROOKIE_TIER = "rookie"

# Commands that keep working without a membership while Rookie mode is on.
# Aliases are listed explicitly because the gate sees the raw command the user
# typed, not the handler it would have reached. ``tests/test_rookie_gate.py``
# pins every name here to a command bot.py actually registers, so a renamed
# alias can't silently lock the front door.
FREE_COMMANDS = frozenset({
    # The way in — a brand-new user must be able to create an account.
    "debut", "d",
    "start", "s",
    # What am I locked out of, and what does it cost?
    "membership", "member", "mysub", "subscription", "plans",
    # Help + support paths: a locked user must still be able to read the guide
    # and reach the admins.
    "howto", "help", "guide",
    "feedback", "fb",
    # The referral code prompt fired by /debut itself.
    "redeem", "code",
    # Liveness check — answers "is the bot down or am I locked out?".
    "botstatus", "bstatus", "ping",
})

# Callback data prefixes that stay tappable: the Skip button on the post-debut
# referral prompt, and the inert filler button.
FREE_CALLBACK_PREFIXES = ("refcode_skip_", "noop")


# ── Mode state ──────────────────────────────────────────────────────

def is_rookie_mode_active(cfg=None) -> bool:
    """True when the admin has Rookie mode switched ON."""
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    return bool((cfg or {}).get("rookie_mode"))


def has_access(user) -> bool:
    """True if ``user`` holds an active tier of at least Rookie.

    ``user`` is a ``models.User`` row (or None for someone who has never run
    ``/debut``). Expiry is honoured on read by ``subscription_service``, so a
    lapsed membership locks the bot again with no background job.
    """
    if user is None:
        return False
    try:
        from services import subscription_service
        return subscription_service.has_tier_at_least(user, ROOKIE_TIER)
    except Exception:
        # A broken tier lookup must never lock the whole bot out.
        logger.exception("Rookie access check failed — failing open")
        return True


def is_bypassed(telegram_id) -> bool:
    """True for admins/owners, who are never locked out by Rookie mode."""
    if telegram_id is None:
        return False
    try:
        from services.admin_ids import is_admin
        return is_admin(int(telegram_id))
    except Exception:
        logger.exception("Rookie bypass lookup failed (non-fatal)")
        return False


def is_free_command(name: str) -> bool:
    """True for a doorway command that works without a membership."""
    return (name or "").strip().casefold() in FREE_COMMANDS


def is_free_callback(data: str) -> bool:
    """True for a button that stays tappable without a membership."""
    data = data or ""
    return any(data.startswith(prefix) for prefix in FREE_CALLBACK_PREFIXES)


# Telegram service messages — somebody joined, left, renamed the chat. They are
# events ABOUT a chat, not a player using the bot, so the gate leaves them
# alone: blocking them would silence the group welcome that tells a brand-new
# player to run /debut in the first place.
_SERVICE_MESSAGE_FIELDS = (
    "new_chat_members", "left_chat_member", "new_chat_title", "new_chat_photo",
    "delete_chat_photo", "pinned_message", "group_chat_created",
    "supergroup_chat_created", "channel_chat_created",
    "migrate_to_chat_id", "migrate_from_chat_id",
)


def is_service_message(message) -> bool:
    """True for a Telegram service message rather than something a user sent."""
    if message is None:
        return False
    return any(getattr(message, field, None)
               for field in _SERVICE_MESSAGE_FIELDS)


# The ONE plain-text message a non-member may send: the referral code /debut
# just asked them for. The conditions mirror the handler that consumes it
# (``handlers.redeem.text_code_handler`` — private chat, and text shaped like a
# code), because an exemption wider than its handler is simply a hole: the
# ``awaiting_referral_code`` flag survives until a code is redeemed or Skip is
# tapped, so a player who answers with neither would otherwise keep a permanent
# pass into every text-driven handler (WordChase, Bluff, XI quick-select…).
# ``tests/test_rookie_gate.py`` pins this pattern against the redeem handler's.
_REFERRAL_CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{6}$")


def looks_like_referral_code(text: str) -> bool:
    """True if ``text`` is shaped like a referral code (6 chars, our alphabet)."""
    return bool(_REFERRAL_CODE_RE.match((text or "").strip().upper().lstrip("/")))


def is_referral_code_reply(update) -> bool:
    """True for a private-chat message whose text is shaped like a code.

    The caller supplies the other half of the answer — whether the bot is
    actually waiting for one (``allow_text``).
    """
    message = getattr(update, "message", None)
    if message is None:
        return False
    chat_type = getattr(getattr(message, "chat", None), "type", None)
    if chat_type is None:
        chat_type = getattr(getattr(update, "effective_chat", None), "type", None)
    if chat_type != "private":
        return False
    return looks_like_referral_code(getattr(message, "text", "") or "")


# ── Bot updates ─────────────────────────────────────────────────────

def should_block_update(update, *, has_membership, bot_username=None,
                        cfg=None, allow_text=False) -> bool:
    """Decide whether Rookie mode blocks this Telegram update.

    Rules:
      - Rookie mode off → never block
      - Member (Rookie or better) or bypassed admin → never block
      - Doorway button (``FREE_CALLBACK_PREFIXES``) → allowed
      - Not a message at all (chat-member updates, the bot being added to a
        group, edited messages…) → not the gate's business
      - Telegram service message (someone joined/left the chat) → allowed, so
        the group welcome that invites new players still fires
      - Doorway command (``FREE_COMMANDS``) → allowed
      - ``allow_text`` (the bot is waiting for the referral code /debut asked
        for) AND the message actually looks like one, sent in a DM → allowed
      - Everything else — commands, buttons and chatter alike → blocked

    Unlike maintenance mode, in-flight callback queries are NOT exempt: Rookie
    mode is a paywall, not a pause, and a non-member has nothing in flight to
    protect (they could not have started it).
    """
    if not is_rookie_mode_active(cfg):
        return False
    if has_membership:
        return False

    user = getattr(update, "effective_user", None)
    if user is not None and is_bypassed(getattr(user, "id", None)):
        return False

    callback = getattr(update, "callback_query", None)
    if callback is not None:
        return not is_free_callback(getattr(callback, "data", "") or "")

    message = getattr(update, "message", None)
    if message is None or is_service_message(message):
        return False

    name = command_name_from_update(update, bot_username)
    if name:
        return not is_free_command(name)

    # Plain (non-command) message: blocked, but silently — see should_reply.
    # The single exception is the referral code the bot is waiting on, and only
    # when it really looks like one (see is_referral_code_reply).
    return not (allow_text and is_referral_code_reply(update))


def should_reply(update, bot_username=None) -> bool:
    """True when a blocked update deserves the upsell instead of silence.

    Commands and button taps get an answer; plain group chatter is dropped
    silently so a locked-down bot doesn't spam every group message.
    """
    if getattr(update, "callback_query", None) is not None:
        return True
    return bool(command_name_from_update(update, bot_username))


# ── Mini App ────────────────────────────────────────────────────────

def should_block_miniapp_path(path, *, has_membership, telegram_id=None,
                              cfg=None) -> bool:
    """Decide whether Rookie mode blocks this Mini App request.

    Reuses maintenance mode's prefix list, so a Mini App API added later is
    locked by default rather than quietly staying open. Ad-network postbacks
    stay open for the same reason they do during maintenance — they are an
    external service reporting a finished ad, not a player using the app, and
    dropping one destroys a reward that was already earned.
    """
    from services.maintenance_service import is_ad_postback_path, is_miniapp_path

    if not is_miniapp_path(path):
        return False
    if not is_rookie_mode_active(cfg):
        return False
    if is_ad_postback_path(path):
        return False
    if has_membership:
        return False
    return not is_bypassed(telegram_id)


# ── Messaging ───────────────────────────────────────────────────────

def _plans_line() -> str:
    """"🐣 Rookie (₹15/mo)… " — every tier, cheapest first, from the config."""
    try:
        from services.subscription_service import tier_price_list
        return tier_price_list()
    except Exception:
        logger.exception("Rookie plan list failed (non-fatal)")
        return ""


def rookie_price_label() -> str:
    """"🐣 Rookie (₹15/month)" for headline copy, straight from the config."""
    try:
        from config import SUBSCRIPTION_TIERS
        from services.subscription_service import tier_label
        cfg = SUBSCRIPTION_TIERS.get(ROOKIE_TIER) or {}
        price = cfg.get("price_inr")
        label = tier_label(ROOKIE_TIER)
        return f"{label} (₹{price}/month)" if price else label
    except Exception:
        logger.exception("Rookie label lookup failed (non-fatal)")
        return "Rookie"


def rookie_required_message(cfg=None, *, is_new_user=False) -> str:
    """The upsell a locked user sees, in Telegram HTML.

    An admin can replace it with ``rookie_message`` on the Maintenance page;
    the default explains the price, that any higher tier already includes
    access, and what to do next. ``is_new_user`` (no account yet) points at
    /debut first — telling someone to check /membership before they have an
    account is a dead end.
    """
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    custom = ((cfg or {}).get("rookie_message") or "").strip()
    if custom:
        return custom

    if is_new_user:
        return (
            "🔒 <b>Members only</b>\n\n"
            "Start with /debut to create your account — that part is free.\n\n"
            f"After that you'll need {rookie_price_label()} to keep playing: "
            "it unlocks every command and the Mini App. Any higher membership "
            "includes it too.\n\n"
            "<i>Ask an admin to activate your membership.</i>"
        )

    plans = _plans_line()
    return (
        "🔒 <b>Membership required</b>\n\n"
        f"The bot is members-only right now. {rookie_price_label()} unlocks "
        "every command and the Mini App — and every higher membership includes "
        "that access.\n\n"
        + (f"<b>Plans:</b> {plans}\n\n" if plans else "")
        + "Check /membership for your status, then ask an admin to activate it."
    )


def rookie_required_alert() -> str:
    """Short plain-text version for callback answers and JSON payloads.

    Telegram's ``answer(show_alert=True)`` and the Mini App's error toast both
    render text, not HTML — markup would show through as literal tags.
    """
    return (f"🔒 Members only — {rookie_price_label()} unlocks the bot. "
            "Send /membership for details.")
