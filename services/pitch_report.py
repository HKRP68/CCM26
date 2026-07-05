"""Dynamic pitch conditions + Pitch Report for Challenge Leagues.

After the host picks a surface, ``build_pitch_report`` produces a compact,
self-contained Pitch Report card (weather, temperature, day/night, humidity,
wind, dew, pitch age, grass, outfield), effectiveness ratings for
Pacers / Spinners / Batters, and a "win the toss → bat/bowl first" conclusion.

The same generated ``conditions`` dict is threaded into the live match so the
weather actually nudges ball outcomes: ``make_environment_hook`` returns a
``raw_weights -> raw_weights`` weight hook for
``engine.ball_outcome.calculate_outcome`` (the same mechanism the trait system
uses in ``services.cipl_match``).

Design rules (from the conditions spec):
  • The selected pitch stays the primary condition — env effects only *modify*.
  • Modifiers influence probabilities, never force an outcome; each bucket
    multiplier is capped to ±25% so player ratings stay dominant.
  • Dew is night-only and must not appear in every night match.

The module keeps its own small pitch base table so it works for all six league
surfaces (Dry, Dusty, Hard, Flat, Green, Bouncy) regardless of which names the
outcome engine recognises.
"""

import random
from html import escape

# Surfaces offered in Challenge Leagues.
PITCH_TYPES = ["Dry", "Dusty", "Hard", "Even", "Flat", "Green", "Bouncy"]

# Weather set (matches the conditions spec).
WEATHERS = [
    "Clear and Sunny", "Partly Cloudy", "Overcast", "Humid",
    "Hot and Dry", "Light Rain Earlier", "Windy",
]

# Short, friendly one-liners per surface (independent of the sim engine, which
# only knows Green/Dry/Hard/Flat/Dead).
_PITCH_BLURB = {
    "Dry": "Spin-friendly, tough to score",
    "Dusty": "Big turn — spinners thrive",
    "Hard": "True bounce, balanced contest",
    "Flat": "Batting paradise, run-fest",
    "Green": "Seam movement, pace dominates",
    "Bouncy": "Extra carry, pace & bounce",
    "Even": "Neutral, balanced contest",
}

# Base effectiveness (0-5) before conditions are layered on.
_PITCH_BASE = {
    #          pacers spin batters
    "Dry":    (2, 4, 2),
    "Dusty":  (1, 5, 2),
    "Hard":   (3, 3, 4),
    "Flat":   (2, 2, 5),
    "Green":  (5, 1, 2),
    "Bouncy": (4, 2, 3),
    "Even":   (3, 3, 3),
}

# Default surface a toss should be won to do — overridden by conditions below.
_PITCH_TOSS = {
    "Dry": "bat", "Dusty": "bat", "Hard": "bat",
    "Flat": "bat", "Green": "bowl", "Bouncy": "bowl", "Even": "bat",
}

# Typical grass cover per surface (a weighted pick is taken around this).
_PITCH_GRASS = {
    "Green": ["Heavy", "Heavy", "Medium"],
    "Bouncy": ["Medium", "Medium", "Heavy"],
    "Hard": ["Medium", "Little", "Medium"],
    "Flat": ["Little", "Little", "No Grass"],
    "Dry": ["Little", "No Grass", "Little"],
    "Dusty": ["No Grass", "No Grass", "Little"],
    "Even": ["Little", "Medium", "Little"],
}

_EFF_LABEL = {0: "Minimal", 1: "Low", 2: "Moderate", 3: "Good",
              4: "High", 5: "Excellent"}


# ───────────────────────── condition generation ─────────────────────────

