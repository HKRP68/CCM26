"""World T20 Championship rules and fixture lifecycle."""
import json
import random
import re
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from fractions import Fraction

from sqlalchemy import update
from sqlalchemy.orm import joinedload, selectinload

from models import (
    ChallengeLeague, ChallengeMode, Match, User, T20Championship as Championship,
    T20ChampionshipCommand as Command,
    T20ChampionshipFixture as Fixture, T20ChampionshipSeries as Series,
    T20ChampionshipTeam as Team,
)
from services import t20_championship_schedule
from utils.tournament_time import IST_OFFSET, parse_ist_deadline

ROSTER_MODE = "World T20 Championship squads"
POINTS = {"win": 2, "tie": 1, "no_result": 1, "loss": 0, "forfeit": 0, "series_win_bonus": 0}
PITCH_MODES = {"home": "Home team chooses", "random": "Random per match"}
SERIES_FORMATS = {"single": "Single T20", "best_of_3": "Best of 3", "best_of_5": "Best of 5",
                  "five_matches": "Five matches (play all five)"}
PLAYOFFS = {"none": "Off", **SERIES_FORMATS}
FINALS = {"none": "No Final", **SERIES_FORMATS}
MATCH_COUNTS = {"single": 1, "best_of_3": 3, "best_of_5": 5, "five_matches": 5}
CLINCH_WINS = {"best_of_3": 2, "best_of_5": 3}
VIEWS = ("info", "help", "table", "fixtures", "teams", "series", "final", "stats", "player",
         "statstour", "tournamentstats", "awards", "scorecard", "play", "teamfix")
SUBCOMMANDS = (*VIEWS, "playoffs", "teamfixtures", "today", "deadline")
# Commands that may be missing from a championship created before they existed;
# ``find_command`` resolves them by suffix so no backfill migration is needed.
LATE_COMMANDS = {"tournamentstats": "tournamentstats", "statstour": "statstour",
                 "teamfix": "teamfix"}
GLOBAL_COMMANDS = {"wtctoday", "matchremind"}
TERMINAL = {"completed", "forfeited", "cancelled"}
STATUS = {"upcoming": "Upcoming", "available": "Available to Play", "live": "In Progress",
          "completed": "Completed", "postponed": "Postponed", "forfeited": "Forfeited",
          "cancelled": "Cancelled"}
SCHEDULE_MODES = {"calendar": "Calendar-based", "open_sequential": "Open — sequential progression"}

# Precise display labels for a fixture that is scheduled but not yet playable.
# ``fixture_status`` still returns the coarse ``upcoming``; ``lock_reason``
# explains which gate is holding a fixture so a schedule can say "Locked —
# Previous Series" rather than a bare "Upcoming".
LOCK_PREVIOUS_MATCH = "Locked — Previous Match"
LOCK_PREVIOUS_SERIES = "Locked — Previous Series"
LOCK_WAITING_OPPONENT = "Waiting for Opponent"
LOCK_OPENS_LATER = "Scheduled — Opens Later"
LOCK_NOT_STARTED = "Scheduled — Opens Later"


def open_mode(championship):
    return (getattr(championship, "schedule_mode", None) or "calendar") == "open_sequential"


def all_commands(championship):
    prefix = championship.command
    return {prefix: "play", **{prefix + view: view for view in VIEWS}}


def find_command(db, command):
    alias = db.query(Command).filter(Command.command == command).first()
    if alias:
        return alias.championship, alias.view
    # Commands added after a championship was created have no alias row for it;
    # resolve them by suffix (longest first, so ``statstour`` is not mistaken for
    # the ``stats`` of a prefix that happens to end that way).
    for suffix, view in sorted(LATE_COMMANDS.items(), key=lambda item: -len(item[0])):
        if command.endswith(suffix):
            championship = db.query(Championship).filter_by(
                command=command[:-len(suffix)]).first()
            if championship:
                return championship, view
    return None


