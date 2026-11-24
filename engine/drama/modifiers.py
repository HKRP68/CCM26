"""The single shared drama budget (Codex Prompt 05, §11).

Momentum, clutch and rare-event effects are NOT allowed to multiply
independently. They are summed in log space, the sum is clamped to one window,
and only then applied::

    combined_log_delta       = momentum_delta + clutch_delta + rare_event_delta
    combined_log_delta       = clamp(combined, -ln(1.15), +ln(1.15))
    adjusted_weight[outcome] = original_weight[outcome] * exp(combined_log_delta)

Then every adjusted weight is renormalised. An outcome the rules have set to
zero is NEVER revived. Two further safety limits bound the whole overlay per
delivery:

  * the absolute change in P(Wicket) is at most ``max_absolute_wicket_prob_delta``;
  * the absolute change in expected runs is at most ``max_absolute_expected_runs_delta``.

If a proposed overlay breaches either, its strength is reduced SMOOTHLY (a
single scalar on every log delta) until it complies — never by clipping one
outcome and leaving the rest.
"""

import math

_RUNS = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6,
         "Wicket": 0, "Extras": 1}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def combine_log_deltas(*delta_dicts):
    """Sum several per-outcome log-delta dicts into one."""
    out = {}
    for d in delta_dicts:
        if not d:
            continue
        for k, v in d.items():
            out[k] = out.get(k, 0.0) + float(v)
    return out


def _normalized(weights):
    total = sum(max(0.0, v) for v in weights.values())
    if total <= 0.0:
        return dict(weights)
    return {k: max(0.0, v) / total for k, v in weights.items()}


def _expected_runs(probs):
    return sum(probs.get(k, 0.0) * _RUNS.get(k, 0) for k in probs)


def _apply_scaled(weights, deltas, scale, lo, hi, free_hit):
    """Weights after applying ``scale * delta`` per outcome, clamped and
    normalised. A zero-weight outcome stays zero; on a free hit no positive
    wicket adjustment is allowed (a free hit cannot be bowled/caught out)."""
    out = {}
    for k, w in weights.items():
        w = max(0.0, float(w))
        if w <= 0.0:
            out[k] = 0.0
            continue
        d = _clamp(scale * deltas.get(k, 0.0), lo, hi)
        if free_hit and k == "Wicket" and d > 0.0:
            d = 0.0
        out[k] = w * math.exp(d)
    return _normalized(out)


def apply(weights, deltas, cfg, free_hit=False):
    """Apply the combined drama ``deltas`` to ``weights`` under the budget and
    the two safety limits. Returns ``(new_probs, info)``.

    ``info`` reports the scale actually used (1.0 if nothing had to be reduced)
    and the realised wicket / expected-run changes, for diagnostics.
    """
    limits = cfg.get("effect_limits", {})
    lo = float(limits.get("min_log_multiplier", -math.log(1.15)))
    hi = float(limits.get("max_log_multiplier", math.log(1.15)))
    wkt_cap = float(limits.get("max_absolute_wicket_probability_delta", 0.012))
    run_cap = float(limits.get("max_absolute_expected_runs_delta", 0.08))

    base = _normalized(weights)
    if not deltas:
        return base, {"scale": 0.0, "wicket_delta": 0.0, "runs_delta": 0.0}

    base_wkt = base.get("Wicket", 0.0)
    base_runs = _expected_runs(base)

    def breach(scale):
        cand = _apply_scaled(weights, deltas, scale, lo, hi, free_hit)
        dw = abs(cand.get("Wicket", 0.0) - base_wkt)
        dr = abs(_expected_runs(cand) - base_runs)
        return dw > wkt_cap + 1e-12 or dr > run_cap + 1e-12

    # Full strength is usually fine; only search when it breaches a cap.
    scale = 1.0
    if breach(1.0):
        lo_s, hi_s = 0.0, 1.0
        for _ in range(24):                     # binary search for max legal scale
            mid = 0.5 * (lo_s + hi_s)
            if breach(mid):
                hi_s = mid
            else:
                lo_s = mid
        scale = lo_s

    final = _apply_scaled(weights, deltas, scale, lo, hi, free_hit)
    return final, {
        "scale": scale,
        "wicket_delta": final.get("Wicket", 0.0) - base_wkt,
        "runs_delta": _expected_runs(final) - base_runs,
    }
