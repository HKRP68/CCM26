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
SETUP_PICKING = "PICKING"
SETUP_AWAIT_OPENERS = "AWAIT_OPENERS"
SETUP_AWAIT_BOWLER = "AWAIT_BOWLER"
SETUP_AWAIT_READY = "AWAIT_READY"
SETUP_DONE = "DONE"


def init_match_for_webapp(session, match_id, xi_overrides=None):
    """Create the initial live state for a Mini-App-played match, right after
    the toss. Openers/bowler are placeholders until the teams pick them.
    Returns (ok, msg). Safe to call once; no-op if state already exists.

    xi_overrides: optional {user_id: [xi player dicts]} for synthetic teams
    (e.g. the AI bot, whose XI isn't in UserRoster). When a user_id is present
    here, that XI is used instead of querying UserRoster.
    """
    from services.match_engine import create_match_state
    from models import UserRoster, Player

    xi_overrides = xi_overrides or {}

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
        if uid in xi_overrides:
            return xi_overrides[uid]
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
    s["setup"] = SETUP_PICKING
    s["openers_done"] = False
    s["bowler_done"] = False
    s["batting_order"] = []
    s["current_bowler"] = None
    s["played_via"] = "webapp"

    try:
        from handlers.match import BOT_TG_ID_
        bot_user = None
        if bu.telegram_id == BOT_TG_ID_:
            bot_user = bu
        elif bwu.telegram_id == BOT_TG_ID_:
            bot_user = bwu
        if bot_user:
            s["is_vsbot"] = True
            s["bot_user_id"] = bot_user.id
            if bu.id == bot_user.id:
                s["batting_order"] = [bxi[0], bxi[1]] + [p for p in bxi if p["roster_id"] not in (bxi[0]["roster_id"], bxi[1]["roster_id"])]
                s["striker_idx"] = 0; s["non_striker_idx"] = 1; s["next_batsman_idx"] = 2
                s["openers_done"] = True
            if bwu.id == bot_user.id:
                s["current_bowler"] = bwxi[0]
                s["bowler_done"] = True
    except Exception:
        logger.exception("vsbot init detection failed (non-fatal)")

    next_act = "SETUP"
    if s.get("openers_done") and s.get("bowler_done"):
        s["setup"] = SETUP_DONE
        next_act = A_PICK_DELIVERY
    mwa.save_state(match_id, s, next_action=next_act)
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


def _stat_lookup(stats, roster_id):
    """Return a stats row regardless of JSON stringifying dict keys."""
    if not isinstance(stats, dict):
        return {}
    return stats.get(roster_id) or stats.get(str(roster_id)) or {}


def _stat_slot(stats, roster_id, default):
    """Mutable stats slot that tolerates int keys before JSON and str keys after."""
    if str(roster_id) in stats:
        return stats[str(roster_id)]
    if roster_id in stats:
        return stats[roster_id]
    stats[str(roster_id)] = default
    return stats[str(roster_id)]


def _bat_card(state, idx):
    order = state.get("batting_order", [])
    if idx is None or idx < 0 or idx >= len(order):
        return None
    p = order[idx]
    st = _stat_lookup(state.get("bat_stats", {}), p["roster_id"])
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
    bs = _stat_lookup(state.get("bowl_stats", {}), b["roster_id"])
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
            "crr": round(
                (state.get("total_runs", 0) / max(
                    1,
                    ((state.get("current_over", 1) - 1) * 6
                     + state.get("current_ball", 0)),
                )) * 6,
                2,
            ),
        },
        "partnership": {
            "runs": state.get("partnership_runs", 0),
            "balls": state.get("partnership_balls", 0),
        },
        "bat_team_name": state.get("bat_team_name", "Batting"),
        "bowl_team_name": state.get("bowl_team_name", "Bowling"),
        "striker": striker,
        "non_striker": non_striker,
        "bowler": bowler,
        "timeline": state.get("timeline", [])[-12:],
        "selected_variation": state.get("selected_variation"),
        "current_delivery": state.get("current_delivery"),
        "last_ball": state.get("last_ball"),
    }

    # Role-specific option payloads — both pickers available at once.
    in_setup = setup in (SETUP_PICKING, SETUP_AWAIT_OPENERS, SETUP_AWAIT_BOWLER,
                         SETUP_AWAIT_READY)
    openers_done = bool(state.get("openers_done"))
    bowler_done = bool(state.get("bowler_done"))
    if role == "batsman" and in_setup and not openers_done:
        snap["openers_options"] = [
            {"roster_id": p["roster_id"], "name": p["name"],
             "bat_rating": p.get("bat_rating"), "rating": p.get("rating"),
             "category": p.get("category")}
            for p in state.get("bat_xi", [])
        ]
    if role == "bowler" and in_setup and not bowler_done:
        snap["bowler_options"] = [
            {"roster_id": p["roster_id"], "name": p["name"],
             "bowl_rating": p.get("bowl_rating"), "rating": p.get("rating"),
             "bowl_style": p.get("bowl_style"), "category": p.get("category")}
            for p in state.get("bowl_xi", [])
        ]

    snap["setup_progress"] = {
        "openers_done": openers_done,
        "bowler_done": bowler_done,
    }
    return snap


