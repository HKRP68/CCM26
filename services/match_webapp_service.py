"""Role-aware match snapshots + setup actions for the Mini App.

These functions read/write the SAME live match state the bot uses (via the
DB-backed store), so a match can be played from the bot, the Mini App, or a
mix. Pure-ish helpers here; the Flask endpoints in admin.py wrap them with
auth + JSON.

Roles:
  - "batsman"   → the user whose team is currently batting
  - "bowler"    → the user whose team is currently bowling
  - "spectator" → anyone else (read-only)
"""

import logging

from models import Match, User
from services import match_webapp_access as mwa
from services.match_state_store import (
    A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT, A_PICK_NEW_BATSMAN,
    A_PICK_NEW_BOWLER, A_INNINGS_BREAK, A_COMPLETED,
)

logger = logging.getLogger(__name__)

# Setup-phase actions (before the ball loop): tracked in state["setup"]
SETUP_AWAIT_OPENERS = "AWAIT_OPENERS"
SETUP_AWAIT_BOWLER = "AWAIT_BOWLER"
SETUP_AWAIT_READY = "AWAIT_READY"
SETUP_DONE = "DONE"


def init_match_for_webapp(session, match_id):
    """Create the initial live state for a Mini-App-played match, right after
    the toss. Openers/bowler are placeholders until the teams pick them.
    Returns (ok, msg). Safe to call once; no-op if state already exists.
    """
    from services.match_engine import create_match_state
    from models import UserRoster, Player

    if mwa.get_state(match_id):
        return True, "Already initialized."

    m = session.query(Match).get(match_id)
    if not m or not m.batting_first_id or not m.bowling_first_id:
        return False, "Match toss not completed."

    bu = session.query(User).get(m.batting_first_id)
    bwu = session.query(User).get(m.bowling_first_id)
    if not bu or not bwu:
        return False, "Players missing."

    def _xi(uid):
        rows = (session.query(UserRoster, Player)
                .join(Player, UserRoster.player_id == Player.id)
                .filter(UserRoster.user_id == uid)
                .order_by(UserRoster.order_position).limit(11).all())
        return [{
            "roster_id": e.id, "player_id": p.id, "name": p.name,
            "rating": p.rating, "category": p.category,
            "bat_rating": p.bat_rating, "bowl_rating": p.bowl_rating,
            "bowl_style": p.bowl_style, "bowl_hand": p.bowl_hand,
            "bat_hand": p.bat_hand,
        } for e, p in rows]

    bxi = _xi(bu.id)
    bwxi = _xi(bwu.id)
    if len(bxi) < 2 or len(bwxi) < 1:
        return False, "Both teams need a full XI."

    op1, op2 = bxi[0], bxi[1]   # placeholders, replaced on opener pick
    bowler = bwxi[0]
    s = create_match_state(match_id, m.overs, bu.id, bwu.id, bxi, bwxi,
                           op1, op2, bowler)
    bt = bu.team_name or f"@{bu.username}'s XI"
    bwt = bwu.team_name or f"@{bwu.username}'s XI"
    s["chat_id"] = m.chat_id
    s["bat_user_tg"] = bu.telegram_id
    s["bowl_user_tg"] = bwu.telegram_id
    s["bat_team_name"] = bt
    s["bowl_team_name"] = bwt
    s["bat_username"] = bu.username
    s["bowl_username"] = bwu.username
    s["pitch_type"] = m.pitch_type
    s["setup"] = SETUP_AWAIT_OPENERS
    s["ready_bat"] = False
    s["ready_bowl"] = False
    s["batting_order"] = []
    s["current_bowler"] = None
    s["played_via"] = "webapp"
    mwa.save_state(match_id, s, next_action="SETUP")
    m.status = "playing"
    session.commit()
    return True, "Match initialized for Mini App."


def role_for(state, user_id):
    """Return the user's role in the current innings."""
    if not state:
        return "spectator"
    if user_id == state.get("bat_team_id"):
        return "batsman"
    if user_id == state.get("bowl_team_id"):
        return "bowler"
    return "spectator"


