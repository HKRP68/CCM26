"""Rain stops play, and the target is revised.

Until now nothing in an interactive match could shorten it: the conditions
engine changes the cloud, the wind and the dew, and `docs/conditions-engine-v4`
says so plainly — there is no rain, nothing stops play or sets a revised
target. A 20-over game can live with that. A fifty-over game cannot: a
rain-reduced chase and a revised target are most of what makes the long format
feel like the long format.

This module adds it for ODI-rules matches only (21 to 50 overs — see
``services.match_formats``). Short formats are left exactly as they were.

The maths is not new. ``engine.dls`` already holds the Duckworth–Lewis
resource curve the bot uses, and ``engine.sim.rain_dls`` already turns it into
targets for ``/sim ODI``; this is the same two functions driving an
interactive match, so the auto-sim and the over-by-over game cannot disagree
about what a stoppage is worth.

What a stoppage does:

* **First innings** — both sides lose the overs, and the chasing side's target
  is scaled by the ratio of resources (with the G50 top-up when the chase ends
  up with *more* resources than the side that batted first).
* **Second innings** — only the chase is shortened, and the target is revised
  by ``engine.dls.revised_target``.

The innings length lives in ``state["overs"]``, which is what
``cipl_match.total_balls`` reads, so shortening an innings is one write: the
innings end, the chase maths, the phases, the par curve, the bowling quota and
every surface follow from it. ``state["scheduled_overs"]`` keeps the length
the match was scheduled for, because a scorecard should still be able to say
a fifty-over match was cut to thirty-two.

Rain never washes a match out. Each side keeps at least
:data:`MIN_OVERS_PER_SIDE`, and a stoppage that cannot fit inside that is a
shower that costs nothing.

Pure except for the env read in :func:`enabled` — no Telegram, no DB.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: The fewest overs a side must get. Below this a limited-overs match is not a
#: contest, so the stoppage is reduced to fit rather than cutting further.
MIN_OVERS_PER_SIDE = 20

#: Per-over chance of a stoppage, by what the sky is doing. The conditions
#: engine's own weather values (``services.pitch_report.WEATHERS``) are the
#: keys; anything not listed — clear, hot and dry, windy — cannot rain.
#:
#: These are per OVER, and a match has about ninety eligible ones, so they
#: compound hard: the first draft used 0.016 for a damp sky and interrupted 45
#: of 60 matches, which is not weather, it is a climate. At these numbers the
#: wettest sky at the wettest ground stops play in roughly one match in five,
#: and across the seven weathers the engine draws from, about one ODI in thirty
#: sees rain at all. Rain should be an event people talk about, not a tax.
WEATHER_CHANCE = {
    "Light Rain Earlier": 0.0013,
    "Overcast": 0.0009,
    "Humid": 0.0004,
    "Partly Cloudy": 0.00015,
}

#: How many overs one stoppage costs, and how often. A short break is the
#: common case; the long one is the match that gets talked about.
LOSS_WEIGHTS = ((2, 40), (3, 22), (4, 14), (6, 10), (8, 7), (12, 5), (18, 2))

#: At most this many stoppages per innings, so a wet night cannot turn into a
#: sequence of interruptions that leaves nothing to play.
MAX_STOPPAGES_PER_INNINGS = 2

#: The Duckworth-Lewis G50 constant — the average a side scores in a full
#: 50-over innings — used for the top-up when the chasing side ends up with
#: more resources than the side that batted first. The same value
#: ``config/sim_engine.json`` gives the ODI format for /sim.
G50 = 245.0

#: The powerplay and the last over are left alone: a stoppage in the first few
#: overs is indistinguishable from a delayed start, and one with a single over
#: to go only denies the chase its finish.
SKIP_FIRST_OVERS = 5
SKIP_LAST_OVERS = 1


def enabled():
    """``CMU_RAIN_ENABLED`` — on by default, like the ODI experiment itself."""
    return os.getenv("CMU_RAIN_ENABLED", "1").strip().lower() in {"1", "yes", "true", "on"}


def applies(state):
    """True for a match rain can interrupt — an ODI-rules match, not practice.

    A Super Over and a bot practice match are left dry: one is six balls, and
    the other is a player learning the game, where losing twelve overs to the
    weather teaches nothing.
    """
    from services import odi_approach
    if not enabled() or not odi_approach.is_odi(state):
        return False
    if state.get("standalone_super_over") or state.get("is_bot_match"):
        return False
    return True


def scheduled_overs(state):
    """The length the match was scheduled for, before any stoppage."""
    return int(state.get("scheduled_overs") or state.get("overs") or 0)


def chance(state):
    """The per-over probability of a stoppage, 0.0 when it cannot rain."""
    if not applies(state):
        return 0.0
    conditions = state.get("conditions") or {}
    base = WEATHER_CHANCE.get(str(conditions.get("weather") or ""), 0.0)
    if not base:
        return 0.0
    # A ground's own climate is the other half of it: the engine's stadium data
    # carries a rain chance per venue, and a dry venue in cloud is not Colombo.
    climate = (conditions.get("climate") or {}) if isinstance(conditions, dict) else {}
    venue = climate.get("rainChance")
    if venue is None:
        return base
    try:
        return base * max(0.2, min(2.0, float(venue) / 30.0))
    except (TypeError, ValueError):
        return base


def window(state):
    """True when the over just finished is one a stoppage may follow."""
    over = int(state.get("current_over") or 1)
    total = int(state.get("overs") or 0)
    return SKIP_FIRST_OVERS < over <= total - SKIP_LAST_OVERS


def stoppages(state, innings=None):
    """How many stoppages this innings has already had."""
    innings = state.get("innings", 1) if innings is None else innings
    return sum(1 for r in (state.get("rain") or [])
               if int(r.get("innings") or 0) == int(innings))


def due(state):
    """True when play may be interrupted right now (before the roll)."""
    return (applies(state) and window(state)
            and stoppages(state) < MAX_STOPPAGES_PER_INNINGS
            and overs_losable(state) > 0)


def overs_losable(state):
    """The most overs a stoppage could take without breaking the floor."""
    from engine.sim import rain_dls
    overs = int(state.get("overs") or 0)
    bowled = int(state.get("current_over") or 1) - 1
    floor = min(MIN_OVERS_PER_SIDE, max(1, scheduled_overs(state) // 2))
    return max(0, rain_dls.cap_overs_lost(overs, bowled, overs, floor))


def roll(state, rng):
    """``overs_lost`` for this over's weather check — 0 for no stoppage.

    ``rng`` is any object with ``random()`` and ``choices()``; the ball loop's
    match-local stream is what the live match passes, so a replay rains in the
    same places.
    """
    if not due(state):
        return 0
    if rng.random() >= chance(state):
        return 0
    losses = [n for n, _w in LOSS_WEIGHTS]
    weights = [w for _n, w in LOSS_WEIGHTS]
    wanted = rng.choices(losses, weights)[0]
    return min(wanted, overs_losable(state))


def apply(state, overs_lost):
    """Shorten the innings and revise the target. Returns the record, or None.

    The record is appended to ``state["rain"]`` and is what the chat announces:
    ``{innings, over, overs_lost, new_overs, target, previous_target}``.
    """
    from engine.sim import rain_dls

    overs_lost = int(overs_lost or 0)
    if overs_lost <= 0 or not applies(state):
        return None
    # Clamp here as well as in :func:`roll`. This is the public door — the
    # live handler, a test, an admin tool — and an unclamped number wrote a
    # negative innings length, which every surface then read as an innings
    # that was already over.
    overs_lost = min(overs_lost, overs_losable(state))
    if overs_lost <= 0:
        return None
    from services import cipl_match

    innings = int(state.get("innings", 1))
    overs = int(state["overs"])
    bowled = cipl_match.balls_bowled(state) / 6.0
    wickets = int(state.get("total_wickets") or 0)
    state.setdefault("scheduled_overs", overs)
    new_overs = overs - overs_lost
    previous_target = state.get("target")

    if innings == 1:
        # Both sides get the shorter innings; the target is scaled when the
        # first innings ends (see :func:`first_innings_target`), because the
        # score it is scaled from is not final yet.
        #
        # The resource slice this stoppage costs is banked NOW, while the
        # overs left and the wickets down are still the ones it happened at.
        # A second stoppage adds its own slice rather than replacing the
        # first's — overwriting it was a bug that forgot the earlier loss
        # entirely and set a target as if only the last shower had happened.
        state["overs"] = new_overs
        cut = state.setdefault("rain_inn1", {
            "scheduled_overs": scheduled_overs(state),
            "overs_lost": 0, "resource_lost": 0.0, "stoppages": 0,
            "first_overs_at_stop": round(bowled, 1),
            "first_wickets_at_stop": wickets})
        cut["overs_lost"] += overs_lost
        cut["resource_lost"] += resource_slice(
            scheduled_overs(state), overs - bowled, overs_lost, wickets)
        cut["stoppages"] += 1
        target = None
    else:
        state["overs"] = new_overs
        target = rain_dls.second_innings_target(
            int(state.get("inn1_runs") or 0), overs, bowled, overs_lost, wickets)
        state["target"] = target
        state["dls_target"] = target

    record = {"innings": innings, "over": int(state.get("current_over") or 1) - 1,
              "overs_lost": overs_lost, "new_overs": new_overs,
              "target": target, "previous_target": previous_target,
              "runs": int(state.get("total_runs") or 0), "wickets": wickets}
    state.setdefault("rain", []).append(record)
    logger.info("rain: match %s innings %s lost %s overs (now %s); target %s",
                state.get("match_id"), innings, overs_lost, new_overs, target)
    return record


def resource_slice(scheduled, overs_left_before, overs_lost, wickets):
    """The percentage of a full innings one stoppage takes away.

    The Duckworth-Lewis resource curve, through ``engine.dls`` — the same
    primitive ``engine.sim.rain_dls`` uses, so the interactive match and
    ``/sim`` price a stoppage identically.
    """
    from engine import dls
    before = max(0.0, float(overs_left_before))
    after = max(0.0, before - int(overs_lost))
    return (dls.resource_pct(before, wickets, scheduled)
            - dls.resource_pct(after, wickets, scheduled))


def first_innings_target(state):
    """The revised target after a first innings that lost overs, or ``None``.

    Called at the innings break, once the first innings' score is final.
    ``cipl_match.end_first_innings`` sets ``score + 1``; this replaces it with
    the resource-scaled par when the innings was cut.

    Team 1's resources are a full innings minus every slice its stoppages took
    (banked in :func:`apply`, so several showers add up). Team 2's are a full
    innings of the reduced length with all ten wickets. Where team 2 ends up
    with MORE resources than team 1 had — an innings cut late, after the side
    batting first had already spent most of its own — the G50 top-up applies,
    exactly as ``engine.sim.rain_dls`` does it for /sim.
    """
    from engine import dls

    cut = state.get("rain_inn1")
    if not cut:
        return None
    try:
        score = int(state.get("inn1_runs") or 0)
        scheduled = int(cut["scheduled_overs"])
        reduced = int(state.get("overs") or scheduled)
        r1 = max(1e-9, 100.0 - float(cut.get("resource_lost") or 0.0))
        r2 = dls.resource_pct(reduced, 0, scheduled)
        if r2 <= r1:
            par = score * r2 / r1
        else:
            par = score + G50 * (scheduled / 50.0) * (r2 - r1) / 100.0
        return max(1, int(par) + 1)
    except Exception:
        logger.exception("rain: could not revise the first-innings target")
        return None


def par_now(state):
    """DLS par for the chasing side at this moment, or ``None``.

    What the chase would need if the rain came again and did not stop — the
    number a captain in a wet second innings is actually playing against.
    """
    from engine.sim import rain_dls
    from services import cipl_match

    if int(state.get("innings", 1)) != 2 or not state.get("rain"):
        return None
    try:
        return rain_dls.par_score(
            int(state.get("inn1_runs") or 0), int(state["overs"]),
            cipl_match.balls_bowled(state) / 6.0,
            int(state.get("total_wickets") or 0))
    except Exception:
        logger.exception("rain: could not compute the DLS par")
        return None


# ── What the chat says ───────────────────────────────────────────────
def announcement(state, record):
    """The interruption, as the lines the chat posts."""
    if not record:
        return []
    overs_lost = record["overs_lost"]
    lines = [
        f"🌧️ <b>Rain stops play</b> — {overs_lost} over"
        f"{'' if overs_lost == 1 else 's'} lost.",
        f"⏱️ This innings is now <b>{record['new_overs']} overs</b> "
        f"(scheduled {scheduled_overs(state)}).",
    ]
    quota = max(1, -(-record["new_overs"] // 5))
    lines.append(f"🎳 Each bowler may now bowl <b>{quota}</b>.")
    if record["innings"] == 1:
        lines.append("<i>Both sides get the shorter innings. The target is "
                     "set by DLS at the break.</i>")
    elif record["target"] is not None:
        previous = record["previous_target"]
        moved = (f" (was {previous})" if previous and previous != record["target"]
                 else "")
        lines.append(f"🎯 <b>Revised target: {record['target']}</b>{moved} "
                     f"from {record['new_overs']} overs.")
    return lines


def break_lines(state, target):
    """The DLS note on the innings break card, or ``[]``."""
    cut = state.get("rain_inn1")
    if not cut or target is None:
        return []
    lost = int(cut.get("overs_lost") or 0)
    return [
        f"🌧️ <i>{lost} over{'' if lost == 1 else 's'} lost to rain — "
        f"{state.get('inn1_bat_team') or 'the first innings'} batted "
        f"{state['overs']} of {cut['scheduled_overs']}.</i>",
        f"🎯 <b>DLS target: {target}</b> from {state['overs']} overs.",
    ]


def summary_line(state):
    """One line for the result card when rain touched the match, else ``""``."""
    records = state.get("rain") or []
    if not records:
        return ""
    lost = sum(int(r.get("overs_lost") or 0) for r in records)
    scheduled = scheduled_overs(state)
    return (f"🌧️ Rain cost {lost} over{'' if lost == 1 else 's'} — "
            f"reduced from {scheduled} overs"
            + (" (DLS target)" if state.get("dls_target") else ""))
