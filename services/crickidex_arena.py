"""Crickidex Arena adapter — serves the UnderCover "Crickidex Arena" Mini App
frontend (public/cricket) on CCM26's Python backend.

The UnderCover frontend (static/cricket/app.js) polls ``GET /api/match`` and
expects a very specific ``serializeMatchState`` JSON shape (host/guest blocks,
``status`` of xi_selection/innings1/innings2/completed, ``myRole`` of
batting/bowling/spectator, fixed ``battingXI``/``bowlingXI`` arrays, a ``stats``
map keyed by player id, ``striker``/``nonStriker``/``bowler`` cards, etc.).

CCM26 stores live matches via ``services.match_webapp_access`` /
``services.match_webapp_service`` with its own field names. This module is the
pure translation layer between the two:

  • ``serialize_match_state``  — CCM26 state  → UnderCover matchState shape
  • ``resolve_viewer``         — Telegram id  → internal User (frontend sends
                                  the raw Telegram id as ``userId``)
  • ``find_active_match``      — latest "playing" match for a user
  • index helpers             — translate the frontend's XI indices into the
                                  roster ids / batting-order indices the
                                  service layer expects.

No Flask/Telegram imports here — just data mapping, so it stays testable.
"""

import logging
import threading
import time as _time

from services import match_webapp_access as mwa
from services.match_webapp_service import (
    role_for, phase_status, turn_state_name, whose_turn,
    SETUP_INNINGS_BREAK, INNINGS_BREAK_SECONDS,
    _is_processing, action_in_progress,
)
from services.cipl_match import is_hundred as _is_hundred, balls_per_unit
from services.perf_log import perf_span

logger = logging.getLogger(__name__)


# ── participant identity cache ───────────────────────────────────────
# serialize_match_state runs on every 150ms poll and only needs four stable
# fields per participant; skip the two User queries per request. Identity
# changes (e.g. /teamname mid-match) surface within the TTL.

_USER_CACHE_TTL = 30.0
_USER_CACHE_LOCK = threading.Lock()
_USER_CACHE = {}  # user_id -> (fetched_at_monotonic, _UserLite)


class _UserLite:
    __slots__ = ("id", "telegram_id", "username", "team_name")

    def __init__(self, u):
        self.id = u.id
        self.telegram_id = u.telegram_id
        self.username = u.username
        self.team_name = u.team_name


def _user_lite(session, user_id):
    if not user_id:
        return None
    now = _time.monotonic()
    with _USER_CACHE_LOCK:
        hit = _USER_CACHE.get(user_id)
        if hit and (now - hit[0]) <= _USER_CACHE_TTL:
            return hit[1]
    from models import User
    u = session.query(User).get(user_id)
    if not u:
        return None
    lite = _UserLite(u)
    with _USER_CACHE_LOCK:
        _USER_CACHE[user_id] = (now, lite)
    return lite


# ── tiny helpers ─────────────────────────────────────────────────────

def _stat(d, rid):
    """Stat lookup tolerant of int/str keys. Always prefers str key (used by
    _apply_outcome) over int key (used by create_match_state initial zeros)."""
    if not d:
        return {}
    str_rid = str(rid)
    v = d.get(str_rid)
    if v is not None:
        return v
    v = d.get(rid)
    if v is not None:
        return v
    return {}


def _p(pd):
    """Map a CCM26 XI player dict → the UnderCover player object shape that
    app.js reads (``id``, ``ovr``, ``role``, ``batting_ovr``/``bowling_ovr``,
    ``bowler_type`` …). Both vocabularies are kept so either side works."""
    if not pd:
        return None
    rid = pd.get("roster_id")
    return {
        "id": rid,
        "roster_id": rid,
        "player_id": pd.get("player_id"),
        "name": pd.get("name"),
        "ovr": pd.get("rating"),
        "rating": pd.get("rating"),
        "role": pd.get("category"),
        "category": pd.get("category"),
        "batting_ovr": pd.get("bat_rating"),
        "bowling_ovr": pd.get("bowl_rating"),
        "bat_rating": pd.get("bat_rating"),
        "bowl_rating": pd.get("bowl_rating"),
        "bowler_type": pd.get("bowl_style"),
        "bowl_style": pd.get("bowl_style"),
        "bowl_hand": pd.get("bowl_hand"),
        "bat_hand": pd.get("bat_hand"),
        "active": pd.get("active", True) is not False,
        "impact_replaced": bool(pd.get("impact_replaced")),
        "impact_replacement": bool(pd.get("impact_replacement")),
    }


