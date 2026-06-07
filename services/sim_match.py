"""Auto-simulated cricket match engine (the /sim command).

Unlike the interactive engine (services/match_engine.py + the Mini App, where the
user picks every delivery and shot), this module simulates an entire match
server-side in one call and returns a fully resolved result with a ball-by-ball
commentary feed. It reuses the existing ball-outcome model
(services.probability_engine.calculate_outcome) so scoring stays consistent with
/wpm and /cm, and the commentary system (services.commentary_service) for text.

Team setup is deliberately simple (the "/sim" design choice):
  - Batting order  = highest batting rating to lowest.
  - Bowling        = only Bowlers and All-rounders bowl, rotated with no bowler
                     bowling consecutive overs and a per-bowler over cap.

The module is pure (no Telegram / asyncio). The handler layer drives it and
handles message sending and the suspense delay.
"""

import math
import random
from datetime import datetime

from services.probability_engine import calculate_outcome
from services.bowling_service import get_delivery_options, AVAILABLE_SHOTS

# Wicket "how" (from probability_engine) → commentary event key.
_HOW_TO_EVENT = {
    "Bowled": "wicket_bowled",
    "LBW": "wicket_lbw",
    "Stumped": "wicket_stumped",
    "Caught Behind": "wicket_caught_keeper",
    "Caught": "wicket_caught_fielder",
    "Caught & Bowled": "wicket_caught_fielder",
    "Run Out": "wicket_runOut",
}

_RUNS_TO_EVENT = {0: "dot", 1: "one", 2: "two", 3: "three", 4: "four", 6: "six"}

# Phase-based shot pools for the auto-batsman.
_ATTACK = ["Drive", "Loft", "Slog", "Pull", "Slog Sweep", "Square Cut", "Hook", "Cut"]
_NORMAL = ["Drive", "Cut", "Flick", "Leg Glance", "On Drive", "Off Drive", "Glance", "Pull"]
_DEFENSIVE = ["Defend", "Leave", "Glance", "Leg Glance", "Flick"]


def _new_bat_stat():
    return {"runs": 0, "balls": 0, "fours": 0, "sixes": 0,
            "out": False, "how": "", "bowler": ""}


def _new_bowl_stat():
    return {"balls": 0, "runs": 0, "wickets": 0, "maidens": 0}


def _pick_shot(over, total_overs, wickets):
    """Choose a shot for the auto-batsman based on phase and wickets lost."""
    powerplay = over <= max(1, total_overs * 0.3)
    death = over > total_overs - max(1, round(total_overs * 0.2))
    if wickets >= 7:
        pool, weights = _DEFENSIVE + _NORMAL, None
    elif powerplay or death:
        pool, weights = _ATTACK + _NORMAL, None
    else:
        pool, weights = _NORMAL + _ATTACK[:3] + _DEFENSIVE[:2], None
    return random.choice(pool)


def _pick_delivery(bowler):
    """Return (variation, length) for the bowler's profile."""
    opts = get_delivery_options(bowler.get("bowl_style"), bowler.get("bowl_hand"))
    if opts.get("is_spinner"):
        return random.choice(opts.get("deliveries") or ["Off Break"]), None
    return (random.choice(opts.get("variations") or ["Seam Up"]),
            random.choice(opts.get("lengths") or ["Good"]))


def _build_bowling_plan(bowling_xi, overs):
    """Pick the over-by-over bowler list (Option B rules).

    Only Bowlers + All-rounders bowl, sorted by bowl rating. No bowler bowls two
    overs in a row; each is capped at the standard ~20% quota (extended only if
    too few bowlers exist to fill the innings).
    """
    eligible = [p for p in bowling_xi
                if p.get("category") in ("Bowler", "All-rounder")]
    if not eligible:  # pathological XI — let anyone bowl so the sim never stalls
        eligible = list(bowling_xi)
    eligible.sort(key=lambda p: p.get("bowl_rating", 0), reverse=True)

    cap = max(math.ceil(overs / 5), math.ceil(overs / len(eligible)))
    counts = {id(p): 0 for p in eligible}
    plan, last = [], None
    for _ in range(overs):
        cands = [p for p in eligible if counts[id(p)] < cap and id(p) != last]
        if not cands:
            cands = [p for p in eligible if id(p) != last] or eligible
        bowler = max(cands, key=lambda p: p.get("bowl_rating", 0))
        plan.append(bowler)
        counts[id(bowler)] += 1
        last = id(bowler)
    return plan