def _bat_card(state, idx):
    order = state.get("batting_order", [])
    if idx is None or idx < 0 or idx >= len(order):
        return None
    p = order[idx]
    st = state.get("bat_stats", {}).get(p["roster_id"], {})
    return {
        "roster_id": p["roster_id"], "name": p["name"],
        "rating": p.get("rating"), "bat_rating": p.get("bat_rating"),
        "runs": st.get("runs", 0), "balls": st.get("balls", 0),
        "fours": st.get("fours", 0), "sixes": st.get("sixes", 0),
        "out": st.get("out", False),
        "sr": (round(st.get("runs", 0) * 100 / st.get("balls", 1), 1)
               if st.get("balls") else 0),
    }


def _bowler_card(state):
    b = state.get("current_bowler")
    if not b:
        return None
    bs = state.get("bowl_stats", {}).get(b["roster_id"], {})
    overs_done = bs.get("overs_done", 0)
    this_over = bs.get("this_over_balls", 0)
    ov_str = f"{overs_done}.{this_over}" if this_over else f"{overs_done}"
    return {
        "roster_id": b["roster_id"], "name": b["name"],
        "rating": b.get("rating"), "bowl_rating": b.get("bowl_rating"),
        "bowl_style": b.get("bowl_style"), "bowl_hand": b.get("bowl_hand"),
        "wickets": bs.get("wickets", 0), "runs": bs.get("runs", 0),
        "overs": ov_str, "balls": bs.get("balls", 0),
    }


def build_snapshot(session, match_id, user_id):
    """Build the polling snapshot for a user. Returns dict or None if no match."""
    state = mwa.get_state(match_id)
    if not state:
        return None
    next_action = mwa.get_next_action(match_id)
    ball_seq = mwa.get_ball_seq(match_id)
    role = role_for(state, user_id)
    setup = state.get("setup")

    m = session.query(Match).get(match_id)
    status = m.status if m else "unknown"

    striker = _bat_card(state, state.get("striker_idx"))
    non_striker = _bat_card(state, state.get("non_striker_idx"))
    bowler = _bowler_card(state)

    # Whose turn is it? (during the ball loop)
    turn = None
    if next_action in (A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_NEW_BOWLER):
        turn = "bowler"
    elif next_action in (A_PICK_SHOT, A_PICK_NEW_BATSMAN):
        turn = "batsman"

    snap = {
        "ok": True,
        "match_id": match_id,
        "status": status,
        "role": role,
        "ball_seq": ball_seq,
        "next_action": next_action,
        "setup": setup,
        "turn": turn,
        "innings": state.get("innings", 1),
        "overs_limit": state.get("overs"),
        "score": {
            "runs": state.get("total_runs", 0),
            "wickets": state.get("total_wickets", 0),
            "over": state.get("current_over", 1) - 1,
            "ball": state.get("current_ball", 0),
            "overs_str": f"{max(0, state.get('current_over',1)-1)}.{state.get('current_ball',0)}",
            "target": state.get("target"),
        },
        "bat_team_name": state.get("bat_team_name", "Batting"),
        "bowl_team_name": state.get("bowl_team_name", "Bowling"),
        "striker": striker,
        "non_striker": non_striker,
        "bowler": bowler,
        "timeline": state.get("timeline", [])[-12:],
        "selected_variation": state.get("selected_variation"),
        "current_delivery": state.get("current_delivery"),
    }

    # Role-specific option payloads for the setup phase
    if role == "batsman" and setup == SETUP_AWAIT_OPENERS:
        snap["openers_options"] = [
            {"roster_id": p["roster_id"], "name": p["name"],
             "bat_rating": p.get("bat_rating"), "rating": p.get("rating"),
             "category": p.get("category")}
            for p in state.get("bat_xi", [])
        ]
    if role == "bowler" and setup == SETUP_AWAIT_BOWLER:
        snap["bowler_options"] = [
            {"roster_id": p["roster_id"], "name": p["name"],
             "bowl_rating": p.get("bowl_rating"), "rating": p.get("rating"),
             "bowl_style": p.get("bowl_style"), "category": p.get("category")}
            for p in state.get("bowl_xi", [])
        ]

    # Ready flags
    snap["ready"] = {
        "batsman": bool(state.get("ready_bat")),
        "bowler": bool(state.get("ready_bowl")),
    }
    return snap


