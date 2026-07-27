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
    A_PICK_NEW_BOWLER, A_INNINGS_BREAK, A_COMPLETED, CasAbort,
)

logger = logging.getLogger(__name__)

# Setup-phase actions (before the ball loop): tracked in state["setup"]
SETUP_PICKING = "PICKING"
SETUP_AWAIT_OPENERS = "AWAIT_OPENERS"
SETUP_AWAIT_BOWLER = "AWAIT_BOWLER"
SETUP_AWAIT_READY = "AWAIT_READY"
SETUP_DONE = "DONE"
# Transient phase shown between innings: target + 1st-innings scorecard, before
# either side picks their 2nd-innings XI. Auto-advances after INNINGS_BREAK_SECONDS.
SETUP_INNINGS_BREAK = "INNINGS_BREAK"
INNINGS_BREAK_SECONDS = 8

# Message returned when a manual Mini-App action is rejected because the match is
# spectate-only (see is_view_only_match).
VIEW_ONLY_MESSAGE = ("Challenge League matches are played in chat — "
                     "the Mini App is view-only (spectate).")


def is_view_only_match(state):
    """True when the live match must not be driven from the Mini App.

    Challenge League (/cipl) matches run as an over-by-over "Approach" game that
    is played entirely from the Telegram chat (coin call, toss, per-over
    bowler/approach inline buttons in cipl_play.py). For those matches the Mini
    App is a read-only spectate board for EVERYONE — including the two captains —
    so every gameplay mutator below refuses to act on them.
    """
    return bool(state) and state.get("mode") == "cipl_approach"



def _player_dict_from_roster(entry, player):
    return {
        "roster_id": entry.id,
        "player_id": player.id,
        "name": player.name,
        "rating": player.rating,
        "bat_rating": player.bat_rating or 0,
        "bowl_rating": player.bowl_rating or 0,
        "category": player.category,
        "bowl_style": player.bowl_style,
        "bowl_hand": player.bowl_hand,
        "bat_hand": player.bat_hand,
    }


def _career_map(session, uid):
    """Precompute career stat fields (R/SR/W/Eco) per player_id for a user.

    Read once at match init so the live commentary path is pure dict lookups
    with no per-ball DB hits. Returns {} on any failure (e.g. synthetic teams).
    """
    out = {}
    if session is None or uid is None:
        return out
    try:
        from models import PlayerGameStats
        rows = (session.query(PlayerGameStats)
                .filter(PlayerGameStats.user_id == uid).all())
    except Exception:
        logger.exception("career stat lookup failed (non-fatal)")
        return out
    for g in rows:
        out[g.player_id] = {
            "careerR": g.runs or 0,
            "careerSR": round((g.runs / g.balls_faced) * 100, 1) if g.balls_faced else 0.0,
            "careerW": g.wickets_taken or 0,
            "careerEco": round(g.runs_conceded / (g.balls_bowled / 6), 1) if g.balls_bowled else 0.0,
        }
    return out


def _bat_career_line(p):
    """'R 514 | SR 189' when career data exists, else 'OVR 84' fallback."""
    p = p or {}
    if p.get("careerR") or p.get("careerSR"):
        return f"R {p.get('careerR', 0)} | SR {p.get('careerSR', 0.0)}"
    return f"OVR {p.get('bat_rating') or p.get('rating') or 0}"


def _bowl_career_line(p):
    """'W 88 | Eco 6.7' when career data exists, else 'OVR 84' fallback."""
    p = p or {}
    if p.get("careerW") or p.get("careerEco"):
        return f"W {p.get('careerW', 0)} | Eco {p.get('careerEco', 0.0)}"
    return f"OVR {p.get('bowl_rating') or p.get('rating') or 0}"


def _bowl_match_fig(state, p):
    """This-match bowling figures as 'overs-runs-wickets' (e.g. '3-31-2'),
    or 'balls-runs-wickets' (e.g. '10b-31-2') for The Hundred."""
    if not p:
        return "0-0-0"
    bws = _stat_row(state.get("bowl_stats"), p.get("roster_id"))
    if _is_hundred_state(state):
        ov_lbl = f"{bws.get('balls', 0)}b"
    else:
        ov = bws.get("overs_done", 0)
        tb = bws.get("this_over_balls", 0)
        ov_lbl = f"{ov}.{tb}" if tb else f"{ov}"
    return f"{ov_lbl}-{bws.get('runs', 0)}-{bws.get('wickets', 0)}"


def _push_commentary(state, entry):
    """Append a standalone styled event card to the scrolling commentary feed,
    capping the log the same way _append_commentary_log does."""
    log = state.get("commentary_log")
    if not isinstance(log, list):
        log = []
    log.append(entry)
    if len(log) > 60:
        log = log[-60:]
    state["commentary_log"] = log


def _emit_new_batsman(state, idx):
    """Green 'comes in' card when a new striker is installed after a wicket."""
    order = state.get("batting_order", []) or []
    if not (0 <= idx < len(order)):
        return
    p = order[idx] or {}
    _push_commentary(state, {
        "type": "new_batsman",
        "name": p.get("name"),
        "text": f"{p.get('name')} comes in. {_bat_career_line(p)}",
    })


def _slot_is_out(state, slot_key):
    """True if the batsman currently occupying ``slot_key`` (striker_idx /
    non_striker_idx) is marked out in bat_stats. State is JSON round-tripped so
    bat_stats keys may be int or str — check both."""
    order = state.get("batting_order", []) or []
    si = state.get(slot_key)
    if si is None or not (0 <= si < len(order)):
        return False
    rid = (order[si] or {}).get("roster_id")
    stats = state.get("bat_stats", {}) or {}
    st = stats.get(rid) or stats.get(str(rid))
    return bool(st and st.get("out"))


def _install_new_batsman(state, idx):
    """Install the incoming batsman (batting-order index ``idx``) into whichever
    crease slot holds the dismissed batsman.

    A wicket only ever dismisses the striker, but the end-of-over swap in
    ``_apply_outcome`` moves that dismissed batsman to ``non_striker_idx`` when
    the wicket falls on the last ball of an over. Blindly writing
    ``striker_idx = idx`` would then overwrite the *not-out* partner and leave
    the dismissed batsman parked at the crease. Replace the out slot directly so
    the not-out partner stays on strike for the new over.
    """
    if _slot_is_out(state, "non_striker_idx") and not _slot_is_out(state, "striker_idx"):
        state["non_striker_idx"] = idx
    else:
        state["striker_idx"] = idx
    _emit_new_batsman(state, idx)


def _bowling_quota(total_overs):
    """Maximum overs a single bowler may bowl: ceil(totalOvers / 5), min 1.
    Matches the per-bowler cap enforced by the Mini App frontend."""
    import math
    try:
        return max(1, math.ceil(int(total_overs) / 5))
    except (TypeError, ValueError):
        return 1


def _emit_new_bowler(state, p):
    """Blue card when a bowler comes on. Shows this-match figures if the bowler
    has already bowled this innings (returning), else career/basic stats."""
    if not p:
        return
    bws = _stat_row(state.get("bowl_stats"), p.get("roster_id"))
    already_bowled = (bws.get("balls", 0) > 0) or (bws.get("overs_done", 0) > 0)
    if already_bowled:
        _push_commentary(state, {
            "type": "returning_bowler",
            "name": p.get("name"),
            "text": f"{p.get('name')} returns to bowl. {_bowl_match_fig(state, p)}",
        })
    else:
        _push_commentary(state, {
            "type": "new_bowler",
            "name": p.get("name"),
            "text": f"{p.get('name')} comes into attack. {_bowl_career_line(p)}",
        })


def _impact_usage(state):
    impact = state.setdefault("impact_players", {})
    usage = impact.setdefault("usage", {})
    for uid in (state.get("bat_team_id"), state.get("bowl_team_id"),
                state.get("inn1_bat_team_id"), state.get("inn1_bowl_team_id")):
        if uid is not None:
            usage.setdefault(str(uid), {"used": False})
    return impact, usage


def _impact_player_summary(state):
    _impact_usage(state)
    usage = (state.get("impact_players") or {}).get("usage") or {}
    summaries = []
    for uid_s, rec in usage.items():
        if not isinstance(rec, dict) or not rec.get("used"):
            continue
        try:
            uid = int(uid_s)
        except (TypeError, ValueError):
            uid = uid_s
        team = rec.get("team_name")
        if not team:
            if uid == state.get("bat_team_id"):
                team = state.get("bat_team_name")
            elif uid == state.get("bowl_team_id"):
                team = state.get("bowl_team_name")
            elif uid == state.get("inn1_bat_team_id"):
                team = state.get("inn1_team")
        summaries.append({
            "user_id": uid,
            "team_name": team or "Team",
            "in_player": rec.get("in_player"),
            "out_player": rec.get("out_player"),
            "used_at": rec.get("used_at"),
            "innings": rec.get("innings"),
        })
    return summaries


IMPACT_INNINGS_BREAK_SETUP_PHASES = (
    SETUP_PICKING,
    SETUP_AWAIT_OPENERS,
    SETUP_AWAIT_BOWLER,
    SETUP_AWAIT_READY,
)


def _impact_break_label(state, next_action=None):
    na = next_action or mwa.get_next_action(state.get("match_id"))
    if (state.get("innings") == 2
            and state.get("setup") in IMPACT_INNINGS_BREAK_SETUP_PHASES):
        return "innings break"
    if na == A_PICK_NEW_BATSMAN:
        return "after wicket"
    if na == A_PICK_NEW_BOWLER:
        return "between overs"
    return None


def _impact_is_legal_break(state, next_action=None):
    return _impact_break_label(state, next_action) is not None


def _team_active_xi_key(state, user_id):
    if user_id == state.get("bat_team_id"):
        return "bat_xi"
    if user_id == state.get("bowl_team_id"):
        return "bowl_xi"
    return None


def _state_bpu(state):
    """Legal balls per unit for a match state: 5 for The Hundred, else 6.

    Only Challenge League "The Hundred" matches set ``ball_format``; every other
    Mini-App match (``/wpm``, ``/cm``, tournaments) is unaffected and stays on
    6-ball overs.
    """
    return 5 if (state or {}).get("ball_format") == "The100" else 6


def _is_hundred_state(state):
    return (state or {}).get("ball_format") == "The100"


def _live_balls_bowled(state):
    """Legal balls bowled in the current innings, format-aware."""
    bpu = _state_bpu(state)
    return (state.get("current_over", 1) - 1) * bpu + state.get("current_ball", 0)


def _overs_display(state):
    """Innings progress for the Mini App: ``"12.3"`` for over formats, or the
    raw ball count (e.g. ``"63"``) for The Hundred, which has no overs."""
    balls = _live_balls_bowled(state)
    if _is_hundred_state(state):
        return str(balls)
    return f"{balls // 6}.{balls % 6}"


def _current_over_label(state):
    if _is_hundred_state(state):
        return f"{_live_balls_bowled(state)} balls"
    return f"{max(0, state.get('current_over', 1) - 1)}.{state.get('current_ball', 0)} ov"


def _is_active_player(player):
    return not isinstance(player, dict) or player.get("active", True) is not False


def _active_players(players):
    return [p for p in (players or []) if _is_active_player(p)]


# Categories permitted to bowl in bot matches (/wpmbot, /vsbot). Batsmen and
# wicket-keepers never bowl there — only all-rounders and specialist bowlers.
_BOWLING_CATEGORIES = {"bowler", "all-rounder", "allrounder", "all rounder"}


def _can_bowl(player):
    return (player.get("category") or "").strip().lower() in _BOWLING_CATEGORIES


def _bowling_eligible(players):
    """Filter players to those allowed to bowl (all-rounders + bowlers).

    Falls back to the full list if none qualify (defensive — XI validation
    already guarantees >=3 bowlers + >=1 all-rounder, so this never fires in
    practice, but it keeps the picker from dead-ending on malformed data)."""
    elig = [p for p in (players or []) if _can_bowl(p)]
    return elig or list(players or [])


def _bot_bowler_pool(state, players, prev_rid):
    """Players a bot-match team may pick from for the next over.

    Bot matches (``/wpmbot``, ``/vsbot``) normally restrict bowling to the
    front-line attack — all-rounders and specialist bowlers. But a team can run
    out of front-line overs: once every all-rounder/bowler is either the bowler
    who just bowled (no consecutive overs) or has used their full quota, there
    is nobody left to legally bowl. Rather than hand a specialist an illegal
    extra over, part-time batsmen come on to bowl — mirroring real cricket,
    where recognised batsmen fill in when the frontline quota is exhausted.

    Returns the front-line attack while at least one front-liner can still
    legally bowl; otherwise returns the full list so batsmen become selectable.
    The caller's usual quota / no-consecutive rules then apply to whichever
    tier is active (batsmen start fresh at 0 overs, so they pass the quota
    check that the exhausted specialists now fail).
    """
    players = list(players or [])
    front_line = [p for p in players if _can_bowl(p)]
    quota = _bowling_quota(state.get("overs"))
    front_available = any(
        p.get("roster_id") != prev_rid
        and _stat_row(state.get("bowl_stats"), p.get("roster_id")).get("overs_done", 0) < quota
        for p in front_line
    )
    if front_line and front_available:
        return front_line
    return players


