"""Timed group bidding using the RCPL player pool, price ladder and squad rules."""

from itertools import islice
import time

from services import auction_league_service as AL

INTRO_SECONDS = 45
LOT_SECONDS = 30
BETWEEN_LOTS_SECONDS = 2
MIN_RESUME_SECONDS = 8
AI_DELAY = 4
CLOSING_CALLS = (10, 3)


def start(state, *, now):
    state["phase"] = AL.PHASE_AUCTION
    state["sets"] = AL.build_sets(state, AL.rng_for(state, "group sets"))
    state["next_lot_at"] = now + INTRO_SECONDS


def next_event_at(state):
    if state.get("paused_at") is not None:
        return None
    lot = state.get("lot")
    if lot is None:
        return state["next_lot_at"]
    deadline = lot["deadline"]
    next_call = next((seconds for seconds in CLOSING_CALLS
                      if seconds < lot.get("closing_call", LOT_SECONDS)), 0)
    return min(lot["ai_at"] or deadline, deadline - next_call)


def _queue_accelerated(state):
    state["accelerated_done"] = True
    rng = AL.rng_for(state, "group accelerated round")
    signed = AL.taken_pids(state)
    candidates = [AL.card(state, pid) for pid in state["unsold"] if pid not in signed]
    nominations = set()
    for team in state["team_order"]:
        owed = AL.owed_roles(state, team)
        ranked = sorted(candidates, key=lambda c: (not owed[c["category"]], -c["rating"]))
        affordable = (c["id"] for c in ranked if AL.max_bid(state, team, c) >= AL.base_price(c["rating"]))
        nominations.update(islice(affordable, AL.ACCEL_NOMS_AI))
    if nominations:
        pids = sorted(nominations)
        rng.shuffle(pids)
        state["sets"].append({"name": "Accelerated round", "pids": pids, "accelerated": True})


def _open_lot(state, now):
    pid = AL._advance_cursor(state, nominate=False)
    if pid is None and not state.get("accelerated_done"):
        _queue_accelerated(state)
        pid = AL._advance_cursor(state, nominate=False)
    state.pop("next_lot_at", None)
    if pid is None:
        AL.finish_auction(state)
        return
    card = AL.card(state, pid)
    rng = AL.rng_for(state, f"group lot {pid}")
    current = AL.current_set(state)
    accelerated = current["accelerated"]
    values = {team: AL.ai_value(state, team, card, rng, accelerated=accelerated)
              for team in state["team_order"] if team not in state["managers"]}
    state["lot_seq"] += 1
    base = AL.base_price(card["rating"])
    state["lot"] = {
        "seq": state["lot_seq"], "pid": pid, "base": base, "price": None,
        "leader": None, "status": "open", "bids": 0, "trail": [],
        "set_name": current["name"], "set_no": state["set_idx"] + 1,
        "accelerated": accelerated,
        "values": values,
    }
    _reset_clock(state["lot"], now)


def _reset_clock(lot, now, *, reschedule_ai=True):
    lot["deadline"] = now + LOT_SECONDS
    lot["closing_call"] = LOT_SECONDS
    lot["ai_at"] = None
    if reschedule_ai:
        next_price = AL.next_price(lot)
        if any(v >= next_price and team != lot["leader"] for team, v in lot["values"].items()):
            lot["ai_at"] = now + AI_DELAY


def _raise(lot, team, price):
    lot.update(price=price, leader=team, bids=lot["bids"] + 1)
    lot["trail"] = (lot["trail"] + [[team, price]])[-6:]


def _require_auction(state):
    if state["phase"] != AL.PHASE_AUCTION:
        raise AL.AuctionLeagueError("The RCPL auction isn't running.")


def _check_quote(lot, seq, expected_price):
    if lot is None or lot["seq"] != seq:
        raise AL.AuctionLeagueError("This lot has ended. Open the current auction board.")
    if AL.next_price(lot) != expected_price:
        raise AL.AuctionLeagueError("The bid changed. Check the current price.")


def bid(state, user_id, seq=None, expected_price=None, *, now=None):
    now = time.time() if now is None else now
    _require_auction(state)
    lot = state.get("lot")
    if seq is not None:
        _check_quote(lot, seq, expected_price)
    if state.get("paused_at") is not None:
        raise AL.AuctionLeagueError("The host paused the auction.")
    if not lot:
        raise AL.AuctionLeagueError("Bidding opens with the next lot.")
    if now >= lot["deadline"]:
        raise AL.AuctionLeagueError("Bidding has closed for this lot.")
    team = next((name for name, manager in state["managers"].items()
                 if manager["id"] == user_id), None)
    if team is None:
        raise AL.AuctionLeagueError("Only a franchise manager can bid in this room.")
    if lot["leader"] == team:
        raise AL.AuctionLeagueError("Your franchise already leads.")
    price = AL.next_price(lot)
    if price > AL.max_bid(state, team, AL.card(state, lot["pid"])):
        raise AL.AuctionLeagueError("This bid would exceed your purse or squad limits.")
    _raise(lot, team, price)
    _reset_clock(lot, now)


def pause(state, *, now):
    _require_auction(state)
    if state.get("paused_at") is not None:
        raise AL.AuctionLeagueError("The auction is already paused.")
    lot = state.get("lot")
    if lot and now >= lot["deadline"]:
        raise AL.AuctionLeagueError("The lot is closing. Pause after the hammer.")
    state["paused_at"] = now


def resume(state, *, now):
    _require_auction(state)
    paused_at = state.pop("paused_at", None)
    if paused_at is None:
        raise AL.AuctionLeagueError("The auction isn't paused.")
    lot = state.get("lot")
    if lot:
        lot["deadline"] = now + max(MIN_RESUME_SECONDS, lot["deadline"] - paused_at)
        lot["closing_call"] = LOT_SECONDS
        if lot["ai_at"] is not None:
            lot["ai_at"] = now + max(0, lot["ai_at"] - paused_at)
    else:
        state["next_lot_at"] = now + max(BETWEEN_LOTS_SECONDS, state["next_lot_at"] - paused_at)


def _close(state):
    lot = state["lot"]
    if lot["leader"] is not None:
        AL._sell(state, lot["leader"], lot["price"], AL.HOW_AUCTION)
    else:
        lot["status"] = "unsold"
        if not lot.get("accelerated"):
            state["unsold"].append(lot["pid"])
        AL._close_lot(state)


def finish_early(state, seq=None, expected_price=None):
    _require_auction(state)
    lot = state.get("lot")
    if seq is not None:
        _check_quote(lot, seq, expected_price)
    if lot:
        _close(state)
    state.pop("paused_at", None)
    state.pop("next_lot_at", None)
    AL.finish_auction(state)


def tick(state, *, now):
    if state["phase"] != AL.PHASE_AUCTION:
        return False
    due = next_event_at(state)
    if due is None or now < due:
        return False
    lot = state.get("lot")
    if lot is None:
        _open_lot(state, now)
    elif now >= lot["deadline"]:
        _close(state)
        state["next_lot_at"] = now + BETWEEN_LOTS_SECONDS
    elif lot["ai_at"] is not None and now >= lot["ai_at"]:
        _run_ai(state, lot, now)
    if lot and state.get("lot") is lot:
        left = lot["deadline"] - now
        lot["closing_call"] = min((seconds for seconds in CLOSING_CALLS if left <= seconds),
                                  default=LOT_SECONDS)
    return True


def _run_ai(state, lot, now):
    # Resolve competing AI bids in one burst before giving managers a full window to respond.
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
        _reset_clock(lot, now, reschedule_ai=False)
