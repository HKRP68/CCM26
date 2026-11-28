"""Canonical Live Event tournaments and their registration, fixtures and claims."""

from copy import deepcopy
from datetime import timedelta
from secrets import token_hex, randbelow
import os
from types import SimpleNamespace

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import defer

from models import User
from models_events import (
    StudioAudit, StudioCompetition, StudioCompetitionNotice, StudioCompetitionReward, StudioCompetitionXP,
    StudioContribution, StudioEnrollment, StudioEvent, StudioFixture, StudioFixtureResult,
    StudioParticipant, StudioRevision, StudioStarClaim,
)
from services import cipl_picks as picks, event_studio as studio, event_studio_cricket as cricket
from services import event_studio_objectives as goals, event_studio_play as legacy
from services import event_studio_progress as progression, event_studio_rules as rules, event_studio_schedule as schedule
from services.activity_service import log_activity
from services.economy_transactions import lock_user_for_update
from services.match_state_store import A_INNINGS_BREAK
from services.subscription_service import get_tier

PENDING = ("locked", "scheduled", "in_progress", "simulating")
FIRST_FIXTURE_BUFFER = timedelta(minutes=30)


def mode_enabled(config, mode):
    if not goals.tournament(config) or mode not in config["competition"]["modes"]:
        return False
    return mode == "solo" or (studio.flag("MULTIPLAYER") and
                              (config["format"] != "ODI" or studio.flag("MULTIPLAYER_ODI")))


def _audit(db, event_id, actor, action, now, **detail):
    db.add(StudioAudit(event_id=event_id, actor=str(actor), action=action,
                       detail=detail, created_at=now))


def membership(db, event, user_id):
    return db.scalar(select(StudioParticipant).filter_by(event_id=event.id,
                     revision=event.published_number, user_id=user_id))


def _config(db, competition):
    return db.get(StudioRevision, (competition.event_id, competition.revision)).config


def _entries(db, competition):
    return db.scalars(select(StudioParticipant).filter_by(competition_id=competition.id)
                      .order_by(StudioParticipant.id)).all()


def _human_side(db, competition, user_id):
    return db.scalar(select(StudioParticipant).filter_by(competition_id=competition.id, user_id=user_id))


def _event(db, event_id, *, lock=False):
    event = studio.lock(db, StudioEvent, event_id) if lock else db.get(StudioEvent, event_id)
    if not event or not event.published_number:
        raise ValueError("Live Event is unavailable.")
    return event


def _registration_open(event, config, now):
    return (event.status in studio.OPEN_STATUSES and event.opens_at <= now < legacy._cutoff(event, config)
            and studio.allowed("live", config["format"]))


def _entrant(user):
    if not user or user.is_banned or user.needs_debut:
        raise ValueError("Complete /debut with an active account to join.")


def _reserve(db, event, config, competition, user, team, now):
    _entrant(user)
    if not _registration_open(event, config, now):
        raise ValueError("Enrollment is closed.")
    if team not in config["snapshot"] or (config["schedule"]["preset"] == "survival"
                                           and team != config["teams"][0]["key"]):
        raise ValueError("Choose an available franchise.")
    prior = membership(db, event, user.id)
    if prior:
        if prior.competition_id != competition.id:
            raise studio.Conflict("You already have an entry in this event.")
        if prior.team_key == team:
            return prior
    occupied = db.scalar(select(StudioParticipant.id).filter_by(competition_id=competition.id, team_key=team))
    if occupied:
        raise studio.Conflict("Another manager has claimed that franchise.")
    if prior:
        prior.team_key = team
        return prior
    cap = db.scalar(select(func.count(StudioParticipant.id)).where(
        StudioParticipant.event_id == event.id, StudioParticipant.revision == event.published_number,
        StudioParticipant.user_id.is_not(None)))
    if cap >= config["max_users"]:
        raise ValueError("This event has reached its participant capacity.")
    entry = StudioParticipant(competition_id=competition.id, event_id=event.id, revision=competition.revision,
                              user_id=user.id, team_key=team, joined_at=now)
    db.add(entry)
    db.flush()
    return entry


def create(db, user, event_id, mode, team, now, *, chat_id=None, clock=None):
    event = _event(db, event_id, lock=True)
    now = clock() if clock else now
    config = studio.revision(db, event, published=True).config
    _entrant(user)
    prior = membership(db, event, user.id)
    if prior:
        competition = db.get(StudioCompetition, prior.competition_id)
        if competition.kind == mode:
            return competition
        raise studio.Conflict("You already have an entry in this event.")
    if db.scalar(select(StudioEnrollment.id).filter_by(event_id=event.id, user_id=user.id)):
        raise studio.Conflict("You already have a legacy entry in this event.")
    if not mode_enabled(config, mode) or not _registration_open(event, config, now):
        raise ValueError("This competition mode is unavailable for registration.")
    until = now + timedelta(minutes=config["competition"]["registration_minutes"]) if mode == "multi" else now
    if mode == "multi" and until + FIRST_FIXTURE_BUFFER > legacy._cutoff(event, config):
        raise ValueError("There is not enough time for the full registration hour and first fixture.")
    competition = StudioCompetition(event_id=event.id, revision=event.published_number,
                                    kind=mode, host_id=user.id, chat_id=chat_id,
                                    invite=token_hex(12) if mode == "multi" else None,
                                    created_at=now, join_until=until)
    db.add(competition)
    db.flush()
    _reserve(db, event, config, competition, user, team, now)
    _audit(db, event.id, user.id, "competition_created", now, competition=competition.id, mode=mode, team=team)
    if mode == "solo":
        _start(db, competition, event, config, now)
    return competition


def _room(db, room_id, *, write=False):
    row = db.get(StudioCompetition, room_id)
    if not row:
        raise ValueError("Competition is unavailable.")
    event = _event(db, row.event_id, lock=write)
    if write:
        row = studio.lock(db, StudioCompetition, row.id)
    return row, event, _config(db, row)


def _room_access(db, row, user, *, invite=None, chat_id=None):
    _entrant(user)
    actor = _human_side(db, row, user.id)
    if actor or row.status == "lobby" and ((invite and invite == row.invite)
                                           or chat_id is not None and chat_id == row.chat_id):
        return actor
    raise ValueError("Use a valid lobby invitation or open the lobby in its hosting group.")


def _by_invite(db, invite):
    if not isinstance(invite, str) or not 10 <= len(invite) <= 32:
        raise ValueError("Enter a valid lobby code.")
    key = db.scalar(select(StudioCompetition.id).filter_by(invite=invite))
    if key is None:
        raise ValueError("Lobby invitation is unavailable.")
    return key