def _replace_player_in_list(players, out_rid, incoming):
    for i, p in enumerate(players or []):
        if p.get("roster_id") == out_rid:
            players[i] = incoming
            return i
    return None


def _apply_impact_to_identity_list(players, out_rid, incoming):
    """Keep the outgoing player's identity for stat lookup, but mark inactive.

    Impact substitutions can occur after the outgoing player has already batted
    or bowled. Scorecard and career persistence resolve stat rows through these
    XI lists, so replacing the dict in-place can orphan existing stats.
    """
    for i, p in enumerate(players or []):
        if p.get("roster_id") == out_rid and _is_active_player(p):
            inactive = dict(p)
            inactive["active"] = False
            inactive["impact_replaced"] = True
            inactive["replaced_by_roster_id"] = incoming.get("roster_id")
            players[i] = inactive

            if not any(pl.get("roster_id") == incoming.get("roster_id") for pl in players):
                replacement = dict(incoming)
                replacement["active"] = True
                replacement["impact_replacement"] = True
                replacement["replaced_roster_id"] = out_rid
                players.append(replacement)
            return i
    return None


def get_impact_player_options(session, match_id, user_id):
    from models import UserRoster, Player
    state = mwa.get_state(match_id)
    if not state:
        return {"ok": False, "message": "Match not found."}
    if user_id not in (state.get("bat_team_id"), state.get("bowl_team_id")):
        return {"ok": False, "message": "Spectators cannot use Impact Player."}
    impact, usage = _impact_usage(state)
    rec = usage.setdefault(str(user_id), {"used": False})
    legal_label = _impact_break_label(state)
    used = bool(rec.get("used"))

    active_key = _team_active_xi_key(state, user_id)
    active_xi = _active_players(state.get(active_key, []) or [])
    active_ids = {p.get("roster_id") for p in active_xi}
    unavailable = set(active_ids)
    for urec in usage.values():
        if isinstance(urec, dict):
            if urec.get("in_roster_id") is not None:
                unavailable.add(urec.get("in_roster_id"))
            if urec.get("out_roster_id") is not None:
                unavailable.add(urec.get("out_roster_id"))

    rows = (session.query(UserRoster, Player).join(Player, UserRoster.player_id == Player.id)
            .filter(UserRoster.user_id == user_id)
            .order_by(UserRoster.order_position.asc(), UserRoster.acquired_date.asc())
            .all())
    incoming = [_player_dict_from_roster(e, p) for e, p in rows if e.id not in unavailable]

    # The Impact Player picker should let the team choose any still-active
    # member of the current Playing XI as the outgoing player.  Do not hide or
    # disable contextual players here: after a wicket the dismissed striker is
    # still the striker_idx until the replacement batter is picked, and between
    # overs the previous bowler is tracked in prev_bowler_rid.  Both are valid
    # Impact Player choices.
    replaceable = [{**p, "disabled": False, "disabled_reason": None} for p in active_xi]

    return {
        "ok": True,
        "can_use": (not used and bool(legal_label) and bool(incoming)),
        "used": used,
        "legal_break": legal_label,
        "message": ("Impact Player already used." if used else
                    "Impact Player is available." if legal_label else
                    "Impact Player can be used between overs, after a wicket, or at innings break."),
        "incoming_options": incoming,
        "replaceable_players": replaceable,
        "summary": _impact_player_summary(state),
    }


def use_impact_player(session, match_id, user_id, in_roster_id, out_roster_id):
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found.", None
    if is_view_only_match(state):
        return False, VIEW_ONLY_MESSAGE, None
    if user_id not in (state.get("bat_team_id"), state.get("bowl_team_id")):
        return False, "Spectators cannot use Impact Player.", None
    if not _impact_is_legal_break(state):
        return False, "Impact Player can be used only between overs, after a wicket, or at innings break.", None
    na = mwa.get_next_action(match_id)
    # When the Impact Player comes in live (after a wicket / between overs) they
    # walk straight to the crease or bowl the next over. ``forced_next_action``
    # captures that transition; ``None`` keeps the existing phase.
    forced_next_action = None
    impact, usage = _impact_usage(state)
    rec = usage.setdefault(str(user_id), {"used": False})
    if rec.get("used"):
        return False, "Impact Player already used by your team.", None

    opts = get_impact_player_options(session, match_id, user_id)
    incoming_by_rid = {p["roster_id"]: p for p in opts.get("incoming_options", [])}
    replaceable_by_rid = {p["roster_id"]: p for p in opts.get("replaceable_players", [])}
    incoming = incoming_by_rid.get(in_roster_id)
    outgoing = replaceable_by_rid.get(out_roster_id)
    if not incoming:
        return False, "Pick an incoming substitute from outside your Playing XI.", None
    if not outgoing:
        return False, "Pick a player from your current Playing XI to replace.", None
    if outgoing.get("disabled"):
        return False, outgoing.get("disabled_reason") or "That player cannot be replaced right now.", None

    # Bot matches keep the "only all-rounders & bowlers bowl" rule intact even
    # for Impact subs: if bringing this player in would hand them the ball
    # (they replace the current bowler, or they come on at the new-bowler
    # break), reject an ineligible substitute before any state mutation.
    if state.get("is_vsbot") and user_id == state.get("bowl_team_id") and not _can_bowl(incoming):
        would_bowl = (
            (state.get("current_bowler") or {}).get("roster_id") == out_roster_id
            or na == A_PICK_NEW_BOWLER
        )
        if would_bowl:
            return False, ("Only all-rounders and bowlers can bowl — bring on an "
                           "eligible bowler for the next over."), None

    xi_key = _team_active_xi_key(state, user_id)
    idx = _apply_impact_to_identity_list(state.get(xi_key, []), out_roster_id, incoming)
    if idx is None:
        return False, "Outgoing player is not in your active XI.", None

    if user_id == state.get("bat_team_id"):
        state.setdefault("bat_stats", {})[str(in_roster_id)] = {
            "runs": 0, "balls": 0, "fours": 0, "sixes": 0,
            "out": False, "how_out": "", "bowled_by": "",
        }
        order = state.get("batting_order", []) or []
        out_batting = (state.get("bat_stats", {}).get(out_roster_id)
                       or state.get("bat_stats", {}).get(str(out_roster_id))
                       or {})
        has_batted = bool(out_batting.get("balls") or out_batting.get("runs")
                          or out_batting.get("out"))
        if has_batted:
            # A dismissed/used batter must stay in the historical order, so the
            # incoming impact player is added as the next available batter.
            if not any(p.get("roster_id") == in_roster_id for p in order):
                order.append(incoming)
        else:
            replaced_in_order = _replace_player_in_list(order, out_roster_id, incoming)
            if replaced_in_order is None:
                order.append(incoming)
        state["batting_order"] = order

        # After a wicket the Impact substitute walks straight to the crease as
        # the next batsman — no separate "pick next batsman" step. Mirrors the
        # bookkeeping in select_wicket_batsman().
        if na == A_PICK_NEW_BATSMAN:
            in_idx = next((i for i, p in enumerate(order)
                           if p.get("roster_id") == in_roster_id), None)
            if in_idx is not None:
                _install_new_batsman(state, in_idx)
                used = max(in_idx, state.get("non_striker_idx", 1))
                state["next_batsman_idx"] = max(
                    state.get("next_batsman_idx", 2), used + 1)
                state["last_dismissed"] = None
                forced_next_action = (
                    A_PICK_NEW_BOWLER if state.pop("pending_new_bowler", False)
                    else A_PICK_DELIVERY)
    else:
        state.setdefault("bowl_stats", {})[str(in_roster_id)] = {
            "balls": 0, "runs": 0, "wickets": 0, "overs_done": 0,
            "this_over_balls": 0, "maidens": 0, "this_over_runs": 0,
        }
        if (state.get("current_bowler") or {}).get("roster_id") == out_roster_id:
            state["current_bowler"] = incoming
        elif na == A_PICK_NEW_BOWLER:
            # Between overs: the Impact bowler comes on to bowl the next over.
            state["current_bowler"] = incoming
            _emit_new_bowler(state, incoming)
            forced_next_action = A_PICK_DELIVERY

    rec.update({
        "used": True,
        "in_roster_id": in_roster_id,
        "out_roster_id": out_roster_id,
        "in_player": incoming.get("name"),
        "out_player": outgoing.get("name"),
        "team_name": state.get("bat_team_name") if user_id == state.get("bat_team_id") else state.get("bowl_team_name"),
        "used_at": _current_over_label(state),
        "innings": state.get("innings"),
        "break": _impact_break_label(state),
    })
    state["impact_players"] = impact
    state.setdefault("commentary_log", []).append({
        "type": "impact_player",
        "team": rec.get("team_name"),
        "inPlayer": incoming.get("name"),
        "outPlayer": outgoing.get("name"),
        "over": rec.get("used_at"),
        "text": f"Impact Player used! {incoming.get('name')} replaces "
                f"{outgoing.get('name')}. {_bat_career_line(incoming)}",
    })
    mwa.save_state(match_id, state, next_action=(
        forced_next_action if forced_next_action is not None
        else mwa.get_next_action(match_id)))
    return True, f"Impact Player confirmed: {incoming.get('name')} replaces {outgoing.get('name')}.", rec

