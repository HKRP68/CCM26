"""Shadow-mode recorder: measure the new model without playing it.

Shadow mode (§10 of the brief) computes both distributions for every
delivery, uses the **legacy** one for the actual match, and records how far
apart they were. The two properties that make it safe are structural, not
promised:

* :func:`engine.realism.outcome_distribution.build` draws no random numbers,
  so the live match consumes exactly the same random stream in exactly the
  same order whether shadow mode is on or off. A shadow run and a non-shadow
  run from the same seed produce the identical scorecard —
  ``tests/test_realism_integration.py`` asserts this.

* Nothing here writes to match state. The recorder is an in-process
  accumulator; ``snapshot()`` reads it and ``reset()`` clears it. If the
  recorder itself raised, the delivery would still be resolved — the caller
  wraps it.

Metrics are deliberately the ones a calibration decision actually turns on:
mean absolute difference per outcome, expected-runs difference, and the
total-variation distance, bucketed by phase so a model that is right overall
but wrong at the death cannot hide.
"""

import collections
import threading

OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")

_lock = threading.Lock()
_state = {"n": 0, "by_phase": collections.defaultdict(
    lambda: {"n": 0, "abs": collections.Counter(), "legacy": collections.Counter(),
             "realism": collections.Counter(), "tv": 0.0, "d_runs": 0.0})}


def _bucket(phase):
    return _state["by_phase"][phase or "unknown"]


def record(legacy_dist: dict, realism_dist: dict, phase: str = None):
    """Accumulate one delivery's divergence. Never raises on bad input."""
    try:
        lt = sum(max(0.0, float(legacy_dist.get(o, 0.0))) for o in OUTCOMES)
        rt = sum(max(0.0, float(realism_dist.get(o, 0.0))) for o in OUTCOMES)
        if lt <= 0 or rt <= 0:
            return
        legacy = {o: max(0.0, float(legacy_dist.get(o, 0.0))) / lt for o in OUTCOMES}
        realism = {o: max(0.0, float(realism_dist.get(o, 0.0))) / rt for o in OUTCOMES}
    except Exception:
        return
    runs = {"Dot": 0, "Single": 1, "Double": 2, "Three": 3, "Four": 4, "Six": 6,
            "Wicket": 0, "Extras": 1}
    with _lock:
        _state["n"] += 1
        b = _bucket(phase)
        b["n"] += 1
        tv = 0.0
        for o in OUTCOMES:
            d = realism[o] - legacy[o]
            b["abs"][o] += abs(d)
            b["legacy"][o] += legacy[o]
            b["realism"][o] += realism[o]
            tv += abs(d)
        b["tv"] += tv / 2.0
        b["d_runs"] += (sum(runs[o] * realism[o] for o in OUTCOMES)
                        - sum(runs[o] * legacy[o] for o in OUTCOMES))


def snapshot() -> dict:
    """Averaged divergence metrics, overall and per phase."""
    with _lock:
        out = {"deliveries": _state["n"], "phases": {}}
        agg = {"n": 0, "tv": 0.0, "d_runs": 0.0,
               "abs": collections.Counter(), "legacy": collections.Counter(),
               "realism": collections.Counter()}
        for phase, b in _state["by_phase"].items():
            n = max(1, b["n"])
            out["phases"][phase] = {
                "deliveries": b["n"],
                "total_variation": b["tv"] / n,
                "exp_runs_delta": b["d_runs"] / n,
                "mean_abs_diff": {o: b["abs"][o] / n for o in OUTCOMES},
                "legacy_mean": {o: b["legacy"][o] / n for o in OUTCOMES},
                "realism_mean": {o: b["realism"][o] / n for o in OUTCOMES},
            }
            agg["n"] += b["n"]
            agg["tv"] += b["tv"]
            agg["d_runs"] += b["d_runs"]
            for o in OUTCOMES:
                agg["abs"][o] += b["abs"][o]
                agg["legacy"][o] += b["legacy"][o]
                agg["realism"][o] += b["realism"][o]
        n = max(1, agg["n"])
        out["overall"] = {
            "deliveries": agg["n"],
            "total_variation": agg["tv"] / n,
            "exp_runs_delta": agg["d_runs"] / n,
            "mean_abs_diff": {o: agg["abs"][o] / n for o in OUTCOMES},
            "legacy_mean": {o: agg["legacy"][o] / n for o in OUTCOMES},
            "realism_mean": {o: agg["realism"][o] / n for o in OUTCOMES},
        }
        return out


def reset():
    with _lock:
        _state["n"] = 0
        _state["by_phase"].clear()


def format_report(snap: dict = None) -> str:
    """A table for the console / logs."""
    snap = snap or snapshot()
    if not snap["deliveries"]:
        return "shadow mode: no deliveries recorded"
    lines = ["shadow mode: %d deliveries" % snap["deliveries"],
             "%-10s %7s %7s %8s  %s" % ("phase", "balls", "TV", "d_runs",
                                        "biggest shifts")]
    order = ["powerplay", "middle", "death", "unknown"]
    keys = [k for k in order if k in snap["phases"]]
    keys += [k for k in snap["phases"] if k not in order]
    for phase in keys + ["overall"]:
        row = snap["overall"] if phase == "overall" else snap["phases"][phase]
        shifts = sorted(((row["realism_mean"][o] - row["legacy_mean"][o], o)
                         for o in OUTCOMES), key=lambda x: -abs(x[0]))[:3]
        lines.append("%-10s %7d %7.4f %+8.4f  %s" % (
            phase, row["deliveries"], row["total_variation"],
            row["exp_runs_delta"],
            "  ".join("%s %+0.4f" % (o, d) for d, o in shifts)))
    return "\n".join(lines)
