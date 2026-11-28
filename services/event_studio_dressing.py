"""Pinned, bounded fixture adjustments for opt-in, noncompetitive Solo play."""

from copy import deepcopy
import math

from services.event_studio_insights import dressing_room, performances


WEIGHTS = {"morale": .35, "unity": .15, "form": .30, "fatigue": .25, "captain": .20}


def ratings(db, config, room, xis, benches, now):
    settings = config["competition"]
    cap = settings.get("management_cap", 0)
    if room.kind != "solo" or not settings.get("dressing_room") or not cap:
        return xis, benches, {}
    weights = {**WEIGHTS, **settings.get("management_weights", {})}
    xis, benches = deepcopy(xis), deepcopy(benches)
    pinned = {}
    for team in xis:
        history = dressing_room(config, team, performances(db, room.id, team=team), now)
        summary = {row["player_id"]: row for row in history["players"]}
        morale = (history["morale"] - 50) / 50
        unity = ((history["unity"] - 75) / 25 if history["unity"] is not None else 0)
        captain = summary.get(xis[team][0]["player_id"], {})
        captain_value = min(1, captain.get("captain_impact", 0) / 25)
        changes = {}
        for card in (*xis[team], *benches.get(team, ())):
            row = summary[card["player_id"]]
            bat_form = 0 if row["strike_rate"] is None else math.tanh((row["strike_rate"] - 110) / 50)
            bowl_form = 0 if row["economy"] is None else math.tanh((7 - row["economy"]) / 4)
            sample = min(1, row["appearances"] / 3)
            fatigue = min(1, row["workload"] / (120 if config["format"] == "ODI" else 65))
            common = (weights["morale"] * morale + weights["unity"] * unity + weights["captain"] * captain_value
                      - weights["fatigue"] * fatigue)
            deltas = {}
            for field, form in (("bat_rating", bat_form), ("bowl_rating", bowl_form)):
                adjustment = min(cap, max(-cap, cap * (common + weights["form"] * sample * form)))
                original = card[field]
                # Keep the printed rating and all original squad data separate.
                card.setdefault("card_" + field, original)
                card[field] = round(max(1, original + adjustment), 2)
                deltas[field] = round(card[field] - original, 2)
            changes[card["player_id"]] = deltas
        pinned[team] = changes
    return xis, benches, {"version": 1, "cap": cap, "ratings": pinned}
