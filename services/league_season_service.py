"""League Seasons — a competition ("IPL") and its Season 1, 2, 3…

Everything a season needs already exists somewhere else: the squads in a
``ChallengeLeague``, the matches in a ``Tournament``, the money in an
``AuctionSeason``, last season's stats in a ``StatsSeason``. This module owns
none of it. A ``LeagueSeries`` is a name, and each ``LeagueSeason`` is the
thread through one season's league, auction and tournament — plus the honours,
copied in, because a deleted tournament takes its final with it.

What it adds is the **turn of the season**, IPL-style. When a season is
completed the next one starts one of three ways:

* **🔨 Mega auction** — squads dissolve; every franchise may keep a few
  (retention + Right To Match, IPL 2025: six), purses reset to full, everyone
  else goes back into the pool. ``auction_service`` already does all of that;
  this sets the knobs.
* **🔁 Mini auction** — every franchise keeps its whole squad at what each
  player cost last season, releases a few, and only the released and the new
  are auctioned. Purse = the new cap − what the kept players cost
  (``auction_service.carry_previous_squads``).
* **✍️ Manual teams** — the squads are typed, Challenge Data style, starting
  from last season's (``previous_squads_text``).

Then the squads are published, the season's tournament is made from the last
one's rules, and the league's commands move to the new league so ``/cipl`` and
friends play the new season.

The stage of a season is never stored: ``season_stage`` reads it off the
auction and the tournament every time, so a season moved along from the bot or
another page is never out of date here.

Session-first, **commits nothing**, raises ``LeagueSeasonError`` in plain text
— the ``auction_service`` convention.
"""

import json
import logging
from datetime import datetime

from sqlalchemy import func

from models import (
    AuctionFranchise, AuctionSeason, ChallengeLeague, ChallengePlayer,
    ChallengeTeam, LeagueSeason, LeagueSeries, StatsSeason, StatsSeasonPlayer,
    Tournament, TournamentHonour, TournamentMatch, TournamentPlayerStats,
    TournamentTeam,
)

logger = logging.getLogger(__name__)


class LeagueSeasonError(Exception):
    """A refusal, worded for the admin who asked."""


MODE_MEGA = "mega"
MODE_MINI = "mini"
MODE_MANUAL = "manual"
MODE_EXISTING = "existing"
SQUAD_MODES = {
    MODE_MEGA: ("🔨", "Mega auction"),
    MODE_MINI: ("🔁", "Mini auction"),
    MODE_MANUAL: ("✍️", "Manual teams"),
    MODE_EXISTING: ("📥", "Added"),
}

AWARD_KEYS = ("champion", "runner_up", "orange_cap", "purple_cap", "most_sixes", "most_fours", "mvp", "player_of_final", "emerging_player", "highest_sr", "most_catches", "most_dots", "winning_captain")

# IPL 2025's retention slabs, in lakh: ₹18 / 14 / 11 / 18 / 14 Cr.
IPL_MEGA_LADDER = (1800, 1400, 1100, 1800, 1400)
DEFAULT_MEGA_PURSE_LAKH = 12000          # ₹120 Cr
DEFAULT_MEGA_KEEPS = 6
MINI_PURSE_TOP_UP_LAKH = 500             # the IPL's usual +₹5 Cr a year

RUNNING_TOUR_STATUSES = ("draft", "scheduled", "active", "paused")


def _loads(raw, fallback):
    try:
        value = json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback
    return value if value is not None else fallback


def _dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────
# Competitions and their seasons
# ──────────────────────────────────────────────────────────────────────

def series_list(session, *, active_only=False):
    q = session.query(LeagueSeries)
    if active_only:
        q = q.filter(LeagueSeries.is_active == True)  # noqa: E712
    return q.order_by(LeagueSeries.sort_order.asc(), LeagueSeries.name.asc()).all()


def get_series(session, series_id):
    try:
        return session.get(LeagueSeries, int(series_id))
    except (TypeError, ValueError):
        return None


def find_series(session, text):
    """A competition by name or short name, case-insensitive; None if unknown."""
    key = (text or "").strip().lower()
    if not key:
        return None
    for series in series_list(session):
        if key in ((series.name or "").lower(), (series.short_name or "").lower()):
            return series
    return None


def create_series(session, name, *, short_name=None, description=None,
                  logo_url=None):
    name = (name or "").strip()
    if not name:
        raise LeagueSeasonError("A competition needs a name.")
    clash = (session.query(LeagueSeries)
             .filter(func.lower(LeagueSeries.name) == name.lower()).first())
    if clash is not None:
        raise LeagueSeasonError(f"There is already a competition called "
                                f"“{clash.name}”.")
    top = session.query(func.coalesce(func.max(LeagueSeries.sort_order), 0)).scalar()
    series = LeagueSeries(name=name[:120],
                          short_name=(short_name or "").strip()[:30] or None,
                          description=(description or "").strip() or None,
                          logo_url=(logo_url or "").strip()[:500] or None,
                          sort_order=int(top or 0) + 1)
    session.add(series)
    session.flush()
    return series


def update_series(session, series, *, name=None, short_name=None,
                  description=None, logo_url=None, is_active=None):
    if name is not None:
        name = name.strip()
        if not name:
            raise LeagueSeasonError("A competition needs a name.")
        clash = (session.query(LeagueSeries)
                 .filter(func.lower(LeagueSeries.name) == name.lower(),
                         LeagueSeries.id != series.id).first())
        if clash is not None:
            raise LeagueSeasonError(f"There is already a competition called "
                                    f"“{clash.name}”.")
        series.name = name[:120]
    if short_name is not None:
        series.short_name = short_name.strip()[:30] or None
    if description is not None:
        series.description = description.strip() or None
    if logo_url is not None:
        series.logo_url = logo_url.strip()[:500] or None
    if is_active is not None:
        series.is_active = bool(is_active)
    session.flush()
    return series


def seasons(session, series):
    """Every season of a competition, Season 1 first."""
    return (session.query(LeagueSeason)
            .filter(LeagueSeason.series_id == series.id)
            .order_by(LeagueSeason.season_no.asc()).all())


def get_season(session, series, season_id):
    try:
        season = session.get(LeagueSeason, int(season_id))
    except (TypeError, ValueError):
        return None
    if season is None or season.series_id != series.id:
        return None
    return season


def latest_season(session, series):
    return (session.query(LeagueSeason)
            .filter(LeagueSeason.series_id == series.id)
            .order_by(LeagueSeason.season_no.desc()).first())


def previous_season(session, season):
    """The season before this one in its competition — by number, no link kept."""
    return (session.query(LeagueSeason)
            .filter(LeagueSeason.series_id == season.series_id,
                    LeagueSeason.season_no < season.season_no)
            .order_by(LeagueSeason.season_no.desc()).first())


def season_title(season):
    return (season.name or "").strip() or f"Season {season.season_no}"


def mode_label(season):
    emoji, label = SQUAD_MODES.get(season.squad_mode or MODE_EXISTING,
                                   SQUAD_MODES[MODE_EXISTING])
    return f"{emoji} {label}"