def select_openers(match_id, user_id, striker_rid, non_striker_rid):
    """Batsman picks the two openers. Returns (ok, msg)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found."
    if user_id != state.get("bat_team_id"):
        return False, "Only the batting side picks openers."
    if state.get("setup") != SETUP_AWAIT_OPENERS:
        return False, "Openers already chosen."
    if striker_rid == non_striker_rid:
        return False, "Striker and non-striker must be different players."

    bat_xi = state.get("bat_xi", [])
    by_rid = {p["roster_id"]: p for p in bat_xi}
    if striker_rid not in by_rid or non_striker_rid not in by_rid:
        return False, "Pick players from your XI."

    opener1 = by_rid[striker_rid]
    opener2 = by_rid[non_striker_rid]
    # Rebuild batting order: openers first, then the rest in XI order
    order = [opener1, opener2]
    for p in bat_xi:
        if p["roster_id"] not in (striker_rid, non_striker_rid):
            order.append(p)
    state["batting_order"] = order
    state["striker_idx"] = 0
    state["non_striker_idx"] = 1
    state["next_batsman_idx"] = 2
    state["setup"] = SETUP_AWAIT_BOWLER  # now wait for bowler pick
    mwa.save_state(match_id, state)
    return True, "Openers locked in."


def select_bowler(match_id, user_id, bowler_rid):
    """Bowling side picks the opening bowler. Returns (ok, msg)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found."
    if user_id != state.get("bowl_team_id"):
        return False, "Only the bowling side picks the bowler."
    if state.get("setup") not in (SETUP_AWAIT_BOWLER, SETUP_AWAIT_OPENERS):
        return False, "Bowler already chosen."

    bowl_xi = state.get("bowl_xi", [])
    by_rid = {p["roster_id"]: p for p in bowl_xi}
    if bowler_rid not in by_rid:
        return False, "Pick a bowler from your XI."

    state["current_bowler"] = by_rid[bowler_rid]
    # If openers not yet picked, keep waiting on them; else move to ready
    if state.get("batting_order"):
        state["setup"] = SETUP_AWAIT_READY
    mwa.save_state(match_id, state)
    return True, "Bowler selected."


def mark_ready(match_id, user_id):
    """A side marks itself ready. When both ready, the ball loop begins.
    Returns (ok, both_ready, msg)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, False, "Match not found."
    role = role_for(state, user_id)
    if role == "batsman":
        if not state.get("batting_order"):
            return False, False, "Pick your openers first."
        state["ready_bat"] = True
    elif role == "bowler":
        if not state.get("current_bowler"):
            return False, False, "Pick your bowler first."
        state["ready_bowl"] = True
    else:
        return False, False, "Spectators can't ready up."

    both = bool(state.get("ready_bat") and state.get("ready_bowl"))
    if both:
        state["setup"] = SETUP_DONE
        mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
    else:
        mwa.save_state(match_id, state)
    return True, both, ("Both teams ready — play begins!" if both
                        else "You're ready. Waiting for the other side…")


# ══════════════════ Phase 2: the live ball loop ══════════════════════
# We reuse the bot's outcome engine (_calc → calculate_outcome) so the
# Mini App produces identical outcomes (same probabilities/traits/form).
# Only the deterministic bookkeeping is mirrored here.

from services.bowling_service import (
    get_delivery_options as _get_delivery_options,
    is_spinner as _is_spinner, AVAILABLE_SHOTS,
)
from services.match_engine import (
    get_striker, get_non_striker, get_bowler, is_innings_over,
    add_to_timeline, SYM,
)


def get_bowling_options(match_id, user_id):
    """Return the bowler's delivery options (variations+lengths or spin
    deliveries). Returns dict."""
    state = mwa.get_state(match_id)
    if not state:
        return {"ok": False, "message": "Match not found."}
    if user_id != state.get("bowl_team_id"):
        return {"ok": False, "message": "Only the bowling side delivers."}
    if state.get("setup") not in (None, SETUP_DONE):
        return {"ok": False, "message": "Match not ready yet."}
    bowler = state.get("current_bowler")
    if not bowler:
        return {"ok": False, "message": "No bowler selected."}
    opts = _get_delivery_options(bowler.get("bowl_style", "Medium Pacer"),
                                 bowler.get("bowl_hand", "Right"))
    return {"ok": True, "options": opts,
            "bowler": {"name": bowler["name"], "bowl_style": bowler.get("bowl_style"),
                       "bowl_rating": bowler.get("bowl_rating")}}


def set_delivery(match_id, user_id, variation, length=None):
    """Bowler locks in the delivery (variation + optional length). The batsman
    then sees 'delivery coming' and picks a shot. Returns (ok, msg)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found."
    if user_id != state.get("bowl_team_id"):
        return False, "Only the bowling side delivers."
    na = mwa.get_next_action(match_id)
    if na not in (A_PICK_DELIVERY, A_PICK_LENGTH, "SETUP"):
        return False, "Not your turn to bowl right now."

    bowler = state.get("current_bowler") or {}
    spinner = _is_spinner(bowler.get("bowl_style", ""))
    if spinner:
        delivery = variation  # spinners pick a single delivery
    else:
        if not length:
            # store the variation, wait for length selection
            state["selected_variation"] = variation
            mwa.save_state(match_id, state, next_action=A_PICK_LENGTH)
            return True, "Variation set — now pick a length."
        delivery = f"{variation} {length}".strip()

    state["current_delivery"] = delivery
    state["selected_variation"] = variation
    # Random "speed" flavour for pacers (cosmetic, like the bot)
    if not spinner:
        import random
        base = 125 + int((bowler.get("bowl_rating", 80) - 75) * 0.6)
        state["last_speed"] = max(115, min(155, base + random.randint(-6, 8)))
    mwa.save_state(match_id, state, next_action=A_PICK_SHOT)
    return True, "Delivery on its way — batsman to play."