def _full_dismissal(bs):
    """Render a full scorecard dismissal line from a batting stat row.

    Prefers a pre-rendered ``dismissal`` (set by the Challenge League engine);
    otherwise builds one from how_out + bowled_by (+ fielder) so the scorecard
    reads ``c Kohli b Bumrah`` / ``lbw b Shami`` / ``run out (Jadeja)`` instead
    of a bare ``caught`` / ``bowled`` / ``lbw``.
    """
    if not bs.get("out"):
        return ""
    pre = (bs.get("dismissal") or "").strip()
    if pre:
        return pre
    how = (bs.get("how_out") or "").strip()
    bowler = bs.get("bowled_by") or ""
    fielder = bs.get("fielder") or ""
    low = how.lower()
    if not how:
        return "out"
    if low in ("bowled", "b"):
        return f"b {bowler}".strip()
    if low == "lbw":
        return f"lbw b {bowler}".strip()
    if low in ("caught", "c"):
        if fielder and bowler and fielder == bowler:
            return f"c & b {bowler}".strip()
        return (f"c {fielder} b {bowler}" if fielder else f"c b {bowler}").strip()
    if low in ("caught and bowled", "c&b", "caught & bowled"):
        return f"c & b {bowler}".strip()
    if low == "stumped":
        return (f"st {fielder} b {bowler}" if fielder else f"st b {bowler}").strip()
    if low in ("run out", "runout"):
        return f"run out ({fielder})".strip() if fielder else "run out"
    if low == "hit wicket":
        return f"hit wicket b {bowler}".strip()
    # Already a full line (e.g. an "c X b Y" stored verbatim)? Keep it.
    return how


def _merge_stats(state, innings_filter=None):
    """Build the ``stats`` map keyed by str(roster_id), combining batting and
    bowling figures the way UnderCover's frontend expects.

    innings_filter: None (both) | 1 (innings-1 only) | 2 (innings-2 only).
    """
    out = {}
    bat = state.get("bat_stats", {}) or {}
    bowl = state.get("bowl_stats", {}) or {}
    inn1_bat = state.get("inn1_bat_stats", {}) or {}
    inn1_bowl = state.get("inn1_bowl_stats", {}) or {}

    keys = set()
    for d in (bat, bowl, inn1_bat, inn1_bowl):
        for k in d.keys():
            keys.add(str(k))
    for side in ("bat_xi", "bowl_xi", "batting_order",
                 "inn1_bat_xi", "inn1_bowl_xi"):
        for pl in state.get(side, []) or []:
            keys.add(str(pl.get("roster_id")))

    # Which stats pools to use based on innings_filter
    if innings_filter == 1:
        # During innings 1, inn1_bat/inn1_bowl are empty (saved only at innings end).
        # Fall back to the live bat/bowl stats so the scorecard shows live data.
        bat_pool = inn1_bat if inn1_bat else bat
        bowl_pool = inn1_bowl if inn1_bowl else bowl
    elif innings_filter == 2:
        bat_pool, bowl_pool = bat, bowl
    else:
        # Merge: prefer current-innings bat stats; fall back to inn1 for players
        # who have only batted in innings 1 (e.g. viewing inn1 scorecard in inn2).
        bat_pool = bat
        bowl_pool = bowl

    for k in keys:
        bs = _stat(bat_pool, k)
        # For the combined view, if current innings has no batting data for this
        # player, try innings-1 stats (covers the inn1 scorecard tab in inn2).
        if innings_filter is None and not any(bs.values()):
            bs_alt = _stat(inn1_bat, k)
            if any(bs_alt.values()):
                bs = bs_alt
        ws = _stat(bowl_pool, k)
        if innings_filter is None and not any(ws.values()):
            ws_alt = _stat(inn1_bowl, k)
            if any(ws_alt.values()):
                ws = ws_alt
        bpu = balls_per_unit(state)
        w_balls = ws.get("balls", 0)
        overs_done = ws.get("overs_done", 0)
        this_over = ws.get("this_over_balls", 0)
        if _is_hundred(state):
            overs_display = f"{w_balls}b"
        else:
            overs_display = f"{overs_done}.{this_over}" if this_over else f"{overs_done}"
        out[k] = {
            "runs": bs.get("runs", 0),
            "balls": bs.get("balls", 0),
            "fours": bs.get("fours", 0),
            "sixes": bs.get("sixes", 0),
            "isOut": bool(bs.get("out", False)),
            "how_out": _full_dismissal(bs),
            "wickets": ws.get("wickets", 0),
            "runsConceded": ws.get("runs", 0),
            "maidens": ws.get("maidens", 0),
            "overs": overs_done,  # whole units completed — used for bowling-quota comparisons
            "oversDisplay": overs_display,  # "10b" (Hundred) or "1.4"/"2" (over formats)
            "ballsBowled": w_balls,
            "economy": round(ws.get("runs", 0) / (w_balls / bpu), 2) if w_balls else 0,
        }
    return out


