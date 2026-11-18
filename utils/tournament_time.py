from datetime import datetime, timedelta


IST_OFFSET = timedelta(hours=5, minutes=30)


def format_ist(value, fmt="%d %b %Y, %H:%M"):
    """Format a stored UTC timestamp in Indian Standard Time."""
    return (value + IST_OFFSET).strftime(fmt) + " IST"


def parse_ist_deadline(value):
    """Read an IST date and time and return the naive UTC value used by the database."""
    try:
        return datetime.strptime(value.strip().replace("T", " "), "%Y-%m-%d %H:%M") - IST_OFFSET
    except (AttributeError, ValueError, OverflowError) as exc:
        raise ValueError("Enter a deadline with a date and time in IST (YYYY-MM-DD HH:MM).") from exc
