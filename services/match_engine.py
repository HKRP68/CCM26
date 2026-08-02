"""Match engine — state management for ball-by-ball play."""

import html
import random


def _active_players(players):
    return [
        p for p in (players or [])
        if not isinstance(p, dict) or p.get("active", True) is not False
    ]


# Timeline ball symbols
SYM = {
    0: "0️⃣", 1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣",
    "W": "🟥", "WD": "↔️", "NB": "🄽🄱", "LB": "𓂾",
}


def create_match_state(match_id, overs, bat_user_id, bowl_user_id,
                       bat_xi, bowl_xi, opener1, opener2, bowler):
    bat_stats = {}
    for p in bat_xi:
        bat_stats[str(p["roster_id"])] = {
            "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "dots": 0,
            "out": False, "how_out": "", "bowled_by": "",
        }
    bowl_stats = {}
    for p in bowl_xi:
        bowl_stats[str(p["roster_id"])] = {
            "balls": 0, "runs": 0, "wickets": 0, "dots": 0,
            "overs_done": 0, "this_over_balls": 0,
            "maidens": 0,
            "this_over_runs": 0,
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
        "timeline": [],
        "over_runs": [],        # list of runs scored each completed over
        "partnership_runs": 0, "partnership_balls": 0,
        "partnership_history": [],  # [{runs, balls, batsman1, batsman2}]
        # Live-match mechanics (UnderCover /cric parity)
        "free_hit": False,          # next legal ball is a free hit (set after a no-ball)
        "mystery_active": False,    # this ball is a "mystery ball" (rolled per ball)
        "delivery_history": [],     # delivery strings bowled this over (spam penalty)
        "recent_runs_window": [],   # batting runs over last ~12 balls (momentum)
        "consec_wickets": 0,        # wickets in a row for the bowling side (momentum)
        "chat_id": None,
        # 1st innings result (saved after innings 1 ends)
        "inn1_runs": 0, "inn1_wickets": 0, "inn1_overs": "",
        "inn1_team": "",
        "inn1_over_runs": [],
    }


def note_bowler_ball(bws, *, bowler_wicket):
    """Record one LEGAL delivery against a bowler's hat-trick streak.

    Every ball loop kept ``wickets`` per bowler but nothing ever wrote the
    ``hattrick`` flag the quest tracker reads, so 'take a hat-trick' quests
    could never complete — and a career hat-trick quest put the weekly streak
    jackpot permanently out of reach. This is the one place that flag is set.

    Call once per legal ball, after the outcome has been applied.
    ``bowler_wicket`` is True only for a dismissal credited to the bowler (a
    run-out is not), and the streak resets on any other legal delivery — three
    *consecutive* deliveries is what makes a hat-trick. Wides and no-balls are
    not legal deliveries, so callers skip them and a hat-trick survives them.
    """
    if not isinstance(bws, dict):
        return
    if bowler_wicket:
        streak = bws.get("wkt_streak", 0) + 1
        bws["wkt_streak"] = streak
        if streak >= 3:
            bws["hattrick"] = True
    else:
        bws["wkt_streak"] = 0


def get_striker(s):
    return s["batting_order"][s["striker_idx"]]

def get_non_striker(s):
    return s["batting_order"][s["non_striker_idx"]]

def get_bowler(s):
    return s["current_bowler"]


def chase_requirements(s):
    """Return target, runs required, and balls remaining for a chase.

    Values are clamped at zero so callers never display negative requirements
    after the winning ball. The target is always the first-innings score + 1,
    as set by :func:`transition_to_second_innings`.
    """
    if s.get("innings") != 2 or not s.get("target"):
        return None
    # The Hundred is played as 20 units of 5 balls (100); every other format is
    # 6-ball overs. Non-Hundred states carry no ball_format, so this is a no-op
    # for the standard /wpm, /cm and tournament paths.
    bpu = 5 if s.get("ball_format") == "The100" else 6
    balls_played = ((s.get("current_over", 1) - 1) * bpu
                    + s.get("current_ball", 0))
    return {
        "target": int(s["target"]),
        "runs_required": max(0, int(s["target"]) - int(s.get("total_runs", 0))),
        "balls_remaining": max(0, int(s.get("overs", 0)) * bpu - balls_played),
    }


def is_innings_over(s):
    # A chase ends on the winning ball, before any wicket/over follow-up can
    # ask for another batsman or bowler.
    chase = chase_requirements(s)
    if chase and chase["runs_required"] == 0:
        return True
    if s["total_wickets"] >= s.get("wicket_limit", 10):
        return True
    total_balls = (s["current_over"] - 1) * 6 + s["current_ball"]
    if total_balls >= s["overs"] * 6:
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
    rid = s["current_bowler"]["roster_id"]
    bw = s["bowl_stats"].get(str(rid)) or s["bowl_stats"].get(rid) or {}
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


def build_chemistry_line(s):
    """Team Chemistry block for the live board — both sides, or '' if unscorable.

    Chemistry already decides real things in a live match (a role bonus on every
    effective rating, a fielding-quality bonus, a doubled effect at the death),
    but until now the only place a player could see the number was /cmuchem
    before the toss. Traits announce themselves ball by ball; chemistry should
    too, so it sits on the board next to the partnership it feeds.

    Renders nothing rather than a wrong number when a side can't be scored — a
    bot/synthetic XI carries no country or card version, and inventing 30/100
    for it would read as a bug.
    """
    try:
        from services import chemistry
        bat = chemistry.live_badge(s.get("bat_xi") or [])
        bowl = chemistry.live_badge(s.get("bowl_xi") or [])
    except Exception:
        return ""
    # All or nothing. One side's number alone invites a comparison that can't be
    # made — and the side that renders nothing is precisely the synthetic one.
    if not bat or not bowl:
        return ""
    bat_name = html.escape(str(s.get("bat_team_name") or "Batting"))
    bowl_name = html.escape(str(s.get("bowl_team_name") or "Bowling"))
    return ("🧪 <b>TEAM CHEMISTRY</b>\n"
            f"🏏 {bat_name}: {bat}\n"
            f"🎯 {bowl_name}: {bowl}\n\n")


