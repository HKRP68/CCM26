"""First-innings projected score and second-innings score comparison.

Pure, read-only helpers shared by the Telegram approach card (both the HTML
``handlers.cipl_play._approach_card`` and the rich-text
``_build_approach_card_blocks``).  Nothing here mutates match state, consumes
match RNG, or touches player profiles/ratings — every function is a
deterministic read of the match state plus the calibrated reference tables in
``engine.format_config`` / ``engine.ground_config``.

Two public entry points:

* :func:`project_first_innings` — where a live first innings is heading, as an
  *expected* total and a *likely range*, informed by the format's par curve and
  the pitch's calibrated par band rather than a flat ``run_rate × overs``.
* :func:`compare_innings` — in a chase, how the two innings stack up at the same
  number of completed overs, read off the recorded over-by-over history.

Both are defensive: they return ``None`` (rather than raise) whenever the state
is not applicable or is missing the data they need, so a caller can simply skip
the section.  Projection ball-accounting mirrors ``services.cipl_match`` exactly
(6 legal balls to an over, 5 to a The-Hundred set) so the two never disagree.
"""

from __future__ import annotations

import math

# Legal balls per captain turn, mirroring services.cipl_match.FORMAT_SPECS so
# this module stays importable without pulling in the whole match engine.
_BALLS_PER_UNIT = {"T20": 6, "The100": 5}

# How much the historical par-curve estimate is trusted over a plain run-rate
# extrapolation.  The brief asks us to *prefer* the historical estimate, so the
# par model carries the majority weight; the rate anchor keeps a freak start
# from being blindly extrapolated by the curve.
_PAR_WEIGHT = 0.65

# Form (how far ahead/behind par the side is) is clamped to a sane band and
# only partly carried forward — an innings regresses toward par, and early form
# is the least reliable.
_FORM_MIN, _FORM_MAX = 0.65, 1.60

# Wicket adjustment (resources in hand vs a par-paced side) is likewise clamped.
_WKT_ADJ_MIN, _WKT_ADJ_MAX = 0.55, 1.25

# Above this many legal balls an innings uses the List-A (50-over) par curve;
# everything shorter (5/10/20-over, The Hundred) sits inside the T20 curve.
_LISTA_BALL_THRESHOLD = 250


# ── ball accounting (mirror of services.cipl_match) ──────────────────────────

def _bpu(state):
    return _BALLS_PER_UNIT.get((state or {}).get("ball_format"), 6)


def _total_balls(state):
    return int((state or {}).get("overs", 20) or 20) * _bpu(state)


def _balls_bowled(state):
    state = state or {}
    return (int(state.get("current_over", 1) or 1) - 1) * _bpu(state) \
        + int(state.get("current_ball", 0) or 0)


def _unit_word(state):
    return "set" if (state or {}).get("ball_format") == "The100" else "over"


# ── calibrated reference data ────────────────────────────────────────────────

def _format_config(innings_balls):
    """The engine FormatConfig whose par curve best fits this innings length."""
    try:
        from engine import format_config
    except Exception:
        return None
    name = "ListA" if innings_balls >= _LISTA_BALL_THRESHOLD else "T20"
    try:
        return format_config.get_format(name)
    except Exception:
        return None


def _par_at(curve, native_overs, over_pos):
    """Cumulative par runs at ``over_pos`` (fractional overs) on ``curve``.

    ``curve`` is the FormatConfig ``par_scores`` dict keyed by integer over.
    Linear interpolation between the bracketing whole overs, clamped to the
    curve's range.
    """
    over_pos = max(0.0, min(float(native_overs), float(over_pos)))
    lo = int(over_pos)
    hi = min(native_overs, lo + 1)
    base = curve[lo]
    return base + (curve[hi] - curve[lo]) * (over_pos - lo)


def _pitch_band(pitch):
    """The pitch's calibrated ``scoring_dynamics`` dict, or ``None``.

    Shape ``{floor, par_low, par_high, ceiling, anomaly}`` — a full-innings
    (20-over) first-innings distribution on that surface.
    """
    if not pitch:
        return None
    try:
        from engine import ground_config
        band = ground_config.get_scoring_dynamics(pitch)
    except Exception:
        return None
    if not band:
        return None
    try:
        return {k: float(band[k]) for k in ("floor", "par_low", "par_high",
                                            "ceiling", "anomaly") if k in band}
    except Exception:
        return None


