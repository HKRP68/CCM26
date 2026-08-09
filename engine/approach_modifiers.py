"""Approach modifiers for the over-by-over Challenge League simulation.

Each over both captains pick a tactical *approach*. These approaches scale the
raw outcome weights produced by ``engine.ball_outcome.calculate_outcome`` right
before the final delivery is sampled, so the result still respects ratings,
pitch, momentum, pressure and scenario logic — the approach only *tilts* the
odds the way the spec in ``New Features.md`` describes.

Outcome keys match the engine's weight dict:
    Dot, Single, Double, Three, Four, Six, Wicket, Extras
Any key omitted from a table is left unchanged (multiplier 1.0). The "balanced"
approach on both sides is intentionally neutral (all multipliers 1.0).

Layers
------
:func:`apply_approach_modifiers` multiplies together, in order:

1. the batting intent (:data:`_BATTING_MULT`),
2. the bowling plan (:data:`_BOWLING_MULT`),
3. the interaction term for that specific pair (:data:`_COUNTER_MULT`),
4. the named special combination the pair makes (:data:`SPECIAL_COMBOS`),

all four of which depend only on the two picks — and then, *only* when the
caller passes a ``context`` built by :func:`make_context`, the situational
layers:

5. pitch × approach (:data:`_PITCH_RULES`),
6. phase × approach (:data:`_PHASE_RULES`),
7. the mind game — read-the-bowler / outthink-the-batter (:data:`_READS`),
8. momentum, bowler fatigue and partnership chemistry,
9. anything the *previous* over's combination carried into this one.

Layers 5-9 are inert without a context, so a caller that knows nothing about
where the over is being bowled still gets a coherent 5×5 game.

Two deliberate omissions from the design doc, both to avoid double-counting a
thing the engine already models properly:

* **Player rating modifiers.** ``engine.rating_duel`` plus the level/tier layers
  in ``engine.ball_outcome`` already scale boundaries, dots and wickets by the
  batter's and bowler's absolute ratings *and* by the gap between them, far more
  finely than an Elite/Good/Average ladder. Adding the ladder on top would apply
  class twice and unpick the innings-total calibration.
* **Rating- and bowling-type-only pitch effects** ("spinners get +5% wicket on a
  dusty pitch"). ``PITCH_WICKET_FACTOR`` / ``ground_conditions.yaml`` already do
  exactly this. Only the parts of the pitch table that depend on the *approach*
  are implemented here, because only those are new information.

Reading the doc's numbers
-------------------------
The design doc is written in per-over units — "+2 runs to the range", "+5%
wicket chance" — while this layer scales per-ball outcome weights. The
translation is deliberate and lives in one place: :func:`_runs` treats one run
per over as :data:`_RUN_UNIT` of scoring weight, and percentage lines are read
as *relative* nudges to the outcome's weight rather than absolute additions to
a probability. Absolute addition would be far more violent than intended — two
points on a 4% per-ball wicket weight is a 50% jump — and would fight the
innings calibration the rest of the engine is tuned to.
"""

# ── Batting approaches ────────────────────────────────────────────────
# Ordered for button display: (key, emoji, label).
BATTING_APPROACHES = [
    ("defensive", "🛡", "Defensive"),
    ("rotate", "🔁", "Rotate Strike"),
    ("balanced", "⚖", "Balanced"),
    ("aggressive", "🚀", "Aggressive"),
    ("ultra", "💥", "Ultra Attack"),
]

# ── Bowling approaches ────────────────────────────────────────────────
BOWLING_APPROACHES = [
    ("defensive", "🛡", "Defensive"),
    ("balanced", "⚖", "Balanced"),
    ("mixed", "☠️", "Mixed"),
    ("aggressive", "🔥", "Aggressive"),
    ("variation", "🌀", "Variation"),
]

BATTING_KEYS = {k for k, _, _ in BATTING_APPROACHES}
BOWLING_KEYS = {k for k, _, _ in BOWLING_APPROACHES}

DEFAULT_BATTING = "balanced"
DEFAULT_BOWLING = "balanced"

