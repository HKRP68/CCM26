"""Versioned objectives and conservative liability for supported wallet assets."""

from math import floor

ASSETS = {"coins": "total_coins", "gems": "total_gems", "quest_points": "quest_points"}
STUDIO_ENGINE = "approach-t20-v1"
FORMATS = {"T20": {"overs": 20, "engine": STUDIO_ENGINE},
           "ODI": {"overs": 50, "engine": "odi-approach-1"}}

OBJECTIVES = {
    "win": ("Win the match", 1),
    "boundaries": ("Hit boundaries", 200),
    "wickets": ("Take wickets", 10),
    "runs": ("Score runs", 600),
}
RESULTS = ("win", "loss", "tie", "success")
TIERS = ("free", "diamond")


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}.")
    return value


def is_moment(config):
    return config["kind"] == "moment" or (config["kind"] == "boss" and config["challenge_style"] == "moment")


def auto_objectives(config):
    if is_moment(config):
        if config["moment"]["side"] == "bat":
            return [{"key": "boundaries", "threshold": max(1, config["moment"]["overs"] - 1)}]
        return [{"key": "wickets", "threshold": 1}]
    odi = config["format"] == "ODI"
    return [{"key": "win", "threshold": 1},
            {"key": "boundaries", "threshold": 12 if odi else 5, "min_balls": 24 if odi else 12},
            {"key": "wickets", "threshold": 4 if odi else 3, "min_balls": 24 if odi else 12}]


def validate_objectives(config):
    rows = config["objectives"]
    if not isinstance(rows, list) or len(rows) > (4 if is_moment(config) else 5):
        raise ValueError("Choose up to four Moment extras or five full-match objectives.")
    seen = set()
    for row in rows:
        key = row["key"]
        if key not in OBJECTIVES or key in seen:
            raise ValueError(f"Unsupported or duplicate objective {key}.")
        integer(row["threshold"], key, 1, OBJECTIVES[key][1])
        moment = config["moment"] if is_moment(config) else None
        inclusive = moment and moment.get("objective_baseline") == "inclusive"
        window = ((moment["overs"] + (moment["start_over"] if inclusive else 0)) * 6
                  if moment else FORMATS[config["format"]]["overs"] * 6)
        integer(row.get("min_balls", 0), f"{key} minimum legal balls", 0, window)
        if key == "win" and row.get("min_balls"):
            raise ValueError("A match-result objective has no minimum delivery count.")
        if moment:
            allowed = {"runs", "boundaries"} if moment["side"] == "bat" else {"wickets"}
            if key not in allowed:
                raise ValueError(f"{key} cannot be measured for this Moment's role.")
            reach = {"runs": moment["goal"] if moment["goal_type"] == "segment" else window * 6,
                     "boundaries": window, "wickets": 10}
            if not inclusive and row["threshold"] > reach[key]:
                raise ValueError(f"{key} exceeds the Moment's configured opportunity.")
        seen.add(key)


def evaluate(assignments, metrics, opportunities):
    values = []
    for row in assignments:
        key = row["key"]
        measured = metrics[key]
        met = measured >= row["threshold"]
        sufficient = opportunities.get(key, 0) >= row.get("min_balls", 0)
        values.append({"key": key, "title": OBJECTIVES[key][0], "threshold": row["threshold"],
                       "measured": measured, "met": met,
                       "status": "met" if met else "missed" if sufficient else "not_applicable"})
    return values


def candidate_paths(config):
    if config["kind"] != "live":
        return [["challenge"]]
    return [
        [row["key"] for row in config["fixtures"]
         if (team["key"] in (row["home"], row["away"])
             or row["stage"] == "playoff" and any(":" in row[side] for side in ("home", "away")))]
        for team in config["teams"]
    ]


def max_sources(config):
    return max(map(len, candidate_paths(config)), default=0)


def reward_peak(rewards, results, asset):
    return max((rewards.get(tier, {}).get(key, {}).get(asset, 0)
                for key in results for tier in TIERS), default=0)


def fixture_rules(config, key):
    overrides = config["fixture_overrides"].get(key)
    return {**config, **overrides} if overrides else config


def liability(config):
    ruling = ("win", "loss", "tie") if config["kind"] == "live" else ("success",)
    paths = candidate_paths(config)
    payout = {key: fixture_rules(config, key)["rewards"] for path in paths for key in path}
    return {asset: config["max_users"] * (
        max((sum(reward_peak(payout[key], ruling, asset) for key in path) for path in paths), default=0)
        + milestone_liability(config, asset)) for asset in ASSETS}


