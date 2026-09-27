"""Past seasons: keep a tournament's player stats after it is deleted, and
choose which seasons "Total Season Stats" adds up.

Two things live here:

**Saving a season.** Deleting a tournament cascades away its fixtures, table
and ``TournamentPlayerStats``. ``delete_tournament(..., keep_stats=True)``
first copies every player row into a ``StatsSeason`` / ``StatsSeasonPlayer``,
so the season keeps counting in /statstour after the tournament is gone.

**Linking seasons.** ``Tournament.linked_seasons_json`` is a list of season
references — ``"t:12"`` a live tournament, ``"s:3"`` a saved season. When it
is empty the tournament uses the automatic default: every tournament and
saved season in the same league. Saving a tournament rewrites every link that
pointed at it (``t:12`` → ``s:<new id>``), so a link survives the delete.
"""

import json
import logging
from datetime import datetime

from sqlalchemy import func, or_

from models import (StatsSeason, StatsSeasonPlayer, Tournament,
                    TournamentPlayerStats)

logger = logging.getLogger(__name__)

STAT_FIELDS = ("matches", "bat_innings", "bat_runs", "bat_balls", "bat_fours",
               "bat_sixes", "bat_outs", "highest_score", "bowl_innings",
               "bowl_wickets", "bowl_runs", "bowl_balls", "best_bowl_wickets",
               "best_bowl_runs")


# ── References ───────────────────────────────────────────────────────

def tour_ref(tournament_id):
    return f"t:{int(tournament_id)}"


def saved_ref(season_id):
    return f"s:{int(season_id)}"


def _parse_ref(ref):
    """``("t", 12)`` / ``("s", 3)``, or None for anything malformed."""
    try:
        kind, _, number = str(ref).strip().partition(":")
        if kind in ("t", "s"):
            return kind, int(number)
    except (TypeError, ValueError):
        pass
    return None