def lobby(db, user, room_id=None, *, invite=None, chat_id=None, now):
    row, event, config = _room(db, room_id if room_id is not None else _by_invite(db, invite))
    actor = _room_access(db, row, user, invite=invite, chat_id=chat_id)
    return _lobby_view(db, row, event, config, actor, user, now)


def _lobby_view(db, row, event, config, actor, user, now):
    entries = {p.team_key: p for p in _entries(db, row)}
    names = {user_id: name for user_id, name in db.execute(
        select(User.id, func.coalesce(User.first_name, User.username, "Manager"))
        .where(User.id.in_([p.user_id for p in entries.values() if p.user_id])))}
    invite = row.invite if user.id == row.host_id else None
    username = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    return {"id": row.id, "event_id": event.id, "name": event.name, "kind": row.kind, "status": row.status,
            "invite_url": f"https://t.me/{username}?start=eventlobby_{invite}" if invite and username else None,
            "version": row.version, "invite": invite,
            "join_until": schedule.utc_text(row.join_until), "timezone": config["timezone"],
            "is_host": row.host_id == user.id, "my_team": actor.team_key if actor else None,
            "can_join": row.status == "lobby" and now < row.join_until and _registration_open(event, config, now),
            "early_start": config["competition"]["early_start"], "minimum": config["competition"]["min_humans"],
            "ai_count": len(config["snapshot"]) - sum(bool(p.user_id) for p in entries.values()),
            "teams": [{"key": key, "name": team["name"], "owner": names.get(entries[key].user_id) if key in entries else None}
                      for key, team in config["snapshot"].items()]}


def change_lobby(db, user, room_id, action, now, *, team=None, invite=None, chat_id=None, version=None, clock=None):
    row, event, config = _room(db, room_id, write=True)
    now = clock() if clock else now
    actor = _room_access(db, row, user, invite=invite, chat_id=chat_id)
    if row.status != "lobby":
        if action == "claim" and actor or action == "start" and row.host_id == user.id and row.started_at:
            return _lobby_view(db, row, event, config, actor, user, now)
        raise studio.Conflict("Registration has ended.")
    if action != "cancel" and (now >= row.join_until or not _registration_open(event, config, now)):
        raise ValueError("Registration is no longer open.")
    if action not in ("claim", "release") and version != row.version:
        raise studio.Conflict("The lobby changed. Review its latest seats before confirming.")
    if action == "claim":
        if actor and actor.team_key == team:
            return _lobby_view(db, row, event, config, actor, user, now)
        actor = _reserve(db, event, config, row, user, team, now)
        if len(_entries(db, row)) == len(config["snapshot"]):
            _start(db, row, event, config, now)
    elif action == "release":
        if row.host_id == user.id:
            raise ValueError("The host can close the lobby before it starts.")
        if not actor:
            return _lobby_view(db, row, event, config, actor, user, now)
        db.delete(actor)
        db.flush()
        actor = None
    elif action in ("start", "cancel"):
        if row.host_id != user.id:
            raise ValueError("Only the lobby host can do that.")
        if action == "start":
            if not config["competition"]["early_start"]:
                raise ValueError("The lobby starts when registration closes.")
            _start(db, row, event, config, now)
        else:
            _cancel(db, row, now)
            actor = None
    else:
        raise ValueError("Unknown lobby action.")
    row.version += 1
    _audit(db, event.id, user.id, f"lobby_{action}", now, competition=row.id, team=team)
    return _lobby_view(db, row, event, config, actor, user, now)


def _cancel(db, row, now):
    row.status = "cancelled"
    row.closed_at = now
    row.version += 1
    db.execute(delete(StudioParticipant).where(StudioParticipant.competition_id == row.id))


def _start(db, row, event, config, now):
    if row.status != "lobby":
        return
    if event.status not in studio.OPEN_STATUSES or now >= legacy._cutoff(event, config):
        raise ValueError("The event has closed registration.")
    humans = _entries(db, row)
    minimum = config["competition"]["min_humans"] if row.kind == "multi" else 1
    if len(humans) < minimum or not any(p.user_id == row.host_id for p in humans):
        raise ValueError(f"At least {minimum} human franchise manager(s), including the host, must claim seats.")
    owned = {part.team_key for part in humans}
    for key in config["snapshot"].keys() - owned:
        db.add(StudioParticipant(competition_id=row.id, event_id=event.id, revision=row.revision,
                                 team_key=key, joined_at=now))
    for template in config["fixtures"]:
        home, away = template["home"], template["away"]
        locked = ":" in home or ":" in away
        shift = timedelta(seconds=event.delay_seconds)
        opens = schedule.utc_instant(template["opens_at"], config["timezone"]) + shift
        deadline = schedule.utc_instant(template["deadline"], config["timezone"]) + shift
        db.add(StudioFixture(competition_id=row.id, fixture_key=template["key"], opens_at=opens, deadline=deadline,
                             due_at=opens if locked or not ({home, away} & owned) else deadline,
                             home=None if locked else home, away=None if locked else away,
                             status="locked" if locked else "scheduled", seed=token_hex(32)))
    row.status = "running"
    row.started_at = now
    row.version += 1
    if row.kind == "multi" and config["notifications"]["enabled"]:
        db.add_all([StudioCompetitionNotice(competition_id=row.id, user_id=p.user_id, created_at=now) for p in humans])
    _audit(db, event.id, "system", "competition_started", now, competition=row.id, humans=len(humans))
    db.flush()


def expire_lobby(db, room_id, now, *, clock=None):
    row, event, config = _room(db, room_id, write=True)
    now = clock() if clock else now
    if row.status != "lobby" or now < row.join_until or event.status == "paused":
        return
    if (event.status not in studio.OPEN_STATUSES or now >= legacy._cutoff(event, config)
            or len(_entries(db, row)) < config["competition"]["min_humans"]):
        _cancel(db, row, now)
        _audit(db, event.id, "system", "registration_expired", now, competition=row.id)
    else:
        _start(db, row, event, config, now)


def save_lobby_message(db, room_id, chat_id, message_id):
    row = studio.lock(db, StudioCompetition, room_id)
    if row and row.chat_id == chat_id:
        row.message_id = message_id


def _rows(db, room):
    return db.execute(select(StudioFixture, StudioFixtureResult)
                      .options(defer(StudioFixture.state), defer(StudioFixture.seed))
                      .outerjoin(StudioFixtureResult, StudioFixtureResult.fixture_id == StudioFixture.id)
                      .where(StudioFixture.competition_id == room.id)
                      .order_by(StudioFixture.opens_at, StudioFixture.id)).all()


