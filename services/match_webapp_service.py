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
    # Stable host (user1) / guest (user2) display names for the match header,
    # independent of which side is batting in the current innings.
    def _disp(uid):
        if uid == bu.id:
            return bt
        if uid == bwu.id:
            return bwt
        uu = session.query(User).get(uid)
        return (uu.team_name or (f"@{uu.username}" if uu and uu.username else "Player")) if uu else "Player"
    s["host_name"] = _disp(m.user1_id)
    s["guest_name"] = _disp(m.user2_id)
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


# ── Spec-aligned vocabulary ──────────────────────────────────────────
# The engine uses internal constants (PICK_DELIVERY, setup=PICKING, etc.).
# These mappers expose the cleaner names the Mini App spec uses, without
# renaming the internal machinery (which other code depends on).

def phase_status(state, match_status):
    """Normalized match phase:
      'xi_selection' (setup), 'innings1', 'innings2', 'completed', or the raw
      match status as a fallback."""
    if match_status == "completed":
        return "completed"
    setup = state.get("setup")
    if setup in (SETUP_PICKING, SETUP_AWAIT_OPENERS, SETUP_AWAIT_BOWLER,
                 SETUP_AWAIT_READY):
        return "xi_selection"
    inn = state.get("innings", 1)
    return "innings2" if inn == 2 else "innings1"


def turn_state_name(next_action):
    """Map the internal next_action to the spec's gameplay turn states:
      bowling_delivery / batting_shot / selecting_wicket_batsman /
      selecting_over_bowler. Returns None outside the ball loop."""
    return {
        A_PICK_DELIVERY: "bowling_delivery",
        A_PICK_LENGTH: "bowling_delivery",   # still the bowler's delivery step
        A_PICK_SHOT: "batting_shot",
        A_PICK_NEW_BATSMAN: "selecting_wicket_batsman",
        A_PICK_NEW_BOWLER: "selecting_over_bowler",
    }.get(next_action)


def whose_turn(state, next_action, user_id):
    """Compute, for a given user, whether it's their turn and what side acts.
    Returns (turn_side, is_my_turn) where turn_side is 'bowler'/'batsman'/None.
    Based on status/turnState + batting team id + current user (per spec)."""
    turn_side = None
    if next_action in (A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_NEW_BOWLER):
        turn_side = "bowler"
    elif next_action in (A_PICK_SHOT, A_PICK_NEW_BATSMAN):
        turn_side = "batsman"
    role = role_for(state, user_id)
    is_mine = ((turn_side == "bowler" and role == "bowler") or
               (turn_side == "batsman" and role == "batsman"))
    return turn_side, is_mine


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
        # Spec-aligned vocabulary (computed from the engine state)
        "phase": phase_status(state, status),
        "turn_state": turn_state_name(next_action),
        "is_my_turn": whose_turn(state, next_action, user_id)[1],
        # Squad lists (names + role) for the Squads tab
        "bat_xi": [{"name": p.get("name"), "bowl_style": p.get("bowl_style"),
                    "category": p.get("category")} for p in state.get("bat_xi", [])],
        "bowl_xi": [{"name": p.get("name"), "bowl_style": p.get("bowl_style"),
                     "category": p.get("category")} for p in state.get("bowl_xi", [])],
        # Stable host/guest identities for the match header (independent of who
        # is currently batting). host = user1, guest = user2.
        "host_name": state.get("host_name"),
        "guest_name": state.get("guest_name"),
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


