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
from engine import chase_chance
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

# ── Match-format spec ────────────────────────────────────────────────
# Both supported formats are an innings of **20 units**, one captain turn per
# unit, with a strike end-change at unit boundaries. T20 = 20 overs × 6 balls
# (120). The Hundred = 20 sets × 5 balls (100), where the strike changes ends
# only every 2nd set (10 balls) and a bowler may bowl two consecutive sets
# (a 10-ball spell) but not a third. ``state["overs"]`` stays 20 (the unit
# count) for both; only the per-unit ball count and rotation rules differ.
FORMAT_SPECS = {
    "T20": {
        "balls_per_unit": 6, "swap_balls": 6, "powerplay_units": 6,
        "max_consecutive_units": 1, "unit_word": "over", "label": "20 Overs",
    },
    "The100": {
        "balls_per_unit": 5, "swap_balls": 10, "powerplay_units": 5,
        "max_consecutive_units": 2, "unit_word": "set", "label": "The Hundred",
    },
}


def _spec(state):
    """Return the FORMAT_SPECS entry for a state (defaults to the T20/over rules)."""
    return FORMAT_SPECS.get((state or {}).get("ball_format"), FORMAT_SPECS["T20"])


def balls_per_unit(state):
    """Legal balls in one captain turn (6 for an over, 5 for a Hundred set)."""
    return _spec(state)["balls_per_unit"]


def total_balls(state):
    """Legal balls in a full innings (overs × balls-per-unit) — 120 or 100."""
    return int(state.get("overs", 20)) * balls_per_unit(state)


def is_hundred(state):
    """True when the state is a The-Hundred (100-ball) match."""
    return (state or {}).get("ball_format") == "The100"


# Chase-chance steering: nudge ball outcomes toward the matrix-estimated
# chasing/defending chance in the back end of a chase (the matrix is an
# end-of-innings model — apply it over roughly the last 8 overs, "until the
# 18th over"). STRENGTH scales how hard the matrix nudge pulls (0 = off).
#
# Two layers, applied during the 2nd innings only:
#   1. A mild always-on chasing assist (the real "knows the target / dew" edge)
#      that balances batting-first vs chasing toward ~50-50 overall.
#   2. The matrix steer on top, in the back overs, for situational realism
#      (e.g. 30 needed with ≤5 down stays a genuine ~56% chase).
CHASE_STEER_BALLS = 30
CHASE_STEER_STRENGTH = 0.30
CHASE_BASELINE_ASSIST = {"boundary_modifier": 1.065, "wicket_modifier": 0.905,
                         "dot_bonus": -0.015}


def _merge_pressure(pressure_effects, bias):
    """Fold a steering bias into a pressure_effects dict: dot_bonus is additive,
    everything else multiplies onto any existing value."""
    for key, value in bias.items():
        if key == "dot_bonus":
            pressure_effects[key] = pressure_effects.get(key, 0.0) + value
        elif key in pressure_effects:
            pressure_effects[key] *= value
        else:
            pressure_effects[key] = value

# Dramatic-finish scenario types the engine can steer a 2nd-innings chase toward.
# ``controlled_finish`` completes the chase at a chosen ball (see
# _select_finish_profile) so finishes are spread out instead of always landing on
# the last ball.
SCENARIO_TYPES = ("last_ball_six", "win_by_1_run", "super_over_thriller",
                  "controlled_finish")


