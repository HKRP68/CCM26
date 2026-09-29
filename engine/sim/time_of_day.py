"""Time of day: morning swing, afternoon heat, evening dew, the pink ball.

The session is read from the *clock hour of the delivery*, not the session the
match started in — an ODI that starts at 13:30 is bowling under lights by the
chase, and that is exactly when the dew arrives.
"""


def session_at(hour, cfg):
    t = cfg["timeOfDay"]
    if hour < t["morning"]["untilHour"]:
        return "Morning"
    if hour < t["afternoon"]["untilHour"]:
        return "Afternoon"
    if hour < t["eveningNight"]["dewFromHour"]:
        return "Evening"
    return "Night"


def dew_intensity(cond, cfg):
    """0..1 — how wet the ball is at this delivery.

    Dew needs the evening (``eveningNight.dewFromHour``) and ramps in over
    ``dewRampHours``. Its ceiling is the ground's ``dewFactor`` (or a measured
    ``dew_override``): at or above ``stadium.dew.threshold`` it is full dew.
    """
    en = cfg["timeOfDay"]["eveningNight"]
    if cond.hour < en["dewFromHour"]:
        return 0.0
    level = cond.dew_override if cond.dew_override is not None else cond.stadium.dew_factor
    thr = max(1.0, float(cfg["stadium"]["dew"]["threshold"]))
    ceiling = max(0.0, min(1.0, float(level) / thr))
    ramp = min(1.0, (cond.hour - en["dewFromHour"]) / max(0.01, en["dewRampHours"]))
    return ceiling * max(0.0, ramp)


def _clock(hour):
    h = int(hour) % 24
    suffix = "am" if h < 12 else "pm"
    return f"{(h % 12) or 12}{suffix}"


def apply(fs, cond, ball, cfg):
    t = cfg["timeOfDay"]
    session = session_at(cond.hour, cfg)
    w = cond.weather

    if session == "Morning":
        m = t["morning"]
        fs.mul("swing", m["swing"], key="morning_swing",
               text="Morning moisture — the new ball talked early")
        fs.mul("seam", m["seam"])
        if w.cloud_cover > m["cloudThreshold"]:
            fs.mul("swing", m["cloudBonus"])
            fs.mul("seam", m["cloudBonus"])
    elif session == "Afternoon":
        a = t["afternoon"]
        sessions = max(1, int(cond.afternoon_sessions))
        fs.mul("spin", 1 + a["spinPerSession"] * sessions, key="afternoon_heat",
               text="The afternoon sun baked the surface and it started to grip")

    dew = dew_intensity(cond, cfg)
    fs.dew = dew
    if dew > 0:
        en = t["eveningNight"]
        spin = 1 - (1 - en["spinGripPenalty"]) * dew
        fs.mul("spin", spin, key="dew",
               text=f"The dew after {_clock(en['dewFromHour'])} killed the spinners' grip (spin x{spin:.2f})")
        fs.mul("spin_wkt", spin)
        fs.mul("seam", 1 - (1 - en["seamPenalty"]) * dew)
        fs.mul("four_carry", 1 + (en["boundaryBonus"] - 1) * dew)
        fs.drop_add += en["dropBonus"] * dew
        fs.note("dew", "drop", en["dropBonus"] * dew,
                "A wet ball under lights — catches went down")

    pb = t["pinkBall"]
    if ball is not None and ball.color == "Pink" and ball.overs_old < pb["overs"]:
        fs.mul("swing", pb["swing"], key="pink_ball",
               text=f"The new pink ball hooped around for its first {pb['overs']} overs")
