"""Claimed-star reward road on the canonical competition entitlement ledger."""

from math import ceil
import re

from services import event_studio_items as inventory
from services import event_studio_objectives as objectives, event_studio_rules as rules
from services.event_studio_schedule import utc_text


def enabled(config):
    return config.get("competition", {}).get("event_pass", False)


def source(milestone):
    return f'pass:{milestone["id"]}'


def baseline_stars(config):
    """Upper bound on accessible stars before playoffs on the shortest guaranteed path."""
    per_fixture = {"wins": 1, "played_matches": 1, "consecutive_wins": 1, "successful_chases": 1,
                   "successful_defenses": 1, "wickets": 10, "team_dismissals": 10,
                   "three_wicket_hauls": 3, "five_wicket_hauls": 2, "qualification": 0, "champion": 0}
    overs = rules.FORMATS[config["format"]]["overs"]
    per_fixture.update(maidens=overs, bowling_dot_balls=overs * 6)
    total = 0
    for row in objectives.active_rows(config):
        eligible = set(row.get("stages", objectives.STAGES)) & {"league", "survival"}
        matches = objectives.guaranteed(config, eligible)
        if not matches or row.get("min_balls", 0) > matches * overs * 6:
            continue
        max_per_match = per_fixture.get(row["metric"])
        if max_per_match is not None and row["target"] > matches * max_per_match:
            continue
        total += row["stars"]
    return total


def generate(config, *, overwrite=False):
    existing = config["milestones"]
    if existing and not overwrite:
        return existing
    retained = [row for row in existing if row.get("locked")]
    ceiling = baseline_stars(config)
    if not ceiling:
        return retained
    occupied = {row["stars"] for row in retained}
    steps = sorted({ceil(ceiling * i / 4) for i in range(1, 5)} - occupied)[:20 - len(retained)]
    if not steps:
        return retained
    fraction = 35 if config["competition"]["match_rewards"] else 100
    divisor = config["max_users"]
    bundles = {}
    for asset in rules.ASSETS:
        fixed = sum(max(row.get(tier, {}).get(asset, 0) for tier in rules.TIERS) for row in retained)
        available = config["budget"].get(asset, 0) // divisor - fixed
        if available < 0:
            raise ValueError(f"{asset}: locked Event Pass milestones exceed the budget.")
        amount = min(1_000_000, available * fraction // (100 * len(steps)))
        if amount:
            bundles[asset] = amount
    if not bundles and not any(config.get("pass_inventory", {}).get("auto_" + tier) for tier in rules.TIERS):
        return retained
    if not bundles:
        steps = steps[-1:]
    used_ids = {row["id"] for row in retained}
    for target in steps:
        key = f"milestone-{target}"
        while key in used_ids:
            key += "-auto"
        used_ids.add(key)
        retained.append({"id": key, "stars": target, "diamond": dict(bundles),
                         "free": {asset: max(1, amount // 2) for asset, amount in bundles.items()}})
    return sorted(retained, key=lambda row: row["stars"])


def validate(config):
    if not enabled(config):
        return
    if not objectives.tournament(config):
        raise ValueError("Event Pass requires tournament objectives.")
    baseline = baseline_stars(config)
    previous, ids = 0, set()
    for row in config["milestones"]:
        if set(row) - {"id", "stars", "free", "diamond", "free_items", "diamond_items", "locked", "bonus"}:
            raise ValueError("Invalid Event Pass milestone fields.")
        key = row.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", key) or key in ids:
            raise ValueError("Use a unique milestone ID.")
        ids.add(key)
        stars = row["stars"]
        if stars <= previous:
            raise ValueError("Event Pass thresholds must strictly increase.")
        previous = stars
        if any(type(row.get(field, False)) is not bool for field in ("locked", "bonus")):
            raise ValueError("Invalid milestone lock or bonus.")
        if any(not any(row.get(tier, {}).values()) and not row.get(tier + "_items") for tier in rules.TIERS):
            raise ValueError("Both Event Pass tracks must award a supported asset.")
        if not row.get("bonus") and stars > baseline:
            raise ValueError(f"{key}: baseline milestone exceeds the non-playoff star maximum.")
    if not config["milestones"]:
        raise ValueError("Add at least one Event Pass milestone.")


def view(config, entrant, rewards, expires, now):
    by_source = {row["source"]: row for row in rewards}
    locked_state = "expired" if expires <= now else "locked"
    next_target = next((row["stars"] for row in config["milestones"]
                        if row["stars"] > entrant.claimed_stars), None) if expires > now else None
    return {"claimed": entrant.claimed_stars, "next_target": next_target, "expires_at": utc_text(expires),
            "milestones": [{**row, "item_preview": inventory.previews(config, row), "state": by_source.get(source(row), {}).get("status", locked_state),
                            "reward": by_source.get(source(row))}
                           for row in config["milestones"]]}