def _fixture(db, fixture_id, *, write=False):
    row = db.get(StudioFixture, fixture_id)
    if not row:
        raise ValueError("Fixture is unavailable.")
    room, event, config = _room(db, row.competition_id, write=write)
    if write:
        row = studio.lock(db, StudioFixture, fixture_id)
    return row, room, event, config


def _managers(db, room, row):
    return {p.team_key: p for p in _entries(db, room) if p.user_id is not None and p.team_key in (row.home, row.away)}


def _fixture_player(db, room, row, user):
    _entrant(user)
    actor = _human_side(db, room, user.id)
    if not actor or actor.team_key not in (row.home, row.away):
        raise ValueError("Only a captain in this fixture can play.")
    return actor


def _round_open(db, room, row, config):
    template = rules.fixture_rows(config)
    round_no = template[row.fixture_key]["round"]
    return all(result is not None for other, result in _rows(db, room) if template[other.fixture_key]["round"] < round_no)


def _window(event, row, now):
    return event.status in studio.OPEN_STATUSES and row.opens_at <= now < row.deadline


def team_cards(config, team_key):
    team = config["snapshot"][team_key]
    return {"key": team_key, "name": team["name"], "crest": team.get("logo_url"),
            "eligible": config["schedule"]["preset"] != "survival" or config["teams"][0]["key"] == team_key,
            "venue": team.get("home_venue") or next((r.get("venue") for r in config["fixtures"] if r["home"] == team_key), None),
            "ovr": round(sum(p["rating"] for p in team["default_xi"]) / len(team["default_xi"]), 1),
            "squad": [{key: p.get(key) for key in ("name", "version", "category", "rating", "roster_id")} for p in team["squad"]]}


def _setup_view(row, config, actor, managers):
    state = row.setup
    team = actor.team_key
    cards = team_cards(config, team)
    toss = state.get("toss")
    return {"id": row.id, "version": row.version, "status": row.status,
            "ready": team in state.get("ready", ()), "all_ready": set(managers) <= set(state.get("ready", ())),
            "can_call": state.get("caller") == team, "toss": toss, "won_toss": bool(toss and toss["winner"] == team),
            "confirmed_xi": state.get("xis", {}).get(team), "all_confirmed": set(managers) <= set(state.get("xis", {})),
            "team": cards, "fixed_xi": "xi" in config["snapshot"][team],
            "default_xi": [p["roster_id"] for p in config["snapshot"][team]["default_xi"]],
            "opponent": config["snapshot"][row.away if team == row.home else row.home]["name"],
            "manager_count": len(managers), "deadline": schedule.utc_text(row.deadline)}


def setup(db, user, fixture_id, now, *, action=None, version=None, value=None, source="app"):
    row, room, event, config = _fixture(db, fixture_id, write=action is not None)
    actor = _fixture_player(db, room, row, user)
    managers = _managers(db, room, row)
    if row.status == "in_progress" and action in (None, "start"):
        return _setup_view(row, config, actor, managers)
    if row.failure_at:
        raise studio.ExecutionUnavailable("The fixture is awaiting technical review.")
    if row.status != "scheduled" or not _window(event, row, now) or not _round_open(db, room, row, config):
        raise ValueError("This fixture is waiting for its opening window or earlier fixtures.")
    if not action:
        return _setup_view(row, config, actor, managers)
    if action != "ready" and (type(version) is not int or version != row.version):
        raise studio.Conflict("The fixture changed. Open its latest setup.")
    team = actor.team_key
    settings = deepcopy(row.setup)
    ready = settings.setdefault("ready", [])
    if action == "ready":
        if team not in ready:
            ready.append(team)
        settings["caller"] = row.home if row.home in managers else row.away
    elif set(managers) - set(ready):
        raise ValueError("All human captains must mark ready first.")
    elif action == "coin":
        if team != settings["caller"] or value not in ("heads", "tails"):
            raise ValueError("Only the calling captain can choose Heads or Tails.")
        if not settings.get("toss"):
            coin = ("heads", "tails")[randbelow(2)]
            other = row.away if team == row.home else row.home
            winner = team if coin == value else other
            from services.bot_captain import elect_toss_decision
            settings["toss"] = {"coin": coin, "winner": winner,
                                "decision": None if winner in managers else elect_toss_decision()}
    elif action == "elect":
        toss = settings.get("toss")
        if not toss or toss["winner"] != team or value not in ("bat", "bowl") or toss["decision"] not in (None, value):
            raise ValueError("Only the toss winner may choose the innings order once.")
        toss["decision"] = value
    elif action in ("xi", "edit"):
        if not settings.get("toss", {}).get("decision"):
            raise ValueError("Finish the toss before choosing the Playing XI.")
        xis = settings.setdefault("xis", {})
        if action == "edit":
            xis.pop(team, None)
        else:
            cards = config["snapshot"][team]
            legal_ids = {p["roster_id"]: p for p in cards["squad"]}
            if (not isinstance(value, list) or len(value) != 11
                    or any(type(pid) is not int or pid not in legal_ids for pid in value)):
                raise ValueError("Choose 11 cards from your published franchise squad.")
            if "xi" in cards and value != [p["roster_id"] for p in cards["default_xi"]]:
                raise ValueError("This franchise has a fixed Playing XI.")
            studio.validate_xi([legal_ids[pid] for pid in value], config)
            xis[team] = value
    elif action == "start":
        try:
            _launch(db, row, room, event, config, managers, now, source)
        except ValueError as exc:
            from engine.realism.odi import ODIUnavailable
            if not isinstance(exc, ODIUnavailable):
                raise
            note_failure(db, row.id, now, exc)
            raise studio.ExecutionUnavailable("The fixture is awaiting technical review.") from exc
        except RuntimeError as exc:
            note_failure(db, row.id, now, exc)
            raise studio.ExecutionUnavailable("The fixture is awaiting technical review.") from exc
        return _setup_view(row, config, actor, managers)
    else:
        raise ValueError("Unknown fixture setup action.")
    if settings != row.setup:
        row.setup = settings
        row.version += 1
    return _setup_view(row, config, actor, managers)