def build_match_state_api(session, match_id, user_id):
    """Richer, fully-serialized match state for GET /api/match.

    Wraps build_snapshot and adds: pitch, toss winner/decision, explicit
    role booleans, host/guest blocks, innings data, last ball, and commentary.
    Returns dict or None if the match has no live state.
    """
    base = build_snapshot(session, match_id, user_id)
    if not base:
        return None
    state = mwa.get_state(match_id)
    m = session.query(Match).get(match_id)

    role = base.get("role")  # batsman / bowler / spectator
    turn = base.get("turn")  # batsman / bowler / None

    # host = match.user1, guest = match.user2 (stable identities)
    def _user_block(uid):
        if not uid:
            return None
        u = session.query(User).get(uid)
        if not u:
            return None
        is_bat = (uid == state.get("bat_team_id"))
        is_bowl = (uid == state.get("bowl_team_id"))
        return {
            "user_id": u.id,
            "telegram_id": u.telegram_id,
            "name": u.first_name or u.username or "Player",
            "username": u.username,
            "team_name": u.team_name,
            "side": "batting" if is_bat else ("bowling" if is_bowl else None),
        }

    host = _user_block(m.user1_id) if m else None
    guest = _user_block(m.user2_id) if m else None

    # Toss winner as a friendly label
    toss_winner = None
    if m and m.toss_winner_id:
        tw = session.query(User).get(m.toss_winner_id)
        toss_winner = {
            "user_id": m.toss_winner_id,
            "name": (tw.first_name or tw.username) if tw else None,
            "is_host": (m.toss_winner_id == m.user1_id),
        }

    is_my_turn = (
        (role == "batsman" and turn == "batsman") or
        (role == "bowler" and turn == "bowler")
    )

    return {
        "ok": True,
        "match_id": match_id,
        "pitch": (m.pitch_type if m else None) or state.get("pitch_type"),
        "overs": base.get("overs_limit"),
        "status": base.get("status"),                 # raw match.status
        "phase": base.get("phase"),                    # xi_selection/innings1/innings2/completed
        "toss_winner": toss_winner,
        "toss_decision": (m.toss_decision if m else None),
        "turn_state": base.get("turn_state"),          # bowling_delivery / batting_shot / ...
        "raw_action": base.get("next_action"),         # internal action (debug)
        "setup": base.get("setup"),
        "setup_progress": base.get("setup_progress"),
        # Explicit role booleans (per spec)
        "is_batting": role == "batsman",
        "is_bowling": role == "bowler",
        "is_spectator": role == "spectator",
        "is_my_turn": bool(is_my_turn),
        "role": role,
        "turn": turn,
        # Identities
        "host": host,
        "guest": guest,
        "is_vsbot": bool(state.get("is_vsbot")),
        # Innings + score
        "innings": base.get("innings"),
        "innings_data": {
            "number": base.get("innings"),
            "batting_team": base.get("bat_team_name"),
            "bowling_team": base.get("bowl_team_name"),
            "target": state.get("target"),
        },
        "score": base.get("score"),
        # Players on the field
        "striker": base.get("striker"),
        "non_striker": base.get("non_striker"),
        "bowler": base.get("bowler"),
        # Setup pickers (if applicable for this user)
        "openers_options": base.get("openers_options"),
        "bowler_options": base.get("bowler_options"),
        # Live texture
        "timeline": base.get("timeline"),
        "current_delivery": base.get("current_delivery"),
        "selected_variation": base.get("selected_variation"),
        "last_ball": state.get("last_ball"),
        "commentary": state.get("last_commentary"),
        # Sync
        "ball_seq": base.get("ball_seq"),
    }


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


def select_players(match_id, user_id, striker_idx=None, non_striker_idx=None,
                   bowler_idx=None):
    """Unified index-based setup submission (POST /api/match/select-players).

    The batting side sends strikerIdx + nonStrikerIdx; the bowling side sends
    bowlerIdx. Indices are positions into the user's XI (bat_xi / bowl_xi).
    Converts indices → roster ids and reuses the validated select_openers /
    select_bowler logic (which enforces role, dedupe, and auto-starts the match
    when both sides have submitted).

    Returns (ok, started, msg).
    """
    state = mwa.get_state(match_id)
    if not state:
        return False, False, "Match not found."

    role = role_for(state, user_id)

    # Batting side → openers
    if striker_idx is not None or non_striker_idx is not None:
        if role != "batsman":
            return False, False, "Only the batting side picks openers."
        if striker_idx is None or non_striker_idx is None:
            return False, False, "Provide both strikerIdx and nonStrikerIdx."
        bat_xi = state.get("bat_xi", [])
        if not (0 <= striker_idx < len(bat_xi)) or not (0 <= non_striker_idx < len(bat_xi)):
            return False, False, "Player index out of range."
        if striker_idx == non_striker_idx:
            return False, False, "Striker and non-striker must be different players."
        s_rid = bat_xi[striker_idx]["roster_id"]
        ns_rid = bat_xi[non_striker_idx]["roster_id"]
        return select_openers(match_id, user_id, s_rid, ns_rid)

    # Bowling side → bowler
    if bowler_idx is not None:
        if role != "bowler":
            return False, False, "Only the bowling side picks the bowler."
        bowl_xi = state.get("bowl_xi", [])
        if not (0 <= bowler_idx < len(bowl_xi)):
            return False, False, "Bowler index out of range."
        b_rid = bowl_xi[bowler_idx]["roster_id"]
        return select_bowler(match_id, user_id, b_rid)

    return False, False, "Nothing to select — send openers or a bowler."


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


