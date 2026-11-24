"""Live, match-only state the drama overlay reads (Codex Prompt 05).

``DramaContext`` is a thin snapshot of the scoreboard for one delivery. Every
field is current match state — nothing here is, or becomes, a stored player
attribute. It is built by the match loop (cipl / sim) just before a delivery
and thrown away with the ball.
"""

from dataclasses import dataclass
from typing import Optional

from engine.realism import win_probability as _wp


@dataclass
class DramaContext:
    innings: int = 1
    runs: int = 0
    wickets: int = 0
    target: Optional[int] = None          # runs to win (innings 2), else None
    innings_balls: int = 120              # total legal balls in the innings
    balls_bowled: int = 0                 # legal balls completed so far
    wicket_limit: int = 10
    edge: float = 0.0                     # batting-vs-bowling rating edge
    is_knockout: bool = False             # final / knockout match
    free_hit: bool = False
    phase: str = "middle"                 # powerplay | middle | death

    # ---- derived helpers -------------------------------------------------
    @property
    def balls_remaining(self) -> int:
        return max(0, int(self.innings_balls) - int(self.balls_bowled))

    @property
    def wickets_in_hand(self) -> int:
        return max(0, int(self.wicket_limit) - int(self.wickets))

    @property
    def is_chase(self) -> bool:
        return self.innings >= 2 and self.target is not None

    @property
    def runs_required(self) -> Optional[int]:
        if not self.is_chase:
            return None
        return max(0, int(self.target) - int(self.runs))

    @property
    def decided(self) -> bool:
        """Is the match already over as a contest? (No drama after this.)"""
        if self.wickets_in_hand <= 0:
            return True
        if self.is_chase:
            if self.runs_required is not None and self.runs_required <= 0:
                return True
            if self.balls_remaining <= 0:
                return True
        else:
            if self.balls_remaining <= 0:
                return True
        return False

    def batting_win_prob(self) -> float:
        """The batting side's estimated win probability, from the realism
        win-probability model. Chase in the second innings; an integrated
        projected-total estimate in the first. Bounded [0, 1]."""
        if self.is_chase:
            rr = self.runs_required
            if rr is None:
                return 0.5
            p = _wp.chase_success(rr, self.balls_remaining,
                                  self.wickets_in_hand, self.edge)
        else:
            p = _wp.first_innings_win_prob(
                self.runs, self.balls_remaining, self.wickets_in_hand,
                innings_balls=self.innings_balls, edge=self.edge)
        return max(0.0, min(1.0, float(p)))
