"""Trait Engine — applies trait effects to probability dict per ball.
Called by probability_engine.calculate_outcome() before normalization.
"""

import logging
from config import (
    TRAIT_LEVEL_PCT, TRAIT_STACK_WEIGHTS, TRAIT_MAX_EFFECTIVE_PCT,
    TRAIT_LEVEL_5_HIDDEN_BONUS_PCT,
)

logger = logging.getLogger(__name__)


def _level_pct(level: int) -> float:
    base = TRAIT_LEVEL_PCT.get(level, 0)
    if level == 5:
        base += TRAIT_LEVEL_5_HIDDEN_BONUS_PCT
    return float(base)


def _apply_delta(probs: dict, key: str, delta: float):
    probs[key] = max(0.0, probs.get(key, 0.0) + delta)


# ── Per-trait handlers. ctx = {over, total_overs, rrr, bat_balls_faced} ─
def _bat_finisher(ctx, x):
    if ctx["over"] >= ctx["total_overs"] - 2:
        return [("6", x), ("4", x / 2)]
    return []

def _bat_power_hitter(ctx, x):
    return [("6", x), ("W", x / 3)]

def _bat_anchor(ctx, x):
    return [("W", -x), ("6", -x / 4)]

def _bat_fast_starter(ctx, x):
    if ctx.get("bat_balls_faced", 0) <= 10:
        return [("4", x / 2), ("6", x / 4)]
    return []

def _bat_clutch(ctx, x):
    rrr = ctx.get("rrr", 0)
    if rrr and rrr > 8:
        return [("4", x / 2), ("6", x / 2)]
    return []

def _mental_consistency(ctx, x):
    return [("W", -x), ("6", -x / 3)]

def _mental_momentum(ctx, x):
    balls = ctx.get("bat_balls_faced", 0)
    if balls <= 0:
        return []
    scale = min(1.0, balls / 30.0)
    bonus = x * scale
    return [("4", bonus / 2), ("6", bonus / 2)]

def _bowl_death(ctx, x):
    if ctx["over"] >= ctx["total_overs"] - 2:
        return [("6", -x), ("W", x / 2)]
    return []

def _bowl_wicket_hunter(ctx, x):
    return [("W", x)]

def _bowl_dot_specialist(ctx, x):
    return [("dot", x)]

def _bowl_powerplay(ctx, x):
    if ctx["over"] <= 3:
        return [("dot", x), ("W", x / 2)]
    return []

def _bowl_yorker(ctx, x):
    if ctx["over"] >= ctx["total_overs"] - 2:
        return [("W", x / 2), ("dot", x / 2)]
    return []

def _field_safe_hands(ctx, x):
    return [("W", x / 4)]

def _field_sniper(ctx, x):
    return [("W", x / 4)]


TRAIT_HANDLERS = {
    "bat_finisher": _bat_finisher,
    "bat_power_hitter": _bat_power_hitter,
    "bat_anchor": _bat_anchor,
    "bat_fast_starter": _bat_fast_starter,
    "bat_clutch": _bat_clutch,
    "bowl_death": _bowl_death,
    "bowl_wicket_hunter": _bowl_wicket_hunter,
    "bowl_dot_specialist": _bowl_dot_specialist,
    "bowl_powerplay": _bowl_powerplay,
    "bowl_yorker": _bowl_yorker,
    "field_safe_hands": _field_safe_hands,
    "field_sniper": _field_sniper,
    "mental_consistency": _mental_consistency,
    "mental_momentum": _mental_momentum,
}


def apply_traits(probs, striker_traits, bowler_traits, ctx):
    """Apply all active traits in-place.

    Args:
      probs: dict of probability keys
      striker_traits: [{"effect_key", "level", "display_name"}, ...]
      bowler_traits: same
      ctx: {"over", "total_overs", "rrr", "bat_balls_faced", ...}

    Returns: list of "Name Lv.X" strings of traits that ACTIVATED this ball.
    """
    activated = []
    all_deltas = []  # (key, delta, role, name, level)

    for role, traits in [("batter", striker_traits or []), ("bowler", bowler_traits or [])]:
        for i, t in enumerate(traits):
            handler = TRAIT_HANDLERS.get(t.get("effect_key"))
            if not handler:
                continue
            x = _level_pct(t.get("level", 1))
            weight = TRAIT_STACK_WEIGHTS[i] if i < len(TRAIT_STACK_WEIGHTS) else 0.3
            deltas = handler(ctx, x)
            if not deltas:
                continue
            name = t.get("display_name", t.get("effect_key"))
            level = t.get("level", 1)
            activated.append(f"{name} Lv.{level}")
            for key, d in deltas:
                all_deltas.append((key, d * weight, role, name, level))

    # Cap positive sum per role
    for role in ("batter", "bowler"):
        pos_total = sum(max(0, d[1]) for d in all_deltas if d[2] == role)
        if pos_total > TRAIT_MAX_EFFECTIVE_PCT:
            scale = TRAIT_MAX_EFFECTIVE_PCT / pos_total
            for i, (k, d, r, n, lv) in enumerate(all_deltas):
                if r == role and d > 0:
                    all_deltas[i] = (k, d * scale, r, n, lv)

    for key, delta, _, _, _ in all_deltas:
        _apply_delta(probs, key, delta)

    return activated
