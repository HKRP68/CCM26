"""Trait Engine — applies trait effects to probability dict per ball.
Called by probability_engine.calculate_outcome() before normalization.

Every handler takes ``(ctx, x, role)`` and returns a list of ``(key, delta)``
pairs, where ``x`` is the trait's level strength in percentage points and
``role`` is "batter" or "bowler" (the side the trait is equipped on). Returning
an empty list means the trait did not fire on this ball.

THE CONTRACT FOR ctx
────────────────────
Every key is optional. A handler must read ctx with ``.get()`` and a default,
and must return [] rather than raise when the information it wants isn't there
— several ball loops feed this engine and they know different amounts about the
match. That is what lets a new trait ship without touching every caller: it
simply stays silent in the loops that can't tell it what it needs.

Keys the loops supply today (services.cipl_match, handlers.match,
handlers.super_over):

  over, total_overs        1-based over number and innings length
  innings, rrr             innings number; required run rate (0 when not chasing)
  balls_left, runs_needed  chase state
  bat_balls_faced          balls the striker has faced
  bat_runs                 runs the striker has made
  bat_position             striker's place in the batting order (1-11)
  boundary_streak          the striker's current run of consecutive boundaries
  bat_boundaries           boundaries the striker has hit this innings
  wickets                  wickets the batting side has lost
  partnership_runs         runs in the current partnership
  bowler_balls             legal balls bowled by this bowler in the innings
  bowler_runs              runs conceded by this bowler in the innings
  is_spin                  True when the bowler is a spinner
  bat_rating, bowl_rating  the two effective ratings for this ball
  bat_hand, bowl_hand      "Left" / "Right"
  striker_id, bowler_id    stable ids, used only to keep a per-over roll stable
"""

import logging
import random
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


def _num(ctx, key, default=0):
    """Read a numeric ctx value defensively — None and junk read as the default."""
    try:
        value = ctx.get(key, default)
        if value is None:
            return default
        return float(value)
    except (AttributeError, TypeError, ValueError):
        return default


def _death_overs(ctx) -> bool:
    """The last three overs — and only when the phase is actually known.

    Both values have to be positive: with neither supplied the comparison is
    ``0 >= -2``, which would silently declare every ball a death over and fire
    Finisher / Death Specialist / Yorker in any loop with a thin context.
    """
    over, total_overs = _num(ctx, "over"), _num(ctx, "total_overs")
    return over > 0 and total_overs > 0 and over >= total_overs - 2


def _powerplay(ctx) -> bool:
    return 0 < _num(ctx, "over") <= 3


def _over_roll(ctx, salt) -> float:
    """A 0-1 roll that stays the SAME for every ball of one over.

    Traits that describe a whole over ("occasionally produces an exceptional
    over") must not re-roll per delivery, or they'd fire on scattered balls
    instead of an over. Hashing the over + innings + bowler gives a value that
    is stable within the over and unpredictable across overs, with no state to
    carry. Falls back gracefully when the loop doesn't pass ids.
    """
    seed = (int(_num(ctx, "over")), int(_num(ctx, "innings", 1)),
            int(_num(ctx, "bowler_id")), str(salt))
    # hash() is randomised per process, which is fine (and desirable) here —
    # the requirement is stability WITHIN a process's over, not across runs.
    return (hash(seed) % 10_000) / 10_000.0


# ── Batting ──────────────────────────────────────────────────────────
def _bat_finisher(ctx, x, role):
    if _death_overs(ctx):
        return [("6", x), ("4", x / 2)]
    return []


def _bat_power_hitter(ctx, x, role):
    return [("6", x), ("W", x / 3)]


def _bat_anchor(ctx, x, role):
    return [("W", -x), ("6", -x / 4)]


def _bat_fast_starter(ctx, x, role):
    if _num(ctx, "bat_balls_faced") <= 10:
        return [("4", x / 2), ("6", x / 4)]
    return []


def _bat_clutch(ctx, x, role):
    if _num(ctx, "rrr") > 8:
        return [("4", x / 2), ("6", x / 2)]
    return []


def _bat_spin_basher(ctx, x, role):
    """Stronger against spin — the ball comes on slower, so it goes further."""
    if not ctx.get("is_spin"):
        return []
    return [("4", x / 2), ("6", x / 2), ("W", -x / 4)]


def _bat_pace_destroyer(ctx, x, role):
    """The mirror of Spin Basher: uses the pace of the ball against the bowler."""
    if ctx.get("is_spin") is not False:
        # Unknown bowler type reads as "not established pace" and stays silent,
        # rather than firing against every bowler in loops that don't say.
        return []
    return [("4", x / 2), ("6", x / 2), ("W", -x / 4)]


