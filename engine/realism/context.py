"""The per-delivery context the realism model reads.

A :class:`BallContext` is a plain value object. It holds *nothing* that is not
either (a) already on a CCM26 player card, or (b) already in live match state.
In particular it holds no derived player attribute: ``bat_rating`` and
``bowl_rating`` are the card numbers, and ``balls_faced`` / ``batter_runs`` /
``fatigue`` are match-state counters that die with the innings.

Construction is deliberately forgiving — every field has a default and
:func:`from_engine_call` tolerates the partial information some callers have —
because a missing field must degrade the model, never crash a delivery.
"""

from dataclasses import dataclass, field
from typing import Optional

from engine.realism import styles

from engine.realism import phases

# Phase keys. These are the hierarchy level-3 node names in the reference
# files, and they are format-agnostic: a 100-ball innings has a powerplay and
# a death too, they are just shorter. Re-exported from engine.realism.phases,
# the single source of truth for the phase boundaries.
POWERPLAY = phases.POWERPLAY
MIDDLE = phases.MIDDLE
DEATH = phases.DEATH


def wickets_bucket(wickets: int) -> str:
    """Hierarchy level-5 node name."""
    w = int(wickets or 0)
    if w <= 2:
        return "w0_2"
    if w <= 5:
        return "w3_5"
    return "w6_plus"


def chase_bucket(required_rate: Optional[float], format_name=None) -> Optional[str]:
    """Hierarchy level-6 node name, or None when not chasing."""
    if required_rate is None:
        return None
    r = float(required_rate)
    if format_name == "ODI":
        return ("rrr_lt4" if r < 4 else "rrr_4_6" if r < 6 else
                "rrr_6_8" if r < 8 else "rrr_8_10" if r < 10 else "rrr_10_plus")
    if r < 6.0:
        return "rrr_lt6"
    if r < 9.0:
        return "rrr_6_9"
    if r < 12.0:
        return "rrr_9_12"
    return "rrr_12_plus"


@dataclass
class BallContext:
    """Everything the realism model is allowed to look at."""

    # --- format & position in the innings -------------------------------
    format_name: str = "T20"
    over: int = 0                 # 0-indexed over number
    ball_in_over: int = 0
    innings: int = 1
    balls_per_over: int = 6
    innings_balls: int = 120      # legal balls available this innings

    # --- live match state ----------------------------------------------
    runs: int = 0
    wickets: int = 0
    balls_bowled: int = 0
    target: Optional[int] = None  # runs needed to WIN, when chasing

    # --- the two cards (ratings only) ----------------------------------
    bat_rating: float = 60.0
    bowl_rating: float = 60.0
    bat_style: str = "Right-handed"
    bowl_style: str = "Right arm medium fast"
    bowl_hand: str = "Right"
    bowling_type: str = "Medium-fast"

    # --- temporary, match-state-only batter variables -------------------
    batter_runs: int = 0
    balls_faced: int = 0
    batting_position: int = 5

    # --- conditions & tactics ------------------------------------------
    pitch: str = "Even"
    pitch_wear: float = 0.0
    is_day_night: bool = False
    dew: float = 0.0              # 0..1, from the conditions engine
    batting_approach: Optional[str] = None
    bowling_approach: Optional[str] = None
    bowler_fatigue: float = 1.0   # 1.0 = fresh (engine convention)
    free_hit: bool = False
    competition: Optional[str] = None   # baseline/era key override

    extra: dict = field(default_factory=dict)

    # --- derived (pure functions of the above) --------------------------
    @property
    def balls_remaining(self) -> int:
        return max(0, int(self.innings_balls) - int(self.balls_bowled))

    @property
    def phase(self) -> str:
        """Powerplay / middle / death, scaled to the innings length.

        The thresholds live in :mod:`engine.realism.phases`, which is the
        single source of truth shared with the reference builder so a
        delivery is always looked up under the node it was filed under.
        """
        return phases.phase_for_ball(self.balls_bowled, self.innings_balls, self.format_name)

    @property
    def innings_key(self) -> str:
        return "inn1" if int(self.innings or 1) == 1 else "inn2"

    @property
    def required_rate(self) -> Optional[float]:
        """Runs per over still needed, or None when not chasing."""
        if not self.target or int(self.innings or 1) != 2:
            return None
        need = max(0, int(self.target) - int(self.runs))
        balls = self.balls_remaining
        if balls <= 0:
            return None
        return need * float(self.balls_per_over) / float(balls)

    @property
    def par_rate(self):
        """The asking rate a chase STARTED with, or None when not chasing.

        This is the reference a chasing side actually measures itself
        against. Using the live run rate instead (or worse, a hardcoded
        "typical" 8.0) mis-reads the very first ball of a big chase as
        already behind, which then applies a behind-the-asking-rate penalty
        for the whole innings and makes chases fail far too often.
        """
        if not self.target or int(self.innings or 1) != 2:
            return None
        balls = max(1, int(self.innings_balls))
        return int(self.target) * float(self.balls_per_over) / balls

    @property
    def current_rate(self) -> float:
        if self.balls_bowled <= 0:
            return 0.0
        return self.runs * float(self.balls_per_over) / float(self.balls_bowled)

    @property
    def matchup(self) -> str:
        return styles.matchup_key(self.bat_style, self.bowl_hand, self.bowling_type)

    @property
    def bowling_family(self) -> str:
        return styles.bowling_family(self.bowling_type)

    def node_path(self, competition: str):
        """The hierarchy path this delivery looks up, most general first."""
        path = [self.format_name, competition, self.phase, self.innings_key,
                wickets_bucket(self.wickets)]
        chase = chase_bucket(self.required_rate, self.format_name)
        if chase:
            path.append(chase)
        return path