def create(db, name, command, reserved=()):
    name, command = name.strip(), command.strip().lower().removeprefix("/")
    if not name or len(name) > 120:
        raise ValueError("Enter a championship name of up to 120 characters.")
    if not re.fullmatch(r"[a-z0-9_]{1,23}", command):
        raise ValueError("Enter a command prefix of 1–23 letters, digits or underscores.")
    championship = Championship(name=name, command=command, points=json.dumps(POINTS))
    taken = set(reserved) | GLOBAL_COMMANDS
    commands = all_commands(championship)
    taken.update(command for command, in
                 db.query(Command.command).filter(Command.command.in_(commands)))
    taken.update(f"{prefix}teamfix" for prefix, in db.query(Championship.command)
                 .filter(Championship.command.in_([cmd[:-7] for cmd in commands if cmd.endswith("teamfix")])))
    clash = sorted(taken.intersection(commands))
    if clash:
        raise ValueError("Commands already in use: " + ", ".join("/" + cmd for cmd in clash))
    mode = db.query(ChallengeMode).filter_by(name=ROSTER_MODE).first()
    if mode is None:
        mode = ChallengeMode(name=ROSTER_MODE, is_active=False)
        db.add(mode)
    championship.league = ChallengeLeague(
        mode=mode, name=command, is_active=False, multi_enabled=False, auction_enabled=False)
    db.add(championship)
    db.add_all(Command(command=name, championship=championship, view=view)
               for name, view in commands.items())
    db.flush()
    return championship


def update_points(championship, points):
    if championship.started_at:
        raise ValueError("The points system is locked once the championship starts.")
    settings = {key: points.get(key, default) for key, default in POINTS.items()}
    win = settings["win"]
    if win <= 0 or any(not 0 <= value <= win for key, value in settings.items()
                       if key != "series_win_bonus"):
        raise ValueError("Win points must be positive. Other match points must be between zero and win points.")
    if settings["series_win_bonus"] < 0:
        raise ValueError("The series-win bonus must be zero or more.")
    championship.points = json.dumps(settings)


def point_system(championship):
    return {**POINTS, **json.loads(championship.points)}


def series_list(db, championship, *, refresh=False):
    query = (db.query(Series).filter_by(championship_id=championship.id)
            .options(joinedload(Series.home).joinedload(Team.team),
                     joinedload(Series.away).joinedload(Team.team),
                     selectinload(Series.fixtures).joinedload(Fixture.match).load_only(
                         Match.pitch_type, Match.stadium, Match.margin_type, Match.margin_value,
                         Match.completed_at))
            .order_by(Series.position))
    return (query.populate_existing() if refresh else query).all()


def lock(db, championship_id):
    # All writes serialize on the championship, including on SQLite where
    # SELECT FOR UPDATE provides no row lock.
    db.execute(update(Championship).where(Championship.id == championship_id)
               .values(command=Championship.command))
    return db.get(Championship, championship_id, populate_existing=True)


def day_window(day):
    opens = datetime.combine(day, datetime.min.time()) - IST_OFFSET
    return opens, opens + timedelta(days=1, seconds=-1)


def match_reference(fixture):
    return f"S{fixture.series_id}M{fixture.number}"


def find_fixture(rows, reference):
    match = re.fullmatch(r"S([0-9]{1,10})M([0-9]{1,10})", reference, re.IGNORECASE)
    if match:
        series_id, number = map(int, match.groups())
        return next((f for series in rows if series.id == series_id
                     for f in series.fixtures if f.number == number), None)
    numeric = reference.removeprefix("#")
    if re.fullmatch(r"[0-9]{1,10}", numeric):
        return next((f for series in rows for f in series.fixtures if f.id == int(numeric)), None)
    return None


def extend_deadline(db, championship_id, reference, value, now=None):
    now = now or datetime.utcnow()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            deadline = day_window(date.fromisoformat(value))[1]
        except (ValueError, OverflowError) as exc:
            raise ValueError("Enter a valid date in YYYY-MM-DD format.") from exc
    else:
        deadline = parse_ist_deadline(value)
    championship = lock(db, championship_id)
    if championship is None or championship.completed_at:
        raise ValueError("This championship is closed for deadline changes.")
    fixture = find_fixture(series_list(db, championship, refresh=True), reference)
    if fixture is None:
        raise ValueError("Fixture not found. Copy its S…M… reference or numeric ID from the schedule.")
    if fixture.status not in ("scheduled", "postponed"):
        raise ValueError("Only scheduled or postponed fixtures can have their deadline extended.")
    if not is_postseason(fixture.series):
        editable_schedule(championship)
        # In open mode a league fixture is gated by the shared league deadline,
        # not its own, so a per-fixture extension would silently change nothing.
        if open_mode(championship):
            raise ValueError(
                "This championship is in open mode, where one shared league "
                "deadline governs every league fixture. Change it from the "
                "Schedule availability setting, not one fixture at a time.")
    if deadline <= max(now, fixture.deadline) or deadline < fixture.opens_at:
        raise ValueError("Choose a future deadline later than the current one.")
    previous = fixture.deadline
    fixture.deadline, fixture.status = deadline, "scheduled"
    if not is_postseason(fixture.series):
        championship.end_at = max(championship.end_at or deadline, deadline)
    return fixture, previous


