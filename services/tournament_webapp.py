"""JSON views of a live tournament for the Mini App's Tournament tab.

The admin dashboard (``admin.admin_tournament_dashboard``) and the bot's
``/tournament`` boards already know how to read a tournament; this module asks
the same questions of ``tournament_service`` and friends and answers in plain
dicts the Mini App can render. Nothing here writes — the caller decides whether
to refresh the aggregates first.

Active competitions and completed results are available to players. Draft and
unpublished competitions stay in the admin panel.
"""
import json
import logging
from sqlalchemy import and_, func, or_

from models import (Tournament, TournamentGroup, TournamentMatch, TournamentTeam)
from services import tournament_service
from services.tournament_service import KIND_CHALLENGE, KIND_LETSPLAY

logger = logging.getLogger(__name__)

KIND_LABEL = {KIND_CHALLENGE: "Challenge League", KIND_LETSPLAY: "Let's Play"}

STAGE_LABEL = {
    "league": "League", "group": "Group",
    "quarterfinal": "Quarter-final", "semifinal": "Semi-final",
    "qualifier1": "Qualifier 1", "eliminator": "Eliminator",
    "qualifier2": "Qualifier 2", "final": "Final",
    "round_of_16": "Round of 16", "round_of_32": "Round of 32",
}

STATUS_LABEL = {
    "draft": "Draft", "scheduled": "Scheduled", "active": "Live",
    "paused": "Paused", "completed": "Completed", "cancelled": "Cancelled",
}

# Stat boards in the order the Mini App's picker shows them:
# (key, label, icon, unit of the headline number).
BOARDS = (
    ("runs", "Most Runs", "🏏", "runs"),
    ("wkts", "Most Wickets", "🎯", "wkts"),
    ("hs", "Highest Score", "💯", ""),
    ("fig", "Best Figures", "🔥", ""),
    ("sixes", "Most Sixes", "💥", "6s"),
    ("fours", "Most Fours", "🎾", "4s"),
    ("fifties", "Most 50s", "⭐", "50s"),
    ("hundreds", "Most 100s", "🌟", "100s"),
    ("sr", "Best Strike Rate", "⚡", "SR"),
    ("econ", "Best Economy", "🧊", "Econ"),
    ("avg", "Best Average", "📈", "Avg"),
)


# ──────────────────────────────────────────────────────────────────────
# Which tournaments are live
# ──────────────────────────────────────────────────────────────────────

def featured_tournament(session):
    """The most recently activated competition that is still live."""
    return (session.query(Tournament)
            .filter(Tournament.is_active.is_(True), Tournament.status == "active")
            .order_by(Tournament.activated_at.desc().nullslast(), Tournament.id.desc())
            .first())


def live_summary(session):
    """The small ``tournament`` block ``/api/webapp/init`` carries."""
    tour = featured_tournament(session)
    if tour is None:
        return {"live": False, "tournaments": []}
    kind = tournament_service.tournament_kind(tour)
    return {"live": True, "tournaments": [
        {"id": tour.id, "name": tour.name, "kind": kind, "kind_label": KIND_LABEL.get(kind, "")}
    ]}


def resolve_live(session, tournament_id=None):
    tour = featured_tournament(session)
    if tour and (not tournament_id or tour.id == int(tournament_id)):
        return tour
    return None


# ──────────────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────

def overs(balls):
    """Balls as cricket overs — 110 balls is 18.2, never 18.33."""
    balls = int(balls or 0)
    return f"{balls // 6}.{balls % 6}"


def _score(runs, wickets, balls):
    if runs is None:
        return None
    return f"{int(runs)}/{int(wickets or 0)} ({overs(balls)})"


def _sr(r):
    return (r.bat_runs * 100.0 / r.bat_balls) if r.bat_balls else 0.0


def _econ(r):
    o = (r.bowl_balls or 0) / 6.0
    return ((r.bowl_runs or 0) / o) if o else 0.0