def from_engine_call(batter, bowler, *, pitch, over_number, innings,
                     batter_runs, balls_faced, batting_position,
                     format_config=None, game_state=None, pitch_wear=0.0,
                     is_day_night=False, batting_approach=None,
                     bowling_approach=None, approach_context=None,
                     free_hit=False, bat_rating_effective=None,
                     bowl_rating_effective=None,
                     match_context=None) -> BallContext:
    """Build a :class:`BallContext` from ``calculate_outcome``'s arguments.

    ``bat_rating_effective`` / ``bowl_rating_effective`` let the caller pass
    the ratings *after* the engine's existing position and fatigue scaling,
    so the realism model does not re-apply something the engine already did.

    ``match_context`` is the scoreboard: ``{"runs", "wickets", "balls_bowled",
    "target", "innings_balls"}``. It matters more than it looks. The game-state vector that
    ``calculate_outcome`` already receives (``engine.game_state_engine
    .compute_game_state_vector``) does NOT carry the score, the wickets or
    the target — it carries derived indices like ``rr_ratio``,
    ``required_aggression``, ``wickets_in_hand`` and ``rrr``. Reading
    ``game_state["score"]`` or ``["target"]`` therefore silently yields zero
    and None, which pins every delivery to the ``w0_2`` hierarchy node and
    switches the entire chase layer off without any error. So the scoreboard
    is taken from ``match_context`` when the caller supplies it, and
    otherwise reconstructed from the game-state keys that DO exist.
    """
    batter = batter or {}
    bowler = bowler or {}
    gs = game_state or {}
    mc = match_context or {}
    ctx_in = approach_context or {}

    fmt_name = "T20"
    innings_balls = 120
    bpu = 6
    if format_config is not None:
        fmt_name = getattr(format_config, "name", "T20") or "T20"
        total_overs = getattr(format_config, "overs", None) or 20
        bpu = int(getattr(format_config, "balls_per_over", 6) or 6)
        innings_balls = int(total_overs) * bpu
    # The caller's own innings length wins, because the FormatConfig's does
    # not always match the match being played: services.sim_match
    # ._fmt_to_engine_fmt returns the 20-over T20 config for ANY match of 20
    # overs or fewer, so a 10-over /sim would otherwise be told it has 120
    # balls. Its phase fraction would then top out at 0.5 and the innings
    # would never reach its death overs at all.
    if match_context and match_context.get("innings_balls"):
        innings_balls = max(1, int(match_context["innings_balls"]))

    # Balls bowled: the scoreboard, else the game-state's balls_remaining,
    # else the over number (which is a whole-over approximation).
    balls_bowled = mc.get("balls_bowled")
    if balls_bowled is None and gs.get("balls_remaining") is not None:
        balls_bowled = innings_balls - int(gs["balls_remaining"])
    if balls_bowled is None:
        balls_bowled = int(over_number or 0) * bpu
    balls_bowled = max(0, int(balls_bowled))

    # Wickets: the scoreboard, else 10 - wickets_in_hand.
    if mc.get("wickets") is not None:
        wickets = int(mc["wickets"])
    elif gs.get("wickets_in_hand") is not None:
        wickets = max(0, 10 - int(gs["wickets_in_hand"]))
    else:
        wickets = 0

    runs = int(mc.get("runs") or 0)
    target = mc.get("target") or None
    if target:
        target = int(target)
        if target <= 0:
            target = None
    # No target on the scoreboard, but the game-state vector knows the
    # required rate: reconstruct the runs still needed from it so the chase
    # layer works even for a caller that has not been wired up yet.
    if target is None and int(innings or 1) == 2:
        rrr = gs.get("rrr") or 0.0
        left = gs.get("balls_remaining")
        if rrr > 0 and left:
            target = runs + int(round(float(rrr) * int(left) / float(bpu)))

    return BallContext(
        format_name=fmt_name,
        over=int(over_number or 0),
        innings=int(innings or 1),
        balls_per_over=bpu,
        innings_balls=innings_balls,
        runs=int(runs),
        wickets=int(wickets),
        balls_bowled=int(balls_bowled),
        target=int(target) if target else None,
        bat_rating=float(bat_rating_effective
                         if bat_rating_effective is not None
                         else batter.get("batting_rating", 60) or 60),
        bowl_rating=float(bowl_rating_effective
                          if bowl_rating_effective is not None
                          else bowler.get("bowling_rating", 60) or 60),
        bat_style=str(batter.get("batting_hand") or batter.get("bat_style")
                      or "Right"),
        bowl_style=str(bowler.get("bowl_style") or bowler.get("bowling_type") or ""),
        bowl_hand=str(bowler.get("bowling_hand") or "Right"),
        bowling_type=str(bowler.get("bowling_type") or bowler.get("bowl_style") or ""),
        batter_runs=int(batter_runs or 0),
        balls_faced=int(balls_faced or 0),
        batting_position=int(batting_position or 5),
        pitch=str(pitch or "Even"),
        pitch_wear=float(pitch_wear or 0.0),
        is_day_night=bool(is_day_night),
        dew=float(ctx_in.get("dew") or 0.0),
        batting_approach=batting_approach,
        bowling_approach=bowling_approach,
        bowler_fatigue=float(ctx_in.get("fatigue", 1.0) or 1.0),
        free_hit=bool(free_hit),
        extra={"approach_context": ctx_in},
    )
