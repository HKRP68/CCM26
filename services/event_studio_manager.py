"""Manager XP and optional zero-sum ratings, independent of Community XP."""

from collections import defaultdict

from sqlalchemy import func, or_, select

from models import User
from models_events import (StudioCompetition, StudioContribution, StudioEvent, StudioFixture, StudioFixtureResult, StudioHonourSnapshot,
    StudioManagerEntry, StudioParticipant, StudioRevision)
from services.economy_transactions import lock_user_for_update


POLICY_VERSION = 1
XP_WEIGHTS = {"match": 10, "win": 5, "star": 2, "completion": 25, "title": 50}


def xp_weight(config, category):
    return config["competition"].get("manager_xp_weights", {}).get(category, XP_WEIGHTS[category])

LEVELS = ((0, "Rookie"), (100, "Amateur"), (400, "Professional"), (1000, "Expert"),
          (2500, "Elite"), (5000, "Master"), (10000, "Legendary"), (20000, "Immortal"))


def rating_change(first, second, score, k):
    expected = 1 / (1 + 10 ** min(100, max(-100, (second - first) / 400)))
    return round(k * (score - expected)), expected


def latest(db, user_id, match_format, band=None):
    query = select(StudioManagerEntry.rating).where(
        StudioManagerEntry.user_id == user_id,
        StudioManagerEntry.rating["format"].as_string() == match_format)
    if band is not None:
        query = query.where(StudioManagerEntry.rating["band"].as_integer() == band)
    value = db.scalars(query.order_by(StudioManagerEntry.id.desc()).limit(1)).first()
    return (value["after"], value["games"]) if value else (1500, 0)


def add(db, entrant, config, source, raw_xp, now, *, rating=None):
    if not config["competition"].get("manager_profile"):
        return
    db.flush()
    if db.scalar(select(StudioManagerEntry.id).filter_by(participant_id=entrant.id, source=source)):
        return
    event_total = db.scalar(select(func.coalesce(func.sum(StudioManagerEntry.xp), 0))
                            .filter_by(participant_id=entrant.id))
    remaining = max(0, config["competition"]["manager_xp_cap"] - event_total)
    db.add(StudioManagerEntry(participant_id=entrant.id, user_id=entrant.user_id, source=source,
                              xp=min(raw_xp, remaining), rating=rating, created_at=now))