def _figure(r):
    w = getattr(r, "best_bowl_wickets", None)
    runs = getattr(r, "best_bowl_runs", None)
    if w is None or runs is None or runs < 0:
        return None
    return f"{w}/{runs}"


def _team_brief(tt):
    if tt is None:
        return None
    return {"id": tt.id, "name": tt.name,
            "short_name": tt.short_name or _short(tt.name),
            "logo_url": tt.logo_url}


def _short(name):
    words = [w for w in (name or "").split() if w]
    if len(words) >= 2:
        return "".join(w[0] for w in words[:4]).upper()
    return (name or "?")[:3].upper()


def _represents_team(team, telegram_id):
    return (team.user_tg_id == telegram_id
            or tournament_service.is_team_member(team, telegram_id))


def my_team_ids(session, tour, tg_id):
    """Ids of the teams in ``tour`` the viewer owns, co-owns or *is*."""
    if not tg_id:
        return set()
    return {team.id for team in session.query(TournamentTeam).filter_by(tournament_id=tour.id)
            if _represents_team(team, int(tg_id))}


# ──────────────────────────────────────────────────────────────────────
# Overview payload: header, table, fixtures, injuries
# ──────────────────────────────────────────────────────────────────────

def _table_rows(session, tour, group_id, mine):
    from services.cl_tournament_view import team_form
    from services import qualification
    marks, spots = qualification.table_marks(session, tour)
    rows = []
    for pos, tt in enumerate(tournament_service.points_table(session, tour.id,
                                                               group_id=group_id), 1):
        rows.append({
            "pos": pos, "team_id": tt.id, "name": tt.name,
            "short_name": tt.short_name or _short(tt.name),
            "logo_url": tt.logo_url, "owner_name": tt.owner_name,
            "played": tt.played or 0, "won": tt.won or 0, "lost": tt.lost or 0,
            "tied": tt.tied or 0, "no_result": tt.no_result or 0,
            "points": tt.points or 0, "nrr": tt._nrr,
            "points_adjust": tt.points_adjust or 0,
            "points_adjust_note": tt.points_adjust_note,
            "form": list(reversed(team_form(session, tour.id, tt.id, limit=5))),
            "is_mine": tt.id in mine,
            "qual": getattr(marks.get(tt.id), "mark", None),
            "qual_note": (qualification.need_line(tt.name or "—", marks.get(tt.id), spots)
                          if tt.id in marks else None),
        })
    return rows


def _fixture(fx, teams, next_id):
    from services.tournament_match_settings import day_night
    t1 = teams.get(fx.team1_id)
    t2 = teams.get(fx.team2_id)
    return {
        "id": fx.id, "match_no": fx.match_no, "round_no": fx.round_no,
        "league_phase": fx.league_phase,
        "stage": fx.stage, "stage_label": STAGE_LABEL.get(fx.stage, (fx.stage or "").title()),
        "status": fx.status,
        "team1": _team_brief(t1), "team2": _team_brief(t2),
        "slot1_label": fx.slot1_label or ("TBD" if t1 is None else None),
        "slot2_label": fx.slot2_label or ("TBD" if t2 is None else None),
        "home_team_id": fx.home_team_id, "venue": fx.venue,
        "pitch_type": fx.pitch_type,
        "day_night": day_night(fx),
        "score1": _score(fx.inn1_runs, fx.inn1_wickets, fx.inn1_balls),
        "score2": _score(fx.inn2_runs, fx.inn2_wickets, fx.inn2_balls),
        "winner_team_id": fx.winner_team_id,
        "result_text": fx.result_text,
        "completed_at": fx.completed_at.isoformat() if fx.completed_at else None,
        "has_scorecard": bool(fx.match_id or fx.scorecard_json),
        "is_next": fx.id == next_id,
    }