# ── The linked rows, read tolerantly ─────────────────────────────────
#
# Plain ids, not foreign keys (see ``models.LeagueSeason``): a missing row is
# an unlinked one, and a Lets Play tournament never counts.

def season_tournament(session, season):
    if season is None or not season.tournament_id:
        return None
    tour = session.get(Tournament, int(season.tournament_id))
    if tour is None or (tour.kind or "challenge") != "challenge":
        return None
    return tour


def season_league(session, season):
    if season is None or not season.league_id:
        return None
    return session.get(ChallengeLeague, int(season.league_id))


def season_auction(session, season):
    if season is None or not season.auction_season_id:
        return None
    return session.get(AuctionSeason, int(season.auction_season_id))


def season_saved(session, season):
    if season is None or not season.stats_season_id:
        return None
    return session.get(StatsSeason, int(season.stats_season_id))


def series_of(session, season):
    return session.get(LeagueSeries, int(season.series_id))


def season_for_tournament(session, tournament_id):
    if not tournament_id:
        return None
    return (session.query(LeagueSeason)
            .filter(LeagueSeason.tournament_id == int(tournament_id)).first())


def season_for_auction(session, auction_id):
    if not auction_id:
        return None
    return (session.query(LeagueSeason)
            .filter(LeagueSeason.auction_season_id == int(auction_id)).first())


# ──────────────────────────────────────────────────────────────────────
# Where a season stands — derived, never stored
# ──────────────────────────────────────────────────────────────────────

def _stage(key, emoji, label, step, *, detail="", done=False):
    return {"key": key, "emoji": emoji, "label": label, "step": step,
            "detail": detail, "done": done}


