"""Bot-admin Telegram IDs — a single, dependency-light source of truth.

Admin IDs come from environment variables (primary) plus the
``maintenance_bypass_ids`` admin-config value (convenience, so deployments that
already maintain admin IDs there don't have to duplicate them).

This module intentionally avoids importing ``telegram`` so it can be used from
both handler and service layers (and unit-tested) without pulling the bot
framework in.
"""

import logging
import os
from collections.abc import Iterable

from services.config_service import get_config

logger = logging.getLogger(__name__)

ADMIN_ID_ENV_VARS = (
    "BOT_ADMIN_IDS",
    "ADMIN_IDS",
    "ADMIN_USER_IDS",
    "SUDO_USERS",
    "OWNER_IDS",
    "ADMIN_CHAT_ID",
)

# Owner IDs are a strict subset of admins used to gate the most sensitive
# commands (e.g. /grant subscriptions). Read only from owner-specific env vars.
OWNER_ID_ENV_VARS = (
    "OWNER_IDS",
    "BOT_OWNER_IDS",
)


def parse_id_list(raw_values: Iterable[str | None]) -> set[int]:
    """Parse comma/space/semicolon/newline separated positive Telegram IDs."""
    ids: set[int] = set()
    for raw in raw_values:
        if not raw:
            continue
        normalized = str(raw).replace(";", ",").replace("\n", ",")
        for part in normalized.replace(" ", ",").split(","):
            token = part.strip()
            if not token:
                continue
            try:
                value = int(token)
            except ValueError:
                continue
            # Negative IDs are groups/channels — they can't identify an
            # individual command sender, so they're ignored here.
            if value > 0:
                ids.add(value)
    return ids


def configured_admin_ids() -> set[int]:
    """Return global bot-admin Telegram user IDs from env + admin config."""
    raw_values = [os.getenv(name) for name in ADMIN_ID_ENV_VARS]
    try:
        raw_values.append((get_config() or {}).get("maintenance_bypass_ids"))
    except Exception:
        logger.exception("Failed to load maintenance bypass admin IDs")
    return parse_id_list(raw_values)


def is_admin(user_id: int | None, admin_ids: set[int] | None = None) -> bool:
    """Check whether a Telegram user is a configured bot admin."""
    if user_id is None:
        return False
    allowed = admin_ids if admin_ids is not None else configured_admin_ids()
    return int(user_id) in allowed


def configured_owner_ids() -> set[int]:
    """Return owner-only Telegram user IDs from the owner env vars."""
    return parse_id_list(os.getenv(name) for name in OWNER_ID_ENV_VARS)


def is_owner(user_id: int | None) -> bool:
    """Check whether a Telegram user is a configured bot OWNER.

    Falls back to the general admin allowlist when no owner IDs are configured,
    so the owner-only commands aren't dead on deployments that only set admin
    IDs (rather than silently locking everyone out)."""
    if user_id is None:
        return False
    owners = configured_owner_ids()
    if not owners:
        return is_admin(user_id)
    return int(user_id) in owners


# ════════════════════════════════════════════════════════════════════
# Approval reviewers — who sees and decides player submissions
# ════════════════════════════════════════════════════════════════════
#
# Team logos and CMU News stories are shown to everyone once approved, so the
# review DMs (and the ✅/❌ buttons) go to a deliberately narrow set:
#
#   * the bot admins and owners named in the environment, plus
#   * the extra reviewers in ``GameConfig.approval_reviewer_ids``, which the
#     owners manage from the website or with /approvers.
#
# The ``maintenance_bypass_ids`` list is NOT part of it. That list is for
# people testing the bot during maintenance, and counting them as admins is
# how review cards ended up in testers' DMs.

def env_admin_ids() -> set[int]:
    """Bot admins and owners from the environment only — no admin-config IDs."""
    return (parse_id_list(os.getenv(name) for name in ADMIN_ID_ENV_VARS)
            | configured_owner_ids())


def extra_reviewer_ids() -> set[int]:
    """The reviewers added on top of the bot admins (admin config)."""
    try:
        return parse_id_list([(get_config() or {}).get("approval_reviewer_ids")])
    except Exception:
        logger.exception("Failed to load approval reviewer IDs")
        return set()


def configured_reviewer_ids() -> set[int]:
    """Everyone who receives team-logo / CMU News review DMs.

    A deployment that names its admins only through the admin config (no
    environment IDs, no extra reviewers) falls back to that list rather than
    leaving every submission without a reviewer.
    """
    reviewers = env_admin_ids() | extra_reviewer_ids()
    return reviewers or configured_admin_ids()


def is_reviewer(user_id: int | None) -> bool:
    """May this Telegram user approve or reject a player submission?"""
    if user_id is None:
        return False
    return int(user_id) in configured_reviewer_ids()


def can_manage_reviewers(user_id: int | None) -> bool:
    """Only an environment admin/owner may change the reviewer list.

    An added reviewer must not be able to add more reviewers, and neither may
    a maintenance-bypass tester.
    """
    if user_id is None:
        return False
    managers = env_admin_ids()
    if not managers:
        return is_admin(user_id)
    return int(user_id) in managers


def format_id_list(ids: Iterable[int]) -> str | None:
    """Canonical storage form: sorted, comma-separated, ``None`` when empty."""
    out = ", ".join(str(i) for i in sorted(set(ids)))
    return out or None