# ── Balance note ──────────────────────────────────────────────────────
# The first version of these tables was measurably broken: simulated against
# the live engine (500 overs per cell, four pitches, seven rating match-ups),
# Ultra Attack beat every other batting approach in *every* bowling column and
# Defensive bowling beat every other plan in *every* batting row. Held for a
# whole innings that was 244 all-out-proof runs for Ultra against 180 for
# Defensive, so the approach layer had exactly one correct answer for each
# captain and picking anything else was a mistake. The tables below restore the
# trade-off, along two axes:
#
#   • **Risk is priced.** Aggressive and Ultra now buy their boundaries with a
#     genuinely higher wicket rate, so slogging from ball one runs out of
#     batting long before it runs out of overs — which is what stops "always
#     Ultra" from being free.
#   • **Every plan has a weakness** (:data:`_COUNTER_MULT`). Defensive bowling
#     shuts the boundary down but hands over singles; attacking plans buy
#     wickets by offering pace to hit; change-of-pace punishes a slogger but is
#     wasted on a batter content to knock it around.
#
# Between them those make the over a real simultaneous-move game with a mixed
# solution instead of a table with one right answer. Measured the same way
# afterwards, the best reply now moves with what the other captain picked:
#
#   vs Defensive bowling → Ultra          vs Defensive batting  → Aggressive
#   vs Balanced  bowling → Ultra          vs Rotate    batting  → Defensive
#   vs Mixed     bowling → Rotate         vs Balanced  batting  → Mixed
#   vs Aggressive bowling→ Aggressive     vs Aggressive batting → Variation
#   vs Variation bowling → Balanced       vs Ultra     batting  → Variation
#
# Two options are deliberately never *optimal* on runs alone and that is the
# role they play rather than a bug: Balanced bowling is the safe neutral, and
# Defensive batting is a survival tool whose value (protecting the last wicket,
# seeing off a spell) a runs-scored yardstick cannot show.
#
# Any retune should be re-checked the same way — ``tests/test_cipl_approach.py``
# asserts the no-dominant-approach property directly, so a change that brings
# back a single right answer fails the suite rather than shipping quietly.

# Per-approach outcome multipliers. Missing outcomes default to 1.0.
_BATTING_MULT = {
    "defensive": {"Dot": 1.45, "Single": 1.25, "Double": 0.90, "Three": 0.80,
                  "Four": 0.45, "Six": 0.25, "Wicket": 0.45},
    "rotate":    {"Dot": 0.78, "Single": 1.66, "Double": 1.45, "Three": 1.15,
                  "Four": 0.72, "Six": 0.48, "Wicket": 0.64},
    "balanced":  {},
    "aggressive": {"Dot": 0.86, "Single": 0.92, "Double": 1.00, "Three": 1.00,
                   "Four": 1.38, "Six": 1.45, "Wicket": 1.45},
    "ultra":     {"Dot": 0.72, "Single": 0.78, "Double": 0.88, "Three": 0.90,
                  "Four": 1.51, "Six": 1.98, "Wicket": 1.80},
}

_BOWLING_MULT = {
    "defensive": {"Dot": 1.04, "Single": 1.48, "Double": 0.98,
                  "Four": 0.76, "Six": 0.70, "Wicket": 0.66},
    "balanced":  {},
    "mixed":     {"Dot": 1.00, "Single": 1.14, "Double": 1.08, "Four": 0.84,
                  "Six": 0.60, "Wicket": 1.18},
    "aggressive": {"Dot": 0.86, "Single": 1.02, "Four": 1.29,
                   "Six": 1.30, "Wicket": 1.55},
    "variation": {"Dot": 1.05, "Single": 0.98, "Four": 0.90,
                  "Six": 0.84, "Wicket": 1.10},
}

# ── The counter matrix ───────────────────────────────────────────────
# Applied on top of both tables above, keyed (batting approach, bowling plan).
# The per-approach tables alone are separable — each one scales its outcomes
# the same way whatever the other captain does — and a separable game collapses
# to one dominant answer per side. These interaction terms are what make the
# over rock-paper-scissors: the same plan is a match-winner against one intent
# and a gift against another, so neither captain can settle on a favourite.
#
# Read each line as the cricket it describes.
_COUNTER_MULT = {
    # Change of pace is the answer to a slogger — but only to a slogger.
    ("ultra", "variation"):      {"Six": 0.55, "Four": 0.72, "Wicket": 1.48},
    ("aggressive", "variation"): {"Six": 0.68, "Four": 0.82, "Wicket": 1.34},
    ("ultra", "mixed"):          {"Six": 0.70, "Four": 0.86, "Wicket": 1.24},
    ("aggressive", "mixed"):     {"Six": 0.82, "Four": 0.92, "Wicket": 1.22},

    # ...and pace on a length to a batter swinging through the line is a gift.
    ("ultra", "aggressive"):      {"Four": 1.10, "Six": 1.12, "Wicket": 0.90},
    ("aggressive", "aggressive"): {"Four": 1.16, "Six": 1.20, "Wicket": 0.94},

    # Attacking fields against someone with no intent of scoring: all upside.
    ("defensive", "aggressive"): {"Wicket": 1.75, "Four": 0.78, "Six": 0.72,
                                  "Dot": 1.12},
    ("rotate", "aggressive"):    {"Wicket": 1.45, "Four": 0.84, "Six": 0.80,
                                  "Single": 1.14},

    # Boundary-denial concedes the single — so milking it beats it.
    ("rotate", "defensive"):     {"Single": 1.30, "Double": 1.12, "Dot": 0.84},
    ("balanced", "defensive"):   {"Single": 1.12, "Dot": 0.94},
    ("aggressive", "defensive"): {"Four": 1.14, "Six": 1.18},
    ("ultra", "defensive"):      {"Four": 1.18, "Six": 1.24},
    # Two captains both refusing to take a risk: nobody scores, nobody gets out.
    ("defensive", "defensive"):  {"Dot": 1.14, "Wicket": 0.82},

    # Mixed and Variation are built to strangle the single, which is exactly
    # what a strike-rotator needs — and they nip at a batter with no answer.
    ("rotate", "mixed"):         {"Single": 0.84, "Dot": 1.12, "Wicket": 1.14},
    ("rotate", "variation"):     {"Single": 1.02, "Dot": 0.98, "Wicket": 0.96},
    ("balanced", "mixed"):       {"Wicket": 1.24, "Four": 0.88},
    ("defensive", "mixed"):      {"Wicket": 1.10},
    ("defensive", "variation"):  {"Wicket": 1.12},
}


