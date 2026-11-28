"""Discovery and personal next actions over existing competition services."""

from collections import Counter, defaultdict

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import defer

from models import Tournament, TournamentMatch, TournamentTeam
from models_events import StudioCompetition, StudioEvent, StudioFixture, StudioParticipant, StudioRevision
from services import competition_sources as sources


PAGE_SIZE = 12
SCAN = 250
STATUSES = ("open", "joined", "history", "all")


def _live_cards(db, user, now):
    from services import event_studio as studio, event_studio_play as play

    memberships = db.execute(select(StudioCompetition, StudioParticipant, StudioEvent, StudioRevision)
        .join(StudioParticipant, StudioParticipant.competition_id == StudioCompetition.id)
        .join(StudioEvent, StudioEvent.id == StudioCompetition.event_id)
        .join(StudioRevision, and_(StudioRevision.event_id == StudioCompetition.event_id,
                                  StudioRevision.number == StudioCompetition.revision))
        .where(StudioParticipant.user_id == user.id).order_by(StudioParticipant.joined_at.desc()).limit(SCAN)).all()
    rooms = {room.id: (room, part, rev.config) for room, part, _, rev in memberships}
    by_room = defaultdict(list)
    if rooms:
        clauses = or_(*(and_(StudioFixture.competition_id == key,
                             or_(StudioFixture.home == part.team_key, StudioFixture.away == part.team_key))
                       for key, (_, part, _) in rooms.items()))
        query = select(StudioFixture).where(clauses, StudioFixture.status != "locked").options(
            defer(StudioFixture.state), defer(StudioFixture.seed)).order_by(StudioFixture.deadline, StudioFixture.id)
        for fixture in db.scalars(query):
            by_room[fixture.competition_id].append(fixture)

    cards = []
    for room, part, event, rev in memberships:
        cfg = rev.config
        team = cfg["snapshot"][part.team_key]["name"]
        personal = by_room[room.id]
        remaining = [row for row in personal if
                     (row.status == "scheduled" and row.deadline > now)
                     or (row.status == "in_progress" and (row.due_at or row.deadline) > now)]
        candidate = min(remaining, key=lambda row: (row.status != "in_progress", row.deadline, row.id), default=None)
        actions = None
        if candidate and room.status == "running" and event.status in studio.OPEN_STATUSES:
            actions = {"fixture": candidate.id, "opens": sources.utc_text(candidate.opens_at),
                       "deadline": sources.utc_text(candidate.deadline),
                       "grace": sources.utc_text(candidate.due_at) if candidate.started_at else None,
                       "status": candidate.status, "in_grace": candidate.deadline <= now}
        cards.append({"source": sources.LIVE, "instance": room.id, "type": "room", "event_id": event.id,
                      "name": event.name, "format": cfg["format"], "status": room.status,
                      "mode": room.kind, "access": "participants", "mine": True, "teams": [team],
                      "started": sources.utc_text(room.started_at) if room.started_at else None,
                      "deadline": sources.utc_text(room.join_until) if room.status == "lobby" else None,
                      "end": sources.utc_text(event.closes_at), "next": actions,
                      "played": sum(row.status == "completed" for row in personal),
                      "total": len(personal), "stars": part.claimed_stars,
                      "capabilities": {"analytics": sources.flag("ANALYTICS") and room.status != "lobby",
                                       "objectives": True, "rewards": True},
                      "action": "Open lobby" if room.status == "lobby" else "Open event"})
    joined = {part.event_id for _, part, _, _ in memberships}
    template_query = select(StudioEvent, StudioRevision).join(StudioRevision, and_(
        StudioRevision.event_id == StudioEvent.id, StudioRevision.number == StudioEvent.published_number))
    template_query = template_query.where(StudioEvent.status.in_(studio.OPEN_STATUSES),
            StudioEvent.closes_at > now, StudioEvent.id.not_in(joined),
            StudioRevision.config["kind"].as_string() == "live").order_by(StudioEvent.opens_at, StudioEvent.id).limit(SCAN)
    for event, rev in db.execute(template_query):
        cfg = rev.config
        summary = play.event_summary(event, cfg, False, now)
        if not studio.allowed("live", cfg["format"]):
            continue
        cards.append({"source": sources.LIVE, "instance": event.id, "type": "template", "event_id": event.id,
                      "name": event.name, "format": cfg["format"], "mode": "Live Events", "status": summary["availability"],
                      "mine": False, "access": "open registration", "teams": [],
                      "started": summary["opens_at"], "end": summary["closes_at"],
                      "enrollment_deadline": sources.utc_text(play._cutoff(event, cfg)), "next": None,
                      "capabilities": {"join": summary["can_join"]}, "action": "View event"})
    return cards