def tournament_payload(session, tour, tg_id=None):
    """Everything the Tournament tab shows except the stat boards."""
    from services import injury_service, qualification
    tid = tour.id
    mine = my_team_ids(session, tour, tg_id)
    teams = {tt.id: tt for tt in
             session.query(TournamentTeam).filter_by(tournament_id=tid).all()}

    group_tables = []
    if tour.league_format == "groups" and tour.group_points_mode == "separate":
        groups = (session.query(TournamentGroup).filter_by(tournament_id=tid)
                  .order_by(TournamentGroup.sort_order, TournamentGroup.id).all())
        group_tables = [{"id": g.id, "name": g.name,
                         "rows": _table_rows(session, tour, g.id, mine)}
                        for g in groups]

    # match_no first: it is continuous across stages, so league rounds sort
    # ahead of the knockout (whose round_no restarts at 1) — as on the admin
    # dashboard.
    fixtures = (session.query(TournamentMatch).filter_by(tournament_id=tid)
                .order_by(TournamentMatch.match_no, TournamentMatch.round_no,
                          TournamentMatch.id).all())
    # The schedule is released one league round at a time: later rounds are
    # held back (and counted) until the open round is finished.
    from services import league_schedule_service
    from services.tournament_match_settings import fixture_sections
    fixtures = [fx for _label, group in fixture_sections(fixtures) for fx in group]
    all_fixtures = fixtures
    fixtures, locked, progress = league_schedule_service.split_open_round(
        session, tid, all_fixtures)
    next_id = next((f.id for f in fixtures
                    if f.status == "scheduled" and f.team1_id and f.team2_id), None)

    played, total = tournament_service.league_progress(session, tid)
    champ = tournament_service.tournament_champion(session, tid)
    kind = tournament_service.tournament_kind(tour)

    injuries = []
    injuries_on = injury_service.enabled_for(tour)
    if injuries_on:
        for inj in injury_service.active_injuries(session, tid):
            tt = teams.get(inj.tournament_team_id)
            injuries.append({
                "player_name": inj.player_name, "team": tt.name if tt else None,
                "injury_type": inj.injury_type,
                "severity": inj.severity,
                "severity_label": injury_service.severity_label(inj.severity),
                "how": inj.how, "matches_out": inj.matches_out,
                "matches_remaining": inj.matches_remaining,
            })

    table_rows = _table_rows(session, tour, None, mine)
    return {
        "tournament": {
            "id": tid, "name": tour.name, "kind": kind,
            "kind_label": KIND_LABEL.get(kind, ""),
            "description": tour.description,
            "status": tour.status,
            "status_label": STATUS_LABEL.get((tour.status or "").lower(), tour.status),
            "format": tour.format, "overs": tour.overs,
            "league_format": tour.league_format,
            "league_match_target": tour.league_match_target,
            "qualify": qualification.spots_for(tour) or None,
            "points_win": tour.points_win, "points_tie": tour.points_tie,
            "points_loss": tour.points_loss,
            "points_no_result": tour.points_no_result,
            "teams": len(teams),
            "league_played": played, "league_total": total,
            "matches_played": sum(1 for f in all_fixtures if f.status == "completed"),
            "matches_total": len(all_fixtures),
            "current_round": progress["round"] if progress else None,
            "rounds_total": progress["rounds"] if progress else None,
            "round_played": progress["played"] if progress else None,
            "round_total": progress["total"] if progress else None,
            "release_mode": league_schedule_service.release_mode(tour),
            "rounds_per_batch": tour.fixture_round_window,
            "open_through": league_schedule_service.open_round(session, tid),
            "locked_fixtures": locked,
            "round_banner": league_schedule_service.round_banner(progress, locked, tour),
            "champion": _team_brief(champ),
        },
        "teams": [dict(_team_brief(team), played=team.played, won=team.won,
                       points=team.points, mine=team.id in mine,
                       has_roster=bool(team.challenge_team_id and tour.status != "completed"), stadium=team.home_stadium)
                  for team in sorted(teams.values(), key=lambda row: (row.name.casefold(), row.id))],
        "table": table_rows,
        "group_tables": group_tables,
        "points_footnote": [
            f"{r['name']}: {r['points_adjust']:+d} pts"
            + (f" — {r['points_adjust_note']}" if r["points_adjust_note"] else "")
            for r in table_rows if r["points_adjust"]],
        "fixtures": [_fixture(f, teams, next_id) for f in fixtures],
        "next_fixture_id": next_id,
        "injuries_enabled": injuries_on,
        "injuries": injuries,
        "my_team_ids": sorted(mine),
    }


