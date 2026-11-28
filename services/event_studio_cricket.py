"""Official match adapter around the shared Approach batting and bowling loop."""

from copy import deepcopy
from typing import TypedDict
from hashlib import sha256
import hmac
import random

from engine import match_random
from engine.approach_modifiers import BATTING_APPROACHES, BOWLING_APPROACHES
from services import bot_captain as captain, cipl_match as match, cipl_picks as picks, pitch_report
from services.match_state_store import A_INNINGS_BREAK
from services.event_studio_rules import FORMATS, evaluate


class EventMatchContext(TypedDict):
    event_id: int
    enrollment_id: int
    game_id: int
    fixture: str
    attempt: int
    revision: int
    squad_keys: list[str]
    track: str
    difficulty: str
    format: str
    deadline: str
    grace_until: str
    reward_tier: str
    statistics: str


def difficulty(config, mode):
    return config.get("difficulty") or ("hard_plus" if mode == "own_roster" else "hard")


def pin_difficulty(state, config, mode):
    level = difficulty(config, mode)
    state["bot_difficulty"] = "hard" if level == "hard_plus" else level
    state["event_ai_policy"] = "hard-plus-v1" if level == "hard_plus" else f"{level}-v1"


def rng_for(seed, label):
    material = hmac.digest(bytes.fromhex(seed), label.encode(), "sha256")
    return random.Random(int.from_bytes(material))


def build_state(config, game_id, home, away, xis, seed, *, human=None, mode=None, batting=None,
                toss_winner=None, toss_decision=None, benches=None):
    spec = FORMATS.get(config["format"])
    if not spec or config["engine"] != spec["engine"]:
        raise RuntimeError("This match requires an unavailable engine version.")
    rng = rng_for(seed, "toss")
    first = batting or rng.choice((home, away))
    second = away if first == home else home
    teams = config["snapshot"]
    benches = benches or {}
    conditions = None
    if config.get("weather"):
        conditions = pitch_report.generate_conditions(
            config["pitch"], rng_for(seed, "conditions"), weather=config["weather"])
        conditions["timeline"] = []
    with match_random.use(rng):
        state = match.build_cipl_state(
            game_id, spec["overs"], first, second, None, None, deepcopy(xis[first]), deepcopy(xis[second]),
            teams[first]["name"], teams[second]["name"], None, pitch_type=config["pitch"],
            stadium=config.get("venue") or None, conditions=conditions,
            bat_bench=deepcopy(benches.get(first)), bowl_bench=deepcopy(benches.get(second)),
            ball_format=config["format"], odi_settings=deepcopy(config.get("odi_settings")),
            rng_seed=seed)
        state.update(official_event=True, is_bot_match=True, bot_difficulty="hard",
                     event_engine_version=config["engine"], toss_batting=first,
                     ground_reference=config.get("ground_reference"), reference_hash=config["reference_hash"],
                     toss_winner=toss_winner or first, toss_decision=toss_decision or "bat",
                     stats_disabled=True, is_vsbot=True, event_impact=bool(benches),
                     xi_rules={"min_overseas": config.get("min_overseas", 0), "max_overseas": config.get("max_overseas", 11)})
        if config.get("realism_profile"):
            state["event_realism_profile"] = deepcopy(config["realism_profile"])
            state["event_realism_profile_hash"] = config["realism_profile_hash"]
        pin_difficulty(state, config, mode)
        opponent = (away if human == home else home) if human else second
        state["bot_user_id"] = opponent
        captain.assign_persona(state)
    return state


def public_choices(state, team):
    batting = state["bat_team_id"] == team
    stage = state.get("event_next_action")
    options = BATTING_APPROACHES if batting else BOWLING_APPROACHES
    return {
        "role": "bat" if batting else "bowl",
        "stage": stage,
        "bowler": (state.get("current_bowler") or {}).get("name"),
        "incoming": [{"id": p["roster_id"], "name": p["name"]} for p in match.available_batsmen(state)]
        if stage == picks.PICK_ACTION[picks.PICK_NEW_BATSMAN] else [],
        "approaches": [{"key": key, "label": label} for key, _, label in options],
        "bowlers": [] if batting else
        [{"id": p["roster_id"], "name": p["name"], "overs_left": match.overs_left(state, p)}
         for p in match.eligible_bowlers(state)],
    }


