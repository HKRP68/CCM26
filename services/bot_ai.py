"""AI decision module for bot opponents in /vsbot.

When the bot team is bowling, this picks deliveries (variation + length).
When the bot team is batting, this picks shots.

Strategy:
  - Phase-aware (Powerplay aggressive bowling, Death wicket-hunting)
  - Slight randomness so matches feel different
  - Difficulty-based: Easy = mostly safe choices, Legendary = wicket-hunting + Slog
"""

import random
import logging

from services.bowling_service import get_delivery_options, is_spinner, AVAILABLE_SHOTS

logger = logging.getLogger(__name__)


# Bot tg_id sentinel (no real user has a negative Telegram ID)
BOT_TG_ID = -1


# ════════════════════════════════════════════════════════════════════
# Bowling AI — picks a delivery (variation + length)
# ════════════════════════════════════════════════════════════════════

# Phase-weighted preferences
BOWLING_PHASE_PREFS = {
    "Powerplay": {  # overs 1-6 — restrict & take wickets
        "lengths": ["Good", "Good Length", "Hard", "Short of Length", "Back of Length"],
        "weights": [3, 3, 2, 1, 1],
    },
    "Middle": {     # overs 7-15 — control, mix it up
        "lengths": ["Good", "Good Length", "Short of Length", "Back of Length", "Hard"],
        "weights": [3, 3, 2, 2, 1],
    },
    "Death": {      # overs 16-20 — yorkers and bouncers
        "lengths": ["Yorker", "Full", "Full Length", "Bouncer", "Good"],
        "weights": [4, 2, 2, 1, 1],
    },
}


