"""Compare versioned Live Event AI profiles against the existing automatic policy."""

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from statistics import median
from time import perf_counter
from unittest.mock import patch

from engine.ground_config import get_config
from engine.realism.odi import new_settings
from services import cipl_match, competition_ai, event_studio_cricket as cricket
from services.event_studio import digest
from services.event_studio_rules import FORMATS
from tools.calibrate_event_studio import interval, teams

PITCHES = ("Hard", "Flat", "Green", "Dry")
STRENGTH = (-8, 0, 8)


def _match(fmt, reference, index, profile, blend):
    spec = FORMATS[fmt]
    pitch = PITCHES[index % len(PITCHES)]
    gap = STRENGTH[(index // len(PITCHES)) % len(STRENGTH)]
    config = {"snapshot": {side: {"name": side} for side in ("guest", "opponent")},
              "engine": spec["engine"], "format": fmt, "pitch": pitch, "reference_hash": digest(reference)}
    config["odi_settings" if fmt == "ODI" else "ground_reference"] = reference
    seed = sha256(f"universe-calibration-v1:{fmt}:{index}".encode()).hexdigest()
    xis = teams(gap)
    for team in xis.values():
        for player in team:
            player["player_id"] = player["roster_id"]
    if profile and blend:
        xis["guest"] = competition_ai.order_xi(xis["guest"], (profile, competition_ai.VERSION))
    state = cricket.build_state(config, 1, "guest", "opponent", xis, seed)
    if profile:
        # Alternate between the legacy opponent and a pinned Traditionalist.
        state["event_ai_managers"] = {"guest": (profile, competition_ai.VERSION)}
        if index % 2:
            state["event_ai_managers"]["opponent"] = ("traditionalist", competition_ai.VERSION)
    state["event_ai_blend"] = blend
    elapsed, violations = [], 0
    choose = cricket._automated_choices

    def checked(state):
        nonlocal violations
        allowed = {row["roster_id"] for row in cipl_match.eligible_bowlers(state)}
        batting, bowling, bowler = choose(state)
        violations += bowler["roster_id"] not in allowed
        return batting, bowling, bowler

    with patch.object(cricket, "_automated_choices", checked):
        for step in range(spec["overs"] * 3):
            start = perf_counter()
            outcome = cricket.advance(state, seed, step)
            elapsed.append((perf_counter() - start) * 1000)
            if outcome == "completed":
                break
        else:
            raise RuntimeError("The calibration fixture did not complete.")
    winner, _ = cricket.match_result(state)
    batting = Counter()
    phase = {}
    guest_inning = 1 if state["inn1_bat_team_id"] == "guest" else 2
    for over in (*state["inn1_approach_log"], *state["approach_log"]):
        if over["innings"] == guest_inning:
            batting[over["bat"]] += 1
            counts = phase.setdefault(over["phase"], Counter())
            counts.update({key: over[key] for key in ("runs", "balls", "wickets")})
    return winner, elapsed, violations, batting, phase, pitch, gap


def _report(samples):
    times = sorted(time for sample in samples for time in sample[1])
    wins = sum(sample[0] == "guest" for sample in samples)
    batting = Counter()
    phase = {}
    groups = {}
    for winner, _, _, approaches, figures, pitch, gap in samples:
        batting.update(approaches)
        for key, values in figures.items():
            phase.setdefault(key, Counter()).update(values)
        groups.setdefault(f"{pitch} / {gap:+d}", Counter())[winner or "tie"] += 1
    return {"matches": len(samples), "wins": wins, "win_rate_ci95": interval(wins, len(samples)),
            "approach_counts": batting, "phase_totals": phase, "results_by_pitch_and_rating_gap": groups,
            "bowling_eligibility_failures": sum(sample[2] for sample in samples),
            "over_ms_p50": round(median(times), 2), "over_ms_p95": round(times[int(.95 * (len(times) - 1))], 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=12)
    parser.add_argument("--blend", type=float, default=.12)
    args = parser.parse_args()
    if not 1 <= args.pairs <= 1000 or not 0 <= args.blend <= competition_ai.MAX_BLEND:
        parser.error(f"Use 1–1000 pairs and a blend between 0 and {competition_ai.MAX_BLEND}.")
    for fmt in ("T20", "ODI"):
        reference = new_settings() if fmt == "ODI" else deepcopy(get_config())
        if fmt == "T20":
            reference["active_game_mode"] = "natural_game"
        legacy = [_match(fmt, reference, index, None, 0) for index in range(args.pairs)]
        print(json.dumps({"format": fmt, "profile": "legacy", **_report(legacy)}), flush=True)
        for profile in competition_ai.PROFILES:
            samples = [_match(fmt, reference, index, profile, args.blend) for index in range(args.pairs)]
            report = _report(samples)
            changed = sum(before[0] != after[0] for before, after in zip(legacy, samples))
            print(json.dumps({"format": fmt, "profile": profile, "changed_winners": changed, **report}), flush=True)


if __name__ == "__main__":
    main()
