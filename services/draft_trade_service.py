"""Post-draft player trading between draft franchises — the ``/dtrade`` rules.

``/trade`` swaps two cards between two captains and is built on one rule: the
two cards must be the **exact same OVR**. That rule is what keeps a card
economy honest, and it is exactly the wrong rule here. A draft squad is not a
collection, it is a team: a franchise with three keepers and no death bowler
wants to send 92 OVR out and take 84 OVR back, and being told the two numbers
must match means the trade simply never happens.

So **there is no rating rule at all**. Any player, for any player. What
replaces it is not a cheaper restriction but a different kind of one — the
rules the draft already enforced on every pick, re-checked on a squad the trade
has not made yet:

* **Counts match.** A squad's size is its number of slots in the order sheet,
  so a trade is N-for-N. Two-for-two is a trade; two-for-one is a transfer that
  would leave one franchise a player short of a legal XI.
* **The tier quota holds.** A Platinum slot takes Platinum or below, which is
  what caps a team's Platinum count at its number of Platinum slots. A trade
  must not be the back door around it — see :func:`tier_overflow`.
* **The overseas cap holds**, and **role minimums hold**.
* **Never worse than it already was.** A squad with a passed slot can already
  be short of a minimum. Such a squad may still trade; what it may not do is
  make the shortfall deeper. Every check below compares the after against the
  before, not against perfection, so one skipped slot in round four cannot
  freeze a franchise out of the trade window for good.

And the gap the rating rule used to close is closed by daylight instead: the
offer card prints both sides' totals and says out loud which way the OVR moved.
A lopsided trade is allowed, visible to the whole draft group, and permanently
in ``/dtrades``.

**Offers live in the database.** ``/trade`` keeps its in-flight state in
``context.bot_data``, which a redeploy empties; this host redeploys often and a
trade window is open for days, so a half-built offer is a ``DraftTrade`` row.
A button pressed after a restart picks up exactly where it was.

Contract, matching ``services.draft_service``: session first, **nothing here
commits**, every refusal is a ``DraftError`` carrying plain text. The
``render_*`` helpers at the bottom build HTML and escape as they go.
"""

import json
import logging
from datetime import datetime, timedelta
from html import escape

from models import (
    ChallengePlayer, ChallengeTeam, DraftPlayer, DraftSquadEdit, DraftTeam,
    DraftTrade,
)
from services import draft_service as ds
from services.draft_service import DraftError

logger = logging.getLogger(__name__)

# How long an offer stays open. The draft's own pick clock is 15 minutes and
# this is the same order of thing — two people in a group chat agreeing on
# something — but with two squads to read rather than one pool, so it gets
# longer. Short enough that a forgotten offer does not block the two teams'
# next one for the rest of the evening.
TRADE_EXPIRES_SECONDS = 30 * 60

# A squad is 15-odd players; two per row keeps the names readable on a phone.
SQUAD_PAGE_SIZE = 8

# Statuses. ``building_a`` / ``building_b`` are the two selection steps,
# ``offered`` is the confirmation card both sides tap.
STATUS_BUILDING_A = "building_a"
STATUS_BUILDING_B = "building_b"
STATUS_OFFERED = "offered"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"

LIVE_STATUSES = (STATUS_BUILDING_A, STATUS_BUILDING_B, STATUS_OFFERED)


# ──────────────────────────────────────────────────────────────────────
# The window
# ──────────────────────────────────────────────────────────────────────

def require_open(draft):
    """Raise unless this draft is finished and its trade window is open.

    Trading during a draft would be a different feature — a team that has not
    picked yet can achieve the same thing by picking differently, and a squad
    changing shape under a live clock breaks the reachability checks that make
    role minimums enforceable. So: after the last slot is filled, not before.
    """
    if draft.status == ds.STATUS_CANCELLED:
        raise DraftError("This draft was cancelled — there is nothing to trade.")
    if draft.status != ds.STATUS_COMPLETED:
        raise DraftError(
            "Trading opens when the draft finishes. This draft is "
            f"{ds.status_label(draft)} — finish the picks first.")
    if not trades_open(draft):
        raise DraftError("The trade window is closed. An admin reopens it with "
                         "/dtradelock off.")


def trades_open(draft):
    """Whether the window is open, treating a legacy NULL as open."""
    return draft.trades_open is None or bool(draft.trades_open)


def set_window(session, draft, is_open):
    draft.trades_open = bool(is_open)
    if not is_open:
        # An offer left half-built when the window shuts would come back to
        # life the moment it reopened, hours later and against squads that have
        # moved on. Close them with the window.
        cancel_live_trades(session, draft, reason=STATUS_CANCELLED)
    session.flush()
    return draft


