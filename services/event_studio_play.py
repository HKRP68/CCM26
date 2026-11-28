"""Transactional official starts, cricket results, and wallet claims shared by both clients."""

from copy import deepcopy
from datetime import datetime, timedelta
import logging
import os
from secrets import token_hex

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.orm import defer, joinedload

from models import UserRoster
from models_events import (
    StudioAudit, StudioEnrollment, StudioEntitlement, StudioEvent, StudioGame, StudioResult, StudioRevision, StudioXPJob,
)
from services import cipl_picks as picks, event_studio as studio, event_studio_cricket as cricket
from services.match_state_store import A_INNINGS_BREAK
from services import event_studio_notices as notices
from services import event_studio_progress as progress, event_studio_rules as rules, event_studio_schedule as schedule
from services.activity_service import log_activity
from services.economy_transactions import lock_user_for_update
from services.subscription_service import get_tier

PENDING = ("locked", "scheduled", "in_progress", "simulating")
UNPLAYED = ("forfeited", "expired", "abandoned", "no_result", "technical_void", "cancelled")
logger = logging.getLogger(__name__)


def _time(clock):
    return clock() if clock is not None else datetime.utcnow()


def _cutoff(event, config):
    """When joining stops — derived, not authored. See studio.enrollment_closes."""
    return studio.enrollment_closes(config) + timedelta(seconds=event.delay_seconds)


def _enrollment(db, event_id, user_id):
    return db.scalar(select(StudioEnrollment).filter_by(event_id=event_id, user_id=user_id))


def _team_keys(config):
    return tuple(team["key"] for team in config["teams"])


def _ladder(config):
    return config["kind"] == "live" and config["schedule"]["preset"] == "survival"


def list_events(db, user, now):
    joined = {row.event_id for row in db.scalars(select(StudioEnrollment)
                                               .filter_by(user_id=user.id))}
    events = db.scalars(select(StudioEvent).where(StudioEvent.published_number.is_not(None))
                        .where(((StudioEvent.closes_at > now) & (StudioEvent.status != "cancelled"))
                               | StudioEvent.id.in_(joined))
                        .order_by(StudioEvent.opens_at, StudioEvent.id).limit(100))
    output = []
    for event in events:
        config = studio.revision(db, event, published=True).config
        if event.id in joined or studio.allowed(config["kind"], config["format"]):
            output.append(event_summary(event, config, event.id in joined, now))
    return sorted(output, key=lambda event: not event["featured"])


def event_summary(event, config, joined, now):
    return {"id": event.id, "slug": event.slug, "name": event.name, "kind": config["kind"],
            "format": config["format"], "description": config["description"], "featured": config["featured"],
            "banner_url": config.get("banner_url"), "cadence": config["rotation"]["cadence"],
            "opens_at": schedule.utc_text(event.opens_at), "closes_at": schedule.utc_text(event.closes_at),
            "availability": (event.status if event.status in ("paused", "cancelled") else "upcoming" if now < event.opens_at
                             else "closed" if now >= event.closes_at else "live"), "joined": joined,
            "can_join": (event.status in studio.OPEN_STATUSES and event.opens_at <= now < _cutoff(event, config)
                         and studio.allowed(config["kind"], config["format"])), "timezone": config["timezone"]}


def _published(db, event_id):
    event = db.get(StudioEvent, event_id)
    if event is None or not event.published_number:
        raise ValueError("Event is unavailable.")
    return event, studio.revision(db, event, published=True).config


def _chapter_lock(db, user_id, config):
    requirement = config.get("chapter_requirement")
    if not requirement:
        return None
    stars = db.scalar(select(func.max(StudioResult.detail["stars"].as_integer()))
                      .select_from(StudioResult)
                      .join(StudioGame, StudioGame.id == StudioResult.game_id)
                      .join(StudioEnrollment, StudioEnrollment.id == StudioGame.enrollment_id)
                      .where(StudioEnrollment.event_id == requirement["event_id"],
                             StudioEnrollment.revision == requirement["revision"],
                             StudioEnrollment.user_id == user_id, StudioResult.kind == "success")) or 0
    required = requirement["min_stars"]
    if stars >= required:
        return None
    return f'Complete {requirement["name"]} with at least {required} star{"s" if required != 1 else ""}.'


def _boss_progress(db, event_id, config):
    target = config["community"]["boss_goal"]
    if not target:
        return None
    victories = (StudioResult.kind == "success" if rules.is_moment(config)
                 else (StudioResult.kind == "completed") & (StudioResult.winner == StudioEnrollment.team_key))
    count = db.scalar(select(func.count(func.distinct(StudioEnrollment.user_id)))
                      .select_from(StudioEnrollment)
                      .join(StudioGame, StudioGame.enrollment_id == StudioEnrollment.id)
                      .join(StudioResult, StudioResult.game_id == StudioGame.id)
                      .where(StudioEnrollment.event_id == event_id, victories))
    return {"target": target, "winners": count}


def join(db, user, event_id, team_key, mode, now):
    event = studio.lock(db, StudioEvent, event_id)
    if event is None or not event.published_number:
        raise ValueError("Event is unavailable.")
    previous = _enrollment(db, event.id, user.id)
    if previous:
        return previous
    config = studio.revision(db, event, published=True).config
    if not event_summary(event, config, False, now)["can_join"]:
        raise ValueError("Enrollment is closed.")
    restriction = _chapter_lock(db, user.id, config)
    if restriction:
        raise ValueError(restriction)
    if mode not in config["mode"] or team_key not in config["snapshot"]:
        raise ValueError("Choose a participating team and track.")
    if (config["kind"] != "live" or _ladder(config)) and team_key != _team_keys(config)[0]:
        raise ValueError("This challenge has a fixed playable side.")
    # There is no enrollment cap: anyone may join while the event is open. ``max_users``
    # is only the figure the reward budget is planned against (event_studio_rules).
    roster = []
    if mode == "own_roster":
        owned = db.scalars(select(UserRoster).options(joinedload(UserRoster.player))
                           .filter_by(user_id=user.id).order_by(UserRoster.order_position, UserRoster.id))
        roster = [studio.card_snapshot(db, record.player, config, roster_id=record.id) for record in owned
                  if record.player.is_active and studio._gender_eligible(record.player, config)]
        from services.trait_rating_service import roster_traits
        traits = roster_traits(db, [p["roster_id"] for p in roster])
        for player in roster:
            player["traits"] = traits.get(player["roster_id"], [])
        studio.best_xi(roster, config)
    entry = StudioEnrollment(event_id=event.id, user_id=user.id, revision=event.published_number,
                             team_key=team_key, mode=mode, roster=roster, joined_at=now)
    db.add(entry)
    db.flush()
    if config["kind"] == "live":
        reminders = []
        for row in config["fixtures"]:
            shift = timedelta(seconds=event.delay_seconds)
            opens = schedule.utc_instant(row["opens_at"], config["timezone"]) + shift
            deadline = schedule.utc_instant(row["deadline"], config["timezone"]) + shift
            home, away = row["home"], row["away"]
            locked = ":" in home or ":" in away
            game = StudioGame(enrollment_id=entry.id, fixture_key=row["key"], opens_at=opens,
                              deadline=deadline, due_at=deadline if team_key in (home, away) else opens,
                              home=None if locked else home, away=None if locked else away,
                              status="locked" if locked else "scheduled")
            db.add(game)
            if config["notifications"]["enabled"] and not locked and team_key in (home, away):
                reminders.append(game)
        if reminders:
            db.flush()
            for game in reminders:
                notices.enqueue(db, game, config, now)
    return entry


