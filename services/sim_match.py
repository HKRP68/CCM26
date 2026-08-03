"""Auto-simulated cricket match engine (the /sim command).

Uses the SimCricketX engine (engine/) which provides:
  - Momentum & par-score curves (game_state_engine)
  - Psychological pressure factors (pressure_engine)
  - Realistic ball outcomes keyed to pitch profiles (ball_outcome / ground_config)
  - Format-aware bowler rotation (bowler_manager / format_config)

Team setup:
  - Batting order  = the order the player saved with /sbo, when they saved one
                     (the XI arrives stamped ``order_locked``); otherwise
                     highest batting rating to lowest.
  - Bowling        = Bowlers and All-rounders bowl; BowlerManager enforces quota
                     and no-consecutive-overs rule automatically.

The module is pure (no Telegram / asyncio). The handler layer drives it.
"""

import math
import random
from datetime import datetime

from engine.ball_outcome import calculate_outcome
from engine.pressure_engine import PressureEngine
from engine.game_state_engine import (
    make_ball_event,
    compute_game_state_vector,
    BALL_HISTORY_WINDOW,
)
from engine.format_config import get_format, FORMAT_REGISTRY, FormatConfig, Phase
from engine.bowler_manager import BowlerManager
from services import batting_order_service as _bos

# Wicket type → commentary event key.
_WICKET_EVENT = {
    "Bowled": "wicket_bowled",
    "LBW": "wicket_lbw",
    "Stumped": "wicket_stumped",
    "Caught Behind": "wicket_caught_keeper",
    "Caught": "wicket_caught_fielder",
    "Caught & Bowled": "wicket_caught_fielder",
    "Run Out": "wicket_runOut",
}

_RUNS_TO_EVENT = {0: "dot", 1: "one", 2: "two", 3: "three", 4: "four", 6: "six"}

# Re-export constants used by services/match_dynamics.py (super-over helper).
_HOW_TO_EVENT = _WICKET_EVENT   # alias
_RUNS_TO_EVENT = {0: "dot", 1: "one", 2: "two", 3: "three", 4: "four", 6: "six"}
_ATTACK = ["Drive", "Loft", "Slog", "Pull", "Slog Sweep", "Square Cut", "Hook", "Cut"]
_NORMAL = ["Drive", "Cut", "Flick", "Leg Glance", "On Drive", "Off Drive", "Glance", "Pull"]
_DEFENSIVE = ["Defend", "Leave", "Glance", "Leg Glance", "Flick"]

# Bowl-style name map: CCM26 style strings → engine bowling_type strings
_BOWL_TYPE_MAP = {
    "Fast": "Fast",
    "Fast-medium": "Fast-medium",
    "Fast Medium": "Fast-medium",
    "Medium-fast": "Medium-fast",
    "Medium Fast": "Medium-fast",
    "Medium": "Medium-fast",
    "Off spin": "Off spin",
    "Off Spin": "Off spin",
    "Off Break": "Off spin",
    "Leg spin": "Leg spin",
    "Leg Spin": "Leg spin",
    "Leg Break": "Leg spin",
    "Finger spin": "Finger spin",
    "Finger Spin": "Finger spin",
    "Wrist spin": "Wrist spin",
    "Wrist Spin": "Wrist spin",
    "Left-arm spin": "Finger spin",
    "Left Arm Spin": "Finger spin",
    "Slow Left Arm": "Finger spin",
    "Swing": "Fast-medium",
    "": "Medium-fast",
}


def _adapt_player(p):
    """Convert a CCM26 player dict to the format expected by the engine."""
    bowl_style = p.get("bowl_style") or p.get("bowling_type") or ""
    bowling_type = _BOWL_TYPE_MAP.get(bowl_style, "Medium-fast")
    category = p.get("category", "")
    will_bowl = (
        p.get("will_bowl", False)
        or category in ("Bowler", "All-rounder")
    )
    return {
        **p,
        "batting_rating": float(
            p.get("bat_rating") or p.get("batting_rating") or p.get("rating") or 50
        ),
        "bowling_rating": float(
            p.get("bowl_rating") or p.get("bowling_rating") or 40
        ),
        "fielding_rating": float(
            p.get("fielding_rating") or p.get("field_rating") or 65
        ),
        "batting_hand": p.get("bat_hand") or p.get("batting_hand") or "Right",
        "bowling_hand": p.get("bowl_hand") or p.get("bowling_hand") or "Right",
        "bowling_type": bowling_type,
        "will_bowl": will_bowl,
    }