def init_match_for_webapp(session, match_id, xi_overrides=None, challenge_rules=False,
                          difficulty=None, enforce_fair_stats=False):
    """Create the initial live state for a Mini-App-played match, right after
    the toss. Openers/bowler are placeholders until the teams pick them.
    Returns (ok, msg). Safe to call once; no-op if state already exists.

    xi_overrides: optional {user_id: [xi player dicts]} for synthetic teams
    (e.g. the AI bot, whose XI isn't in UserRoster). When a user_id is present
    here, that XI is used instead of querying UserRoster.

    challenge_rules: enable /cm rules on the Mini App engine: two wickets per
    innings while keeping the same setup, scoring, scorecard, and stat
    persistence paths used by /wpm.

    difficulty: when this is a vs-bot match (one side is the AI bot user), the
    bot team's difficulty ("Easy"/"Medium"/"Hard"/"Legendary"). Stored in state
    so the AI (auto_play_bot_turns / auto_play_user_turns) plays accordingly.

    enforce_fair_stats: user-vs-user /wpm passes this so a wide Team Overall gap
    between the two XIs voids career stats (anti stat-farming). Never set for
    bot matches or tournaments. The two Team Overalls are always stored in state
    (``bat_team_ovr`` / ``bowl_team_ovr``) so callers can surface a warning.
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
        cmap = _career_map(session, uid)
        return [{
            "roster_id": e.id, "player_id": p.id, "name": p.name,
            "rating": p.rating, "category": p.category,
            "bat_rating": p.bat_rating, "bowl_rating": p.bowl_rating,
            "bowl_style": p.bowl_style, "bowl_hand": p.bowl_hand,
            "bat_hand": p.bat_hand,
            **cmap.get(p.id, {}),
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
    # Keep an immutable origin separate from the active gameplay chat. Final
    # /wpm and /cm cards must always return to the lobby that created them.
    s["original_lobby_chat_id"] = m.chat_id
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
    if challenge_rules:
        s["is_challenge"] = True
        s["wicket_limit"] = 2
        s["match_label"] = "Challenge Mode"

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
            s["vsbot_difficulty"] = difficulty or "Medium"
            if bu.id == bot_user.id:
                s["batting_order"] = [bxi[0], bxi[1]] + [p for p in bxi if p["roster_id"] not in (bxi[0]["roster_id"], bxi[1]["roster_id"])]
                s["striker_idx"] = 0; s["non_striker_idx"] = 1; s["next_batsman_idx"] = 2
                s["openers_done"] = True
            if bwu.id == bot_user.id:
                # Bot opens with an eligible bowler (all-rounder/bowler), not
                # whoever happens to sit at the top of the batting order.
                s["current_bowler"] = _bowling_eligible(bwxi)[0]
                s["bowler_done"] = True
    except Exception:
        logger.exception("vsbot init detection failed (non-fatal)")

    # Fair-match stat gate (user-vs-user /wpm). Record each side's Team Overall
    # (average XI rating) so the launch card can show it, and disable career
    # stats when the gap is too wide — this stops players farming stats against
    # a deliberately weak opponent. Bot matches never qualify (enforce_fair_stats
    # is only passed by the /wpm human-vs-human launch, and vsbot is excluded).
    try:
        from services.player_stats_service import (
            team_overall, is_stat_farming_mismatch)
        s["bat_team_ovr"] = team_overall(bxi)
        s["bowl_team_ovr"] = team_overall(bwxi)
        if (enforce_fair_stats and not s.get("is_vsbot")
                and is_stat_farming_mismatch(bxi, bwxi)):
            s["stats_disabled"] = True
    except Exception:
        logger.exception("fair-stats gate failed (non-fatal) for match %s", match_id)

    next_act = "SETUP"
    if s.get("openers_done") and s.get("bowler_done"):
        s["setup"] = SETUP_DONE
        next_act = A_PICK_DELIVERY
    mwa.save_state(match_id, s, next_action=next_act)
    m.status = "playing"
    session.commit()
    return True, "Match initialized for Mini App."


def init_match_for_wsp(session, match_id, xi_overrides=None):
    """Create the initial live state for a WSP auto-simulated match.

    Like ``init_match_for_webapp`` but skips the interactive setup phase:
    openers and bowler are auto-selected from position 0/1 of each XI so
    the ball loop starts immediately. Sets ``played_via='wsp'`` so the
    action endpoints reject manual delivery/shot submissions.
    Returns (ok, msg).
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
        cmap = _career_map(session, uid)
        return [{
            "roster_id": e.id, "player_id": p.id, "name": p.name,
            "rating": p.rating, "category": p.category,
            "bat_rating": p.bat_rating, "bowl_rating": p.bowl_rating,
            "bowl_style": p.bowl_style, "bowl_hand": p.bowl_hand,
            "bat_hand": p.bat_hand,
            **cmap.get(p.id, {}),
        } for e, p in rows]

    bxi = _xi(bu.id)
    bwxi = _xi(bwu.id)
    if len(bxi) < 2 or len(bwxi) < 1:
        return False, "Both teams need at least 2 batsmen and 1 bowler."

    # Auto-select openers and first bowler from top of XI
    op1, op2 = bxi[0], bxi[1]
    bowler = bwxi[0]

    s = create_match_state(match_id, m.overs, bu.id, bwu.id, bxi, bwxi,
                           op1, op2, bowler)

    bt = bu.team_name or f"@{bu.username}'s XI"
    bwt = bwu.team_name or f"@{bwu.username}'s XI"
    s["chat_id"] = m.chat_id
    s["original_lobby_chat_id"] = m.chat_id
    s["bat_user_tg"] = bu.telegram_id
    s["bowl_user_tg"] = bwu.telegram_id
    s["bat_team_name"] = bt
    s["bowl_team_name"] = bwt
    s["bat_username"] = bu.username
    s["bowl_username"] = bwu.username

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

    # Full batting order with openers first
    s["batting_order"] = [op1, op2] + [p for p in bxi if p["roster_id"] not in (op1["roster_id"], op2["roster_id"])]
    s["striker_idx"] = 0
    s["non_striker_idx"] = 1
    s["next_batsman_idx"] = 2
    s["current_bowler"] = bowler
    s["openers_done"] = True
    s["bowler_done"] = True
    s["setup"] = SETUP_DONE
    s["played_via"] = "wsp"

    mwa.save_state(match_id, s, next_action=A_PICK_DELIVERY)
    m.status = "playing"
    session.commit()
    return True, "Match initialized for WSP auto-simulation."


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
      'xi_selection' (setup), 'innings_break', 'innings1', 'innings2',
      'completed', or the raw match status as a fallback."""
    if match_status == "completed":
        return "completed"
    setup = state.get("setup")
    if setup == SETUP_INNINGS_BREAK:
        return "innings_break"
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


def _stat_row(stats, roster_id):
    """Read a stat row before or after JSON has stringified roster-id keys."""
    stats = stats or {}
    return stats.get(roster_id) or stats.get(str(roster_id)) or {}


def _bat_card(state, idx):
    order = state.get("batting_order", [])
    if idx is None or idx < 0 or idx >= len(order):
        return None
    p = order[idx]
    st = _stat_row(state.get("bat_stats"), p["roster_id"])
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
    bs = _stat_row(state.get("bowl_stats"), b["roster_id"])
    overs_done = bs.get("overs_done", 0)
    this_over = bs.get("this_over_balls", 0)
    if _is_hundred_state(state):
        ov_str = f"{bs.get('balls', 0)}b"   # The Hundred: balls, not overs
    else:
        ov_str = f"{overs_done}.{this_over}" if this_over else f"{overs_done}"
    return {
        "roster_id": b["roster_id"], "name": b["name"],
        "rating": b.get("rating"), "bowl_rating": b.get("bowl_rating"),
        "bowl_style": b.get("bowl_style"), "bowl_hand": b.get("bowl_hand"),
        "wickets": bs.get("wickets", 0), "runs": bs.get("runs", 0),
        "overs": ov_str, "balls": bs.get("balls", 0),
    }


def build_snapshot(session, match_id, user_id, state_override=None):
    """Build the polling snapshot for a user. Returns dict or None if no match.

    ``state_override`` lets callers build a read-only snapshot for a completed
    match from its persisted ``arena_state`` after the live state is cleaned up,
    so the Mini App can reopen finished matches instead of 404-ing."""
    state = state_override if state_override is not None else mwa.get_state(match_id)
    if not state:
        return None
    next_action = mwa.get_next_action(match_id)
    ball_seq = mwa.get_ball_seq(match_id)
    role = role_for(state, user_id)
    setup = state.get("setup")

    m = session.query(Match).get(match_id)
    status = m.status if m else "unknown"
    if next_action == A_COMPLETED:
        # The Mini App polls very aggressively and can observe the state-machine
        # pointer after the winning ball before the Match row has been marked
        # completed. Treat the snapshot as completed immediately so the client
        # never falls through to the generic "waiting for opponent" UI with a
        # terminal score such as "Need 0 from 0".
        status = "completed"

    striker = _bat_card(state, state.get("striker_idx"))
    non_striker = _bat_card(state, state.get("non_striker_idx"))
    bowler = _bowler_card(state)

    # Whose turn is it? (during the ball loop)
    turn = None
    if next_action in (A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_NEW_BOWLER):
        turn = "bowler"
    elif next_action in (A_PICK_SHOT, A_PICK_NEW_BATSMAN):
        turn = "batsman"

    from services.match_engine import chase_requirements
    chase = chase_requirements(state)

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
        # "T20" (6-ball overs) or "The100" (5-ball sets) so the client can label
        # progress correctly; absent/"T20" keeps the standard over-based UI.
        "ball_format": state.get("ball_format", "T20"),
        "score": {
            "runs": state.get("total_runs", 0),
            "wickets": state.get("total_wickets", 0),
            "over": state.get("current_over", 1) - 1,
            "ball": state.get("current_ball", 0),
            "overs_str": _overs_display(state),
            "target": state.get("target"),
            "runs_required": chase.get("runs_required") if chase else None,
            "balls_remaining": chase.get("balls_remaining") if chase else None,
        },
        "bat_team_name": state.get("bat_team_name", "Batting"),
        "bowl_team_name": state.get("bowl_team_name", "Bowling"),
        "striker": striker,
        "non_striker": non_striker,
        "bowler": bowler,
        "timeline": state.get("timeline", [])[-12:],
        "selected_variation": state.get("selected_variation"),
        "current_delivery": state.get("current_delivery"),
        # Free hit armed for the upcoming legal ball (UnderCover /cric parity)
        "free_hit": bool(state.get("free_hit")),
        # Spec-aligned vocabulary (computed from the engine state)
        "phase": phase_status(state, status),
        "turn_state": turn_state_name(next_action),
        "is_my_turn": whose_turn(state, next_action, user_id)[1],
        # Squad lists (names + role) for the Squads tab
        "bat_xi": [{"name": p.get("name"), "bowl_style": p.get("bowl_style"),
                    "category": p.get("category"), "active": _is_active_player(p)}
                   for p in state.get("bat_xi", [])],
        "bowl_xi": [{"name": p.get("name"), "bowl_style": p.get("bowl_style"),
                     "category": p.get("category"), "active": _is_active_player(p)}
                    for p in state.get("bowl_xi", [])],
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
            for p in _active_players(state.get("bat_xi", []))
        ]
    if role == "bowler" and in_setup and not bowler_done:
        _bowl_opts = _active_players(state.get("bowl_xi", []))
        if state.get("is_vsbot"):
            _bowl_opts = _bowling_eligible(_bowl_opts)
        snap["bowler_options"] = [
            {"roster_id": p["roster_id"], "name": p["name"],
             "bowl_rating": p.get("bowl_rating"), "rating": p.get("rating"),
             "bowl_style": p.get("bowl_style"), "category": p.get("category")}
            for p in _bowl_opts
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
        "free_hit": bool(state.get("free_hit")),
        "last_ball": state.get("last_ball"),
        "commentary": state.get("last_commentary"),
        # Sync
        "ball_seq": base.get("ball_seq"),
    }


def get_state_is_vsbot(match_id):
    """Quick check: is this a vs-bot match?"""
    st = mwa.get_state(match_id)
    return bool(st and st.get("is_vsbot"))


def _in_setup(state):
    return state.get("setup") in (SETUP_PICKING, SETUP_AWAIT_OPENERS,
                                  SETUP_AWAIT_BOWLER, SETUP_AWAIT_READY)


def _start_match_if_both_done(state):
    """If both openers and bowler are chosen, flip to the ball loop. Returns
    (started, next_action_or_None) for the caller to fold into its CAS write."""
    if state.get("openers_done") and state.get("bowler_done"):
        state["setup"] = SETUP_DONE
        return True, A_PICK_DELIVERY
    return False, None


def _resume_after_innings_break(state):
    """Leave the innings-break screen (target + 1st-innings scorecard) and
    (re)enter 2nd-innings setup. Mutates state in place; returns the
    next_action to persist alongside it."""
    state.pop("innings_break_started_at", None)
    if state.get("is_vsbot"):
        # vsbot: keep it flowing — bowling side picks a bowler (auto for bot).
        state["setup"] = SETUP_DONE
        return A_PICK_NEW_BOWLER
    # PvP: re-enter player selection so BOTH sides pick again
    # (new batting side → openers, new bowling side → bowler).
    state["setup"] = SETUP_PICKING
    state["openers_done"] = False
    state["bowler_done"] = False
    state["current_bowler"] = None
    return "SETUP"


def advance_innings_break_if_due(match_id):
    """If the match is sitting on the innings-break screen and the display
    window has elapsed, atomically resume into 2nd-innings setup.

    Driven from the poll endpoint so both clients transition together off the
    same server clock — no per-user "I'm ready" handshake (which would just
    reintroduce a "waiting for opponent" style stall).
    """
    # Cheap pre-gate: this runs on every poll, so skip the CAS read-modify-
    # write (a guaranteed DB round-trip) unless the cached state says the
    # match is actually sitting on an elapsed innings break. The CAS mutator
    # below re-validates against fresh DB state, so a stale gate can only
    # delay the transition by a cache-TTL tick, never corrupt it.
    st = mwa.get_state(match_id)
    if not st or st.get("setup") != SETUP_INNINGS_BREAK:
        return False
    if (_time.time() - (st.get("innings_break_started_at") or 0)) < INNINGS_BREAK_SECONDS:
        return False

    def _mutate(state):
        if state.get("setup") != SETUP_INNINGS_BREAK:
            raise CasAbort(False)
        started_at = state.get("innings_break_started_at") or 0
        if (_time.time() - started_at) < INNINGS_BREAK_SECONDS:
            raise CasAbort(False)
        next_action = _resume_after_innings_break(state)
        return True, next_action

    return bool(mwa.update_state_cas(match_id, _mutate))


def continue_past_innings_break(match_id, user_id):
    """Let an engaged player skip the innings-break countdown early. Either
    side may call this — it's just a UI gate, not a competitive action — and
    it's idempotent (a no-op once the match has already moved on)."""
    def _mutate(state):
        if is_view_only_match(state):
            raise CasAbort((False, VIEW_ONLY_MESSAGE))
        if user_id not in (state.get("bat_team_id"), state.get("bowl_team_id")):
            raise CasAbort((False, "Spectators can't skip the innings break."))
        if state.get("setup") != SETUP_INNINGS_BREAK:
            raise CasAbort((True, "Already moving on."))
        next_action = _resume_after_innings_break(state)
        return (True, "Heading into 2nd-innings team selection…"), next_action

    result = mwa.update_state_cas(match_id, _mutate)
    if result is None:
        return False, "Match not found."
    return result


