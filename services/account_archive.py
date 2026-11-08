"""Reserved internal identities for displaced game accounts, never Telegram IDs."""

ARCHIVE_ID_BASE = 10**15


def is_archived_identity(value):
    try:
        return int(value) <= -ARCHIVE_ID_BASE
    except (TypeError, ValueError):
        return False
