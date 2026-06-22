"""League Tournament schedule generation + fixture management.

This service turns a tournament's participating teams (and groups) into a set of
pre-created ``TournamentMatch`` *fixtures* (``status='scheduled'``) using the
round-robin circle method, and provides the editing + gating helpers used by the
admin panel and the bot.

Design notes
------------
* ``round_robin_rounds`` is pure (no DB) so it is unit-testable on its own.
* Fixtures are real ``TournamentMatch`` rows with ``status='scheduled'``; they
  only start counting toward standings once played (``status='completed'`` with
  ``stage in ('league','group')`` — see ``tournament_service.recompute_standings``).
* "A team can't play again once its match is done" is enforced by the bot gating
  on ``remaining_opponent_names`` / ``find_open_fixture``: a pair can only be
  played while an uncompleted scheduled fixture exists for it.
"""

import logging

logger = logging.getLogger(__name__)

# NOTE: SQLAlchemy / model imports are intentionally deferred into the DB-facing
# functions below so the pure ``round_robin_rounds`` helper (and its unit tests)
# can be imported without a database stack installed.


# ──────────────────────────────────────────────────────────────────────
# Pure round-robin generation (no DB)
# ──────────────────────────────────────────────────────────────────────

def round_robin_rounds(team_ids, double=False):
    """Return a list of rounds; each round is a list of ``(a, b)`` pairings.

    Uses the standard circle method: the first slot is fixed and the rest rotate,
    so every team plays exactly once per round. An odd team count gets a ``None``
    bye slot (that team sits out the round). ``double=True`` appends a mirrored
    second leg (home/away reversed).
    """
    teams = [t for t in team_ids]
    if len(teams) < 2:
        return []
    if len(teams) % 2:
        teams.append(None)  # bye marker
    n = len(teams)
    half = n // 2
    arr = list(teams)
    rounds = []
    for _ in range(n - 1):
        pairs = []
        for i in range(half):
            a, b = arr[i], arr[n - 1 - i]
            if a is not None and b is not None:
                pairs.append((a, b))
        rounds.append(pairs)
        # Rotate everything except the fixed first element.
        arr = [arr[0]] + [arr[-1]] + arr[1:-1]
    if double:
        rounds = rounds + [[(b, a) for (a, b) in pairs] for pairs in rounds]
    return rounds


# ──────────────────────────────────────────────────────────────────────
# Schedule generation (DB)
# ──────────────────────────────────────────────────────────────────────

def generate_schedule(session, tournament_id):
    """(Re)generate the fixture schedule for a tournament. Caller commits.

    Refuses to run if any match has already been completed (that would orphan
    recorded results); reset the tournament first. Clears existing
    scheduled/live fixtures, then creates fresh ones based on ``league_format``.
    Returns the number of fixtures created.
    """
    from models import Tournament, TournamentTeam, TournamentGroup, TournamentMatch
    tid = int(tournament_id)
    tour = session.query(Tournament).get(tid)
    if not tour:
        return 0

    completed = (session.query(TournamentMatch)
                 .filter_by(tournament_id=tid)
                 .filter(TournamentMatch.status == "completed").count())
    if completed:
        raise ValueError(
            "Completed matches exist — reset the tournament before regenerating the schedule.")

    # Drop any prior scheduled/live fixtures so regeneration is clean.
    (session.query(TournamentMatch)
     .filter_by(tournament_id=tid)
     .filter(TournamentMatch.status != "completed")
     .delete(synchronize_session=False))

    teams = (session.query(TournamentTeam).filter_by(tournament_id=tid)
             .order_by(TournamentTeam.sort_order, TournamentTeam.id).all())
    fmt = tour.league_format or "single_rr"
    match_no = 0
    created = 0

    def _emit(pairs_by_round, *, stage, group_id):
        nonlocal match_no, created
        for rno, pairs in enumerate(pairs_by_round, start=1):
            for (a, b) in pairs:
                match_no += 1
                session.add(TournamentMatch(
                    tournament_id=tid, team1_id=a, team2_id=b,
                    status="scheduled", stage=stage, group_id=group_id,
                    round_no=rno, match_no=match_no))
                created += 1

    if fmt == "groups":
        groups = (session.query(TournamentGroup).filter_by(tournament_id=tid)
                  .order_by(TournamentGroup.sort_order, TournamentGroup.id).all())
        for g in groups:
            gteam_ids = [t.id for t in teams if t.group_id == g.id]
            rounds = round_robin_rounds(gteam_ids, double=(g.rr_mode == "double"))
            _emit(rounds, stage="group", group_id=g.id)
    else:
        team_ids = [t.id for t in teams]
        rounds = round_robin_rounds(team_ids, double=(fmt == "double_rr"))
        _emit(rounds, stage="league", group_id=None)

    tour.schedule_generated = True
    session.flush()
    logger.info("Generated %s fixtures for tournament %s (%s)", created, tid, fmt)
    return created