def _apply_outcome(state, oc, shot, delivery, striker, bowler):
    """Mirror of the bot's _process_shot_core bookkeeping (deterministic given
    the outcome `oc`). Mutates state in place. Returns a result dict."""
    bs = state["bat_stats"].setdefault(striker["roster_id"], {
        "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
        "out": False, "how_out": "", "bowled_by": ""})
    bws = state["bowl_stats"].setdefault(bowler["roster_id"], {
        "balls": 0, "runs": 0, "wickets": 0, "overs_done": 0,
        "this_over_balls": 0, "maidens": 0, "this_over_runs": 0})

    legal = True
    need_new_bat = False
    rtxt = ""
    t = oc["type"]

    if t == "wide":
        state["total_runs"] += 1; state["extras_total"] += 1; state["wides"] += 1
        bws["runs"] += 1; bws["this_over_runs"] = bws.get("this_over_runs", 0) + 1
        add_to_timeline(state, SYM["WD"]); legal = False
        rtxt = "WIDE +1"
    elif t == "noball":
        runs = oc.get("runs", 1)
        state["total_runs"] += runs + 1; state["extras_total"] += 1; state["noballs"] += 1
        bws["runs"] += runs + 1; bs["balls"] += 1
        bws["this_over_runs"] = bws.get("this_over_runs", 0) + runs + 1
        if runs > 0: bs["runs"] += runs
        add_to_timeline(state, SYM["NB"]); legal = False
        rtxt = f"NO BALL +{runs + 1}"
    elif t == "legbye":
        runs = oc.get("runs", 1)
        state["total_runs"] += runs; state["extras_total"] += runs; state["legbyes"] += runs
        bws["runs"] += runs; bs["balls"] += 1
        bws["this_over_runs"] = bws.get("this_over_runs", 0) + runs
        state["partnership_balls"] += 1; state["partnership_runs"] += runs
        add_to_timeline(state, str(runs))
        rtxt = f"LEG BYE +{runs}"
        if runs % 2 == 1:
            state["striker_idx"], state["non_striker_idx"] = state["non_striker_idx"], state["striker_idx"]
    elif t == "wicket":
        runs = oc.get("runs", 0)
        state["total_runs"] += runs; state["total_wickets"] += 1
        bws["wickets"] += 1; bws["runs"] += runs; bs["balls"] += 1; bs["out"] = True
        bws["this_over_runs"] = bws.get("this_over_runs", 0) + runs
        bs["how_out"] = oc.get("how", "Bowled"); bs["bowled_by"] = bowler["name"]
        add_to_timeline(state, SYM["W"])
        state["partnership_runs"] = 0; state["partnership_balls"] = 0
        need_new_bat = True
        rtxt = f"WICKET! {striker['name']} — {oc.get('how','OUT')}"
    else:
        runs = oc.get("runs", 0)
        state["total_runs"] += runs; bs["runs"] += runs; bs["balls"] += 1
        bws["runs"] += runs; state["partnership_runs"] += runs; state["partnership_balls"] += 1
        bws["this_over_runs"] = bws.get("this_over_runs", 0) + runs
        if runs == 4: bs["fours"] += 1
        elif runs == 6: bs["sixes"] += 1
        add_to_timeline(state, SYM.get(runs, str(runs)))
        rtxt = {0: "DOT", 4: "FOUR! 🔥", 6: "SIX! 💥"}.get(runs, f"{runs} run" + ("s" if runs != 1 else ""))
        if runs % 2 == 1:
            state["striker_idx"], state["non_striker_idx"] = state["non_striker_idx"], state["striker_idx"]

    if legal:
        state["current_ball"] += 1
        bws["this_over_balls"] += 1
        bws["balls"] = bws.get("balls", 0) + 1

    eoo = False
    if state["current_ball"] >= 6:
        bws["overs_done"] += 1
        bws["this_over_balls"] = 0
        if bws.get("this_over_runs", 0) == 0:
            bws["maidens"] = bws.get("maidens", 0) + 1
        bws["this_over_runs"] = 0
        state["current_over"] += 1
        state["current_ball"] = 0
        state["striker_idx"], state["non_striker_idx"] = state["non_striker_idx"], state["striker_idx"]
        state["prev_bowler_rid"] = bowler["roster_id"]
        eoo = True

    state["current_delivery"] = None
    state["selected_variation"] = None

    return {"rtxt": rtxt, "type": t, "runs": oc.get("runs", 0),
            "legal": legal, "need_new_bat": need_new_bat, "eoo": eoo,
            "how": oc.get("how"), "traits": oc.get("traits_activated") or []}