def _par_wickets(frac, wicket_limit):
    """Wickets a par-paced side has typically lost ``frac`` of the way in.

    A gently convex curve: few early, accelerating through the innings.  Used
    only to judge whether the batting side is *ahead of or behind* par on
    resources, never to project a score directly.
    """
    return max(0.0, (wicket_limit - 1) * (max(0.0, min(1.0, frac)) ** 1.6) * 0.75)


def _resource_ratio(rem_overs, wkts, exp_wkts, total_overs):
    """DLS resources in hand vs a par-paced side's at the same stage.

    ``> 1`` when fewer wickets have fallen than par (more batting resources
    left → license to score more), ``< 1`` after a collapse.  Falls back to a
    neutral ``1.0`` whenever the DLS helper is unavailable.
    """
    try:
        from engine import dls
    except Exception:
        return 1.0
    try:
        actual = dls.resource_pct(rem_overs, int(round(wkts)), total_overs)
        par = dls.resource_pct(rem_overs, int(round(exp_wkts)), total_overs)
    except Exception:
        return 1.0
    if par <= 0:
        return 1.0
    return actual / par


# ── first-innings projection ─────────────────────────────────────────────────

def project_first_innings(state):
    """Project where a live first innings is heading.

    Returns a dict, or ``None`` when projection does not apply (not the first
    innings, or no ball bowled yet).

    Dict shape::

        {
            "runs": int, "wickets": int,        # the live score
            "expected": int,                    # projected final total
            "low": int | None, "high": int | None,   # likely range (None → omit)
            "approximate": bool,                # True → rate-only fallback
            "complete": bool,                   # innings already finished/all-out
        }

    * ``expected`` is never below the current score.
    * When the innings is all out or has used every ball, ``complete`` is True
      and ``expected`` equals the actual total (nothing left to project).
    * When the format par curve is unavailable the estimate falls back to a
      plain run-rate extrapolation, ``approximate`` is set, and **no** range is
      returned (we do not invent a confidence interval).
    * A likely range is returned only when the pitch's calibrated par band is
      available; it is a model-based band from that calibrated data widened for
      the balls still to be bowled, not a statistical confidence interval.
    """
    if not state or int(state.get("innings", 1) or 1) != 1:
        return None

    runs = int(state.get("total_runs", 0) or 0)
    wkts = int(state.get("total_wickets", 0) or 0)
    wicket_limit = int(state.get("wicket_limit", 10) or 10)
    innings_balls = _total_balls(state)
    bowled = _balls_bowled(state)
    remaining = max(0, innings_balls - bowled)

    # All out, or the innings has run its full distance: nothing to project.
    if wkts >= wicket_limit or remaining <= 0:
        return {"runs": runs, "wickets": wkts, "expected": runs,
                "low": runs, "high": runs, "approximate": False,
                "complete": True}

    # Nothing to go on before the first legal ball.
    if bowled <= 0:
        return None

    if state.get("ball_format") == "ODI":
        from services import odi_approach
        return {"runs": runs, "wickets": wkts, "expected": odi_approach.forecast(state),
                "low": None, "high": None, "approximate": True, "complete": False}

    fmt = _format_config(innings_balls)
    curve = getattr(fmt, "par_scores", None) if fmt else None
    native = getattr(fmt, "overs", None) if fmt else None
    factor = 1.0
    if fmt is not None:
        factor = fmt.pitch_par_factors.get(state.get("pitch_type"), 1.0)

    # Plain run-rate extrapolation — the anchor, and the whole story when there
    # is no calibrated curve to lean on.
    rate_total = runs + (runs / bowled) * remaining

    if not curve or not native:
        expected = max(runs, int(round(rate_total)))
        return {"runs": runs, "wickets": wkts, "expected": expected,
                "low": None, "high": None, "approximate": True,
                "complete": False}

    over_pos_now = bowled / 6.0
    over_pos_end = innings_balls / 6.0
    par_now = _par_at(curve, native, over_pos_now) * factor
    par_end = _par_at(curve, native, over_pos_end) * factor
    full_par = curve[native] * factor  # this pitch's full-length par total

    if par_now <= 0 or par_end <= par_now:
        # Degenerate curve read — fall back to the rate anchor.
        expected = max(runs, int(round(rate_total)))
        return {"runs": runs, "wickets": wkts, "expected": expected,
                "low": None, "high": None, "approximate": True,
                "complete": False}

    remaining_par = par_end - par_now

    # Form: tracking vs par so far, clamped and only partly carried forward.
    frac_bowled = bowled / innings_balls
    form = max(_FORM_MIN, min(_FORM_MAX, runs / par_now))
    carry = 0.35 + 0.45 * frac_bowled          # 0.35 early → 0.80 late
    form_fwd = 1.0 + (form - 1.0) * carry

    # Resources: wickets in hand vs a par-paced side at this stage.
    rem_overs = remaining / 6.0
    total_overs = innings_balls / 6.0
    exp_wkts = _par_wickets(frac_bowled, wicket_limit)
    wicket_adj = max(_WKT_ADJ_MIN, min(_WKT_ADJ_MAX,
                                       _resource_ratio(rem_overs, wkts,
                                                       exp_wkts, total_overs)))

    par_total = runs + remaining_par * form_fwd * wicket_adj
    expected_total = _PAR_WEIGHT * par_total + (1.0 - _PAR_WEIGHT) * rate_total

    band = _pitch_band(state.get("pitch_type"))
    length_scale = (par_end / full_par) if full_par else 1.0

    # Soft ceiling: do not project past a scaled freak-max for the surface.
    if band and "ceiling" in band:
        ceiling_scaled = band["ceiling"] * length_scale
        expected_total = min(expected_total, ceiling_scaled)

    expected = max(runs, int(round(expected_total)))
    expected_remaining = max(0.0, expected_total - runs)

    low = high = None
    if band and "par_low" in band and "par_high" in band:
        rem_frac = remaining / innings_balls
        # Half-width of the likely range: the pitch's calibrated par-band
        # half-spread (scaled to this innings' length and tapered as balls run
        # out) plus the natural spread of the runs still to be scored.  This is
        # a model band from calibrated data, not a statistical CI.
        band_half = (band["par_high"] - band["par_low"]) / 2.0 * length_scale
        half = band_half * (0.4 + 0.6 * rem_frac) \
            + 0.12 * expected_remaining * math.sqrt(rem_frac)
        low = max(runs, int(round(expected - half)))
        high = int(round(expected + half))
        # Clamp the range top at the surface's scaled freak-max so it can never
        # imply an impossible total; the anomaly (not the softer ceiling) is the
        # right bound for the *top* of a range.
        cap = band.get("anomaly") or band.get("ceiling")
        if cap:
            high = min(high, int(round(cap * length_scale)))
        high = max(high, expected)      # keep the band coherent
        low = min(low, expected)

    return {"runs": runs, "wickets": wkts, "expected": expected,
            "low": low, "high": high, "approximate": False,
            "complete": False}


