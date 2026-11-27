"""Match-state adapter, bowling allocation and projections for ODI Approach."""

from functools import lru_cache
import math

from engine import pitch_state
from engine.pressure_engine import PressureEngine
from engine.realism import odi
from engine.realism.context import BallContext
from engine.realism.phases import phase_for_ball
from engine.realism.styles import bowling_family
from services import chemistry
from services import drinks_break


# An ODI-rules innings is scheduled for this many overs unless the match says
# otherwise. ``services.match_formats`` owns the range a command may ask for
# (21 to 50); everything below reads the length off the match state, so a
# shortened ODI keeps the same phase proportions, quotas and par curve.
FULL_OVERS = 50


def is_odi(state):
    return (state or {}).get("ball_format") == "ODI"


def innings_overs(state):
    """Overs scheduled for one innings of this ODI (50 unless shortened)."""
    return max(1, int((state or {}).get("overs") or FULL_OVERS))


def innings_balls(state):
    """Legal balls in one innings of this ODI."""
    return 6 * innings_overs(state)


def phase_windows(total_balls):
    """``(powerplay_end, death_start)`` in legal balls, as the ODI split.

    The real ODI powerplay is the first ten overs of fifty and the death the
    last ten, so the boundaries are 20% and 80% of the innings — which is
    exactly balls 60 and 240 at the full 300, the split the measured reference
    filed its deliveries under.
    """
    from engine.realism.phases import ODI_DEATH_START, ODI_POWERPLAY_END
    total = max(1, int(total_balls))
    return round(total * ODI_POWERPLAY_END), round(total * ODI_DEATH_START)