def _bat_late_bloomer(ctx, x, role):
    """Nothing for the first 20 balls, then a set batter who accelerates."""
    if _num(ctx, "bat_balls_faced") < 20:
        return []
    return [("4", x / 2), ("6", x / 2), ("W", -x / 4)]


def _bat_power_surge(ctx, x, role):
    """Rides a hot streak — live only while consecutive boundaries are landing."""
    streak = _num(ctx, "boundary_streak")
    if streak < 2:
        return []
    scale = min(1.0, streak / 3.0)
    return [("6", x * scale), ("4", x * scale / 2)]


def _bat_pinch_hitter(ctx, x, role):
    if not _powerplay(ctx):
        return []
    return [("4", x / 2), ("6", x / 3), ("W", x / 6)]


# ── Bowling ──────────────────────────────────────────────────────────
def _bowl_death(ctx, x, role):
    if _death_overs(ctx):
        return [("6", -x), ("W", x / 2)]
    return []


def _bowl_wicket_hunter(ctx, x, role):
    return [("W", x)]


def _bowl_dot_specialist(ctx, x, role):
    return [("dot", x)]


def _bowl_powerplay(ctx, x, role):
    if _powerplay(ctx):
        return [("dot", x), ("W", x / 2)]
    return []


def _bowl_yorker(ctx, x, role):
    if _death_overs(ctx):
        return [("W", x / 2), ("dot", x / 2)]
    return []


def _bowl_spell_builder(ctx, x, role):
    """Warms up through the spell — full strength by the third over bowled."""
    balls = _num(ctx, "bowler_balls")
    if balls < 6:
        return []
    scale = min(1.0, balls / 18.0)
    return [("dot", x * scale / 2), ("W", x * scale / 2)]


def _bowl_partner_breaker(ctx, x, role):
    if _num(ctx, "partnership_runs") < 50:
        return []
    return [("W", x), ("dot", x / 2)]


def _bowl_tail_hunter(ctx, x, role):
    if _num(ctx, "bat_position") < 8:
        return []
    return [("W", x), ("dot", x / 2)]


def _bowl_middle_squeeze(ctx, x, role):
    """The overs between the field going back and the death — the quiet ones."""
    over, total = _num(ctx, "over"), _num(ctx, "total_overs")
    if over <= 3 or over > total - 3:
        return []
    return [("dot", x / 2), ("W", x / 3)]


def _bowl_economy(ctx, x, role):
    return [("4", -x / 2), ("6", -x / 2), ("dot", x / 4)]


# ── Fielding ─────────────────────────────────────────────────────────
def _field_safe_hands(ctx, x, role):
    return [("W", x / 4)]


def _field_sniper(ctx, x, role):
    return [("W", x / 4)]


def _field_boundary_rider(ctx, x, role):
    """Saves the four in the deep — the shot still runs, just not to the rope."""
    return [("4", -x / 2), ("2", x / 4)]


def _field_livewire(ctx, x, role):
    return [("dot", x / 2), ("1", -x / 4)]


# ── Mental ───────────────────────────────────────────────────────────
def _mental_consistency(ctx, x, role):
    return [("W", -x), ("6", -x / 3)]


def _mental_momentum(ctx, x, role):
    balls = _num(ctx, "bat_balls_faced")
    if balls <= 0:
        return []
    scale = min(1.0, balls / 30.0)
    bonus = x * scale
    return [("4", bonus / 2), ("6", bonus / 2)]


def _mental_confidence(ctx, x, role):
    """A little more belief with every boundary already hit — capped at four."""
    boundaries = _num(ctx, "bat_boundaries")
    if boundaries <= 0:
        return []
    scale = min(1.0, boundaries / 4.0)
    return [("4", x * scale / 3), ("1", x * scale / 3), ("W", -x * scale / 4)]


def _mental_comeback(ctx, x, role):
    """Answers back after being taken down earlier.

    For a bowler that means an expensive spell so far (9+ an over across at
    least an over's work); for a batter, being tied down — a scoring rate under
    a run a ball once they are past the first few deliveries.
    """
    if role == "bowler":
        balls = _num(ctx, "bowler_balls")
        if balls < 6:
            return []
        econ = _num(ctx, "bowler_runs") / balls * 6.0
        if econ < 9:
            return []
        return [("W", x / 2), ("dot", x / 2)]
    balls = _num(ctx, "bat_balls_faced")
    if balls < 8 or _num(ctx, "bat_runs") >= balls:
        return []
    return [("4", x / 2), ("1", x / 4)]


def _mental_ice(ctx, x, role):
    """Doesn't panic when the rate climbs — takes the risk out of the chase."""
    if _num(ctx, "rrr") <= 8:
        return []
    return [("W", -x / 2), ("1", x / 4)]


