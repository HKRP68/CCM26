"""Match drama for Challenge League (/cipl) and /letsplay.

Four per-ball layers, all built as ``weight_hook``s for
``engine.ball_outcome.calculate_outcome`` and composed by
``services.cipl_match.simulate_over``:

  * **Pitch character** — what a surface *does* in each phase, on top of the
    YAML scoring matrix: the green top seams early and burns off, the dust
    turns square through the middle, the dry deck cracks late, the bouncy one
    rewards real pace. The matrix sets how many runs a pitch is worth; this
    makes the pitch feel like its name.
  * **Score ceiling** — a brake that grows as an innings projects past the
    top of the pitch's par band and turns steep past 240 (or 215 actually on
    the board), pulling the ball back toward normal cricket — so 270+ is a
    once-in-a-season freak rather than a Flat-pitch habit.
  * **Contest** — a mild fight-back when one side is running away with it, so
    the bowling side tightens up instead of the match going dead.
  * **Pressure** — at the business end, the side under the pump makes mistakes.
    A fielding side spills catches, sprays wides and oversteps; a batting side
    freezes, runs itself out and plays the nervous heave.

Every layer is a bounded multiplier and returns ``None`` when it has nothing
to say, so a quiet ball is untouched. Nothing here is used by /sim, /playmatch
or the bot matches.

Lines for the commentary live in ``data/drama_pack.json``.
"""

from __future__ import annotations

import json
import logging
import os
import random

from engine import ground_config

logger = logging.getLogger(__name__)

_PACK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "drama_pack.json")
_PACK = None


def _pack():
    global _PACK
    if _PACK is None:
        try:
            with open(_PACK_PATH, encoding="utf-8") as fh:
                _PACK = json.load(fh)
        except Exception:
            logger.exception("drama pack unavailable; using built-in lines only")
            _PACK = {}
    return _PACK


def line(key, sub=None, **fields):
    """A random line from the drama pack (``key`` or ``key.sub``), filled with
    *fields*. ``None`` when the pack has no such group."""
    group = _pack().get(key)
    if isinstance(group, dict):
        group = group.get(sub) if sub else None
    if not group:
        return None
    text = random.choice(group)
    for k, v in fields.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _scale(mult):
    """A hook that multiplies each named outcome weight — None when neutral."""
    mult = {k: v for k, v in mult.items() if abs(v - 1.0) > 1e-6}
    if not mult:
        return None

    def _hook(raw_weights):
        rw = dict(raw_weights)
        for key, factor in mult.items():
            if key in rw:
                rw[key] *= factor
        return rw

    return _hook


def is_spin(bowling_type):
    return "spin" in str(bowling_type or "").lower()


def is_express(bowling_type):
    return str(bowling_type or "") in ("Fast", "Fast-medium")


# ════════════════════════════════════════════════════════════════════
# 1. Pitch character
# ════════════════════════════════════════════════════════════════════

def pitch_character(pitch, phase, innings, bowling_type, over_frac):
    """``(multipliers, strength)`` for this surface, phase and bowler.

    ``over_frac`` is how far through the innings the ball is (0..1);
    ``strength`` (0..1) is how much the surface is *doing* on this ball, which
    the caller uses to decide whether the commentary should mention it.
    """
    spin = is_spin(bowling_type)
    pace = not spin
    m = {}
    strength = 0.0
    if pitch == "Green":
        # The grass and the shine: full strength in the powerplay, gone by the
        # middle of the innings, and a lot less of it for the chase.
        fade = _clamp(1.0 - over_frac / 0.5)
        if innings == 2:
            fade *= 0.6
        if pace:
            m = {"Wicket": 1.0 + 0.25 * fade, "Four": 1.0 + 0.08 * fade,
                 "Single": 1.0 - 0.06 * fade}
            strength = fade
        else:
            m = {"Wicket": 0.85}
    elif pitch == "Dusty":
        if phase in ("middle", "death"):
            if spin:
                m = {"Wicket": 1.25, "Dot": 1.10, "Six": 0.80}
                strength = 1.0
            else:
                m = {"Wicket": 0.90, "Dot": 1.04}
        elif spin:
            m = {"Wicket": 1.10, "Dot": 1.05}
            strength = 0.5
    elif pitch == "Dry":
        if phase == "middle" and spin:
            m = {"Wicket": 1.12, "Dot": 1.08, "Six": 0.85}
            strength = 0.8
        elif phase == "death":
            # The cracks open: variable bounce for everyone.
            m = {"Wicket": 1.15, "Dot": 1.04}
            strength = 0.7
    elif pitch == "Bouncy":
        settle = 0.6 if innings == 2 else 1.0
        if is_express(bowling_type):
            m = {"Wicket": 1.0 + 0.20 * settle, "Six": 1.0 + 0.08 * settle}
            strength = settle
        elif spin:
            m = {"Wicket": 0.90}
    elif pitch == "Hard":
        m = {"Wicket": 0.92} if spin else {"Wicket": 1.08, "Four": 1.05}
        strength = 0.0 if spin else 0.4
    elif pitch in ("Flat", "Dead"):
        # Nothing in it — the bowler's only currency is change of pace.
        if spin or bowling_type == "Medium-fast":
            m = {"Dot": 1.05}
    return m, strength