# ──────────────────────────────────────────────────────────────────────
# Stat boards + MVP
# ──────────────────────────────────────────────────────────────────────

def _row(name, team, value, *cells, matches=None):
    return {"name": name, "team": team, "value": str(value),
            "matches": matches,
            "cells": [{"label": k, "value": v} for k, v in cells if v is not None]}


def stats_payload(session, tour, limit=15):
    """Every stat board (and the MVP race), computed in one pass."""
    lead = tournament_service.stat_leaders(session, tour.id, limit=limit)
    b = {}
    b["runs"] = [_row(r.name, r.team_name, r.bat_runs or 0,
                      ("Balls", str(r.bat_balls or 0)), ("SR", f"{_sr(r):.2f}"),
                      matches=r.matches) for r in lead["most_runs"] if r.bat_runs]
    b["wkts"] = [_row(r.name, r.team_name, r.bowl_wickets or 0,
                      ("Overs", overs(r.bowl_balls)), ("Econ", f"{_econ(r):.2f}"),
                      ("Best", _figure(r) or "—"),
                      matches=r.matches) for r in lead["most_wickets"] if r.bowl_wickets]
    b["hs"] = [_row(r.name, r.team_name,
                    f"{r.highest_score}{'*' if r.not_out else ''}",
                    ("Balls", str(r.bat_balls or 0)),
                    ("SR", f"{r.highest_score * 100.0 / r.bat_balls:.2f}"
                     if r.bat_balls else "—"))
               for r in lead["highest_score"]]
    b["fig"] = [_row(r.name, r.team_name, f"{r.best_bowl_wickets}/{r.best_bowl_runs}")
                for r in lead["best_figure"]]
    b["sixes"] = [_row(r.name, r.team_name, r.bat_sixes or 0,
                       ("Runs", str(r.bat_runs or 0)), ("SR", f"{_sr(r):.2f}"),
                       matches=r.matches) for r in lead["most_sixes"] if r.bat_sixes]
    b["fours"] = [_row(r.name, r.team_name, r.bat_fours or 0,
                       ("Runs", str(r.bat_runs or 0)), ("SR", f"{_sr(r):.2f}"),
                       matches=r.matches) for r in lead["most_fours"] if r.bat_fours]
    b["fifties"] = [_row(r.name, r.team_name, r.fifties, ("100s", str(r.hundreds)))
                    for r in lead["most_fifties"]]
    b["hundreds"] = [_row(r.name, r.team_name, r.hundreds, ("50s", str(r.fifties)))
                     for r in lead["most_hundreds"]]
    b["sr"] = [_row(r.name, r.team_name, f"{v:.2f}",
                    ("Runs", str(r.bat_runs or 0)), ("Balls", str(r.bat_balls or 0)),
                    matches=r.matches) for r, v in lead["best_strike_rate"]]
    b["econ"] = [_row(r.name, r.team_name, f"{v:.2f}",
                      ("Wkts", str(r.bowl_wickets or 0)), ("Overs", overs(r.bowl_balls)),
                      matches=r.matches) for r, v in lead["best_economy"]]
    b["avg"] = [_row(r.name, r.team_name, f"{v:.2f}",
                     ("Runs", str(r.bat_runs or 0)), ("Outs", str(r.bat_outs or 0)),
                     matches=r.matches) for r, v in lead["top_average"]]

    mvp = [{
        "name": r.name, "team": r.team_name, "points": r.points,
        "matches": r.matches, "bat_points": r.bat_points,
        "bowl_points": r.bowl_points, "result_points": r.result_points,
        "award_points": r.award_points, "wins": r.wins, "awards": r.awards,
        "bat_runs": r.bat_runs, "bowl_wickets": r.bowl_wickets,
    } for r in lead["mvp"]]

    return {
        "boards": [{"key": k, "label": label, "icon": icon, "unit": unit,
                    "rows": b.get(k, [])} for k, label, icon, unit in BOARDS],
        "mvp": mvp,
        "min_balls_for_sr": tour.min_balls_for_sr,
        "min_balls_for_econ": tour.min_balls_for_econ,
    }


