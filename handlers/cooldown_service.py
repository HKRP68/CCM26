"""Cooldown checking and updating."""

from datetime import datetime


def check_cooldown(stats, field: str, cooldown_seconds: int) -> tuple[bool, int]:
    """Return (is_ready, remaining_seconds)."""
    last = getattr(stats, field, None)
    if last is None:
        return True, 0
    now = datetime.utcnow()
    elapsed = (now - last).total_seconds()
    if elapsed >= cooldown_seconds:
        return True, 0
    return False, int(cooldown_seconds - elapsed)


def format_remaining(seconds: int) -> str:
    """Human-readable remaining time."""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if s or not parts:
        parts.append(f"{s}s")
    return " ".join(parts)