# ── second-innings comparison ────────────────────────────────────────────────

def _ceil_div(a, b):
    return -(-int(a) // int(b)) if b else 0


def _wkts_from_fow(fow, stage, bpu):
    """Best-effort wickets fallen by the end of over ``stage`` from a fall-of-
    wickets list.  Returns ``None`` when the list can't be parsed.

    Each entry is ``[runs, wickets, name, overs_str]`` where ``overs_str`` is
    what ``cipl_match.format_overs`` produced ("12.3", or "63 balls" for The
    Hundred).  Only a best-effort fallback for matches recorded before
    per-over wicket tracking existed; the tracked path is exact.

    ``None`` fow (the key is absent) means *unknown* and returns ``None`` so the
    caller shows runs only; an empty list means no wickets fell and returns 0.
    """
    if fow is None:
        return None
    if not fow:
        return 0
    n = 0
    for entry in fow:
        try:
            ov = str(entry[3]).strip()
        except (IndexError, TypeError):
            return None
        try:
            if "balls" in ov:
                balls = int(ov.split()[0])
            elif "." in ov:
                whole, part = ov.split(".")
                balls = int(whole) * bpu + int(part)
            else:
                balls = int(ov) * bpu
        except (ValueError, TypeError):
            return None
        over_idx = _ceil_div(balls, bpu)
        if 1 <= over_idx <= stage:
            n += 1
    return n


def _wickets_at(state, which, stage, bpu):
    """Cumulative wickets at the end of completed over ``stage`` for an innings.

    Prefers the exact per-over wicket snapshots (``over_wkts`` /
    ``inn1_over_wkts``); falls back to the fall-of-wickets list; returns
    ``None`` when neither is usable (caller then renders runs only).
    """
    key = "inn1_over_wkts" if which == "inn1" else "over_wkts"
    lst = state.get(key)
    if isinstance(lst, list):
        return sum(int(w or 0) for w in lst[:stage])
    fow_key = "inn1_fow" if which == "inn1" else "fow"
    return _wkts_from_fow(state.get(fow_key), stage, bpu)


def _completed_units(balls, bpu):
    return balls // bpu if bpu else 0


def compare_innings(state, *, last_n=3):
    """Compare both innings at the same number of completed overs, for a chase.

    Returns a dict, or ``None`` when not applicable (not the second innings, or
    no first-innings over history recorded).

    Dict shape::

        {
            "stage": int,                 # completed overs compared (mutual)
            "unit_word": "over" | "set",
            "inn1": {"name": str, "runs": int, "wkts": int | None},
            "inn2": {"name": str, "runs": int, "wkts": int | None},
            "diff": int,                  # inn2 − inn1 (+ ahead, − behind)
            "chasing": str, "defending": str,
            "last": {"n": int, "inn1": int, "inn2": int} | None,
        }

    Scores are summed from the recorded per-over run history — never estimated
    and never projected beyond the first innings' actual duration.  Only whole,
    mutually completed overs are compared; a partial over in progress is left
    out until it completes.
    """
    if not state or int(state.get("innings", 0) or 0) != 2:
        return None

    bpu = _bpu(state)
    inn1_over_runs = list(state.get("inn1_over_runs") or [])
    inn2_over_runs = list(state.get("over_runs") or [])
    if not inn1_over_runs:
        return None

    # Completed whole overs in each innings.  ``over_runs`` can carry a final
    # partial over (an all-out mid-over), so cap by the ball count too.
    inn1_balls = state.get("inn1_balls")
    if inn1_balls is None:
        inn1_full = len(inn1_over_runs)
    else:
        inn1_full = min(len(inn1_over_runs), _completed_units(int(inn1_balls), bpu))
    inn2_full = min(len(inn2_over_runs), _completed_units(_balls_bowled(state), bpu))

    stage = min(inn1_full, inn2_full)
    if stage <= 0:
        return None

    inn1_runs = sum(int(r or 0) for r in inn1_over_runs[:stage])
    inn2_runs = sum(int(r or 0) for r in inn2_over_runs[:stage])
    inn1_wkts = _wickets_at(state, "inn1", stage, bpu)
    inn2_wkts = _wickets_at(state, "inn2", stage, bpu)

    defending = (state.get("inn1_team") or state.get("inn1_bat_team")
                 or state.get("bowl_team_name") or "Team 1")
    chasing = state.get("bat_team_name") or "Team 2"

    last = None
    if stage >= last_n:
        last = {
            "n": last_n,
            "inn1": sum(int(r or 0) for r in inn1_over_runs[stage - last_n:stage]),
            "inn2": sum(int(r or 0) for r in inn2_over_runs[stage - last_n:stage]),
        }

    return {
        "stage": stage,
        "unit_word": _unit_word(state),
        "inn1": {"name": str(defending), "runs": inn1_runs, "wkts": inn1_wkts},
        "inn2": {"name": str(chasing), "runs": inn2_runs, "wkts": inn2_wkts},
        "diff": inn2_runs - inn1_runs,
        "chasing": str(chasing),
        "defending": str(defending),
        "last": last,
    }


# ── live match intelligence ──────────────────────────────────────────────────

def _calibrated_par_total(state):
    """The calibrated expected first-innings total for this pitch & format,
    scaled to the match's over count.  Returns ``(par, approximate)`` — ``par``
    is ``None`` when no calibrated curve is available.

    Drawn from the format par curve (``engine.format_config``) at the match's
    full length, times the pitch's par factor; the pitch's calibrated par band
    (``scoring_dynamics``) refines it to the band midpoint when present.  No
    historical average is invented: without calibrated data this returns
    ``(None, True)`` and the caller labels any fallback approximate.
    """
    if state.get("ball_format") == "ODI":
        from services import odi_approach
        from engine.realism import odi
        ctx = odi_approach.context(state)
        return round(odi_approach.mean_remaining(
            ctx.innings_balls, 10, ctx, odi.settings(state))), True
    innings_balls = _total_balls(state)
    fmt = _format_config(innings_balls)
    curve = getattr(fmt, "par_scores", None) if fmt else None
    native = getattr(fmt, "overs", None) if fmt else None
    if not curve or not native:
        return None, True
    factor = fmt.pitch_par_factors.get(state.get("pitch_type"), 1.0)
    over_pos_end = innings_balls / 6.0
    par = _par_at(curve, native, over_pos_end) * factor
    full_par = curve[native] * factor
    band = _pitch_band(state.get("pitch_type"))
    approximate = band is None
    if band and "par_low" in band and "par_high" in band and full_par:
        # Refine with the pitch's calibrated band midpoint, scaled to length.
        length_scale = par / full_par if full_par else 1.0
        par = (band["par_low"] + band["par_high"]) / 2.0 * length_scale
    return int(round(par)), approximate


def par_outlook(state):
    """First-innings par score, projection and outlook for the live card.

    Returns ``None`` outside a live first innings (or before a ball is bowled).
    Dict shape::

        {"par": int, "projected": int,
         "outlook": "Above Par" | "On Par" | "Below Par",
         "approximate": bool}

    ``outlook`` compares the projection against the calibrated par total, so it
    reads the same way the two numbers shown do.  ``approximate`` is True when
    the projection or the par figure fell back off calibrated data.
    """
    proj = project_first_innings(state)
    if not proj or proj.get("complete"):
        return None
    par, par_approx = _calibrated_par_total(state)
    if par is None:
        return None
    projected = proj["expected"]
    thr = max(4, int(round(0.03 * par)))
    if projected > par + thr:
        outlook = "Above Par"
    elif projected < par - thr:
        outlook = "Below Par"
    else:
        outlook = "On Par"
    return {"par": par, "projected": projected, "outlook": outlook,
            "approximate": bool(proj.get("approximate") or par_approx)}


def rrr_trend(state, *, back=3):
    """How the required run rate has moved over the last ``back`` completed overs.

    Second innings only.  Returns ``None`` when there is no live chase or fewer
    than ``back`` completed overs exist (nothing to trend).  Dict shape::

        {"then": float, "now": float, "delta": float,
         "direction": "rising" | "falling" | "flat", "overs": int}

    ``direction`` describes chase pressure: a required rate that has climbed is
    ``rising`` (pressure building on the chasing side); one that has dropped is
    ``falling`` (pressure easing).  All figures come from the recorded over
    history and the real target — nothing is invented.
    """
    if not state or int(state.get("innings", 0) or 0) != 2:
        return None
    target = state.get("target")
    if not target:
        return None
    bpu = _bpu(state)
    innings_balls = _total_balls(state)
    over_runs = list(state.get("over_runs") or [])
    completed = min(len(over_runs), _balls_bowled(state) // bpu)
    if completed < back:
        return None

    def _rrr(runs_scored, balls_bowled_then):
        runs_needed = int(target) - int(runs_scored)
        balls_left = innings_balls - balls_bowled_then
        if balls_left <= 0:
            return None
        return max(0.0, runs_needed * 6.0 / balls_left)

    now = _rrr(sum(int(r or 0) for r in over_runs[:completed]), completed * bpu)
    then_stage = completed - back
    then = _rrr(sum(int(r or 0) for r in over_runs[:then_stage]), then_stage * bpu)
    if now is None or then is None:
        return None
    delta = now - then
    direction = "rising" if delta > 0.05 else "falling" if delta < -0.05 else "flat"
    return {"then": round(then, 2), "now": round(now, 2),
            "delta": round(delta, 2), "direction": direction, "overs": back}


def chase_win_chance(state):
    """The chasing side's win probability (%) from CCM26's existing chase model,
    or ``None`` when no valid live estimate applies.

    A thin read-only wrapper over :func:`services.cipl_match.chase_chance_now`
    (the same model that steers the simulation), so this never computes a second
    opinion and never influences the result.
    """
    if not state or int(state.get("innings", 0) or 0) != 2:
        return None
    try:
        from services import cipl_match
        info = cipl_match.chase_chance_now(state)
    except Exception:
        return None
    if not info:
        return None
    pct = info.get("chasing_chance")
    if pct is None:
        return None
    try:
        return int(round(pct))
    except (TypeError, ValueError):
        return None