def select_openers(match_id, user_id, striker_rid, non_striker_rid):
    """Batsman picks openers. Independent of bowler pick; auto-starts when both
    are in. Returns (ok, started, msg).

    Runs as an atomic read-modify-write (services.match_state_store.update_state_cas)
    so a concurrent bowler pick from the other side can't be silently overwritten —
    both flags are validated and set against the SAME freshly-read state, on every
    retry attempt, instead of racing two independent whole-state overwrites.
    """
    def _mutate(state):
        if is_view_only_match(state):
            raise CasAbort((False, False, VIEW_ONLY_MESSAGE))
        if user_id != state.get("bat_team_id"):
            raise CasAbort((False, False, "Only the batting side picks openers."))
        if not _in_setup(state) or state.get("openers_done"):
            raise CasAbort((False, False, "Openers already chosen."))
        if striker_rid == non_striker_rid:
            raise CasAbort((False, False, "Striker and non-striker must be different players."))

        bat_xi = _active_players(state.get("bat_xi", []))
        by_rid = {p["roster_id"]: p for p in bat_xi}
        if striker_rid not in by_rid or non_striker_rid not in by_rid:
            raise CasAbort((False, False, "Pick players from your XI."))

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

        started, next_action = _start_match_if_both_done(state)
        msg = ("Openers locked in — match starting!" if started
               else "Openers locked in. Waiting for the bowler…")
        return (True, started, msg), next_action

    result = mwa.update_state_cas(match_id, _mutate)
    if result is None:
        return False, False, "Match not found."
    return result


def select_bowler(match_id, user_id, bowler_rid):
    """Bowling side picks bowler. Independent of openers pick; auto-starts when
    both are in. Returns (ok, started, msg).

    See select_openers — uses the same atomic CAS write to avoid clobbering a
    concurrent openers pick from the batting side.
    """
    def _mutate(state):
        if is_view_only_match(state):
            raise CasAbort((False, False, VIEW_ONLY_MESSAGE))
        if user_id != state.get("bowl_team_id"):
            raise CasAbort((False, False, "Only the bowling side picks the bowler."))
        if not _in_setup(state) or state.get("bowler_done"):
            raise CasAbort((False, False, "Bowler already chosen."))

        bowl_xi = _active_players(state.get("bowl_xi", []))
        by_rid = {p["roster_id"]: p for p in bowl_xi}
        if bowler_rid not in by_rid:
            raise CasAbort((False, False, "Pick a bowler from your XI."))
        if state.get("is_vsbot") and not _can_bowl(by_rid[bowler_rid]):
            raise CasAbort((False, False,
                            "Only all-rounders and bowlers can bowl."))

        state["current_bowler"] = by_rid[bowler_rid]
        state["bowler_done"] = True

        started, next_action = _start_match_if_both_done(state)
        msg = ("Bowler selected — match starting!" if started
               else "Bowler selected. Waiting for the openers…")
        return (True, started, msg), next_action

    result = mwa.update_state_cas(match_id, _mutate)
    if result is None:
        return False, False, "Match not found."
    return result


def mark_ready(match_id, user_id):
    """Deprecated in the simultaneous model; reports/forces start state.

    select_openers/select_bowler now flip setup → SETUP_DONE atomically as soon
    as both flags land, so this is normally a no-op report — but it still uses
    the CAS write for the rare case where it needs to force the flip, to avoid
    clobbering concurrent state changes."""
    def _mutate(state):
        both = bool(state.get("openers_done") and state.get("bowler_done"))
        if both and state.get("setup") != SETUP_DONE:
            state["setup"] = SETUP_DONE
            return (True, both, "Match starting!"), A_PICK_DELIVERY
        return (True, both, "Match starting!" if both else "Waiting for both picks…"), None

    result = mwa.update_state_cas(match_id, _mutate)
    if result is None:
        return False, False, "Match not found."
    return result


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
        # Frontend XI indices are positions in the serialized XI, which includes
        # inactive Impact-replaced placeholders so scorecard/stat identity remains
        # stable. Do not compact the XI before indexing; select_openers below
        # still validates that the resolved roster ids are active selections.
        bat_xi = state.get("bat_xi", []) or []
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
        # Same index-space contract as openers: indices address the serialized
        # bowling XI, not the compacted active-only list. select_bowler rejects
        # inactive Impact-replaced roster ids after translation.
        bowl_xi = state.get("bowl_xi", []) or []
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


def _ci_lookup(options, value):
    """Case-insensitive option lookup that returns the canonical option text."""
    raw = str(value or "").strip()
    for opt in options or []:
        if str(opt).strip().lower() == raw.lower():
            return opt
    return None


def _normalise_delivery_choice(state, delivery, length=None):
    """Validate and canonicalize a Mini-App delivery using /playmatch options.

    The Mini App sends one button label such as ``Leg Cutter Good Length`` while
    the older bot flow sends ``variation`` and ``length`` separately.  This
    helper accepts either shape, resolves the canonical variation/length from
    ``services.bowling_service.get_delivery_options`` (the same vocabulary used
    by /playmatch and /vsbot), and returns a full delivery string that the shared
    probability engine can parse correctly.
    """
    bowler = state.get("current_bowler") or {}
    opts = _get_delivery_options(bowler.get("bowl_style", "Medium Pacer"),
                                 bowler.get("bowl_hand", "Right"))
    raw = str(delivery or "").strip()
    if not raw:
        return False, "Pick a delivery.", None

    if opts.get("is_spinner"):
        spin_delivery = _ci_lookup(opts.get("deliveries", []), raw)
        if not spin_delivery:
            allowed = ", ".join(opts.get("deliveries", []))
            return False, f"Pick a valid spin delivery: {allowed}", None
        return True, "", {
            "delivery": spin_delivery,
            "variation": spin_delivery,
            "length": None,
            "is_spinner": True,
            "options": opts,
        }

    variations = opts.get("variations", [])
    lengths = opts.get("lengths", [])
    if length:
        variation = _ci_lookup(variations, raw)
        canonical_length = _ci_lookup(lengths, length)
        if variation and canonical_length:
            return True, "", {
                "delivery": f"{variation} {canonical_length}",
                "variation": variation,
                "length": canonical_length,
                "is_spinner": False,
                "options": opts,
            }

    # One-button Mini App shape: split by the longest valid length suffix first.
    for candidate_length in sorted(lengths, key=len, reverse=True):
        if raw.lower().endswith(candidate_length.lower()):
            prefix = raw[:len(raw) - len(candidate_length)].strip()
            variation = _ci_lookup(variations, prefix)
            if variation:
                return True, "", {
                    "delivery": f"{variation} {candidate_length}",
                    "variation": variation,
                    "length": candidate_length,
                    "is_spinner": False,
                    "options": opts,
                }

    # A variation-only pacer delivery is allowed for backward compatibility; use
    # Good/Good Length when available, matching the old /playmatch fallback.
    variation = _ci_lookup(variations, raw)
    if variation:
        default_length = (_ci_lookup(lengths, "Good")
                          or _ci_lookup(lengths, "Good Length")
                          or (lengths[0] if lengths else "Good"))
        return True, "", {
            "delivery": f"{variation} {default_length}".strip(),
            "variation": variation,
            "length": default_length,
            "is_spinner": False,
            "options": opts,
        }

    combos = [f"{v} {l}" for v in variations for l in lengths] or variations
    return False, "Pick a valid delivery: " + ", ".join(combos[:12]), None


def set_delivery(match_id, user_id, variation, length=None):
    """Bowler locks in the delivery (variation + optional length). The batsman
    then sees 'delivery coming' and picks a shot. Returns (ok, msg)."""
    state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found."
    if is_view_only_match(state):
        return False, VIEW_ONLY_MESSAGE
    if user_id != state.get("bowl_team_id"):
        return False, "Only the bowling side delivers."
    na = mwa.get_next_action(match_id)
    if na not in (A_PICK_DELIVERY, A_PICK_LENGTH, "SETUP"):
        return False, "Not your turn to bowl right now."

    bowler = state.get("current_bowler") or {}
    spinner = _is_spinner(bowler.get("bowl_style", ""))
    if not spinner and not length:
        opts = _get_delivery_options(bowler.get("bowl_style", "Medium Pacer"),
                                     bowler.get("bowl_hand", "Right"))
        variation_only = _ci_lookup(opts.get("variations", []), variation)
        if variation_only:
            # Older two-step flow: variation first, length second. Keep this
            # behaviour for Telegram /playmatch-style callers.
            state["selected_variation"] = variation_only
            mwa.save_state(match_id, state, next_action=A_PICK_LENGTH)
            return True, "Variation set — now pick a length."
        # Mini-App/REST callers may send the full pacer combo as `variation`.
        ok_norm, msg_norm, info = _normalise_delivery_choice(state, variation)
        if not ok_norm:
            return False, msg_norm
        delivery = info["delivery"]
        variation = info["variation"]
        length = info.get("length")
    else:
        ok_norm, msg_norm, info = _normalise_delivery_choice(state, variation, length=length)
        if not ok_norm:
            return False, msg_norm
        delivery = info["delivery"]
        variation = info["variation"]
        length = info.get("length")

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
_ACTION_LOCK_SECONDS = 1.5  # short guard: prevents double-taps without slowing play


def _is_processing(state):
    """True if a prior action is still within the processing window."""
    ts = state.get("action_processing_at")
    if not ts:
        return False
    return (_time.time() - ts) < _ACTION_LOCK_SECONDS


def _set_processing(state, on=True):
    state["action_processing_at"] = _time.time() if on else None


# In-process per-match action guard. Replaces the persisted processing-flag
# round-trips (save flag → act → save cleared flag) on the Mini App hot path:
# bot + Flask share one process, so a lock-guarded dict gives the same
# double-tap protection without two extra DB commits per ball. The TTL
# self-heals a claim abandoned by a crashed request thread.
import threading as _threading
_ACTION_GUARD_TTL = _ACTION_LOCK_SECONDS
_ACTION_GUARD_LOCK = _threading.Lock()
_ACTION_GUARD = {}  # match_id -> claimed_at (time.monotonic)


def _claim_action(match_id):
    """Atomically claim the per-match action slot. False if a live claim
    (younger than the TTL) already exists."""
    now = _time.monotonic()
    with _ACTION_GUARD_LOCK:
        claimed_at = _ACTION_GUARD.get(match_id)
        if claimed_at is not None and (now - claimed_at) < _ACTION_GUARD_TTL:
            return False
        _ACTION_GUARD[match_id] = now
        return True


def _release_action(match_id):
    with _ACTION_GUARD_LOCK:
        _ACTION_GUARD.pop(match_id, None)