def _automated_choices(state):
    # AI-only fixtures use a bounded, observable phase and quota policy.
    bowlers = match.eligible_bowlers(state)
    phase = match.approach_phase(state)
    bowler = bowlers[0]
    chase = match.chase(state)
    chase_rate = 6.5 if state["ball_format"] == "ODI" else 9
    bat = "aggressive" if phase == "death" or (chase and chase["rrr"] > chase_rate) else "balanced"
    bowl = "variation" if phase == "death" else "aggressive" if phase == "powerplay" else "mixed"
    return bat, bowl, bowler


def prepare_pick(state, seed, step, human):
    """At an over boundary, commit the AI's bowler and private plan before prompting."""
    if state.get("event_pvp"):
        state["event_next_action"] = picks.PICK_ACTION[picks.PICK_BOWLER]
        return
    if state.get("event_impact"):
        from services import impact_player
        bot = state["bot_user_id"]
        stage = picks.PICK_ACTION[picks.PICK_BOWLER]
        swap = captain.pick_impact_swap(state, bot, stage)
        if swap:
            incoming, outgoing, position = swap
            impact_player.cipl_use(state, bot, incoming, outgoing, stage, position)
    if state["bat_team_id"] == human:
        with match_random.use(rng_for(seed, f"captain:{step}")):
            bowler = captain.pick_bowler(state)
            if bowler is None:
                raise RuntimeError("Official bowling capacity was exhausted.")
            state["current_bowler"] = bowler
            state["bowling_approach"] = captain.pick_bowling_approach(state)
        state["event_next_action"] = picks.PICK_ACTION[picks.PICK_BAT_APPROACH]
    else:
        state["event_next_action"] = picks.PICK_ACTION[picks.PICK_BOWLER]


def submit(state, seed, step, human, kind, value):
    selected = picks.validate(state, state.get("event_next_action"), kind, value)
    return apply_pick(state, seed, step, human, kind, selected)


def apply_pick(state, seed, step, human, kind, selected):
    if kind == picks.PICK_BOWLER:
        state["current_bowler"] = selected
        if not state.get("event_pvp"):
            with match_random.use(rng_for(seed, f"captain:{step}")):
                state["batting_approach"] = captain.pick_batting_approach(state)
        state["event_next_action"] = picks.PICK_ACTION[picks.PICK_BOWL_APPROACH]
        return None
    if kind == picks.PICK_NEW_BATSMAN:
        match.bring_in_batsman(state, selected["roster_id"])
    elif kind == picks.PICK_BAT_APPROACH:
        state["batting_approach"] = selected
    else:
        state["bowling_approach"] = selected
        if state.get("event_pvp"):
            state["event_next_action"] = picks.PICK_ACTION[picks.PICK_BAT_APPROACH]
            return None
    return resolve_over(state, seed, step, human=human, moment=state.get("event_moment"))


def resolve_over(state, seed, step, *, human=None, moment=None):
    spec = FORMATS.get(state["ball_format"])
    if not spec or state.get("event_engine_version") != spec["engine"]:
        raise RuntimeError("This match requires an unavailable engine version.")
    pause = human is not None and state["bat_team_id"] == human
    if state.get("event_pvp"):
        pause = False  # Standard CIPL PvP brings in the next batter in XI order.
    with match_random.use(rng_for(seed, f"delivery:{step}:{state.get('_rng_tick', 0)}")):
        while True:
            summary = match.simulate_over(state, pause_on_wicket=pause,
                                          pause_after_delivery=moment is not None)
            if moment and moment["goal_type"] == "segment" and _moment_measure(state, moment) >= moment["goal"]:
                return "success"
            if not summary.get("delivery_paused"):
                break
        if summary.get("paused"):
            state["event_next_action"] = picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]
            return None
        if human is not None and not state.get("event_pvp"):
            captain.observe_over(state, summary)
        if moment:
            if match.is_innings_over(state):
                if moment["goal_type"] == "chase":
                    winner, _ = match_result(state)
                    return "success" if winner == human else "failed"
                return "failed"
            if match.balls_bowled(state) >= moment["end_ball"]:
                return "failed"
        elif match.is_innings_over(state):
            if state["innings"] == 2:
                return "completed"
            match.end_first_innings(state)
            if human is not None:
                state["event_next_action"] = A_INNINGS_BREAK
                return None
    if human is not None:
        prepare_pick(state, seed, step + 1, human)
    return None