def _left_text(seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return "closed"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"closes in {days}d {hours}h"
    if hours:
        return f"closes in {hours}h {minutes}m"
    return f"closes in {max(1, minutes)}m"


def season_stage(session, season, now=None):
    """``{"key", "emoji", "label", "step", "detail", "done"}``.

    ``step`` is the stepper's position: 1 squads, 2 tournament, 3 champion.
    """
    from services import auction_service as A

    tour = season_tournament(session, season)
    if tour is not None:
        status = tour.status or "draft"
        if status == "completed":
            return _stage("completed", "🏆", "Completed", 3, done=True)
        if status == "cancelled":
            return _stage("cancelled", "🚫", "Tournament cancelled", 3, done=True)
        if status in ("draft", "scheduled"):
            return _stage("scheduled", "🗓", "Tournament scheduled", 2)
        return _stage("playing", "🏏", "In play" if status == "active"
                      else "Tournament paused", 2)
    if season_saved(session, season) is not None:
        return _stage("completed", "📦", "Completed (saved stats)", 3, done=True)
    if season_league(session, season) is not None:
        return _stage("ready", "🏟", "Squads ready — create the tournament", 2)
    auction = season_auction(session, season)
    if auction is not None:
        status = auction.status
        mini = season.squad_mode == MODE_MINI
        if status == A.STATUS_SETUP:
            if A.retention_open(auction, now):
                left = A.retention_seconds_left(auction, now)
                word = "Release window" if mini else "Retention"
                return _stage("window", "🔁" if mini else "🔒", f"{word} open", 1,
                              detail=_left_text(left) if left is not None else "")
            return _stage("pool", "📋", "Build the pool and start the auction", 1)
        if status == A.STATUS_LIVE:
            return _stage("auction", "🔴", "Auction live", 1)
        if status == A.STATUS_PAUSED:
            return _stage("auction", "⏸", "Auction paused", 1)
        if status == A.STATUS_COMPLETED:
            return _stage("publish", "📤", "Auction done — publish the squads", 1)
        return _stage("cancelled", "🚫", "Auction cancelled", 1)
    if honours(season).get("champion"):
        return _stage("completed", "📜", "Completed (recorded by hand)", 3, done=True)
    return _stage("empty", "➖", "Nothing linked yet", 1)


def is_completed(session, season):
    return season_stage(session, season)["key"] == "completed"


# ──────────────────────────────────────────────────────────────────────
# Honours — copied in, so history survives a deleted tournament
# ──────────────────────────────────────────────────────────────────────

def honours(season):
    data = _loads(getattr(season, "honours_json", None), {})
    return data if isinstance(data, dict) else {}


def _computed_honours(session, tour):
    """What the tournament itself says: its honours, else the final, else the
    table (a league-only tournament has no final) and the stat leaders."""
    from services import tournament_awards, tournament_service

    out = {}
    for row in (session.query(TournamentHonour)
                .filter(TournamentHonour.tournament_id == tour.id).all()):
        if row.award in AWARD_KEYS:
            out[row.award] = {"team": row.team_name, "player": row.player_name,
                              "value": row.value}
    if "champion" not in out:
        try:
            for award, team, player, _uid, value in tournament_awards.winners(session, tour):
                out.setdefault(award, {"team": team, "player": player,
                                       "value": value})
        except Exception:
            logger.exception("league seasons: reading the final failed")
    if "champion" not in out and tour.status == "completed":
        try:
            table = tournament_service.points_table(session, tour.id)
        except Exception:
            logger.exception("league seasons: reading the table failed")
            table = []
        if table:
            out["champion"] = {"team": table[0].name, "player": None,
                               "value": "top of the table"}
        if len(table) > 1:
            out.setdefault("runner_up", {"team": table[1].name, "player": None,
                                         "value": "second in the table"})
    if "orange_cap" not in out or "purple_cap" not in out:
        try:
            lead = tournament_service.stat_leaders(session, tour.id, limit=1)
        except Exception:
            logger.exception("league seasons: reading the leaders failed")
            lead = {}
        runs = lead.get("most_runs") or []
        if runs and (runs[0].bat_runs or 0) > 0:
            out.setdefault("orange_cap", {"team": runs[0].team_name,
                                          "player": runs[0].name,
                                          "value": f"{runs[0].bat_runs} runs"})
        wkts = lead.get("most_wickets") or []
        if wkts and (wkts[0].bowl_wickets or 0) > 0:
            out.setdefault("purple_cap", {"team": wkts[0].team_name,
                                          "player": wkts[0].name,
                                          "value": f"{wkts[0].bowl_wickets} wkts"})
    return out


def refresh_honours(session, season):
    """Copy the tournament's honours onto the season. True when it changed.

    Only while the tournament exists; a hand-recorded season keeps what was
    typed, and a key the tournament cannot answer keeps its old value.
    """
    tour = season_tournament(session, season)
    if tour is None:
        return False
    current = honours(season)
    merged = dict(current)
    merged.update(_computed_honours(session, tour))
    changed = False
    if merged != current:
        season.honours_json = _dumps(merged) if merged else None
        changed = True
    done_at = tour.completed_at if tour.status == "completed" else None
    if done_at and season.completed_at != done_at:
        season.completed_at = done_at
        changed = True
    if changed:
        session.flush()
    return changed


def set_manual_honours(season, *, champion=None, runner_up=None):
    data = honours(season)
    for key, team in (("champion", champion), ("runner_up", runner_up)):
        team = (team or "").strip()
        if team:
            data[key] = {"team": team[:120], "player": None, "value": None}
        elif key in data and not data[key].get("player"):
            data.pop(key)
    season.honours_json = _dumps(data) if data else None


# ──────────────────────────────────────────────────────────────────────
# Keeping up with work done elsewhere
# ──────────────────────────────────────────────────────────────────────

def attached_tournament_ids(session, *, except_season=None):
    q = session.query(LeagueSeason.tournament_id).filter(
        LeagueSeason.tournament_id.isnot(None))
    if except_season is not None:
        q = q.filter(LeagueSeason.id != except_season.id)
    return {int(row[0]) for row in q.all()}


def sync_season(session, season):
    """Pick up what happened on other pages and in the bot. True if it wrote.

    * squads published with ``/apublish`` or on the auction page → the league;
    * a tournament made on the Tournaments page → linked, when exactly one
      unclaimed Challenge tournament sits on the season's league;
    * the honours, from the tournament.

    Writes only when something actually changed, so a page view of a quiet
    season costs reads and nothing else.
    """
    changed = False
    auction = season_auction(session, season)
    if season.auction_season_id and auction is None:
        season.auction_season_id = None
        changed = True
    if auction is not None and auction.league_id and season_league(session, season) is None:
        season.league_id = auction.league_id
        changed = True
    if season.league_id and season_league(session, season) is None:
        season.league_id = None
        changed = True
    if season.tournament_id and season_tournament(session, season) is None:
        season.tournament_id = None
        changed = True
    if not season.tournament_id and not season.stats_season_id and season.league_id:
        taken = attached_tournament_ids(session)
        candidates = [t for t in tournaments_on_league(session, season.league_id)
                      if t.id not in taken]
        if len(candidates) == 1:
            season.tournament_id = candidates[0].id
            session.flush()
            link_season_stats(session, season)
            changed = True
    if refresh_honours(session, season):
        changed = True
    if changed:
        session.flush()
    return changed


def sync_series(session, series):
    changed = False
    for season in seasons(session, series):
        changed = sync_season(session, season) or changed
    return changed


def tournaments_on_league(session, league_id):
    from services.tournament_service import KIND_CHALLENGE, kind_filter
    if not league_id:
        return []
    return (session.query(Tournament)
            .filter(Tournament.league_id == int(league_id),
                    kind_filter(KIND_CHALLENGE))
            .order_by(Tournament.created_at.asc(), Tournament.id.asc()).all())


# ── Cleanup when a linked row goes away ──────────────────────────────
#
# Called by every delete path, because SQLite neither enforces SET NULL nor
# promises not to hand a deleted row's id to the next row made.

def forget_tournament(session, tour, saved=None):
    """``tour`` is about to be deleted; ``saved`` is the StatsSeason it became."""
    for season in (session.query(LeagueSeason)
                   .filter(LeagueSeason.tournament_id == tour.id).all()):
        try:
            refresh_honours(session, season)
        except Exception:
            logger.exception("league seasons: honours snapshot before delete failed")
        if saved is not None:
            from services import league_records
            league_records.preserve(session, season)
        season.tournament_id = None
        if saved is not None:
            season.stats_season_id = saved.id
    session.flush()


def forget_league(session, league_id):
    (session.query(LeagueSeason)
     .filter(LeagueSeason.league_id == int(league_id))
     .update({"league_id": None}, synchronize_session=False))


def forget_auction(session, auction_id):
    (session.query(LeagueSeason)
     .filter(LeagueSeason.auction_season_id == int(auction_id))
     .update({"auction_season_id": None}, synchronize_session=False))


def forget_saved_season(session, saved_id):
    from models import LeagueRecordConfig
    for season in session.query(LeagueSeason).filter_by(stats_season_id=int(saved_id)).all():
        cfg = session.get(LeagueRecordConfig, season.id)
        if cfg is not None:
            cfg.archive_json = None
            cfg.standings_json = None
    session.flush()
    (session.query(LeagueSeason)
     .filter(LeagueSeason.stats_season_id == int(saved_id))
     .update({"stats_season_id": None}, synchronize_session=False))


# ──────────────────────────────────────────────────────────────────────
# Adding a season that already happened
# ──────────────────────────────────────────────────────────────────────

def next_number(session, series):
    latest = latest_season(session, series)
    return (latest.season_no + 1) if latest else 1


def attach_season(session, series, *, season_no=None, tournament=None,
                  saved=None, name=None, champion=None, runner_up=None):
    """Add Season N from an existing tournament, a 📦 saved season, or by hand.

    "Add a tournament and its previous season": any Challenge League
    tournament can become a season, and so can a deleted one's saved stats —
    or, for a season that predates the bot, just a champion's name.
    """
    from services.tournament_service import tournament_kind

    number = int(season_no) if season_no else next_number(session, series)
    if number < 1:
        raise LeagueSeasonError("Season numbers start at 1.")
    clash = (session.query(LeagueSeason)
             .filter(LeagueSeason.series_id == series.id,
                     LeagueSeason.season_no == number).first())
    if clash is not None:
        raise LeagueSeasonError(f"{series.name} already has a Season {number}.")
    season = LeagueSeason(series_id=series.id, season_no=number,
                          name=(name or "").strip()[:120] or None,
                          squad_mode=MODE_EXISTING)
    if tournament is not None:
        if tournament_kind(tournament) != "challenge":
            raise LeagueSeasonError("Only a Challenge League tournament can be a "
                                    "season — a Lets Play one has no squads.")
        owner = season_for_tournament(session, tournament.id)
        if owner is not None:
            raise LeagueSeasonError(f"{tournament.name} is already "
                                    f"{season_title(owner)} of "
                                    f"{series_of(session, owner).name}.")
        season.tournament_id = tournament.id
        season.league_id = tournament.league_id
        if tournament.league_id:
            auction = (session.query(AuctionSeason)
                       .filter(AuctionSeason.league_id == tournament.league_id)
                       .order_by(AuctionSeason.id.desc()).first())
            if auction is not None and season_for_auction(session, auction.id) is None:
                season.auction_season_id = auction.id
        season.name = season.name or tournament.name[:120]
    elif saved is not None:
        if (session.query(LeagueSeason)
                .filter(LeagueSeason.stats_season_id == saved.id).first()):
            raise LeagueSeasonError(f"{saved.name} is already a season.")
        season.stats_season_id = saved.id
        season.league_id = saved.league_id
        season.completed_at = saved.completed_at
        season.name = season.name or saved.name[:120]
    else:
        if not (champion or "").strip():
            raise LeagueSeasonError("Pick a tournament or a saved season — or, "
                                    "for a season before the bot, type its "
                                    "champion.")
    set_manual_honours(season, champion=champion, runner_up=runner_up)
    session.add(season)
    session.flush()
    if saved is not None:
        _saved_honours(session, season, saved)
    relink_series_stats(session, series)
    refresh_honours(session, season)
    return season


def _saved_honours(session, season, saved):
    """A saved season's honours: the Hall of Fame rows its tournament wrote,
    matched by the name copied onto them (the id went NULL with the delete)."""
    data = honours(season)
    for row in (session.query(TournamentHonour)
                .filter(TournamentHonour.tournament_name == saved.name).all()):
        if row.award in AWARD_KEYS:
            data.setdefault(row.award, {"team": row.team_name,
                                        "player": row.player_name,
                                        "value": row.value})
    rows = (session.query(StatsSeasonPlayer)
            .filter(StatsSeasonPlayer.season_id == saved.id).all())
    if rows and "orange_cap" not in data:
        top = max(rows, key=lambda r: (r.bat_runs or 0))
        if (top.bat_runs or 0) > 0:
            data["orange_cap"] = {"team": top.team_name, "player": top.name,
                                  "value": f"{top.bat_runs} runs"}
    if rows and "purple_cap" not in data:
        top = max(rows, key=lambda r: (r.bowl_wickets or 0, -(r.bowl_runs or 0)))
        if (top.bowl_wickets or 0) > 0:
            data["purple_cap"] = {"team": top.team_name, "player": top.name,
                                  "value": f"{top.bowl_wickets} wkts"}
    season.honours_json = _dumps(data) if data else None
    session.flush()


def update_season(session, season, *, name=None, champion=None, runner_up=None,
                  notes=None):
    if name is not None:
        season.name = name.strip()[:120] or None
    if notes is not None:
        season.notes = notes.strip() or None
    if season_tournament(session, season) is None and (champion is not None
                                                      or runner_up is not None):
        set_manual_honours(season, champion=champion, runner_up=runner_up)
    session.flush()
    return season


def detach_season(session, season):
    """Take a season off the competition. Unlinks only — the tournament, the
    league and the auction are left exactly as they are."""
    series = series_of(session, season)
    gone = _season_ref(session, season)
    session.delete(season)
    session.flush()
    if gone and series is not None:
        from services import season_archive
        for other in seasons(session, series):
            tour = season_tournament(session, other)
            if tour is None:
                continue
            refs = season_archive.linked_refs(tour)
            if gone in refs:
                season_archive.set_linked_refs(session, tour,
                                               [r for r in refs if r != gone])


# ──────────────────────────────────────────────────────────────────────
# Total Season Stats across the competition
# ──────────────────────────────────────────────────────────────────────

def _season_ref(session, season):
    from services import season_archive
    if season_tournament(session, season) is not None:
        return season_archive.tour_ref(season.tournament_id)
    if season_saved(session, season) is not None:
        return season_archive.saved_ref(season.stats_season_id)
    return None


def link_season_stats(session, season):
    """Make ``/statstour``'s Total Season Stats count every earlier season.

    Each season plays on its own league, and the default grouping is "every
    season in the same league" — which would drop the earlier ones. The
    competition's earlier seasons are MERGED into whatever the tournament
    already counts (its own links, or the league default written out), so a
    link an admin set by hand survives.
    """
    from services import season_archive
    tour = season_tournament(session, season)
    if tour is None:
        return []
    earlier = [ref for ref in (
        _season_ref(session, s) for s in
        (session.query(LeagueSeason)
         .filter(LeagueSeason.series_id == season.series_id,
                 LeagueSeason.season_no < season.season_no)
         .order_by(LeagueSeason.season_no.asc()).all())) if ref]
    if not earlier:
        return season_archive.linked_refs(tour)
    base = season_archive.linked_refs(tour) or season_archive.default_refs(session, tour)
    merged = list(base) + [ref for ref in earlier if ref not in base]
    if merged != season_archive.linked_refs(tour):
        season_archive.set_linked_refs(session, tour, merged)
    return merged


def relink_series_stats(session, series):
    for season in seasons(session, series):
        link_season_stats(session, season)


# ──────────────────────────────────────────────────────────────────────
# The next season
# ──────────────────────────────────────────────────────────────────────

def next_season_blocker(session, series):
    """Why a next season cannot start yet, or None. Also returns the season
    it would follow: ``(reason, previous)``."""
    latest = latest_season(session, series)
    if latest is None:
        return ("Add the first season (an existing tournament) before starting "
                "the next one.", None)
    stage = season_stage(session, latest)
    if stage["key"] != "completed":
        return (f"{season_title(latest)} is not completed yet ({stage['label']}).",
                latest)
    return None, latest


def _require_next(session, series, *, needs_league=True):
    reason, prev = next_season_blocker(session, series)
    if reason:
        raise LeagueSeasonError(reason)
    league = season_league(session, prev)
    if needs_league and league is None:
        raise LeagueSeasonError(f"{season_title(prev)} has no squads to carry on "
                                f"from (no Challenge League is linked).")
    return prev, league


def _new_season_row(session, series, prev, mode, **links):
    number = prev.season_no + 1
    season = LeagueSeason(series_id=series.id, season_no=number,
                          name=f"Season {number}", squad_mode=mode, **links)
    session.add(season)
    session.flush()
    return season


def _latest_auction(session, series):
    for season in reversed(seasons(session, series)):
        auction = season_auction(session, season)
        if auction is not None:
            return auction
    return None


def _check_deadline(deadline, now=None):
    if deadline is not None and deadline <= (now or datetime.utcnow()):
        raise LeagueSeasonError("The deadline has to be in the future.")


def _next_auction(session, series, prev, league, name, *, by_tg_id=None):
    """The auction for the season after ``prev`` — made, or adopted.

    An auction may already follow last season's league (the tournament
    page's "🏆 Next season" or ``/aclone`` made one): it is adopted rather
    than a second field of franchises built beside it, as long as it is still
    in setup with nobody signed.
    """
    from services import auction_service as A

    existing = A.season_following_league(session, league.id)
    if existing is not None and season_for_auction(session, existing.id) is None:
        counts = A.pool_counts(session, existing.id)
        signed = counts.get("retained", 0) + counts.get("drafted", 0)
        if existing.status != A.STATUS_SETUP or signed:
            raise LeagueSeasonError(
                f"“{existing.name}” already follows {season_title(prev)}'s "
                f"league and is under way — add it as the next season instead.")
        return existing, True

    prev_auction = season_auction(session, prev)
    try:
        if prev_auction is not None:
            chat = prev_auction.chat_id
            if chat is not None:
                holder = A.season_for_chat(session, chat)
                if holder is not None and holder.id != prev_auction.id:
                    chat = None
                elif prev_auction.status not in (A.STATUS_COMPLETED,
                                                 A.STATUS_CANCELLED):
                    chat = None
            fresh = A.clone_season(session, prev_auction, name, chat_id=chat,
                                   by_tg_id=by_tg_id)
            if fresh.previous_league_id != league.id:
                A.link_previous_season(session, fresh, league.id)
        else:
            fresh = A.season_from_league(session, league, name,
                                         template=_latest_auction(session, series),
                                         by_tg_id=by_tg_id)
    except A.AuctionError as exc:
        raise LeagueSeasonError(str(exc))
    fill_owners_from_tournament(session, fresh, prev)
    return fresh, False


def fill_owners_from_tournament(session, auction, prev_season):
    """Owners for franchises that have none, from last season's tournament.

    A ``ChallengeTeam`` has no owner, so an auction started from a league
    comes out ownerless — but the tournament that league played knows exactly
    who ran each team. Matched by the team the franchise WAS
    (``previous_team_links``), never by name. Never overwrites an owner.
    """
    from services import auction_service as A

    tour = season_tournament(session, prev_season)
    if tour is None:
        return 0
    by_team = {tt.challenge_team_id: tt for tt in
               session.query(TournamentTeam)
               .filter(TournamentTeam.tournament_id == tour.id).all()
               if tt.challenge_team_id}
    filled = 0
    for franchise, team in A.previous_team_links(session, auction).values():
        if franchise.owner_tg_id:
            continue
        tt = by_team.get(team.id)
        if tt is None or not tt.owner_tg_id:
            continue
        franchise.owner_tg_id = tt.owner_tg_id
        franchise.owner_name = tt.owner_name
        if tt.co_owner_ids_json and not franchise.co_owner_ids_json:
            franchise.co_owner_ids_json = tt.co_owner_ids_json
        filled += 1
    session.flush()
    return filled


def _zero_rtm_cards(session, auction):
    from services import auction_service as A
    for franchise in A.franchises(session, auction.id):
        franchise.rtm_cards_total = int(franchise.rtm_cards_used or 0)


def start_mega_auction(session, series, *, purse_lakh, keep_max=DEFAULT_MEGA_KEEPS,
                       rtm=True, deadline=None, name=None, by_tg_id=None,
                       now=None):
    """🔨 Season N+1 by mega auction. Returns ``(season, auction)``.

    Squads dissolve: each franchise may keep up to ``keep_max`` (IPL 2025: six,
    through retention and Right To Match together — the cards are the spots a
    franchise did not use, ``auction_service.rtm_auto_cards``), every purse
    resets to ``purse_lakh``, and the rest of last season goes back into the
    pool when the pool is built. Retention runs as it always has: offers the
    owners accept, then the window closes.
    """
    from services import auction_service as A

    prev, league = _require_next(session, series)
    purse = int(purse_lakh or 0)
    keep_max = int(keep_max or 0)
    if purse <= 0:
        raise LeagueSeasonError("The purse has to be more than zero.")
    if keep_max < 0 or keep_max > 25:
        raise LeagueSeasonError("Players kept must be between 0 and 25.")
    _check_deadline(deadline, now)
    title = (name or "").strip() or f"{series.name} · Season {prev.season_no + 1} Mega Auction"
    auction, adopted = _next_auction(session, series, prev, league, title,
                                     by_tg_id=by_tg_id)

    # Classic retention: the slots of dynamic retention ARE the keep count
    # there, so the two cannot both be in charge of it.
    auction.retention_mode = None
    auction.max_retentions = keep_max
    auction.min_retentions = 0
    auction.retention_max_spend_lakh = None
    if not A._loads(getattr(auction, "retention_price_rules_json", None), []):
        auction.retention_price_rules_json = A._dumps(
            [{"slab": i + 1, "price_lakh": p} for i, p in enumerate(IPL_MEGA_LADDER)])
    auction.rtm_enabled = bool(rtm) and keep_max > 0
    auction.retention_deadline_at = deadline
    auction.retention_locked_at = None
    try:
        A.set_opening_purse(session, auction, purse, by_tg_id=by_tg_id)
    except A.AuctionError as exc:
        raise LeagueSeasonError(str(exc))
    if auction.rtm_enabled:
        A.sync_rtm_cards(session, auction)
    else:
        _zero_rtm_cards(session, auction)
    session.flush()
    season = _new_season_row(session, series, prev, MODE_MEGA,
                             auction_season_id=auction.id)
    return season, auction


def start_mini_auction(session, series, *, purse_lakh, deadline=None, name=None,
                       by_tg_id=None, now=None):
    """🔁 Season N+1 by mini auction. Returns ``(season, auction, report)``.

    Every franchise keeps its whole squad at what each player cost last
    season; the purse is ``purse_lakh`` minus that. Owners (``/arelease``)
    and the admin (the release board) let players go until the window closes
    — each one goes back into the pool with his money back in the purse —
    and only the released and the new are auctioned. No Right To Match.

    Every retention knob is set here, not inherited: a season cloned from a
    dynamic-retention year would otherwise price every player by slot.
    """
    from services import auction_service as A

    prev, league = _require_next(session, series)
    purse = int(purse_lakh or 0)
    if purse <= 0:
        raise LeagueSeasonError("The purse has to be more than zero.")
    _check_deadline(deadline, now)
    title = (name or "").strip() or f"{series.name} · Season {prev.season_no + 1} Mini Auction"
    auction, adopted = _next_auction(session, series, prev, league, title,
                                     by_tg_id=by_tg_id)

    biggest = max([len(team_players) for team_players in
                   _league_squads(session, league.id).values()] or [0])
    auction.retention_mode = None
    auction.max_retentions = max(int(auction.max_squad_size or 0), biggest, 1)
    auction.min_retentions = 0
    auction.retention_max_spend_lakh = None
    auction.retention_min_rating = None
    auction.retention_max_rating = None
    auction.retention_categories_json = None
    auction.rtm_enabled = False
    auction.rtm_per_team = 0
    auction.retention_deadline_at = deadline
    auction.retention_locked_at = None
    try:
        A.set_opening_purse(session, auction, purse, by_tg_id=by_tg_id)
        _zero_rtm_cards(session, auction)
        session.flush()
        report = A.carry_previous_squads(session, auction, now=now,
                                         by_tg_id=by_tg_id)
    except A.AuctionError as exc:
        raise LeagueSeasonError(str(exc))
    season = _new_season_row(session, series, prev, MODE_MINI,
                             auction_season_id=auction.id)
    return season, auction, report


def _league_squads(session, league_id):
    out = {}
    for team in (session.query(ChallengeTeam)
                 .filter(ChallengeTeam.league_id == int(league_id)).all()):
        out[team.id] = (session.query(ChallengePlayer)
                        .filter(ChallengePlayer.team_id == team.id).all())
    return out


def default_mega_purse(session, series):
    auction = _latest_auction(session, series)
    return int(auction.opening_purse_lakh) if auction else DEFAULT_MEGA_PURSE_LAKH


def default_mini_purse(session, series):
    auction = _latest_auction(session, series)
    base = int(auction.opening_purse_lakh) if auction else DEFAULT_MEGA_PURSE_LAKH
    return base + MINI_PURSE_TOP_UP_LAKH


# ── ✍️ Manual teams ───────────────────────────────────────────────────

def previous_squads_text(session, league):
    """Last season's squads as Challenge Data import text.

    ``Team | Player | Version`` per line — exactly what the import reads back,
    with the card's version so the same edition comes back.
    """
    if league is None:
        return ""
    lines = ["Team Name | Player Name | Version"]
    teams = (session.query(ChallengeTeam)
             .filter(ChallengeTeam.league_id == league.id)
             .order_by(ChallengeTeam.sort_order.asc(), ChallengeTeam.name.asc()).all())
    for team in teams:
        players = (session.query(ChallengePlayer)
                   .filter(ChallengePlayer.team_id == team.id)
                   .order_by(ChallengePlayer.sort_order.asc(),
                             ChallengePlayer.name.asc()).all())
        for cp in players:
            details = _loads(cp.details_json, {})
            version = (details.get("version") if isinstance(details, dict) else None)
            if not version and cp.source_player is not None:
                version = cp.source_player.version
            line = f"{team.name} | {cp.name}"
            if version:
                line += f" | {version}"
            lines.append(line)
    return "\n".join(lines)


LEAGUE_COPY_FIELDS = ("home_country", "min_overseas", "max_overseas",
                      "match_format", "same_team_allowed", "multi_enabled",
                      "auction_enabled", "image_url")


def new_season_league(session, series, prev_league, name):
    """An empty Challenge League for the new season, set up like the last one.

    The commands are NOT copied here — they move when the season's tournament
    is created (``hand_over_league``), so the old season keeps answering them
    until the new one is actually ready to play.
    """
    from services import auction_service as A

    name = (name or "").strip()[:120]
    if not name:
        raise LeagueSeasonError("The new league needs a name.")
    mode_id = prev_league.mode_id if prev_league is not None else A._ensure_default_mode(session).id
    clash = (session.query(ChallengeLeague)
             .filter(ChallengeLeague.mode_id == mode_id,
                     func.lower(ChallengeLeague.name) == name.lower()).first())
    if clash is not None:
        raise LeagueSeasonError(f"A league called “{clash.name}” already exists.")
    top = (session.query(func.coalesce(func.max(ChallengeLeague.sort_order), 0))
           .filter(ChallengeLeague.mode_id == mode_id).scalar())
    league = ChallengeLeague(mode_id=mode_id, name=name, is_active=True,
                             sort_order=int(top or 0) + 1)
    if prev_league is not None:
        for field in LEAGUE_COPY_FIELDS:
            setattr(league, field, getattr(prev_league, field))
    session.add(league)
    session.flush()
    return league


def copy_team_looks(session, prev_league, new_league):
    """Each new team takes its old self's short name, badge and colours."""
    from services import auction_service as A
    if prev_league is None:
        return 0
    old = {A._team_key(t.name): t for t in
           session.query(ChallengeTeam)
           .filter(ChallengeTeam.league_id == prev_league.id).all()}
    copied = 0
    for team in (session.query(ChallengeTeam)
                 .filter(ChallengeTeam.league_id == new_league.id).all()):
        was = old.get(A._team_key(team.name))
        if was is None:
            continue
        team.short_name = was.short_name or team.short_name
        team.logo_url = team.logo_url or was.logo_url
        team.primary_color = team.primary_color or was.primary_color
        team.secondary_color = team.secondary_color or was.secondary_color
        team.sort_order = was.sort_order
        copied += 1
    session.flush()
    return copied


def duplicate_rows(rows):
    """``[(row_number, message)]`` for a player listed under two teams.

    The Challenge Data import only refuses a duplicate inside one team; a
    season's squads must not share a player at all. ``rows`` are
    ``[team, player, version]`` with the header already removed; numbering
    starts at ``first``.
    """
    seen, out = {}, []
    for number, (team, player, version) in rows:
        key = ((player or "").strip().lower(), (version or "").strip().lower())
        if not key[0] or not (team or "").strip():
            continue
        where = seen.get(key)
        if where is not None and where.lower() != team.strip().lower():
            out.append((number, f"{player} is already on {where}"))
            continue
        seen.setdefault(key, team.strip())
    return out


def start_manual_season(session, series, league):
    """✍️ Season N+1 from squads typed into ``league``."""
    prev, _prev_league = _require_next(session, series, needs_league=False)
    if league is None:
        raise LeagueSeasonError("The new league is missing.")
    teams = (session.query(ChallengeTeam)
             .filter(ChallengeTeam.league_id == league.id).count())
    if teams < 2:
        raise LeagueSeasonError("A season needs at least two teams.")
    return _new_season_row(session, series, prev, MODE_MANUAL, league_id=league.id)


# ── Publishing, the hand-over, the tournament ────────────────────────

def publish_season_squads(session, season):
    """The auction's squads into a Challenge League — the same call as the
    auction page's 📤 Publish and ``/apublish``."""
    from services import auction_service as A
    auction = season_auction(session, season)
    if auction is None:
        raise LeagueSeasonError("This season has no auction to publish.")
    series = series_of(session, season)
    try:
        league = A.publish_to_league(
            session, auction,
            league_name=f"{series.name} · {season_title(season)}"[:120])
    except A.AuctionError as exc:
        raise LeagueSeasonError(str(exc))
    season.league_id = league.id
    session.flush()
    return league


HANDOVER_FIELDS = ("command", "tournament_command", "fixtures_command",
                   "tournament_view_commands_json", "short_code")


def hand_over_league(session, prev_league, new_league):
    """Move the league's commands to the new season's league.

    MOVED, not copied: the bot only reads active leagues, but the Challenge
    Data page refuses a view command any other league holds — inactive ones
    included — so a copy would be thrown away the next time anybody saved
    the new league. The old league is deactivated and keeps its squads; its
    finished tournament still answers through ``Tournament.command_snapshot``.
    Returns the fields moved.
    """
    if prev_league is None or new_league is None or prev_league.id == new_league.id:
        return []
    moved = []
    for field in HANDOVER_FIELDS:
        value = getattr(prev_league, field)
        if value and not getattr(new_league, field):
            setattr(new_league, field, value)
            setattr(prev_league, field, None)
            moved.append(field)
    prev_league.is_active = False
    new_league.is_active = True
    session.flush()
    return moved


# Every Tournament column is either a RULE (carried into next season's
# tournament) or RUN STATE (this tournament's own). tests/test_league_seasons.py
# checks the two lists cover the model exactly, so a column added later fails a
# test instead of silently starting next season on its default.
TOURNAMENT_RULE_FIELDS = (
    "description", "format", "overs", "max_teams", "league_format",
    "group_points_mode", "knockout_type",
    "points_win", "points_tie", "points_loss", "points_no_result",
    "min_balls_for_sr", "min_balls_for_econ",
    "min_overseas", "max_overseas", "rating_rules_json",
    "enforce_team_owner", "fixtures_all_at_once", "fixture_round_window",
    "pitch_mode", "preferred_pitches_json", "preferred_pitch_pct",
    "preferred_pitch_limit",
    "injuries_enabled", "injury_chance", "injury_max_matches",
    "announce_chat_id", "round_hours", "tiebreak", "stadiums_json", "prizes_json",
    "playoff_conditions_json", "channels_json",
)
TOURNAMENT_RUN_STATE = (
    "id", "league_id", "league_name", "command_snapshot", "name", "kind",
    "status", "is_active", "schedule_generated",
    # A custom bracket's config names last season's teams.
    "knockout_config_json", "knockout_generated",
    "linked_seasons_json", "best_catch_json",
    "round_deadline_at", "round_tracked", "round_reminder_stage", "recap_round",
    "bracket_posted_key", "awards_given_at", "awards_announced_at", "ceremony_claimed_at",
    "activated_at", "completed_at", "created_at", "updated_at",
)
TEAM_CARRY_FIELDS = ("home_pitch", "home_stadium", "preferred_pitches_json", "award_photo_url")


def _owner_for(session, league, team_name, prev_teams):
    """``(owner_tg_id, owner_name, co_owner_ids, previous TournamentTeam)``."""
    from services import auction_service as A
    from services import tournament_service as TS
    owner, owner_name, extras = TS.draft_owner_for_team(session, league.id, team_name)
    was = prev_teams.get(A._team_key(team_name))
    if not owner and not extras and was is not None and was.owner_tg_id:
        owner, owner_name = was.owner_tg_id, was.owner_name
        extras = [int(x) for x in _loads(was.co_owner_ids_json, [])
                  if str(x).lstrip("-").isdigit()]
    return owner, owner_name, extras, was


def _build_structure(session, tour, prev_tour):
    """The fixtures, the way the Tournaments page's Create would have built
    them — worked out from the stored columns, since the form's preset is not
    stored."""
    from services import knockout_service, league_schedule_service
    try:
        if tour.knockout_type == "pure_knockout":
            n = knockout_service.generate_knockout(session, tour.id)
            tour.schedule_generated = True
            return f"{n}-match knockout bracket ready."
        if (tour.format or "") == "Custom Series":
            matches = 3
            if prev_tour is not None:
                matches = (session.query(TournamentMatch)
                           .filter(TournamentMatch.tournament_id == prev_tour.id)
                           .count()) or 3
            n = league_schedule_service.generate_series(session, tour.id, matches)
            return f"{n}-match series ready."
        if tour.league_format == "groups":
            return "Add the groups and assign teams on the Manage page, then generate the schedule."
        n = league_schedule_service.generate_schedule(session, tour.id)
        return f"{n} fixtures scheduled."
    except ValueError as exc:
        return f"⚠️ {exc} Finish setup on the Manage page."


def create_season_tournament(session, season, *, name=None, activate=False,
                             hand_over=True):
    """The season's tournament, made from last season's rules. Returns
    ``(tournament, message)``.

    Every team of the season's league is entered, with its owner: the people
    who bought the squad (draft or auction), else whoever owned the same side
    last season — its home ground and pitches come along too. With
    ``hand_over`` the league's commands move first, so the tournament's
    ``command_snapshot`` is the command people will actually type.
    """
    from services import tournament_service as TS

    if season_tournament(session, season) is not None:
        raise LeagueSeasonError(f"{season_title(season)} already has a tournament.")
    league = season_league(session, season)
    if league is None:
        raise LeagueSeasonError("Publish or enter the squads first — there is no "
                                "league for the tournament to be played on.")
    teams = (session.query(ChallengeTeam)
             .filter(ChallengeTeam.league_id == league.id,
                     ChallengeTeam.is_active == True)  # noqa: E712
             .order_by(ChallengeTeam.sort_order.asc(), ChallengeTeam.name.asc()).all())
    if len(teams) < 2:
        raise LeagueSeasonError("The league needs at least two teams.")
    prev = previous_season(session, season)
    prev_tour = season_tournament(session, prev) if prev is not None else None
    prev_league = season_league(session, prev) if prev is not None else None
    if hand_over and prev_league is not None:
        hand_over_league(session, prev_league, league)

    series = series_of(session, season)
    settings = ({f: getattr(prev_tour, f) for f in TOURNAMENT_RULE_FIELDS}
                if prev_tour is not None else {})
    title = (name or "").strip() or f"{series.name} {season_title(season)}"
    tour = Tournament(name=title[:120], league_id=league.id,
                      league_name=league.name,
                      command_snapshot=league.tournament_command,
                      kind=TS.KIND_CHALLENGE, status="draft", **settings)
    # A season may have its qualifications edited before its tournament exists.
    from models import LeagueRecordConfig
    record_cfg = session.get(LeagueRecordConfig, season.id)
    if record_cfg is not None:
        tour.min_balls_for_sr = record_cfg.min_bat_balls
        tour.min_balls_for_econ = record_cfg.min_bowl_balls
    tour.max_teams = max(int(settings.get("max_teams") or 8), len(teams))
    if prev_tour is None:
        tour.format = "Round Robin"
        tour.league_format = "single_rr"
    session.add(tour)
    session.flush()

    from services import auction_service as A
    prev_teams = {}
    if prev_tour is not None:
        prev_teams = {A._team_key(tt.name): tt for tt in
                      session.query(TournamentTeam)
                      .filter(TournamentTeam.tournament_id == prev_tour.id).all()}
    for team in teams:
        owner, owner_name, extras, was = _owner_for(session, league, team.name,
                                                    prev_teams)
        row = TournamentTeam(tournament_id=tour.id, challenge_team_id=team.id,
                             name=team.name, short_name=team.short_name,
                             logo_url=team.logo_url, sort_order=team.sort_order,
                             owner_tg_id=owner, owner_name=owner_name,
                             co_owner_ids_json=TS._dump_ids(extras))
        if was is not None:
            for field in TEAM_CARRY_FIELDS:
                setattr(row, field, getattr(was, field))
        session.add(row)
    session.flush()
    message = _build_structure(session, tour, prev_tour)

    season.tournament_id = tour.id
    session.flush()
    link_season_stats(session, season)
    if activate:
        if prev_tour is not None and prev_tour.is_active:
            TS.deactivate_tournament(session, prev_tour.id)
        TS.activate_tournament(session, tour.id)
    session.flush()
    return tour, message


def complete_season(session, season):
    """Complete the season's tournament — the Tournaments page's Complete."""
    from services import tournament_service as TS
    tour = season_tournament(session, season)
    if tour is None:
        raise LeagueSeasonError("This season has no tournament to complete.")
    if tour.status in ("completed", "cancelled"):
        raise LeagueSeasonError(f"{tour.name} is already {tour.status}.")
    TS.set_status(session, tour.id, "completed")
    session.flush()
    refresh_honours(session, season)
    return honours(season).get("champion")


# ──────────────────────────────────────────────────────────────────────
# Stats — one season, every season, one player
# ──────────────────────────────────────────────────────────────────────
#
# A season's player rows are its live ``TournamentPlayerStats`` or, once the
# tournament is deleted, its ``StatsSeasonPlayer``; both carry the same columns
# and ``tournament_service.CareerStats`` sums either. Across seasons a player
# is matched by NAME: the catalogue has one ``Player`` row per card edition,
# so the master id splits a cricketer whose edition changed between seasons.

def season_rows(session, season):
    tour = season_tournament(session, season)
    if tour is not None:
        return (session.query(TournamentPlayerStats)
                .filter(TournamentPlayerStats.tournament_id == tour.id).all())
    saved = season_saved(session, season)
    if saved is not None:
        return (session.query(StatsSeasonPlayer)
                .filter(StatsSeasonPlayer.season_id == saved.id).all())
    return []


def _key(name):
    return " ".join((name or "").strip().lower().split())


def _grouped(rows):
    """``{name key: CareerStats}`` for one season's rows."""
    from services.tournament_service import CareerStats
    out = {}
    for row in rows:
        key = _key(row.name)
        if not key:
            continue
        stats = out.get(key)
        if stats is None:
            stats = out[key] = CareerStats(row.name)
            stats.seasons = 1
        stats.add(row)
        stats.team_name = row.team_name or stats.team_name
    return out


def season_top(session, season, limit=5):
    """``{"batters": [...], "bowlers": [...]}`` — CareerStats, best first."""
    grouped = list(_grouped(season_rows(session, season)).values())
    batters = sorted((s for s in grouped if s.bat_runs),
                     key=lambda s: (-s.bat_runs, -(s.bat_sixes or 0)))[:limit]
    bowlers = sorted((s for s in grouped if s.bowl_wickets),
                     key=lambda s: (-s.bowl_wickets, s.bowl_runs))[:limit]
    return {"batters": batters, "bowlers": bowlers}


def all_time_table(session, series):
    """``{name key: CareerStats}`` summed over every season of the competition."""
    from services.tournament_service import CareerStats, _better_figure
    total = {}
    for season in seasons(session, series):
        for key, stats in _grouped(season_rows(session, season)).items():
            row = total.get(key)
            if row is None:
                row = total[key] = CareerStats(stats.name)
            for field in CareerStats._SUMMED:
                setattr(row, field, getattr(row, field) + getattr(stats, field))
            row.highest_score = max(row.highest_score, stats.highest_score)
            if stats.best_bowl_runs >= 0 and _better_figure(
                    stats.best_bowl_wickets, stats.best_bowl_runs,
                    row.best_bowl_wickets, row.best_bowl_runs):
                row.best_bowl_wickets = stats.best_bowl_wickets
                row.best_bowl_runs = stats.best_bowl_runs
            row.seasons += 1
            row.team_name = stats.team_name or row.team_name
            row.name = stats.name or row.name
    return total


ALL_TIME_CATEGORIES = (
    ("runs", "🏏 Runs"), ("wkts", "🎯 Wickets"), ("sixes", "6️⃣ Sixes"),
    ("hs", "⭐ Highest score"), ("fig", "💥 Best figures"), ("avg", "⚖️ Average"),
    ("titles", "🏆 Titles"),
)


def all_time_leaders(session, series, category="runs", limit=25):
    """CareerStats rows for one board, best first (not for ``titles``)."""
    from services.tournament_service import batting_average
    rows = list(all_time_table(session, series).values())
    if category == "wkts":
        rows = [r for r in rows if r.bowl_wickets]
        rows.sort(key=lambda r: (-r.bowl_wickets, r.bowl_runs))
    elif category == "sixes":
        rows = [r for r in rows if r.bat_sixes]
        rows.sort(key=lambda r: (-r.bat_sixes, -r.bat_runs))
    elif category == "hs":
        rows = [r for r in rows if r.highest_score]
        rows.sort(key=lambda r: (-r.highest_score, -r.bat_runs))
    elif category == "fig":
        rows = [r for r in rows if r.best_bowl_runs >= 0 and r.best_bowl_wickets]
        rows.sort(key=lambda r: (-r.best_bowl_wickets, r.best_bowl_runs))
    elif category == "avg":
        rows = [r for r in rows if (r.bat_outs or 0) >= 2]
        rows.sort(key=lambda r: -(batting_average(r) or 0.0))
    else:
        rows = [r for r in rows if r.bat_runs]
        rows.sort(key=lambda r: (-r.bat_runs, -r.bat_sixes))
    return rows[:limit]


def titles(session, series):
    """``[(team, count, [season titles])]`` — most titles first."""
    tally = {}
    for season in seasons(session, series):
        champ = (honours(season).get("champion") or {}).get("team")
        if not champ:
            continue
        entry = tally.setdefault(_key(champ), [champ, 0, []])
        entry[1] += 1
        entry[2].append(season_title(season))
    return sorted((tuple(v) for v in tally.values()),
                  key=lambda t: (-t[1], t[0]))


def player_history(session, series, query):
    """One player season by season.

    Returns ``{"name", "breakdown": [(season title, CareerStats)], "total"}``,
    or ``{"choices": [names]}`` when the name is ambiguous, or None.
    """
    from services.tournament_service import CareerStats
    want = _key(query)
    if not want:
        return None
    per_season, names = [], {}
    for season in seasons(session, series):
        grouped = _grouped(season_rows(session, season))
        per_season.append((season, grouped))
        for key, stats in grouped.items():
            if want in key:
                names.setdefault(key, stats.name)
    if not names:
        return None
    if want in names:
        key = want
    else:
        words = {k: n for k, n in names.items() if want in k.split()}
        pool = words or names
        if len(pool) > 1:
            return {"choices": sorted(pool.values())[:10]}
        key = next(iter(pool))
    breakdown, total = [], CareerStats(names[key])
    for season, grouped in per_season:
        stats = grouped.get(key)
        if stats is None:
            continue
        breakdown.append((season_title(season), stats))
        total.add(stats)
        total.seasons += 1
        total.team_name = stats.team_name or total.team_name
    total.breakdown = breakdown
    return {"name": names[key], "breakdown": breakdown, "total": total}


# ──────────────────────────────────────────────────────────────────────
# One competition, read for a page or a card
# ──────────────────────────────────────────────────────────────────────

def overview(session, series, now=None):
    """Everything the series page and ``/seasons`` show, newest season first."""
    out = []
    for season in reversed(seasons(session, series)):
        out.append({
            "season": season,
            "title": season_title(season),
            "stage": season_stage(session, season, now),
            "honours": honours(season),
            "mode": mode_label(season),
            "tournament": season_tournament(session, season),
            "league": season_league(session, season),
            "auction": season_auction(session, season),
            "saved": season_saved(session, season),
        })
    return out


def auction_checklist(session, auction, now=None):
    """``[(ok, text)]`` — what ``auction_service.start`` will refuse, said
    before anybody runs ``/astart`` and finds out the hard way."""
    from services import auction_service as A
    if auction is None:
        return []
    field = A.franchises(session, auction.id)
    ownerless = [f.name for f in field if not f.owner_tg_id]
    counts = A.pool_counts(session, auction.id)
    queued = counts.get(A.LOT_QUEUED, 0)
    items = [
        (not ownerless, "Every franchise has an owner" if not ownerless
         else "No owner yet: " + ", ".join(ownerless)),
        (bool(auction.chat_id), "Bound to its group" if auction.chat_id
         else f"Not bound — run /abind {auction.name} in the auction group"),
        (queued > 0, f"{queued} player(s) queued in the pool" if queued
         else "The pool is empty — build it on the auction page"),
    ]
    if auction.status == A.STATUS_SETUP:
        if A.retention_open(auction, now):
            left = A.retention_seconds_left(auction, now)
            items.append((False, "Retention / release window open"
                          + (f" ({_left_text(left)})" if left is not None else "")
                          + " — it closes when the auction starts"))
        else:
            items.append((True, "Retention / release window closed"))
    return items
