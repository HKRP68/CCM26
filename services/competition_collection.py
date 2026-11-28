"""Read official honours into a cosmetic collection. This module cannot pay rewards."""

from collections import Counter
from datetime import datetime

from sqlalchemy import and_, exists, extract, func, select, text, update
from sqlalchemy.orm import defer

from database import get_session
from models import Match, StatsSeason, Tournament, TournamentHonour, TournamentMatch, TournamentPrizePayout, TournamentTeam, User
from models_events import StudioCompetition, StudioContribution, StudioEvent, StudioFixtureResult, StudioHonourSnapshot, StudioParticipant, StudioRevision
from models_universe import (CompetitionCatalogue, CompetitionCatalogueLink, CompetitionCollectible,
                             CompetitionControllerEvidence, CompetitionProjection)
from services import competition_sources as sources

CAP = 15_000
VERSION = 1


def _live(db):
    query = (select(StudioParticipant.id, StudioParticipant.user_id, StudioParticipant.team_key,
                    StudioCompetition.id, StudioCompetition.kind, StudioCompetition.revision,
                    StudioEvent.id, StudioEvent.name, StudioHonourSnapshot, StudioRevision.config)
             .join(StudioCompetition, StudioCompetition.id == StudioParticipant.competition_id)
             .join(StudioEvent, StudioEvent.id == StudioCompetition.event_id)
             .join(StudioRevision, and_(StudioRevision.event_id == StudioCompetition.event_id,
                                       StudioRevision.number == StudioCompetition.revision))
             .join(StudioHonourSnapshot, and_(StudioHonourSnapshot.competition_id == StudioCompetition.id,
                                             StudioHonourSnapshot.version == 1))
             .where(StudioParticipant.user_id.is_not(None),
                    exists(select(StudioContribution.participant_id).where(
                           StudioContribution.participant_id == StudioParticipant.id))).limit(CAP + 1))
    output = {}
    rows = db.execute(query).all()
    if len(rows) > CAP:
        raise ValueError("Collection scan exceeded the safe rebuild size.")
    for _, user, team, room, kind, rev, event_id, title, snapshot, config in rows:
        data = snapshot.detail
        award = ("champion" if data.get("champion") == team else
                 "runner_up" if data.get("runner_up_key") == team else None)
        if award:
            label = data.get("champion_name" if award == "champion" else "runner_up")
            key = (sources.LIVE, room, user, award, f"snapshot:{snapshot.version}:{team}")
            output[key] = (event_id, {"name": title, "team": label, "mode": kind, "role": "verified manager",
                                    "event_id": event_id, "revision": rev, "format": config["format"]}, snapshot.created_at)
    return output

def _live_participation(db):
    earliest = (select(StudioContribution.participant_id.label("participant"),
                       func.min(StudioFixtureResult.recorded_at).label("played"))
                .join(StudioFixtureResult, StudioFixtureResult.fixture_id == StudioContribution.fixture_id)
                .where(StudioFixtureResult.kind == "completed").group_by(StudioContribution.participant_id).subquery())
    rows = db.execute(select(StudioParticipant, StudioCompetition, StudioEvent.name, StudioRevision.config, earliest.c.played)
        .join(earliest, earliest.c.participant == StudioParticipant.id)
        .join(StudioCompetition, StudioCompetition.id == StudioParticipant.competition_id)
        .join(StudioEvent, StudioEvent.id == StudioCompetition.event_id)
        .join(StudioRevision, and_(StudioRevision.event_id == StudioEvent.id,
                                  StudioRevision.number == StudioCompetition.revision))
        .where(StudioParticipant.user_id.is_not(None), StudioCompetition.status == "completed").limit(CAP + 1)).all()
    if len(rows) > CAP:
        raise ValueError("Collection participation scan exceeded the safe rebuild size.")
    result = {}
    for member, room, title, config, date in rows:
        key = sources.LIVE, room.id, member.user_id, "participation", "official-match:v1"
        result[key] = (room.event_id, {"name": title, "event_id": room.event_id, "mode": room.kind,
                        "format": config["format"], "role": "verified manager",
                        "team": config["snapshot"][member.team_key]["name"]}, date)
    return result