def _launch(db, row, room, event, config, managers, now, source):
    settings = row.setup
    toss = settings.get("toss") or {}
    if not toss.get("decision") or set(managers) - settings.get("xis", {}).keys():
        raise ValueError("All captains must confirm a legal Playing XI before starting.")
    users = {}
    for participant in sorted(managers.values(), key=lambda p: p.user_id):
        player = lock_user_for_update(db, db.get(User, participant.user_id))
        _entrant(player)
        users[participant.team_key] = player
    active = (select(StudioFixture.id).join(StudioParticipant, and_(
        StudioFixture.competition_id == StudioParticipant.competition_id,
        or_(StudioFixture.home == StudioParticipant.team_key, StudioFixture.away == StudioParticipant.team_key)))
        .where(StudioFixture.status == "in_progress", StudioParticipant.user_id.in_([p.id for p in users.values()])))
    if db.scalar(active.limit(1)):
        raise ValueError("A captain already has an active Event fixture.")
    effective = rules.fixture_rules(config, row.fixture_key)
    xis, benches = {}, {}
    for side in (row.home, row.away):
        team = config["snapshot"][side]
        ids = settings["xis"].get(side)
        if ids:
            by_id = {p["roster_id"]: p for p in team["squad"]}
            xis[side] = [by_id[pid] for pid in ids]
        else:
            xis[side] = legacy._opponent_xi(team, effective, "tournament_team")
        if effective.get("impact_player"):
            selected = {p["roster_id"] for p in xis[side]}
            benches[side] = [p for p in team["squad"] if p["roster_id"] not in selected]
    winner = toss["winner"]
    other = row.home if winner == row.away else row.away
    batting = winner if toss["decision"] == "bat" else other
    row.seed = row.seed or token_hex(32)
    human = next(iter(managers))
    state = cricket.build_state(effective, row.id, row.home, row.away, xis, row.seed, human=human,
                                batting=batting, toss_winner=winner, toss_decision=toss["decision"], benches=benches)
    if len(managers) == 2:
        state.update(is_bot_match=False, is_vsbot=False, bot_user_id=None, event_pvp=True)
    state.update(event_match_key=f"competition-{row.id}", event_human_teams=list(managers), play_mode=source,
                 is_private=True, user_names={str(p.telegram_id): p.first_name or p.username or "Captain" for p in users.values()},
                 event_captains={side: xi[0]["roster_id"] for side, xi in xis.items()})
    state["bat_user_tg"] = users[state["bat_team_id"]].telegram_id if state["bat_team_id"] in users else None
    state["bowl_user_tg"] = users[state["bowl_team_id"]].telegram_id if state["bowl_team_id"] in users else None
    cricket.prepare_pick(state, row.seed, row.step, human)
    row.state, row.started_at, row.status = state, now, "in_progress"
    row.due_at = min(event.closes_at, row.deadline + legacy._grace(config, row.fixture_key))
    row.version += 1
    _audit(db, event.id, "system", "fixture_started", now, competition=room.id, fixture=row.id, humans=len(managers))


def _actor(state):
    stage = state["event_next_action"]
    if stage == A_INNINGS_BREAK:
        return None
    return state["bat_team_id"] if stage in (picks.PICK_ACTION[picks.PICK_BAT_APPROACH],
                                              picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]) else state["bowl_team_id"]


def _apply(db, row, room, event, config, state, kind, value, now):
    human = state["event_human_teams"][0]
    stage = state["event_next_action"]
    if kind == "continue":
        if stage != A_INNINGS_BREAK:
            raise ValueError("The innings break is already over.")
        selected = None
    else:
        selected = picks.validate(state, stage, kind, value)
    try:
        if kind == "continue":
            cricket.prepare_pick(state, row.seed, row.step, human)
            terminal = None
        else:
            terminal = cricket.apply_pick(state, row.seed, row.step, human, kind, selected)
            if state["event_next_action"] == A_INNINGS_BREAK and state["play_mode"] == "chat":
                cricket.prepare_pick(state, row.seed, row.step + 1, human)
    except Exception as exc:
        row.failure_at, row.failure_type = now, type(exc).__name__
        row.next_pick_at = None
        _audit(db, event.id, "system", "fixture_engine_failure", now, fixture=row.id, error=type(exc).__name__)
        raise studio.ExecutionUnavailable("The verified engine failure preserved this fixture checkpoint.") from exc
    bowled = kind not in (picks.PICK_BOWLER, "continue") and not (
        kind == picks.PICK_BOWL_APPROACH and state.get("event_pvp"))
    advance = int(bowled and (terminal is not None or
                            state["event_next_action"] != picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]))
    if terminal:
        try:
            with db.begin_nested():
                row.state = state
                row.failure_at = row.failure_type = None
                row.step += advance
                _finish(db, row, room, event, config, terminal, now)
        except Exception as exc:
            note_failure(db, row.id, now, exc)
            raise studio.ExecutionUnavailable("The result could not be recorded. The previous turn is saved.") from exc
    else:
        row.state = state
        row.failure_at = row.failure_type = None
        row.step += advance
        row.version += 1
        legacy._schedule_pick(row, state, now)


def action(db, user, fixture_id, version, name, value, now, *, source):
    row, room, event, config = _fixture(db, fixture_id, write=True)
    actor = _fixture_player(db, room, row, user)
    if row.status != "in_progress" or type(version) is not int or row.version != version:
        raise studio.Conflict("The fixture changed. Reload the current turn.")
    if row.failure_at:
        raise studio.ExecutionUnavailable("The fixture is awaiting technical review.")
    if event.status not in (*studio.OPEN_STATUSES, "closing"):
        raise ValueError("The event is paused or closed.")
    if now > row.due_at:
        raise ValueError("The fixture deadline has passed.")
    state = deepcopy(row.state)
    if name == "abandon":
        winner = row.away if actor.team_key == row.home else row.home
        _finish(db, row, room, event, config, "forfeited", now, winner=winner,
                reason="abandoned", offender=actor.team_key)
    elif name in ("switch", "resume", "pause"):
        if name == "pause" and state["play_mode"] != source:
            raise ValueError("Pause the match from its active surface.")
        # Both participants retain access to the same checkpoint. A captain may
        # only switch the shared surface while it is their turn.
        if _actor(state) not in (None, actor.team_key):
            raise ValueError("Wait for your turn before changing the match controls.")
        state.update(play_mode=source, event_paused=name == "pause")
        row.state = state
        row.version += 1
        if name == "pause":
            row.next_pick_at = None
        else:
            legacy._schedule_pick(row, state, now)
    else:
        if state.get("event_paused") or state["play_mode"] != source:
            raise ValueError("Resume the fixture on the active play surface.")
        if name == "impact":
            from services import impact_player
            if not state.get("event_impact"):
                raise ValueError("Impact Player is disabled.")
            ok, message, _ = impact_player.cipl_use(state, actor.team_key, value.get("inRosterId"),
                                                    value.get("outRosterId"), state["event_next_action"],
                                                    value.get("batPosition"))
            if not ok:
                raise ValueError(message)
            row.state = state
            row.version += 1
        else:
            if _actor(state) not in (None, actor.team_key):
                raise ValueError("It is the other captain's turn.")
            if name in ("retire_batsman", "live_batting_order"):
                from services import live_batting
                stage = state["event_next_action"]
                if actor.team_key != state["bat_team_id"]:
                    raise ValueError("Only the batting captain can manage batting.")
                if name == "retire_batsman":
                    live_batting.retire(state, stage, value.get("outRosterId"), value.get("inRosterId"), value.get("token"))
                else:
                    live_batting.reorder(state, stage, value.get("rosterIds"), value.get("token"))
                row.state = state
                row.version += 1
            else:
                _apply(db, row, room, event, config, state, name, value, now)
    return row


