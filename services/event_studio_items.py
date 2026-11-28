"""Event Pass inventory authoring and grants under the existing account lock."""

from collections import Counter
from hashlib import sha256
from secrets import token_hex

from sqlalchemy import func, select

from config import MAX_ROSTER
from models import Pack, Player, UnopenedPack, UserRoster
from models_events import StudioRewardItem
from services import pack_service
from services.event_studio_rules import TIERS, integer


KINDS = {"pack": "packs", "player": "cards", "random_player": "cards"}


def validate_items(items):
    if not isinstance(items, list) or len(items) > 8:
        raise ValueError("Enter up to eight items per reward track.")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Enter a reward item object.")
        kind = item.get("kind")
        if kind == "pack":
            fields = {"kind", "pack_id"}
            integer(item.get("pack_id"), "Pack ID", 1, 2_147_483_647)
        elif kind == "player":
            fields = {"kind", "player_id"}
            integer(item.get("player_id"), "Player card ID", 1, 2_147_483_647)
        elif kind == "random_player":
            fields = {"kind", "min_rating", "max_rating", "version"}
            minimum = integer(item.get("min_rating"), "Minimum card rating", 1, 200)
            integer(item.get("max_rating"), "Maximum card rating", minimum, 200)
            version = item.get("version")
            if version is not None and (not isinstance(version, str) or not version.strip() or len(version) > 50):
                raise ValueError("Enter a valid player version.")
        else:
            raise ValueError("Unsupported Event Pass item.")
        if item.keys() - fields:
            raise ValueError("Unsupported Event Pass item field.")


def liability(config):
    total = dict.fromkeys(("packs", "cards"), 0)
    for row in config["milestones"]:
        tier_counts = [Counter(KINDS[item["kind"]] for item in row.get(tier + "_items", ())) for tier in TIERS]
        for asset in total:
            total[asset] += config["max_users"] * max(count[asset] for count in tier_counts)
    return total


def validate(config, *, require_budget=True):
    settings = config.get("pass_inventory", {})
    for asset in ("packs", "cards"):
        integer(settings.get(asset, 0), f"{asset} limit", 0, 2_000_000)
    has_items = False
    for row in config["milestones"]:
        if not isinstance(row, dict):
            raise ValueError("Each star milestone must be an object.")
        for tier in TIERS:
            items = row.get(tier + "_items", [])
            validate_items(items)
            has_items |= bool(items)
    for tier in TIERS:
        validate_items(settings.get("auto_" + tier, []))
        has_items |= bool(settings.get("auto_" + tier))
    if has_items and (config["kind"] != "live" or not config["competition"].get("event_pass")):
        raise ValueError("Enable the tournament Event Pass before adding inventory rewards.")
    result = liability(config)
    if require_budget:
        for asset, count in result.items():
            if count > settings.get(asset, 0):
                raise ValueError(f"Maximum {asset} liability {count:,} exceeds its inventory budget.")
    return result


def generate(config):
    settings = config.get("pass_inventory", {})
    if not any(settings.get("auto_" + tier) for tier in TIERS):
        return
    rows = config["milestones"]
    if not any(row.get(tier + "_items") for row in rows for tier in TIERS):
        eligible = next((row for row in reversed(rows) if not row.get("locked")), None)
        if not eligible:
            raise ValueError("Add an unlocked milestone for Auto Rewards inventory items.")
        for tier in TIERS:
            if settings.get("auto_" + tier):
                eligible[tier + "_items"] = list(settings["auto_" + tier])
    validate(config)


def _player_query():
    return select(Player).where(Player.is_active.is_(True), Player.is_career.is_(False))