def _challenge(db):
    query = (select(TournamentHonour, Tournament.league_id, Tournament.overs)
             .join(Tournament, Tournament.id == TournamentHonour.tournament_id)
             .where((Tournament.kind == sources.CHALLENGE) | Tournament.kind.is_(None),
                    Tournament.status.in_(sources.CL_VISIBLE),
                    TournamentHonour.award.in_(("champion", "runner_up"))).limit(CAP + 1))
    honours = db.execute(query).all()
    if len(honours) > CAP:
        raise ValueError("Collection scan exceeded the safe rebuild size.")
    recipients = {}
    if honours:
        payouts = db.execute(select(TournamentPrizePayout.honour_id, TournamentPrizePayout.revision,
                                    User.id).join(User, User.telegram_id == TournamentPrizePayout.recipient_tg_id)
                             .where(TournamentPrizePayout.honour_id.in_([item[0].id for item in honours]),
                                    TournamentPrizePayout.reversed_at.is_(None),
                                    TournamentPrizePayout.status.in_(("pending", "paid"))))
        for honour, revision, user in payouts:
            recipients.setdefault((honour, revision), set()).add(user)
    output = {}
    chosen = set()
    for honour, family, overs in sorted(honours, reverse=True, key=lambda entry: (entry[0].prize_revision or 0,
                                                                                entry[0].awarded_at, entry[0].id)):
        users = recipients.get((honour.id, honour.prize_revision), set())
        if honour.user_id:
            users = {*users, honour.user_id}
        for user in users:
            identity = honour.tournament_id, user, honour.award
            if identity in chosen:
                continue
            chosen.add(identity)
            key = sources.CHALLENGE, honour.tournament_id, user, honour.award, f"honour:{honour.id}"
            role = "recorded honour recipient" if user == honour.user_id else "locked prize-share recipient"
            output[key] = (family, {"name": honour.tournament_name, "team": honour.team_name,
                                   "mode": sources.CHALLENGE, "role": role,
                                   "format": sources.format_of(overs)}, honour.awarded_at)
    return output


def _challenge_participation(db):
    query = (select(Tournament, TournamentMatch, Match, CompetitionControllerEvidence)
             .join(TournamentMatch, TournamentMatch.tournament_id == Tournament.id)
             .join(Match, Match.id == TournamentMatch.match_id)
             .join(CompetitionControllerEvidence, CompetitionControllerEvidence.fixture_id == TournamentMatch.id)
             .where(Tournament.status == "completed", TournamentMatch.status == "completed",
                    (Tournament.kind == sources.CHALLENGE) | Tournament.kind.is_(None),
                    CompetitionControllerEvidence.reason == "verified")
             .options(defer(TournamentMatch.scorecard_json))
             .order_by(TournamentMatch.completed_at, TournamentMatch.id).limit(CAP + 1))
    rows = db.execute(query).all()
    if len(rows) > CAP:
        raise ValueError("Challenge League participation scan exceeded the safe rebuild size.")
    sides = {side for _, fixture, _, _ in rows for side in (fixture.team1_id, fixture.team2_id) if side is not None}
    teams = dict(db.execute(select(TournamentTeam.id, TournamentTeam.name).where(TournamentTeam.id.in_(sides))).all()) if sides else {}
    output = {}
    for tour, fixture, match, evidence in rows:
        result = sources.challenge_result(tour, fixture, match, evidence)
        if result.eligibility != "verified" or not result.recorded_at:
            continue
        for user, team in zip(result.controllers, result.sides):
            if not user or team not in teams:
                continue
            key = (sources.CHALLENGE, tour.id, user, "participation", "verified-controller:v1")
            output.setdefault(key, (tour.league_id, {"name": tour.name, "team": teams[team], "mode": sources.CHALLENGE,
                "role": "verified match controller", "format": result.format, "fixture": result.fixture}, result.recorded_at))
    return output


