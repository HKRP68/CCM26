"""/sim ODI and /sim Test — long-format matches on the Conditions Engine.

The short-format /sim (services.sim_match) is tuned for T20 scoring and
collapses sides over 50 overs. ``engine.sim.match`` is built for the long
game: two new balls and death overs in an ODI; sessions, 80-over new balls,
spell limits, follow-ons, declarations, rain, bad light and teams batting out
time for a draw in a Test. This module feeds it the /sim XIs and turns its
result into the Telegram messages and JSON feed the handler sends.

Pure (no Telegram / asyncio / DB), like services.sim_match.
"""

import logging
import random

from engine.sim.match import MatchSetup, Team, simulate_match
from services import batting_order_service as _bos
from services.sim_match import _esc

logger = logging.getLogger(__name__)

LONG_FORMATS = ("ODI", "Test")
_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def _batting_order(xi):
    """A saved /sbo line-up bats as written; anything else by batting rating."""
    xi = list(xi)
    if _bos.is_order_locked(xi):
        return xi
    return sorted(xi, key=lambda p: p.get("bat_rating") or p.get("rating") or 0,
                  reverse=True)


def _pick_stadium():
    """A real ground from the stadium database, or None for the neutral one."""
    try:
        from engine.sim import stadium as stadium_mod
        grounds = stadium_mod.all_stadiums()
        return random.choice(grounds) if grounds else None
    except Exception:
        logger.exception("sim_long: stadium lookup failed — using a neutral venue")
        return None


def simulate_long_match(home_xi, away_xi, home_name, away_name, fmt_key,
                        pitch=None, stadium=None, seed=None):
    """Play an ODI or a Test between two /sim XIs.

    Returns ``engine.sim.report.build_result``'s dict plus ``potm`` (a name)
    and ``potm_team``. The toss is the engine's own: the winner reads the
    pitch and the light, the way a real captain would.
    """
    if fmt_key not in LONG_FORMATS:
        raise ValueError(f"not a long format: {fmt_key!r}")
    setup = MatchSetup(
        fmt=fmt_key,
        team1=Team(name=home_name, players=_batting_order(home_xi)),
        team2=Team(name=away_name, players=_batting_order(away_xi)),
        pitch=pitch,
        stadium=stadium if stadium is not None else _pick_stadium(),
        seed=seed if seed is not None else random.getrandbits(48),
    )
    res = simulate_match(setup)
    res["potm"], res["potm_team"] = player_of_the_match(res)
    return res


def player_of_the_match(res):
    """``(name, team)`` with the highest impact on the winning side.

    A draw or a tie has no winning side, so the award goes to the highest
    impact in the match. A loser can still win it with a performance that
    outweighs every winner's (impact 10 is the match's best by definition).
    """
    impact = res.get("impact") or []
    if not impact:
        return None, None
    winner = (res.get("result") or {}).get("winner")
    if winner:
        best_winner = next((r for r in impact if r["team"] == winner), None)
        top = impact[0]
        if best_winner and (top["team"] == winner or top["impact"] - best_winner["impact"] < 2.5):
            return best_winner["name"], best_winner["team"]
        return top["name"], top["team"]
    return impact[0]["name"], impact[0]["team"]


def _sr(runs, balls):
    return round(runs * 100.0 / balls, 1) if balls else 0.0


def _econ(runs, balls):
    return round(runs * 6.0 / balls, 2) if balls else 0.0


def innings_label(res, inn):
    """'1st innings' for an ODI; 'India 2nd innings' style for a Test."""
    if res.get("format") != "Test":
        return f"Innings {inn['number']}"
    n = sum(1 for i in res["innings"][:inn["number"]] if i["team"] == inn["team"])
    return f"{_ORDINAL.get(n, f'{n}th')} innings"


def _score(inn):
    if inn.get("all_out") or inn["wickets"] >= 10:
        s = f"{inn['runs']}"
    else:
        s = f"{inn['runs']}/{inn['wickets']}"
    if inn.get("declared"):
        s += " dec"
    return s