def action_in_progress(match_id):
    """Read-only view of the guard (surfaced as isProcessing to clients)."""
    with _ACTION_GUARD_LOCK:
        claimed_at = _ACTION_GUARD.get(match_id)
    return claimed_at is not None and (_time.monotonic() - claimed_at) < _ACTION_GUARD_TTL


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
    if is_view_only_match(state):
        return False, VIEW_ONLY_MESSAGE, None
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

    ok_norm, msg_norm, info = _normalise_delivery_choice(state, delivery)
    if not ok_norm:
        return False, msg_norm, None

    if not _claim_action(match_id):
        return False, "Previous action still processing — hold on.", None
    try:
        _set_processing(state, True)
        bowler = state.get("current_bowler") or {}
        kmh = _speed_to_kmh(speed, bowler)

        state["current_delivery"] = info["delivery"]
        state["current_speed"] = (speed or "medium")
        state["last_speed"] = kmh          # km/h, surfaced to clients
        state["selected_variation"] = info["variation"]
        state["selected_length"] = info.get("length")
        _set_processing(state, False)
        mwa.save_state(match_id, state, next_action=A_PICK_SHOT)
    finally:
        _release_action(match_id)
    return True, "Delivery on its way — batsman to play.", {
        "delivery": info["delivery"],
        "variation": info["variation"],
        "length": info.get("length"),
        "speed": speed or "medium",
        "kmh": kmh}


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
    if is_view_only_match(state):
        return False, VIEW_ONLY_MESSAGE, None
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
    # In-process guard instead of persisting a processing flag: skips two DB
    # commits per ball while keeping the same 1.5s double-tap window. The
    # mutated local state (current_shot/manual_batsman) rides into play_shot
    # directly and is persisted by its single post-ball save.
    if not _claim_action(match_id):
        return False, "Previous action still processing — hold on.", None
    try:
        ok, res = play_shot(match_id, user_id, idx, state=state)
    finally:
        _release_action(match_id)
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
    if is_view_only_match(state):
        return False, VIEW_ONLY_MESSAGE, None
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

    # Install the incoming batsman into the dismissed batsman's slot. After a
    # last-ball wicket the over-end swap leaves the out batsman at non_striker,
    # so don't assume the striker slot.
    _install_new_batsman(state, idx)
    # Keep next_batsman_idx ahead of the highest used position.
    used = max(idx, state.get("non_striker_idx", 1))
    state["next_batsman_idx"] = max(state.get("next_batsman_idx", 2), used + 1)
    state["last_dismissed"] = None
    # If the wicket fell on the last ball of the over, the new bowler still
    # needs picking before the next delivery; otherwise resume the over.
    next_action = (A_PICK_NEW_BOWLER if state.pop("pending_new_bowler", False)
                   else A_PICK_DELIVERY)
    mwa.save_state(match_id, state, next_action=next_action)
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
        # Record partnership before resetting
        if "partnership_history" not in state:
            state["partnership_history"] = []
        ns_idx = state.get("non_striker_idx")
        ns_player = None
        order = state.get("batting_order", [])
        if ns_idx is not None and 0 <= ns_idx < len(order):
            ns_player = order[ns_idx]
        state["partnership_history"].append({
            "runs": state.get("partnership_runs", 0),
            "balls": state.get("partnership_balls", 0),
            "batsman1": striker.get("name", ""),
            "batsman2": ns_player.get("name", "") if ns_player else "",
            "wicket": state["total_wickets"],
        })
        state["partnership_runs"] = 0; state["partnership_balls"] = 0
        # Capture the dismissed batsman's name now, before the end-of-over swap
        # below can move them off the striker slot.
        state["last_dismissed"] = striker.get("name")
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
        over_runs_scored = bws.get("this_over_runs", 0)
        if over_runs_scored == 0:
            bws["maidens"] = bws.get("maidens", 0) + 1
        # Preserve the just-bowled over's runs for the end-of-over card.
        bws["last_over_runs"] = over_runs_scored
        bws["this_over_runs"] = 0
        # Track over-by-over runs for Manhattan chart
        if "over_runs" not in state:
            state["over_runs"] = []
        state["over_runs"].append(over_runs_scored)
        state["current_over"] += 1
        state["current_ball"] = 0
        state["striker_idx"], state["non_striker_idx"] = state["non_striker_idx"], state["striker_idx"]
        state["prev_bowler_rid"] = bowler["roster_id"]
        eoo = True

    # ── Live-match mechanics bookkeeping (UnderCover /cric parity) ──────────
    # Batting momentum window — off-the-bat runs over the last ~12 balls.
    runs_off_bat = oc.get("runs", 0) if t in ("runs", "wicket", "noball") else 0
    win = state.get("recent_runs_window") or []
    win.append(int(runs_off_bat))
    state["recent_runs_window"] = win[-12:]

    # Bowling momentum — wickets in a row (reset by any legal non-wicket ball).
    if t == "wicket":
        state["consec_wickets"] = int(state.get("consec_wickets", 0) or 0) + 1
    elif legal:
        state["consec_wickets"] = 0

    # Free hit — a no-ball arms it; the next legal ball consumes it. A wide
    # leaves it standing so the free hit survives until a legal ball is bowled.
    if t == "noball":
        state["free_hit"] = True
    elif legal:
        state["free_hit"] = False

    # Delivery-spam history — tracked within the current over only.
    if eoo:
        state["delivery_history"] = []
    else:
        hist = state.get("delivery_history") or []
        hist.append(delivery)
        state["delivery_history"] = hist

    # Mystery is a single-ball event; clear it now that the ball is bowled.
    state["mystery_active"] = False

    state["current_delivery"] = None
    state["selected_variation"] = None

    return {"rtxt": rtxt, "type": t, "runs": oc.get("runs", 0),
            "legal": legal, "need_new_bat": need_new_bat, "eoo": eoo,
            "how": oc.get("how"), "free_hit": oc.get("free_hit", False),
            "mystery": oc.get("mystery", False),
            "traits": oc.get("traits_activated") or []}


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
    result_type = res.get("type")
    is_wkt = result_type == "wicket"
    event_key = {"wicket": "wicket", "wide": "wide", "noball": "no_ball"}.get(result_type)
    if not event_key and result_type not in ("legbye",) and runs == 0:
        event_key = "dot_ball"
    elif not event_key and runs == 4:
        event_key = "four"
    elif not event_key and runs == 6:
        event_key = "six"
    if res.get("traits"):
        event_key = "implant"
    if striker:
        rid = striker.get("roster_id")
        stats = (state.get("bat_stats", {}) or {}).get(str(rid)) or (state.get("bat_stats", {}) or {}).get(rid) or {}
        current_runs = int(stats.get("runs") or 0)
        previous_runs = max(0, current_runs - int(runs or 0))
        if previous_runs < 100 <= current_runs:
            event_key = "century"
        elif previous_runs < 50 <= current_runs:
            event_key = "fifty"

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
        "eventKey": event_key,
        "text": text or res.get("rtxt") or "",
        "batsmanName": (striker.get("name") if striker else ""),
        "bowlerName": (bowler.get("name") if bowler else ""),
    })

    # Red OUT card — names the dismissed batsman, his score, and dismissal type.
    if is_wkt and striker:
        bs = _stat_row(state.get("bat_stats"), striker.get("roster_id"))
        how = res.get("how") or "out"
        bwl = bowler.get("name") if bowler else ""
        log.append({
            "type": "wicket",
            "text": f"OUT! {striker.get('name')} {bs.get('runs', 0)}({bs.get('balls', 0)}) "
                    f"{how} b {bwl}".strip(),
        })

    def _bat_card(player):
        if not player:
            return None
        # State is JSON-round-tripped, so bat_stats keys may be strings. Use the
        # string-tolerant reader so the end-of-over card shows real figures
        # instead of zeros.
        bs = _stat_row(state.get("bat_stats"), player.get("roster_id"))
        return {"name": player.get("name"),
                "runs": bs.get("runs", 0), "balls": bs.get("balls", 0)}

    # End-of-over summary card.
    if res.get("eoo"):
        bws = _stat_row(state.get("bowl_stats"), bowler.get("roster_id")) if bowler else {}
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
                "overs": b_overs_done,
                "balls": b_this,
            },
        })
        # Gray one-liner with the bowler's match figures.
        if bowler:
            log.append({
                "type": "over_complete",
                "name": bowler.get("name"),
                "text": f"{bowler.get('name')} completes the over. {_bowl_match_fig(state, bowler)}",
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
            "overs": overs_done,
            "balls": balls,
            "target": state.get("target"),
            "winner": None,   # filled on the result screen via result.motm
            "motm": None,
        })

    # Cap the log so state stays small.
    if len(log) > 60:
        log = log[-60:]
    state["commentary_log"] = log


def play_shot(match_id, user_id, shot_index, state=None):
    """Batsman plays a shot. Resolves the ball through the engine, mutates
    state, advances the loop. Returns (ok, result_dict|msg).

    `state` lets set_shot_action pass its already-validated, already-mutated
    copy so the ball resolves and persists in a single save."""
    if state is None:
        state = mwa.get_state(match_id)
    if not state:
        return False, "Match not found."
    if is_view_only_match(state):
        return False, VIEW_ONLY_MESSAGE
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

    from services.match_state_store import get_match_lock
    import handlers.match as _bm  # for the shared _calc outcome engine

    shot = AVAILABLE_SHOTS[shot_index]
    striker = get_striker(state)
    bowler = get_bowler(state)

    # Mystery ball — ~25% chance per over (≈4.7% per ball). Rolled here so it
    # only affects the live Mini App flow; _calc reads it off the state.
    import random as _rnd
    state["mystery_active"] = (not state.get("free_hit")) and (_rnd.random() < 0.047)

    # Reuse the bot's improved probability engine for identical /wpm, /cm,
    # /vsbot, and /playmatch simulations (pitch wear, form, traits, delivery
    # length/variation, and shot choice all feed the same calculator).
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
        "free_hit": bool(res.get("free_hit")),
        "mystery": bool(res.get("mystery")),
    }
    state["last_commentary"] = commentary or res.get("rtxt")

    traits = res.get("traits") or oc.get("traits_activated") or []
    if traits:
        state["last_ball"]["eventKey"] = "implant"

    # Accumulate a scrolling commentary log (ball rows + end-of-over /
    # end-of-innings summary cards) so the Mini App feed matches UnderCover.
    _append_commentary_log(state, res, striker, bowler,
                           commentary or res.get("rtxt"))

    # Next action
    if is_innings_over(state):
        from services.match_engine import (transition_to_second_innings,
                                           compute_match_result)
        if state.get("innings", 1) == 1:
            # End of 1st innings → set up the chase, then show a brief
            # "innings break" screen (target + 1st-innings scorecard) before
            # either side has to pick its 2nd-innings XI. The poll endpoint
            # auto-advances out of this via advance_innings_break_if_due()
            # once INNINGS_BREAK_SECONDS has elapsed (see _resume_after_innings_break
            # for what happens next, vsbot vs PvP).
            transition_to_second_innings(state)
            res["innings_break"] = True
            state["setup"] = SETUP_INNINGS_BREAK
            state["innings_break_started_at"] = _time.time()
            next_act = A_INNINGS_BREAK
        else:
            # End of 2nd innings → match over
            result = compute_match_result(state)
            state["match_result"] = result
            next_act = A_COMPLETED
            res["match_over"] = True
            res["result"] = result
    elif res["need_new_bat"] and state["total_wickets"] < state.get("wicket_limit", 10):
        # last_dismissed was captured in _apply_outcome (before the over-end swap).
        # If the wicket fell on the last ball of the over, a new bowler is still
        # owed once the incoming batsman has been chosen — remember that here so
        # the (possibly manual) batsman pick routes to the bowler picker, not
        # straight back to the same bowler.
        state["pending_new_bowler"] = bool(res["eoo"])
        next_act = A_PICK_NEW_BATSMAN
        if state.get("manual_batsman"):
            # Manual mode: the batting player picks the next batsman.
            # Stay in A_PICK_NEW_BATSMAN (do NOT auto-advance).
            pass
        else:
            nb = state.get("next_batsman_idx", 2)
            if nb < len(state.get("batting_order", [])):
                _install_new_batsman(state, nb)
                state["next_batsman_idx"] = nb + 1
                next_act = A_PICK_NEW_BOWLER if res["eoo"] else A_PICK_DELIVERY
                state.pop("pending_new_bowler", None)
    elif res["eoo"]:
        next_act = A_PICK_NEW_BOWLER
    else:
        next_act = A_PICK_DELIVERY

    mwa.save_state(match_id, state, next_action=next_act, bump_ball_seq=True)

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
    if is_view_only_match(state):
        return False, VIEW_ONLY_MESSAGE
    if user_id != state.get("bowl_team_id"):
        return False, "Only the bowling side picks the bowler."
    na = mwa.get_next_action(match_id)
    if na != A_PICK_NEW_BOWLER:
        return False, "Not time to change bowler."
    by_rid = {p["roster_id"]: p for p in _active_players(state.get("bowl_xi", []))}
    if bowler_rid not in by_rid:
        return False, "Pick a bowler from your XI."
    prev = state.get("prev_bowler_rid")
    # In bot matches only the front-line attack (all-rounders + bowlers) may
    # bowl — until it is exhausted, at which point batsmen become eligible so
    # the innings can be completed (see _bot_bowler_pool). A batsman picked
    # while a front-liner still has legal overs left is rejected.
    if state.get("is_vsbot"):
        pool = _bot_bowler_pool(state, list(by_rid.values()), prev)
        pool_ids = {p["roster_id"] for p in pool}
        if bowler_rid not in pool_ids:
            return False, ("Only all-rounders and bowlers can bowl while your "
                           "front-line attack still has overs left.")
    if bowler_rid == prev:
        return False, "Same bowler can't bowl consecutive overs."
    # Per-bowler over limit (ceil(overs / 5)). Keep the cap in force while any
    # quota-safe bowler remains; relax it only if nobody is left so the picker
    # can never dead-end. In bot matches the quota universe is the current pool
    # (front-line only, or the full XI once batsmen have been unlocked) —
    # otherwise idle, still-ineligible players (0 overs) keep `under_quota`
    # non-empty and every legal over-quota pick is wrongly rejected once the
    # real bowlers are all capped, dead-ending the innings.
    quota = _bowling_quota(state.get("overs"))
    quota_pool = by_rid
    if state.get("is_vsbot"):
        quota_pool = {rid: p for rid, p in by_rid.items() if rid in pool_ids}
    under_quota = [
        rid for rid, p in quota_pool.items()
        if rid != prev
        and _stat_row(state.get("bowl_stats"), rid).get("overs_done", 0) < quota
    ]
    if under_quota and bowler_rid not in under_quota:
        return False, f"That bowler has bowled their {quota}-over quota."
    state["current_bowler"] = by_rid[bowler_rid]
    _emit_new_bowler(state, by_rid[bowler_rid])
    mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
    return True, "New bowler set."