def fixtures_on_day(rows, day):
    return [f for series in rows for f in series.fixtures
            if day in ((f.opens_at + IST_OFFSET).date(), (f.deadline + IST_OFFSET).date())]


def command_championships(db, prefix="", *, viewer_id=None, active_only=False):
    query = db.query(Championship).order_by(Championship.id.desc())
    if prefix:
        query = query.filter(Championship.command == prefix.lower().removeprefix("/"))
    candidates = query.all()
    if prefix:
        return [c for c in candidates if not active_only or (c.started_at and not c.completed_at)]
    active = [c for c in candidates if c.started_at and not c.completed_at]
    candidates = active if active or active_only else candidates
    assigned_cups = [c for c in candidates if viewer_id is not None and
                     any(assigned(t, viewer_id) for t in c.teams)]
    return assigned_cups or candidates


def _series(championship, home, away, position, windows, **settings):
    series = Series(championship=championship, home=home, away=away, position=position, **settings)
    series.fixtures = [Fixture(number=i, opens_at=opens, deadline=deadline)
                       for i, (opens, deadline) in enumerate(windows, 1)]
    return series


def generate(db, championship, *, team_count, opponents, matches, start, end, rest,
             team_ids=None, max_matches=None):
    if championship.started_at:
        raise ValueError("The schedule generator is available before the championship starts.")
    selected = set(team_ids) if team_ids is not None else {
        team.id for team in championship.teams if team.is_participating}
    teams = sorted((team for team in championship.teams if team.id in selected),
                   key=lambda team: (team.name.casefold(), team.id))
    if team_count != len(teams) or len(teams) != len(selected):
        raise ValueError(f"Select {team_count} of the saved teams as participants.")
    maximum = matches if max_matches is None else max_matches
    schedule = t20_championship_schedule.plan(teams, opponents, matches, start, end, rest,
                                             max_duration=maximum)
    championship.series.clear()
    db.flush()
    for team in championship.teams:
        team.is_participating = team.id in selected
    for position, (home, away, day, count) in enumerate(schedule, 1):
        db.add(_series(championship, home, away, position,
                       [day_window(day + timedelta(days=i)) for i in range(count)]))
    championship.start_at, championship.end_at = day_window(start)[0], day_window(end)[1]
    championship.opponents, championship.matches_per_series, championship.rest_days = opponents, matches, rest
    championship.max_matches_per_series = maximum
    return {count for _, _, _, count in schedule}


def editable_schedule(championship):
    if championship.completed_at or any(is_postseason(series) for series in championship.series):
        raise ValueError("The league schedule is locked once the postseason starts or the championship ends.")


def regenerable_series(series):
    return (not is_postseason(series) and
            all(fixture.status in ("scheduled", "postponed") and not fixture.match_id
                for fixture in series.fixtures))


def regenerate_remaining(db, championship, *, matches, max_matches, start, end, rest, now=None):
    """Rebuild untouched tours in place while preserving the league's pairings and results."""
    editable_schedule(championship)
    if not championship.started_at:
        raise ValueError("Generate the draft schedule first.")
    today = ((now or datetime.utcnow()) + IST_OFFSET).date()
    if start < today:
        raise ValueError("Choose today or a later date for the unplayed schedule.")
    if rest < 0:
        raise ValueError("Use nonnegative rest days.")
    rows = series_list(db, championship)
    upcoming = [series for series in rows if regenerable_series(series)]
    if not upcoming:
        raise ValueError("There are no untouched league series to regenerate.")
    kept = [series for series in rows if not regenerable_series(series)]
    teams = [team.id for team in championship.teams if team.is_participating]
    totals = dict.fromkeys(teams, 0)
    ready_on = {}
    last_kept_deadline = None
    for series in kept:
        fixtures = [fixture for fixture in series.fixtures if fixture.status != "cancelled"]
        for team in (series.home_id, series.away_id):
            totals[team] += len(fixtures)
        if not fixtures:
            continue
        last = max(max(fixture.deadline,
                       (fixture.match.completed_at if fixture.match else None) or fixture.deadline)
                   for fixture in fixtures)
        last_kept_deadline = max(last_kept_deadline or last, last)
        ready = (last + IST_OFFSET).date() + timedelta(days=rest + 1)
        for team in (series.home_id, series.away_id):
            ready_on[team] = max(ready_on.get(team, start), ready)

    by_pair = defaultdict(deque)
    matchups = []
    for series in upcoming:
        pair = (series.home_id, series.away_id)
        matchups.append(pair)
        by_pair[pair].append(series)
    schedule = t20_championship_schedule.plan_remaining(
        teams, matchups, totals, ready_on, matches, max_matches, start, end, rest)

    # Clear the affected sort positions before assigning the new order.
    for series in upcoming:
        series.position = -series.id
    db.flush()
    next_position = max((series.position for series in kept), default=0) + 1
    for position, (home, away, day, count) in enumerate(schedule, next_position):
        series = by_pair[home, away].popleft()
        series.position = position
        del series.fixtures[count:]
        for index in range(count):
            opens, deadline = day_window(day + timedelta(days=index))
            if index < len(series.fixtures):
                fixture = series.fixtures[index]
                fixture.opens_at, fixture.deadline, fixture.status = opens, deadline, "scheduled"
            else:
                series.fixtures.append(Fixture(number=index + 1, opens_at=opens, deadline=deadline))
    end_at = day_window(end)[1]
    championship.end_at = max(end_at, last_kept_deadline or end_at)
    championship.matches_per_series = matches
    championship.max_matches_per_series = max_matches
    championship.rest_days = rest
    return {count for _, _, _, count in schedule}


