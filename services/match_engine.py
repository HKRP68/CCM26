"""Match engine — state management for ball-by-ball play."""

import random

# Timeline ball symbols
SYM = {
    0: "0️⃣", 1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣",
    "W": "🟥", "WD": "↔️", "NB": "🄽🄱", "LB": "𓂾",
}


def create_match_state(match_id, overs, bat_user_id, bowl_user_id,
                       bat_xi, bowl_xi, opener1, opener2, bowler):
    bat_stats = {}
    for p in bat_xi:
        bat_stats[p["roster_id"]] = {
            "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
            "out": False, "how_out": "", "bowled_by": "",
        }
    bowl_stats = {}
    for p in bowl_xi:
        bowl_stats[p["roster_id"]] = {
            "balls": 0, "runs": 0, "wickets": 0,
            "overs_done": 0, "this_over_balls": 0,
            "maidens": 0,            # NEW: count of overs with 0 runs conceded
            "this_over_runs": 0,     # NEW: runs in current over (reset every over)
        }

    order = [opener1, opener2]
    for p in bat_xi:
        if p["roster_id"] not in (opener1["roster_id"], opener2["roster_id"]):
            order.append(p)

    return {
        "match_id": match_id, "overs": overs,
        "innings": 1, "target": None,
        "bat_team_id": bat_user_id, "bowl_team_id": bowl_user_id,
        "bat_xi": bat_xi, "bowl_xi": bowl_xi,
        "batting_order": order,
        "current_over": 1, "current_ball": 0,
        "total_runs": 0, "total_wickets": 0,
        "extras_total": 0, "wides": 0, "noballs": 0, "legbyes": 0, "byes": 0,
        "striker_idx": 0, "non_striker_idx": 1, "next_batsman_idx": 2,
        "current_bowler": bowler,
        "prev_bowler_rid": None,
        "selected_variation": None,
        "bat_stats": bat_stats, "bowl_stats": bowl_stats,
        "over_balls": [],
        "timeline": [],  # last N ball symbols for display
        "partnership_runs": 0, "partnership_balls": 0,
        "chat_id": None,
        # 1st innings result (saved after innings 1 ends)
        "inn1_runs": 0, "inn1_wickets": 0, "inn1_overs": "",
        "inn1_team": "",
    }


def get_striker(s):
    return s["batting_order"][s["striker_idx"]]

def get_non_striker(s):
    return s["batting_order"][s["non_striker_idx"]]

def get_bowler(s):
    return s["current_bowler"]


def is_innings_over(s):
    if s["total_wickets"] >= 10:
        return True
    total_balls = (s["current_over"] - 1) * 6 + s["current_ball"]
    if total_balls >= s["overs"] * 6:
        return True
    if s["innings"] == 2 and s["target"] and s["total_runs"] >= s["target"]:
        return True
    return False


def format_score(s):
    return f"{s['total_runs']}/{s['total_wickets']}"

def format_overs(s):
    c = s["current_over"] - 1
    b = s["current_ball"]
    return f"{c + 1}.0" if b == 6 else f"{c}.{b}"

def crr(s):
    tb = (s["current_over"] - 1) * 6 + s["current_ball"]
    return round((s["total_runs"] / tb) * 6, 2) if tb else 0.0

def rrr(s):
    if s["innings"] != 2 or not s["target"]:
        return None
    needed = s["target"] - s["total_runs"]
    tb = s["overs"] * 6 - ((s["current_over"] - 1) * 6 + s["current_ball"])
    return round((needed / tb) * 6, 2) if tb > 0 else 999.0

def get_phase(s):
    ov, tot = s["current_over"], s["overs"]
    if tot <= 5:
        return "T20 Blast"
    if ov <= 6:
        return "Powerplay"
    elif ov <= tot - 4:
        return "Middle Overs"
    return "Death Overs"


def add_to_timeline(s, symbol):
    s["timeline"].append(symbol)
    if len(s["timeline"]) > 12:
        s["timeline"] = s["timeline"][-12:]


def format_timeline(s):
    return " ".join(s["timeline"][-10:]) if s["timeline"] else ""


def bowler_figures(s):
    """Return string like '1.3 • 13 • 1' for current bowler."""
    bw = s["bowl_stats"].get(s["current_bowler"]["roster_id"], {})
    done = bw.get("overs_done", 0)
    extra = bw.get("this_over_balls", 0)
    ov_str = f"{done}.{extra}" if extra else f"{done}"
    return f"{ov_str} • {bw.get('runs', 0)} • {bw.get('wickets', 0)}"


