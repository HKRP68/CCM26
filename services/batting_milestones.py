"""Exact batting milestones, captured on the scoring delivery, never inferred."""

THRESHOLDS = (50, 100, 150)


def capture(stats, previous_runs):
    """Save the first balls-faced count for each newly crossed threshold.

    Called after a delivery's runs and balls faced have been applied. Crossing
    checks deliberately leave already-passed milestones in older states unknown.
    The fields survive the existing match-state/scorecard JSON persistence.
    """
    runs = int(stats.get("runs") or 0)
    balls = int(stats.get("balls") or 0)
    for threshold in THRESHOLDS:
        if previous_runs < threshold <= runs and balls > 0:
            stats.setdefault(f"balls_to_{threshold}", balls)


def saved(stats, threshold, *, runs_key="runs", balls_key="balls"):
    """A valid recorded milestone, or None for legacy/malformed evidence."""
    value = stats.get(f"balls_to_{threshold}")
    if type(value) is not int or value <= 0:
        return None
    if int(stats.get(runs_key) or 0) < threshold or value > int(stats.get(balls_key) or 0):
        return None
    return value
