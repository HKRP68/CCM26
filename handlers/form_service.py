"""Player Form — tracks recent performance, modifies in-match outcomes.

Each player has a "form score" (-10 to +10) computed from their last 5 matches:
  - Batting: weighted by runs, fifties, hundreds, ducks
  - Bowling: weighted by wickets, economy, 5fers
  - Form decays over time (matches 1-5 ago weighted differently)

In-match: form score is converted to a small effective rating modifier
(typically ±2 OVR). It nudges, doesn't dominate.

Form modifiers:
  +6 to +10 = "On Fire" (visible green badge in match flow)
   +2 to +5 = "In Form"
   -1 to +1 = "Steady"
   -5 to -2 = "Off Form"
  -10 to -6 = "Out of Form" (visible red badge)
"""

import logging
from sqlalchemy import desc

from models import PlayerFormHistory, Player

logger = logging.getLogger(__name__)

# Window: last N matches contribute to form
FORM_WINDOW = 5
# Cap on the rating modifier this can produce
MAX_FORM_MOD = 2.5  # ±2.5 OVR


def record_match_performance(session, user_id, player_id, match_id,
                             runs=0, balls=0, out=False,
                             wickets=0, runs_conceded=0, overs_bowled=0.0):
    """Record one match performance for one player.
    Call from _save_match_stats after computing per-player aggregates."""
    try:
        h = PlayerFormHistory(
            user_id=user_id, player_id=player_id, match_id=match_id,
            runs=runs, balls=balls, out=out,
            wickets=wickets, runs_conceded=runs_conceded, overs_bowled=overs_bowled,
        )
        session.add(h)
        session.flush()
        # Trim — keep only last 5 records per (user, player)
        all_rows = (session.query(PlayerFormHistory)
                    .filter(PlayerFormHistory.user_id == user_id,
                            PlayerFormHistory.player_id == player_id)
                    .order_by(desc(PlayerFormHistory.recorded_at)).all())
        for old in all_rows[FORM_WINDOW:]:
            session.delete(old)
        session.flush()
    except Exception:
        logger.exception(f"record_match_performance failed for u={user_id} p={player_id}")


def get_form_records(session, user_id, player_id):
    """Most-recent first."""
    return (session.query(PlayerFormHistory)
            .filter(PlayerFormHistory.user_id == user_id,
                    PlayerFormHistory.player_id == player_id)
            .order_by(desc(PlayerFormHistory.recorded_at))
            .limit(FORM_WINDOW).all())


def compute_form_score(session, user_id, player_id, player_category=None):
    """Returns int (-10..+10).

    Compares last-5 performance to baseline.
    - For batsmen: avg runs vs expected baseline
    - For bowlers: avg wickets vs expected baseline
    - For all-rounders: weighted blend
    """
    rows = get_form_records(session, user_id, player_id)
    if not rows:
        return 0  # no history → neutral form

    # Get player to know category
    player = session.query(Player).get(player_id)
    if not player:
        return 0
    cat = player_category or player.category

    # Recency weighting (most recent counts most)
    weights = [1.0, 0.85, 0.7, 0.55, 0.4][:len(rows)]
    total_w = sum(weights)

    bat_score = 0.0
    bowl_score = 0.0
    bat_weight_sum = 0.0
    bowl_weight_sum = 0.0

    for w, h in zip(weights, rows):
        # Batting contribution
        if h.balls > 0:
            # Score: runs/15 - 3 means +1 form per 15 runs above 0, -3 baseline
            r_score = (h.runs / 15.0) - 3.0
            # Cap at ±5 per match
            r_score = max(-5.0, min(5.0, r_score))
            bat_score += w * r_score
            bat_weight_sum += w
        # Bowling contribution
        if h.overs_bowled and h.overs_bowled > 0:
            # Score: wickets * 2 - econ deviation
            wk_score = h.wickets * 2.0
            balls = max(1, int(h.overs_bowled * 6))
            econ = (h.runs_conceded / balls) * 6
            # Penalize bad econ above 9, reward below 6
            econ_dev = (7.5 - econ) / 1.5  # roughly -2..+2
            econ_dev = max(-3.0, min(3.0, econ_dev))
            bowl_score += w * (wk_score + econ_dev)
            bowl_weight_sum += w

    bat_avg = (bat_score / bat_weight_sum) if bat_weight_sum else 0
    bowl_avg = (bowl_score / bowl_weight_sum) if bowl_weight_sum else 0

    # Blend by category
    if cat == "Batsman" or cat == "Wicket Keeper":
        score = bat_avg
    elif cat == "Bowler":
        score = bowl_avg
    elif cat == "All-rounder":
        score = (bat_avg + bowl_avg) / 2
    else:
        score = bat_avg

    # Clamp to -10..+10
    score = max(-10.0, min(10.0, score))
    return int(round(score))


def form_label(score):
    """Player-facing label for a form score."""
    if score >= 6:  return "🔥 On Fire"
    if score >= 2:  return "📈 In Form"
    if score <= -6: return "🥶 Out of Form"
    if score <= -2: return "📉 Off Form"
    return "▪️ Steady"


def form_to_rating_mod(score):
    """Convert form score (-10..+10) to a rating modifier (-MAX..+MAX OVR)."""
    return (score / 10.0) * MAX_FORM_MOD


def get_form_summary(session, user_id, player_id):
    """Returns dict for display: score, label, history."""
    score = compute_form_score(session, user_id, player_id)
    return {
        "score": score,
        "label": form_label(score),
        "modifier": round(form_to_rating_mod(score), 2),
        "history": get_form_records(session, user_id, player_id),
    }