def _card(state, player, kind):
    """striker/nonStriker/bowler card = player object + its ``stats`` block."""
    if not player:
        return None
    base = _p(player)
    rid = player.get("roster_id")
    if kind == "bowler":
        ws = _stat(state.get("bowl_stats", {}), rid)
        bpu = balls_per_unit(state)
        w_balls = ws.get("balls", 0)
        overs_done = ws.get("overs_done", 0)
        this_over = ws.get("this_over_balls", 0)
        if _is_hundred(state):
            overs_display = f"{w_balls}b"
        else:
            overs_display = f"{overs_done}.{this_over}" if this_over else f"{overs_done}"
        base["stats"] = {
            "runsConceded": ws.get("runs", 0),
            "wickets": ws.get("wickets", 0),
            "maidens": ws.get("maidens", 0),
            "overs": overs_done,  # whole units completed — used for bowling-quota comparisons
            "oversDisplay": overs_display,
            "balls": w_balls,
            "economy": round(ws.get("runs", 0) / (w_balls / bpu), 2) if w_balls else 0,
        }
    else:
        bs = _stat(state.get("bat_stats", {}), rid)
        base["stats"] = {
            "runs": bs.get("runs", 0),
            "balls": bs.get("balls", 0),
            "fours": bs.get("fours", 0),
            "sixes": bs.get("sixes", 0),
            "isOut": bool(bs.get("out", False)),
        }
    return base


def _by_idx(order, idx):
    if idx is None:
        return None
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        return None
    if 0 <= idx < len(order):
        return order[idx]
    return None


def _parse_overs_str(ov):
    """'12.3' → (overs=12, balls=3). Tolerant of '' / ints."""
    if ov is None or ov == "":
        return 0, 0
    s = str(ov)
    if "." in s:
        a, b = s.split(".", 1)
        try:
            return int(a), int(b)
        except ValueError:
            return 0, 0
    try:
        return int(s), 0
    except ValueError:
        return 0, 0


# ── viewer / match resolution ────────────────────────────────────────

def resolve_viewer(session, user_id_param):
    """The frontend sends the raw Telegram user id as ``userId`` (or the
    literal 'spectator'). Resolve it to an internal User. Returns the User or
    None (spectator)."""
    from models import User
    if not user_id_param or str(user_id_param).lower() in ("spectator", "null", "undefined"):
        return None
    raw = str(user_id_param).strip()
    # Telegram ids are large; internal ids are small. Try telegram_id first,
    # then fall back to internal id so older links keep working.
    try:
        tg = int(raw)
    except ValueError:
        return None
    u = session.query(User).filter(User.telegram_id == tg).first()
    if u:
        return u
    return session.query(User).get(tg)


def find_active_match(session, user, match_id=None):
    """Resolve the Match to show. Explicit match_id wins; otherwise the user's
    latest 'playing' match. Returns a Match or None."""
    from models import Match
    if match_id:
        try:
            return session.query(Match).get(int(match_id))
        except (TypeError, ValueError):
            return None
    if not user:
        return None
    return (session.query(Match)
            .filter(((Match.user1_id == user.id) | (Match.user2_id == user.id)),
                    Match.status == "playing")
            .order_by(Match.id.desc()).first())


# ── index translation for the write endpoints ───────────────────────

def xi_index_to_batting_order_index(state, xi_index):
    """The frontend's wicket-batsman picker indexes into the fixed batting XI;
    CCM26's select_wicket_batsman indexes into batting_order. Translate via
    roster id."""
    bat_xi = state.get("bat_xi", []) or []
    p = _by_idx(bat_xi, xi_index)
    if not p:
        return None
    rid = p.get("roster_id")
    order = state.get("batting_order", []) or []
    for i, op in enumerate(order):
        if op.get("roster_id") == rid:
            return i
    return None


def xi_index_to_bowler_rid(state, xi_index):
    """Frontend bowler index → roster id (for select_new_bowler)."""
    bowl_xi = state.get("bowl_xi", []) or []
    p = _by_idx(bowl_xi, xi_index)
    return p.get("roster_id") if p else None


# ── the big one: full serialized match state ─────────────────────────

def serialize_match_state(session, match, viewer_user):
    """Build the UnderCover ``serializeMatchState`` payload from CCM26's live
    state. ``viewer_user`` may be None (spectator). Returns dict or None if the
    match has no live state."""
    with perf_span("serialize_match_state", match.id):
        return _serialize_match_state_impl(session, match, viewer_user)


