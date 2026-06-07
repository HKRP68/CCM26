"""Match formats (adapted from SimCricketX engine/format_config.py).

Provides per-format settings the auto-sim uses: innings overs, the per-bowler
over quota, the powerplay/death over windows and the expected run rate per phase
(used to shape batting aggression). Pure data — no external dependencies.
"""

# key -> config. quota follows the standard ~20% rule (overs // 5).
FORMATS = {
    "T10": {
        "label": "T10", "overs": 10, "max_bowler_overs": 2,
        "powerplay_end": 3, "death_start": 8,
        "expected_rr": {"Powerplay": 9.0, "Middle": 9.5, "Death": 12.0},
    },
    "T20": {
        "label": "T20", "overs": 20, "max_bowler_overs": 4,
        "powerplay_end": 6, "death_start": 16,
        "expected_rr": {"Powerplay": 7.5, "Middle": 8.0, "Death": 10.5},
    },
    # NOTE: longer formats (ODI/Test) need the ball-outcome model retuned for
    # 50-over wicket rates — the current model is T20-tuned and collapses sides
    # too quickly over 50 overs. Left as future work.
}

_ALIASES = {
    "T10": "T10", "10": "T10",
    "T20": "T20", "20": "T20",
}


def resolve_format(token):
    """Map a user token (e.g. 'T20', 'odi', '50') to a format key, or None."""
    if not token:
        return None
    return _ALIASES.get(str(token).strip().upper())


def get_format(key):
    """Return the FORMATS config dict for a key (defaults to T20)."""
    return FORMATS.get(key, FORMATS["T20"])


def custom_format(overs):
    """Build an ad-hoc format for an arbitrary over count (/sim N)."""
    overs = max(1, int(overs))
    return {
        "label": f"{overs} ov",
        "overs": overs,
        "max_bowler_overs": max(1, -(-overs // 5)),  # ceil(overs/5)
        "powerplay_end": max(1, round(overs * 0.3)),
        "death_start": overs - max(1, round(overs * 0.2)) + 1,
        "expected_rr": {"Powerplay": 7.5, "Middle": 8.0, "Death": 10.5},
    }


def phase_for(over, fmt):
    """Return 'Powerplay' | 'Middle' | 'Death' for a 1-based over in a format."""
    if over <= fmt["powerplay_end"]:
        return "Powerplay"
    if over >= fmt["death_start"]:
        return "Death"
    return "Middle"