def league_series_limit(championship):
    return max(5, championship.max_matches_per_series or 0)


def add_series(db, championship, home, away, count, start, **settings):
    editable_schedule(championship)
    if home is away:
        raise ValueError("Choose two different teams.")
    limit = league_series_limit(championship)
    if not 3 <= count <= limit:
        raise ValueError(f"Choose between 3 and {limit} matches.")
    if championship.started_at and not (home.is_participating and away.is_participating):
        raise ValueError("Choose two participating teams.")
    position = max((series.position for series in championship.series), default=0) + 1
    row = _series(championship, home, away, position,
                  [day_window(start + timedelta(days=i)) for i in range(count)], **settings)
    db.add(row)
    home.is_participating = away.is_participating = True
    return row


def start(championship, now=None):
    if championship.started_at:
        raise ValueError("The championship has already started.")
    if not any(series.fixtures for series in championship.series):
        raise ValueError("Generate a schedule or add a series first.")
    teams = [team for team in championship.teams if team.is_participating]
    if len(teams) < 2:
        raise ValueError("Set up at least two teams first.")
    if championship.playoff_format != "none":
        if len(teams) < 4:
            raise ValueError("Top-four semifinals require at least four participating teams.")
        if championship.final_format == "none":
            raise ValueError("Choose a final format to use the semifinals.")
    for team in teams:
        if len(team.team.players) < 11:
            raise ValueError(f"{team.name} needs at least 11 squad members.")
        if not team.home_stadium:
            raise ValueError(f"Set a home stadium for {team.name} on the Teams tab before starting.")
    championship.started_at = now or datetime.utcnow()


def series_complete(series):
    return all(fixture.status in TERMINAL for fixture in series.fixtures)


def is_postseason(series):
    return bool(series.is_final or series.semifinal_no)


def series_score(series):
    return tuple(sum(f.winner_id == team_id for f in series.fixtures if f.status in TERMINAL)
                 for team_id in (series.home_id, series.away_id))


def needs_decider(series):
    home, away = series_score(series)
    return is_postseason(series) and series_complete(series) and home == away


def series_name(series):
    if series.is_final:
        return f"World T20 Championship Final · {series.home.name} vs {series.away.name}"
    if series.semifinal_no:
        return f"Semifinal {series.semifinal_no} · {series.home.name} vs {series.away.name}"
    return f"{series.away.name} tour of {series.home.name}"


def _potm_name(fixture):
    if not fixture.scorecard:
        return ""
    data = json.loads(fixture.scorecard)
    best = next((p for p in data["players"] if p["roster_id"] == data.get("potm")), None)
    return f'{best["name"]} ({best["team_name"]})' if best else ""


def _next_match_note(championship, series):
    """The next unplayed match in this series and its availability/lock reason."""
    upcoming = next((f for f in series.fixtures if f.status not in TERMINAL), None)
    if upcoming is None:
        return ""
    rows = sorted(championship.series, key=lambda row: row.position)
    label, blocker = lock_reason(championship, upcoming, next_fixtures(rows))
    note = f"Next · {match_reference(upcoming)} · {label}"
    return note + (f" ({blocker})" if blocker else "")


