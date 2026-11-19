"""Timed, shared bidding using the RCPL player pool, price ladder and squad rules."""

import time

from services import auction_league_service as AL

LOT_SECONDS = 20
AI_DELAY = 4


def start(state, *, now):
    # A group auction starts with a fresh player pool and equal purses.
    state["phase"] = AL.PHASE_AUCTION
    state["sets"] = AL.build_sets(state, AL.rng_for(state, "group sets"))
    state["accelerated_done"] = True
    _open_lot(state, now)


def next_event_at(state):
    lot = state["lot"]
    return min(lot["ai_at"] or lot["deadline"], lot["deadline"])


def _open_lot(state, now):
    pid = AL._advance_cursor(state)
    if pid is None:
        AL.finish_auction(state)
        return
    card = AL.card(state, pid)
    rng = AL.rng_for(state, f"group lot {pid}")
    values = {team: AL.ai_value(state, team, card, rng)
              for team in state["team_order"] if team not in state["managers"]}
    state["lot_seq"] += 1
    current = AL.current_set(state)
    base = AL.base_price(card["rating"])
    state["lot"] = {
        "seq": state["lot_seq"], "pid": pid, "base": base, "price": None,
        "leader": None, "status": "open", "bids": 0, "trail": [],
        "set_name": current["name"], "set_no": state["set_idx"] + 1,
        "deadline": now + LOT_SECONDS, "ai_at": _ai_time(values, base, now),
        "values": values,
    }


def _ai_time(values, price, now, leader=None):
    return now + AI_DELAY if any(v >= price and team != leader for team, v in values.items()) else None


def _raise(lot, team, price):
    lot.update(price=price, leader=team, bids=lot["bids"] + 1)
    lot["trail"] = (lot["trail"] + [[team, price]])[-6:]


def _quoted_lot(state, seq, expected_price, now):
    lot = state.get("lot")
    if state["phase"] != AL.PHASE_AUCTION or not lot or lot["seq"] != seq:
        raise AL.AuctionLeagueError("This lot has ended. Open the current auction board.")
    if AL.next_price(lot) != expected_price:
        raise AL.AuctionLeagueError("The bid changed. Check the current price.")
    if now >= lot["deadline"]:
        raise AL.AuctionLeagueError("Bidding has closed for this lot.")
    return lot


def bid(state, user_id, seq, expected_price, *, now=None):
    now = time.time() if now is None else now
    lot = _quoted_lot(state, seq, expected_price, now)
    team = next((name for name, manager in state["managers"].items()
                 if manager["id"] == user_id), None)
    if team is None:
        raise AL.AuctionLeagueError("Only a franchise manager can bid in this room.")
    if lot["leader"] == team:
        raise AL.AuctionLeagueError("Your franchise already leads.")
    if expected_price > AL.max_bid(state, team, AL.card(state, lot["pid"])):
        raise AL.AuctionLeagueError("This bid would exceed your purse or squad limits.")
    _raise(lot, team, expected_price)
    lot["deadline"] = now + LOT_SECONDS
    lot["ai_at"] = _ai_time(lot["values"], AL.next_price(lot), now, team)


def _close(state):
    lot = state["lot"]
    if lot["leader"] is not None:
        AL._sell(state, lot["leader"], lot["price"], AL.HOW_AUCTION)
    else:
        lot["status"] = "unsold"
        state["unsold"].append(lot["pid"])
        AL._close_lot(state)


def finish_early(state, seq, expected_price, *, now=None):
    _quoted_lot(state, seq, expected_price, time.time() if now is None else now)
    _close(state)
    AL.finish_auction(state)


def tick(state, *, now):
    if state["phase"] != AL.PHASE_AUCTION or next_event_at(state) > now:
        return False
    lot = state["lot"]
    if now >= lot["deadline"]:
        _close(state)
        _open_lot(state, now)
        return True

    # Resolve competing AI bids in one burst, giving managers a full window to respond.
    ceilings = lot["values"]
    rng = AL.rng_for(state, f"group bids {lot['seq']}")
    bids_before = lot["bids"]
    for _ in range(AL.BID_LIMIT):
        step = AL.next_price(lot)
        willing = [team for team, ceiling in ceilings.items() if team != lot["leader"] and ceiling >= step]
        if not willing:
            break
        team = rng.choices(willing, weights=[ceilings[name] - step + 1 for name in willing], k=1)[0]
        _raise(lot, team, step)
    lot["ai_at"] = None
    if lot["bids"] != bids_before:
        lot["deadline"] = now + LOT_SECONDS
    return True