# ──────────────────────────────────────────────────────────────────────
# One fixture's scorecard
# ──────────────────────────────────────────────────────────────────────

def fixture_scorecard(session, tour, fixture_id, user_id=None):
    """A fixture's scorecard in the Mini App drawer's ``innings`` shape.

    A match played through the bot has a full ``MatchScorecard`` (or a live
    state) behind ``match_id`` — that is served as-is. A result entered by hand
    or imported only has the per-player lines in ``scorecard_json``; those are
    grouped into two innings (team1 bats first, as ``inn1_*`` always is) with no
    dismissal text.
    """
    fx = session.get(TournamentMatch, int(fixture_id or 0))
    if fx is None or fx.tournament_id != tour.id:
        return {"ok": False, "message": "Match not found."}
    teams = {tt.id: tt for tt in session.query(TournamentTeam)
             .filter(TournamentTeam.id.in_([i for i in (fx.team1_id, fx.team2_id) if i]))
             .all()}
    t1 = teams.get(fx.team1_id)
    t2 = teams.get(fx.team2_id)
    head = {
        "fixture_id": fx.id, "match_no": fx.match_no,
        "stage_label": STAGE_LABEL.get(fx.stage, (fx.stage or "").title()),
        "team1": t1.name if t1 else (fx.slot1_label or "TBD"),
        "team2": t2.name if t2 else (fx.slot2_label or "TBD"),
    }

    if fx.match_id:
        try:
            from services.match_webapp_service import get_scorecard_any
            data = get_scorecard_any(session, fx.match_id, user_id)
            if data.get("ok") is not False and data.get("innings"):
                data["ok"] = True
                data.setdefault("result_text", fx.result_text)
                data.update(head)
                return data
        except Exception:
            logger.exception("tournament fixture %s: full scorecard failed", fx.id)

    try:
        lines = json.loads(fx.scorecard_json) if fx.scorecard_json else []
    except Exception:
        lines = []
    lines = [ln for ln in lines if isinstance(ln, dict)] if isinstance(lines, list) else []
    if not lines:
        if fx.status == "scheduled":
            return {"ok": False, "message": "This match hasn't been played yet."}
        return {"ok": False, "message": "No scorecard was recorded for this match."}

    # Group the lines by side, then decide which side is team1.
    order, by_team = [], {}
    for ln in lines:
        key = (ln.get("team_name") or "").strip()
        if key not in by_team:
            order.append(key)
            by_team[key] = []
        by_team[key].append(ln)
    name1 = (t1.name if t1 else "").strip().lower()
    first = next((k for k in order if k.lower() == name1), order[0])
    second = next((k for k in order if k != first), None)

    def batting(side):
        rows = []
        for ln in by_team.get(side, []):
            if not ln.get("batted") and not ln.get("bat_balls") and not ln.get("bat_runs"):
                continue
            runs, balls = int(ln.get("bat_runs") or 0), int(ln.get("bat_balls") or 0)
            rows.append({"name": ln.get("name") or "Player", "runs": runs, "balls": balls,
                         "fours": int(ln.get("bat_fours") or 0),
                         "sixes": int(ln.get("bat_sixes") or 0),
                         "how_out": "out" if ln.get("bat_out") else "not out",
                         "sr": round(runs * 100 / balls, 1) if balls else 0})
        return rows

    def bowling(side):
        rows = []
        for ln in by_team.get(side, []) if side is not None else []:
            balls = int(ln.get("bowl_balls") or 0)
            if not balls:
                continue
            runs = int(ln.get("bowl_runs") or 0)
            rows.append({"name": ln.get("name") or "Player", "overs": overs(balls),
                         "maidens": "—", "runs": runs,
                         "wickets": int(ln.get("bowl_wickets") or 0),
                         "econ": round(runs * 6 / balls, 2)})
        return rows

    innings = [
        {"number": 1, "bat_team": head["team1"] if t1 else (first or "Team 1"),
         "runs": fx.inn1_runs or 0, "wickets": fx.inn1_wickets or 0,
         "overs": overs(fx.inn1_balls), "batting": batting(first),
         "bowling": bowling(second)},
        {"number": 2, "bat_team": head["team2"] if t2 else (second or "Team 2"),
         "runs": fx.inn2_runs or 0, "wickets": fx.inn2_wickets or 0,
         "overs": overs(fx.inn2_balls), "batting": batting(second) if second else [],
         "bowling": bowling(first)},
    ]
    out = {"ok": True, "innings": innings, "current_innings": 1,
           "completed": fx.status == "completed", "result_text": fx.result_text,
           "summary_only": True}
    out.update(head)
    return out