def result_news(fixture):
    """Announce a completed championship match: series score, POTM and what is next.

    Reports on every completed match, not only a series-closing one, so owners
    see the running series score and whether their next match is open.
    """
    if fixture is None:
        return ""
    series = fixture.series
    championship = series.championship
    if championship.champion_id:
        winner = next(team.name for team in championship.teams if team.id == championship.champion_id)
        return f"🏆 {winner} won {championship.name}!"
    home, away = series_score(series)
    score = f"{series.home.name} {home}–{away} {series.away.name}"
    lines = []
    potm = _potm_name(fixture)
    if potm:
        lines.append(f"POTM · {potm}")
    if series_complete(series):
        if needs_decider(series):
            lines += [f"{score} · Decider needed", f"/{championship.command} playoffs"]
        elif series.semifinal_no:
            winner = series.home if home > away else series.away
            lines += [f"{score} · {winner.name} advances to the final",
                      f"/{championship.command} playoffs"]
        else:
            result = ("Series drawn" if home == away else
                      f"{(series.home if home > away else series.away).name} won the series")
            lines += [f"{score} · {result}", f"/{championship.command}table"]
        return "\n".join(lines)
    lines.append(f"Series · {score}")
    note = _next_match_note(championship, series)
    if note:
        lines.append(note)
    return "\n".join(lines)


def next_fixtures(series_rows):
    """The first unfinished series for each team and first fixture in each series."""
    next_by_team = {}
    for series in series_rows:
        fixture = next((f for f in series.fixtures if f.status not in TERMINAL), None)
        if fixture:
            for team_id in (series.home_id, series.away_id):
                next_by_team.setdefault(team_id, fixture.id)
    return next_by_team


def effective_deadline(championship, fixture):
    """The deadline that actually gates a fixture for its mode and stage.

    In open sequential mode a league fixture's own date and per-fixture deadline
    are ignored for eligibility: the shared league-stage deadline applies
    instead, safely inheriting the league end date when none is set explicitly.
    Postseason fixtures always keep their own configured deadline.
    """
    if open_mode(championship) and not is_postseason(fixture.series):
        return championship.league_deadline or championship.end_at or fixture.deadline
    return fixture.deadline


def _league_open(championship, fixture):
    """True when a league fixture's own opening date no longer gates it."""
    return open_mode(championship) and not is_postseason(fixture.series)


def fixture_status(championship, fixture, next_by_team, now=None):
    if fixture.status != "scheduled":
        return fixture.status
    now = now or datetime.utcnow()
    if not championship.started_at:
        return "upcoming"
    if not _league_open(championship, fixture) and now < fixture.opens_at:
        return "upcoming"
    series = fixture.series
    if all(next_by_team.get(team_id) == fixture.id for team_id in (series.home_id, series.away_id)):
        return "available" if now <= effective_deadline(championship, fixture) else "postponed"
    return "upcoming"


def lock_reason(championship, fixture, next_by_team, now=None):
    """A precise display label and optional blocking reference for a fixture.

    Returns ``(label, blocking_reference)``. For an available/live/terminal
    fixture the label is the ordinary status and the reference is empty; for a
    fixture that is scheduled but not yet playable it explains exactly why.
    """
    status = fixture_status(championship, fixture, next_by_team, now)
    if status == "postponed" and fixture.status == "scheduled":
        # Derived rather than admin-set: the effective deadline has passed.
        return "Postponed — Deadline Passed", ""
    if status != "upcoming":
        return STATUS[status], ""
    now = now or datetime.utcnow()
    if not championship.started_at:
        return LOCK_NOT_STARTED, ""
    series = fixture.series
    # An earlier unfinished match in this same series blocks this one.
    earlier = next((f for f in series.fixtures
                    if f.number < fixture.number and f.status not in TERMINAL), None)
    if earlier is not None:
        return LOCK_PREVIOUS_MATCH, match_reference(earlier)

    def blocking(team_id):
        pending = next_by_team.get(team_id)
        if pending is None or pending == fixture.id:
            return None
        return next((f for row in series.championship.series for f in row.fixtures
                     if f.id == pending), None)

    home_ready = next_by_team.get(series.home_id) == fixture.id
    away_ready = next_by_team.get(series.away_id) == fixture.id
    # One side is ready and waiting on the other to finish its current series.
    if home_ready != away_ready:
        waiting_for = series.away if home_ready else series.home
        return LOCK_WAITING_OPPONENT, waiting_for.name
    # Neither side has reached this fixture: it sits behind an earlier series.
    for team_id in (series.home_id, series.away_id):
        blocker = blocking(team_id)
        if blocker is not None and not is_postseason(blocker.series):
            team = series.home if series.home_id == team_id else series.away
            return LOCK_PREVIOUS_SERIES, f"{team.name} · {match_reference(blocker)}"
    if not _league_open(championship, fixture) and now < fixture.opens_at:
        return LOCK_OPENS_LATER, ""
    return LOCK_WAITING_OPPONENT, ""