def _available_roster(db, config, entry):
    team = config["snapshot"][entry.team_key]
    if entry.mode != "own_roster":
        return team["squad"], team["default_xi"]
    records = db.execute(select(UserRoster.id, UserRoster.player_id).join(UserRoster.player)
                         .where(UserRoster.user_id == entry.user_id, UserRoster.player.has(is_active=True)))
    owned = dict(records.all())
    return [p for p in entry.roster if owned.get(p["roster_id"]) == p["player_id"]], None


def _selected_xi(db, config, entry, selected):
    team = config["snapshot"][entry.team_key]
    roster, default = _available_roster(db, config, entry)
    if entry.mode == "tournament_team" and "xi" in team and selected is not None:
        if selected != [p["roster_id"] for p in default]:
            raise ValueError("This fixture uses a fixed XI and batting order.")
    if selected is None:
        return default or studio.best_xi(roster, config)
    if not isinstance(selected, list) or len(selected) != 11 or any(type(pid) is not int for pid in selected):
        raise ValueError("Select and order exactly 11 players.")
    players = {p["roster_id"]: p for p in roster}
    if any(pid not in players for pid in selected):
        raise ValueError("The selected XI includes a card outside your eligible roster.")
    xi = [players[pid] for pid in selected]
    studio.validate_xi(xi, config)
    return xi


def _ownership(db, user_id, game_id):
    entry = db.scalar(select(StudioEnrollment).join(StudioGame, StudioGame.enrollment_id == StudioEnrollment.id)
                      .where(StudioGame.id == game_id, StudioEnrollment.user_id == user_id))
    if entry is None:
        raise ValueError("Game is unavailable.")
    game = studio.lock(db, StudioGame, game_id)
    event = db.get(StudioEvent, entry.event_id)
    if event.status == "paused":
        raise ValueError("The event is paused. Resume this game after play reopens.")
    config = db.get(StudioRevision, (entry.event_id, entry.revision)).config
    if entry.team_key not in (game.home, game.away):
        raise ValueError("This fixture is simulated.")
    return game, entry, config


def _fixture(config, key):
    return next((row for row in config["fixtures"] if row["key"] == key), None)


def _ensure_start(event, config, now):
    if (event.status not in studio.OPEN_STATUSES or not event.opens_at <= now < event.closes_at
            or not studio.allowed(config["kind"], config["format"])):
        raise ValueError("Official starts are closed.")


def _grace(config, key):
    row = _fixture(config, key)
    return timedelta(minutes=row["grace"] if row else config["schedule"]["grace"])


def _earlier_pending(db, entry, config, row):
    earlier = {f["key"] for f in config["fixtures"] if f["round"] < row["round"]}
    return earlier and db.scalar(select(StudioGame.id).where(
        StudioGame.enrollment_id == entry.id, StudioGame.fixture_key.in_(earlier),
        StudioGame.status.in_(PENDING),
        (StudioGame.home == entry.team_key) | (StudioGame.away == entry.team_key)).limit(1))


def _start_window(db, game, entry, event, config, now):
    _ensure_start(event, config, now)
    if game.status != "scheduled" or not game.opens_at <= now < game.deadline:
        raise ValueError("This fixture is outside its start window.")
    if _earlier_pending(db, entry, config, _fixture(config, game.fixture_key)):
        raise ValueError("Finish your earlier fixture first.")


def _last_attempt(db, entry):
    return db.scalar(select(StudioGame).filter_by(enrollment_id=entry.id)
                     .order_by(StudioGame.attempt.desc()).limit(1))


def setup_fixture(db, user, event_id, game_id, previous_attempt, now, *,
                  call=None, decision=None, xi=None, edit=False):
    """Persist the toss and confirmed XI for a fixture or full-match attempt."""
    if game_id:
        game, entry, config = _ownership(db, user.id, game_id)
        if entry.event_id != event_id:
            raise ValueError("This fixture is unavailable.")
        event = db.get(StudioEvent, entry.event_id)
        _start_window(db, game, entry, event, config, now)
        slot = f"fixture:{game.id}"
        home, away = game.home, game.away
        cutoff = game.deadline
        key = game.fixture_key
        entry = studio.lock(db, StudioEnrollment, entry.id)
    else:
        event, config = _published(db, event_id)
        entry = _enrollment(db, event_id, user.id)
        if not entry or config["kind"] == "live" or rules.is_moment(config):
            raise ValueError("This challenge has no full-match setup.")
        entry = studio.lock(db, StudioEnrollment, entry.id)
        _ensure_start(event, config, now)
        previous = _last_attempt(db, entry)
        count = previous.attempt if previous else 0
        cutoff = event.closes_at - _grace(config, "challenge")
        used = db.scalar(select(func.count(StudioGame.id)).where(
            StudioGame.enrollment_id == entry.id, StudioGame.status != "technical_void"))
        if count != previous_attempt or (previous and previous.status == "in_progress"):
            raise studio.Conflict("Refresh the attempt history before starting.")
        if now >= cutoff or used >= config["attempts"]:
            raise ValueError("The official attempt is unavailable.")
        slot = f"attempt:{count + 1}"
        home, away = _team_keys(config)
        key = "challenge"
    opponent = away if home == entry.team_key else home
    team = config["snapshot"][entry.team_key]
    default = _selected_xi(db, config, entry, None)
    squad, _ = _available_roster(db, config, entry)
    setups = dict(entry.match_setup or {})
    saved = dict(setups.get(slot) or {})
    editing_xi = None
    if call is not None:
        if call not in ("heads", "tails"):
            raise ValueError("Call heads or tails.")
        if not saved:
            from services.match_broadcast import flip_coin
            from services.bot_captain import elect_toss_decision
            coin, won = flip_coin(call)
            saved = {"coin": coin, "call": call, "winner": entry.team_key if won else opponent,
                     "decision": None if won else elect_toss_decision()}
    if decision is not None:
        if decision not in ("bat", "bowl") or not saved or saved["winner"] != entry.team_key:
            raise ValueError("Only the toss winner can choose to bat or bowl.")
        if saved["decision"] not in (None, decision):
            raise studio.Conflict("The toss decision is already locked.")
        saved["decision"] = decision
    if xi is not None or edit:
        if not saved.get("decision"):
            raise ValueError("Decide the toss before picking your Playing XI.")
        if edit:
            if entry.mode == "tournament_team" and "xi" in team:
                raise ValueError("This fixture uses a fixed XI and batting order.")
            editing_xi = saved.pop("xi", None)
        else:
            confirmed = [p["roster_id"] for p in _selected_xi(db, config, entry, xi)]
            if saved.get("xi") and saved["xi"] != confirmed:
                raise studio.Conflict("The Playing XI has already been confirmed. Choose Edit XI to change it.")
            saved["xi"] = confirmed
    if saved != setups.get(slot, {}):
        setups[slot] = saved
        entry.match_setup = setups
    effective = rules.fixture_rules(config, key)
    toss = ({key: saved[key] for key in ("coin", "call", "winner", "decision")} if saved else None)
    if toss:
        toss["human_won"] = saved["winner"] == entry.team_key
    return {"team": team["name"], "opponent": config["snapshot"][opponent]["name"],
            "toss": toss, "confirmed_xi": saved.get("xi"), "editing_xi": editing_xi,
            "cutoff": schedule.utc_text(cutoff), "timezone": config["timezone"],
            "format": effective["format"], "pitch": effective["pitch"],
            "venue": effective.get("venue"), "weather": effective.get("weather"),
            "difficulty": cricket.difficulty(effective, entry.mode),
            "min_overseas": config["min_overseas"], "max_overseas": config["max_overseas"],
            "fixed_xi": entry.mode == "tournament_team" and "xi" in team,
            "squad": [{key: p.get(key) for key in ("roster_id", "name", "version", "rating", "category", "is_overseas")}
                      for p in squad],
            "default_xi": [p["roster_id"] for p in default],
            "objectives": [{**row, "title": rules.OBJECTIVES[row["key"]][0]} for row in effective["objectives"]]}