def valid_xi(xi, overs=FULL_OVERS):
    """True when an XI can legally bowl out an ``overs``-long ODI innings."""
    from services.cipl_match import is_part_time_bowler, PART_TIME_MAX_OVERS
    overs = max(1, int(overs or FULL_OVERS))
    quota = max(1, -(-overs // 5))
    return len(xi) == 11 and sum(min(quota, PART_TIME_MAX_OVERS) if is_part_time_bowler(p)
                                 else quota for p in xi) >= overs


@lru_cache(maxsize=8192)
def can_allocate(quotas, previous, overs):
    """Exact constant-horizon feasibility bound for interchangeable overs.

    A bowler can fill every other slot, starting in slot two if they bowled
    the preceding over. Truncating each quota at its alternating-slot bound
    gives the available capacity. The resulting multiset can be interleaved
    with no equal neighbours, including at the boundary with the last over.
    """
    if overs <= 0:
        return True
    return sum(min(q, (overs + (i != previous)) // 2)
               for i, q in enumerate(quotas)) >= overs


def candidates(state, xi):
    from services.cipl_match import overs_left, is_part_time_bowler

    previous = str(state.get("prev_bowler_rid"))
    remaining = innings_overs(state) + 1 - state["current_over"]
    if remaining <= 0 or state["total_wickets"] >= state.get("wicket_limit", 10):
        return []
    quotas = tuple(overs_left(state, p) for p in xi)
    choices = []
    for i, (p, quota) in enumerate(zip(xi, quotas)):
        if quota <= 0 or str(p["roster_id"]) == previous:
            continue
        after = quotas[:i] + (quota - 1,) + quotas[i + 1:]
        if can_allocate(after, i, remaining - 1):
            choices.append(p)
    specialists = [p for p in choices if not is_part_time_bowler(p)]
    return sorted(specialists or choices,
                  key=lambda p: (-float(p.get("bowl_rating") or 0), str(p["roster_id"])))


def bowler_workload(state, bowler):
    from services.cipl_match import approach_bowling_type

    rid = bowler.get("roster_id")
    return odi.workload(
        state.get("approach_log", []), state["innings"], state["current_over"], rid,
        state.get("bowl_stats", {}).get(str(rid), {}).get("balls", 0),
        bowling_family(approach_bowling_type(bowler)) == "spin", odi.settings(state))


def _players(state, bowler):
    order = state.get("batting_order") or []
    striker = order[state["striker_idx"]] if order else {}
    partner = order[state["non_striker_idx"]] if order else {}
    return striker, partner, bowler if bowler is not None else state.get("current_bowler") or {}


def _dew(state):
    cond = state.get("conditions") or {}
    dew = str(cond.get("dew") or "").lower()
    return {"light": 0.33, "moderate": 0.66, "heavy": 1.0}.get(dew, 0.0)


def context(state, bowler=None, *, free_hit=None, workload=None):
    """Public match context. Captain choices are accepted separately."""
    from services import cipl_match as cm

    fmt = cm.approach_engine_format(state)
    striker, partner, bowl = _players(state, bowler)
    faced = state.get("bat_stats", {}).get(str(striker.get("roster_id")), {})
    done = cm.balls_bowled(state)
    total = innings_balls(state)
    innings = state["innings"]
    pitch = state.get("pitch_type") or "Hard"
    target = state.get("target")
    # Use List A's ODI chase and rebuild response. Bound its effect before
    # passing it to the measured model and leave the outcome entirely random.
    required = max(0, target - state["total_runs"]) * 6 / max(1, total - done) if target else 0
    pressure = _pressure(innings, done, state["total_runs"], state["total_wickets"],
                         required, pitch, fmt.name, total)
    bond = chemistry.partnership_bond(striker, partner) if striker and partner else 0
    carry = state.get("approach_carry") or {}
    return BallContext(
        format_name="ODI", over=done // 6, ball_in_over=done % 6, innings=innings,
        balls_per_over=6, innings_balls=total, runs=state["total_runs"], wickets=state["total_wickets"],
        balls_bowled=done, target=target,
        bat_rating=float(striker.get("bat_rating") or striker.get("batting_rating") or 50),
        bowl_rating=float(bowl.get("bowl_rating") or bowl.get("bowling_rating") or 50),
        bat_style=striker.get("bat_hand") or "Right", bowl_hand=bowl.get("bowl_hand") or "Right",
        bowl_style=bowl.get("bowl_style") or "",
        bowling_type=cm.approach_bowling_type(bowl),
        batter_runs=faced.get("runs", 0), balls_faced=faced.get("balls", 0),
        batting_position=state.get("striker_idx", 0) + 1,
        pitch=pitch, pitch_wear=pitch_state.carry_wear(innings, done, total), dew=_dew(state),
        free_hit=bool(state.get("free_hit") if free_hit is None else free_hit),
        extra={"workload": workload or bowler_workload(state, bowl),
               "chemistry": max(0, min(1, float(bond or 0))),
               "pressure": pressure, "momentum": state.get("momentum", 0),
               "bat_repeat": state.get("bat_repeat", 0), "bowl_repeat": state.get("bowl_repeat", 0),
               "last_bat": state.get("last_bat_approach"), "last_bowl": state.get("last_bowl_approach"),
               "carry": 0.015 if carry else 0.0,
               # Both captains' drinks-interval plans for this session, as a
               # (runs, wicket) pair — the identity until they are called.
               "session": drinks_break.modifier(state),
               "fielding": cm._fielding_quality(state.get("bowl_xi") or []) if bowl else 60},
    )


@lru_cache(maxsize=1)
def _pressure_engine(mechanical_name):
    from engine.format_config import get_format
    return PressureEngine(format_config=get_format(mechanical_name))


def _pressure(innings, done, score, wickets, required, pitch, mechanical_name,
              total_balls=6 * FULL_OVERS):
    engine = _pressure_engine(mechanical_name)
    risk = engine.calculate_unified_risk_factor({
        "innings": innings, "current_over": done // 6, "score": score,
        "wickets": wickets, "required_run_rate": required,
        "overs_remaining": (total_balls - done) / 6, "pitch": pitch,
    })
    return max(-1.0, min(1.0, risk - 1.0))


def trait_hook(state, ctx, bowler=None, collector=None):
    from services import cipl_match as cm

    striker, _, bowl = _players(state, bowler)
    if not (striker.get("traits") or bowl.get("traits")):
        return None
    srid, brid = str(striker.get("roster_id")), str(bowl.get("roster_id"))
    return cm._make_trait_hook(
        striker.get("traits"), bowl.get("traits"), cm._trait_ctx(
            state, striker, bowl, state["bat_stats"].get(srid, {}),
            state["bowl_stats"].get(brid, {}), state.get("batter_streaks", {}).get(srid),
            50, ctx.innings, ctx.required_rate or 0, ctx.balls_remaining, ctx.target, ctx.pitch),
        collector=collector,
    )


def delivery_model(state, bowler=None, *, free_hit=None, workload=None, collector=None):
    cfg = odi.settings(state)
    ctx = context(state, bowler, free_hit=free_hit, workload=workload)
    base, node, meta = odi.baseline(ctx, cfg)
    return ctx, cfg, base, node, meta, trait_hook(state, ctx, bowler, collector)


def probabilities(model, bat=None, bowl=None):
    ctx, cfg, base, _, _, hook = model
    return odi.approach_distribution(base, ctx, cfg, bat, bowl, hook)


def bowl_delivery(state, bowler, bat, bowl, rng, *, free_hit, workload, collector):
    model = delivery_model(state, bowler, free_hit=free_hit, workload=workload, collector=collector)
    ctx, cfg, _, node, meta, _ = model
    return odi.sample(probabilities(model, bat, bowl), ctx, cfg, rng, node, meta, bat)


def mean_remaining(balls, hand, ctx, cfg, *, edge=0):
    if balls <= 0 or hand <= 0:
        return 0.0
    total = max(1, int(ctx.innings_balls))
    balls = min(balls, total)
    start = total - balls
    rates = cfg["phase_rates"]
    pp_end, death_start = phase_windows(total)
    pp = max(0, pp_end - start)
    middle = max(0, death_start - max(start, pp_end))
    death = total - max(start, death_start)
    par = sum(n * rates[phase] / 6 for n, phase in
              ((pp, "powerplay"), (middle, "middle"), (death, "death")))
    per_wicket = 29 + 2 * hand
    resource = 1 - math.exp(-hand * per_wicket / max(1, balls))
    resource = min(1.0, resource / (1 - math.exp(-10 * 49 / max(1, balls))))
    pitch = cfg["pitch"].get(ctx.pitch, cfg["pitch"]["Hard"])[0]
    return par * resource * pitch * (1 + 0.12 * edge)


def win_chance(needed, balls, hand, ctx, cfg, edge=0):
    if needed <= 0:
        return 1.0
    if balls <= 0 or hand <= 0:
        return 0.0
    expected = mean_remaining(balls, hand, ctx, cfg, edge=edge)
    deviation = max(2.0, 1.7 * math.sqrt(balls))
    return 0.5 * math.erfc((needed - 0.5 - expected) / (math.sqrt(2) * deviation))


def forecast(state):
    ctx = context(state)
    cfg = odi.settings(state)
    hand = max(0, 10 - ctx.wickets)
    if ctx.target is None:
        baseline = mean_remaining(ctx.balls_remaining, hand, ctx, cfg)
        # Use the current pace once there is enough innings evidence.
        trend = min(0.32, ctx.balls_bowled / 500)
        current_pace = ctx.current_rate * ctx.balls_remaining / 6
        return round(ctx.runs + (1 - trend) * baseline + trend * current_pace)
    return win_chance(ctx.target - ctx.runs, ctx.balls_remaining, hand, ctx, cfg)


@lru_cache(maxsize=8192)
def _reserve_score(quotas, selected, overs_left, preferred, demand, cap=5):
    """Evaluate the next legal choice and one further over toward the death.

    ``cap`` is how much of a single bowler's quota counts toward the death
    reserve — half of it, so the full ODI quota of ten contributes at most five
    and a shortened ODI's smaller quota contributes proportionally less.
    """
    def quality(available):
        return sum(min(cap, available[i]) for i in preferred)

    after = quotas[:selected] + (quotas[selected] - 1,) + quotas[selected + 1:]
    if overs_left <= 0 or demand <= 0:
        return 0.0
    next_qualities = []
    for i, q in enumerate(after):
        if i != selected and q:
            plan = after[:i] + (q - 1,) + after[i + 1:]
            if can_allocate(plan, i, overs_left - 1):
                next_qualities.append(quality(plan))
    remaining = max(next_qualities, default=quality(after))
    return 7.5 * min(0, remaining - demand) + 0.25 * min(3, remaining - demand)


def bowling_scores(state, options):
    from services import cipl_match as cm
    from services import impact_player

    xi = impact_player.active_players(state["bowl_xi"])
    ids = [p["roster_id"] for p in xi]
    quotas = tuple(cm.overs_left(state, p) for p in xi)
    def death_quality(player):
        style = bowling_family(cm.approach_bowling_type(player))
        return float(player.get("bowl_rating") or 0) + (3.0 if style == "pace" else 0.0)
    preferred = tuple(sorted(range(len(xi)), key=lambda i: (-death_quality(xi[i]), str(ids[i])))[:3])
    over = state["current_over"]
    chase = cm.chase(state)
    total_overs = innings_overs(state)
    # The scheduled death is the last fifth of the innings — ten overs of
    # fifty. Hold back that many top-three overs until it starts.
    death_overs = max(1, round(total_overs * 0.2))
    middle_end = total_overs - death_overs
    cap = max(1, cm.max_bowler_overs(state) // 2)
    demand = death_overs if over <= middle_end else 0
    if chase and chase["rrr"] < 5.5 and chase["runs_required"] < max(0, (middle_end - over) * 4):
        # A well-placed chase may finish before the scheduled death.
        demand = 0
    scores = []
    for player in options:
        model = delivery_model(state, player)
        ctx, cfg, _, _, _, _ = model
        runs, wickets = odi.expectations(probabilities(model), ctx, cfg)
        index = ids.index(player["roster_id"])
        rating = float(player.get("bowl_rating") or 0)
        reserve = _reserve_score(quotas, index, total_overs - over, preferred, demand, cap)
        matchup = -14 * runs + 125 * wickets
        freshness = 4 * ctx.extra["workload"].penalty
        scores.append(0.45 * rating + matchup + reserve - freshness)
    return scores
