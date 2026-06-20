"""Challenge League Tournament service.

Central logic shared by the admin panel and the bot:
  • lifecycle / single-active-tournament rule,
  • recording a completed tournament match (standings + per-player stats),
  • leaderboard / points-table queries for the dashboard.

Only one Tournament may be ``is_active`` at a time; activating one deactivates
all others. Completed / inactive tournaments retain all their rows untouched.
"""

import json
import logging
from datetime import datetime

from models import (
    Tournament, TournamentTeam, TournamentMatch, TournamentPlayerStats,
    ChallengeLeague, ChallengeTeam,
)

logger = logging.getLogger(__name__)

# Statuses in which the tournament command may actually start a match.
PLAYABLE_STATUS = "active"
TERMINAL_STATUSES = ("completed", "cancelled")


# ──────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────

def get_active_tournament(session):
    """Return the single currently-active tournament, or None."""
    return (session.query(Tournament)
            .filter(Tournament.is_active == True)  # noqa: E712
            .order_by(Tournament.activated_at.desc(), Tournament.id.desc())
            .first())


def activate_tournament(session, tournament_id):
    """Make ``tournament_id`` the one active tournament (deactivating others)."""
    tour = session.query(Tournament).get(int(tournament_id))
    if not tour:
        return None
    # Deactivate every other tournament — enforces the single-active rule.
    session.query(Tournament).filter(
        Tournament.id != tour.id, Tournament.is_active == True  # noqa: E712
    ).update({Tournament.is_active: False}, synchronize_session=False)
    tour.is_active = True
    if tour.status in ("draft", "scheduled"):
        tour.status = "active"
    tour.activated_at = datetime.utcnow()
    return tour


def deactivate_tournament(session, tournament_id):
    tour = session.query(Tournament).get(int(tournament_id))
    if tour:
        tour.is_active = False
    return tour


def set_status(session, tournament_id, status):
    """Update lifecycle status (start/pause/resume/complete/cancel)."""
    tour = session.query(Tournament).get(int(tournament_id))
    if not tour:
        return None
    tour.status = status
    if status in TERMINAL_STATUSES:
        # Keep ``is_active`` so the tournament command still reports the precise
        # "already completed / cancelled" message (spec §4). The admin clears the
        # active slot by activating another tournament or pressing Deactivate.
        tour.completed_at = datetime.utcnow()
    elif status == "active":
        tour.activated_at = tour.activated_at or datetime.utcnow()
    return tour


def reset_tournament(session, tournament_id):
    """Clear all match records, player stats and standings — keep config/teams."""
    tid = int(tournament_id)
    session.query(TournamentMatch).filter_by(tournament_id=tid).delete(synchronize_session=False)
    session.query(TournamentPlayerStats).filter_by(tournament_id=tid).delete(synchronize_session=False)
    for tt in session.query(TournamentTeam).filter_by(tournament_id=tid).all():
        tt.played = tt.won = tt.lost = tt.tied = tt.no_result = tt.points = 0
        tt.runs_for = tt.balls_for = tt.runs_against = tt.balls_against = 0
    tour = session.query(Tournament).get(tid)
    if tour and tour.status not in ("cancelled",):
        tour.status = "draft" if not tour.is_active else "active"
    return tour


# ──────────────────────────────────────────────────────────────────────
# Participating-team helpers
# ──────────────────────────────────────────────────────────────────────

def tournament_team_for_challenge_team(session, tournament_id, challenge_team_id):
    if not challenge_team_id:
        return None
    return (session.query(TournamentTeam)
            .filter_by(tournament_id=int(tournament_id),
                       challenge_team_id=int(challenge_team_id))
            .first())


def participating_team_names(session, tournament_id):
    """Return the set of participating ChallengeTeam names for a tournament."""
    rows = session.query(TournamentTeam).filter_by(tournament_id=int(tournament_id)).all()
    return {(r.name or "").strip() for r in rows if (r.name or "").strip()}


def pairing_already_played(session, tournament_id, cid_a, cid_b):
    """True if these two participating teams already completed a tournament match."""
    ta = tournament_team_for_challenge_team(session, tournament_id, cid_a)
    tb = tournament_team_for_challenge_team(session, tournament_id, cid_b)
    if not ta or not tb:
        return False
    q = session.query(TournamentMatch).filter(
        TournamentMatch.tournament_id == int(tournament_id),
        TournamentMatch.team1_id.in_([ta.id, tb.id]),
        TournamentMatch.team2_id.in_([ta.id, tb.id]),
    )
    return q.first() is not None


