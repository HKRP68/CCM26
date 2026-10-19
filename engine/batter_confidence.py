"""Short-lived batting rhythm for over-by-over Approach matches.

Runs/balls faced already buy settling bonuses in ball_outcome, and the team's
momentum is already modelled. This tracks only a batter's recent execution:
it can fall as well as rise, decays toward neutral, and never changes ratings.
The maximum weight nudge is 2.4%, with scoring mass conserved independently of
extras. No random draws, external state, pitch bonus or innings-total target.
"""

NEUTRAL = 50.0
RETENTION = 0.80
SIGNALS = {0: -0.60, 1: 0.10, 2: 0.30, 3: 0.45, 4: 0.80, 6: 1.0}
SCORING = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket")


def value(stats):
    """Old saved matches without this field resume at neutral."""
    return max(0.0, min(100.0, float((stats or {}).get("confidence", NEUTRAL))))


def label(stats):
    confidence = value(stats)
    if confidence >= 60:
        return "In rhythm"
    if confidence <= 40:
        return "Tied down"
    return "Steady"


def record(stats, runs):
    """Record one legal shot for this batter, after its outcome is settled.

    Caller excludes extras and wickets. An extra neither boosts confidence nor
    decays it; a dismissed batter cannot affect their replacement's state.
    """
    signal = SIGNALS.get(runs, 0.0)
    recent = (value(stats) - NEUTRAL) / 50.0
    recent = RETENTION * recent + (1.0 - RETENTION) * signal
    stats["confidence"] = round(NEUTRAL + 50.0 * recent, 6)


def apply(weights, stats):
    """Return weights tilted by recent rhythm, preserving the non-extra mass.

    The first few deliveries retain the existing new-batter vulnerability.
    This tiny residual is strongest after six shots, and is not another
    permanent bonus for being set. A neutral/missing confidence is a no-op.
    """
    recent = max(-0.60, min(0.60, (value(stats) - NEUTRAL) / 50.0))
    recent *= min(1.0, max(0, (stats or {}).get("balls", 0)) / 6.0)
    if recent == 0:
        return weights
    multipliers = {"Dot": 1.0 - 0.03 * recent,
                   "Single": 1.0 + 0.015 * recent,
                   "Double": 1.0 + 0.015 * recent,
                   "Four": 1.0 + 0.04 * recent,
                   "Six": 1.0 + 0.03 * recent,
                   "Wicket": 1.0 - 0.02 * recent}
    out = dict(weights)
    before = sum(out.get(k, 0.0) for k in SCORING)
    for key, factor in multipliers.items():
        if key in out:
            out[key] *= factor
    after = sum(out.get(k, 0.0) for k in SCORING)
    if after > 0:
        for key in SCORING:
            if key in out:
                out[key] *= before / after
    return out