def render_long_innings_card(res, inn):
    """One innings as a Telegram HTML scorecard (body in an expandable quote)."""
    header = [
        f"🏏 <b>{_esc(inn['team'])}</b> — {_esc(innings_label(res, inn))}",
        f"<b>{_score(inn)}</b> ({inn['overs']} ov)"
        + (f" · target {inn['target']}" if inn.get("target") else ""),
    ]
    body = ["<b>BATTING</b>"]
    dnb = []
    for b in inn["batting"]:
        if not b["balls"] and not b["out"]:
            dnb.append(b["name"])
            continue
        how = _esc(b["out"]) if b["out"] else "not out"
        mark = "" if b["out"] else "*"
        body.append(f"{_esc(b['name'])} {mark}{b['runs']} ({b['balls']}) [{how}] "
                    f"4s:{b['fours']} 6s:{b['sixes']} SR:{_sr(b['runs'], b['balls'])}")
    body.append(f"Extras: {inn['extras']}")
    body.append(f"<b>TOTAL: {_score(inn)} ({inn['overs']} ov)</b>")
    if dnb:
        body.append(f"<i>DNB: {_esc(', '.join(dnb))}</i>")
    body.append("━━━━━━━━━━━━━━━━━━━")
    body.append("<b>BOWLING</b>")
    for w in inn["bowling"]:
        body.append(f"{_esc(w['name'])} {w['overs']}-{w['maidens']}-{w['runs']}-{w['wkts']} "
                    f"(econ {_econ(w['runs'], w['balls'])})")
    if inn["fow"]:
        fow = " · ".join(f"{f['runs']}/{f['wicket']} ({_esc(f['batter'])}, {f['over']})"
                         for f in inn["fow"])
        body.append(f"<b>FoW:</b> {fow}")
    best = max(inn.get("partnerships") or [{"runs": 0}], key=lambda p: p["runs"])
    if best.get("runs", 0) >= 50:
        body.append(f"<i>Best stand: {best['runs']} for wicket {best['wicket']}</i>")
    return "\n".join(header) + "\n<blockquote expandable>" + "\n".join(body) + "</blockquote>"


def render_match_setup(res, pitch_description=""):
    """The pre-scorecard line-up: venue, conditions and the toss."""
    st = res.get("stadium") or {}
    t = res.get("time") or {}
    w = res.get("final_weather") or {}
    venue = st.get("name") or "Neutral Venue"
    where = ", ".join(x for x in (st.get("city"), st.get("country")) if x)
    if res["format"] == "Test":
        title = "Test match (5 days)"
        light = "day/night, pink ball" if t.get("day_night") else "red ball"
    else:
        title = "ODI (50 ov)"
        light = "day/night" if t.get("day_night") else "day game"
    toss = res.get("toss") or {}
    lines = [
        f"🏏 <b>SIM MATCH — {title}</b>",
        f"🏟️ <b>{_esc(venue)}</b>" + (f" · {_esc(where)}" if where else ""),
        f"📍 <b>{_esc(res['pitch'])}</b> pitch"
        + (f" — <i>{_esc(pitch_description)}</i>" if pitch_description else ""),
        f"🌤️ {light} · {w.get('temperatureC', '?')}°C · cloud {w.get('cloudCover', '?')}% · "
        f"humidity {w.get('humidity', '?')}%",
        f"🪙 {_esc(toss.get('winner', ''))} won the toss and chose to "
        f"{_esc(toss.get('decision', 'bat'))} first",
    ]
    return "\n".join(lines)


def render_stumps(res):
    """Day-by-day close-of-play reports for a Test ('' when there are none)."""
    stumps = res.get("stumps") or []
    if not stumps:
        return ""
    return ("🌇 <b>CLOSE OF PLAY</b>\n"
            + "\n".join(f"• {_esc(s['text'])}" for s in stumps))