# A typo in the tables above ("Sixes", "Wickets") would silently be a no-op —
# no error, just a quietly unbalanced match-up that the directional tests would
# not catch. Fail at import instead.
_OUTCOME_KEYS = {"Dot", "Single", "Double", "Three", "Four", "Six",
                 "Wicket", "Extras"}
for _table, _label in ((_BATTING_MULT, "_BATTING_MULT"),
                       (_BOWLING_MULT, "_BOWLING_MULT"),
                       (_COUNTER_MULT, "_COUNTER_MULT")):
    for _key, _mults in _table.items():
        _unknown = set(_mults) - _OUTCOME_KEYS
        assert not _unknown, (
            f"{_label}[{_key!r}] scales unknown outcome(s) {sorted(_unknown)} — "
            f"valid outcomes are {sorted(_OUTCOME_KEYS)}")
assert set(_COUNTER_MULT) <= {(b, w) for b in BATTING_KEYS for w in BOWLING_KEYS}, (
    "_COUNTER_MULT is keyed on an approach pair that does not exist")


# ════════════════════════════════════════════════════════════════════
# The situational layers — pitch, phase, mind game, momentum, fatigue
# ════════════════════════════════════════════════════════════════════

PHASES = ("powerplay", "middle", "death")

# One run per over, expressed as a shift in scoring weight, and the one knob
# that scales every "+N runs" rule below.
#
# It is not 1:1 and is not meant to be. Weights are renormalised before the ball
# is sampled, so pushing weight onto the boundaries partly pays for itself out of
# the other outcomes: measured against the live engine (20k overs, 70-vs-70, Even
# pitch, over 9) a nominal +1 lands as about +0.55 real runs, +3 as +1.65, and a
# -2 as -1.2. That half-strength is deliberate. These rules *stack* — a death
# Ultra on a Hard pitch with momentum, chemistry and a correct read is five of
# them at once — and at 1:1 apiece that over would routinely go for 20+, which is
# a different game from the one the innings totals are calibrated to.
_RUN_UNIT = 0.12

# How a scoring shift is spread across the run outcomes. Boundaries move most
# (that is where an aggressive intent cashes in), singles barely move, and dots
# move the other way — the over has to find the extra runs from somewhere.
_RUN_SPREAD = {
    "Six": 1.25, "Four": 1.10, "Three": 0.60, "Double": 0.60,
    "Single": 0.25, "Dot": -0.70,
}


def _runs_factor(factor):
    """Outcome multipliers for "this over scores ``factor``× as freely"."""
    gain = float(factor) - 1.0
    return {key: max(0.0, 1.0 + gain * spread)
            for key, spread in _RUN_SPREAD.items()}


def _runs(delta_runs):
    """The doc's "+N runs to the range" as outcome multipliers."""
    return _runs_factor(1.0 + _RUN_UNIT * float(delta_runs))


def _wkt(relative):
    """The doc's "+N% wicket chance", read as a relative nudge (see module doc)."""
    return {"Wicket": max(0.0, 1.0 + float(relative))}


def _merge(*tables):
    """Multiply any number of outcome-multiplier tables together."""
    out = {}
    for table in tables:
        for key, mult in (table or {}).items():
            out[key] = out.get(key, 1.0) * mult
    return out


