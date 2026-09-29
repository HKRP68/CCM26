"""Weather: swing from cloud and humidity, heat, wind, and (rare) rain.

Recomputed every over: :func:`drift` moves the weather a little, seeded, so a
cloudy morning can clear by lunch. Rain is deliberately rare — see
``weather.rain`` in the config for the exact per-over probability.
"""

from dataclasses import replace

from engine.sim.models import Weather


def swing_factor(w, cfg):
    """swing = swingBase + cloud/100 * swingCloudScale (+ a small humidity term).

    With the shipped numbers: clear sky 0.6, half cloud 1.2, full cloud 1.8.
    """
    c = cfg["weather"]
    f = c["swingBase"] + w.cloud_cover / 100.0 * c["swingCloudScale"]
    f += c.get("swingHumidityScale", 0.0) * (w.humidity - 50.0) / 50.0
    return max(0.2, f)


def swing_window_extra(w, cfg):
    """Extra overs of conventional swing on a humid day."""
    h = cfg["weather"]["humidity"]
    return int(h["swingWindowExtraOvers"]) if w.humidity > h["threshold"] else 0


def is_hot(w, cfg):
    return w.temperature_c > cfg["weather"]["heat"]["threshold"]


def wind_relation(w, bowling_end, cfg):
    """``"into"`` (bowler runs in against the wind), ``"with"``, or ``None``."""
    if w.wind_kph <= cfg["weather"]["wind"]["threshold"] or not w.wind_toward:
        return None
    return "into" if w.wind_toward == bowling_end else "with"


def apply(fs, cond, cfg):
    w = cond.weather
    c = cfg["weather"]
    sf = swing_factor(w, cfg)
    if sf >= 1.25:
        text = f"Cloud cover ({w.cloud_cover:.0f}%) made the ball swing"
    elif sf <= 0.8:
        text = f"Clear skies ({w.cloud_cover:.0f}% cloud) took the swing out of it"
    else:
        text = None
    fs.mul("swing", sf, key="cloud_swing" if text else None, text=text)
    fs.swing_window_extra = swing_window_extra(w, cfg)
    if fs.swing_window_extra:
        fs.note("humid_window", "swing", 1.0 + fs.swing_window_extra / 10.0,
                f"Humidity of {w.humidity:.0f}% kept the new ball swinging {fs.swing_window_extra} overs longer")

    if is_hot(w, cfg):
        fs.mul("stamina_drain", c["heat"]["staminaDrain"], key="heat",
               text=f"Heat of {w.temperature_c:.0f}°C sapped the bowlers and dried the surface")
        fs.mul("six", c["heat"].get("boundaryBonus", 1.0))
        fs.mul("four_carry", c["heat"].get("boundaryBonus", 1.0))
    elif w.temperature_c < c["cold"]["threshold"]:
        fs.mul("seam", c["cold"]["seam"], key="cold",
               text=f"A cold {w.temperature_c:.0f}°C day helped the seamers")

    rel = wind_relation(w, cond.bowling_end, cfg)
    if rel == "into":
        fs.mul("spin", c["wind"]["spinDrift"], key="wind_drift",
               text=f"A {w.wind_kph:.0f} km/h wind gave the spinners drift")
        fs.mul("six", c["wind"]["sixDownwind"], key="wind_six",
               text=f"Hitting downwind in a {w.wind_kph:.0f} km/h breeze, the ball flew")
    elif rel == "with":
        fs.mul("pace", c["wind"]["paceWithWind"])
        fs.mul("six", c["wind"]["sixUpwind"])

    if cond.rained:
        fs.mul("swing", c["rain"].get("swingAfterRain", 1.0))


def drift(w, rng, cfg):
    """The next over's weather: a small seeded random walk, clamped."""
    d = cfg["weather"]["driftPerOver"]
    return replace(
        w,
        cloud_cover=w.cloud_cover + rng.gauss(0, d["cloudCover"]),
        humidity=w.humidity + rng.gauss(0, d["humidity"]),
        wind_kph=w.wind_kph + rng.gauss(0, d["windKPH"]),
        temperature_c=w.temperature_c + rng.gauss(0, d["temperatureC"]),
    ).clamped()


def rain_prob_per_over(w, cfg):
    r = cfg["weather"]["rain"]
    p = r["basePerOverProb"] * (w.rain_chance / 50.0)
    if w.cloud_cover > 70:
        p *= r["cloudyBoost"]
    return max(0.0, p)


def rain_starts(w, rng, cfg, interruptions_so_far=0):
    """Does rain stop play this over?"""
    r = cfg["weather"]["rain"]
    if interruptions_so_far >= r["maxInterruptionsPerMatch"]:
        return False
    return rng.chance(rain_prob_per_over(w, cfg))


def overs_lost(rng, fmt, cfg):
    lo, hi = cfg["weather"]["rain"]["oversLost"].get(fmt, [2, 6])
    return rng.randint(int(lo), int(hi))


def from_climate(climate, rng=None):
    """A :class:`Weather` from a stadium's climate dict, lightly randomised."""
    c = dict(climate or {})

    def j(v, s):
        return v + (rng.gauss(0, s) if rng else 0.0)

    toward = rng.choice(["A", "B", None]) if rng else None
    return Weather(
        cloud_cover=j(c.get("cloudCover", 30), 15),
        humidity=j(c.get("humidity", 55), 8),
        rain_chance=j(c.get("rainChance", 10), 5),
        temperature_c=j(c.get("temperatureC", 28), 3),
        wind_kph=j(c.get("windKPH", 10), 5),
        wind_toward=toward,
    ).clamped()