def get_state_is_vsbot(match_id):
    """Quick check: is this a vs-bot match?"""
    st = mwa.get_state(match_id)
    return bool(st and st.get("is_vsbot"))


def _maybe_start_match(state, match_id):
    """If both openers and bowler are chosen, start the ball loop."""
    if state.get("openers_done") and state.get("bowler_done"):
        state["setup"] = SETUP_DONE
        mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
        return True
    mwa.save_state(match_id, state)
    return False


def _in_setup(state):
    return state.get("setup") in (SETUP_PICKING, SETUP_AWAIT_OPENERS,
                                  SETUP_AWAIT_BOWLER, SETUP_AWAIT_READY)


def select_openers(match_id, user_id, striker_rid, non_striker_rid):
    """Batsman picks openers. Independent of bowler pick; auto-starts when both
    are in. Returns (ok, started, msg)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, False, "Match not found."
    if user_id != state.get("bat_team_id"):
        return False, False, "Only the batting side picks openers."
    if not _in_setup(state) or state.get("openers_done"):
        return False, False, "Openers already chosen."
    if striker_rid == non_striker_rid:
        return False, False, "Striker and non-striker must be different players."

    bat_xi = state.get("bat_xi", [])
    by_rid = {p["roster_id"]: p for p in bat_xi}
    if striker_rid not in by_rid or non_striker_rid not in by_rid:
        return False, False, "Pick players from your XI."

    opener1 = by_rid[striker_rid]
    opener2 = by_rid[non_striker_rid]
    order = [opener1, opener2]
    for p in bat_xi:
        if p["roster_id"] not in (striker_rid, non_striker_rid):
            order.append(p)
    state["batting_order"] = order
    state["striker_idx"] = 0
    state["non_striker_idx"] = 1
    state["next_batsman_idx"] = 2
    state["openers_done"] = True
    started = _maybe_start_match(state, match_id)
    return True, started, ("Openers locked in — match starting!" if started
                           else "Openers locked in. Waiting for the bowler…")


def select_bowler(match_id, user_id, bowler_rid):
    """Bowling side picks bowler. Independent of openers pick; auto-starts when
    both are in. Returns (ok, started, msg)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, False, "Match not found."
    if user_id != state.get("bowl_team_id"):
        return False, False, "Only the bowling side picks the bowler."
    if not _in_setup(state) or state.get("bowler_done"):
        return False, False, "Bowler already chosen."

    bowl_xi = state.get("bowl_xi", [])
    by_rid = {p["roster_id"]: p for p in bowl_xi}
    if bowler_rid not in by_rid:
        return False, False, "Pick a bowler from your XI."

    state["current_bowler"] = by_rid[bowler_rid]
    state["bowler_done"] = True
    started = _maybe_start_match(state, match_id)
    return True, started, ("Bowler selected — match starting!" if started
                           else "Bowler selected. Waiting for the openers…")