# Public names for the three translation helpers above. The doc's units — "this
# over scores 5% more freely", "+10% wicket chance", "both of those at once" —
# are the vocabulary every rules layer in the engine is written in, so the
# neighbouring layers (engine.pitch_state, engine.momentum) share these rather
# than each inventing their own reading of a percentage.
runs_factor = _runs_factor
wicket_factor = _wkt
merge_multipliers = _merge


_SPIN_TYPES = {"Off spin", "Leg spin", "Finger spin", "Wrist spin"}


def _is_spin(bowling_type):
    """True for the four spin styles the engine models, for the rules that only
    apply to spin (hitting off the square in the middle overs)."""
    return str(bowling_type or "") in _SPIN_TYPES


# Shared with the other rules layers, for the same reason as the helpers above:
# "is this a spinner" must mean one thing across the engine.
is_spin = _is_spin


# ── Section 4: special combination effects ───────────────────────────
# Named match-ups that play differently enough to deserve their own rule. Each
# entry carries the in-over multipliers, and (where the doc's rule is about what
# happens *next* over) a carry-over resolved by :func:`combo_carry` once the
# over's result is known.
#
# Two rules from the doc are implemented in spirit rather than to the letter,
# because taking them literally would break the ball-by-ball simulation they sit
# on top of:
#   • The Shootout's "runs are doubled" — doubling a completed over's runs would
#     put scores no batting card could explain on the board, and would ignore the
#     wickets that fell inside it. It is modelled as the volatility it is meant
#     to represent: boundaries and wickets both spike, the safe outcomes drain.
#   • The Gladiator Over's 18% wicket cap sits above the engine's own 12% cap on
#     the normalised per-ball wicket share, which binds first. What survives is
#     the part that bites — the boundary surge and a mild damp on the wicket.
SPECIAL_COMBOS = {
    ("defensive", "defensive"): {
        "name": "The Blockathon",
        "flavour": "Nobody wants to blink.",
        "mult": {},
        # If runs < 4 the batter's frustration comes out next over.
        "carry_if": lambda runs, wkts: runs < 4,
        "carry": {"mult": _runs_factor(1.05),
                  "note": "frustration released after a strangled over"},
    },
    ("rotate", "defensive"): {
        "name": "The Grind",
        "flavour": "Ones and twos, over after over.",
        # The doc's "15% chance of a quick-single misfield → extra run" is a
        # single turning into a two, which is exactly a Double.
        "mult": {"Double": 1.15},
    },
    ("aggressive", "aggressive"): {
        "name": "The Gladiator Over",
        "flavour": "Both sides swinging. Somebody is going to blink.",
        "mult": {"Four": 1.20, "Six": 1.20, "Wicket": 0.90},
    },
    ("ultra", "variation"): {
        "name": "The Chess Match",
        "flavour": "Every ball is a guess — and both of them know it.",
        "mult": {"Four": 1.15, "Six": 1.20, "Wicket": 1.10,
                 "Dot": 1.08, "Single": 0.85, "Double": 0.85},
    },
    ("ultra", "aggressive"): {
        "name": "The Shootout",
        "flavour": "Pace on, arms through. Rapid fire.",
        "mult": {"Four": 1.25, "Six": 1.35, "Wicket": 1.25,
                 "Dot": 0.85, "Single": 0.85},
    },
    ("defensive", "aggressive"): {
        "name": "The Survival",
        "flavour": "Bouncers and body blows — and a batter refusing to go.",
        "mult": {},
        # Survive the storm and you are in.
        "carry_if": lambda runs, wkts: wkts == 0,
        "carry": {"mult": {"Wicket": 0.90},
                  "note": "settled in after seeing off the short stuff"},
    },
    ("rotate", "mixed"): {
        "name": "The Confusion",
        "flavour": "Nobody has read a length all over.",
        "mult": {"Dot": 1.10, "Four": 1.12, "Six": 1.10, "Single": 0.92},
    },
    ("balanced", "balanced"): {
        "name": "The Purist",
        "flavour": "Straight cricket, played straight.",
        "mult": {},
        # Exactly par: both captains earned something from it.
        "carry_if": lambda runs, wkts: runs == 8,
        "carry": {"mult": _merge(_runs_factor(1.03), {"Wicket": 1.03}),
                  "note": "momentum tokens from a perfectly poised over"},
    },
}

