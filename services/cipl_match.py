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
from engine import ground_config
from engine.pressure_engine import PressureEngine
from engine.game_state_engine import (
    make_ball_event,
    compute_game_state_vector,
    _compute_momentum,
    BALL_HISTORY_WINDOW,
)
from engine.approach_modifiers import batting_label, bowling_label
from services.match_engine import note_bowler_ball
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
# unit, with a strike end-change at unit boundaries. T20 = 20 overs x 6 balls
# (120). The Hundred = 20 sets x 5 balls (100), where the strike changes ends
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
    """Legal balls in a full innings (overs x balls-per-unit) -- 120 or 100."""
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
#   1. The spec total-band chase baseline (see _chase_baseline_effects, applied
#      in simulate_over) that tilts the whole chase toward the pitch's Chase Win%.
#   2. The matrix steer on top, in the back overs, for situational realism
#      (e.g. 30 needed with ≤5 down stays a genuine ~56% chase).
CHASE_STEER_BALLS = 30
CHASE_STEER_STRENGTH = 0.30

# LetsPlay "clutch finale": how many legal balls before the innings end the
# rating/trait-aware death resolution kicks in (in addition to any scenario-engine
# finale phase). One over by default.
CLUTCH_WINDOW_BALLS = 6


# ══════════════════════════════════════════════════════════════════════
# PITCH RULE ENGINE — spec-driven realism layers
# ══════════════════════════════════════════════════════════════════════
# Three global rules from the Pitch Rule Engine spec, each implemented as a
# BOUNDED weight nudge layered on top of the existing rating/trait weights
# (never an override — ratings and traits still decide the raw distribution):
#
#   Sub-100 Rule   — a side bowled out under ~100 is very rare; lower-order
#                    hitting pushes the score to 110+ (see _make_floor_hook).
#   Variance Rule  — the Ceiling is a cap, not a baseline; totals fluctuate
#                    between Floor and Ceiling, most near Par (_make_variance_hook).
#   Fighting Match — chases stay close and deep; a low-win-probability side loses
#                    by ~10-15 rather than capitulating (_make_no_collapse_hook +
#                    the total-band chase baseline set at the innings break).

# (Sub-100) The tail digs in once 5+ down and short of the pitch's floor.
FLOOR_MIN_WICKETS = 5
FLOOR_GLOBAL = 100               # fallback floor when a pitch has no spec dynamics
FLOOR_WICKET_SCALE_MIN = 0.22    # hardest Wicket damping when far below floor
FLOOR_BOUNDARY_MAX = 1.70        # cap on lower-order boundary uplift

# (Variance) One multiplier per innings — the innings' scoring "mood".
VARIANCE_SIGMA = 0.11
VARIANCE_LO, VARIANCE_HI = 0.76, 1.20

# (Fighting Match) Anti-capitulation corridor for the 2nd-innings chase. It
# PURELY preserves wickets (no scoring help) so a losing chase bats deep and
# loses close instead of folding — it must not drag the chase back into a win.
CORRIDOR_BALLS_FROM = 30         # engage from ~over 5 once a chase can fall behind
CORRIDOR_WICKET_DAMP_MIN = 0.52  # ease the fold without preserving a slog-to-win bank
CORRIDOR_BOUNDARY_MAX = 1.0      # no catch-up scoring — anti-fold only
CORRIDOR_GAP_RUNS = 30.0         # deficit vs par-chase line that saturates the hook