def _opponent_xi(team, config, mode):
    if cricket.difficulty(config, mode) == "hard_plus":
        return team.get("hard_plus_xi") or team["default_xi"]
    return team["default_xi"]


def _saved_setup(entry, game):
    slot = f"fixture:{game.id}" if game.fixture_key != "challenge" else f"attempt:{game.attempt}"
    return (entry.match_setup or {}).get(slot) or {}


def _launch_xi(entry, game, selected, require_confirmed):
    saved = _saved_setup(entry, game)
    if require_confirmed and not saved.get("xi"):
        raise ValueError("Confirm your Playing XI before starting the match.")
    return saved.get("xi", selected)


def _launch_settings(entry, game, xis, decision, captain_id, require_toss):
    if captain_id is not None and (type(captain_id) is not int or
            captain_id not in {p["roster_id"] for p in xis[entry.team_key]}):
        raise ValueError("Choose a captain in your playing XI.")
    setup = _saved_setup(entry, game)
    if not setup:
        if require_toss:
            raise ValueError("Call the toss before starting the official match.")
        return {}
    winner = setup["winner"]
    decision = setup["decision"] or decision
    if decision not in ("bat", "bowl"):
        raise ValueError("The toss winner must choose to bat or bowl.")
    batting = winner if decision == "bat" else game.away if winner == game.home else game.home
    return {"batting": batting, "toss_winner": winner, "toss_decision": decision}


def start_fixture(db, user, game_id, selected, clock=None, *, source="chat",
                  decision=None, captain_id=None, require_toss=False, require_confirmed=False):
    game, entry, config = _ownership(db, user.id, game_id)
    if game.status not in ("locked", "scheduled"):
        if require_toss and game.status != "in_progress":
            raise studio.Conflict("This fixture already has its official result.")
        return game
    now = _time(clock)
    event = db.get(StudioEvent, entry.event_id)
    _start_window(db, game, entry, event, config, now)
    entry = studio.lock(db, StudioEnrollment, entry.id)
    effective = rules.fixture_rules(config, game.fixture_key)
    opponent = game.away if game.home == entry.team_key else game.home
    selected = _launch_xi(entry, game, selected, require_confirmed)
    xis = {entry.team_key: _selected_xi(db, config, entry, selected),
           opponent: _opponent_xi(config["snapshot"][opponent], effective, entry.mode)}
    toss = _launch_settings(entry, game, xis, decision, captain_id, require_toss)
    _launch(db, game, event, entry, config, xis, user, clock, source=source, toss=toss, captain_id=captain_id)
    return game


def start_attempt(db, user, event_id, previous_attempt, clock=None, *, source="chat", selected=None,
                  decision=None, captain_id=None, require_toss=False, require_confirmed=False):
    event, config = _published(db, event_id)
    entry = _enrollment(db, event.id, user.id)
    if not entry or config["kind"] == "live":
        raise ValueError("Join the challenge before starting an attempt.")
    entry = studio.lock(db, StudioEnrollment, entry.id)
    previous = _last_attempt(db, entry)
    count = previous.attempt if previous else 0
    if previous and (previous.status == "in_progress" or previous_attempt < count):
        return previous
    if previous_attempt != count:
        raise studio.Conflict("Refresh the attempt history before starting.")
    _ensure_start(event, config, _time(clock))
    used = db.scalar(select(func.count(StudioGame.id)).where(
        StudioGame.enrollment_id == entry.id, StudioGame.status != "technical_void"))
    if used >= config["attempts"]:
        raise ValueError("All official attempts have been used.")
    home, away = _team_keys(config)
    cutoff = event.closes_at - _grace(config, "challenge")
    game = StudioGame(enrollment_id=entry.id, home=home, away=away, fixture_key="challenge",
                      attempt=count + 1, opens_at=event.opens_at, deadline=cutoff, due_at=cutoff)
    db.add(game)
    db.flush()
    xis = {key: config["snapshot"][key]["default_xi"] for key in (home, away)}
    toss = {}
    if not rules.is_moment(config):
        selected = _launch_xi(entry, game, selected, require_confirmed)
        xis[entry.team_key] = _selected_xi(db, config, entry, selected)
        effective = rules.fixture_rules(config, "challenge")
        opponent = away if home == entry.team_key else home
        xis[opponent] = _opponent_xi(config["snapshot"][opponent], effective, entry.mode)
        toss = _launch_settings(entry, game, xis, decision, captain_id, require_toss)
    _launch(db, game, event, entry, config, xis, user, clock, source=source, toss=toss, captain_id=captain_id)
    return game


def _launch(db, game, event, entry, config, xis, user, clock, *, source, toss, captain_id):
    seed = token_hex(32)
    effective = rules.fixture_rules(config, game.fixture_key)
    if rules.is_moment(config):
        state = cricket.start_moment(config, game.id, seed, game.away)
        cricket.pin_difficulty(state, effective, entry.mode)
    else:
        benches = {}
        if effective.get("impact_player"):
            for key, xi in xis.items():
                source_squad = (_available_roster(db, config, entry)[0] if key == entry.team_key
                                else config["snapshot"][key]["squad"])
                picked = {player["roster_id"] for player in xi}
                benches[key] = [p for p in source_squad if p["roster_id"] not in picked]
        state = cricket.build_state(effective, game.id, game.home, game.away, xis, seed,
                                    human=entry.team_key, mode=entry.mode, benches=benches, **(toss or {}))
    now = _time(clock)
    if now >= game.deadline:
        raise ValueError("The official start deadline has passed.")
    game.seed = seed
    game.tier = "diamond" if get_tier(user) == "diamond" else "free"
    game.status = "in_progress"
    game.started_at = now
    game.due_at = min(event.closes_at, game.deadline + _grace(config, game.fixture_key))
    event_context: cricket.EventMatchContext = {
        "event_id": event.id, "enrollment_id": entry.id, "game_id": game.id,
        "fixture": game.fixture_key, "attempt": game.attempt, "revision": entry.revision,
        "squad_keys": [game.home, game.away], "track": entry.mode,
        "difficulty": cricket.difficulty(effective, entry.mode), "format": config["format"],
        "deadline": schedule.utc_text(game.deadline), "grace_until": schedule.utc_text(game.due_at),
        "reward_tier": game.tier, "statistics": "event_only",
    }
    state.update(event_context=event_context, play_mode=source, is_private=True,
                 event_captain=captain_id or xis[entry.team_key][0]["roster_id"])
    state["user_names"] = {str(user.telegram_id): user.username or user.first_name or "Player"}
    _bind_actor(state, entry.team_key, user.telegram_id)
    cricket.prepare_pick(state, game.seed, game.step, entry.team_key)
    game.state = state
    game.version += 1


def _bind_actor(state, team, telegram_id):
    state["bat_user_tg"] = telegram_id if state["bat_team_id"] == team else None
    state["bowl_user_tg"] = telegram_id if state["bowl_team_id"] == team else None


def _schedule_pick(game, state, now):
    stage = state.get("event_next_action")
    if stage == A_INNINGS_BREAK:
        seconds = 8
    elif stage == picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]:
        seconds = int(os.getenv("CIPL_TIMEOUT_SECONDS", "120"))
    else:
        game.next_pick_at = None
        return
    game.next_pick_at = now + timedelta(seconds=seconds)