# Section 3's per-cell flavour. Atmospheric only — it never changes a number,
# and it is what the chat shows when the reveal is on (see cipl_play).
MATRIX_FLAVOUR = {
    ("defensive", "defensive"): "Boring over.",
    ("defensive", "balanced"): "Boring over.",
    ("defensive", "mixed"): "Batter uncomfortable.",
    ("defensive", "aggressive"): "Batter survives.",
    ("defensive", "variation"): "Batter survives.",
    ("rotate", "defensive"): "Bowler wins that one.",
    ("rotate", "balanced"): "Even contest.",
    ("rotate", "mixed"): "High variance.",
    ("rotate", "aggressive"): "Batter under pressure.",
    ("rotate", "variation"): "Batter reading it well.",
    ("balanced", "defensive"): "Batter frustrated.",
    ("balanced", "balanced"): "Classic T20 over.",
    ("balanced", "mixed"): "Mind games.",
    ("balanced", "aggressive"): "Edge-of-seat over.",
    ("balanced", "variation"): "Cat and mouse.",
    ("aggressive", "defensive"): "Batter dominates.",
    ("aggressive", "balanced"): "Batter in control.",
    ("aggressive", "mixed"): "Anyone's game.",
    ("aggressive", "aggressive"): "Fireworks!",
    ("aggressive", "variation"): "Batter guessing.",
    ("ultra", "defensive"): "Batter destroys it.",
    ("ultra", "balanced"): "Batter destroys it.",
    ("ultra", "mixed"): "Chaos over.",
    ("ultra", "aggressive"): "Bloodbath!",
    ("ultra", "variation"): "Russian roulette.",
}

# ── Section 6: pitch × approach ──────────────────────────────────────
# Only the approach-conditional half of the doc's pitch table (see module doc
# for why the rest is left to the engine's own pitch model).
#   "bat"       → batting approach       → multipliers
#   "bowl"      → bowling approach       → multipliers
#   "bat_phase" → (batting approach, phase) → multipliers
_PITCH_RULES = {
    # Flat: no pace to work with and no help off the deck, so the plans that
    # depend on beating the bat lose their teeth.
    "Flat": {"bowl": {"aggressive": _wkt(-0.20), "variation": _wkt(-0.20)}},
    "Dead": {"bowl": {"aggressive": _wkt(-0.20), "variation": _wkt(-0.20)}},
    # Dusty: spin strangles the nudgers; but the field is up in the powerplay
    # and the ball still comes on, so early aggression cashes in.
    "Dusty": {"bat": {"rotate": _runs(-1), "defensive": _runs(-1)},
              "bat_phase": {("aggressive", "powerplay"): _runs(+2)}},
    # Dry: the ball doesn't come on — swinging hard is a mug's game, nudging is
    # not. Cutters and slower balls grip.
    "Dry": {"bat": {"aggressive": _runs(-2), "rotate": _runs(+1)},
            "bowl": {"variation": _wkt(+0.10)}},
    # Green: seam does enough early that blocking and playing straight both
    # score less; once the shine goes the attackers cash in.
    "Green": {"bat_phase": {("defensive", "powerplay"): _runs(-1),
                            ("balanced", "powerplay"): _runs(-1),
                            ("aggressive", "middle"): _runs(+2),
                            ("aggressive", "death"): _runs(+2)}},
    "Even": {},
    # Hard: true bounce, the ball comes on — hitting works and blocking wastes it.
    "Hard": {"bat": {"aggressive": _runs(+2), "ultra": _runs(+2),
                     "defensive": _runs(-1)}},
    # Bouncy: top edges carry for six, blocking is awkward, bouncers bite.
    "Bouncy": {"bat": {"ultra": _runs(+1), "defensive": _runs(-1)},
               "bowl": {"aggressive": _wkt(+0.08)}},
}

# ── Section 7: phase × approach ──────────────────────────────────────
#   "bat"          → batting approach → multipliers
#   "bowl"         → bowling approach → multipliers
#   "bowl_any"     → applies to every bowling plan in the phase
#   "bat_vs_spin"  → batting approach → multipliers, spin bowling only
#   "bowl_spin"    → applies to every spin bowler in the phase
_PHASE_RULES = {
    # Two fielders out: the attacking intents get their value, and the bowling
    # side's strike rate suffers for the same reason.
    "powerplay": {"bat": {"aggressive": _runs(+1), "ultra": _runs(+1)},
                  "bowl_any": _wkt(-0.08)},
    # Spin, sweepers out, a single always available: the accumulator's phase,
    # and the worst phase to try to hit spin off the square.
    "middle": {"bat": {"rotate": _runs(+1)},
               "bat_vs_spin": {"aggressive": _runs(-1)},
               "bowl_spin": _runs(-0.5)},
    # Everyone swings; the yorker and the slower ball are what stop them.
    "death": {"bat": {"ultra": _runs(+3), "defensive": _runs(-2)},
              "bowl": {"variation": _wkt(+0.06), "aggressive": _runs(-1)}},
}

