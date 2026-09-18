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
* A fixture also carries its *venue*: which side is at home, and — when the
  tournament's ``pitch_mode`` says so — the one surface it may be played on.
  See ``assign_fixture_venues`` and ``locked_pitch_for_pair``.
* ``services.pitch_report`` is imported at module level on purpose: it is pure
  Python (no database, no models), so it does not cost the pure-import property
  the note below protects.
"""

import logging
import random

from services.pitch_report import PITCH_TYPES

logger = logging.getLogger(__name__)

# Surfaces the fixture generator may draw from. Same list the host's pitch
# picker offers, so a fixture-locked surface is always one players recognise.
FIXTURE_PITCHES = list(PITCH_TYPES)

# ``Tournament.pitch_mode`` values.
PITCH_MODE_HOST = "host"        # host picks during setup (original behaviour)
PITCH_MODE_FIXTURE = "fixture"  # each fixture carries its own surface
PITCH_MODE_HOME = "home"        # ditto, seeded from the home team's home_pitch
PITCH_MODES = (PITCH_MODE_HOST, PITCH_MODE_FIXTURE, PITCH_MODE_HOME)


def pitch_locked(tour):
    """True when fixtures — not the host — decide the surface for ``tour``."""
    return (getattr(tour, "pitch_mode", PITCH_MODE_HOST) or PITCH_MODE_HOST) \
        in (PITCH_MODE_FIXTURE, PITCH_MODE_HOME)

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
# Per-fixture venue: home side + the surface the match must be played on
# ──────────────────────────────────────────────────────────────────────

def _home_pitch_map(session, tournament_id):
    """``{TournamentTeam.id: home_pitch}`` for teams that declared one."""
    from models import TournamentTeam
    rows = (session.query(TournamentTeam)
            .filter_by(tournament_id=int(tournament_id)).all())
    return {r.id: (r.home_pitch or "").strip() for r in rows
            if (r.home_pitch or "").strip() in FIXTURE_PITCHES}


def pick_pitch(mode, home_team_id=None, home_pitches=None, rng=None):
    """The surface for one fixture, given the tournament's ``pitch_mode``.

    In ``home`` mode the home side's declared ``home_pitch`` wins; a team that
    never declared one falls back to a random surface, so a half-configured
    tournament still produces a complete, playable schedule.
    """
    rng = rng or random
    if mode == PITCH_MODE_HOME and home_team_id and home_pitches:
        declared = home_pitches.get(home_team_id)
        if declared:
            return declared
    return rng.choice(FIXTURE_PITCHES)


def assign_fixture_venues(session, tournament_id, *, overwrite=False):
    """Give every unplayed fixture a home side and a surface. Caller commits.

    Only fixtures that are still ``scheduled`` are touched — a live or completed
    match has already been played on whatever surface it was given, and rewriting
    that would rewrite history. ``overwrite`` re-rolls the *pitch* only: a
    home side an admin chose — including a deliberate "neutral venue" — is never
    overwritten, so re-rolling pitches can't silently undo the home/away edits.
    Returns the number of fields changed.
    """
    from models import Tournament, TournamentMatch
    tid = int(tournament_id)
    tour = session.get(Tournament, tid)
    if not tour:
        return 0
    mode = (tour.pitch_mode or PITCH_MODE_HOST)
    home_pitches = _home_pitch_map(session, tid) if mode == PITCH_MODE_HOME else {}
    changed = 0
    for fx in (session.query(TournamentMatch)
               .filter_by(tournament_id=tid, status="scheduled").all()):
        # The home side defaults to slot 1 — the round-robin generator already
        # alternates the slots between legs, so this gives a balanced home/away
        # split for free. A dangling reference (the team was swapped out) is
        # always repaired; an empty one is only filled on the ordinary pass, so
        # an explicit re-roll of the pitches leaves "neutral venue" alone.
        dangling = (fx.home_team_id is not None
                    and fx.home_team_id not in (fx.team1_id, fx.team2_id))
        if dangling or (fx.home_team_id is None and not overwrite):
            new_home = fx.team1_id or fx.team2_id
            if new_home != fx.home_team_id:
                fx.home_team_id = new_home
                changed += 1
        if mode == PITCH_MODE_HOST:
            # The host picks during setup. Leave existing stamps alone on a
            # normal pass, but an explicit overwrite (an admin switching the
            # mode back) clears them, so a stale surface can't resurface if the
            # mode is later flipped again.
            if overwrite and fx.pitch_type:
                fx.pitch_type = None
                changed += 1
            continue
        if overwrite or not (fx.pitch_type or "").strip():
            fx.pitch_type = pick_pitch(mode, fx.home_team_id, home_pitches)
            changed += 1
    if changed:
        session.flush()
    return changed


def set_fixture_pitch(session, fixture_id, pitch):
    """Pin (or clear, with a falsy ``pitch``) one fixture's surface. Caller commits."""
    from models import TournamentMatch
    fx = session.get(TournamentMatch, int(fixture_id))
    if not fx:
        return None
    if fx.status == "completed":
        raise ValueError("That match has already been played — its pitch is history.")
    name = (pitch or "").strip()
    if name and name not in FIXTURE_PITCHES:
        raise ValueError("Unknown pitch %r. Choose one of: %s"
                         % (pitch, ", ".join(FIXTURE_PITCHES)))
    fx.pitch_type = name or None
    session.flush()
    return fx