def _finish(db, game, entry, config, kind, now, *, reason=None):
    if db.get(StudioResult, game.id) is not None:
        return
    user_game = entry.team_key in (game.home, game.away)
    winner = None
    detail = {"reason": reason} if reason else {}
    if kind == "completed":
        winner, detail = cricket.match_result(game.state)
    elif kind == "forfeited":
        winner = game.away if game.home == entry.team_key else game.home
    if user_game and kind in ("completed", "success", "failed"):
        detail["performance"] = progress.contribution(game.state, entry.team_key)
    if user_game and kind in ("completed", "success", "failed"):
        detail.update(cricket.objectives(rules.fixture_rules(config, game.fixture_key),
                                         game.state, entry.team_key, kind, winner))
        if game.state.get("event_moment"):
            detail["continuation"] = cricket.view(game.state, entry.team_key)["moment"]
    result = StudioResult(game_id=game.id, kind=kind, winner=winner, detail=detail, recorded_at=now)
    db.add(result)
    game.status = kind
    game.next_pick_at = None
    game.version += 1
    if user_game and _ladder(config) and winner != entry.team_key:
        current = _fixture(config, game.fixture_key)
        later = {row["key"] for row in config["fixtures"] if row["round"] > current["round"]}
        if later:
            future = db.scalars(select(StudioGame).options(defer(StudioGame.state))
                               .where(StudioGame.enrollment_id == entry.id,
                                      StudioGame.fixture_key.in_(later), StudioGame.status == "scheduled")
                               .order_by(StudioGame.id).with_for_update()).all()
            for fixture in future:
                fixture.status = "cancelled"
                fixture.version += 1
                db.add(StudioResult(game_id=fixture.id, kind="cancelled", winner=None,
                                    detail={"reason": "eliminated"}, recorded_at=now))
    if not user_game or kind not in ("completed", "success"):
        return
    outcome = ("win" if winner == entry.team_key else "loss" if winner else "tie")
    close = db.get(StudioEvent, entry.event_id).closes_at
    expires = close + timedelta(days=config["claim_days"])
    db.flush()
    community = config["community"]
    xp_eligible = config["kind"] == "live" or kind == "success" or outcome == "win"
    if community["completion_xp"] and xp_eligible:
        studio.lock(db, StudioEnrollment, entry.id)
        existing = db.scalar(select(StudioXPJob.id).filter_by(enrollment_id=entry.id, source=game.fixture_key))
        if existing is None:
            db.add(StudioXPJob(game_id=game.id, enrollment_id=entry.id, source=game.fixture_key,
                               amount=community["completion_xp"], daily_limit=community["daily_limit"],
                               due_at=now))
    if config["kind"] == "live" or kind == "success" or outcome == "win":
        source_outcome = outcome if config["kind"] == "live" else "success"
        reward_config = rules.fixture_rules(config, game.fixture_key)["rewards"]
        bundle = reward_config.get(game.tier, {}).get(source_outcome, {})
        _entitle(db, entry, game, game.fixture_key, bundle, expires, track_zero=config["kind"] != "live")
    if config["milestones"]:
        studio.lock(db, StudioEnrollment, entry.id)
        earned_stars = _stars(db, entry, config)
        for row in config["milestones"]:
            if earned_stars >= row["stars"]:
                _entitle(db, entry, game, f'milestone:{row["stars"]}', row.get(game.tier, {}), expires,
                         track_zero=True)


def _entitle(db, entry, game, source, bundle, expires, *, track_zero=False):
    bundle = {asset: amount for asset, amount in bundle.items() if amount}
    if not bundle and not track_zero:
        return
    previous = db.scalar(select(StudioEntitlement.id).filter_by(enrollment_id=entry.id, source=source))
    if previous is None:
        db.add(StudioEntitlement(enrollment_id=entry.id, source=source, game_id=game.id, tier=game.tier,
                                 assets=bundle, expires_at=expires))


def _stars(db, entry, config):
    value = StudioResult.detail["stars"].as_integer()
    total = func.sum(value) if config["kind"] == "live" else func.max(value)
    return db.scalar(select(total).select_from(StudioResult)
                     .join(StudioGame, StudioGame.id == StudioResult.game_id)
                     .where(StudioGame.enrollment_id == entry.id)) or 0


def _expire(db, game, entry, config, now):
    if game.started_at is not None:
        kind = "forfeited" if config["kind"] == "live" else "expired"
        reason = "grace_expired" if game.due_at > game.deadline else "deadline"
    else:
        kind, reason = "forfeited", "deadline"
    _finish(db, game, entry, config, kind, now, reason=reason)


def _apply_pick(db, game, entry, config, state, kind, value, clock=None, *, now=None):
    stage = state.get("event_next_action")
    if kind == "continue" and stage != A_INNINGS_BREAK:
        raise ValueError("The innings break is already over.")
    selected = None if kind == "continue" else picks.validate(state, stage, kind, value)
    try:
        if kind == "continue":
            cricket.prepare_pick(state, game.seed, game.step, entry.team_key)
            terminal = None
        else:
            terminal = cricket.apply_pick(state, game.seed, game.step, entry.team_key, kind, selected)
            if (state.get("event_next_action") == A_INNINGS_BREAK
                    and state.get("play_mode", "chat") == "chat"):
                cricket.prepare_pick(state, game.seed, game.step + 1, entry.team_key)
    except Exception as exc:
        game.failure_at = now or _time(clock)
        game.failure_type = type(exc).__name__[:90]
        game.next_pick_at = None
        logger.exception("Official Events Studio game %s could not advance", game.id)
        raise studio.ExecutionUnavailable(
            "Cricket could not continue from this checkpoint. Your official match is saved.") from exc
    game.failure_at = None
    game.failure_type = None
    now = now or _time(clock)
    if not terminal and now > game.due_at:
        _expire(db, game, entry, config, now)
        return
    game.state = state
    if (kind not in (picks.PICK_BOWLER, "continue") and
            (terminal or state.get("event_next_action") != picks.PICK_ACTION[picks.PICK_NEW_BATSMAN])):
        game.step += 1
    if terminal:
        _finish(db, game, entry, config, terminal, now)
    else:
        game.version += 1
        _schedule_pick(game, state, now)


def _checkpoint(game, entry, user=None):
    state = deepcopy(game.state)
    if not state.get("event_next_action"):
        state.setdefault("play_mode", "chat")
        # Existing official saves were all at over boundaries.
        cricket.prepare_pick(state, game.seed, game.step, entry.team_key)
    if user is not None:
        _bind_actor(state, entry.team_key, user.telegram_id)
    return state


def submit_pick(db, user, game_id, version, kind, value, *, source, clock=None):
    game, entry, config = _ownership(db, user.id, game_id)
    if game.status != "in_progress":
        raise studio.Conflict("This official game has ended.")
    now = _time(clock)
    if now > game.due_at:
        if game.failure_at:
            raise studio.ExecutionUnavailable("The verified engine failure is awaiting review. Your game is saved.")
        _expire(db, game, entry, config, now)
        return game
    if version != game.version:
        raise studio.Conflict("This turn changed. Reload before choosing again.")
    state = _checkpoint(game, entry, user)
    if state.get("event_paused"):
        raise ValueError("Resume the official match before making a pick.")
    if state.get("play_mode", "chat") != source:
        raise ValueError("This match is being played on the other surface. Switch play location to continue here.")
    _apply_pick(db, game, entry, config, state, kind, value, clock)
    return game