def generate_conditions(pitch_type, rng=None):
    """Generate a consistent set of match conditions for ``pitch_type``.

    Returns a dict with every field shown in the Pitch Report. Pass an
    ``rng`` (``random.Random``) for deterministic output in tests.
    """
    r = rng or random
    pitch = pitch_type if pitch_type in _PITCH_BASE else "Hard"

    day_night = r.choice(["Day", "Day", "Night"])  # day matches a touch likelier
    if day_night == "Night":
        match_time = r.choice(["Evening (5:30 PM)", "Night (7:30 PM)",
                               "Night (8:00 PM)"])
    else:
        match_time = r.choice(["Morning (10:00 AM)", "Afternoon (1:30 PM)",
                               "Late Afternoon (3:30 PM)"])

    weather = r.choice(WEATHERS)

    # Temperature band shaped by weather.
    if weather == "Hot and Dry":
        temperature = r.randint(33, 41)
    elif weather in ("Overcast", "Light Rain Earlier"):
        temperature = r.randint(15, 25)
    elif weather == "Clear and Sunny":
        temperature = r.randint(26, 37)
    else:
        temperature = r.randint(19, 34)

    # Humidity, biased by weather.
    if weather in ("Humid", "Light Rain Earlier", "Overcast"):
        humidity = r.choice(["High", "High", "Medium"])
    elif weather == "Hot and Dry":
        humidity = r.choice(["Low", "Low", "Medium"])
    else:
        humidity = r.choice(["Low", "Medium", "High"])

    wind_direction = r.choice(["Headwind", "Tailwind", "Crosswind"])
    if weather == "Windy":
        wind_strength = r.choice(["Moderate", "Strong", "Strong"])
    else:
        wind_strength = r.choice(["Calm", "Light", "Light", "Moderate"])

    grass = r.choice(_PITCH_GRASS.get(pitch, ["Medium"]))
    pitch_age = r.choice(["Fresh", "Fresh", "Used", "Very Used"])

    if weather == "Light Rain Earlier":
        outfield = r.choice(["Slow", "Wet", "Slow"])
    elif weather in ("Hot and Dry", "Clear and Sunny"):
        outfield = r.choice(["Fast", "Fast", "Normal"])
    else:
        outfield = r.choice(["Fast", "Normal", "Normal", "Slow"])

    dew_level, dew_text = _roll_dew(day_night, weather, temperature, humidity, r)

    conditions = {
        "pitch_type": pitch,
        "day_night": day_night,
        "match_time": match_time,
        "weather": weather,
        "temperature": temperature,
        "humidity": humidity,
        "wind_direction": wind_direction,
        "wind_strength": wind_strength,
        "dew": dew_level,            # None / Light / Moderate / Heavy
        "dew_text": dew_text,        # display string for the report
        "grass": grass,
        "pitch_age": pitch_age,
        "outfield": outfield,
    }
    return conditions


def _roll_dew(day_night, weather, temperature, humidity, r):
    """Roll a night-dew level. Day matches never carry meaningful dew, and even
    at night dew must not be guaranteed (spec rule)."""
    if day_night != "Night":
        return None, "Negligible (day match)"

    score = 0
    score += {"High": 2, "Medium": 1, "Low": 0}[humidity]
    if temperature <= 22:
        score += 2
    elif temperature <= 27:
        score += 1
    if weather in ("Humid", "Overcast", "Light Rain Earlier"):
        score += 1
    score += r.randint(0, 2)  # genuine randomness — not every night gets dew

    if score >= 6:
        return "Heavy", "Heavy 💧 (very likely)"
    if score >= 4:
        return "Moderate", "Moderate (likely)"
    if score >= 3:
        return "Light", "Light (possible)"
    return None, "None (unlikely)"


# ───────────────────────── effectiveness ─────────────────────────

def _clamp(v, lo=0, hi=5):
    return max(lo, min(hi, v))


def pitch_effectiveness(pitch_type, conditions):
    """Return ``{'pacers','spinners','batters'}`` → ``(score 0-5, label)``."""
    pitch = pitch_type if pitch_type in _PITCH_BASE else "Hard"
    pace, spin, bat = _PITCH_BASE[pitch]

    w = conditions.get("weather")
    temp = conditions.get("temperature", 26)
    hum = conditions.get("humidity")
    dew = conditions.get("dew")
    grass = conditions.get("grass")
    outfield = conditions.get("outfield")
    age = conditions.get("pitch_age")

    # Weather.
    if w in ("Overcast", "Light Rain Earlier"):
        pace += 1
        bat -= 1
    elif w in ("Clear and Sunny", "Hot and Dry"):
        pace -= 1
        bat += 1
        if w == "Hot and Dry":
            spin += 1
    elif w == "Humid":
        pace += 1

    # Temperature.
    if temp <= 18:
        pace += 1
    elif temp >= 34:
        pace -= 1
        spin += 1

    # Humidity.
    if hum == "High":
        pace += 1
    elif hum == "Low":
        spin += 1

    # Dew (night) — skid hurts spin, helps the bat.
    if dew == "Heavy":
        spin -= 2
        bat += 1
    elif dew == "Moderate":
        spin -= 1
        bat += 1
    elif dew == "Light":
        spin -= 1

    # Grass.
    if grass == "Heavy":
        pace += 1
        spin -= 1
    elif grass in ("No Grass", "Little"):
        spin += 1

    # Outfield.
    if outfield == "Fast":
        bat += 1
    elif outfield in ("Slow", "Wet"):
        bat -= 1

    # Pitch age / wear.
    if age == "Very Used":
        spin += 1
        bat -= 1
    elif age == "Used":
        spin += 1

    pace, spin, bat = _clamp(pace), _clamp(spin), _clamp(bat)
    return {
        "pacers": (pace, _EFF_LABEL[pace]),
        "spinners": (spin, _EFF_LABEL[spin]),
        "batters": (bat, _EFF_LABEL[bat]),
    }


# ───────────────────────── toss conclusion ─────────────────────────