def _contributions(db, entrant):
    return db.scalars(select(StudioContribution).join(
        StudioFixtureResult, StudioFixtureResult.fixture_id == StudioContribution.fixture_id)
        .where(StudioContribution.participant_id == entrant.id)
        .order_by(StudioFixtureResult.recorded_at, StudioContribution.fixture_id)).all()


def _tier(db, entrant):
    if not entrant.reward_tier:
        user = db.get(User, entrant.user_id)
        entrant.reward_tier = "diamond" if get_tier(user) == "diamond" else "free"
    return entrant.reward_tier


def _reward(db, entrant, source, assets, expires):
    assets = {key: amount for key, amount in assets.items() if amount > 0}
    if assets:
        db.add(StudioCompetitionReward(participant_id=entrant.id, source=source,
                                       tier=entrant.reward_tier, assets=assets, expires_at=expires))


def _finish(db, row, room, event, config, kind, now, *, winner=None, reason=None, offender=None):
    if db.get(StudioFixtureResult, row.id):
        return
    detail = {"reason": reason, "offender": offender} if reason else {}
    if kind == "completed":
        winner, detail = cricket.match_result(row.state)
    db.add(StudioFixtureResult(fixture_id=row.id, kind=kind, winner=winner, detail=detail, recorded_at=now))
    row.status, row.next_pick_at = kind, None
    row.version += 1
    db.flush()
    if kind == "completed":
        playoffs = [item for item in config["fixtures"] if item["stage"] == "playoff" and item["key"] != "third-place"]
        final = bool(playoffs and row.fixture_key == max(playoffs, key=lambda item: item["round"])["key"])
        deadline = event.closes_at + timedelta(days=config["claim_days"])
        stage = rules.fixture_rows(config)[row.fixture_key]["stage"]
        for team, entrant in _managers(db, room, row).items():
            performance = progression.contribution(row.state, team)
            db.add(StudioContribution(fixture_id=row.id, participant_id=entrant.id, stage=stage,
                                      metrics=goals.fixture_metrics(row.state, team, winner, performance, stage=stage, final=final),
                                      performance=performance))
            db.flush()
            all_samples = _contributions(db, entrant)
            earned = set(db.scalars(select(StudioStarClaim.objective_id).filter_by(participant_id=entrant.id)))
            for objective in config["tournament_objectives"]:
                if objective["id"] not in earned and goals.measure(objective, all_samples)[1]:
                    _tier(db, entrant)
                    db.add(StudioStarClaim(participant_id=entrant.id, objective_id=objective["id"],
                                           fixture_id=row.id, stars=objective["stars"], earned_at=now,
                                           expires_at=deadline))
            if config["community"]["completion_xp"]:
                db.add(StudioCompetitionXP(fixture_id=row.id, participant_id=entrant.id,
                                           amount=config["community"]["completion_xp"],
                                           daily_limit=config["community"]["daily_limit"], due_at=now))
            if config["competition"]["match_rewards"]:
                tier = _tier(db, entrant)
                outcome = ("win" if winner == team else "loss") if winner else "tie"
                assets = rules.fixture_rules(config, row.fixture_key)["rewards"].get(tier, {}).get(outcome, {})
                _reward(db, entrant, f"fixture:{row.id}", assets, deadline)
    if config["schedule"]["preset"] == "survival" and row.home and row.away:
        solo = next((p.team_key for p in _entries(db, room) if p.user_id), None)
        if solo in (row.home, row.away) and winner != solo:
            for future, outcome in _rows(db, room):
                if outcome is None:
                    future.status = "cancelled"
                    db.add(StudioFixtureResult(fixture_id=future.id, kind="cancelled",
                                               detail={"reason": "eliminated"}, recorded_at=now))
    db.flush()
    outstanding = db.scalar(select(StudioFixture.id).where(
        StudioFixture.competition_id == room.id, StudioFixture.status.in_(PENDING)).limit(1))
    if not outstanding:
        room.status, room.closed_at = "completed", now
        room.version += 1
    _audit(db, event.id, "system", "fixture_result", now, fixture=row.id, winner=winner, kind=kind, reason=reason)


def claim(db, user, event_id, now, *, objective_id=None, reward_id=None, wallet=False):
    _entrant(user)
    event = _event(db, event_id, lock=True)
    entrant = membership(db, event, user.id)
    if not entrant:
        raise ValueError("Your tournament entry is unavailable.")
    room = studio.lock(db, StudioCompetition, entrant.competition_id)
    entrant = studio.lock(db, StudioParticipant, entrant.id)
    config = _config(db, room)
    if not wallet:
        stmt = select(StudioStarClaim).filter_by(participant_id=entrant.id)
        if objective_id is not None:
            stmt = stmt.where(StudioStarClaim.objective_id == objective_id)
        rows = db.scalars(stmt.order_by(StudioStarClaim.id)).all()
        if objective_id is not None and (not rows or not rows[0].claimed_at and rows[0].expires_at <= now):
            raise ValueError("This objective is unavailable or its claim expired.")
        before = entrant.claimed_stars
        for row in rows:
            if row.claimed_at is None and row.expires_at > now:
                entrant.claimed_stars += row.stars
                row.claimed_at = now
                row.total_at_claim = entrant.claimed_stars
                _audit(db, event.id, user.id, "stars_claimed", now,
                       participant=entrant.id, objective=row.objective_id, stars=row.stars)
        if entrant.claimed_stars > before:
            expires = event.closes_at + timedelta(days=config["claim_days"])
            for milestone in config["milestones"]:
                if before < milestone["stars"] <= entrant.claimed_stars:
                    _reward(db, entrant, f'stars:{milestone["stars"]}', milestone.get(_tier(db, entrant), {}), expires)
        return {"claimed_stars": entrant.claimed_stars, "receipts": [
            {"objective_id": p.objective_id, "stars": p.stars, "total": p.total_at_claim,
             "claimed_at": schedule.utc_text(p.claimed_at)} for p in rows if p.claimed_at]}
    account = lock_user_for_update(db, user)
    stmt = select(StudioCompetitionReward).filter_by(participant_id=entrant.id)
    if reward_id is not None:
        stmt = stmt.where(StudioCompetitionReward.id == reward_id)
    rows = db.scalars(stmt.order_by(StudioCompetitionReward.id)).all()
    if reward_id is not None and (not rows or not rows[0].claimed_at and rows[0].expires_at <= now):
        raise ValueError("Reward is unavailable or expired.")
    for reward in rows:
        if reward.claimed_at or reward.expires_at <= now:
            continue
        for asset, amount in reward.assets.items():
            field = rules.ASSETS[asset]
            total = (getattr(account, field) or 0) + amount
            if total > 2_147_483_647:
                raise ValueError("This reward would exceed wallet capacity.")
            setattr(account, field, total)
        receipt = log_activity(db, account.id, "event_reward",
                               f"Live Event {event_id}, competition reward {reward.id}, Quest Points +{reward.assets.get('quest_points', 0)}",
                               coins_change=reward.assets.get("coins", 0), gems_change=reward.assets.get("gems", 0))
        db.flush()
        reward.claimed_at, reward.ledger_id = now, receipt.id
        _audit(db, event.id, user.id, "competition_reward_claim", now, reward=reward.id, receipt=receipt.id)
    return {"receipts": [r.id for r in rows if r.claimed_at]}