def set_fixture_home(session, fixture_id, team_id):
    """Set which side is at home. Must be one of the fixture's two teams. Caller commits."""
    from models import TournamentMatch
    fx = session.get(TournamentMatch, int(fixture_id))
    if not fx:
        return None
    if fx.status == "completed":
        raise ValueError("That match has already been played.")
    if not team_id:
        fx.home_team_id = None
        session.flush()
        return fx
    tid = int(team_id)
    if tid not in (fx.team1_id, fx.team2_id):
        raise ValueError("The home team must be one of the two teams in the fixture.")
    fx.home_team_id = tid
    session.flush()
    return fx


def fixture_for_pair(session, tournament_id, name1, name2):
    """The earliest open fixture between two team *names*, or None.

    Name-based because the bot's team picker works in names; used during setup
    to read the fixture's locked surface and home side before the match is
    actually reserved at the toss.
    """
    tid = int(tournament_id)
    a = _team_id_by_name(session, tid, name1)
    b = _team_id_by_name(session, tid, name2)
    if not a or not b:
        return None
    return find_open_fixture(session, tid, a, b)


def locked_pitch_for_pair(session, tour, name1, name2):
    """``(pitch, home_team_name)`` fixed for this pairing, or ``(None, None)``.

    Returns nothing when the tournament lets the host choose, when there is no
    generated schedule, or when the fixture was never given a surface — every one
    of which means "fall back to the host's pitch picker".
    """
    if not tour or not pitch_locked(tour):
        return None, None
    if not (tour.schedule_generated or tour.knockout_generated):
        return None, None
    fx = fixture_for_pair(session, tour.id, name1, name2)
    if fx is None:
        return None, None
    pitch = (fx.pitch_type or "").strip() or None
    home = None
    if fx.home_team_id:
        from models import TournamentTeam
        row = session.get(TournamentTeam, int(fx.home_team_id))
        home = (row.name or "").strip() if row else None
    return pitch, home


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
    tour = session.get(Tournament, tid)
    if not tour:
        return 0

    # A Pure Knockout has no league stage — the round-robin generator would delete
    # its bracket and replace it with league fixtures, so refuse here.
    if (tour.knockout_type or "") == "pure_knockout":
        raise ValueError(
            "This is a Pure Knockout tournament — use “Generate bracket” instead "
            "of the league schedule generator.")

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
        """Write scheduled TournamentMatch rows for a list of rounds."""
        nonlocal match_no, created
        for rno, pairs in enumerate(pairs_by_round, start=1):
            for (a, b) in pairs:
                match_no += 1
                session.add(TournamentMatch(
                    tournament_id=tid, team1_id=a, team2_id=b,
                    # Slot 1 hosts. The circle method already mirrors the slots
                    # in a double round-robin, so each pair gets one home leg
                    # each without any extra bookkeeping.
                    home_team_id=a,
                    status="scheduled", stage=stage, group_id=group_id,
                    round_no=rno, match_no=match_no))
                created += 1

    if fmt == "groups":
        # Every participating team must belong to a group, else it would be
        # silently dropped from fixtures and then blocked by fixture gating.
        unassigned = [t.name for t in teams if t.group_id is None]
        if unassigned:
            raise ValueError(
                "Assign every team to a group first. Unassigned: %s"
                % ", ".join(unassigned))
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

    if created == 0:
        raise ValueError("No fixtures were generated — add at least two teams "
                         "(and, for groups, at least two teams per group).")
    tour.schedule_generated = True
    session.flush()
    # Give every new fixture its surface (a no-op while pitch_mode is "host").
    # The rows were just created, so nothing existing is overwritten.
    assign_fixture_venues(session, tid)
    logger.info("Generated %s fixtures for tournament %s (%s)", created, tid, fmt)
    return created