def settle(db, config, room, fixture, managers, winner, now):
    settings = config["competition"]
    if not settings.get("manager_profile") or not managers:
        return
    source = f"fixture:{fixture.id}"
    ratings = {}
    bands = {team: int(sum(card["rating"] for card in config["snapshot"][team]["default_xi"])
                       / len(config["snapshot"][team]["default_xi"]) // 10) * 10 for team in managers}
    if room.kind == "multi" and settings["competitive_rating"] and len(managers) == 2 and len(set(bands.values())) == 1:
        first, second = sorted(managers.values(), key=lambda p: p.user_id)
        accounts = [lock_user_for_update(db, db.get(User, player.user_id)) for player in (first, second)]
        # Only official games between currently eligible accounts can affect rating.
        if not any(account.is_banned for account in accounts):
            fmt = config["format"]
            band = bands[first.team_key]
            a, a_games = latest(db, first.user_id, fmt, band)
            b, b_games = latest(db, second.user_id, fmt, band)
            score = .5 if winner is None else int(winner == first.team_key)
            delta, expected = rating_change(a, b, score, settings["rating_k"])
            ratings[first.id] = {"before": a, "after": a + delta, "games": a_games + 1, "format": fmt, "band": band,
                                 "expected": expected, "version": POLICY_VERSION}
            ratings[second.id] = {"before": b, "after": b - delta, "games": b_games + 1, "format": fmt, "band": band,
                                  "expected": 1 - expected, "version": POLICY_VERSION}
    for team, player in managers.items():
        add(db, player, config, source, xp_weight(config, "match") + xp_weight(config, "win") * (winner == team),
            now, rating=ratings.get(player.id))


def profile(db, user):
    rows = db.execute(select(StudioManagerEntry, StudioCompetition.kind, StudioRevision.config)
                      .join(StudioParticipant, StudioParticipant.id == StudioManagerEntry.participant_id)
                      .join(StudioCompetition, StudioCompetition.id == StudioParticipant.competition_id)
                      .join(StudioRevision, (StudioRevision.event_id == StudioCompetition.event_id) &
                            (StudioRevision.number == StudioCompetition.revision))
                      .where(StudioManagerEntry.user_id == user.id)).all()
    xp = sum(entry.xp for entry, _, _ in rows)
    modes = defaultdict(lambda: {"xp": 0, "entries": 0, "completed_matches": 0, "completed_events": 0,
                                 "wins": 0, "losses": 0, "ties": 0, "forfeits": 0,
                                 "playoff_matches": 0, "best_streak": 0, "stars": 0, "titles": 0})
    for entry, mode, config in rows:
        totals = modes[(mode, config["format"])]
        totals["xp"] += entry.xp
        totals["completed_events"] += entry.source in ("completion", "title")
        totals["titles"] += entry.source == "title"
    rank = max((level for level in LEVELS if level[0] <= xp))[1]
    after = next((level[0] for level in LEVELS if level[0] > xp), None)
    competitions = db.execute(select(StudioParticipant, StudioCompetition, StudioRevision.config)
                       .join(StudioCompetition, StudioCompetition.id == StudioParticipant.competition_id)
                       .join(StudioRevision, (StudioRevision.event_id == StudioCompetition.event_id) &
                             (StudioRevision.number == StudioCompetition.revision))
                       .where(StudioParticipant.user_id == user.id, StudioCompetition.started_at.is_not(None))).all()
    campaign = {}
    for entry, room, config in competitions:
        if not config["competition"].get("manager_profile"):
            continue
        totals = modes[(room.kind, config["format"])]
        totals["entries"] += 1
        totals["stars"] += entry.claimed_stars
        campaign[room.id] = (totals, {row["key"] for row in config["fixtures"] if row["stage"] == "playoff"})
    fixtures = db.execute(select(StudioFixture, StudioFixtureResult, StudioParticipant.team_key)
                         .join(StudioFixtureResult, StudioFixtureResult.fixture_id == StudioFixture.id)
                         .join(StudioParticipant, StudioParticipant.competition_id == StudioFixture.competition_id)
                         .where(StudioParticipant.user_id == user.id, StudioFixture.competition_id.in_(campaign),
                                or_(StudioParticipant.team_key == StudioFixture.home,
                                    StudioParticipant.team_key == StudioFixture.away))
                         .order_by(StudioFixtureResult.recorded_at, StudioFixture.id))
    streak = defaultdict(int)
    for fixture, result, team in fixtures:
        totals, playoff_keys = campaign[fixture.competition_id]
        if result.kind == "forfeited":
            totals["forfeits"] += result.detail.get("offender") == team
            continue
        if result.kind != "completed":
            continue
        totals["completed_matches"] += 1
        totals["playoff_matches"] += fixture.fixture_key in playoff_keys
        won = result.winner == team
        totals["wins" if won else "losses" if result.winner else "ties"] += 1
        streak[fixture.competition_id] = streak[fixture.competition_id] + 1 if won else 0
        totals["best_streak"] = max(totals["best_streak"], streak[fixture.competition_id])
    for totals in modes.values():
        completed = totals["completed_matches"]
        totals["win_rate"] = round(100 * totals["wins"] / completed, 1) if completed else None
    bands = {(entry.rating["format"], entry.rating["band"]) for entry, _, _ in rows if entry.rating}
    ratings = []
    for fmt, band in sorted(bands):
        value, games = latest(db, user.id, fmt, band)
        ratings.append({"format": fmt, "rating": value, "games": games, "provisional": games < 10,
                        "strength_band": f"{band}–{band + 9} OVR"})

    return {"xp": xp, "rank": rank, "next_xp": after, "ratings": ratings,
            "modes": [{"mode": mode, "format": fmt, **counts} for (mode, fmt), counts in sorted(modes.items())]}


def trophies(db, user, limit=25):
    rows = db.execute(select(StudioHonourSnapshot, StudioCompetition, StudioParticipant.team_key,
                             StudioEvent.name, StudioRevision.config)
                      .join(StudioCompetition, StudioCompetition.id == StudioHonourSnapshot.competition_id)
                      .join(StudioParticipant, StudioParticipant.competition_id == StudioCompetition.id)
                      .join(StudioEvent, StudioEvent.id == StudioCompetition.event_id)
                      .join(StudioRevision, (StudioRevision.event_id == StudioCompetition.event_id) &
                            (StudioRevision.number == StudioCompetition.revision))
                      .where(StudioParticipant.user_id == user.id,
                             StudioHonourSnapshot.detail["champion"].as_string() == StudioParticipant.team_key,
                             select(StudioContribution.participant_id).where(
                                 StudioContribution.participant_id == StudioParticipant.id).exists())
                      .order_by(StudioHonourSnapshot.created_at.desc(), StudioCompetition.id.desc()).limit(limit))
    return [{"event": name, "event_id": room.event_id, "mode": room.kind, "format": config["format"],
             "team": snapshot.detail["champion_name"]} for snapshot, room, _, name, config in rows]


def is_champion(db, user_id, event_id):
    return db.scalar(select(StudioHonourSnapshot.competition_id)
                     .join(StudioCompetition, StudioCompetition.id == StudioHonourSnapshot.competition_id)
                     .join(StudioParticipant, StudioParticipant.competition_id == StudioCompetition.id)
                     .join(StudioContribution, StudioContribution.participant_id == StudioParticipant.id)
                     .where(StudioParticipant.user_id == user_id, StudioCompetition.event_id == event_id,
                            StudioHonourSnapshot.detail["champion"].as_string() == StudioParticipant.team_key)
                     .limit(1)) is not None


def dynasty_history(db, user, entrant, config):
    """Summarize a manager's verified results for the same franchise in linked editions."""
    previous = config.get("previous_edition") if config["schedule"].get("template") == "dynasty" else None
    visited, history = set(), []
    while previous and len(visited) < 12:
        key = previous["id"], previous["revision"]
        if key in visited:
            break
        visited.add(key)
        revision = db.get(StudioRevision, key)
        if revision is None:
            break
        owner = db.scalar(select(StudioParticipant).filter_by(event_id=key[0], revision=key[1],
                          user_id=user.id, team_key=entrant.team_key))
        if owner:
            results = db.scalars(select(StudioFixtureResult)
                        .join(StudioContribution, StudioContribution.fixture_id == StudioFixtureResult.fixture_id)
                        .where(StudioContribution.participant_id == owner.id,
                               StudioFixtureResult.kind == "completed")).all()
            snapshot = db.get(StudioHonourSnapshot, (owner.competition_id, 1))
            wins = sum(result.winner == owner.team_key for result in results)
            history.append({"event": previous["name"], "event_id": key[0], "played": len(results),
                            "wins": wins, "title": bool(results and snapshot and snapshot.detail["champion"] == owner.team_key)})
        previous = (revision.config.get("previous_edition")
                    if revision.config["schedule"].get("template") == "dynasty" else None)
    return history
