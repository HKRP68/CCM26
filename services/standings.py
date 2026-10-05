"""Points-table arithmetic shared by the table, the recap and the tracker.

No database here: callers hand in team rows (anything with ``id``, ``points``,
``won``, ``runs_for``, ``balls_for``, ``runs_against``, ``balls_against``) and
the completed league matches. That keeps one definition of "who is above whom"
for the live table (``tournament_service.points_table``), the table as it stood
before a round (the recap's movers) and the qualification tracker.

Tiebreaks after points (``Tournament.tiebreak``):

* ``"nrr"`` — wins, then net run rate (the long-standing order)
* ``"h2h"`` — the head-to-head mini-table among the teams level on points,
  then net run rate. A pair (or group) that hasn't met yet falls straight
  through to NRR.
"""

from types import SimpleNamespace

TIEBREAKS = ("nrr", "h2h")
TIEBREAK_LABEL = {"nrr": "Points → wins → net run rate",
                  "h2h": "Points → head-to-head → net run rate"}


def nrr(row):
    """Net run rate at full precision (0 before a ball has been bowled)."""
    of = (row.balls_for or 0) / 6.0
    oa = (row.balls_against or 0) / 6.0
    rf = (row.runs_for or 0) / of if of else 0.0
    ra = (row.runs_against or 0) / oa if oa else 0.0
    return rf - ra


def tally(teams, matches, points_win=2, points_tie=1, points_loss=0):
    """Fresh standings from ``matches`` — ``{team_id: SimpleNamespace}``.

    ``teams`` is ``[(id, name, points_adjust)]``. Mirrors
    ``tournament_service.recompute_standings`` exactly, adjustments included.
    """
    rows = {tid: SimpleNamespace(id=tid, name=name, played=0, won=0, lost=0,
                                 tied=0, points=int(adj or 0), runs_for=0,
                                 balls_for=0, runs_against=0, balls_against=0)
            for tid, name, adj in teams}
    for m in matches:
        t1, t2 = rows.get(m.team1_id), rows.get(m.team2_id)
        i1r, i1b = int(m.inn1_runs or 0), int(m.inn1_balls or 0)
        i2r, i2b = int(m.inn2_runs or 0), int(m.inn2_balls or 0)
        if t1:
            t1.played += 1
            t1.runs_for += i1r; t1.balls_for += i1b
            t1.runs_against += i2r; t1.balls_against += i2b
        if t2:
            t2.played += 1
            t2.runs_for += i2r; t2.balls_for += i2b
            t2.runs_against += i1r; t2.balls_against += i1b
        if m.winner_team_id is None:
            for t in (t1, t2):
                if t:
                    t.tied += 1
                    t.points += points_tie
        else:
            win = rows.get(m.winner_team_id)
            lose = t2 if win is t1 else t1
            if win:
                win.won += 1
                win.points += points_win
            if lose:
                lose.lost += 1
                lose.points += points_loss
    return rows


def _h2h_points(ids, matches, points_win, points_tie):
    """Mini-table points among ``ids`` from the matches they played each other."""
    pts = {i: 0 for i in ids}
    for m in matches:
        if m.team1_id in pts and m.team2_id in pts:
            if m.winner_team_id is None:
                pts[m.team1_id] += points_tie
                pts[m.team2_id] += points_tie
            elif m.winner_team_id in pts:
                pts[m.winner_team_id] += points_win
    return pts


def order(rows, tiebreak="nrr", matches=(), points_win=2, points_tie=1):
    """``rows`` sorted best first. Sets ``_nrr_sort`` and ``_nrr`` on each row."""
    rows = list(rows)
    for r in rows:
        r._nrr_sort = nrr(r)
        r._nrr = round(r._nrr_sort, 3)
    if (tiebreak or "nrr") != "h2h":
        rows.sort(key=lambda t: (t.points or 0, t.won or 0, t._nrr_sort),
                  reverse=True)
        return rows
    rows.sort(key=lambda t: (t.points or 0), reverse=True)
    out, i = [], 0
    while i < len(rows):
        j = i
        while j < len(rows) and (rows[j].points or 0) == (rows[i].points or 0):
            j += 1
        group = rows[i:j]
        if len(group) > 1:
            mini = _h2h_points([r.id for r in group], matches, points_win, points_tie)
            group.sort(key=lambda t: (mini.get(t.id, 0), t._nrr_sort), reverse=True)
        out += group
        i = j
    return out