# ── Section 9: the "Read the Game" bonus ─────────────────────────────
# Which bowling plans each batting intent is *built* to beat (section 1's "Best
# against"). Picking the right intent for the plan you are actually facing —
# blind, since neither captain sees the other's pick — is the read paying off.
_READS = {
    "defensive": {"aggressive", "variation"},
    "rotate": {"defensive", "balanced"},
    "balanced": {"balanced", "mixed"},
    "aggressive": {"defensive", "balanced"},
    "ultra": {"defensive", "aggressive"},
}
READ_BONUS = 1.08          # batter read the plan → +8% runs
# Every other cell is the same coin the other way up: the batter walked into a
# plan their intent has no answer to, which is precisely the doc's "bowler
# outthinks the batter" (its own example — Aggressive premeditating into
# Variations — is one of these cells). Pricing both directions is also what
# keeps the mind game from being a standing gift to the batting side.
OUTTHINK_BONUS = _wkt(+0.10)

# ── Section 9: momentum, fatigue, partnership chemistry ──────────────
MOMENTUM_OVER_RUNS = 12            # 12+ in an over and the batter is flying
MOMENTUM_BAT = 1.05                # → +5% runs next over
MOMENTUM_BOWL = _wkt(+0.08)        # a wicket last over → +8% wicket chance

# A bowler's third over of the innings is where the legs go, and the fourth is
# worse. The engine never lets a T20 bowler send down back-to-back overs, so
# "consecutive" is read as how deep into their own spell this over is — which is
# the quantity the rule is really about, and the one that forces a captain to
# think about who is left.
FATIGUE = {
    3: {"Wicket": 0.95, "Dot": 0.98},
    4: _merge({"Wicket": 0.90, "Dot": 0.96}, _runs_factor(1.05)),
}
CHEMISTRY_OVERS = 4                # overs together before a pair is humming
CHEMISTRY_BAT = 1.03               # → +3% runs


def _validate_situational_tables():
    """A typo in any table above would be a silent no-op — fail at import."""
    def check(table, label):
        """Assert every key in one multiplier table is a real outcome."""
        unknown = set(table or {}) - _OUTCOME_KEYS
        assert not unknown, (f"{label} scales unknown outcome(s) "
                             f"{sorted(unknown)}")

    for pair, combo in SPECIAL_COMBOS.items():
        assert pair[0] in BATTING_KEYS and pair[1] in BOWLING_KEYS, (
            f"SPECIAL_COMBOS is keyed on a pair that does not exist: {pair}")
        check(combo.get("mult"), f"SPECIAL_COMBOS[{pair!r}]['mult']")
        check((combo.get("carry") or {}).get("mult"),
              f"SPECIAL_COMBOS[{pair!r}]['carry']['mult']")
        assert bool(combo.get("carry")) == bool(combo.get("carry_if")), (
            f"SPECIAL_COMBOS[{pair!r}] has a carry without a condition "
            f"(or the other way round)")
    assert set(MATRIX_FLAVOUR) == {(b, w) for b in BATTING_KEYS
                                   for w in BOWLING_KEYS}, (
        "MATRIX_FLAVOUR must cover every cell of the 5×5 matrix exactly")
    for pitch, rules in _PITCH_RULES.items():
        for slot in ("bat", "bowl"):
            keys = BATTING_KEYS if slot == "bat" else BOWLING_KEYS
            for key, table in (rules.get(slot) or {}).items():
                assert key in keys, f"_PITCH_RULES[{pitch!r}][{slot!r}]: {key!r}"
                check(table, f"_PITCH_RULES[{pitch!r}][{slot!r}][{key!r}]")
        for (key, ph), table in (rules.get("bat_phase") or {}).items():
            assert key in BATTING_KEYS and ph in PHASES, (
                f"_PITCH_RULES[{pitch!r}]['bat_phase']: {(key, ph)!r}")
            check(table, f"_PITCH_RULES[{pitch!r}]['bat_phase'][{(key, ph)!r}]")
    for ph, rules in _PHASE_RULES.items():
        assert ph in PHASES, f"_PHASE_RULES has an unknown phase {ph!r}"
        for slot in ("bat", "bat_vs_spin", "bowl"):
            keys = BOWLING_KEYS if slot == "bowl" else BATTING_KEYS
            for key, table in (rules.get(slot) or {}).items():
                assert key in keys, f"_PHASE_RULES[{ph!r}][{slot!r}]: {key!r}"
                check(table, f"_PHASE_RULES[{ph!r}][{slot!r}][{key!r}]")
        for slot in ("bowl_any", "bowl_spin"):
            check(rules.get(slot), f"_PHASE_RULES[{ph!r}][{slot!r}]")
    assert set(_READS) == BATTING_KEYS and all(
        v <= BOWLING_KEYS for v in _READS.values()), (
        "_READS must map every batting approach onto real bowling plans")
    assert all(v and v != BOWLING_KEYS for v in _READS.values()), (
        "_READS: an intent that reads every plan (or none) makes the mind game "
        "a constant, since the two branches are complements")
    check(OUTTHINK_BONUS, "OUTTHINK_BONUS")
    for over_no, table in FATIGUE.items():
        check(table, f"FATIGUE[{over_no!r}]")


