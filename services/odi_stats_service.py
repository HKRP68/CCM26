"""ODI career records, kept apart from the short-format career table.

An innings of 21 overs or more is played under ODI rules
(``services.match_formats.ball_format_for_overs``), and such an innings is
written to ``odi_player_innings`` instead of ``player_game_stats`` — see
``services.player_stats_service._persist_odi_innings``. That table is one row
per match, innings, discipline and player, holding the raw live-state figures
as JSON, which is what makes a long-format record auditable: the per-innings
rows survive, and anything printed here is a fold over them rather than a
counter that can drift.

This module is the read side. It never writes, so it is safe to call from a
command handler, a card renderer or the website with a read-only session.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# A fifty and a hundred in a long-format innings mean the same thing they do
# anywhere else; they are named here so a future 40-over milestone rule has one
# place to change.
FIFTY, HUNDRED = 50, 100

_EMPTY_BATTING = {
    "innings": 0, "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "dots": 0,
    "outs": 0, "not_outs": 0, "fifties": 0, "hundreds": 0, "ducks": 0,
    "highest": 0, "highest_not_out": False,
}
_EMPTY_BOWLING = {
    "innings": 0, "balls": 0, "runs": 0, "wickets": 0, "maidens": 0,
    "dots": 0, "three_fers": 0, "five_fers": 0,
    "best_wickets": 0, "best_runs": 0,
}


def _figures(raw: Any) -> Dict[str, Any]:
    """One row's ``figures`` column as a dict (``{}`` when unreadable)."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        logger.warning("odi stats: unreadable figures column", exc_info=True)
        return {}
    return value if isinstance(value, dict) else {}