def render_long_result(res):
    """The final result with the story of the match."""
    lines = ["🏆 <b>MATCH RESULT</b>", "━━━━━━━━━━━━━━━━━━━"]
    for inn in res["innings"]:
        label = innings_label(res, inn)
        tag = f" ({label})" if res["format"] == "Test" else ""
        lines.append(f"{_esc(inn['team'])}{_esc(tag)}: <b>{_score(inn)}</b> ({inn['overs']} ov)")
    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🎉 <b>{_esc(res['result']['text'])}</b>")
    if res.get("dls_target"):
        lines.append(f"🌧️ DLS target: {res['dls_target']}")
    if res.get("potm"):
        lines.append(f"🌟 Player of the Match: <b>{_esc(res['potm'])}</b>"
                     + (f" ({_esc(_potm_line(res))})" if _potm_line(res) else ""))

    story = []
    for e in res.get("turning_points") or []:
        story.append(f"• {_esc(e['text'])}")
    for r in res.get("rain") or []:
        if r["text"] not in {e["text"] for e in res.get("turning_points") or []}:
            story.append(f"• 🌧️ {_esc(r['text'])}")
    told = {e["text"] for e in res.get("turning_points") or []}
    for d in (res.get("drama") or [])[:3]:
        if d["text"] not in told:
            told.add(d["text"])
            story.append(f"• ⚡ {_esc(d['text'])}")
    if story:
        lines.append("")
        lines.append("<b>📖 Match story</b>")
        lines.extend(story[:8])
    infl = res.get("influences") or []
    if infl:
        lines.append("")
        lines.append("<b>🌍 What the conditions did</b>")
        lines.extend(f"• {_esc(i['text'])}" for i in infl[:4])
    return "\n".join(lines)


def _potm_line(res):
    name = res.get("potm")
    if not name:
        return ""
    runs = wkts = 0
    for inn in res["innings"]:
        for b in inn["batting"]:
            if b["name"] == name:
                runs += b["runs"]
        for w in inn["bowling"]:
            if w["name"] == name:
                wkts += w["wkts"]
    bits = []
    if runs:
        bits.append(f"{runs} runs")
    if wkts:
        bits.append(f"{wkts} wkts")
    return ", ".join(bits)


def _plain(text):
    """Text the card's fonts can draw: no emoji (a "🤖 Sim XI" draws as tofu)."""
    return " ".join("".join(ch for ch in str(text or "") if ord(ch) <= 0xFFFF
                            and not 0x2600 <= ord(ch) <= 0x27BF).split())


def _top(inn):
    """The poster's top-four batters and bowlers of one innings."""
    bats = sorted((b for b in inn["batting"] if b["balls"] or b["out"]),
                  key=lambda b: (b["runs"], -b["balls"]), reverse=True)[:4]
    bowls = sorted(inn["bowling"], key=lambda w: (w["wkts"], -w["runs"]),
                   reverse=True)[:4]
    return {
        "team": _plain(inn["team"]),
        "batters": [{"name": b["name"], "runs": b["runs"], "balls": b["balls"],
                     "fours": b["fours"], "sixes": b["sixes"], "out": bool(b["out"])}
                    for b in bats],
        "bowlers": [{"name": w["name"], "wickets": w["wkts"], "runs": w["runs"],
                     "overs": w["overs"]} for w in bowls],
    }


def conditions_chips(res):
    """``[(kind, label, value), ...]`` for the card's conditions strip."""
    st = res.get("stadium") or {}
    t = res.get("time") or {}
    w = res.get("final_weather") or {}
    toss = res.get("toss") or {}
    venue = st.get("name") or "Neutral Venue"
    if st.get("city"):
        venue = f"{venue}, {st['city']}"
    sky = w.get("cloudCover")
    sky_word = ("Clear" if sky is not None and sky < 25 else
                "Overcast" if sky is not None and sky > 70 else "Partly cloudy")
    weather = (f"{sky_word} · {w['temperatureC']:.0f}°C"
               if w.get("temperatureC") is not None else sky_word)
    ball = f"{t.get('ball', 'White')} · {'Day/Night' if t.get('day_night') else 'Day'}"
    chips = [
        ("venue", "Venue", venue),
        ("pitch", "Pitch", res.get("pitch") or ""),
        ("weather", "Weather", weather),
        ("ball", "Ball", ball),
    ]
    if toss.get("winner"):
        chips.append(("toss", "Toss", f"{_plain(toss['winner'])} · {toss.get('decision', 'bat')}"))
    return chips


def _potm_totals(res):
    """Match totals for the POTM showcase: runs, balls, 4s, 6s, wkts, conceded, overs."""
    name = res.get("potm")
    t = {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "wkts": 0, "conceded": 0,
         "bowl_balls": 0, "batted": False, "bowled": False, "not_out": False}
    for inn in res["innings"]:
        for b in inn["batting"]:
            if b["name"] == name and (b["balls"] or b["out"]):
                t["batted"] = True
                for k in ("runs", "balls", "fours", "sixes"):
                    t[k] += b[k]
                t["not_out"] = not b["out"]
        for w in inn["bowling"]:
            if w["name"] == name:
                t["bowled"] = True
                t["wkts"] += w["wkts"]
                t["conceded"] += w["runs"]
                t["bowl_balls"] += w["balls"]
    return t