def change_batting(db, user, game_id, version, kind, data, *, source, clock=None):
    from services import live_batting
    game, entry, config = _ownership(db, user.id, game_id)
    if game.status != "in_progress" or version != game.version:
        raise studio.Conflict("This official match changed. Reload before continuing.")
    now = _time(clock)
    if now > game.due_at:
        if game.failure_at:
            raise studio.ExecutionUnavailable("The verified engine failure is awaiting review. Your game is saved.")
        _expire(db, game, entry, config, now)
        return game
    state = _checkpoint(game, entry, user)
    if (state.get("event_paused") or state.get("play_mode") != source
            or state["bat_team_id"] != entry.team_key):
        raise ValueError("Only the batting captain can change batting on the active play surface.")
    stage = state["event_next_action"]
    if kind == "retire_batsman":
        live_batting.retire(state, stage, data.get("outRosterId"), data.get("inRosterId"), data.get("token"))
    else:
        live_batting.reorder(state, stage, data.get("rosterIds"), data.get("token"))
    game.state = state
    game.version += 1
    return game


def use_impact(db, user, game_id, version, incoming, outgoing, position, *, source, clock=None):
    from services import impact_player
    game, entry, config = _ownership(db, user.id, game_id)
    if game.status != "in_progress" or version != game.version:
        raise studio.Conflict("This official match changed. Reload before continuing.")
    now = _time(clock)
    if now > game.due_at:
        if game.failure_at:
            raise studio.ExecutionUnavailable("The verified engine failure is awaiting review. Your game is saved.")
        _expire(db, game, entry, config, now)
        return game
    state = _checkpoint(game, entry, user)
    if (not state.get("event_impact") or state.get("event_paused") or source != state.get("play_mode")
            or state.get("ball_format") != "T20"):
        raise ValueError("Impact Player is unavailable in this match or at this stage.")
    if type(incoming) is not int or type(outgoing) is not int or (
            position is not None and type(position) is not int):
        raise ValueError("Choose an eligible Impact Player.")
    ok, message, _ = impact_player.cipl_use(state, entry.team_key, incoming, outgoing,
                                            state["event_next_action"], position)
    if not ok:
        raise ValueError(message)
    game.state = state
    game.version += 1
    return game


def set_play_state(db, user, game_id, version, action, source, clock=None):
    game, entry, config = _ownership(db, user.id, game_id)
    if game.status != "in_progress" or version != game.version:
        raise studio.Conflict("This official match changed. Reload before continuing.")
    now = _time(clock)
    if now > game.due_at:
        if game.failure_at:
            raise studio.ExecutionUnavailable("The verified engine failure is awaiting review. Your game is saved.")
        _expire(db, game, entry, config, now)
        return game
    state = _checkpoint(game, entry, user)
    if action == "pause":
        if state.get("play_mode", "chat") != source:
            raise ValueError("Pause the match where you are playing it.")
        state["event_paused"] = True
        game.next_pick_at = None
    elif action in ("resume", "switch"):
        state.update(event_paused=False, play_mode=source)
        _schedule_pick(game, state, now)
    else:
        raise ValueError("Invalid play action.")
    game.state = state
    game.version += 1
    return game


def abandon(db, user, game_id, version, clock=None):
    game, entry, config = _ownership(db, user.id, game_id)
    if game.status != "in_progress":
        return game
    if version != game.version:
        raise studio.Conflict("This match changed. Confirm again after reloading.")
    now = _time(clock)
    if now > game.due_at:
        _expire(db, game, entry, config, now)
    else:
        kind = "forfeited" if config["kind"] == "live" else "abandoned"
        _finish(db, game, entry, config, kind, now, reason="abandon")
    return game


def claim(db, user, event_id, reward_id, now):
    entry = _enrollment(db, event_id, user.id)
    if not entry:
        raise ValueError("Reward is unavailable.")
    wallet = lock_user_for_update(db, user)
    stmt = select(StudioEntitlement).filter_by(enrollment_id=entry.id)
    if reward_id is not None:
        stmt = stmt.where(StudioEntitlement.id == reward_id)
    rows = db.scalars(stmt.order_by(StudioEntitlement.id).with_for_update()).all()
    if reward_id is not None and (not rows or rows[0].expires_at <= now and not rows[0].claimed_at):
        raise ValueError("Reward is unavailable or expired.")
    for reward in rows:
        if not reward.assets or reward.claimed_at or reward.expires_at <= now:
            continue
        for asset, amount in reward.assets.items():
            field = rules.ASSETS[asset]
            total = (getattr(wallet, field) or 0) + amount
            if total > 2_147_483_647:
                raise ValueError("This reward would exceed wallet capacity.")
            setattr(wallet, field, total)
        log = log_activity(db, wallet.id, "event_reward",
                           f"Event {event_id}, reward {reward.id}, Quest Points +{reward.assets.get('quest_points', 0)}",
                           coins_change=reward.assets.get("coins", 0), gems_change=reward.assets.get("gems", 0))
        db.flush()
        reward.claimed_at = now
        reward.ledger_id = log.id
    return rows


def table(config, rows):
    standings = {key: {"team": key, "name": data["name"], "points": 0, "played": 0,
                       "won": 0, "lost": 0, "tied": 0, "no_result": 0, "forfeited": 0,
                       "scored": 0, "faced": 0, "conceded": 0, "bowled": 0}
                 for key, data in config["snapshot"].items()}
    league = {f["key"] for f in config["fixtures"] if f["stage"] == "league"}
    innings_limit = rules.FORMATS[config["format"]]["overs"] * 6
    for game, result in rows:
        if game.fixture_key not in league or result is None or result.kind == "cancelled":
            continue
        for key, opponent in ((game.home, game.away), (game.away, game.home)):
            side = standings[key]
            side["played"] += 1
            if result.winner == key:
                side["won"] += 1
                side["points"] += config["points"]["win"]
            elif result.winner:
                side["lost"] += 1
                side["forfeited"] += result.kind == "forfeited"
                if result.kind != "forfeited":
                    side["points"] += config["points"]["loss"]
            else:
                outcome = "tie" if result.kind == "completed" else "no_result"
                side["tied" if outcome == "tie" else outcome] += 1
                side["points"] += config["points"][outcome]
            innings = {part["team"]: part for part in result.detail.get("innings", [])}
            if innings:
                batting, bowling = innings[key], innings[opponent]
                side["scored"] += batting["runs"]
                side["conceded"] += bowling["runs"]
                side["faced"] += innings_limit if batting["wickets"] == 10 else batting["balls"]
                side["bowled"] += innings_limit if bowling["wickets"] == 10 else bowling["balls"]
    for side in standings.values():
        side["nrr"] = (6 * (side["scored"] / side["faced"] - side["conceded"] / side["bowled"])
                       if side["faced"] and side["bowled"] else 0)
    return sorted(standings.values(), key=lambda row: (-row["points"], -row["won"], -row["nrr"]))


def _game_rows(db, entry):
    return db.execute(select(StudioGame, StudioResult).options(defer(StudioGame.state), defer(StudioGame.seed))
                      .outerjoin(StudioResult, StudioResult.game_id == StudioGame.id)
                      .where(StudioGame.enrollment_id == entry.id).order_by(StudioGame.opens_at, StudioGame.id)).all()


def _resolve(config, row, rows):
    previous = {game.fixture_key: (game, result) for game, result in rows}
    order = [x["team"] for x in table(config, rows)]
    league = [item for item in config["fixtures"] if item["stage"] == "league"]
    seed_order = order if league else list(config["snapshot"])
    resolved = []
    for slot in (row["home"], row["away"]):
        if ":" not in slot:
            resolved.append(slot)
            continue
        kind, dependency = slot.split(":", 1)
        if kind in ("rank", "group_rank"):
            if any(not previous[item["key"]][1] for item in league):
                return None
            if kind == "rank":
                resolved.append(order[int(dependency) - 1])
            else:
                group, rank = dependency.split(":")
                group_order = [key for key in order if key in config["schedule"]["groups"][group]]
                resolved.append(group_order[int(rank) - 1])
            continue
        game, result = previous[dependency]
        if result is None:
            return None
        winner = result.winner or min((game.home, game.away), key=seed_order.index)
        loser = game.away if game.home == winner else game.home
        resolved.append(winner if kind == "winner" else loser)
    if len(set(resolved)) != 2:
        raise ValueError("Playoff dependencies resolved to the same team.")
    return tuple(resolved)