def projected_score(s):
    """Calculate projected score for 1st innings based on current run rate."""
    if s["innings"] != 1:
        return None
    tb = (s["current_over"] - 1) * 6 + s["current_ball"]
    if tb < 6:  # need at least 1 over
        return None
    total_balls = s["overs"] * 6
    rate_per_ball = s["total_runs"] / tb
    return int(rate_per_ball * total_balls)


def build_live_scorecard(s):
    """Build the live match update message."""
    striker = get_striker(s)
    non_striker = get_non_striker(s)
    bowler = get_bowler(s)
    bs_strike = s["bat_stats"][striker["roster_id"]]
    bs_non = s["bat_stats"][non_striker["roster_id"]]

    bat_name = s["bat_team_name"]
    bowl_name = s["bowl_team_name"]

    # In 2nd innings show both scores
    if s["innings"] == 2:
        inn1_line = f"🔴 <b>{s['inn1_team']}</b>\n{s['inn1_runs']}/{s['inn1_wickets']} ({s['inn1_overs']})"
        inn2_line = f"🟢 <b>{bat_name}</b>\n{format_score(s)} ({format_overs(s)} / {s['overs']})"
    else:
        proj = projected_score(s)
        proj_text = f" | Proj: {proj}" if proj else ""
        inn1_line = f"🟢 <b>{bat_name}</b>\n{format_score(s)} ({format_overs(s)} / {s['overs']}){proj_text}"
        inn2_line = f"🔴 <b>{bowl_name}</b>\nYet to bat"

    strike_mark_s = " *" 
    strike_mark_n = ""

    cr = crr(s)
    rr_val = rrr(s)
    rr_line = f"CRR: {cr} ⚡"
    if rr_val is not None:
        rr_line += f"\nRRR: {rr_val} 🎯"
    proj = projected_score(s)
    if proj and s["innings"] == 1:
        rr_line += f"\n📈 Projected: {proj}"

    bf = bowler_figures(s)

    tl = format_timeline(s)

    # Pitch wear note (only when meaningful)
    pitch_line = ""
    try:
        from services.probability_engine import calc_pitch_wear
        wear = calc_pitch_wear(s.get("innings", 1), s.get("current_over", 1), s.get("overs", 20))
        if wear >= 30:
            pitch_type = s.get("pitch_type", "Flat")
            if wear >= 60:
                wear_label = "Heavily Worn 🟫"
            elif wear >= 45:
                wear_label = "Worn 🟤"
            elif wear >= 30:
                wear_label = "Moderately Worn 🟧"
            else:
                wear_label = "Fresh 🟢"
            pitch_line = f"\n📍 <b>{pitch_type}</b> · {wear_label}\n"
    except Exception:
        pass

    return (
        f"🏏 <b>LIVE MATCH UPDATE</b>\n\n"
        f"{inn1_line}\n\n"
        f"{inn2_line}\n"
        f"{pitch_line}\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏏 <b>BATSMAN</b>\n"
        f"✦ {striker['name']:<18} {bs_strike['runs']} ({bs_strike['balls']}){strike_mark_s}\n"
        f"  {non_striker['name']:<18} {bs_non['runs']} ({bs_non['balls']}){strike_mark_n}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤝 <b>PARTNERSHIP</b>  ➤ {s['partnership_runs']} ({s['partnership_balls']})\n\n"
        f"📊 <b>RUN RATE</b>\n{rr_line}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 <b>BOWLER</b>\n"
        f"{bowler['name']}\n{bf}\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n\n"
        f"⏱ <b>TIMELINE</b>\n➤ {tl}\n\n"
        f"━━━━━━━━━━━━━━━━━━━"
    )


# ══════════════════ Pure innings/result helpers (shared) ═════════════
# These contain NO Telegram/ctx code so both the bot and the Mini App can use
# them. They mutate/read the state dict only. (Addresses scoring-path drift.)

