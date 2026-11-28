"""Versioned tactical priors for AI-managed Live Event franchises."""

from dataclasses import dataclass
from hashlib import sha256
import hmac
from types import MappingProxyType

from sqlalchemy import select

from models_events import StudioAIManager


VERSION = 1
MAX_BLEND = .2
BAT = ("defensive", "rotate", "balanced", "aggressive", "ultra")
BOWL = ("defensive", "balanced", "mixed", "aggressive", "variation")


@dataclass(frozen=True)
class Profile:
    name: str
    summary: str
    bat: tuple[int, ...]
    bowl: tuple[int, ...]
    phases: tuple[str, ...]


PROFILES = MappingProxyType({
    "aggressor": Profile("The Aggressor", "Attacks in the powerplay", (1, 2, 4, 8, 6), (1, 2, 3, 7, 3), ("powerplay",)),
    "professor": Profile("The Professor", "Studies matchups", (2, 3, 8, 3, 1), (2, 4, 6, 2, 6), ("middle",)),
    "wall": Profile("The Wall", "Protects wickets and contains runs", (7, 6, 4, 1, 1), (8, 5, 3, 1, 2), ("middle",)),
    "gambler": Profile("The Gambler", "Varies the tempo across overs", (4, 2, 2, 3, 6), (4, 1, 4, 5, 6), ()),
    "finisher": Profile("The Finisher", "Builds early and attacks at the death", (1, 2, 5, 8, 5), (1, 2, 5, 2, 9), ("death",)),
    "specialist": Profile("The Specialist", "Varies tactics with the surface", (2, 2, 5, 6, 1), (2, 3, 7, 2, 7), ()),
    "traditionalist": Profile("The Traditionalist", "Uses stable roles and balanced plans", (2, 4, 9, 3, 1), (2, 9, 4, 2, 1), ()),
    "innovator": Profile("The Innovator", "Rotates the strike and changes the bowling plan", (2, 8, 3, 4, 2), (2, 2, 8, 2, 7), ("middle",)),
    "hunter": Profile("The Hunter", "Creates wicket pressure", (1, 3, 4, 6, 1), (1, 3, 3, 10, 2), ("powerplay", "middle")),
    "architect": Profile("The Architect", "Builds an innings around available wickets", (3, 7, 8, 2, 1), (4, 6, 7, 1, 3), ("middle",)),
})


def validate(config):
    settings = config.get("ai_managers", {})
    if type(settings.get("enabled", False)) is not bool:
        raise ValueError("Choose whether AI managers are enabled.")
    blend = settings.get("blend", .12)
    if type(blend) not in (int, float) or not 0 <= blend <= MAX_BLEND:
        raise ValueError("AI manager influence must be between 0 and 0.20.")
    pool = settings.get("allowlist", [])
    if not isinstance(pool, list) or any(not isinstance(key, str) or key not in PROFILES for key in pool):
        raise ValueError("Choose known AI manager profiles.")
    fixed = settings.get("franchises", {})
    teams = {team["key"] for team in config["teams"]}
    if not isinstance(fixed, dict) or any(team not in teams or not isinstance(profile, str) or
                                         profile not in PROFILES or pool and profile not in pool
                                         for team, profile in fixed.items()):
        raise ValueError("Use published franchise keys and profiles from the allowlist.")
    if settings.get("enabled") and config.get("objective_scope") != "tournament":
        raise ValueError("Persistent AI managers require a shared Live Event tournament.")


def assign(db, room, config, teams):
    secret = config.get("ai_manager_seed")
    if not secret:
        return
    settings = config["ai_managers"]
    pool = sorted(set(settings.get("allowlist") or PROFILES))
    for team in sorted(teams):
        seed = hmac.digest(bytes.fromhex(secret), f"{room.id}:{team}".encode(), "sha256")
        profile = settings.get("franchises", {}).get(team) or pool[int.from_bytes(seed, "big") % len(pool)]
        db.add(StudioAIManager(competition_id=room.id, team_key=team, profile_key=profile,
                               profile_version=VERSION, assignment_seed=seed.hex()))


def pins(db, room, *, sides=None):
    query = select(StudioAIManager).filter_by(competition_id=room.id)
    if sides is not None:
        query = query.where(StudioAIManager.team_key.in_(sides))
    return {row.team_key: (row.profile_key, row.profile_version) for row in db.scalars(query)}


