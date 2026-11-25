"""Review and settle Mini App purchases, player sales and conversions."""

import json
from datetime import datetime, timedelta
from uuid import uuid4

from config import COINS_PER_GEM, MAX_ROSTER, TRAIT_SHOP_DAILY_PURCHASE_LIMIT, get_buy_value, get_sell_value
from models import GlobalPlayerMarket, GlobalTraitMarket, MiniappEconomyIntent, Pack, Player, Trait, UnopenedPack, UserRoster
from services import global_market
from services.activity_service import log_activity
from services.economy_transactions import debit_coins
from services.roster_service import get_roster_ordered


class EconomyRejected(ValueError):
    """The offer is invalid, unavailable or out of date."""


def sale_cards(db, user, ids):
    rows = get_roster_ordered(db, user.id)
    owned = {row.id: (position, row, player) for position, (row, player) in enumerate(rows, 1)}
    cards = []
    for roster_id in ids:
        if roster_id not in owned:
            raise EconomyRejected("A selected card is no longer in your squad.")
        position, row, player = owned[roster_id]
        if row.sale_locked:
            raise EconomyRejected("Unlock the selected card before selling it.")
        if position <= 11:
            raise EconomyRejected("Replace this player in your Playing XI before selling.")
        if player.is_career or user.captain_roster_id == row.id:
            raise EconomyRejected("A selected card cannot be sold.")
        cards.append({"id": row.id, "player_id": player.id, "name": player.name,
                      "version": player.version or "Base", "rating": player.rating,
                      "acquired_at": row.acquired_date.isoformat() if row.acquired_date else None,
                      "price": get_sell_value(player.rating)})
    return cards


def preview(db, user, action, target):
    if action == "sell":
        cards = sale_cards(db, user, target)
        total = sum(card["price"] for card in cards)
        return {"cards": cards, "price": total, "balance_after": (user.total_coins or 0) + total}
    if action == "browse":
        from services.version_service import user_owns_any_version
        player = db.get(Player, target)
        if not player or not player.is_active or player.is_career or player.restricted_from_buypl:
            raise EconomyRejected("This player is unavailable for direct purchase.")
        if user_owns_any_version(db, user.id, player.id):
            raise EconomyRejected("You already own a version of this player.")
        if (user.roster_count or 0) >= MAX_ROSTER:
            raise EconomyRejected("Make space in your squad before buying.")
        price = get_buy_value(player.rating)
        after = (user.total_coins or 0) - price
        if after < 0:
            raise EconomyRejected(f"You need {-after:,} more coins to buy this player.")
        return {"item_id": player.id, "name": player.name, "version": player.version or "Base",
                "rating": player.rating, "price": price, "balance_after": after}
    if action == "market":
        slot = db.get(GlobalPlayerMarket, target)
        player = db.get(Player, slot.player_id) if slot and slot.is_active else None
        if not player or not player.is_active or player.is_career or global_market.is_sold_out(slot):
            raise EconomyRejected("This listing is no longer available.")
        from services.version_service import user_owns_any_version
        if user_owns_any_version(db, user.id, player.id):
            raise EconomyRejected("You already own a version of this player.")
        if (user.roster_count or 0) >= MAX_ROSTER:
            raise EconomyRejected("Make space in your squad before signing.")
        _, price = global_market.player_slot_prices(user, slot, player)
        after = (user.total_coins or 0) - price
        if after < 0:
            raise EconomyRejected(f"You need {-after:,} more coins to sign this player.")
        return {"slot_id": slot.id, "slot_index": slot.slot_index, "item_id": player.id,
                "name": player.name, "version": player.version or "Base", "rating": player.rating,
                "price": price, "balance_after": after}
    if action == "trait":
        slot = db.get(GlobalTraitMarket, target)
        trait = db.get(Trait, slot.trait_id) if slot and slot.is_active else None
        if not trait or not trait.is_active or global_market.is_sold_out(slot) or slot.final_price < 0:
            raise EconomyRejected("This trait is no longer available.")
        from services.trait_service import _get_or_create_daily
        daily = _get_or_create_daily(db, user.id)
        if (daily.purchases or 0) >= TRAIT_SHOP_DAILY_PURCHASE_LIMIT:
            raise EconomyRejected("You have reached your daily trait purchase limit.")
        after = (user.total_gems or 0) - slot.final_price
        if after < 0:
            raise EconomyRejected(f"You need {-after:,} more gems to buy this trait.")
        return {"slot_id": slot.id, "slot_index": slot.slot_index, "item_id": trait.id,
                "name": trait.name, "price": slot.final_price, "balance_after": after}
    if action == "pack":
        pack = db.get(Pack, target[0])
        currency = target[1]
        columns = {"coins": "cost_coins", "gems": "cost_gems",
                   "quest_points": "cost_quest_points"}
        if not pack or not pack.is_active:
            raise EconomyRejected("This pack is no longer available.")
        price = getattr(pack, columns[currency]) if currency in columns else 0
        if (currency == "free" and any(getattr(pack, column) for column in columns.values())
                or currency != "free" and not price):
            raise EconomyRejected("That pack price is no longer available.")
        wallet = balances(user)
        after = wallet.get(currency, 0) - price
        if after < 0:
            raise EconomyRejected(f"You need {-after:,} more {currency.replace('_', ' ')}.")
        return {"item_id": pack.id, "name": pack.name, "currency": currency, "price": price,
                "balance_after": after}
    if action == "open_pack":
        inventory = db.query(UnopenedPack).filter_by(id=target, user_id=user.id).first()
        pack = db.get(Pack, inventory.pack_id) if inventory else None
        if not pack:
            raise EconomyRejected("This pack is no longer in your inventory.")
        return {"name": pack.name, "item_id": pack.id,
                "acquired_at": inventory.acquired_at.isoformat() if inventory.acquired_at else None,
                "main_count": pack.main_count, "bonus_count": pack.bonus_count,
                "main_min_rating": pack.main_min_rating, "main_max_rating": pack.main_max_rating,
                "bonus_min_rating": pack.bonus_min_rating, "bonus_max_rating": pack.bonus_max_rating}
    if action == "convert":
        gems, leftover = divmod(target, COINS_PER_GEM)
        if not gems or leftover:
            raise EconomyRejected(f"Enter a multiple of {COINS_PER_GEM:,} coins.")
        after = (user.total_coins or 0) - target
        if after < 0:
            raise EconomyRejected(f"You need {-after:,} more coins.")
        if (user.total_gems or 0) + gems >= 2**31:
            raise EconomyRejected("This conversion would exceed your wallet limit.")
        return {"price": target, "gems": gems, "rate": COINS_PER_GEM,
                "balance_after": after, "gems_after": (user.total_gems or 0) + gems}
    raise EconomyRejected("Unknown action.")