def linked_refs(tour):
    """The tournament's explicit season links; ``[]`` means the default."""
    try:
        data = json.loads(getattr(tour, "linked_seasons_json", None) or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for ref in data if isinstance(data, list) else []:
        parsed = _parse_ref(ref)
        if parsed and f"{parsed[0]}:{parsed[1]}" not in out:
            out.append(f"{parsed[0]}:{parsed[1]}")
    return out


def set_linked_refs(session, tour, refs):
    """Save the links (never a link to the tournament itself). ``[]`` = auto."""
    clean = []
    for ref in refs or ():
        parsed = _parse_ref(ref)
        if not parsed:
            continue
        text = f"{parsed[0]}:{parsed[1]}"
        if text == tour_ref(tour.id) or text in clean:
            continue
        clean.append(text)
    tour.linked_seasons_json = json.dumps(clean) if clean else None
    session.flush()
    return clean


def _same_competition_tournaments(session, tour):
    from services.tournament_service import KIND_CHALLENGE, kind_filter
    q = session.query(Tournament).filter(Tournament.id != tour.id)
    if tour.league_id:
        q = q.filter(Tournament.league_id == tour.league_id)
    else:
        q = q.filter(kind_filter(tour.kind or KIND_CHALLENGE))
    return q.all()


def _same_competition_saved(session, tour):
    from services.tournament_service import KIND_CHALLENGE
    q = session.query(StatsSeason)
    if tour.league_id:
        q = q.filter(StatsSeason.league_id == tour.league_id)
    else:
        kind = tour.kind or KIND_CHALLENGE
        q = q.filter(StatsSeason.league_id.is_(None))
        if kind == KIND_CHALLENGE:
            q = q.filter(or_(StatsSeason.kind == kind, StatsSeason.kind.is_(None)))
        else:
            q = q.filter(StatsSeason.kind == kind)
    return q.all()


def default_refs(session, tour):
    """Every other tournament and saved season in the same league."""
    return ([tour_ref(t.id) for t in _same_competition_tournaments(session, tour)]
            + [saved_ref(s.id) for s in _same_competition_saved(session, tour)])


def effective_refs(session, tour):
    """The seasons "Total Season Stats" adds to this one."""
    return linked_refs(tour) or default_refs(session, tour)


# ── Labels and choices ───────────────────────────────────────────────

def _when(value):
    return value.strftime("%b %Y") if value else ""


def _tour_date(t):
    return t.completed_at or t.activated_at or t.created_at


def _saved_date(s):
    return s.completed_at or s.archived_at


def describe_ref(session, ref):
    """``(label, date, exists)`` for one reference."""
    parsed = _parse_ref(ref)
    if not parsed:
        return (str(ref), None, False)
    kind, number = parsed
    if kind == "t":
        t = session.get(Tournament, number)
        if t is None:
            return (f"Tournament #{number} (deleted)", None, False)
        return (t.name, _tour_date(t), True)
    s = session.get(StatsSeason, number)
    if s is None:
        return (f"Saved season #{number} (removed)", None, False)
    return (f"{s.name} 📦", _saved_date(s), True)


def season_choices(session, tour):
    """Every tournament and saved season this one could link to, oldest first.

    Returns ``[{"ref", "label", "league", "when", "saved", "linked"}]``.
    """
    linked = set(linked_refs(tour))
    choices = []
    for t in session.query(Tournament).filter(Tournament.id != tour.id).all():
        choices.append({"ref": tour_ref(t.id), "label": t.name,
                        "league": t.league_name or "", "saved": False,
                        "date": _tour_date(t), "when": _when(_tour_date(t))})
    for s in session.query(StatsSeason).all():
        choices.append({"ref": saved_ref(s.id), "label": s.name,
                        "league": s.league_name or "", "saved": True,
                        "date": _saved_date(s), "when": _when(_saved_date(s))})
    choices.sort(key=lambda c: (c["date"] or datetime.min, c["label"]))
    for c in choices:
        c["linked"] = c["ref"] in linked
    return choices


# ── The two lists linking works from ─────────────────────────────────
#
# /tseasons and the website's 🔗 Link seasons card both number the same two
# lists, so "/tseasons 2 | 3" means "running #2 adds completed #3". Both are
# computed the same way every time, so the numbers are stable between the
# listing and the command.

RUNNING_STATUSES = ("draft", "scheduled", "active", "paused")


def running_tournaments(session):
    """Tournaments still being played (or set up), oldest first."""
    return (session.query(Tournament)
            .filter(Tournament.status.in_(RUNNING_STATUSES))
            .order_by(Tournament.created_at.asc(), Tournament.id.asc()).all())


def completed_seasons(session):
    """Finished tournaments and 📦 saved seasons, newest first.

    ``[{"ref", "label", "league", "when", "saved"}]`` — the seasons a running
    tournament can add to its Total Season Stats.
    """
    out = []
    for t in (session.query(Tournament)
              .filter(Tournament.status.notin_(RUNNING_STATUSES)).all()):
        out.append({"ref": tour_ref(t.id), "label": t.name,
                    "league": t.league_name or "", "saved": False,
                    "date": _tour_date(t), "when": _when(_tour_date(t))})
    for s in session.query(StatsSeason).all():
        out.append({"ref": saved_ref(s.id), "label": s.name,
                    "league": s.league_name or "", "saved": True,
                    "date": _saved_date(s), "when": _when(_saved_date(s))})
    out.sort(key=lambda c: (c["date"] or datetime.min, c["ref"]), reverse=True)
    return out


def link_labels(session, tour):
    """Names of the seasons a tournament is linked to; ``[]`` when automatic."""
    return [describe_ref(session, ref)[0] for ref in linked_refs(tour)]


def parse_link_command(text):
    """Parse ``/tseasons`` arguments.

    ``""`` → ``("list", None, None)``; ``"2"`` → ``("show", 2, None)``;
    ``"2 | 3 5"`` → ``("link", 2, [3, 5])``; ``"2 | -3"`` → ``("unlink", 2,
    [3])``; ``"2 | auto"`` → ``("auto", 2, None)``. Raises ``ValueError``.
    """
    text = (text or "").strip()
    if not text:
        return "list", None, None
    left, bar, right = text.partition("|")
    try:
        running = int(left.strip().lstrip("#"))
    except ValueError:
        raise ValueError("Start with the running tournament's number, e.g. 2 | 3")
    if not bar:
        return "show", running, None
    right = right.strip().lower()
    if right in ("auto", "clear", "none", "reset", "0"):
        return "auto", running, None
    tokens = right.replace(",", " ").split()
    if not tokens:
        raise ValueError("Name the completed season after the bar, e.g. 2 | 3")
    unlink = all(tok.startswith("-") for tok in tokens)
    if not unlink and any(tok.startswith("-") for tok in tokens):
        raise ValueError("Link or unlink in one go, not both.")
    try:
        numbers = [int(tok.lstrip("-")) for tok in tokens]
    except ValueError:
        raise ValueError("Use numbers from the list, e.g. 2 | 3 5")
    return ("unlink" if unlink else "link"), running, numbers


# ── Saving and deleting ──────────────────────────────────────────────

def archive_tournament(session, tour):
    """Copy the tournament's player stats into a new saved season.

    Returns the ``StatsSeason`` (with no players when the tournament had no
    stats — it still stands for the season, so links to it keep meaning).
    Every link that pointed at this tournament is moved to the saved season.
    """
    season = StatsSeason(source_tournament_id=tour.id, name=tour.name,
                         league_id=tour.league_id, league_name=tour.league_name,
                         kind=tour.kind, completed_at=_tour_date(tour),
                         archived_at=datetime.utcnow())
    session.add(season)
    session.flush()
    rows = (session.query(TournamentPlayerStats)
            .filter(TournamentPlayerStats.tournament_id == tour.id).all())
    for row in rows:
        session.add(StatsSeasonPlayer(
            season_id=season.id, player_id=row.player_id, name=row.name,
            team_name=row.team_name,
            **{field: getattr(row, field) for field in STAT_FIELDS}))
    session.flush()

    old, new = tour_ref(tour.id), saved_ref(season.id)
    for other in (session.query(Tournament)
                  .filter(Tournament.linked_seasons_json.isnot(None),
                          Tournament.id != tour.id).all()):
        refs = linked_refs(other)
        if old in refs:
            set_linked_refs(session, other,
                            [new if r == old else r for r in refs])
    season.player_count = len(rows)
    return season


def delete_tournament(session, tour, *, keep_stats=True):
    """Delete a tournament; with ``keep_stats`` its player stats are saved first.

    Returns the ``StatsSeason`` made, or None when nothing was kept.
    """
    season = archive_tournament(session, tour) if keep_stats else None
    if not keep_stats:
        # Links to a tournament that is gone for good are dropped.
        old = tour_ref(tour.id)
        for other in (session.query(Tournament)
                      .filter(Tournament.linked_seasons_json.isnot(None),
                              Tournament.id != tour.id).all()):
            refs = linked_refs(other)
            if old in refs:
                set_linked_refs(session, other, [r for r in refs if r != old])
    session.delete(tour)
    session.flush()
    return season


def saved_seasons(session):
    """``[(StatsSeason, player_count)]``, newest first."""
    counts = dict(session.query(StatsSeasonPlayer.season_id,
                                func.count(StatsSeasonPlayer.id))
                  .group_by(StatsSeasonPlayer.season_id).all())
    seasons = (session.query(StatsSeason)
               .order_by(StatsSeason.archived_at.desc(), StatsSeason.id.desc())
               .all())
    return [(s, int(counts.get(s.id, 0))) for s in seasons]


def delete_saved_season(session, season_id):
    """Remove a saved season for good, and every link to it."""
    season = session.get(StatsSeason, int(season_id))
    if season is None:
        return None
    ref = saved_ref(season.id)
    for other in (session.query(Tournament)
                  .filter(Tournament.linked_seasons_json.isnot(None)).all()):
        refs = linked_refs(other)
        if ref in refs:
            set_linked_refs(session, other, [r for r in refs if r != ref])
    name = season.name
    session.delete(season)
    session.flush()
    return name


# ── One player across seasons ────────────────────────────────────────

def _player_filter(model, row):
    if getattr(row, "player_id", None) is not None:
        return model.player_id == row.player_id
    return func.lower(model.name) == (row.name or "").strip().lower()


def stat_rows_for(session, tour, row):
    """``[(label, CareerStats)]`` — this player season by season, oldest first.

    The current tournament is always included; the others are the linked
    seasons, or the league default. A season the player did not appear in is
    left out.
    """
    from services.tournament_service import CareerStats

    seasons = []   # (date, label, stats)

    def _collect(label, date, rows):
        rows = list(rows)
        if not rows:
            return
        stats = CareerStats(row.name)
        for r in rows:
            stats.add(r)
        stats.seasons = 1
        seasons.append((date, label, stats))

    _collect(tour.name, _tour_date(tour),
             session.query(TournamentPlayerStats)
             .filter(TournamentPlayerStats.tournament_id == tour.id,
                     _player_filter(TournamentPlayerStats, row)).all())
    for ref in effective_refs(session, tour):
        parsed = _parse_ref(ref)
        if not parsed:
            continue
        kind, number = parsed
        if kind == "t":
            t = session.get(Tournament, number)
            if t is None or t.id == tour.id:
                continue
            _collect(t.name, _tour_date(t),
                     session.query(TournamentPlayerStats)
                     .filter(TournamentPlayerStats.tournament_id == t.id,
                             _player_filter(TournamentPlayerStats, row)).all())
        else:
            s = session.get(StatsSeason, number)
            if s is None:
                continue
            _collect(s.name, _saved_date(s),
                     session.query(StatsSeasonPlayer)
                     .filter(StatsSeasonPlayer.season_id == s.id,
                             _player_filter(StatsSeasonPlayer, row)).all())
    seasons.sort(key=lambda item: (item[0] or datetime.min, item[1]))
    return [(label, stats) for _date, label, stats in seasons]