def _fielder_keeper(bowling_xi):
    keeper = next((p["name"] for p in bowling_xi
                   if p.get("category") == "Wicket Keeper"), "the keeper")
    fielders = [p["name"] for p in bowling_xi] or ["the fielder"]
    return fielders, keeper


def simulate_innings(batting_xi, bowling_xi, overs, pitch_type,
                     innings_no, batting_team, bowling_team,
                     target=None, commentary=None, feed=None):
    """Simulate one innings and return its result dict.

    commentary: optional callable(event_key, batsman, bowler, fielder, keeper,
                runs) -> str|None used to render a line per ball.
    feed: optional list to append ball-by-ball commentary entries to.
    """
    order = sorted(batting_xi, key=lambda p: p.get("bat_rating", 0) or p.get("rating", 0),
                   reverse=True)
    plan = _build_bowling_plan(bowling_xi, overs)
    fielders, keeper = _fielder_keeper(bowling_xi)

    bat_stats = {id(p): _new_bat_stat() for p in order}
    bowl_stats = {id(p): _new_bowl_stat() for p in plan}

    total_runs = 0
    total_wkts = 0
    extras = {"wides": 0, "noballs": 0, "legbyes": 0}
    fow = []  # [(runs, wkts, name, over_str)]
    timeline = []
    over_summaries = []

    striker_i, non_striker_i, next_i = 0, 1, 2
    free_hit = False
    chased = False

    def _balls_to_overs(b):
        return f"{b // 6}.{b % 6}"

    def _batting_snapshot():
        active = []
        for idx in (striker_i, non_striker_i):
            if idx >= len(order):
                continue
            p = order[idx]
            bs = bat_stats[id(p)]
            active.append({
                "name": p["name"],
                "runs": bs["runs"],
                "balls": bs["balls"],
                "fours": bs["fours"],
                "sixes": bs["sixes"],
                "out": bs["out"],
                "striker": idx == striker_i,
            })
        return active

    def _emit(event_key, batsman, bowler, runs, legal_balls):
        line = None
        if commentary:
            try:
                line = commentary(event_key, batsman, bowler,
                                  random.choice(fielders), keeper, runs)
            except Exception:
                line = None
        if feed is not None:
            feed.append({
                "innings": innings_no,
                "over": _balls_to_overs(legal_balls),
                "score": f"{total_runs}/{total_wkts}",
                "striker": batsman,
                "bowler": bowler,
                "event": event_key,
                "text": line or "",
            })

    def _record_over_summary(over_no, bowler, over_timeline, completed_balls):
        summary = {
            "innings": innings_no,
            "over": over_no,
            "over_label": _balls_to_overs(completed_balls),
            "batting_team": batting_team,
            "bowling_team": bowling_team,
            "team_score": f"{total_runs}/{total_wkts}",
            "batsmen_score": _batting_snapshot(),
            "bowler": bowler["name"],
            "over_timeline": list(over_timeline),
        }
        over_summaries.append(summary)
        if feed is not None:
            current_striker = order[striker_i]["name"] if striker_i < len(order) else ""
            feed.append({
                "innings": innings_no,
                "over": _balls_to_overs(completed_balls),
                "score": summary["team_score"],
                "striker": current_striker,
                "bowler": bowler["name"],
                "event": "end_of_over",
                "team_score": summary["team_score"],
                "batsmen_score": summary["batsmen_score"],
                "over_timeline": summary["over_timeline"],
                "text": (f"End of over {over_no}: {batting_team} "
                         f"{summary['team_score']}"),
            })

    legal_balls = 0
    for over_idx, bowler in enumerate(plan):
        if total_wkts >= 10 or chased:
            break
        bw = bowl_stats[id(bowler)]
        over_bowler_runs = 0
        over_had_extra = False
        balls_this_over = 0
        over_timeline = []
        while balls_this_over < 6:
            if total_wkts >= 10 or chased:
                break
            striker = order[striker_i]
            bs = bat_stats[id(striker)]
            shot = _pick_shot(over_idx + 1, overs, total_wkts)
            variation, length = _pick_delivery(bowler)
            oc = calculate_outcome(
                bowler.get("bowl_style"), bowler.get("bowl_hand"),
                variation, length, pitch_type,
                over_idx + 1, overs, shot,
                striker.get("bat_rating", 0) or striker.get("rating", 0),
                bowler.get("bowl_rating", 0),
                free_hit=free_hit,
            )
            otype = oc["type"]

            if otype == "wide":
                total_runs += 1
                bw["runs"] += 1
                extras["wides"] += 1
                over_bowler_runs += 1
                over_had_extra = True
                timeline.append("WD")
                over_timeline.append("WD")
                _emit("wide", striker["name"], bowler["name"], 0, legal_balls)
                if target is not None and total_runs >= target:
                    chased = True
                continue  # not a legal ball, no strike change

            if otype == "noball":
                runs = oc.get("runs", 0)
                total_runs += 1 + runs
                bw["runs"] += 1 + runs
                extras["noballs"] += 1
                over_bowler_runs += 1 + runs
                over_had_extra = True
                if runs:
                    bs["runs"] += runs
                    if runs == 4:
                        bs["fours"] += 1
                    elif runs == 6:
                        bs["sixes"] += 1
                timeline.append("NB")
                over_timeline.append("NB")
                _emit("no_ball", striker["name"], bowler["name"], runs, legal_balls)
                if target is not None and total_runs >= target:
                    chased = True
                free_hit = True
                if runs % 2 == 1:
                    striker_i, non_striker_i = non_striker_i, striker_i
                continue  # not a legal ball

            # ---- legal ball from here ----
            balls_this_over += 1
            legal_balls += 1
            bw["balls"] += 1
            bs["balls"] += 1

            if otype == "legbye":
                runs = oc.get("runs", 1)
                total_runs += runs
                extras["legbyes"] += runs  # not charged to bowler
                over_had_extra = True
                timeline.append("LB")
                over_timeline.append("LB")
                _emit("extras", striker["name"], bowler["name"], runs, legal_balls)
                if runs % 2 == 1:
                    striker_i, non_striker_i = non_striker_i, striker_i

            elif otype == "wicket":
                how = oc.get("how", "Caught")
                runs = oc.get("runs", 0)  # run-out can have completed runs
                if runs:
                    total_runs += runs
                    bs["runs"] += runs
                    over_bowler_runs += runs
                    bw["runs"] += runs
                total_wkts += 1
                bs["out"] = True
                bs["how"] = how
                bs["bowler"] = bowler["name"]
                if how != "Run Out":
                    bw["wickets"] += 1
                timeline.append("W")
                over_timeline.append("W")
                fow.append((total_runs, total_wkts, striker["name"],
                            _balls_to_overs(legal_balls)))
                _emit(_HOW_TO_EVENT.get(how, "wicket_caught_fielder"),
                      striker["name"], bowler["name"], 0, legal_balls)
                free_hit = False
                # Next batsman comes in at the striker's end. When nobody is
                # left to partner, the side is all out.
                if next_i < len(order):
                    striker_i = next_i
                    next_i += 1
                else:
                    total_wkts = 10  # all out
            else:  # runs
                runs = oc.get("runs", 0)
                total_runs += runs
                bs["runs"] += runs
                over_bowler_runs += runs
                bw["runs"] += runs
                if runs == 4:
                    bs["fours"] += 1
                elif runs == 6:
                    bs["sixes"] += 1
                timeline.append(str(runs))
                over_timeline.append(str(runs))
                _emit(_RUNS_TO_EVENT.get(runs, "general"),
                      striker["name"], bowler["name"], runs, legal_balls)
                free_hit = False
                if runs % 2 == 1:
                    striker_i, non_striker_i = non_striker_i, striker_i

            if target is not None and total_runs >= target:
                chased = True

        # end of over
        if balls_this_over >= 6:
            if over_bowler_runs == 0 and not over_had_extra:
                bw["maidens"] += 1
            _record_over_summary(over_idx + 1, bowler, over_timeline, legal_balls)
            # swap strike at end of over
            striker_i, non_striker_i = non_striker_i, striker_i

    opening_bowler_name = plan[0]["name"] if plan else ""
    opening_striker_name = order[0]["name"] if order else ""
    opening_non_striker_name = order[1]["name"] if len(order) > 1 else ""

    return {
        "innings": innings_no,
        "batting_team": batting_team,
        "bowling_team": bowling_team,
        "openers": [name for name in (opening_striker_name, opening_non_striker_name) if name],
        "opening_striker": opening_striker_name,
        "opening_bowler": opening_bowler_name,
        "innings_intro": [
            f"INNINGS {innings_no}",
            f"Batting {batting_team}",
            f"Bowling {bowling_team}",
            (f"{opening_striker_name} and {opening_non_striker_name} will open the attack "
             f"for {batting_team}. {opening_striker_name} is on strike."
             if opening_striker_name and opening_non_striker_name else ""),
            (f"{opening_bowler_name} will bowl the opening over for {bowling_team}"
             if opening_bowler_name else ""),
        ],
        "runs": total_runs,
        "wickets": total_wkts,
        "overs": _balls_to_overs(legal_balls),
        "balls": legal_balls,
        "extras": extras,
        "extras_total": extras["wides"] + extras["noballs"] + extras["legbyes"],
        "fow": fow,
        "timeline": timeline,
        "over_summaries": over_summaries,
        "order": order,
        "bat_stats": {id(p): bat_stats[id(p)] for p in order},
        "bowl_plan": plan,
        "bowl_stats": bowl_stats,
        "_order_objs": order,
    }