def run_due(db, game_id, now, *, over_budget=8):
    game = studio.lock(db, StudioGame, game_id, skip_locked=True)
    if (game is None or game.status not in PENDING
            or (game.due_at >= now and (game.next_pick_at is None or game.next_pick_at > now))
            or game.retry_at and game.retry_at > now):
        return
    game.retry_at = None
    entry = db.get(StudioEnrollment, game.enrollment_id)
    event, config = _published(db, entry.event_id)
    if event.status not in ("scheduled", "live", "closing"):
        return
    if game.status == "locked":
        slots = _resolve(config, _fixture(config, game.fixture_key), _game_rows(db, entry))
        if slots is None:
            game.due_at = now + timedelta(seconds=30)
            return
        game.home, game.away = slots
        game.status = "scheduled"
        game.due_at = game.deadline if entry.team_key in slots else game.opens_at
        if entry.team_key in slots:
            notices.enqueue(db, game, config, now)
    if entry.team_key in (game.home, game.away):
        if _ladder(config) and _earlier_pending(db, entry, config, _fixture(config, game.fixture_key)):
            game.retry_at = now + timedelta(seconds=20)
            return
        if game.due_at < now:
            if game.failure_at:
                game.retry_at = now + timedelta(minutes=2)
            else:
                _expire(db, game, entry, config, now)
        elif game.status == "in_progress" and game.next_pick_at and game.next_pick_at <= now:
            state = _checkpoint(game, entry)
            if state.get("event_next_action") == A_INNINGS_BREAK:
                kind, value = "continue", 0
            else:
                kind = picks.PICK_NEW_BATSMAN
                value = cricket.match.available_batsmen(state)[0]["roster_id"]
            try:
                _apply_pick(db, game, entry, config, state, kind, value, now=now)
            except studio.ExecutionUnavailable:
                return
            if state.get("play_mode", "chat") == "chat":
                return entry.user_id, entry.event_id, game.version
        return
    if game.status not in ("scheduled", "simulating"):
        return
    maximum = 2 * rules.FORMATS[config["format"]]["overs"]
    if game.status == "scheduled":
        game.seed = token_hex(32)
        xis = {key: config["snapshot"][key]["default_xi"] for key in (game.home, game.away)}
        state = cricket.build_state(rules.fixture_rules(config, game.fixture_key),
                                    game.id, game.home, game.away, xis, game.seed)
    else:
        state = deepcopy(game.state)
    for step in range(game.step, min(maximum, game.step + over_budget)):
        terminal = cricket.advance(state, game.seed, step)
        game.failure_at = None
        game.failure_type = None
        game.step = step + 1
        if terminal == "completed":
            game.state = state
            _finish(db, game, entry, config, "completed", now)
            return
    if game.step >= maximum:
        raise RuntimeError("AI simulation did not reach a final result.")
    game.state = state
    game.status = "simulating"
    # Give other due fixtures an opportunity between bounded over batches.
    game.due_at = now


def due_ids(db, now, limit=12):
    priority = case(
        (StudioGame.status == "in_progress", 0),
        (or_(StudioGame.home == StudioEnrollment.team_key, StudioGame.away == StudioEnrollment.team_key), 0),
        else_=1,
    )
    return db.scalars(select(StudioGame.id)
                      .join(StudioEnrollment, StudioGame.enrollment_id == StudioEnrollment.id)
                      .join(StudioEvent, StudioEnrollment.event_id == StudioEvent.id)
                      .where(StudioEvent.status.in_(("scheduled", "live", "closing")),
                             StudioGame.status.in_(PENDING),
                             or_(StudioGame.due_at < now, StudioGame.next_pick_at <= now),
                             or_(StudioGame.retry_at.is_(None), StudioGame.retry_at <= now))
                      .order_by(priority, StudioGame.due_at, StudioGame.id).limit(limit)).all()


def sync_lifecycle(db, now):
    db.execute(update(StudioEvent).where(StudioEvent.status == "scheduled",
                                        StudioEvent.opens_at <= now).values(status="live"))
    db.execute(update(StudioEvent).where(StudioEvent.status == "live",
                                        StudioEvent.closes_at <= now).values(status="closing"))
    pending = select(StudioEnrollment.event_id).join(StudioGame, StudioGame.enrollment_id == StudioEnrollment.id)
    db.execute(update(StudioEvent).where(StudioEvent.status == "closing",
                                        StudioEvent.id.not_in(pending.where(StudioGame.status.in_(PENDING))))
               .values(status="completed"))


def game_view(game, result, human, now, config, *, paused=False, track="tournament_team"):
    row = _fixture(config, game.fixture_key)
    names = config["snapshot"]
    personal = human in (game.home, game.away)
    cutoff = game.due_at if game.status == "in_progress" else game.deadline
    under_review = game.status == "in_progress" and game.failure_at and now > game.due_at
    player_paused = game.status == "in_progress" and (game.state or {}).get("event_paused")
    actual_status = ("paused" if (paused and game.status == "in_progress") or player_paused else
                     "under_review" if under_review else
                     "grace" if game.status == "in_progress" and now > game.deadline else game.status)
    # Every fixture carries the same keys whatever its status, so a caller can read
    # result, score or scouting without first asking whether this one has them. A
    # fixture that is scheduled, locked behind a playoff slot, or expired has none.
    view = {"id": game.id, "key": game.fixture_key, "attempt": game.attempt, "status": actual_status,
            "home": names[game.home]["name"] if game.home else None,
            "away": names[game.away]["name"] if game.away else None,
            "opens_at": schedule.utc_text(game.opens_at), "cutoff": schedule.utc_text(cutoff),
            "deadline": schedule.utc_text(game.deadline),
            "result": None, "score": None, "scouting": None,
            "version": game.version, "personal": personal,
            "player_paused": bool(player_paused), "event_paused": paused,
            "play_mode": (game.state or {}).get("play_mode", "chat"), "round": row["round"] if row else None,
            "stage": row["stage"] if row else None,
            "can_start": (personal and not paused and game.status == "scheduled" and game.opens_at <= now < game.deadline
                          and studio.allowed(config["kind"], config["format"]))}
    if personal and game.status in PENDING:
        opponent = game.away if game.home == human else game.home
        squad = _opponent_xi(names[opponent], rules.fixture_rules(config, game.fixture_key), track)
        assignments = rules.fixture_rules(config, game.fixture_key)["objectives"]
        view["scouting"] = {
            "opponent": names[opponent]["name"],
            "average_rating": round(sum(p["rating"] for p in squad) / len(squad), 1),
            "xi": [{"name": p["name"], "rating": p["rating"], "role": p["category"]} for p in squad],
            "objectives": [{**row, "title": rules.OBJECTIVES[row["key"]][0]} for row in assignments],
        }
    if personal and game.status == "in_progress" and game.state:
        state = game.state
        if not state.get("event_next_action"):
            state = deepcopy(state)
            cricket.prepare_pick(state, game.seed, game.step, human)
        view["score"] = cricket.view(state, human)
        view["score"]["objectives"] = cricket.objective_progress(
            rules.fixture_rules(config, game.fixture_key), game.state, human)
        if paused or player_paused or under_review:
            view["score"]["choices"] = None
    if result:
        view["result"] = {"kind": result.kind, "winner": names[result.winner]["name"] if result.winner else None,
                          "outcome": "win" if result.winner == human else "loss" if result.winner else result.kind,
                          **{key: value for key, value in result.detail.items() if key != "performance"}}
        if personal and config["kind"] == "live" and result.kind in ("completed", "forfeited"):
            outcome = "tie" if result.winner is None else view["result"]["outcome"]
            view["result"]["points"] = 0 if result.kind == "forfeited" else config["points"][outcome]
    return view