def make_pitch_character_hook(pitch, phase, innings, bowling_type, over_frac):
    mult, strength = pitch_character(pitch, phase, innings, bowling_type, over_frac)
    return _scale(mult), strength


def pitch_flavour(pitch, strength, oc):
    """Occasionally a line about the surface after a dot or a wicket."""
    if strength < 0.5:
        return None
    is_wkt = bool(oc.get("batter_out") or oc.get("type") == "wicket")
    is_dot = (not is_wkt and not oc.get("is_extra") and not oc.get("runs"))
    if not (is_wkt or is_dot):
        return None
    if random.random() >= (0.35 if is_wkt else 0.12):
        return None
    return line("pitch_flavour", pitch)


# ════════════════════════════════════════════════════════════════════
# 2. Score ceiling (the 270+ brake)
# ════════════════════════════════════════════════════════════════════

#: Above this projected total the brake turns steep, whatever the pitch.
HARD_LINE = 240
HARD_SPAN = 30          # fully engaged at HARD_LINE + HARD_SPAN
SOFT_SPAN = 45          # soft brake fully engaged this far over the par band
RUNS_LINE = 215         # actual runs from which the hard brake also engages
RUNS_SPAN = 45
CEILING_FROM_BALLS = 24  # projections are noise before the 5th over


def _par_share(frac):
    """Share of a par total a T20 innings has usually scored ``frac`` of the
    way through — read off the neutral par curve, so the death overs' surge
    is priced in rather than assumed away."""
    try:
        from engine import format_config
        curve = format_config.get_format("T20").par_scores
    except Exception:       # pragma: no cover - format_config always ships
        return frac
    overs = max(curve)
    full = float(curve[overs]) or 1.0
    x = max(0.0, min(1.0, frac)) * overs
    lo = int(x)
    hi = min(overs, lo + 1)
    v = curve[lo] + (curve[hi] - curve[lo]) * (x - lo)
    return v / full


def projected_total(runs, bowled, innings_balls, par=None):
    """Where the innings is heading.

    With a pitch *par*, the balls still to come are worth what they usually
    are on that surface — the death surge included — scaled by how far ahead
    of or behind par the innings already is. Without one, the current rate.
    """
    if bowled <= 0:
        return float(runs)
    left = max(0, innings_balls - bowled)
    if not par:
        return runs + runs / bowled * left
    share = _par_share(bowled / float(innings_balls))
    if share <= 0:
        return float(runs)
    form = max(0.7, min(1.4, runs / (share * par)))
    return runs + (1.0 - share) * par * form


def make_score_ceiling_hook(pitch, runs, bowled, innings_balls, innings=1,
                            target=None):
    """Damp boundaries and lift dots/wickets once the innings projects past the
    top of the pitch's par band, steeply past :data:`HARD_LINE`. None while it
    doesn't."""
    if bowled < CEILING_FROM_BALLS or bowled >= innings_balls:
        return None
    if innings == 2 and target and target < HARD_LINE:
        # A chase stops at its target; the brake only matters for a mammoth one.
        return None
    dyn = ground_config.get_scoring_dynamics(pitch) or {}
    # The soft brake starts at the top of the par band and is fully on by the
    # Ceiling-and-beyond: a big total is still possible, a freak one is not.
    top = float(dyn.get("par_high") or 200)
    par = (float(dyn.get("par_low") or top) + top) / 2.0
    proj = projected_total(runs, bowled, innings_balls, par)
    soft = _clamp((proj - top) / SOFT_SPAN)
    hard = _clamp((proj - HARD_LINE) / HARD_SPAN)
    if innings == 2:
        soft = 0.0           # only the freak-score brake applies to a chase
    # Runs already on the board, late on: nothing more for free. This is what
    # catches the 150-off-14 innings that would otherwise take 120 off the
    # last six overs.
    if runs >= RUNS_LINE:
        hard = max(hard, _clamp((runs - RUNS_LINE) / RUNS_SPAN))
    if soft <= 0 and hard <= 0:
        return None
    alpha = _clamp(SOFT_PULL * soft + HARD_PULL * hard, 0.0, MAX_PULL)
    return _blend_hook(alpha)


#: A sane T20 ball — what a set batter facing a decent bowler actually gets.
#: The brake pulls a runaway innings' weights toward this rather than scaling
#: them: by the last over the engine can be offering ~90% boundaries and no
#: wicket at all, and multiplying a near-zero Dot or Wicket weight does nothing.
SANE_BALL = {"Dot": 0.30, "Single": 0.36, "Double": 0.08, "Three": 0.005,
             "Four": 0.115, "Six": 0.055, "Wicket": 0.065, "Extras": 0.02}
SOFT_PULL = 0.30
HARD_PULL = 0.60
MAX_PULL = 0.75