def balances(user):
    return {"coins": user.total_coins or 0, "gems": user.total_gems or 0,
            "quest_points": user.quest_points or 0, "roster_count": user.roster_count or 0}


def create_quote(db, user, action, target):
    terms = preview(db, user, action, target)
    now = datetime.utcnow()
    # Expired unused quotes have no effect to audit.
    db.query(MiniappEconomyIntent).filter(
        MiniappEconomyIntent.user_id == user.id, MiniappEconomyIntent.result.is_(None),
        MiniappEconomyIntent.expires_at < now - timedelta(days=1),
    ).delete(synchronize_session=False)
    intent = MiniappEconomyIntent(id=uuid4().hex, user_id=user.id, action=action,
                                quote=json.dumps({"target": target, "terms": terms}),
                                expires_at=now + timedelta(minutes=5))
    db.add(intent)
    db.commit()
    return dict(terms, quote_id=intent.id, expires_in=300, ok=True)


def settle(db, user, intent):
    if intent.result:
        return json.loads(intent.result)
    if intent.expires_at <= datetime.utcnow():
        raise EconomyRejected("This quote expired. Review a new offer.")
    request = json.loads(intent.quote)
    target, terms = request["target"], request["terms"]
    action = intent.action
    live = preview(db, user, action, target)
    if live != terms:
        raise EconomyRejected("Your balance or offer changed. Review the new amounts.")
    db.info["economy_operation"] = intent.id
    receipt = {"ok": True, "state": "succeeded", "receipt": intent.id, "name": terms.get("name")}
    if action == "market":
        ok, message, bonus = global_market.buy_player(
            db, user, terms["slot_index"], expected_slot_id=terms["slot_id"],
            expected_price=terms["price"], expected_player_id=terms["item_id"])
        if not ok:
            raise EconomyRejected(message.replace("<b>", "").replace("</b>", ""))
        price = terms["price"]
        log_activity(db, user.id, "market_buy", f"Signed {terms['name']}",
                     coins_change=-price, player_name=terms["name"], player_rating=terms["rating"])
        receipt.update(player_name=terms["name"], spent=price, gem_bonus=bonus)
    elif action == "browse":
        from services.buy_bonus import award_buy_gem_bonus
        from services.undo_service import record_buy
        player = db.get(Player, terms["item_id"])
        price = terms["price"]
        if not debit_coins(db, user, price):
            raise EconomyRejected("Your balance changed. Review the new offer.")
        user.roster_count = (user.roster_count or 0) + 1
        entry = UserRoster(user_id=user.id, player_id=player.id, order_position=user.roster_count)
        db.add(entry)
        activity = log_activity(db, user.id, "buy", f"Bought {player.name}",
                                coins_change=-price, player_name=player.name, player_rating=player.rating)
        bonus = award_buy_gem_bonus(db, user, player, price, source="miniapp")
        db.flush()
        record_buy(db, user.id, roster_id=entry.id, player_id=player.id, player_name=player.name,
                   rating=player.rating, price=price, gem_bonus=bonus, activity_id=activity.id)
        receipt.update(player_name=player.name, spent=price, gem_bonus=bonus)
    elif action == "sell":
        from handlers.release import _do_release
        lookup = {row.id: (row, player) for row, player in get_roster_ordered(db, user.id)}
        result = _do_release(db, user, [lookup[card["id"]] for card in terms["cards"]])
        if not result["success"]:
            raise EconomyRejected(result["message"])
        receipt.update(total_coins=result["total_coins"], released_count=len(terms["cards"]))
    elif action == "trait":
        from services.trait_service import _get_or_create_daily
        from services.quest_service import safe_track
        ok, message = global_market.buy_trait(db, user, terms["slot_index"],
                                              expected_slot_id=terms["slot_id"],
                                              expected_price=terms["price"], expected_trait_id=terms["item_id"])
        if not ok:
            raise EconomyRejected(message)
        daily = _get_or_create_daily(db, user.id)
        daily.purchases = (daily.purchases or 0) + 1
        safe_track(db, user.id, "trait_buy", 1)
        log_activity(db, user.id, "buy_trait", f"Bought {terms['name']}", gems_change=-terms["price"])
    elif action == "pack":
        from services.pack_service import buy_pack
        pack = db.get(Pack, terms["item_id"])
        result = buy_pack(db, user, pack, request_id=intent.id, payment=terms["currency"],
                          expected_cost=terms["price"])
        if not result["success"]:
            raise EconomyRejected(result["message"])
        price, currency = result["cost_paid"], result["currency"]
        log_activity(db, user.id, "buy_pack", f"Bought {terms['name']}",
                     coins_change=-price if currency == "coins" else 0,
                     gems_change=-price if currency == "gems" else 0,
                     quest_points_change=-price if currency == "quest_points" else 0)
        receipt.update(inventory_id=result["inventory_id"], cost_paid=price, currency=currency)
    elif action == "open_pack":
        from config import get_sell_value
        from services.overflow_service import record_overflow
        from services.pack_service import open_unopened_pack

        result = open_unopened_pack(db, user, target, expected_pack=terms)
        if not result["success"]:
            raise EconomyRejected(result["message"])
        cards = [{"id": p.id, "name": p.name, "rating": p.rating, "category": p.category,
                  "country": p.country, "version": p.version or "Base",
                  "card_url": f"/webapp/player-card/{p.id}", "slot": slot,
                  "sell_value": get_sell_value(p.rating)}
                 for p, slot in result["players"]]
        overflow = [record_overflow(db, user, player, source="pack")
                    for player in result["players_to_claim"]]
        receipt.update(pack_name=terms["name"], players=cards, overflow=overflow,
                       history_id=result["history_id"], pity_triggered=result["pity_triggered"])
    elif action == "convert":
        convert_coins(db, user, terms["price"], terms["gems"])
        receipt["gems"] = terms["gems"]
    receipt["balance"] = balances(user)
    intent.result = json.dumps(receipt)
    db.commit()
    return receipt


def convert_coins(db, user, spend, gems):
    if (gems < 1 or spend != gems * COINS_PER_GEM or (user.total_gems or 0) + gems >= 2**31
            or not debit_coins(db, user, spend)):
        raise EconomyRejected("The amount exceeds your balance or wallet limit.")
    user.total_gems = (user.total_gems or 0) + gems
    log_activity(db, user.id, "coins2gems", f"Converted {spend:,} coins into {gems:,} gems",
                 coins_change=-spend, gems_change=gems)


def reward_result(db, user_id, action, request_id):
    from models import MiniappRewardReceipt

    if request_id:
        row = db.query(MiniappRewardReceipt).filter_by(
            user_id=user_id, action=action, request_id=request_id).first()
        if row:
            return json.loads(row.result)
    return None


def record_reward_result(db, user_id, action, request_id, payload):
    from models import MiniappRewardReceipt

    if request_id:
        payload["receipt"] = request_id
        db.add(MiniappRewardReceipt(user_id=user_id, action=action, request_id=request_id,
                                    result=json.dumps(payload)))
