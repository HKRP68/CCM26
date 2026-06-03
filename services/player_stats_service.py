"""Shared career player-stat persistence for every match mode.

The canonical career table is ``player_game_stats``.  Bot matches and Mini App
matches both keep the same live-state shape (``bat_stats``/``bowl_stats`` plus
first-innings snapshots), so this module centralizes the save logic and keeps
/wpm, /cm, /vsbot, and /playmatch in one stats system.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from models import PlayerGameStats


def _rid_key(roster_id: Any) -> Any:
    """Normalize roster-id keys that may be serialized as strings."""
    if isinstance(roster_id, str):
        try:
            return int(roster_id)
        except ValueError:
            return roster_id
    return roster_id


def _build_lookup(xi_list: Iterable[Dict[str, Any]], user_id: int | None) -> Dict[Any, Tuple[int, int | None]]:
    """Map roster_id -> (player_id, owning user_id) for a batting/bowling XI."""
    if not user_id:
        return {}
    lookup: Dict[Any, Tuple[int, int | None]] = {}
    for player in xi_list or []:
        roster_id = player.get("roster_id")
        player_id = player.get("player_id")
        if roster_id is None or player_id is None:
            continue
        lookup[_rid_key(roster_id)] = (player_id, user_id)
    return lookup


def _ensure_stats_row(session, user_id: int, player_id: int) -> PlayerGameStats:
    row = (session.query(PlayerGameStats)
           .filter(PlayerGameStats.user_id == user_id,
                   PlayerGameStats.player_id == player_id)
           .first())
    if not row:
        row = PlayerGameStats(user_id=user_id, player_id=player_id)
        session.add(row)
        session.flush()
    return row


def _update_batting(session, user_id: int, player_id: int, batting: Dict[str, Any]) -> bool:
    """Apply one innings' batting score to a career row."""
    if not batting or batting.get("balls", 0) == 0:
        return False

    row = _ensure_stats_row(session, user_id, player_id)
    runs = batting.get("runs", 0)
    is_out = batting.get("out", False)

    row.bat_inns += 1
    row.runs += runs
    row.balls_faced += batting.get("balls", 0)
    row.fours += batting.get("fours", 0)
    row.sixes += batting.get("sixes", 0)

    if runs >= 100:
        row.hundreds += 1
    elif runs >= 50:
        row.fifties += 1
    if is_out:
        row.times_out += 1
        if runs == 0:
            row.ducks += 1
    if runs > row.highest_score:
        row.highest_score = runs
        row.highest_score_not_out = not is_out
    elif runs == row.highest_score and not is_out:
        row.highest_score_not_out = True
    return True


def _update_bowling(session, user_id: int, player_id: int, bowling: Dict[str, Any]) -> bool:
    """Apply one innings' bowling figures to a career row."""
    if not bowling:
        return False
    balls = bowling.get("balls", 0)
    wickets = bowling.get("wickets", 0)
    runs = bowling.get("runs", 0)
    if balls == 0 and wickets == 0 and runs == 0:
        return False

    row = _ensure_stats_row(session, user_id, player_id)
    row.bowl_inns += 1
    row.wickets_taken += wickets
    row.runs_conceded += runs
    row.balls_bowled += balls
    row.overs_bowled = round(row.balls_bowled / 6, 1)

    if wickets >= 5:
        row.five_fers += 1
    elif wickets >= 3:
        row.three_fers += 1

    if wickets > row.best_bowl_wickets or (
            wickets == row.best_bowl_wickets and runs < row.best_bowl_runs):
        row.best_bowl_wickets = wickets
        row.best_bowl_runs = runs
    return True


def persist_player_game_stats(session, state: Dict[str, Any]) -> Dict[str, int]:
    """Persist batting and bowling career stats from any full match state.

    The state shape is shared by /playmatch, /vsbot, /cm, and /wpm.  If a bot
    Telegram match has already saved first-innings stats at the innings break,
    ``state['inn1_stats_saved']`` prevents those rows being counted twice.

    Returns a small count summary for logging/tests.  The caller owns commit and
    rollback so this can be composed with match finalization.
    """
    if not state:
        return {"batting": 0, "bowling": 0}

    # If a match is abandoned in innings 1, snapshot the active innings so the
    # same persistence path can handle it.
    if state.get("innings") == 1 and not state.get("inn1_bat_team_id"):
        state["inn1_bat_stats"] = dict(state.get("bat_stats", {}))
        state["inn1_bowl_stats"] = dict(state.get("bowl_stats", {}))
        state["inn1_bat_team_id"] = state.get("bat_team_id")
        state["inn1_bowl_team_id"] = state.get("bowl_team_id")
        state["inn1_bat_xi"] = list(state.get("bat_xi", []))
        state["inn1_bowl_xi"] = list(state.get("bowl_xi", []))

    skip_saved_inn1 = bool(state.get("inn1_stats_saved"))
    bat_lookup_1 = {} if skip_saved_inn1 else _build_lookup(
        state.get("inn1_bat_xi", []), state.get("inn1_bat_team_id"))
    bowl_lookup_1 = {} if skip_saved_inn1 else _build_lookup(
        state.get("inn1_bowl_xi", []), state.get("inn1_bowl_team_id"))

    if state.get("innings", 1) >= 2:
        bat_lookup_2 = _build_lookup(state.get("bat_xi", []), state.get("bat_team_id"))
        bowl_lookup_2 = _build_lookup(state.get("bowl_xi", []), state.get("bowl_team_id"))
    else:
        bat_lookup_2 = {}
        bowl_lookup_2 = {}

    counts = {"batting": 0, "bowling": 0}

    for stats_map, lookup in (
            (state.get("inn1_bat_stats", {}), bat_lookup_1),
            (state.get("bat_stats", {}), bat_lookup_2)):
        for roster_id, batting in (stats_map or {}).items():
            player = lookup.get(_rid_key(roster_id))
            if player and _update_batting(session, player[1], player[0], batting):
                counts["batting"] += 1

    for stats_map, lookup in (
            (state.get("inn1_bowl_stats", {}), bowl_lookup_1),
            (state.get("bowl_stats", {}), bowl_lookup_2)):
        for roster_id, bowling in (stats_map or {}).items():
            player = lookup.get(_rid_key(roster_id))
            if player and _update_bowling(session, player[1], player[0], bowling):
                counts["bowling"] += 1

    return counts