# ──────────────────────────────────────────────────────────────────────
# Fixture editing (DB)
# ──────────────────────────────────────────────────────────────────────

def add_fixture(session, tournament_id, team1_id, team2_id, group_id=None, round_no=0):
    """Add a single scheduled fixture. Caller commits."""
    from sqlalchemy import func
    from models import Tournament, TournamentMatch
    tid = int(tournament_id)
    tour = session.query(Tournament).get(tid)
    stage = "group" if (tour and tour.league_format == "groups") else "league"
    max_no = (session.query(func.max(TournamentMatch.match_no))
              .filter_by(tournament_id=tid).scalar()) or 0
    tm = TournamentMatch(
        tournament_id=tid,
        team1_id=int(team1_id) if team1_id else None,
        team2_id=int(team2_id) if team2_id else None,
        status="scheduled", stage=stage,
        group_id=int(group_id) if group_id else None,
        round_no=int(round_no or 0), match_no=max_no + 1)
    session.add(tm)
    session.flush()
    return tm


def delete_fixture(session, fixture_id):
    """Delete a scheduled fixture (not a completed one). Caller commits."""
    from models import TournamentMatch
    tm = session.query(TournamentMatch).get(int(fixture_id))
    if not tm:
        return None
    if tm.status == "completed":
        raise ValueError("Use the match-delete action to remove a completed match.")
    tid = tm.tournament_id
    session.delete(tm)
    session.flush()
    return tid


def swap_fixture_team(session, fixture_id, slot, new_team_id):
    """Replace one side of a scheduled fixture. ``slot`` is 1 or 2. Caller commits."""
    from models import TournamentMatch
    tm = session.query(TournamentMatch).get(int(fixture_id))
    if not tm:
        return None
    if tm.status == "completed":
        raise ValueError("Cannot edit a completed match.")
    new_id = int(new_team_id) if new_team_id else None
    if int(slot) == 1:
        tm.team1_id = new_id
    else:
        tm.team2_id = new_id
    session.flush()
    return tm


def list_fixtures(session, tournament_id):
    """All fixtures (scheduled + completed) ordered for display."""
    from models import TournamentMatch
    return (session.query(TournamentMatch)
            .filter_by(tournament_id=int(tournament_id))
            .order_by(TournamentMatch.round_no, TournamentMatch.match_no,
                      TournamentMatch.id).all())


# ──────────────────────────────────────────────────────────────────────
# Gating helpers (used by the bot)
# ──────────────────────────────────────────────────────────────────────

def find_open_fixture(session, tournament_id, team1_id, team2_id):
    """The earliest uncompleted scheduled fixture for an (unordered) pair, or None."""
    from sqlalchemy import or_, and_
    from models import TournamentMatch
    tid = int(tournament_id)
    a, b = int(team1_id), int(team2_id)
    return (session.query(TournamentMatch)
            .filter_by(tournament_id=tid)
            .filter(TournamentMatch.status != "completed")
            .filter(or_(
                and_(TournamentMatch.team1_id == a, TournamentMatch.team2_id == b),
                and_(TournamentMatch.team1_id == b, TournamentMatch.team2_id == a)))
            .order_by(TournamentMatch.round_no, TournamentMatch.match_no).first())


def remaining_opponents(session, tournament_id, tournament_team_id):
    """Set of TournamentTeam ids that still have an open fixture vs this team."""
    from sqlalchemy import or_
    from models import TournamentMatch
    tid = int(tournament_id)
    ttid = int(tournament_team_id)
    rows = (session.query(TournamentMatch)
            .filter_by(tournament_id=tid)
            .filter(TournamentMatch.status != "completed")
            .filter(or_(TournamentMatch.team1_id == ttid,
                        TournamentMatch.team2_id == ttid)).all())
    opp = set()
    for m in rows:
        if m.team1_id == ttid and m.team2_id:
            opp.add(m.team2_id)
        elif m.team2_id == ttid and m.team1_id:
            opp.add(m.team1_id)
    return opp


def remaining_opponent_names(session, tournament_id, team_name):
    """Set of opponent team *names* with an open fixture vs ``team_name``.

    The bot's team picker works with names, so this maps the id-based gate to the
    name space. Returns an empty set if the team has no remaining fixtures.
    """
    from models import TournamentTeam
    tid = int(tournament_id)
    tt = (session.query(TournamentTeam)
          .filter_by(tournament_id=tid)
          .filter(TournamentTeam.name == team_name).first())
    if not tt:
        return set()
    opp_ids = remaining_opponents(session, tid, tt.id)
    if not opp_ids:
        return set()
    rows = (session.query(TournamentTeam)
            .filter(TournamentTeam.id.in_(opp_ids)).all())
    return {(r.name or "").strip() for r in rows if (r.name or "").strip()}
