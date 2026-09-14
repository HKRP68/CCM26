"""Trait Rating Boost — what equipped traits are worth on the team card.

``services.trait_engine`` is the *simulation* half of the trait system: it nudges
the per-ball probability weights while a match is running. This module is the
*card* half. It answers one question — "how much Overall Rating do this player's
(or this XI's) traits add?" — and it answers it the same way for every screen
that asks, so the number a captain reads on the Playing XI card before the toss
is the number the live board shows during the match.

The ladder doubles with level (``config.TRAIT_RATING_BONUS``)::

    Lv.1 +0.2   Lv.2 +0.4   Lv.3 +0.8   Lv.4 +1.6   Lv.5 +3.2

Two ceilings keep it honest, both from ``config``:

* per card — traits on one player stack with the ball engine's own diminishing
  returns (``TRAIT_STACK_WEIGHTS``, strongest first) and then cap at
  ``TRAIT_RATING_BONUS_MAX_PER_PLAYER``;
* per XI — the team boost is the *mean* of the eleven cards' boosts, capped at
  ``TRAIT_RATING_BONUS_MAX_PER_TEAM``, which is sized under
  ``STATS_FAIRNESS_OVR_GAP`` so trait investment can never on its own open a gap
  wide enough to read as stat farming.

WHAT THIS DELIBERATELY DOES NOT DO
──────────────────────────────────
It does not feed the ball engine. Traits already move the simulation once, per
delivery, through ``trait_engine.apply_traits``; adding the boost to the ratings
that engine reads would pay the same trait out twice. The boost is a *card*
number — the visible, comparable measure of a squad's trait investment — and the
one number it must not silently move is the anti stat-farming gap, which stays
on the printed card ratings (see ``TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS``).

TRAIT SHAPES THIS ACCEPTS
─────────────────────────
Every caller that already talks to the trait engine passes trait dicts shaped
``{"effect_key", "level", "display_name", "emoji", "category"}``. Only ``level``
is read here, and it is read defensively: a missing, null or junk level counts
as Lv.1 rather than raising, because a card screen must never be the thing that
takes a match setup down.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from config import (
    TRAIT_RATING_BONUS,
    TRAIT_RATING_BONUS_MAX_PER_PLAYER,
    TRAIT_RATING_BONUS_MAX_PER_TEAM,
    TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS,
    TRAIT_STACK_WEIGHTS,
)

logger = logging.getLogger(__name__)

#: Emoji the boost is labelled with everywhere it is shown.
BOOST_EMOJI = "⚡"


# ──────────────────────────────────────────────────────────────────────
# The ladder
# ──────────────────────────────────────────────────────────────────────

def _clamp_level(level: Any) -> int:
    """Read a trait level defensively. Anything unusable counts as Lv.1."""
    try:
        value = int(level)
    except (TypeError, ValueError):
        return 1
    if not TRAIT_RATING_BONUS:
        return 1
    return max(min(TRAIT_RATING_BONUS), min(max(TRAIT_RATING_BONUS), value))


def level_bonus(level: Any) -> float:
    """Rating points one trait of ``level`` is worth, before stacking."""
    return float(TRAIT_RATING_BONUS.get(_clamp_level(level), 0.0))


def _stack_weight(index: int) -> float:
    """Diminishing returns by slot — the ball engine's own weights."""
    if not TRAIT_STACK_WEIGHTS:
        return 1.0
    if index < len(TRAIT_STACK_WEIGHTS):
        return float(TRAIT_STACK_WEIGHTS[index])
    return float(TRAIT_STACK_WEIGHTS[-1])


def player_bonus(traits: Optional[Iterable[Dict[str, Any]]]) -> float:
    """Rating points a single card's traits add, stacked and capped.

    Traits are ordered by level descending first, exactly as
    ``trait_engine.apply_traits`` orders them, so the highest level always takes
    the full-strength stack slot. Without that the boost would depend on the
    order the rows came back from the database — two captains with identical
    squads would read different Team Overalls.
    """
    levels: List[int] = []
    for trait in traits or ():
        if isinstance(trait, dict):
            levels.append(_clamp_level(trait.get("level", 1)))
        else:  # a bare level, which some callers find easier to build
            levels.append(_clamp_level(trait))
    if not levels:
        return 0.0
    levels.sort(reverse=True)
    total = sum(level_bonus(lv) * _stack_weight(i) for i, lv in enumerate(levels))
    return round(min(total, float(TRAIT_RATING_BONUS_MAX_PER_PLAYER)), 2)