# ── Processing lock: stops a double-tap from firing two actions ───────
import time as _time
_ACTION_LOCK_SECONDS = 8  # auto-expires so a crashed action can't wedge a match


def _is_processing(state):
    """True if a prior action is still within the processing window."""
    ts = state.get("action_processing_at")
    if not ts:
        return False
    return (_time.time() - ts) < _ACTION_LOCK_SECONDS


def _set_processing(state, on=True):
    state["action_processing_at"] = _time.time() if on else None


# ── Speed: qualitative → km/h (bowler-type aware) ────────────────────
def _speed_to_kmh(speed_label, bowler):
    """Map slow/medium/fast to a realistic km/h, modulated by bowler type and
    rating. Spinners top out far lower than pacers."""
    import random
    style = (bowler.get("bowl_style", "") or "").lower()
    rating = bowler.get("bowl_rating", 80) or 80
    spinner = _is_spinner(bowler.get("bowl_style", ""))
    label = (speed_label or "medium").lower()
    if spinner:
        bands = {"slow": (70, 82), "medium": (82, 92), "fast": (92, 102)}
    else:
        bands = {"slow": (118, 128), "medium": (130, 142), "fast": (142, 154)}
    lo, hi = bands.get(label, bands["medium"])
    # Higher-rated bowlers lean to the top of the band.
    skill = max(0, min(1, (rating - 70) / 30.0))
    base = lo + (hi - lo) * (0.4 + 0.5 * skill)
    return int(round(base + random.uniform(-2, 2)))


def set_delivery_action(match_id, user_id, delivery, speed=None):
    """Unified bowling action (POST /api/match/action, type=delivery).

    Accepts a single {delivery, speed} per the spec (e.g. yorker/fast) rather
    than the two-step variation→length. Validates role/turn/processing, stores
    currentDelivery + currentSpeed + a generated km/h, and hands over to the
    batsman. Returns (ok, msg, info) where info carries the resolved km/h.
    """
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found.", None
    # user must be in the match and NOT batting (i.e. must be the bowler)
    if user_id != state.get("bowl_team_id"):
        return False, "Only the bowling side delivers.", None
    na = mwa.get_next_action(match_id)
    if na not in (A_PICK_DELIVERY, A_PICK_LENGTH):
        return False, "It's not the bowling delivery phase.", None
    if _is_processing(state):
        return False, "Previous action still processing — hold on.", None
    if not delivery:
        return False, "Pick a delivery.", None

    _set_processing(state, True)
    bowler = state.get("current_bowler") or {}
    kmh = _speed_to_kmh(speed, bowler)

    state["current_delivery"] = str(delivery)
    state["current_speed"] = (speed or "medium")
    state["last_speed"] = kmh          # km/h, surfaced to clients
    state["selected_variation"] = str(delivery)
    _set_processing(state, False)
    mwa.save_state(match_id, state, next_action=A_PICK_SHOT)
    return True, "Delivery on its way — batsman to play.", {
        "delivery": str(delivery), "speed": speed or "medium", "kmh": kmh}


def _resolve_shot_index(shot):
    """Map a shot to its AVAILABLE_SHOTS index. Accepts an int index, an exact
    name, or a case-insensitive name ('pull' → 'Pull'). Returns int or None."""
    from services.bowling_service import AVAILABLE_SHOTS
    if shot is None:
        return None
    # numeric index
    if isinstance(shot, int):
        return shot if 0 <= shot < len(AVAILABLE_SHOTS) else None
    sval = str(shot).strip()
    if sval.isdigit():
        i = int(sval)
        return i if 0 <= i < len(AVAILABLE_SHOTS) else None
    # name (case-insensitive, tolerant of spacing)
    norm = sval.lower().replace("_", " ").replace("-", " ")
    for i, name in enumerate(AVAILABLE_SHOTS):
        if name.lower() == norm:
            return i
    return None