def play_shot(match_id, user_id, shot_index):
    """Batsman plays a shot. Resolves the ball through the engine, mutates
    state, advances the loop. Returns (ok, result_dict|msg)."""
    from services.match_state_store import get_match_lock
    import handlers.match as _bm  # for the shared _calc outcome engine

    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found."
    if user_id != state.get("bat_team_id"):
        return False, "Only the batting side plays shots."
    na = mwa.get_next_action(match_id)
    if na != A_PICK_SHOT:
        return False, "No delivery to play right now."
    if shot_index < 0 or shot_index >= len(AVAILABLE_SHOTS):
        return False, "Invalid shot."

    delivery = state.get("current_delivery")
    if not delivery:
        return False, "Bowler hasn't delivered yet."

    shot = AVAILABLE_SHOTS[shot_index]
    striker = get_striker(state)
    bowler = get_bowler(state)

    # Reuse the bot's outcome engine for identical probabilities/traits/form
    oc = _bm._calc(state, striker, bowler, shot, delivery)
    res = _apply_outcome(state, oc, shot, delivery, striker, bowler)

    # Next action
    if is_innings_over(state):
        next_act = A_INNINGS_BREAK
    elif res["need_new_bat"] and state["total_wickets"] < 10:
        next_act = A_PICK_NEW_BATSMAN
        # auto-advance to next batsman
        nb = state.get("next_batsman_idx", 2)
        if nb < len(state.get("batting_order", [])):
            state["striker_idx"] = nb
            state["next_batsman_idx"] = nb + 1
            next_act = A_PICK_DELIVERY  # new batsman in, continue bowling
    elif res["eoo"]:
        next_act = A_PICK_NEW_BOWLER
    else:
        next_act = A_PICK_DELIVERY

    mwa.save_state(match_id, state, next_action=next_act)
    mwa.bump_ball_seq(match_id)

    res["shot"] = shot
    res["delivery"] = delivery
    res["speed"] = state.get("last_speed")
    res["next_action"] = next_act
    res["innings_over"] = is_innings_over(state)
    return True, res