# ──────────────────────────────────────────────────────────────────────
# A whole XI
# ──────────────────────────────────────────────────────────────────────

def _traits_of(player: Any) -> List[Dict[str, Any]]:
    if not isinstance(player, dict):
        return []
    traits = player.get("traits")
    return list(traits) if isinstance(traits, (list, tuple)) else []


def _base_rating(player: Any) -> float:
    """The printed card rating of one XI entry.

    ``card_rating`` wins where it exists: some modes adjust the engine's
    ``rating`` before the first ball (see ``cipl_match.display_rating``), and the
    boost must be quoted against the number the captain actually sees.
    """
    if not isinstance(player, dict):
        return 0.0
    value = player.get("card_rating")
    if value is None:
        value = player.get("rating")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def team_base_overall(xi: Optional[Sequence[Dict[str, Any]]]) -> int:
    """Team Overall from the printed card ratings — no boost, rounded."""
    ratings = [_base_rating(p) for p in (xi or ()) if isinstance(p, dict)]
    return round(sum(ratings) / len(ratings)) if ratings else 0


def team_bonus(xi: Optional[Sequence[Dict[str, Any]]],
               bonus_by_index: Optional[Dict[int, float]] = None) -> float:
    """Rating points this XI's traits add to its Team Overall.

    Team Overall is an *average* of eleven cards, so the boost is the average of
    the eleven cards' boosts — one Lv.5 trait in an XI lifts the team by 3.2/11,
    not by 3.2. Kitting out the whole XI is what moves the number, which is the
    right thing for the squad-wide trait budget to be spent on.

    ``bonus_by_index`` lets a caller that loaded traits separately (a card built
    from ORM rows rather than engine dicts) supply per-slot boosts by position.
    """
    players = [p for p in (xi or ()) if isinstance(p, dict)] if xi else []
    if bonus_by_index is not None:
        count = max(len(players), (max(bonus_by_index) + 1) if bonus_by_index else 0)
        if not count:
            return 0.0
        total = sum(float(bonus_by_index.get(i, 0.0)) for i in range(count))
    else:
        if not players:
            return 0.0
        count = len(players)
        total = sum(player_bonus(_traits_of(p)) for p in players)
    return round(min(total / count, float(TRAIT_RATING_BONUS_MAX_PER_TEAM)), 2)


def team_effective_overall(xi: Optional[Sequence[Dict[str, Any]]]) -> float:
    """Team Overall with the trait boost applied, to one decimal."""
    return round(team_base_overall(xi) + team_bonus(xi), 1)


def team_rating_card(xi: Optional[Sequence[Dict[str, Any]]],
                     bonus_by_index: Optional[Dict[int, float]] = None
                     ) -> Dict[str, float]:
    """``{base, bonus, effective}`` for one XI — the shape every screen shows."""
    base = team_base_overall(xi)
    bonus = team_bonus(xi, bonus_by_index=bonus_by_index)
    return {"base": base, "bonus": bonus, "effective": round(base + bonus, 1)}


def annotate_xi(xi: Optional[Sequence[Dict[str, Any]]]
                ) -> Optional[Sequence[Dict[str, Any]]]:
    """Stamp ``trait_bonus`` and ``effective_rating`` on each XI entry, in place.

    Returns the same list so it can be used inline. Entries without traits get
    ``0.0`` and their own rating rather than being skipped, so downstream code
    can read the keys unconditionally.
    """
    for player in (xi or ()):
        if not isinstance(player, dict):
            continue
        bonus = player_bonus(_traits_of(player))
        player["trait_bonus"] = bonus
        player["effective_rating"] = round(_base_rating(player) + bonus, 1)
    return xi


# ──────────────────────────────────────────────────────────────────────
# Loading traits for rosters the caller only has ORM rows for
# ──────────────────────────────────────────────────────────────────────