def advance(state, seed, step):
    """Simulate an over for an AI-only fixture or a reproducible Moment prefix."""
    bat, bowl, bowler = _automated_choices(state)
    state.update(batting_approach=bat, bowling_approach=bowl, current_bowler=bowler)
    return resolve_over(state, seed, step)


def _wickets(stats):
    return sum(row.get("wickets", 0) for row in stats.values())


def _boundaries(stats):
    return sum(row.get("fours", 0) + row.get("sixes", 0) for row in stats.values())


def _moment_measure(state, moment):
    if moment["side"] == "bat" or moment["goal_type"] == "chase":
        return state["total_runs"] - moment["start_runs"]
    return _wickets(state["bowl_stats"]) - moment["start_bowling_wickets"]


def build_moment(config):
    """Derive a real, reproducible first-innings checkpoint through Approach."""
    teams = config["snapshot"]
    home, away = (team["key"] for team in config["teams"])
    definition = config["moment"]
    seed = sha256(f"studio-prefix-v1:{definition['seed']}".encode()).hexdigest()
    chasing = definition["goal_type"] == "chase"
    checkpoint = definition.get("checkpoint")
    second_innings = chasing or bool(checkpoint and checkpoint.get("first_innings") is not None)
    user_bats_first = (definition["side"] == "bat") != second_innings
    state = build_state(config, 0, home, away, {key: t["default_xi"] for key, t in teams.items()},
                        seed, batting=home if user_bats_first else away)
    if checkpoint is not None:
        from services.event_studio_checkpoint import load
        if set(checkpoint) - {"current", "first_innings", "target"} or "current" not in checkpoint:
            raise ValueError("A recorded Moment requires a current scorecard.")
        prior = checkpoint.get("first_innings")
        if second_innings:
            load(state, prior, complete=True)
            match.end_first_innings(state)
            if type(checkpoint.get("target")) is not int or checkpoint["target"] != state["target"]:
                raise ValueError("Moment target must be one more than the first-innings score.")
        elif checkpoint.get("target") is not None:
            raise ValueError("A first-innings Moment cannot have a target.")
        load(state, checkpoint["current"])
        if match.balls_bowled(state) != definition["start_over"] * 6 or match.is_innings_over(state):
            raise ValueError("The Moment start must match the configured over and remain playable.")
    else:
        step = 0
        while chasing and state["innings"] == 1:
            advance(state, seed, step)
            step += 1
        expected_innings = 2 if chasing else 1
        for _ in range(definition["start_over"]):
            end = advance(state, seed, step)
            step += 1
            if end or state["innings"] != expected_innings or match.is_innings_over(state):
                raise ValueError("The simulated innings ended before the Moment checkpoint. Change the prefix seed or start over.")
    remaining_wickets = 10 - state["total_wickets"]
    if definition["side"] == "bowl":
        baseline = (_wickets(state["bowl_stats"]) if definition.get("objective_baseline") == "inclusive" else 0)
        targets = [row["threshold"] - baseline for row in config["objectives"]]
        if not chasing:
            targets.append(definition["goal"])
        if targets and max(targets) > min(remaining_wickets, definition["overs"] * 6):
            raise ValueError("The Moment cannot take the required wickets from this checkpoint.")
    goal = state["target"] - state["total_runs"] if chasing else definition["goal"]
    if chasing and definition["side"] == "bat" and goal > definition["overs"] * 36:
        raise ValueError("The chase target exceeds the legal-ball window. Increase the window or adjust the checkpoint.")
    state["event_moment"] = {
        **{key: value for key, value in definition.items() if key != "checkpoint"}, "goal": goal, "target": state["target"], "start_runs": state["total_runs"], "start_wickets": state["total_wickets"],
        "start_bowling_wickets": _wickets(state["bowl_stats"]),
        "start_boundaries": _boundaries(state["bat_stats"]),
        "start_ball": match.balls_bowled(state),
        "end_ball": match.balls_bowled(state) + definition["overs"] * 6,
    }
    state["event_preset_stats"] = {"batting": deepcopy(state["bat_stats"]), "bowling": deepcopy(state["bowl_stats"])}
    return state