def set_schedule_mode(championship, mode, deadline=None):
    """Switch the schedule availability mode, keeping the original calendar.

    Does not start the championship and does not touch fixture opening dates or
    individual deadlines — those stay as history and for switching back. In open
    mode a shared league-stage deadline is required: it is taken from ``deadline``
    when given, else the current league deadline, else the league end date.
    """
    if mode not in SCHEDULE_MODES:
        raise ValueError("Choose a schedule availability mode.")
    if championship.completed_at:
        raise ValueError("The schedule mode is locked once the championship ends.")
    if mode == "open_sequential":
        deadline = deadline or championship.league_deadline or championship.end_at
        if deadline is None:
            raise ValueError("Set a league-stage deadline, or generate the schedule first so one can be inherited.")
        championship.league_deadline = deadline
    championship.schedule_mode = mode
    return championship


def _teams_for_fixture(fixture):
    return fixture.series.home, fixture.series.away


def captain_ids(team):
    return [team.owner_id, *team.co_owner_ids]


def assigned(team, telegram_id):
    return telegram_id in captain_ids(team)


def meeting(fixture, host_telegram, target_telegram):
    home, away = _teams_for_fixture(fixture)
    if assigned(home, host_telegram) and assigned(away, target_telegram):
        return home, away
    if assigned(away, host_telegram) and assigned(home, target_telegram):
        return away, home
    return None


def validate_stadium(name):
    from services.tournament_stadiums import resolve

    if name is not None and not isinstance(name, str):
        raise ValueError("Enter a stadium name from Stadium Data.")
    name = (name or "").strip()
    if not name:
        return None
    stadium = resolve(name)
    if not stadium:
        raise ValueError(f"“{name}” was not found in Stadium Data.")
    return stadium


def series_stadium(series):
    if series.is_final and series.stadium:
        return series.stadium
    return series.home.home_stadium


def _available_fixture(db, championship_id, fixture_id):
    championship = lock(db, championship_id)
    if championship is None:
        raise FixtureUnavailable("This championship no longer exists.")
    rows = series_list(db, championship, refresh=True)
    fixture = next((f for row in rows for f in row.fixtures if f.id == fixture_id), None)
    if fixture is None or fixture_status(championship, fixture, next_fixtures(rows)) != "available":
        raise FixtureUnavailable("This championship fixture is no longer available.")
    return championship, fixture


def prepare_conditions(db, championship_id, fixture_id):
    """Keep a random draw on its fixture across setup retries."""
    from services.match_constants import PITCH_TYPES

    championship, fixture = _available_fixture(db, championship_id, fixture_id)
    series = fixture.series
    stadium = series_stadium(series)
    if not stadium:
        raise FixtureUnavailable(f"Set a home stadium for {series.home.name} in the championship Teams tab.")
    try:
        validate_stadium(stadium)
    except ValueError as exc:
        raise FixtureUnavailable(str(exc)) from exc
    pitch = None
    if championship.pitch_mode == "random":
        if fixture.random_pitch not in PITCH_TYPES:
            fixture.random_pitch = random.choice(PITCH_TYPES)
            db.flush()
        pitch = fixture.random_pitch
    return fixture, championship.pitch_mode, stadium, pitch


def bind(db, tag, match, host, target):
    """Re-check the exact fixture at kickoff and link it in the Match transaction."""
    from services.match_constants import PITCH_TYPES

    championship, fixture = _available_fixture(db, tag["championship_id"], tag["fixture_id"])
    teams = meeting(fixture, host.telegram_id, target.telegram_id)
    if not teams or teams[0].id != fixture.series.home_id or [t.id for t in teams] != tag["teams"]:
        raise FixtureUnavailable("Team assignments changed. Start the fixture again.")
    stadium = series_stadium(fixture.series)
    pitch = fixture.random_pitch if championship.pitch_mode == "random" else match.pitch_type
    if (not stadium or stadium != tag.get("stadium") or stadium != match.stadium
            or championship.pitch_mode != tag.get("pitch_mode")
            or pitch not in PITCH_TYPES or match.pitch_type != pitch):
        raise FixtureUnavailable("The fixture's conditions changed. Start the fixture again.")
    fixture.status, fixture.match_id = "live", match.id


class FixtureUnavailable(ValueError):
    pass


def release(db, fixture_id, match_id):
    return (db.query(Fixture).filter_by(id=fixture_id, match_id=match_id, status="live")
            .update({Fixture.status: "scheduled", Fixture.match_id: None}))


