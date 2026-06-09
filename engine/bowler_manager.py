"""
engine/bowler_manager.py
========================

Centralises all bowler selection and constraint enforcement for any cricket
format.  Previously this logic was scattered across ~15 inline snippets in
match.py (all hardcoding the T20 quota of 4 overs).

Rules enforced
--------------
1. Bowling quota   — a bowler may not exceed format_config.max_bowler_overs
                     per innings while quota-safe alternatives exist.
2. No-consecutive  — when format_config.allow_consecutive_overs is False, a
                     bowler may not bowl two overs in a row.
3. Fresh-bowler    — every bowler marked will_bowl must bowl at least 1 over
                     if the remaining overs allow it (mirrors existing logic
                     in match.py).
4. Fatigue         — diminishing effectiveness multiplier per over bowled,
                     extended to 10 overs for ListA.

Usage (in match.py)
-------------------
    from engine.bowler_manager import BowlerManager

    # Construction — once per innings
    self.bowler_manager = BowlerManager(self.bowling_team, self.fmt)

    # At the start of each over
    eligible = self.bowler_manager.get_eligible_bowlers(self.current_over,
                                                        overs_remaining)
    selected = <pick from eligible>

    # At the end of each over
    self.bowler_manager.record_over_completion(selected["name"],
                                               runs_this_over)

    # When computing effective bowler dict
    fatigue = self.bowler_manager.get_fatigue_mult(bowler_name)
    prev_runs = self.bowler_manager.prev_over_runs(bowler_name)

    # For UI / scorecard
    overs_left = self.bowler_manager.overs_remaining(bowler_name)
"""

import logging
from typing import Dict, List, Optional

from engine.format_config import FormatConfig

logger = logging.getLogger(__name__)