def get_new_bowler_options(match_id, user_id):
    """Bowlers eligible for the next over (excludes the one who just bowled and
    anyone who has reached the per-bowler over quota)."""
    state = mwa.get_state(match_id)
    if not state:
        return {"ok": False, "message": "Match not found."}
    prev = state.get("prev_bowler_rid")
    quota = _bowling_quota(state.get("overs"))
    players = _active_players(state.get("bowl_xi", []))
    if state.get("is_vsbot"):
        # Bot matches: front-line attack (all-rounders + bowlers) only, but
        # once every front-liner is spent (quota used / just bowled), batsmen
        # join the pool so a part-timer can fill the remaining overs.
        players = _bot_bowler_pool(state, players, prev)
    # Only enforce the quota if at least one quota-safe bowler is available;
    # otherwise relax it (still excluding the previous bowler) to avoid dead-ends.
    enforce_quota = any(
        p["roster_id"] != prev
        and _stat_row(state.get("bowl_stats"), p["roster_id"]).get("overs_done", 0) < quota
        for p in players
    )
    opts = []
    for p in players:
        rid = p["roster_id"]
        overs_done = _stat_row(state.get("bowl_stats"), rid).get("overs_done", 0)
        disabled = (rid == prev) or (enforce_quota and overs_done >= quota)
        opts.append({
            "roster_id": rid, "name": p["name"],
            "bowl_rating": p.get("bowl_rating"), "bowl_style": p.get("bowl_style"),
            "rating": p.get("rating"), "disabled": disabled,
        })
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
            st = _stat_row(stats, p["roster_id"])
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
            st = _stat_row(stats, p["roster_id"])
            if not st.get("balls"):
                continue
            if _is_hundred_state(state):
                overs = f"{st.get('balls', 0)}b"   # The Hundred: balls, not overs
            else:
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
        inn1_overs = _overs_display(state)
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
            "overs": _overs_display(state),
            "batting": _batting(state.get("bat_xi", []), state.get("bat_stats", {})),
            "bowling": _bowling(state.get("bowl_xi", []), state.get("bowl_stats", {})),
        })

    return {"ok": True, "innings": innings, "current_innings": cur_inn,
            "target": state.get("target"),
            "ball_format": state.get("ball_format", "T20"),
            "impact_players": _impact_player_summary(state)}


# ══════════════════ Persisted scorecards (completed matches) ═════════

def save_final_scorecard(session, match_id, result_text=None, extra_innings=None,
                         super_over=None, potm=None):
    """Snapshot the final scorecard from live state into MatchScorecard so it
    can be viewed read-only after the match. Idempotent. Call at completion,
    BEFORE the live state is cleaned up.

    ``extra_innings`` (optional) is appended after the main innings — used to
    carry Super Over innings so the Mini App shows them like the main match.
    ``super_over`` (optional) is a compact summary (winner + per-innings totals)
    surfaced on the Mini App result screen. ``potm`` (optional) is
    ``(name, stats, team)`` for the archived text scorecard's byline.
    """
    import json as _json
    from models import MatchScorecard

    existing = (session.query(MatchScorecard)
                .filter(MatchScorecard.match_id == match_id).first())
    if existing:
        return True  # already saved

    sc = build_scorecard(match_id, None)  # user_id not needed for full card
    if not sc.get("ok"):
        # Live state already gone → no scorecard can be snapshotted. The recap
        # path has a live-state fallback, but log so this is visible if it recurs.
        logger.warning("save_final_scorecard: build_scorecard not ok for match %s "
                       "(state missing?) — no row persisted", match_id)
        return False
    all_innings = list(sc["innings"]) + list(extra_innings or [])
    row = MatchScorecard(
        match_id=match_id,
        scorecard_json=_json.dumps({"innings": all_innings,
                                    "current_innings": sc.get("current_innings"),
                                    "target": sc.get("target"),
                                    "impact_players": sc.get("impact_players", []),
                                    "super_over": super_over,
                                    # Keep the completed Arena board queryable
                                    # after live match_state cleanup. This lets
                                    # /wpm Play Match reopen as a read-only
                                    # result screen instead of a dead room.
                                    "arena_state": mwa.get_state(match_id)}),
        result_text=(result_text or "")[:300] or None,
    )
    session.add(row)

    # Archive a human-readable text scorecard (MatchNo<id>.txt) to the Telegram
    # storage channel — once, on first save — for every match that flows through
    # this seam (Mini-App /cm, /cipl, Super Over), complete or abandoned.
    _archive_text_scorecard(match_id, all_innings, result_text, super_over,
                            potm=potm)
    return True


BOT_CREDIT = "Bot"


def _team_credits(state):
    """``{team name: "@username"}`` for the two captains in a live match state.

    The archived scorecard used to name only the teams, which is fine for a
    league fixture and useless for working out *who* played it months later —
    two different players fielding RCB produce identical files. The live state
    still holds both captains when the archive is written, so the credit is
    recoverable exactly once: here.

    A bot side is credited as "Bot" rather than given an @handle, since the
    shared bot user is not a person anyone can look up.
    """
    if not isinstance(state, dict):
        return {}
    names = state.get("user_names") or {}
    bot_uid = state.get("bot_user_id") if state.get("is_bot_match") else None

    team_credits = {}
    for side in ("bat", "bowl"):
        team = state.get(f"{side}_team_name")
        if not team:
            continue
        if bot_uid is not None and state.get(f"{side}_team_id") == bot_uid:
            team_credits[team] = BOT_CREDIT
            continue
        raw = names.get(str(state.get(f"{side}_user_tg")))
        if not raw:
            continue
        raw = str(raw).strip()
        # user_names holds "username or first_name". A handle has no spaces, so
        # only prefix "@" when the value can actually be one — inventing
        # "@Ranjan Himanshu" would be worse than plain text.
        team_credits[team] = raw if (" " in raw or raw.startswith("@")) else f"@{raw}"

    # No fallback beyond this point on purpose. An earlier version filled a
    # missing innings-1 credit from the only other one it had, which in a
    # two-team match is by definition the *opponent's* captain — a wrong byline
    # is worse than no byline in a permanent record. Both sides are already
    # covered by bat_/bowl_ whichever way the innings has swapped.
    return team_credits


def _credited(team, team_credits):
    """``"RCB"`` → ``"RCB (@alice)"`` when we know who was captaining it."""
    tag = (team_credits or {}).get(team)
    return f"{team} ({tag})" if tag else team


def _potm_line(potm, team_credits=None):
    """``"Player of the Match: Kohli (74(48)) — RCB (@alice)"``, or ``None``."""
    if not potm:
        return None
    name, stats, team = (list(potm) + [None, None, None])[:3]
    if not name:
        return None
    line = f"Player of the Match: {name}"
    if stats:
        line += f" ({stats})"
    if team:
        line += f" — {_credited(team, team_credits)}"
    return line


def _build_text_scorecard(match_id, innings, result_text=None, super_over=None,
                          state=None, potm=None):
    """Render the persisted innings list into a plain-text scorecard string
    (same spirit as the main-engine MatchNo<id>.txt archive).

    ``state`` is the live match state, still present when the archive is
    written. It carries the things the persisted innings list does not — who
    captained each side, the pitch, the ground — and every one of them is worth
    more in an archive than on screen, because the archive is what anyone reads
    after the match is gone. ``potm`` is ``(name, stats, team)`` for the Player
    of the Match, which the on-screen card has always shown and this file never
    did.
    """
    team_credits = _team_credits(state)
    teams = [inn.get("bat_team") or f"Innings {inn.get('number', '?')}" for inn in innings]
    title = " vs ".join(_credited(t, team_credits)
                        for t in dict.fromkeys(t for t in teams if t)) or "Match"
    out = [f"Match Summary: {title}", f"Match Number: #{match_id}"]
    for label, key in (("Pitch", "pitch_type"), ("Stadium", "stadium")):
        value = (state or {}).get(key)
        if value:
            out.append(f"{label}: {value}")
    if result_text:
        out.append(f"Result: {result_text}")
    potm_line = _potm_line(potm, team_credits)
    if potm_line:
        out.append(potm_line)
    out.append("")

    for inn in innings:
        bat_team = inn.get("bat_team") or f"Innings {inn.get('number', '?')}"
        # Only the team name is shouted — an upper-cased "@ALICE" reads like a
        # different handle from the one it credits.
        tag = team_credits.get(bat_team)
        out.append(f"{bat_team.upper()} INNINGS" + (f"  —  {tag}" if tag else ""))
        bsep = "-" * 95
        out.append(bsep)
        out.append(f"{'Batsman':<22}{'Status':<38}{'R':>3} {'B':>4} {'4s':>4} {'6s':>4} {'SR':>7}")
        out.append(bsep)
        for r in inn.get("batting", []):
            status = r.get("how_out") or ("out" if r.get("out") else "not out")
            out.append(
                f"{str(r.get('name','?'))[:21]:<22}{str(status)[:37]:<38}"
                f"{r.get('runs',0):>3} {r.get('balls',0):>4} {r.get('fours',0):>4} "
                f"{r.get('sixes',0):>4} {float(r.get('sr',0) or 0):>7.2f}"
            )
        out.append("")
        out.append(f"Total: {inn.get('runs',0)}/{inn.get('wickets',0)} ({inn.get('overs','0')} Overs)")
        out.append("")
        obsep = "-" * 68
        out.append(obsep)
        out.append(f"{'Bowler':<26}{'O':>5} {'M':>5} {'R':>5} {'W':>5} {'Econ':>7}")
        out.append(obsep)
        for b in inn.get("bowling", []):
            out.append(
                f"{str(b.get('name','?'))[:25]:<26}{str(b.get('overs','0')):>5} "
                f"{b.get('maidens',0):>5} {b.get('runs',0):>5} {b.get('wickets',0):>5} "
                f"{float(b.get('econ',0) or 0):>7.2f}"
            )
        out.append("=" * 55)
        out.append("")

    if super_over:
        out.append(f"Super Over: {super_over}")
    return "\n".join(out).rstrip() + "\n"