_validate_situational_tables()


def make_context(pitch=None, phase=None, bowling_type=None,
                 bowler_over_index=0, partnership_overs=0.0,
                 batting_momentum=False, bowling_momentum=False,
                 carry=None, mind_games=True):
    """Build the situational context :func:`apply_approach_modifiers` reads.

    Every field is optional and a missing one simply switches its layer off, so
    a caller that knows only the pitch still gets the pitch rules.

    pitch              — pitch name as the engine spells it ("Hard", "Dusty"…).
    phase              — "powerplay" / "middle" / "death".
    bowling_type       — the bowler's style, for the spin-specific rules.
    bowler_over_index  — which over of this bowler's own spell this is (1-based).
    partnership_overs  — overs the current pair have batted together.
    batting_momentum   — the batting side took 12+ off the previous over.
    bowling_momentum   — this bowler took a wicket in their previous over.
    carry              — ``{"mult": {...}}`` handed over by the previous over's
                         special combination (see :func:`combo_carry`).
    mind_games         — False disables the read/outthink layer, for callers
                         whose picks are not simultaneous and hidden.
    """
    return {
        "pitch": pitch,
        "phase": phase if phase in PHASES else None,
        "bowling_type": bowling_type,
        "bowler_over_index": int(bowler_over_index or 0),
        "partnership_overs": float(partnership_overs or 0.0),
        "batting_momentum": bool(batting_momentum),
        "bowling_momentum": bool(bowling_momentum),
        "carry": carry or None,
        "mind_games": bool(mind_games),
    }


def special_combo(batting_approach, bowling_approach):
    """The named special combination for this pair, or None."""
    return SPECIAL_COMBOS.get((normalize_batting(batting_approach),
                               normalize_bowling(bowling_approach)))


def matrix_flavour(batting_approach, bowling_approach):
    """Section 3's one-line description of how this match-up tends to look."""
    return MATRIX_FLAVOUR.get((normalize_batting(batting_approach),
                               normalize_bowling(bowling_approach)), "")


def over_flavour(batting_approach, bowling_approach):
    """``(name, text)`` for the over just picked — the combination's name when
    the pair has one, else ``(None, matrix flavour)``."""
    combo = special_combo(batting_approach, bowling_approach)
    if combo:
        return combo["name"], combo["flavour"]
    return None, matrix_flavour(batting_approach, bowling_approach)


def combo_carry(batting_approach, bowling_approach, over_runs, over_wickets):
    """What this over's combination hands to the next one, or None.

    Call once the over is complete; feed the result back through
    ``make_context(carry=...)`` on the following over.
    """
    combo = special_combo(batting_approach, bowling_approach)
    if not combo or not combo.get("carry_if"):
        return None
    try:
        fired = bool(combo["carry_if"](int(over_runs), int(over_wickets)))
    except (TypeError, ValueError):
        return None
    if not fired:
        return None
    carry = dict(combo["carry"])
    carry["name"] = combo["name"]
    return carry


def _situational_multipliers(bat, bowl, context):
    """Layers 5-9: everything that depends on where and when the over is bowled."""
    tables = []

    pitch_rules = _PITCH_RULES.get(str(context.get("pitch") or ""), {})
    phase = context.get("phase")
    tables.append((pitch_rules.get("bat") or {}).get(bat))
    tables.append((pitch_rules.get("bowl") or {}).get(bowl))
    if phase:
        tables.append((pitch_rules.get("bat_phase") or {}).get((bat, phase)))
        phase_rules = _PHASE_RULES.get(phase, {})
        tables.append((phase_rules.get("bat") or {}).get(bat))
        tables.append((phase_rules.get("bowl") or {}).get(bowl))
        tables.append(phase_rules.get("bowl_any"))
        if _is_spin(context.get("bowling_type")):
            tables.append((phase_rules.get("bat_vs_spin") or {}).get(bat))
            tables.append(phase_rules.get("bowl_spin"))

    if context.get("mind_games", True):
        if bowl in _READS.get(bat, ()):
            tables.append(_runs_factor(READ_BONUS))
        else:
            tables.append(OUTTHINK_BONUS)

    if context.get("batting_momentum"):
        tables.append(_runs_factor(MOMENTUM_BAT))
    if context.get("bowling_momentum"):
        tables.append(MOMENTUM_BOWL)

    over_index = int(context.get("bowler_over_index") or 0)
    if over_index >= 3:
        tables.append(FATIGUE[min(over_index, max(FATIGUE))])

    if float(context.get("partnership_overs") or 0.0) >= CHEMISTRY_OVERS:
        tables.append(_runs_factor(CHEMISTRY_BAT))

    carry = context.get("carry") or {}
    tables.append(carry.get("mult"))

    return _merge(*tables)