def cancel_live_trades(session, draft, reason=STATUS_CANCELLED):
    rows = (session.query(DraftTrade)
            .filter(DraftTrade.draft_id == draft.id,
                    DraftTrade.status.in_(LIVE_STATUSES)).all())
    now = datetime.utcnow()
    for trade in rows:
        trade.status = reason
        trade.updated_at = now
    if rows:
        session.flush()
    return len(rows)


# ──────────────────────────────────────────────────────────────────────
# Offers
# ──────────────────────────────────────────────────────────────────────

def _ids(raw):
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [int(v) for v in value if isinstance(v, (int, str)) and str(v).isdigit()]


def _dump_ids(ids):
    return json.dumps([int(i) for i in ids], separators=(",", ":"))


def selection(trade, side):
    """The DraftPlayer ids one side has ticked, in the order they ticked them."""
    return _ids(trade.players_a_json if side == "a" else trade.players_b_json)


def set_selection(trade, side, ids):
    if side == "a":
        trade.players_a_json = _dump_ids(ids)
    else:
        trade.players_b_json = _dump_ids(ids)
    trade.updated_at = datetime.utcnow()


def is_expired(trade, now=None):
    if trade.expires_at is None:
        return False
    return (now or datetime.utcnow()) >= trade.expires_at


def expire_stale(session, draft):
    """Time out live offers past their deadline. Returns how many."""
    now = datetime.utcnow()
    rows = (session.query(DraftTrade)
            .filter(DraftTrade.draft_id == draft.id,
                    DraftTrade.status.in_(LIVE_STATUSES),
                    DraftTrade.expires_at.isnot(None),
                    DraftTrade.expires_at < now).all())
    for trade in rows:
        trade.status = STATUS_EXPIRED
        trade.updated_at = now
    if rows:
        session.flush()
    return len(rows)


def get_trade(session, trade_id):
    return (session.query(DraftTrade)
            .filter(DraftTrade.id == int(trade_id)).first())


def live_trade_for_team(session, draft, team_id):
    """The open offer either side of which is this team, or ``None``."""
    expire_stale(session, draft)
    return (session.query(DraftTrade)
            .filter(DraftTrade.draft_id == draft.id,
                    DraftTrade.status.in_(LIVE_STATUSES),
                    ((DraftTrade.team_a_id == team_id)
                     | (DraftTrade.team_b_id == team_id)))
            .order_by(DraftTrade.id.desc()).first())


def open_trade(session, draft, team_a, team_b, *, by_tg_id=None, chat_id=None):
    """Start an offer from ``team_a`` to ``team_b``. Returns the DraftTrade."""
    require_open(draft)
    if team_a is None or team_b is None:
        raise DraftError("Name the team you want to trade with, e.g. "
                         "/dtrade Mumbai.")
    if team_a.id == team_b.id:
        raise DraftError("You cannot trade with yourself.")
    if team_a.draft_id != draft.id or team_b.draft_id != draft.id:
        raise DraftError("Both teams have to be in this draft.")

    for team in (team_a, team_b):
        existing = live_trade_for_team(session, draft, team.id)
        if existing is not None:
            other = (existing.team_b if existing.team_a_id == team.id
                     else existing.team_a)
            raise DraftError(
                f"{team.name} already has an open trade with "
                f"{other.name if other else 'another team'}. Finish or cancel "
                f"it first — /dtradecancel.")

    for team in (team_a, team_b):
        if not tradable_squad(session, team.id):
            raise DraftError(f"{team.name} has no players to trade.")

    now = datetime.utcnow()
    trade = DraftTrade(
        draft_id=draft.id,
        team_a_id=team_a.id,
        team_b_id=team_b.id,
        players_a_json=_dump_ids([]),
        players_b_json=_dump_ids([]),
        status=STATUS_BUILDING_A,
        opened_by_tg_id=by_tg_id,
        chat_id=chat_id,
        expires_at=now + timedelta(seconds=TRADE_EXPIRES_SECONDS),
        created_at=now,
        updated_at=now,
    )
    session.add(trade)
    session.flush()
    return trade


def touch(trade):
    """Push the deadline out — somebody is still working on this offer."""
    trade.expires_at = (datetime.utcnow()
                        + timedelta(seconds=TRADE_EXPIRES_SECONDS))
    trade.updated_at = datetime.utcnow()


def cancel(session, trade, *, by_tg_id=None, status=STATUS_CANCELLED):
    trade.status = status
    trade.closed_by_tg_id = by_tg_id
    trade.updated_at = datetime.utcnow()
    session.flush()
    return trade


# ──────────────────────────────────────────────────────────────────────
# Who may drive which side
# ──────────────────────────────────────────────────────────────────────