def _select_finish_profile(overs):
    """Pick a finish profile for an armed chase, returning ``(scenario_type,
    finish_ball)``. The weighting spreads finishes out to reduce last-ball drama:

        30%  win with 1 ball to spare (19.5)
        20%  last-ball win            (19.6)
        10%  tie → Super Over
        20%  finish in the 19th over  (18.1–18.6)
        20%  comfortable win          (17.1–17.6, 2+ overs to spare)

    ``finish_ball`` is an absolute legal-ball index (1..overs*6); ``None`` for the
    tie profile, which is handled by the super_over_thriller script.
    """
    balls = overs * 6
    r = random.random()
    if r < 0.30:
        return "controlled_finish", balls - 1
    if r < 0.50:
        return "controlled_finish", balls
    if r < 0.60:
        return "super_over_thriller", None
    if r < 0.80:
        return "controlled_finish", random.randint(balls - 11, balls - 6)
    return "controlled_finish", random.randint(balls - 17, balls - 12)

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
    stype, finish_ball = _select_finish_profile(int(state.get("overs", 20)))
    state["scenario"] = {
        "type": stype,
        "active": True,
        "finish_ball": finish_ball,
        "finale_script": None,
        "finale_ball_index": 0,
        "convergence_logged": False,
        "endgame_checked_overs": [],
    }
    logger.info("[CIPL Scenario] Armed finish '%s' (finish_ball=%s) for match %s (target=%s)",
                stype, finish_ball, state.get("match_id"), state.get("target"))


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
        eng = ScenarioEngine(sc["type"], _ScenarioMatchShim(state),
                              finish_ball=sc.get("finish_ball"))
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
        "finish_ball": getattr(eng, "finish_ball", None),
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
                     bat_team_emoji="🏏", bowl_team_emoji="🏏", conditions=None,
                     ball_format="T20"):
    """Build the initial state dict for a Challenge League approach match."""
    bat_stats = {str(p["roster_id"]): _new_bat_stat() for p in bat_xi}
    bowl_stats = {str(p["roster_id"]): _new_bowl_stat() for p in bowl_xi}
    return {
        "mode": "cipl_approach",
        "match_id": match_id, "overs": overs,
        # Match format: "T20" (20 overs × 6 balls) or "The100" (20 sets × 5).
        "ball_format": ball_format if ball_format in FORMAT_SPECS else "T20",
        # Bowling-spell tracker for The Hundred's 5/10-ball consecutive rule.
        "spell_rid": None, "spell_units": 0,
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
        # Dynamic match conditions from the Pitch Report (None for non-league
        # callers) — drives the environmental weight hook in the ball loop.
        "conditions": conditions,
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
    """Per-bowler unit quota — ceil(units / 5), as in standard limited-overs.

    Both formats are 20 units, so this is 4 units for either — i.e. 24 balls in
    T20 (4 overs) and 20 balls in The Hundred (4 sets), exactly the real caps.
    """
    return max(1, -(-state["overs"] // 5))


def _overs_bowled(state, rid):
    """Units (overs / sets) a bowler has completed so far."""
    return state["bowl_stats"].get(str(rid), {}).get("balls", 0) // balls_per_unit(state)


# Emergency part-time bowlers (batsmen) are capped well below the regular quota
# so a mismanaged attack can't simply farm out the innings to a batsman.
PART_TIME_MAX_OVERS = 2


def is_part_time_bowler(p):
    """A batsman/keeper offered as an *emergency* bowler — i.e. not a specialist
    Bowler or All-rounder. Used to annotate the picker so the captain knows the
    front-line attack is exhausted."""
    return p.get("category") not in ("Bowler", "All-rounder")


def quota_for(state, p):
    """Per-bowler over cap: the regular quota for specialists, but emergency
    part-timers are capped at PART_TIME_MAX_OVERS (and never more than a
    specialist could bowl)."""
    base = max_bowler_overs(state)
    if is_part_time_bowler(p):
        return min(base, PART_TIME_MAX_OVERS)
    return base


def overs_left(state, p):
    """Overs this player may still bowl under their (part-time-aware) quota."""
    return max(0, quota_for(state, p) - _overs_bowled(state, p["roster_id"]))


def eligible_bowlers(state):
    """Bowlers a captain may pick for the upcoming over, in preference order.

    Tiered so an over can ALWAYS be bowled — even when the front-line attack has
    been mismanaged (e.g. only five bowlers and all their overs used up before
    the last over):

      1. Specialists (Bowler / All-rounder) under quota, not the previous bowler.
      2. Emergency part-timers (batsmen) under quota, not the previous bowler —
         this is the "someone has to bowl the 20th over" fallback.
      3. Last resort: anyone left under quota (ignoring the no-back-to-back rule),
         then anyone at all, so the match never deadlocks.

    Within each tier, highest bowl_rating first.
    """
    xi = state.get("bowl_xi") or []
    # Which bowler is blocked from taking the next unit. In T20 it is simply the
    # previous over's bowler (no back-to-back overs). In The Hundred a bowler may
    # bowl two consecutive sets (a 10-ball spell), so only block them once that
    # spell hits the format's consecutive-unit cap.
    if is_hundred(state):
        spell_rid = state.get("spell_rid")
        spell_units = int(state.get("spell_units", 0) or 0)
        blocked = spell_rid if spell_units >= _spec(state)["max_consecutive_units"] else None
    else:
        blocked = state.get("prev_bowler_rid")

    def _avail(pool, enforce_quota=True, enforce_prev=True):
        out = list(pool)
        if enforce_quota:
            out = [p for p in out
                   if _overs_bowled(state, p["roster_id"]) < quota_for(state, p)]
        if enforce_prev and blocked is not None:
            out = [p for p in out if p["roster_id"] != blocked]
        return out

    def _sorted(pool):
        return sorted(pool, key=lambda p: p.get("bowl_rating", 0), reverse=True)

    specialists = [p for p in xi if p.get("category") in ("Bowler", "All-rounder")]
    part_timers = [p for p in xi if is_part_time_bowler(p)]

    # 1) Front-line attack with overs left.
    pool = _avail(specialists)
    if pool:
        return _sorted(pool)

    # 2) Specialists are bowled out (or only the previous bowler remains) — throw
    #    the ball to a part-time batsman so the over can still be bowled.
    pool = _avail(part_timers)
    if pool:
        return _sorted(pool)

    # 3) Everyone is at quota / only the previous bowler is left: relax the
    #    back-to-back rule, then the quota, so play can always continue.
    pool = (_avail(xi, enforce_prev=False)
            or _avail(xi, enforce_quota=False, enforce_prev=False)
            or list(xi))
    return _sorted(pool)


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


def _apply_trait_weights(raw_weights, striker_traits, bowler_traits, ctx,
                         collector=None):
    """Nudge the engine's raw outcome weights by the active player traits.

    services.trait_engine.apply_traits works on a ~0-100 percentage-point scale
    (the scale of the legacy probability engine), so we rescale the engine's
    normalised weights to sum to 100, let apply_traits add its deltas to the
    Six / Four / Wicket / Dot buckets, then rescale back. The engine renormalises
    afterwards. Capping / stacking / level logic is reused verbatim, so /letsplay
    traits behave exactly like /cm and /wpm traits.

    When ``collector`` (a ``{"bat": set, "bowl": set}`` dict) is supplied, the
    traits that actually fired this ball are recorded — tagged by role and
    prefixed with their emoji — so the over summary can reveal them.
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
        activated = apply_traits(probs, striker_traits or [], bowler_traits or [], ctx)
    except Exception:
        logger.exception("letsplay trait application failed; ignoring this ball")
        return raw_weights
    new = dict(raw_weights)
    for tkey, ekey in _TRAIT_KEY_MAP:
        new[ekey] = max(0.0, probs.get(tkey, 0.0)) / scale
    if collector is not None and activated:
        _record_activated_traits(collector, activated, striker_traits, bowler_traits)
    return new


def _trait_label(t):
    name = t.get("display_name") or t.get("effect_key") or "Trait"
    return f"{name} Lv.{t.get('level', 1)}"


def _record_activated_traits(collector, activated, striker_traits, bowler_traits):
    """Classify the apply_traits 'Name Lv.X' strings into bat/bowl, with emoji."""
    st = {_trait_label(t): t for t in (striker_traits or [])}
    bw = {_trait_label(t): t for t in (bowler_traits or [])}
    for label in activated:
        if label in st:
            t = st[label]
            collector["bat"].add(f"{t.get('emoji', '✨')} {label}")
        elif label in bw:
            t = bw[label]
            collector["bowl"].add(f"{t.get('emoji', '✨')} {label}")
        else:
            collector["bowl"].add(f"✨ {label}")


def _make_trait_hook(striker_traits, bowler_traits, ctx, collector=None):
    """Return a one-arg weight hook (raw_weights -> raw_weights) bound to this
    delivery's traits + context, or None when neither side has any trait."""
    if not striker_traits and not bowler_traits:
        return None
    return lambda raw_weights: _apply_trait_weights(
        raw_weights, striker_traits, bowler_traits, ctx, collector)


def _make_environment_hook(conditions, ctx):
    """Per-ball weight hook for the dynamic conditions (dew/weather/overs).

    Thin wrapper over services.pitch_report so a missing module never crashes a
    delivery — returns None (no-op) on any failure or when there are no
    conditions to apply."""
    if not conditions:
        return None
    try:
        from services.pitch_report import make_environment_hook
        return make_environment_hook(conditions, ctx)
    except Exception:
        logger.exception("environment hook build failed; ignoring conditions")
        return None


# Progressive throttle on the Wicket weight once wickets have already fallen in
# the CURRENT over. The momentum/collapse/consecutive-wicket layers in the
# game-state engine are recomputed after every ball, so each fresh wicket pushes
# the next ball's wicket chance up — left unchecked they stack toward the GSME
# clamp and produce unrealistic 4-5 wicket overs. Real T20 overs almost never
# yield more than two wickets; three is hat-trick-grade, four-plus effectively
# never. These scales keep the occasional double (and rare triple) without the
# cascade. Indexed by wickets already taken THIS over; 3+ falls back to the last.
_WICKET_OVER_THROTTLE = {1: 0.50, 2: 0.22, 3: 0.08}


def _make_wicket_cluster_hook(wkts_this_over):
    """Damp the per-ball Wicket weight after the first wicket of the over, so a
    single over can't realistically produce the 4-5 wicket cascades the raw
    momentum/collapse layers can otherwise stack up. Returns None (no-op) before
    the over's first wicket, so a normal delivery is completely unaffected."""
    if wkts_this_over <= 0:
        return None
    scale = _WICKET_OVER_THROTTLE.get(wkts_this_over, 0.08)

    def _hook(raw_weights):
        if "Wicket" not in raw_weights:
            return raw_weights
        rw = dict(raw_weights)
        rw["Wicket"] *= scale
        return rw

    return _hook


def _make_last_over_drama_hook(is_last_over, is_live_chase):
    """Mild final-over uplift so the last over of an innings swings harder — more
    big hits and late wickets, the tension of a finish coming down to the wire.
    A touch stronger during a live chase. Returns None (no-op) for every over
    except the innings' final over, so earlier overs are unchanged."""
    if not is_last_over:
        return None
    six = 1.18 if is_live_chase else 1.12
    four = 1.12 if is_live_chase else 1.08
    wkt = 1.15 if is_live_chase else 1.10
    dot = 0.90

    def _hook(raw_weights):
        rw = dict(raw_weights)
        if "Six" in rw:
            rw["Six"] *= six
        if "Four" in rw:
            rw["Four"] *= four
        if "Wicket" in rw:
            rw["Wicket"] *= wkt
        if "Dot" in rw:
            rw["Dot"] *= dot
        return rw

    return _hook


def _compose_hooks(*hooks):
    """Combine weight hooks into one, applied left to right. Drops Nones and
    returns the single hook (or None) when zero/one remain."""
    real = [h for h in hooks if h is not None]
    if not real:
        return None
    if len(real) == 1:
        return real[0]

    def _composed(raw_weights):
        w = raw_weights
        for h in real:
            out = h(w)
            if out:
                w = out
        return w

    return _composed


# ════════════════════════════════════════════════════════════════════
# Score / chase helpers
# ════════════════════════════════════════════════════════════════════

def chase_chance_now(state):
    """Live chasing-chance estimate for the current 2nd-innings situation (matrix
    + player/pitch/momentum modifiers), or None when not a live chase. Used for
    steering and safe to surface read-only — it never decides the result."""
    if state.get("innings") != 2 or not state.get("target"):
        return None
    balls_left = state["overs"] * 6 - balls_bowled(state)
    runs_needed = int(state["target"]) - int(state["total_runs"])
    if balls_left <= 0 or runs_needed <= 0:
        return None
    order = state.get("batting_order", []) or []
    s_idx, ns_idx = state.get("striker_idx", 0), state.get("non_striker_idx", 1)
    striker = order[s_idx] if s_idx < len(order) else {}
    non_striker = order[ns_idx] if ns_idx < len(order) else {}
    bowler = state.get("current_bowler") or {}
    s_runs = state.get("bat_stats", {}).get(str(striker.get("roster_id")), {}).get("runs", 0)
    required_rr = runs_needed / balls_left * 6.0
    recent = (state.get("ball_history") or [])[-6:]
    recent_runs = sum(int(b.get("runs", 0) or 0) for b in recent)
    info = chase_chance.final_chase_chance(
        runs_needed, int(state["total_wickets"]),
        batter_mod=chase_chance.batter_modifier(striker, non_striker, s_runs),
        bowler_mod=chase_chance.bowler_modifier(bowler, is_emergency=is_part_time_bowler(bowler)),
        pitch_mod=chase_chance.pitch_modifier(state.get("pitch_type")),
        momentum_mod=chase_chance.momentum_modifier(required_rr, recent_runs, len(recent)),
    )
    # The matrix is keyed only on runs+wickets; fold in the balls actually left so
    # an out-of-reach ask (e.g. 16 off 1) correctly favours the defence.
    return chase_chance.apply_feasibility(info, runs_needed, balls_left)


def balls_bowled(state):
    bpu = balls_per_unit(state)
    return (state["current_over"] - 1) * bpu + state["current_ball"]


def format_overs(state):
    b = balls_bowled(state)
    if is_hundred(state):
        # The Hundred has no overs — progress is shown as a ball count.
        return f"{b} balls"
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
    balls_left = max(0, total_balls(state) - balls_bowled(state))
    rrr = (runs_req / balls_left * 6.0) if balls_left > 0 else 0.0
    return {"target": target, "runs_required": runs_req,
            "balls_remaining": balls_left, "rrr": rrr}


def is_innings_over(state):
    c = chase(state)
    if c and c["runs_required"] == 0:
        return True
    if state["total_wickets"] >= state.get("wicket_limit", WICKET_LIMIT):
        return True
    if balls_bowled(state) >= total_balls(state):
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
    # ── Format ──────────────────────────────────────────────────────
    bpu = balls_per_unit(state)            # 6 (over) or 5 (Hundred set)
    innings_balls = total_balls(state)     # 120 or 100
    hundred = is_hundred(state)
    swap_balls = _spec(state)["swap_balls"]  # change ends every N legal balls
    # The SimCricketX engine is natively over-based (6 balls/over). For The
    # Hundred we map the 100-ball innings onto ~17 six-ball overs so the
    # pressure / momentum / par-score curves operate over the right ball count.
    # The per-ball engine calls below feed it a divmod-by-6 view of the absolute
    # ball count, which is identical to the over view for T20.
    if hundred:
        engine_overs = max(1, round(innings_balls / 6))  # 100 → 17
        engine_fmt = _fmt_to_engine_fmt(
            {"label": "The100", "overs": engine_overs, "max_bowler_overs": 4,
             "powerplay_end": 4, "death_start": max(2, engine_overs - 3)}, overs_total)
    else:
        engine_overs = overs_total
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
    # Traits that fire this over (populated by the per-ball trait hook). Stays
    # empty for Challenge League players, who carry no traits.
    over_traits = {"bat": set(), "bowl": set()}

    # Commentary: announce the bowler taking the new over (into attack / returns).
    _emit_bowler_card(state, bowler)

    while balls_this_over < bpu and not chased:
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
        # Is THIS delivery a free hit (set by a no-ball earlier)? Captured before
        # the flag is consumed so the ball's commentary can carry the marker.
        is_free_hit_ball = bool(free_hit)
        fh_prefix = "🆓 FREE HIT — " if is_free_hit_ball else ""

        # Pressure. Use the actual ball count for the required rate, and feed the
        # engine its native 6-ball-over view of the innings (divmod by 6).
        balls_left = innings_balls - balls_bowled(state)
        eng_over_idx, eng_ball = divmod(balls_bowled(state), 6)
        required_rr = 0.0
        if target is not None and balls_left > 0:
            required_rr = max(0, target - state["total_runs"]) / balls_left * 6.0
        match_state = {
            "innings": innings, "current_over": eng_over_idx,
            "score": state["total_runs"], "wickets": state["total_wickets"],
            "required_run_rate": required_rr,
            "overs_remaining": engine_overs - eng_over_idx,
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
            score=state["total_runs"], current_over=eng_over_idx,
            current_ball=eng_ball, wickets=state["total_wickets"],
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
                _merge_pressure(pressure_effects, scenario_eng.get_scenario_bias({}))

            # Chase-chance steering — a controlled nudge toward the matrix-estimated
            # chasing chance. Skipped while a dramatic-finish scenario is actively
            # steering (those matches are curated by the ScenarioEngine), so the
            # two systems never fight.
            if (scenario_eng is None or scenario_phase == "inactive") \
                    and innings == 2 and target:
                # Layer 1: mild always-on chasing assist → ~50-50 baseline.
                _merge_pressure(pressure_effects, CHASE_BASELINE_ASSIST)
                # Layer 2: matrix steer in the back overs (situational realism).
                _balls_left_now = innings_balls - balls_bowled(state)
                if 0 < _balls_left_now <= CHASE_STEER_BALLS:
                    _cc = chase_chance_now(state)
                    if _cc:
                        _merge_pressure(pressure_effects,
                                        chase_chance.chase_steer_effects(
                                            _cc["chasing_chance"],
                                            strength=CHASE_STEER_STRENGTH))

            pitch_wear = min(1.0, balls_bowled(state) / max(1, innings_balls))
            # /letsplay traits: build a per-ball weight hook from the striker's
            # and bowler's active traits (None for Challenge League players, who
            # carry no traits — so the engine call is unchanged for /cipl).
            trait_hook = _make_trait_hook(
                striker.get("traits"), bowler.get("traits"),
                {"over": state["current_over"], "total_overs": overs_total,
                 "rrr": required_rr, "bat_balls_faced": bs["balls"]},
                collector=over_traits)
            # Dynamic conditions hook (dew/weather/overs progression) for
            # Challenge League matches. None for callers without conditions, and
            # composed after traits so it layers on the final weights.
            env_hook = _make_environment_hook(
                state.get("conditions"),
                {"over": state["current_over"], "total_overs": overs_total,
                 "innings": innings})
            # Anti-cascade: throttle the wicket weight once wickets have already
            # fallen in THIS over, so the recomputed momentum/collapse layers
            # can't stack into a 4-5 wicket over (see _make_wicket_cluster_hook).
            wicket_hook = _make_wicket_cluster_hook(
                state["total_wickets"] - wkts_before)
            # A little extra drama in the innings' final over (more boundaries /
            # late wickets), nudged up further when a live chase is on.
            drama_hook = _make_last_over_drama_hook(
                state["current_over"] >= overs_total,
                bool(target) and not chased)
            weight_hook = _compose_hooks(trait_hook, env_hook,
                                         wicket_hook, drama_hook)
            oc = _normalize_outcome(calculate_outcome(
                batter=batter_adapted, bowler=bowl_adapted, pitch=pitch,
                streak=streak, over_number=over_idx, batter_runs=bs["runs"],
                innings=innings, pressure_effects=pressure_effects,
                allow_extras=True, free_hit=free_hit, balls_faced=bs["balls"],
                game_state=game_state, pitch_wear=pitch_wear,
                batting_position=state["striker_idx"] + 1,
                format_config=engine_fmt,
                batting_approach=bat_app, bowling_approach=bowl_app,
                weight_hook=weight_hook))

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
                over_events.append({"sym": "NB", "text": f"No ball +{runs} — FREE HIT next"})
                _nb_text = (_ec() or f"No ball! {bowler['name']} oversteps.")
                _push_commentary(state, "extra", striker_name,
                                 f"{_nb_text} 🆓 FREE HIT next ball — only a run out "
                                 f"can dismiss.",
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
                             fh_prefix + (_ec() or f"Leg byes, {runs} run(s)."),
                             runs=runs, event_key="legbye")
            free_hit = False  # legal delivery consumes the free hit
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
            # On a free hit this can only be a run out (engine-enforced).
            _wkt_line = fh_prefix + (_ec() or f"OUT! {striker_name} {bs['dismissal']}.")
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
                # Commentary: announce the incoming batsman (unless the innings
                # is ending on this wicket — no one walks out then).
                if not is_innings_over(state):
                    _emit_new_batsman_card(
                        state, state["batting_order"][state["striker_idx"]])
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
            # Maiden = full over/set of dots with no runs off it yet.
            _maiden = (balls_this_over >= bpu and over_runs_before == 0 and runs == 0)
            _run_key = ("dot_ball" if runs == 0 else "four" if runs == 4
                        else "six" if runs == 6 else None)
            _push_commentary(state, _run_event(runs), striker_name,
                             fh_prefix + (_ec(is_maiden=_maiden)
                                          or _run_text(runs, striker_name, bowler["name"])),
                             runs=runs, event_key=_run_key)
            # A free hit is consumed by this one legal delivery — clear it even if
            # the batter found the boundary (otherwise the free-hit run-out lock
            # would wrongly carry on to the next ball).
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
    if balls_this_over >= bpu and over_runs == 0:
        bws["maidens"] += 1
    bws["overs_done"] = bws["balls"] // bpu
    bws["this_over_balls"] = 0
    state["over_runs"].append(over_runs)
    # Snapshot this over for the approach-prompt scorecard card.
    state["last_over_timeline"] = list(over_timeline)
    state["last_over_commentary"] = list(state.get("commentary_log", [])[cmt_start:])
    state["free_hit"] = free_hit
    state["ball_history"] = ball_history
    state["batter_streaks"] = streaks
    state["prev_bowler_rid"] = bowler["roster_id"]
    over_completed = balls_this_over >= bpu
    # Track the current bowling spell for The Hundred's 5/10-ball rule: a bowler
    # may bowl up to two consecutive sets, then must hand the ball over. (Unused
    # for T20, where prev_bowler_rid already enforces no back-to-back overs.)
    if over_completed:
        if state.get("spell_rid") == bowler["roster_id"]:
            state["spell_units"] = int(state.get("spell_units", 0) or 0) + 1
        else:
            state["spell_rid"] = bowler["roster_id"]
            state["spell_units"] = 1
    # Persist the scenario engine's mutable state (script, ball index, active
    # flag) so it survives the JSON round-trip to the next over.
    _save_scenario_engine(state, scenario_eng)

    momentum_after = _compute_momentum(ball_history)
    state["momentum_prev"] = momentum_after

    # Change ends only at a format end-change boundary: every over (6 balls) in
    # T20, every 2nd set (10 balls) in The Hundred — so the batters keep their
    # ends after the first five-ball set.
    if over_completed and not is_innings_over(state) and balls_bowled(state) % swap_balls == 0:
        _swap_strike(state)

    # Mini App commentary: post an end-of-over summary card when the over is
    # complete (chronological feed only — the chat gets its own summary message).
    if over_completed:
        _emit_end_of_over_card(state, bowler, state["current_over"], over_runs)

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
        "bowler_figures": _bowler_figures(bws, bpu),
        "traits_activated": {"bat": sorted(over_traits["bat"]),
                             "bowl": sorted(over_traits["bowl"])},
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


def _bowler_figures(bws, bpu=6):
    balls = bws.get("balls", 0)
    if bpu != 6:  # The Hundred — figures are shown in balls, not overs.
        return f"{balls}b-{bws['runs']}-{bws['wickets']}"
    overs = f"{balls // 6}.{balls % 6}"
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


def _push_card(state, entry):
    """Append a rich Mini App commentary *card* (new_bowler / new_batsman /
    end_of_over / over_complete). Always carries ``over``/``text`` so the
    snapshot consumers (and the over-by-over chat block) can rely on them.
    """
    entry.setdefault("over", format_overs(state))
    entry.setdefault("text", "")
    log = state.setdefault("commentary_log", [])
    log.append(entry)
    if len(log) > 240:
        del log[:len(log) - 240]


def _bat_card_for(state, player):
    """``{name, runs, balls}`` snapshot used by the end-of-over summary card."""
    bs = state["bat_stats"].get(str(player["roster_id"]), {})
    return {"name": player["name"],
            "runs": bs.get("runs", 0), "balls": bs.get("balls", 0)}


def _emit_bowler_card(state, bowler):
    """Blue 'into the attack' (first spell) or 'returns to bowl' (subsequent
    spell) card, posted to the commentary feed at the start of every over."""
    bws = state["bowl_stats"].get(str(bowler["roster_id"]), {})
    if bws.get("balls", 0) > 0:
        _push_card(state, {
            "type": "returning_bowler", "name": bowler["name"],
            "text": f"{bowler['name']} returns to bowl "
                    f"({_bowler_figures(bws, balls_per_unit(state))})."})
    else:
        style = bowler.get("bowl_style") or ""
        suffix = f" — {style}" if style else ""
        _push_card(state, {
            "type": "new_bowler", "name": bowler["name"],
            "text": f"{bowler['name']} comes into the attack{suffix}."})


def _emit_new_batsman_card(state, player):
    """Green 'comes to the crease' card when a new batsman is promoted."""
    _push_card(state, {
        "type": "new_batsman", "name": player["name"],
        "text": f"{player['name']} comes to the crease."})


def _emit_end_of_over_card(state, bowler, over_no, over_runs):
    """Cricbuzz-style end-of-over summary card (+ a one-line bowler figure)."""
    bpu = balls_per_unit(state)
    unit = _spec(state)["unit_word"]  # "over" or "set"
    bws = state["bowl_stats"].get(str(bowler["roster_id"]), {})
    balls = bws.get("balls", 0)
    overs_str = (f"{balls}b" if bpu != 6
                 else (f"{balls // 6}.{balls % 6}" if balls % 6 else str(balls // 6)))
    striker = state["batting_order"][state["striker_idx"]]
    non_striker = state["batting_order"][state["non_striker_idx"]]
    _push_card(state, {
        "type": "end_of_over",
        "text": f"End of {unit} {over_no}: {over_runs} run(s), "
                f"{state['total_runs']}/{state['total_wickets']}.",
        "overNumber": over_no,
        "runsScored": over_runs,
        "totalRuns": state["total_runs"],
        "totalWickets": state["total_wickets"],
        "striker": _bat_card_for(state, striker),
        "nonStriker": _bat_card_for(state, non_striker),
        "bowler": {"name": bowler["name"], "wickets": bws.get("wickets", 0),
                   "runsConceded": bws.get("runs", 0), "overs": overs_str},
    })
    _push_card(state, {
        "type": "over_complete", "name": bowler["name"],
        "text": f"{bowler['name']} completes the {unit} ({_bowler_figures(bws, bpu)})."})


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
    # Reset the bowling-spell tracker for the new innings (The Hundred rule).
    state["spell_rid"] = None
    state["spell_units"] = 0
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
