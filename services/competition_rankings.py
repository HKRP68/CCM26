"""Persisted Elo replay over uniquely identified, verified official fixtures."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
import logging

from sqlalchemy import and_, delete, func, insert, select, text, update
from sqlalchemy.orm import defer

from database import get_session
from models import Match, Tournament, TournamentMatch, User
from models_events import (StudioCompetition, StudioFixture, StudioFixtureResult, StudioLobbyPolicy,
                           StudioParticipant, StudioRevision)
from models_universe import (CompetitionArchivedResult, CompetitionControllerEvidence, CompetitionProjection,
                             CompetitionRating, CompetitionRatingEvent)
from services import competition_sources as sources


POLICY = 1
START = 1500
PROVISIONAL = 10
MIN_OPPONENTS = 3
MAX_RESULTS = 20_000
REFRESH = timedelta(minutes=5)
logger = logging.getLogger(__name__)


def change(a, b, score, k):
    expected = 1 / (1 + 10 ** max(-100, min(100, (b - a) / 400)))
    return round(k * (score - expected)), expected


def _live(db, limit):
    rows = db.execute(select(StudioCompetition, StudioRevision, StudioFixture, StudioFixtureResult)
        .join(StudioLobbyPolicy, StudioLobbyPolicy.competition_id == StudioCompetition.id)
        .join(StudioRevision, and_(StudioRevision.event_id == StudioCompetition.event_id,
                                  StudioRevision.number == StudioCompetition.revision))
        .join(StudioFixture, StudioFixture.competition_id == StudioCompetition.id)
        .join(StudioFixtureResult, StudioFixtureResult.fixture_id == StudioFixture.id)
        .where(StudioCompetition.kind == "multi", StudioLobbyPolicy.visibility == "public",
               StudioRevision.config["competition"]["world_rankings"].as_boolean().is_(True))
        .options(defer(StudioFixture.state), defer(StudioFixture.seed))
        .order_by(StudioFixtureResult.recorded_at, StudioFixture.id).limit(limit)).all()
    if not rows:
        return []
    humans = defaultdict(dict)
    for room, team, user in db.execute(select(StudioParticipant.competition_id, StudioParticipant.team_key,
                                              StudioParticipant.user_id).where(
            StudioParticipant.competition_id.in_({room.id for room, _, _, _ in rows}))).all():
        humans[room][team] = user
    templates = {}
    for room, revision, fixture, outcome in rows:
        key = room.event_id, room.revision
        if key not in templates:
            templates[key] = {row["key"]: row for row in revision.config["fixtures"]}
    return [sources.live_result(room, rev.config, fixture, result, humans[room.id],
                                templates[(room.event_id, room.revision)]) for room, rev, fixture, result in rows]


def _challenge(db, limit):
    query = (select(Tournament, TournamentMatch, Match, CompetitionControllerEvidence)
             .join(TournamentMatch, TournamentMatch.tournament_id == Tournament.id)
             .join(CompetitionControllerEvidence, CompetitionControllerEvidence.fixture_id == TournamentMatch.id)
             .outerjoin(Match, Match.id == TournamentMatch.match_id)
             .where(Tournament.status.in_(sources.CL_VISIBLE), TournamentMatch.status == "completed")
             .options(defer(TournamentMatch.scorecard_json))
             .order_by(TournamentMatch.completed_at, TournamentMatch.id).limit(limit))
    rows = [sources.challenge_result(*row) for row in db.execute(query)]
    if len(rows) < limit:
        archive = select(CompetitionArchivedResult).order_by(
            CompetitionArchivedResult.recorded_at, CompetitionArchivedResult.fixture_id).limit(limit-len(rows))
        for row in db.scalars(archive):
            data = row.detail
            rows.append(sources.Result(sources.CHALLENGE, row.tournament_id, row.fixture_id, 0, data["format"],
                tuple(data["sides"]), data["winner"], "completed", data["stage"], row.recorded_at,
                tuple(data["innings"]), tuple(data["controllers"]), "verified"))
    return rows


def archive_challenge(db, tour):
    """Keep a normalized rating input when official fixtures are archived."""
    if not sources.enabled(sources.CHALLENGE) or not sources.flag("RANKINGS"):
        return
    if tour.kind not in (None, sources.CHALLENGE) or tour.status != "completed":
        return
    rows = sources.result_rows(db, sources.CHALLENGE, tour, limit=MAX_RESULTS + 1)
    if len(rows) > MAX_RESULTS:
        logger.error("Challenge League %s exceeded the verified result archive limit", tour.id)
        return
    existing = set(db.scalars(select(CompetitionArchivedResult.fixture_id)
                              .where(CompetitionArchivedResult.tournament_id == tour.id)))
    for result, _ in rows:
        if result.eligibility != "verified" or not result.recorded_at or result.fixture in existing:
            continue
        db.add(CompetitionArchivedResult(fixture_id=result.fixture, tournament_id=tour.id, recorded_at=result.recorded_at,
               detail={"format": result.format, "sides": result.sides, "winner": result.winner, "stage": result.stage,
                       "innings": result.innings, "controllers": result.controllers}))


def source_results(db):
    rows = _live(db, MAX_RESULTS + 1) if sources.enabled(sources.LIVE) else []
    if sources.enabled(sources.CHALLENGE) and len(rows) <= MAX_RESULTS:
        rows.extend(_challenge(db, MAX_RESULTS + 1 - len(rows)))
    if len(rows) > MAX_RESULTS:
        raise ValueError("Ranking scan exceeded the configured safe rebuild size.")
    if sources.enabled(sources.CHALLENGE):
        historic = db.scalar(select(func.count(TournamentMatch.id))
                 .join(Tournament, Tournament.id == TournamentMatch.tournament_id)
                 .outerjoin(CompetitionControllerEvidence, CompetitionControllerEvidence.fixture_id == TournamentMatch.id)
                 .where(Tournament.status.in_(sources.CL_VISIBLE),
                        (Tournament.kind == sources.CHALLENGE) | Tournament.kind.is_(None),
                        TournamentMatch.status == "completed", CompetitionControllerEvidence.fixture_id.is_(None)))
    else:
        historic = 0
    return rows, historic


def replay(results, active):
    boards = defaultdict(dict)
    events = []
    exclusions = Counter()
    encounters = defaultdict(list)
    candidates = sorted(results, key=lambda row: (row.recorded_at or datetime.min, row.source, row.instance, row.fixture))
    seen = set()
    for row in candidates:
        identity = row.source, row.instance, row.fixture
        if identity in seen:
            continue
        seen.add(identity)
        a, b = row.controllers
        reason = row.eligibility
        if reason == "verified" and (a == b or a not in active or b not in active):
            reason = "account_ineligible"
        if reason == "verified" and (not row.recorded_at or row.format not in sources.FORMATS):
            reason = "unqualified_format"
        if reason != "verified":
            exclusions[reason] += 1
            continue
        if row.winner is not None and row.winner not in row.sides:
            exclusions["invalid_winner"] += 1
            continue
        score = .5 if row.winner is None else float(row.winner == row.sides[0])
        pair = tuple(sorted((a, b)))
        recent = encounters[pair]
        recent[:] = [stamp for stamp in recent if row.recorded_at - stamp < timedelta(days=30)]
        recent.append(row.recorded_at)
        flags = []
        if len(recent) >= 6:
            flags.append("frequent_opponents")
        if len(row.innings) == 2 and all(part["balls"] is not None for part in row.innings):
            if sum(part["balls"] for part in row.innings) < 24:
                flags.append("short_match")
        exclusions["flagged"] += bool(flags)
        for scope in (row.source, "overall"):
            for fmt in ("all", row.format):
                for season in (0, row.recorded_at.year):
                    board = boards[scope, fmt, season]
                    x, y = [board.setdefault(key, {"rating": START, "matches": 0, "wins": 0,
                                                    "draws": 0, "opponents": set()}) for key in (a, b)]
                    k = 32 if min(x["matches"], y["matches"]) < PROVISIONAL else 24
                    before = x["rating"], y["rating"]
                    delta, expected = change(*before, score, k)
                    x["rating"] += delta
                    y["rating"] -= delta
                    for value, opponent, won in ((x, b, score == 1), (y, a, score == 0)):
                        value["matches"] += 1
                        value["wins"] += won
                        value["draws"] += score == .5
                        value["opponents"].add(opponent)
                    events.append({"scope": scope, "format": fmt, "season": season, "source": row.source, "instance_id": row.instance,
                                   "fixture_id": row.fixture, "recorded_at": row.recorded_at,
                                   "detail": {"version": POLICY, "controllers": [a, b],
                                              "before": before, "after": [x["rating"], y["rating"]],
                                              "k": k, "expected": expected, "flags": flags,
                                              "competition_id": row.instance}})
    return boards, events, dict(exclusions)


def rebuild(db, now, *, dry_run=False, force=False):
    if not dry_run:
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(76460261)"))
        elif dialect == "sqlite":
            db.execute(update(CompetitionProjection).where(CompetitionProjection.id == 1).values(id=1))
    state = db.get(CompetitionProjection, 1, populate_existing=True)
    source_modes = [source for source in sources.SCOPES if sources.enabled(source)]
    if (state and state.counts.get("sources") == source_modes and not force and not dry_run
            and state.computed_at and now - state.computed_at < REFRESH):
        return state.counts
    results, historic = source_results(db)
    user_ids = {key for row in results for key in row.controllers if key}
    active = set(db.scalars(select(User.id).where(User.id.in_(user_ids), User.is_banned.is_(False),
                                                  User.needs_debut.is_(False)))) if user_ids else set()
    signature = [[row.source, row.fixture, row.instance, row.revision, row.status, row.winner,
                  row.controllers, row.eligibility, row.format, row.innings, str(row.recorded_at)]
                 for row in sorted(results, key=lambda item: (item.source, item.instance, item.fixture))]
    hashed = sha256(json.dumps([POLICY, source_modes, sorted(active), historic, signature], sort_keys=True).encode()).hexdigest()
    if state and state.fingerprint == hashed and not dry_run:
        state.computed_at = now
        return state.counts
    boards, events, counts = replay(results, active)
    counts.update(scanned=len(results), historic_unverified=historic, sources=source_modes,
                  verified=sum(event["scope"] == "overall" and event["format"] == "all" and event["season"] == 0
                               for event in events))
    if dry_run:
        return counts
    db.execute(delete(CompetitionRating))
    projection = []
    for (scope, fmt, season), board in boards.items():
        for uid, record in board.items():
            projection.append({"scope": scope, "format": fmt, "season": season, "user_id": uid, **record,
                               "opponents": len(record["opponents"])})
    if projection:
        db.execute(insert(CompetitionRating), projection)
    exists = db.scalar(select(CompetitionRatingEvent.fixture_id).where(
                       CompetitionRatingEvent.revision == hashed).limit(1))
    if events and exists is None:
        for offset in range(0, len(events), 1000):
            db.execute(insert(CompetitionRatingEvent), [{**event, "revision": hashed}
                       for event in events[offset:offset + 1000]])
    if state is None:
        state = CompetitionProjection(id=1)
        db.add(state)
    state.fingerprint, state.computed_at, state.counts, state.reason = hashed, now, counts, None
    return counts


def page(db, user, *, scope="overall", fmt="all", season=0, index=0):
    if scope not in ("overall", "solo", *sources.SCOPES) or fmt not in ("all", *sources.FORMATS):
        raise ValueError("Choose a supported ranking board.")
    if type(season) is not int or not (season == 0 or 2000 <= season <= 2100):
        raise ValueError("Choose an eligible UTC season.")
    if type(index) is not int or not 0 <= index <= 100:
        raise ValueError("Choose a valid rankings page.")
    if scope == "solo" and sources.enabled(sources.LIVE) and sources.flag("RANKINGS"):
        from services.event_studio_manager import profile
        summary = profile(db, user)
        return {"available": True, "rows": [], "own": None, "solo": [
            item for item in summary["modes"] if item["mode"] == "solo" and fmt in ("all", item["format"])]}
    state = db.get(CompetitionProjection, 1)
    source_modes = [source for source in sources.SCOPES if sources.enabled(source)]
    available = bool(source_modes) and sources.flag("RANKINGS") and (scope == "overall" or scope in source_modes)
    if not available or not state or state.counts.get("sources") != source_modes:
        return {"rows": [], "available": available, "own": None}
    common = (CompetitionRating.scope == scope, CompetitionRating.format == fmt, CompetitionRating.season == season)
    eligibility = (*common, CompetitionRating.matches >= PROVISIONAL,
                   CompetitionRating.opponents >= MIN_OPPONENTS, User.is_banned.is_(False))
    position = func.rank().over(order_by=CompetitionRating.rating.desc()).label("position")
    relation = select(CompetitionRating, User.first_name, User.username, position).join(User)
    results = db.execute(relation.where(*eligibility).order_by(
        CompetitionRating.rating.desc(), CompetitionRating.user_id).offset(index * 25).limit(26)).all()
    own = db.get(CompetitionRating, (scope, fmt, season, user.id))

    def display(row):
        return {"rating": row.rating, "matches": row.matches, "opponents": row.opponents, "wins": row.wins,
                "draws": row.draws, "provisional": row.matches < PROVISIONAL}

    rankings = []
    for row, first, alias, rank in results[:25]:
        rankings.append({**display(row), "rank": rank, "name": first or alias or "Manager", "own": row.user_id == user.id})
    own_view = display(own) if own else None
    if own and own.matches >= PROVISIONAL and own.opponents >= MIN_OPPONENTS:
        own_view["rank"] = db.scalar(select(func.count()).select_from(CompetitionRating).join(User).where(
                                      *eligibility, CompetitionRating.rating > own.rating)) + 1
    history = []
    if own:
        rating = CompetitionRatingEvent
        query = select(rating).where(rating.revision == state.fingerprint, rating.scope == scope,
                rating.format == fmt, rating.season == season,
                (rating.detail["controllers"][0].as_integer() == user.id) |
                (rating.detail["controllers"][1].as_integer() == user.id)).order_by(
                    rating.recorded_at.desc(), rating.source, rating.fixture_id.desc()).limit(10)
        for row in db.scalars(query):
            side = row.detail["controllers"].index(user.id)
            history.append({"source": row.source, "date": sources.utc_text(row.recorded_at),
                            "before": row.detail["before"][side], "after": row.detail["after"][side]})
    return {"available": True, "rows": rankings, "own": own_view, "history": history,
            "more": len(results) > 25, "calculated_at": sources.utc_text(state.computed_at) if state.computed_at else None,
            "explanation": "Public ranks require 10 verified matches and 3 distinct opponents. AI matches are excluded."}


def sweep(now=None):
    if not sources.enabled() or not sources.flag("RANKINGS"):
        return
    try:
        with get_session() as db:
            rebuild(db, now or datetime.utcnow())
            db.commit()
    except Exception:
        logger.exception("Competition Elo projection could not refresh")