def simulate_match(home_xi, away_xi, overs, pitch_type,
                   home_name, away_name, toss_winner=None,
                   toss_decision="bat", commentary=None):
    """Simulate a full two-innings match.

    home_xi / away_xi: lists of player dicts with keys: name, rating,
        bat_rating, bowl_rating, category, bowl_style, bowl_hand, bat_hand.
    Returns a dict with both innings, the result, and a commentary feed.
    """
    # Toss winner may choose to bat or bowl first (default: bat for backward compatibility).
    normalized_decision = str(toss_decision or "bat").strip().lower()
    if normalized_decision not in ("bat", "bowl"):
        normalized_decision = "bat"

    teams_by_name = {home_name: home_xi, away_name: away_xi}
    if toss_winner not in teams_by_name:
        toss_winner = home_name

    toss_winner_is_away = toss_winner == away_name
    toss_loser_name = home_name if toss_winner_is_away else away_name

    if normalized_decision == "bat":
        first_name = toss_winner
        second_name = toss_loser_name
    else:
        first_name = toss_loser_name
        second_name = toss_winner

    first_bat, first_bowl = teams_by_name[first_name], teams_by_name[second_name]

    feed = []
    inn1 = simulate_innings(first_bat, first_bowl, overs, pitch_type,
                            1, first_name, second_name,
                            target=None, commentary=commentary, feed=feed)
    target = inn1["runs"] + 1
    inn2 = simulate_innings(first_bowl, first_bat, overs, pitch_type,
                            2, second_name, first_name,
                            target=target, commentary=commentary, feed=feed)

    result = _compute_result(inn1, inn2, target)
    return {
        "overs": overs,
        "pitch": pitch_type,
        "innings1": inn1,
        "innings2": inn2,
        "target": target,
        "result": result,
        "toss": {
            "winner": toss_winner,
            "decision": normalized_decision,
            "text": f"{toss_winner} won the toss and elected to {normalized_decision.title()} first",
        },
        "commentary_feed": feed,
        "potm": _player_of_the_match(inn1, inn2, result),
    }


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_innings_card(inn):
    """Render a single innings as an HTML scorecard for Telegram."""
    e = inn["extras"]
    lines = [
        f"🏏 <b>{_esc(inn['batting_team'])}</b>",
        f"<b>{inn['runs']}/{inn['wickets']}</b> ({inn['overs']} ov)",
        "━━━━━━━━━━━━━━━━━━━",
        "<b>BATTING</b>",
    ]
    rows = []
    dnb = []
    for p in inn["order"]:
        bs = inn["bat_stats"][id(p)]
        if bs["balls"] == 0 and not bs["out"]:
            dnb.append(p["name"])
            continue
        sr = round(bs["runs"] / bs["balls"] * 100, 1) if bs["balls"] else 0.0
        mark = "" if bs["out"] else "*"
        dismissal = f" [{_esc(bs['how'])}]" if bs["out"] and bs["how"] else ""
        rows.append(
            f"{_esc(p['name'])} {mark}{bs['runs']} ({bs['balls']}){dismissal} "
            f"4s:{bs['fours']} 6s:{bs['sixes']} SR:{sr}"
        )
    lines.append("<blockquote>" + "\n".join(rows) + "</blockquote>")
    lines.append(
        f"Extras: {inn['extras_total']} "
        f"(wd {e['wides']}, nb {e['noballs']}, lb {e['legbyes']})"
    )
    lines.append(f"<b>TOTAL: {inn['runs']}/{inn['wickets']} ({inn['overs']} ov)</b>")
    if dnb:
        lines.append(f"<i>DNB: {_esc(', '.join(dnb))}</i>")

    lines.append("━━━━━━━━━━━━━━━━━━━")
    lines.append("<b>BOWLING</b>")
    seen = set()
    brows = []
    for bp in inn["bowl_plan"]:
        if id(bp) in seen:
            continue
        seen.add(id(bp))
        bw = inn["bowl_stats"][id(bp)]
        ov = f"{bw['balls'] // 6}.{bw['balls'] % 6}"
        econ = round(bw["runs"] / (bw["balls"] / 6), 2) if bw["balls"] else 0.0
        brows.append(
            f"{_esc(bp['name'])} {ov}-{bw['maidens']}-{bw['runs']}-{bw['wickets']} (econ {econ})"
        )
    lines.append("<blockquote>" + "\n".join(brows) + "</blockquote>")

    if inn["fow"]:
        fow = " · ".join(f"{r}/{w} ({_esc(nm)}, {ov})" for r, w, nm, ov in inn["fow"])
        lines.append(f"<b>FoW:</b> {fow}")
    return "\n".join(lines)


