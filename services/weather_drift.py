"""Weather and dew that change *during* a match.

The Pitch Report (``services.pitch_report``) rolls one set of conditions before
the toss, and until now they held from the first ball to the last. Real
conditions move: cloud rolls in and the ball starts to swing, the sun burns the
cloud off, the wind drops, and — the big one — dew settles as a night match
goes on, so the side bowling second is holding a wet ball.

:func:`plan` turns the pre-match conditions into a *forecast*: a short list of
timed changes, fixed at the start so the match is reproducible from its state.
:func:`apply_due` is called at the start of every over; it applies whatever the
forecast says has happened by now to ``state["conditions"]`` — which the
per-ball environment hook (``pitch_report.make_environment_hook``) already
reads — and returns the lines to announce.

Deliberately **no rain**: nothing here ever produces rain, a stoppage, a
shortened innings or a DLS target. Weather only nudges the ball-outcome weights
through the existing, capped (±25%) environment hook.

Dew now *builds*: a night match starts dry and the dew arrives towards the end
of the first innings or during the chase, rising to the level the Pitch Report
forecast. The report's toss advice still reads the forecast, so "heavy dew
expected — bowl first" stays good advice.
"""

import random

# Cloud cover only ever moves one step at a time along this line, and never
# into rain.
_CLOUD_STEPS = ["Clear and Sunny", "Partly Cloudy", "Overcast"]
_DEW_STEPS = [None, "Light", "Moderate", "Heavy"]

_WEATHER_TEXT = {
    ("Clear and Sunny", "Partly Cloudy"): "☁️ Cloud drifts in over the ground — a touch more help for the seamers",
    ("Partly Cloudy", "Overcast"): "🌥️ The cloud thickens overhead — the ball could start to swing",
    ("Overcast", "Partly Cloudy"): "🌤️ The cloud is breaking up — batting gets easier",
    ("Partly Cloudy", "Clear and Sunny"): "🌤️ The sky clears — good batting conditions",
    ("Clear and Sunny", "Hot and Dry"): "🔥 The heat is building — the surface is drying out",
    ("Hot and Dry", "Clear and Sunny"): "🌇 The heat eases off as the day cools",
    ("Humid", "Partly Cloudy"): "🌤️ The humidity lifts",
    ("Humid", "Overcast"): "🌥️ Heavy, humid air turns to thick cloud — swing weather",
    ("Windy", "Partly Cloudy"): "🍃 The wind drops away",
    ("Light Rain Earlier", "Partly Cloudy"): "🌤️ The outfield is drying out after the earlier showers",
}

_DEW_TEXT = {
    "Light": "💧 Dew is starting to settle — the ball is getting slippery",
    "Moderate": "💧💧 The dew is getting heavier — spinners are struggling to grip",
    "Heavy": "💧💧💧 Heavy dew now — the ball is like a bar of soap",
}


def _weather_options(weather, day):
    """Where ``weather`` can move to next (never rain)."""
    if weather in _CLOUD_STEPS:
        i = _CLOUD_STEPS.index(weather)
        opts = []
        if i > 0:
            opts.append(_CLOUD_STEPS[i - 1])
        if i < len(_CLOUD_STEPS) - 1:
            opts.append(_CLOUD_STEPS[i + 1])
        if weather == "Clear and Sunny" and day:
            opts.append("Hot and Dry")
        return opts
    return {"Hot and Dry": ["Clear and Sunny"],
            "Humid": ["Partly Cloudy", "Overcast"],
            "Windy": ["Partly Cloudy"],
            "Light Rain Earlier": ["Partly Cloudy"]}.get(weather, [])


def _pick_over(r, total_overs, lo, hi):
    """A 1-based over between ``lo`` and ``hi`` fractions of the innings."""
    total = max(1, int(total_overs or 20))
    a = max(1, int(round(total * lo)))
    b = max(a, int(round(total * hi)))
    return r.randint(a, min(b, total))


