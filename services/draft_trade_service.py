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
    ChallengePlayer, ChallengeTeam, DraftPlayer, DraftTeam, DraftTrade,
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

def _tiers_in_order(draft, *groups):
    """The tier ladder, with any tier a row actually carries appended after it.

    A pool tier is normalised to the ladder on import, so the tail is normally
    empty. It exists so a hand-edited row cannot make the fit check silently
    ignore a player — an unknown tier is counted, at the bottom, for the slots
    and for the squad alike.
    """
    order = list(ds.tier_order(draft))
    seen = set(order)
    for group in groups:
        for tier in group:
            if tier not in seen:
                seen.add(tier)
                order.append(tier)
    return order


def tier_overflow_at(draft, slots, players):
    """``(overflow, tier)`` — how far a squad exceeds its slots, and where.

    A slot's tier is a **ceiling**, so the question is not "does each tier
    match" but "can these players be laid into these slots at all". With a
    nested ladder that has one answer: walk the ladder from the top, and the
    best *k* tiers of players must fit in the best *k* tiers of slots. The
    worst prefix is the overflow, and zero means the squad is one this team
    could have drafted.

    A Platinum slot spent on a Gold player leaves that slot able to house a
    Gold player for ever after — which is exactly right: the team paid Platinum
    money for it, and a trade that fills it back up with Gold is not a
    loophole, it is the slot working as specified.
    """
    by_tier = {}
    for player in players:
        by_tier[player.tier] = by_tier.get(player.tier, 0) + 1
    worst, where = 0, None
    have = held = 0
    for tier in _tiers_in_order(draft, slots.keys(), by_tier.keys()):
        held += by_tier.get(tier, 0)
        have += slots.get(tier, 0)
        if held - have > worst:
            worst, where = held - have, tier
    return max(0, worst), where


def tier_overflow(draft, slots, players):
    """How many players a squad holds that its slots could not have bought."""
    return tier_overflow_at(draft, slots, players)[0]


def overseas_in(players):
    return sum(1 for p in players if not p.is_indian)


def _squad_after(session, team_id, out_players, in_players):
    """The squad a team would hold if this trade went through."""
    outgoing = {p.id for p in out_players}
    keep = [p for p in tradable_squad(session, team_id) if p.id not in outgoing]
    return keep + list(in_players)


def _check_side(session, draft, team, out_players, in_players):
    """Raise ``DraftError`` if this half of the trade breaks that squad.

    Every check is *relative*: a squad that is already short (a slot the clock
    passed, a cap an admin lowered afterwards) stays tradable, it just may not
    get shorter. Absolute checks would lock exactly the franchises that most
    need to fix themselves out of the only tool for it.
    """
    before = tradable_squad(session, team.id)
    after = _squad_after(session, team.id, out_players, in_players)

    cap = draft.max_overseas if draft.max_overseas is not None else 11
    was, now = overseas_in(before), overseas_in(after)
    if now > cap and now > was:
        return (f"{team.name} would have {now} overseas players and the cap is "
                f"{cap}. Send an overseas player the other way, or take a home "
                f"player back.")

    slots = ds.tier_slots(session, draft.id, team.id)
    was_over, _where = tier_overflow_at(draft, slots, before)
    now_over, where = tier_overflow_at(draft, slots, after)
    if now_over > 0 and now_over > was_over:
        band = f"{where}-or-better" if where else "top-tier"
        return (f"{team.name} would hold {now_over} more {band} player(s) than "
                f"it has {band} slots. A slot takes its own tier or below — "
                f"send one of those out too, or take a lower tier back.")

    was_gap = ds.role_shortfall(draft, before)
    now_gap = ds.role_shortfall(draft, after)
    worse = {role: need for role, need in now_gap.items()
             if need > was_gap.get(role, 0)}
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
    """Move the published ``ChallengePlayer`` rows to follow a trade.

    A published draft is a live Challenge League, and the league is what the
    match engine reads — so a trade that only moved ``DraftPlayer`` rows would
    show in ``/dsquad`` and change nothing anybody plays with. Republishing
    would not fix it either: ``publish_to_league`` adds and updates, so the
    player would arrive at their new team and stay at the old one as well.

    Does nothing for an unpublished draft; the publish step will read the
    post-trade squads. Returns how many rows moved.
    """
    if not draft.league_id:
        return 0
    league_teams = (session.query(ChallengeTeam)
                    .filter(ChallengeTeam.league_id == draft.league_id).all())
    teams_by_name = {(ct.name or "").lower(): ct for ct in league_teams}
    # One read of the league's players, keyed by the name ``publish_to_league``
    # matches on — rather than a query per player per team, which on a ten-team
    # draft is a few hundred round trips for one swap.
    published = {}
    if league_teams:
        for cp in (session.query(ChallengePlayer)
                   .filter(ChallengePlayer.team_id.in_(
                       [ct.id for ct in league_teams])).all()):
            published[(cp.name or "").lower()] = cp

    moved = 0
    for team in ds.teams(session, draft.id):
        ct = teams_by_name.get((team.name or "")[:120].lower())
        if ct is None:
            continue
        for order, player in enumerate(ds.squad_sorted(session, draft, team.id)):
            cp = published.get((player.name or "")[:150].lower())
            if cp is None:
                continue
            if cp.team_id != ct.id:
                cp.team_id = ct.id
                moved += 1
            cp.sort_order = order
    if moved:
        logger.info("draft %s: %d published player(s) moved by a trade",
                    draft.id, moved)
    session.flush()
    return moved


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
    lines.append("Squads: /dsquad · Every trade: /dtrades")
    return "\n".join(lines)


def render_log(session, draft, limit=10):
    rows = completed_trades(session, draft, limit=limit)
    total = trade_count(session, draft)
    if not rows:
        state = ("open" if trades_open(draft) else "closed")
        return (f"🔁 <b>Trades</b> — none yet.\n"
                f"The window is <b>{state}</b>. An owner starts one with "
                f"<code>/dtrade &lt;team&gt;</code>.")
    lines = [f"🔁 <b>Trades</b> — {total} completed", ds.RULE]
    for trade in rows:
        a_players = players_for(session, trade, "a")
        b_players = players_for(session, trade, "b")
        when = trade.completed_at.strftime("%d %b %H:%M") if trade.completed_at else ""
        lines.append(f"<b>{escape(trade.team_a.name or '')}</b> ⇄ "
                     f"<b>{escape(trade.team_b.name or '')}</b>"
                     + (f" · <i>{when}</i>" if when else ""))
        lines.append(f"   → {_moved(b_players)}")
        lines.append(f"   ← {_moved(a_players)}")
    if total > len(rows):
        lines.append(f"<i>…and {total - len(rows)} earlier trade(s).</i>")
    return "\n".join(lines)


def _moved(players):
    if not players:
        return "<i>nobody</i>"
    return ", ".join(f"{escape(p.name or '')} <code>{p.rating}</code>"
                     for p in players)