def render_result(match):
    """Render the final result / winner announcement."""
    res = match["result"]
    i1, i2 = match["innings1"], match["innings2"]
    return (
        "🏆 <b>MATCH RESULT</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"{_esc(i1['batting_team'])}: <b>{i1['runs']}/{i1['wickets']}</b> ({i1['overs']})\n"
        f"{_esc(i2['batting_team'])}: <b>{i2['runs']}/{i2['wickets']}</b> ({i2['overs']})\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🎉 <b>{_esc(res['text'])}</b>\n"
        f"🌟 Player of the Match: <b>{_esc(match['potm'])}</b>"
    )


def _top_batters(inn, limit=4):
    rows = []
    for p in inn["order"]:
        bs = inn["bat_stats"][id(p)]
        if bs["balls"] == 0 and not bs["out"]:
            continue
        rows.append({
            "name": p["name"],
            "runs": bs["runs"],
            "balls": bs["balls"],
            "fours": bs["fours"],
            "sixes": bs["sixes"],
            "out": bs["out"],
        })
    return sorted(rows, key=lambda r: (r["runs"], -r["balls"]), reverse=True)[:limit]


def _top_bowlers(inn, limit=4):
    rows = []
    seen = set()
    for bp in inn["bowl_plan"]:
        if id(bp) in seen:
            continue
        seen.add(id(bp))
        bw = inn["bowl_stats"][id(bp)]
        if bw["balls"] == 0:
            continue
        rows.append({
            "name": bp["name"],
            "wickets": bw["wickets"],
            "runs": bw["runs"],
            "overs": f"{bw['balls'] // 6}.{bw['balls'] % 6}",
        })
    return sorted(rows, key=lambda r: (r["wickets"], -r["runs"]), reverse=True)[:limit]