# ── Awareness ────────────────────────────────────────────────────────
def _aware_pitch(ctx, x, role):
    """Reads the surface early, so the vulnerable first overs cost less.

    A batter settles in faster; a bowler finds their length faster. Both are
    "the opening period is less of a lottery", which is the same effect from
    opposite ends.
    """
    if role == "bowler":
        if _num(ctx, "bowler_balls") > 12:
            return []
        return [("dot", x / 3), ("4", -x / 4)]
    if _num(ctx, "bat_balls_faced") > 15:
        return []
    return [("W", -x / 2), ("dot", -x / 4), ("1", x / 4)]


def _aware_rotation(ctx, x, role):
    return [("1", x), ("dot", -x)]


def _aware_gap(ctx, x, role):
    return [("2", x / 2), ("4", x / 2)]


# ── Special ──────────────────────────────────────────────────────────
def _special_giantkiller(ctx, x, role):
    """Raises their game against a better opponent, and only against one."""
    bat, bowl = _num(ctx, "bat_rating"), _num(ctx, "bowl_rating")
    if not bat or not bowl:
        return []
    gap = (bowl - bat) if role == "batter" else (bat - bowl)
    if gap < 5:
        return []
    scale = min(1.0, gap / 15.0)
    if role == "batter":
        return [("4", x * scale / 2), ("6", x * scale / 3), ("W", -x * scale / 3)]
    return [("W", x * scale / 2), ("dot", x * scale / 3)]


# ── Elite ────────────────────────────────────────────────────────────
def _elite_master_blaster(ctx, x, role):
    """Good in all three phases, and best in the one that phase rewards."""
    if _powerplay(ctx):
        return [("4", x / 2), ("6", x / 4), ("W", -x / 6)]
    if _death_overs(ctx):
        return [("6", x / 2), ("4", x / 4), ("W", -x / 6)]
    return [("4", x / 3), ("1", x / 3), ("W", -x / 6)]


def _elite_run_machine(ctx, x, role):
    """Doesn't give it away, and cashes in once set."""
    scale = min(1.0, (_num(ctx, "bat_balls_faced") + 10) / 40.0)
    return [("W", -x / 2), ("1", x * scale / 3), ("4", x * scale / 3)]


def _elite_ice_finisher(ctx, x, role):
    """The last three overs of a live chase — nothing else.

    Deliberately narrower than Finisher: it needs a chase (rrr > 0) as well as
    the death overs, which is what makes it the elite version rather than a
    strictly better copy of a rare trait.
    """
    if not _death_overs(ctx) or _num(ctx, "rrr") <= 0:
        return []
    return [("6", x), ("4", x / 2), ("W", -x / 4)]


def _elite_bowling_wizard(ctx, x, role):
    return [("dot", x / 3), ("W", x / 3), ("6", -x / 3)]


def _elite_magic_spell(ctx, x, role):
    """One over in roughly eight is a different bowler entirely."""
    if _over_roll(ctx, "magic_spell") >= 0.12:
        return []
    return [("W", x), ("dot", x), ("4", -x / 2), ("6", -x / 2)]


def _elite_unplayable(ctx, x, role):
    """Shuts the door the moment a batter starts to get away."""
    if _num(ctx, "boundary_streak") < 1:
        return []
    return [("4", -x), ("6", -x), ("dot", x / 2)]


def _elite_golden_arm(ctx, x, role):
    """Strikes in the first over of the spell — the captain's hunch that works.

    ``bowler_balls`` counts the balls bowled BEFORE this delivery, so the first
    over is 0-5 and the cut-off is ``>= 6``. ``> 6`` would have let it fire on
    a seventh ball, i.e. the first ball of the second over.
    """
    if _num(ctx, "bowler_balls") >= 6:
        return []
    return [("W", x)]


def _elite_big_fish(ctx, x, role):
    position = _num(ctx, "bat_position")
    if position <= 0 or position > 3:
        return []
    return [("W", x), ("dot", x / 3)]


def _elite_matchup(ctx, x, role):
    """Owns one type of batter — the one the ball is angled across.

    Needs both hands to be known, so it stays silent in loops that don't track
    handedness rather than firing against everyone.
    """
    bat_hand = str(ctx.get("bat_hand") or "").strip().lower()
    bowl_hand = str(ctx.get("bowl_hand") or "").strip().lower()
    if not bat_hand or not bowl_hand or bat_hand == bowl_hand:
        return []
    return [("W", x), ("4", -x / 3)]