def _serialize_match_state_impl(session, match, viewer_user):
    match_id = match.id
    state = mwa.get_state(match_id)
    completed_snapshot = False
    if not state and match.status == "completed":
        # Live state is intentionally cleaned up at the end of a match. Restore
        # the persisted read-only Arena snapshot so Play Match can reopen the
        # result summary and scorecard later.
        try:
            from services.match_webapp_service import load_final_scorecard
            state = (load_final_scorecard(session, match_id) or {}).get("arena_state")
            completed_snapshot = bool(state)
        except Exception:
            logger.exception("completed arena snapshot load failed")
    if not state:
        return None

    next_action = "COMPLETED" if completed_snapshot else mwa.get_next_action(match_id)
    # Prefer the state-machine terminal pointer over the Match row while an
    # action request is still finalizing. This prevents the Arena controls from
    # rendering an opponent-waiting sheet after a chase has already reached its
    # target but before the DB row is observed as completed.
    effective_match_status = "completed" if next_action == "COMPLETED" else match.status
    status = phase_status(state, effective_match_status)  # xi_selection/innings1/innings2/completed
    turn_state = turn_state_name(next_action)            # bowling_delivery/batting_shot/...
    viewer_uid = viewer_user.id if viewer_user else None

    role = role_for(state, viewer_uid) if viewer_uid else "spectator"
    my_role = {"batsman": "batting", "bowler": "bowling"}.get(role, "spectator")

    # Challenge League (/cipl) matches are played over-by-over entirely in the
    # Telegram chat (bowler/approach inline buttons in cipl_play.py). The Mini
    # App is a spectate-only board for EVERYONE — including the two captains — so
    # force the read-only spectator view: no batting/bowling controls, no "your
    # turn" prompts, no Impact Player picker. The action endpoints reject manual
    # submissions too (services.match_webapp_service.is_view_only_match).
    cipl_view_only = state.get("mode") == "cipl_approach"
    if cipl_view_only:
        role = "spectator"
        my_role = "spectator"

    # Telegram-id map for the two participants (battingId/bowlingId fields use
    # telegram ids in the UnderCover shape).
    u1 = _user_lite(session, match.user1_id)
    u2 = _user_lite(session, match.user2_id)
    tg_of = {}
    if u1:
        tg_of[u1.id] = u1.telegram_id
    if u2:
        tg_of[u2.id] = u2.telegram_id

    bat_team_id = state.get("bat_team_id")
    bowl_team_id = state.get("bowl_team_id")
    openers_done = bool(state.get("openers_done"))
    bowler_done = bool(state.get("bowler_done"))

    # ── is it my turn? ──
    if status == "xi_selection":
        if my_role == "batting":
            is_my_turn = not openers_done
        elif my_role == "bowling":
            is_my_turn = not bowler_done
        else:
            is_my_turn = False
    else:
        _, is_my_turn = whose_turn(state, next_action, viewer_uid) if viewer_uid else (None, False)

    # CIPL: a captain is still bat_team_id/bowl_team_id in state, so whose_turn
    # would otherwise hand them the turn. Spectate-only means it's never "my
    # turn" in the Mini App.
    if cipl_view_only:
        is_my_turn = False

    # ── host / guest blocks (stable: host=user1, guest=user2) ──
    def _team_block(u):
        if not u:
            return None
        is_bat = (u.id == bat_team_id)
        xi = state.get("bat_xi", []) if is_bat else state.get("bowl_xi", [])
        confirmed = openers_done if is_bat else bowler_done
        team_name = (state.get("host_name") if u.id == match.user1_id
                     else state.get("guest_name"))
        # Challenge League (/cipl) matches carry the real franchise names on the
        # batting/bowling side rather than host_name/guest_name. bat_team_id and
        # bat_team_name swap together each innings, so the side flag still maps a
        # user to their own team. This is what turns "@User vs @User" into the
        # actual "CSK vs MI".
        if not team_name:
            team_name = (state.get("bat_team_name") if is_bat
                         else state.get("bowl_team_name"))
        return {
            "telegramId": u.telegram_id,
            "username": u.username,
            "teamName": team_name or u.team_name or (f"@{u.username}" if u.username else "Player"),
            "xi": [_p(p) for p in xi],
            "confirmed": bool(confirmed),
        }

    host = _team_block(u1)
    guest = _team_block(u2)

    # ── score / innings ──
    is_hundred_match = _is_hundred(state)
    bpu = balls_per_unit(state)
    cur_runs = state.get("total_runs", 0)
    cur_wkts = state.get("total_wickets", 0)
    cur_overs = max(0, state.get("current_over", 1) - 1)
    cur_balls = state.get("current_ball", 0)
    target = state.get("target")
    innings_no = state.get("innings", 1)
    current_innings_idx = innings_no - 1

    # Projected score — extrapolate the current run rate across the remaining
    # balls of the innings. 1st innings only (2nd innings shows the target bar).
    total_balls = (state.get("overs", 0) or 0) * bpu
    balls_bowled = cur_overs * bpu + cur_balls
    projected = None
    if innings_no == 1 and balls_bowled:
        projected = round(cur_runs + (cur_runs / balls_bowled) * max(0, total_balls - balls_bowled))

    score = {
        "runs": cur_runs, "wickets": cur_wkts,
        "balls": cur_balls, "overs": cur_overs,
        "ballsBowled": balls_bowled,
        "target": target,
        "batTeamName": state.get("bat_team_name"),
        "bowlTeamName": state.get("bowl_team_name"),
        "projected": projected,
    }

    # Live win probability for a chase (2nd innings). Reuses the engine's
    # chase-chance model (matrix + batter/bowler/pitch/momentum modifiers +
    # feasibility for the balls left) so the broadcast bar agrees with the
    # in-match steer. None in the 1st innings / once the chase is settled.
    win_probability = None
    if innings_no == 2 and target is not None and balls_bowled < total_balls:
        try:
            from engine import chase_chance as cc
            runs_needed = max(0, int(target) - int(cur_runs))
            balls_left = max(0, total_balls - balls_bowled)
            if balls_left > 0:
                order = state.get("batting_order") or []
                striker_p = _by_idx(order, state.get("striker_idx", 0)) or {}
                non_striker_p = _by_idx(order, state.get("non_striker_idx", 1)) or {}
                bowler_p = state.get("current_bowler") or {}
                bat_stats = state.get("bat_stats", {})
                striker_runs = (bat_stats.get(str(striker_p.get("roster_id")), {})
                                or {}).get("runs", 0) if isinstance(striker_p, dict) else 0
                rrr = runs_needed * 6.0 / balls_left
                window = state.get("recent_runs_window") or []
                info = cc.final_chase_chance(
                    runs_needed, cur_wkts,
                    batter_mod=cc.batter_modifier(striker_p, non_striker_p, striker_runs),
                    bowler_mod=cc.bowler_modifier(bowler_p),
                    pitch_mod=cc.pitch_modifier(state.get("pitch_type")),
                    momentum_mod=cc.momentum_modifier(
                        rrr, sum(window) if window else None,
                        len(window) if window else None),
                )
                info = cc.apply_feasibility(info, runs_needed, balls_left)
                win_probability = {
                    "batting": info["chasing_chance"],
                    "bowling": info["defending_chance"],
                    "battingTeam": state.get("bat_team_name"),
                    "bowlingTeam": state.get("bowl_team_name"),
                }
        except Exception:
            logger.exception("arena win probability failed")

    # Recent-ball timeline → structured chips for the Mini App's doodle row.
    # Reuses the emoji symbols already maintained in state["timeline"] (SYM dict).
    _TL_MAP = {
        "🟥": ("W", "wkt"), "↔️": ("wd", "extra"),
        "🄽🄱": ("nb", "extra"), "𓂾": ("lb", "extra"),
        "0️⃣": ("0", "dot"), "1️⃣": ("1", "run"), "2️⃣": ("2", "run"),
        "3️⃣": ("3", "run"), "4️⃣": ("4", "four"), "5️⃣": ("5", "run"),
        "6️⃣": ("6", "six"),
    }
    timeline_chips = [
        {"label": _TL_MAP.get(s, (s, "run"))[0], "kind": _TL_MAP.get(s, (s, "run"))[1]}
        for s in (state.get("timeline") or [])[-10:]
    ]

    # ── innings break (target + 1st-innings scorecard, before 2nd-innings XI
    # picking) — server-clock-driven so both clients count down identically ──
    innings_break = None
    if state.get("setup") == SETUP_INNINGS_BREAK:
        started_at = state.get("innings_break_started_at") or 0
        elapsed = max(0.0, _time.time() - started_at)
        innings_break = {
            "active": True,
            "secondsRemaining": max(0, round(INNINGS_BREAK_SECONDS - elapsed)),
            "durationSeconds": INNINGS_BREAK_SECONDS,
        }

    # innings[0] (first) and innings[1] (second)
    inn1_bat_tg = tg_of.get(state.get("inn1_bat_team_id"))
    inn1_bowl_tg = tg_of.get(state.get("inn1_bowl_team_id"))
    bat_tg = tg_of.get(bat_team_id)
    bowl_tg = tg_of.get(bowl_team_id)

    innings_arr = [None, None]
    if innings_no == 1 and status != "completed":
        innings_arr[0] = {
            "battingId": bat_tg, "bowlingId": bowl_tg,
            "runs": cur_runs, "wickets": cur_wkts,
            "balls": cur_balls, "overs": cur_overs,
            "extras": state.get("extras_total", 0), "target": None,
        }
        innings_arr[1] = {
            "battingId": bowl_tg, "bowlingId": bat_tg,
            "runs": 0, "wickets": 0, "balls": 0, "overs": 0,
            "extras": 0, "target": None,
        }
    else:
        # First innings has finished — read the saved summary.
        i1_ov, i1_b = _parse_overs_str(state.get("inn1_overs"))
        innings_arr[0] = {
            "battingId": inn1_bat_tg, "bowlingId": inn1_bowl_tg,
            "runs": state.get("inn1_runs", 0), "wickets": state.get("inn1_wickets", 0),
            "balls": i1_b, "overs": i1_ov,
            "extras": 0, "target": None,
        }
        innings_arr[1] = {
            "battingId": bat_tg, "bowlingId": bowl_tg,
            "runs": cur_runs, "wickets": cur_wkts,
            "balls": cur_balls, "overs": cur_overs,
            "extras": state.get("extras_total", 0), "target": target,
        }

    # ── players on the field ──
    order = state.get("batting_order", []) or []
    striker = _card(state, _by_idx(order, state.get("striker_idx")), "bat")
    non_striker = _card(state, _by_idx(order, state.get("non_striker_idx")), "bat")
    bowler = _card(state, state.get("current_bowler"), "bowler")

    # ── last ball + commentary ──
    last_ball_raw = state.get("last_ball") or {}
    lb_type = (last_ball_raw.get("type") or "").lower()
    lb_runs = last_ball_raw.get("runs", 0)
    is_wicket = lb_type == "wicket" or bool(last_ball_raw.get("how"))
    is_boundary = lb_type in ("four", "six") or lb_runs in (4, 6)
    last_ball = None
    commentary = []
    if last_ball_raw:
        last_ball = {
            "runs": lb_runs,
            "isWicket": is_wicket,
            "isBoundary": is_boundary,
            "commentary": last_ball_raw.get("text"),
            "delivery": last_ball_raw.get("delivery") or "",
            "shot": last_ball_raw.get("shot") or "",
            "batter": last_ball_raw.get("batsman") or "",
            "bowler": last_ball_raw.get("bowler") or "",
            "eventKey": last_ball_raw.get("eventKey"),
            "isFreeHit": bool(last_ball_raw.get("free_hit")),
            "isMystery": bool(last_ball_raw.get("mystery")),
        }
        commentary = [{
            "type": "ball",
            "over": f"{cur_overs}.{cur_balls}",
            "runs": lb_runs,
            "isWicket": is_wicket,
            "eventKey": (last_ball_raw.get("eventKey")
                         or {"wicket": "wicket", "wide": "wide", "noball": "no_ball"}.get(lb_type)
                         or ("dot_ball" if lb_runs == 0 else "four" if lb_runs == 4 else "six" if lb_runs == 6 else None)),
            "text": state.get("last_commentary") or last_ball_raw.get("text") or "",
        }]

    # Prefer the full accumulated feed (ball rows + end_of_over /
    # end_of_innings cards) when present, newest-first like UnderCover.
    full_log = state.get("commentary_log")
    if isinstance(full_log, list) and full_log:
        commentary = list(reversed(full_log))
        if last_ball_raw and commentary and isinstance(commentary[0], dict):
            # Preserve milestone/special event keys from the latest delivery so
            # the MiniApp GIF box can fire Fifty/Century/Implant animations.
            latest_key = (last_ball_raw.get("eventKey")
                          or {"wicket": "wicket", "wide": "wide", "noball": "no_ball"}.get(lb_type)
                          or ("dot_ball" if lb_runs == 0 else "four" if lb_runs == 4 else "six" if lb_runs == 6 else None))
            if latest_key and not commentary[0].get("eventKey"):
                commentary[0]["eventKey"] = latest_key

    # ── /playmatch-compatible delivery vocabulary ──
    # Source these from the same service used by the Telegram /playmatch flow,
    # replacing the premium UI's hard-coded UnderCover-only variations.
    delivery_options = None
    current_bowler = state.get("current_bowler") or {}
    if current_bowler:
        try:
            from services.bowling_service import get_delivery_options
            delivery_options = get_delivery_options(
                current_bowler.get("bowl_style"), current_bowler.get("bowl_hand"))
        except Exception:
            logger.exception("arena delivery options failed (non-fatal)")

    # ── toss ──
    toss_winner_tg = tg_of.get(match.toss_winner_id) if match.toss_winner_id else None

    # ── autoplay status ──
    autoplay_users = state.get("autoplay_users") or {}
    autoplay_active_uids = {
        int(uid) for uid, active in autoplay_users.items()
        if active and str(uid).lstrip("-").isdigit()
    }

    def _team_name_for_uid(uid):
        if uid == match.user1_id and host:
            return host.get("teamName")
        if uid == match.user2_id and guest:
            return guest.get("teamName")
        return None

    autoplay_teams = [
        {"userId": uid, "teamName": _team_name_for_uid(uid)}
        for uid in autoplay_active_uids
    ]
    my_autoplay_active = bool(viewer_uid in autoplay_active_uids)
    opponent_autoplay = next(
        (team for team in autoplay_teams if team.get("userId") != viewer_uid),
        None,
    )

    # ── Impact Player availability/summary ──
    impact_player = {"canUse": False, "used": False, "summary": _impact_summary_for_result(state)}
    if viewer_uid and status != "completed" and not cipl_view_only:
        try:
            from services.match_webapp_service import get_impact_player_options
            opts = get_impact_player_options(session, match_id, viewer_uid)
            if opts.get("ok"):
                impact_player = {
                    "canUse": bool(opts.get("can_use")),
                    "used": bool(opts.get("used")),
                    "legalBreak": opts.get("legal_break"),
                    "message": opts.get("message"),
                    "incomingOptions": [_p(p) for p in opts.get("incoming_options", [])],
                    "replaceablePlayers": [{**_p(p),
                                             "disabled": bool(p.get("disabled")),
                                             "disabledReason": p.get("disabled_reason")}
                                            for p in opts.get("replaceable_players", [])],
                    "summary": _impact_summary_for_result(state),
                }
        except Exception:
            logger.exception("impact player options failed (non-fatal)")

    # ── result (completed) ──
    result = None
    if status == "completed":
        result = _build_result(session, state, match, tg_of, host, guest)
        # Enrich the final end_of_innings card with winner/MOTM so the
        # commentary feed's innings-end overlay reads the same as the result.
        if isinstance(commentary, list) and result:
            win = result.get("winner") or {}
            win_name = win.get("teamName") or win.get("username")
            for ev in commentary:
                if isinstance(ev, dict) and ev.get("type") == "end_of_innings":
                    if ev.get("winner") is None:
                        ev["winner"] = win_name
                    if ev.get("motm") is None:
                        ev["motm"] = result.get("motm")

    # Build over-by-over run data for Manhattan chart
    inn1_over_runs = list(state.get("inn1_over_runs") or [])
    inn2_over_runs = list(state.get("over_runs") or [])
    if innings_no == 1:
        inn1_over_runs = inn2_over_runs
        inn2_over_runs = []

    # Partnership history per innings
    inn1_partnerships = list(state.get("inn1_partnership_history") or [])
    inn2_partnerships = list(state.get("partnership_history") or [])
    if innings_no == 1:
        inn1_partnerships = inn2_partnerships
        inn2_partnerships = []

    return {
        "id": str(match_id),
        "type": "pve" if state.get("is_vsbot") else "pvp",
        "chatId": state.get("chat_id"),
        "pitch": (match.pitch_type or state.get("pitch_type") or "normal"),
        "totalOvers": state.get("overs"),
        # "T20" (6-ball overs) or "The100" (5-ball sets, 100 balls/innings) so
        # the client can label progress correctly; absent/"T20" keeps the
        # standard over-based UI.
        "ballFormat": state.get("ball_format", "T20"),
        "isHundred": is_hundred_match,
        "totalBalls": total_balls,
        "status": status,
        "tossWinnerId": toss_winner_tg,
        "tossDecision": match.toss_decision,
        "turnState": turn_state,
        # Free hit armed for the upcoming legal ball (UnderCover /cric parity)
        "freeHit": bool(state.get("free_hit")),
        "ballSeq": mwa.get_ball_seq(match_id),
        # TTL-aware persisted-flag check (legacy in-flight matches) OR the
        # live in-process action guard — keeps app.js's autoplay gating
        # behavior while the flag is no longer persisted per ball.
        "isProcessing": _is_processing(state) or action_in_progress(match_id),
        "myRole": my_role,
        "isMyTurn": bool(is_my_turn),
        "result": result,
        "impactPlayer": impact_player,
        "deliveryOptions": delivery_options,
        "host": host,
        "guest": guest,
        "currentInningsIdx": current_innings_idx,
        "innings": innings_arr,
        "inningsBreak": innings_break,
        "score": score,
        "timeline": timeline_chips,
        "striker": striker,
        "nonStriker": non_striker,
        "bowler": bowler,
        "battingXI": [_p(p) for p in state.get("bat_xi", [])],
        "bowlingXI": [_p(p) for p in state.get("bowl_xi", [])],
        "stats": _merge_stats(state),
        # Per-innings stats for the scorecard tabs
        "innings1Stats": _merge_stats(state, innings_filter=1),
        "innings2Stats": _merge_stats(state, innings_filter=2),
        "commentary": commentary,
        "currentDelivery": state.get("current_delivery"),
        # Keep the qualitative speed label and numeric km/h value separate.
        # The batting controls render the label with string operations, while
        # last_speed is the simulation-generated numeric speed.
        "currentSpeed": state.get("current_speed"),
        "currentSpeedKmh": state.get("last_speed"),
        "lastBall": last_ball,
        "lastAutoplayDelivery": state.get("last_autoplay_delivery"),
        "lastAutoplayShot": state.get("last_autoplay_shot"),
        "autoplay": {
            "isOnForMe": my_autoplay_active,
            "opponent": opponent_autoplay,
            "teams": autoplay_teams,
        },
        "partnership": {
            "runs": state.get("partnership_runs", 0),
            "balls": state.get("partnership_balls", 0),
        },
        "inn1OverRuns": inn1_over_runs,
        "inn2OverRuns": inn2_over_runs,
        "inn1PartnershipHistory": inn1_partnerships,
        "inn2PartnershipHistory": inn2_partnerships,
        "winProbability": win_probability,
    }



