"""Qualification tracker — who has made the playoffs, who is out.

:func:`status` is pure arithmetic and deliberately cautious: it only marks a
team **Q** when no combination of remaining results can push it out of the
qualifying places, and **E** only when no combination can lift it in. A tie on
points is treated as a threat (NRR and head-to-head can't be predicted), so a
mark is never shown that a later result could take back.

``wins_to_be_sure`` is the smallest number of further wins after which the team
is mathematically through whatever everyone else does — None when wins alone
can't guarantee it (it still needs help), 0 once it's already through.
"""

from types import SimpleNamespace

# Teams that go through to the knockout, by ``knockout_type``. For the group
# formats it is per group.
QUALIFY = {"top4_sf": 4, "ipl_playoffs": 4, "groups_top2_sf": 2,
           "groups_top4_qf": 4}

Q, E = "Q", "E"


def status(points, remaining, spots, points_win):
    """``{team_id: SimpleNamespace(mark, wins_to_be_sure, max_points)}``.

    ``points`` is ``{team_id: current points}``; ``remaining`` is
    ``{team_id: unplayed league matches}``.
    """
    pw = max(0, int(points_win or 0))
    maxp = {t: int(points[t]) + int(remaining.get(t, 0)) * pw for t in points}
    out = {}
    for t, p in points.items():
        others = [o for o in points if o != t]
        threats = sum(1 for o in others if maxp[o] >= p)
        above = sum(1 for o in others if points[o] > maxp[t])
        mark = None
        if spots > 0 and threats < spots:
            mark = Q
        elif spots > 0 and above >= spots:
            mark = E
        need = None
        if mark == Q:
            need = 0
        elif mark is None and spots > 0:
            rivals = sorted((maxp[o] for o in others), reverse=True)
            bar = rivals[spots - 1] if len(rivals) >= spots else -1
            for w in range(0, int(remaining.get(t, 0)) + 1):
                if p + w * pw > bar:
                    need = w
                    break
        out[t] = SimpleNamespace(mark=mark, wins_to_be_sure=need,
                                 max_points=maxp[t])
    return out


def spots_for(tour):
    """Qualifying places per table for ``tour``, or 0 when there are no playoffs."""
    return QUALIFY.get((getattr(tour, "knockout_type", None) or "").strip(), 0)


def tracker(session, tour, group_id=None):
    """:func:`status` for one table of a running tournament, or ``{}``.

    Empty when the tournament has no playoffs, no generated schedule, or the
    bracket already exists (the question is settled by then).
    """
    from models import TournamentMatch, TournamentTeam
    spots = spots_for(tour)
    if not spots or not tour.schedule_generated or tour.knockout_generated:
        return {}
    q = session.query(TournamentTeam).filter_by(tournament_id=tour.id)
    if group_id is not None:
        q = q.filter(TournamentTeam.group_id == int(group_id))
    teams = q.all()
    if len(teams) <= spots:
        return {}
    ids = {t.id for t in teams}
    remaining = {t.id: 0 for t in teams}
    for fx in (session.query(TournamentMatch)
               .filter_by(tournament_id=tour.id)
               .filter(TournamentMatch.stage.in_(("league", "group")),
                       TournamentMatch.status != "completed").all()):
        for side in (fx.team1_id, fx.team2_id):
            if side in ids:
                remaining[side] += 1
    return status({t.id: int(t.points or 0) for t in teams}, remaining,
                  spots, tour.points_win or 0)


def legend(spots):
    return (f"<i>Q = through to the playoffs · E = can no longer finish in the "
            f"top {spots}</i>")


def need_line(team_name, st, spots):
    """"Mumbai: 2 more wins to be sure of the top 4" (plain text), or ""."""
    if st is None:
        return ""
    if st.mark == Q:
        return f"{team_name}: through to the playoffs ✅"
    if st.mark == E:
        return f"{team_name}: can no longer finish in the top {spots}"
    if st.wins_to_be_sure is None:
        return f"{team_name}: needs other results to go their way for the top {spots}"
    n = st.wins_to_be_sure
    return (f"{team_name}: {n} more win{'s' if n != 1 else ''} to be sure of "
            f"the top {spots}")


def table_marks(session, tour):
    """``({team_id: status}, spots)`` for the whole tournament.

    Group formats are worked out group by group (each group sends its own top
    N through); everything else as one table.
    """
    spots = spots_for(tour)
    if not spots:
        return {}, 0
    if (tour.league_format or "") == "groups":
        from models import TournamentGroup
        out = {}
        for g in session.query(TournamentGroup).filter_by(tournament_id=tour.id).all():
            out.update(tracker(session, tour, group_id=g.id))
        return out, spots
    return tracker(session, tour), spots


def race_lines(rows, marks, spots, limit=8):
    """Plain-text lines for the teams still in the race (no Q/E yet)."""
    out = []
    for tt in rows:
        st = marks.get(tt.id)
        if st is not None and st.mark is None:
            out.append(need_line(tt.name or "—", st, spots))
    return out[:limit]