def _archived(db, previous):
    rows = [row for row in previous.values() if row.source == sources.CHALLENGE and row.revoked_at is None]
    if not rows:
        return {}
    instances = {row.instance_id for row in rows}
    saved = dict(db.execute(select(StatsSeason.source_tournament_id, func.max(StatsSeason.id))
        .outerjoin(Tournament, Tournament.id == StatsSeason.source_tournament_id)
        .where(StatsSeason.source_tournament_id.in_(instances), Tournament.id.is_(None))
        .group_by(StatsSeason.source_tournament_id)).all())
    award_ids = [int(row.evidence.split(":", 1)[1]) for row in rows if row.instance_id in saved
                 and row.evidence.startswith("honour:")]
    honoured = {row.id: row for row in db.scalars(select(TournamentHonour).where(TournamentHonour.id.in_(award_ids),
                      TournamentHonour.tournament_id.is_(None)))} if award_ids else {}
    shares = {(award, revision, uid) for award, revision, uid in db.execute(
        select(TournamentPrizePayout.honour_id, TournamentPrizePayout.revision, User.id)
        .join(User, User.telegram_id == TournamentPrizePayout.recipient_tg_id)
        .where(TournamentPrizePayout.honour_id.in_(honoured),
               TournamentPrizePayout.reversed_at.is_(None),
               TournamentPrizePayout.status.in_(("pending", "paid"))))} if honoured else set()
    output = {}
    for row in rows:
        if row.instance_id not in saved:
            continue
        if row.kind != "participation":
            honour = honoured.get(int(row.evidence.split(":", 1)[1]))
            if honour is None or honour.award != row.kind or not (
                honour.user_id == row.user_id or (honour.id, honour.prize_revision, row.user_id) in shares
            ):
                continue
        key = row.source, row.instance_id, row.user_id, row.kind, row.evidence
        output[key] = (row.family_id, {**row.details, "archived": True}, row.earned_at)
    return output


def sync(db, now, *, dry_run=False):
    if not dry_run:
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(76460262)"))
        elif dialect == "sqlite":
            db.execute(update(CompetitionProjection).where(CompetitionProjection.id == 2).values(id=2))
    source_names = [source for source in sources.SCOPES if sources.enabled(source)]
    collected = {}
    for source in source_names:
        if source == sources.LIVE:
            collected.update(_live(db))
            collected.update(_live_participation(db))
        else:
            collected.update(_challenge(db))
            collected.update(_challenge_participation(db))
    if len(collected) > CAP:
        raise ValueError("Collection scan exceeded the safe rebuild size.")
    links = {(link.source, link.edition_id): link.family_id for link in db.scalars(
        select(CompetitionCatalogueLink).where(CompetitionCatalogueLink.source.in_(source_names)))}
    query = select(CompetitionCollectible).where(CompetitionCollectible.source.in_(source_names))
    prior = {(row.source, row.instance_id, row.user_id, row.kind, row.evidence): row
             for row in db.scalars(query)}
    if sources.CHALLENGE in source_names:
        collected.update(_archived(db, prior))
    add, retain, revoke, mapped = 0, 0, 0, 0
    for key, (family, data, earned) in collected.items():
        family = links.get((key[0], data.get("event_id", key[1])), family)
        old = prior.pop(key, None)
        if old and data.get("archived") and not old.details.get("archived") and not dry_run:
            old.details = data
        if old and old.family_id != family:
            mapped += 1
            if not dry_run:
                old.family_id = family
        if old and old.revoked_at is None:
            retain += 1
            continue
        add += 1
        if not dry_run:
            if old:
                old.revoked_at = None
            else:
                source, instance, user, kind, evidence = key
                db.add(CompetitionCollectible(source=source, instance_id=instance, user_id=user,
                    kind=kind, evidence=evidence, family_id=family, details={**data, "rule_version": VERSION},
                    earned_at=earned))
    for row in prior.values():
        if row.revoked_at is None:
            revoke += 1
            if not dry_run:
                row.revoked_at = now
    counts = {"new": add, "retained": retain, "revoked": revoke, "family_changes": mapped}
    if not dry_run:
        state = db.get(CompetitionProjection, 2)
        if state is None:
            state = CompetitionProjection(id=2)
            db.add(state)
        state.computed_at, state.counts = now, counts
    return counts


