"""DramaEngine — the Balanced Drama overlay orchestrator (Codex Prompt 05, §5).

One engine per match. It holds a small JSON-serialisable state dict (so a cipl
match can persist it between turns) covering momentum, the rolling window, the
clutch/rare event state, the win-probability trail and the highlight log. It
never samples an outcome itself: ``before_delivery`` returns shaped weights that
the existing ``calculate_outcome`` funnel samples ONCE, and ``after_delivery``
updates state and detects highlights from the real result.

Processing contract (§12): the drama overlay is applied LAST, on top of the
realism baseline, ratings, pitch/weather, approaches and legitimate game-state
effects, as one budgeted modifier (modifiers.apply) — never a second
independent sample and never a second momentum system.
"""

import math

from engine.drama import clutch as _clutch
from engine.drama import config as _config
from engine.drama import events as _events
from engine.drama import modifiers as _modifiers
from engine.drama import momentum as _momentum
from engine.drama import tension as _tension


def _fresh_state(preset):
    return {
        "preset": preset,
        "innings_seen": 0,
        "momentum": 0.0,
        "window": [],
        "clutch": {"active": False, "kind": None, "balls_left": 0,
                   "used_batting": 0, "used_bowling": 0},
        "rare": {"active": False, "kind": None, "balls_left": 0, "used": 0},
        "wp_history": [],
        "wickets_window": [],          # legal-ball indices of recent wickets
        "boundary_streak": 0,
        "over_runs": 0,
        "over_wickets": 0,
        "bowler_wkt_streak": {},       # bowler_ref -> consecutive bowler wickets
        "seen_events": [],
        "highlights": [],
        "last_tension": 0,
        "last_diag": {},
    }


