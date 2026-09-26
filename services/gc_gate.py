"""Forced Official GC join — the bot-wide "join our group first" gate.

An admin switch (Website → Maintenance → "Force Official GC join"). While it is
OFF, or no Official GC is configured, nothing changes and this module costs a
dict lookup per update. While it is ON, using the bot requires being a member
of the Official GC (``GameConfig.official_group_id``):

* every command and button is locked until the user joins;
* the doorway commands below (/start, /debut, help, feedback…) keep working,
  so a brand-new player can still see what the bot is and how to get in;
* the join prompt carries a "Join" link and an "✅ I've joined" button that
  re-checks membership on the spot;
* admins in the bypass list are never locked out;
* anything said *inside* the Official GC passes — being there is the proof.

Like ``services/rookie_gate.py`` the rules are pure: the bot middleware asks
Telegram whether the user is a member (cached) and passes the answer in. A
failed membership lookup (for example the bot is not an admin of the GC)
fails OPEN — a misconfigured gate must never lock the whole bot.
"""

from __future__ import annotations

import html
import logging
import re

from services.command_token import command_name_from_update
from services.rookie_gate import (
    is_bypassed, is_referral_code_reply, is_service_message,
)

logger = logging.getLogger(__name__)

# Commands that keep working before the user has joined. Aliases are listed
# explicitly (the gate sees the raw command). ``tests/test_gc_gate.py`` pins
# every name here to a command bot.py actually registers.
FREE_COMMANDS = frozenset({
    "start", "s",
    "debut", "d",
    "howto", "help", "guide",
    "feedback", "fb",
    "redeem", "code",
    "botstatus", "bstatus", "ping",
    "commands", "cmds",
})

# The re-check button itself, the referral Skip button, and the inert filler.
VERIFY_CALLBACK = "gcjoin_check"
FREE_CALLBACK_PREFIXES = (VERIFY_CALLBACK, "refcode_skip_", "noop")

# ChatMember statuses that count as "in the group". ``restricted`` counts only
# while ``is_member`` is true (a muted member is still a member).
MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


# ── Mode state ──────────────────────────────────────────────────────

def _cfg_get(cfg, key, default=None):
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def official_group_id(cfg=None):
    """The Official GC's numeric chat id, or None when unconfigured."""
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    gid = _cfg_get(cfg, "official_group_id")
    try:
        return int(gid) if gid else None
    except (TypeError, ValueError):
        return None


def is_gc_gate_active(cfg=None) -> bool:
    """True when the switch is ON *and* there is a group to join."""
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    return bool(_cfg_get(cfg, "force_gc_join")) and official_group_id(cfg) is not None


def join_url(cfg=None) -> str | None:
    """A clickable t.me link to the Official GC, or None."""
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    link = (_cfg_get(cfg, "official_group_link") or "").strip()
    if link:
        if link.startswith("@"):
            return "https://t.me/" + link[1:]
        if not re.match(r"^https?://", link) and link.startswith("t.me/"):
            return "https://" + link
        return link
    uname = (_cfg_get(cfg, "branding_group_username") or "").strip().lstrip("@")
    return ("https://t.me/" + uname) if uname else None


def is_member_status(member) -> bool:
    """True if a Telegram ``ChatMember`` means the user is in the chat."""
    status = getattr(member, "status", None)
    if status not in MEMBER_STATUSES:
        return False
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return True


async def check_membership(bot, group_id, user_id) -> bool | None:
    """Ask Telegram whether ``user_id`` is in ``group_id``.

    Returns True/False, or None when Telegram could not answer (the bot is not
    in the group, not an admin there, a network blip…). Callers treat None as
    "let them through" — see the module docstring.
    """
    try:
        member = await bot.get_chat_member(group_id, user_id)
    except Exception as exc:
        text = str(exc).lower()
        # "user not found" / "participant_id_invalid" really mean "not a member".
        if "user not found" in text or "participant" in text:
            return False
        logger.warning("Official GC membership lookup failed (%s) — failing open", exc)
        return None
    return is_member_status(member)