def _potm_kwargs(res):
    t = _potm_totals(res)
    overs = f"{t['bowl_balls'] // 6}.{t['bowl_balls'] % 6}" if t["bowled"] else None
    return {
        "potm_name": res.get("potm"),
        "potm_team": _plain(res.get("potm_team") or ""),
        "potm_stats": (_potm_line(res) or "Impact performance") + ("*" if t["not_out"] else ""),
        "potm_runs": t["runs"] if t["batted"] else None,
        "potm_balls": t["balls"] if t["batted"] else None,
        "potm_fours": t["fours"] if t["batted"] else None,
        "potm_sixes": t["sixes"] if t["batted"] else None,
        "potm_wickets": t["wkts"] if t["bowled"] else None,
        "potm_conceded": t["conceded"] if t["bowled"] else None,
        "potm_overs": overs,
    }


def summary_image_kwargs(res):
    """ODI result → the keyword arguments of ``generate_match_summary``
    (None for anything that is not a two-innings ODI)."""
    if res.get("format") != "ODI" or len(res.get("innings") or []) != 2:
        return None
    i1, i2 = res["innings"]
    result = res["result"]
    kw = {
        "inn1_team": _plain(i1["team"]), "inn1_runs": i1["runs"],
        "inn1_wickets": i1["wickets"], "inn1_overs": i1["overs"],
        "inn2_team": _plain(i2["team"]), "inn2_runs": i2["runs"],
        "inn2_wickets": i2["wickets"], "inn2_overs": i2["overs"],
        "winner_name": _plain(result.get("winner") or "Tied"),
        "win_margin_text": _margin_text(result),
        "overs_total": 50,
        "top_per_team": {"inn1": _top(i1), "inn2": _top(i2)},
        "conditions": conditions_chips(res),
        "tagline": "FIFTY OVERS|ONE DAY|INTERNATIONAL",
        "header_left": "ODI",
        "inn1_score_text": _test_score_text(i1),
        "inn2_score_text": _test_score_text(i2),
    }
    kw.update(_potm_kwargs(res))
    kw["dynamic_flourish"] = True
    return kw


def _margin_text(result):
    """'by 8 wickets' / 'by an innings and 45 runs' from the result text."""
    text = result.get("text") or ""
    winner = result.get("winner")
    if winner and " won " in text:
        return text.split(" won ", 1)[1]
    return text or "Match tied"


def _test_score_text(inn):
    if inn.get("declared"):
        return f"{inn['runs']}-{inn['wickets']} d"
    if inn.get("all_out") or inn["wickets"] >= 10:
        return f"{inn['runs']}"
    return f"{inn['runs']}-{inn['wickets']}"


def test_days(res):
    """Timeline nodes: one per close of play, then the result day."""
    rain_days = {r.get("day") for r in res.get("rain") or [] if r.get("overs_lost")}
    days = []
    for st in res.get("stumps") or []:
        days.append({"label": f"Day {st['day']}",
                     "line": f"{_plain(st.get('team', ''))} {st.get('runs', '')}/{st.get('wickets', '')}",
                     "rain": st["day"] in rain_days, "final": False})
    final_day = res.get("days_played") or (len(days) + 1)
    final_day = max(final_day, (days and int(days[-1]["label"].split()[-1]) + 1) or 1)
    final_day = min(final_day, 5)
    result = res.get("result") or {}
    if result.get("winner"):
        verdict = f"{_plain(result['winner'])} win"
    elif result.get("margin") == "tie":
        verdict = "Tied"
    else:
        verdict = "Drawn"
    if days and int(days[-1]["label"].split()[-1]) >= final_day:
        days[-1]["final"] = True
        days[-1]["line"] = verdict
    else:
        days.append({"label": f"Day {final_day}", "line": verdict,
                     "rain": final_day in rain_days, "final": True})
    return days