def owned_game(db, user, game_id):
    return db.execute(select(StudioGame, StudioEnrollment).join(
        StudioEnrollment, StudioGame.enrollment_id == StudioEnrollment.id)
        .where(StudioGame.id == game_id, StudioEnrollment.user_id == user.id,
               (StudioGame.home == StudioEnrollment.team_key) | (StudioGame.away == StudioEnrollment.team_key))
    ).one_or_none()


def arena(db, user, game_id, now):
    from services.crickidex_arena import serialize_event_match
    from services.event_media_service import get_miniapp_event_gifs
    from services.event_sound_service import get_miniapp_event_sounds

    entry = db.scalar(select(StudioEnrollment).join(StudioGame, StudioGame.enrollment_id == StudioEnrollment.id)
                      .where(StudioGame.id == game_id, StudioEnrollment.user_id == user.id))
    if entry is None:
        raise ValueError("Match is unavailable.")
    game = db.get(StudioGame, game_id, populate_existing=True)
    if not game.state or entry.team_key not in (game.home, game.away):
        raise ValueError("Match is unavailable.")
    event = db.get(StudioEvent, entry.event_id)
    result = db.get(StudioResult, game.id)
    config = db.get(StudioRevision, (entry.event_id, entry.revision)).config
    details = game_view(game, result, entry.team_key, now, config, paused=event.status == "paused", track=entry.mode)
    overlay = {key: details[key] for key in (
        "status", "cutoff", "version", "round", "attempt", "player_paused", "event_paused", "play_mode")}
    overlay.update(name=event.name, id=event.id, game_id=game.id, deadline=schedule.utc_text(game.deadline),
                   difficulty=cricket.difficulty(rules.fixture_rules(config, game.fixture_key), entry.mode),
                   venue=game.state.get("stadium"), weather=(game.state.get("conditions") or {}).get("weather"),
                   objectives=(details.get("score") or details.get("result") or {}).get("objectives", []),
                   moment=(details.get("score") or {}).get("moment"), result=details.get("result"))
    if result:
        if config["kind"] == "live":
            rows = _game_rows(db, entry)
            if "points" in details["result"]:
                overlay["points"] = details["result"]["points"]
            upcoming = next((fixture for fixture, _ in rows if fixture.status == "scheduled" and
                             entry.team_key in (fixture.home, fixture.away)), None)
            if upcoming:
                other = upcoming.away if upcoming.home == entry.team_key else upcoming.home
                overlay["next_fixture"] = {"opponent": config["snapshot"][other]["name"],
                                           "opens_at": schedule.utc_text(upcoming.opens_at)}
        overlay["reward_eligible"] = bool(db.scalar(select(StudioEntitlement.id).where(
            StudioEntitlement.enrollment_id == entry.id, StudioEntitlement.game_id == game.id)))
    state = (_checkpoint(game, entry, user) if not result and not game.state.get("event_next_action")
             else game.state)
    payload = serialize_event_match(db, game, entry, event, user, result, overlay, state=state)
    payload["eventGifs"] = get_miniapp_event_gifs(db)
    payload["eventSounds"] = get_miniapp_event_sounds(db)
    return payload


def reward_view(reward, now):
    return {"id": reward.id, "game_id": reward.game_id, "source": reward.source, "assets": reward.assets, "tier": reward.tier,
            "expires_at": schedule.utc_text(reward.expires_at),
            "status": "claimed" if reward.claimed_at else "expired" if reward.expires_at <= now else "claimable",
            "receipt": reward.id if reward.claimed_at else None}


def detail(db, user, event_id, now):
    event, config = _published(db, event_id)
    entry = _enrollment(db, event.id, user.id)
    summary = event_summary(event, config, entry is not None, now)
    if entry is None and (event.status == "cancelled" or not studio.allowed(config["kind"], config["format"])):
        raise ValueError("Event is unavailable.")
    if entry is None:
        summary["chapter_lock"] = _chapter_lock(db, user.id, config)
        summary["can_join"] = summary["can_join"] and not bool(summary["chapter_lock"])
    summary["chapter"] = {key: config["chapter"][key] for key in ("name", "position")}
    summary["boss_goal"] = _boss_progress(db, event_id, config)
    names = config["snapshot"]
    keys = _team_keys(config) if config["kind"] == "live" and not _ladder(config) else _team_keys(config)[:1]
    challenge = config.get("moment_state", {}).get("event_moment")
    preview_rules = rules.fixture_rules(config, "challenge") if config["kind"] != "live" else config
    summary.update(rules={
        "ties": "Ties count as ties. The higher seed advances in a tied knockout.",
        "attempts": ("Win each round to advance. A tie, loss or forfeit ends the run." if _ladder(config)
                     else "Every fixture has one official result." if config["kind"] == "live"
                     else "Each committed start uses one attempt. You can resume it before its cutoff."),
        "tier": "Rewards use the active tier at launch. The cricket rules are the same across tiers.",
    }, modes=config["mode"], teams=[{"key": key, "name": names[key]["name"]} for key in keys],
                   claim_days=config["claim_days"], rewards_preview=preview_rules["rewards"], survival=_ladder(config),
                   milestones=config["milestones"], community_xp=config["community"]["completion_xp"],
                   # A Live Event's ground is per fixture, so there is no one pitch, venue or
                   # weather to advertise here; each fixture reports its own in setup_fixture.
                   enrollment_cutoff=schedule.utc_text(_cutoff(event, config)),
                   pitch=None if config["kind"] == "live" else config["pitch"],
                   venue=None if config["kind"] == "live" else config.get("venue"),
                   weather=None if config["kind"] == "live" else config.get("weather"),
                   difficulty=config.get("difficulty"),
                   attempt_limit=config["attempts"] if config["kind"] != "live" else None,
                   objectives=[{**row, "title": rules.OBJECTIVES[row["key"]][0]} for row in preview_rules["objectives"]],
                   moment=({key: challenge[key] for key in ("side", "goal", "start_over", "start_runs",
                                                           "start_wickets", "overs", "goal_type", "target")} if challenge else None))
    if entry is None:
        return summary
    rows = _game_rows(db, entry)
    rewards = db.scalars(select(StudioEntitlement).filter_by(enrollment_id=entry.id)
                         .order_by(StudioEntitlement.id))
    team = names[entry.team_key]
    roster = entry.roster if entry.mode == "own_roster" else team["squad"]
    standings = table(config, rows) if config["kind"] == "live" and not _ladder(config) else []
    summary.update(team=team["name"], mode=entry.mode, trophy=progress.trophy(config, entry, rows, standings),
                   games=[game_view(game, result, entry.team_key, now, config, paused=event.status == "paused", track=entry.mode)
                          for game, result in rows],
                   table=standings,
                   rewards=[reward_view(row, now) for row in rewards if row.assets],
                   best_stars=max((row.detail.get("stars", 0) for _, row in rows if row), default=0),
                   total_stars=(sum(row.detail.get("stars", 0) for _, row in rows if row)
                                if config["kind"] == "live" else
                                max((row.detail.get("stars", 0) for _, row in rows if row), default=0)),
                   roster=[{key: p[key] for key in ("roster_id", "name", "version", "rating", "category")} for p in roster],
                   default_xi=([p["roster_id"] for p in team["default_xi"]] if entry.mode == "tournament_team" else []),
                   fixed_xi=(rules.is_moment(config) or entry.mode == "tournament_team" and "xi" in team))
    # Always present, so a panel can read them without branching on the event kind.
    summary.setdefault("attempts_used", 0)
    summary.setdefault("can_attempt", False)
    if config["kind"] != "live":
        attempts_used = sum(game.status != "technical_void" for game, _ in rows)
        summary["attempts_used"] = attempts_used
        summary["can_attempt"] = (event.status in studio.OPEN_STATUSES
                                  and now < event.closes_at - _grace(config, "challenge")
                                  and studio.allowed(config["kind"], config["format"])
                                  and attempts_used < config["attempts"]
                                  and not any(game.status == "in_progress" for game, _ in rows))
    return summary


