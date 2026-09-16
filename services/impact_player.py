"""Impact Player — the one-per-team mid-match substitution, for every mode.

Two match engines need this rule and must not drift apart:

  * the Mini App ball-by-ball flow (``services.match_webapp_service``), which
    has had Impact Player since it shipped; and
  * the over-by-over "Approach" flow that ``/letsplay`` and Challenge League
    share (``services.cipl_match`` + ``handlers.cipl_play``).

The mode-agnostic half lives here and both engines import it.
``match_webapp_service`` keeps its private aliases so its own phase-specific
``get_impact_player_options`` / ``use_impact_player`` are untouched; the
over-by-over pair is ``cipl_options`` / ``cipl_use`` below.

The over-by-over pair deliberately takes the **state dict**, not a match id, and
takes **no DB session**:

  * the caller owns load/save, so the handler can run the swap inside
    ``match_state_store.update_state_cas`` and not lose a concurrent write; and
  * the bench is snapshotted onto the state at launch (``bat_bench`` /
    ``bowl_bench``), because the three squad sources feeding that one engine —
    ``UserRoster`` for /letsplay, ``ChallengePlayer`` for a league, and the
    draft dict itself for /cdraft — are all gone by the time an over is bowled.

That makes the whole swap pure dict manipulation: no query, no source branch.
"""

import logging