def is_free_command(name: str) -> bool:
    return (name or "").strip().casefold() in FREE_COMMANDS


def is_free_callback(data: str) -> bool:
    data = data or ""
    return any(data.startswith(prefix) for prefix in FREE_CALLBACK_PREFIXES)


def is_in_official_group(update, cfg=None) -> bool:
    """True when the update was sent inside the Official GC itself."""
    chat = getattr(update, "effective_chat", None)
    gid = official_group_id(cfg)
    return bool(chat is not None and gid is not None
                and getattr(chat, "id", None) == gid)


# ── Bot updates ─────────────────────────────────────────────────────

def needs_check(update, *, bot_username=None, cfg=None, allow_text=False) -> bool:
    """True when this update would be blocked for a non-member.

    The middleware calls this *before* asking Telegram about membership, so
    doorway commands, service messages and updates from inside the Official
    GC never cost an API call.
    """
    if not is_gc_gate_active(cfg):
        return False

    user = getattr(update, "effective_user", None)
    if user is None or getattr(user, "is_bot", False):
        return False
    if is_bypassed(getattr(user, "id", None)):
        return False
    if is_in_official_group(update, cfg):
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

    # Plain text: blocked (silently — see should_reply), except the referral
    # code /debut is waiting for.
    return not (allow_text and is_referral_code_reply(update))


def should_block_update(update, *, is_member, bot_username=None, cfg=None,
                        allow_text=False) -> bool:
    """Full decision: blocked when the update needs a check and the user is
    not a member. ``is_member`` None (lookup failed) never blocks."""
    if is_member or is_member is None:
        return False
    return needs_check(update, bot_username=bot_username, cfg=cfg,
                       allow_text=allow_text)


def should_reply(update, bot_username=None) -> bool:
    """Commands and button taps get the join prompt; chatter is dropped."""
    if getattr(update, "callback_query", None) is not None:
        return True
    return bool(command_name_from_update(update, bot_username))


# ── Messaging ───────────────────────────────────────────────────────

def _group_label(cfg=None) -> str:
    uname = (_cfg_get(cfg, "branding_group_username") or "").strip().lstrip("@")
    if uname:
        return "@" + uname
    link = (_cfg_get(cfg, "official_group_link") or "").strip()
    m = re.search(r"t\.me/([A-Za-z0-9_]{3,})$", link)
    return ("@" + m.group(1)) if m else "our Official Group"


def join_required_message(cfg=None, *, first_name=None) -> str:
    """The join prompt, in Telegram HTML. An admin can override it."""
    if cfg is None:
        from services.config_service import get_config
        cfg = get_config()
    custom = (_cfg_get(cfg, "gc_join_message") or "").strip()
    if custom:
        return custom
    hello = f"Hey {html.escape(first_name)}! " if first_name else ""
    return (
        f"👋 {hello}<b>One quick step before you play</b>\n\n"
        f"Join <b>{html.escape(_group_label(cfg))}</b>, our official community. "
        "That's where you'll find match partners, giveaways, tournament "
        "fixtures, patch news and help from the admins.\n\n"
        "1️⃣ Tap <b>Join Official GC</b>\n"
        "2️⃣ Come back and tap <b>✅ I've joined</b>\n\n"
        "<i>It's free and takes 5 seconds, and every command unlocks straight away.</i>"
    )


def join_required_alert() -> str:
    """Plain-text version for callback-query alerts."""
    return "🔒 Join the Official GC first, then tap ✅ I've joined to unlock the bot."


def join_keyboard(cfg=None):
    """[🔗 Join Official GC] [✅ I've joined] — or just the check button."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    url = join_url(cfg)
    rows = []
    if url:
        rows.append([InlineKeyboardButton("🔗 Join Official GC", url=url)])
    rows.append([InlineKeyboardButton("✅ I've joined", callback_data=VERIFY_CALLBACK)])
    return InlineKeyboardMarkup(rows)
