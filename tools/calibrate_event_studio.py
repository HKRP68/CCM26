"""Compare event captain policies on paired, synthetic T20 or ODI matches.

Run from the repository with python -m tools.calibrate_event_studio.
"""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from hashlib import sha256
import json
import math
from statistics import median
from time import perf_counter

from engine.ground_config import get_config
from services import cipl_picks as picks
from services.cipl_match import available_batsmen, eligible_bowlers, max_bowler_overs, quota_for
from services.event_studio import digest
from services.event_studio_cricket import (
    BATTING_APPROACHES, BOWLING_APPROACHES, build_state, match_result, prepare_pick, submit,
)
from services.match_state_store import A_INNINGS_BREAK
from services.event_studio_rules import FORMATS

STRATEGIES = ("balanced", "aggressive", "defensive")
PITCHES = ("Hard", "Flat", "Green")


def teams(offset):
    output = {}
    for side, delta in (("guest", offset), ("opponent", 0)):
        squad = []
        for position in range(11):
            rid = position + (1 if side == "guest" else 21)
            role = "Wicketkeeper" if position == 0 else "Batsman" if position < 6 else "Bowler"
            bat = (83 - 2 * position if position < 6 else 49 - position) + delta
            bowl = (91 - position if position >= 6 else 42 - position) + delta
            squad.append({"name": f"{side} {rid}", "roster_id": rid, "category": role,
                          "bat_rating": bat, "bowl_rating": bowl, "rating": max(bat, bowl),
                          "bowl_style": "Off spin" if position % 2 else "Fast",
                          "bat_hand": "Left" if position % 3 == 1 else "Right", "bowl_hand": "Right"})
        output[side] = squad
    return output


def sample(index, mode, reference, format_key):
    strength = (-8, 0, 8)[index % 3]
    pitch = PITCHES[index // 3 % len(PITCHES)]
    strategy = STRATEGIES[index // 9 % len(STRATEGIES)]
    settings = FORMATS[format_key]
    config = {"snapshot": {key: {"name": key} for key in ("guest", "opponent")},
              "engine": settings["engine"], "format": format_key, "pitch": pitch,
              "reference_hash": digest(reference)}
    if format_key == "ODI":
        config["odi_settings"] = reference
    else:
        config["ground_reference"] = reference
    seed = sha256(f"ccm26-events-calibration-v1:{index}".encode()).hexdigest()
    xis = teams(strength)
    state = build_state(config, index, "guest", "opponent", xis, seed, human="guest", mode=mode)
    durations = []
    step = 0
    status = None
    prepare_pick(state, seed, step, "guest")
    for _ in range(settings["overs"] * 8):
        start = perf_counter()
        stage = state["event_next_action"]
        if stage == A_INNINGS_BREAK:
            prepare_pick(state, seed, step, "guest")
            continue
        kind = next(name for name, action in picks.PICK_ACTION.items() if stage == action)
        if kind == picks.PICK_BOWLER:
            value = eligible_bowlers(state)[0]["roster_id"]
        elif kind == picks.PICK_NEW_BATSMAN:
            value = available_batsmen(state)[0]["roster_id"]
        else:
            options = BATTING_APPROACHES if kind == picks.PICK_BAT_APPROACH else BOWLING_APPROACHES
            value = next(index for index, row in enumerate(options) if row[0] == strategy)
        status = submit(state, seed, step, "guest", kind, value)
        durations.append((perf_counter() - start) * 1000)
        if status:
            break
        if kind != picks.PICK_BOWLER and state["event_next_action"] != picks.PICK_ACTION[picks.PICK_NEW_BATSMAN]:
            step += 1
    if status != "completed":
        raise RuntimeError("Match did not finish.")
    winner, _ = match_result(state)
    previous = None
    warnings = 0
    collect = Counter()
    for over in [*state["inn1_approach_log"], *state["approach_log"]]:
        current = (over["innings"], over["bowler_rid"])
        warnings += current == previous
        previous = current
        key = over["phase"]
        collect[key + "-runs"] += over["runs"]
        collect[key + "-wickets"] += over["wickets"]
        collect[key + "-balls"] += over["balls"]
        symbols = over.get("timeline") or []
        collect["dots"] += symbols.count("0")
        collect["boundaries"] += symbols.count("4") + symbols.count("6")
    for xi, stats in ((state["inn1_bowl_xi"], state["inn1_bowl_stats"]), (state["bowl_xi"], state["bowl_stats"])):
        if max_bowler_overs(state) != settings["overs"] // 5:
            warnings += 1
        warnings += sum(stats.get(str(player["roster_id"]), {}).get("balls", 0) > 6 * quota_for(state, player)
                        for player in xi)
    return winner, durations, warnings, collect, strategy


def interval(wins, n):
    share = wins / n
    z = 1.96
    denominator = 1 + z**2 / n
    centre = (share + z**2 / (2 * n)) / denominator
    radius = z * math.sqrt((share * (1 - share) + z**2 / (4 * n)) / n) / denominator
    return [round(value, 4) for value in (centre - radius, centre + radius)]


def run(pairs, format_key):
    if format_key == "ODI":
        from engine.realism.odi import new_settings
        reference = new_settings()
    else:
        reference = deepcopy(get_config())
        reference["active_game_mode"] = "natural_game"
    reports = {}
    paired = defaultdict(Counter)
    for mode in ("tournament_team", "own_roster"):
        totals, outcomes, elapsed, violations = Counter(), Counter(), [], 0
        for index in range(pairs):
            winner, durations, warnings, stats, strategy = sample(index, mode, reference, format_key)
            totals.update(stats)
            outcomes[winner or "tie"] += 1
            violations += warnings
            elapsed.extend(durations)
            paired[strategy][mode] += winner == "opponent"
        ordered = sorted(elapsed)
        reports[mode] = {"results": dict(outcomes), "opponent_win_ci95": interval(outcomes["opponent"], pairs),
                         "pick_ms_p50": round(median(elapsed), 2),
                         "pick_ms_p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
                         "bowling_rule_violations": violations, "totals": dict(totals)}
    return {"pairs": pairs, "format": format_key, "policies": reports,
            "opponent_wins_by_strategy": {key: dict(value) for key, value in paired.items()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=108)
    parser.add_argument("--format", choices=FORMATS, default="T20")
    args = parser.parse_args()
    if args.pairs < 1:
        parser.error("--pairs must be positive")
    print(json.dumps(run(args.pairs, args.format), indent=2))