def _int(figures: Dict[str, Any], key: str) -> int:
    try:
        return int(figures.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def fold_batting(rows: Iterable[Any]) -> Dict[str, Any]:
    """Sum a player's ODI batting innings into one career record.

    ``rows`` are ``figures`` blobs (or already-parsed dicts) of the ``bat``
    rows. An innings counts once the batter has faced a ball or scored, which
    is the same test the short-format career table applies, so a batter who
    never got on strike does not collect an innings.
    """
    out = dict(_EMPTY_BATTING)
    for raw in rows:
        figures = _figures(raw)
        balls, runs = _int(figures, "balls"), _int(figures, "runs")
        is_out = bool(figures.get("out"))
        if not (balls or runs or is_out):
            continue
        out["innings"] += 1
        out["runs"] += runs
        out["balls"] += balls
        out["fours"] += _int(figures, "fours")
        out["sixes"] += _int(figures, "sixes")
        out["dots"] += _int(figures, "dots")
        if is_out:
            out["outs"] += 1
            if runs == 0:
                out["ducks"] += 1
        else:
            out["not_outs"] += 1
        if runs >= HUNDRED:
            out["hundreds"] += 1
        elif runs >= FIFTY:
            out["fifties"] += 1
        # The best score, and whether the batter was still there for it. A
        # repeat of the same score upgrades to not out, matching /stats.
        if runs > out["highest"]:
            out["highest"], out["highest_not_out"] = runs, not is_out
        elif runs == out["highest"] and not is_out:
            out["highest_not_out"] = True
    return out


def fold_bowling(rows: Iterable[Any]) -> Dict[str, Any]:
    """Sum a player's ODI bowling innings into one career record."""
    out = dict(_EMPTY_BOWLING)
    for raw in rows:
        figures = _figures(raw)
        balls = _int(figures, "balls")
        runs = _int(figures, "runs")
        wickets = _int(figures, "wickets")
        if not (balls or runs or wickets):
            continue
        out["innings"] += 1
        out["balls"] += balls
        out["runs"] += runs
        out["wickets"] += wickets
        out["maidens"] += _int(figures, "maidens")
        out["dots"] += _int(figures, "dots")
        if wickets >= 5:
            out["five_fers"] += 1
        elif wickets >= 3:
            out["three_fers"] += 1
        if wickets > out["best_wickets"] or (
                wickets == out["best_wickets"] and runs < out["best_runs"]):
            out["best_wickets"], out["best_runs"] = wickets, runs
    return out


# ── Derived figures ──────────────────────────────────────────────────
def overs_text(balls: int) -> str:
    """``balls`` as cricket overs — ``9.3``, or ``10`` on a whole over."""
    balls = max(0, int(balls or 0))
    return f"{balls // 6}.{balls % 6}" if balls % 6 else str(balls // 6)


def batting_average(batting: Dict[str, Any]) -> Optional[float]:
    """Runs per dismissal, or ``None`` while the batter has never been out."""
    return round(batting["runs"] / batting["outs"], 2) if batting.get("outs") else None


def strike_rate(batting: Dict[str, Any]) -> Optional[float]:
    return round(100 * batting["runs"] / batting["balls"], 2) if batting.get("balls") else None


def economy(bowling: Dict[str, Any]) -> Optional[float]:
    return round(6 * bowling["runs"] / bowling["balls"], 2) if bowling.get("balls") else None


def bowling_average(bowling: Dict[str, Any]) -> Optional[float]:
    return round(bowling["runs"] / bowling["wickets"], 2) if bowling.get("wickets") else None


def bowling_strike_rate(bowling: Dict[str, Any]) -> Optional[float]:
    return round(bowling["balls"] / bowling["wickets"], 2) if bowling.get("wickets") else None


def highest_text(batting: Dict[str, Any]) -> str:
    if not batting.get("innings"):
        return "-"
    return f"{batting['highest']}{'*' if batting.get('highest_not_out') else ''}"


def best_bowling_text(bowling: Dict[str, Any]) -> str:
    if not bowling.get("innings") or not bowling.get("best_wickets"):
        return "-"
    return f"{bowling['best_wickets']}/{bowling['best_runs']}"


def _num(value: Any, dash: str = "-") -> str:
    return dash if value is None else f"{value}"


# ── Queries ──────────────────────────────────────────────────────────
def _rows(session, *, player_ids=None, user_id=None):
    """The raw ``ODIPlayerInnings`` rows a lookup asks for.

    ``player_ids`` is one card's editions (or a single id); omit it for every
    player. ``user_id`` narrows to one owner; omit it to sum across owners.
    """
    from models import ODIPlayerInnings

    query = session.query(ODIPlayerInnings)
    if player_ids is not None:
        ids = [int(pid) for pid in player_ids]
        if not ids:
            return []
        query = query.filter(ODIPlayerInnings.player_id.in_(ids))
    if user_id is not None:
        query = query.filter(ODIPlayerInnings.user_id == int(user_id))
    return query.all()


def record(batting, bowling, *, matches=0, player_id=None, user_id=None):
    """A career record from folded batting and bowling, with its derived rates.

    One builder, so a per-player lookup, a whole-squad fold and a merge across
    a card's editions all print the same figures computed the same way.
    """
    return {
        "player_id": None if player_id is None else int(player_id),
        "user_id": None if user_id is None else int(user_id),
        "matches": int(matches or 0),
        "batting": batting,
        "bowling": bowling,
        "batting_average": batting_average(batting),
        "strike_rate": strike_rate(batting),
        "economy": economy(bowling),
        "bowling_average": bowling_average(bowling),
        "bowling_strike_rate": bowling_strike_rate(bowling),
        "highest": highest_text(batting),
        "best_bowling": best_bowling_text(bowling),
        "overs": overs_text(bowling["balls"]),
    }


def _fold_rows(rows, *, player_id=None, user_id=None):
    return record(
        fold_batting(r.figures for r in rows if r.discipline == "bat"),
        fold_bowling(r.figures for r in rows if r.discipline == "bowl"),
        matches=len({r.match_id for r in rows}),
        player_id=player_id, user_id=user_id)


def career(session, player_id: int, user_id: Optional[int] = None) -> Dict[str, Any]:
    """One player's ODI career record.

    With ``user_id`` the record covers that owner's copy of the card only,
    which is what ``/odistats`` prints; without it, every owner's ODI innings
    for that player are summed — the long-format twin of ``/gstats``.
    """
    return _fold_rows(_rows(session, player_ids=[player_id], user_id=user_id),
                      player_id=player_id, user_id=user_id)


def careers(session, player_ids, user_id: Optional[int] = None) -> Dict[str, Any]:
    """One record covering several players — every edition of a re-released card.

    A card that has been re-released shares one identity everywhere else in the
    game, so its ODI record is the sum over its editions, read in one query.
    """
    player_ids = [int(pid) for pid in player_ids or ()]
    if not player_ids:
        return record(dict(_EMPTY_BATTING), dict(_EMPTY_BOWLING), user_id=user_id)
    return _fold_rows(_rows(session, player_ids=player_ids, user_id=user_id),
                      player_id=player_ids[0], user_id=user_id)


def has_record(rec: Dict[str, Any]) -> bool:
    """True when there is anything worth printing."""
    return bool(rec["batting"]["innings"] or rec["bowling"]["innings"])


def squad_careers(session, user_id: int) -> Dict[int, Dict[str, Any]]:
    """Every player this owner has ODI innings for, keyed by ``player_id``.

    One query for a whole roster, so a squad view does not fan out into a
    lookup per card.
    """
    by_player: Dict[int, List[Any]] = {}
    for row in _rows(session, user_id=user_id):
        by_player.setdefault(int(row.player_id), []).append(row)
    return {player_id: _fold_rows(player_rows, player_id=player_id, user_id=user_id)
            for player_id, player_rows in by_player.items()}


# ── Display ──────────────────────────────────────────────────────────
def record_block(rec: Dict[str, Any]) -> str:
    """The two-column career block, in the layout ``/stats`` uses."""
    bat, bowl = rec["batting"], rec["bowling"]
    rows = [
        ("Inns", bat["innings"], "Inns", bowl["innings"]),
        ("Runs", bat["runs"], "Wickets", bowl["wickets"]),
        ("50s", bat["fifties"], "3-Fers", bowl["three_fers"]),
        ("100s", bat["hundreds"], "5-Fers", bowl["five_fers"]),
        ("4/6", f"{bat['fours']}/{bat['sixes']}", "Overs", rec["overs"]),
        ("Avg", _num(rec["batting_average"]), "Avg", _num(rec["bowling_average"])),
        ("SR", _num(rec["strike_rate"]), "Econ", _num(rec["economy"])),
        ("NO", bat["not_outs"], "SR", _num(rec["bowling_strike_rate"])),
        ("Ducks", bat["ducks"], "Maidens", bowl["maidens"]),
        ("HS", rec["highest"], "BBF", rec["best_bowling"]),
    ]
    lines = [f"{'🏏 BATTING':<20}{'🎯 BOWLING'}", "─" * 38]
    for bat_label, bat_value, bowl_label, bowl_value in rows:
        left = f"{bat_label}: {bat_value}"
        lines.append(f"{left:<20}{bowl_label}: {bowl_value}")
    return "\n".join(lines)
