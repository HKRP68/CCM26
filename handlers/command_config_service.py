"""Command config service — read BotCommand + CommandReward from DB,
with safe fallbacks to config.py constants when the DB row is missing or
the override value is 0/unset.

Handlers call these helpers instead of reading config constants directly,
so admins can adjust rewards/cooldowns from the website without redeploying.
"""

import logging

logger = logging.getLogger(__name__)


def get_command(session, command_key):
    """Returns BotCommand row or None."""
    try:
        from models import BotCommand
        return (session.query(BotCommand)
                       .filter(BotCommand.command_key == command_key)
                       .first())
    except Exception:
        logger.exception("get_command failed")
        return None


def is_command_enabled(session, command_key):
    """Returns True if the command is enabled (or no row exists — fail open)."""
    row = get_command(session, command_key)
    if row is None:
        return True  # fail-open if not configured
    return bool(row.enabled)


def get_cooldown(session, command_key, default_seconds):
    """Get effective cooldown for a command. 0 in DB → use default."""
    row = get_command(session, command_key)
    if row is None or not row.cooldown_seconds:
        return default_seconds
    return row.cooldown_seconds


def get_reward(session, command_key):
    """Returns CommandReward row or None."""
    try:
        from models import CommandReward
        return (session.query(CommandReward)
                       .filter(CommandReward.command_key == command_key)
                       .first())
    except Exception:
        logger.exception("get_reward failed")
        return None


def get_coin_amount(session, command_key, default):
    """Get coin reward — DB value if set (>0), else default."""
    r = get_reward(session, command_key)
    if r is None:
        return default
    if r.coin_amount and r.coin_amount > 0:
        return r.coin_amount
    return default


def get_gem_amount(session, command_key, default):
    """Get gem reward — DB value if set (>0), else default."""
    r = get_reward(session, command_key)
    if r is None:
        return default
    if r.gem_amount and r.gem_amount > 0:
        return r.gem_amount
    return default


def get_player_count(session, command_key, default):
    """Get player-count reward — DB value if set (>0), else default."""
    r = get_reward(session, command_key)
    if r is None:
        return default
    if r.player_count and r.player_count > 0:
        return r.player_count
    return default


def get_disabled_message(session, command_key):
    """Polite message when a command is disabled by admin."""
    row = get_command(session, command_key)
    name = row.display_name if row else command_key
    return (f"🚧 <b>{name}</b> is temporarily disabled by the admin.\n"
            f"<i>Check back later — or use a different command meanwhile.</i>")