def scorecard(db, user, game_id):
    entry = db.scalar(select(StudioEnrollment).join(StudioGame, StudioGame.enrollment_id == StudioEnrollment.id)
                      .where(StudioGame.id == game_id, StudioEnrollment.user_id == user.id))
    game = db.get(StudioGame, game_id) if entry else None
    if not game or not game.state or game.status in UNPLAYED:
        raise ValueError("This game has no scorecard.")
    from services.match_webapp_service import approach_scorecard
    return approach_scorecard(game.state)


def operate(db, event, action, actor, reason, now):
    """Log bounded lifecycle operations without rewriting an official result."""
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ValueError("Enter an operations reason of up to 500 characters.")
    previous = event.status
    before = {"status": previous, "opens_at": schedule.utc_text(event.opens_at),
              "closes_at": schedule.utc_text(event.closes_at)}
    game_ids = (select(StudioGame.id)
                .join(StudioEnrollment, StudioGame.enrollment_id == StudioEnrollment.id)
                .where(StudioEnrollment.event_id == event.id, StudioGame.status.in_(PENDING))
                .order_by(StudioGame.id))
    if action == "pause" and previous in studio.OPEN_STATUSES and now < event.closes_at:
        # Wait for any currently committing over or AI calculation before the
        # pause takes effect. Acquiring all game locks first avoids stale starts.
        db.execute(game_ids.with_for_update(of=StudioGame)).all()
        event.paused_at = now
        event.status = "paused"
    elif action == "resume" and previous == "paused" and event.paused_at:
        seconds = max(0, int((now - event.paused_at).total_seconds()))
        shift = timedelta(seconds=seconds)
        config = studio.revision(db, event, published=True).config
        for game_id in db.scalars(game_ids).all():
            game = studio.lock(db, StudioGame, game_id)
            if game.status not in PENDING:
                continue
            game.opens_at += shift
            game.deadline += shift
            game.due_at += shift
            if game.next_pick_at:
                game.next_pick_at += shift
            game.retry_at = None
            game.version += 1
            if game.status != "locked" and game.started_at is None:
                entry = db.get(StudioEnrollment, game.enrollment_id)
                if entry.team_key in (game.home, game.away):
                    notices.enqueue(db, game, config, now)
        event.opens_at += shift
        event.closes_at += shift
        event.delay_seconds += seconds
        event.paused_at = None
        event.status = "scheduled" if now < event.opens_at else "live"
    elif action == "cancel" and previous in ("scheduled", "live", "paused", "closing"):
        pending = db.scalars(select(StudioGame).options(defer(StudioGame.state), defer(StudioGame.seed))
                             .where(StudioGame.id.in_(game_ids)).order_by(StudioGame.id).with_for_update()).all()
        for game in pending:
            game.status = "cancelled"
            game.version += 1
            db.add(StudioResult(game_id=game.id, kind="cancelled", winner=None,
                                detail={"reason": "event_cancelled"}, recorded_at=now))
        event.status = "cancelled"
    elif action == "retry" and previous in ("scheduled", "live", "closing"):
        db.execute(update(StudioGame).where(StudioGame.id.in_(game_ids), StudioGame.retry_at.is_not(None))
                   .values(retry_at=None).execution_options(synchronize_session=False))
    elif action == "archive" and previous == "completed":
        event.status = "archived"
    else:
        raise ValueError("This operation is unavailable in the current event state.")
    db.add(StudioAudit(event_id=event.id, actor=actor, action=action,
                       detail={"reason": reason, "before": before,
                               "after": {"status": event.status,
                                         "opens_at": schedule.utc_text(event.opens_at),
                                         "closes_at": schedule.utc_text(event.closes_at)}}))


def progression(db, user, event_id):
    _, config = _published(db, event_id)
    entry = _enrollment(db, event_id, user.id)
    if entry is None:
        raise ValueError("Join this event to see track progression.")
    return {"leaderboard": progress.leaderboard(db, entry, config),
            "stats": progress.player_stats(_game_rows(db, entry))}


def extend_deadline(db, event, game_id, deadline, actor, reason, now):
    """Move one unresolved fixture within its event and dependency windows."""
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ValueError("Enter an extension reason of up to 500 characters.")
    if event.status not in ("scheduled", "live", "paused"):
        raise ValueError("Deadline extensions require an open or paused event.")
    entry = db.scalar(select(StudioEnrollment).join(StudioGame)
                      .where(StudioGame.id == game_id, StudioEnrollment.event_id == event.id))
    if entry is None:
        raise ValueError("The fixture is outside this event.")
    game = studio.lock(db, StudioGame, game_id)
    if game.status not in ("scheduled", "in_progress") or entry.team_key not in (game.home, game.away):
        raise ValueError("Only unresolved personal fixtures can be extended.")
    config = studio.revision(db, event, published=True).config
    grace = _grace(config, game.fixture_key)
    latest = event.closes_at
    row = _fixture(config, game.fixture_key)
    if row:
        future_keys = {item["key"] for item in config["fixtures"] if item["round"] > row["round"]}
        if future_keys:
            next_release = db.scalar(select(func.min(StudioGame.opens_at)).where(
                StudioGame.enrollment_id == entry.id, StudioGame.fixture_key.in_(future_keys)))
            if next_release:
                latest = min(latest, next_release)
    mandatory_now = event.paused_at if event.status == "paused" else now
    if deadline <= max(mandatory_now, game.deadline) or deadline + grace > latest:
        raise ValueError("The extension must move the deadline later and leave grace before the next round or event close.")
    previous = game.deadline
    game.deadline = deadline
    game.due_at = deadline + grace if game.started_at is not None else deadline
    game.retry_at = None
    game.version += 1
    if game.started_at is None:
        notices.enqueue(db, game, config, now)
    db.add(StudioAudit(event_id=event.id, actor=actor, action="extend", detail={
        "reason": reason, "game_id": game.id, "before": schedule.utc_text(previous),
        "after": schedule.utc_text(deadline),
    }))


def resolve_technical_failure(db, event, game_id, actor, reason, now):
    """Close verified failed official state without minting an innings or reward."""
    reason = reason.strip()
    if not reason or len(reason) > 500:
        raise ValueError("Enter the verified incident reason, up to 500 characters.")
    if event.status not in ("scheduled", "live", "paused", "closing"):
        raise ValueError("The event must still be open or paused.")
    entry = db.scalar(select(StudioEnrollment).join(StudioGame)
                      .where(StudioGame.id == game_id, StudioEnrollment.event_id == event.id))
    if entry is None:
        raise ValueError("The official game is outside this event.")
    game = studio.lock(db, StudioGame, game_id)
    if game.status not in ("in_progress", "simulating") or game.failure_at is None:
        raise ValueError("This game has no verified unresolved engine failure.")
    config = studio.revision(db, event, published=True).config
    status = "no_result" if config["kind"] == "live" else "technical_void"
    _finish(db, game, entry, config, status, now, reason="verified_engine_failure")
    db.add(StudioAudit(event_id=event.id, actor=actor, action=status, detail={
        "reason": reason, "game_id": game.id, "failure_type": game.failure_type,
        "failure_at": schedule.utc_text(game.failure_at),
    }))
