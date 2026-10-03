"""On-field chirp for Challenge League (/cipl) and /letsplay.

Now and then a bowler or a fielder has a word with the batter. The batter may
answer back, and about one exchange in five boils over into a proper verbal
fight that the umpire has to break up. It does more than decorate the feed:
the batter comes out of it either **fired up** (more boundaries, more risk) or
**rattled** (more dots, more wickets) for the next few balls they face. Which
one depends on the batter's class against the bowler's, plus a little luck.

Kept rare on purpose: at most :data:`MAX_PER_INNINGS` exchanges an innings,
never two within :data:`COOLDOWN_BALLS` balls, and a small chance on each
trigger. A sledge every over is noise; three good ones a match is a story.

Pure logic. ``services.cipl_match`` calls :func:`maybe_sledge` after each legal
ball, :func:`make_hook` before each ball, and :func:`tick` once a ball is
faced. Lines come from ``data/sledging_pack.json``.
"""

from __future__ import annotations

import json
import logging
import os
import random

logger = logging.getLogger(__name__)

MAX_PER_INNINGS = 3
COOLDOWN_BALLS = 12
EFFECT_BALLS = 6
ESCALATE_CHANCE = 0.20
REPLY_CHANCE = 0.65

#: Chance that each trigger starts an exchange.
TRIGGER_CHANCE = {
    "drop": 0.30,
    "sendoff": 0.08,
    "beaten_then_hit": 0.03,
    "boundaries": 0.06,
    "dots": 0.05,
    "chase": 0.05,
}

#: Weight multipliers while the effect lasts.
EFFECTS = {
    "fired": {"Four": 1.12, "Six": 1.12, "Wicket": 1.10, "Dot": 0.90},
    "rattled": {"Wicket": 1.15, "Dot": 1.10, "Single": 0.92},
}

_PACK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "sledging_pack.json")
_PACK = None


def _pack():
    global _PACK
    if _PACK is None:
        try:
            with open(_PACK_PATH, encoding="utf-8") as fh:
                _PACK = json.load(fh)
        except Exception:
            logger.exception("sledging pack unavailable; chirp disabled")
            _PACK = {}
    return _PACK


def _line(group, **fields):
    if not group:
        return None
    text = random.choice(group)
    for k, v in fields.items():
        text = text.replace("{" + k + "}", str(v or ""))
    return text


def _book(state):
    """This innings' sledge counter, reset when the innings changes."""
    book = state.get("sledge_book")
    if not isinstance(book, dict) or book.get("innings") != state.get("innings"):
        book = {"innings": state.get("innings"), "count": 0, "last_ball": -999}
        state["sledge_book"] = book
    return book


def pick_trigger(*, wicket, dropped, runs, is_extra, prev_was_dot,
                 trailing_dots, prev_was_boundary, chase_tight):
    """The trigger this ball offers, or None. One per ball, the loudest wins."""
    if dropped:
        return "drop"
    if wicket:
        return "sendoff"
    if is_extra:
        return None
    if runs in (4, 6) and prev_was_boundary:
        return "boundaries"
    if runs in (4, 6) and prev_was_dot:
        return "beaten_then_hit"
    if trailing_dots >= 3:
        return "dots"
    if chase_tight:
        return "chase"
    return None


def fired_chance(batter_rating, bowler_rating):
    """How likely the batter answers a sledge by lifting their game."""
    gap = (float(batter_rating or 50) - float(bowler_rating or 50)) / 60.0
    return max(0.2, min(0.8, 0.5 + gap))


def maybe_sledge(state, trigger, *, batter, bowler, fielder="",
                 batter_rid=None, batter_rating=50, bowler_rating=50,
                 ball_no=0, rng=random):
    """Roll for an exchange on *trigger*. Returns the event dict or None.

    The event carries the lines to show and the ``mode`` it left the batter in:
    ``"fired"``, ``"rattled"`` or ``"sendoff"`` (a dismissed batter has no next
    ball, so a send-off is words only).
    """
    if not trigger or trigger not in TRIGGER_CHANCE:
        return None
    pack = _pack()
    if not pack:
        return None
    book = _book(state)
    if book["count"] >= MAX_PER_INNINGS or ball_no - book["last_ball"] < COOLDOWN_BALLS:
        return None
    if rng.random() >= TRIGGER_CHANCE[trigger]:
        return None

    fielder = fielder or bowler
    names = {"batter": batter, "bowler": bowler, "fielder": fielder}
    lines = []
    if trigger == "sendoff":
        lines.append(_line(pack.get("sendoff"), **names))
        mode = "sendoff"
    else:
        opener = (pack.get("opener") or {}).get(trigger)
        lines.append(_line(opener, **names))
        if trigger != "drop" and rng.random() < REPLY_CHANCE:
            lines.append(_line(pack.get("reply"), **names))
        # The batter started the drop taunt; they are already on top.
        if trigger == "drop":
            mode = "fired"
        else:
            mode = ("fired" if rng.random() < fired_chance(batter_rating, bowler_rating)
                    else "rattled")
    escalated = rng.random() < ESCALATE_CHANCE
    if escalated:
        lines.append(_line(pack.get("escalation"), **names))
    if mode in ("fired", "rattled"):
        lines.append(_line(pack.get(f"outcome_{mode}"), **names))
        if batter_rid is not None:
            state["sledge"] = {"rid": str(batter_rid), "mode": mode,
                               "balls_left": EFFECT_BALLS}
    lines = [ln for ln in lines if ln]
    if not lines:
        return None

    book["count"] += 1
    book["last_ball"] = ball_no
    return {"trigger": trigger, "mode": mode, "escalated": escalated,
            "batter": batter, "bowler": bowler, "fielder": fielder,
            "lines": lines}


def make_hook(state, striker_rid):
    """The weight hook for a batter still carrying a sledge, else None."""
    eff = state.get("sledge")
    if not isinstance(eff, dict) or eff.get("rid") != str(striker_rid):
        return None
    if int(eff.get("balls_left") or 0) <= 0:
        return None
    mult = EFFECTS.get(eff.get("mode"))
    if not mult:
        return None

    def _hook(raw_weights):
        rw = dict(raw_weights)
        for key, factor in mult.items():
            if key in rw:
                rw[key] *= factor
        return rw

    return _hook


def tick(state, striker_rid, dismissed=False):
    """A legal ball faced by *striker_rid*: wear the effect down one ball."""
    eff = state.get("sledge")
    if not isinstance(eff, dict) or eff.get("rid") != str(striker_rid):
        return
    eff["balls_left"] = int(eff.get("balls_left") or 0) - 1
    if dismissed or eff["balls_left"] <= 0:
        state.pop("sledge", None)