def side_for(trade, tg_id):
    """``"a"``, ``"b"`` or ``None`` — which side of this trade a user drives.

    Owners **and co-owners**, the same people ``/pick`` lets act for a team:
    a franchise run by two people does not stop being run by two people once
    the draft ends.
    """
    for side, team in (("a", trade.team_a), ("b", trade.team_b)):
        if ds.may_pick_for(team, tg_id):
            return side
    return None


def team_on(trade, side):
    return trade.team_a if side == "a" else trade.team_b


def other_side(side):
    return "b" if side == "a" else "a"


def active_side(trade):
    """The side whose turn it is to tick players, or ``None`` when both are done."""
    if trade.status == STATUS_BUILDING_A:
        return "a"
    if trade.status == STATUS_BUILDING_B:
        return "b"
    return None


def confirmed(trade, side):
    return (trade.confirmed_a_by if side == "a" else trade.confirmed_b_by) is not None


def set_confirmed(trade, side, tg_id):
    if side == "a":
        trade.confirmed_a_by = tg_id
    else:
        trade.confirmed_b_by = tg_id
    trade.updated_at = datetime.utcnow()


# ──────────────────────────────────────────────────────────────────────
# Building the offer
# ──────────────────────────────────────────────────────────────────────

def tradable_squad(session, team_id):
    """Every player on a squad, best tier first — all of them are tradable.

    There is no untradable card here. A drafted player belongs to the franchise
    that spent a slot on them, and the franchise may move them.
    """
    rows = (session.query(DraftPlayer)
            .filter(DraftPlayer.picked_by_team_id == team_id).all())
    return sorted(rows, key=lambda p: (-(p.rating or 0), p.name or ""))


def squad_in_page_order(session, draft, team_id):
    """A squad in the order the trade buttons show it: best tier, then best OVR."""
    return sorted(tradable_squad(session, team_id),
                  key=lambda p: (ds.tier_rank(draft, p.tier)
                                 if ds.tier_rank(draft, p.tier) is not None else 99,
                                 -(p.rating or 0), p.name or ""))


