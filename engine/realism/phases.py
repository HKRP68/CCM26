"""The single source of truth for realism-mode match phases.

Phases in the realism engine are defined as **fractions of the innings**, not
hardcoded over numbers: powerplay is the first 30%, death is the last 20%,
the middle is what is left. Expressed this way, T20 maps to its real overs
(0-5 / 6-15 / 16-19), The Hundred's 100 balls get a correctly proportioned
powerplay and death, and a custom-over /sim behaves sensibly, all from one
rule and with no per-format table.

These two thresholds are the reason this module exists. They were duplicated
in ``engine/realism/context.py`` (which decides a live delivery's phase) and
``tools/build_realism_reference.py`` (which assigns each counted delivery to a
phase when the reference is built). If those two copies ever drifted apart,
the baseline node a delivery was filed under at build time would not match the
node looked up at play time, and the engine would silently read the wrong
distribution. Centralising them makes that class of bug impossible.

Requirement 10 of the Fairness & Simulation Fixes: centralise the
format-specific powerplay / middle / death boundaries.

The legacy, over-based engine keeps its own boundaries in
``engine/format_config.FormatConfig`` (the ``Phase`` objects). That system is
unchanged; this module is the realism engine's single source, and the two are
cross-referenced here so a reader can find both.
"""

POWERPLAY = "powerplay"
MIDDLE = "middle"
DEATH = "death"

PHASES = (POWERPLAY, MIDDLE, DEATH)

#: Upper fraction of the innings at which the powerplay ends and the death
#: begins. ``balls_bowled / innings_balls < POWERPLAY_END`` is the powerplay;
#: ``>= DEATH_START`` is the death; between is the middle. For a 20-over T20
#: (120 balls) this is overs 0-5, 6-15 and 16-19, which is the split the
#: legacy engine's FormatConfig also uses.
POWERPLAY_END = 0.30
DEATH_START = 0.80

#: The same two thresholds for an ODI-rules innings, whose real powerplay is
#: the first ten overs of fifty and whose death is the last ten — 20% and 80%
#: rather than 30% and 80%. Expressed as fractions so a shortened ODI (a
#: 21-to-49-over match on the same engine) splits proportionally; at the full
#: 300 balls they land on ball 60 and ball 240, which is where the measured
#: reference filed its deliveries.
ODI_POWERPLAY_END = 0.20
ODI_DEATH_START = 0.80


def phase_for_fraction(frac: float) -> str:
    """The phase at a given fraction (0..1) through the innings."""
    if frac < POWERPLAY_END:
        return POWERPLAY
    if frac < DEATH_START:
        return MIDDLE
    return DEATH


def phase_for_ball(balls_bowled: int, innings_balls: int, format_name=None) -> str:
    """The phase of the next delivery, from balls bowled and the innings length.

    ``balls_bowled`` is clamped into ``[0, innings_balls)`` so the last ball of
    the innings is read as a death ball rather than rolling over to the next
    phase bucket.
    """
    total = max(1, int(innings_balls))
    done = min(total - 1, max(0, int(balls_bowled)))
    frac = done / float(total)
    if format_name == "ODI":
        return (POWERPLAY if frac < ODI_POWERPLAY_END
                else MIDDLE if frac < ODI_DEATH_START else DEATH)
    return phase_for_fraction(frac)