def heal_abandoned(db):
    # A completed Match can still have a live fixture if an admin cleared the
    # game before it produced a result. Keep paused matches bound for /continue.
    return (db.query(Fixture).filter(Fixture.status == "live", Fixture.match_id.in_(
        db.query(Match.id).filter(Match.status.in_(("abandoned", "cancelled", "completed")))))
        .update({Fixture.status: "scheduled", Fixture.match_id: None}, synchronize_session=False))


def league_match_totals(championship, series_rows):
    totals = {team.id: 0 for team in championship.teams if team.is_participating}
    for series in series_rows:
        if not is_postseason(series):
            count = sum(fixture.status != "cancelled" for fixture in series.fixtures)
            totals[series.home_id] += count
            totals[series.away_id] += count
    return [(team.name, totals[team.id]) for team in championship.teams if team.is_participating]


def standings(championship, rows):
    points = point_system(championship)
    series_bonus = points["series_win_bonus"]
    table = {team.id: dict(team=team, played=0, won=0, lost=0, tied=0, nr=0, points=0,
                          bonus=0, max_points=0,
                          pct=Fraction(), nrr=Fraction(), series_played=0, series_won=0,
                          series_lost=0, series_drawn=0, home_wins=0, away_wins=0)
             for team in championship.teams if team.is_participating}
    head = defaultdict(int)
    nrr = defaultdict(lambda: [0, 0, 0, 0])
    for series in rows:
        if is_postseason(series):
            continue
        home, away = series.home_id, series.away_id
        played = [f for f in series.fixtures if f.status in ("completed", "forfeited")]
        for f in played:
            for team_id, other in ((home, away), (away, home)):
                row = table[team_id]
                row["played"] += 1
                row["max_points"] += points["win"]
                if f.winner_id:
                    won = f.winner_id == team_id
                    row["won" if won else "lost"] += 1
                    row["points"] += points["win" if won else ("forfeit" if f.result == "forfeit" else "loss")]
                    head[team_id, other] += int(won)
                else:
                    row["tied" if f.result == "tie" else "nr"] += 1
                    row["points"] += points[f.result]
            if f.scorecard and f.result in ("win", "tie"):
                innings = json.loads(f.scorecard)["innings"]
                if len(innings) == 2 and all(i["balls"] for i in innings):
                    for a, b in (innings, innings[::-1]):
                        total = nrr[a["team_id"]]
                        total[0] += a["runs"]
                        total[1] += 120 if a["wickets"] >= 10 else a["balls"]
                        total[2] += b["runs"]
                        total[3] += 120 if b["wickets"] >= 10 else b["balls"]
        if played and series_complete(series):
            h, a = series_score(series)
            for team_id, wins, losses, field in ((home, h, a, "home_wins"), (away, a, h, "away_wins")):
                row = table[team_id]
                row["series_played"] += 1
                row["max_points"] += series_bonus
                if wins > losses:
                    row["bonus"] += series_bonus
                row["series_won" if wins > losses else "series_lost" if wins < losses else "series_drawn"] += 1
                row[field] += int(wins > losses)

    for team_id, row in table.items():
        row["points"] += row["bonus"]
        if row["max_points"]:
            row["pct"] = Fraction(row["points"] * 100, row["max_points"])
        r, b, ar, ab = nrr[team_id]
        if b and ab:
            row["nrr"] = Fraction(6*r, b) - Fraction(6*ar, ab)

    # Apply head-to-head inside each tied group. A pairwise sort can become
    # non-transitive when three teams each beat one of the others.
    groups = defaultdict(list)
    for row in table.values():
        groups[(bool(row["played"]), row["pct"], row["points"], row["nrr"], row["won"])].append(row)
    ranked = []
    for key in sorted(groups, reverse=True):
        group = groups[key]
        ids = [row["team"].id for row in group]
        ranked.extend(sorted(group, key=lambda row: (
            -sum(head[row["team"].id, other] - head[other, row["team"].id] for other in ids),
            row["team"].name.casefold(), row["team"].id)))
    return ranked


def _knockout_winner(series, match_format):
    home, away = series_score(series)
    target = CLINCH_WINS.get(match_format)
    if target and max(home, away) >= target:
        for fixture in series.fixtures:
            if fixture.status not in TERMINAL:
                fixture.status = "cancelled"
    if home != away and series_complete(series):
        return series.home if home > away else series.away
    return None


def _round_windows(championship, now, deadline, match_format):
    deadline = deadline or max(championship.end_at or now, now) + timedelta(days=7)
    return [(min(now, deadline), deadline)] * MATCH_COUNTS[match_format]


