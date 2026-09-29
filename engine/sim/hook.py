"""The Conditions Engine inside the live /letsplay and CIPL matches.

``services.cipl_match.simulate_over`` builds a chain of bounded weight hooks
around ``engine.ball_outcome.calculate_outcome``. This module supplies one more
link, :func:`make_conditions_hook`, and it replaces the old string-table
environment hook (``services.pitch_report.make_environment_hook``) when it is
switched on — both describe the weather, and applying both would count it twice.

What it applies is a **delta**: the bucket multipliers under the real
conditions divided by the multipliers under neutral conditions on the *same
pitch*. The pitch itself, the phase of the innings, pressure and momentum are
already modelled by the calibrated live engine and cancel out of the ratio, so
the par bands in ``config/ground_conditions.yaml`` hold. The delta is blended by
``meta.live_strength`` and clamped to ``meta.live_bounds``.

Switched per format by ``meta.enabled_live`` in ``config/sim_engine.json``.
Every failure degrades to ``None`` (no hook), never to a crashed delivery.
"""

import logging
import math
import re

from engine.sim import ball_aging, factors, outcome, stadium as stadium_mod
from engine.sim.config import get_config
from engine.sim.models import Conditions, MatchTime, Weather
from engine.sim.player_adapter import to_batter, to_bowler

logger = logging.getLogger(__name__)

# The Pitch Report's coarse strings → numbers the engine understands.
WEATHER_MAP = {
    # cloud %, rain chance %, humidity nudge, wind km/h floor
    "Clear and Sunny": (10, 3, -5, 0),
    "Partly Cloudy": (40, 12, 0, 0),
    "Overcast": (80, 30, 8, 0),
    "Humid": (50, 18, 12, 0),
    "Hot and Dry": (5, 1, -12, 0),
    "Light Rain Earlier": (70, 40, 12, 0),
    "Windy": (35, 10, -3, 28),
}
HUMIDITY_MAP = {"Low": 35, "Medium": 60, "High": 84}
WIND_MAP = {"Calm": 3, "Light": 10, "Moderate": 20, "Strong": 32}
DEW_MAP = {None: 0, "None": 0, "Light": 35, "Moderate": 65, "Heavy": 90}
OUTFIELD_DELTA = {"Fast": 12, "Normal": 0, "Slow": -15, "Wet": -25}
AGE_WEAR = {"Fresh": 0.0, "Used": 0.5, "Very Used": 1.0}

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)", re.I)


def parse_hour(text, default=15.0):
    """``"Night (7:30 PM)"`` → 19.5."""
    m = _TIME_RE.search(str(text or ""))
    if not m:
        return default
    h = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "PM" else 0)
    return h + int(m.group(2) or 0) / 60.0


def live_format(state):
    if state.get("ball_format") == "The100":
        return "The100"
    return "ODI" if int(state.get("overs") or 20) >= 40 else "T20"


def enabled(state, cfg=None):
    cfg = cfg or get_config()
    if cfg.get("_broken"):
        return False
    return bool((cfg["meta"].get("enabled_live") or {}).get(live_format(state)))


def weather_from(conditions):
    c = conditions or {}
    cloud, rain, hum_nudge, wind_floor = WEATHER_MAP.get(c.get("weather"), (30, 10, 0, 0))
    humidity = HUMIDITY_MAP.get(c.get("humidity"), 55) + hum_nudge
    wind = max(wind_floor, WIND_MAP.get(c.get("wind_strength"), 8))
    toward = {"Headwind": "A", "Tailwind": "B"}.get(c.get("wind_direction"))
    return Weather(cloud_cover=cloud, humidity=humidity, rain_chance=rain,
                   temperature_c=float(c.get("temperature") or 28), wind_kph=wind,
                   wind_toward=toward).clamped()


def conditions_for(state, over_idx, innings, cfg):
    """(Conditions, BallState) for the delivery about to be bowled."""
    c = state.get("conditions") or {}
    fmt = live_format(state)
    pitch = state.get("pitch_type") or "Even"
    st = stadium_mod.find(state.get("stadium")) or stadium_mod.neutral(cfg)
    speed = st.outfield_speed + OUTFIELD_DELTA.get(c.get("outfield"), 0)
    from dataclasses import replace
    st = replace(st, outfield_speed=max(0.0, min(100.0, speed)))

    night = str(c.get("day_night") or "").lower().startswith("n")
    start = parse_hour(c.get("match_time"), 19.5 if night else 14.0)
    t = cfg["timeOfDay"]
    mpo = t["minutesPerOver"].get(fmt, 4.2)
    overs_total = int(state.get("overs") or 20)
    elapsed = over_idx * mpo
    if innings == 2:
        elapsed += overs_total * mpo + t["inningsBreakMinutes"].get(fmt, 20)
    hour = start + elapsed / 60.0

    wear = AGE_WEAR.get(c.get("pitch_age"), 0.0)
    if innings == 2:
        wear += cfg["pitchDecay"]["limitedOversInnings2Crack"]
    dew = c.get("dew")
    cond = Conditions(
        pitch=pitch, weather=weather_from(c), stadium=st, fmt=fmt,
        time=MatchTime(session="Night" if night else "Afternoon", is_day_night=night,
                       ball_color="White", start_hour=start),
        hour=hour, innings=innings, over=over_idx, grass=c.get("grass"), wear=wear,
        dew_override=float(DEW_MAP.get(dew, 0)) if night else 0.0,
        bowling_end="A" if over_idx % 2 == 0 else "B")
    two_balls = fmt == "ODI" and cfg["ballAging"]["newBall"]["odiTwoBalls"]
    age = over_idx / 2.0 if two_balls else float(over_idx)
    ball = ball_aging.ball_at(age, pitch, cfg, color="White")
    return cond, ball