# Chase baseline (who wins): steer a tough chase toward the spec Chase Win% by
# suppressing SCORING (they fall short), NOT by taking wickets (which would make
# them fold cheaply and break the Fighting Match rule). Wicket coupling is kept
# deliberately weak so the losing side bats deep and loses close.
BASELINE_BOUNDARY_K = 0.32       # ±32% boundaries — kept mild so a losing chase
BASELINE_DOT_K = 0.05            # stays in touch and loses CLOSE, not blown away
BASELINE_WICKET_K = 0.12         # only ∓12% wickets (weak on purpose)
# The engine gives the side batting second an intrinsic edge (it knows the
# target, dew, etc.). Shift the effective win% down so a spec-50% total is
# actually defended ~50% of the time rather than routinely chased down.
CHASE_INTRINSIC_BIAS = 14


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _draw_variance():
    """Draw this innings' scoring-mood multiplier (Variance Rule)."""
    return max(VARIANCE_LO, min(VARIANCE_HI, random.gauss(1.0, VARIANCE_SIGMA)))


def _chase_baseline_effects(chasing_pct):
    """Map the chasing side's spec win% to a ``pressure_effects`` bias that tilts
    the whole chase toward that result — but mostly via scoring, not wickets, so a
    low-probability chase falls SHORT rather than capitulating (Fighting Match
    Rule). ``tilt`` is +1 for a nailed-on chase, -1 for a hopeless one."""
    tilt = _clamp((chasing_pct - CHASE_INTRINSIC_BIAS - 50) / 50.0, -1.0, 1.0)
    return {
        "boundary_modifier": 1.0 + BASELINE_BOUNDARY_K * tilt,
        "dot_bonus": -BASELINE_DOT_K * tilt,
        "wicket_modifier": 1.0 - BASELINE_WICKET_K * tilt,
    }


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
    finish_ball)``. Thriller-forward weighting — finishes cluster at the wire so
    the mode lives up to "more drama":

        32%  last-ball win            (19.6)
        22%  win with 1 ball to spare (19.5)
        16%  tie → Super Over
        20%  finish in the 19th over  (18.1–18.6)
        10%  comfortable win          (17.1–17.6, 2+ overs to spare)

    ``finish_ball`` is an absolute legal-ball index (1..overs*6); ``None`` for the
    tie profile, which is handled by the super_over_thriller script.
    """
    balls = overs * 6
    r = random.random()
    if r < 0.32:
        return "controlled_finish", balls
    if r < 0.54:
        return "controlled_finish", balls - 1
    if r < 0.70:
        return "super_over_thriller", None
    if r < 0.90:
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
    # Deliberately no ``country``/``version``: Challenge League squads are league
    # rosters handed to both captains, not cards anyone collected, so — exactly
    # like traits — Team Chemistry does not apply to them. See
    # handlers.cipl_play._chem_line for the display side of the same rule.
    rating = int(_g("rating", default=50) or 50)
    bat_rating = int(_g("bat_rating", default=50) or 50)
    bowl_rating = int(_g("bowl_rating", default=40) or 40)
    return {
        "roster_id": int(getattr(cp, "id")),
        "player_id": _g("source_player_id", default=getattr(cp, "source_player_id", None)),
        "name": getattr(cp, "name", None) or _g("name", default="Player"),
        # ``rating``/``bat_rating``/``bowl_rating`` are the ENGINE's numbers and
        # may be adjusted before the first ball (see
        # handlers.cipl_play._compress_team_gap). ``card_*`` is the squad-sheet
        # rating the captain picked, is never touched by any balancing pass, and
        # is what every piece of UI must show — otherwise an 87 turns up as 86 in
        # one dugout and 88 in the other, which reads as a bug even though the
        # simulation is behaving as designed. See display_rating() below.
        "rating": rating,
        "bat_rating": bat_rating,
        "bowl_rating": bowl_rating,
        "card_rating": rating,
        "card_bat_rating": bat_rating,
        "card_bowl_rating": bowl_rating,
        "category": category,
        "bowl_style": _g("bowl_style", default="") or "",
        "bowl_hand": _g("bowl_hand", default="Right") or "Right",
        "bat_hand": _g("bat_hand", default="Right") or "Right",
    }


def display_rating(player, key="rating"):
    """The rating to SHOW for ``player`` — always the printed card number.

    ``key`` is the engine key ('rating', 'bat_rating', 'bowl_rating'); the
    matching ``card_<key>`` wins when present. States created before card
    ratings were stored have no ``card_*`` keys, so those fall back to the
    engine value and read exactly as they did before.
    """
    if not player:
        return 0
    val = player.get("card_" + key)
    if val is None:
        val = player.get(key, 0)
    return int(val or 0)


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
    Tunable via the CIPL_SCENARIO_PROBABILITY env var (default 0.70 —
    thriller-forward, so most chases go down to the wire). Raised from 0.55 to
    make Challenge League matches less one-sided and more likely to finish
    close; the engine still self-disables a scripted finish per over when it
    would look unrealistic, so a genuinely dominant chase stays dominant."""
    import math
    try:
        v = float(os.environ.get("CIPL_SCENARIO_PROBABILITY", "0.70"))
    except (TypeError, ValueError):
        return 0.70
    if not math.isfinite(v):  # nan/inf → safe default
        return 0.70
    return max(0.0, min(1.0, v))


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
    # The Hundred keeps overs == 20 but is a 100-ball innings; the scenario
    # engine's finish corridors are calibrated in 6-ball overs (finish balls up
    # to ~119), so never arm it for Hundred matches.
    if is_hundred(state):
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
        # Match format: "T20" (20 overs x 6 balls) or "The100" (20 sets x 5).
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
        # Per-innings scoring "mood" for the Variance Rule (redrawn at the break).
        "innings_variance": _draw_variance(),
        # Chasing side's spec win% for the 2nd-innings target (set at the break).
        "chase_target_pct": 50,
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

    # 3) Everyone is at quota / only the blocked bowler is left.
    if is_hundred(state):
        # Keep The Hundred's 20-ball-per-bowler cap strict — never relax quota
        # (which would offer an illegal 5th set). Only as a final resort relax
        # the 10-ball consecutive-spell rule so a set can still be bowled; with a
        # normal XI (44 sets of capacity vs 20 needed) this branch is unreachable.
        pool = _avail(xi, enforce_prev=False) or list(xi)
        return _sorted(pool)
    # T20: relax the back-to-back rule, then the quota, so play can always continue.
    pool = (_avail(xi, enforce_prev=False)
            or _avail(xi, enforce_quota=False, enforce_prev=False)
            or list(xi))
    return _sorted(pool)


