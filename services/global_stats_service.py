"""Career stats for a player summed over every owner — powers /gstats.

``player_game_stats`` holds one row per (owner, card) and is written by every
match mode there is — /playmatch, /letsplay, /wpm, /cm, /vsbot, Challenge
League, tours, super overs. Summing a player's rows therefore gives that
cricketer's record across the whole game, in all match types, which is what
/gstats reports. /stats stays personal: your card, your numbers.

The aggregate is done in SQL (one grouped query) so a card owned by thousands
of managers costs the same as one owned by three.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Sequence

from sqlalchemy import func

from models import Player, PlayerGameStats, User

logger = logging.getLogger(__name__)

# Columns that are plain running totals — summing them is the whole aggregate.
_SUM_COLUMNS = (
    "potm",
    "bat_inns", "runs", "fifties", "hundreds", "fours", "sixes",
    "balls_faced", "times_out", "ducks",
    "bowl_inns", "wickets_taken", "runs_conceded", "balls_bowled",
    "three_fers", "five_fers", "hattricks",
)


def _empty_totals() -> Dict[str, object]:
    totals = {name: 0 for name in _SUM_COLUMNS}
    totals.update({
        "owners": 0,
        "highest_score": 0,
        "highest_score_not_out": False,
        "best_bowl_wickets": 0,
        "best_bowl_runs": 0,
        "has_best_bowl": False,
    })
    return totals


def _rows_query(session, player_ids: Sequence[int]):
    return (session.query(PlayerGameStats)
            .filter(PlayerGameStats.player_id.in_(list(player_ids))))


def aggregate_stats(session, player_ids: Sequence[int]) -> Dict[str, object]:
    """Sum every owner's career rows for these card ids into one record.

    Counting totals add up; records do not — the highest score is the best of
    the per-owner bests, and the best bowling is the most wickets, then the
    fewest runs at that many wickets.
    """
    totals = _empty_totals()
    if not player_ids:
        return totals

    ids = list(player_ids)
    columns = [func.sum(getattr(PlayerGameStats, name)) for name in _SUM_COLUMNS]
    row = (session.query(func.count(func.distinct(PlayerGameStats.user_id)), *columns)
           .filter(PlayerGameStats.player_id.in_(ids))
           .one())
    totals["owners"] = int(row[0] or 0)
    for name, value in zip(_SUM_COLUMNS, row[1:]):
        totals[name] = int(value or 0)

    # Highest score: the best innings anyone played, keeping its not-out flag.
    # A not-out knock wins the tie so "84*" is never printed as "84".
    best_bat = (_rows_query(session, ids)
                .filter(PlayerGameStats.bat_inns > 0)
                .order_by(PlayerGameStats.highest_score.desc(),
                          PlayerGameStats.highest_score_not_out.desc())
                .first())
    if best_bat:
        totals["highest_score"] = int(best_bat.highest_score or 0)
        totals["highest_score_not_out"] = bool(best_bat.highest_score_not_out)

    # Best bowling: most wickets, then cheapest at that haul.
    best_bowl = (_rows_query(session, ids)
                 .filter(PlayerGameStats.best_bowl_wickets > 0)
                 .order_by(PlayerGameStats.best_bowl_wickets.desc(),
                           PlayerGameStats.best_bowl_runs.asc())
                 .first())
    if best_bowl:
        totals["best_bowl_wickets"] = int(best_bowl.best_bowl_wickets or 0)
        totals["best_bowl_runs"] = int(best_bowl.best_bowl_runs or 0)
        totals["has_best_bowl"] = True

    return totals


def derived(totals: Dict[str, object]) -> Dict[str, object]:
    """Averages, strike rates and economy computed from the summed totals.

    Always recomputed from the sums — averaging the owners' averages would
    weight a manager with two innings the same as one with two hundred.
    """
    runs = totals.get("runs", 0)
    outs = totals.get("times_out", 0)
    balls = totals.get("balls_faced", 0)
    conceded = totals.get("runs_conceded", 0)
    wickets = totals.get("wickets_taken", 0)
    bowled = totals.get("balls_bowled", 0)
    return {
        "bat_avg": round(runs / outs, 2) if outs else 0.0,
        "bat_sr": round(runs * 100 / balls, 2) if balls else 0.0,
        "bowl_avg": round(conceded / wickets, 2) if wickets else 0.0,
        "bowl_sr": round(bowled / wickets, 2) if wickets else 0.0,
        "economy": round(conceded * 6 / bowled, 2) if bowled else 0.0,
        "overs": f"{bowled // 6}.{bowled % 6}" if bowled else "0.0",
    }


def hs_str(totals: Dict[str, object]) -> str:
    if not totals.get("bat_inns"):
        return "-"
    return f"{totals['highest_score']}{'*' if totals['highest_score_not_out'] else ''}"


def bbf_str(totals: Dict[str, object]) -> str:
    if not totals.get("has_best_bowl"):
        return "-"
    return f"{totals['best_bowl_wickets']}/{totals['best_bowl_runs']}"


def top_owners(session, player_ids: Sequence[int], metric: str = "runs",
               limit: int = 3) -> List[Dict[str, object]]:
    """The managers with the best record using this card.

    ``metric`` is ``runs`` or ``wickets_taken``. Rows scoring zero are left out
    — "top scorer: nobody, 0 runs" is noise, not information.
    """
    if not player_ids or metric not in ("runs", "wickets_taken"):
        return []
    column = getattr(PlayerGameStats, metric)
    rows = (session.query(User, func.sum(column).label("value"))
            .join(PlayerGameStats, PlayerGameStats.user_id == User.id)
            .filter(PlayerGameStats.player_id.in_(list(player_ids)))
            .group_by(User.id)
            .having(func.sum(column) > 0)
            .order_by(func.sum(column).desc())
            .limit(limit)
            .all())
    return [{"user": user, "value": int(value or 0)} for user, value in rows]


def per_version(session, versions: Sequence[Player]) -> List[Dict[str, object]]:
    """One aggregate per edition, for cards that have variants."""
    return [{"player": version, "totals": aggregate_stats(session, [version.id])}
            for version in versions]