def neutral_conditions(cond, cfg):
    ref = cfg["meta"]["neutral_reference"]
    from dataclasses import replace
    return replace(
        cond,
        weather=Weather(cloud_cover=ref["cloudCover"], humidity=ref["humidity"],
                        temperature_c=ref["temperatureC"], wind_kph=ref["windKPH"]),
        time=MatchTime(session=ref["session"], ball_color=ref["ballColor"], start_hour=ref["hour"]),
        stadium=stadium_mod.neutral(cfg), hour=float(ref["hour"]), grass=None,
        wear=cfg["pitchDecay"]["limitedOversInnings2Crack"] if cond.innings == 2 else 0.0,
        dew_override=0.0, rained=False)


def live_multipliers(state, striker, bowler, over_idx, innings, cfg=None):
    """``({bucket: mult}, outfield_shift, actual FactorSet, neutral FactorSet)``
    — the blended, clamped delta."""
    cfg = cfg or get_config()
    cond, ball = conditions_for(state, over_idx, innings, cfg)
    neu = neutral_conditions(cond, cfg)
    bat = to_batter(striker or {}, cfg)
    bowl = to_bowler(bowler or {}, cfg)
    fmt = cond.fmt
    fs_a = factors.compose(cond, ball, cfg, bowler=bowl)
    fs_n = factors.compose(neu, ball, cfg, bowler=bowl)
    m_a = outcome.bucket_multipliers(bat, bowl, fs_a, cfg, fmt=fmt)
    m_n = outcome.bucket_multipliers(bat, bowl, fs_n, cfg, fmt=fmt)
    k = float(cfg["meta"]["live_strength"])
    lo, hi = cfg["meta"]["live_bounds"]["min"], cfg["meta"]["live_bounds"]["max"]
    mult = {}
    for b in outcome.BUCKETS:
        delta = m_a[b] / m_n[b] if m_n[b] else 1.0
        mult[b] = max(lo, min(hi, 1.0 + (delta - 1.0) * k))
    shift = {}
    for runs, bucket in ((1, "Single"), (2, "Double"), (3, "Three")):
        d = (stadium_mod.outfield_four_prob(runs, fs_a.outfield, cfg)
             - stadium_mod.outfield_four_prob(runs, fs_n.outfield, cfg)) * k
        if abs(d) > 1e-9:
            shift[bucket] = d
    return mult, shift, fs_a, fs_n


def _record(state, fs, fs_neutral, bowler):
    """Tally the conditions that made this ball differ from a neutral day.

    An effect the neutral reference also has (the new ball swinging, the
    afternoon clock) cancels out of the delta, so it is not credited either.
    """
    log = state.setdefault("sim_influences", {})
    neutral = {(e.key, round(e.value, 4)) for e in fs_neutral.effects}
    is_spin = bool(bowler) and bowler.is_spin
    pace_ch = {"swing", "seam", "pace", "pace_wkt", "bounce"}
    for e in fs.effects:
        if e.key.startswith("pitch_") or (e.key, round(e.value, 4)) in neutral:
            continue      # the calibrated pitch owns the surface; neutral cancels
        if (e.channel in pace_ch and is_spin) or (e.channel in ("spin", "spin_wkt") and not is_spin):
            continue
        w = abs(math.log(e.value)) if (e.value > 0 and e.channel != "drop") else e.value
        row = log.setdefault(e.key, {"text": e.text, "balls": 0, "weight": 0.0, "top": 0.0})
        row["balls"] += 1
        row["weight"] = round(row["weight"] + w, 4)
        if w >= row.get("top", 0.0):
            row["top"], row["text"] = round(w, 4), e.text


def make_conditions_hook(state, striker, bowler, ctx=None):
    """A ``raw_weights -> raw_weights`` hook, or ``None`` when switched off.

    ``ctx`` = ``{"over": 1-based over, "innings": 1|2}``.
    """
    try:
        cfg = get_config()
        if not state.get("conditions") or not enabled(state, cfg):
            return None
        ctx = ctx or {}
        over_idx = max(0, int(ctx.get("over", state.get("current_over", 1))) - 1)
        innings = int(ctx.get("innings", state.get("innings", 1)))
        mult, shift, fs, fs_n = live_multipliers(state, striker, bowler, over_idx, innings, cfg)
        _record(state, fs, fs_n, to_bowler(bowler or {}, cfg))
    except Exception:
        logger.exception("conditions hook build failed; ignoring")
        return None

    def _hook(raw_weights):
        w = dict(raw_weights)
        for b, f in mult.items():
            if b in w:
                w[b] = max(0.0, w[b] * f)
        for b, d in shift.items():
            if b in w and "Four" in w:
                moved = w[b] * max(-0.5, min(0.5, d))
                w[b] -= moved
                w["Four"] = max(0.0, w["Four"] + moved)
        return w

    return _hook


def influence_summary(state, n=5):
    """Top conditions for the post-match report: ``[{text, overs, weight}]``."""
    log = state.get("sim_influences") or {}
    rows = sorted(log.values(), key=lambda r: -r["weight"])[:n]
    return [{"text": r["text"], "overs": round(r["balls"] / 6.0, 1), "weight": r["weight"]}
            for r in rows]