def page(db, user, index=0, *, source=None, mode=None, fmt=None, season=None):
    if type(index) is not int or not 0 <= index <= 100:
        raise ValueError("Choose a valid Collection Book page.")
    if source is not None and source not in sources.SCOPES:
        raise ValueError("Choose a collection source.")
    if mode is not None and mode not in ("solo", "multi", sources.CHALLENGE):
        raise ValueError("Choose a collection mode.")
    if fmt is not None and fmt not in sources.FORMATS:
        raise ValueError("Choose a collection format.")
    if season is not None and (type(season) is not int or not 2000 <= season <= 2100):
        raise ValueError("Choose a UTC season.")
    if not sources.flag("COLLECTION"):
        return {"available": False, "rows": [], "sets": []}
    active = [name for name in sources.SCOPES if sources.enabled(name) and (source is None or source == name)]
    query = select(CompetitionCollectible).where(CompetitionCollectible.user_id == user.id,
                                                 CompetitionCollectible.source.in_(active))
    counts = Counter((row.source, row.family_id) for row in db.scalars(query.where(
                    CompetitionCollectible.kind == "champion", CompetitionCollectible.revoked_at.is_(None))))
    catalog = db.scalars(select(CompetitionCatalogue).where(CompetitionCatalogue.source.in_(active))
                         .order_by(CompetitionCatalogue.category, CompetitionCatalogue.label).limit(100))
    shelf = [{"label": slot.label, "category": slot.category, "version": slot.version,
              "earned": counts[slot.source, slot.family_id], "retired": slot.retired_at is not None} for slot in catalog]
    if mode is not None:
        query = query.where(CompetitionCollectible.details["mode"].as_string() == mode)
    if fmt is not None:
        query = query.where(CompetitionCollectible.details["format"].as_string() == fmt)
    if season is not None:
        query = query.where(extract("year", CompetitionCollectible.earned_at) == season)
    rows = db.scalars(query.order_by(CompetitionCollectible.earned_at.desc(), CompetitionCollectible.source,
                            CompetitionCollectible.instance_id, CompetitionCollectible.kind)
                      .offset(index * 25).limit(26)).all()
    active_slots = {(item.source, item.family_id) for item in db.scalars(
        select(CompetitionCatalogue).where(CompetitionCatalogue.source.in_(active),
                                           CompetitionCatalogue.retired_at.is_(None)))}
    titles = sum(counts.values())
    progress = {"titles": titles, "families": sum(bool(counts[key]) for key in active_slots),
                "available_families": len(active_slots)}
    return {"available": True, "progress": progress, "rows": [{"source": row.source, "instance": row.instance_id, "kind": row.kind,
             "earned_at": sources.utc_text(row.earned_at), "revoked": row.revoked_at is not None, **row.details}
            for row in rows[:25]], "sets": shelf, "more": len(rows) > 25}


def sweep(now=None):
    if not sources.enabled() or not sources.flag("COLLECTION"):
        return
    from datetime import timedelta
    import logging
    now = now or datetime.utcnow()
    try:
        with get_session() as db:
            state = db.get(CompetitionProjection, 2)
            if state and state.computed_at and now - state.computed_at < timedelta(minutes=5):
                return
            sync(db, now)
            db.commit()
    except Exception:
        logging.getLogger(__name__).exception("Competition collection projection could not refresh")