def _table_config(config):
    return {**config, "points": {**config["points"], "no_result": config["competition"]["double_no_show_points"]}}


def run_fixture(db, fixture_id, now, *, budget=6):
    row, room, event, config = _fixture(db, fixture_id, write=True)
    if (room.status != "running" or event.status not in (*studio.OPEN_STATUSES, "closing")
            or row.status not in PENDING):
        return
    if row.status == "locked":
        if row.opens_at > now:
            return
        resolved = legacy._resolve(_table_config(config), rules.fixture_rows(config)[row.fixture_key], _rows(db, room))
        if resolved is None:
            row.due_at = now + timedelta(seconds=20)
            return
        row.home, row.away = resolved
        row.status = "scheduled"
        row.due_at = row.deadline if _managers(db, room, row) else row.opens_at
        row.version += 1
    if row.status in ("scheduled", "simulating") and (now < row.opens_at or not _round_open(db, room, row, config)):
        if row.due_at <= now:
            row.due_at = now + timedelta(seconds=20)
        return
    managers = _managers(db, room, row)
    if managers:
        if row.failure_at:
            return
        if now > (row.due_at if row.status == "in_progress" else row.deadline):
            ready = row.setup.get("ready", ())
            ready_sides = set(ready) & managers.keys()
            if len(managers) == 1:
                loser = next(iter(managers))
                winner = row.away if loser == row.home else row.home
            elif row.status == "scheduled" and len(ready_sides) == 1:
                winner = next(iter(ready_sides))
                loser = row.away if winner == row.home else row.home
            else:
                winner = loser = None
            _finish(db, row, room, event, config, "forfeited" if winner else "no_result", now,
                    winner=winner, reason="deadline", offender=loser)
        elif row.status == "in_progress" and row.next_pick_at and row.next_pick_at <= now:
            state = deepcopy(row.state)
            if not state.get("event_paused"):
                if state["event_next_action"] == A_INNINGS_BREAK:
                    kind, value = "continue", 0
                else:
                    kind = picks.PICK_NEW_BATSMAN
                    value = cricket.match.available_batsmen(state)[0]["roster_id"]
                _apply(db, row, room, event, config, state, kind, value, now)
                if state["play_mode"] == "chat":
                    return row.id
        return
    if row.due_at > now:
        return
    effective = rules.fixture_rules(config, row.fixture_key)
    if row.status == "scheduled":
        row.seed = row.seed or token_hex(32)
        xis = {side: legacy._opponent_xi(config["snapshot"][side], effective, "tournament_team")
               for side in (row.home, row.away)}
        state = cricket.build_state(effective, row.id, row.home, row.away, xis, row.seed)
    else:
        state = deepcopy(row.state)
    for _ in range(budget):
        terminal = cricket.advance(state, row.seed, row.step)
        row.step += 1
        if terminal:
            row.state = state
            row.failure_at = row.failure_type = None
            _finish(db, row, room, event, config, terminal, now)
            return
    row.state, row.status, row.due_at = state, "simulating", now
    row.version += 1


def due_work(db, now, limit=24):
    rooms = db.scalars(select(StudioCompetition.id).join(StudioEvent, StudioEvent.id == StudioCompetition.event_id)
                       .where(StudioCompetition.status == "lobby", StudioCompetition.join_until <= now,
                              StudioEvent.status != "paused").order_by(StudioCompetition.join_until).limit(limit)).all()
    fixtures = db.scalars(select(StudioFixture.id).join(
        StudioCompetition, StudioCompetition.id == StudioFixture.competition_id)
        .join(StudioEvent, StudioEvent.id == StudioCompetition.event_id)
        .where(StudioFixture.status.in_(PENDING), StudioFixture.failure_at.is_(None),
               StudioEvent.status.in_((*studio.OPEN_STATUSES, "closing")),
               or_(StudioFixture.due_at <= now, StudioFixture.next_pick_at <= now))
        .order_by(StudioFixture.due_at, StudioFixture.id).limit(limit)).all()
    return rooms, fixtures


def note_failure(db, fixture_id, now, error):
    row, room, event, _ = _fixture(db, fixture_id, write=True)
    if row.status in PENDING and not row.failure_at:
        row.failure_at = now
        row.failure_type = type(error).__name__
        _audit(db, event.id, "system", "fixture_engine_failure", now, fixture=row.id, error=row.failure_type)


def _fixture_view(row, result, entrant, config, now, event, round_open, *, managers=None):
    template = rules.fixture_rows(config)[row.fixture_key]
    team = entrant.team_key
    names = config["snapshot"]
    personal = team in (row.home, row.away)
    status = "under_review" if row.failure_at else row.status
    return {"id": row.id, "key": row.fixture_key, "version": row.version, "status": status,
            "home": names[row.home]["name"] if row.home else None, "away": names[row.away]["name"] if row.away else None,
            "home_key": row.home, "away_key": row.away, "round": template["round"], "stage": template["stage"],
            "personal": personal, "opens_at": schedule.utc_text(row.opens_at),
            "deadline": schedule.utc_text(row.deadline), "cutoff": schedule.utc_text(row.due_at if row.started_at else row.deadline),
            "can_start": personal and status == "scheduled" and round_open and _window(event, row, now),
            "managers": managers,
            "result": {"kind": result.kind, "winner": names[result.winner]["name"] if result.winner else None,
                       **result.detail} if result else None}