def set_shot_action(match_id, user_id, shot):
    """Batting action (POST /api/match/action, type=shot). Accepts a shot name
    (e.g. 'pull') or index, validates role/phase/processing, and plays the ball.
    Returns (ok, result_or_msg, info)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found.", None
    if user_id != state.get("bat_team_id"):
        return False, "Only the batting side plays shots.", None
    na = mwa.get_next_action(match_id)
    if na != A_PICK_SHOT:
        return False, "It's not the batting shot phase.", None
    if _is_processing(state):
        return False, "Previous action still processing — hold on.", None

    idx = _resolve_shot_index(shot)
    if idx is None:
        from services.bowling_service import AVAILABLE_SHOTS
        return False, f"Unknown shot '{shot}'. Options: {', '.join(AVAILABLE_SHOTS)}", None

    from services.bowling_service import AVAILABLE_SHOTS
    state["current_shot"] = AVAILABLE_SHOTS[idx]
    state["manual_batsman"] = True   # envelope flow: player picks next batsman
    _set_processing(state, True)
    mwa.save_state(match_id, state)
    ok, res = play_shot(match_id, user_id, idx)
    # play_shot saves state; clear the lock afterward.
    st2 = mwa.get_state(match_id)
    if st2:
        _set_processing(st2, False)
        mwa.save_state(match_id, st2)
    if not ok:
        return False, res, None
    return True, res, {"shot": AVAILABLE_SHOTS[idx]}


def select_wicket_batsman(match_id, user_id, index):
    """After a wicket, the batting player picks the next batsman (by index into
    the batting order). Validates per spec:
      • only the batting side selects
      • match is in selecting_wicket_batsman (A_PICK_NEW_BATSMAN)
      • the selected player exists
      • not already on strike / non-strike
      • not already out
    On success the player becomes striker and play returns to bowling delivery.
    Returns (ok, msg, info)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found.", None
    if user_id != state.get("bat_team_id"):
        return False, "Only the batting side selects the next batsman.", None
    na = mwa.get_next_action(match_id)
    if na != A_PICK_NEW_BATSMAN:
        return False, "It's not the new-batsman selection phase.", None

    order = state.get("batting_order", [])
    try:
        idx = int(index)
    except (ValueError, TypeError):
        return False, "Invalid batsman index.", None
    if not (0 <= idx < len(order)):
        return False, "Selected player does not exist.", None

    player = order[idx]
    rid = player.get("roster_id")
    bat_stats = state.get("bat_stats", {})
    # State is JSON-round-tripped, so dict keys may be strings. Check both.
    st = bat_stats.get(rid) or bat_stats.get(str(rid))
    if st and st.get("out"):
        return False, "That batsman is already out.", None
    if idx in (state.get("striker_idx"), state.get("non_striker_idx")):
        return False, "That batsman is already at the crease.", None

    # Install as striker; resume the delivery loop.
    state["striker_idx"] = idx
    # Keep next_batsman_idx ahead of the highest used position.
    used = max(idx, state.get("non_striker_idx", 1))
    state["next_batsman_idx"] = max(state.get("next_batsman_idx", 2), used + 1)
    state["last_dismissed"] = None
    mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
    return True, f"{player.get('name')} comes to the crease.", {
        "index": idx, "name": player.get("name")}