def _fmt_to_engine_fmt(fmt_dict, overs):
    """Return an engine FormatConfig for the given fmt dict / overs count.

    Builds a custom FormatConfig when the label isn't in FORMAT_REGISTRY so
    that non-standard formats (T10, custom over counts) respect their
    max_bowler_overs and phase windows.
    """
    if fmt_dict is not None:
        label = fmt_dict.get("label", "") or fmt_dict.get("name", "") or ""
        if label in FORMAT_REGISTRY:
            return FORMAT_REGISTRY[label]
        fmt_overs = int(fmt_dict.get("overs", overs))
        max_q = int(fmt_dict.get("max_bowler_overs") or max(1, -(-fmt_overs // 5)))
        pp_end = int(fmt_dict.get("powerplay_end", max(1, round(fmt_overs * 0.3))))
        death_start = int(fmt_dict.get("death_start", fmt_overs - max(1, round(fmt_overs * 0.2)) + 1))
    else:
        fmt_overs = int(overs)
        max_q = max(1, -(-fmt_overs // 5))
        pp_end = max(1, round(fmt_overs * 0.3))
        death_start = fmt_overs - max(1, round(fmt_overs * 0.2)) + 1
        label = "T20" if fmt_overs <= 20 else "ListA"
        if label in FORMAT_REGISTRY:
            return FORMAT_REGISTRY[label]

    # Build a lightweight FormatConfig from the services.match_formats dict
    pp_phase = Phase(name="Powerplay", start=0, end=pp_end - 1)
    mid_phase = Phase(name="Middle", start=pp_end, end=death_start - 2)
    death_phase = Phase(name="Death", start=death_start - 1, end=fmt_overs - 1)

    base = FORMAT_REGISTRY["T20"]
    return FormatConfig(
        name=label or f"{fmt_overs}ov",
        overs=fmt_overs,
        max_bowler_overs=max_q,
        allow_consecutive_overs=False,
        powerplay_phases=[pp_phase],
        middle_phase=mid_phase,
        death_phase=death_phase,
        par_scores=base.par_scores,
        pitch_par_factors=base.pitch_par_factors,
        expected_rr=base.expected_rr,
        extras_per_innings=base.extras_per_innings,
        target_scores=base.target_scores,
        correct_toss_choice=base.correct_toss_choice,
        correct_toss_choice_dn=base.correct_toss_choice_dn,
        rrr_baseline=base.rrr_baseline,
    )


def _new_bat_stat():
    return {"runs": 0, "balls": 0, "fours": 0, "sixes": 0,
            "out": False, "how": "", "bowler": ""}


def _new_bowl_stat():
    return {"balls": 0, "runs": 0, "wickets": 0, "maidens": 0}


def _eligible_bowlers(bowling_xi):
    eligible = [p for p in bowling_xi if p.get("will_bowl", False)
                or p.get("category") in ("Bowler", "All-rounder")]
    return eligible or list(bowling_xi)


def _bowler_overs_bowled(stat):
    return (stat or {}).get("balls", 0) / 6


def _bowler_economy(stat):
    overs = _bowler_overs_bowled(stat)
    if overs <= 0:
        return 0
    return (stat or {}).get("runs", 0) / overs


def _select_ai_bowler(bowling_xi, bowl_stats, over_idx, total_overs,
                      prev_bowler_id=None, max_bowler_overs=None):
    """Pick the next /sim bowler using rating, role, economy, wickets and phase.

    Kept for backward compatibility (tests import this directly).
    bowl_stats is keyed by id(player).
    """
    eligible = _eligible_bowlers(bowling_xi)
    if not eligible:
        return None

    quota = max_bowler_overs or math.ceil(total_overs / 5)
    cap = max(quota, math.ceil(total_overs / len(eligible)))

    def under_cap(player):
        return _bowler_overs_bowled(bowl_stats.get(id(player))) < cap

    available = [p for p in eligible if under_cap(p) and id(p) != prev_bowler_id]
    if not available:
        available = [p for p in eligible if under_cap(p)]
    if not available:
        available = [p for p in eligible if id(p) != prev_bowler_id] or eligible

    is_powerplay = over_idx < math.ceil(total_overs * 0.3)
    is_death_over = over_idx >= math.floor(total_overs * 0.75)

    def score(player):
        stat = bowl_stats.get(id(player), {})
        rating = player.get("bowl_rating", 0) or player.get("bowling_rating", 0) or 0
        economy = _bowler_economy(stat)
        overs_bowled = _bowler_overs_bowled(stat)
        wickets = stat.get("wickets", 0)

        value = rating * 2.2
        if player.get("category") == "Bowler":
            value += 18
        elif player.get("category") == "All-rounder":
            value += 10
        if overs_bowled > 0:
            value += max(0, 10 - economy) * 8
        else:
            value += 12
        value += wickets * 14
        if is_death_over:
            value += rating * 0.8
            if economy > 10 and overs_bowled > 0:
                value -= 35
            if wickets > 0:
                value += 20
        if is_powerplay:
            if rating >= 80:
                value += 20
            if player.get("category") == "Bowler":
                value += 10
        value -= (overs_bowled / cap) * 20
        return value

    return max(available, key=lambda p: (score(p),
                                         p.get("bowl_rating") or p.get("bowling_rating") or 0,
                                         p.get("name", "")))


def _build_bowling_plan(bowling_xi, overs, max_bowler_overs=None):
    """Pick an AI-style over-by-over bowler list for pre-match previews/tests.

    Kept for backward compatibility (tests import this directly).
    """
    eligible = _eligible_bowlers(bowling_xi)
    bowl_stats = {id(p): _new_bowl_stat() for p in eligible}
    plan, last = [], None
    for over_idx in range(overs):
        bowler = _select_ai_bowler(
            eligible, bowl_stats, over_idx, overs, last, max_bowler_overs)
        if bowler is None:
            break
        plan.append(bowler)
        bowl_stats[id(bowler)]["balls"] += 6
        last = id(bowler)
    return plan


def _normalize_outcome(oc):
    """Normalise a ball outcome dict to the new-engine format.

    Handles both new-engine format (type='run'/'wicket'/'extra') and the
    legacy probability_engine format (type='runs'/'wicket'/'wide'/'noball'/
    'legbye') so that monkeypatched tests still work.
    """
    otype = oc.get("type", "")
    # Already new-engine format
    if otype in ("run", "wicket", "extra"):
        return oc
    # Legacy format → new format
    if otype == "wide":
        return {"type": "extra", "is_extra": True, "extra_type": "Wide",
                "runs": oc.get("runs", 0), "batter_out": False, "wicket_type": None}
    if otype == "noball":
        return {"type": "extra", "is_extra": True, "extra_type": "No Ball",
                "runs": oc.get("runs", 0), "batter_out": False, "wicket_type": None}
    if otype == "legbye":
        return {"type": "extra", "is_extra": True, "extra_type": "LegByes",
                "runs": oc.get("runs", 1), "batter_out": False, "wicket_type": None}
    if otype == "runs":
        return {"type": "run", "is_extra": False,
                "runs": oc.get("runs", 0), "batter_out": False, "wicket_type": None}
    # Wicket: both old and new use type='wicket', but old uses 'how' not 'wicket_type'
    if otype == "wicket":
        return {"type": "wicket", "is_extra": False,
                "runs": oc.get("runs", 0),
                "batter_out": True,
                "wicket_type": oc.get("wicket_type") or oc.get("how", "Caught"),
                "extra_type": ""}
    return oc


def _select_fallback_bowler(eligible, bowl_stats_by_name, over_idx, total_overs,
                             prev_bowler_name=None, max_bowler_overs=None):
    """Fallback bowler selection when BowlerManager returns empty."""
    if not eligible:
        return None
    quota = max_bowler_overs or math.ceil(total_overs / 5)
    cap = max(quota, math.ceil(total_overs / len(eligible)))
    is_powerplay = over_idx < math.ceil(total_overs * 0.3)
    is_death = over_idx >= math.floor(total_overs * 0.75)

    def score(p):
        stat = bowl_stats_by_name.get(p.get("name", ""), {})
        overs_done = stat.get("balls", 0) / 6
        rating = p.get("bowling_rating", 0)
        value = rating * 2.2
        if overs_done > 0:
            economy = stat.get("runs", 0) / overs_done
            value += max(0, 10 - economy) * 8
        value += stat.get("wickets", 0) * 14
        if is_death:
            value += rating * 0.8
        if is_powerplay and rating >= 80:
            value += 20
        value -= (overs_done / cap) * 20
        return value

    available = [
        p for p in eligible
        if (bowl_stats_by_name.get(p.get("name", ""), {}).get("balls", 0) / 6) < cap
        and p.get("name") != prev_bowler_name
    ]
    if not available:
        available = eligible
    return max(available, key=lambda p: (score(p), p.get("bowling_rating", 0)))


def _fielder_keeper(bowling_xi):
    keeper = next(
        (p["name"] for p in bowling_xi if p.get("category") == "Wicket Keeper"),
        "the keeper",
    )
    fielders = [p["name"] for p in bowling_xi] or ["the fielder"]
    return fielders, keeper


def simulate_innings(batting_xi, bowling_xi, overs, pitch_type,
                     innings_no, batting_team, bowling_team,
                     target=None, commentary=None, feed=None,
                     fmt=None, run_factor=1.0, scenario=False):
    """Simulate one innings and return its result dict.

    Uses the SimCricketX engine (pressure_engine, game_state_engine,
    ball_outcome, bowler_manager) for realistic ball-by-ball simulation.

    commentary: optional callable(event_key, batsman, bowler, fielder, keeper,
                runs) -> str|None used to render a line per ball.
    feed: optional list to append ball-by-ball commentary entries to.
    fmt: optional services.match_formats format dict (or None).
    run_factor: pitch run factor (kept for API compatibility; engine uses
                ground_config's pitch scoring matrix directly).
    """
    # -- Player adaptation --
    # A line-up the player saved with /sbo is already in the order they want to
    # bat in, so it is used verbatim (``order_locked`` is stamped by
    # services.batting_order_service and survives _adapt_player's dict copy).
    # Everything else — bot XIs, and users who never saved an order — still gets
    # the batting-rating sort.
    adapted = [_adapt_player(p) for p in batting_xi]
    if _bos.is_order_locked(adapted):
        batting_order = adapted
    else:
        batting_order = sorted(
            adapted,
            key=lambda p: p.get("bat_rating") or p.get("batting_rating") or p.get("rating") or 0,
            reverse=True,
        )
    bowling_adapted = [_adapt_player(p) for p in bowling_xi]

    # Ensure we always have enough bowlers: promote batters/keepers if needed
    eligible = _eligible_bowlers(bowling_adapted)
    if len(eligible) < max(3, math.ceil(overs / 4)):
        for p in bowling_adapted:
            if not p.get("will_bowl"):
                p["will_bowl"] = True
            if len(_eligible_bowlers(bowling_adapted)) >= max(3, math.ceil(overs / 4)):
                break

    # -- Engine format config --
    engine_fmt = _fmt_to_engine_fmt(fmt, overs)

    # -- BowlerManager for quota + consecutive enforcement --
    bowler_mgr = BowlerManager(bowling_adapted, engine_fmt)

    # -- PressureEngine for psychological pressure --
    pressure_eng = PressureEngine(format_config=engine_fmt)

    # -- Misc state --
    fielders, keeper = _fielder_keeper(bowling_adapted)
    bat_stats = {id(p): _new_bat_stat() for p in batting_order}
    bowl_stats_by_name = {}   # name → _new_bowl_stat()
    bowl_stats_by_id = {}     # id(p) → same dict (for legacy scorecard rendering)
    for p in bowling_adapted:
        d = _new_bowl_stat()
        bowl_stats_by_name[p["name"]] = d
        bowl_stats_by_id[id(p)] = d

    total_runs = 0
    total_wkts = 0
    extras = {"wides": 0, "noballs": 0, "legbyes": 0}
    fow = []
    timeline = []
    over_summaries = []
    plan = []

    # GSME ball history
    ball_history = []
    batter_streaks = {}    # name → {"boundaries": int}

    striker_i, non_striker_i, next_i = 0, 1, 2
    free_hit = False
    chased = False
    legal_balls = 0
    last_bowler_name = None

    def _balls_to_overs(b):
        return f"{b // 6}.{b % 6}"

    def _batting_snapshot():
        active = []
        for idx in (striker_i, non_striker_i):
            if idx >= len(batting_order):
                continue
            p = batting_order[idx]
            bs = bat_stats[id(p)]
            active.append({
                "name": p["name"],
                "runs": bs["runs"],
                "balls": bs["balls"],
                "fours": bs["fours"],
                "sixes": bs["sixes"],
                "out": bs["out"],
                "striker": idx == striker_i,
            })
        return active

    def _emit(event_key, batsman, bowler_name, runs, lb):
        line = None
        if commentary:
            try:
                line = commentary(event_key, batsman, bowler_name,
                                  random.choice(fielders), keeper, runs)
            except Exception:
                line = None
        if feed is not None:
            feed.append({
                "innings": innings_no,
                "over": _balls_to_overs(lb),
                "score": f"{total_runs}/{total_wkts}",
                "striker": batsman,
                "bowler": bowler_name,
                "event": event_key,
                "text": line or "",
            })

    def _record_over_summary(over_no, bowler_name, over_timeline, completed_balls):
        summary = {
            "innings": innings_no,
            "over": over_no,
            "over_label": _balls_to_overs(completed_balls),
            "batting_team": batting_team,
            "bowling_team": bowling_team,
            "team_score": f"{total_runs}/{total_wkts}",
            "batsmen_score": _batting_snapshot(),
            "bowler": bowler_name,
            "over_timeline": list(over_timeline),
        }
        over_summaries.append(summary)
        if feed is not None:
            current_striker = (batting_order[striker_i]["name"]
                               if striker_i < len(batting_order) else "")
            feed.append({
                "innings": innings_no,
                "over": _balls_to_overs(completed_balls),
                "score": summary["team_score"],
                "striker": current_striker,
                "bowler": bowler_name,
                "event": "end_of_over",
                "team_score": summary["team_score"],
                "batsmen_score": summary["batsmen_score"],
                "over_timeline": summary["over_timeline"],
                "text": (f"End of over {over_no}: {batting_team} "
                         f"{summary['team_score']}"),
            })

    # -- Main simulation loop --
    for over_idx in range(overs):
        if total_wkts >= 10 or chased:
            break

        # Select bowler via BowlerManager
        overs_remaining_in_innings = overs - over_idx
        eligible_now = bowler_mgr.get_eligible_bowlers(over_idx, overs_remaining_in_innings)
        if not eligible_now:
            # Fallback: use old rating-based selector
            eligible_now = [
                _select_fallback_bowler(
                    _eligible_bowlers(bowling_adapted),
                    bowl_stats_by_name,
                    over_idx, overs,
                    last_bowler_name,
                    engine_fmt.max_bowler_overs,
                )
            ]
            if not eligible_now or eligible_now[0] is None:
                break

        # Pick highest-rated eligible bowler, avoiding consecutive same bowler
        def _bowler_score(p):
            stat = bowl_stats_by_name.get(p.get("name", ""), {})
            rating = p.get("bowling_rating", 0)
            overs_done = stat.get("balls", 0) / 6
            wickets = stat.get("wickets", 0)
            is_death = engine_fmt.is_death(over_idx)
            is_pp = engine_fmt.is_powerplay(over_idx)
            v = rating * 2.0 + wickets * 12
            if overs_done > 0:
                economy = stat.get("runs", 0) / overs_done
                v += max(0, 10 - economy) * 6
            if is_death:
                v += rating * 0.5
            if is_pp and rating >= 80:
                v += 15
            return v

        bowler = max(eligible_now, key=_bowler_score)
        plan.append(bowler)
        last_bowler_name = bowler.get("name")
        bw = bowl_stats_by_name[last_bowler_name]

        over_bowler_runs = 0
        over_had_extra = False
        balls_this_over = 0
        over_timeline = []

        while balls_this_over < 6:
            if total_wkts >= 10 or chased:
                break

            striker = batting_order[striker_i]
            bs = bat_stats[id(striker)]
            striker_name = striker["name"]
            batting_position = striker_i + 1

            # Build match_state for pressure engine
            balls_left = overs * 6 - legal_balls
            required_rr = 0.0
            if target is not None and balls_left > 0:
                needed = max(0, target - total_runs)
                required_rr = needed / balls_left * 6.0
            match_state = {
                "innings": innings_no,
                "current_over": over_idx,
                "score": total_runs,
                "wickets": total_wkts,
                "required_run_rate": required_rr,
                "overs_remaining": overs_remaining_in_innings,
            }

            # Pressure score (0-100 scale) from unified risk factor
            risk_factor = pressure_eng.calculate_unified_risk_factor(match_state)
            pressure_score = min(100.0, max(0.0, (risk_factor - 1.0) * 50.0))
            pressure_effects = pressure_eng.get_pressure_effects(
                pressure_score,
                striker.get("batting_rating", 50),
                bowler.get("bowling_rating", 50),
                pitch_type,
            )

            # GSME game state
            game_state = compute_game_state_vector(
                ball_history=ball_history[-BALL_HISTORY_WINDOW:],
                score=total_runs,
                current_over=over_idx,
                current_ball=balls_this_over,
                wickets=total_wkts,
                innings=innings_no,
                target=target or 0,
                pitch=pitch_type,
                format_config=engine_fmt,
            )

            # Batter form/streak
            streak = batter_streaks.get(striker_name, {"boundaries": 0})
            batter_with_form = dict(striker)

            # Bowler effectiveness with fatigue
            fatigue = bowler_mgr.get_fatigue_mult(last_bowler_name)
            bowler_effective = dict(bowler)
            bowler_effective["bowling_rating"] = bowler.get("bowling_rating", 40) * fatigue

            # pitch_wear: fraction of balls bowled this innings
            total_balls = overs * 6
            pitch_wear = min(1.0, legal_balls / max(1, total_balls))

            oc = _normalize_outcome(calculate_outcome(
                batter=batter_with_form,
                bowler=bowler_effective,
                pitch=pitch_type,
                streak=streak,
                over_number=over_idx,
                batter_runs=bs["runs"],
                innings=innings_no,
                pressure_effects=pressure_effects,
                allow_extras=True,
                free_hit=free_hit,
                balls_faced=bs["balls"],
                game_state=game_state,
                pitch_wear=pitch_wear,
                batting_position=batting_position,
                format_config=engine_fmt,
            ))

            otype = oc.get("type")        # "run" | "wicket" | "extra"
            runs = oc.get("runs", 0)
            is_extra = oc.get("is_extra", False)
            extra_type = oc.get("extra_type", "")
            batter_out = oc.get("batter_out", False)
            wicket_type = oc.get("wicket_type")

            # -- Extra deliveries (Wide, No Ball) are not legal balls --
            if is_extra and extra_type in ("Wide", "No Ball"):
                total_runs += 1 + runs
                bw["runs"] += 1 + runs
                over_bowler_runs += 1 + runs
                over_had_extra = True

                if extra_type == "Wide":
                    extras["wides"] += 1
                    timeline.append("WD")
                    over_timeline.append("WD")
                    _emit("wide", striker_name, last_bowler_name, 0, legal_balls)
                else:  # No Ball
                    extras["noballs"] += 1
                    if runs:
                        bs["runs"] += runs
                        if runs == 4:
                            bs["fours"] += 1
                        elif runs == 6:
                            bs["sixes"] += 1
                    timeline.append("NB")
                    over_timeline.append("NB")
                    _emit("no_ball", striker_name, last_bowler_name, runs, legal_balls)
                    free_hit = True
                    if runs % 2 == 1:
                        striker_i, non_striker_i = non_striker_i, striker_i

                ball_history.append(make_ball_event(oc))
                if target is not None and total_runs >= target:
                    chased = True
                continue  # not a legal ball

            # -- Legal ball --
            balls_this_over += 1
            legal_balls += 1
            bw["balls"] += 1
            bs["balls"] += 1

            if is_extra and extra_type in ("Byes", "LegByes", "LegBye", "Leg Byes"):
                # Leg byes / byes: runs to team, not to bowler, not to batter
                total_runs += runs
                extras["legbyes"] += runs
                over_had_extra = True
                timeline.append("LB")
                over_timeline.append("LB")
                _emit("extras", striker_name, last_bowler_name, runs, legal_balls)
                if runs % 2 == 1:
                    striker_i, non_striker_i = non_striker_i, striker_i

            elif otype == "wicket" or batter_out:
                wtype = wicket_type or "Caught"
                bat_runs = runs  # any completed runs on a run-out
                if bat_runs:
                    total_runs += bat_runs
                    bs["runs"] += bat_runs
                    over_bowler_runs += bat_runs
                    bw["runs"] += bat_runs
                total_wkts += 1
                bs["out"] = True
                bs["how"] = wtype
                bs["bowler"] = last_bowler_name
                if wtype != "Run Out":
                    bw["wickets"] += 1
                timeline.append("W")
                over_timeline.append("W")
                fow.append((total_runs, total_wkts, striker_name,
                            _balls_to_overs(legal_balls)))
                event_key = _WICKET_EVENT.get(wtype, "wicket_caught_fielder")
                _emit(event_key, striker_name, last_bowler_name, 0, legal_balls)
                free_hit = False
                batter_streaks.pop(striker_name, None)
                if next_i < len(batting_order):
                    striker_i = next_i
                    next_i += 1
                else:
                    total_wkts = 10

            else:  # run outcome
                total_runs += runs
                bs["runs"] += runs
                over_bowler_runs += runs
                bw["runs"] += runs
                if runs == 4:
                    bs["fours"] += 1
                    cur = batter_streaks.get(striker_name, {"boundaries": 0})
                    batter_streaks[striker_name] = {"boundaries": cur["boundaries"] + 1}
                elif runs == 6:
                    bs["sixes"] += 1
                    cur = batter_streaks.get(striker_name, {"boundaries": 0})
                    batter_streaks[striker_name] = {"boundaries": cur["boundaries"] + 1}
                else:
                    batter_streaks[striker_name] = {"boundaries": 0}
                timeline.append(str(runs))
                over_timeline.append(str(runs))
                _emit(_RUNS_TO_EVENT.get(runs, "general"),
                      striker_name, last_bowler_name, runs, legal_balls)
                # A free hit is consumed by this one legal delivery — clear it
                # even on a boundary, else the run-out-only protection would
                # wrongly carry to the next ball.
                free_hit = False
                if runs % 2 == 1:
                    striker_i, non_striker_i = non_striker_i, striker_i

            ball_history.append(make_ball_event(oc))
            if len(ball_history) > BALL_HISTORY_WINDOW:
                ball_history = ball_history[-BALL_HISTORY_WINDOW:]

            if target is not None and total_runs >= target:
                chased = True

        # End of over
        if balls_this_over >= 6:
            if over_bowler_runs == 0 and not over_had_extra:
                bw["maidens"] += 1
            bowler_mgr.record_over_completion(last_bowler_name, over_bowler_runs)
            _record_over_summary(over_idx + 1, last_bowler_name, over_timeline, legal_balls)
            striker_i, non_striker_i = non_striker_i, striker_i

    opening_bowler_name = plan[0]["name"] if plan else ""
    opening_striker_name = batting_order[0]["name"] if batting_order else ""
    opening_non_striker_name = batting_order[1]["name"] if len(batting_order) > 1 else ""

    # Build bowl_stats keyed by player id for backward-compatible scorecard rendering
    final_bowl_stats = {}
    for p in bowling_adapted:
        final_bowl_stats[id(p)] = bowl_stats_by_id[id(p)]

    return {
        "innings": innings_no,
        "batting_team": batting_team,
        "bowling_team": bowling_team,
        "openers": [n for n in (opening_striker_name, opening_non_striker_name) if n],
        "opening_striker": opening_striker_name,
        "opening_bowler": opening_bowler_name,
        "innings_intro": [
            f"INNINGS {innings_no}",
            f"Batting {batting_team}",
            f"Bowling {bowling_team}",
            (f"{opening_striker_name} and {opening_non_striker_name} will open "
             f"the batting for {batting_team}. {opening_striker_name} is on strike."
             if opening_striker_name and opening_non_striker_name else ""),
            (f"{opening_bowler_name} will bowl the opening over for {bowling_team}"
             if opening_bowler_name else ""),
        ],
        "runs": total_runs,
        "wickets": total_wkts,
        "overs": _balls_to_overs(legal_balls),
        "balls": legal_balls,
        "extras": extras,
        "extras_total": extras["wides"] + extras["noballs"] + extras["legbyes"],
        "fow": fow,
        "timeline": timeline,
        "over_summaries": over_summaries,
        "order": batting_order,
        "bat_stats": {id(p): bat_stats[id(p)] for p in batting_order},
        "bowl_plan": plan,
        "bowl_stats": final_bowl_stats,
        "_order_objs": batting_order,
    }


def simulate_match(home_xi, away_xi, overs, pitch_type,
                   home_name, away_name, toss_winner=None,
                   toss_decision=None, commentary=None, fmt=None, scenario=True):
    """Simulate a full two-innings match using the SimCricketX engine.

    home_xi / away_xi: lists of player dicts with keys: name, rating,
        bat_rating, bowl_rating, category, bowl_style, bowl_hand, bat_hand.
    fmt: optional services.match_formats format dict.
         If omitted, a format is derived from ``overs``.
    Returns a dict with both innings, the result, and a commentary feed.
    """
    from services.ground_conditions import get_pitch_meta

    pitch_meta = get_pitch_meta(pitch_type)
    run_factor = pitch_meta.get("run_factor", 1.0)

    teams_by_name = {home_name: home_xi, away_name: away_xi}
    if toss_winner not in teams_by_name:
        toss_winner = home_name

    if toss_decision is None:
        normalized_decision = str(pitch_meta.get("ideal_toss", "bat")).strip().lower()
    else:
        normalized_decision = str(toss_decision).strip().lower()
    if normalized_decision not in ("bat", "bowl"):
        normalized_decision = "bat"

    toss_winner_is_away = toss_winner == away_name
    toss_loser_name = home_name if toss_winner_is_away else away_name

    if normalized_decision == "bat":
        first_name = toss_winner
        second_name = toss_loser_name
    else:
        first_name = toss_loser_name
        second_name = toss_winner

    first_bat, first_bowl = teams_by_name[first_name], teams_by_name[second_name]

    feed = []
    inn1 = simulate_innings(first_bat, first_bowl, overs, pitch_type,
                            1, first_name, second_name,
                            target=None, commentary=commentary, feed=feed,
                            fmt=fmt, run_factor=run_factor, scenario=scenario)
    target = inn1["runs"] + 1
    inn2 = simulate_innings(first_bowl, first_bat, overs, pitch_type,
                            2, second_name, first_name,
                            target=target, commentary=commentary, feed=feed,
                            fmt=fmt, run_factor=run_factor, scenario=scenario)

    result = _compute_result(inn1, inn2, target)

    # Tie → resolve with an auto super over (reusing existing dynamics engine).
    super_over = None
    if result["margin_type"] == "tie":
        from services.match_dynamics import resolve_super_over
        so_feed = []
        # first_bat="b" — the side that batted SECOND in the match bats first in
        # the super over, as the Laws have it (and as the interactive Super Over
        # in handlers/super_over.py already did).
        super_over = resolve_super_over(
            first_bat, first_bowl, first_name, second_name, pitch_type,
            run_factor=run_factor, commentary=commentary, feed=so_feed,
            first_bat="b")
        feed.extend(so_feed)
        if not super_over.get("shared"):
            result = {"winner": super_over["winner"], "loser": super_over["loser"],
                      "margin_type": "super_over", "margin": 0,
                      "text": f"Match tied — {super_over['text']}"}
        else:
            result = {"winner": None, "loser": None, "margin_type": "tie",
                      "margin": 0, "text": super_over["text"]}

    # Derive fmt label for output
    engine_fmt = _fmt_to_engine_fmt(fmt, overs)
    fmt_label = (fmt.get("label") if fmt and isinstance(fmt, dict) else None) or engine_fmt.name

    return {
        "overs": overs,
        "pitch": pitch_type,
        "pitch_meta": pitch_meta,
        "format": fmt_label,
        "toss": {
            "winner": toss_winner,
            "decision": normalized_decision,
            "text": f"{toss_winner} won the toss and elected to {normalized_decision.title()} first",
        },
        "innings1": inn1,
        "innings2": inn2,
        "target": target,
        "result": result,
        "super_over": super_over,
        "commentary_feed": feed,
        "potm": _player_of_the_match(inn1, inn2, result),
    }


# ---------------------------------------------------------------------------
# Scorecard rendering helpers (unchanged API, kept for the handler layer)
# ---------------------------------------------------------------------------

def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_innings_card(inn):
    """Render a single innings as an HTML scorecard for Telegram.

    The full scorecard body is wrapped in a single expandable blockquote so the
    message stays collapsed in chat until the reader taps to expand it. Telegram
    does not allow nested blockquotes, so the batting/bowling tables are kept as
    plain lines inside the one outer expandable quote.
    """
    e = inn["extras"]
    # Header lines stay visible above the expandable quote.
    header = [
        f"🏏 <b>{_esc(inn['batting_team'])}</b>",
        f"<b>{inn['runs']}/{inn['wickets']}</b> ({inn['overs']} ov)",
    ]

    body = ["<b>BATTING</b>"]
    dnb = []
    for p in inn["order"]:
        bs = inn["bat_stats"][id(p)]
        if bs["balls"] == 0 and not bs["out"]:
            dnb.append(p["name"])
            continue
        sr = round(bs["runs"] / bs["balls"] * 100, 1) if bs["balls"] else 0.0
        mark = "" if bs["out"] else "*"
        dismissal = f" [{_esc(bs['how'])}]" if bs["out"] and bs["how"] else ""
        body.append(
            f"{_esc(p['name'])} {mark}{bs['runs']} ({bs['balls']}){dismissal} "
            f"4s:{bs['fours']} 6s:{bs['sixes']} SR:{sr}"
        )
    body.append(
        f"Extras: {inn['extras_total']} "
        f"(wd {e['wides']}, nb {e['noballs']}, lb {e['legbyes']})"
    )
    body.append(f"<b>TOTAL: {inn['runs']}/{inn['wickets']} ({inn['overs']} ov)</b>")
    if dnb:
        body.append(f"<i>DNB: {_esc(', '.join(dnb))}</i>")

    body.append("━━━━━━━━━━━━━━━━━━━")
    body.append("<b>BOWLING</b>")
    seen = set()
    for bp in inn["bowl_plan"]:
        if id(bp) in seen:
            continue
        seen.add(id(bp))
        bw = inn["bowl_stats"].get(id(bp), _new_bowl_stat())
        ov = f"{bw['balls'] // 6}.{bw['balls'] % 6}"
        econ = round(bw["runs"] / (bw["balls"] / 6), 2) if bw["balls"] else 0.0
        body.append(
            f"{_esc(bp['name'])} {ov}-{bw['maidens']}-{bw['runs']}-{bw['wickets']} (econ {econ})"
        )

    if inn["fow"]:
        fow = " · ".join(f"{r}/{w} ({_esc(nm)}, {ov})" for r, w, nm, ov in inn["fow"])
        body.append(f"<b>FoW:</b> {fow}")

    return "\n".join(header) + "\n<blockquote expandable>" + "\n".join(body) + "</blockquote>"


def render_result(match):
    """Render the final result / winner announcement."""
    res = match["result"]
    i1, i2 = match["innings1"], match["innings2"]
    so_line = ""
    if match.get("super_over") and match["super_over"].get("innings"):
        parts = []
        for fn, fi, sn, si in match["super_over"]["innings"]:
            parts.append(f"⚡ Super Over — {_esc(fn)}: {fi['runs']}/{fi['wickets']} · "
                         f"{_esc(sn)}: {si['runs']}/{si['wickets']}")
        so_line = "\n" + "\n".join(parts)
    return (
        "🏆 <b>MATCH RESULT</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{_esc(i1['batting_team'])}: <b>{i1['runs']}/{i1['wickets']}</b> ({i1['overs']})\n"
        f"{_esc(i2['batting_team'])}: <b>{i2['runs']}/{i2['wickets']}</b> ({i2['overs']})"
        f"{so_line}\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🎉 <b>{_esc(res['text'])}</b>\n"
        f"🌟 Player of the Match: <b>{_esc(match['potm'])}</b>"
    )


def _top_batters(inn, limit=4):
    rows = []
    for p in inn["order"]:
        bs = inn["bat_stats"][id(p)]
        if bs["balls"] == 0 and not bs["out"]:
            continue
        rows.append({
            "name": p["name"],
            "runs": bs["runs"],
            "balls": bs["balls"],
            "fours": bs["fours"],
            "sixes": bs["sixes"],
            "out": bs["out"],
        })
    return sorted(rows, key=lambda r: (r["runs"], -r["balls"]), reverse=True)[:limit]


def _top_bowlers(inn, limit=4):
    rows = []
    seen = set()
    for bp in inn["bowl_plan"]:
        if id(bp) in seen:
            continue
        seen.add(id(bp))
        bw = inn["bowl_stats"].get(id(bp), _new_bowl_stat())
        if bw["balls"] == 0:
            continue
        rows.append({
            "name": bp["name"],
            "wickets": bw["wickets"],
            "runs": bw["runs"],
            "overs": f"{bw['balls'] // 6}.{bw['balls'] % 6}",
        })
    return sorted(rows, key=lambda r: (r["wickets"], -r["runs"]), reverse=True)[:limit]


def _potm_stats(match):
    name = match.get("potm")
    if not name:
        return None
    runs = wickets = 0
    for inn in (match["innings1"], match["innings2"]):
        for p in inn["order"]:
            if p["name"] == name:
                runs += inn["bat_stats"][id(p)]["runs"]
        seen = set()
        for bp in inn["bowl_plan"]:
            if id(bp) in seen:
                continue
            seen.add(id(bp))
            if bp["name"] == name:
                wickets += inn["bowl_stats"].get(id(bp), {}).get("wickets", 0)
    bits = []
    if runs:
        bits.append(f"{runs} runs")
    if wickets:
        bits.append(f"{wickets} wkts")
    return ", ".join(bits) or "Impact performance"


def render_match_summary_image(match, *, text_settings=None, stadium=None, match_no=None):
    """Render the /sim match summary PNG bytes, or None if rendering fails."""
    from services.match_summary_card import generate_match_summary

    i1, i2 = match["innings1"], match["innings2"]
    res = match["result"]
    winner = res.get("winner") or "Tied"
    margin = res.get("text") or "Match tied"
    top_per_team = {
        "inn1": {
            "team": i1["batting_team"],
            "batters": _top_batters(i1),
            "bowlers": _top_bowlers(i1),
        },
        "inn2": {
            "team": i2["batting_team"],
            "batters": _top_batters(i2),
            "bowlers": _top_bowlers(i2),
        },
    }
    return generate_match_summary(
        inn1_team=i1["batting_team"],
        inn1_runs=i1["runs"],
        inn1_wickets=i1["wickets"],
        inn1_overs=i1["overs"],
        inn2_team=i2["batting_team"],
        inn2_runs=i2["runs"],
        inn2_wickets=i2["wickets"],
        inn2_overs=i2["overs"],
        winner_name=winner,
        win_margin_text=margin,
        overs_total=match["overs"],
        potm_name=match.get("potm"),
        potm_stats=_potm_stats(match),
        top_per_team=top_per_team,
        stadium=stadium or match.get("pitch"),
        match_date=datetime.utcnow(),
        match_no=match_no,
        text_settings=text_settings,
    )


def _compute_result(inn1, inn2, target):
    chasing_runs = inn2["runs"]
    chasing_wkts = inn2["wickets"]
    chasing_name = inn2["batting_team"]
    defending_name = inn1["batting_team"]
    if chasing_runs >= target:
        wkts_left = max(0, 10 - chasing_wkts)
        return {"winner": chasing_name, "loser": defending_name,
                "margin_type": "wickets", "margin": wkts_left,
                "text": f"{chasing_name} won by {wkts_left} wicket"
                        f"{'s' if wkts_left != 1 else ''}"}
    runs_short = target - 1 - chasing_runs
    if runs_short == 0:
        return {"winner": None, "loser": None, "margin_type": "tie",
                "margin": 0, "text": "Match tied"}
    return {"winner": defending_name, "loser": chasing_name,
            "margin_type": "runs", "margin": runs_short,
            "text": f"{defending_name} won by {runs_short} run"
                    f"{'s' if runs_short != 1 else ''}"}


def _player_of_the_match(inn1, inn2, result):
    """Highest impact player by runs + wickets*25 across both innings.

    POTM rule: the award goes to the highest-impact player on the winning team;
    a losing-team player is only eligible with 50+ impact. A tie (no winner)
    falls back to the overall highest impact.
    """
    # name -> {"impact": int, "team": str}. Batters belong to their innings'
    # batting team, bowlers to its bowling team.
    impact = {}

    def _bump(name, team, amount):
        entry = impact.setdefault(name, {"impact": 0, "team": team})
        entry["impact"] += amount
        if team and not entry.get("team"):
            entry["team"] = team

    for inn in (inn1, inn2):
        bat_team = inn.get("batting_team")
        bowl_team = inn.get("bowling_team")
        for p in inn["order"]:
            _bump(p["name"], bat_team, inn["bat_stats"][id(p)]["runs"])
        seen_bowlers = set()
        for bp in inn["bowl_plan"]:
            if id(bp) in seen_bowlers:
                continue
            seen_bowlers.add(id(bp))
            wkts = inn["bowl_stats"].get(id(bp), {}).get("wickets", 0)
            _bump(bp["name"], bowl_team, wkts * 25)
    if not impact:
        return None

    winner_name = (result or {}).get("winner")
    eligible = {
        name: data for name, data in impact.items()
        if (winner_name and data["team"] == winner_name)
        or (winner_name and data["impact"] >= 50)
        or not winner_name
    }
    if not eligible:
        eligible = impact
    return max(eligible.items(), key=lambda kv: kv[1]["impact"])[0]
