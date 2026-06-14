"""Over-by-over "Approach" simulation for Challenge League (/cipl) matches.

Unlike services/sim_match.py (which auto-simulates whole innings), this module
drives the match interactively: each over the bowling captain picks a bowler and
a bowling approach, the batting captain picks a batting approach, and then this
module simulates the six balls of that over using the SimCricketX engine
(ball_outcome + pressure + game-state/momentum), tilted by the chosen approaches.

State is the JSON-serialisable dict persisted through services/match_state_store.
Stats are keyed by ``str(roster_id)`` so they survive a DB round-trip. The batting
order is fixed at Playing XI selection, so new batsmen come in automatically — no
mid-over batsman pick is required (per New Features.md).
"""

import json
import logging
import os
import random

from engine.ball_outcome import calculate_outcome
from engine.pressure_engine import PressureEngine
from engine.game_state_engine import (
    make_ball_event,
    compute_game_state_vector,
    _compute_momentum,
    BALL_HISTORY_WINDOW,
)
from engine.approach_modifiers import batting_label, bowling_label
from services.sim_match import (
    _adapt_player,
    _fmt_to_engine_fmt,
    _normalize_outcome,
)

logger = logging.getLogger(__name__)

# Optional "dramatic finishes" realism layer (engine/scenario_engine.py). Loaded
# defensively — if the import fails the match simply runs without scenario
# steering, exactly as before.
try:
    from engine.scenario_engine import ScenarioEngine
except Exception:  # pragma: no cover - defensive import guard
    logger.exception("ScenarioEngine unavailable; CIPL runs without dramatic-finish steering")
    ScenarioEngine = None

WICKET_LIMIT = 10  # all out after 10 wickets (11-man side)

# Dramatic-finish scenario types the engine can steer a 2nd-innings chase toward.
SCENARIO_TYPES = ("last_ball_six", "win_by_1_run", "super_over_thriller")

# Full SimCricketX commentary engine — micro (per-ball) lines + macro narratives
# (collapse, milestones, partnership, maiden/big/expensive over, last-over drama,
# death overs, powerplay, high-pressure dot). Loaded once; if it fails to load we
# silently fall back to the built-in terse lines so an over never crashes.
try:
    from engine.commentary_engine import CommentaryEngine
    _COMMENTARY = CommentaryEngine()
except Exception:  # pragma: no cover - defensive import guard
    logger.exception("CommentaryEngine unavailable; using fallback commentary")
    _COMMENTARY = None

_SYM = {0: "0️⃣", 1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 6: "6️⃣",
        "W": "🟥", "WD": "↔️", "NB": "🅽🅱", "LB": "🅻🅱"}


# ════════════════════════════════════════════════════════════════════
# Player conversion (ChallengePlayer ORM → engine-friendly dict)
# ════════════════════════════════════════════════════════════════════

def cp_to_player_dict(cp):
    """Convert a ChallengePlayer row into the player dict the engine expects.

    Ratings/handedness/style live in ``details_json`` (written by the admin
    panel from the master Player). ``roster_id`` uses the ChallengePlayer id so
    every selected player has a unique, stable stats key.
    """
    details = {}
    raw = getattr(cp, "details_json", None) or ""
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                details = parsed
        except Exception:
            details = {}

    def _g(*keys, default=None):
        for k in keys:
            if details.get(k) not in (None, ""):
                return details.get(k)
        return default

    category = _g("category", "role", default="Batsman")
    return {
        "roster_id": int(getattr(cp, "id")),
        "player_id": _g("source_player_id", default=getattr(cp, "source_player_id", None)),
        "name": getattr(cp, "name", None) or _g("name", default="Player"),
        "rating": int(_g("rating", default=50) or 50),
        "category": category,
        "bat_rating": int(_g("bat_rating", default=50) or 50),
        "bowl_rating": int(_g("bowl_rating", default=40) or 40),
        "bowl_style": _g("bowl_style", default="") or "",
        "bowl_hand": _g("bowl_hand", default="Right") or "Right",
        "bat_hand": _g("bat_hand", default="Right") or "Right",
    }


# ════════════════════════════════════════════════════════════════════
# Scenario engine (dramatic-finish realism layer) — JSON-safe adapter
# ════════════════════════════════════════════════════════════════════
#
# engine.scenario_engine.ScenarioEngine was written against engine.match.Match
# (an in-memory object). CIPL persists its match as a JSON-serialisable dict
# between overs, so we (1) expose that dict through a small attribute shim that
# mirrors the handful of Match fields the ScenarioEngine reads, and (2) marshal
# the engine's own mutable state (finale script, ball index, active flag …) in
# and out of state["scenario"] each over.


class _ScenarioFmtShim:
    """Stand-in for Match.fmt — the ScenarioEngine only reads ``.overs``."""
    __slots__ = ("overs",)

    def __init__(self, overs):
        self.overs = int(overs or 20)


class _ScenarioMatchShim:
    """Read-only adapter exposing the CIPL state dict through the attribute
    interface engine.scenario_engine.ScenarioEngine expects."""

    def __init__(self, state):
        self._s = state
        self.fmt = _ScenarioFmtShim(state.get("overs", 20))

    @property
    def innings(self):
        return self._s.get("innings", 1)

    @property
    def target(self):
        return self._s.get("target")

    @property
    def score(self):
        return self._s.get("total_runs", 0)

    @property
    def wickets(self):
        return self._s.get("total_wickets", 0)

    @property
    def current_over(self):
        # ScenarioEngine works in 0-indexed overs; CIPL stores it 1-indexed.
        return self._s.get("current_over", 1) - 1

    @property
    def current_ball(self):
        # Legal balls already bowled this over == 0-indexed upcoming ball.
        return self._s.get("current_ball", 0)

    @property
    def bowling_team(self):
        return self._s.get("bowl_xi", [])


