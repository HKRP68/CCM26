"""The AI captain for /lpbot and /ciplbot — the bot's three per-over decisions.

Every over of a Challenge League "Approach" match asks the two captains for
three things: the bowling side picks a bowler, then a Bowling Approach, then the
batting side picks a Batting Approach. When one of those captains is the bot,
these functions supply the answer.

Everything is a pure function of the live ``cipl_approach`` state dict — no
Telegram, no DB — so the tactics can be unit-tested directly. Eligibility,
quotas and the no-back-to-back rule are NOT re-implemented here: they come from
``services.cipl_match.eligible_bowlers``, which the human picker uses too, so
the bot plays under exactly the same constraints as its opponent.

The decisions read the match, not a dice roll: phase, wickets in hand, the
striker's ability and form, and the chase maths all move the answer. A small
weighted-random top is layered on the bowler choice so two matches never unfold
identically.
"""

import logging
import random

from engine.approach_modifiers import (
    BATTING_APPROACHES, BOWLING_APPROACHES, DEFAULT_BATTING, DEFAULT_BOWLING,
)
from services import cipl_match

logger = logging.getLogger(__name__)


# Batting approaches ordered least → most aggressive. The chase logic picks an
# index into this ladder and then nudges it up or down, which keeps "one notch
# more/less aggressive" honest instead of scattering magic strings around.
BAT_LADDER = ["defensive", "rotate", "balanced", "aggressive", "ultra"]

# Overs that count as the death at the end of an innings.
DEATH_UNITS = 4
# Overs that count as the powerplay at the start (scaled down for short formats).
POWERPLAY_UNITS = 6

# Trait bonuses when choosing who bowls, by phase.
_POWERPLAY_TRAITS = {"bowl_powerplay"}
_DEATH_TRAITS = {"bowl_death", "bowl_yorker"}


# ════════════════════════════════════════════════════════════════════
# Match-reading helpers
# ════════════════════════════════════════════════════════════════════

def _total_units(state):
    try:
        return max(1, int(state.get("overs") or 20))
    except (TypeError, ValueError):
        return 20


def _current_unit(state):
    try:
        return max(1, int(state.get("current_over") or 1))
    except (TypeError, ValueError):
        return 1


def phase(state):
    """``"powerplay"`` / ``"middle"`` / ``"death"`` for the upcoming over.

    Scaled for short formats so a 5-over match still has all three phases
    instead of being one long powerplay.
    """
    total = _total_units(state)
    over = _current_unit(state)
    pp = POWERPLAY_UNITS if total >= 20 else max(1, round(total * 0.3))
    death = DEATH_UNITS if total >= 20 else max(1, round(total * 0.2))
    if over <= pp:
        return "powerplay"
    if over > total - death:
        return "death"
    return "middle"


def _units_left(state):
    """Overs still to be bowled in this innings, counting the upcoming one."""
    return max(0, _total_units(state) - _current_unit(state) + 1)


def _death_units_left(state):
    """How many of the remaining overs fall in the death phase."""
    total = _total_units(state)
    death = DEATH_UNITS if total >= 20 else max(1, round(total * 0.2))
    first_death_over = total - death + 1
    return max(0, total - max(_current_unit(state), first_death_over) + 1)


def _traits_of(player):
    return {str((t or {}).get("effect_key") or "")
            for t in (player.get("traits") or []) if isinstance(t, dict)}


def _striker(state):
    order = state.get("batting_order") or []
    idx = state.get("striker_idx", 0)
    if 0 <= idx < len(order):
        return order[idx]
    return {}


def _striker_form(state):
    """``(balls_faced, strike_rate)`` for the batter on strike."""
    striker = _striker(state)
    stats = (state.get("bat_stats") or {}).get(str(striker.get("roster_id")), {})
    balls = int(stats.get("balls", 0) or 0)
    runs = int(stats.get("runs", 0) or 0)
    sr = (runs / balls * 100.0) if balls else 0.0
    return balls, sr


def _wickets_lost(state):
    try:
        return int(state.get("total_wickets") or 0)
    except (TypeError, ValueError):
        return 0


# ════════════════════════════════════════════════════════════════════
# Bowler selection
# ════════════════════════════════════════════════════════════════════

def _bowler_score(state, player, ph, hold_back):
    """Rank a candidate bowler for the upcoming over.

    Rating is the backbone; phase-matched traits add a real but not overwhelming
    bump; a part-timer is a last resort; and in the middle overs the frontline
    quicks are pushed down so their overs survive until the death.
    """
    score = float(player.get("bowl_rating") or 0)
    traits = _traits_of(player)

    if ph == "powerplay" and traits & _POWERPLAY_TRAITS:
        score += 12
    if ph == "death" and traits & _DEATH_TRAITS:
        score += 15
    if ph == "death" and traits & _POWERPLAY_TRAITS:
        score -= 4  # a new-ball specialist is not the answer at the death

    if cipl_match.is_part_time_bowler(player):
        score -= 25

    if player.get("roster_id") in hold_back:
        score -= 30

    return score


def _hold_back_for_death(state, candidates):
    """Roster ids to keep in reserve during the middle overs.

    A captain who burns their two best bowlers by over 14 has nothing left when
    it matters. If the death phase still needs covering, the top two bowlers'
    remaining overs are earmarked for it — unless they have enough spare overs
    to bowl now AND at the death.
    """
    if phase(state) != "middle":
        return set()
    death_left = _death_units_left(state)
    if death_left <= 0:
        return set()
    best = sorted(candidates, key=lambda p: p.get("bowl_rating") or 0, reverse=True)[:2]
    hold = set()
    for p in best:
        if cipl_match.overs_left(state, p) <= death_left:
            hold.add(p.get("roster_id"))
    # Never hold back everyone — somebody has to bowl this over.
    if len(hold) >= len(candidates):
        return set()
    return hold