# ──────────────────────────────────────────────────────────────────────
# Recording a completed match
# ──────────────────────────────────────────────────────────────────────

def _legal_balls(stats_map):
    """Sum legal balls faced/bowled from a roster-keyed stats map."""
    total = 0
    for st in (stats_map or {}).values():
        total += int(st.get("balls", 0) or 0)
    return total


def _collect_player_lines(state):
    """Aggregate per (user_id, player identity) batting+bowling for this match.

    Mirrors the aggregation in handlers/match.py: each XI list belongs to one
    user; a player bats in one innings and bowls in the other, so figures are
    combined per user. Returns a list of dicts.
    """
    name_by_user = {}
    bat_uid = state.get("bat_team_id")
    bowl_uid = state.get("bowl_team_id")
    if bat_uid is not None:
        name_by_user[int(bat_uid)] = state.get("bat_team_name")
    if bowl_uid is not None:
        name_by_user[int(bowl_uid)] = state.get("bowl_team_name")

    # (xi, stats, user_id, kind)
    feeds = [
        (state.get("inn1_bat_xi"), state.get("inn1_bat_stats"), state.get("inn1_bat_team_id"), "bat"),
        (state.get("inn1_bowl_xi"), state.get("inn1_bowl_stats"), state.get("inn1_bowl_team_id"), "bowl"),
        (state.get("bat_xi"), state.get("bat_stats"), state.get("bat_team_id"), "bat"),
        (state.get("bowl_xi"), state.get("bowl_stats"), state.get("bowl_team_id"), "bowl"),
    ]

    agg = {}  # key -> entry
    for xi, stats, uid, kind in feeds:
        if not xi or uid is None:
            continue
        uid = int(uid)
        stats = stats or {}
        for p in xi:
            rid = p.get("roster_id")
            pid = p.get("player_id")
            key = (uid, pid if pid is not None else ("r", rid))
            entry = agg.setdefault(key, {
                "user_id": uid, "player_id": pid, "roster_id": rid,
                "name": p.get("name", "Player"),
                "team_name": name_by_user.get(uid),
                "bat_runs": 0, "bat_balls": 0, "bat_fours": 0, "bat_sixes": 0,
                "bat_out": False, "batted": False,
                "bowl_wickets": 0, "bowl_runs": 0, "bowl_balls": 0, "bowled": False,
            })
            st = stats.get(str(rid), {}) or {}
            if kind == "bat":
                entry["bat_runs"] += int(st.get("runs", 0) or 0)
                entry["bat_balls"] += int(st.get("balls", 0) or 0)
                entry["bat_fours"] += int(st.get("fours", 0) or 0)
                entry["bat_sixes"] += int(st.get("sixes", 0) or 0)
                entry["bat_out"] = entry["bat_out"] or bool(st.get("out"))
                if st.get("balls") or st.get("out"):
                    entry["batted"] = True
            else:
                entry["bowl_wickets"] += int(st.get("wickets", 0) or 0)
                entry["bowl_runs"] += int(st.get("runs", 0) or 0)
                entry["bowl_balls"] += int(st.get("balls", 0) or 0)
                if st.get("balls"):
                    entry["bowled"] = True
    return list(agg.values())


def _better_figure(new_w, new_r, cur_w, cur_r):
    """True if (new_w/new_r) is a better bowling figure than (cur_w/cur_r)."""
    if cur_r < 0:
        return True
    if new_w != cur_w:
        return new_w > cur_w
    return new_r < cur_r