def _scenario_probability():
    """Chance (0–1) that an eligible chase is armed with a dramatic finish.
    Tunable via the CIPL_SCENARIO_PROBABILITY env var (default 0.30)."""
    try:
        return max(0.0, min(1.0, float(os.environ.get("CIPL_SCENARIO_PROBABILITY", "0.30"))))
    except (TypeError, ValueError):
        return 0.30


def _maybe_enable_scenario(state):
    """Optionally arm a dramatic-finish scenario for the 2nd-innings chase.

    Gated to 20-over matches because the scenario corridors / phase boundaries
    (free-play < 15, convergence 15–17, finale 18–19) are calibrated for T20.
    The engine self-disables per over when a scripted finish would look
    unrealistic, so arming it is always safe.
    """
    state["scenario"] = None
    if ScenarioEngine is None:
        return
    if int(state.get("overs", 0)) != 20:
        return
    if not state.get("target"):
        return
    if random.random() >= _scenario_probability():
        return
    stype = random.choice(SCENARIO_TYPES)
    state["scenario"] = {
        "type": stype,
        "active": True,
        "finale_script": None,
        "finale_ball_index": 0,
        "convergence_logged": False,
        "endgame_checked_overs": [],
    }
    logger.info("[CIPL Scenario] Armed dramatic finish '%s' for match %s (target=%s)",
                stype, state.get("match_id"), state.get("target"))


def _load_scenario_engine(state):
    """Rebuild a ScenarioEngine from state["scenario"], restoring its mutable
    fields. Returns None when no scenario is active for this innings."""
    if ScenarioEngine is None:
        return None
    sc = state.get("scenario")
    if not sc or not sc.get("active") or not sc.get("type"):
        return None
    if state.get("innings") != 2 or not state.get("target"):
        return None
    try:
        eng = ScenarioEngine(sc["type"], _ScenarioMatchShim(state))
    except Exception:  # pragma: no cover - defensive
        logger.exception("[CIPL Scenario] Failed to construct ScenarioEngine")
        return None
    eng.active = bool(sc.get("active", True))
    eng.finale_script = sc.get("finale_script")
    eng.finale_ball_index = int(sc.get("finale_ball_index", 0))
    eng._convergence_logged = bool(sc.get("convergence_logged", False))
    eng._endgame_checked_overs = set(sc.get("endgame_checked_overs", []))
    return eng


def _save_scenario_engine(state, eng):
    """Marshal a ScenarioEngine's mutable state back into state["scenario"]."""
    if eng is None:
        return
    state["scenario"] = {
        "type": eng.scenario_type,
        "active": bool(eng.active),
        "finale_script": eng.finale_script,
        "finale_ball_index": int(eng.finale_ball_index),
        "convergence_logged": bool(getattr(eng, "_convergence_logged", False)),
        "endgame_checked_overs": sorted(getattr(eng, "_endgame_checked_overs", set())),
    }


# ════════════════════════════════════════════════════════════════════
# State construction
# ════════════════════════════════════════════════════════════════════

def _new_bat_stat():
    return {"runs": 0, "balls": 0, "fours": 0, "sixes": 0,
            "out": False, "how_out": "", "bowled_by": ""}


def _new_bowl_stat():
    return {"balls": 0, "runs": 0, "wickets": 0, "overs_done": 0,
            "this_over_balls": 0, "this_over_runs": 0, "maidens": 0}


def build_cipl_state(match_id, overs, bat_user_id, bowl_user_id,
                     bat_user_tg, bowl_user_tg, bat_xi, bowl_xi,
                     bat_team_name, bowl_team_name, chat_id,
                     pitch_type="Hard", is_private=False, stadium=None,
                     bat_team_code="", bowl_team_code="",
                     bat_team_emoji="🏏", bowl_team_emoji="🏏"):
    """Build the initial state dict for a Challenge League approach match."""
    bat_stats = {str(p["roster_id"]): _new_bat_stat() for p in bat_xi}
    bowl_stats = {str(p["roster_id"]): _new_bowl_stat() for p in bowl_xi}
    return {
        "mode": "cipl_approach",
        "match_id": match_id, "overs": overs,
        # Team identity for the broadcast-style scorecard card
        "bat_team_code": bat_team_code, "bowl_team_code": bowl_team_code,
        "bat_team_emoji": bat_team_emoji or "🏏",
        "bowl_team_emoji": bowl_team_emoji or "🏏",
        "innings": 1, "target": None,
        "bat_team_id": bat_user_id, "bowl_team_id": bowl_user_id,
        "bat_user_tg": bat_user_tg, "bowl_user_tg": bowl_user_tg,
        "bat_team_name": bat_team_name, "bowl_team_name": bowl_team_name,
        "bat_xi": bat_xi, "bowl_xi": bowl_xi,
        "batting_order": list(bat_xi),
        "current_over": 1, "current_ball": 0,
        "total_runs": 0, "total_wickets": 0, "extras_total": 0,
        "striker_idx": 0, "non_striker_idx": 1, "next_batsman_idx": 2,
        "current_bowler": None, "prev_bowler_rid": None,
        "batting_approach": None, "bowling_approach": None,
        "bat_stats": bat_stats, "bowl_stats": bowl_stats,
        "timeline": [], "over_runs": [], "fow": [],
        "ball_history": [], "batter_streaks": {},
        "free_hit": False,
        # Narrative inputs for the commentary engine.
        "partnership_runs": 0, "partnership_balls": 0,
        "partnership_history": [], "wkt_marks": [],
        "momentum_prev": 0.0,
        "over_msg_ids": [],
        "commentary_log": [],
        "chat_id": chat_id, "is_private": is_private,
        "pitch_type": pitch_type or "Hard", "stadium": stadium,
        "wicket_limit": WICKET_LIMIT,
        # Dramatic-finish steering (armed at the innings break for 20-over chases).
        "scenario": None,
        # innings-1 archive (filled at the break)
        "inn1_runs": 0, "inn1_wickets": 0, "inn1_overs": "0.0",
    }


