"""GSpin reward service — DB-backed weighted random reward picker.

The /gspin handler calls `pick_reward(session)` to get one row from the
GSpinReward table according to weighted probability. If the table is empty,
returns None so the handler can fall back to the legacy config-based outcomes.
"""

import random
import logging

logger = logging.getLogger(__name__)


def pick_reward(session):
    """Pick one enabled GSpinReward row using weighted random selection.

    Returns the row, or None if no enabled rows exist (caller falls back to
    legacy GSPIN_OUTCOMES config).
    """
    from models import GSpinReward
    rows = (session.query(GSpinReward)
                   .filter(GSpinReward.enabled == True)
                   .order_by(GSpinReward.sort_order, GSpinReward.id)
                   .all())
    if not rows:
        return None

    total_weight = sum(max(1, r.weight or 1) for r in rows)
    if total_weight <= 0:
        return None

    r = random.random() * total_weight
    cumulative = 0
    for row in rows:
        cumulative += max(1, row.weight or 1)
        if r < cumulative:
            return row
    return rows[-1]  # safety fallback


def reward_probability(row, all_enabled_rows):
    """Helper for admin UI — compute the actual probability of a row firing."""
    total = sum(max(1, r.weight or 1) for r in all_enabled_rows
                if r.enabled)
    if total <= 0:
        return 0.0
    return max(1, row.weight or 1) / total