def generate_series(session, tournament_id, matches):
    """Generate an N-match head-to-head series between the two teams. Caller commits.

    Used by the "Custom Series" format (best-of 3/5/7). Requires exactly two
    participating teams; home/away alternates each match. Fixtures are ordinary
    league rows so the points table (and its leader = series winner) works as
    usual. Refuses to run if a completed match already exists.
    """
    from models import Tournament, TournamentTeam, TournamentMatch
    tid = int(tournament_id)
    n = max(1, min(15, int(matches or 1)))

    if (session.query(TournamentMatch).filter_by(tournament_id=tid)
            .filter(TournamentMatch.status == "completed").count()):
        raise ValueError(
            "Completed matches exist — reset the tournament before regenerating.")

    # Validate the team count BEFORE deleting anything, so a bad call can't wipe
    # the existing schedule and then raise (the create handler swallows the error).
    teams = (session.query(TournamentTeam).filter_by(tournament_id=tid)
             .order_by(TournamentTeam.sort_order, TournamentTeam.id).all())
    if len(teams) != 2:
        raise ValueError("A custom series needs exactly 2 teams.")
    t1, t2 = teams[0].id, teams[1].id

    (session.query(TournamentMatch).filter_by(tournament_id=tid)
     .filter(TournamentMatch.status != "completed")
     .delete(synchronize_session=False))
    for i in range(n):
        a, b = (t1, t2) if i % 2 == 0 else (t2, t1)
        add_fixture(session, tid, a, b, round_no=i + 1)

    tour = session.get(Tournament, tid)
    if tour:
        tour.schedule_generated = True
    session.flush()
    assign_fixture_venues(session, tid)
    logger.info("Generated %s-match series for tournament %s", n, tid)
    return n


# ──────────────────────────────────────────────────────────────────────
# Fixture editing (DB)
# ──────────────────────────────────────────────────────────────────────

def _require_team(session, tid, team_id):
    """Return the team id if it belongs to this tournament, else raise."""
    from models import TournamentTeam
    if not team_id:
        return None
    tt = (session.query(TournamentTeam)
          .filter_by(id=int(team_id), tournament_id=tid).first())
    if not tt:
        raise ValueError("Team %s is not a participant in this tournament." % team_id)
    return tt.id


def add_fixture(session, tournament_id, team1_id, team2_id, group_id=None, round_no=0):
    """Add a single scheduled fixture. Caller commits.

    Validates that both teams (and the group, if given) belong to this tournament
    so a tampered request can't create cross-tournament links.
    """
    from sqlalchemy import func
    from models import Tournament, TournamentMatch, TournamentGroup
    tid = int(tournament_id)
    tour = session.get(Tournament, tid)
    t1 = _require_team(session, tid, team1_id)
    t2 = _require_team(session, tid, team2_id)
    if not t1 or not t2:
        raise ValueError("A fixture needs two teams.")
    if t1 == t2:
        raise ValueError("A fixture needs two different teams.")
    gid = int(group_id) if group_id else None
    if gid and not session.query(TournamentGroup).filter_by(
            id=gid, tournament_id=tid).first():
        raise ValueError("Group does not belong to this tournament.")
    stage = "group" if (tour and tour.league_format == "groups") else "league"
    max_no = (session.query(func.max(TournamentMatch.match_no))
              .filter_by(tournament_id=tid).scalar()) or 0
    tm = TournamentMatch(
        tournament_id=tid, team1_id=t1, team2_id=t2, home_team_id=t1,
        status="scheduled", stage=stage, group_id=gid,
        round_no=int(round_no or 0), match_no=max_no + 1)
    session.add(tm)
    session.flush()
    # Fill this one fixture's surface without disturbing the rest of the schedule.
    mode = (tour.pitch_mode or PITCH_MODE_HOST) if tour else PITCH_MODE_HOST
    if mode != PITCH_MODE_HOST:
        tm.pitch_type = pick_pitch(mode, t1, _home_pitch_map(session, tid))
        session.flush()
    return tm


