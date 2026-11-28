"""Validated, optimistic, versioned Events Studio authoring."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from hashlib import sha256
import json
import os
import re

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from engine import pitch_registry
from models import ChallengeLeague, ChallengeTeam, Player
from models_events import StudioEvent, StudioRevision
from services import event_studio_rules as rules, event_studio_schedule as schedule
from services import event_studio_objectives as tournament_goals
from services.challenge_team_io import team_values
from services.xi_rules import _category_is_keeper, validate_challenge_xi_dicts

KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
TRACKS = ("tournament_team", "own_roster")
KINDS = ("live", "moment", "boss")
# The surfaces the engine actually models, from the one registry that owns them.
# "Wicket" was never a surface — it is a ball-outcome bucket that an earlier copy of
# this list mistook for one. It stays accepted so a draft saved with it still loads,
# but it is never drawn and has no ground profile, so publishing with it still fails.
LEGACY_PITCHES = ("Wicket",)
PITCHES = tuple(pitch_registry.PITCHES) + LEGACY_PITCHES
# Config keys that older drafts and published revisions still carry. They are dropped
# on load so those revisions keep opening; nothing reads them.
LEGACY_KEYS = ("enrollment_cutoff",)

ACTIONS = ("schedule", "objectives", "rewards")
OPEN_STATUSES = ("scheduled", "live")


class Conflict(ValueError):
    pass


class ExecutionUnavailable(Exception):
    """An engine failure preserved the previous official checkpoint."""



def allowed(kind, key="T20"):
    name = {"live": "LIVE", "moment": "MOMENTS", "boss": "BOSSES"}.get(kind)
    if not (name and rules.FORMATS.get(key, {}).get("engine")
            and flag("PUBLISH") and flag(key) and flag(name)):
        return False
    if key == "ODI":
        from engine.realism.odi import enabled
        return enabled()
    return True


def flag(key):
    return os.getenv(f"EVENT_STUDIO_{key}", "").strip().lower() in ("1", "true", "yes")


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return sha256(encoded.encode()).hexdigest()


def defaults():
    return {
        "schema_version": 1, "kind": "live", "challenge_style": "full", "format": "T20", "timezone": "Asia/Kolkata",
        "description": "", "banner_url": "", "featured": False, "opens_at": "", "closes_at": "",
        "max_users": 500, "mode": ["tournament_team"], "teams": [],
        "min_overseas": 0, "max_overseas": 11, "gender": None, "home_country": "",
        "pitch": "Hard", "venue": "", "weather": "", "difficulty": None, "impact_player": False, "claim_days": 7, "attempts": 3,
        "points": {"win": 2, "loss": 0, "tie": 1, "no_result": 1},
        "community": {"completion_xp": 0, "daily_limit": 3, "boss_goal": 0},
        "chapter": {"name": "", "position": 1, "prerequisite_slug": "", "min_stars": 1},
        "notifications": {"enabled": False, "reminders_hours": [6, 1]},
        "rotation": {"cadence": "none", "periods": 1, "series_budget": {}},
        "schedule": {"preset": "round_robin", "legs": 2, "games": 3, "playoffs": "top4",
                     "round_spacing_days": 1, "window_hours": 20, "grace": 30,
                     "playing_days": list(range(1, 8)), "blackouts": [], "fixtures_per_day": 24,
                     "match_target": None, "min_rest_minutes": 0,
                     "third_place": False, "groups": {}, "qualify_per_group": 2, "seed": None,
                     "venue_countries": [], "template": "", "rest_minutes": 60, "grand_final_reset": True, "previous_event_id": 0, "tour": []},
        "moment": {"side": "bat", "start_over": 16, "overs": 4, "goal": 35, "seed": 0, "goal_type": "segment",
                   "checkpoint": None, "objective_baseline": "earned"},
        "fixtures": [], "fixture_overrides": {}, "objectives": [], "rewards": {}, "reward_locks": [],
        "objectives_locked": False, "milestones": [],
        "objective_scope": "fixture", "objective_preset": "balanced", "tournament_objectives": [], "objective_ladders": None, "ladder_generator": {},
        "competition": {"modes": ["solo", "multi"], "registration_minutes": 60,
                        "early_start": True, "auto_start_full": True, "min_humans": 1, "match_rewards": True,
                        "double_no_show_points": 0, "event_pass": False,
                        "dressing_room": False, "management_cap": 0, "management_weights": {}, "news": False, "news_upset_gap": 5, "manager_profile": False,
                        "competitive_rating": False, "rating_k": 24, "manager_xp_cap": 500, "manager_xp_weights": {},
                        "qualification": False, "predictor_samples": 0, "honour_board": False,
                        "lobby_v2": False, "group_engagement": False, "world_rankings": False, "analytics": False},
        "ai_managers": {"enabled": False, "blend": .12, "allowlist": [], "franchises": {}},
        "pass_inventory": {"packs": 0, "cards": 0, "auto_free": [], "auto_diamond": []},
        "budget": {"coins": 0, "gems": 0, "quest_points": 0}, "generation": {},
    }


def enrollment_closes(config):
    """When joining stops, derived rather than authored.

    A campaign's games are written when a player joins, carrying the published
    deadlines, so joining after the first fixture deadline would hand them a fixture
    they could never play. A Moment or Boss Battle has no fixtures, so joining stays
    open for as long as the event is.
    """
    zone = config["timezone"]
    deadlines = [schedule.utc_instant(row["deadline"], zone) for row in config["fixtures"]]
    return min(deadlines) if deadlines else schedule.utc_instant(config["closes_at"], zone)


def normalize(raw):
    config = defaults()
    if isinstance(raw, dict) and set(raw) & set(LEGACY_KEYS):
        # Enrollment is no longer authored, but a draft or published revision saved
        # before that still carries the key. Drop it rather than refuse the import.
        raw = {key: value for key, value in raw.items() if key not in LEGACY_KEYS}
    if not isinstance(raw, dict) or set(raw) - set(config):
        raise ValueError("Import contains unsupported fields.")
    for key in ("schedule", "moment", "points", "community", "rotation", "notifications", "chapter", "competition", "pass_inventory", "ai_managers"):
        if key in raw and (not isinstance(raw[key], dict) or set(raw[key]) - set(config[key])):
            raise ValueError(f"{key}: unsupported fields.")
        config[key].update(raw.get(key, {}))
    config.update({key: value for key, value in raw.items() if key not in ("schedule", "moment", "points", "community", "rotation", "notifications", "chapter", "competition", "pass_inventory", "ai_managers")})
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise ValueError("Unsupported event schema version.")
    for key in ("description", "banner_url", "timezone", "kind", "format", "opens_at", "closes_at",
                "home_country", "pitch", "venue", "weather"):
        if not isinstance(config[key], str) or len(config[key]) > ({"description": 2000, "banner_url": 500}.get(key, 80)):
            raise ValueError(f"Invalid {key}.")
    from services.pitch_report import WEATHERS
    if config["weather"] and config["weather"] not in WEATHERS:
        raise ValueError("Choose a supported weather condition.")
    if config["difficulty"] not in (None, "easy", "normal", "hard", "hard_plus"):
        raise ValueError("Choose Easy, Normal, Hard or Hard++.")
    if config["banner_url"]:
        team_values({"logo_url": config["banner_url"]})
    for key in ("featured", "objectives_locked", "impact_player"):
        if type(config[key]) is not bool:
            raise ValueError(f"{key} must be true or false.")
    for key in ("budget", "rewards", "generation", "fixture_overrides"):
        if not isinstance(config[key], dict):
            raise ValueError(f"{key} must be an object.")
    for key in ("teams", "mode", "objectives", "fixtures", "reward_locks", "milestones"):
        if not isinstance(config[key], list):
            raise ValueError(f"{key} must be a list.")
    if config["challenge_style"] not in ("full", "moment"):
        raise ValueError("Boss challenge style must be full or moment.")
    if config["kind"] not in KINDS or config["format"] not in rules.FORMATS:
        raise ValueError("Unknown event type or format.")
    if config["impact_player"] and (config["format"] != "T20" or rules.is_moment(config)):
        raise ValueError("Impact Player is available for full T20 matches.")
    if not config["mode"] or not all(mode in TRACKS for mode in config["mode"]):
        raise ValueError("Enable at least one participation mode.")
    if config["gender"] not in (None, "Male", "Female"):
        raise ValueError("Choose Male, Female or leave gender unrestricted.")
    if config["pitch"] not in PITCHES:
        raise ValueError("Choose a supported pitch.")
    for key, low, high in (("max_users", 1, 5000), ("min_overseas", 0, 11), ("max_overseas", 0, 11),
                           ("claim_days", 1, 60), ("attempts", 1, 10)):
        rules.integer(config[key], key, low, high)
    if config["min_overseas"] > config["max_overseas"]:
        raise ValueError("Minimum overseas exceeds the maximum.")
    if (config["min_overseas"] or config["max_overseas"] != 11) and not config["home_country"].strip():
        raise ValueError("Enter a home country for overseas restrictions.")
    for label, value in config["points"].items():
        rules.integer(value, f"{label} points", 0, 20)
    rules.integer(config["community"]["completion_xp"], "Community completion XP", 0, 100)
    rules.integer(config["community"]["daily_limit"], "Community daily limit", 1, 10)
    rules.integer(config["community"]["boss_goal"], "Community boss goal", 0, config["max_users"])
    if config["community"]["boss_goal"] and config["kind"] != "boss":
        raise ValueError("Distinct-winner goals are supported for Boss Battles.")
    chapter = config["chapter"]
    if (not isinstance(chapter["name"], str) or len(chapter["name"].strip()) > 80
            or not isinstance(chapter["prerequisite_slug"], str)
            or chapter["prerequisite_slug"] and not KEY.fullmatch(chapter["prerequisite_slug"])):
        raise ValueError("Enter a chapter name and a valid prerequisite event slug.")
    rules.integer(chapter["position"], "Chapter position", 1, 100)
    rules.integer(chapter["min_stars"], "Prerequisite stars", 1, 5)
    if (chapter["name"] or chapter["prerequisite_slug"]) and not rules.is_moment(config):
        raise ValueError("Chapters are supported for Cricket Moments.")
    notices = config["notifications"]
    if (type(notices["enabled"]) is not bool or not isinstance(notices["reminders_hours"], list)
            or len(notices["reminders_hours"]) > 4):
        raise ValueError("Choose up to four fixture reminder offsets.")
    for value in notices["reminders_hours"]:
        rules.integer(value, "Reminder hours", 1, 168)
    if len(set(notices["reminders_hours"])) != len(notices["reminders_hours"]):
        raise ValueError("Reminder offsets must be distinct.")
    rotation = config["rotation"]
    if rotation["cadence"] not in ("none", "daily", "weekly") or not isinstance(rotation["series_budget"], dict):
        raise ValueError("Select a supported rotation cadence and budget.")
    rules.integer(rotation["periods"], "Scheduled editions", 1, 12)
    if rotation["cadence"] == "none" and rotation["periods"] != 1:
        raise ValueError("Multiple editions require a daily or weekly cadence.")
    for asset, amount in rotation["series_budget"].items():
        if asset not in rules.ASSETS:
            raise ValueError(f"Unknown rotation budget asset {asset}.")
        rules.integer(amount, f"Rotation {asset} budget", 0, 2_000_000_000)
    settings = config["schedule"]
    for key, low, high in (("legs", 1, 5), ("games", 1, 15), ("round_spacing_days", 1, 30),
                           ("window_hours", 1, 168), ("grace", 0, 240), ("fixtures_per_day", 1, 24),
                           ("qualify_per_group", 1, 4)):
        rules.integer(settings[key], key, low, high)
    schedule.validate_template(config)
    if settings["preset"] not in schedule.PRESETS:
        raise ValueError("Unsupported schedule preset.")
    if type(settings["grand_final_reset"]) is not bool:
        raise ValueError("Grand final reset must be enabled or disabled.")
    if settings["preset"] == "double_elimination" and config["objective_scope"] != "tournament":
        raise ValueError("Double elimination requires a shared tournament schedule.")
    if settings["playoffs"] not in schedule.PLAYOFFS or type(settings["third_place"]) is not bool:
        raise ValueError("Invalid playoff settings.")
    if settings["seed"] is not None:
        rules.integer(settings["seed"], "Schedule seed", 0, 2**32 - 1)
    rules.integer(settings["min_rest_minutes"], "Minimum rest between matches", 0, 10080)
    if settings["match_target"] is not None:
        if settings["preset"] != "one_half_round_robin":
            raise ValueError("A league match target applies only to a 1.5 round robin.")
        if type(settings["match_target"]) not in (int, float) or not 1 <= settings["match_target"] <= 60:
            raise ValueError("League match target must be a number of games from 1 to 60.")
    groups = settings["groups"]
    if not isinstance(groups, dict) or len(groups) > 8 or any(
            not KEY.fullmatch(key) or not isinstance(teams, list) or
            any(not isinstance(team, str) for team in teams) for key, teams in groups.items()):
        raise ValueError("Group settings must map group keys to lists of team keys.")
    countries = settings["venue_countries"]
    if not isinstance(countries, list) or len(countries) > 20 or any(
            not isinstance(name, str) or not name.strip() or len(name) > 60 for name in countries):
        raise ValueError("Venue countries must be up to 20 country names. Leave it empty for every stadium.")
    days = settings["playing_days"]
    if not isinstance(days, list) or not days or any(type(d) is not int or not 1 <= d <= 7 for d in days):
        raise ValueError("Playing days must be weekday numbers from 1 (Monday) through 7.")
    blackouts = settings["blackouts"]
    if not isinstance(blackouts, list) or len(blackouts) > 366:
        raise ValueError("Blackout dates must be a list of local YYYY-MM-DD dates.")
    for date in blackouts:
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise ValueError("Blackout dates must use YYYY-MM-DD.")
        datetime.fromisoformat(date)
    moment = config["moment"]
    if moment["goal_type"] not in ("segment", "chase"):
        raise ValueError("Choose a segment goal or a simulated chase.")
    if moment["objective_baseline"] not in ("earned", "inclusive"):
        raise ValueError("Choose whether Moment objectives include the preset figures.")
    if moment["checkpoint"] is not None and not isinstance(moment["checkpoint"], dict):
        raise ValueError("Moment checkpoint must be a scorecard object.")
    if moment["side"] not in ("bat", "bowl"):
        raise ValueError("A Moment must be batting or bowling.")
    max_overs = rules.FORMATS[config["format"]]["overs"]
    rules.integer(moment["start_over"], "Moment completed overs", 0, max_overs - 1)
    rules.integer(moment["overs"], "Moment play window", 1, max_overs - moment["start_over"])
    rules.integer(moment["goal"], "Moment target", 1, 6 * 6 * moment["overs"] if moment["side"] == "bat" else 10)
    rules.integer(moment["seed"], "Moment prefix seed", 0, 2**32 - 1)
    _validate_team_inputs(config["teams"])
    _validate_fixture_fields(config["fixtures"])
    if config["objective_ladders"] is not None and not isinstance(config["objective_ladders"], dict):
        raise ValueError("Objective ladders must be a versioned object.")
    from services import competition_ai
    competition_ai.validate(config)
    tournament_goals.validate_generator(config)
    tournament_goals.validate(config)
    if config["kind"] != "live":
        config["objective_scope"] = "fixture"
    if config["objective_preset"] not in tournament_goals.PRESETS:
        raise ValueError("Choose a supported tournament objective preset.")
    if tournament_goals.tournament(config):
        settings = config["competition"]
        modes = settings["modes"]
        if not isinstance(modes, list) or not modes or any(mode not in ("solo", "multi") for mode in modes) or len(set(modes)) != len(modes):
            raise ValueError("Enable Solo, Multiplayer, or both.")
        rules.integer(settings["registration_minutes"], "Registration minutes", 60, 60)
        rules.integer(settings["min_humans"], "Minimum humans", 1, 16)
        rules.integer(settings["double_no_show_points"], "Double no-show points", 0, 20)
        toggles = ("early_start", "auto_start_full", "match_rewards", "event_pass", "dressing_room", "news",
                   "manager_profile", "competitive_rating", "qualification", "honour_board",
                   "lobby_v2", "group_engagement", "world_rankings", "analytics")
        if any(type(settings[key]) is not bool for key in toggles):
            raise ValueError("Competition features must be enabled or disabled.")
        rules.integer(settings["management_cap"], "Dressing Room rating cap", 0, 1)
        weights = settings["management_weights"]
        if (not isinstance(weights, dict) or weights.keys() - {"morale", "unity", "form", "fatigue", "captain"}
                or any(type(value) not in (float, int) or not 0 <= value <= 1 for value in weights.values())):
            raise ValueError("Set Dressing Room weights from zero to one.")
        if settings["management_cap"] and (not settings["dressing_room"] or "solo" not in modes):
            raise ValueError("Dressing Room gameplay effects require the Solo Dressing Room.")
        rules.integer(settings["news_upset_gap"], "Upset strength gap", 1, 50)
        if settings["world_rankings"] and ("multi" not in modes or not settings["lobby_v2"]):
            raise ValueError("World Rankings require Multiplayer with public and private lobby controls.")
        rules.integer(settings["rating_k"], "Rating K-factor", 1, 64)
        rules.integer(settings["predictor_samples"], "Qualification samples", 0, 2000)
        if settings["predictor_samples"] and not settings["qualification"]:
            raise ValueError("Enable Qualification Centre before enabling estimates.")
        rules.integer(settings["manager_xp_cap"], "Manager XP cap", 0, 10000)
        xp_weights = settings["manager_xp_weights"]
        if not isinstance(xp_weights, dict) or xp_weights.keys() - {"match", "win", "star", "completion", "title"}:
            raise ValueError("Choose supported Manager XP sources.")
        for amount in xp_weights.values():
            rules.integer(amount, "Manager XP weight", 0, 500)
        if settings["competitive_rating"] and (not settings["manager_profile"] or "multi" not in modes):
            raise ValueError("Competitive rating requires the manager profile and Multiplayer.")
        if config["mode"] != ["tournament_team"] or config["objectives"]:
            raise ValueError("Tournament Live Events use published franchise squads and tournament objectives.")
    rules.validate_objectives(config)
    if len(config["fixture_overrides"]) > schedule.MAX_FIXTURES:
        raise ValueError("Too many fixture overrides.")
    for key, override in config["fixture_overrides"].items():
        if not KEY.fullmatch(key) or not isinstance(override, dict) or set(override) - {"objectives", "rewards", "difficulty", "pitch", "weather", "venue"}:
            raise ValueError(f"{key}: unsupported fixture override.")
        if override.get("difficulty") not in (None, "easy", "normal", "hard", "hard_plus"):
            raise ValueError(f"{key}: choose Easy, Normal, Hard or Hard++.")
        if "pitch" in override and override["pitch"] not in PITCHES:
            raise ValueError(f"{key}: choose a supported pitch.")
        if "weather" in override and override["weather"] not in ("", *WEATHERS):
            raise ValueError(f"{key}: choose supported weather.")
        if "venue" in override and (not isinstance(override["venue"], str) or len(override["venue"]) > 80):
            raise ValueError(f"{key}: venue must be at most 80 characters.")
        if "objectives" in override:
            if tournament_goals.tournament(config):
                raise ValueError("Tournament objectives cannot be overridden per fixture.")
            rules.validate_objectives({**config, **override})
        if "rewards" in override and not isinstance(override["rewards"], dict):
            raise ValueError(f"{key}: rewards must be a tier object.")
    for tier_rewards in config["rewards"].values():
        if not isinstance(tier_rewards, dict) or any(not isinstance(bundle, dict) for bundle in tier_rewards.values()):
            raise ValueError("Rewards must contain tier, result and asset objects.")
    if len(config["milestones"]) > 20:
        raise ValueError("Enter at most 20 star milestones.")
    from services import event_studio_items
    event_studio_items.validate(config, require_budget=False)
    rules.validate_rewards(config, require_budget=False)
    if any(key not in ACTIONS or not isinstance(value, dict) or not isinstance(value.get("input"), str)
           for key, value in config["generation"].items()):
        raise ValueError("Invalid generation provenance.")
    if any(not isinstance(lock, str) or len(lock.split(".")) != 3 for lock in config["reward_locks"]):
        raise ValueError("Reward locks use tier.result.asset.")
    return config


def _validate_team_inputs(teams):
    keys, names = set(), set()
    if len(teams) > 16:
        raise ValueError("Add at most 16 teams.")
    for team in teams:
        key, name = team["key"], team["name"]
        if (not isinstance(key, str) or not KEY.fullmatch(key) or key in keys
                or not isinstance(name, str) or not name.strip() or len(name) > 120
                or name.casefold() in names):
            raise ValueError("Use unique team keys and team names.")
        unknown = set(team) - {"key", "name", "players", "xi", "source_team_id", "short_name",
                               "logo_url", "primary_color", "secondary_color"}
        if unknown:
            raise ValueError(f"{name}: unsupported team fields.")
        team_values({field: team[field] for field in ("name", "short_name", "logo_url",
                     "primary_color", "secondary_color") if field in team})
        home_venue = team.get("home_venue", "")
        if not isinstance(home_venue, str) or len(home_venue) > 80:
            raise ValueError(f"{name}: home venue must be at most 80 characters.")
        ids = team["players"]
        if not isinstance(ids, list) or len(ids) > 40:
            raise ValueError(f"{name}: provide up to 40 card IDs.")
        for card in ids:
            rules.integer(card, f"{name} card ID", 1, 2**31 - 1)
        if len(set(ids)) != len(ids):
            raise ValueError(f"{name}: duplicate card ID.")
        if "xi" in team and (not isinstance(team["xi"], list) or len(team["xi"]) != 11
                             or any(type(pid) is not int or pid not in ids for pid in team["xi"])):
            raise ValueError(f"{name}: fixed XI must list 11 card IDs from the squad.")
        keys.add(key)
        names.add(name.casefold())


def _validate_fixture_fields(rows):
    from services.pitch_report import WEATHERS
    if len(rows) > schedule.MAX_FIXTURES:
        raise ValueError("Too many fixtures.")
    # Conditions are lockable like any other authored field, so a pitch or venue typed
    # into one row survives the next Auto Schedule.
    lockable = {"home", "away", "round", "stage", "opens_at", "deadline", "grace",
                "pitch", "venue", "weather"}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or not KEY.fullmatch(row["key"]):
            raise ValueError("Each fixture needs a valid key.")
        if set(row) - lockable - {"key", "source", "locks", "overrides"}:
            raise ValueError(f'{row["key"]}: unsupported fixture fields.')
        for field in ("home", "away", "opens_at", "deadline"):
            if not isinstance(row.get(field), str) or len(row[field]) > 80:
                raise ValueError(f'{row["key"]}: missing or invalid {field}.')
        if row.get("stage") not in ("league", "playoff", "survival") or row.get("source", "manual") not in ("manual", "generated"):
            raise ValueError(f'{row["key"]}: invalid stage or provenance.')
        rules.integer(row["round"], "Round", 1, schedule.MAX_FIXTURES)
        rules.integer(row["grace"], "Grace minutes", 0, 240)
        if "pitch" in row and row["pitch"] not in PITCHES:
            raise ValueError(f'{row["key"]}: choose a supported pitch.')
        if "weather" in row and row["weather"] not in ("", *WEATHERS):
            raise ValueError(f'{row["key"]}: choose supported weather.')
        if "venue" in row and (not isinstance(row["venue"], str) or len(row["venue"]) > 80):
            raise ValueError(f'{row["key"]}: venue must be at most 80 characters.')
        for metadata in ("locks", "overrides"):
            if (not isinstance(row.get(metadata, []), list)
                    or any(key not in lockable for key in row.get(metadata, []))):
                raise ValueError(f'{row["key"]}: invalid {metadata} field.')


def _gender_eligible(card, config):
    return not config["gender"] or (card.gender or "").lower() == config["gender"].lower()


def card_snapshot(db, card, config, *, roster_id=None):
    base = card
    visited = set()
    while base.parent_player_id is not None:
        if base.id in visited:
            raise ValueError(f"{card.name}: invalid base card identity.")
        visited.add(base.id)
        base = db.get(Player, base.parent_player_id)
        if base is None:
            raise ValueError(f"{card.name}: base card is missing.")
    country = config["home_country"].strip().casefold()
    return {"roster_id": roster_id if roster_id is not None else -card.id, "player_id": card.id, "base_id": base.id,
            "name": card.name, "version": card.version, "gender": card.gender, "country": card.country,
            "is_overseas": bool(country and card.country.strip().casefold() != country),
            "rating": card.rating, "bat_rating": card.bat_rating, "bowl_rating": card.bowl_rating,
            "category": card.category, "bowl_style": card.bowl_style,
            "bowl_hand": card.bowl_hand, "bat_hand": card.bat_hand}


def validate_xi(players, config):
    if len(players) != 11 or len({p["base_id"] for p in players}) != 11:
        raise ValueError("Select 11 distinct base players.")
    legal, issue = validate_challenge_xi_dicts(players, config["min_overseas"], config["max_overseas"])
    if not legal:
        raise ValueError(issue)
    if sum(p["category"] in ("Bowler", "All-rounder") for p in players) < 5:
        raise ValueError("At least five players must have a specialist bowling quota.")
    if config["format"] == "ODI":
        from services.odi_approach import valid_xi
        if not valid_xi(players):
            raise ValueError("An ODI XI must cover all 50 overs.")


def best_xi(squad, config, *, hard_plus=False):
    """Find the highest-OVR legal selection, considering one card per identity."""
    grouped = {}
    for player in sorted(squad, key=lambda p: (-p["rating"], p["roster_id"])):
        grouped.setdefault(player["base_id"], []).append(player)
    states = {(0, 0, 0, 0): (0, [])}
    for versions in grouped.values():
        following = dict(states)
        for (size, keepers, bowlers, overseas), (rating, chosen) in states.items():
            if size == 11:
                continue
            for player in versions:
                keeper = _category_is_keeper(player["category"])
                bowler = player["category"] in ("Bowler", "All-rounder")
                key = (size + 1, min(1, keepers + keeper), min(5, bowlers + bowler),
                       overseas + player["is_overseas"])
                value = player["rating"]
                if hard_plus:
                    bat, bowl = player.get("bat_rating") or 0, player.get("bowl_rating") or 0
                    if player["category"] == "All-rounder":
                        value = 0.50 * value + 0.25 * (bat + bowl)
                    else:
                        value = 0.35 * value + 0.65 * (bowl if bowler else bat)
                score = rating + value
                if key[3] <= config["max_overseas"] and score > following.get(key, (-1, []))[0]:
                    following[key] = score, [*chosen, player]
        states = following
    qualified = [val for (n, wk, bowl, overseas), val in states.items()
                 if n == 11 and wk and bowl == 5 and overseas >= config["min_overseas"]]
    if not qualified:
        raise ValueError("No legal XI meets the keeper, bowling and overseas requirements.")
    xi = sorted(max(qualified, key=lambda val: val[0])[1],
                key=lambda player: (-(player["bat_rating"] or 0), player["roster_id"]))
    validate_xi(xi, config)
    return xi


def copied_teams(db, league_id):
    league = db.get(ChallengeLeague, league_id)
    if not league:
        raise ValueError("Challenge League does not exist.")
    teams = db.scalars(select(ChallengeTeam).options(selectinload(ChallengeTeam.players))
                       .where(ChallengeTeam.league_id == league.id, ChallengeTeam.is_active.is_(True))
                       .order_by(ChallengeTeam.sort_order, ChallengeTeam.id)).all()
    copied = []
    for team in teams:
        if any(not row.source_player_id for row in team.players):
            raise ValueError(f"{team.name}: resolve unlinked Challenge Data players first.")
        copied.append({"key": f"team-{team.id}", "name": team.name, "source_team_id": team.id,
                       "players": [row.source_player_id for row in sorted(team.players, key=lambda p: (p.sort_order, p.id))],
                       "short_name": team.short_name, "logo_url": team.logo_url,
                       "primary_color": team.primary_color, "secondary_color": team.secondary_color})
    _validate_team_inputs(copied)
    return {"teams": copied, "home_country": league.home_country or "",
            "min_overseas": league.min_overseas, "max_overseas": league.max_overseas}


def snapshot_teams(db, config):
    ids = {card for team in config["teams"] for card in team["players"]}
    cards = {p.id: p for p in db.scalars(select(Player).where(Player.id.in_(ids))).all()}
    seen = set()
    frozen = {}
    for team in config["teams"]:
        squad = []
        for pid in team["players"]:
            card = cards.get(pid)
            if not card or not card.is_active or card.is_career or not _gender_eligible(card, config):
                raise ValueError(f'{team["name"]}: card {pid} is missing, inactive or ineligible.')
            player = card_snapshot(db, card, config)
            if player["base_id"] in seen:
                raise ValueError(f'{team["name"]}: {card.name} shares a base identity with a rostered card.')
            seen.add(player["base_id"])
            squad.append(player)
        by_id = {p["player_id"]: p for p in squad}
        xi = [by_id[pid] for pid in team["xi"]] if "xi" in team else best_xi(squad, config)
        validate_xi(xi, config)
        hard_plus_xi = xi if "xi" in team else best_xi(squad, config, hard_plus=True)
        frozen[team["key"]] = {**team, "squad": squad, "default_xi": xi, "hard_plus_xi": hard_plus_xi}
    return frozen


def _inputs(config, action):
    fields = {
        "schedule": ("kind", "teams", "timezone", "opens_at", "closes_at", "schedule"),
        "objectives": ("kind", "format", "moment", "challenge_style"),
        "rewards": ("kind", "max_users", "budget", "teams", "fixtures", "objectives", "milestones",
                    "fixture_overrides"),
    }[action]
    inputs = {key: config[key] for key in fields}
    if action == "schedule" and not inputs["schedule"].get("template") and inputs["schedule"]["preset"] != "double_elimination":
        inputs["schedule"] = {key: value for key, value in inputs["schedule"].items()
                              if key not in ("template", "rest_minutes", "grand_final_reset", "previous_event_id", "tour")}
    if action == "objectives":
        inputs["moment"] = {key: value for key, value in config["moment"].items()
                            if key not in ("checkpoint", "objective_baseline")}
    elif action == "rewards":
        inputs["fixture_overrides"] = {
            key: {name: value for name, value in override.items() if name in ("rewards", "objectives")}
            for key, override in config["fixture_overrides"].items()
            if "rewards" in override or "objectives" in override
        }
    if tournament_goals.tournament(config) and action in ("objectives", "rewards"):
        inputs.update(objective_scope="tournament", tournament_objectives=config["tournament_objectives"]
                      if action == "rewards" else None,
                      competition={key: value for key, value in config["competition"].items()
                                   if key in ("modes", "registration_minutes", "early_start", "min_humans",
                                              "match_rewards", "double_no_show_points")})
        if config.get("objective_ladders"):
            inputs["objective_ladders"] = config["objective_ladders"]
        if config.get("ladder_generator"):
            inputs["ladder_generator"] = config["ladder_generator"]
        if action == "rewards" and config["competition"].get("event_pass"):
            inputs["event_pass"] = True
        if action == "objectives":
            inputs.update(preset=config["objective_preset"], teams=[row["key"] for row in config["teams"]],
                          fixtures=config["fixtures"])
    return digest(inputs)


def condition_pools(config):
    """``(venues, pitches)`` the generator may draw a fixture's ground from.

    Venues come from Stadium Data, narrowed to ``schedule.venue_countries`` when that is
    set. Pitches come from the registry's selectable surfaces, narrowed to what the
    format's pinned reference can model — ODI's reference covers only five of them, and
    ``validate_publish`` refuses anything outside it.
    """
    from services.draft_service import country_key
    from services.tournament_stadiums import all_rows
    wanted = {country_key(name) for name in config["schedule"]["venue_countries"]}
    wanted.discard("")
    venues = [str(row["name"]).strip() for row in all_rows()
              if row.get("name") and (not wanted or country_key(row.get("country")) in wanted)]
    if not venues:
        raise ValueError("No stadium in Stadium Data matches the chosen venue countries."
                         if wanted else "Add a ground to Stadium Data before generating a schedule.")
    pitches = list(pitch_registry.SELECTABLE)
    if config["format"] == "ODI":
        from engine.realism import odi
        supported = set(odi.new_settings()["pitch"])
        pitches = [pitch for pitch in pitches if pitch in supported]
    if not pitches:
        raise ValueError("This format has no pitch its pinned reference can model.")
    return sorted(set(venues)), pitches


def generate(config, action, *, overwrite=False):
    if action not in (*ACTIONS, "all"):
        raise ValueError("Unknown generator.")
    updated = deepcopy(config)
    for step in ACTIONS if action == "all" else (action,):
        if step == "schedule":
            venues, pitches = condition_pools(updated)
            if updated["kind"] != "live":
                # A standalone Moment or Boss has no fixtures, so its one ground is drawn
                # onto the event itself. fixture_rules(config, "challenge") reads it from
                # there, and fixture_overrides["challenge"] still wins if it is set.
                updated.update(schedule.draw_conditions(updated, "challenge", venues, pitches))
            manual = updated["kind"] == "live" and updated["schedule"]["preset"] == "manual"
            if manual and updated["schedule"]["playoffs"] in ("none", "custom"):
                # The admin authors every row, league and playoff alike.
                schedule.validate(updated, updated["fixtures"])
            else:
                old = {row["key"]: row for row in updated["fixtures"]}
                if manual:
                    # The generated bracket replaces whatever playoff rows the draft
                    # carries, so they must not come back as preserved manual rows.
                    old = {key: row for key, row in old.items() if row.get("stage") != "playoff"}
                baseline = updated["generation"].get("schedule", {}).get("fields", {})
                generated = schedule.generate(updated, venues=venues, pitches=pitches)
                for row in generated:
                    previous = old.pop(row["key"], {})
                    if previous and previous.get("source", "manual") == "manual" and not overwrite:
                        row.update(previous)
                    else:
                        locks = previous.get("locks", [])
                        saved = baseline.get(row["key"], {})
                        overrides = {field for field, value in saved.items()
                                     if field in previous and previous[field] != value} if not overwrite else set()
                        overrides.update(previous.get("overrides", []) if not overwrite else [])
                        row.update({field: previous[field] for field in set(locks) | overrides})
                        if locks:
                            row["locks"] = locks
                        if overrides:
                            row["overrides"] = sorted(overrides)
                generated.extend(row for row in old.values()
                                 if row.get("locks") or (row.get("source", "manual") == "manual" and not overwrite))
                schedule.validate(updated, generated)
                updated["fixtures"] = generated
        elif step == "objectives" and tournament_goals.tournament(updated):
            edited = False
            if not updated["objectives_locked"]:
                updated["objective_ladders"] = tournament_goals.generate_ladders(updated, overwrite=overwrite)
                updated["tournament_objectives"] = tournament_goals.generate(updated)
            tournament_goals.validate(updated)
        elif step == "objectives":
            previous = updated["generation"].get("objectives", {})
            edited = previous.get("manual") or (previous and
                     previous.get("output") != digest(updated["objectives"]))
            if not updated["objectives_locked"] and (overwrite or not edited):
                updated["objectives"] = rules.auto_objectives(updated)
            rules.validate_objectives(updated)
        else:
            if tournament_goals.tournament(updated):
                if updated["competition"].get("event_pass"):
                    from services import event_studio_items, event_studio_pass
                    updated["milestones"] = event_studio_pass.generate(updated, overwrite=overwrite)
                    event_studio_items.generate(updated)
                elif not updated["milestones"]:
                    updated["milestones"] = rules.auto_milestones(updated)
            proposed = rules.auto_rewards(updated)
            previous = updated["generation"].get("rewards", {})
            preserved = set(updated["reward_locks"])
            if not overwrite:
                preserved.update(previous.get("manual", []))
                base = previous.get("fields", {})
                for tier, outcomes in updated["rewards"].items():
                    for outcome, bundle in outcomes.items():
                        for asset, amount in bundle.items():
                            if previous and amount != base.get(tier, {}).get(outcome, {}).get(asset):
                                preserved.add(f"{tier}.{outcome}.{asset}")
            for lock in preserved:
                tier, result, asset = lock.split(".")
                try:
                    amount = updated["rewards"][tier][result][asset]
                except KeyError as exc:
                    raise ValueError(f"Locked reward field {lock} has no value.") from exc
                proposed.setdefault(tier, {}).setdefault(result, {})[asset] = amount
            updated["rewards"] = proposed
            rules.validate_rewards(updated)
        output_key = "fixtures" if step == "schedule" else step
        if step == "objectives" and tournament_goals.tournament(updated):
            output_key = "tournament_objectives"
        generated_metadata = {"version": 2 if output_key == "tournament_objectives" else 1, "input": _inputs(updated, step),
                              "output": digest(updated[output_key])}
        if step == "objectives" and updated.get("ladder_generator"):
            from services.event_objective_generator import GENERATOR_VERSION
            generated_metadata["ladder_version"] = GENERATOR_VERSION
        if step == "schedule":
            generated_metadata["fields"] = {
                row["key"]: {key: value for key, value in row.items()
                             if key not in ("locks", "overrides", "source", "key")}
                for row in updated["fixtures"]
            }
        elif step == "objectives" and edited and not overwrite:
            generated_metadata["manual"] = True
        elif step == "rewards":
            generated_metadata["fields"] = deepcopy(updated["rewards"])
            if preserved - set(updated["reward_locks"]):
                generated_metadata["manual"] = sorted(preserved - set(updated["reward_locks"]))
        updated["generation"][step] = generated_metadata
    return updated


def stale(config):
    return [key for key in ACTIONS if key in config["generation"]
            and config["generation"][key].get("input") != _inputs(config, key)]


def lock(db, model, key, *, skip_locked=False):
    db.flush()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(update(model).where(model.id == key).values(id=model.id)
                   .execution_options(synchronize_session=False))
    return db.scalar(select(model).where(model.id == key).with_for_update(skip_locked=skip_locked)
                     .execution_options(populate_existing=True))


def revision(db, event, *, published=False):
    return db.get(StudioRevision, (event.id, event.published_number if published else event.draft_number))


def _record(db, event, actor, action, config):
    row = StudioRevision(event_id=event.id, number=event.draft_number, actor=actor,
                         action=action, config=config, digest=digest(config))
    db.add(row)
    db.flush()
    return row


def create(db, name, slug, actor, initial=None):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or not KEY.fullmatch(slug):
        raise ValueError("Enter a name and a lowercase slug with up to 40 letters, digits, hyphens or underscores.")
    if db.scalar(select(StudioEvent.id).where(StudioEvent.slug == slug)) is not None:
        raise ValueError("That event slug is already in use.")
    event = StudioEvent(name=name.strip(), slug=slug)
    db.add(event)
    db.flush()
    _record(db, event, actor, "create", normalize(initial if initial is not None else {**defaults(), "objective_scope": "tournament"}))
    return event


def save(db, event, expected, config, actor, action="save"):
    if event is None or event.status != "draft" or expected != event.draft_number:
        raise Conflict("The draft changed. Reload it before saving.")
    event.draft_number += 1
    return _record(db, event, actor, action, normalize(config))


def validate_publish(db, config, now):
    if not rules.FORMATS[config["format"]]["engine"]:
        raise ValueError("This format has no qualified Events Approach adapter.")
    opens = schedule.utc_instant(config["opens_at"], config["timezone"])
    closes = schedule.utc_instant(config["closes_at"], config["timezone"])
    if not opens < closes or closes <= now or (closes - opens).days > 365:
        raise ValueError("Event timestamps must form an upcoming window no longer than one year.")
    if len(config["teams"]) < 2 or (config["kind"] != "live" and len(config["teams"]) != 2):
        raise ValueError("Standalone challenges require two teams. Live Events require at least two.")
    if config["kind"] != "live" and config["mode"] != ["tournament_team"]:
        raise ValueError("Standalone challenges require the fixed Tournament Team track.")
    if config["kind"] != "live" and closes - opens <= timedelta(minutes=config["schedule"]["grace"]):
        raise ValueError("A challenge needs a start window longer than its grace period.")
    if stale(config):
        raise ValueError("Regenerate the stale sections before publishing: " + ", ".join(stale(config)))
    schedule.validate(config, config["fixtures"])
    keys = {row["key"] for row in config["fixtures"]} if config["kind"] == "live" else {"challenge"}
    for key, override in config["fixture_overrides"].items():
        if key not in keys:
            raise ValueError(f"{key}: remove the override or restore its fixture.")
        if "objectives" in override and len(override["objectives"]) != len(config["objectives"]):
            raise ValueError(f"{key}: fixture objectives must have the same maximum stars as the event.")
    rules.validate_objectives(config)
    tournament_goals.validate(config, publishing=True)
    if tournament_goals.tournament(config):
        modes = config["competition"]["modes"]
        if "multi" in modes:
            if config["schedule"]["preset"] == "survival":
                raise ValueError("Multiplayer requires a shared schedule. Survival is Solo only.")
            if config["competition"]["min_humans"] > min(config["max_users"], len(config["teams"])):
                raise ValueError("The minimum human count exceeds the team or event capacity.")
            earliest = min(schedule.utc_instant(row["deadline"], config["timezone"]) for row in config["fixtures"])
            if earliest - max(opens, now) <= timedelta(minutes=90):
                raise ValueError("Allow 60 minutes of registration and at least 30 minutes to play the first fixture.")
    maximum = rules.validate_rewards(config)
    from services import event_studio_items, event_studio_pass
    event_studio_pass.validate(config)
    event_studio_items.validate(config)
    inventory_snapshot = event_studio_items.freeze(db, config) if event_studio_pass.enabled(config) else {}

    frozen = snapshot_teams(db, config)
    pitches = {config["pitch"],
               *(row["pitch"] for row in config["fixtures"] if "pitch" in row),
               *(row["pitch"] for row in config["fixture_overrides"].values() if "pitch" in row)}
    if config["format"] == "ODI":
        from engine.realism import odi
        reference = odi.new_settings()
        if pitches - reference["pitch"].keys():
            raise ValueError("Choose an ODI pitch supported by its pinned reference.")
        context = {**config, "snapshot": frozen, "engine": reference["version"], "odi_settings": reference,
                   "reference_hash": reference["reference_sha256"]}
    else:
        from engine import ground_config
        reference = deepcopy(ground_config.get_config())
        if reference:
            reference["active_game_mode"] = "natural_game"
        if not reference or any(not ground_config.get_scoring_matrix(pitch, config=reference) for pitch in pitches):
            raise ValueError("The pitch has no ground reference to pin.")
        context = {**config, "snapshot": frozen, "engine": rules.FORMATS["T20"]["engine"],
                   "ground_reference": reference, "reference_hash": digest(reference)}
        from engine.realism import baselines, config as realism_config, integration
        if integration.live_for():
            measured = baselines.load(format_name="T20")
            profile = {"reference": deepcopy({key: value for key, value in measured.items() if key != "_smoothed"}),
                       "competition": baselines.competition_key(measured),
                       "tuning": deepcopy(realism_config.get_config()),
                       "over_volatility": realism_config.over_volatility_enabled()}
            context.update(realism_profile=profile, realism_profile_hash=digest(profile))
    if inventory_snapshot:
        context["inventory_snapshot"] = inventory_snapshot
    if config["ai_managers"]["enabled"]:
        if not flag("AI_PERSONALITIES"):
            raise ValueError("AI manager publication requires the staging rollout flag.")
        from secrets import token_hex
        context["ai_manager_seed"] = token_hex(32)
    template = config["schedule"].get("template")
    previous_id = config["schedule"].get("previous_event_id")
    if template == "mystery":
        from secrets import token_hex
        context["mystery_seed"] = token_hex(32)
    if previous_id:
        previous = db.get(StudioEvent, previous_id)
        before = revision(db, previous, published=True) if previous and previous.published_number else None
        if not before or previous.closes_at > opens or before.config.get("objective_scope") != "tournament":
            raise ValueError("Link a published tournament that closes before the new event opens.")
        if template == "dynasty" and set(frozen) != set(before.config["snapshot"]):
            raise ValueError("Dynasty editions must retain the same published franchise keys.")
        if template == "champions" and not before.config["competition"].get("honour_board"):
            raise ValueError("A champions-only predecessor must have a canonical Honour Board.")
        context["previous_edition"] = {"id": previous_id, "revision": previous.published_number, "name": previous.name}
    prerequisite = config["chapter"]["prerequisite_slug"]
    if prerequisite:
        previous = db.scalar(select(StudioEvent).where(StudioEvent.slug == prerequisite,
                                                       StudioEvent.published_number.is_not(None)))
        if previous is None or previous.opens_at >= enrollment_closes(config):
            raise ValueError("Choose a published chapter prerequisite that opens before enrollment closes.")
        previous_config = revision(db, previous, published=True).config
        if not rules.is_moment(previous_config) or config["chapter"]["min_stars"] > len(previous_config["objectives"]) + 1:
            raise ValueError("The prerequisite must be a playable Moment with enough available stars.")
        context["chapter_requirement"] = {"event_id": previous.id, "revision": previous.published_number,
                                          "name": previous.name, "min_stars": config["chapter"]["min_stars"]}
    if rules.is_moment(config):
        from services.event_studio_cricket import build_moment
        context["moment_state"] = build_moment(rules.fixture_rules(context, "challenge"))
    return context, opens, closes, maximum


def publish(db, event, expected, actor, now):
    if event is None or event.status != "draft" or expected != event.draft_number:
        raise Conflict("The draft changed. Reload it before publishing.")
    config = revision(db, event).config
    if not allowed(config["kind"], config["format"]):
        raise ValueError("Publishing is disabled for this mode or format.")
    if tournament_goals.tournament(config):
        from services.event_studio_competition import mode_enabled
        if not any(mode_enabled(config, mode) for mode in ("solo", "multi")):
            raise ValueError("Enable Solo or qualify this format for Multiplayer before publishing.")
    if (("own_roster" in config["mode"] and config.get("difficulty") is None)
            or config.get("difficulty") == "hard_plus"
            or any(row.get("difficulty") == "hard_plus" for row in config["fixture_overrides"].values())
            or ("own_roster" in config["mode"] and any("difficulty" in row and row["difficulty"] is None
                                                       for row in config["fixture_overrides"].values()))) and not flag("HARD_PLUS"):
        raise ValueError("Own Roster publication requires the Hard++ staging flag. Calibration has yet to establish a tactical win-rate improvement.")
    frozen, opens, closes, maximum = validate_publish(db, config, now)
    editions = rotation_editions(frozen, opens, closes, maximum)
    first = _publish_revision(db, event, *editions[0], actor, now)
    for index, (edition, starts, ends) in enumerate(editions[1:], 2):
        suffix = f"-edition-{index}"
        slug = event.slug[:40 - len(suffix)] + suffix
        name = f"{event.name[:104]} · {index}"
        child = create(db, name, slug, actor, {key: edition[key] for key in defaults()})
        _publish_revision(db, child, edition, starts, ends, actor, now)
    return first


def _publish_revision(db, event, frozen, opens, closes, actor, now):
    event.draft_number += 1
    event.published_number = event.draft_number
    event.opens_at = opens
    event.closes_at = closes
    event.status = "scheduled" if now < opens else "live"
    return _record(db, event, actor, "publish", frozen)


def rotation_editions(config, opens, closes, maximum):
    """Build bounded, prevalidated editions from one approved Moment template."""
    rotation = config["rotation"]
    cadence = rotation["cadence"]
    if cadence == "none":
        return [(config, opens, closes)]
    if not rules.is_moment(config):
        raise ValueError("Daily and weekly rotation requires a Moment checkpoint.")
    periods = rotation["periods"]
    step = timedelta(days=1 if cadence == "daily" else 7)
    zone = ZoneInfo(config["timezone"])
    count = {asset: amount * periods for asset, amount in maximum.items()}
    if periods > 1:
        for asset, cost in count.items():
            if cost > rotation["series_budget"].get(asset, 0):
                raise ValueError(f"{asset}: the rotation liability of {cost:,} exceeds the series budget.")

    def shifted(stamp, offset):
        local = stamp.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
        return schedule.utc_instant((local + offset).isoformat(), config["timezone"])

    editions = [(config, opens, closes)]
    from services.event_studio_cricket import build_moment
    for index in range(1, periods):
        opening = shifted(opens, step * index)
        closing = shifted(closes, step * index)
        if opening < editions[-1][2]:
            raise ValueError("The published window overlaps the next rotation edition.")
        if closing - opening <= timedelta(minutes=config["schedule"]["grace"]):
            raise ValueError("A rotation edition needs a play window longer than its grace period.")
        seed = int(digest([config["moment"]["seed"], index, schedule.utc_text(opening)])[:8], 16)
        # Enrollment is derived from the edition's own window, so it shifts with it.
        upcoming = {**config, "opens_at": schedule.utc_text(opening), "closes_at": schedule.utc_text(closing),
                    "moment": {**config["moment"], "seed": seed},
                    "rotation": {**rotation, "periods": 1, "series_budget": {}},
                    "generation": {}}
        try:
            upcoming["moment_state"] = build_moment(rules.fixture_rules(upcoming, "challenge"))
        except ValueError as exc:
            raise ValueError(f"Rotation edition {index + 1}: {exc}") from exc
        editions.append((upcoming, opening, closing))
    return editions