def _upsert_player_stats(session, tournament_id, line):
    pid = line.get("player_id")
    rid = line.get("roster_id")
    q = session.query(TournamentPlayerStats).filter_by(
        tournament_id=tournament_id, user_id=line["user_id"])
    if pid is not None:
        row = q.filter(TournamentPlayerStats.player_id == pid).first()
    else:
        row = q.filter(TournamentPlayerStats.player_id.is_(None),
                       TournamentPlayerStats.roster_id == rid).first()
    if not row:
        row = TournamentPlayerStats(
            tournament_id=tournament_id, user_id=line["user_id"],
            player_id=pid, roster_id=rid, name=line.get("name"),
            team_name=line.get("team_name"), best_bowl_runs=-1)
        session.add(row)
    # Refresh display snapshots
    row.name = line.get("name") or row.name
    if line.get("team_name"):
        row.team_name = line["team_name"]

    featured = line["batted"] or line["bowled"]
    if featured:
        row.matches = (row.matches or 0) + 1
    if line["batted"]:
        row.bat_innings = (row.bat_innings or 0) + 1
        row.bat_runs = (row.bat_runs or 0) + line["bat_runs"]
        row.bat_balls = (row.bat_balls or 0) + line["bat_balls"]
        row.bat_fours = (row.bat_fours or 0) + line["bat_fours"]
        row.bat_sixes = (row.bat_sixes or 0) + line["bat_sixes"]
        if line["bat_out"]:
            row.bat_outs = (row.bat_outs or 0) + 1
        if line["bat_runs"] > (row.highest_score or 0):
            row.highest_score = line["bat_runs"]
    if line["bowled"]:
        row.bowl_innings = (row.bowl_innings or 0) + 1
        row.bowl_wickets = (row.bowl_wickets or 0) + line["bowl_wickets"]
        row.bowl_runs = (row.bowl_runs or 0) + line["bowl_runs"]
        row.bowl_balls = (row.bowl_balls or 0) + line["bowl_balls"]
        if _better_figure(line["bowl_wickets"], line["bowl_runs"],
                          row.best_bowl_wickets or 0,
                          row.best_bowl_runs if row.best_bowl_runs is not None else -1):
            row.best_bowl_wickets = line["bowl_wickets"]
            row.best_bowl_runs = line["bowl_runs"]


def record_tournament_match(session, state, winner_user_id=None, result_text=None):
    """Record a completed tournament match: standings + per-player stats.

    Idempotent on ``match_id``. ``winner_user_id`` overrides result derivation
    (used by the Super Over decider); otherwise the winner is derived from the
    innings totals. Call inside an open session; the caller commits.
    """
    tid = state.get("tournament_id")
    if not tid:
        return None
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return None

    match_id = state.get("match_id")
    if match_id and session.query(TournamentMatch).filter_by(match_id=match_id).first():
        return None  # already recorded

    tour = session.query(Tournament).get(tid)
    if not tour:
        return None

    team_by_user = {int(k): v for k, v in (state.get("tournament_team_by_user") or {}).items() if v}

    inn1_bat_uid = state.get("inn1_bat_team_id")
    inn2_bat_uid = state.get("bat_team_id")
    inn1_bowl_uid = state.get("inn1_bowl_team_id")
    inn1_runs = int(state.get("inn1_runs") or 0)
    inn1_wkts = int(state.get("inn1_wickets") or 0)
    inn2_runs = int(state.get("total_runs") or 0)
    inn2_wkts = int(state.get("total_wickets") or 0)
    inn1_balls = _legal_balls(state.get("inn1_bat_stats"))
    inn2_balls = _legal_balls(state.get("bat_stats"))

    # Winner
    if winner_user_id is not None:
        win_uid = int(winner_user_id)
    elif inn1_runs == inn2_runs:
        win_uid = None  # tie
    else:
        win_uid = int(inn1_bat_uid) if inn1_runs > inn2_runs else int(inn2_bat_uid)

    def tteam(uid):
        return tournament_team_for_challenge_team(session, tid, team_by_user.get(int(uid))) \
            if uid is not None else None

    t_inn1 = tteam(inn1_bat_uid)       # team that batted first
    t_inn2 = tteam(inn2_bat_uid)       # team that batted second
    win_team = tteam(win_uid) if win_uid is not None else None

    # ── Standings ──
    for tt in (t_inn1, t_inn2):
        if tt:
            tt.played = (tt.played or 0) + 1
    if t_inn1:
        t_inn1.runs_for = (t_inn1.runs_for or 0) + inn1_runs
        t_inn1.balls_for = (t_inn1.balls_for or 0) + inn1_balls
        t_inn1.runs_against = (t_inn1.runs_against or 0) + inn2_runs
        t_inn1.balls_against = (t_inn1.balls_against or 0) + inn2_balls
    if t_inn2:
        t_inn2.runs_for = (t_inn2.runs_for or 0) + inn2_runs
        t_inn2.balls_for = (t_inn2.balls_for or 0) + inn2_balls
        t_inn2.runs_against = (t_inn2.runs_against or 0) + inn1_runs
        t_inn2.balls_against = (t_inn2.balls_against or 0) + inn1_balls

    if win_uid is None:  # tie
        for tt in (t_inn1, t_inn2):
            if tt:
                tt.tied = (tt.tied or 0) + 1
                tt.points = (tt.points or 0) + (tour.points_tie or 0)
    else:
        lose_team = t_inn2 if win_team is t_inn1 else t_inn1
        if win_team:
            win_team.won = (win_team.won or 0) + 1
            win_team.points = (win_team.points or 0) + (tour.points_win or 0)
        if lose_team:
            lose_team.lost = (lose_team.lost or 0) + 1
            lose_team.points = (lose_team.points or 0) + (tour.points_loss or 0)

    # ── Per-player stats ──
    lines = _collect_player_lines(state)
    for line in lines:
        _upsert_player_stats(session, tid, line)

    # ── Match row ──
    if not result_text:
        if win_uid is None:
            result_text = "Match Tied"
        else:
            wname = (win_team.name if win_team else None) or "Winner"
            result_text = f"{wname} won"

    tm = TournamentMatch(
        tournament_id=tid,
        match_id=match_id,
        team1_id=t_inn1.id if t_inn1 else None,
        team2_id=t_inn2.id if t_inn2 else None,
        winner_team_id=win_team.id if win_team else None,
        stage="league",
        result_text=result_text[:300] if result_text else None,
        inn1_runs=inn1_runs, inn1_wickets=inn1_wkts, inn1_balls=inn1_balls,
        inn2_runs=inn2_runs, inn2_wickets=inn2_wkts, inn2_balls=inn2_balls,
        host_user_id=inn1_bat_uid, target_user_id=inn2_bat_uid,
        scorecard_json=json.dumps(lines, default=str),
        completed_at=datetime.utcnow(),
    )
    session.add(tm)
    logger.info("Recorded tournament match for tournament %s (match_id=%s)", tid, match_id)
    return tm