def delete_fixture(session, fixture_id):
    """Delete a *scheduled* fixture only. Caller commits."""
    from models import TournamentMatch
    tm = session.get(TournamentMatch, int(fixture_id))
    if not tm:
        return None
    if tm.status != "scheduled":
        raise ValueError("Only scheduled fixtures can be deleted "
                         "(this one is %s)." % tm.status)
    tid = tm.tournament_id
    session.delete(tm)
    session.flush()
    return tid


def swap_fixture_team(session, fixture_id, slot, new_team_id):
    """Replace one side of a *scheduled* fixture. ``slot`` is 1 or 2. Caller commits."""
    from models import TournamentMatch
    tm = session.get(TournamentMatch, int(fixture_id))
    if not tm:
        return None
    if tm.status != "scheduled":
        raise ValueError("Only scheduled fixtures can be edited "
                         "(this one is %s)." % tm.status)
    new_id = _require_team(session, tm.tournament_id, new_team_id) if new_team_id else None
    old_id = tm.team1_id if int(slot) == 1 else tm.team2_id
    if int(slot) == 1:
        if new_id and tm.team2_id == new_id:
            raise ValueError("A fixture needs two different teams.")
        tm.team1_id = new_id
    else:
        if new_id and tm.team1_id == new_id:
            raise ValueError("A fixture needs two different teams.")
        tm.team2_id = new_id
    # The home side must stay one of the two teams in the fixture: follow the
    # swap when the replaced team was at home, rather than leaving a dangling id.
    if tm.home_team_id == old_id:
        tm.home_team_id = new_id or tm.team1_id or tm.team2_id
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


def _team_id_by_name(session, tid, name):
    """Return the TournamentTeam id with this name in the tournament, or None."""
    from models import TournamentTeam
    tt = (session.query(TournamentTeam).filter_by(tournament_id=tid)
          .filter(TournamentTeam.name == name).first())
    return tt.id if tt else None


def reserve_fixture(session, tournament_id, team1_id, team2_id):
    """Atomically claim the earliest *scheduled* fixture for a pair (scheduled->live).

    Returns the reserved ``TournamentMatch`` or None when there's no open fixture
    or another draft just claimed it. The claim is an ``UPDATE ... WHERE
    status='scheduled'`` so two concurrent drafts can't both reserve the same
    fixture. Caller commits.
    """
    from sqlalchemy import or_, and_
    from models import TournamentMatch
    tid = int(tournament_id)
    a, b = int(team1_id), int(team2_id)
    fx = (session.query(TournamentMatch)
          .filter_by(tournament_id=tid, status="scheduled")
          .filter(or_(
              and_(TournamentMatch.team1_id == a, TournamentMatch.team2_id == b),
              and_(TournamentMatch.team1_id == b, TournamentMatch.team2_id == a)))
          .order_by(TournamentMatch.round_no, TournamentMatch.match_no).first())
    if not fx:
        return None
    claimed = (session.query(TournamentMatch)
               .filter(TournamentMatch.id == fx.id,
                       TournamentMatch.status == "scheduled")
               .update({TournamentMatch.status: "live"}, synchronize_session=False))
    if not claimed:
        return None
    session.flush()
    session.refresh(fx)
    return fx


def reserve_fixture_by_names(session, tournament_id, name1, name2):
    """Name-based wrapper around ``reserve_fixture`` for the bot draft flow."""
    tid = int(tournament_id)
    a = _team_id_by_name(session, tid, name1)
    b = _team_id_by_name(session, tid, name2)
    if not a or not b:
        return None
    return reserve_fixture(session, tid, a, b)