def advance(db, championship, now=None):
    """Advance exactly once while holding the championship write lock."""
    if not championship.started_at or championship.completed_at:
        return
    now = now or datetime.utcnow()
    db.flush()
    rows = series_list(db, championship)
    final = next((row for row in rows if row.is_final), None)
    if final:
        winner = _knockout_winner(final, championship.final_format)
        if winner:
            decisive = next(f for f in reversed(final.fixtures) if f.winner_id == winner.id)
            championship.champion_id = winner.id
            championship.champion_captain = decisive.captain_id or winner.owner_id
            championship.completed_at = now
        return
    semifinals = [row for row in rows if row.semifinal_no]
    if semifinals:
        finalists = [_knockout_winner(row, championship.playoff_format) for row in semifinals]
        if len(finalists) != 2 or not all(finalists):
            return
    else:
        if not rows or not all(series_complete(series) for series in rows):
            return
        table = standings(championship, rows)
        if not any(row["played"] for row in table):
            return
        leader = table[0]["team"]
        if championship.final_format == "none":
            championship.champion_id = leader.id
            championship.champion_captain = leader.owner_id
            championship.completed_at = now
            return
        if championship.playoff_format != "none":
            if len(table) < 4:
                return
            windows = _round_windows(championship, now, championship.playoff_deadline,
                                     championship.playoff_format)
            seeds = (row["team"] for row in table[:4])
            first, second, third, fourth = seeds
            for number, (home, away) in enumerate(((first, fourth), (second, third)), 1):
                db.add(_series(championship, home, away, rows[-1].position + number,
                               windows, semifinal_no=number))
            return
        finalists = [leader, table[1]["team"]]
    windows = _round_windows(championship, now, championship.final_deadline, championship.final_format)
    db.add(_series(championship, *finalists, rows[-1].position + 1,
                   windows, is_final=True, venue=championship.final_venue,
                   stadium=championship.final_stadium))


def set_result(db, championship, fixture, result, winner_id=None, captain_id=None):
    if result not in ("win", "tie", "no_result", "forfeit"):
        raise ValueError("Choose a valid result.")
    if result in ("win", "forfeit"):
        if winner_id not in (fixture.series.home_id, fixture.series.away_id):
            raise ValueError("Select one of the fixture's teams as the winner.")
    else:
        winner_id, captain_id = None, None
    fixture.result, fixture.winner_id, fixture.captain_id = result, winner_id, captain_id
    fixture.status = "forfeited" if result == "forfeit" else "completed"
    advance(db, championship)


def record_played(db, state, *, winner_user_id=None, forfeit=False):
    tag = state.get("t20_championship")
    if not tag:
        return
    championship = lock(db, tag["championship_id"])
    fixture = db.get(Fixture, tag["fixture_id"], populate_existing=True)
    if not fixture or fixture.status != "live" or fixture.match_id != state["match_id"]:
        return
    from services.t20_championship_stats import capture
    users = dict(zip(tag["users"], tag["teams"]))
    winner_id = users.get(winner_user_id)
    if winner_user_id is not None and winner_id is None:
        raise ValueError("The winning user is outside the championship fixture.")
    winner = db.get(Team, winner_id) if winner_id else None
    fixture.scorecard = json.dumps(capture(state, users, winner.name if winner else None,
                                            award_potm=not forfeit))
    captain = db.get(User, winner_user_id) if winner_user_id else None
    set_result(db, championship, fixture, "forfeit" if forfeit else "win" if winner else "tie",
               winner_id, captain.telegram_id if captain else None)
    return fixture


def record_forfeit(db, match, winner_user_id, state=None):
    """Cover Telegram and Mini App exits, including games with no loaded state."""
    if state and state.get("t20_championship"):
        record_played(db, state, winner_user_id=winner_user_id, forfeit=True)
        return True
    from services.match_outcome import TYPE_CIPL
    if match.match_type != TYPE_CIPL:
        return False
    fixture = db.query(Fixture).filter_by(match_id=match.id, status="live").first()
    if fixture is None:
        return False
    host, target = (db.get(User, uid) for uid in (match.user1_id, match.user2_id))
    teams = meeting(fixture, host.telegram_id, target.telegram_id)
    if not teams:
        raise ValueError("Could not resolve the captains for this championship fixture.")
    tag = dict(championship_id=fixture.series.championship_id, fixture_id=fixture.id,
               users=[host.id, target.id], teams=[team.id for team in teams])
    record_played(db, {**(state or {}), "match_id": match.id, "t20_championship": tag},
                  winner_user_id=winner_user_id, forfeit=True)
    return True