def squad_page(session, draft, team_id, page=0):
    """``(rows, page, pages)`` for one page of a squad's trade buttons."""
    rows = squad_in_page_order(session, draft, team_id)
    pages = max(1, -(-len(rows) // SQUAD_PAGE_SIZE))
    page = max(0, min(int(page or 0), pages - 1))
    start = page * SQUAD_PAGE_SIZE
    return rows[start:start + SQUAD_PAGE_SIZE], page, pages


def toggle(session, trade, side, player_id):
    """Tick or un-tick one player on a side's list. Returns the new list."""
    team_id = team_on(trade, side).id
    player = (session.query(DraftPlayer)
              .filter(DraftPlayer.id == int(player_id)).first())
    if player is None or player.draft_id != trade.draft_id:
        raise DraftError("That player is not in this draft.")
    if player.picked_by_team_id != team_id:
        raise DraftError(f"{player.name} is not on that squad any more.")
    chosen = selection(trade, side)
    if player.id in chosen:
        chosen.remove(player.id)
    else:
        chosen.append(player.id)
    set_selection(trade, side, chosen)
    touch(trade)
    session.flush()
    return chosen


def finish_side(session, trade, side):
    """Close one side's selection and move the offer on.

    Side A sets the size of the deal; side B has to match it, which is where
    N-for-N is actually enforced rather than merely described.
    """
    chosen = selection(trade, side)
    if not chosen:
        raise DraftError("Tick at least one player first.")
    if side == "a":
        trade.status = STATUS_BUILDING_B
    else:
        wanted = len(selection(trade, "a"))
        if len(chosen) != wanted:
            raise DraftError(
                f"A trade is player-for-player: {trade.team_a.name} offered "
                f"{wanted}, so pick {wanted}. You have ticked {len(chosen)}.")
        # Both lists are in; refuse now rather than at the confirmation tap, so
        # whoever built the illegal half is the one told about it.
        validate(session, trade)
        trade.status = STATUS_OFFERED
    touch(trade)
    session.flush()
    return trade


def players_for(session, trade, side):
    """The chosen ``DraftPlayer`` rows for one side, in the ticked order."""
    ids = selection(trade, side)
    if not ids:
        return []
    rows = {p.id: p for p in session.query(DraftPlayer)
            .filter(DraftPlayer.id.in_(ids)).all()}
    return [rows[i] for i in ids if i in rows]


# ──────────────────────────────────────────────────────────────────────
# The rules — checked against a squad the trade has not made yet
# ──────────────────────────────────────────────────────────────────────

# The squad rules themselves live in ``draft_service`` beside ``tier_slots``
# and ``role_shortfall``: they are what a *squad* has to satisfy, not something
# trading invented, and one implementation is what stops a trade refusing
# something ``/dsquad`` calls legal. Re-exported here because this is where the
# checks below read most naturally.
tier_overflow_at = ds.tier_overflow_at
tier_overflow = ds.tier_overflow
overseas_in = ds.overseas_in


def _squad_after(session, team_id, out_players, in_players):
    """The squad a team would hold if this trade went through."""
    outgoing = {p.id for p in out_players}
    keep = [p for p in tradable_squad(session, team_id) if p.id not in outgoing]
    return keep + list(in_players)


def _check_side(session, draft, team, out_players, in_players):
    """The rule this half of the trade would break, or ``None``.

    Both squads are measured with ``draft_service.squad_health`` — the same
    count ``/dsquad`` prints — so a trade can never refuse something the
    readout calls legal, or wave through something it marks with a ⚠️.

    Every check is *relative*: a squad that is already short (a slot the clock
    passed, a cap an admin lowered afterwards) stays tradable, it just may not
    get worse. Absolute checks would lock exactly the franchises that most need
    to fix themselves out of the only tool for it. Squad size is not checked at
    all — a trade is N-for-N, so it cannot change.
    """
    before = ds.squad_health(session, draft, team.id)
    after = ds.squad_health(session, draft, team.id,
                            players=_squad_after(session, team.id,
                                                 out_players, in_players))

    cap = after["overseas_cap"]
    if after["overseas"] > cap and after["overseas"] > before["overseas"]:
        return (f"{team.name} would have {after['overseas']} overseas players "
                f"and the cap is {cap}. Send an overseas player the other way, "
                f"or take a home player back.")

    if after["overflow"] > 0 and after["overflow"] > before["overflow"]:
        band = (f"{after['overflow_tier']}-or-better"
                if after["overflow_tier"] else "top-tier")
        return (f"{team.name} would hold {after['overflow']} more {band} "
                f"player(s) than it has {band} slots. A slot takes its own "
                f"tier or below — send one of those out too, or take a lower "
                f"tier back.")

    worse = {role: need for role, need in after["shortfall"].items()
             if need > before["shortfall"].get(role, 0)}
    if worse:
        wanted = ", ".join(f"{n}× {role}" for role, n in sorted(worse.items()))
        return (f"{team.name} would be short of {wanted}. This draft sets a "
                f"minimum squad shape and a trade may not break it.")
    return None


def validate(session, trade):
    """Raise ``DraftError`` unless this offer is a legal trade for both squads."""
    draft = trade.draft
    require_open(draft)
    a_players = players_for(session, trade, "a")
    b_players = players_for(session, trade, "b")

    if not a_players or not b_players:
        raise DraftError("Both teams have to put a player in.")
    if len(a_players) != len(b_players):
        raise DraftError(
            f"A trade is player-for-player: {len(a_players)} offered against "
            f"{len(b_players)}. Squad sizes are fixed by the order sheet, so "
            f"the counts have to match.")
    if len({p.id for p in a_players}) != len(a_players) or \
            len({p.id for p in b_players}) != len(b_players):
        raise DraftError("The same player is in that offer twice.")

    # Re-read ownership at validation time: an offer can sit on screen while
    # the other trade these squads had open goes through.
    for side, players in (("a", a_players), ("b", b_players)):
        team = team_on(trade, side)
        for player in players:
            if player.picked_by_team_id != team.id:
                raise DraftError(f"{player.name} is no longer on "
                                 f"{team.name}'s squad.")

    for team, out_players, in_players in (
            (trade.team_a, a_players, b_players),
            (trade.team_b, b_players, a_players)):
        problem = _check_side(session, draft, team, out_players, in_players)
        if problem:
            raise DraftError(problem)
    return True


# ──────────────────────────────────────────────────────────────────────
# Doing it
# ──────────────────────────────────────────────────────────────────────

def execute(session, trade, *, by_tg_id=None):
    """Swap the players over. Returns ``(a_players, b_players)`` as they moved.

    ``DraftPick`` rows are deliberately **not** touched. "R1 P3 was Mumbai's
    Platinum slot, and Mumbai spent it on Bumrah" is a fact about the draft
    that stays true however many times Bumrah is traded afterwards — and it is
    also where ``tier_slots`` reads a team's quota from, which must keep
    describing the order sheet rather than the current squad.
    """
    validate(session, trade)
    a_players = players_for(session, trade, "a")
    b_players = players_for(session, trade, "b")
    now = datetime.utcnow()

    for player in a_players:
        player.picked_by_team_id = trade.team_b_id
    for player in b_players:
        player.picked_by_team_id = trade.team_a_id

    trade.status = STATUS_COMPLETED
    trade.closed_by_tg_id = by_tg_id
    trade.completed_at = now
    trade.updated_at = now
    session.flush()

    sync_league(session, trade.draft)
    return a_players, b_players


def sync_league(session, draft):
    """Reconcile the published Challenge League with the drafted squads.

    A published draft is a live Challenge League, and the league is what the
    match engine reads — so a squad change that only moved ``DraftPlayer`` rows
    would show in ``/dsquad`` and change nothing anybody plays with.
    Republishing would not fix it either: ``publish_to_league`` adds and
    updates but never removes, so a traded player would arrive at their new
    team and **stay at the old one as well**.

    All three directions, because ``/dadd`` and ``/ddrop`` produce the two a
    trade never does:

    * **moved** — the row changes ``team_id`` (a trade, or ``/dadd``);
    * **added** — a player with no row yet gets one (``/dadd`` after publish);
    * **dropped** — a pool player no longer on any squad loses their row.

    A ``ChallengePlayer`` whose name is **not in this draft's pool at all** is
    left strictly alone: an admin may have added them by hand on the Challenge
    Data page, and this is a reconcile against the draft, not a claim to own
    every row in the league.

    Does nothing for an unpublished draft; the publish step will read the
    squads as they then stand. Returns ``(moved, added, dropped)``.
    """
    if not draft.league_id:
        return (0, 0, 0)
    league_teams = (session.query(ChallengeTeam)
                    .filter(ChallengeTeam.league_id == draft.league_id).all())
    if not league_teams:
        return (0, 0, 0)
    teams_by_name = {(ct.name or "").lower(): ct for ct in league_teams}
    # One read of the league's players, keyed by the name
    # ``publish_to_league`` matches on — rather than a query per player per
    # team, which on a ten-team draft is a few hundred round trips for one swap.
    published = {}
    for cp in (session.query(ChallengePlayer)
               .filter(ChallengePlayer.team_id.in_(
                   [ct.id for ct in league_teams])).all()):
        published[(cp.name or "").lower()] = cp

    moved = added = dropped = 0
    still_drafted = set()
    for team in ds.teams(session, draft.id):
        ct = teams_by_name.get((team.name or "")[:120].lower())
        if ct is None:
            continue
        for order, player in enumerate(ds.squad_sorted(session, draft, team.id)):
            key = (player.name or "")[:150].lower()
            still_drafted.add(key)
            cp = published.get(key)
            if cp is None:
                cp = ChallengePlayer(team_id=ct.id,
                                     name=(player.name or "")[:150])
                session.add(cp)
                published[key] = cp
                added += 1
            elif cp.team_id != ct.id:
                cp.team_id = ct.id
                moved += 1
            cp.source_player_id = player.source_player_id
            cp.details_json = ds.challenge_details_json(player)
            cp.is_overseas = not bool(player.is_indian)
            cp.sort_order = order

    # Anyone in this draft's pool who is no longer on a squad loses their row.
    # Scoped to the pool on purpose — a hand-added league player is not ours
    # to delete.
    in_pool = {(name or "").lower() for (name,) in
               session.query(DraftPlayer.name)
               .filter(DraftPlayer.draft_id == draft.id).all()}
    for key, cp in list(published.items()):
        if key in still_drafted or key not in in_pool:
            continue
        session.delete(cp)
        dropped += 1

    if moved or added or dropped:
        logger.info("draft %s league re-sync: %d moved, %d added, %d dropped",
                    draft.id, moved, added, dropped)
    session.flush()
    return (moved, added, dropped)


# ──────────────────────────────────────────────────────────────────────
# Admin overrides — /dadd and /ddrop
# ──────────────────────────────────────────────────────────────────────
#
# These deliberately enforce **nothing**. An admin untangling a mess has to be
# able to pass through an illegal squad to reach a legal one — drop the extra
# keeper, then add the quick — and a gate that refuses the first half makes the
# tool useless at exactly the moment it is needed. A trade is two owners
# agreeing, so it is checked; this is an admin correcting the record, so it is
# *reported* instead: every call returns the squads it touched so the command
# can print their rule state, and every call is announced in the group and
# written to ``DraftSquadEdit``.

def _record_edit(session, draft, player, from_team, to_team, by_tg_id):
    edit = DraftSquadEdit(
        draft_id=draft.id,
        draft_player_id=player.id,
        player_name=(player.name or "")[:150],
        from_team_id=from_team.id if from_team else None,
        to_team_id=to_team.id if to_team else None,
        from_team_name=(from_team.name or "")[:120] if from_team else None,
        to_team_name=(to_team.name or "")[:120] if to_team else None,
        by_tg_id=by_tg_id,
    )
    session.add(edit)
    session.flush()
    return edit


def _holder(session, player):
    if not player.picked_by_team_id:
        return None
    return (session.query(DraftTeam)
            .filter(DraftTeam.id == player.picked_by_team_id).first())


def admin_assign(session, draft, team, player, *, by_tg_id=None):
    """Put ``player`` on ``team``, whether they were free or on another squad.

    One verb for add and move, because from the admin's side they are the same
    instruction — *this player belongs to that team now* — and making them type
    a different command depending on a state they may not have checked is a way
    to get the wrong one.
    """
    if player is None:
        raise DraftError("No such player in this draft's pool.")
    if player.draft_id != draft.id:
        raise DraftError(f"{player.name} is not in this draft's pool.")
    if team is None or team.draft_id != draft.id:
        raise DraftError("That team is not in this draft.")
    if draft.status == ds.STATUS_CANCELLED:
        raise DraftError("This draft was cancelled.")
    was = _holder(session, player)
    if was is not None and was.id == team.id:
        raise DraftError(f"{player.name} is already on {team.name}.")

    player.picked_by_team_id = team.id
    player.picked_at = player.picked_at or datetime.utcnow()
    session.flush()
    # A player who has just been given to a team must not still be sitting in
    # somebody's auto-pick wishlist for a live draft.
    ds._drop_from_queues(session, draft.id, player.id)
    edit = _record_edit(session, draft, player, was, team, by_tg_id)
    sync_league(session, draft)
    return edit, [t for t in (was, team) if t is not None]


def admin_release(session, draft, player, *, by_tg_id=None):
    """Send ``player`` back to the pool from whatever squad holds them."""
    if player is None:
        raise DraftError("No such player in this draft's pool.")
    if player.draft_id != draft.id:
        raise DraftError(f"{player.name} is not in this draft's pool.")
    if draft.status == ds.STATUS_CANCELLED:
        raise DraftError("This draft was cancelled.")
    was = _holder(session, player)
    if was is None:
        raise DraftError(f"{player.name} is already in the pool — "
                         f"nobody has them.")

    player.picked_by_team_id = None
    player.picked_at = None
    session.flush()
    edit = _record_edit(session, draft, player, was, None, by_tg_id)
    sync_league(session, draft)
    return edit, [was]


def squad_edits(session, draft, limit=10):
    return (session.query(DraftSquadEdit)
            .filter(DraftSquadEdit.draft_id == draft.id)
            .order_by(DraftSquadEdit.created_at.desc(),
                      DraftSquadEdit.id.desc())
            .limit(limit).all())


def edit_count(session, draft):
    return (session.query(DraftSquadEdit)
            .filter(DraftSquadEdit.draft_id == draft.id).count())


# ──────────────────────────────────────────────────────────────────────
# The log
# ──────────────────────────────────────────────────────────────────────

def completed_trades(session, draft, limit=10):
    return (session.query(DraftTrade)
            .filter(DraftTrade.draft_id == draft.id,
                    DraftTrade.status == STATUS_COMPLETED)
            .order_by(DraftTrade.completed_at.desc(), DraftTrade.id.desc())
            .limit(limit).all())


def trade_count(session, draft):
    return (session.query(DraftTrade)
            .filter(DraftTrade.draft_id == draft.id,
                    DraftTrade.status == STATUS_COMPLETED).count())


# ──────────────────────────────────────────────────────────────────────
# Rendering — these build HTML and escape as they go
# ──────────────────────────────────────────────────────────────────────

def total_ovr(players):
    return sum(p.rating or 0 for p in players)


def _name_line(draft, player, ticked=False):
    mark = "☑️" if ticked else "▫️"
    return (f"{mark} {ds.tier_emoji(player.tier)} "
            f"<b>{escape(player.name or '')}</b> <code>{player.rating}</code> "
            f"{ds.player_flag(player, draft)} {escape(player.category or '')}")


def render_build(session, draft, trade, side):
    """The "tick your players" card for one side."""
    team = team_on(trade, side)
    other = team_on(trade, other_side(side))
    chosen = players_for(session, trade, side)
    lines = [f"🔁 <b>TRADE</b> · {escape((trade.team_a.name or '').upper())} "
             f"⇄ {escape((trade.team_b.name or '').upper())}",
             ds.RULE]
    if side == "a":
        lines.append(f"<b>{escape(team.name or '')}</b>, tick the players you "
                     f"are sending to <b>{escape(other.name or '')}</b>.")
    else:
        wanted = len(selection(trade, "a"))
        offered = players_for(session, trade, "a")
        lines.append(f"<b>{escape(other.name or '')}</b> offers "
                     f"{_squad_line(offered)}")
        lines.append("")
        lines.append(f"<b>{escape(team.name or '')}</b>, tick "
                     f"<b>{wanted}</b> player(s) to send back — "
                     f"<i>any rating, any tier</i>.")
    lines.append("")
    if chosen:
        lines.append("Ticked: " + _squad_line(chosen))
    else:
        lines.append("<i>Nothing ticked yet.</i>")
    return "\n".join(lines)


def _squad_line(players):
    if not players:
        return "<i>nobody</i>"
    names = ", ".join(f"<b>{escape(p.name or '')}</b> "
                      f"<code>{p.rating}</code>" for p in players)
    return f"{names} ({total_ovr(players)} OVR)"


def render_offer(session, draft, trade):
    """The confirmation card both sides tap — and the case for the trade.

    The OVR line is the whole design of this command in one row. There is no
    rule stopping a franchise handing 97 OVR over for 74, so the card says what
    is happening in numbers nobody has to work out for themselves, in front of
    the group, before either tap.
    """
    a_players = players_for(session, trade, "a")
    b_players = players_for(session, trade, "b")
    a_total, b_total = total_ovr(a_players), total_ovr(b_players)
    lines = ["🔁 <b>TRADE OFFER</b>", ds.RULE,
             f"🏏 <b>{escape((trade.team_a.name or '').upper())}</b> sends:"]
    lines += [f"   • {_plain(draft, p)}" for p in a_players]
    lines.append("")
    lines.append(f"🏏 <b>{escape((trade.team_b.name or '').upper())}</b> sends:")
    lines += [f"   • {_plain(draft, p)}" for p in b_players]
    lines.append(ds.RULE)
    lines.append(f"{len(a_players)}-for-{len(b_players)} · "
                 f"{a_total} OVR ⇄ {b_total} OVR")
    lines.append(_verdict(trade, a_total, b_total))
    lines.append("")
    lines.append(_confirm_line(trade))
    return "\n".join(lines)


def _plain(draft, player):
    return (f"{ds.tier_emoji(player.tier)} <b>{escape(player.name or '')}</b> "
            f"<code>{player.rating}</code> {ds.player_flag(player, draft)} "
            f"{escape(player.category or '')}")


def _verdict(trade, a_total, b_total):
    """One line saying which way the OVR moved, and how far.

    Not a veto and not a score — a fact. An owner sending 24 OVR of talent away
    should have to read that sentence before they tap, and the rest of the
    group should be able to read it afterwards.
    """
    gap = abs(a_total - b_total)
    if gap == 0:
        return "⚖️ Dead even on paper."
    winner = trade.team_b if a_total > b_total else trade.team_a
    strength = ("a shade" if gap <= 3 else
                "clearly" if gap <= 12 else "heavily")
    return (f"📈 <b>{escape(winner.name or '')}</b> gains "
            f"<b>+{gap} OVR</b> — {strength} in their favour. "
            f"There is no rating rule here: if both owners tap, it stands.")


def _confirm_line(trade):
    waiting = [team_on(trade, side).name or "" for side in ("a", "b")
               if not confirmed(trade, side)]
    if len(waiting) == 2:
        return "Both owners must confirm."
    if not waiting:
        return "✅ Both owners confirmed."
    done = [team_on(trade, side).name or "" for side in ("a", "b")
            if confirmed(trade, side)]
    return (f"✅ {escape(done[0])} confirmed — waiting for "
            f"<b>{escape(waiting[0])}</b>.")


def render_done(session, draft, trade, a_players, b_players):
    """The announcement the whole draft group sees."""
    a_total, b_total = total_ovr(a_players), total_ovr(b_players)
    lines = ["🔁 <b>TRADE COMPLETE</b>", ds.RULE,
             f"🏏 <b>{escape((trade.team_b.name or '').upper())}</b> get:"]
    lines += [f"   • {_plain(draft, p)}" for p in a_players]
    lines.append("")
    lines.append(f"🏏 <b>{escape((trade.team_a.name or '').upper())}</b> get:")
    lines += [f"   • {_plain(draft, p)}" for p in b_players]
    lines.append(ds.RULE)
    lines.append(f"{len(a_players)}-for-{len(b_players)} · "
                 f"{a_total} OVR ⇄ {b_total} OVR")
    if draft.league_id:
        lines.append("<i>The published league squads have been updated.</i>")
    lines.append("Squads: /dsquad · Every change: /dtrades")
    return "\n".join(lines)


ACTION_LABEL = {"added": "➕ added", "released": "➖ released to the pool",
                "moved": "↔️ moved"}


def render_edit(session, draft, edit, *, by=None):
    """The group announcement for one ``/dadd`` or ``/ddrop``.

    A draft is a public event, and an admin quietly moving a player between two
    franchises is precisely how a league ends up disputed. There is no rule
    stopping the move, so the room seeing it is what stands in for one.
    """
    who = f" by {by}" if by else ""
    if edit.action == "added":
        head = (f"➕ <b>{escape(edit.player_name)}</b> added to "
                f"<b>{escape((edit.to_team_name or '').upper())}</b>")
    elif edit.action == "released":
        head = (f"➖ <b>{escape(edit.player_name)}</b> released by "
                f"<b>{escape((edit.from_team_name or '').upper())}</b> "
                f"— back in the pool")
    else:
        head = (f"↔️ <b>{escape(edit.player_name)}</b>: "
                f"<b>{escape((edit.from_team_name or '').upper())}</b> → "
                f"<b>{escape((edit.to_team_name or '').upper())}</b>")
    return [f"🛠 <b>ADMIN SQUAD EDIT</b>{who}", ds.RULE, head]


def render_edit_result(session, draft, edit, teams, *, by=None):
    """The announcement, followed by the rule state of every squad it touched.

    An override that enforces nothing has to *report* everything: the admin who
    just moved a player is the one person who can undo it, and they should not
    have to run ``/dsquad`` twice to find out what they have done.
    """
    settled = draft.status in (ds.STATUS_COMPLETED, ds.STATUS_CANCELLED)
    lines = render_edit(session, draft, edit, by=by)
    for team in teams:
        health = ds.squad_health(session, draft, team.id)
        lines.append("")
        lines.append(f"🏏 <b>{escape((team.name or '').upper())}</b>")
        lines += ds.render_squad_rules(draft, health, settled=settled)[1:]
    lines.append("")
    lines.append("Full squads: /dsquad · Every change: /dtrades")
    return "\n".join(lines)


def render_log(session, draft, limit=10):
    """Every post-draft squad change: the trades, then the admin overrides.

    Pick rows are never rewritten by either, so this is the only place a squad
    change after the draft is on the record.
    """
    rows = completed_trades(session, draft, limit=limit)
    total = trade_count(session, draft)
    edits = squad_edits(session, draft, limit=limit)
    edits_total = edit_count(session, draft)

    if not rows and not edits:
        state = ("open" if trades_open(draft) else "closed")
        return (f"🔁 <b>Squad changes</b> — none yet.\n"
                f"The trade window is <b>{state}</b>. An owner starts one with "
                f"<code>/dtrade &lt;team&gt;</code>.")

    lines = []
    if rows:
        lines.append(f"🔁 <b>Trades</b> — {total} completed")
        lines.append(ds.RULE)
        for trade in rows:
            a_players = players_for(session, trade, "a")
            b_players = players_for(session, trade, "b")
            when = (trade.completed_at.strftime("%d %b %H:%M")
                    if trade.completed_at else "")
            lines.append(f"<b>{escape(trade.team_a.name or '')}</b> ⇄ "
                         f"<b>{escape(trade.team_b.name or '')}</b>"
                         + (f" · <i>{when}</i>" if when else ""))
            lines.append(f"   → {_moved(b_players)}")
            lines.append(f"   ← {_moved(a_players)}")
        if total > len(rows):
            lines.append(f"<i>…and {total - len(rows)} earlier trade(s).</i>")

    if edits:
        if lines:
            lines.append("")
        lines.append(f"🛠 <b>Admin squad edits</b> — {edits_total}")
        lines.append(ds.RULE)
        for edit in edits:
            when = edit.created_at.strftime("%d %b %H:%M") if edit.created_at else ""
            where = {
                "added": f"→ <b>{escape(edit.to_team_name or '')}</b>",
                "released": f"← <b>{escape(edit.from_team_name or '')}</b>, "
                            f"back in the pool",
            }.get(edit.action,
                  f"<b>{escape(edit.from_team_name or '')}</b> → "
                  f"<b>{escape(edit.to_team_name or '')}</b>")
            lines.append(f"{ACTION_LABEL[edit.action].split()[0]} "
                         f"{escape(edit.player_name)} {where}"
                         + (f" · <i>{when}</i>" if when else ""))
        if edits_total > len(edits):
            lines.append(f"<i>…and {edits_total - len(edits)} earlier edit(s).</i>")
    return "\n".join(lines)


def _moved(players):
    if not players:
        return "<i>nobody</i>"
    return ", ".join(f"{escape(p.name or '')} <code>{p.rating}</code>"
                     for p in players)
