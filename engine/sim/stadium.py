"""Stadiums: boundary size, outfield speed, altitude, slope — and the database.

``data/stadiums.json`` is the editable database (admin UI or by hand). Lookups
accept the full name, any alias, or a case-insensitive substring, so the live
bot's venue strings ("Wankhede Stadium", "MCG") all resolve.

Formulas (all in ``stadium`` in the config):

* six probability by boundary: <62 m x1.5, 62-70 x1.2, 70-78 x1.0, >78 x0.75.
  Altitude and a following wind make the ball carry, which is modelled as a
  *shorter effective boundary* before the band is read.
* outfield: a 1/2/3 becomes a 4 when shotPower > 100 - outfieldSpeed.
* altitude > 800 m: carry +8%, swing x0.85, six distance +10% (+4% per 1000 m
  above the threshold — Dharamsala's 1300 m reads +12%).
"""

import json
import logging
import os
import threading

from engine.sim.models import Stadium, BOUNDARY_REGIONS

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(_ROOT, "data", "stadiums.json")

_lock = threading.Lock()
_db = None


def from_dict(d):
    b = d.get("boundaryM") or {}
    bm = tuple((r, float(b.get(r, 70))) for r in BOUNDARY_REGIONS)
    return Stadium(
        name=d.get("name", "Neutral Venue"),
        city=d.get("city", ""), country=d.get("country", ""),
        altitude_m=float(d.get("altitudeMeters", 0) or 0),
        boundary_m=bm,
        outfield_speed=float(d.get("outfieldSpeed", 65)),
        dew_factor=float(d.get("dewFactor", 0)),
        typical_pitch=d.get("typicalPitch", "Even"),
        avg_first_innings=int(d.get("avgFirstInningsScore", 170)),
        slope=bool(d.get("slope", False)),
        climate=tuple(sorted((d.get("climate") or {}).items())),
    )


def to_dict(s):
    return {
        "name": s.name, "city": s.city, "country": s.country,
        "altitudeMeters": s.altitude_m, "boundaryM": s.boundaries,
        "outfieldSpeed": s.outfield_speed, "dewFactor": s.dew_factor,
        "typicalPitch": s.typical_pitch, "avgFirstInningsScore": s.avg_first_innings,
        "slope": s.slope, "climate": dict(s.climate),
    }


def load_db(path=None):
    """``[(stadium_dict, Stadium)]`` from the JSON database."""
    try:
        with open(path or DB_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception as exc:
        logger.warning("stadiums: cannot read %s: %s", path or DB_PATH, exc)
        return []
    rows = raw.get("stadiums", raw) if isinstance(raw, dict) else raw
    return [(r, from_dict(r)) for r in rows if isinstance(r, dict) and r.get("name")]


def _get_db():
    global _db
    if _db is None:
        with _lock:
            if _db is None:
                _db = load_db()
    return _db


def reload():
    global _db
    with _lock:
        _db = None
    return _get_db()


def all_stadiums():
    return [s for _, s in _get_db()]


def find(name, db=None):
    """Resolve a venue name/alias to a :class:`Stadium`, or ``None``."""
    if not name:
        return None
    rows = db if db is not None else _get_db()
    q = str(name).strip().lower()
    for raw, s in rows:
        if s.name.lower() == q or q in (a.lower() for a in raw.get("aliases", [])):
            return s
    # Substring fallback: the longest matching name wins, so "Kensington Oval,
    # Barbados" resolves to Kensington Oval rather than The Oval's "Oval".
    best, best_len = None, 0
    for raw, s in rows:
        names = [s.name.lower()] + [a.lower() for a in raw.get("aliases", [])]
        for n in names:
            if len(n) >= 4 and (q in n or n in q) and len(n) > best_len:
                best, best_len = s, len(n)
    return best


def neutral(cfg):
    ref = cfg["meta"]["neutral_reference"]
    b = float(ref["boundaryM"])
    return Stadium(boundary_m=tuple((r, b) for r in BOUNDARY_REGIONS),
                   outfield_speed=float(ref["outfieldSpeed"]),
                   dew_factor=float(ref["dewFactor"]),
                   altitude_m=float(ref["altitudeMeters"]))


# ── formulas ────────────────────────────────────────────────────────────

def six_band(boundary_m, cfg):
    for band in cfg["stadium"]["sixProbBands"]:
        if boundary_m < band["maxM"]:
            return float(band["mult"])
    return float(cfg["stadium"]["sixProbBands"][-1]["mult"])


def altitude_six_distance(altitude_m, cfg):
    a = cfg["stadium"]["altitude"]
    if altitude_m <= a["threshold"]:
        return 1.0
    return a["six"] + a["sixPer1000mAbove"] * (altitude_m - a["threshold"]) / 1000.0


def six_multiplier(stadium, cfg, six_distance=1.0):
    """Six probability multiplier averaged over the four boundary regions."""
    b = stadium.boundaries
    return sum(six_band(m / max(0.5, six_distance), cfg) for m in b.values()) / len(b)


def outfield_four_prob(runs, outfield_speed, cfg, shot_quality=1.0):
    """P(a ``runs`` = 1/2/3 shot races away for four).

    Only a share of those shots are struck into a gap (``candidateShare``); for
    those, shotPower ~ U(0, 100) x shot quality, and it goes for four when
    shotPower > 100 - outfieldSpeed.
    """
    share = float(cfg["stadium"]["outfield"]["candidateShare"].get(str(runs), 0.0))
    need = 100.0 - outfield_speed
    q = max(0.05, shot_quality)
    p_power = max(0.0, min(1.0, 1.0 - need / (100.0 * q)))
    return share * p_power


def apply(fs, cond, cfg):
    s = cond.stadium
    alt = cfg["stadium"]["altitude"]
    six_dist = altitude_six_distance(s.altitude_m, cfg)
    six = six_multiplier(s, cfg, six_distance=six_dist)
    avg = s.avg_boundary
    if six >= 1.15:
        fs.mul("six", six, key="small_ground",
               text=f"Short boundaries at {s.name} (avg {avg:.0f} m) — sixes flew")
    elif six <= 0.9:
        fs.mul("six", six, key="big_ground",
               text=f"The big {s.name} boundaries (avg {avg:.0f} m) swallowed sixes")
    else:
        fs.mul("six", six)

    if s.altitude_m > alt["threshold"]:
        fs.mul("four_carry", alt["carry"], key="altitude",
               text=f"{s.altitude_m:.0f} m of altitude — the ball carried and swung less")
        fs.mul("swing", alt["swing"])

    if s.slope:
        fs.mul("seam", cfg["stadium"]["slopeSeam"], key="slope",
               text=f"The slope at {s.name} fed the seamers")

    speed = s.outfield_speed
    if cond.rained:
        speed += cfg["weather"]["rain"].get("outfieldAfterRain", 0)
    fs.outfield = max(0.0, min(100.0, speed))
    if fs.outfield >= 85:
        fs.note("fast_outfield", "four_carry", 1 + fs.outfield / 1000,
                f"A lightning outfield ({fs.outfield:.0f}) turned twos into fours")
    elif fs.outfield <= 40:
        fs.note("slow_outfield", "four_carry", 1 - (60 - fs.outfield) / 200,
                f"A slow, heavy outfield ({fs.outfield:.0f}) held the ball up")
