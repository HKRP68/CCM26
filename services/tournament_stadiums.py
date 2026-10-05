"""Tournament stadiums — the grounds a tournament is played at, and home grounds.

There is **one** stadium list in the bot: the Stadium Data on the admin site
(Conditions → Stadium), read through ``engine.sim.stadium``. It is the same
data the Conditions Engine plays — boundaries, outfield, dew, altitude,
climate and the ground's own modifiers — so a fixture played "at Wankhede"
really plays like Wankhede. A stadium added on that page can be picked for a
tournament at once; nothing here keeps a copy of the data, only names.

Names are stored canonically (an alias like "Mumbai" becomes "Wankhede
Stadium") and re-resolved at kickoff, so a stadium deleted from Stadium Data
later falls back to another of the tournament's grounds, or to a neutral one,
instead of breaking the match.
"""

import json
import logging
import random

logger = logging.getLogger(__name__)


# ── The Stadium Data list ─────────────────────────────────────────────

def all_rows():
    """Every stadium dict in Stadium Data (admin-saved list, else the file)."""
    from engine.sim import stadium
    try:
        return stadium.live_rows()
    except Exception:
        logger.exception("Could not read Stadium Data")
        return []


def resolve(name):
    """The canonical Stadium Data name for ``name`` (or an alias), or None."""
    if not name or not str(name).strip():
        return None
    from engine.sim import stadium
    try:
        # A fresh read, so a stadium added on the admin page a moment ago is
        # found even if this process's cache predates it.
        rows = stadium.load_db(use_store=True)
        hit = stadium.find(name, db=rows)
    except Exception:
        logger.exception("Stadium lookup failed for %r", name)
        return None
    return hit.name if hit is not None else None


def describe(name):
    """"Wankhede Stadium (Mumbai)" — the name with its city when known."""
    for row in all_rows():
        if row.get("name") == name:
            city = (row.get("city") or "").strip()
            return f"{name} ({city})" if city else name
    return name


# ── A tournament's list, a team's home ground ─────────────────────────

def tour_stadiums(tour):
    try:
        names = json.loads(getattr(tour, "stadiums_json", None) or "[]")
    except (TypeError, ValueError):
        return []
    return [n for n in names if isinstance(n, str) and n.strip()]


def set_tour_stadiums(tour, names):
    """Replace the list; every name must be in Stadium Data. Returns the list."""
    out = []
    for raw in names:
        canon = resolve(raw)
        if canon is None:
            raise ValueError(f"“{raw}” isn't in Stadium Data. Add it on the admin "
                             "site (Conditions → Stadium) first, or check the name "
                             "with /tstadiums all.")
        if canon not in out:
            out.append(canon)
    tour.stadiums_json = json.dumps(out) if out else None
    return out


def add_tour_stadium(tour, name):
    names = tour_stadiums(tour)
    canon = resolve(name)
    if canon is None:
        set_tour_stadiums(tour, [name])     # raises the helpful error
    if canon not in names:
        names.append(canon)
    tour.stadiums_json = json.dumps(names)
    return canon


def remove_tour_stadium(tour, name):
    names = tour_stadiums(tour)
    canon = resolve(name) or name
    if canon not in names:
        raise ValueError(f"“{name}” isn't on this tournament's stadium list.")
    names.remove(canon)
    tour.stadiums_json = json.dumps(names) if names else None
    return canon


def set_home_stadium(session, team, name):
    """Set (or with a falsy name, clear) a team's home ground; re-venue its
    unplayed home fixtures. Returns the canonical name or None."""
    canon = None
    if name and str(name).strip():
        canon = resolve(name)
        if canon is None:
            raise ValueError(f"“{name}” isn't in Stadium Data. Add it on the admin "
                             "site (Conditions → Stadium) first.")
    team.home_stadium = canon
    session.flush()
    assign_venues(session, team.tournament_id, team_id=team.id)
    return canon


# ── Fixture venues ────────────────────────────────────────────────────

def venue_for(fx, tour, homes, rng=None):
    """Where one fixture is played: the home team's ground for a league match,
    else a ground from the tournament's list (a neutral venue for knockouts),
    else None (the old random venue)."""
    rng = rng or random
    names = tour_stadiums(tour)
    league = (fx.stage or "league") in ("league", "group")
    if league and fx.home_team_id and homes.get(fx.home_team_id):
        return homes[fx.home_team_id]
    if names:
        return rng.choice(names)
    if not league and fx.home_team_id and homes.get(fx.home_team_id):
        return homes[fx.home_team_id]
    return None


def assign_venues(session, tournament_id, *, overwrite=False, team_id=None, rng=None):
    """Fill ``venue`` on unplayed fixtures. Caller commits. Returns the count.

    Played and live fixtures are never touched. ``team_id`` re-venues just the
    fixtures that team hosts (after its home ground changed); ``overwrite``
    re-rolls every unplayed fixture.
    """
    from models import Tournament, TournamentMatch, TournamentTeam
    tour = session.get(Tournament, int(tournament_id))
    if tour is None:
        return 0
    homes = {t.id: t.home_stadium for t in
             session.query(TournamentTeam).filter_by(tournament_id=tour.id).all()
             if (t.home_stadium or "").strip()}
    if not homes and not tour_stadiums(tour) and not overwrite:
        return 0
    changed = 0
    for fx in (session.query(TournamentMatch)
               .filter_by(tournament_id=tour.id, status="scheduled").all()):
        if team_id is not None and fx.home_team_id != int(team_id):
            continue
        if fx.venue and not overwrite and team_id is None:
            continue
        new = venue_for(fx, tour, homes, rng=rng)
        if new != fx.venue:
            fx.venue = new
            changed += 1
    if changed:
        session.flush()
    return changed


def kickoff_stadium(session, fx):
    """The stadium name a match on ``fx`` is played at, re-resolved now.

    None when the fixture has no venue (the match keeps its random ground).
    A venue that has since left Stadium Data falls back to another of the
    tournament's grounds, then to nothing.
    """
    if fx is None or not (fx.venue or "").strip():
        return None
    canon = resolve(fx.venue)
    if canon:
        return canon
    from models import Tournament
    tour = session.get(Tournament, fx.tournament_id)
    for name in tour_stadiums(tour) if tour else []:
        canon = resolve(name)
        if canon:
            return canon
    return None