# ──────────────────────────────────────────────────────────────────────
# Dashboard queries
# ──────────────────────────────────────────────────────────────────────

def points_table(session, tournament_id):
    """Teams ordered by points, then net run-rate."""
    rows = session.query(TournamentTeam).filter_by(tournament_id=int(tournament_id)).all()

    def nrr(tt):
        of = (tt.balls_for or 0) / 6.0
        oa = (tt.balls_against or 0) / 6.0
        rf = (tt.runs_for or 0) / of if of else 0.0
        ra = (tt.runs_against or 0) / oa if oa else 0.0
        return rf - ra

    out = []
    for tt in rows:
        tt._nrr = round(nrr(tt), 3)
        out.append(tt)
    out.sort(key=lambda t: (t.points or 0, t._nrr), reverse=True)
    return out


def _stat_rows(session, tournament_id):
    return session.query(TournamentPlayerStats).filter_by(
        tournament_id=int(tournament_id)).all()


def stat_leaders(session, tournament_id, limit=10):
    """Return a dict of leaderboard lists for the dashboard."""
    rows = _stat_rows(session, tournament_id)
    tour = session.query(Tournament).get(int(tournament_id))
    min_sr = (tour.min_balls_for_sr if tour else 20) or 0
    min_econ = (tour.min_balls_for_econ if tour else 12) or 0

    def top(key, n=limit, reverse=True, filt=None):
        pool = [r for r in rows if (filt is None or filt(r))]
        pool.sort(key=key, reverse=reverse)
        return pool[:n]

    def sr(r):
        return (r.bat_runs / r.bat_balls * 100.0) if r.bat_balls else 0.0

    def econ(r):
        overs = (r.bowl_balls or 0) / 6.0
        return (r.bowl_runs / overs) if overs else 0.0

    return {
        "most_runs": top(lambda r: (r.bat_runs or 0, r.bat_sixes or 0)),
        "most_wickets": top(lambda r: (r.bowl_wickets or 0, -(r.bowl_runs or 0))),
        "most_sixes": top(lambda r: (r.bat_sixes or 0)),
        "most_fours": top(lambda r: (r.bat_fours or 0)),
        "highest_score": top(lambda r: (r.highest_score or 0)),
        "best_figure": top(lambda r: (r.best_bowl_wickets or 0,
                                      -(r.best_bowl_runs if r.best_bowl_runs is not None and r.best_bowl_runs >= 0 else 9999)),
                           filt=lambda r: (r.best_bowl_runs is not None and r.best_bowl_runs >= 0)),
        "best_strike_rate": [(r, round(sr(r), 2)) for r in top(
            sr, filt=lambda r: (r.bat_balls or 0) >= min_sr)],
        "best_economy": [(r, round(econ(r), 2)) for r in top(
            econ, reverse=False, filt=lambda r: (r.bowl_balls or 0) >= min_econ)],
    }