def bind_fixture_match(session, fixture_id, match_id):
    """Record which live ``Match`` is playing a reserved fixture. Caller commits.

    ``reserve_fixture`` claims the row before the match exists, so the fixture
    sits 'live' with nothing on it pointing at the game being played. Writing the
    match id here closes that gap, and is what lets ``heal_live_fixtures`` tell a
    fixture whose match is still in progress from one whose match ended without
    ever recording a result.

    Only fills a fixture that is actually reserved and unbound, so it can never
    overwrite the match id of a completed result.
    """
    from models import TournamentMatch
    if not fixture_id or not match_id:
        return None
    fx = session.get(TournamentMatch, int(fixture_id))
    if fx is None or fx.status != "live" or fx.match_id:
        return None
    fx.match_id = int(match_id)
    session.flush()
    return fx


# ``Match`` statuses that mean the game is over. Healing keys off this list
# rather than off "not currently active" on purpose: an unrecognised status is
# left alone, so a flow that parks a match in some state this module has never
# heard of can never have its fixture pulled out from under it.
_MATCH_ENDED = ("completed", "abandoned", "expired", "cancelled", "forfeit")


def heal_live_fixtures(session, tournament_id=None, grace_minutes=180):
    """Revert fixtures stuck on 'live' to 'scheduled'. Returns the healed rows.

    A fixture goes 'live' when two teams launch their match and 'completed' when
    the result is recorded. Any path that ends a match without recording — a
    crash mid-innings, a bot restart, a recording that found no open fixture —
    leaves it on 'live', where every view of the tournament keeps reporting a
    finished match as still in progress.

    A fixture is only healed when the match behind it has demonstrably ended:

    * its bound ``Match`` row is gone, or has reached a terminal status; or
    * it has no bound match at all and has been 'live' longer than
      ``grace_minutes`` (old fixtures reserved before match binding existed, and
      matches that died between reserving and launching).

    A completed fixture is never touched, and neither is one whose match is still
    being played, however long it has been running. Caller commits.
    """
    from datetime import datetime, timedelta
    from models import Match, TournamentMatch

    q = (session.query(TournamentMatch)
         .filter(TournamentMatch.status == "live"))
    if tournament_id is not None:
        q = q.filter(TournamentMatch.tournament_id == int(tournament_id))
    live = q.all()
    if not live:
        return []

    cutoff = datetime.utcnow() - timedelta(minutes=max(0, int(grace_minutes)))
    match_ids = {fx.match_id for fx in live if fx.match_id}
    matches = {}
    if match_ids:
        matches = {m.id: m for m in
                   session.query(Match).filter(Match.id.in_(match_ids)).all()}

    healed = []
    for fx in live:
        if fx.match_id:
            m = matches.get(fx.match_id)
            if m is not None and (m.status or "") not in _MATCH_ENDED:
                continue  # still being played — leave it alone
            reason = "match missing" if m is None else f"match {m.status}"
        else:
            started = fx.created_at or datetime.utcnow()
            if started > cutoff:
                continue  # just reserved; give the match time to start
            reason = f"no match bound for over {grace_minutes} minutes"
        fx.status = "scheduled"
        fx.match_id = None
        fx._heal_reason = reason  # transient, for the caller's report
        healed.append(fx)
        logger.info("Healed stale live fixture %s in tournament %s (%s)",
                    fx.id, fx.tournament_id, reason)
    if healed:
        session.flush()
    return healed


def release_fixture(session, fixture_id):
    """Revert a reserved fixture (live -> scheduled) when a match is abandoned.

    Clears the bound match id along with the status: the fixture is open again,
    and the match that reserved it is not the one that will eventually fill it.
    No-ops if the fixture isn't currently ``live`` (e.g. already completed or
    released). Caller commits.
    """
    from models import TournamentMatch
    if not fixture_id:
        return
    (session.query(TournamentMatch)
     .filter(TournamentMatch.id == int(fixture_id),
             TournamentMatch.status == "live")
     .update({TournamentMatch.status: "scheduled", TournamentMatch.match_id: None},
             synchronize_session=False))
    session.flush()


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