def _progress(db, entrant, config, now):
    claims = {row.objective_id: row for row in db.scalars(select(StudioStarClaim).filter_by(participant_id=entrant.id))}
    samples = _contributions(db, entrant)
    entries = []
    for obj in config["tournament_objectives"]:
        measured, _ = goals.measure(obj, samples)
        receipt = claims.get(obj["id"])
        threshold = float(obj["target"])
        progress = min(1, measured / threshold) if obj.get("comparator") != "<=" else (
            min(1, threshold / measured) if measured else 0)
        if receipt:
            progress = 1.0
            status = "claimed" if receipt.claimed_at else "expired" if receipt.expires_at <= now else "ready"
        else:
            status = "in_progress" if samples else "locked"
        entries.append({**obj, "title": obj.get("title") or goals.METRICS[obj["metric"]],
                        "measured": measured, "progress": progress, "status": status,
                        "expires_at": schedule.utc_text(receipt.expires_at) if receipt else None})
    return {"objectives": entries, "maximum": goals.max_stars(config), "claimed": entrant.claimed_stars,
            "earned": sum(row.stars for row in claims.values()),
            "unclaimed": sum(row.stars for row in claims.values() if not row.claimed_at and row.expires_at > now)}


def dashboard(db, user, event_id, now):
    event = _event(db, event_id)
    config = studio.revision(db, event, published=True).config
    _entrant(user)
    if not goals.tournament(config):
        raise ValueError("This event uses its existing campaign dashboard.")
    entrant = membership(db, event, user.id)
    max_stars = goals.max_stars(config)
    view = {"id": event.id, "name": event.name, "joined": bool(entrant), "format": config["format"],
            "timezone": config["timezone"], "banner": config["banner_url"], "description": config["description"],
            "open": schedule.utc_text(event.opens_at), "close": schedule.utc_text(event.closes_at),
            "enroll_until": schedule.utc_text(legacy._cutoff(event, config)), "fixture_count": len(config["fixtures"]),
            "difficulty": config.get("difficulty") or "hard", "maximum": max_stars,
            "registration_open": _registration_open(event, config, now),
            "modes": [mode for mode in config["competition"]["modes"] if mode_enabled(config, mode)],
            "objectives": [{**obj, "title": obj.get("title") or goals.METRICS[obj["metric"]]}
                           for obj in config["tournament_objectives"]],
            "milestones": sorted(config["milestones"], key=lambda m: m["stars"]),
            "teams": [team_cards(config, key) for key in config["snapshot"]]}
    if entrant is None:
        return view
    room = db.get(StudioCompetition, entrant.competition_id)
    view.update(room=_lobby_view(db, room, event, config, entrant, user, now), team=entrant.team_key,
                tier=entrant.reward_tier, claim_days=config["claim_days"])
    if room.status == "lobby":
        return view
    rows = _rows(db, room)
    board = legacy.table(_table_config(config), rows)
    unresolved = [rules.fixture_rows(config)[fixture.fixture_key]["round"] for fixture, res in rows if not res]
    current_round = min(unresolved, default=0)
    managers = {team["key"]: team["owner"] for team in view["room"]["teams"]}
    view["fixtures"] = [_fixture_view(fixture, res, entrant, config, now, event,
                                      rules.fixture_rows(config)[fixture.fixture_key]["round"] <= current_round,
                                      managers={key: managers[key] for key in (fixture.home, fixture.away) if key})
                        for fixture, res in rows]
    view["standings"] = board
    view["my_standing"] = next(item for item in board if item["team"] == entrant.team_key)
    view["remaining"] = sum(item["personal"] and item["result"] is None for item in view["fixtures"])
    recent = [res for fixture, res in rows if res and entrant.team_key in (fixture.home, fixture.away)]
    view["form"] = [("W" if res.winner == entrant.team_key else "L" if res.winner else
                     "T" if res.kind == "completed" else "NR") for res in recent[-5:]]
    view["trophy"] = progression.trophy(config, entrant, rows, board)
    if not view["trophy"] and room.status == "completed":
        playoffs = [item for item in config["fixtures"] if item["stage"] == "playoff" and item["key"] != "third-place"]
        if playoffs:
            final_key = max(playoffs, key=lambda item: item["round"])["key"]
            fixture, outcome = next((f, res) for f, res in rows if f.fixture_key == final_key)
            if outcome and outcome.kind == "no_result" and fixture.home and fixture.away:
                seed_order = [side["team"] for side in board] if any(
                    f["stage"] == "league" for f in config["fixtures"]) else list(config["snapshot"])
                if entrant.team_key == min((fixture.home, fixture.away), key=seed_order.index):
                    view["trophy"] = "Tournament champion · advanced by seed"
    view["stars"] = _progress(db, entrant, config, now)
    # Scores from AI fixtures feed standings, never human goal or player stats.
    statistics = db.scalars(select(StudioContribution).join(StudioParticipant).where(
        StudioParticipant.competition_id == room.id)).all()
    view["statistics"] = progression.player_stats([(None, SimpleNamespace(kind="completed",
        detail={"performance": row.performance})) for row in statistics])
    view["rewards"] = [_reward_view(row, now) for row in db.scalars(
        select(StudioCompetitionReward).filter_by(participant_id=entrant.id).order_by(StudioCompetitionReward.id))]
    view["leaderboard"] = leaderboard(db, event, entrant, room.kind)
    return view


def _reward_view(row, now):
    return {"id": row.id, "source": row.source, "tier": row.tier, "assets": row.assets,
            "expires_at": schedule.utc_text(row.expires_at), "status": "claimed" if row.claimed_at else
            "expired" if row.expires_at <= now else "claimable", "receipt": row.ledger_id}


def leaderboard(db, event, entrant, mode):
    rows = db.execute(select(StudioParticipant.id, StudioParticipant.claimed_stars,
                             StudioParticipant.user_id, User.first_name, User.username)
        .select_from(StudioParticipant).join(StudioCompetition, StudioCompetition.id == StudioParticipant.competition_id)
        .join(User, User.id == StudioParticipant.user_id)
        .where(StudioParticipant.event_id == event.id, StudioParticipant.revision == entrant.revision,
               StudioCompetition.kind == mode, StudioCompetition.started_at.is_not(None))
        .order_by(StudioParticipant.claimed_stars.desc(), StudioParticipant.id).limit(25)).all()
    return [{"rank": i, "name": "You" if user_id == entrant.user_id else first or username or "Manager", "stars": stars}
            for i, (_, stars, user_id, first, username) in enumerate(rows, 1)]


