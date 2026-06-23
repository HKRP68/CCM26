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