def best_toss_decision(pitch_type, conditions):
    """Return ``(decision, reason)`` where decision is 'bat' or 'bowl'."""
    pitch = pitch_type if pitch_type in _PITCH_TOSS else "Hard"
    decision = _PITCH_TOSS[pitch]
    reason = {
        "bat": "the surface only worsens — post a total first",
        "bowl": "use the fresh surface before it eases",
    }[decision]

    w = conditions.get("weather")
    dew = conditions.get("dew")
    night = conditions.get("day_night") == "Night"

    # Strong overrides, most decisive last.
    if pitch in ("Flat", "Dead") and w in ("Clear and Sunny", "Hot and Dry"):
        decision, reason = "bat", "flat track in the sun — pile on a big score"
    if pitch in ("Green", "Bouncy") and w in ("Overcast", "Light Rain Earlier"):
        decision, reason = "bowl", "overcast + seam — exploit the new ball"
    if night and dew in ("Moderate", "Heavy"):
        decision, reason = "bowl", "heavy night dew — chasing is much easier"

    return decision, reason


# ───────────────────────── formatting ─────────────────────────

def _bar(score):
    return "▰" * score + "▱" * (5 - score)


def format_pitch_report(pitch_type, conditions, eff, decision):
    """Build the compact HTML Pitch Report card.

    Every interpolated value is HTML-escaped because the result is sent with
    Telegram ``parse_mode="HTML"`` — e.g. the Bouncy blurb contains a literal
    ``&`` that Telegram would otherwise reject.
    """
    c = conditions
    # Use the normalised pitch from conditions so the title always matches the
    # stored/simulated surface (unknown surfaces fall back to "Hard").
    display_pitch = escape(str(c.get("pitch_type", pitch_type)))
    blurb = escape(_PITCH_BLURB.get(c.get("pitch_type", pitch_type), ""))
    dn_icon = "🌙" if c.get("day_night") == "Night" else "☀️"
    decide, reason = decision
    decide_word = "BOWL first" if decide == "bowl" else "BAT first"

    def e(key):
        return escape(str(c.get(key, "")))

    pac = eff["pacers"]
    spn = eff["spinners"]
    bts = eff["batters"]

    lines = [
        f"🌱 <b>PITCH REPORT</b> — <b>{display_pitch}</b>",
        f"<i>{blurb}</i>",
        "━━━━━━━━━━━━━━━",
        f"🌤️ {e('weather')}  ·  🌡️ {e('temperature')}°C",
        f"{dn_icon} {e('day_night')} · {e('match_time')}",
        f"💧 Humidity: {e('humidity')}  ·  💨 {e('wind_direction')} ({e('wind_strength')})",
        f"❄️ Dew: {e('dew_text')}",
        f"🌿 Grass: {e('grass')}  ·  🕳️ Pitch: {e('pitch_age')}  ·  🟩 Outfield: {e('outfield')}",
        "━━━━━━━━━━━━━━━",
        "<b>Effective for</b>",
        f"⚡ Pacers   {_bar(pac[0])} {escape(str(pac[1]))}",
        f"🌀 Spinners {_bar(spn[0])} {escape(str(spn[1]))}",
        f"🏏 Batters  {_bar(bts[0])} {escape(str(bts[1]))}",
        "━━━━━━━━━━━━━━━",
        f"🪙 <b>Win the toss → {decide_word}</b>",
        f"<i>{escape(str(reason))}</i>",
    ]
    return "\n".join(lines)


def build_pitch_report(pitch_type, rng=None):
    """Generate conditions + effectiveness + toss and return ``(text, conditions)``.

    The returned ``conditions`` dict also carries the computed ``best_toss`` so
    downstream code can reuse it without recomputing.
    """
    conditions = generate_conditions(pitch_type, rng=rng)
    eff = pitch_effectiveness(pitch_type, conditions)
    decision = best_toss_decision(pitch_type, conditions)
    conditions["best_toss"] = decision[0]
    conditions["best_toss_reason"] = decision[1]
    text = format_pitch_report(conditions["pitch_type"], conditions, eff, decision)
    return text, conditions


# ───────────────────────── in-match weight hook ─────────────────────────

# Engine raw-weight buckets (engine.ball_outcome.calculate_outcome).
_BUCKETS = ("Dot", "Single", "Double", "Three", "Four", "Six", "Wicket", "Extras")
_CAP_LO, _CAP_HI = 0.75, 1.25  # ±25% per-bucket cap (spec balancing rule)


def _phase(over, total_overs):
    """'powerplay' | 'middle' | 'death' from the 1-based current over."""
    total = max(1, total_overs)
    if over <= max(2, round(total * 0.3)):
        return "powerplay"
    if over > round(total * 0.8):
        return "death"
    return "middle"


