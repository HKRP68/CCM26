"""Printed squad ratings, shared by league and auction roster reports."""
import math

RATING_FIELDS = ('rating', 'bat_rating', 'bowl_rating')


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def averages(players):
    """Mean of known printed ratings; missing values never become zeroes."""
    rows = list(players)
    result = {}
    for field in RATING_FIELDS:
        values = [number(getattr(p, field, None)) for p in rows]
        values = [value for value in values if value is not None]
        result[field] = f'{sum(values) / len(values):.1f}' if values else '—'
    return result


def summary(values):
    return (f"⭐ Squad rating: {values['rating']} OVR · BAT {values['bat_rating']} "
            f"· BOWL {values['bowl_rating']} (squad averages)")


def display(value):
    value = number(value)
    return f'{value:g}' if value is not None else '—'