def _potm_stats(match):
    name = match.get("potm")
    if not name:
        return None
    runs = wickets = 0
    for inn in (match["innings1"], match["innings2"]):
        for p in inn["order"]:
            if p["name"] == name:
                runs += inn["bat_stats"][id(p)]["runs"]
        seen = set()
        for bp in inn["bowl_plan"]:
            if id(bp) in seen:
                continue
            seen.add(id(bp))
            if bp["name"] == name:
                wickets += inn["bowl_stats"][id(bp)]["wickets"]
    bits = []
    if runs:
        bits.append(f"{runs} runs")
    if wickets:
        bits.append(f"{wickets} wkts")
    return ", ".join(bits) or "Impact performance"


def render_match_summary_image(match, *, text_settings=None, stadium=None, match_no=None):
    """Render the /sim match summary PNG bytes, or None if rendering fails."""
    from services.match_summary_card import generate_match_summary

    i1, i2 = match["innings1"], match["innings2"]
    res = match["result"]
    winner = res.get("winner") or "Tied"
    margin = res.get("text") or "Match tied"
    top_per_team = {
        "inn1": {
            "team": i1["batting_team"],
            "batters": _top_batters(i1),
            "bowlers": _top_bowlers(i1),
        },
        "inn2": {
            "team": i2["batting_team"],
            "batters": _top_batters(i2),
            "bowlers": _top_bowlers(i2),
        },
    }
    return generate_match_summary(
        inn1_team=i1["batting_team"],
        inn1_runs=i1["runs"],
        inn1_wickets=i1["wickets"],
        inn1_overs=i1["overs"],
        inn2_team=i2["batting_team"],
        inn2_runs=i2["runs"],
        inn2_wickets=i2["wickets"],
        inn2_overs=i2["overs"],
        winner_name=winner,
        win_margin_text=margin,
        overs_total=match["overs"],
        potm_name=match.get("potm"),
        potm_stats=_potm_stats(match),
        top_per_team=top_per_team,
        stadium=stadium or match.get("pitch"),
        match_date=datetime.utcnow(),
        match_no=match_no,
        text_settings=text_settings,
    )