def roster_traits(session, roster_ids: Optional[Iterable[Any]]
                  ) -> Dict[int, List[Dict[str, Any]]]:
    """``{roster_id: [trait dicts]}`` for a batch of ``UserRoster`` ids.

    One query for the whole XI rather than one per card: the Playing XI card is
    rebuilt on every ``/change``, and eleven round trips per redraw is a cost
    nobody needs to pay. Any failure reads as "no traits" — a card screen must
    degrade, not raise.
    """
    ids = []
    for rid in roster_ids or ():
        try:
            ids.append(int(rid))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    try:
        from models import PlayerTrait, Trait
        rows = (session.query(PlayerTrait, Trait)
                .join(Trait, PlayerTrait.trait_id == Trait.id)
                .filter(PlayerTrait.roster_id.in_(ids),
                        Trait.is_active == True)  # noqa: E712
                .all())
    except Exception:
        logger.exception("trait rating: roster trait fetch failed")
        return {}
    out: Dict[int, List[Dict[str, Any]]] = {}
    for pt, trait in rows:
        out.setdefault(int(pt.roster_id), []).append({
            "effect_key": trait.effect_key, "level": pt.level,
            "display_name": trait.name, "emoji": trait.emoji,
            "category": trait.category,
        })
    return out


def roster_bonus_map(session, roster_ids: Optional[Iterable[Any]]
                     ) -> Dict[int, float]:
    """``{roster_id: boost}`` for a batch of ``UserRoster`` ids."""
    return {rid: player_bonus(traits)
            for rid, traits in roster_traits(session, roster_ids).items()}


# ──────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────

def counts_for_fairness() -> bool:
    """True when the boost is part of the anti stat-farming Team Overall gap."""
    return bool(TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS)


def fairness_overall(xi: Optional[Sequence[Dict[str, Any]]]) -> float:
    """The Team Overall the stat-farming gate should compare.

    Base by default; effective only when the operator has opted in. Every gate
    reads this rather than deciding for itself, so the switch means one thing
    everywhere.
    """
    return (team_effective_overall(xi) if counts_for_fairness()
            else float(team_base_overall(xi)))


def format_bonus(bonus: float) -> str:
    """``+1.2`` — always signed, always one decimal, ``0.0`` reads as ``+0.0``."""
    return f"{float(bonus or 0):+.1f}"


def format_player_rating(base: Any, bonus: float) -> str:
    """``84`` or ``84 ⚡+1.6`` — the per-card rating for an XI list.

    An unboosted card keeps the exact text it had before the boost existed, so a
    squad with no traits sees no new noise on its XI card.
    """
    try:
        base_text = f"{int(float(base or 0))}"
    except (TypeError, ValueError):
        base_text = "0"
    if not bonus:
        return base_text
    return f"{base_text} {BOOST_EMOJI}{format_bonus(bonus)}"


def format_team_line(label_a: str, card_a: Dict[str, float],
                     label_b: str, card_b: Dict[str, float]) -> Optional[str]:
    """The ``⚡ Trait Boost`` line for a two-sided card, or None when neither
    side has any traits equipped (nothing to say — so nothing is said)."""
    if not (card_a.get("bonus") or card_b.get("bonus")):
        return None
    return (f"{BOOST_EMOJI} <b>Trait Boost:</b> "
            f"{label_a} {card_a['base']} → <b>{card_a['effective']:.1f}</b> "
            f"({format_bonus(card_a['bonus'])}) · "
            f"{label_b} {card_b['base']} → <b>{card_b['effective']:.1f}</b> "
            f"({format_bonus(card_b['bonus'])})")


def top_contributors(xi: Optional[Sequence[Dict[str, Any]]], limit: int = 3
                     ) -> List[Tuple[str, float]]:
    """``[(name, boost), …]`` for the cards carrying this XI's boost."""
    rows = []
    for player in (xi or ()):
        if not isinstance(player, dict):
            continue
        bonus = player_bonus(_traits_of(player))
        if bonus:
            rows.append((str(player.get("name") or "Player"), bonus))
    rows.sort(key=lambda r: (-r[1], r[0]))
    return rows[:limit]