def pick_bowler(state):
    """Choose the bot's bowler for the upcoming over.

    Only ever returns a player ``cipl_match.eligible_bowlers`` already approved,
    so the quota, the no-back-to-back rule and the part-timer tiers are honoured
    exactly as they are for a human captain. Returns ``None`` only when the XI
    somehow has nobody to bowl.
    """
    candidates = cipl_match.eligible_bowlers(state)
    if not candidates:
        logger.warning("bot captain: no eligible bowlers for match %s",
                       state.get("match_id"))
        return None
    if len(candidates) == 1:
        return candidates[0]

    ph = phase(state)
    hold_back = _hold_back_for_death(state, candidates)
    ranked = sorted(candidates,
                    key=lambda p: _bowler_score(state, p, ph, hold_back),
                    reverse=True)
    # Weighted-random over the best three so the attack isn't robotic, while the
    # top choice still bowls most of the time.
    top = ranked[:3]
    weights = [6, 3, 1][:len(top)]
    return random.choices(top, weights=weights, k=1)[0]


# ════════════════════════════════════════════════════════════════════
# Bowling approach
# ════════════════════════════════════════════════════════════════════

def pick_bowling_approach(state):
    """Choose the bot's Bowling Approach for the over.

    Attack a new batter, mix it up at the death, use variations against someone
    who is set, and shut up shop when a chase has already run away from the
    batting side.
    """
    ph = phase(state)
    balls_faced, sr = _striker_form(state)
    chase = cipl_match.chase(state)

    # Defending, and the chase is comfortably behind — squeeze rather than gamble.
    if chase and chase["balls_remaining"] > 0 and chase["rrr"] >= 13:
        return "defensive"

    if ph == "death":
        return "mixed"

    # A brand-new batter is at their most vulnerable — go after them.
    if balls_faced < 6:
        return "aggressive"

    # Someone flying: change of pace beats more of the same.
    if sr >= 150 and balls_faced >= 8:
        return "variation"

    if ph == "powerplay":
        return random.choices(["aggressive", "balanced"], weights=[3, 2], k=1)[0]

    # Middle overs: control, with the occasional wicket-hunting over.
    return random.choices(["balanced", "variation", "aggressive"],
                          weights=[4, 3, 2], k=1)[0]


# ════════════════════════════════════════════════════════════════════
# Batting approach
# ════════════════════════════════════════════════════════════════════

def _base_bat_index(state):
    """Where on the aggression ladder this situation starts, before adjustments."""
    ph = phase(state)
    chase = cipl_match.chase(state)

    if chase and chase["balls_remaining"] > 0:
        rrr = chase["rrr"]
        if rrr < 6:
            return BAT_LADDER.index("rotate")
        if rrr < 8:
            return BAT_LADDER.index("balanced")
        if rrr < 10:
            return BAT_LADDER.index("aggressive")
        return BAT_LADDER.index("ultra")

    # First innings — build, then accelerate.
    if ph == "powerplay":
        return BAT_LADDER.index("aggressive")
    if ph == "death":
        return BAT_LADDER.index("ultra")
    return BAT_LADDER.index("balanced")


def pick_batting_approach(state):
    """Choose the bot's Batting Approach for the over.

    Situation first (phase, or the required rate when chasing), then adjusted for
    wickets in hand, how close the finish is, and who is actually on strike — a
    number eleven does not slog like a top-order batter unless the chase leaves
    no choice.
    """
    idx = _base_bat_index(state)
    wickets = _wickets_lost(state)
    chase = cipl_match.chase(state)
    desperate = bool(chase and chase["balls_remaining"] > 0 and chase["rrr"] >= 12)

    # Wickets in hand buy risk; wickets lost demand care.
    if wickets >= 8:
        idx -= 2
    elif wickets >= 6:
        idx -= 1

    # The last few overs of an innings are worth swinging at regardless.
    if phase(state) == "death":
        idx += 1

    # Ability check — mirrors the rating-aware shot logic in services/bot_ai.py.
    striker = _striker(state)
    try:
        bat_rating = float(striker.get("bat_rating")
                           or striker.get("rating") or 50)
    except (TypeError, ValueError):
        bat_rating = 50.0
    if not desperate:
        if bat_rating < 45:
            idx -= 1
        elif bat_rating >= 85:
            idx += 1

    # A desperate chase overrides caution: you cannot block your way to a win.
    if desperate:
        idx = max(idx, BAT_LADDER.index("aggressive"))

    idx = max(0, min(len(BAT_LADDER) - 1, idx))
    return BAT_LADDER[idx]


# ════════════════════════════════════════════════════════════════════
# Labels for the chat messages
# ════════════════════════════════════════════════════════════════════

_BAT_LABELS = {k: (e, l) for k, e, l in BATTING_APPROACHES}
_BOWL_LABELS = {k: (e, l) for k, e, l in BOWLING_APPROACHES}


def batting_label(key):
    emoji, label = _BAT_LABELS.get(key, _BAT_LABELS[DEFAULT_BATTING])
    return f"{emoji} {label}"


def bowling_label(key):
    emoji, label = _BOWL_LABELS.get(key, _BOWL_LABELS[DEFAULT_BOWLING])
    return f"{emoji} {label}"


def elect_toss_decision():
    """Bat or bowl after winning the toss — a straight 50/50, as specified."""
    return random.choice(("bat", "bowl"))
