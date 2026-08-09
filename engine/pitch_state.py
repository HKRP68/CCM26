"""Dynamic Pitch State — the surface as a living thing across two innings.

Section 2 of the Unified T20 Engine v3.0. The pitch a side bats on second is not
the pitch the first side batted on, and *how* it differs is the pitch's own
character: a dry track cracks and turns the death overs into a lottery, a green
one loses its grass and flattens out, footmarks on a dusty surface make the
middle overs a graveyard. A flat deck, famously, does none of this.

Two separate things live here, and they are separate on purpose:

* :func:`carry_wear` fixes *when* the surface wears. The engine's own wear model
  (``engine.ball_outcome._apply_pitch_wear``) is driven by a 0-1 fraction that
  CIPL used to compute per-innings, so the second innings began on a brand-new
  pitch every match. Wear is now measured across the whole match: innings 1 runs
  0.0 → 0.5, innings 2 runs 0.5 → 1.0.

* :func:`innings2_effects` is the *differential identity* — the per-pitch,
  per-phase behaviour the doc's Innings-2 column describes, which the generic
  wear curve has no way to express (it does not model Even, Bouncy or Dusty at
  all, and cannot say "only after over 16"). It returns outcome multipliers for
  one ball and is inert in the first innings.

Keeping them apart is what stops the pitch being applied twice: the wear curve
supplies the slow drift, this module supplies the character, and neither repeats
the other. Everything here is a pure function of its arguments — no state, no
config reads, no imports from ``services``.

Public API:
    carry_wear(innings, balls_bowled, innings_balls) -> float
    innings2_effects(pitch, over, overs_total, is_spin=False) -> dict
    innings2_trace(pitch, over, overs_total) -> list[str]
    evolution_note(pitch) -> str | None
"""

from engine.approach_modifiers import (
    merge_multipliers,
    runs_factor,
    wicket_factor,
)


# ── When the surface wears ───────────────────────────────────────────

def carry_wear(innings, balls_bowled, innings_balls):
    """Match-long pitch wear as a 0-1 fraction, for ``calculate_outcome``.

    A T20 pitch is bowled on for 40 overs, not 20. Reading wear per-innings —
    which is what CIPL did — hands the chasing side a fresh square every time and
    makes the two innings mechanically interchangeable, which is the single
    biggest thing Section 2 is written against.

    Innings 1 spans 0.0 → 0.5, innings 2 spans 0.5 → 1.0, so a ball bowled in the
    50th over of the match is read as a 50th-over ball. Innings beyond the second
    (a Super Over is scored separately) stay pinned at the top of the range.
    """
    innings_balls = max(1, int(innings_balls or 1))
    bowled = max(0, int(balls_bowled or 0))
    prior = max(0, int(innings or 1) - 1)
    if prior >= 2:
        # A third innings is a Super Over, scored separately. Whatever it is, the
        # square is not fresher than it was at the end of the second.
        return 1.0
    within = min(1.0, bowled / innings_balls)
    return min(1.0, 0.5 * prior + 0.5 * within)


# ── How the surface changes ──────────────────────────────────────────
#
# Each rule is ``(gate, multipliers, label)``:
#   gate         → a predicate over (over, overs_total, is_spin); None = always
#   multipliers  → outcome multipliers, in the shared runs/wicket vocabulary
#   label        → what to call this in the match analysis file
#
# The doc's over numbers ("after Over 8", "Overs 7-12", "after Over 16") are
# written for a 20-over innings. They are stored as fractions of the innings so
# a 5-over game, or The Hundred's 20 five-ball sets, still gets the same shape of
# pitch rather than a rule that never fires or fires from ball one.

def _at(over, overs_total, frac):
    """True from the over that sits ``frac`` of the way through the innings."""
    return int(over) >= max(1, round(float(frac) * max(1, int(overs_total))))


def _between(over, overs_total, lo, hi):
    return _at(over, overs_total, lo) and not _at(over, overs_total, hi)


def _spin_only(rule):
    return lambda over, total, is_spin: is_spin and rule(over, total)


