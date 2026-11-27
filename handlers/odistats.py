"""/odistats <player> — a cricketer's long-format career record.

``/stats`` answers "how has my copy of this card played in the short formats".
This answers the same question for ODI cricket, which CCM26 keeps in a table of
its own: any innings of 21 overs or more is played under ODI rules and recorded
in ``odi_player_innings`` rather than the short-format career table, so a
50-over hundred never flatters a T20 strike rate and a T20 spell never flatters
an ODI economy. See ``services/odi_stats_service.py`` for the read model and
``docs/odi-approach.md`` for the format rule.

``/odistats <player> global`` sums the same innings across every owner of that
card, the long-format twin of ``/gstats``.
"""

from __future__ import annotations

import logging
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from models import Player, User, UserRoster
from services import odi_stats_service as odi_stats
from services.flags import get_flag

logger = logging.getLogger(__name__)

COMMAND = "odistats"
#: Trailing word that widens the lookup from "my copy" to "every owner".
GLOBAL_TOKENS = {"global", "all", "world"}

USAGE_HTML = (
    "Usage: <code>/odistats &lt;player name&gt;</code>\n"
    "Example: <code>/odistats Virat Kohli</code>\n\n"
    "<i>ODI career record — every innings played over 21 overs or more. "
    "Add <code>global</code> for every owner's innings. Use /stats for the "
    "short-format record.</i>"
)


def split_scope(args):
    """``(search_name, global_scope)`` from the command's words."""
    words = [str(a) for a in (args or [])]
    scope = bool(words) and words[-1].strip().lower() in GLOBAL_TOKENS
    if scope:
        words = words[:-1]
    return " ".join(words).strip(), scope


def _owned_player(session, user_id, search):
    """The player in this owner's roster matching ``search``, or ``None``."""
    row = (session.query(Player)
           .join(UserRoster, UserRoster.player_id == Player.id)
           .filter(UserRoster.user_id == user_id, Player.name.ilike(f"%{search}%"))
           .first())
    return row


def _any_player(session, search):
    return (session.query(Player)
            .filter(Player.name.ilike(f"%{search}%"))
            .order_by(Player.rating.desc())
            .first())


def record_text(player, record, *, owner_label=None, global_scope=False):
    """The message body for one career record."""
    flag = get_flag(player.country)
    scope = ("every owner" if global_scope
             else owner_label or "your copy")
    header = (
        f"🏏 <b>{escape(player.name)}</b> {flag}\n"
        f"⭐ {player.rating} OVR | {escape(str(player.category or ''))}\n"
        f"🥇 <b>ODI career</b> · <i>{escape(str(scope))}</i>\n"
        f"📋 Matches: {record['matches']}\n"
    )
    return f"{header}\n<code>{escape(odi_stats.record_block(record))}</code>"


def empty_text(player, *, global_scope=False):
    flag = get_flag(player.country)
    who = "anyone" if global_scope else "you"
    return (
        f"📭 <b>{escape(player.name)}</b> {flag} has no ODI innings for {who} yet.\n\n"
        f"<i>ODI records come from matches of 21 overs or more — start one with "
        f"<code>/lp 50 @username</code> or a league command such as "
        f"<code>/cipl 50 @username</code>.</i>"
    )


async def odistats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    tg_user = update.effective_user
    if message is None or tg_user is None:
        return

    search, global_scope = split_scope(getattr(context, "args", None))
    if not search:
        await message.reply_text(USAGE_HTML, parse_mode="HTML")
        return

    session = get_session()
    try:
        from services.command_config_service import (
            get_disabled_message, is_command_enabled)
        if not is_command_enabled(session, COMMAND):
            await message.reply_text(get_disabled_message(session, COMMAND),
                                     parse_mode="HTML")
            return

        user = session.query(User).filter(User.telegram_id == tg_user.id).first()
        if not user:
            await message.reply_text("❌ Do /debut first!")
            return

        player = (_any_player(session, search) if global_scope
                  else _owned_player(session, user.id, search))
        if not player and not global_scope:
            await message.reply_text(
                f"❌ Player '{escape(search)}' isn't in your roster. You can only "
                f"view your own cards' records — add <code>global</code> for "
                f"every owner's ODI innings.", parse_mode="HTML")
            return
        if not player:
            await message.reply_text(f"❌ No player matches '{escape(search)}'.",
                                     parse_mode="HTML")
            return

        # A re-released edition shares its parent's identity everywhere else,
        # so the record follows the same rule: every edition of the card, in
        # one query.
        record = odi_stats.careers(session, edition_ids(session, player),
                                   None if global_scope else user.id)
        if not odi_stats.has_record(record):
            await message.reply_text(empty_text(player, global_scope=global_scope),
                                     parse_mode="HTML")
            return

        owner_label = f"@{user.username}" if user.username else (user.first_name or "you")
        await message.reply_text(
            record_text(player, record, owner_label=owner_label,
                        global_scope=global_scope),
            parse_mode="HTML")
    except Exception:
        logger.exception("/odistats failed for %s", tg_user.id)
        await message.reply_text("⚠️ Error. Try again.")
    finally:
        session.close()


def edition_ids(session, player):
    """Every edition of ``player``'s card — the base card and its re-releases."""
    base_id = player.parent_player_id or player.id
    rows = (session.query(Player.id)
            .filter((Player.id == base_id)
                    | (Player.parent_player_id == base_id)).all())
    return [row.id for row in rows] or [player.id]
