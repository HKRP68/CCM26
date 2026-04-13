"""Match engine — state management for ball-by-ball play."""

import random


def create_match_state(match_id, overs, bat_user_id, bowl_user_id,
                       bat_xi, bowl_xi, opener1, opener2, bowler):
    """Create initial match state dict.
    bat_xi/bowl_xi: list of dicts {roster_id, player_id, name, rating, category, bat_rating, bowl_rating, bowl_style, bowl_hand, bat_hand}
    opener1/opener2/bowler: dicts with same keys
    """
    bat_stats = {}
    for p in bat_xi:
        bat_stats[p["roster_id"]] = {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "out": False, "how_out": ""}

    bowl_stats = {}
    for p in bowl_xi:
        bowl_stats[p["roster_id"]] = {"overs": 0.0, "balls": 0, "runs": 0, "wickets": 0, "maidens": 0}

    batting_order = list(bat_xi)

    # Move openers to front
    order = [opener1, opener2]
    for p in batting_order:
        if p["roster_id"] not in (opener1["roster_id"], opener2["roster_id"]):
            order.append(p)
    batting_order = order

    return {
        "match_id": match_id,
        "overs": overs,
        "innings": 1,
        "target": None,
        "bat_team_id": bat_user_id,
        "bowl_team_id": bowl_user_id,
        "bat_xi": bat_xi,
        "bowl_xi": bowl_xi,
        "batting_order": batting_order,
        "current_over": 1,
        "current_ball": 0,  # 0 = start of over, incremented on each legal delivery
        "total_runs": 0,
        "total_wickets": 0,
        "extras": 0,
        "striker_idx": 0,
        "non_striker_idx": 1,
        "next_batsman_idx": 2,
        "current_bowler": bowler,
        "prev_bowler_rid": None,  # can't bowl consecutive overs
        "bowler_this_over_runs": 0,
        "selected_variation": None,  # for 2-step pacer
        "bat_stats": bat_stats,
        "bowl_stats": bowl_stats,
        "over_balls": [],  # runs per ball this over for display
        "chat_id": None,
    }


def get_striker(state):
    return state["batting_order"][state["striker_idx"]]


def get_non_striker(state):
    return state["batting_order"][state["non_striker_idx"]]


def get_bowler(state):
    return state["current_bowler"]


def is_innings_over(state):
    if state["total_wickets"] >= 10:
        return True
    total_balls = (state["current_over"] - 1) * 6 + state["current_ball"]
    if total_balls >= state["overs"] * 6:
        return True
    if state["innings"] == 2 and state["target"] and state["total_runs"] >= state["target"]:
        return True
    return False


def format_score(state):
    return f"{state['total_runs']}/{state['total_wickets']}"


def format_overs(state):
    completed = state["current_over"] - 1
    balls = state["current_ball"]
    if balls == 6:
        return f"{completed + 1}.0"
    return f"{completed}.{balls}"


def run_rate(state):
    total_balls = (state["current_over"] - 1) * 6 + state["current_ball"]
    if total_balls == 0:
        return 0.0
    return round((state["total_runs"] / total_balls) * 6, 2)


def get_phase(state):
    ov = state["current_over"]
    total = state["overs"]
    if total <= 5:
        return "T20 Blast"
    if ov <= 6:
        return "Powerplay"
    elif ov <= total - 4:
        return "Middle Overs"
    else:
        return "Death Overs"