def freeze(db, config):
    """Pin the card pool and display labels at publication. Pack opening uses its existing definition."""
    frozen = {}
    for row in config["milestones"]:
        for tier in TIERS:
            for index, item in enumerate(row.get(tier + "_items", [])):
                key = f'{row["id"]}:{tier}:{index}'
                kind = item["kind"]
                if kind == "pack":
                    pack = db.get(Pack, item["pack_id"])
                    if not pack or not pack.is_active or pack_service.count_main_pool(db, pack) < 1:
                        raise ValueError("Choose an active reward pack with an available main card.")
                    frozen[key] = {"kind": kind, "pack_id": pack.id, "label": pack.name}
                elif kind == "player":
                    card = db.scalar(_player_query().where(Player.id == item["player_id"]))
                    if not card:
                        raise ValueError("Choose an active collectible reward player.")
                    frozen[key] = {"kind": "player", "player_id": card.id,
                                   "label": f'{card.name} · {card.version or "Base"}'}
                else:
                    query = _player_query().where(Player.rating.between(item["min_rating"], item["max_rating"]))
                    version = item.get("version")
                    if version:
                        query = query.where(func.lower(func.coalesce(Player.version, "Base")) == version.strip().lower())
                    pool = db.scalars(query.with_only_columns(Player.id).order_by(Player.id).limit(5001)).all()
                    if not 0 < len(pool) <= 5000:
                        raise ValueError("A random card reward must match between 1 and 5,000 active cards.")
                    frozen[key] = {"kind": kind, "pool": pool,
                                   "label": f'{item["min_rating"]}–{item["max_rating"]} OVR' +
                                            (f' · {version}' if version else "")}
    return {"items": frozen, "salt": token_hex(32)} if frozen else {}


def bundle(config, entrant, milestone, tier):
    assets = dict(milestone.get(tier, {}))
    frozen = config.get("inventory_snapshot") or {}
    items = []
    for index, _ in enumerate(milestone.get(tier + "_items", [])):
        key = f'{milestone["id"]}:{tier}:{index}'
        item = dict(frozen["items"][key])
        if item["kind"] == "random_player":
            pool = item.pop("pool")
            hashed = sha256(f'{frozen["salt"]}|{entrant.id}|{key}'.encode()).digest()
            item.update(kind="player", player_id=pool[int.from_bytes(hashed, "big") % len(pool)])
        items.append(item)
    if items:
        assets["items"] = items
    return assets


def grant(db, account, reward, now):
    items = reward.assets.get("items", [])
    counts = Counter(item["kind"] for item in items)
    if counts["player"] and (account.roster_count or 0) + counts["player"] > MAX_ROSTER:
        raise ValueError("Free roster slots to claim these Event Pass cards. Your reward remains available.")
    if counts["pack"]:
        owned = db.scalar(select(func.count(UnopenedPack.id)).filter_by(user_id=account.id))
        if owned + counts["pack"] > pack_service.MAX_INVENTORY:
            raise ValueError("Open existing packs to make room for this Event Pass reward.")
    for index, item in enumerate(items):
        if item["kind"] == "pack":
            pack = db.get(Pack, item["pack_id"], with_for_update=True)
            if not pack or not pack_service.count_main_pool(db, pack):
                raise ValueError("This reward pack is temporarily unavailable.")
            pack_row = pack_service.grant_pack(db, account.id, pack.id, source="event_pass")
            detail = {"kind": "pack", "inventory_id": pack_row.id, "name": pack.name}
        else:
            card = db.scalar(_player_query().where(Player.id == item["player_id"]).with_for_update())
            if card is None:
                raise ValueError("This pinned reward card is temporarily unavailable.")
            account.roster_count = (account.roster_count or 0) + 1
            roster = UserRoster(user_id=account.id, player_id=card.id, order_position=account.roster_count,
                                acquired_date=now)
            db.add(roster)
            db.flush()
            detail = {"kind": "player", "roster_id": roster.id, "player_id": card.id,
                      "name": card.name, "version": card.version or "Base", "rating": card.rating}
        db.add(StudioRewardItem(reward_id=reward.id, position=index, detail=detail, granted_at=now))


def receipts(db, ids):
    if not ids:
        return []
    return [{"reward_id": row.reward_id, **row.detail} for row in db.scalars(
        select(StudioRewardItem).where(StudioRewardItem.reward_id.in_(ids))
        .order_by(StudioRewardItem.reward_id, StudioRewardItem.position))]


def previews(config, milestone):
    entries = (config.get("inventory_snapshot") or {}).get("items", {})
    return {tier: [entries[f'{milestone["id"]}:{tier}:{i}']["label"]
                   for i, _ in enumerate(milestone.get(tier + "_items", []))] for tier in TIERS}