def _elite_goat(ctx, x, role):
    """Rare, and only when the match is actually on the line."""
    balls_left = _num(ctx, "balls_left")
    pressure = (_num(ctx, "rrr") > 10
                or (0 < balls_left <= 12)
                or _num(ctx, "wickets") >= 7)
    if not pressure or random.random() >= 0.25:
        return []
    if role == "bowler":
        return [("W", x), ("dot", x / 2)]
    return [("4", x), ("6", x), ("W", -x / 2)]


TRAIT_HANDLERS = {
    # Batting
    "bat_finisher": _bat_finisher,
    "bat_power_hitter": _bat_power_hitter,
    "bat_anchor": _bat_anchor,
    "bat_fast_starter": _bat_fast_starter,
    "bat_clutch": _bat_clutch,
    "bat_spin_basher": _bat_spin_basher,
    "bat_pace_destroyer": _bat_pace_destroyer,
    "bat_late_bloomer": _bat_late_bloomer,
    "bat_power_surge": _bat_power_surge,
    "bat_pinch_hitter": _bat_pinch_hitter,
    # Bowling
    "bowl_death": _bowl_death,
    "bowl_wicket_hunter": _bowl_wicket_hunter,
    "bowl_dot_specialist": _bowl_dot_specialist,
    "bowl_powerplay": _bowl_powerplay,
    "bowl_yorker": _bowl_yorker,
    "bowl_spell_builder": _bowl_spell_builder,
    "bowl_partner_breaker": _bowl_partner_breaker,
    "bowl_tail_hunter": _bowl_tail_hunter,
    "bowl_middle_squeeze": _bowl_middle_squeeze,
    "bowl_economy": _bowl_economy,
    # Fielding
    "field_safe_hands": _field_safe_hands,
    "field_sniper": _field_sniper,
    "field_boundary_rider": _field_boundary_rider,
    "field_livewire": _field_livewire,
    # Mental
    "mental_consistency": _mental_consistency,
    "mental_momentum": _mental_momentum,
    "mental_confidence": _mental_confidence,
    "mental_comeback": _mental_comeback,
    "mental_ice": _mental_ice,
    # Awareness
    "aware_pitch": _aware_pitch,
    "aware_rotation": _aware_rotation,
    "aware_gap": _aware_gap,
    # Special
    "special_giantkiller": _special_giantkiller,
    # Elite
    "elite_master_blaster": _elite_master_blaster,
    "elite_run_machine": _elite_run_machine,
    "elite_ice_finisher": _elite_ice_finisher,
    "elite_bowling_wizard": _elite_bowling_wizard,
    "elite_magic_spell": _elite_magic_spell,
    "elite_unplayable": _elite_unplayable,
    "elite_golden_arm": _elite_golden_arm,
    "elite_big_fish": _elite_big_fish,
    "elite_matchup": _elite_matchup,
    "elite_goat": _elite_goat,
}


def _stack_weight(index: int) -> float:
    if index < len(TRAIT_STACK_WEIGHTS):
        return TRAIT_STACK_WEIGHTS[index]
    return TRAIT_STACK_WEIGHTS[-1] if TRAIT_STACK_WEIGHTS else 0.3


def apply_traits(probs, striker_traits, bowler_traits, ctx):
    """Apply all active traits in-place.

    Args:
      probs: dict of probability keys
      striker_traits: [{"effect_key", "level", "display_name"}, ...]
      bowler_traits: same
      ctx: see the module docstring — every key optional

    Returns: list of "Name Lv.X" strings of traits that ACTIVATED this ball.
    """
    activated = []
    all_deltas = []  # (key, delta, role, name, level)

    for role, traits in [("batter", striker_traits or []),
                         ("bowler", bowler_traits or [])]:
        # Diminishing returns are applied strongest-first. Ordering by level
        # (rather than by the order the traits happen to come back from the
        # database) is what makes the stack deterministic: a player's Lv.5
        # trait always takes the full-strength slot, whichever slot it sits in.
        #
        # effect_key breaks ties, because level alone does not finish the job:
        # two traits at the SAME level would keep their input order and take
        # different stack weights from it, which puts the row order back in
        # charge for the case where it is most likely (a player levelling their
        # traits together).
        ordered = sorted(traits, key=lambda t: (-int(t.get("level", 1) or 1),
                                                str(t.get("effect_key", ""))))
        for i, t in enumerate(ordered):
            handler = TRAIT_HANDLERS.get(t.get("effect_key"))
            if not handler:
                continue
            x = _level_pct(t.get("level", 1))
            weight = _stack_weight(i)
            try:
                deltas = handler(ctx or {}, x, role)
            except Exception:
                # One misbehaving trait must never cost the whole ball its
                # other traits, so it is dropped rather than raised.
                logger.exception("trait handler %s failed", t.get("effect_key"))
                continue
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