def start_moment(config, game_id, seed, away):
    state = deepcopy(config["moment_state"])
    state.update(match_id=game_id, bot_user_id=away, rng_seed=seed)
    if "event_preset_stats" not in state:
        state["event_preset_stats"] = {"batting": deepcopy(state["bat_stats"]),
                                       "bowling": deepcopy(state["bowl_stats"])}
    with match_random.use(rng_for(seed, "persona")):
        captain.assign_persona(state)
    return state


def match_result(state):
    result = match.compute_result(state)
    first, second = state["inn1_bat_team_id"], state["bat_team_id"]
    innings = [
        {"team": first, "runs": state["inn1_runs"], "wickets": state["inn1_wickets"], "balls": state["inn1_balls"]},
        {"team": second, "runs": state["total_runs"], "wickets": state["total_wickets"], "balls": match.balls_bowled(state)},
    ]
    winner = None if result["tie"] else (second if result["margin_type"] == "wickets" else first)
    return winner, {"innings": innings, "margin": result["margin"], "margin_type": result["margin_type"]}


def objective_progress(config, state, team, *, won=False):
    moment = state.get("event_moment")
    if moment:
        inclusive = moment.get("objective_baseline") == "inclusive"
        metrics = {"win": int(won), "runs": state["total_runs"],
                   "wickets": _wickets(state["bowl_stats"]), "boundaries": _boundaries(state["bat_stats"])}
        if not inclusive:
            for key, baseline in (("runs", "start_runs"), ("wickets", "start_bowling_wickets"),
                                  ("boundaries", "start_boundaries")):
                metrics[key] -= moment[baseline]
        batting_balls = bowling_balls = match.balls_bowled(state) - (0 if inclusive else moment["start_ball"])
    else:
        batting = state["bat_team_id"] == team
        batting_stats = state["bat_stats"] if batting else state.get("inn1_bat_stats", {})
        bowling_stats = state.get("inn1_bowl_stats", {}) if batting else state["bowl_stats"]
        metrics = {"win": int(won),
                   "runs": state["total_runs"] if batting else state["inn1_runs"],
                   "boundaries": _boundaries(batting_stats), "wickets": _wickets(bowling_stats)}
        first, current = state.get("inn1_balls", 0), match.balls_bowled(state)
        batting_balls, bowling_balls = (current, first) if batting else (first, current)
    return evaluate(config["objectives"], metrics,
                    {"runs": batting_balls, "boundaries": batting_balls, "wickets": bowling_balls})


def objectives(config, state, team, status, winner):
    moment = state.get("event_moment")
    values = objective_progress(config, state, team,
                                won=status == "success" if moment else winner == team)
    stars = (1 if moment and status == "success" else 0) + sum(value["met"] for value in values)
    return {"objectives": values, "stars": 0 if moment and status != "success" else stars}


def view(state, human):
    public = {"innings": state["innings"], "batting": state["bat_team_name"],
              "runs": state["total_runs"], "wickets": state["total_wickets"],
              "overs": match.format_overs(state), "target": state.get("target"),
              "last_over": state.get("last_over_timeline") or [],
              "choices": public_choices(state, human) if not match.is_innings_over(state) else None}
    moment = state.get("event_moment")
    if moment:
        public["moment"] = {
            "start_runs": moment["start_runs"], "start_wickets": moment["start_wickets"],
            "start_ball": moment["start_ball"], "goal": moment["goal"], "side": moment["side"],
            "goal_type": moment["goal_type"],
            "progress": _moment_measure(state, moment),
            "balls_left": max(0, moment["end_ball"] - match.balls_bowled(state)),
        }
    return public
