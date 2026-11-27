"""Server-side selection rules shared by CIPL and official event checkpoints."""

from engine.approach_modifiers import BATTING_APPROACHES, BOWLING_APPROACHES
from services import cipl_match
from services.match_state_store import (
    A_PICK_BAT_APPROACH, A_PICK_BOWL_APPROACH, A_PICK_CIPL_BOWLER, A_PICK_CIPL_NEW_BATSMAN,
)

PICK_BOWLER = "bowler"
PICK_BOWL_APPROACH = "bowl_approach"
PICK_BAT_APPROACH = "bat_approach"
PICK_NEW_BATSMAN = "new_batsman"

PICK_ACTION = {
    PICK_BOWLER: A_PICK_CIPL_BOWLER,
    PICK_BOWL_APPROACH: A_PICK_BOWL_APPROACH,
    PICK_BAT_APPROACH: A_PICK_BAT_APPROACH,
    PICK_NEW_BATSMAN: A_PICK_CIPL_NEW_BATSMAN,
}
ALREADY_CHOSEN = {
    PICK_BOWLER: "Bowler already chosen.",
    PICK_BOWL_APPROACH: "Already chosen.",
    PICK_BAT_APPROACH: "Already chosen.",
    PICK_NEW_BATSMAN: "The new batsman is already in.",
}


def validate(state, next_action, kind, value):
    """Return the selected player or Approach key for the current turn."""
    if kind not in PICK_ACTION or type(value) is not int:
        raise ValueError("Invalid selection.")
    if next_action != PICK_ACTION[kind]:
        raise ValueError(ALREADY_CHOSEN[kind])
    if kind == PICK_BOWLER:
        for player in cipl_match.eligible_bowlers(state):
            if player["roster_id"] == value:
                return player
        raise ValueError("That bowler isn't eligible for this over.")
    if kind == PICK_NEW_BATSMAN:
        if not cipl_match.needs_new_batsman(state):
            raise ValueError(ALREADY_CHOSEN[kind])
        for player in cipl_match.available_batsmen(state):
            if player["roster_id"] == value:
                return player
        raise ValueError("That batsman isn't available.")
    options = BOWLING_APPROACHES if kind == PICK_BOWL_APPROACH else BATTING_APPROACHES
    if not 0 <= value < len(options):
        raise ValueError("Invalid approach.")
    return options[value][0]