def _impact_summary_for_result(state):
    usage = ((state.get("impact_players") or {}).get("usage") or {})
    rows = []
    for uid_s, rec in usage.items():
        if not isinstance(rec, dict) or not rec.get("used"):
            continue
        rows.append({
            "teamName": rec.get("team_name") or "Team",
            "inPlayer": rec.get("in_player"),
            "outPlayer": rec.get("out_player"),
            "usedAt": rec.get("used_at"),
            "innings": rec.get("innings"),
        })
    return rows

def _build_result(session, state, match, tg_of, host, guest):
    """Result overlay payload for a completed match."""
    inn1_runs = state.get("inn1_runs", 0)
    inn2_runs = state.get("total_runs", 0)
    target = state.get("target")

    # Which side batted second this match? bat_team_id is the 2nd-innings
    # batting side once we're past innings 1.
    second_bat_uid = state.get("bat_team_id")
    first_bat_uid = state.get("inn1_bat_team_id")

    winner_uid = None
    if target and inn2_runs >= target:
        winner_uid = second_bat_uid
    elif inn2_runs < inn1_runs:
        winner_uid = first_bat_uid
    elif inn2_runs > inn1_runs:           # safety (shouldn't trip if target set)
        winner_uid = second_bat_uid

    def _block_for_uid(uid):
        if uid == match.user1_id:
            return host
        if uid == match.user2_id:
            return guest
        return None

    win_block = _block_for_uid(winner_uid)

    # Rewards (coins) for the overlay. Prefer the amounts actually awarded at
    # finalization (including event multipliers), with a config fallback for
    # older persisted matches.
    awarded = state.get("_completed_rewards") or {}
    winner_reward = awarded.get("winner_coins", 0)
    loser_reward = awarded.get("loser_coins", 0)
    if not awarded:
        try:
            from services.config_service import get_config
            cfg = get_config(session)
            overs = state.get("overs") or 0
            winner_reward = int(overs * cfg["match_win_coins_per_over"])
            loser_reward = int(overs * cfg["match_loss_coins_per_over"])
        except Exception:
            logger.exception("crickidex result reward calc failed (non-fatal)")

    # Man of the match — best (runs + 25*wickets) across both XIs.
    stats = _merge_stats(state)
    best_rid, best_score = None, -1
    name_by_rid = {}
    for side in ("bat_xi", "bowl_xi"):
        for pl in state.get(side, []) or []:
            name_by_rid[str(pl.get("roster_id"))] = pl.get("name")
    for rid, st in stats.items():
        sc = st.get("runs", 0) + st.get("wickets", 0) * 25
        if sc > best_score:
            best_score, best_rid = sc, rid
    motm = None
    persisted_motm = state.get("_player_of_match") or {}
    if best_rid is not None and best_score > 0:
        st = stats[best_rid]
        motm = {
            "name": persisted_motm.get("name") or name_by_rid.get(best_rid, "Player"),
            "runs": st.get("runs", 0),
            "balls": st.get("balls", 0),
            "wickets": st.get("wickets", 0),
            "overs": st.get("oversDisplay", 0),
            "impactPoints": persisted_motm.get("impact_points", best_score),
        }

    return {
        "winner": ({"telegramId": win_block.get("telegramId"),
                    "username": win_block.get("username"),
                    "teamName": win_block.get("teamName")} if win_block else None),
        "winnerReward": winner_reward,
        "loserReward": loser_reward,
        "resultText": (state.get("match_result") or {}).get("text"),
        "motm": motm,
        "impactPlayers": state.get("impact_player_summary") or _impact_summary_for_result(state),
    }