def _fielding_quality(xi):
    """Average fielding quality (35-95) of a fielding XI.

    Players carry no dedicated fielding rating, so overall ``rating`` is the
    proxy (falling back to the bat/bowl mean). Feeds the drop-catch and
    misfield mechanics in engine/ball_outcome.calculate_outcome.
    """
    vals = []
    for p in xi or []:
        v = (p.get("fielding_rating") or p.get("rating")
             or (float(p.get("bat_rating") or 50) + float(p.get("bowl_rating") or 40)) / 2)
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return 65.0
    return max(35.0, min(95.0, sum(vals) / len(vals)))


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
    six = 1.32 if is_live_chase else 1.24
    four = 1.20 if is_live_chase else 1.14
    wkt = 1.24 if is_live_chase else 1.16
    dot = 0.84

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


def _make_clutch_hook(runs_needed, balls_left, wickets_left, is_final_ball):
    """Death-overs "clutch" amplifier for a live LetsPlay chase.

    Unlike the scenario engine (which scripts a fixed run value and bypasses
    ratings/traits), this is a BOUNDED weight nudge layered on top of the normal
    rating/trait weights — so the batsman's rating, the bowler's rating and the
    active clutch traits (Finisher / Clutch / Death / Yorker) still decide the
    ball. It only tilts intent by how much the chase needs:

      • required-per-ball ``rpb`` high  → go big: Six/Four up, Dot down, a modest
        Wicket bump (risk of going for it).
      • ``rpb`` low                     → play safe: Dot up, Six/Wicket down.
      • ``is_final_ball``               → maximum six-or-bust spread.

    Returns None when there's nothing to chase (defensive), leaving weights raw.
    """
    if balls_left <= 0:
        return None
    rpb = max(0.0, runs_needed) / balls_left

    # Intent factor 0..1: ~0 at rpb<=1 (cruising), ~1 at rpb>=2.5 (all-out).
    intent = max(0.0, min(1.0, (rpb - 1.0) / 1.5))

    six = 1.0 + 0.75 * intent          # up to 1.75
    four = 1.0 + 0.45 * intent         # up to 1.45
    wkt = 1.0 + 0.35 * intent          # up to 1.35 (going for it → risk)
    dot = 1.0 - 0.40 * intent          # down to 0.60
    single = 1.0 - 0.15 * intent

    if rpb < 0.7:
        # Cruising home — protect wickets, milk singles, no need to slog.
        six, four, wkt, dot, single = 0.80, 0.90, 0.80, 1.25, 1.20

    if is_final_ball:
        # The very last ball of a live chase: crank the spread to the max within
        # bounds. Ratings/traits still pick who wins the moment.
        six = max(six, 1.85)
        four = max(four, 1.45)
        wkt = max(wkt, 1.45)
        dot = min(dot, 0.55)

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
        if "Single" in rw:
            rw["Single"] *= single
        return rw

    return _hook


