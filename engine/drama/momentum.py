"""Bounded match momentum (Codex Prompt 05, §8).

Momentum creates continuity — a side that has just cut loose stays slightly on
top for a few balls — WITHOUT ever becoming unstoppable. It is a single scalar
in [-max, +max] held in the active match state, recomputed AFTER every legal
delivery from a rolling window, and never used to alter the same delivery that
produced it.

The residual compares what actually happened in the window to what the
pre-delivery probabilities expected, in units of its own standard deviation, so
a flurry of boundaries against easy bowling (high expectation) moves momentum
less than the same flurry against good bowling::

    R  = actual runs in the window
    ER = expected runs in the window (from each ball's pre-delivery probs)
    W  = actual wickets in the window
    EW = expected wickets in the window
    VR = expected run variance ;  VW = expected wicket variance
    residual      = (R - ER - 4*(W - EW)) / sqrt(max(VR + 16*VW, 4))
    residual      = clamp(residual, -3, 3)
    momentum_next = clamp(0.80*momentum_prev + 0.20*tanh(residual/2), -0.75, +0.75)

Positive momentum mildly favours batting, negative mildly favours bowling,
through small LOG adjustments to the outcome weights — which the global drama
budget (modifiers.py) then caps together with clutch and rare-event effects, so
the two never multiply out of control.
"""

import math

# Run value of each scoring bucket. Extras is treated as ~1 run for the
# expectation; it is a bowler-error bucket, not a batting shot.
_RUNS = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6,
         "Wicket": 0, "Extras": 1}

# Per-outcome LOG adjustment per unit of momentum (§8). Unlisted buckets
# (Extras) stay neutral.
_MOMENTUM_LOG = {
    "Dot": -0.035, "Single": 0.020, "Double": 0.025,
    "Four": 0.045, "Six": 0.050, "Wicket": -0.025,
}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def expected_from_probs(pre_probs):
    """(expected_runs, expected_wickets, var_runs, var_wickets) for ONE delivery
    from its pre-delivery outcome probabilities. Probabilities are normalised
    defensively."""
    total = sum(max(0.0, float(v)) for v in pre_probs.values())
    if total <= 0.0:
        return 0.0, 0.0, 0.0, 0.0
    er = ex2 = ew = 0.0
    for k, w in pre_probs.items():
        p = max(0.0, float(w)) / total
        r = _RUNS.get(k, 0)
        er += p * r
        ex2 += p * r * r
        if k == "Wicket":
            ew += p
    vr = max(0.0, ex2 - er * er)
    vw = max(0.0, ew * (1.0 - ew))
    return er, ew, vr, vw


def ball_record(outcome_key, runs, is_wicket, pre_probs):
    """A rolling-window record for one legal delivery."""
    er, ew, vr, vw = expected_from_probs(pre_probs)
    return {"runs": int(runs), "wicket": 1 if is_wicket else 0,
            "er": er, "ew": ew, "vr": vr, "vw": vw}


def recompute(prev_momentum, window, cfg=None):
    """Momentum AFTER the latest delivery, from the rolling window of records.

    ``window`` is the list of the most recent legal-delivery records (the caller
    keeps it trimmed to ``window_legal_balls``). An empty window decays momentum
    toward zero via the EMA, which is the intended behaviour at an innings start.
    """
    cfg = cfg or {}
    ema = float(cfg.get("ema_previous_weight", 0.80))
    cap = float(cfg.get("max_absolute_state", 0.75))

    R = sum(r["runs"] for r in window)
    W = sum(r["wicket"] for r in window)
    ER = sum(r["er"] for r in window)
    EW = sum(r["ew"] for r in window)
    VR = sum(r["vr"] for r in window)
    VW = sum(r["vw"] for r in window)

    denom = math.sqrt(max(VR + 16.0 * VW, 4.0))
    residual = (R - ER - 4.0 * (W - EW)) / denom
    residual = _clamp(residual, -3.0, 3.0)
    nxt = ema * float(prev_momentum) + (1.0 - ema) * math.tanh(residual / 2.0)
    return _clamp(nxt, -cap, cap)


def log_deltas(momentum):
    """Per-outcome LOG adjustments for the current momentum (pre-budget)."""
    m = float(momentum)
    if m == 0.0:
        return {}
    return {k: coef * m for k, coef in _MOMENTUM_LOG.items()}