def build_bond_line(s, striker, non_striker):
    """Partnership-bond cue for the pair at the crease, or ''.

    The bond is the one live-moving part of chemistry — it changes on every
    wicket and every strike rotation — so it is called out on the partnership
    line itself rather than in the static team block above.
    """
    try:
        from services import chemistry
        label = chemistry.bond_label(
            chemistry.partnership_bond(striker, non_striker))
    except Exception:
        return ""
    return f"\n{label}" if label else ""


def build_live_scorecard(s):
    """Build the live match update message."""
    striker = get_striker(s)
    non_striker = get_non_striker(s)
    bowler = get_bowler(s)
    bs_strike = s["bat_stats"].get(str(striker["roster_id"])) or s["bat_stats"].get(striker["roster_id"]) or {}
    bs_non = s["bat_stats"].get(str(non_striker["roster_id"])) or s["bat_stats"].get(non_striker["roster_id"]) or {}

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

    chem_line = build_chemistry_line(s)
    bond_line = build_bond_line(s, striker, non_striker)

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
        f"{chem_line}"
        f"🤝 <b>PARTNERSHIP</b>  ➤ {s['partnership_runs']} ({s['partnership_balls']})"
        f"{bond_line}\n\n"
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

    # Snapshot 1st innings for the scorecard (preserve string keys)
    s["inn1_bat_stats"] = {str(k): v for k, v in s["bat_stats"].items()}
    s["inn1_bowl_stats"] = {str(k): v for k, v in s["bowl_stats"].items()}
    s["inn1_bat_team_id"] = s.get("bat_team_id")
    s["inn1_bowl_team_id"] = s.get("bowl_team_id")
    s["inn1_bat_xi"] = list(s["bat_xi"])
    s["inn1_bowl_xi"] = list(s["bowl_xi"])
    s["inn1_batting_order"] = list(s.get("batting_order", []))
    # Snapshot fall-of-wickets + extras so the innings-1 batting/bowling cards
    # can be rendered after completion (mirrors handlers.match._end_innings).
    s["inn1_fow"] = list(s.get("fow", []))
    s["inn1_wides"] = s.get("wides", 0)
    s["inn1_noballs"] = s.get("noballs", 0)
    s["inn1_legbyes"] = s.get("legbyes", 0)

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
    # Reset live-match mechanics for the new innings
    s["free_hit"] = False
    s["mystery_active"] = False
    s["delivery_history"] = []
    s["recent_runs_window"] = []
    s["consec_wickets"] = 0
    # Clear sequence-aware commentary flags so the first ball of the chase
    # can't inherit innings-1's last delivery (back-to-back / post-wicket /
    # dot-streak narratives).
    s["last_ball_boundary"] = False
    s["last_ball_wicket"] = False
    s["cmt_consec_dots"] = 0
    s["bat_team_id"], s["bowl_team_id"] = s.get("bowl_team_id"), s.get("bat_team_id")
    s["bat_user_tg"], s["bowl_user_tg"] = s.get("bowl_user_tg"), s.get("bat_user_tg")
    s["bat_team_name"], s["bowl_team_name"] = s["bowl_team_name"], s["bat_team_name"]
    s["bat_username"], s["bowl_username"] = s.get("bowl_username"), s.get("bat_username")
    s["bat_xi"], s["bowl_xi"] = s["bowl_xi"], s["bat_xi"]
    s["batting_order"] = list(_active_players(s["bat_xi"]))
    s["striker_idx"] = 0
    s["non_striker_idx"] = 1
    s["next_batsman_idx"] = 2
    s["prev_bowler_rid"] = None
    s["selected_variation"] = None
    s["current_bowler"] = None
    s["bat_stats"] = {str(p["roster_id"]): {"runs": 0, "balls": 0, "fours": 0,
                                             "sixes": 0, "out": False, "how_out": "",
                                             "bowled_by": ""} for p in s["bat_xi"]}
    s["bowl_stats"] = {str(p["roster_id"]): {"balls": 0, "runs": 0, "wickets": 0,
                                              "overs_done": 0, "this_over_balls": 0,
                                              "maidens": 0, "this_over_runs": 0}
                       for p in s["bowl_xi"]}
    # Save innings 1 over-by-over data and partnerships before resetting
    s["inn1_over_runs"] = list(s.get("over_runs", []))
    s["over_runs"] = []
    s["inn1_partnership_history"] = list(s.get("partnership_history", []))
    s["partnership_history"] = []
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
        # Chasing side won — by wickets remaining. Challenge mode uses a
        # two-wicket innings, so respect the state's wicket limit instead of
        # assuming a ten-wicket match.
        wickets_left = max(0, int(s.get("wicket_limit", 10)) - chasing_wkts)
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
                "margin_type": "tie", "margin_value": 0, "text": "Match Tied"}
    return {"winner_team_id": defending_team_id, "loser_team_id": chasing_team_id,
            "margin_type": "runs", "margin_value": runs_short,
            "text": f"{defending_name} won by {runs_short} run"
                    f"{'s' if runs_short != 1 else ''}"}