def select_new_bowler(match_id, user_id, bowler_rid):
    """At end of over, bowling side picks the next bowler (can't be same as
    the over just bowled). Returns (ok, msg)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found."
    if user_id != state.get("bowl_team_id"):
        return False, "Only the bowling side picks the bowler."
    na = mwa.get_next_action(match_id)
    if na != A_PICK_NEW_BOWLER:
        return False, "Not time to change bowler."
    by_rid = {p["roster_id"]: p for p in state.get("bowl_xi", [])}
    if bowler_rid not in by_rid:
        return False, "Pick a bowler from your XI."
    if bowler_rid == state.get("prev_bowler_rid"):
        return False, "Same bowler can't bowl consecutive overs."
    state["current_bowler"] = by_rid[bowler_rid]
    mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
    return True, "New bowler set."


def get_new_bowler_options(match_id, user_id):
    """Bowlers eligible for the next over (excludes the one who just bowled)."""
    state = mwa.get_state(match_id)
    if not state:
        return {"ok": False, "message": "Match not found."}
    prev = state.get("prev_bowler_rid")
    opts = [{"roster_id": p["roster_id"], "name": p["name"],
             "bowl_rating": p.get("bowl_rating"), "bowl_style": p.get("bowl_style"),
             "rating": p.get("rating"), "disabled": (p["roster_id"] == prev)}
            for p in state.get("bowl_xi", [])]
    return {"ok": True, "options": opts}


def build_scorecard(match_id, user_id):
    """Full tabbed scorecard: batting + bowling for both innings.
    Returns dict with innings list + which is current."""
    state = mwa.get_state(match_id)
    if not state:
        return {"ok": False, "message": "Match not found."}

    def _batting(xi, stats):
        rows = []
        for p in xi:
            st = stats.get(p["roster_id"], {})
            if not st.get("balls") and not st.get("out") and not st.get("runs"):
                continue  # didn't bat
            rows.append({
                "name": p["name"], "runs": st.get("runs", 0),
                "balls": st.get("balls", 0), "fours": st.get("fours", 0),
                "sixes": st.get("sixes", 0), "out": st.get("out", False),
                "how_out": st.get("how_out", "") or ("not out" if not st.get("out") else "out"),
                "sr": round(st.get("runs", 0) * 100 / st.get("balls", 1), 1) if st.get("balls") else 0,
            })
        return rows

    def _bowling(xi, stats):
        rows = []
        for p in xi:
            st = stats.get(p["roster_id"], {})
            if not st.get("balls"):
                continue
            overs = f"{st.get('overs_done', 0)}.{st.get('this_over_balls', 0)}" if st.get("this_over_balls") else str(st.get("overs_done", 0))
            econ = round(st.get("runs", 0) / (st.get("balls", 1) / 6), 2) if st.get("balls") else 0
            rows.append({
                "name": p["name"], "overs": overs,
                "runs": st.get("runs", 0), "wickets": st.get("wickets", 0),
                "maidens": st.get("maidens", 0), "econ": econ,
            })
        return rows

    innings = []
    cur_inn = state.get("innings", 1)

    # Innings 1
    if cur_inn == 1:
        inn1_bat_xi = state.get("bat_xi", [])
        inn1_bat_stats = state.get("bat_stats", {})
        inn1_bowl_xi = state.get("bowl_xi", [])
        inn1_bowl_stats = state.get("bowl_stats", {})
        inn1_bat_team = state.get("bat_team_name", "")
        inn1_bowl_team = state.get("bowl_team_name", "")
        inn1_runs = state.get("total_runs", 0)
        inn1_wkts = state.get("total_wickets", 0)
        inn1_overs = f"{max(0, state.get('current_over',1)-1)}.{state.get('current_ball',0)}"
    else:
        inn1_bat_xi = state.get("inn1_bat_xi", [])
        inn1_bat_stats = state.get("inn1_bat_stats", {})
        inn1_bowl_xi = state.get("inn1_bowl_xi", [])
        inn1_bowl_stats = state.get("inn1_bowl_stats", {})
        inn1_bat_team = state.get("inn1_team", "")
        inn1_bowl_team = state.get("bat_team_name", "")  # current batting = inn1 bowling
        inn1_runs = state.get("inn1_runs", 0)
        inn1_wkts = state.get("inn1_wickets", 0)
        inn1_overs = state.get("inn1_overs", "")

    innings.append({
        "number": 1, "bat_team": inn1_bat_team, "bowl_team": inn1_bowl_team,
        "runs": inn1_runs, "wickets": inn1_wkts, "overs": inn1_overs,
        "batting": _batting(inn1_bat_xi, inn1_bat_stats),
        "bowling": _bowling(inn1_bowl_xi, inn1_bowl_stats),
    })

    # Innings 2 (only if in progress)
    if cur_inn == 2:
        innings.append({
            "number": 2, "bat_team": state.get("bat_team_name", ""),
            "bowl_team": state.get("bowl_team_name", ""),
            "runs": state.get("total_runs", 0), "wickets": state.get("total_wickets", 0),
            "overs": f"{max(0, state.get('current_over',1)-1)}.{state.get('current_ball',0)}",
            "batting": _batting(state.get("bat_xi", []), state.get("bat_stats", {})),
            "bowling": _bowling(state.get("bowl_xi", []), state.get("bowl_stats", {})),
        })

    return {"ok": True, "innings": innings, "current_innings": cur_inn,
            "target": state.get("target")}
