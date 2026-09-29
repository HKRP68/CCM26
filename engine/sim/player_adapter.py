"""Engine player dict → :class:`Batter` / :class:`Bowler`.

Cards in the database carry ``rating``, ``bat_rating``, ``bowl_rating``,
``category``, ``bowl_style`` and handedness — not technique, swing or seam. The
missing attributes are *derived*: a fixed recipe from the ratings plus a small
jitter seeded by the player's id, so the same card always gets the same
profile. Any attribute present on the dict (``technique``, ``swing``,
``is_death_specialist`` ...) is used as-is, which is how a hand-built team
file overrides the derivation.
"""

from engine.sim.models import Batter, Bowler
from engine.sim.rng import stable_unit


def _clamp(v, lo=1.0, hi=100.0):
    return max(lo, min(hi, float(v)))


def _pid(p):
    return str(p.get("roster_id") or p.get("player_id") or p.get("id") or p.get("name") or "?")


def _jit(pid, attr, amount):
    return (stable_unit("player", pid, attr) * 2.0 - 1.0) * amount


def is_spinner(style, cfg):
    s = str(style or "").lower()
    if not s:
        return False
    if any(k in s for k in ("fast", "medium", "seam", "pace")):
        return False
    return any(k in s for k in cfg["playerDerivation"]["spinKeywords"])


def _pace_for(style, bowl, cfg):
    s = str(style or "").lower()
    table = cfg["playerDerivation"]["paceByStyle"]
    for key in sorted(table, key=len, reverse=True):
        if key in s:
            return float(table[key])
    return 60.0 + bowl * 0.2


def to_batter(p, cfg):
    d = cfg["playerDerivation"]
    pid = _pid(p)
    j = float(d["jitter"])
    bat = float(p.get("bat_rating") or p.get("rating") or 50)
    cat = str(p.get("category") or "Batsman")
    agg_base = float(d["aggressionByCategory"].get(cat, 55))

    def get(k, default):
        return _clamp(p[k]) if p.get(k) is not None else _clamp(default)

    return Batter(
        id=pid, name=str(p.get("name") or pid),
        technique=get("technique", bat + _jit(pid, "tech", j)),
        vs_pace=get("vs_pace", bat + _jit(pid, "vsp", j)),
        vs_spin=get("vs_spin", bat + _jit(pid, "vss", j)),
        aggression=get("aggression", agg_base + (bat - 60) * 0.25 + _jit(pid, "agg", j * 2)),
        form=get("form", d["defaultForm"] + _jit(pid, "form", j * 2)),
        stamina=get("stamina", d["defaultStamina"]),
        bat_hand=str(p.get("bat_hand") or "Right"),
    )


def to_bowler(p, cfg, traits=None):
    d = cfg["playerDerivation"]
    pid = _pid(p)
    j = float(d["jitter"])
    bowl = float(p.get("bowl_rating") or 30)
    style = str(p.get("bowl_style") or "")
    spin = is_spinner(style, cfg)

    def get(k, default):
        return _clamp(p[k]) if p.get(k) is not None else _clamp(default)

    if spin:
        pace = 45 + _jit(pid, "pace", 5)
        swing, seam = 15 + _jit(pid, "sw", 5), 15 + _jit(pid, "se", 5)
        spin_v = bowl + _jit(pid, "spin", j)
    else:
        pace = _pace_for(style, bowl, cfg) + _jit(pid, "pace", 4)
        tilt = _jit(pid, "tilt", 12)          # swing bowler (+) or seam bowler (-)
        swing = bowl + tilt + _jit(pid, "sw", j * 0.5)
        seam = bowl - tilt + _jit(pid, "se", j * 0.5)
        spin_v = 10.0

    keys = set(d["deathTraitKeys"])
    trait_keys = {str(t.get("effect_key") if isinstance(t, dict) else t)
                  for t in (traits or p.get("traits") or [])}
    death = p.get("is_death_specialist")
    if death is None:
        death = bool(keys & trait_keys) or (
            not spin and bowl >= d["deathSpecialistMinBowl"]
            and stable_unit("player", pid, "death") < 0.5)

    return Bowler(
        id=pid, name=str(p.get("name") or pid),
        kind="spin" if spin else "pace", style=style,
        pace=get("pace", pace), swing=get("swing", swing), seam=get("seam", seam),
        spin=get("spin", spin_v),
        accuracy=get("accuracy", bowl + _jit(pid, "acc", j)),
        stamina=get("stamina", d["defaultStamina"]),
        is_death_specialist=bool(death),
    )