def plan(conditions, total_overs=20, rng=None):
    """Return a copy of ``conditions`` carrying its in-match forecast.

    Adds ``timeline`` (the timed changes, oldest first), ``dew_forecast`` (what
    the report predicted) and resets ``dew`` to what it is at the first ball.
    Planning twice is a no-op, so a resumed match keeps its forecast.
    """
    if not conditions:
        return conditions
    if conditions.get("timeline") is not None:
        return conditions
    r = rng or random
    c = dict(conditions)
    night = c.get("day_night") == "Night"
    events = []

    # ── Weather: up to two steps, no rain ──
    weather = c.get("weather")
    if r.random() < 0.55:
        steps = 2 if r.random() < 0.3 else 1
        slots = sorted({(1, _pick_over(r, total_overs, 0.35, 0.9)),
                        (2, _pick_over(r, total_overs, 0.15, 0.7))})
        r.shuffle(slots)
        slots = sorted(slots[:steps])
        for inn, over in slots:
            opts = _weather_options(weather, not night)
            if not opts:
                break
            new = r.choice(opts)
            change = {"weather": new}
            if weather == "Windy":
                change["wind_strength"] = "Light"
            if weather == "Light Rain Earlier" and c.get("outfield") in ("Wet", "Slow"):
                change["outfield"] = "Normal"
            events.append({"inn": inn, "over": over, "kind": "weather",
                           "set": change,
                           "text": _WEATHER_TEXT.get((weather, new),
                                                     f"🌦️ The weather changes: {new}")})
            weather = new

    # ── Wind: may pick up in the second innings ──
    if c.get("wind_strength") in ("Calm", "Light") and r.random() < 0.25:
        events.append({"inn": 2, "over": _pick_over(r, total_overs, 0.2, 0.8),
                       "kind": "wind", "set": {"wind_strength": "Moderate"},
                       "text": f"💨 The wind is picking up — a {str(c.get('wind_direction') or 'cross').lower()} now"})

    # ── Night: it cools, and the dew builds towards the forecast ──
    forecast = c.get("dew")
    if night:
        temp = c.get("temperature")
        if isinstance(temp, int):
            events.append({"inn": 2, "over": 1, "kind": "temp",
                           "set": {"temperature": temp - r.randint(2, 4)},
                           "text": None})
        target = forecast
        if target is None and r.random() < 0.2:
            target = "Light"          # the odd night the forecast misses
        if target:
            top = _DEW_STEPS.index(target)
            # Heavy dew arrives before the break; lighter dew waits for the chase.
            if top >= 3:
                points = [(1, _pick_over(r, total_overs, 0.7, 0.9)),
                          (2, _pick_over(r, total_overs, 0.05, 0.25)),
                          (2, _pick_over(r, total_overs, 0.35, 0.6))]
            elif top == 2:
                points = [(2, 1), (2, _pick_over(r, total_overs, 0.3, 0.6))]
            else:
                points = [(2, _pick_over(r, total_overs, 0.1, 0.5))]
            for level, (inn, over) in zip(_DEW_STEPS[1:top + 1], points):
                events.append({"inn": inn, "over": over, "kind": "dew",
                               "set": {"dew": level}, "text": _DEW_TEXT[level]})
        c["dew_forecast"] = forecast
        c["dew"] = None
    else:
        c["dew_forecast"] = forecast

    events.sort(key=lambda e: (e["inn"], e["over"]))
    for e in events:
        e["done"] = False
    c["timeline"] = events
    return c


def apply_due(state):
    """Apply every forecast change due by the current over. Returns the texts.

    Mutates ``state["conditions"]`` in place. Safe on states without a
    forecast (returns ``[]``).
    """
    c = state.get("conditions") if isinstance(state, dict) else None
    if not c or not c.get("timeline"):
        return []
    now = (int(state.get("innings") or 1), int(state.get("current_over") or 1))
    texts = []
    for e in c["timeline"]:
        if e.get("done") or (e["inn"], e["over"]) > now:
            continue
        c.update(e.get("set") or {})
        e["done"] = True
        if e.get("text"):
            texts.append(e["text"])
    return texts


def conditions_line(conditions):
    """Compact "🌥️ Overcast · 🌡️ 22°C · 💧 Light dew" strip for the chat."""
    if not conditions or not conditions.get("weather"):
        return ""
    bits = [f"🌤️ {conditions['weather']}"]
    if conditions.get("temperature") is not None:
        bits.append(f"🌡️ {conditions['temperature']}°C")
    if conditions.get("dew"):
        bits.append(f"💧 {conditions['dew']} dew")
    elif conditions.get("dew_forecast"):
        bits.append(f"💧 {conditions['dew_forecast']} dew expected")
    return "  ·  ".join(bits)