def milestone_liability(config, asset):
    return sum(max(row.get(tier, {}).get(asset, 0) for tier in TIERS)
               for row in config["milestones"])


def _validate_bundle(bundle):
    if not isinstance(bundle, dict):
        raise ValueError("A reward bundle must be an asset object.")
    for asset, amount in bundle.items():
        if asset not in ASSETS:
            raise ValueError(f"Unsupported reward asset {asset}.")
        integer(amount, asset, 0, 1_000_000)


def _validate_tracks(rewards):
    for tier, rows in rewards.items():
        if tier not in TIERS:
            raise ValueError(f"Unknown reward tier {tier}.")
        for key, bundle in rows.items():
            if key not in RESULTS:
                raise ValueError(f"Rewards cannot be assigned to {key}.")
            _validate_bundle(bundle)
    for key in RESULTS:
        free = rewards.get("free", {}).get(key, {})
        diamond = rewards.get("diamond", {}).get(key, {})
        if any(diamond.get(asset, 0) < amount for asset, amount in free.items()):
            raise ValueError(f"Diamond must award at least the Free amount for {key}.")


def validate_rewards(config, *, require_budget=True):
    for asset, limit in config["budget"].items():
        if asset not in ASSETS:
            raise ValueError(f"Unsupported reward asset {asset}.")
        integer(limit, f"{asset} budget", 0, 2_000_000_000)
    _validate_tracks(config["rewards"])
    for override in config["fixture_overrides"].values():
        if "rewards" in override:
            _validate_tracks(override["rewards"])
    thresholds = set()
    max_stars = (len(config["objectives"]) + 1 if is_moment(config)
                 else max_sources(config) * len(config["objectives"]))
    for row in config["milestones"]:
        if require_budget and not max_stars:
            raise ValueError("Star milestones need a schedule and star objectives.")
        threshold = integer(row["stars"], "Milestone stars", 1, max_stars if require_budget else 1200)
        if threshold in thresholds:
            raise ValueError("Choose distinct milestone thresholds.")
        thresholds.add(threshold)
        free, diamond = row.get("free", {}), row.get("diamond", {})
        _validate_bundle(free)
        _validate_bundle(diamond)
        if any(diamond.get(asset, 0) < amount for asset, amount in free.items()):
            raise ValueError("Diamond must award at least the Free amount for each milestone.")
    maximum = liability(config)
    for asset, cost in maximum.items() if require_budget else ():
        if cost > config["budget"].get(asset, 0):
            raise ValueError(f"{asset}: maximum liability {cost:,} exceeds the event budget.")
    return maximum


def auto_rewards(config):
    counts = config["max_users"] * max_sources(config)
    if not counts:
        raise ValueError("Add teams and generate fixtures before generating rewards.")
    free = {"win": {"coins": 1500, "gems": 10, "quest_points": 30},
            "loss": {"coins": 150, "quest_points": 5}, "tie": {"coins": 300, "quest_points": 10},
            "success": {"coins": 1000, "gems": 5, "quest_points": 20}}
    ruling = ("win", "loss", "tie") if config["kind"] == "live" else ("success",)
    free = {key: free[key] for key in ruling}
    overrides = {key: override["rewards"] for key, override in config["fixture_overrides"].items()
                 if "rewards" in override}
    paths = candidate_paths(config)
    scaled = {}
    for asset in ASSETS:
        default_peak = max(2 * row.get(asset, 0) for row in free.values())
        scales = [1]
        for path in paths:
            fixed = sum(reward_peak(overrides[key], ruling, asset) for key in path if key in overrides)
            budget = (config["budget"].get(asset, 0)
                      - config["max_users"] * (fixed + milestone_liability(config, asset)))
            if budget < 0:
                raise ValueError(f"{asset}: locked fixture and milestone liability exceeds the budget.")
            peak = config["max_users"] * default_peak * sum(key not in overrides for key in path)
            if peak:
                scales.append(budget / peak)
        scaled[asset] = min(scales)
    return {
        tier: {key: {asset: floor(amount * multiplier * scaled[asset]) for asset, amount in bundle.items()}
               for key, bundle in free.items()}
        for tier, multiplier in (("free", 1), ("diamond", 2))
    }
