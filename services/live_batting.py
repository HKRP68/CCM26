"""Match-local batting changes. Temporary retirement never records a wicket.

Keep the two crease slots and the consumed prefix of the order intact. A
retirement exchanges the player in a crease slot with a waiting player; the
retiree occupies that waiting slot and can return through the normal picker.
All figures stay keyed by roster id, including confidence and milestones.
"""
import hashlib
import json

from services.match_state_store import (
    A_PICK_CIPL_BOWLER, A_PICK_BOWL_APPROACH, A_PICK_BAT_APPROACH,
    A_PICK_DELIVERY, A_PICK_NEW_BOWLER,
)

RETIRED_LABEL = "Retired — can return"
SAFE_ACTIONS = {A_PICK_CIPL_BOWLER, A_PICK_BOWL_APPROACH, A_PICK_BAT_APPROACH,
                A_PICK_DELIVERY, A_PICK_NEW_BOWLER}


def stat(state, rid):
    rows = state.get("bat_stats") or {}
    return rows.get(str(rid), rows.get(rid, {}))


def _row(state, rid):
    rows = state.setdefault("bat_stats", {})
    if str(rid) in rows:
        return rows[str(rid)]
    if rid in rows:
        return rows[rid]
    rows[str(rid)] = {}
    return rows[str(rid)]


def eligible(player, stats):
    return (player.get("active", True) is not False
            and not player.get("injured") and not player.get("concussion_replaced")
            and not stats.get("out") and not stats.get("retired_hurt"))


def waiting_indices(state):
    crease = (state.get("striker_idx"), state.get("non_striker_idx"))
    return [i for i, p in enumerate(state.get("batting_order") or [])
            if i not in crease and eligible(p, stat(state, p.get("roster_id")))]


def token(state, next_action):
    """Bind a selection to its innings, delivery, crease and available order."""
    values = [state.get(k) for k in (
        "match_id", "innings", "current_over", "current_ball", "total_runs",
        "total_wickets", "striker_idx", "non_striker_idx", "bat_user_tg")]
    values += [next_action, state.get("live_batting_revision", 0), [
        [str(p.get("roster_id")), p.get("active", True),
         bool(stat(state, p.get("roster_id")).get("out"))]
        for p in state.get("batting_order") or []]]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:12]


def blocked_reason(state, next_action):
    if not state or not state.get("batting_order"):
        return "The batting innings has not started."
    if state.get("played_via") == "wsp":
        return "This match is auto-simulated."
    if state.get("mode") in ("super_over", "superover") or state.get("is_super_over"):
        return "Finish the Super Over before changing batting."
    if state.get("setup") not in (None, "DONE"):
        return "Finish innings setup before changing batting."
    if state.get("concussion_pending"):
        return "Choose the concussion substitute first."
    bpu = 5 if state.get("ball_format") == "The100" else 6
    played = (state.get("current_over", 1) - 1) * bpu + state.get("current_ball", 0)
    if (state.get("total_wickets", 0) >= state.get("wicket_limit", 10)
            or played >= state.get("overs", 20) * bpu
            or (state.get("target") is not None
                and state.get("total_runs", 0) >= state["target"])):
        return "This innings is already over."
    if next_action not in SAFE_ACTIONS or state.get("over_in_progress"):
        return "Change batting between overs or before the next delivery."
    order = state["batting_order"]
    slots = [state.get("striker_idx"), state.get("non_striker_idx")]
    if (any(not isinstance(i, int) or not 0 <= i < len(order) for i in slots)
            or slots[0] == slots[1]
            or any(not eligible(order[i], stat(state, order[i]["roster_id"])) for i in slots)):
        return "Select the incoming batsman first."
    return None


def _card(state, player):
    st = stat(state, player["roster_id"])
    return {"id": player["roster_id"], "name": player.get("name", "Batter"),
            "runs": st.get("runs", 0), "balls": st.get("balls", 0),
            "retired": bool(st.get("retired"))}