def test_image_kwargs(res):
    """Test result → the keyword arguments of ``generate_test_summary``."""
    if res.get("format") != "Test" or not res.get("innings"):
        return None
    innings = []
    for inn in res["innings"][:4]:
        top = _top(inn)
        innings.append({
            "team": _plain(inn["team"]), "runs": inn["runs"], "wickets": inn["wickets"],
            "overs": inn["overs"], "score_text": _test_score_text(inn),
            "meta_text": f"{innings_label(res, inn)} · {inn['overs']} overs",
            "batters": top["batters"], "bowlers": top["bowlers"],
        })
    result = res["result"]
    winner = result.get("winner")
    if winner:
        headline, margin = None, _margin_text(result)
    else:
        headline = "MATCH TIED" if result.get("margin") == "tie" else "MATCH DRAWN"
        margin = ""
    kw = {
        "innings": innings,
        "side_a": innings[0]["team"],
        "winner_name": _plain(winner or ""),
        "win_margin_text": margin,
        "result_headline": headline,
        "days": test_days(res),
        "conditions": conditions_chips(res),
    }
    kw.update(_potm_kwargs(res))
    return kw


def render_long_summary_image(res, *, text_settings=None, stadium=None,
                              inn1_user_id=None, inn2_user_id=None,
                              potm_player_id=None):
    """Summary PNG bytes for an ODI or a Test (None if rendering fails).

    ``inn1_user_id`` / ``inn2_user_id`` are the sides that batted first and
    second; the branding lookup is the same as
    ``services.sim_match.render_match_summary_image``.
    """
    fmt = res.get("format")
    kwargs = summary_image_kwargs(res) if fmt == "ODI" else test_image_kwargs(res)
    if kwargs is None:
        return None
    from datetime import datetime
    first = _plain(res["innings"][0]["team"])
    second = next((_plain(i["team"]) for i in res["innings"] if _plain(i["team"]) != first),
                  _plain(res["innings"][0]["bowling_team"]))
    visuals = {}
    try:
        from services import card_identity, scorecard_delivery
        session = card_identity.open_session()
        try:
            visuals = card_identity.summary_visuals(
                session,
                inn1_team=first, inn2_team=second,
                inn1_user_id=inn1_user_id, inn2_user_id=inn2_user_id,
                potm_player_id=potm_player_id, potm_name=res.get("potm"),
                potm_card=scorecard_delivery.potm_card_inline(),
                include_style=False)
        finally:
            session.close()
    except Exception:
        logger.exception("sim_long summary branding lookup failed — drawing plain")
    venue = stadium or (res.get("stadium") or {}).get("name") or res.get("pitch")
    try:
        if fmt == "ODI":
            from services.match_summary_card import generate_match_summary
            return generate_match_summary(
                **visuals, **kwargs, stadium=venue, match_date=datetime.utcnow(),
                text_settings=text_settings)
        from services.test_summary_card import generate_test_summary
        return generate_test_summary(
            **visuals, **kwargs, stadium=venue, match_date=datetime.utcnow(),
            text_settings=text_settings)
    except Exception:
        logger.exception("sim_long summary image failed")
        return None


def long_feed_payload(res, home_name, away_name, pitch_description=""):
    """The ball-by-ball JSON the handler attaches."""
    return {
        "format": res["format"],
        "seed": res.get("seed"),
        "pitch": res["pitch"],
        "pitch_description": pitch_description,
        "stadium": (res.get("stadium") or {}).get("name"),
        "time": res.get("time"),
        "toss": res.get("toss"),
        "teams": {"home": home_name, "away": away_name},
        "result": res["result"]["text"],
        "player_of_the_match": res.get("potm"),
        "stumps": res.get("stumps") or [],
        "rain": res.get("rain") or [],
        "turning_points": res.get("turning_points") or [],
        "innings": [
            {
                "innings": inn["number"],
                "label": innings_label(res, inn),
                "batting_team": inn["team"],
                "bowling_team": inn["bowling_team"],
                "score": _score(inn),
                "overs": inn["overs"],
                "target": inn.get("target"),
                "fall_of_wickets": inn["fow"],
                "commentary": inn.get("commentary") or [],
            }
            for inn in res["innings"]
        ],
        "note": "/sim is a friendly simulation and does not update player batting or bowling stats.",
    }
