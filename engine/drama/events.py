"""Rare volatility events and highlight detection (Codex Prompt 05, §10 & §14).

Two jobs, both "events":

1. RARE HIGH-VOLATILITY (§10) — an occasional batting counterattack or bowling
   surge. It is a small, bounded weight nudge (fed through the same shared drama
   budget as momentum and clutch), activated at most once per innings, never at
   the same time as a clutch event, lasting at most one over. It does NOT script
   a result: no fake wickets, no fixed sixes, no guaranteed over totals.

2. HIGHLIGHT DETECTION (§14) — dramatic moments are recognised AFTER they happen
   from the real match event log, never scripted in advance. Each detected event
   carries a stable, idempotent ``event_id`` so a paused/resumed match does not
   store it twice. These are match events, not player profile data.
"""

import hashlib
import math

# ── Rare volatility ──────────────────────────────────────────────────────
RARE_COUNTERATTACK = "batting_counterattack"
RARE_BOWLING_SURGE = "bowling_surge"

# Small RELATIVE nudges (converted to log space, then still capped by the
# global budget). A counterattack lifts boundaries and the risk that pays for
# them; a surge lifts dots and wickets.
_RARE_MODIFIERS = {
    RARE_COUNTERATTACK: {"Four": 1.10, "Six": 1.12, "Wicket": 1.08, "Dot": 0.95},
    RARE_BOWLING_SURGE: {"Dot": 1.10, "Wicket": 1.12, "Four": 0.92, "Six": 0.90},
}


def rare_activation(rng, probability_per_eligible_over):
    """True if a rare event fires this over (one seeded draw)."""
    return rng.random() < float(probability_per_eligible_over)


def choose_rare_type(rng):
    """Symmetric batting/bowling choice on the seeded RNG."""
    return RARE_COUNTERATTACK if rng.random() < 0.5 else RARE_BOWLING_SURGE


def rare_log_deltas(kind):
    mods = _RARE_MODIFIERS.get(kind, {})
    return {k: math.log(v) for k, v in mods.items()}


# ── Highlight / event detection ──────────────────────────────────────────
# Event type -> default severity (1 minor .. 5 headline). Presentation only.
EVENT_SEVERITY = {
    "explosive_over": 3,
    "three_consecutive_boundaries": 2,
    "major_collapse": 4,
    "hat_trick": 5,
    "fifty_under_pressure": 3,
    "century_under_pressure": 4,
    "double_wicket_over": 3,
    "last_over_thriller": 4,
    "last_ball_victory": 5,
    "major_comeback": 5,
    "tie": 5,
}


def build_event(event_type, match_id, innings, legal_ball_index,
                tension_index=0, team=None, player=None,
                description_key=None, severity=None, dedup_key=None):
    """A detected event with a STABLE id.

    ``event_id`` is a hash of (match_id, innings, event_type, dedup_key). The
    dedup key defaults to the legal-ball index, so re-running detection over the
    same delivery on resume yields the same id and the caller can drop the
    duplicate. For an innings-unique event (e.g. a collapse) pass a coarser
    dedup_key so it is not re-emitted every ball.
    """
    key = dedup_key if dedup_key is not None else legal_ball_index
    raw = "|".join(str(x) for x in (match_id, innings, event_type, key))
    event_id = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return {
        "event_id": event_id,
        "event_type": event_type,
        "match_id": match_id,
        "innings": int(innings),
        "legal_ball_index": int(legal_ball_index),
        "team_reference": team,
        "relevant_player_reference": player,
        "severity": int(severity if severity is not None
                        else EVENT_SEVERITY.get(event_type, 2)),
        "tension_index": int(tension_index),
        "description_key": description_key or event_type,
    }


# -- precise detection rules (§14) ----------------------------------------
# Each takes explicit inputs the engine tracks and returns an event dict or None.

def explosive_over(over_number, over_runs, **kw):
    """At least 20 team runs in one completed over."""
    if int(over_runs) >= 20:
        return build_event("explosive_over", dedup_key="over%d" % over_number,
                           **kw)
    return None


def major_collapse(wickets_in_window, window_legal_balls=12, over_number=0, **kw):
    """Three or more wickets within a rolling window of 12 legal deliveries."""
    if int(wickets_in_window) >= 3:
        # Coarse dedup so a single collapse is one event, not one per ball.
        return build_event("major_collapse",
                           dedup_key="collapse%d" % over_number, **kw)
    return None


def three_consecutive_boundaries(boundary_streak, **kw):
    # Fire once, at the moment the streak reaches three — not every ball it
    # stays at or above three (a six-boundary burst is still one highlight).
    if int(boundary_streak) == 3:
        return build_event("three_consecutive_boundaries", **kw)
    return None


def hat_trick(consecutive_wickets_same_bowler, bowler_ref=None, **kw):
    """Three bowler-credited wickets on three consecutive legal deliveries by
    the same bowler (the engine enforces the same-bowler / over-interruption
    rule before calling this)."""
    if int(consecutive_wickets_same_bowler) >= 3:
        return build_event("hat_trick", player=bowler_ref, **kw)
    return None


def double_wicket_over(wickets_this_over, over_number=0, **kw):
    if int(wickets_this_over) >= 2:
        return build_event("double_wicket_over",
                           dedup_key="dwo%d" % over_number, **kw)
    return None


def milestone(runs_before, runs_after, high_pressure=False, player=None, **kw):
    """Fifty / century, flagged *under pressure* when the game state is tense."""
    for mark, etype in ((100, "century_under_pressure"),
                        (50, "fifty_under_pressure")):
        if runs_before < mark <= runs_after:
            sev = EVENT_SEVERITY[etype] + (0 if high_pressure else -1)
            return build_event(etype, player=player, severity=max(1, sev),
                               dedup_key="%s_%s" % (etype, player), **kw)
    return None