def transition_to_second_innings(s):
    """Pure: end of 1st innings → set up 2nd innings (target, team swap, resets).
    Mirrors the bookkeeping in handlers.match._end_innings (innings 1 branch),
    minus all the Telegram sends. Returns the target."""
    s["inn1_runs"] = s["total_runs"]
    s["inn1_wickets"] = s["total_wickets"]
    s["inn1_overs"] = format_overs(s)
    s["inn1_team"] = s["bat_team_name"]
    target = s["total_runs"] + 1

    # Snapshot 1st innings for the scorecard
    s["inn1_bat_stats"] = dict(s["bat_stats"])
    s["inn1_bowl_stats"] = dict(s["bowl_stats"])
    s["inn1_bat_team_id"] = s.get("bat_team_id")
    s["inn1_bowl_team_id"] = s.get("bowl_team_id")
    s["inn1_bat_xi"] = list(s["bat_xi"])
    s["inn1_bowl_xi"] = list(s["bowl_xi"])
    s["inn1_batting_order"] = list(s.get("batting_order", []))

    # Reset + swap for 2nd innings
    s["innings"] = 2
    s["target"] = target
    s["total_runs"] = 0
    s["total_wickets"] = 0
    s["extras_total"] = 0
    s["wides"] = 0
    s["noballs"] = 0
    s["legbyes"] = 0
    s["current_over"] = 1
    s["current_ball"] = 0
    s["timeline"] = []
    s["partnership_runs"] = 0
    s["partnership_balls"] = 0
    s["bat_team_id"], s["bowl_team_id"] = s.get("bowl_team_id"), s.get("bat_team_id")
    s["bat_user_tg"], s["bowl_user_tg"] = s.get("bowl_user_tg"), s.get("bat_user_tg")
    s["bat_team_name"], s["bowl_team_name"] = s["bowl_team_name"], s["bat_team_name"]
    s["bat_username"], s["bowl_username"] = s.get("bowl_username"), s.get("bat_username")
    s["bat_xi"], s["bowl_xi"] = s["bowl_xi"], s["bat_xi"]
    s["batting_order"] = list(s["bat_xi"])
    s["striker_idx"] = 0
    s["non_striker_idx"] = 1
    s["next_batsman_idx"] = 2
    s["prev_bowler_rid"] = None
    s["selected_variation"] = None
    s["current_delivery"] = None
    s["last_ball"] = None
    s["current_bowler"] = None
    s["bat_stats"] = {p["roster_id"]: {"runs": 0, "balls": 0, "fours": 0,
                                       "sixes": 0, "out": False, "how_out": "",
                                       "bowled_by": ""} for p in s["bat_xi"]}
    s["bowl_stats"] = {p["roster_id"]: {"balls": 0, "runs": 0, "wickets": 0,
                                        "overs_done": 0, "this_over_balls": 0,
                                        "maidens": 0, "this_over_runs": 0}
                       for p in s["bowl_xi"]}
    s["fow"] = []
    return target


def compute_match_result(s):
    """Pure: determine the result after the 2nd innings.
    Returns dict {winner_team_id, loser_team_id, margin_type, margin_value, text}
    or None if the match isn't actually over."""
    if s.get("innings") != 2:
        return None
    target = s.get("target")
    chasing_runs = s.get("total_runs", 0)
    chasing_wkts = s.get("total_wickets", 0)
    chasing_team_id = s.get("bat_team_id")
    defending_team_id = s.get("bowl_team_id")
    chasing_name = s.get("bat_team_name", "Chasing")
    defending_name = s.get("bowl_team_name", "Defending")

    if not target:
        return None

    if chasing_runs >= target:
        # Chasing side won — by wickets remaining
        wickets_left = 10 - chasing_wkts
        return {"winner_team_id": chasing_team_id, "loser_team_id": defending_team_id,
                "margin_type": "wickets", "margin_value": wickets_left,
                "text": f"{chasing_name} won by {wickets_left} wicket"
                        f"{'s' if wickets_left != 1 else ''}"}
    # Innings over without reaching target
    if not is_innings_over(s):
        return None
    runs_short = target - 1 - chasing_runs
    if runs_short == 0:
        return {"winner_team_id": None, "loser_team_id": None,
                "margin_type": "tie", "margin_value": 0, "text": "Match tied!"}
    return {"winner_team_id": defending_team_id, "loser_team_id": chasing_team_id,
            "margin_type": "runs", "margin_value": runs_short,
            "text": f"{defending_name} won by {runs_short} run"
                    f"{'s' if runs_short != 1 else ''}"}