def _apply_outcome(state, oc, shot, delivery, striker, bowler):
    """Mirror of the bot's _process_shot_core bookkeeping (deterministic given
    the outcome `oc`). Mutates state in place. Returns a result dict."""
    # Always use str keys so JSON round-trips don't create duplicate int/str key
    # collisions that silently reset accumulated stats to zero.
    s_rid = str(striker["roster_id"])
    b_rid = str(bowler["roster_id"])
    bs = state["bat_stats"].setdefault(s_rid, {
        "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
        "out": False, "how_out": "", "bowled_by": ""})
    bws = state["bowl_stats"].setdefault(b_rid, {
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
        # Preserve the just-bowled over's runs for the end-of-over card.
        bws["last_over_runs"] = bws.get("this_over_runs", 0)
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


def _append_commentary_log(state, res, striker, bowler, text):
    """Accumulate a scrolling commentary feed in ``state['commentary_log']``.

    Stores three event kinds, exactly matching the fields UnderCover's
    frontend renders (static/cricket/app.js renderCommentaryFeed):
      • ``ball``           — one row per delivery
      • ``end_of_over``    — summary card when an over completes
      • ``end_of_innings`` — summary card when an innings ends
    Kept newest-last here; serialize_match_state reverses it for display.
    """
    log = state.get("commentary_log")
    if not isinstance(log, list):
        log = []

    overs_done = max(0, state.get("current_over", 1) - 1)
    balls = state.get("current_ball", 0)
    runs = res.get("runs", 0)
    is_wkt = res.get("type") == "wicket"

    # Ball row. After an over rolls (eoo), current_ball was reset to 0 and the
    # over counter advanced, so reconstruct the ball's real over.address here.
    if res.get("eoo"):
        ball_over_label = f"{overs_done - 1}.6" if overs_done >= 1 else "0.6"
    else:
        ball_over_label = f"{overs_done}.{balls}"
    log.append({
        "type": "ball",
        "over": ball_over_label,
        "runs": runs,
        "isWicket": is_wkt,
        "text": text or res.get("rtxt") or "",
    })

    def _bat_card(player):
        if not player:
            return None
        bs = (state.get("bat_stats", {}) or {}).get(player.get("roster_id"), {})
        return {"name": player.get("name"),
                "runs": bs.get("runs", 0), "balls": bs.get("balls", 0)}

    # End-of-over summary card.
    if res.get("eoo"):
        bws = (state.get("bowl_stats", {}) or {}).get(bowler.get("roster_id"), {}) if bowler else {}
        b_overs_done = bws.get("overs_done", 0)
        b_this = bws.get("this_over_balls", 0)
        log.append({
            "type": "end_of_over",
            "overNumber": overs_done,  # the over that just finished
            "runsScored": bws.get("last_over_runs", 0),
            "totalRuns": state.get("total_runs", 0),
            "totalWickets": state.get("total_wickets", 0),
            "striker": _bat_card(get_striker(state)),
            "nonStriker": _bat_card(get_non_striker(state)),
            "bowler": {
                "name": bowler.get("name") if bowler else "",
                "wickets": bws.get("wickets", 0),
                "runsConceded": bws.get("runs", 0),
                "overs": f"{b_overs_done}.{b_this}" if b_this else f"{b_overs_done}",
            },
        })

    # End-of-innings summary card. Called before the next-action block runs,
    # so state still holds the just-completed innings totals (the transition
    # that resets them hasn't happened yet). Detect directly.
    if is_innings_over(state):
        innings_idx = state.get("innings", 1) - 1
        log.append({
            "type": "end_of_innings",
            "inningsIdx": innings_idx,
            "runs": state.get("total_runs", 0),
            "wickets": state.get("total_wickets", 0),
            "overs": f"{overs_done}.{balls}",
            "target": state.get("target"),
            "winner": None,   # filled on the result screen via result.motm
            "motm": None,
        })

    # Cap the log so state stays small.
    if len(log) > 60:
        log = log[-60:]
    state["commentary_log"] = log


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

    # Persist last-ball summary + commentary in state so the match-state API
    # can surface them to clients that weren't the one who made the call.
    state["last_ball"] = {
        "text": res.get("rtxt"),
        "type": res.get("type"),
        "runs": res.get("runs", 0),
        "shot": shot,
        "delivery": delivery,
        "batsman": striker.get("name") if striker else None,
        "bowler": bowler.get("name") if bowler else None,
        "how": res.get("how"),
    }
    state["last_commentary"] = commentary or res.get("rtxt")

    # Accumulate a scrolling commentary log (ball rows + end-of-over /
    # end-of-innings summary cards) so the Mini App feed matches UnderCover.
    _append_commentary_log(state, res, striker, bowler,
                           commentary or res.get("rtxt"))

    # Next action
    if is_innings_over(state):
        from services.match_engine import (transition_to_second_innings,
                                           compute_match_result)
        if state.get("innings", 1) == 1:
            # End of 1st innings → set up the chase
            transition_to_second_innings(state)
            res["innings_break"] = True
            if state.get("is_vsbot"):
                # vsbot: keep it flowing — bowling side picks a bowler (auto for bot).
                next_act = A_PICK_NEW_BOWLER
                state["setup"] = SETUP_DONE
            else:
                # PvP: re-enter player selection so BOTH sides pick again
                # (new batting side → openers, new bowling side → bowler).
                state["setup"] = SETUP_PICKING
                state["openers_done"] = False
                state["bowler_done"] = False
                state["current_bowler"] = None
                next_act = "SETUP"
        else:
            # End of 2nd innings → match over
            result = compute_match_result(state)
            state["match_result"] = result
            next_act = A_COMPLETED
            res["match_over"] = True
            res["result"] = result
    elif res["need_new_bat"] and state["total_wickets"] < 10:
        # Save the dismissed batsman name (for selecting_wicket_batsman UI).
        try:
            dismissed = get_striker(state)
            state["last_dismissed"] = dismissed.get("name") if dismissed else None
        except Exception:
            state["last_dismissed"] = None
        next_act = A_PICK_NEW_BATSMAN
        if state.get("manual_batsman"):
            # Manual mode: the batting player picks the next batsman.
            # Stay in A_PICK_NEW_BATSMAN (do NOT auto-advance).
            pass
        else:
            nb = state.get("next_batsman_idx", 2)
            if nb < len(state.get("batting_order", [])):
                state["striker_idx"] = nb
                state["next_batsman_idx"] = nb + 1
                next_act = A_PICK_DELIVERY
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


# ── Completed-match cache: keep a finished match queryable for 5 minutes ──
import time as _time2
_COMPLETED_CACHE = {}            # match_id -> (expires_at, payload)
_COMPLETED_TTL = 5 * 60          # 5 minutes


def _cache_completed(match_id, payload):
    _COMPLETED_CACHE[match_id] = (_time2.time() + _COMPLETED_TTL, payload)
    # opportunistic sweep of expired entries
    now = _time2.time()
    for mid in [k for k, (exp, _) in _COMPLETED_CACHE.items() if exp < now]:
        _COMPLETED_CACHE.pop(mid, None)


def get_completed_cached(match_id):
    """Return a recently-completed match payload if still within the 5-min
    window, else None."""
    entry = _COMPLETED_CACHE.get(match_id)
    if not entry:
        return None
    exp, payload = entry
    if _time2.time() > exp:
        _COMPLETED_CACHE.pop(match_id, None)
        return None
    return payload


def _pick_player_of_match(state, result):
    """Choose Player of the Match from both innings' stats. Simple, explainable
    scoring: runs + 20×wickets + small boundary bonus; the winning side's
    players get a modest edge on ties. Returns {name, team, runs, wickets} or None."""
    if not state:
        return None
    candidates = {}  # roster_id -> {name, runs, wkts, fours, sixes, side_winner}
    winner_uid = (result or {}).get("winner_team_id")

    def _ingest(bat_stats, bowl_stats, xi, team_uid):
        by_rid = {p["roster_id"]: p for p in (xi or [])}
        for rid, st in (bat_stats or {}).items():
            try:
                rid_i = int(rid)
            except (ValueError, TypeError):
                rid_i = rid
            p = by_rid.get(rid_i) or by_rid.get(rid)
            name = p["name"] if p else str(rid)
            c = candidates.setdefault(rid_i, {"name": name, "runs": 0, "wkts": 0,
                                              "fours": 0, "sixes": 0,
                                              "winner": team_uid == winner_uid})
            c["runs"] += st.get("runs", 0)
            c["fours"] += st.get("fours", 0)
            c["sixes"] += st.get("sixes", 0)
        for rid, st in (bowl_stats or {}).items():
            try:
                rid_i = int(rid)
            except (ValueError, TypeError):
                rid_i = rid
            p = by_rid.get(rid_i) or by_rid.get(rid)
            name = p["name"] if p else str(rid)
            c = candidates.setdefault(rid_i, {"name": name, "runs": 0, "wkts": 0,
                                              "fours": 0, "sixes": 0,
                                              "winner": team_uid == winner_uid})
            c["wkts"] += st.get("wickets", 0)

    # Innings 1 (snapshotted) + innings 2 (current)
    _ingest(state.get("inn1_bat_stats"), state.get("inn1_bowl_stats"),
            state.get("inn1_bat_xi"), state.get("inn1_bat_team_id"))
    # innings-1 bowlers belong to innings-1 bowling team
    _ingest({}, state.get("inn1_bowl_stats"), state.get("inn1_bowl_xi"),
            state.get("inn1_bowl_team_id"))
    _ingest(state.get("bat_stats"), state.get("bowl_stats"),
            state.get("bat_xi"), state.get("bat_team_id"))
    _ingest({}, state.get("bowl_stats"), state.get("bowl_xi"),
            state.get("bowl_team_id"))

    if not candidates:
        return None
    def score(c):
        return c["runs"] + 20 * c["wkts"] + c["fours"] * 1 + c["sixes"] * 2 + (5 if c["winner"] else 0)
    best_rid = max(candidates, key=lambda r: score(candidates[r]))
    b = candidates[best_rid]
    return {"name": b["name"], "runs": b["runs"], "wickets": b["wkts"]}


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

    # Player of the match (from both innings' stats, before cleanup).
    pom = None
    try:
        pom = _pick_player_of_match(state, result)
        if pom:
            result["player_of_match"] = pom
    except Exception:
        logger.exception("player-of-match selection failed (non-fatal)")

    session.commit()

    payload = {"ok": True, "result": result, "rewards": rewards,
               "player_of_match": pom}

    # Keep the finished match queryable for 5 minutes after state cleanup.
    try:
        _cache_completed(match_id, payload)
    except Exception:
        pass

    # Clean up the live state now that everything is persisted
    try:
        from services.match_state_store import cleanup_state
        cleanup_state(mwa.fresh_ctx(), match_id)
    except Exception:
        pass

    return payload


# ══════════════════ Abandon / timeout ═══════════════════════════════

# A Mini-App match with no ball activity for this long is force-terminated.
WEBAPP_MATCH_TIMEOUT_SECONDS = 20 * 60  # 20 minutes (per spec)


def _balls_bowled_total(state):
    """Total legal balls bowled across the match so far (both innings).
    1st-innings balls are snapshotted at the break; 2nd-innings balls come from
    the live over/ball counters."""
    if not state:
        return 0
    inn = state.get("innings", 1)
    cur = (state.get("current_over", 1) - 1) * 6 + state.get("current_ball", 0)
    if inn == 1:
        return cur
    # innings 2: add the completed first-innings balls
    i1_over = state.get("inn1_overs")  # stored as "x.y" string
    i1_balls = 0
    if isinstance(i1_over, str) and "." in i1_over:
        try:
            o, b = i1_over.split(".")
            i1_balls = int(o) * 6 + int(b)
        except Exception:
            i1_balls = 0
    return i1_balls + cur


def quit_penalty_quote(match_id, user_id=None):
    """Compute the quit penalty for a confirmation prompt (no mutation).
      ratio   = ballsBowled / (totalOvers * 12)
      penalty = ratio * totalOvers * 1000
    The penalty is the same regardless of which player quits, so user_id is
    optional (accepted for API symmetry). Returns dict or None."""
    state = mwa.get_state(match_id)
    if not state:
        return None
    total_overs = state.get("overs", 1) or 1
    balls = _balls_bowled_total(state)
    denom = total_overs * 12
    ratio = (balls / denom) if denom else 0.0
    penalty = int(round(ratio * total_overs * 1000))
    return {
        "balls_bowled": balls,
        "total_overs": total_overs,
        "ratio": round(ratio, 4),
        "penalty": penalty,
        "has_progress": balls > 0,
    }


def handle_match_termination(session, match_id, quitter_id, reason="quit"):
    """Terminate a match because a player quit (or timed out).

    • No balls bowled → no penalty, no rewards; the match just ends with no W/L.
    • Balls bowled → quitter loses `penalty` coins (capped at their balance),
      opponent receives the same as compensation, and W/L records update
      (quitter = loss, opponent = win).
    Returns (ok, info|msg).
    """
    from models import Match, User
    from services.activity_service import log_activity
    m = session.query(Match).get(match_id)
    if not m:
        return False, "Match not found."
    if m.status == "completed":
        return False, "Match already finished."
    if quitter_id not in (m.user1_id, m.user2_id):
        return False, "You're not in this match."

    opponent_id = m.user2_id if quitter_id == m.user1_id else m.user1_id
    state = mwa.get_state(match_id)
    q = quit_penalty_quote(match_id) or {"balls_bowled": 0, "penalty": 0,
                                         "has_progress": False}
    penalty = q["penalty"]
    balls = q["balls_bowled"]

    quitter = session.query(User).get(quitter_id)
    opponent = session.query(User).get(opponent_id)

    applied_penalty = 0
    compensation = 0
    if q["has_progress"]:
        # Quitter loses coins (never below zero).
        applied_penalty = min(penalty, quitter.total_coins or 0) if quitter else 0
        if quitter:
            quitter.total_coins = (quitter.total_coins or 0) - applied_penalty
            quitter.matches_lost = (quitter.matches_lost or 0) + 1
            quitter.matches_played = (quitter.matches_played or 0) + 1
            log_activity(session, quitter.id, "match_quit",
                         f"Quit match #{match_id} ({balls} balls) — penalty",
                         coins_change=-applied_penalty)
        # Opponent compensation = full penalty value (not just what was deducted).
        compensation = penalty
        if opponent:
            opponent.total_coins = (opponent.total_coins or 0) + compensation
            opponent.matches_won = (opponent.matches_won or 0) + 1
            opponent.matches_played = (opponent.matches_played or 0) + 1
            log_activity(session, opponent.id, "match_quit_comp",
                         f"Opponent quit match #{match_id} — compensation",
                         coins_change=compensation)
        margin_type = "forfeit"
        win_id, lose_id = opponent_id, quitter_id
        result_text = f"Won by forfeit ({reason})"
    else:
        # No progress → clean cancel, no rewards, no records.
        margin_type = "cancelled"
        win_id, lose_id = None, None
        result_text = "Match cancelled — no balls bowled."

    if state:
        state["match_result"] = {
            "winner_team_id": win_id, "loser_team_id": lose_id,
            "margin_type": margin_type, "margin_value": 0,
            "text": result_text,
            "penalty": applied_penalty, "compensation": compensation,
        }
        mwa.save_state(match_id, state)

    m.status = "completed"
    m.completed_at = __import__("datetime").datetime.utcnow()
    m.margin_type = margin_type
    m.winner_id = win_id
    m.loser_id = lose_id
    if state:
        m.inn1_runs = state.get("inn1_runs"); m.inn1_wickets = state.get("inn1_wickets")
        m.inn2_runs = state.get("total_runs"); m.inn2_wickets = state.get("total_wickets")
    try:
        save_final_scorecard(session, match_id, result_text=result_text)
    except Exception:
        pass
    session.commit()
    try:
        from services.match_state_store import cleanup_state
        cleanup_state(mwa.fresh_ctx(), match_id)
    except Exception:
        pass
    return True, {"penalty": applied_penalty, "compensation": compensation,
                  "balls_bowled": balls, "cancelled": not q["has_progress"],
                  "winner_id": win_id, "loser_id": lose_id,
                  "result_text": result_text}


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


def restore_active_matches(session):
    """On bot startup, load and validate all active (non-completed) Mini-App
    matches from the database so live games survive a restart.

    The full game state already lives in the match_state table (state_json),
    upserted on every save, and the matches table holds chat/host/guest/status.
    This re-validates each live match, drops orphaned/stale state, and returns a
    summary dict. It does NOT mutate healthy live matches — they simply resume,
    because both the bot and the Mini App read the same DB-backed state.
    """
    from models import MatchState, Match
    restored, orphaned, completed_leftover = [], 0, 0
    try:
        rows = session.query(MatchState).all()
    except Exception:
        logger.exception("restore_active_matches: could not query match_state")
        return {"restored": 0, "orphaned": 0, "completed_cleaned": 0, "active": []}

    for ms in rows:
        mid = ms.match_id
        m = session.query(Match).get(mid)
        # Orphan: state with no parent match → clean up.
        if not m:
            try:
                from services.match_state_store import cleanup_state
                cleanup_state(mwa.fresh_ctx(), mid)
                orphaned += 1
            except Exception:
                logger.exception("restore: failed to clean orphan state %s", mid)
            continue
        # Completed match with lingering live state → clean up.
        if m.status == "completed":
            try:
                from services.match_state_store import cleanup_state
                cleanup_state(mwa.fresh_ctx(), mid)
                completed_leftover += 1
            except Exception:
                pass
            continue
        # Validate the state actually deserializes.
        state = mwa.get_state(mid)
        if not state:
            try:
                from services.match_state_store import cleanup_state
                cleanup_state(mwa.fresh_ctx(), mid)
                orphaned += 1
            except Exception:
                pass
            continue
        # Healthy active match — it will resume from DB state as-is.
        restored.append({
            "match_id": mid,
            "chat_id": state.get("chat_id") or m.chat_id,
            "host_id": m.user1_id,
            "guest_id": m.user2_id,
            "status": m.status,
            "next_action": mwa.get_next_action(mid),
            "innings": state.get("innings", 1),
            "is_vsbot": bool(state.get("is_vsbot")),
        })

    logger.info("restore_active_matches: %d active, %d orphan(s) cleaned, "
                "%d completed-leftover cleaned",
                len(restored), orphaned, completed_leftover)
    return {"restored": len(restored), "orphaned": orphaned,
            "completed_cleaned": completed_leftover, "active": restored}


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
            # PvP timeout → terminate with the proportional penalty (the idle
            # player is treated as the quitter). vsbot → plain forfeit (no
            # penalty against a human for a bot stall).
            if state.get("is_vsbot"):
                abandon_match(session, ms.match_id, idle, reason="timeout")
            else:
                handle_match_termination(session, ms.match_id, idle,
                                         reason="inactivity timeout")
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
            try:
                _c = _bm._maybe_pick_commentary(oc, striker, bowler, oc.get("runs", 0))
            except Exception:
                _c = None
            state["last_ball"] = {
                "text": res.get("rtxt"), "type": res.get("type"),
                "runs": res.get("runs", 0), "shot": shot, "delivery": delivery,
                "batsman": striker.get("name") if striker else None,
                "bowler": bowler.get("name") if bowler else None,
                "how": res.get("how"),
            }
            state["last_commentary"] = _c or res.get("rtxt")
            _append_commentary_log(state, res, striker, bowler,
                                   _c or res.get("rtxt"))
            steps.append({"type": "bot_shot", "shot": shot, "rtxt": res["rtxt"]})

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