def resolve_visible(session, tournament_id=None, include_completed=False):
    tour = resolve_live(session, tournament_id)
    if tour or not include_completed or not tournament_id:
        return tour
    archive = session.get(Tournament, int(tournament_id))
    return archive if archive and archive.status == "completed" else None


def tournament_career(session, user_id, telegram_id):
    """Career totals from published tournaments with recorded participation.

    For match records, named captains take precedence. Team registration is used
    on older or manually entered fixtures where a side has no recorded captain.
    Titles belong to the winning team members and the captain who won the final.
    """
    statuses = ("active", "paused", "completed")
    teams = (session.query(TournamentTeam).join(Tournament)
             .filter(Tournament.status.in_(statuses), or_(
                 TournamentTeam.user_tg_id == telegram_id,
                 TournamentTeam.owner_tg_id == telegram_id,
                 TournamentTeam.co_owner_ids_json.contains(str(telegram_id)))))
    owned = {team.id: team.tournament_id for team in teams if _represents_team(team, telegram_id)}
    entered = set(owned.values())
    wins = losses = 0
    match = TournamentMatch
    results = session.query(match).join(Tournament).filter(
        Tournament.status.in_(statuses),
        match.status == "completed",
        match.is_simulated.is_(False),
        match.team1_id.isnot(None), match.team2_id.isnot(None),
        or_(
            match.host_user_id == user_id, match.target_user_id == user_id,
            and_(match.host_user_id.is_(None), match.team1_id.in_(owned)),
            and_(match.target_user_id.is_(None), match.team2_id.in_(owned)),
        ),
    )
    for result in results:
        entered.add(result.tournament_id)
        if result.result_type in ("tie", "no_result"):
            continue
        if result.host_user_id == user_id:
            side = result.team1_id
        elif result.target_user_id == user_id:
            side = result.team2_id
        else:
            sides = [team_id for team_id, captain in (
                (result.team1_id, result.host_user_id), (result.team2_id, result.target_user_id)
            ) if captain is None and team_id in owned]
            if len(sides) != 1:
                continue
            side = sides[0]
        wins += result.winner_team_id == side
        losses += (result.winner_team_id in (result.team1_id, result.team2_id)
                   and result.winner_team_id != side)

    # Match the final selection used by the archive's champion card.
    finals = (session.query(match, Tournament).join(Tournament)
              .filter(Tournament.id.in_(entered), Tournament.status == "completed",
                      match.stage == "final", match.status == "completed",
                      match.winner_team_id.isnot(None))
              .order_by(Tournament.completed_at.desc().nullslast(), Tournament.id.desc(),
                        match.match_no.desc(), match.id.desc()))
    seen = set()
    titles = 0
    latest_title = None
    for final, tour in finals:
        if tour.id in seen:
            continue
        seen.add(tour.id)
        winning_captain = (final.host_user_id if final.winner_team_id == final.team1_id
                           else final.target_user_id if final.winner_team_id == final.team2_id else None)
        if final.winner_team_id in owned or winning_captain == user_id:
            titles += 1
            if latest_title is None:
                latest_title = {"id": tour.id, "name": tour.name}

    return {"played": len(entered), "titles": titles, "match_wins": wins,
            "match_losses": losses, "latest_title": latest_title}