def _make_variance_hook(v):
    """Variance Rule: shift the WHOLE innings up or down by one per-innings
    multiplier ``v`` — boost boundaries and trim dots symmetrically (Wicket
    untouched, so ratings and collapses still decide the result). Successive
    matches on the same pitch therefore fluctuate between the Floor and the
    Ceiling, most landing near Par. No-op when ``v`` is neutral."""
    if v is None or abs(v - 1.0) < 1e-6:
        return None
    dot_scale = max(0.1, 2.0 - v)   # v>1 → fewer dots; v<1 → more dots
    dbl_scale = 1.0 + (v - 1.0) * 0.5

    def _hook(raw_weights):
        rw = dict(raw_weights)
        if "Four" in rw:
            rw["Four"] *= v
        if "Six" in rw:
            rw["Six"] *= v
        if "Double" in rw:
            rw["Double"] *= dbl_scale
        if "Dot" in rw:
            rw["Dot"] *= dot_scale
        return rw

    return _hook


def _make_floor_hook(state, pitch):
    """Sub-100 Rule: once the batting side is several wickets down and short of
    the pitch's expected Floor, the tail digs in and swings — damp Wicket and
    lift Four/Six, scaled by how far below the Floor the score is and how many
    are down. Bounded (Wicket never below ``FLOOR_WICKET_SCALE_MIN``) so genuine
    collapses still happen, but a full all-out under ~100 becomes rare. No-op
    above threshold."""
    wkts = state.get("total_wickets", 0)
    if wkts < FLOOR_MIN_WICKETS:
        return None
    dyn = ground_config.get_scoring_dynamics(pitch) or {}
    # Key to the pitch's own expected Floor — scores rarely dip below it, so a
    # side collapsing under it gets tail resistance until it climbs back toward it.
    floor = int(dyn.get("floor", FLOOR_GLOBAL))
    runs = state.get("total_runs", 0)
    if runs >= floor:
        return None
    # 0 at the floor → 1 when half the floor short.
    deficit = min(1.0, (floor - runs) / max(1.0, floor * 0.5))
    wkt_urgency = (wkts - FLOOR_MIN_WICKETS + 1) / (WICKET_LIMIT - FLOOR_MIN_WICKETS + 1)
    strength = min(1.0, deficit * (0.78 + 0.22 * wkt_urgency))
    wkt_scale = 1.0 - (1.0 - FLOOR_WICKET_SCALE_MIN) * strength
    bdry_scale = 1.0 + (FLOOR_BOUNDARY_MAX - 1.0) * strength

    def _hook(raw_weights):
        rw = dict(raw_weights)
        if "Wicket" in rw:
            rw["Wicket"] *= wkt_scale
        if "Four" in rw:
            rw["Four"] *= bdry_scale
        if "Six" in rw:
            rw["Six"] *= bdry_scale
        if "Dot" in rw:
            rw["Dot"] *= (1.0 - 0.15 * strength)
        return rw

    return _hook


