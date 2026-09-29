"""Pitch base multipliers and how a surface decays.

Base values come from ``pitches`` in the config; which names exist comes from
``engine.pitch_registry`` so an unknown surface degrades to the neutral one
rather than to whatever sorts first.

Decay: every completed session adds ``crackRate`` crack points (x1.3 for a
session played in the heat, +0.1 per afternoon session). Each crack point
turns and cracks the surface a little more (``pitchDecay``). On days 4-5 a Dry
or Dusty Test pitch is a minefield: spin wicket chance x2.0.
"""

from engine import pitch_registry


def normalise(name):
    return pitch_registry.normalise(name)


def profile(name, cfg):
    """The nine base multipliers for surface *name*."""
    pitches = cfg.get("pitches") or {}
    n = normalise(name)
    return dict(pitches.get(n) or pitches.get(pitch_registry.DEFAULT) or {})


def crack_level(cond, cfg):
    """Crack points accumulated by this delivery (0 on a fresh surface)."""
    prof = profile(cond.pitch, cfg)
    rate = float(prof.get("crackRate", 1.0))
    heat = float(cfg["weather"]["heat"]["crackRate"])
    afternoon = float(cfg["timeOfDay"]["afternoon"]["crackPerSession"])
    sessions = max(0, int(cond.test_session))
    hot = min(sessions, max(0, int(cond.hot_sessions)))
    crack = rate * (sessions - hot) + rate * heat * hot
    crack += afternoon * max(0, int(cond.afternoon_sessions))
    crack += float(cond.wear) * rate
    return min(float(cfg["pitchDecay"]["maxCrack"]), crack)


def centring(prof, cfg):
    """(help_centre, wicket_centre) that ``pitchCentering`` divides out, or
    (1, 1) when it is off. Mode "best" centres on the surface's best bowling
    option, "mean" on the attack-weighted mean."""
    c = cfg.get("pitchCentering") or {}
    if not c.get("enabled"):
        return 1.0, 1.0
    ps = float(c.get("paceShare", 0.6))
    mix = cfg["outcome"]["bowlingMix"]["pace"]
    wsum = mix["pace"] + mix["swing"] + mix["seam"]
    pace_h = (mix["pace"] * prof.get("paceHelp", 1) + mix["swing"] * prof.get("swingHelp", 1)
              + mix["seam"] * prof.get("seamHelp", 1)) / wsum
    spin_h = prof.get("spinTurn", 1)
    pw, sw = prof.get("paceWicketChance", 1), prof.get("spinWicketChance", 1)
    if c.get("mode", "best") == "best":
        help_c, wkt_c = max(pace_h, spin_h), max(pw, sw)
    else:
        help_c = ps * pace_h + (1 - ps) * spin_h
        wkt_c = ps * pw + (1 - ps) * sw
    k = float(c.get("strength", 1.0))
    return max(0.1, help_c) ** k, max(0.1, wkt_c) ** k


def apply(fs, cond, cfg):
    prof = profile(cond.pitch, cfg)
    name = normalise(cond.pitch)
    mapping = (("paceHelp", "pace"), ("seamHelp", "seam"), ("swingHelp", "swing"),
               ("spinTurn", "spin"), ("bounce", "bounce"), ("battingEase", "bat_ease"),
               ("paceWicketChance", "pace_wkt"), ("spinWicketChance", "spin_wkt"))
    words = {"pace": "pace off the surface", "seam": "seam movement",
             "swing": "swing", "spin": "turn", "bounce": "bounce",
             "bat_ease": "easy batting", "pace_wkt": "wickets for the quicks",
             "spin_wkt": "wickets for the spinners"}
    for key, ch in mapping:
        v = float(prof.get(key, 1.0))
        # Only the surface's defining traits make the summary; the rest still apply.
        if v >= 1.3 or v <= 0.77:
            text = (f"The {name} pitch offered {words[ch]} (x{v:.2f})" if v > 1
                    else f"The {name} pitch gave little {words[ch]} (x{v:.2f})")
            fs.mul(ch, v, key=f"pitch_{ch}", text=text)
        else:
            fs.mul(ch, v)

    centre_help, centre_wkt = centring(prof, cfg)
    if centre_help != 1.0:
        for ch in ("pace", "seam", "swing", "spin"):
            fs.mul(ch, 1.0 / centre_help)
        fs.mul("pace_wkt", 1.0 / centre_wkt)
        fs.mul("spin_wkt", 1.0 / centre_wkt)

    crack = crack_level(cond, cfg)
    if crack > 0:
        d = cfg["pitchDecay"]
        fs.mul("spin", 1 + crack * d["spinTurnPerCrack"], key="pitch_wear",
               text=f"The {name} surface broke up and turned more as it wore (crack {crack:.1f})")
        fs.mul("bounce", max(0.3, 1 + crack * d["bouncePerCrack"]))
        fs.mul("bat_ease", max(0.3, 1 + crack * d["battingEasePerCrack"]))
        fs.mul("pace_wkt", 1 + crack * d["paceWicketPerCrack"])
        fs.mul("spin_wkt", 1 + crack * d["spinWicketPerCrack"])

    t = cfg["timeOfDay"]["test"]
    if (cond.fmt == "Test" and cond.test_day in t["minefieldDays"]
            and name in t["minefieldPitches"]):
        fs.mul("spin_wkt", float(t["spinWicket"]), key="minefield",
               text=f"Day {cond.test_day} on a {name} pitch — a minefield for the batters")


def apply_grass(fs, cond, cfg):
    g = (cfg.get("grass") or {}).get(cond.grass or "")
    if not g:
        return
    fs.mul("seam", float(g.get("seam", 1.0)), key="grass" if g.get("seam", 1) > 1.05 else None,
           text=f"{cond.grass} grass cover kept the seamers interested")
    fs.mul("swing", float(g.get("swing", 1.0)))
    fs.mul("spin", float(g.get("spin", 1.0)))