def arena(db, user, fixture_id, now):
    from services.crickidex_arena import serialize_event_match
    from services.event_media_service import get_miniapp_event_gifs
    from services.event_sound_service import get_miniapp_event_sounds

    row, room, event, config = _fixture(db, fixture_id)
    entrant = _fixture_player(db, room, row, user)
    if not row.state:
        raise ValueError("This fixture has no live checkpoint.")
    result = db.get(StudioFixtureResult, row.id)
    view = _fixture_view(row, result, entrant, config, now, event, True)
    state = row.state
    opponent = next((p for key, p in _managers(db, room, row).items() if key != entrant.team_key), None)
    overlay = {**view, "name": event.name, "id": event.id, "game_id": row.id,
               "play_mode": state["play_mode"], "player_paused": bool(state.get("event_paused")),
               "event_paused": event.status == "paused", "venue": state.get("stadium"),
               "weather": (state.get("conditions") or {}).get("weather"),
               "difficulty": cricket.difficulty(rules.fixture_rules(config, row.fixture_key), "tournament_team"),
               "objectives": [], "competition_id": room.id,
               "reward_eligible": bool(db.scalar(select(StudioCompetitionReward.id).filter_by(
                   participant_id=entrant.id, source=f"fixture:{row.id}")))}
    payload = serialize_event_match(db, row, entrant, event, user, result, overlay, state=state,
                                    opponent_user=db.get(User, opponent.user_id) if opponent else None)
    payload.update(eventGifs=get_miniapp_event_gifs(db), eventSounds=get_miniapp_event_sounds(db))
    return payload


def operate(db, event, action, actor, reason, now, *, shift=None):
    rooms = db.scalars(select(StudioCompetition).filter_by(event_id=event.id)
                       .where(StudioCompetition.status.in_(("lobby", "running")))
                       .order_by(StudioCompetition.id).with_for_update()).all()
    for room in rooms:
        if room.status == "lobby" and action == "cancel":
            _cancel(db, room, now)
        if room.status != "running":
            continue
        for fixture, result in _rows(db, room):
            if result:
                continue
            if action == "resume":
                fixture.opens_at += shift
                fixture.deadline += shift
                fixture.due_at += shift
                if fixture.next_pick_at:
                    fixture.next_pick_at += shift
                fixture.version += 1
            elif action == "cancel":
                fixture.status = "cancelled"
                fixture.version += 1
                fixture.next_pick_at = None
                db.add(StudioFixtureResult(fixture_id=fixture.id, kind="cancelled",
                                           detail={"reason": reason}, recorded_at=now))
        if action == "cancel":
            room.status, room.closed_at = "cancelled", now
            room.version += 1


def administer(db, event_id, actor, action, target, reason, now, *, deadline=None):
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 500:
        raise ValueError("Enter an operations reason.")
    if action == "close_lobby":
        room, event, _ = _room(db, target, write=True)
        if event.id != event_id or room.status != "lobby":
            raise ValueError("Only a pre-start lobby can be closed.")
        _cancel(db, room, now)
        _audit(db, event.id, actor, action, now, competition=room.id, reason=reason)
        return
    row, room, event, config = _fixture(db, target, write=True)
    if event.id != event_id or row.status not in PENDING:
        raise ValueError("This fixture already has an official result or is unavailable.")
    if action == "extend":
        end = schedule.utc_instant(deadline, config["timezone"])
        if not max(now, row.deadline) < end or end + legacy._grace(config, row.fixture_key) > event.closes_at:
            raise ValueError("Extend within the event's published close and grace period.")
        row.deadline = end
        row.due_at = end + legacy._grace(config, row.fixture_key) if row.started_at else end
        row.version += 1
    elif action == "retry_fixture" and row.failure_at:
        if now > (row.due_at if row.started_at else row.deadline):
            raise ValueError("Extend the fixture within the event window before retrying its checkpoint.")
        row.failure_at = row.failure_type = None
        row.version += 1
    elif action == "void_fixture" and row.failure_at:
        _finish(db, row, room, event, config, "no_result", now, reason="verified_technical_incident")
    else:
        raise ValueError("That technical operation is unavailable.")
    _audit(db, event.id, actor, action, now, fixture=row.id, reason=reason, deadline=deadline)


def browser(db, user, now):
    rows = db.execute(select(StudioEvent, StudioRevision).join(StudioRevision, and_(
        StudioEvent.id == StudioRevision.event_id, StudioEvent.published_number == StudioRevision.number))
        .where(StudioEvent.status.in_(studio.OPEN_STATUSES), StudioEvent.opens_at <= now, StudioEvent.closes_at > now)).all()
    published = [(event, rev.config) for event, rev in rows
                 if rev.config["kind"] == "live" and studio.allowed("live", rev.config["format"])
                 and (not goals.tournament(rev.config) or any(mode_enabled(rev.config, mode)
                      for mode in ("solo", "multi")) or membership(db, event, user.id))]
    return sorted(published, key=lambda pair: (not pair[1].get("featured"), pair[0].closes_at, pair[0].id))


def take_start_notices(db, now, limit=10):
    """Reserve each joined player's start notification once, before networking."""
    pending = db.execute(select(StudioCompetitionNotice.competition_id, StudioCompetitionNotice.user_id,
                                StudioCompetition.event_id, StudioEvent.name, User.telegram_id)
                         .join(StudioCompetition, StudioCompetition.id == StudioCompetitionNotice.competition_id)
                         .join(StudioEvent, StudioEvent.id == StudioCompetition.event_id)
                         .join(User, User.id == StudioCompetitionNotice.user_id)
                         .where(StudioCompetitionNotice.sent_at.is_(None), StudioCompetition.status == "running",
                                StudioEvent.status.in_(studio.OPEN_STATUSES), User.is_banned.is_(False),
                                User.dm_blocked.is_(False), User.notifications_enabled.is_(True))
                         .order_by(StudioCompetitionNotice.created_at).limit(limit)).all()
    sent = []
    for room_id, user_id, event_id, name, tg in pending:
        taken = db.execute(update(StudioCompetitionNotice).where(
            StudioCompetitionNotice.competition_id == room_id, StudioCompetitionNotice.user_id == user_id,
            StudioCompetitionNotice.sent_at.is_(None)).values(sent_at=now))
        if taken.rowcount:
            sent.append((tg, event_id, name))
    return sent