def _challenge_cards(db, user, now):
    from services import league_schedule_service as schedule, tournament_service as tournament
    tours = db.scalars(select(Tournament).where(
        Tournament.status.in_(sources.CL_VISIBLE),
        or_(Tournament.kind == sources.CHALLENGE, Tournament.kind.is_(None))).order_by(
        Tournament.is_active.desc(), Tournament.id.desc()).limit(SCAN)).all()
    if not tours:
        return []
    ids = [row.id for row in tours]
    mine = defaultdict(dict)
    names = {}
    for team in db.scalars(select(TournamentTeam).where(TournamentTeam.tournament_id.in_(ids))):
        names[team.id] = team.name
        if tournament.is_team_member(team, user.telegram_id):
            mine[team.tournament_id][team.id] = team.name
    fixtures = defaultdict(list)
    query = select(TournamentMatch).where(TournamentMatch.tournament_id.in_(ids)).options(
        defer(TournamentMatch.scorecard_json)).order_by(TournamentMatch.round_no, TournamentMatch.match_no, TournamentMatch.id)
    for row in db.scalars(query):
        fixtures[row.tournament_id].append(row)
    cards = []
    for tour in tours:
        upcoming = None
        scoped = fixtures[tour.id]
        owned = mine[tour.id]
        phase = None
        if owned and tour.status == "active":
            opened, _, _ = schedule.split_open_round(db, tour.id, scoped)
            candidate = next((fx for fx in opened if fx.status in ("scheduled", "live") and fx.team1_id and fx.team2_id
                              and (fx.team1_id in owned or fx.team2_id in owned)), None)
            if candidate:
                phase = tour.round_deadline_at if candidate.stage in schedule.LEAGUE_STAGES else None
                upcoming = {"fixture": candidate.id, "opens": None, "status": candidate.status,
                            "deadline": sources.utc_text(phase) if phase and phase > now else None,
                            "grace": None, "sides": [names.get(candidate.team1_id), names.get(candidate.team2_id)]}
        counts = Counter(fx.status for fx in scoped)
        cards.append({"source": sources.CHALLENGE, "instance": tour.id, "type": "season",
                      "name": tour.name, "status": tour.status, "mode": "Challenge League",
                      "format": sources.format_of(tour.overs), "access": "managed team" if owned else "spectator",
                      "mine": bool(owned), "teams": list(owned.values()), "played": counts["completed"],
                      "total": len(scoped), "next": upcoming, "capabilities": {"analytics": sources.flag("ANALYTICS")},
                      "action": "View tournament"})
    return cards


def directory(db, user, now, *, source="all", status="all", fmt=None, page=0):
    if not sources.enabled() or user.is_banned:
        raise ValueError("Competition Universe is unavailable.")
    if source not in ("all", *sources.SCOPES) or status not in STATUSES:
        raise ValueError("Choose an available competition filter.")
    if type(page) is not int or not 0 <= page <= 80:
        raise ValueError("Choose a valid competition page.")
    if fmt and (not isinstance(fmt, str) or len(fmt) > 20):
        raise ValueError("Choose a valid match format.")
    items = []
    if source in ("all", sources.LIVE) and sources.enabled(sources.LIVE):
        items.extend(_live_cards(db, user, now))
    if source in ("all", sources.CHALLENGE) and sources.enabled(sources.CHALLENGE):
        items.extend(_challenge_cards(db, user, now))
    items = [row for row in items if not fmt or row["format"] == fmt]
    all_next = sorted((row for row in items if row["next"]), key=lambda row: (
        row["next"]["deadline"] is None,
        (row["next"]["grace"] if row["next"].get("in_grace") else row["next"]["deadline"]) or "",
        row["source"], row["next"]["fixture"]))
    history = {"completed", "cancelled", "archived", "closed"}
    if status == "open":
        items = [row for row in items if row["status"] not in history and not row["mine"]]
    elif status == "joined":
        items = [row for row in items if row["mine"] and row["status"] not in history]
    elif status == "history":
        items = [row for row in items if row["status"] in history]
    items.sort(key=lambda row: (row["status"] in history, not row["mine"],
                               (row["next"] or {}).get("deadline") or "~", -row["instance"], row["source"]))
    start = page * PAGE_SIZE
    return {"items": items[start:start+PAGE_SIZE], "next": all_next[:8], "more": len(items) > start + PAGE_SIZE,
            "personal": _personal(db, user) if page == 0 and source == "all" and status == "all" else {},
            "capabilities": {key: sources.flag(key.upper()) for key in ("rankings", "analytics", "collection")}}


def _personal(db, user):
    from sqlalchemy import func
    from models_universe import CompetitionCollectible, CompetitionProjection, CompetitionRating

    scopes = [source for source in sources.SCOPES if sources.enabled(source)]
    personal = {}
    if sources.flag("RANKINGS"):
        state = db.get(CompetitionProjection, 1)
        if state and state.counts.get("sources") == scopes:
            rating = db.get(CompetitionRating, ("overall", "all", 0, user.id))
            if rating:
                personal["pvp_rating"] = rating.rating
    if sources.flag("COLLECTION"):
        personal["titles"] = db.scalar(select(func.count(CompetitionCollectible.evidence))
            .where(CompetitionCollectible.user_id == user.id, CompetitionCollectible.source.in_(scopes),
                   CompetitionCollectible.kind == "champion", CompetitionCollectible.revoked_at.is_(None)))
    return personal