from services.match_state_store import (
    A_PICK_CIPL_BOWLER, A_PICK_BOWL_APPROACH, A_PICK_BAT_APPROACH,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════
# Shared, mode-agnostic helpers (also aliased by match_webapp_service)
# ════════════════════════════════════════════════════════════════════

def usage_for(state):
    """Return ``(impact, usage)``, seeding a record for every team in the match.

    Both innings' team ids are seeded because a side's usage has to survive the
    innings swap — a team that used its Impact Player while bowling must not get
    a second one when it comes out to bat.
    """
    impact = state.setdefault("impact_players", {})
    usage = impact.setdefault("usage", {})
    for uid in (state.get("bat_team_id"), state.get("bowl_team_id"),
                state.get("inn1_bat_team_id"), state.get("inn1_bowl_team_id")):
        if uid is not None:
            usage.setdefault(str(uid), {"used": False})
    return impact, usage


def summary(state):
    """One entry per team that has used its Impact Player, for the scorecard."""
    usage_for(state)
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


# How an Impact substitute is marked wherever a name is shown. One constant so
# the Telegram scorecard, the HTML analysis and the Mini App cannot drift into
# three different suffixes.
IMPACT_SUFFIX = " -IP"


def is_impact(player):
    """True for a player who came on as an Impact substitute."""
    return bool(isinstance(player, dict) and player.get("impact_replacement"))


def display_name(player, name=None):
    """Player name with the ``-IP`` suffix when they came on as a substitute.

    ``name`` overrides the dict's own name for callers that have already
    formatted or escaped it.
    """
    base = name if name is not None else (player or {}).get("name", "")
    base = str(base or "")
    return base + IMPACT_SUFFIX if is_impact(player) else base


def is_active(player):
    return not isinstance(player, dict) or player.get("active", True) is not False


def active_players(players):
    return [p for p in (players or []) if is_active(p)]


def replace_in_list(players, out_rid, incoming):
    for i, p in enumerate(players or []):
        if p.get("roster_id") == out_rid:
            players[i] = incoming
            return i
    return None


def apply_to_identity_list(players, out_rid, incoming):
    """Keep the outgoing player's identity for stat lookup, but mark inactive.

    Impact substitutions can occur after the outgoing player has already batted
    or bowled. Scorecard and career persistence resolve stat rows through these
    XI lists, so replacing the dict in-place can orphan existing stats.
    """
    for i, p in enumerate(players or []):
        if p.get("roster_id") == out_rid and is_active(p):
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


# ════════════════════════════════════════════════════════════════════
# Over-by-over ("Approach") mode — /letsplay and Challenge League
# ════════════════════════════════════════════════════════════════════

# Every point before the over is actually simulated is the same window: the
# whole over runs in one go, so there is no mid-over moment to protect. The
# innings break needs no entry of its own — _innings_break hands straight back
# to A_PICK_CIPL_BOWLER for the first over of the chase.
CIPL_LEGAL_ACTIONS = (
    A_PICK_CIPL_BOWLER, A_PICK_BOWL_APPROACH, A_PICK_BAT_APPROACH,
)

NOT_A_BREAK_MESSAGE = (
    "Impact Player can only be used between overs or at the innings break — "
    "an over is simulated in one go, so there is no mid-over window."
)


def cipl_break_label(state, next_action):
    """Human label for the current window, or None when it is not a legal one."""
    if next_action not in CIPL_LEGAL_ACTIONS:
        return None
    innings = state.get("innings") or 1
    first_over = (state.get("current_over") or 1) <= 1
    if innings == 2 and first_over:
        return "innings break"
    if innings == 1 and first_over:
        return "before the first over"
    return "between overs"


def cipl_is_legal_break(state, next_action):
    return cipl_break_label(state, next_action) is not None


def cipl_side(state, user_id):
    """"bat" / "bowl" for a participating captain, else None."""
    if user_id is not None and user_id == state.get("bat_team_id"):
        return "bat"
    if user_id is not None and user_id == state.get("bowl_team_id"):
        return "bowl"
    return None


def _crease_roster_ids(state):
    """Roster ids of the two batters currently at the crease."""
    order = state.get("batting_order") or []
    out = set()
    for key in ("striker_idx", "non_striker_idx"):
        idx = state.get(key)
        if isinstance(idx, int) and 0 <= idx < len(order):
            rid = (order[idx] or {}).get("roster_id")
            if rid is not None:
                out.add(rid)
    return out


def _spent_roster_ids(usage):
    """Everyone already consumed by an Impact swap, either direction."""
    spent = set()
    for rec in (usage or {}).values():
        if not isinstance(rec, dict):
            continue
        for key in ("in_roster_id", "out_roster_id"):
            if rec.get(key) is not None:
                spent.add(rec[key])
    return spent


def cipl_batting_slots(state, out_roster_id=None):
    """Legal batting positions for an incoming substitute, as (index, label).

    ``striker_idx`` and ``non_striker_idx`` are always below ``next_batsman_idx``
    — the order starts 0/1/2 and each wicket does ``striker_idx =
    next_batsman_idx; next_batsman_idx += 1``. So every index from
    ``next_batsman_idx`` up is safe to insert at without shifting anyone who is
    batting or has already batted, and no index needs fixing up afterwards.
    """
    order = list(state.get("batting_order") or [])
    nb = state.get("next_batsman_idx")
    nb = nb if isinstance(nb, int) else len(order)
    nb = max(0, min(nb, len(order)))

    # A substitute for a player who has not batted yet frees up their own slot,
    # so quote the positions as the captain will actually get them.
    if out_roster_id is not None:
        out_idx = next((i for i, p in enumerate(order)
                        if (p or {}).get("roster_id") == out_roster_id), None)
        if out_idx is not None and out_idx >= nb:
            order.pop(out_idx)

    slots = []
    for k in range(nb, len(order) + 1):
        if k == len(order):
            label = "Last"
        elif k == nb:
            label = f"Next in (before {order[k].get('name', '?')})"
        else:
            label = f"Before {order[k].get('name', '?')}"
        slots.append((k, label))
    return slots


def cipl_options(state, user_id, next_action):
    """What this captain may do with their Impact Player right now.

    Pure: reads the bench snapshotted onto the state at launch, so it needs no
    DB session and works identically for /letsplay, a Challenge League match and
    a /cdraft squad.
    """
    side = cipl_side(state, user_id)
    if side is None:
        return {"ok": False, "message": "Only the two captains can use Impact Player."}

    impact, usage = usage_for(state)
    rec = usage.setdefault(str(user_id), {"used": False})
    used = bool(rec.get("used"))
    legal_label = cipl_break_label(state, next_action)

    xi = active_players(state.get(f"{side}_xi") or [])
    spent = _spent_roster_ids(usage)
    bench = [p for p in (state.get(f"{side}_bench") or [])
             if p.get("roster_id") not in spent
             and not any(x.get("roster_id") == p.get("roster_id") for x in xi)]

    if side == "bat":
        # Real Impact Player rule, and it keeps striker_idx/non_striker_idx from
        # ever pointing at someone who has left the field: a batter at the crease
        # cannot be replaced. Everyone else active is either out or yet to bat.
        crease = _crease_roster_ids(state)
        replaceable = [p for p in xi if p.get("roster_id") not in crease]
        blocked_note = "Batters at the crease cannot be replaced."
    else:
        # The bowler already handed the ball for this over is off limits — you
        # cannot change bowler mid-over. Swapping at the bowler-pick step instead
        # lets the substitute be chosen to bowl straight away, which also avoids
        # having to re-check quota and back-to-back eligibility here.
        current = (state.get("current_bowler") or {}).get("roster_id")
        replaceable = [p for p in xi if p.get("roster_id") != current]
        blocked_note = ("The bowler already picked for this over cannot be "
                        "replaced — swap before picking the bowler.")

    if used:
        message = "Impact Player already used."
    elif not legal_label:
        message = NOT_A_BREAK_MESSAGE
    elif not bench:
        message = "No substitutes available outside your Playing XI."
    elif not replaceable:
        message = blocked_note
    else:
        message = f"Impact Player is available ({legal_label})."

    return {
        "ok": True,
        "side": side,
        # Why a given XI member is not in replaceable_players. Shown when a
        # stale button names one, where the generic status message ("Impact
        # Player is available") would be nonsense as a rejection.
        "blocked_note": blocked_note,
        "can_use": bool(not used and legal_label and bench and replaceable),
        "used": used,
        "legal_break": legal_label,
        "message": message,
        "incoming_options": bench,
        "replaceable_players": replaceable,
        "summary": summary(state),
    }


def _new_bat_stat():
    return {"runs": 0, "balls": 0, "fours": 0, "sixes": 0,
            "out": False, "how_out": "", "bowled_by": ""}


def _new_bowl_stat():
    return {"balls": 0, "runs": 0, "wickets": 0, "overs_done": 0,
            "this_over_balls": 0, "maidens": 0, "this_over_runs": 0}


def insert_into_batting_order(state, incoming, out_roster_id, position=None):
    """Put the substitute into the batting order at ``position``.

    Returns the index actually used. Two cases, and they differ:

    * the outgoing player has **not** batted — their slot is freed, and the
      substitute takes it unless the captain asked for another one;
    * the outgoing player is already **dismissed** — they must stay in the order
      at their historical slot or the scorecard loses their innings, so the
      substitute is *inserted* at the chosen slot. Appending (what the Mini App
      does) would force every impact batter to bat last, which is the whole
      reason the captain gets to choose.

    Only indices at or above ``next_batsman_idx`` are touched, so no batting
    index ever needs fixing up — see cipl_batting_slots for why.
    """
    order = state.get("batting_order") or []
    nb = state.get("next_batsman_idx")
    nb = nb if isinstance(nb, int) else len(order)
    nb = max(0, min(nb, len(order)))

    out_idx = next((i for i, p in enumerate(order)
                    if (p or {}).get("roster_id") == out_roster_id), None)
    default = len(order)
    if out_idx is not None and out_idx >= nb:
        order.pop(out_idx)          # yet to bat — free their slot
        default = out_idx

    hi = len(order)
    pos = default if position is None else max(nb, min(int(position), hi))
    order.insert(pos, incoming)
    state["batting_order"] = order
    return pos


def cipl_use(state, user_id, in_roster_id, out_roster_id, next_action,
             bat_position=None):
    """Apply an Impact Player swap to an over-by-over match state.

    Pure state mutation — the caller loads, saves and locks. Returns
    ``(ok, message, rec)``; ``rec`` is the usage record on success.
    """
    opts = cipl_options(state, user_id, next_action)
    if not opts.get("ok"):
        return False, opts.get("message", "Impact Player is unavailable."), None
    if opts["used"]:
        return False, "Impact Player already used by your team.", None
    if not opts["legal_break"]:
        return False, NOT_A_BREAK_MESSAGE, None

    side = opts["side"]
    incoming = next((p for p in opts["incoming_options"]
                     if p.get("roster_id") == in_roster_id), None)
    outgoing = next((p for p in opts["replaceable_players"]
                     if p.get("roster_id") == out_roster_id), None)
    if not incoming:
        return False, "Pick a substitute from outside your Playing XI.", None
    if not outgoing:
        return False, opts.get("blocked_note") or opts["message"], None

    # Mark the substitute BEFORE they are filed anywhere. apply_to_identity_list
    # stamps its own copy for the XI list, but batting_order gets this dict —
    # and every surface that renders a name (the chat scorecard, the summary
    # card image) reads the order, not the XI. Without this they show the
    # substitute as an ordinary player.
    incoming = dict(incoming)
    incoming["active"] = True
    incoming["impact_replacement"] = True
    incoming["replaced_roster_id"] = out_roster_id

    xi_key = f"{side}_xi"
    if apply_to_identity_list(state.get(xi_key) or [], out_roster_id, incoming) is None:
        return False, "That player is not in your active XI.", None

    # The substitute has left the bench either way.
    state[f"{side}_bench"] = [p for p in (state.get(f"{side}_bench") or [])
                              if p.get("roster_id") != in_roster_id]

    slot = None
    if side == "bat":
        state.setdefault("bat_stats", {})[str(in_roster_id)] = _new_bat_stat()
        slot = insert_into_batting_order(state, incoming, out_roster_id, bat_position)
    else:
        # A fresh bowling row gives the substitute their own full quota, while
        # the outgoing bowler's figures stay put for the scorecard.
        state.setdefault("bowl_stats", {})[str(in_roster_id)] = _new_bowl_stat()
        # This side has no batting order yet when it is bowling first; the stored
        # position is applied when end_first_innings builds one.
        slot = bat_position

    _, usage = usage_for(state)
    rec = usage.setdefault(str(user_id), {"used": False})
    rec.update({
        "used": True,
        "in_roster_id": in_roster_id,
        "out_roster_id": out_roster_id,
        "in_player": incoming.get("name"),
        "out_player": outgoing.get("name"),
        "team_name": state.get(f"{side}_team_name"),
        "used_at": f"{max(0, (state.get('current_over') or 1) - 1)} ov",
        "innings": state.get("innings"),
        "break": opts["legal_break"],
        "side": side,
        "bat_position": slot,
    })
    state["impact_players"] = state.get("impact_players") or {}
    state["impact_players"]["usage"] = usage

    state.setdefault("commentary_log", []).append({
        "type": "impact_player",
        "team": rec.get("team_name"),
        "inPlayer": incoming.get("name"),
        "outPlayer": outgoing.get("name"),
        "over": rec.get("used_at"),
        "text": (f"Impact Player! {incoming.get('name')} replaces "
                 f"{outgoing.get('name')} for {rec.get('team_name')}."),
    })
    return True, (f"Impact Player confirmed: {incoming.get('name')} replaces "
                  f"{outgoing.get('name')}."), rec


def rebuild_batting_order(xi, usage, user_id):
    """Batting order for a side starting its innings, honouring an earlier swap.

    Called from cipl_match.end_first_innings. The XI list carries the outgoing
    player as an inactive entry with the substitute appended last, so a naive
    ``list(xi)`` would both send a player who has left the field out to open and
    bury the substitute at number 12.
    """
    order = active_players(xi)
    rec = (usage or {}).get(str(user_id))
    if not isinstance(rec, dict) or not rec.get("used"):
        return order
    in_rid = rec.get("in_roster_id")
    pos = rec.get("bat_position")
    if in_rid is None or pos is None:
        return order
    sub = next((p for p in order if p.get("roster_id") == in_rid), None)
    if sub is None:
        return order
    order = [p for p in order if p.get("roster_id") != in_rid]
    order.insert(max(0, min(int(pos), len(order))), sub)
    return order
