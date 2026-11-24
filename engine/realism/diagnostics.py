"""Opt-in per-delivery probability diagnostics.

Requirement 8 of the Fairness & Simulation Fixes: a way to see, for any
delivery, *every* modifier that was applied, the distribution before and
after each stage, and the outcome that was finally sampled — so a surprising
result can be traced rather than guessed at.

It is **off by default and free when off**: ``enabled()`` is a single env-var
read, and nothing in the hot path allocates or records unless it returns true.
When on, each delivery appends one record to an in-process ring buffer; the
buffer is bounded so a long auto-sim cannot exhaust memory. Records never
touch match state, are never persisted, and carry no player identity beyond
the ratings already on the card.

Turn it on with::

    SIM_REALISM_DIAGNOSTICS=1

and read it with :func:`records`, :func:`last` or :func:`format_record`, or
from the engine via ``result["realism"]["diagnostics"]`` when a single
delivery is being inspected.
"""

import collections
import os
import threading

OUTCOMES = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket",
            "Extras")

_TRUTHY = ("1", "true", "yes", "on")
_lock = threading.Lock()
_BUFFER_MAX = 512
_buffer = collections.deque(maxlen=_BUFFER_MAX)


def enabled() -> bool:
    """Is per-delivery diagnostics recording switched on?"""
    raw = os.getenv("SIM_REALISM_DIAGNOSTICS")
    return bool(raw) and raw.strip().lower() in _TRUTHY


def _dist(d):
    if not d:
        return {}
    return {o: round(float(d.get(o, 0.0)), 6) for o in OUTCOMES}


def build_record(explained: dict, ctx, final_weights: dict,
                 sampled: str = None) -> dict:
    """Assemble one delivery's diagnostic record from the realism `explain`.

    ``explained`` is the dict ``outcome_distribution.build(..., explain=True)``
    returns — baseline, ratings, conditions, approach, gain, node, phase. The
    record lays out the pipeline stage by stage so the effect of each modifier
    is visible, ends with the fully-composed pre-sample distribution, and
    records which outcome was drawn.
    """
    base = explained.get("baseline") or {}
    ratings = explained.get("ratings") or {}
    cond = explained.get("conditions") or {}
    approach = explained.get("approach") or {}
    gain = explained.get("gain") or {}
    dist = explained.get("dist") or {}

    # Normalise the final sampled weights to a distribution for comparison.
    fw = {o: max(0.0, float((final_weights or {}).get(o, 0.0))) for o in OUTCOMES}
    ft = sum(fw.values()) or 1.0
    final_dist = {o: round(fw[o] / ft, 6) for o in OUTCOMES}

    return {
        "context": {
            "format": ctx.format_name, "phase": explained.get("phase"),
            "innings": ctx.innings, "over": ctx.over,
            "balls_bowled": ctx.balls_bowled, "wickets": ctx.wickets,
            "runs": ctx.runs, "target": ctx.target,
            "required_rate": (round(ctx.required_rate, 3)
                              if ctx.required_rate is not None else None),
            "bat_rating": ctx.bat_rating, "bowl_rating": ctx.bowl_rating,
            "bat_style": ctx.bat_style, "bowling_type": ctx.bowling_type,
            "pitch": ctx.pitch, "pitch_wear": round(ctx.pitch_wear, 4),
            "batting_approach": ctx.batting_approach,
            "bowling_approach": ctx.bowling_approach,
            "balls_faced": ctx.balls_faced, "batter_runs": ctx.batter_runs,
        },
        "provenance": {
            "node": explained.get("node"),
            "competition": explained.get("competition"),
            "model_version": explained.get("model_version"),
            "node_n": (explained.get("node_meta") or {}).get("n"),
            "count_kind": (explained.get("node_meta") or {}).get("count_kind"),
        },
        # Each modifier stage, as per-outcome multipliers (1.0 = no effect).
        "modifiers": {
            "rating_curve": _dist(ratings),
            "conditions": _dist(cond),
            "approach": _dist(approach) if approach else None,
            "downstream_gain": _dist(gain),
        },
        # The distribution at each stage.
        "stages": {
            "baseline": _dist(base),
            "realism_out": _dist(dist),      # after rating+conditions+gain+clamp
            "final_pre_sample": final_dist,  # after GSME/pressure/approach/hooks
        },
        "sampled": sampled,
    }


def record(rec: dict):
    """Append one record to the ring buffer. No-op when disabled."""
    if not enabled():
        return
    with _lock:
        _buffer.append(rec)


def records(limit: int = None) -> list:
    """A snapshot copy of the recorded deliveries, oldest first."""
    with _lock:
        items = list(_buffer)
    return items[-limit:] if limit else items


def last() -> dict:
    with _lock:
        return _buffer[-1] if _buffer else None


def reset():
    with _lock:
        _buffer.clear()


def format_record(rec: dict) -> str:
    """A human-readable rendering of one record, for the console or a log."""
    if not rec:
        return "no diagnostic record"
    c = rec["context"]
    lines = [
        "delivery: %s inn%s over %s  %s/%s  %s vs %s on %s (%s)" % (
            c["format"], c["innings"], c["over"] + 1, c["runs"], c["wickets"],
            int(c["bat_rating"]), int(c["bowl_rating"]), c["pitch"], c["phase"]),
        "  node=%s n=%s kind=%s" % (
            rec["provenance"]["node"], rec["provenance"]["node_n"],
            rec["provenance"]["count_kind"]),
        "  %-16s %s" % ("outcome", "  ".join("%7s" % o[:7] for o in OUTCOMES)),
    ]
    stages = rec["stages"]
    for label in ("baseline", "realism_out", "final_pre_sample"):
        d = stages.get(label) or {}
        lines.append("  %-16s %s" % (
            label, "  ".join("%7.4f" % d.get(o, 0.0) for o in OUTCOMES)))
    for label in ("rating_curve", "conditions", "approach", "downstream_gain"):
        m = rec["modifiers"].get(label)
        if m:
            lines.append("  x%-15s %s" % (
                label, "  ".join("%7.3f" % m.get(o, 1.0) for o in OUTCOMES)))
    lines.append("  sampled: %s" % rec.get("sampled"))
    return "\n".join(lines)
