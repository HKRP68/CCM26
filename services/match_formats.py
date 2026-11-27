"""Match formats (adapted from SimCricketX engine/format_config.py).

Provides per-format settings the auto-sim uses (T10, T20, ODI, Test): innings
overs, the per-bowler over quota, the powerplay/death over windows and the expected run rate per phase
(used to shape batting aggression). Pure data — no external dependencies.
"""

# key -> config. quota follows the standard ~20% rule (overs // 5).
#
# expected_rr is the neutral-surface rate per phase. The T20 numbers mirror
# engine.format_config's T20 curve (par 196 on Even, split 8.9 / 9.8 / 11.2
# across its own phase windows); they were 7.5 / 8.0 / 10.5, which projected a
# 167 innings and so shaped the sim's aggression against a rate thirty runs
# below what the engine actually produces. This module is deliberately
# dependency-free, so the numbers are copied rather than imported.
FORMATS = {
    "T10": {
        "label": "T10", "overs": 10, "max_bowler_overs": 2,
        "powerplay_end": 3, "death_start": 8,
        "expected_rr": {"Powerplay": 10.0, "Middle": 10.6, "Death": 12.6},
    },
    "T20": {
        "label": "T20", "overs": 20, "max_bowler_overs": 4,
        "powerplay_end": 6, "death_start": 16,
        "expected_rr": {"Powerplay": 8.9, "Middle": 9.8, "Death": 11.2},
    },
    # Long formats run on the Conditions Engine (engine.sim.match), which is
    # tuned for 50-over and multi-day cricket — see services.sim_long. The
    # phase fields here are only descriptive; that engine reads its own
    # config/sim_engine.json.
    "ODI": {
        "label": "ODI", "overs": 50, "max_bowler_overs": 10,
        "powerplay_end": 10, "death_start": 41,
        "expected_rr": {"Powerplay": 5.6, "Middle": 5.2, "Death": 8.5},
        "long_format": True,
    },
    "Test": {
        "label": "Test", "overs": None, "max_bowler_overs": None,
        "days": 5, "long_format": True,
    },
}

_ALIASES = {
    "T10": "T10", "10": "T10",
    "T20": "T20", "20": "T20",
    "ODI": "ODI", "50": "ODI", "ONEDAY": "ODI", "OD": "ODI", "50OV": "ODI",
    "TEST": "Test", "TESTMATCH": "Test", "5DAY": "Test", "5DAYS": "Test",
}


def resolve_format(token):
    """Map a user token (e.g. 'T20', 'odi', '50') to a format key, or None."""
    if not token:
        return None
    return _ALIASES.get(str(token).strip().upper().replace("-", "").replace(" ", ""))


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
        "expected_rr": {"Powerplay": 8.9, "Middle": 9.8, "Death": 11.2},
    }


def phase_for(over, fmt):
    """Return 'Powerplay' | 'Middle' | 'Death' for a 1-based over in a format."""
    if over <= fmt["powerplay_end"]:
        return "Powerplay"
    if over >= fmt["death_start"]:
        return "Death"
    return "Middle"


# ── Custom overs for /letsplay and the Challenge League commands ──────
CUSTOM_OVERS_MIN = 1
CUSTOM_OVERS_MAX = 50
# The shortest innings that runs under ODI rules. 1-20 overs stay on the T20
# realism engine exactly as before; 21-50 run on the measured ODI model
# (``engine.realism.odi``) with its phases, ten-over quota and par curve
# scaled to the length asked for. The over count is the only switch, which is
# also what makes "ODI career stats" a clean rule: an innings earns them when
# it is 21 overs or longer.
MEDIUM_OVERS_MIN = 21
# A full-length ODI.
ODI_OVERS = 50
# A bare number below this is an over count; anything larger is left alone,
# because a bare number is also how a command names a Telegram user id.
_OVERS_TOKEN_CEILING = 1000


def is_medium_format(overs):
    """True when an over count runs under ODI rules (21 to 50 overs)."""
    try:
        overs = int(overs or 0)
    except (TypeError, ValueError):
        return False
    return MEDIUM_OVERS_MIN <= overs <= ODI_OVERS


def ball_format_for_overs(overs):
    """The match-state ``ball_format`` an over count should be played under.

    ``"ODI"`` for 21 to 50 overs, ``"T20"`` for anything shorter — the single
    rule every entry point (``/letsplay``, ``/lpbot``, ``/lpmulti`` and the
    Challenge League commands) uses, so one over count never means two
    different engines depending on which command typed it.
    """
    return "ODI" if is_medium_format(overs) else "T20"


def extract_overs_arg(args, lo=CUSTOM_OVERS_MIN, hi=CUSTOM_OVERS_MAX):
    """Pull an over count out of a command's arguments.

    Accepts ``5``, ``5ov``, ``5overs`` or ``T5`` anywhere in ``args``. Returns
    ``(overs or None, remaining_args, error or None)``: the remaining args are
    what the command's target lookup should see (an ``@username`` or a user
    id), and ``error`` is a user-facing message for an over count out of range.
    Pure — no Telegram.
    """
    import re
    overs, rest, error = None, [], None
    for tok in list(args or []):
        raw = str(tok).strip()
        m = re.fullmatch(r"(?i)(?:t(\d{1,3})|(\d{1,3})\s*(?:ov|ovs|over|overs)?)", raw)
        if overs is None and error is None and m:
            n = int(m.group(1) or m.group(2))
            if n < _OVERS_TOKEN_CEILING:
                if lo <= n <= hi:
                    overs = n
                else:
                    error = f"Overs must be between {lo} and {hi}."
                continue
        rest.append(tok)
    return overs, rest, error


#: What a bad length token is told to use instead.
_LENGTH_HELP = (f"Use ODI, or a length between {CUSTOM_OVERS_MIN} and "
                f"{CUSTOM_OVERS_MAX} overs.")


def extract_approach_format(args):
    """Parse one explicit Approach format and leave opponent/mode tokens intact.

    Returns ``(ball_format, overs, remaining_args, error)``. ``ball_format`` is
    ``"ODI"`` for any length of :data:`MEDIUM_OVERS_MIN` overs or more (``ODI``
    on its own means the full :data:`ODI_OVERS`) and ``"T20"`` below that; the
    engine follows the over count, never the spelling of the token.
    """
    import re

    from engine.realism import odi

    format_name, overs, remaining = "T20", None, []
    for token in args or ():
        word = str(token).strip().lower()
        match = re.fullmatch(r"(?:t(\d{1,3})|(\d{1,3})\s*(?:ov|ovs|over|overs)?)", word)
        if word == "odi":
            length = ODI_OVERS
        elif match:
            length = int(match[1] or match[2])
        elif (re.fullmatch(r"odi[+-]?\d.*", word) or re.fullmatch(r"t[+-]?\d.*", word)
              or re.fullmatch(r"[+-]?\d{1,3}\.\d+", word) or re.fullmatch(r"-\d{1,3}", word)):
            return None, None, remaining, _LENGTH_HELP
        else:
            remaining.append(token)
            continue
        if overs is not None:
            return None, None, remaining, "Choose one match format or over limit."
        if not CUSTOM_OVERS_MIN <= length <= CUSTOM_OVERS_MAX:
            return (None, None, remaining,
                    f"Custom matches must be between {CUSTOM_OVERS_MIN} and {CUSTOM_OVERS_MAX} overs.")
        if is_medium_format(length):
            if not odi.enabled():
                return None, None, remaining, "ODI Approach is currently unavailable."
            format_name = "ODI"
        overs = length
    return format_name, overs, remaining, None