def registration_open(tour):
    from datetime import datetime

    return (tournament_service.tournament_kind(tour) == "letsplay"
            and tour.status in ("draft", "scheduled") and not tour.schedule_generated
            and tour.registration_closes_at is not None
            and tour.registration_closes_at > datetime.utcnow())


def register_team(session, tour, user):
    from services.roster_service import get_roster_ordered
    from services.xi_rules import validate_roster_xi

    existing = session.query(TournamentTeam).filter_by(tournament_id=tour.id, user_tg_id=user.telegram_id).first()
    if existing:
        return {"ok": True, "team": existing.name, "entered": True}
    if not registration_open(tour):
        return {"ok": False, "message": "Registration has closed."}
    if session.query(TournamentTeam).filter_by(tournament_id=tour.id).count() >= tour.max_teams:
        return {"ok": False, "message": "This tournament is full."}
    players = [(row, player) for row, player in get_roster_ordered(session, user.id)[:11] if player.is_active]
    valid, errors = validate_roster_xi(players)
    if not valid:
        return {"ok": False, "message": " ".join(errors)}
    name = (user.team_name or user.first_name or user.username or "Player")[:120]
    session.add(TournamentTeam(tournament_id=tour.id, user_tg_id=user.telegram_id,
                               owner_tg_id=user.telegram_id, name=name))
    session.commit()
    return {"ok": True, "team": name, "entered": True}


def directory(session, user_id, telegram_id):
    """Featured competition, career totals and recent completed results."""
    active = featured_tournament(session)
    from datetime import datetime
    from services.miniapp_clubhouse import utc_string

    upcoming = (session.query(Tournament)
                .filter(Tournament.kind == "letsplay", Tournament.status.in_(("draft", "scheduled")),
                        Tournament.registration_closes_at > datetime.utcnow(),
                        Tournament.schedule_generated.is_(False))
                .order_by(Tournament.registration_closes_at).limit(20).all())
    completed = (session.query(Tournament).filter_by(status="completed")
                 .order_by(Tournament.id.desc()).limit(20).all())

    def card(tour):
        kind = tournament_service.tournament_kind(tour)
        summary = session.query(TournamentMatch.status, func.count(TournamentMatch.id)).filter_by(
            tournament_id=tour.id).group_by(TournamentMatch.status).all()
        counts = dict(summary)
        mine = my_team_ids(session, tour, telegram_id)
        return {
            "id": tour.id, "name": tour.name, "format": tour.format,
            "status": STATUS_LABEL.get(tour.status, tour.status),
            "kind_label": KIND_LABEL.get(kind, ""),
            "played": counts.get("completed", 0), "total": sum(counts.values()),
            "my_team": bool(mine),
            "registration_closes_at": utc_string(tour.registration_closes_at),
            "registration_open": registration_open(tour),
            "description": tour.description,
            "overs": tour.overs,
            "max_teams": tour.max_teams,
            "champion": _team_brief(tournament_service.tournament_champion(session, tour.id))
                        if tour.status == "completed" else None,
        }

    return {"ok": True, "current": [card(active)] if active else [],
            "upcoming": [card(t) for t in upcoming],
            "archive": [card(t) for t in completed],
            "career": tournament_career(session, user_id, telegram_id)}



def team_roster(session, tour, team_id):
    from models import ChallengePlayer
    from services.xi_rules import challenge_player_category, challenge_rating_or_none
    team = session.query(TournamentTeam).filter_by(tournament_id=tour.id, id=team_id).first()
    if not team:
        return None
    if tour.status == "completed" or not team.challenge_team_id:
        return []
    players = (session.query(ChallengePlayer)
               .filter_by(team_id=team.challenge_team_id)
               .order_by(ChallengePlayer.sort_order, ChallengePlayer.id).all())
    return [{"name": player.name, "role": challenge_player_category(player),
             "rating": challenge_rating_or_none(player), "overseas": player.is_overseas}
            for player in players]
