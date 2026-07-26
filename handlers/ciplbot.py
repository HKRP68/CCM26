"""/ciplbot — play the /cipl league format against the AI captain.

Your half of the flow is the ordinary Challenge League experience: you pick your
team, you pick the pitch, and you pick your Playing XI through the same numbered
squad list, tap-to-pick buttons and quick-select-by-numbers that /cipl uses. Only
the opponent is automated — the bot takes one of the remaining teams and fields
the best legal XI that squad allows (highest OVR first, keeper + five bowling
options + the league's overseas limits), then answers its own per-over prompts
via ``services.bot_captain``.

Every /ciplbot match is UNRANKED practice: no career stats, no Player of the
Match credit, no coins or gems, no Win/Loss, no streak, and it is never recorded
against a tournament or a CL Tour series.

Usage:
  /ciplbot            → IPL (the default league)
  /ciplbot bbl        → a specific league by short code
  /c<league>bot       → an admin league's own command with a "bot" suffix
"""

import logging
from html import escape

from telegram import Update
from telegram.ext import ContextTypes

from database import get_session
from services.telegram_user_service import sync_telegram_user
from handlers.challenge import (
    _challenge_leagues, _get_challenge_league_record, _league_display_from_key,
    _league_teams, _send_league_team_picker, normalize_challenge_league,
)

logger = logging.getLogger(__name__)

DEFAULT_LEAGUE_KEY = "ipl"

# Commands that already have their own CommandHandler in bot.py. python-telegram-bot
# runs one handler PER GROUP, so the catch-all league-command regex handler (group 1)
# sees these too — and "ciplbot" minus its suffix is "cipl", a real league command.
# Without this guard /ciplbot would start two drafts and the second would fail with
# "a team selection is already in progress". Every command here ends in "bot", which
# is the only case the alias resolver looks at.
_DIRECTLY_REGISTERED = frozenset({
    "ciplbot", "challengeiplbot",          # this module's own commands
    "lpbot", "letsplaybot",                # handlers/lpbot.py
    "vsbot", "wpmbot", "wspbot", "botvsbot",  # the older ball-by-ball bot modes
})


def league_key_from_bot_command(command_name, session):
    """Resolve a ``<something>bot`` command to a league, or ``(None, None)``.

    Lets an admin league's own command gain a bot variant for free: if
    ``/cbbl`` starts a BBL challenge then ``/cbblbot`` starts a BBL bot match.
    Returns ``(league_key, display_name)``.
    """
    from handlers.challenge import is_challenge_league_command

    command = (command_name or "").lower().lstrip("/").split("@", 1)[0]
    if not command.endswith("bot") or len(command) <= 3:
        return None, None
    if command in _DIRECTLY_REGISTERED:
        return None, None
    return is_challenge_league_command(command[:-3], session)


def _resolve_league(session, args, command_name):
    """Pick the league for this /ciplbot invocation.

    Priority: an explicit argument (``/ciplbot bbl``), then a ``<command>bot``
    alias, then IPL. Returns ``(league_key, display_name)`` or ``(None, None)``
    when the user named a league that doesn't exist.
    """
    leagues = _challenge_leagues(session)
    requested = (args[0] if args else "").strip()
    if requested:
        key = normalize_challenge_league(requested)
        if not key or key not in leagues:
            return None, None
        return key, _league_display_from_key(key, leagues)

    key, name = league_key_from_bot_command(command_name, session)
    if key:
        return key, name
    return DEFAULT_LEAGUE_KEY, _league_display_from_key(DEFAULT_LEAGUE_KEY, leagues)


async def ciplbot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ciplbot [league] — start an unranked league match against the bot."""
    message = getattr(update, "effective_message", None)
    tg = getattr(update, "effective_user", None)
    if message is None or tg is None:
        return

    command_name = ""
    text = (getattr(message, "text", None) or "").strip()
    if text.startswith("/"):
        command_name = text.split()[0]
    args = list(getattr(context, "args", None) or [])

    session = get_session()
    try:
        challenger = sync_telegram_user(session, tg)
        if not challenger:
            await message.reply_text("❌ Use /debut first.")
            return

        league_key, league_name = _resolve_league(session, args, command_name)
        if not league_key:
            # League keys are admin-configured, and this goes out as HTML — an
            # unescaped "<" would fail the send outright instead of showing the
            # help text.
            known = ", ".join(escape(k) for k in sorted(_challenge_leagues(session))) \
                or "none configured"
            await message.reply_text(
                f"❌ Unknown league. Try one of: {known}.\n"
                f"Example: <code>/ciplbot ipl</code>", parse_mode="HTML")
            return

        league_record = _get_challenge_league_record(session, league_key)
        teams = _league_teams(session, league_key, league_record)
        if len(teams) < 2:
            await message.reply_text(
                f"❌ {league_name} needs at least two teams to play a bot match.")
            return

        from handlers.vsbot import _get_or_create_bot_user
        bot_user = _get_or_create_bot_user(session)
        session.commit()

        await _send_league_team_picker(
            update, context, challenger=challenger, target=bot_user,
            league_key=league_key, league_name=league_name,
            league_record=league_record, teams=teams, session=session,
            vs_bot=True,
        )
    except Exception:
        logger.exception("/ciplbot setup failed")
        try:
            await message.reply_text("⚠️ Couldn't start the bot match. Try again.")
        except Exception:
            pass
    finally:
        session.close()