def _make_no_collapse_hook(state):
    """Fighting Match Rule (2nd innings): keep a chase alive and deep. When the
    chasing side has slipped behind the even par-chase line, damp Wicket so they
    keep wickets in hand rather than folding — scaled by how far behind par they
    are. This tightens the losing margin and carries the game into the closing
    overs WITHOUT manufacturing a win: it adds no scoring (``CORRIDOR_BOUNDARY_MAX``
    is 1.0 by default — pure anti-fold), disengages the moment the score reaches
    par, and the result is still arbitrated by the total-band chase baseline + the
    back-overs matrix steer. No-op in innings 1, the opening overs, or when ahead."""
    if state.get("innings") != 2 or not state.get("target"):
        return None
    innings_balls = total_balls(state)
    bowled = balls_bowled(state)
    if bowled < CORRIDOR_BALLS_FROM or bowled >= innings_balls:
        return None
    target = int(state["target"])
    par_line = target * (bowled / innings_balls)   # runs an even chase would have
    behind = par_line - state.get("total_runs", 0)
    if behind <= 0:
        return None
    strength = min(1.0, behind / CORRIDOR_GAP_RUNS)
    wkt_scale = 1.0 - (1.0 - CORRIDOR_WICKET_DAMP_MIN) * strength
    bdry_scale = 1.0 + (CORRIDOR_BOUNDARY_MAX - 1.0) * strength

    def _hook(raw_weights):
        rw = dict(raw_weights)
        if "Wicket" in rw:
            rw["Wicket"] *= wkt_scale
        # Boundary lift is gated by CORRIDOR_BOUNDARY_MAX (1.0 by default = none).
        # No Dot reduction: the corridor must not add scoring — it only stops the
        # fold (see docstring), so a losing chase loses close, never gets dragged
        # back into a win.
        if bdry_scale != 1.0:
            if "Four" in rw:
                rw["Four"] *= bdry_scale
            if "Six" in rw:
                rw["Six"] *= bdry_scale
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
    balls_left = total_balls(state) - balls_bowled(state)
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
    # Fielding side quality — activates dropped catches / misfields in the
    # engine (computed once per over; the XI doesn't change mid-over).
    fielding_q = _fielding_quality(state.get("bowl_xi"))
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

    # Defensive: if some other code path shortened the innings, recompute the
    # over-derived inputs so this over's balls-left / required-rate / phase
    # boundaries match the current length.
    if state["overs"] != overs_total:
        overs_total = state["overs"]
        innings_balls = total_balls(state)
        if hundred:
            engine_overs = max(1, round(innings_balls / 6))
            engine_fmt = _fmt_to_engine_fmt(
                {"label": "The100", "overs": engine_overs, "max_bowler_overs": 4,
                 "powerplay_end": 4, "death_start": max(2, engine_overs - 3)}, overs_total)
        else:
            engine_overs = overs_total
            engine_fmt = _fmt_to_engine_fmt(None, overs_total)
        pressure_eng = PressureEngine(format_config=engine_fmt)

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

        # ── LetsPlay clutch finale ──
        # In a live LetsPlay chase, the death overs (scenario finale phase, or the
        # last CLUTCH_WINDOW_BALLS legal balls) are resolved through the NORMAL
        # engine path with a clutch intent amplifier — so ratings + traits decide
        # the six/wicket, instead of the scenario engine's fixed scripted value.
        # CIPL never sets ``clutch_finale`` so its scripted finale is untouched.
        runs_needed = (target - state["total_runs"]) if target is not None else 0
        letsplay_finale = (
            bool(state.get("clutch_finale"))
            and innings == 2 and target is not None
            and runs_needed > 0 and balls_left > 0
            and (scenario_phase == "finale" or balls_left <= CLUTCH_WINDOW_BALLS)
        )

        # ── Scenario engine hook ──
        # Finale phase scripts the delivery outright; free-play/convergence
        # phases instead nudge the pressure effects toward the target corridor.
        # Skipped entirely for a LetsPlay clutch finale (no scripted override).
        scenario_override = (scenario_eng.get_override_outcome(striker, bowler)
                             if (scenario_eng and not letsplay_finale) else None)
        if scenario_override:
            oc = _normalize_outcome(scenario_override)
        else:
            if scenario_eng and not letsplay_finale:
                _merge_pressure(pressure_effects, scenario_eng.get_scenario_bias({}))

            # Chase-chance steering — a controlled nudge toward the matrix-estimated
            # chasing chance. Skipped while a dramatic-finish scenario is actively
            # steering (those matches are curated by the ScenarioEngine), so the
            # two systems never fight.
            if (scenario_eng is None or scenario_phase == "inactive") \
                    and innings == 2 and target:
                # Layer 1: spec total-band baseline — tilt the whole chase toward
                # the pitch's Chase Win% for this target (the "who wins" lever),
                # biasing scoring rather than wickets so a losing chase falls short
                # instead of folding cheaply.
                _merge_pressure(pressure_effects,
                                _chase_baseline_effects(state.get("chase_target_pct", 50)))
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
            # Pitch Rule Engine layers: Sub-100 floor guard, the anti-capitulation
            # fighting-match corridor (2nd innings), and the per-innings variance
            # mood. All bounded nudges on top of the rating/trait weights.
            floor_hook = _make_floor_hook(state, pitch)
            corridor_hook = _make_no_collapse_hook(state)
            variance_hook = _make_variance_hook(state.get("innings_variance"))
            # LetsPlay clutch amplifier — layered AFTER traits so trait deltas
            # (Finisher/Clutch/Death/Yorker) land first, then chase intent scales
            # the six-or-bust spread. None for /cipl and non-finale balls.
            clutch_hook = (
                _make_clutch_hook(
                    runs_needed, balls_left,
                    state.get("wicket_limit", WICKET_LIMIT) - state["total_wickets"],
                    balls_left == 1)
                if letsplay_finale else None)
            weight_hook = _compose_hooks(trait_hook, env_hook,
                                         wicket_hook, drama_hook,
                                         floor_hook, corridor_hook, variance_hook,
                                         clutch_hook)
            oc = _normalize_outcome(calculate_outcome(
                batter=batter_adapted, bowler=bowl_adapted, pitch=pitch,
                streak=streak, over_number=eng_over_idx, batter_runs=bs["runs"],
                innings=innings, pressure_effects=pressure_effects,
                allow_extras=True, free_hit=free_hit, balls_faced=bs["balls"],
                game_state=game_state, pitch_wear=pitch_wear,
                batting_position=state["striker_idx"] + 1,
                fielding_quality=fielding_q,
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
        # Hat-trick streak baseline — compared once the outcome is applied.
        wkts_before_ball = bws["wickets"]
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
            # A dropped catch converts a wicket into runs in the engine —
            # call it out, it's one of the most dramatic balls in cricket.
            if oc.get("dropped_catch"):
                _drop_text = (f"DROPPED! {striker_name} gets a life — "
                              f"the chance goes down and they scamper {runs}."
                              if runs else
                              f"DROPPED! {striker_name} gets a life — "
                              f"a costly miss in the field.")
                # Replace the generic runs event appended above — don't add a
                # second event for the same delivery.
                over_events[-1] = {"sym": str(runs), "text": f"Dropped catch! {striker_name} survives"}
                _push_commentary(state, _run_event(runs), striker_name,
                                 fh_prefix + _drop_text,
                                 runs=runs, event_key=_run_key)
            elif oc.get("duel") == "beaten" and runs == 0:
                # The bowler won this ball's rating duel outright — say so.
                _push_commentary(
                    state, _run_event(runs), striker_name,
                    fh_prefix + f"Beaten! {bowler['name']} squares "
                    f"{striker_name} up completely — pure class wins the duel.",
                    runs=runs, event_key=_run_key)
            elif oc.get("duel") == "punished" and runs in (4, 6):
                _push_commentary(
                    state, _run_event(runs), striker_name,
                    fh_prefix + f"PUNISHED! The moment {bowler['name']} "
                    f"missed the mark, {striker_name} put it away for {runs}.",
                    runs=runs, event_key=_run_key)
            else:
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

        note_bowler_ball(bws, bowler_wicket=bws["wickets"] > wkts_before_ball)

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
            # Sequence-aware fields reflect the *previous* delivery.
            "last_ball_boundary": bool(state.get("last_ball_boundary")),
            "last_ball_wicket": bool(state.get("last_ball_wicket")),
            "consecutive_dots": int(state.get("cmt_consec_dots", 0)),
        }
        text = (_COMMENTARY.get_commentary(ball_context, match_state) or "").strip()
        # Record this ball so the next delivery's commentary can reference it.
        _runs = oc.get("runs", 0)
        _scoring = (not is_wkt) and (not ball_context["is_extra"])
        state["last_ball_boundary"] = bool(_scoring and _runs in (4, 6))
        state["last_ball_wicket"] = bool(is_wkt)
        if _scoring and _runs == 0:
            state["cmt_consec_dots"] = int(state.get("cmt_consec_dots", 0)) + 1
        else:
            state["cmt_consec_dots"] = 0
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
    # Clear sequence-aware commentary flags so innings-1's final ball can't
    # trigger a back-to-back / post-wicket / dot-streak line on the first
    # delivery of the chase.
    state["last_ball_boundary"] = False
    state["last_ball_wicket"] = False
    state["cmt_consec_dots"] = 0
    # Drop the 1st-innings over snapshot so innings 2 starts with a clean card.
    state["last_over_timeline"] = []
    state["last_over_commentary"] = []

    # Fresh scoring mood for the chase (Variance Rule).
    state["innings_variance"] = _draw_variance()
    # Total-band Chase Win% for defending this target on this pitch — the
    # baseline that biases who wins the chase (Pitch Rule Engine spec).
    state["chase_target_pct"] = ground_config.get_chase_win_pct(
        state.get("pitch_type", "Hard"), state["target"] - 1)

    # Optionally arm a dramatic-finish scenario for the chase now that the
    # target is known (20-over matches only; self-disables if unrealistic).
    _maybe_enable_scenario(state)


def compute_result(state):
    """Return a result dict for a finished match (call after innings 2)."""
    inn1 = state.get("inn1_runs", 0)
    inn2 = state.get("total_runs", 0)
    target = state.get("target") or (inn1 + 1)
    # Par is one run short of the target, which equals the first-innings score
    # (target == inn1 + 1), so ties and run margins are judged against it.
    par = target - 1
    # Side that batted second is the current bat side.
    second_batting = state["bat_team_name"]
    first_batting = state.get("inn1_bat_team", state["bowl_team_name"])
    if inn2 >= target:
        wickets_in_hand = state.get("wicket_limit", WICKET_LIMIT) - state["total_wickets"]
        return {"winner": second_batting, "loser": first_batting,
                "margin_type": "wickets", "margin": max(0, wickets_in_hand),
                "tie": False}
    if inn2 == par:
        return {"winner": None, "loser": None, "margin_type": "tie",
                "margin": 0, "tie": True}
    return {"winner": first_batting, "loser": second_batting,
            "margin_type": "runs", "margin": par - inn2, "tie": False}