def _compute_result(inn1, inn2, target):
    chasing_runs = inn2["runs"]
    chasing_wkts = inn2["wickets"]
    chasing_name = inn2["batting_team"]
    defending_name = inn1["batting_team"]
    if chasing_runs >= target:
        wkts_left = max(0, 10 - chasing_wkts)
        return {"winner": chasing_name, "loser": defending_name,
                "margin_type": "wickets", "margin": wkts_left,
                "text": f"{chasing_name} won by {wkts_left} wicket"
                        f"{'s' if wkts_left != 1 else ''}"}
    runs_short = target - 1 - chasing_runs
    if runs_short == 0:
        return {"winner": None, "loser": None, "margin_type": "tie",
                "margin": 0, "text": "Match tied"}
    return {"winner": defending_name, "loser": chasing_name,
            "margin_type": "runs", "margin": runs_short,
            "text": f"{defending_name} won by {runs_short} run"
                    f"{'s' if runs_short != 1 else ''}"}


def _player_of_the_match(inn1, inn2, result):
    """Highest impact player by runs + wickets*25 across both innings."""
    # Aggregate runs (from batting) and wickets (from bowling) per name.
    impact = {}
    for inn in (inn1, inn2):
        for p in inn["order"]:
            impact.setdefault(p["name"], 0)
            impact[p["name"]] += inn["bat_stats"][id(p)]["runs"]
        seen_bowlers = set()
        for bp in inn["bowl_plan"]:
            if id(bp) in seen_bowlers:
                continue
            seen_bowlers.add(id(bp))
            impact.setdefault(bp["name"], 0)
            impact[bp["name"]] += inn["bowl_stats"][id(bp)]["wickets"] * 25
    if not impact:
        return None
    return max(impact.items(), key=lambda kv: kv[1])[0]
