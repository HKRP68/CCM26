"""Regression test for the END OF OVER summary card stats.

Match state is JSON-round-tripped, so bat_stats / bowl_stats dict keys are
strings (str(roster_id)). The end-of-over card previously read them with the
raw int roster_id and so reported zeros. This test builds a state with
string-keyed stats and asserts the card carries the real figures.
"""
from services.match_webapp_service import _append_commentary_log


def _make_state():
    striker = {"roster_id": 100, "name": "A Striker"}
    non_striker = {"roster_id": 101, "name": "B NonStriker"}
    bowler = {"roster_id": 200, "name": "C Bowler"}
    state = {
        "batting_order": [striker, non_striker],
        "striker_idx": 0,
        "non_striker_idx": 1,
        "current_bowler": bowler,
        # NOTE: string keys, mimicking a JSON round-trip.
        "bat_stats": {
            "100": {"runs": 12, "balls": 6, "fours": 1, "sixes": 1},
            "101": {"runs": 4, "balls": 6},
        },
        "bowl_stats": {
            "200": {"wickets": 1, "runs": 8, "overs_done": 1,
                    "this_over_balls": 0, "last_over_runs": 8},
        },
        "total_runs": 16,
        "total_wickets": 1,
        # over just completed -> current_over advanced, ball reset
        "current_over": 2,
        "current_ball": 0,
        "innings": 1,
        "overs": 2,
        "wicket_limit": 2,
        "commentary_log": [],
    }
    return state, striker, bowler


def _eoo_card(state):
    return next(c for c in state["commentary_log"]
                if c.get("type") == "end_of_over")


def test_end_of_over_card_has_real_stats():
    state, striker, bowler = _make_state()
    res = {"eoo": True, "runs": 1, "type": "single"}

    _append_commentary_log(state, res, striker, bowler, "End of the over")

    card = _eoo_card(state)
    # Bowler figures must not be zeroed by the int/str key mismatch.
    assert card["bowler"]["wickets"] == 1
    assert card["bowler"]["runsConceded"] == 8
    assert card["runsScored"] == 8
    # Batsman figures resolve through the string-keyed stats too.
    assert card["striker"]["runs"] == 12
    assert card["striker"]["balls"] == 6
    assert card["nonStriker"]["runs"] == 4