def _archive_text_scorecard(match_id, innings, result_text, super_over=None,
                            potm=None):
    """Best-effort, non-blocking upload of the text scorecard. Schedules the
    async upload on the running loop; silently no-ops if storage is unconfigured
    or there is no running loop."""
    try:
        from services import tg_storage_service
        if not tg_storage_service.is_configured():
            return
        # Read the live state here rather than taking it from the caller: every
        # entry point into this archive (Mini-App /cm, /cipl, Super Over) has a
        # state in the store at completion time, and none of them had to be
        # changed to say so.
        text = _build_text_scorecard(match_id, innings, result_text, super_over,
                                     state=mwa.get_state(match_id), potm=potm)
        import asyncio
        loop = asyncio.get_running_loop()
        loop.create_task(tg_storage_service.upload_text_async(
            text, f"MatchNo{match_id}.txt",
            caption=f"📄 Scorecard · Match {match_id}"))
    except RuntimeError:
        pass  # no running event loop — skip (caller is sync/non-async context)
    except Exception:
        logger.exception("text scorecard archive failed (non-fatal)")


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
            c = candidates.setdefault(rid_i, {"name": name,
                                              "player_id": p.get("player_id") if p else None,
                                              "runs": 0, "wkts": 0,
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
            c = candidates.setdefault(rid_i, {"name": name,
                                              "player_id": p.get("player_id") if p else None,
                                              "runs": 0, "wkts": 0,
                                              "fours": 0, "sixes": 0,
                                              "winner": team_uid == winner_uid})
            c["wkts"] += st.get("wickets", 0)

    # Innings 1 (snapshotted) + innings 2 (current). Ingest batting and
    # bowling separately so wickets are counted exactly once and the player's
    # XI supplies the correct display name.
    _ingest(state.get("inn1_bat_stats"), {}, state.get("inn1_bat_xi"),
            state.get("inn1_bat_team_id"))
    _ingest({}, state.get("inn1_bowl_stats"), state.get("inn1_bowl_xi"),
            state.get("inn1_bowl_team_id"))
    _ingest(state.get("bat_stats"), {}, state.get("bat_xi"),
            state.get("bat_team_id"))
    _ingest({}, state.get("bowl_stats"), state.get("bowl_xi"),
            state.get("bowl_team_id"))

    if not candidates:
        return None

    def impact(c):
        # Raw impact (no winner edge) — used both for the 50+ eligibility cutoff
        # and for ranking, so the threshold is measured on true performance.
        return c["runs"] + 20 * c["wkts"] + c["fours"] + c["sixes"] * 2

    # POTM rule: winning-team players are always eligible; losing-team players
    # only qualify with 50+ impact. Falls back to all when no winner (tie).
    eligible = [r for r, c in candidates.items()
                if c["winner"] or impact(c) >= 50]
    if not eligible:
        eligible = list(candidates)

    def score(c):
        return impact(c) + (5 if c["winner"] else 0)
    best_rid = max(eligible, key=lambda r: score(candidates[r]))
    b = candidates[best_rid]
    return {"roster_id": best_rid, "player_id": b.get("player_id"),
            "name": b["name"], "runs": b["runs"], "wickets": b["wkts"],
            "impact_points": score(b)}


def _tour_user_label(session, user_id):
    from models import User
    u = session.query(User).get(user_id) if user_id else None
    if not u:
        return "Player"
    if u.username:
        return f"@{u.username}"
    return u.first_name or "Player"


def _build_tour_announcement(session, tour, match_id, winner_uid):
    """Build the group message that reports a tour match result + standings.

    Mirrors the in-chat tour announcement so /mytours-launched Mini App matches
    keep players informed of the series score and any tour winner.
    """
    try:
        from models import TourMatch
        tm = (session.query(TourMatch)
              .filter(TourMatch.match_id == match_id).first())
        u1_label = _tour_user_label(session, tour.user1_id)
        u2_label = _tour_user_label(session, tour.user2_id)
        u1w = tour.user1_wins or 0
        u2w = tour.user2_wins or 0

        if winner_uid == tour.user1_id:
            result_line = f"🏆 <b>{u1_label}</b> won this match!"
        elif winner_uid == tour.user2_id:
            result_line = f"🏆 <b>{u2_label}</b> won this match!"
        else:
            result_line = "🤝 This match was a tie."

        header = "🏏 <b>TOUR UPDATE</b>"
        if tm is not None and getattr(tm, "match_number", None):
            header = f"🏏 <b>TOUR — Match {tm.match_number} done</b>"

        lines = [
            header,
            result_line,
            "",
            f"📊 <b>Series:</b> {u1_label} {u1w} — {u2w} {u2_label}",
        ]
        if tour.status == "completed":
            if tour.winner_id is None:
                lines.append("\n🎉 <b>Tour finished — it's a TIE!</b>")
            else:
                champ = (u1_label if tour.winner_id == tour.user1_id else u2_label)
                lines.append(f"\n🎉 <b>Tour champion: {champ}!</b> 🏆")
        else:
            lines.append("\n▶️ Open <code>/mytours</code> to play the next match.")
        return "\n".join(lines)
    except Exception:
        logger.exception("build tour announcement failed (non-fatal)")
        return None


def finalize_webapp_match(session, match_id):
    """Finalize a completed Mini-App match: update the Match record, persist
    the scorecard, and clean up live state. Returns the result dict.
    Idempotent. Rewards are applied via the existing award path if available."""
    from models import Match, User
    state = mwa.get_state(match_id)
    # /cipl (Challenge League "approach") matches are driven and finalized
    # entirely in the Telegram chat by handlers/cipl_play._complete_match. Their
    # live state happens to share the match_state store the read-only Mini App
    # polls, so the poll self-heal path could otherwise finalize+broadcast a
    # SECOND scorecard (with a divergent POTM). Never finalize them here.
    if state and state.get("mode") == "cipl_approach":
        return None
    # Lock the match row before the status check. Finalization sets
    # status="completed" but only commits ~100 lines later, after stats, POTM,
    # quests and the tour hook. Without the lock, every concurrent caller in
    # that window (Mini App poll self-heal, the heartbeat, a second tab) reads
    # the pre-commit status, decides the match is unfinished, and finalizes it
    # again — the storm of "duplicate key ... ix_match_scorecards_match_id"
    # errors. Waiters now block here and see {"already": True} instead.
    m = (session.query(Match)
         .filter(Match.id == match_id)
         .populate_existing()
         .with_for_update()
         .first())
    if not m:
        return None
    if m.status == "completed":
        return {"already": True}

    result = (state or {}).get("match_result") or {}
    # Map team ids → user ids (bat/bowl team ids ARE user ids in our state)
    winner_uid = result.get("winner_team_id")
    loser_uid = result.get("loser_team_id")

    # ── TIED MATCH → AUTO SUPER OVER (non-challenge Mini-App matches) ──
    # /wpm and /wpmbot resolve a tie with an auto-simulated super over (the
    # shared dynamics engine, same as /sim and the chat flow). /cm challenge
    # matches keep the shared-tie behavior here; routing them to an interactive
    # bowl-out needs a Mini-App-aware finalizer and is a separate change.
    if (result.get("margin_type") == "tie" and state
            and not state.get("is_challenge")):
        try:
            from services.match_dynamics import resolve_super_over
            bat_name = state.get("bat_team_name", "Team A")
            bowl_name = state.get("bowl_team_name", "Team B")
            so = resolve_super_over(
                state.get("bat_xi") or [], state.get("bowl_xi") or [],
                bat_name, bowl_name, state.get("pitch_type"))
            if not so.get("shared") and so.get("winner"):
                if so["winner"] == bat_name:
                    winner_uid = state.get("bat_team_id")
                    loser_uid = state.get("bowl_team_id")
                else:
                    winner_uid = state.get("bowl_team_id")
                    loser_uid = state.get("bat_team_id")
                result["winner_team_id"] = winner_uid
                result["loser_team_id"] = loser_uid
                result["margin_type"] = "super_over"
                result["margin_value"] = 0
                result["text"] = f"Match tied — {so['text']}"
                state["match_result"] = result
        except Exception:
            logger.exception("auto super over resolution failed (non-fatal)")

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

    # Full rewards via the shared reward core (coins/gems/season points/W-L).
    # Map winner/loser team ids → user ids (they ARE user ids in our state).
    rewards = None
    try:
        from services.match_rewards import award_match_rewards_core
        is_vsbot = bool((state or {}).get("is_vsbot"))
        # A /wpm mismatch flagged for anti stat-farming counts for nothing —
        # no coins/gems, no W/L record, no streak, no matches-played, no active
        # day. count_result=False makes every reward path below a no-op.
        count_result = not bool((state or {}).get("stats_disabled"))
        # tie → no winner; both still get a "played" + loss-tier reward each
        if result.get("margin_type") == "tie":
            # credit both as participants (loss-tier each), no W/L winner
            u1 = m.user1_id; u2 = m.user2_id
            from models import User as _U
            if count_result:
                for uid in (u1, u2):
                    usr = session.query(_U).get(uid)
                    if usr:
                        usr.matches_played = (usr.matches_played or 0) + 1
            # A tie breaks nobody's streak, but it was still a day spent playing.
            from services.match_rewards import record_match_result_stats
            record_match_result_stats(session, None, None, tie_user_ids=(u1, u2),
                                      count_result=count_result)
        elif winner_uid:
            wc, wg, lc, lg = award_match_rewards_core(
                session, winner_uid, loser_uid, m.overs or 1, is_vsbot=is_vsbot,
                count_result=count_result)
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
            m.potm_impact = pom.get("impact_points")
            m.potm_player_id = pom.get("player_id")
    except Exception:
        logger.exception("player-of-match selection failed (non-fatal)")

    # Persist the full read-only Arena snapshot only after rewards and POTM are
    # known, but still before cleanup removes live match_state.
    if state is not None:
        state["_completed_rewards"] = rewards or {}
        state["_player_of_match"] = pom
        state["match_result"] = result
        mwa.save_state(match_id, state, next_action=A_COMPLETED)
    try:
        # The archived scorecard's POTM byline. ``_pick_player_of_match``
        # returns a dict with no team, so the team slot stays empty rather than
        # being guessed — the name and figures are the part worth keeping.
        archive_potm = None
        if pom and pom.get("name"):
            figures = []
            if pom.get("runs"):
                figures.append(f"{pom['runs']} runs")
            if pom.get("wickets"):
                figures.append(f"{pom['wickets']} wkts")
            archive_potm = (pom["name"], " | ".join(figures) or None, None)
        save_final_scorecard(session, match_id, result_text=result.get("text"),
                             potm=archive_potm)
    except Exception:
        logger.exception("save_final_scorecard failed")

    try:
        from services.player_stats_service import persist_player_game_stats
        saved_counts = persist_player_game_stats(session, state or {})
        logger.info("Saved webapp player stats for match %s: %s", match_id, saved_counts)
    except Exception:
        logger.exception("webapp player-stat persistence failed")

    # Player-of-the-Match career credit. ``persist_player_game_stats`` covers
    # batting/bowling but not the POTM award, so increment it here (parity with
    # the chat finalize in handlers.match). Find the POTM player's owning side
    # from the innings XI lists, then bump that owner's PlayerGameStats.potm.
    try:
        if pom and pom.get("player_id") and state and not state.get("stats_disabled"):
            from models import PlayerGameStats
            pom_rid = pom.get("roster_id")
            pom_pid = pom.get("player_id")
            owner_uid = None
            for xi, owner in (
                (state.get("inn1_bat_xi"), state.get("inn1_bat_team_id")),
                (state.get("inn1_bowl_xi"), state.get("inn1_bowl_team_id")),
                (state.get("bat_xi"), state.get("bat_team_id")),
                (state.get("bowl_xi"), state.get("bowl_team_id")),
            ):
                if any(p.get("roster_id") == pom_rid for p in (xi or [])):
                    owner_uid = owner
                    break
            if owner_uid:
                row = (session.query(PlayerGameStats)
                       .filter(PlayerGameStats.user_id == owner_uid,
                               PlayerGameStats.player_id == pom_pid).first())
                if row:
                    row.potm = (row.potm or 0) + 1
                else:
                    session.add(PlayerGameStats(
                        user_id=owner_uid, player_id=pom_pid, potm=1))
    except Exception:
        logger.exception("webapp POTM career credit failed (non-fatal)")

    # Match-end quest tracking. The chat finalize fires per-user quest events
    # (matches, wins, runs, 50s/100s, etc.) but the Mini App path never did, so
    # /wpm and /wpmbot matches counted toward no quests. Fire the same shared
    # tracker here for both human participants (the bot user is skipped inside).
    try:
        from services.quest_service import track_user_match_quests
        from models import User as _QUser
        is_vsbot_q = bool((state or {}).get("is_vsbot"))
        is_tie = result.get("margin_type") == "tie"
        for uid in (m.user1_id, m.user2_id):
            if not uid:
                continue
            qu = session.query(_QUser).get(uid)
            track_user_match_quests(
                session, state or {}, qu,
                bool(winner_uid and uid == winner_uid and not is_tie),
                is_vsbot_q, winner_uid)
    except Exception:
        logger.exception("webapp match-end quest tracking failed")

    # ── Tour result hook ──
    # Mini-App matches that belong to a tour (launched /wpm-style from /mytours)
    # must update the tour standings exactly like the in-chat flow does. This is
    # a no-op for standalone matches (record_match_result returns None).
    tour_announcement = None
    try:
        from services.tour_service import record_match_result
        tour_after = record_match_result(session, match_id, winner_uid)
        if tour_after is not None:
            tour_announcement = _build_tour_announcement(
                session, tour_after, match_id, winner_uid)
    except Exception:
        logger.exception("Tour-result hook failed (non-fatal)")

    session.commit()

    payload = {"ok": True, "result": result, "rewards": rewards,
               "player_of_match": pom}
    if tour_announcement:
        payload["tour_announcement"] = tour_announcement

    # Keep the finished match queryable for 5 minutes after state cleanup.
    try:
        _cache_completed(match_id, payload)
    except Exception:
        pass

    # NOTE: the live match_state row is intentionally LEFT IN PLACE here. It is
    # removed by the match-summary broadcast path (admin._broadcast_match_result)
    # only after the Match Summary has been delivered to the lobby chat. Keeping
    # it alive through the final ball + summary avoids a race where a concurrent
    # poll observes the deleted state with a still-"playing" Match row and the
    # Mini App falls through to its no-match screen. Bot startup
    # (restore_active_matches) reaps any completed-match leftover state as a
    # backstop, and serialize_match_state reloads the read-only snapshot from the
    # persisted final scorecard once the row is gone.

    return payload


def ensure_webapp_match_completed(session, match_id):
    """Self-heal a Mini-App match that has reached a terminal chase state.

    Normal action endpoints finalize immediately after ``play_shot`` or AI
    auto-play stores ``A_COMPLETED``.  If a poll request sees a terminal score
    first (for example target reached on the last ball), finalize there too so
    the Arena cannot stay on an opponent-waiting panel with no runs/balls left.
    Returns the finalization payload when it completed the match, otherwise
    ``None``.
    """
    from models import Match
    from services.match_engine import compute_match_result, is_innings_over
    from services.match_state_store import A_COMPLETED

    m = session.query(Match).get(match_id)
    if not m or m.status == "completed":
        return None

    state = mwa.get_state(match_id)
    if not state:
        return None
    # /cipl matches finalize in chat (see finalize_webapp_match) — never let the
    # Mini App poll self-heal complete/broadcast them and duplicate the scorecard.
    if state.get("mode") == "cipl_approach":
        return None

    next_action = mwa.get_next_action(match_id)
    terminal = next_action == A_COMPLETED

    if not terminal and state.get("innings") == 2 and is_innings_over(state):
        result = state.get("match_result") or compute_match_result(state)
        if result:
            state["match_result"] = result
            mwa.save_state(match_id, state, next_action=A_COMPLETED)
            terminal = True

    if terminal:
        return finalize_webapp_match(session, match_id)
    return None


# ══════════════════ Abandon / timeout ═══════════════════════════════

# A Mini-App match with no ball activity for this long is force-terminated.
WEBAPP_MATCH_TIMEOUT_SECONDS = 20 * 60  # 20 minutes (per spec)


def _balls_bowled_total(state):
    """Total legal balls bowled across the match so far (both innings).
    1st-innings balls are snapshotted at the break; 2nd-innings balls come from
    the live over/ball counters."""
    if not state:
        return 0
    bpu = _state_bpu(state)
    inn = state.get("innings", 1)
    cur = (state.get("current_over", 1) - 1) * bpu + state.get("current_ball", 0)
    if inn == 1:
        return cur
    # innings 2: add the completed first-innings balls. The first-innings figure
    # is stored as "x.y" for over formats or "N balls" for The Hundred.
    i1_over = state.get("inn1_overs")
    i1_balls = 0
    if isinstance(i1_over, str):
        try:
            if "ball" in i1_over:                 # The Hundred: "63 balls"
                i1_balls = int(i1_over.split()[0])
            elif "." in i1_over:                  # over format: "12.3"
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
    # A /wpm mismatch flagged for anti stat-farming counts for nothing: no coin
    # penalty/compensation and no W/L record or streak — quitting it is free.
    count_result = not bool((state or {}).get("stats_disabled"))
    if q["has_progress"] and count_result:
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
        # A forfeit is still a decided match: it extends the winner's streak,
        # breaks the quitter's, and counts as an active day for both.
        from services.match_rewards import record_match_result_stats
        record_match_result_stats(session, win_id, lose_id)
    elif q["has_progress"] and not count_result:
        # Mismatch: the game happened, but no coins/records change hands. Still
        # mark it a forfeit result so the winner/loser show on the match card.
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
    try:
        if q["has_progress"]:
            from services.player_stats_service import persist_player_game_stats
            saved_counts = persist_player_game_stats(session, state or {})
            logger.info("Saved terminated webapp player stats for match %s: %s", match_id, saved_counts)
    except Exception:
        logger.exception("terminated webapp player-stat persistence failed")
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
    try:
        if state and _balls_bowled_total(state) > 0:
            from services.player_stats_service import persist_player_game_stats
            saved_counts = persist_player_game_stats(session, state)
            logger.info("Saved abandoned webapp player stats for match %s: %s", match_id, saved_counts)
    except Exception:
        logger.exception("abandoned webapp player-stat persistence failed")

    # Tour result hook — a forfeited tour match still counts (other side wins).
    # Build the same standings announcement the normal finalize path produces so
    # the lobby chat is told the series moved on.
    tour_announcement = None
    try:
        from services.tour_service import record_match_result
        tour_after = record_match_result(session, match_id, winner_id, forfeit=True)
        if tour_after is not None:
            tour_announcement = _build_tour_announcement(
                session, tour_after, match_id, winner_id)
    except Exception:
        logger.exception("Tour-result hook (forfeit) failed (non-fatal)")

    # CL Tour result hook — a forfeit from the Mini App still decides the match,
    # so record it for the series too; otherwise the linked CLTourMatch stays
    # 'playing' and the best-of series can never advance. Look the slot up by
    # match_id so it works regardless of whether the live state carried the tag.
    try:
        from models import CLTourMatch
        from services.cl_tour_service import record_cl_match_result
        ctm = (session.query(CLTourMatch)
               .filter(CLTourMatch.match_id == match_id,
                       CLTourMatch.status == "playing").first())
        if ctm is not None:
            record_cl_match_result(session, ctm.id, winner_id)
    except Exception:
        logger.exception("CL-tour-result hook (forfeit) failed (non-fatal)")

    session.commit()
    try:
        from services.match_state_store import cleanup_state
        cleanup_state(mwa.fresh_ctx(), match_id)
    except Exception:
        pass

    # Announce the tour standings to the lobby chat, mirroring the finalize path.
    if tour_announcement:
        try:
            from admin import _announce_tour_after_result
            _announce_tour_after_result(
                match_id, {"tour_announcement": tour_announcement})
        except Exception:
            logger.exception("forfeit tour announcement dispatch failed (non-fatal)")

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
        diff = state.get("vsbot_difficulty", "Medium")

        if na in (A_PICK_DELIVERY, A_PICK_LENGTH):
            bowler = get_bowler(state)
            pick = bot_ai.pick_bot_delivery(bowler, over, total, difficulty=diff)
            state["current_delivery"] = pick["delivery"]
            state["selected_variation"] = pick.get("variation")
            state["last_autoplay_delivery"] = {
                "bowler": bowler.get("name") if bowler else None,
                "delivery": pick["delivery"],
            }
            mwa.save_state(match_id, state, next_action=A_PICK_SHOT)
            # If the human is batting, stop here so they can play their shot
            if not _is_bot_side(state, "bat"):
                steps.append({
                    "type": "bot_delivery",
                    "bowler": bowler.get("name") if bowler else None,
                    "delivery": pick["delivery"],
                })
                break
            # Bot batting too → continue to auto-shot below on next loop
            steps.append({
                "type": "bot_delivery",
                "bowler": bowler.get("name") if bowler else None,
                "delivery": pick["delivery"],
            })
            continue

        if na == A_PICK_SHOT:
            # Bot batting plays a shot
            striker = get_striker(state)
            bowler = get_bowler(state)
            delivery = state.get("current_delivery") or "Good"
            _name, idx = bot_ai.pick_bot_shot(
                striker, bowler, over, total,
                state.get("total_runs", 0), state.get("total_wickets", 0),
                target=state.get("target"), current_ball=state.get("current_ball", 0),
                difficulty=diff)
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
            state["last_autoplay_shot"] = {
                "batter": striker.get("name") if striker else None,
                "shot": shot,
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
            elif res["need_new_bat"] and state["total_wickets"] < state.get("wicket_limit", 10):
                nb = state.get("next_batsman_idx", 2)
                if nb < len(state.get("batting_order", [])):
                    _install_new_batsman(state, nb)
                    state["next_batsman_idx"] = nb + 1
                # A wicket on the last ball still owes a new-bowler pick.
                nxt = A_PICK_NEW_BOWLER if res["eoo"] else A_PICK_DELIVERY
                mwa.save_state(match_id, state, next_action=nxt)
            elif res["eoo"]:
                mwa.save_state(match_id, state, next_action=A_PICK_NEW_BOWLER)
            else:
                mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            mwa.bump_ball_seq(match_id)
            continue

        if na == A_PICK_NEW_BOWLER:
            new_bowler = bot_ai.pick_bot_next_bowler(
                _bot_bowler_pool(state, _active_players(state["bowl_xi"]),
                                 state.get("prev_bowler_rid")),
                state.get("prev_bowler_rid"),
                state["bowl_stats"], state["overs"])
            state["current_bowler"] = new_bowler
            _emit_new_bowler(state, new_bowler)
            mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            steps.append({"type": "bot_bowler", "name": new_bowler["name"]})
            continue

        if na == A_PICK_NEW_BATSMAN:
            nb = state.get("next_batsman_idx", 2)
            if nb < len(state.get("batting_order", [])):
                _install_new_batsman(state, nb)
                state["next_batsman_idx"] = nb + 1
            nxt = (A_PICK_NEW_BOWLER if state.pop("pending_new_bowler", False)
                   else A_PICK_DELIVERY)
            mwa.save_state(match_id, state, next_action=nxt)
            continue

        break

    return steps


def _user_controls_current_action(state, next_action, user_id):
    """Does `user_id` control whatever the next action requires?"""
    from services.match_state_store import (
        A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_SHOT,
        A_PICK_NEW_BATSMAN, A_PICK_NEW_BOWLER,
    )
    if next_action in (A_PICK_DELIVERY, A_PICK_LENGTH, A_PICK_NEW_BOWLER):
        return user_id == state.get("bowl_team_id")
    if next_action in (A_PICK_SHOT, A_PICK_NEW_BATSMAN):
        return user_id == state.get("bat_team_id")
    return False


def auto_play_user_turns(session, match_id, user_id, max_steps=200, difficulty=None):
    """Play ALL of ``user_id``'s currently-pending turns with the bot AI.

    This is the per-user analogue of ``auto_play_bot_turns`` — it powers the Mini
    App "autoplay" toggle so a human can hand their own side to the same
    difficulty/phase-aware engine ``/vsbot`` uses. It automates only the
    ball-by-ball play of whichever side the user controls (bowling: deliveries;
    batting: shots), reusing ``_apply_outcome`` and the same next-action
    bookkeeping as the manual ``play_shot`` path. Every SELECTION stays manual,
    even under Autoplay: XI/opener setup, the new bowler at the end of an over,
    the incoming batsman after a wicket, and the opening bowler at the 2nd-innings
    break are all handed back to the user to tap.

    Stops when it's the opponent's/bot's turn, a selection is owed, the match
    completes, or no progress is made (loop-safety). Saves state as it goes;
    caller need not commit. Returns a list of step dicts (for optional
    commentary)."""
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

        diff = difficulty or state.get("vsbot_difficulty") or "Medium"

        # ── XI setup phase ──
        # Bowler/batsman SELECTION is always the user's call, even under Autoplay:
        # opener selection (and the rest of the setup) stays manual. Autoplay only
        # ever takes over deliveries and shots, so stop here and let the user pick.
        if _in_setup(state):
            break

        if not _user_controls_current_action(state, na, user_id):
            break  # opponent's / bot's turn — leave it for them

        over = state.get("current_over", 1)
        total = state.get("overs", 1)

        if na in (A_PICK_DELIVERY, A_PICK_LENGTH):
            bowler = get_bowler(state)
            pick = bot_ai.pick_bot_delivery(bowler, over, total, difficulty=diff)
            state["current_delivery"] = pick["delivery"]
            state["selected_variation"] = pick.get("variation")
            state["last_autoplay_delivery"] = {
                "bowler": bowler.get("name") if bowler else None,
                "delivery": pick["delivery"],
            }
            mwa.save_state(match_id, state, next_action=A_PICK_SHOT)
            steps.append({
                "type": "auto_delivery",
                "bowler": bowler.get("name") if bowler else None,
                "delivery": pick["delivery"],
            })
            # If this same user also bats (PvP self-play is impossible, but be
            # safe), keep going; otherwise hand the ball to the batsman.
            if user_id != state.get("bat_team_id"):
                break
            continue

        if na == A_PICK_SHOT:
            striker = get_striker(state)
            bowler = get_bowler(state)
            delivery = state.get("current_delivery") or "Good"
            _name, idx = bot_ai.pick_bot_shot(
                striker, bowler, over, total,
                state.get("total_runs", 0), state.get("total_wickets", 0),
                target=state.get("target"), current_ball=state.get("current_ball", 0),
                difficulty=diff)
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
            state["last_autoplay_shot"] = {
                "batter": striker.get("name") if striker else None,
                "shot": shot,
            }
            state["last_commentary"] = _c or res.get("rtxt")
            _append_commentary_log(state, res, striker, bowler, _c or res.get("rtxt"))
            steps.append({
                "type": "auto_shot",
                "batter": striker.get("name") if striker else None,
                "shot": shot,
                "rtxt": res["rtxt"],
            })

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
            elif res["need_new_bat"] and state["total_wickets"] < state.get("wicket_limit", 10):
                # Wicket: batsman selection stays manual even under Autoplay —
                # hand control back so the user picks the incoming batsman. A
                # last-ball wicket still owes a new-bowler pick afterwards.
                state["pending_new_bowler"] = bool(res["eoo"])
                mwa.save_state(match_id, state, next_action=A_PICK_NEW_BATSMAN)
                mwa.bump_ball_seq(match_id)
                break
            elif res["eoo"]:
                mwa.save_state(match_id, state, next_action=A_PICK_NEW_BOWLER)
            else:
                mwa.save_state(match_id, state, next_action=A_PICK_DELIVERY)
            mwa.bump_ball_seq(match_id)
            continue

        # Bowler/batsman SELECTION always stays the user's call, even under
        # Autoplay — picking the new bowler at the end of an over, the incoming
        # batsman after a wicket, and the opening bowler when the 2nd innings
        # begins are all manual. Autoplay only ever automates deliveries and
        # shots, so hand control back here and let the user tap. (This also
        # mirrors the Mini App client, which never auto-acts on selection turns.)
        if na in (A_PICK_NEW_BOWLER, A_PICK_NEW_BATSMAN):
            break

        break

    return steps