def public(key, version):
    if version != VERSION or key not in PROFILES:
        return None
    profile = PROFILES[key]
    return {"key": key, "version": version, "name": profile.name, "summary": profile.summary}


def mix(state, baseline, *, batting, phase, solver=None, controller=None):
    current = state.get("bot_user_id") if controller is None else controller
    pin = state.get("event_ai_managers", {}).get(current)
    if not pin or pin[1] != VERSION or pin[0] not in PROFILES:
        return baseline
    alpha = min(MAX_BLEND, max(0, state.get("event_ai_blend", 0)))
    if not alpha:
        return baseline
    profile = PROFILES[pin[0]]
    keys = BAT if batting else BOWL
    biases = dict(zip(keys, profile.bat if batting else profile.bowl))
    raw = {}
    pitch = (state.get("pitch_type") or "").lower()
    for key, weight in baseline.items():
        if weight <= 0 or key not in biases:
            continue
        influence = biases[key] if not profile.phases or phase in profile.phases else (biases[key] + 4) / 2
        if pin[0] == "finisher" and batting and phase != "death":
            influence = {"defensive": 3, "rotate": 8, "balanced": 7, "aggressive": 2, "ultra": 1}[key]
        if pin[0] == "specialist" and not batting:
            if key == "aggressive" and ("green" in pitch or "seam" in pitch):
                influence *= 1.5
            if key == "variation" and ("dry" in pitch or "dust" in pitch):
                influence *= 1.5
        # Leave near-rejected solver actions close to their existing weight.
        support = solver.get(key, 0) / max(solver.values()) if solver and max(solver.values()) else 1
        raw[key] = max(.01, influence) * (.01 + support)
    if not raw:
        return baseline
    total = sum(raw.values())
    scale = sum(max(0, weight) for weight in baseline.values())
    return {key: (1-alpha) * max(0, value) / scale + alpha * raw.get(key, 0) / total
            for key, value in baseline.items()}


def scouting(rows, ai):
    from collections import Counter
    output = {key: {"batting": Counter(), "bowling": Counter(), "recorded_overs": 0} for key in ai}
    for _, result in rows:
        if result is None or result.kind != "completed" or len(result.detail.get("innings", ())) != 2:
            continue
        innings = result.detail["innings"]
        for key, values in result.detail.get("universe_telemetry", {}).get("tactics", {}).items():
            number, bat, bowl = key.split(":")
            for side, choice, team in (("batting", bat, innings[int(number)-1]["team"]),
                                       ("bowling", bowl, innings[2-int(number)]["team"])):
                if team in output:
                    output[team][side][choice] += values["overs"]
                    output[team]["recorded_overs"] += values["overs"]
    return output


def bowling_bias(state, player, phase):
    if not state.get("event_ai_blend"):
        return 0
    pin = state.get("event_ai_managers", {}).get(state.get("bot_user_id"))
    if not pin or pin[1] != VERSION:
        return 0
    key = pin[0]
    style = (player.get("bowl_style") or "").lower()
    spin = any(word in style for word in ("spin", "break", "orthodox", "china"))
    pitch = (state.get("pitch_type") or "").lower()
    if key == "specialist":
        if ("green" in pitch or "seam" in pitch) and not spin or ("dry" in pitch or "dust" in pitch) and spin:
            return .8
    if key == "hunter" and phase == "powerplay" and not spin:
        return .6
    if key in ("professor", "innovator") and phase == "middle" and spin:
        return .6
    return 0


def order_xi(xi, pin):
    """Adjust batting order within the leading six; never change the selected XI."""
    if not pin or pin[0] not in ("aggressor", "finisher", "wall", "innovator"):
        return xi
    key = pin[0]

    def rank(player):
        baseline = player.get("bat_rating") or player.get("rating") or 0
        approach = player.get("category", "").lower()
        bonus = (2 if key == "aggressor" and "batsman" in approach else
                 2 if key in ("finisher", "wall") and "keeper" in approach else
                 2 if key == "innovator" and "all" in approach else 0)
        return baseline + bonus

    # Compare neighbouring options only. The standard captain remains first.
    initial = list(xi)
    for position in (1, 2, 3, 4):
        left, right = initial[position:position+2]
        if abs((left.get("bat_rating") or 0) - (right.get("bat_rating") or 0)) <= 2 and rank(right) > rank(left):
            initial[position:position+2] = right, left
    return initial