def _phase_for_over(over, total_overs=20):
    if total_overs <= 5:
        if over <= max(1, total_overs // 3): return "Powerplay"
        if over >= total_overs - 1: return "Death"
        return "Middle"
    if over <= 6: return "Powerplay"
    if over <= 15: return "Middle"
    return "Death"


def pick_bot_delivery(bowler, over, total_overs, difficulty="Medium"):
    """Choose a delivery for the AI bowler.

    Returns dict: {"variation": str, "length": str | None, "is_spinner": bool, "delivery": str}
    """
    opts = get_delivery_options(bowler.get("bowl_style", "Medium Pacer"),
                                bowler.get("bowl_hand", "Right"))
    phase = _phase_for_over(over, total_overs)

    if opts["is_spinner"]:
        # Spinners pick from variations only
        deliveries = opts["deliveries"]
        # Avoid 'Surprise' on Easy difficulty
        if difficulty == "Easy" and "Surprise" in deliveries:
            deliveries = [d for d in deliveries if d != "Surprise"]
        chosen = random.choice(deliveries) if deliveries else "Off Break"
        if chosen == "Surprise":
            ns = [x for x in opts["deliveries"] if x != "Surprise"]
            if ns:
                chosen = random.choice(ns) + " (Surprise)"
        return {
            "variation": chosen, "length": None,
            "is_spinner": True, "delivery": chosen,
        }
    else:
        # Pacer: pick variation + length
        variations = opts["variations"]
        variation = random.choice(variations) if variations else "Seam Up"

        # Length weighted by phase
        prefs = BOWLING_PHASE_PREFS.get(phase, BOWLING_PHASE_PREFS["Middle"])
        # Filter to lengths actually available for this bowler style
        available_lengths = opts["lengths"]
        weighted = [(l, w) for l, w in zip(prefs["lengths"], prefs["weights"])
                    if l in available_lengths]
        if not weighted:
            length = random.choice(available_lengths) if available_lengths else "Good"
        else:
            ls = [l for l, _ in weighted]
            ws = [w for _, w in weighted]
            length = random.choices(ls, weights=ws, k=1)[0]

        return {
            "variation": variation, "length": length,
            "is_spinner": False, "delivery": f"{variation} {length}",
        }


# ════════════════════════════════════════════════════════════════════
# Batting AI — picks a shot
# ════════════════════════════════════════════════════════════════════

# Phase-weighted shot preferences
BATTING_PHASE_PREFS = {
    "Powerplay": {  # overs 1-6 — boundary hunting
        "shots": ["Drive", "Cut", "Pull", "Flick", "Slog", "Loft"],
        "weights": [4, 3, 3, 2, 2, 2],
    },
    "Middle": {     # overs 7-15 — anchor + rotate
        "shots": ["Flick", "Drive", "Cut", "Sweep", "Pull"],
        "weights": [5, 3, 2, 2, 1],
    },
    "Death": {      # overs 16-20 — go big or go home
        "shots": ["Slog", "Loft", "Pull", "Drive"],
        "weights": [5, 4, 2, 1],
    },
}


def pick_bot_shot(striker, bowler, over, total_overs,
                  current_runs, current_wickets, target=None,
                  current_ball=0, difficulty="Medium"):
    """Choose a shot for the AI batsman.

    Returns: shot_name (str), shot_index (int into AVAILABLE_SHOTS)
    """
    phase = _phase_for_over(over, total_overs)

    # Compute required run rate if chasing
    rrr = 0.0
    if target:
        runs_left = max(0, target - current_runs)
        balls_played = (over - 1) * 6 + current_ball
        balls_left = (total_overs * 6) - balls_played
        if balls_left > 0:
            rrr = (runs_left / balls_left) * 6

    # Adjust phase preferences based on RRR
    if rrr > 12 and phase != "Powerplay":
        # Desperate — go aggressive
        prefs = BATTING_PHASE_PREFS["Death"]
    elif rrr > 9:
        # Need quick runs — boundary mix
        if phase == "Middle":
            shots = ["Drive", "Slog", "Loft", "Pull", "Cut"]
            weights = [3, 3, 2, 2, 2]
            prefs = {"shots": shots, "weights": weights}
        else:
            prefs = BATTING_PHASE_PREFS[phase]
    elif current_wickets >= 7 and phase != "Death":
        # Lost lots of wickets — anchor more, occasional defend
        prefs = {
            "shots": ["Flick", "Drive", "Defend", "Cut"],
            "weights":  [3, 3, 2, 1],
        }
    else:
        prefs = BATTING_PHASE_PREFS[phase]

    shots = list(prefs["shots"])
    weights = list(prefs["weights"])

    # Easy difficulty: less Slog (lower wicket risk)
    if difficulty == "Easy":
        weights = [w * 0.5 if s in ("Slog",) else w
                   for s, w in zip(shots, weights)]

    # Legendary: more aggressive
    if difficulty == "Legendary":
        weights = [w * 1.5 if s in ("Slog", "Loft", "Pull") else w
                   for s, w in zip(shots, weights)]

    # ── Rating-aware shot selection ──
    # A tail-ender shouldn't tee off like an opener: weak batters play
    # within themselves (unless the chase is truly desperate), stars back
    # their power game. Keeps a bot XI's innings shaped like a real one.
    _AGGRESSIVE = ("Slog", "Loft", "Pull")
    bat_rating = 50.0
    try:
        bat_rating = float(striker.get("bat_rating") or striker.get("rating") or 50)
    except (TypeError, ValueError, AttributeError):
        pass
    desperate = rrr > 12  # must swing regardless of ability
    if bat_rating < 45 and not desperate:
        weights = [w * 0.35 if s in _AGGRESSIVE else w
                   for s, w in zip(shots, weights)]
        # Survival instinct: the rabbit blocks and nudges for singles.
        if "Defend" not in shots:
            shots.append("Defend")
            weights.append(2.5)
        if "Flick" not in shots:
            shots.append("Flick")
            weights.append(2.0)
    elif bat_rating < 60 and not desperate:
        weights = [w * 0.7 if s in _AGGRESSIVE else w
                   for s, w in zip(shots, weights)]
    elif bat_rating >= 85:
        weights = [w * 1.3 if s in _AGGRESSIVE else w
                   for s, w in zip(shots, weights)]

    chosen_shot = random.choices(shots, weights=weights, k=1)[0]
    # Find index in AVAILABLE_SHOTS
    try:
        idx = AVAILABLE_SHOTS.index(chosen_shot)
    except ValueError:
        idx = 0
        chosen_shot = AVAILABLE_SHOTS[0]
    return chosen_shot, idx


# ════════════════════════════════════════════════════════════════════
# Bowler rotation — picks who bowls next
# ════════════════════════════════════════════════════════════════════

def _overs_done(bowl_stats, roster_id):
    """Read a bowler's completed-overs count, tolerant of roster-id keys that
    JSON serialization may have stringified (live state round-trips through the
    match-state store, so ``bowl_stats`` keys are strings while ``roster_id`` in
    the XI stays an int). A raw ``.get(int_key)`` would miss the string key and
    silently report 0 overs, letting a bowler blow past their quota."""
    row = bowl_stats.get(roster_id)
    if row is None:
        row = bowl_stats.get(str(roster_id), {})
    return row.get("overs_done", 0)


def pick_bot_next_bowler(bowl_xi, prev_bowler_rid, bowl_stats, total_overs):
    """Pick next bowler from XI:
    - Cannot be previous bowler
    - Cannot exceed quota (ceil(total_overs / 5) per bowler)
    - Prefer best bowl_rating with overs remaining
    """
    # Per-bowler over cap = ceil(total_overs / 5), min 1 (e.g. 20→4, 10→2, 6→2).
    quota = max(1, -(-int(total_overs) // 5))
    bowl_stats = bowl_stats or {}
    candidates = []
    for p in bowl_xi:
        if p["roster_id"] == prev_bowler_rid:
            continue
        if _overs_done(bowl_stats, p["roster_id"]) >= quota:
            continue
        candidates.append(p)

    if not candidates:
        # Allow consecutive if no choice
        candidates = [p for p in bowl_xi if p["roster_id"] != prev_bowler_rid] or bowl_xi[:]

    # Prefer high bowl_rating
    candidates.sort(key=lambda x: x["bowl_rating"], reverse=True)
    # Top 3 by rating, pick weighted-randomly
    top = candidates[:3] if len(candidates) >= 3 else candidates
    weights = [3, 2, 1][:len(top)]
    return random.choices(top, weights=weights, k=1)[0]


# ════════════════════════════════════════════════════════════════════
# New batsman picker
# ════════════════════════════════════════════════════════════════════

def pick_bot_next_batsman(batting_order, striker_idx, non_striker_idx, bat_stats):
    """Pick next batsman from batting order: first available not-out player."""
    for i, p in enumerate(batting_order):
        if i in (striker_idx, non_striker_idx):
            continue
        bs = bat_stats.get(p["roster_id"], {})
        if not bs.get("out", False):
            return i, p
    return None, None
