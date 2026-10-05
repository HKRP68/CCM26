"""The round recap — one card in the group when a league round finishes.

Posted by the tournament watch (``services.tournament_watch``) exactly once per
round, however the round's last result arrived. It answers the four things a
group asks the moment a round closes: what happened, who moved, who starred,
and who plays whom next.
"""

import logging
from html import escape

from models import Tournament, TournamentMatch, TournamentTeam

logger = logging.getLogger(__name__)


def _ordinal(n):
    n = int(n)
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _score(runs, wkts, balls):
    if runs is None:
        return ""
    overs = f"{int(balls or 0) // 6}.{int(balls or 0) % 6}" if balls else ""
    return f"{runs}/{wkts}" + (f" ({overs})" if overs else "")


def round_fixtures(session, tid, round_no):
    return (session.query(TournamentMatch)
            .filter(TournamentMatch.tournament_id == int(tid),
                    TournamentMatch.stage.in_(("league", "group")),
                    TournamentMatch.round_no == int(round_no))
            .order_by(TournamentMatch.match_no, TournamentMatch.id).all())


def movers(session, tour, round_no):
    """``{team_id: (old_position, new_position)}`` for the teams that moved."""
    from services import standings, tournament_service
    teams = (session.query(TournamentTeam).filter_by(tournament_id=tour.id).all())
    meta = [(t.id, t.name, t.points_adjust) for t in teams]
    before_matches = tournament_service.completed_league_matches(
        session, tour.id, exclude_round=round_no)
    before = standings.order(
        standings.tally(meta, before_matches, tour.points_win or 0,
                        tour.points_tie or 0, tour.points_loss or 0).values(),
        tour.tiebreak or "nrr", before_matches,
        points_win=tour.points_win or 0, points_tie=tour.points_tie or 0)
    old = {r.id: i for i, r in enumerate(before, 1)}
    now = {r.id: i for i, r in enumerate(
        tournament_service.points_table(session, tour.id), 1)}
    return {tid: (old[tid], now[tid]) for tid in now
            if tid in old and old[tid] != now[tid]}


def player_of_round(session, tour, round_no):
    """``(name, team, points, line_text)`` of the round's best performer, or None."""
    from services import tournament_mvp, tournament_service
    ids = {fx.id for fx in round_fixtures(session, tour.id, round_no)}
    agg = {}
    for match, lines in tournament_service.match_scorecards(session, tour.id):
        if match.id not in ids:
            continue
        for line in lines:
            if not (line.get("batted") or line.get("bowled")):
                continue
            bat, bowl = tournament_mvp.line_impact(line)
            key = tournament_service.player_identity(line)
            row = agg.setdefault(key, {"name": line.get("name"),
                                       "team": line.get("team_name"),
                                       "points": 0.0, "bits": []})
            row["points"] += bat + bowl
            if line.get("batted") and int(line.get("bat_balls") or 0):
                row["bits"].append(f"{int(line.get('bat_runs') or 0)} "
                                   f"({int(line.get('bat_balls') or 0)})")
            if line.get("bowled") and int(line.get("bowl_balls") or 0):
                row["bits"].append(f"{int(line.get('bowl_wickets') or 0)}/"
                                   f"{int(line.get('bowl_runs') or 0)}")
    if not agg:
        return None
    best = max(agg.values(), key=lambda r: r["points"])
    if best["points"] <= 0:
        return None
    return best["name"] or "—", best["team"] or "", round(best["points"]), \
        " & ".join(best["bits"])


def render_round_recap(session, tour, round_no, now=None):
    """The recap card as one HTML message."""
    from services import league_schedule_service as lss
    from services import match_reminder_service as mrs
    from services import qualification, tournament_service, tournament_watch
    names = {t.id: t.name or "—" for t in
             session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()}
    out = [f"📋 <b>Round {round_no} recap</b> — {escape(tour.name or 'Tournament')}", ""]

    out.append("<b>Results</b>")
    for fx in round_fixtures(session, tour.id, round_no):
        a, b = escape(names.get(fx.team1_id, "TBD")), escape(names.get(fx.team2_id, "TBD"))
        tag = f"M{fx.match_no}" if fx.match_no else "•"
        s1 = _score(fx.inn1_runs, fx.inn1_wickets, fx.inn1_balls)
        s2 = _score(fx.inn2_runs, fx.inn2_wickets, fx.inn2_balls)
        line = f"<code>{tag}</code> {a} {s1} · {b} {s2}"
        if fx.result_text:
            line += f"\n   ✅ {escape(fx.result_text)}"
        out.append(line)

    moved = movers(session, tour, round_no)
    if moved:
        out += ["", "<b>Table movers</b>"]
        for tid, (old, new) in sorted(moved.items(), key=lambda kv: kv[1][1]):
            arrow = "⬆️" if new < old else "⬇️"
            out.append(f"{arrow} {escape(names.get(tid, '—'))} — now "
                       f"{_ordinal(new)} (was {_ordinal(old)})")

    star = player_of_round(session, tour, round_no)
    if star:
        name, team, pts, bits = star
        out += ["", f"⭐ <b>Player of the round:</b> {escape(name)}"
                    + (f" ({escape(team)})" if team else "")
                    + (f" — {escape(bits)}" if bits else "")
                    + f" · {pts} pts"]

    rows = tournament_service.points_table(session, tour.id)
    marks, spots = qualification.table_marks(session, tour)
    if rows:
        out += ["", "<b>Top of the table</b>"]
        for i, tt in enumerate(rows[:4], 1):
            mark = getattr(marks.get(tt.id), "mark", None)
            out.append(f"{i}. {escape(tt.name or '—')} — {tt.points or 0} pts"
                       + (f" ({mark})" if mark else ""))

    nxt = lss.current_round(session, tour.id)
    fixtures_cmd = tournament_watch._fixtures_cmd(tour)
    if nxt is not None:
        out += ["", f"<b>Round {nxt} — up next</b>"]
        teams = {t.id: t for t in
                 session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()}
        for fx in round_fixtures(session, tour.id, nxt):
            if fx.status == "completed":
                continue
            a, b = teams.get(fx.team1_id), teams.get(fx.team2_id)
            if not a or not b:
                continue
            tag = f"M{fx.match_no}" if fx.match_no else "•"
            mentions = " ".join(m for _tg, m in
                                mrs.team_contacts(session, a) + mrs.team_contacts(session, b))
            venue = f" · 🏟️ {escape(fx.venue)}" if getattr(fx, "venue", None) else ""
            out.append(f"<code>{tag}</code> {escape(a.name or '—')} vs "
                       f"{escape(b.name or '—')}{venue}"
                       + (f"\n   {mentions}" if mentions else ""))
        left = tournament_watch.deadline_text(tour, now)
        if left:
            out.append(f"\n{left}")
    elif tour.knockout_generated:
        out += ["", f"🏆 <b>The league stage is over — the Playoffs are set!</b> "
                    f"See {fixtures_cmd}."]
    else:
        out += ["", "🏁 <b>The league stage is complete.</b>"]
    return "\n".join(out)