class DramaEngine:
    def __init__(self, preset_cfg, rng=None, match_id=None, state=None):
        self.cfg = preset_cfg or {}
        self.rng = rng
        self.match_id = match_id
        self.state = state if state is not None else _fresh_state(
            self.cfg.get("preset", "BALANCED"))
        self.overlay_active = _config.overlay_active(self.cfg)
        self._mcfg = self.cfg.get("momentum", {})
        self._ccfg = self.cfg.get("clutch", {})
        self._rcfg = self.cfg.get("rare_volatility", {})

    # ---- (de)serialisation ------------------------------------------------
    def to_state(self):
        return self.state

    @classmethod
    def from_state(cls, state, preset_cfg, rng=None, match_id=None):
        return cls(preset_cfg, rng=rng, match_id=match_id, state=state)

    # ---- lifecycle --------------------------------------------------------
    def new_innings(self, innings):
        """Reset per-innings drama state (momentum, window, event trackers).
        The per-side clutch budget and rare budget are per innings."""
        s = self.state
        s["innings_seen"] = int(innings)
        s["momentum"] = 0.0
        s["window"] = []
        s["clutch"] = {"active": False, "kind": None, "balls_left": 0,
                       "used_batting": 0, "used_bowling": 0}
        s["rare"] = {"active": False, "kind": None, "balls_left": 0, "used": 0}
        s["wp_history"] = []
        s["wickets_window"] = []
        s["boundary_streak"] = 0
        s["over_runs"] = 0
        s["over_wickets"] = 0
        s["bowler_wkt_streak"] = {}

    def _rng_random(self):
        if self.rng is None:
            return 0.5                 # deterministic no-op without an RNG
        return self.rng.random()

    # ---- over boundary: decide clutch / rare activation -------------------
    def on_over_start(self, ctx, high_pressure=False):
        if not self.overlay_active or ctx.decided:
            return
        s = self.state
        clutch_fired = False

        # Clutch Focus (§9) — one per side per innings, final-overs / pressure.
        if (self._ccfg.get("enabled") and not s["clutch"]["active"]
                and _clutch.eligible(ctx.balls_remaining, high_pressure,
                                     ctx.decided)):
            kind = _clutch.choose_type(self.rng if self.rng else _DummyRng())
            used_key = "used_%s" % kind
            cap_each = int(self._ccfg.get("max_activations_per_side_per_innings", 1))
            if s["clutch"][used_key] < cap_each:
                p = ctx.batting_win_prob()
                prob = _clutch.activation_probability(
                    p, cap=self._ccfg.get("max_activation_probability", 0.12),
                    first_innings=(ctx.innings == 1))
                if self._rng_random() < prob:
                    s["clutch"].update(active=True, kind=kind,
                                       balls_left=int(self._ccfg.get(
                                           "max_duration_legal_balls", 6)))
                    s["clutch"][used_key] += 1
                    clutch_fired = True

        # Rare volatility (§10) — never simultaneous with a clutch activation.
        if (self._rcfg.get("enabled") and not clutch_fired
                and not s["clutch"]["active"] and not s["rare"]["active"]
                and s["rare"]["used"] < int(self._rcfg.get("max_activations_per_innings", 1))):
            prob = self._rcfg.get("probability_per_eligible_over", 0.015)
            if self._rng_random() < float(prob):
                kind = _events.choose_rare_type(self.rng if self.rng else _DummyRng())
                # One over of effect; trimmed per legal ball in after_delivery.
                s["rare"].update(active=True, kind=kind, balls_left=6)
                s["rare"]["used"] += 1

    # ---- per delivery -----------------------------------------------------
    def before_delivery(self, ctx, base_weights):
        """Shaped weights for this delivery (the only authoritative sample runs
        on these). A pure pass-through when the overlay is inactive."""
        if not self.overlay_active:
            return base_weights
        s = self.state
        parts = {}
        if self._mcfg.get("enabled"):
            parts["momentum"] = _momentum.log_deltas(s["momentum"])
        if s["clutch"]["active"]:
            parts["clutch"] = _clutch.log_deltas(s["clutch"]["kind"])
        if s["rare"]["active"]:
            parts["rare"] = _events.rare_log_deltas(s["rare"]["kind"])

        combined = _modifiers.combine_log_deltas(*parts.values())
        new_weights, info = _modifiers.apply(base_weights, combined, self.cfg,
                                             free_hit=ctx.free_hit)
        s["last_diag"] = {
            "momentum_state": s["momentum"],
            "clutch": s["clutch"]["kind"] if s["clutch"]["active"] else None,
            "rare": s["rare"]["kind"] if s["rare"]["active"] else None,
            "systems": {k: {o: round(v, 5) for o, v in d.items()}
                        for k, d in parts.items()},
            "scale": info["scale"],
            "wicket_delta": round(info["wicket_delta"], 5),
            "runs_delta": round(info["runs_delta"], 5),
        }
        return new_weights

    def after_delivery(self, ctx, outcome, pre_weights):
        """Update momentum and event state from the realised ``outcome``.

        ``outcome`` keys: key, runs, is_wicket, legal, is_bowler_wicket,
        bowler_ref, team_ref, striker_ref, striker_runs_before,
        striker_runs_after, legal_ball_index.
        """
        s = self.state
        legal = bool(outcome.get("legal", True))
        runs = int(outcome.get("runs", 0))
        is_wkt = bool(outcome.get("is_wicket"))
        key = outcome.get("key", "Dot")
        lbi = int(outcome.get("legal_ball_index", ctx.balls_bowled))

        # Momentum (§8): record AFTER the delivery, never altering this ball.
        if legal and self._mcfg.get("enabled"):
            rec = _momentum.ball_record(key, runs, is_wkt, pre_weights or {})
            s["window"].append(rec)
            win = int(self._mcfg.get("window_legal_balls", 12))
            if len(s["window"]) > win:
                s["window"] = s["window"][-win:]
            s["momentum"] = _momentum.recompute(s["momentum"], s["window"],
                                                self._mcfg)

        # Clutch / rare duration (count down on legal balls).
        if legal:
            if s["clutch"]["active"]:
                s["clutch"]["balls_left"] -= 1
                if s["clutch"]["balls_left"] <= 0:
                    s["clutch"].update(active=False, kind=None, balls_left=0)
            if s["rare"]["active"]:
                s["rare"]["balls_left"] -= 1
                if s["rare"]["balls_left"] <= 0:
                    s["rare"].update(active=False, kind=None, balls_left=0)

        # Win-probability trail for tension's recent-swing term.
        s["wp_history"].append(ctx.batting_win_prob())
        if len(s["wp_history"]) > 6:
            s["wp_history"] = s["wp_history"][-6:]

        # Over accumulators + highlight trackers.
        s["over_runs"] += runs
        if is_wkt:
            s["over_wickets"] += 1
            s["wickets_window"].append(lbi)
            s["wickets_window"] = [b for b in s["wickets_window"]
                                   if lbi - b < 12]
        # Boundary streak (legal boundaries only; a wicket or dot breaks it).
        if legal and key in ("Four", "Six"):
            s["boundary_streak"] += 1
        elif legal:
            s["boundary_streak"] = 0

        # Bowler hat-trick streak (bowler-credited wickets only).
        bowler = outcome.get("bowler_ref")
        if legal and bowler is not None:
            streaks = s["bowler_wkt_streak"]
            if is_wkt and outcome.get("is_bowler_wicket"):
                streaks[bowler] = streaks.get(bowler, 0) + 1
            else:
                streaks[bowler] = 0

        self._detect_delivery_events(ctx, outcome, lbi)

    def on_over_complete(self, ctx, over_number):
        """Detect over-level highlights, then reset the over accumulators and
        clear any one-over rare event."""
        s = self.state
        self._emit(_events.explosive_over(
            over_number, s["over_runs"], match_id=self.match_id,
            innings=ctx.innings, legal_ball_index=ctx.balls_bowled,
            tension_index=s["last_tension"]))
        self._emit(_events.double_wicket_over(
            s["over_wickets"], over_number=over_number, match_id=self.match_id,
            innings=ctx.innings, legal_ball_index=ctx.balls_bowled,
            tension_index=s["last_tension"]))
        s["over_runs"] = 0
        s["over_wickets"] = 0
        if s["rare"]["active"]:
            s["rare"].update(active=False, kind=None, balls_left=0)

    # ---- detection / presentation ----------------------------------------
    def _detect_delivery_events(self, ctx, outcome, lbi):
        s = self.state
        common = dict(match_id=self.match_id, innings=ctx.innings,
                      legal_ball_index=lbi, tension_index=s["last_tension"],
                      team=outcome.get("team_ref"))
        self._emit(_events.three_consecutive_boundaries(
            s["boundary_streak"], **common))
        if outcome.get("is_wicket"):
            self._emit(_events.major_collapse(
                len(s["wickets_window"]), over_number=lbi // 6, **common))
            bowler = outcome.get("bowler_ref")
            if bowler is not None:
                self._emit(_events.hat_trick(
                    s["bowler_wkt_streak"].get(bowler, 0), bowler_ref=bowler,
                    **{k: v for k, v in common.items() if k != "team"}))
        self._emit(_events.milestone(
            int(outcome.get("striker_runs_before", 0)),
            int(outcome.get("striker_runs_after", 0)),
            high_pressure=s["last_tension"] >= 50,
            player=outcome.get("striker_ref"), **common))

    def _emit(self, event):
        if not event or not self.cfg.get("highlights", {}).get("enabled", True):
            return
        if event["event_id"] in self.state["seen_events"]:
            return
        self.state["seen_events"].append(event["event_id"])
        self.state["highlights"].append(event)

    def get_tension(self, ctx):
        """Live Tension Index for presentation (never alters an outcome)."""
        if not self.cfg.get("tension", {}).get("enabled", True):
            return 0
        swing = 0.0
        hist = self.state["wp_history"]
        for a, b in zip(hist, hist[1:]):
            swing = max(swing, abs(b - a))
        if ctx.is_chase:
            t = _tension.chase_tension(ctx.batting_win_prob(),
                                       ctx.balls_remaining, ctx.innings_balls,
                                       is_knockout=ctx.is_knockout,
                                       recent_swing=swing)
        else:
            recent = self.state["window"][-6:]
            t = _tension.first_innings_intensity(
                sum(r["runs"] for r in recent), max(1, len(recent)),
                sum(r["wicket"] for r in recent), ctx.wickets_in_hand,
                is_knockout=ctx.is_knockout)
        self.state["last_tension"] = int(t)
        return int(t)

    def get_highlights(self):
        return list(self.state["highlights"])

    def get_diagnostics(self):
        return dict(self.state["last_diag"])


class _DummyRng:
    def random(self):
        return 0.5