def _blend_hook(alpha):
    if alpha <= 0:
        return None

    def _hook(raw_weights):
        total = sum(raw_weights.values())
        if total <= 0:
            return raw_weights
        keys = [k for k in raw_weights if k in SANE_BALL]
        sane_total = sum(SANE_BALL[k] for k in keys) or 1.0
        rw = dict(raw_weights)
        for k in keys:
            rw[k] = total * ((1.0 - alpha) * raw_weights[k] / total
                             + alpha * SANE_BALL[k] / sane_total)
        return rw

    return _hook


# ════════════════════════════════════════════════════════════════════
# 3. Contest — no runaway matches
# ════════════════════════════════════════════════════════════════════

CONTEST_FROM_BALLS = 36


def make_contest_hook(pitch, innings, runs, wickets, bowled, innings_balls,
                      target=None, chase_active=False):
    """A mild tightening from the fielding side when the batting side is
    running away with the match. Bounded at ~10%, so a genuinely better side
    still wins — just not by a street."""
    if bowled < CONTEST_FROM_BALLS or bowled >= innings_balls or chase_active:
        return None
    frac = bowled / float(innings_balls)
    if innings == 1:
        dyn = ground_config.get_scoring_dynamics(pitch) or {}
        par = ((dyn.get("par_low") or 180) + (dyn.get("par_high") or 195)) / 2.0
        ahead = runs - par * frac
        if wickets > 4 or ahead < 25:
            return None
        s = _clamp((ahead - 25) / 40.0)
        return _scale({"Dot": 1.0 + 0.06 * s, "Wicket": 1.0 + 0.09 * s})
    if not target:
        return None
    ahead = runs - target * frac
    if wickets > 3 or ahead < 20:
        return None
    s = _clamp((ahead - 20) / 35.0)
    return _scale({"Dot": 1.0 + 0.05 * s, "Wicket": 1.0 + 0.10 * s})


# ════════════════════════════════════════════════════════════════════
# 4. Pressure mistakes
# ════════════════════════════════════════════════════════════════════

#: Below this a side is not "under pressure" and nothing changes.
PRESSURE_FLOOR = 0.25


def bowl_pressure(phase, innings, target, runs, wickets, balls_left,
                  over_runs, wicket_limit=10):
    """0..1: how much the FIELDING side is feeling it on this ball."""
    p = 0.0
    if phase == "death":
        p += 0.30
        if balls_left <= 12:
            p += 0.10
    if innings == 2 and target and balls_left > 0:
        need = target - runs
        wkts_left = wicket_limit - wickets
        if need > 0 and wkts_left >= 2 and balls_left <= 36:
            rr = need / balls_left * 6.0
            # A chase that is genuinely on is when hands start to shake.
            close = _clamp(1.0 - abs(rr - 9.5) / 5.0)
            p += 0.40 * close
            if balls_left <= 12:
                p += 0.15 * close
    if over_runs >= 10:
        p += 0.15
    return _clamp(p)


def bat_pressure(phase, innings, target, runs, wickets, balls_left,
                 recent_wickets, wicket_limit=10):
    """0..1: how much the BATTING side is feeling it on this ball."""
    p = 0.0
    if innings == 2 and target and balls_left > 0:
        need = target - runs
        if need > 0:
            rr = need / balls_left * 6.0
            if rr >= 10 and balls_left <= 48:
                p += 0.30 + 0.40 * _clamp((rr - 10) / 8.0)
            if balls_left <= 12 and need > 0:
                p += 0.15
    if recent_wickets >= 2:
        p += 0.25
    if phase == "death" and wickets >= 6:
        p += 0.15
    if wicket_limit - wickets <= 2:
        p += 0.10
    return _clamp(p)


def bowl_pressure_effects(p):
    """``(hook, engine_kwargs)`` for a fielding side under pressure *p*."""
    if p < PRESSURE_FLOOR:
        return None, {}
    kwargs = {
        "drop_mult": 1.0 + 1.2 * p,
        "misfield_mult": 1.0 + 0.8 * p,
        "extras_split": [0.40 + 0.10 * p, 0.25 + 0.10 * p,
                         max(0.05, 0.20 - 0.10 * p), max(0.05, 0.15 - 0.10 * p)],
    }
    return _scale({"Extras": 1.0 + 1.3 * p}), kwargs


def make_bat_pressure_hook(p):
    if p < PRESSURE_FLOOR:
        return None
    return _scale({"Dot": 1.0 + 0.08 * p, "Single": 1.0 - 0.08 * p,
                   "Wicket": 1.0 + 0.15 * p})


def maybe_mixup(oc, p, free_hit=False):
    """Turn a Caught/Bowled wicket into a run-out mix-up under batting pressure.

    Mutates and returns *oc*. The engine's run-out convention is one run
    completed, out going for the second.
    """
    if p < PRESSURE_FLOOR or free_hit:
        return oc
    if not (oc.get("batter_out") or oc.get("type") == "wicket"):
        return oc
    if oc.get("wicket_type") not in ("Caught", "Bowled"):
        return oc
    if random.random() < 0.20 * p:
        oc["wicket_type"] = "Run Out"
        oc["runs"] = 1
        oc["mixup"] = True
    return oc