INNINGS2_EVOLUTION = {
    # Minimal surface wear. The doc's own summary is "virtually identical to the
    # 1st innings", and the rule is here mostly to say so explicitly.
    "Flat": [
        (None, runs_factor(0.98), "minimal surface wear (-2%)"),
    ],
    "Dead": [
        (None, runs_factor(0.98), "minimal surface wear (-2%)"),
    ],
    # Slight softening: the ball stops coming on quite as fast, and the spinners
    # get the first small dividend of that.
    "Hard": [
        (None, runs_factor(0.97), "pace carry down (-3%)"),
        (_spin_only(lambda o, t: True), wicket_factor(+0.05),
         "softer surface helps spin"),
    ],
    # Moderate wear: a good length is harder to get away, and the ball grips
    # just enough for the spinners to notice.
    "Even": [
        (None, runs_factor(0.95), "batting ease down (-5%)"),
        (_spin_only(lambda o, t: True), wicket_factor(+0.05),
         "minor grip increase for spin"),
    ],
    # The surface settles: the steep, unplayable bounce goes out of it, which
    # costs the quicks their wicket edge and makes the pull and hook safe shots.
    "Bouncy": [
        (None, merge_multipliers(runs_factor(1.03), wicket_factor(-0.04)),
         "bounce settles (-4% viciousness), pull/hook on"),
    ],
    # Cracks open. Nothing much changes until they start to matter, and then the
    # death overs become a genuine lottery — the doc's "timing extremely
    # difficult after Over 16".
    "Dry": [
        (lambda o, t, s: not _at(o, t, 0.80), runs_factor(0.98),
         "surface slowing"),
        (lambda o, t, s: _at(o, t, 0.80),
         merge_multipliers(runs_factor(0.90), wicket_factor(+0.10)),
         "cracks open (+10% variable bounce) — death overs dangerous"),
    ],
    # Grass burns off. The powerplay stops being a survival exercise around the
    # third over, and from the ninth the pitch is simply flat.
    "Green": [
        (lambda o, t, s: _between(o, t, 0.15, 0.45), wicket_factor(-0.08),
         "lateral movement fading (-8%) — powerplay less lethal"),
        (lambda o, t, s: _at(o, t, 0.45),
         merge_multipliers(runs_factor(1.05), wicket_factor(-0.15)),
         "grass gone — pitch has flattened out"),
    ],
    # Footmarks deepen. Spin bites all innings, and in the middle overs the
    # batting side simply cannot get off strike.
    "Dusty": [
        (_spin_only(lambda o, t: True), wicket_factor(+0.15),
         "footmarks deepen (+15% spin grip)"),
        (lambda o, t, s: _between(o, t, 0.35, 0.65), runs_factor(0.90),
         "middle overs are a graveyard"),
    ],
}

# One-line character note per pitch, for the match analysis report.
EVOLUTION_NOTES = {
    "Flat": "Minimal wear — the chase gets the same batting paradise.",
    "Dead": "Minimal wear — the chase gets the same batting paradise.",
    "Hard": "Softened slightly; pace carry down, spin a shade more dangerous.",
    "Even": "Moderate wear; a good length grips and is harder to score off.",
    "Bouncy": "Settled down; less vicious bounce, pull and hook back on.",
    "Dry": "Cracked open; the death overs became a lottery.",
    "Green": "Grass burnt off; seam faded early and the pitch flattened out.",
    "Dusty": "Footmarks deepened; the middle overs turned square.",
}


def _rules(pitch):
    return INNINGS2_EVOLUTION.get(str(pitch or ""), ())


def innings2_effects(pitch, over, overs_total, is_spin=False):
    """Outcome multipliers for one ball of the second innings.

    Returns an empty dict for an unknown pitch or a phase with no rule, which
    the caller treats as "no change" — so a new pitch type costs nothing until
    someone writes its row.
    """
    out = {}
    for gate, mult, _label in _rules(pitch):
        if gate is None or gate(over, overs_total, bool(is_spin)):
            out = merge_multipliers(out, mult)
    return out


def innings2_trace(pitch, over, overs_total):
    """The labels of the rules live at this point of the innings.

    Spin-gated rules are reported as live: the trace describes the *pitch*, and
    whether a spinner happened to be bowling is the captain's business, not the
    surface's. Used by the match analysis report, never by the simulation.
    """
    labels = []
    for gate, _mult, label in _rules(pitch):
        if gate is None or gate(over, overs_total, True):
            labels.append(label)
    return labels


def evolution_note(pitch):
    """One-line summary of how this pitch changed for the chase, or ``None``."""
    return EVOLUTION_NOTES.get(str(pitch or ""))


# Every rule must scale outcomes the engine actually produces, and every pitch
# with a rule must have a note to go with it — caught at import, not in a match.
_OUTCOME_KEYS = {"Dot", "Single", "Double", "Three", "Four", "Six",
                 "Wicket", "Extras"}
for _pitch, _rule_list in INNINGS2_EVOLUTION.items():
    assert _pitch in EVOLUTION_NOTES, (
        f"INNINGS2_EVOLUTION[{_pitch!r}] has no EVOLUTION_NOTES entry")
    for _gate, _mult, _label in _rule_list:
        _unknown = set(_mult) - _OUTCOME_KEYS
        assert not _unknown, (
            f"INNINGS2_EVOLUTION[{_pitch!r}] ({_label}) scales unknown "
            f"outcome(s) {sorted(_unknown)}")
