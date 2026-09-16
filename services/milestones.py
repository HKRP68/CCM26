"""Milestone detection for the over-by-over ("Approach") simulation.

The over-by-over engine simulates a whole over in one call, so milestones are
found by diffing: snapshot the counters that matter before
``cipl_match.simulate_over``, compare after, and emit one event per threshold
actually crossed. Crossings, not equality — a batter can go 48 -> 52 and must
still get their fifty.

Each event is ``(event_key, fields)``. The key selects the admin-configured
message/image (services/event_media_service.EVENT_KEYS); the fields fill its
caption placeholders. Deliberately free of Telegram imports so it can be tested
on plain dicts.

Hat-tricks need no tracking here: ``cipl_match.simulate_over`` already calls
``match_engine.note_bowler_ball``, which maintains ``wkt_streak`` and sets
``hattrick`` on the bowler's stat row (run-outs excluded). ``end_first_innings``
rebuilds ``bowl_stats`` wholesale, so a streak cannot leak across innings.
"""

import logging

logger = logging.getLogger(__name__)

# Thresholds, each "first crossing wins". Team totals are per innings because
# bowl_stats/bat_stats/total_runs all reset at the break.
BAT_MILESTONES = ((100, "century"), (50, "fifty"))
BOWL_MILESTONES = ((5, "five_fer"), (3, "three_fer"))
PARTNERSHIP_MILESTONES = ((100, "partnership_100"), (50, "partnership_50"))
TEAM_MILESTONES = ((200, "team_200"), (150, "team_150"), (100, "team_100"))


def _int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def snapshot(state):
    """Counters to diff against, taken immediately before an over is simulated."""
    state = state or {}
    bat = {}
    for rid, st in (state.get("bat_stats") or {}).items():
        bat[str(rid)] = _int((st or {}).get("runs"))
    bowl = {}
    for rid, st in (state.get("bowl_stats") or {}).items():
        st = st or {}
        bowl[str(rid)] = (_int(st.get("wickets")), bool(st.get("hattrick")))
    return {
        "innings": state.get("innings"),
        "bat": bat,
        "bowl": bowl,
        "total": _int(state.get("total_runs")),
        "partnership": _int(state.get("partnership_runs")),
    }


def _name_for(state, rid, *keys):
    """Player name for a roster id, searched across the given XI list keys."""
    rid_s = str(rid)
    for key in keys:
        for p in (state.get(key) or []):
            if str((p or {}).get("roster_id")) == rid_s:
                return p.get("name") or "Player"
    return "Player"


def _crossed(before, after, thresholds):
    """Every threshold passed going from ``before`` to ``after``, low first."""
    hit = [(n, key) for n, key in thresholds if before < n <= after]
    return list(reversed(hit))          # tables are high-first; fire low-first


def _overs(state):
    try:
        from services import cipl_match
        return cipl_match.format_overs(state)
    except Exception:
        return ""


def _score(state):
    try:
        from services import cipl_match
        return cipl_match.format_score(state)
    except Exception:
        return str(state.get("total_runs", 0))


def _figures(st):
    st = st or {}
    balls = _int(st.get("balls"))
    return f"{_int(st.get('wickets'))}/{_int(st.get('runs'))} ({balls // 6}.{balls % 6})"


def detect(state, before, summary=None):
    """Milestones crossed by the over just simulated, in the order to announce.

    ``before`` is a snapshot() taken before the over. Returns
    ``[(event_key, fields), ...]`` — possibly several, since one over can hold a
    fifty, a three-fer and the team hundred at once.
    """
    if not state or not before:
        return []
    # The innings changed under us (the over ended it and the caller already
    # swapped sides). Counters reset at the break, so a diff is meaningless.
    if before.get("innings") != state.get("innings"):
        return []

    events = []
    team = state.get("bat_team_name") or "Team"
    opponent = state.get("bowl_team_name") or "Opposition"
    common = {"team": team, "opponent": opponent,
              "score": _score(state), "overs": _overs(state)}

    try:
        # ── Batting: fifty, century ──
        for rid, st in (state.get("bat_stats") or {}).items():
            st = st or {}
            prev = before["bat"].get(str(rid), 0)
            runs = _int(st.get("runs"))
            for _n, key in _crossed(prev, runs, BAT_MILESTONES):
                events.append((key, dict(
                    common,
                    player=_name_for(state, rid, "batting_order", "bat_xi"),
                    runs=runs, balls=_int(st.get("balls")))))

        # ── Bowling: hat-trick, three-fer, five-fer ──
        for rid, st in (state.get("bowl_stats") or {}).items():
            st = st or {}
            prev_wkts, prev_hat = before["bowl"].get(str(rid), (0, False))
            wickets = _int(st.get("wickets"))
            name = _name_for(state, rid, "bowl_xi")
            if bool(st.get("hattrick")) and not prev_hat:
                events.append(("hattrick", dict(
                    common, player=name, bowler=name,
                    wickets=wickets, figures=_figures(st),
                    team=opponent, opponent=team)))
            for _n, key in _crossed(prev_wkts, wickets, BOWL_MILESTONES):
                events.append((key, dict(
                    common, player=name, bowler=name,
                    wickets=wickets, figures=_figures(st),
                    team=opponent, opponent=team)))

        # ── Partnership ──
        # Only when the stand survived the over: a wicket resets partnership_runs
        # to 0, so a fallen stand would read as a drop, never a crossing.
        stand = _int(state.get("partnership_runs"))
        for _n, key in _crossed(before["partnership"], stand,
                                PARTNERSHIP_MILESTONES):
            events.append((key, dict(common, partnership=stand, runs=stand)))

        # ── Team total ──
        total = _int(state.get("total_runs"))
        for _n, key in _crossed(before["total"], total, TEAM_MILESTONES):
            events.append((key, dict(common, runs=total)))
    except Exception:
        # A celebration is never worth failing an over for.
        logger.exception("milestone detection failed")
        return events

    return events


def match_won_event(state, result_line="", winner_name="", margin=""):
    """The end-of-match event, fired from the completion path."""
    return ("match_won", {
        "team": winner_name or "",
        "player": winner_name or "",
        "opponent": "",
        "margin": margin or "",
        "score": result_line or "",
        "overs": "",
        "runs": "",
    })