# ════════════════════════════════════════════════════════════════════
# Bowler eligibility
# ════════════════════════════════════════════════════════════════════

def max_bowler_overs(state):
    """Per-bowler over quota — ceil(overs / 5), as in standard limited-overs."""
    return max(1, -(-state["overs"] // 5))


def _overs_bowled(state, rid):
    return state["bowl_stats"].get(str(rid), {}).get("balls", 0) // 6


def eligible_bowlers(state):
    """Bowlers + all-rounders, excluding the previous over's bowler and any who
    have used their full over quota."""
    xi = state.get("bowl_xi") or []
    elig = [p for p in xi if p.get("category") in ("Bowler", "All-rounder")]
    if not elig:
        elig = list(xi)
    quota = max_bowler_overs(state)
    under_quota = [p for p in elig if _overs_bowled(state, p["roster_id"]) < quota]
    if under_quota:
        elig = under_quota
    prev = state.get("prev_bowler_rid")
    if prev is not None and len(elig) > 1:
        filtered = [p for p in elig if p["roster_id"] != prev]
        if filtered:
            elig = filtered
    # Highest bowl_rating first
    return sorted(elig, key=lambda p: p.get("bowl_rating", 0), reverse=True)


def find_player(xi, roster_id):
    for p in xi:
        if p["roster_id"] == roster_id:
            return p
    return None


# ════════════════════════════════════════════════════════════════════
# Player traits (active only in /letsplay — personal-roster matches)
# ════════════════════════════════════════════════════════════════════
#
# Challenge League (/cipl) players carry no traits, so the trait hook below is
# never built for them and the engine call stays a no-op. /letsplay attaches a
# ``traits`` list to each player dict (see handlers/letsplay.py); those traits
# are folded into the engine's final outcome weights every ball via the generic
# ``weight_hook`` on engine.ball_outcome.calculate_outcome.

# trait_engine probability keys → engine raw-weight keys.
_TRAIT_KEY_MAP = (("6", "Six"), ("4", "Four"), ("W", "Wicket"), ("dot", "Dot"))


def _apply_trait_weights(raw_weights, striker_traits, bowler_traits, ctx):
    """Nudge the engine's raw outcome weights by the active player traits.

    services.trait_engine.apply_traits works on a ~0-100 percentage-point scale
    (the scale of the legacy probability engine), so we rescale the engine's
    normalised weights to sum to 100, let apply_traits add its deltas to the
    Six / Four / Wicket / Dot buckets, then rescale back. The engine renormalises
    afterwards. Capping / stacking / level logic is reused verbatim, so /letsplay
    traits behave exactly like /cm and /wpm traits.
    """
    try:
        from services.trait_engine import apply_traits
    except Exception:
        return raw_weights
    total = sum(raw_weights.values())
    if total <= 0:
        return raw_weights
    scale = 100.0 / total
    probs = {tkey: raw_weights.get(ekey, 0.0) * scale
             for tkey, ekey in _TRAIT_KEY_MAP}
    try:
        apply_traits(probs, striker_traits or [], bowler_traits or [], ctx)
    except Exception:
        logger.exception("letsplay trait application failed; ignoring this ball")
        return raw_weights
    new = dict(raw_weights)
    for tkey, ekey in _TRAIT_KEY_MAP:
        new[ekey] = max(0.0, probs.get(tkey, 0.0)) / scale
    return new


def _make_trait_hook(striker_traits, bowler_traits, ctx):
    """Return a one-arg weight hook (raw_weights -> raw_weights) bound to this
    delivery's traits + context, or None when neither side has any trait."""
    if not striker_traits and not bowler_traits:
        return None
    return lambda raw_weights: _apply_trait_weights(
        raw_weights, striker_traits, bowler_traits, ctx)


# ════════════════════════════════════════════════════════════════════
# Score / chase helpers
# ════════════════════════════════════════════════════════════════════

def balls_bowled(state):
    return (state["current_over"] - 1) * 6 + state["current_ball"]


def format_overs(state):
    b = balls_bowled(state)
    return f"{b // 6}.{b % 6}"


def format_score(state):
    return f"{state['total_runs']}/{state['total_wickets']}"


def current_run_rate(state):
    """Runs per over so far (0.0 before any legal ball is bowled)."""
    balls = balls_bowled(state)
    if balls <= 0:
        return 0.0
    return state["total_runs"] / balls * 6.0


def chase(state):
    if state.get("innings") != 2 or not state.get("target"):
        return None
    target = int(state["target"])
    runs_req = max(0, target - int(state["total_runs"]))
    balls_left = max(0, state["overs"] * 6 - balls_bowled(state))
    rrr = (runs_req / balls_left * 6.0) if balls_left > 0 else 0.0
    return {"target": target, "runs_required": runs_req,
            "balls_remaining": balls_left, "rrr": rrr}


def is_innings_over(state):
    c = chase(state)
    if c and c["runs_required"] == 0:
        return True
    if state["total_wickets"] >= state.get("wicket_limit", WICKET_LIMIT):
        return True
    if balls_bowled(state) >= state["overs"] * 6:
        return True
    return False


# ════════════════════════════════════════════════════════════════════
# Over simulation
# ════════════════════════════════════════════════════════════════════

def simulate_over(state):
    """Simulate the current over using the stored approaches. Mutates state and
    returns a summary dict for rendering. Auto-advances batsmen on wickets and
    stops early on all-out / target reached / innings end.
    """
    bat_app = state.get("batting_approach")
    bowl_app = state.get("bowling_approach")
    bowler = state["current_bowler"]
    bowler_rid = str(bowler["roster_id"])
    pitch = state.get("pitch_type", "Hard")
    overs_total = state["overs"]
    over_idx = state["current_over"] - 1
    innings = state["innings"]
    target = state.get("target")
    engine_fmt = _fmt_to_engine_fmt(None, overs_total)
    pressure_eng = PressureEngine(format_config=engine_fmt)
    # Dramatic-finish steering: live for the 2nd innings of an armed 20-over
    # chase, else None (and the per-ball logic below is a no-op).
    scenario_eng = _load_scenario_engine(state)

    bowl_adapted = _adapt_player(bowler)
    bws = state["bowl_stats"].setdefault(bowler_rid, _new_bowl_stat())
    bws["this_over_balls"] = 0
    bws["this_over_runs"] = 0

    ball_history = list(state.get("ball_history", []))
    streaks = dict(state.get("batter_streaks", {}))

    over_timeline = []
    over_events = []
    runs_before = state["total_runs"]
    wkts_before = state["total_wickets"]
    momentum_before = state.get("momentum_prev", 0.0)
    free_hit = state.get("free_hit", False)

    balls_this_over = 0
    deliveries = 0
    chased = bool(target) and state["total_runs"] >= target
    cmt_start = len(state.get("commentary_log", []))

    while balls_this_over < 6 and not chased:
        if state["total_wickets"] >= state.get("wicket_limit", WICKET_LIMIT):
            break
        deliveries += 1
        if deliveries > 30:  # safety against pathological extra loops
            break

        striker = state["batting_order"][state["striker_idx"]]
        srid = str(striker["roster_id"])
        bs = state["bat_stats"].setdefault(srid, _new_bat_stat())
        striker_name = striker["name"]
        batter_adapted = _adapt_player(striker)

        # Pressure
        balls_left = overs_total * 6 - balls_bowled(state)
        required_rr = 0.0
        if target is not None and balls_left > 0:
            required_rr = max(0, target - state["total_runs"]) / balls_left * 6.0
        match_state = {
            "innings": innings, "current_over": over_idx,
            "score": state["total_runs"], "wickets": state["total_wickets"],
            "required_run_rate": required_rr,
            "overs_remaining": overs_total - over_idx,
        }
        risk = pressure_eng.calculate_unified_risk_factor(match_state)
        pressure_score = min(100.0, max(0.0, (risk - 1.0) * 50.0))
        pressure_effects = pressure_eng.get_pressure_effects(
            pressure_score, batter_adapted.get("batting_rating", 50),
            bowl_adapted.get("bowling_rating", 50), pitch)

        # Scenario phase for THIS delivery (free_play / convergence / finale, or
        # "inactive" when no scenario is armed). get_phase() also runs the
        # per-over feasibility gate that self-disables unrealistic steering.
        scenario_phase = scenario_eng.get_phase() if scenario_eng else "inactive"

        # Game state / momentum. Passing scenario_phase lets GSME dampen its
        # collapse layers during convergence so the scenario can steer the
        # wicket count instead of being overwhelmed by a cascade.
        game_state = compute_game_state_vector(
            ball_history=ball_history[-BALL_HISTORY_WINDOW:],
            score=state["total_runs"], current_over=over_idx,
            current_ball=balls_this_over, wickets=state["total_wickets"],
            innings=innings, target=target or 0, pitch=pitch,
            partnership_balls=state.get("partnership_balls", 0),
            partnership_runs=state.get("partnership_runs", 0),
            scenario_phase=scenario_phase,
            format_config=engine_fmt)

        streak = streaks.get(srid, {"boundaries": 0})

        # ── Scenario engine hook ──
        # Finale phase scripts the delivery outright; free-play/convergence
        # phases instead nudge the pressure effects toward the target corridor.
        scenario_override = (scenario_eng.get_override_outcome(striker, bowler)
                             if scenario_eng else None)
        if scenario_override:
            oc = _normalize_outcome(scenario_override)
        else:
            if scenario_eng:
                for key, value in scenario_eng.get_scenario_bias({}).items():
                    if key == "dot_bonus":
                        pressure_effects[key] = pressure_effects.get(key, 0.0) + value
                    elif key in pressure_effects:
                        pressure_effects[key] *= value
                    else:
                        pressure_effects[key] = value

            pitch_wear = min(1.0, balls_bowled(state) / max(1, overs_total * 6))
            # /letsplay traits: build a per-ball weight hook from the striker's
            # and bowler's active traits (None for Challenge League players, who
            # carry no traits — so the engine call is unchanged for /cipl).
            trait_hook = _make_trait_hook(
                striker.get("traits"), bowler.get("traits"),
                {"over": state["current_over"], "total_overs": overs_total,
                 "rrr": required_rr, "bat_balls_faced": bs["balls"]})
            oc = _normalize_outcome(calculate_outcome(
                batter=batter_adapted, bowler=bowl_adapted, pitch=pitch,
                streak=streak, over_number=over_idx, batter_runs=bs["runs"],
                innings=innings, pressure_effects=pressure_effects,
                allow_extras=True, free_hit=free_hit, balls_faced=bs["balls"],
                game_state=game_state, pitch_wear=pitch_wear,
                batting_position=state["striker_idx"] + 1,
                format_config=engine_fmt,
                batting_approach=bat_app, bowling_approach=bowl_app,
                weight_hook=trait_hook))

        otype = oc.get("type")
        runs = oc.get("runs", 0)
        is_extra = oc.get("is_extra", False)
        extra_type = oc.get("extra_type", "")
        batter_out = oc.get("batter_out", False)
        wicket_type = oc.get("wicket_type")

        # Narrative inputs captured BEFORE this ball mutates state, so the engine
        # can detect milestone/partnership/collapse threshold crossings correctly.
        bs_runs_before = bs["runs"]
        partnership_before = state.get("partnership_runs", 0)
        over_runs_before = state["total_runs"] - runs_before
        recent_wkts = _recent_wickets(state)

        def _ec(is_maiden=False):
            return _engine_text(
                state, oc, striker, bowler, over_idx, innings, target,
                required_rr, bs_runs_before, partnership_before, recent_wkts,
                over_runs_before, balls_this_over, overs_total,
                is_maiden=is_maiden)

        # --- Wides / No-balls: not a legal ball ---
        if is_extra and extra_type in ("Wide", "No Ball"):
            state["total_runs"] += 1 + runs
            state["extras_total"] += 1 + runs
            state["partnership_runs"] = partnership_before + 1 + runs
            bws["runs"] += 1 + runs
            bws["this_over_runs"] += 1 + runs
            if extra_type == "Wide":
                over_timeline.append("WD")
                over_events.append({"sym": "WD", "text": f"Wide ({striker_name})"})
                _push_commentary(state, "extra", striker_name,
                                 _ec() or f"Wide. {bowler['name']} strays down leg.",
                                 runs=1 + runs, event_key="wide")
            else:
                if runs:
                    bs["runs"] += runs
                    if runs == 4:
                        bs["fours"] += 1
                    elif runs == 6:
                        bs["sixes"] += 1
                over_timeline.append("NB")
                over_events.append({"sym": "NB", "text": f"No ball +{runs}"})
                _push_commentary(state, "extra", striker_name,
                                 _ec() or "No ball! Free hit coming up.",
                                 runs=1 + runs, event_key="no_ball")
                free_hit = True
                if runs % 2 == 1:
                    _swap_strike(state)
            ball_history.append(make_ball_event(oc))
            if target is not None and state["total_runs"] >= target:
                chased = True
            continue

        # --- Legal ball ---
        balls_this_over += 1
        state["current_ball"] += 1
        bws["balls"] += 1
        bws["this_over_balls"] += 1
        bs["balls"] += 1
        # Legal ball faced by the current pair → counts toward the partnership.
        state["partnership_balls"] = state.get("partnership_balls", 0) + 1

        if is_extra and extra_type in ("Byes", "LegByes", "LegBye", "Leg Byes"):
            state["total_runs"] += runs
            state["extras_total"] += runs
            state["partnership_runs"] = partnership_before + runs
            over_timeline.append("LB")
            over_events.append({"sym": "LB", "text": f"Leg byes +{runs}"})
            _push_commentary(state, "extra", striker_name,
                             _ec() or f"Leg byes, {runs} run(s).",
                             runs=runs, event_key="legbye")
            if runs % 2 == 1:
                _swap_strike(state)

        elif otype == "wicket" or batter_out:
            wtype = wicket_type or "Caught"
            if runs:  # completed runs on a run-out
                state["total_runs"] += runs
                bs["runs"] += runs
                bws["runs"] += runs
                bws["this_over_runs"] += runs
                state["partnership_runs"] = partnership_before + runs
            state["total_wickets"] += 1
            bs["out"] = True
            bs["how_out"] = wtype
            bs["bowled_by"] = bowler["name"]
            # Attribute a fielder so the scorecard shows a full dismissal line
            # (c <fielder> b <bowler> / run out (<fielder>) / st <keeper> ...).
            needs_fielder = wtype.lower() in ("caught", "run out", "stumped")
            fielder = _pick_fielder(state, bowler,
                                    allow_bowler=(wtype.lower() == "run out")) if needs_fielder else ""
            bs["fielder"] = fielder
            bs["dismissal"] = _dismissal_text(wtype, bowler["name"], fielder)
            if wtype != "Run Out":
                bws["wickets"] += 1
            over_timeline.append("W")
            over_events.append({"sym": "W", "text": f"WICKET! {striker_name} {wtype}"})
            state["fow"].append([state["total_runs"], state["total_wickets"],
                                 striker_name, format_overs(state)])
            # Ball row (paints the W badge in the over column) + the red OUT card.
            _wkt_line = _ec() or f"OUT! {striker_name} {bs['dismissal']}."
            _push_commentary(state, "ball", striker_name, _wkt_line,
                             runs=runs, is_wicket=True, event_key="wicket")
            non_striker = state["batting_order"][state["non_striker_idx"]]
            _push_commentary(
                state, "wicket", striker_name,
                f"OUT! {striker_name} {bs['runs']}({bs['balls']}) {bs['dismissal']}")
            # Record the completed partnership for the Partnership tab, then reset.
            state.setdefault("partnership_history", []).append({
                "wicket": state["total_wickets"],
                "batsman1": striker_name,
                "batsman2": non_striker.get("name", ""),
                "runs": state.get("partnership_runs", 0),
                "balls": state.get("partnership_balls", 0),
            })
            state["partnership_runs"] = 0
            state["partnership_balls"] = 0
            state.setdefault("wkt_marks", []).append(balls_bowled(state))
            free_hit = False
            streaks.pop(srid, None)
            # Auto-promote next batsman (order fixed in Playing XI)
            if state["next_batsman_idx"] < len(state["batting_order"]):
                state["striker_idx"] = state["next_batsman_idx"]
                state["next_batsman_idx"] += 1
            else:
                state["total_wickets"] = state.get("wicket_limit", WICKET_LIMIT)

        else:  # runs
            state["total_runs"] += runs
            state["partnership_runs"] = partnership_before + runs
            bs["runs"] += runs
            bws["runs"] += runs
            bws["this_over_runs"] += runs
            if runs == 4:
                bs["fours"] += 1
                streaks[srid] = {"boundaries": streak.get("boundaries", 0) + 1}
            elif runs == 6:
                bs["sixes"] += 1
                streaks[srid] = {"boundaries": streak.get("boundaries", 0) + 1}
            else:
                streaks[srid] = {"boundaries": 0}
            over_timeline.append(str(runs))
            over_events.append({"sym": str(runs), "text": _run_text(runs, striker_name, bowler["name"])})
            # Maiden = full over of dots with no runs off it yet.
            _maiden = (balls_this_over >= 6 and over_runs_before == 0 and runs == 0)
            _run_key = ("dot_ball" if runs == 0 else "four" if runs == 4
                        else "six" if runs == 6 else None)
            _push_commentary(state, _run_event(runs), striker_name,
                             _ec(is_maiden=_maiden)
                             or _run_text(runs, striker_name, bowler["name"]),
                             runs=runs, event_key=_run_key)
            if runs not in (4, 6):
                free_hit = False
            if runs % 2 == 1:
                _swap_strike(state)

        state["timeline"].append(over_timeline[-1] if over_timeline else "0")
        state["timeline"] = state["timeline"][-18:]
        ball_history.append(make_ball_event(oc))
        ball_history = ball_history[-BALL_HISTORY_WINDOW:]
        if target is not None and state["total_runs"] >= target:
            chased = True

    # ── End of over bookkeeping ──
    over_runs = state["total_runs"] - runs_before
    over_wkts = state["total_wickets"] - wkts_before
    if balls_this_over >= 6 and over_runs == 0:
        bws["maidens"] += 1
    bws["overs_done"] = bws["balls"] // 6
    bws["this_over_balls"] = 0
    state["over_runs"].append(over_runs)
    # Snapshot this over for the approach-prompt scorecard card.
    state["last_over_timeline"] = list(over_timeline)
    state["last_over_commentary"] = list(state.get("commentary_log", [])[cmt_start:])
    state["free_hit"] = free_hit
    state["ball_history"] = ball_history
    state["batter_streaks"] = streaks
    state["prev_bowler_rid"] = bowler["roster_id"]
    # Persist the scenario engine's mutable state (script, ball index, active
    # flag) so it survives the JSON round-trip to the next over.
    _save_scenario_engine(state, scenario_eng)

    momentum_after = _compute_momentum(ball_history)
    state["momentum_prev"] = momentum_after

    # Swap strike at end of over (unless innings is ending)
    over_completed = balls_this_over >= 6
    if over_completed and not is_innings_over(state):
        _swap_strike(state)

    summary = {
        "over_no": state["current_over"],
        "bowler": bowler,
        "batting_approach": bat_app,
        "bowling_approach": bowl_app,
        "over_runs": over_runs,
        "over_wickets": over_wkts,
        "over_timeline": over_timeline,
        "over_events": over_events,
        "momentum_shift": momentum_after - momentum_before,
        "bowler_figures": _bowler_figures(bws),
    }

    # Advance the over pointer if the over completed and play continues
    if over_completed and not is_innings_over(state):
        state["current_over"] += 1
        state["current_ball"] = 0
        state["batting_approach"] = None
        state["bowling_approach"] = None
        state["current_bowler"] = None
    return summary


def _swap_strike(state):
    state["striker_idx"], state["non_striker_idx"] = (
        state["non_striker_idx"], state["striker_idx"])


def _run_event(runs):
    return {0: "dot", 1: "one", 2: "two", 3: "three", 4: "four", 6: "six"}.get(runs, "run")


def _run_text(runs, batsman, bowler):
    if runs == 0:
        return f"Dot ball. {bowler} to {batsman}."
    if runs == 4:
        return f"FOUR! {batsman} finds the boundary."
    if runs == 6:
        return f"SIX! {batsman} goes big off {bowler}!"
    return f"{runs} run(s), {batsman}."


def _bowler_figures(bws):
    overs = f"{bws['balls'] // 6}.{bws['balls'] % 6}"
    return f"{overs}-{bws.get('maidens', 0)}-{bws['runs']}-{bws['wickets']}"


def _recent_wickets(state, window=12):
    """Wickets fallen within the last ``window`` legal balls (for the collapse
    narrative, which the engine triggers at >= 3)."""
    now = balls_bowled(state)
    marks = state.get("wkt_marks") or []
    return sum(1 for m in marks if m > now - window)


def _engine_text(state, oc, striker, bowler, over_idx, innings, target,
                 required_rr, bs_runs_before, partnership_before, recent_wkts,
                 over_runs_before, balls_this_over, overs_total, is_maiden=False):
    """Build the rich ball-by-ball line from the SimCricketX commentary engine
    (micro template + any macro narrative). Returns None on any failure so the
    caller can fall back to the built-in terse line."""
    if _COMMENTARY is None:
        return None
    try:
        otype = oc.get("type")
        is_wkt = bool(oc.get("batter_out") or otype == "wicket")
        ball_context = {
            "type": "wicket" if is_wkt else "run",
            "runs": oc.get("runs", 0),
            "is_extra": bool(oc.get("is_extra", False)),
            "extra_type": oc.get("extra_type", "") or "",
            "wicket_type": (oc.get("wicket_type") or "caught").lower().replace(" ", "_"),
            "batter": striker.get("name", "The batter"),
            "bowler": bowler.get("name", "The bowler"),
            "bowling_type": (bowler.get("bowl_style") or "").lower(),
            "batting_team": state.get("bat_team_name", "The batting side"),
            "bowling_team": state.get("bowl_team_name", "The fielding side"),
            "batter_out": is_wkt,
        }
        runs_needed = (max(0, int(target) - int(state["total_runs"]))
                       if target else 999)
        match_state = {
            "current_over": over_idx, "current_ball": balls_this_over,
            "innings": innings, "score": state["total_runs"],
            "wickets": state["total_wickets"],
            "batter_runs": bs_runs_before,
            "partnership_runs": partnership_before,
            "recent_wickets_match": recent_wkts,
            "required_run_rate": required_rr,
            "runs_needed": runs_needed,
            "current_over_runs": over_runs_before,
            "is_maiden_over": is_maiden,
            "_fmt_last_over": max(0, overs_total - 1),
            "_fmt_death_start": max(0, overs_total - 4),
        }
        text = (_COMMENTARY.get_commentary(ball_context, match_state) or "").strip()
        return text or None
    except Exception:
        logger.exception("cipl engine commentary failed")
        return None


def _push_commentary(state, ctype, name, text, *, runs=None,
                     is_wicket=False, event_key=None):
    """Append a commentary entry the Mini App feed can render.

    Ball rows (``ctype`` not one of the special card types) need ``runs`` /
    ``isWicket`` / ``eventKey`` so static/cricket/app.js can paint the outcome
    badge (run count, ``W`` for a wicket, ``Wd``/``Nb``/``Lb`` for extras)
    instead of defaulting to ``0``.
    """
    log = state.setdefault("commentary_log", [])
    entry = {"type": ctype, "name": name, "text": text,
             "over": format_overs(state), "score": format_score(state),
             "isWicket": bool(is_wicket)}
    if runs is not None:
        entry["runs"] = runs
    if event_key:
        entry["eventKey"] = event_key
    log.append(entry)
    # Keep the log bounded so the JSON state stays small.
    if len(log) > 240:
        del log[:len(log) - 240]


def _pick_fielder(state, bowler, allow_bowler=False):
    """Pick a plausible fielder name from the bowling XI for the scorecard
    dismissal line. The engine only emits a wicket *type*, not a fielder, so
    we attribute the catch/run-out to a random fieldsman."""
    xi = state.get("bowl_xi") or []
    bowler_rid = bowler.get("roster_id") if bowler else None
    pool = [p for p in xi
            if allow_bowler or p.get("roster_id") != bowler_rid]
    if not pool:
        pool = list(xi)
    if not pool:
        return ""
    return random.choice(pool).get("name", "")


def _dismissal_text(how_out, bowler_name, fielder=""):
    """Render a full cricket-scorecard dismissal line, e.g. ``c Kohli b Bumrah``,
    ``lbw b Shami``, ``b Boult``, ``run out (Jadeja)``."""
    how = (how_out or "").strip()
    low = how.lower()
    bwl = bowler_name or ""
    fld = fielder or ""
    if low in ("bowled", "b"):
        return f"b {bwl}".strip()
    if low == "lbw":
        return f"lbw b {bwl}".strip()
    if low in ("caught", "c"):
        if fld and bwl and fld == bwl:
            return f"c & b {bwl}".strip()
        if fld:
            return f"c {fld} b {bwl}".strip()
        return f"c b {bwl}".strip()
    if low in ("caught and bowled", "c&b", "caught & bowled"):
        return f"c & b {bwl}".strip()
    if low == "stumped":
        return f"st {fld} b {bwl}".strip() if fld else f"st b {bwl}".strip()
    if low in ("run out", "runout"):
        return f"run out ({fld})".strip() if fld else "run out"
    if low == "hit wicket":
        return f"hit wicket b {bwl}".strip()
    # Unknown type — show it as-is so nothing is lost.
    return how or "out"


# ════════════════════════════════════════════════════════════════════
# Innings / match transitions
# ════════════════════════════════════════════════════════════════════

def end_first_innings(state):
    """Archive innings 1, set the target and swap batting/bowling sides."""
    state["inn1_runs"] = state["total_runs"]
    state["inn1_wickets"] = state["total_wickets"]
    state["inn1_overs"] = format_overs(state)
    state["inn1_bat_team"] = state["bat_team_name"]
    state["inn1_bowl_team"] = state["bowl_team_name"]
    # Alias used by the Mini App scorecard (services.match_webapp_service.build_scorecard)
    # to label the 1st-innings batting side while the 2nd innings is in progress.
    state["inn1_team"] = state["bat_team_name"]
    state["inn1_bat_team_id"] = state["bat_team_id"]
    state["inn1_bowl_team_id"] = state["bowl_team_id"]
    state["inn1_bat_stats"] = state["bat_stats"]
    state["inn1_bowl_stats"] = state["bowl_stats"]
    state["inn1_bat_xi"] = state["bat_xi"]
    state["inn1_bowl_xi"] = state["bowl_xi"]
    state["inn1_fow"] = state["fow"]
    state["inn1_timeline"] = state["timeline"]
    state["inn1_over_runs"] = list(state.get("over_runs") or [])
    state["target"] = state["total_runs"] + 1

    # Record the unbroken closing partnership, then freeze the 1st-innings
    # partnership list for the Mini App "Partnership" tab.
    if state.get("partnership_balls") or state.get("partnership_runs"):
        order = state.get("batting_order", []) or []
        s_idx, ns_idx = state.get("striker_idx", 0), state.get("non_striker_idx", 1)
        b1 = order[s_idx].get("name", "") if s_idx < len(order) else ""
        b2 = order[ns_idx].get("name", "") if ns_idx < len(order) else ""
        state.setdefault("partnership_history", []).append({
            "wicket": state["total_wickets"] + 1,
            "batsman1": b1, "batsman2": b2,
            "runs": state.get("partnership_runs", 0),
            "balls": state.get("partnership_balls", 0),
            "notout": True,
        })
    state["inn1_partnership_history"] = list(state.get("partnership_history") or [])
    state["partnership_history"] = []

    # Swap sides for innings 2
    state["bat_team_id"], state["bowl_team_id"] = state["bowl_team_id"], state["bat_team_id"]
    state["bat_user_tg"], state["bowl_user_tg"] = state["bowl_user_tg"], state["bat_user_tg"]
    state["bat_team_name"], state["bowl_team_name"] = state["bowl_team_name"], state["bat_team_name"]
    state["bat_team_code"], state["bowl_team_code"] = (
        state.get("bowl_team_code", ""), state.get("bat_team_code", ""))
    state["bat_team_emoji"], state["bowl_team_emoji"] = (
        state.get("bowl_team_emoji", "🏏"), state.get("bat_team_emoji", "🏏"))
    state["bat_xi"], state["bowl_xi"] = state["bowl_xi"], state["bat_xi"]
    state["batting_order"] = list(state["bat_xi"])

    state["bat_stats"] = {str(p["roster_id"]): _new_bat_stat() for p in state["bat_xi"]}
    state["bowl_stats"] = {str(p["roster_id"]): _new_bowl_stat() for p in state["bowl_xi"]}

    state["innings"] = 2
    state["current_over"] = 1
    state["current_ball"] = 0
    state["total_runs"] = 0
    state["total_wickets"] = 0
    state["extras_total"] = 0
    state["striker_idx"], state["non_striker_idx"], state["next_batsman_idx"] = 0, 1, 2
    state["current_bowler"] = None
    state["prev_bowler_rid"] = None
    state["batting_approach"] = None
    state["bowling_approach"] = None
    state["timeline"] = []
    state["over_runs"] = []
    state["fow"] = []
    state["ball_history"] = []
    state["batter_streaks"] = {}
    state["free_hit"] = False
    state["partnership_runs"] = 0
    state["partnership_balls"] = 0
    state["wkt_marks"] = []
    state["momentum_prev"] = 0.0
    # Drop the 1st-innings over snapshot so innings 2 starts with a clean card.
    state["last_over_timeline"] = []
    state["last_over_commentary"] = []

    # Optionally arm a dramatic-finish scenario for the chase now that the
    # target is known (20-over matches only; self-disables if unrealistic).
    _maybe_enable_scenario(state)


def compute_result(state):
    """Return a result dict for a finished match (call after innings 2)."""
    inn1 = state.get("inn1_runs", 0)
    inn2 = state.get("total_runs", 0)
    target = state.get("target") or (inn1 + 1)
    # Side that batted second is the current bat side.
    second_batting = state["bat_team_name"]
    first_batting = state.get("inn1_bat_team", state["bowl_team_name"])
    if inn2 >= target:
        wickets_in_hand = state.get("wicket_limit", WICKET_LIMIT) - state["total_wickets"]
        return {"winner": second_batting, "loser": first_batting,
                "margin_type": "wickets", "margin": max(0, wickets_in_hand),
                "tie": False}
    if inn2 == inn1:
        return {"winner": None, "loser": None, "margin_type": "tie",
                "margin": 0, "tie": True}
    return {"winner": first_batting, "loser": second_batting,
            "margin_type": "runs", "margin": inn1 - inn2, "tie": False}
