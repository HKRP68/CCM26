"""Thin runtime glue between the match loops and the Balanced Drama Engine.

Keeps the engine wiring out of the (already large) sim/cipl loops: resolving the
preset, building the per-match engine, mapping a resolved outcome to the drama
outcome key, and composing the drama overlay onto an existing weight hook so it
is the LAST modifier before the single authoritative sample (Codex Prompt 05
§12). Nothing here reads or writes player data.
"""

from engine.drama import config as drama_config
from engine.drama.context import DramaContext
from engine.drama.engine import DramaEngine

# Version marker stored with a match's preset, so a saved match records which
# drama engine produced it (Prompt 05 §19 competition fairness).
DRAMA_VERSION = "drama-v1"


def resolve_preset():
    """The preset a new match is created under (env override, else file)."""
    return drama_config.active_preset()


def make_engine(preset_name, rng=None, match_id=None, state=None):
    """A DramaEngine for ``preset_name`` when its overlay is active, else None.

    When None, the match runs exactly as it did before this feature existed —
    the legacy / realism path, untouched.
    """
    cfg = drama_config.preset_config(preset_name)
    if not drama_config.overlay_active(cfg):
        return None
    return DramaEngine(cfg, rng=rng, match_id=match_id, state=state)


# Map a resolved delivery outcome to a drama outcome key + whether it was a
# legal ball and a bowler-credited wicket.
_RUNS_TO_KEY = {0: "Dot", 1: "Single", 2: "Double", 3: "Three", 4: "Four", 6: "Six"}


def outcome_key(otype, runs, is_extra, wicket_type=None):
    """(key, is_wicket, is_bowler_wicket) for the drama engine."""
    if otype == "wicket":
        return "Wicket", True, (wicket_type or "Caught") != "Run Out"
    if is_extra or otype == "extra":
        return "Extras", False, False
    return _RUNS_TO_KEY.get(int(runs), "Single"), False, False


def compose_hook(base_hook, engine, ctx, holder):
    """Wrap ``base_hook`` so the drama overlay runs last and the shaped weights
    it hands the sampler are captured in ``holder['pre']`` for momentum's
    expected-runs term (§8). ``base_hook`` may be None."""
    if engine is None:
        return base_hook

    def _hook(weights):
        w = weights
        if base_hook is not None:
            try:
                hooked = base_hook(weights)
                if hooked:
                    w = hooked
            except Exception:
                w = weights
        shaped = engine.before_delivery(ctx, w)
        holder["pre"] = shaped
        return shaped

    return _hook


def make_context(*, innings, runs, wickets, target, innings_balls, balls_bowled,
                 wicket_limit=10, edge=0.0, is_knockout=False, free_hit=False,
                 phase="middle"):
    return DramaContext(
        innings=innings, runs=runs, wickets=wickets, target=target,
        innings_balls=innings_balls, balls_bowled=balls_bowled,
        wicket_limit=wicket_limit, edge=float(edge), is_knockout=bool(is_knockout),
        free_hit=bool(free_hit), phase=phase)