def _env_multipliers(conditions, ctx):
    """Per-bucket multipliers (capped) from conditions + match phase."""
    mult = {k: 1.0 for k in _BUCKETS}

    def nudge(bucket, factor):
        mult[bucket] *= factor

    phase = _phase(ctx.get("over", 1), ctx.get("total_overs", 20))
    innings = ctx.get("innings", 1)
    w = conditions.get("weather")
    temp = conditions.get("temperature", 26)
    hum = conditions.get("humidity")
    wind_direction = conditions.get("wind_direction")
    wind_strength = conditions.get("wind_strength")
    dew = conditions.get("dew")
    grass = conditions.get("grass")
    outfield = conditions.get("outfield")
    age = conditions.get("pitch_age")
    night = conditions.get("day_night") == "Night"

    # ── Weather + new-ball / wear progression ──
    if w in ("Overcast", "Light Rain Earlier"):
        if phase == "powerplay":
            nudge("Wicket", 1.12)
            nudge("Dot", 1.05)
            nudge("Four", 0.95)
            nudge("Six", 0.93)
        else:
            nudge("Wicket", 1.05)
    elif w in ("Clear and Sunny", "Hot and Dry"):
        nudge("Four", 1.04)
        nudge("Six", 1.04)
        if phase == "death":  # dry surface spins/grips late
            nudge("Wicket", 1.08)
    elif w == "Humid" and phase == "powerplay":
        nudge("Wicket", 1.06)

    # ── Wind — strong/moderate wind affects aerial shots and accuracy ──
    if wind_strength in ("Moderate", "Strong"):
        strong = wind_strength == "Strong"
        if strong:
            nudge("Extras", 1.05)  # accuracy dips in strong wind
        if wind_direction == "Tailwind":
            nudge("Six", 1.04 if strong else 1.02)
        elif wind_direction == "Headwind":
            nudge("Six", 0.96 if strong else 0.98)
        elif wind_direction == "Crosswind" and strong:
            nudge("Wicket", 1.03)  # drift induces edges/mistimes

    # ── Temperature ──
    if temp <= 18 and phase == "powerplay":
        nudge("Wicket", 1.08)
        nudge("Four", 0.96)
    elif temp >= 34 and phase != "powerplay":
        nudge("Wicket", 1.06)
        nudge("Six", 1.03)

    # ── Humidity ──
    if hum == "High" and phase == "powerplay":
        nudge("Wicket", 1.05)
    elif hum == "Low" and phase == "death":
        nudge("Wicket", 1.05)

    # ── Grass ──
    if grass == "Heavy" and phase == "powerplay":
        nudge("Wicket", 1.08)
        nudge("Four", 0.96)
    elif grass in ("No Grass", "Little") and phase != "powerplay":
        nudge("Wicket", 1.04)

    # ── Outfield ──
    if outfield == "Fast":
        nudge("Four", 1.06)
        nudge("Six", 1.04)
    elif outfield in ("Slow", "Wet"):
        nudge("Four", 0.92)
        nudge("Six", 0.92)
        nudge("Double", 1.05)

    # ── Pitch age / wear ──
    if age == "Very Used" and phase != "powerplay":
        nudge("Wicket", 1.08)
        nudge("Four", 0.95)

    # ── Night dew — skid + chasing assist (strongest when chasing) ──
    if night and dew in ("Light", "Moderate", "Heavy"):
        strength = {"Light": 0.4, "Moderate": 0.7, "Heavy": 1.0}[dew]
        if innings == 2:
            strength *= 1.3  # the chasing side benefits most
        nudge("Four", 1.0 + 0.10 * strength)
        nudge("Six", 1.0 + 0.08 * strength)
        nudge("Wicket", 1.0 - 0.10 * strength)   # ball skids, spin loses grip
        nudge("Extras", 1.0 + 0.10 * strength)

    # Cap every bucket to ±25%.
    for k in mult:
        mult[k] = max(_CAP_LO, min(_CAP_HI, mult[k]))
    return mult


def make_environment_hook(conditions, ctx):
    """Return a ``raw_weights -> raw_weights`` weight hook for the outcome engine,
    or ``None`` when there are no conditions to apply.

    ``ctx`` = ``{'over': int, 'total_overs': int, 'innings': int}`` (1-based over).
    Mirrors ``services.cipl_match._make_trait_hook`` so it slots straight into
    ``calculate_outcome(..., weight_hook=...)``.
    """
    if not conditions:
        return None
    mult = _env_multipliers(conditions, ctx or {})
    if all(abs(v - 1.0) < 1e-9 for v in mult.values()):
        return None  # nothing to do this ball

    def _hook(raw_weights):
        adjusted = dict(raw_weights)
        for bucket, factor in mult.items():
            if bucket in adjusted:
                adjusted[bucket] = max(0.0, adjusted[bucket] * factor)
        return adjusted

    return _hook