class BowlerManager:
    """
    Manages bowling quota, consecutive-over restriction, and fatigue for one
    innings of a match.

    Parameters
    ----------
    bowling_xi   : list of player dicts from the bowling team XI.
    format_config: FormatConfig instance for the current match format.
    """

    # Fatigue multiplier keyed by overs already bowled at the START of this over.
    # T20 stops at 4 (the quota limit), ListA extends to 10.
    _FATIGUE_TABLE: Dict[int, float] = {
        0:  1.00,
        1:  1.00,
        2:  0.99,
        3:  0.97,
        4:  0.94,   # T20 quota — values below are ListA-only
        5:  0.91,
        6:  0.88,
        7:  0.84,
        8:  0.80,
        9:  0.75,
        10: 0.70,   # bowling 10th over — significant wear
    }

    def __init__(self, bowling_xi: list, format_config: FormatConfig):
        self.fmt = format_config
        self._quota: Dict[str, int] = {}          # overs completed this innings
        self._last_bowler: Optional[str] = None   # name of bowler in prev over
        self._prev_over_runs: Dict[str, int] = {} # runs given per completed over

        # Build eligible set once — only players flagged will_bowl
        self._eligible_xi: List[dict] = [
            p for p in bowling_xi if p.get("will_bowl", False)
        ]
        # Initialise quota to 0 for each eligible bowler
        for p in self._eligible_xi:
            self._quota[p["name"]] = 0

    # ------------------------------------------------------------------ #
    # Public query interface                                               #
    # ------------------------------------------------------------------ #

    def get_eligible_bowlers(
        self,
        current_over: int,
        overs_remaining_in_innings: int,
    ) -> List[dict]:
        """
        Return bowlers eligible to bowl the current over.

        Eligibility criteria (in priority order):
        1. Player is marked will_bowl in the XI.
        2. Has not exhausted the format bowling quota.
        3. Did not bowl the immediately preceding over
           (if allow_consecutive_overs is False).

        If applying all criteria produces an empty list, the method relaxes
        quota before it relaxes the no-consecutive rule. If the previous bowler
        is the only available bowler, an empty list is returned so callers can
        surface a visible match-state error.

        Additionally, if the count of "fresh" bowlers (0 overs bowled) equals
        the number of overs remaining, those fresh bowlers are returned
        exclusively so that every designated bowler gets at least 1 over.

        Parameters
        ----------
        current_over              : 0-based index of the over about to be bowled.
        overs_remaining_in_innings: total overs left (including this one).
        """
        max_q = self.fmt.max_bowler_overs
        no_consec = not self.fmt.allow_consecutive_overs

        # --- Primary pool: quota + no-consecutive ---
        strict: List[dict] = []
        for p in self._eligible_xi:
            name = p["name"]
            if self._quota.get(name, 0) >= max_q:
                continue
            if no_consec and name == self._last_bowler:
                continue
            strict.append(p)

        # --- Fresh-bowler override ---
        # If all remaining overs must be taken up by unbowled bowlers, force them.
        fresh = [p for p in strict if self._quota.get(p["name"], 0) == 0]
        if fresh and len(fresh) == overs_remaining_in_innings:
            logger.debug(
                "BowlerManager: forcing fresh bowlers %s (overs_remaining=%d)",
                [p["name"] for p in fresh], overs_remaining_in_innings
            )
            return fresh

        if strict:
            return strict

        # --- Fallback 1: relax quota, never consecutive ---
        non_consecutive: List[dict] = [
            p for p in self._eligible_xi
            if not (no_consec and p["name"] == self._last_bowler)
        ]
        if non_consecutive:
            logger.warning(
                "BowlerManager: relaxing quota rule at over %d "
                "to preserve no-consecutive rule (last bowler=%s)",
                current_over, self._last_bowler
            )
            return sorted(
                non_consecutive,
                key=lambda p: self._quota.get(p["name"], 0)
            )

        # --- Fallback 2: no legal non-consecutive bowler exists ---
        logger.warning(
            "BowlerManager: no non-consecutive bowler available at over %d "
            "(last bowler=%s)",
            current_over, self._last_bowler
        )
        return []

    def overs_remaining(self, bowler_name: str) -> int:
        """Overs this bowler can still bowl in the current innings."""
        done = self._quota.get(bowler_name, 0)
        return max(0, self.fmt.max_bowler_overs - done)

    def overs_bowled(self, bowler_name: str) -> int:
        """Overs this bowler has completed in the current innings."""
        return self._quota.get(bowler_name, 0)

    def get_fatigue_mult(self, bowler_name: str) -> float:
        """
        Return the effectiveness multiplier for a bowler's *next* delivery,
        based on how many overs they have already bowled this innings.
        """
        done = self._quota.get(bowler_name, 0)
        return self._FATIGUE_TABLE.get(
            min(done, self.fmt.max_bowler_overs),
            self._FATIGUE_TABLE[self.fmt.max_bowler_overs]
        )

    def prev_over_runs(self, bowler_name: str) -> int:
        """
        Runs conceded by this bowler in their most recent completed over.
        Returns -1 if this is their first over (no history yet).
        """
        return self._prev_over_runs.get(bowler_name, -1)

    def last_bowler(self) -> Optional[str]:
        """Name of the bowler who bowled the previous over, or None."""
        return self._last_bowler

    def at_quota(self, bowler_name: str) -> bool:
        """True if this bowler has exhausted their bowling quota."""
        return self._quota.get(bowler_name, 0) >= self.fmt.max_bowler_overs

    def is_consecutive(self, bowler_name: str) -> bool:
        """True if this bowler bowled the previous over."""
        return bowler_name == self._last_bowler

    def quota_summary(self) -> Dict[str, Dict]:
        """
        Returns a dict of {bowler_name: {bowled, remaining, at_quota}}
        for all eligible bowlers.  Used for UI display and debug logging.
        """
        max_q = self.fmt.max_bowler_overs
        return {
            p["name"]: {
                "bowled":    self._quota.get(p["name"], 0),
                "remaining": self.overs_remaining(p["name"]),
                "at_quota":  self.at_quota(p["name"]),
            }
            for p in self._eligible_xi
        }

    # ------------------------------------------------------------------ #
    # State mutation                                                       #
    # ------------------------------------------------------------------ #

    def record_over_completion(self, bowler_name: str, runs_conceded: int) -> None:
        """
        Call this at the end of every over (after updating bowler_stats).

        Updates:
        - quota counter for the bowler
        - last_bowler tracker (for consecutive-over enforcement)
        - per-bowler previous-over run tally (for performance feedback)
        """
        self._quota[bowler_name] = self._quota.get(bowler_name, 0) + 1
        self._last_bowler = bowler_name
        self._prev_over_runs[bowler_name] = runs_conceded
        logger.debug(
            "BowlerManager: %s completed over — quota now %d/%d",
            bowler_name,
            self._quota[bowler_name],
            self.fmt.max_bowler_overs,
        )

    def reset(self, new_bowling_xi: list) -> None:
        """
        Reset all state for a new innings (called at innings transition).

        Parameters
        ----------
        new_bowling_xi: the XI now bowling in the second innings.
        """
        self._eligible_xi = [
            p for p in new_bowling_xi if p.get("will_bowl", False)
        ]
        self._quota = {p["name"]: 0 for p in self._eligible_xi}
        self._last_bowler = None
        self._prev_over_runs = {}
        logger.debug(
            "BowlerManager: reset for new innings, bowlers=%s",
            [p["name"] for p in self._eligible_xi]
        )

    # ------------------------------------------------------------------ #
    # Compatibility shim — expose the raw quota dict so that existing     #
    # match.py code reading `self.bowler_history` still works during the  #
    # transition period (Phase 3 will migrate those call-sites).          #
    # ------------------------------------------------------------------ #

    @property
    def bowler_history(self) -> Dict[str, int]:
        """Read-only view of quota dict (backward-compat alias)."""
        return dict(self._quota)
