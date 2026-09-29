"""Scorecard, fall of wickets, wagon wheel, and the end-of-match summary.

``build_result`` turns a finished match into a plain, JSON-serialisable dict;
``render_text`` prints it. The summary answers *why*: the key turning points,
the conditions that shaped the game (with the numbers), and an impact rating
for every player.
"""

from engine.sim import factors, stadium as stadium_mod


def _sr(runs, balls):
    return round(100.0 * runs / balls, 1) if balls else 0.0


def _econ(runs, balls):
    return round(runs * 6.0 / balls, 2) if balls else 0.0


def impact_ratings(innings, fmt):
    """0-10 impact for every player who did something.

    Batting: runs, adjusted for strike rate against the match's own rate in
    limited overs. Bowling: 22 per wicket plus runs saved against the match
    economy. Scaled so the best performance of the match is 10.
    """
    runs = sum(i["runs"] for i in innings)
    balls = sum(i["legal_balls"] for i in innings) or 1
    match_sr = 100.0 * runs / balls
    match_econ = runs * 6.0 / balls
    raw = {}
    for inn in innings:
        for b in inn["batting"]:
            if not b["balls"]:
                continue
            v = float(b["runs"])
            if fmt != "Test" and b["runs"] >= 10:
                v *= 1 + 0.5 * (_sr(b["runs"], b["balls"]) - match_sr) / max(1.0, match_sr)
            raw.setdefault(b["name"], [0.0, inn["team"]])[0] += max(0.0, v)
        for w in inn["bowling"]:
            overs = w["balls"] / 6.0
            v = 22.0 * w["wkts"] + (match_econ - _econ(w["runs"], w["balls"])) * overs * (1.5 if fmt != "Test" else 0.6)
            raw.setdefault(w["name"], [0.0, inn["bowling_team"]])[0] += max(0.0, v)
    if not raw:
        return []
    top = max(v for v, _ in raw.values()) or 1.0
    out = [{"name": n, "team": t, "impact": round(10.0 * v / top, 1)} for n, (v, t) in raw.items()]
    return sorted(out, key=lambda r: -r["impact"])


def build_result(m, result):
    cfg = m.cfg
    innings = [i.summary() for i in m.innings]
    influences = factors.top_effects(m.effects, n=cfg["output"]["topInfluences"],
                                     pitch_slots=cfg["output"].get("pitchInfluenceSlots", 2))
    for inf in influences:
        inf["overs"] = round(inf["balls"] / 6.0, 1)
    turning = sorted(m.events, key=lambda e: -e["score"])[:cfg["output"]["topTurningPoints"]]
    turning = sorted(turning, key=lambda e: (e["innings"], e["over"]))
    w = m.weather
    return {
        "seed": m.seed,
        "format": m.fmt,
        "pitch": m.pitch,
        "stadium": stadium_mod.to_dict(m.stadium),
        "time": {"session": m.time.session, "day_night": m.time.is_day_night,
                 "ball": m.time.ball_color, "start_hour": m.time.start_hour},
        "final_weather": {"cloudCover": round(w.cloud_cover), "humidity": round(w.humidity),
                          "temperatureC": round(w.temperature_c, 1), "windKPH": round(w.wind_kph)},
        "teams": [m.setup.team1.name, m.setup.team2.name],
        "toss": m.toss_info,
        "innings": innings,
        "result": result,
        "dls_target": m.dls_target,
        "rain": m.rain_events,
        "drama": m.drama_events,
        "turning_points": turning,
        "influences": influences,
        "impact": impact_ratings(innings, m.fmt),
    }


# ── text rendering ──────────────────────────────────────────────────────

def _bar(n, top, width=18):
    return "█" * max(0, round(width * n / top)) if top else ""


def render_scorecard(inn):
    lines = [f"{inn['team']} — {inn['runs']}/{inn['wickets']} ({inn['overs']} ov)"
             + (" dec" if inn["declared"] else "")
             + (f"  target {inn['target']}" if inn.get("target") else "")]
    lines.append(f"  {'Batter':<22}{'':<28}{'R':>4}{'B':>5}{'4s':>4}{'6s':>4}{'SR':>7}")
    for b in inn["batting"]:
        if not b["balls"] and not b["out"]:
            continue
        lines.append(f"  {b['name'][:21]:<22}{(b['out'] or 'not out')[:27]:<28}"
                     f"{b['runs']:>4}{b['balls']:>5}{b['fours']:>4}{b['sixes']:>4}"
                     f"{_sr(b['runs'], b['balls']):>7}")
    lines.append(f"  Extras {inn['extras']}")
    lines.append(f"  {'Bowler':<22}{'O':>6}{'M':>4}{'R':>5}{'W':>4}{'Econ':>7}")
    for w in inn["bowling"]:
        lines.append(f"  {w['name'][:21]:<22}{w['overs']:>6}{w['maidens']:>4}{w['runs']:>5}"
                     f"{w['wkts']:>4}{_econ(w['runs'], w['balls']):>7}")
    if inn["fow"]:
        fow = ", ".join(f"{f['runs']}-{f['wicket']} ({f['batter']}, {f['over']})" for f in inn["fow"])
        lines.append(f"  FoW: {fow}")
    return "\n".join(lines)


def render_wagon(inn):
    if not inn["wagon"]:
        return ""
    top = max(inn["wagon"].values())
    rows = [f"  {z:<11}{r:>4} {_bar(r, top)}" for z, r in sorted(inn["wagon"].items(), key=lambda x: -x[1])]
    return f"Wagon wheel — {inn['team']}\n" + "\n".join(rows)


def render_summary(res):
    st = res["stadium"]
    t = res["time"]
    out = [
        "═" * 60,
        f"{res['teams'][0]} v {res['teams'][1]} — {res['format']} at {st['name']}",
        f"Pitch: {res['pitch']} · {t['ball']} ball · "
        f"{'day/night' if t['day_night'] else 'day'} · start {t['start_hour']:.1f}h · seed {res['seed']}",
        f"Toss: {res['toss']['winner']} chose to {res['toss']['decision']}",
        f"RESULT: {res['result']['text']}",
        "═" * 60,
    ]
    for inn in res["innings"]:
        out.append(f"  {inn['team']}: {inn['runs']}/{inn['wickets']} ({inn['overs']})"
                   + (" dec" if inn["declared"] else ""))
    if res["rain"]:
        out.append("\nRain:")
        out += [f"  • {r['text']}" for r in res["rain"]]
    out.append("\nKey turning points:")
    out += [f"  • [inn {e['innings']}, ov {e['over']}] {e['text']}" for e in res["turning_points"]] or ["  • none"]
    out.append("\nWhat the conditions did:")
    for inf in res["influences"]:
        out.append(f"  • {inf['text']} — active {inf['overs']} overs")
    out.append("\nPlayer impact (0-10):")
    for r in res["impact"][:8]:
        out.append(f"  {r['impact']:>4}  {r['name']} ({r['team']})")
    return "\n".join(out)


def render_text(res, commentary=True, wagon=True):
    parts = []
    for inn in res["innings"]:
        if commentary and inn["commentary"]:
            parts.append(f"── {inn['team']} innings ──")
            parts += inn["commentary"]
        parts.append("")
        parts.append(render_scorecard(inn))
        if wagon:
            ww = render_wagon(inn)
            if ww:
                parts.append(ww)
        parts.append("")
    parts.append(render_summary(res))
    return "\n".join(parts)