def approach_multipliers(batting_approach=None, bowling_approach=None,
                         bowler_rating=None, context=None):
    """The full per-outcome multiplier for a match-up, as a dict.

    This is the single source of truth for what an approach pair *does* — the
    live over (via :func:`apply_approach_modifiers`) and the AI captain's
    payoff matrix (``services.bot_tactics``) both go through it, so the bot is
    always reasoning about the same over the player is about to face.
    """
    bat = normalize_batting(batting_approach)
    bowl = normalize_bowling(bowling_approach)

    bowl_tbl = dict(_BOWLING_MULT.get(bowl, {}))
    if bowl == "mixed":
        bowl_tbl["Six"] = _mixed_six_mult(bowler_rating)

    # The named combination belongs with the pair, not with the situation: two
    # captains who pick Ultra into Aggressive are in a shootout on any ground in
    # any over. So it applies whether or not a context was supplied, and the
    # base 5×5 balance measurements include it.
    combo = SPECIAL_COMBOS.get((bat, bowl)) or {}
    tables = [_BATTING_MULT.get(bat, {}), bowl_tbl, _COUNTER_MULT.get((bat, bowl)),
              combo.get("mult")]
    if context:
        tables.append(_situational_multipliers(bat, bowl, context))
    return _merge(*tables)


def normalize_batting(approach):
    a = (approach or "").strip().lower()
    return a if a in BATTING_KEYS else DEFAULT_BATTING


def normalize_bowling(approach):
    a = (approach or "").strip().lower()
    return a if a in BOWLING_KEYS else DEFAULT_BOWLING


def _mixed_six_mult(bowler_rating):
    """Mixed bowling suppresses sixes more when the bowler is highly rated
    ("reduces six chance if bowler rating is high"); weaker bowlers leak."""
    try:
        r = float(bowler_rating)
    except (TypeError, ValueError):
        return 0.70
    if r >= 80:
        return 0.55
    if r >= 65:
        return 0.70
    return 0.90


def apply_approach_modifiers(weights, batting_approach=None, bowling_approach=None,
                             bowler_rating=None, context=None):
    """Return a new weight dict scaled by both captains' approaches.

    weights: {outcome_key: float} as built inside calculate_outcome.
    batting_approach / bowling_approach: approach keys (normalized internally).
    bowler_rating: used for Mixed bowling's rating-scaled six suppression.
    context: optional :func:`make_context` dict switching on the situational
        layers (pitch, phase, mind game, momentum, fatigue, chemistry, carry).
        Without it only the three base layers apply, exactly as before.
    """
    mult = approach_multipliers(batting_approach, bowling_approach,
                                bowler_rating=bowler_rating, context=context)
    if not mult:
        return dict(weights)
    return {key: max(0.0, val * mult.get(key, 1.0))
            for key, val in weights.items()}


def batting_keyboard_rows(callback_prefix):
    """[(text, callback_data)] rows for the batting approach buttons."""
    return [(f"{emoji} {label}", f"{callback_prefix}{idx}")
            for idx, (_, emoji, label) in enumerate(BATTING_APPROACHES)]


def bowling_keyboard_rows(callback_prefix):
    """[(text, callback_data)] rows for the bowling approach buttons."""
    return [(f"{emoji} {label}", f"{callback_prefix}{idx}")
            for idx, (_, emoji, label) in enumerate(BOWLING_APPROACHES)]


def batting_label(key):
    key = normalize_batting(key)
    for k, emoji, label in BATTING_APPROACHES:
        if k == key:
            return f"{emoji} {label}"
    return key


def bowling_label(key):
    key = normalize_bowling(key)
    for k, emoji, label in BOWLING_APPROACHES:
        if k == key:
            return f"{emoji} {label}"
    return key