def mark_ready(match_id, user_id):
    """Deprecated in the simultaneous model; reports/forces start state."""
    state = mwa.get_state(match_id)
    if not state:
        return False, False, "Match not found."
    both = bool(state.get("openers_done") and state.get("bowler_done"))
    if both and state.get("setup") != SETUP_DONE:
        state["setup"] = SETUP_DONE
        mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
    return True, both, ("Match starting!" if both else "Waiting for both picks…")


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
    opts = _get_delivery_options(bowler.get("bowl_style", "Medium Pacer"),
                                 bowler.get("bowl_hand", "Right"))
    spinner = _is_spinner(bowler.get("bowl_style", ""))
    if spinner:
        deliveries = opts.get("deliveries") or []
        if variation not in deliveries:
            return False, "Pick a valid delivery."
        delivery = variation
        if variation == "Surprise":
            import random
            choices = [d for d in deliveries if d != "Surprise"]
            if choices:
                delivery = random.choice(choices) + " (Surprise)"
    else:
        variations = opts.get("variations") or []
        lengths = opts.get("lengths") or []
        if variation not in variations:
            return False, "Pick a valid variation."
        if not length:
            # Store the selected variation for clients that still submit the
            # legacy two-step pacer flow. The Mini App now usually sends both
            # variation and length together when the Bowl button is tapped.
            state["selected_variation"] = variation
            mwa.save_state(match_id, state, next_action=A_PICK_LENGTH)
            return True, "Variation set — now pick a length."
        if length not in lengths:
            return False, "Pick a valid length."
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
    bs = _stat_slot(state["bat_stats"], striker["roster_id"], {
        "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
        "out": False, "how_out": "", "bowled_by": ""})
    bws = _stat_slot(state["bowl_stats"], bowler["roster_id"], {
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

    # Same commentary line the bot would generate for this ball
    commentary = None
    try:
        commentary = _bm._maybe_pick_commentary(oc, striker, bowler,
                                                 oc.get("runs", 0))
        if commentary:
            res["commentary"] = commentary
    except Exception:
        pass

    # Next action
    if is_innings_over(state):
        from services.match_engine import (transition_to_second_innings,
                                           compute_match_result)
        if state.get("innings", 1) == 1:
            # End of 1st innings → set up the chase
            transition_to_second_innings(state)
            # 2nd innings: bowling side must pick a bowler first
            next_act = A_PICK_NEW_BOWLER
            state["setup"] = SETUP_DONE
            res["innings_break"] = True
        else:
            # End of 2nd innings → match over
            result = compute_match_result(state)
            state["match_result"] = result
            next_act = A_COMPLETED
            res["match_over"] = True
            res["result"] = result
    elif res["need_new_bat"] and state["total_wickets"] < 10:
        next_act = A_PICK_NEW_BATSMAN
        nb = state.get("next_batsman_idx", 2)
        if nb < len(state.get("batting_order", [])):
            state["striker_idx"] = nb
            state["next_batsman_idx"] = nb + 1
            next_act = A_PICK_DELIVERY
    elif res["eoo"]:
        next_act = A_PICK_NEW_BOWLER
    else:
        next_act = A_PICK_DELIVERY

    res["shot"] = shot
    res["delivery"] = delivery
    state["last_ball"] = {
        "rtxt": res.get("rtxt"),
        "type": res.get("type"),
        "runs": res.get("runs", 0),
        "shot": shot,
        "delivery": delivery,
        "batsman": striker.get("name"),
        "bowler": bowler.get("name"),
        "commentary": commentary,
    }

    mwa.save_state(match_id, state, next_action=next_act)
    mwa.bump_ball_seq(match_id)

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
            st = _stat_lookup(stats, p["roster_id"])
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
            st = _stat_lookup(stats, p["roster_id"])
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


# ══════════════════ Persisted scorecards (completed matches) ═════════

def save_final_scorecard(session, match_id, result_text=None):
    """Snapshot the final scorecard from live state into MatchScorecard so it
    can be viewed read-only after the match. Idempotent. Call at completion,
    BEFORE the live state is cleaned up."""
    import json as _json
    from models import MatchScorecard

    existing = (session.query(MatchScorecard)
                .filter(MatchScorecard.match_id == match_id).first())
    if existing:
        return True  # already saved

    sc = build_scorecard(match_id, None)  # user_id not needed for full card
    if not sc.get("ok"):
        return False
    row = MatchScorecard(
        match_id=match_id,
        scorecard_json=_json.dumps({"innings": sc["innings"],
                                    "current_innings": sc.get("current_innings"),
                                    "target": sc.get("target")}),
        result_text=(result_text or "")[:300] or None,
    )
    session.add(row)
    return True


def load_final_scorecard(session, match_id):
    """Load a persisted scorecard for a completed match. Returns dict or None."""
    import json as _json
    from models import MatchScorecard
    row = (session.query(MatchScorecard)
           .filter(MatchScorecard.match_id == match_id).first())
    if not row:
        return None
    try:
        data = _json.loads(row.scorecard_json)
    except Exception:
        return None
    data["result_text"] = row.result_text
    data["completed"] = True
    return data


def get_scorecard_any(session, match_id, user_id):
    """Return the live scorecard if the match is in progress, else the
    persisted final one. Used by the Mini App scorecard view (read-only for
    completed matches)."""
    state = mwa.get_state(match_id)
    if state:
        sc = build_scorecard(match_id, user_id)
        sc["completed"] = False
        return sc
    final = load_final_scorecard(session, match_id)
    if final:
        final["ok"] = True
        return final
    return {"ok": False, "message": "No scorecard available for this match."}


def finalize_webapp_match(session, match_id):
    """Finalize a completed Mini-App match: update the Match record, persist
    the scorecard, and clean up live state. Returns the result dict.
    Idempotent. Rewards are applied via the existing award path if available."""
    from models import Match, User
    state = mwa.get_state(match_id)
    m = session.query(Match).get(match_id)
    if not m:
        return None
    if m.status == "completed":
        return {"already": True}

    result = (state or {}).get("match_result") or {}
    # Map team ids → user ids (bat/bowl team ids ARE user ids in our state)
    winner_uid = result.get("winner_team_id")
    loser_uid = result.get("loser_team_id")

    m.status = "completed"
    m.completed_at = __import__("datetime").datetime.utcnow()
    m.margin_type = result.get("margin_type")
    m.margin_value = result.get("margin_value")
    if winner_uid:
        m.winner_id = winner_uid
    if loser_uid:
        m.loser_id = loser_uid
    # Innings scores from state snapshots
    if state:
        m.inn1_runs = state.get("inn1_runs")
        m.inn1_wickets = state.get("inn1_wickets")
        m.inn2_runs = state.get("total_runs")
        m.inn2_wickets = state.get("total_wickets")

    # Persist the full scorecard BEFORE cleaning up live state
    try:
        save_final_scorecard(session, match_id, result_text=result.get("text"))
    except Exception:
        logger.exception("save_final_scorecard failed")

    # Full rewards via the shared reward core (coins/gems/season points/W-L).
    # Map winner/loser team ids → user ids (they ARE user ids in our state).
    rewards = None
    try:
        from services.match_rewards import award_match_rewards_core
        is_vsbot = bool((state or {}).get("is_vsbot"))
        # tie → no winner; both still get a "played" + loss-tier reward each
        if result.get("margin_type") == "tie":
            # credit both as participants (loss-tier each), no W/L winner
            u1 = m.user1_id; u2 = m.user2_id
            from models import User as _U
            for uid in (u1, u2):
                usr = session.query(_U).get(uid)
                if usr:
                    usr.matches_played = (usr.matches_played or 0) + 1
        elif winner_uid:
            wc, wg, lc, lg = award_match_rewards_core(
                session, winner_uid, loser_uid, m.overs or 1, is_vsbot=is_vsbot)
            rewards = {"winner_coins": wc, "winner_gems": wg,
                       "loser_coins": lc, "loser_gems": lg}
    except Exception:
        logger.exception("reward core failed (non-fatal)")

    session.commit()

    # Clean up the live state now that everything is persisted
    try:
        from services.match_state_store import cleanup_state
        cleanup_state(mwa.fresh_ctx(), match_id)
    except Exception:
        pass

    return {"ok": True, "result": result, "rewards": rewards}


# ══════════════════ Abandon / timeout ═══════════════════════════════

# A Mini-App match with no ball activity for this long is considered stale.
WEBAPP_MATCH_TIMEOUT_SECONDS = 30 * 60  # 30 minutes


def abandon_match(session, match_id, by_user_id, reason="abandoned"):
    """A participant abandons the match → the OTHER side wins by forfeit.
    Finalizes + persists scorecard. Returns (ok, msg)."""
    from models import Match
    state = mwa.get_state(match_id)
    m = session.query(Match).get(match_id)
    if not m:
        return False, "Match not found."
    if m.status == "completed":
        return False, "Match already finished."
    if by_user_id not in (m.user1_id, m.user2_id):
        return False, "You're not in this match."

    winner_id = m.user2_id if by_user_id == m.user1_id else m.user1_id
    if state:
        state["match_result"] = {
            "winner_team_id": winner_id, "loser_team_id": by_user_id,
            "margin_type": "forfeit", "margin_value": 0,
            "text": f"Won by forfeit ({reason})",
        }
        mwa.save_state(match_id, state)
    m.status = "completed"
    m.completed_at = __import__("datetime").datetime.utcnow()
    m.margin_type = "forfeit"
    m.winner_id = winner_id
    m.loser_id = by_user_id
    if state:
        m.inn1_runs = state.get("inn1_runs"); m.inn1_wickets = state.get("inn1_wickets")
        m.inn2_runs = state.get("total_runs"); m.inn2_wickets = state.get("total_wickets")
    try:
        save_final_scorecard(session, match_id, result_text="Match abandoned (forfeit)")
    except Exception:
        pass
    session.commit()
    try:
        from services.match_state_store import cleanup_state
        cleanup_state(mwa.fresh_ctx(), match_id)
    except Exception:
        pass
    return True, "Match ended (forfeit)."


def sweep_stale_webapp_matches(session):
    """Force-end Mini-App matches idle past the timeout. Returns count ended.
    Intended to be called periodically (e.g. from the cooldown/heartbeat job)."""
    from datetime import datetime, timedelta
    from models import MatchState, Match
    cutoff = datetime.utcnow() - timedelta(seconds=WEBAPP_MATCH_TIMEOUT_SECONDS)
    ended = 0
    rows = session.query(MatchState).all()
    for ms in rows:
        last = ms.last_modified or datetime.utcnow()
        if last >= cutoff:
            continue
        state = mwa.get_state(ms.match_id)
        if not state or state.get("played_via") != "webapp":
            continue
        m = session.query(Match).get(ms.match_id)
        if not m or m.status == "completed":
            continue
        # Whoever's turn it is forfeits (they're the idle one)
        na = mwa.get_next_action(ms.match_id)
        if na in ("PICK_SHOT", "PICK_NEW_BATSMAN"):
            idle = state.get("bat_team_id")
        else:
            idle = state.get("bowl_team_id")
        if idle:
            abandon_match(session, ms.match_id, idle, reason="timeout")
            ended += 1
    return ended


# ══════════════════ vsbot: auto-play the bot's side ═════════════════
# When a match is vs the AI, the bot's turns are decided server-side using
# services.bot_ai (the same logic the Telegram /vsbot flow uses). After each
# human action we call auto_play_bot_turns() which advances every consecutive
# bot turn until it's the human's turn again or the match ends.

def _is_bot_side(state, role_side):
    """role_side: 'bat' or 'bowl'. Returns True if that side is the AI."""
    bot_uid = state.get("bot_user_id")
    if not bot_uid:
        return False
    return state.get(f"{role_side}_team_id") == bot_uid


def _bot_controls_current_action(state, next_action):
    """Does the AI control whatever the next action requires?"""
    from services.match_state_store import (
        A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT,
        A_PICK_NEW_BATSMAN, A_PICK_NEW_BOWLER,
    )
    if next_action in (A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_NEW_BOWLER):
        return _is_bot_side(state, "bowl")
    if next_action in (A_PICK_SHOT, A_PICK_NEW_BATSMAN):
        return _is_bot_side(state, "bat")
    return False


def auto_play_bot_turns(session, match_id, max_steps=200):
    """Advance all consecutive AI turns. Returns list of step descriptions
    (for optional commentary). Stops when it's the human's turn or match ends.
    Caller need not commit; this saves state as it goes."""
    import handlers.match as _bm
    from services import bot_ai
    from services.bowling_service import AVAILABLE_SHOTS
    from services.match_engine import (get_striker, get_bowler, is_innings_over,
                                        transition_to_second_innings,
                                        compute_match_result)
    from services.match_state_store import (
        A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT,
        A_PICK_NEW_BATSMAN, A_PICK_NEW_BOWLER, A_COMPLETED,
    )

    steps = []
    for _ in range(max_steps):
        state = mwa.get_state(match_id)
        if not state:
            break
        na = mwa.get_next_action(match_id)
        if na == A_COMPLETED:
            break
        if not _bot_controls_current_action(state, na):
            break  # human's turn (or nothing to do)

        over = state.get("current_over", 1)
        total = state.get("overs", 1)

        if na in (A_PICK_DELIVERY, A_PICK_LENGTH):
            bowler = get_bowler(state)
            pick = bot_ai.pick_bot_delivery(bowler, over, total)
            state["current_delivery"] = pick["delivery"]
            state["selected_variation"] = pick.get("variation")
            mwa.save_state(match_id, state, next_action=A_PICK_SHOT)
            # If the human is batting, stop here so they can play their shot
            if not _is_bot_side(state, "bat"):
                steps.append({"type": "bot_delivery", "delivery": pick["delivery"]})
                break
            # Bot batting too → continue to auto-shot below on next loop
            steps.append({"type": "bot_delivery", "delivery": pick["delivery"]})
            continue

        if na == A_PICK_SHOT:
            # Bot batting plays a shot
            striker = get_striker(state)
            bowler = get_bowler(state)
            delivery = state.get("current_delivery") or "Good"
            _name, idx = bot_ai.pick_bot_shot(
                striker, bowler, over, total,
                state.get("total_runs", 0), state.get("total_wickets", 0),
                target=state.get("target"), current_ball=state.get("current_ball", 0))
            shot = AVAILABLE_SHOTS[idx]
            oc = _bm._calc(state, striker, bowler, shot, delivery)
            res = _apply_outcome(state, oc, shot, delivery, striker, bowler)
            commentary = None
            try:
                commentary = _bm._maybe_pick_commentary(oc, striker, bowler,
                                                         oc.get("runs", 0))
            except Exception:
                pass
            state["last_ball"] = {
                "rtxt": res.get("rtxt"),
                "type": res.get("type"),
                "runs": res.get("runs", 0),
                "shot": shot,
                "delivery": delivery,
                "batsman": striker.get("name"),
                "bowler": bowler.get("name"),
                "commentary": commentary,
            }
            steps.append({"type": "bot_shot", "shot": shot, "rtxt": res["rtxt"],
                          "commentary": commentary})

            # Determine next action (same logic as human play_shot)
            if is_innings_over(state):
                if state.get("innings", 1) == 1:
                    transition_to_second_innings(state)
                    state["setup"] = SETUP_DONE
                    mwa.save_state(match_id, state, next_action=A_PICK_NEW_BOWLER)
                else:
                    state["match_result"] = compute_match_result(state)
                    mwa.save_state(match_id, state, next_action=A_COMPLETED)
                    mwa.bump_ball_seq(match_id)
                    break
            elif res["need_new_bat"] and state["total_wickets"] < 10:
                nb = state.get("next_batsman_idx", 2)
                if nb < len(state.get("batting_order", [])):
                    state["striker_idx"] = nb
                    state["next_batsman_idx"] = nb + 1
                mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            elif res["eoo"]:
                mwa.save_state(match_id, state, next_action=A_PICK_NEW_BOWLER)
            else:
                mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            mwa.bump_ball_seq(match_id)
            continue

        if na == A_PICK_NEW_BOWLER:
            new_bowler = bot_ai.pick_bot_next_bowler(
                state["bowl_xi"], state.get("prev_bowler_rid"),
                state["bowl_stats"], state["overs"])
            state["current_bowler"] = new_bowler
            mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            steps.append({"type": "bot_bowler", "name": new_bowler["name"]})
            continue

        if na == A_PICK_NEW_BATSMAN:
            nb = state.get("next_batsman_idx", 2)
            if nb < len(state.get("batting_order", [])):
                state["striker_idx"] = nb
                state["next_batsman_idx"] = nb + 1
            mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            continue

        break

    return steps