def options(state, next_action, *, can_control):
    reason = blocked_reason(state, next_action)
    order = state.get("batting_order") or []
    at_crease = [_card(state, order[i]) for i in
                 (state.get("striker_idx"), state.get("non_striker_idx"))
                 if isinstance(i, int) and 0 <= i < len(order)]
    return {"canManage": bool(can_control and not reason),
            "message": reason or "Changes apply to this innings only.",
            "token": token(state, next_action),
            "revision": state.get("live_batting_revision", 0),
            "atCrease": at_crease,
            "waiting": [_card(state, order[i]) for i in waiting_indices(state)]}


def _validate(state, next_action, expected_token):
    reason = blocked_reason(state, next_action)
    if reason:
        raise ValueError(reason)
    if not expected_token or expected_token != token(state, next_action):
        raise ValueError("The match moved on. Reopen Manage batting and choose again.")


def arrive(state, player):
    """Clear temporary retirement when the player returns; retain all figures."""
    row = _row(state, player["roster_id"])
    row.pop("retired", None)
    row["batted"] = True


def _record(state, kind, text, **fields):
    entry = {"type": kind, "text": text, "innings": state.get("innings", 1),
             "over": state.get("current_over", 1), "ball": state.get("current_ball", 0),
             **fields}
    state.setdefault("batting_changes", []).append(entry)
    state["commentary_log"] = (list(state.get("commentary_log") or []) + [entry])[-60:]
    state["live_batting_revision"] = state.get("live_batting_revision", 0) + 1
    return text


def retire(state, next_action, outgoing_id, incoming_id, expected_token):
    _validate(state, next_action, expected_token)
    order = state["batting_order"]
    outgoing = next((i for i in (state["striker_idx"], state["non_striker_idx"])
                     if str(order[i]["roster_id"]) == str(outgoing_id)), None)
    incoming = next((i for i in waiting_indices(state)
                     if str(order[i]["roster_id"]) == str(incoming_id)), None)
    if outgoing is None:
        raise ValueError("Choose a batsman currently at the crease.")
    if incoming is None:
        raise ValueError("Choose an available batsman from your playing XI.")
    old, new = order[outgoing], order[incoming]
    bs, new_bs = stat(state, old["roster_id"]), stat(state, new["roster_id"])
    returning = bool(new_bs.get("retired"))
    partner_index = (state["non_striker_idx"] if outgoing == state["striker_idx"]
                     else state["striker_idx"])
    if state.get("partnership_runs") or state.get("partnership_balls"):
        state.setdefault("partnership_history", []).append({
            "wicket": state.get("total_wickets", 0), "batsman1": old.get("name"),
            "batsman2": order[partner_index].get("name"),
            "runs": state.get("partnership_runs", 0),
            "balls": state.get("partnership_balls", 0),
            "notout": True, "ended_by": "retirement"})
    state["partnership_runs"] = state["partnership_balls"] = 0
    _row(state, old["roster_id"])["retired"] = True
    _row(state, old["roster_id"])["batted"] = True
    order[outgoing], order[incoming] = new, old
    arrive(state, new)
    saved = f"{bs.get('runs', 0)} ({bs.get('balls', 0)})"
    new_score = f"{new_bs.get('runs', 0)} ({new_bs.get('balls', 0)})"
    text = (f"{old.get('name')} retires on {saved} and can return. "
            f"{new.get('name')} {'resumes on' if returning else 'comes in on'} {new_score}. "
            "No wicket added.")
    return _record(state, "retirement", text, outgoing_id=old["roster_id"],
                   incoming_id=new["roster_id"])


def reorder(state, next_action, roster_ids, expected_token):
    _validate(state, next_action, expected_token)
    positions = waiting_indices(state)
    order = state["batting_order"]
    if not isinstance(roster_ids, list):
        raise ValueError("Provide the full order of available batsmen.")
    ids = [str(rid) for rid in roster_ids]
    players = {str(order[i]["roster_id"]): order[i] for i in positions}
    if len(ids) != len(players) or len(set(ids)) != len(ids) or set(ids) != set(players):
        raise ValueError("Include every available batsman exactly once.")
    if ids == [str(order[i]["roster_id"]) for i in positions]:
        raise ValueError("The batting order is unchanged.")
    for i, rid in zip(positions, ids):
        order[i] = players[rid]
    text = "Batting order updated: " + " → ".join(players[rid].get("name", "Batter") for rid in ids)
    return _record(state, "batting_order", text, roster_ids=roster_ids)
