"""IPL-style trades between Franchise Auction franchises — ``/atrade``.

An auction squad is not finished when the hammer falls. The IPL trades in
three windows — before the auction, after it, and (since 2020) in the middle of
the season — and this is all of them, plus one the IPL does not have: a trade
**in the middle of the auction itself**, between lots, so a franchise that has
just landed its third keeper can swap one for a quick before the bowlers come
up.

The money rule is the IPL's own, and it is the whole reason this is not
``/dtrade``:

* **The contract price travels with the player.** The franchise that takes a
  player pays his auction price; the franchise that lets him go gets that much
  back. In a swap only the difference moves.
* **Cash on top is allowed.** ``cash_lakh`` is a fee either way — a sweetener
  on a swap, or the whole consideration in an **all-cash deal** (a player for
  nothing but money).
* **N-for-M.** Up to ``max_players_per_side`` players each way, and either
  side may be empty, never both. Squad size is a rule here, not a constant.

What a trade may not do is the same list every signing already obeys — the
squad maximum, the overseas cap, both ends of the role rule, the rating rules,
and the purse (with, before and during the auction, the reachability reserve
``max_bid_now`` prints) — re-checked on the squad the trade would produce.
Every check is **relative**, the way ``draft_trade_service`` reads them: a
squad already on the wrong side of a rule may still trade, it may not get
worse. Absolute checks would freeze out exactly the franchises that most need
a trade to fix themselves.

**Only the bot owner / a bot admin approves.** With ``require_approval`` on
(the default) a trade both owners accepted waits as ``pending_admin`` until a
bot admin presses Approve or Veto. Auction admins run the room; they do not
sign off squads, because a trade is permanent in a way a bid is not.

Contract, matching ``services.auction_service``: session first, **nothing here
commits**, every refusal is an ``AuctionError`` in plain text; only the
``render_*`` helpers and event headlines produce HTML, and they escape what
they interpolate.
"""

import json
import logging
from datetime import datetime, timedelta
from html import escape

from sqlalchemy import or_

from models import (
    AuctionFranchise, AuctionLedgerEntry, AuctionLot, AuctionTrade,
    AuctionTradeBlock, ChallengePlayer, ChallengeTeam,
)
from services import auction_service as A
from services import rating_rules as RR
from services.auction_service import AuctionError

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Vocabulary
# ──────────────────────────────────────────────────────────────────────

PHASE_PRE = "pre_auction"
PHASE_MID_AUCTION = "mid_auction"
PHASE_POST = "post_auction"
PHASE_MID_SEASON = "mid_season"
PHASES = (PHASE_PRE, PHASE_MID_AUCTION, PHASE_POST, PHASE_MID_SEASON)
PHASE_LABEL = {
    PHASE_PRE: "Pre-auction",
    PHASE_MID_AUCTION: "Mid-auction",
    PHASE_POST: "Post-auction",
    PHASE_MID_SEASON: "Mid-season",
}

STATUS_BUILDING = "building"
STATUS_OFFERED = "offered"
STATUS_PENDING = "pending_admin"
STATUS_COMPLETED = "completed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_VETOED = "vetoed"
STATUS_REVERSED = "reversed"
STATUS_COUNTERED = "countered"
LIVE_STATUSES = (STATUS_BUILDING, STATUS_OFFERED, STATUS_PENDING)
# Statuses the room is told about once (the sweeper's ``announced_at``).
ANNOUNCED_STATUSES = (STATUS_COMPLETED, STATUS_VETOED, STATUS_REVERSED,
                      STATUS_PENDING)

STATUS_LABEL = {
    STATUS_BUILDING: "📝 Being built",
    STATUS_OFFERED: "📨 Offered",
    STATUS_PENDING: "⏳ Waiting for the bot admin",
    STATUS_COMPLETED: "✅ Completed",
    STATUS_REJECTED: "❌ Rejected",
    STATUS_CANCELLED: "✖️ Called off",
    STATUS_EXPIRED: "⌛ Expired",
    STATUS_VETOED: "🚫 Vetoed",
    STATUS_REVERSED: "↩️ Reversed",
    STATUS_COUNTERED: "🔁 Countered",
}

LEDGER_TRADE = "trade"
LEDGER_TRADE_CASH = "trade_cash"

# How long an offer stays open. Mid-auction is a room with a clock running and
# squads moving under it, so an offer there lives minutes, not an evening.
TRADE_EXPIRES_SECONDS = 60 * 60
MID_AUCTION_EXPIRES_SECONDS = 5 * 60
# A trade waiting on the bot admin does not expire on its own: the owners have
# both agreed, and only the admin's answer should end it.

SQUAD_PAGE_SIZE = 8

DEFAULT_RULES = {
    "pre_auction": True,
    "mid_auction": True,
    "post_auction": True,
    "mid_season": True,
    "require_approval": True,
    "allow_cash": True,
    # 0 = no limit, for both.
    "max_trades_per_team": 0,
    "max_cash_lakh": 0,
    "max_players_per_side": 3,
}
BOOL_RULES = ("pre_auction", "mid_auction", "post_auction", "mid_season",
              "require_approval", "allow_cash")
INT_RULES = ("max_trades_per_team", "max_cash_lakh", "max_players_per_side")
# What each rule is called where a person types or reads it.
RULE_ALIASES = {
    "pre": "pre_auction", "preauction": "pre_auction",
    "mid": "mid_auction", "midauction": "mid_auction",
    "auction": "mid_auction",
    "post": "post_auction", "postauction": "post_auction",
    "season": "mid_season", "midseason": "mid_season",
    "approval": "require_approval", "approve": "require_approval",
    "cash": "allow_cash",
    "trades": "max_trades_per_team", "limit": "max_trades_per_team",
    "maxtrades": "max_trades_per_team",
    "maxcash": "max_cash_lakh", "cashcap": "max_cash_lakh",
    "players": "max_players_per_side", "side": "max_players_per_side",
    "maxplayers": "max_players_per_side",
}


def _e(value):
    return escape(str(value if value is not None else ""))


def _money(season, lakh):
    return A.render_money(int(lakh or 0), getattr(season, "currency_label", None) or "₹")


def _signed_money(season, lakh):
    lakh = int(lakh or 0)
    if lakh == 0:
        return _money(season, 0)
    return ("+" if lakh > 0 else "−") + _money(season, abs(lakh))


def is_bot_admin(tg_id):
    """The bot owner or a bot admin — the only people who approve trades."""
    if tg_id is None:
        return False
    from services.admin_ids import is_admin, is_owner
    try:
        return bool(is_admin(tg_id) or is_owner(tg_id))
    except Exception:
        logger.exception("bot-admin check failed")
        return False


# ──────────────────────────────────────────────────────────────────────
# The rules and the window
# ──────────────────────────────────────────────────────────────────────

def trade_rules(season):
    """The season's trade rules with every default filled in."""
    raw = {}
    try:
        raw = json.loads(getattr(season, "trade_rules_json", None) or "{}") or {}
    except (TypeError, ValueError):
        raw = {}
    rules = dict(DEFAULT_RULES)
    for key in BOOL_RULES:
        if key in raw:
            rules[key] = bool(raw[key])
    for key in INT_RULES:
        if key in raw:
            try:
                rules[key] = max(0, int(raw[key]))
            except (TypeError, ValueError):
                pass
    if rules["max_players_per_side"] <= 0:
        rules["max_players_per_side"] = DEFAULT_RULES["max_players_per_side"]
    return rules


def rule_key(name):
    key = (name or "").strip().lower().replace("-", "_")
    key = RULE_ALIASES.get(key.replace("_", ""), key)
    if key not in DEFAULT_RULES:
        raise AuctionError(
            f"“{name}” is not a trade rule. The rules are: "
            + ", ".join(sorted(DEFAULT_RULES)) + ".")
    return key


def set_trade_rules(session, season, changes, *, by_tg_id=None):
    """Apply ``{rule: value}`` and return the full rule set.

    ``require_approval`` may only be changed by the bot owner / a bot admin:
    it is the switch that decides whether they sign off trades at all, and an
    auction admin turning it off would be the one person the rule exists to
    check deciding not to be checked. The website passes ``by_tg_id=None`` and
    is trusted (its login is the bot admin's).
    """
    rules = trade_rules(season)
    for name, value in (changes or {}).items():
        key = rule_key(name)
        if key in BOOL_RULES:
            if isinstance(value, str):
                text = value.strip().lower()
                if text in ("on", "yes", "true", "1", "open"):
                    value = True
                elif text in ("off", "no", "false", "0", "closed"):
                    value = False
                else:
                    raise AuctionError(f"{key} takes on or off.")
            value = bool(value)
            if (key == "require_approval" and by_tg_id is not None
                    and value != rules[key] and not is_bot_admin(by_tg_id)):
                raise AuctionError("Only the bot owner or a bot admin can "
                                   "change whether trades need approval.")
        else:
            if key == "max_cash_lakh" and isinstance(value, str):
                text = value.strip().lower()
                value = 0 if text in ("0", "off", "none", "no") else A.parse_amount(text)
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise AuctionError(f"{key} takes a number.")
            if value < 0:
                raise AuctionError(f"{key} cannot be negative.")
            if key == "max_players_per_side" and not 1 <= value <= 6:
                raise AuctionError("A side can put in 1 to 6 players.")
        rules[key] = value
    season.trade_rules_json = json.dumps(rules, separators=(",", ":"))
    session.flush()
    return rules


def trades_switched_on(season):
    raw = getattr(season, "trades_open", 1)
    if raw is None:
        return True
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return bool(raw)


def set_window(session, season, is_open, *, by_tg_id=None):
    season.trades_open = 1 if is_open else 0
    if not is_open:
        cancel_live(session, season, reason=STATUS_CANCELLED,
                    keep_pending=True)
    A.log_event(session, season, "trade_window",
                "🔁 The trade window is now <b>"
                + ("OPEN" if is_open else "CLOSED") + "</b>.",
                by_tg_id=by_tg_id, by_admin=True)
    session.flush()
    return season


def set_deadline(session, season, *, at=None, matches=None, by_tg_id=None):
    season.trade_deadline_at = at
    season.trade_deadline_matches = (int(matches) if matches else None)
    session.flush()
    return season


def tournaments_for(session, season):
    """Every tournament played on this season's published league."""
    if not season.league_id:
        return []
    from models import Tournament
    return (session.query(Tournament)
            .filter(Tournament.league_id == season.league_id).all())


def season_progress(session, season):
    """``{"started", "league_total", "league_done", "playoffs", "live_team_ids"}``.

    ``live_team_ids`` are the ``ChallengeTeam`` ids with a match being played
    right now — a squad cannot change under a match in progress.
    """
    from models import TournamentMatch, TournamentTeam
    out = {"started": False, "league_total": 0, "league_done": 0,
           "playoffs": False, "finished": False, "live_team_ids": set()}
    tours = tournaments_for(session, season)
    if not tours:
        return out
    tour_ids = [t.id for t in tours]
    rows = (session.query(TournamentMatch)
            .filter(TournamentMatch.tournament_id.in_(tour_ids)).all())
    teams = {t.id: t.challenge_team_id for t in
             session.query(TournamentTeam)
             .filter(TournamentTeam.tournament_id.in_(tour_ids)).all()}
    for match in rows:
        played = match.status in ("completed", "live")
        if played:
            out["started"] = True
        if (match.stage or "league") == "league":
            out["league_total"] += 1
            if match.status == "completed":
                out["league_done"] += 1
        elif played:
            out["playoffs"] = True
        if match.status == "live":
            for tid in (match.team1_id, match.team2_id):
                if teams.get(tid):
                    out["live_team_ids"].add(teams[tid])
    if any((t.status or "") in ("completed", "finished") for t in tours):
        out["finished"] = True
    if (out["league_total"] and out["league_done"] >= out["league_total"]
            and not out["playoffs"]):
        # Every league match is in and the playoffs have not begun: the IPL's
        # mid-season window is long shut by then.
        out["league_over"] = True
    return out


def trade_phase(session, season, now=None):
    """``(phase, closed_reason)`` — which window the season is in, if any.

    ``phase`` is ``None`` when no window is open, with ``closed_reason`` saying
    why in a sentence a person can act on.
    """
    now = now or datetime.utcnow()
    status = season.status
    if status == A.STATUS_CANCELLED:
        return None, "This auction was cancelled."
    if status == A.STATUS_SETUP:
        return PHASE_PRE, None
    if status in (A.STATUS_LIVE, A.STATUS_PAUSED):
        return PHASE_MID_AUCTION, None
    if status != A.STATUS_COMPLETED:
        return None, "This auction is not in a trading state."
    if not season.published_at:
        return PHASE_POST, None
    progress = season_progress(session, season)
    if not progress["started"]:
        return PHASE_POST, None
    if progress["finished"]:
        return None, "The season is over — the next trade window is next season's."
    if progress["playoffs"]:
        return None, "The playoffs have started — the mid-season window is closed."
    if progress.get("league_over"):
        return None, ("Every league match has been played — the mid-season "
                      "window is closed.")
    if season.trade_deadline_at and now >= season.trade_deadline_at:
        return None, ("The mid-season trade deadline has passed ("
                      f"{season.trade_deadline_at:%d %b %H:%M} UTC).")
    if (season.trade_deadline_matches
            and progress["league_done"] >= int(season.trade_deadline_matches)):
        return None, (f"The mid-season trade deadline was after "
                      f"{season.trade_deadline_matches} league matches — "
                      f"{progress['league_done']} have been played.")
    return PHASE_MID_SEASON, None


def require_window(session, season, now=None):
    """The open phase, or an ``AuctionError`` saying why trading is shut."""
    if not trades_switched_on(season):
        raise AuctionError("The trade window is closed. An admin reopens it "
                           "with /atradewindow on.")
    phase, reason = trade_phase(session, season, now)
    if phase is None:
        raise AuctionError(reason or "No trade window is open.")
    if not trade_rules(season).get(phase, True):
        raise AuctionError(f"{PHASE_LABEL[phase]} trades are switched off for "
                           f"this auction.")
    return phase


def window_line(session, season, now=None):
    """One line for every card: is it open, which window, until when."""
    if not trades_switched_on(season):
        return "🔒 Trade window: closed"
    phase, reason = trade_phase(session, season, now)
    if phase is None:
        return f"🔒 Trade window: closed — {reason}"
    if not trade_rules(season).get(phase, True):
        return f"🔒 Trade window: {PHASE_LABEL[phase]} trades are switched off"
    line = f"🔓 Trade window: <b>{PHASE_LABEL[phase]}</b> — open"
    if phase == PHASE_MID_SEASON:
        bits = []
        if season.trade_deadline_at:
            bits.append(f"until {season.trade_deadline_at:%d %b %H:%M} UTC")
        if season.trade_deadline_matches:
            bits.append(f"until match {season.trade_deadline_matches}")
        bits.append("closes when the playoffs start")
        line += " (" + ", ".join(bits) + ")"
    return line


# ──────────────────────────────────────────────────────────────────────
# Reading an offer
# ──────────────────────────────────────────────────────────────────────

def _ids(raw):
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [int(v) for v in value if str(v).lstrip("-").isdigit()]


def _dump(ids):
    return json.dumps([int(i) for i in ids], separators=(",", ":"))


def selection(trade, side):
    return _ids(trade.lots_a_json if side == "a" else trade.lots_b_json)


def set_selection(trade, side, ids):
    if side == "a":
        trade.lots_a_json = _dump(ids)
    else:
        trade.lots_b_json = _dump(ids)
    trade.updated_at = datetime.utcnow()


def lots_for(session, trade, side):
    ids = selection(trade, side)
    if not ids:
        return []
    rows = {lot.id: lot for lot in
            session.query(AuctionLot).filter(AuctionLot.id.in_(ids)).all()}
    return [rows[i] for i in ids if i in rows]


def franchise(session, franchise_id):
    if not franchise_id:
        return None
    return (session.query(AuctionFranchise)
            .filter(AuctionFranchise.id == franchise_id).first())


def team_on(session, trade, side):
    return franchise(session, trade.team_a_id if side == "a" else trade.team_b_id)


def get_trade(session, trade_id):
    try:
        trade_id = int(trade_id)
    except (TypeError, ValueError):
        return None
    return session.query(AuctionTrade).filter(AuctionTrade.id == trade_id).first()


def side_for(trade, franchise_row):
    if franchise_row is None:
        return None
    if franchise_row.id == trade.team_a_id:
        return "a"
    if franchise_row.id == trade.team_b_id:
        return "b"
    return None


def actor_side(session, trade, tg_id):
    """Which side of this trade ``tg_id`` may act for, or ``None``."""
    for side in ("a", "b"):
        team = team_on(session, trade, side)
        if team is not None and A.may_bid_for(team, tg_id):
            return side
    return None


def lot_price(lot):
    return int(lot.sold_price_lakh or 0)


def purse_delta(session, trade):
    """``(a_delta, b_delta)`` in lakh — what this trade does to each purse.

    The IPL rule: a franchise pays the contract price of every player it takes
    and is paid back the contract price of every player it lets go; then the
    cash fee moves on top (positive ``cash_lakh`` = A pays B).
    """
    a_out = sum(lot_price(l) for l in lots_for(session, trade, "a"))
    b_out = sum(lot_price(l) for l in lots_for(session, trade, "b"))
    cash = int(trade.cash_lakh or 0)
    a_delta = a_out - b_out - cash
    return a_delta, -a_delta


def is_expired(trade, now=None):
    if trade.status not in (STATUS_BUILDING, STATUS_OFFERED):
        return False
    return trade.expires_at is not None and (now or datetime.utcnow()) >= trade.expires_at


def expire_stale(session, season, now=None):
    now = now or datetime.utcnow()
    rows = (session.query(AuctionTrade)
            .filter(AuctionTrade.season_id == season.id,
                    AuctionTrade.status.in_((STATUS_BUILDING, STATUS_OFFERED)),
                    AuctionTrade.expires_at.isnot(None),
                    AuctionTrade.expires_at <= now).all())
    for trade in rows:
        trade.status = STATUS_EXPIRED
        trade.updated_at = now
    if rows:
        session.flush()
    return len(rows)


def cancel_live(session, season, *, reason=STATUS_CANCELLED, keep_pending=False):
    statuses = ((STATUS_BUILDING, STATUS_OFFERED) if keep_pending
                else LIVE_STATUSES)
    rows = (session.query(AuctionTrade)
            .filter(AuctionTrade.season_id == season.id,
                    AuctionTrade.status.in_(statuses)).all())
    now = datetime.utcnow()
    for trade in rows:
        trade.status = reason
        trade.updated_at = now
    if rows:
        session.flush()
    return len(rows)


def live_trades(session, season):
    expire_stale(session, season)
    return (session.query(AuctionTrade)
            .filter(AuctionTrade.season_id == season.id,
                    AuctionTrade.status.in_(LIVE_STATUSES))
            .order_by(AuctionTrade.id.asc()).all())


def pending_trades(session, season):
    return (session.query(AuctionTrade)
            .filter(AuctionTrade.season_id == season.id,
                    AuctionTrade.status == STATUS_PENDING)
            .order_by(AuctionTrade.id.asc()).all())


def live_trade_for(session, season, franchise_id):
    """The newest live trade either side of which is this franchise."""
    expire_stale(session, season)
    return (session.query(AuctionTrade)
            .filter(AuctionTrade.season_id == season.id,
                    AuctionTrade.status.in_(LIVE_STATUSES),
                    or_(AuctionTrade.team_a_id == franchise_id,
                        AuctionTrade.team_b_id == franchise_id))
            .order_by(AuctionTrade.id.desc()).first())


def completed_trades(session, season, limit=None):
    query = (session.query(AuctionTrade)
             .filter(AuctionTrade.season_id == season.id,
                     AuctionTrade.status.in_((STATUS_COMPLETED, STATUS_REVERSED)))
             .order_by(AuctionTrade.id.desc()))
    return query.limit(limit).all() if limit else query.all()


def team_trade_count(session, season, franchise_id, phase=None):
    query = (session.query(AuctionTrade)
             .filter(AuctionTrade.season_id == season.id,
                     AuctionTrade.status == STATUS_COMPLETED,
                     or_(AuctionTrade.team_a_id == franchise_id,
                         AuctionTrade.team_b_id == franchise_id)))
    if phase:
        query = query.filter(AuctionTrade.phase == phase)
    return query.count()


def traded_lot_ids(session, season_id, phase=None):
    """Every lot that has moved in a completed trade (optionally, in one window)."""
    query = (session.query(AuctionTrade)
             .filter(AuctionTrade.season_id == season_id,
                     AuctionTrade.status == STATUS_COMPLETED))
    if phase:
        query = query.filter(AuctionTrade.phase == phase)
    out = set()
    for trade in query.all():
        out.update(_ids(trade.lots_a_json))
        out.update(_ids(trade.lots_b_json))
    return out


# ──────────────────────────────────────────────────────────────────────
# The rules a trade must not break
# ──────────────────────────────────────────────────────────────────────

def squad_after(session, team, out_lots, in_lots):
    outgoing = {lot.id for lot in out_lots}
    keep = [lot for lot in A.squad(session, team.id) if lot.id not in outgoing]
    return keep + list(in_lots)


def _shape(season, lots, *, building):
    """Everything a squad rule reads, for one list of lots.

    ``building`` is true before and during the auction: the squad is still
    being bought, so minimums are promises (reachability over the slots still
    open) rather than facts.
    """
    roles = {}
    for lot in lots:
        roles[lot.category] = roles.get(lot.category, 0) + 1
    size = len(lots)
    max_size = int(season.max_squad_size or 0)
    slots_left = max(0, max_size - size) if building else 0
    minimums = A.role_minimums(season)
    owed = sum(max(0, need - roles.get(role, 0)) for role, need in minimums.items())
    rules = A.rating_rules(season)
    ratings = [int(lot.rating or 0) for lot in lots]
    rating_owed = sum(owed_n for _rule, owed_n in RR.shortfalls(rules, ratings,
                                                                slots_left))
    return {
        "size": size,
        "overseas": sum(1 for lot in lots if lot.is_overseas),
        "roles": roles,
        "owed": owed,
        "owed_by_role": {role: max(0, need - roles.get(role, 0))
                         for role, need in minimums.items()},
        "unreachable": max(0, owed - slots_left) if building else owed,
        "rating_owed": rating_owed,
    }


def _reserve(season, size):
    """The base-price floor a franchise must keep for its minimum squad."""
    minimum = int(season.min_squad_size or 0)
    floor = max(0, int(season.min_base_price_lakh or 0))
    return max(0, minimum - size) * floor


def check_side(session, season, phase, team, out_lots, in_lots, delta):
    """The rule this half of the trade would break, or ``None``."""
    building = phase in (PHASE_PRE, PHASE_MID_AUCTION)
    before_lots = A.squad(session, team.id)
    after_lots = squad_after(session, team, out_lots, in_lots)
    before = _shape(season, before_lots, building=building)
    after = _shape(season, after_lots, building=building)

    purse_before = int(team.purse_remaining_lakh or 0)
    purse_after = purse_before + int(delta)
    if purse_after < 0:
        return (f"{team.name} cannot afford this — it needs "
                f"{_money(season, -delta)} and has {_money(season, purse_before)} "
                f"left in its purse.")
    if building:
        headroom_before = purse_before - _reserve(season, before["size"])
        headroom_after = purse_after - _reserve(season, after["size"])
        if headroom_after < 0 and headroom_after < headroom_before:
            return (f"{team.name} would be left with "
                    f"{_money(season, purse_after)} — not enough to fill its "
                    f"minimum squad of {season.min_squad_size} at the "
                    f"{_money(season, season.min_base_price_lakh)} base price.")

    max_size = int(season.max_squad_size or 0)
    if max_size and after["size"] > max_size and after["size"] > before["size"]:
        return (f"{team.name} would have {after['size']} players and the squad "
                f"limit is {max_size}. Send a player back the other way.")
    if not building:
        minimum = int(season.min_squad_size or 0)
        if minimum and after["size"] < minimum and after["size"] < before["size"]:
            return (f"{team.name} would drop to {after['size']} players — "
                    f"below the squad minimum of {minimum}. Take a player back.")

    cap = int(season.max_overseas or 0)
    if cap and after["overseas"] > cap and after["overseas"] > before["overseas"]:
        return (f"{team.name} would have {after['overseas']} overseas players "
                f"and the cap is {cap}.")

    for role, ceiling in A.role_maximums(season).items():
        have_after = after["roles"].get(role, 0)
        if have_after > ceiling and have_after > before["roles"].get(role, 0):
            return (f"{team.name} would have {have_after} {role}s and this "
                    f"auction allows {ceiling}.")

    if after["unreachable"] > before["unreachable"]:
        short = [role for role, need in after["owed_by_role"].items() if need]
        if building:
            return (f"{team.name} would no longer have room to meet its role "
                    f"minimums ({', '.join(sorted(short))}).")
        worse = [role for role, need in after["owed_by_role"].items()
                 if need > before["owed_by_role"].get(role, 0)]
        return (f"{team.name} would be short of "
                f"{', '.join(sorted(worse or short))} — a trade may not break "
                f"the squad's role minimums.")

    if after["rating_owed"] > before["rating_owed"]:
        line = A.rating_rule_line(season)
        return (f"{team.name} would break the rating rule"
                + (f" ({line})" if line else "") + ".")
    return None


def _mid_auction_guard(session, season, trade, a_lots, b_lots):
    lot = A.current_lot(session, season)
    if lot is None:
        return
    moving = {l.id for l in a_lots} | {l.id for l in b_lots}
    if lot.id in moving:
        raise AuctionError(f"{lot.name} is on the block — he cannot be traded.")
    parties = {trade.team_a_id, trade.team_b_id}
    if lot.current_bidder_id in parties:
        holder = franchise(session, lot.current_bidder_id)
        raise AuctionError(
            f"{holder.name if holder else 'A side of this trade'} holds the "
            f"standing bid on {lot.name}. Trade once the hammer falls — a purse "
            f"must not move under a live bid.")
    if lot.status == A.LOT_RTM_OFFERED:
        try:
            holder = A.rtm_holder(session, lot)
        except Exception:
            holder = None
        if holder is not None and holder.id in parties:
            raise AuctionError(f"{holder.name} is answering a Right To Match on "
                               f"{lot.name}. Trade once it is settled.")


def _mid_season_guard(session, season, trade):
    progress = season_progress(session, season)
    live = progress["live_team_ids"]
    if not live:
        return
    for team in (team_on(session, trade, "a"), team_on(session, trade, "b")):
        ct = _league_team(session, season, team)
        if ct is not None and ct.id in live:
            raise AuctionError(f"{team.name} is playing a match right now. "
                               f"Trade once it is over.")


def validate(session, trade, *, now=None, check_window=True):
    """Raise unless this trade is legal for both squads, right now.

    Returns the phase it would execute in.
    """
    season = trade.season
    phase = (require_window(session, season, now) if check_window
             else (trade_phase(session, season, now)[0] or trade.phase))
    rules = trade_rules(season)
    a_lots = lots_for(session, trade, "a")
    b_lots = lots_for(session, trade, "b")
    team_a = team_on(session, trade, "a")
    team_b = team_on(session, trade, "b")
    if team_a is None or team_b is None:
        raise AuctionError("One of these franchises is no longer in the auction.")
    if team_a.id == team_b.id:
        raise AuctionError("A franchise cannot trade with itself.")
    if not a_lots and not b_lots:
        raise AuctionError("Put at least one player in the trade.")
    cap = rules["max_players_per_side"]
    if len(a_lots) > cap or len(b_lots) > cap:
        raise AuctionError(f"A side may put in at most {cap} players.")
    cash = int(trade.cash_lakh or 0)
    if cash and not rules["allow_cash"]:
        raise AuctionError("Cash is switched off for trades in this auction — "
                           "players only.")
    if rules["max_cash_lakh"] and abs(cash) > rules["max_cash_lakh"]:
        raise AuctionError(f"The most cash a trade may carry is "
                           f"{_money(season, rules['max_cash_lakh'])}.")
    for side, lots, team in (("a", a_lots, team_a), ("b", b_lots, team_b)):
        for lot in lots:
            if lot.status != A.LOT_SOLD or lot.sold_to_id != team.id:
                raise AuctionError(f"{lot.name} is no longer on {team.name}'s "
                                   f"squad.")

    if phase:
        recent = traded_lot_ids(session, season.id, phase=phase)
        for lot in a_lots + b_lots:
            if lot.id in recent:
                raise AuctionError(f"{lot.name} has already been traded in this "
                                   f"window — a player moves once per window.")
        limit = rules["max_trades_per_team"]
        if limit:
            for team in (team_a, team_b):
                if team_trade_count(session, season, team.id, phase) >= limit:
                    raise AuctionError(
                        f"{team.name} has used all {limit} of its trades in "
                        f"the {PHASE_LABEL[phase].lower()} window.")

    if phase == PHASE_MID_AUCTION:
        _mid_auction_guard(session, season, trade, a_lots, b_lots)
    if phase == PHASE_MID_SEASON:
        _mid_season_guard(session, season, trade)

    a_delta, b_delta = purse_delta(session, trade)
    for team, out_lots, in_lots, delta in ((team_a, a_lots, b_lots, a_delta),
                                           (team_b, b_lots, a_lots, b_delta)):
        problem = check_side(session, season, phase, team, out_lots, in_lots,
                             delta)
        if problem:
            raise AuctionError(problem)
    return phase


# ──────────────────────────────────────────────────────────────────────
# Building and answering an offer
# ──────────────────────────────────────────────────────────────────────

def _expiry(phase, now):
    seconds = (MID_AUCTION_EXPIRES_SECONDS if phase == PHASE_MID_AUCTION
               else TRADE_EXPIRES_SECONDS)
    return now + timedelta(seconds=seconds)


def touch(trade, now=None):
    now = now or datetime.utcnow()
    trade.expires_at = _expiry(trade.phase, now)
    trade.updated_at = now


def open_trade(session, season, team_a, team_b, *, by_tg_id=None,
               chat_id=None, now=None):
    """Start building an offer from ``team_a`` to ``team_b``."""
    now = now or datetime.utcnow()
    phase = require_window(session, season, now)
    if team_a is None or team_b is None:
        raise AuctionError("Name the franchise you want to trade with.")
    if team_a.id == team_b.id:
        raise AuctionError("A franchise cannot trade with itself.")
    expire_stale(session, season, now)
    existing = (session.query(AuctionTrade)
                .filter(AuctionTrade.season_id == season.id,
                        AuctionTrade.team_a_id == team_a.id,
                        AuctionTrade.status.in_((STATUS_BUILDING, STATUS_OFFERED)))
                .first())
    if existing is not None:
        other = team_on(session, existing, "b")
        raise AuctionError(
            f"{team_a.name} already has an open offer to "
            f"{other.name if other else 'another franchise'} (trade "
            f"#{existing.id}). Finish it, or call it off with /atradecancel.")
    trade = AuctionTrade(season_id=season.id, team_a_id=team_a.id,
                         team_b_id=team_b.id, lots_a_json="[]", lots_b_json="[]",
                         cash_lakh=0, phase=phase, status=STATUS_BUILDING,
                         opened_by_tg_id=by_tg_id, chat_id=chat_id,
                         expires_at=_expiry(phase, now), created_at=now,
                         updated_at=now)
    session.add(trade)
    session.flush()
    return trade


def _require_building(trade):
    if trade.status != STATUS_BUILDING:
        raise AuctionError(f"Trade #{trade.id} is "
                           f"{STATUS_LABEL.get(trade.status, trade.status).split(' ', 1)[-1].lower()}"
                           f" — it can no longer be changed.")
    if is_expired(trade):
        raise AuctionError(f"Trade #{trade.id} has expired. Start a new one "
                           f"with /atrade.")


def toggle_lot(session, trade, lot_id):
    """Tick or untick one player, on whichever side owns him."""
    _require_building(trade)
    lot = session.query(AuctionLot).filter(AuctionLot.id == int(lot_id)).first()
    if lot is None or lot.status != A.LOT_SOLD:
        raise AuctionError("That player is not on a squad.")
    if lot.sold_to_id == trade.team_a_id:
        side = "a"
    elif lot.sold_to_id == trade.team_b_id:
        side = "b"
    else:
        raise AuctionError(f"{lot.name} is not on either squad in this trade.")
    ids = selection(trade, side)
    if lot.id in ids:
        ids.remove(lot.id)
    else:
        cap = trade_rules(trade.season)["max_players_per_side"]
        if len(ids) >= cap:
            raise AuctionError(f"A side may put in at most {cap} players.")
        ids.append(lot.id)
    set_selection(trade, side, ids)
    touch(trade)
    session.flush()
    return side, lot


def set_cash(session, trade, cash_lakh):
    """Set the cash fee. Positive: team A pays team B. Negative: B pays A."""
    _require_building(trade)
    rules = trade_rules(trade.season)
    cash_lakh = int(cash_lakh or 0)
    if cash_lakh and not rules["allow_cash"]:
        raise AuctionError("Cash is switched off for trades in this auction.")
    if rules["max_cash_lakh"] and abs(cash_lakh) > rules["max_cash_lakh"]:
        raise AuctionError(f"The most cash a trade may carry is "
                           f"{_money(trade.season, rules['max_cash_lakh'])}.")
    trade.cash_lakh = cash_lakh
    touch(trade)
    session.flush()
    return trade


def adjust_cash(session, trade, delta_lakh):
    return set_cash(session, trade, int(trade.cash_lakh or 0) + int(delta_lakh))


def set_note(session, trade, note):
    _require_building(trade)
    trade.note = (note or "").strip()[:200] or None
    session.flush()
    return trade


def send_offer(session, trade, *, by_tg_id=None, now=None):
    """Team A is done: the offer goes to team B to accept, reject or counter."""
    _require_building(trade)
    now = now or datetime.utcnow()
    validate(session, trade, now=now)
    trade.status = STATUS_OFFERED
    trade.confirmed_a_by = by_tg_id
    touch(trade, now)
    session.flush()
    return trade


def _require_offered(trade, now=None):
    if trade.status != STATUS_OFFERED:
        raise AuctionError(f"Trade #{trade.id} is not waiting for an answer — "
                           f"it is {STATUS_LABEL.get(trade.status, trade.status)}.")
    if is_expired(trade, now):
        raise AuctionError(f"Trade #{trade.id} has expired.")


def accept(session, trade, *, by_tg_id=None, now=None):
    """Team B says yes. Returns the trade — completed, or waiting for approval."""
    now = now or datetime.utcnow()
    _require_offered(trade, now)
    validate(session, trade, now=now)
    trade.confirmed_b_by = by_tg_id
    trade.updated_at = now
    if trade_rules(trade.season)["require_approval"]:
        trade.status = STATUS_PENDING
        trade.expires_at = None
        trade.announced_at = None
        trade.admin_notified_at = None
        session.flush()
        return trade
    return execute(session, trade, by_tg_id=by_tg_id, now=now)


def reject(session, trade, *, by_tg_id=None, now=None):
    _require_offered(trade, now)
    trade.status = STATUS_REJECTED
    trade.closed_by_tg_id = by_tg_id
    trade.updated_at = now or datetime.utcnow()
    session.flush()
    return trade


def cancel(session, trade, *, by_tg_id=None):
    if trade.status not in LIVE_STATUSES:
        raise AuctionError(f"Trade #{trade.id} is already "
                           f"{STATUS_LABEL.get(trade.status, trade.status)}.")
    trade.status = STATUS_CANCELLED
    trade.closed_by_tg_id = by_tg_id
    trade.updated_at = datetime.utcnow()
    session.flush()
    return trade


def counter(session, trade, *, by_tg_id=None, now=None):
    """Team B answers with its own version: a new builder, sides swapped."""
    now = now or datetime.utcnow()
    _require_offered(trade, now)
    season = trade.season
    phase = require_window(session, season, now)
    trade.status = STATUS_COUNTERED
    trade.closed_by_tg_id = by_tg_id
    trade.updated_at = now
    session.flush()
    new = AuctionTrade(season_id=trade.season_id, team_a_id=trade.team_b_id,
                       team_b_id=trade.team_a_id, lots_a_json=trade.lots_b_json,
                       lots_b_json=trade.lots_a_json,
                       cash_lakh=-int(trade.cash_lakh or 0), phase=phase,
                       status=STATUS_BUILDING, parent_trade_id=trade.id,
                       opened_by_tg_id=by_tg_id, chat_id=trade.chat_id,
                       expires_at=_expiry(phase, now), created_at=now,
                       updated_at=now)
    session.add(new)
    session.flush()
    return new


# ──────────────────────────────────────────────────────────────────────
# Doing it
# ──────────────────────────────────────────────────────────────────────

def _lot_snapshot(lot):
    return {"id": lot.id, "name": lot.name, "rating": int(lot.rating or 0),
            "category": lot.category, "overseas": bool(lot.is_overseas),
            "price": lot_price(lot)}


def _move_money(session, season, team, delta, rows, *, trade, by_tg_id):
    """Apply ``delta`` to one purse in one conditional UPDATE, then ledger it.

    ``rows`` is ``[(kind, amount, lot, note)]`` summing to ``delta``. The cache
    moves once (so a debit can never overdraw, whatever else is happening), and
    each row's ``balance_after`` is the running balance, so the ledger stays
    self-checking row by row.
    """
    delta = int(delta)
    if delta < 0:
        moved = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == team.id,
                         AuctionFranchise.purse_remaining_lakh >= -delta)
                 .update({"purse_remaining_lakh":
                          AuctionFranchise.purse_remaining_lakh + delta},
                         synchronize_session=False))
        if not moved:
            raise AuctionError(f"{team.name} can no longer afford this trade — "
                               f"nothing has moved.")
    elif delta > 0:
        (session.query(AuctionFranchise)
         .filter(AuctionFranchise.id == team.id)
         .update({"purse_remaining_lakh":
                  AuctionFranchise.purse_remaining_lakh + delta},
                 synchronize_session=False))
    session.flush()
    session.refresh(team)
    balance = int(team.purse_remaining_lakh or 0) - delta
    for kind, amount, lot, note in rows:
        if not amount:
            continue
        balance += int(amount)
        session.add(AuctionLedgerEntry(
            season_id=season.id, franchise_id=team.id, kind=kind,
            amount_lakh=int(amount), balance_after=balance,
            lot_id=(lot.id if lot is not None else None),
            player_name=(lot.name if lot is not None else None),
            note=(note or "")[:200] or None, by_tg_id=by_tg_id))


def _apply(session, season, trade, team_a, team_b, a_lots, b_lots, cash, *,
           by_tg_id, label):
    """Move the players and the money for a trade (or its reversal)."""
    for lots, src, dst in ((a_lots, team_a, team_b), (b_lots, team_b, team_a)):
        if not lots:
            continue
        moved = (session.query(AuctionLot)
                 .filter(AuctionLot.id.in_([l.id for l in lots]),
                         AuctionLot.sold_to_id == src.id,
                         AuctionLot.status == A.LOT_SOLD)
                 .update({"sold_to_id": dst.id}, synchronize_session=False))
        if moved != len(lots):
            raise AuctionError("A player in this trade has just moved — "
                               "nothing has been traded.")

    a_rows, b_rows = [], []
    for lot in a_lots:
        price = lot_price(lot)
        a_rows.append((LEDGER_TRADE, price, lot,
                       f"{label}: {lot.name} out to {team_b.name}"))
        b_rows.append((LEDGER_TRADE, -price, lot,
                       f"{label}: {lot.name} in from {team_a.name}"))
    for lot in b_lots:
        price = lot_price(lot)
        b_rows.append((LEDGER_TRADE, price, lot,
                       f"{label}: {lot.name} out to {team_a.name}"))
        a_rows.append((LEDGER_TRADE, -price, lot,
                       f"{label}: {lot.name} in from {team_b.name}"))
    if cash:
        a_rows.append((LEDGER_TRADE_CASH, -cash, None,
                       f"{label}: cash to {team_b.name}"))
        b_rows.append((LEDGER_TRADE_CASH, cash, None,
                       f"{label}: cash from {team_a.name}"))
    a_delta = sum(r[1] for r in a_rows)
    b_delta = sum(r[1] for r in b_rows)
    # Debit first: a credit that landed before a failed debit would have to be
    # unwound, and the transaction rolls back either way.
    order = sorted(((team_a, a_delta, a_rows), (team_b, b_delta, b_rows)),
                   key=lambda item: item[1])
    for team, delta, rows in order:
        _move_money(session, season, team, delta, rows, trade=trade,
                    by_tg_id=by_tg_id)

    size_a = len(b_lots) - len(a_lots)
    for team, change in ((team_a, size_a), (team_b, -size_a)):
        if change:
            (session.query(AuctionFranchise)
             .filter(AuctionFranchise.id == team.id)
             .update({"squad_size": AuctionFranchise.squad_size + change},
                     synchronize_session=False))
    session.flush()
    for row in (team_a, team_b, *a_lots, *b_lots):
        session.refresh(row)
    # A traded player comes off the trade block.
    moved_ids = [l.id for l in a_lots + b_lots]
    if moved_ids:
        (session.query(AuctionTradeBlock)
         .filter(AuctionTradeBlock.lot_id.in_(moved_ids))
         .delete(synchronize_session=False))
    return a_delta, b_delta


def execute(session, trade, *, by_tg_id=None, now=None, by_admin=False,
            check_window=True):
    """Do the trade. Returns it, completed."""
    now = now or datetime.utcnow()
    season = trade.season
    phase = validate(session, trade, now=now, check_window=check_window)
    team_a = team_on(session, trade, "a")
    team_b = team_on(session, trade, "b")
    a_lots = lots_for(session, trade, "a")
    b_lots = lots_for(session, trade, "b")
    cash = int(trade.cash_lakh or 0)
    purse_a0 = int(team_a.purse_remaining_lakh or 0)
    purse_b0 = int(team_b.purse_remaining_lakh or 0)
    snap_a = [_lot_snapshot(l) for l in a_lots]
    snap_b = [_lot_snapshot(l) for l in b_lots]
    verdict = fairness(session, trade)

    a_delta, b_delta = _apply(session, season, trade, team_a, team_b, a_lots,
                              b_lots, cash, by_tg_id=by_tg_id,
                              label=f"Trade #{trade.id}")
    trade.phase = phase or trade.phase
    trade.status = STATUS_COMPLETED
    trade.completed_at = now
    trade.updated_at = now
    trade.closed_by_tg_id = by_tg_id
    trade.by_admin = bool(by_admin or trade.by_admin)
    trade.announced_at = None
    trade.snapshot_json = json.dumps({
        "a": snap_a, "b": snap_b, "cash": cash,
        "a_delta": a_delta, "b_delta": b_delta,
        "a_purse": [purse_a0, int(team_a.purse_remaining_lakh or 0)],
        "b_purse": [purse_b0, int(team_b.purse_remaining_lakh or 0)],
        "phase": trade.phase, "verdict": verdict["verdict"],
    }, separators=(",", ":"))
    session.flush()

    moves = ([(lot, team_a, team_b) for lot in a_lots]
             + [(lot, team_b, team_a) for lot in b_lots])
    sync_league(session, season, moves)
    A.log_event(session, season, "trade",
                trade_headline(session, season, trade),
                franchise=team_a, by_tg_id=by_tg_id, by_admin=trade.by_admin,
                detail={"trade_id": trade.id, "phase": trade.phase})
    return trade


def approve(session, trade, *, by_tg_id=None, by_web=False, now=None):
    """The bot owner / a bot admin signs a pending trade off."""
    if not by_web and not is_bot_admin(by_tg_id):
        raise AuctionError("Only the bot owner or a bot admin can approve trades.")
    if trade.status != STATUS_PENDING:
        raise AuctionError(f"Trade #{trade.id} is not waiting for approval — it "
                           f"is {STATUS_LABEL.get(trade.status, trade.status)}.")
    trade.approved_by_tg_id = by_tg_id
    return execute(session, trade, by_tg_id=by_tg_id, now=now)


def veto(session, trade, *, by_tg_id=None, by_web=False, reason=None):
    if not by_web and not is_bot_admin(by_tg_id):
        raise AuctionError("Only the bot owner or a bot admin can veto trades.")
    if trade.status not in LIVE_STATUSES:
        raise AuctionError(f"Trade #{trade.id} is already "
                           f"{STATUS_LABEL.get(trade.status, trade.status)}.")
    trade.status = STATUS_VETOED
    trade.closed_by_tg_id = by_tg_id
    trade.reason = (reason or "").strip()[:200] or None
    trade.updated_at = datetime.utcnow()
    trade.announced_at = None
    session.flush()
    return trade


def reverse(session, trade, *, by_tg_id=None, by_web=False, now=None):
    """Undo a completed trade exactly — players back, money back.

    Refused once any player in it has moved again (traded on, or the sale
    undone), because "put it back how it was" no longer has one meaning.
    """
    if not by_web and not is_bot_admin(by_tg_id):
        raise AuctionError("Only the bot owner or a bot admin can undo a trade.")
    if trade.status != STATUS_COMPLETED:
        raise AuctionError(f"Trade #{trade.id} is not a completed trade.")
    now = now or datetime.utcnow()
    season = trade.season
    team_a = team_on(session, trade, "a")
    team_b = team_on(session, trade, "b")
    a_lots = lots_for(session, trade, "a")   # now on team B
    b_lots = lots_for(session, trade, "b")   # now on team A
    for lot in a_lots:
        if lot.status != A.LOT_SOLD or lot.sold_to_id != team_b.id:
            raise AuctionError(f"{lot.name} has moved since this trade — it "
                               f"cannot be undone in one step.")
    for lot in b_lots:
        if lot.status != A.LOT_SOLD or lot.sold_to_id != team_a.id:
            raise AuctionError(f"{lot.name} has moved since this trade — it "
                               f"cannot be undone in one step.")
    a_delta, b_delta = purse_delta(session, trade)
    for team, delta in ((team_a, a_delta), (team_b, b_delta)):
        if int(team.purse_remaining_lakh or 0) - delta < 0:
            raise AuctionError(f"{team.name} has spent the money this trade "
                               f"gave it — it cannot be undone.")
    # The reversal is the same trade with the sides swapped: B sends A's
    # players back, A sends B's back, and the cash returns.
    # The cash goes back the way it came: B (now the paying side) returns
    # exactly what A paid it.
    _apply(session, season, trade, team_b, team_a, a_lots, b_lots,
           int(trade.cash_lakh or 0),
           by_tg_id=by_tg_id, label=f"Undo trade #{trade.id}")
    trade.status = STATUS_REVERSED
    trade.closed_by_tg_id = by_tg_id
    trade.updated_at = now
    trade.announced_at = None
    session.flush()
    moves = ([(lot, team_b, team_a) for lot in a_lots]
             + [(lot, team_a, team_b) for lot in b_lots])
    sync_league(session, season, moves)
    A.log_event(session, season, "trade_reversed",
                f"↩️ Trade #{trade.id} between <b>{_e(team_a.name)}</b> and "
                f"<b>{_e(team_b.name)}</b> has been undone by the bot admin.",
                by_tg_id=by_tg_id, by_admin=True,
                detail={"trade_id": trade.id})
    return trade


def void_for_restart(session, season, *, by_tg_id=None):
    """``/arestart`` puts every auction lot back in the pool: undo the cash of
    every mid-auction trade with it, and close those trades as reversed.

    The players need nothing here — the restart refunds each lot's price to
    whoever holds it, which is exactly the price a trade moved with him.
    """
    rows = (session.query(AuctionTrade)
            .filter(AuctionTrade.season_id == season.id,
                    AuctionTrade.phase == PHASE_MID_AUCTION,
                    AuctionTrade.status == STATUS_COMPLETED).all())
    for trade in rows:
        cash = int(trade.cash_lakh or 0)
        team_a = team_on(session, trade, "a")
        team_b = team_on(session, trade, "b")
        if cash and team_a is not None and team_b is not None:
            label = f"Restart undoes trade #{trade.id}"
            for team, delta, note in (
                    (team_a, cash, f"{label}: cash back from {team_b.name}"),
                    (team_b, -cash, f"{label}: cash back to {team_a.name}")):
                team.purse_remaining_lakh = int(team.purse_remaining_lakh or 0) + delta
                session.add(AuctionLedgerEntry(
                    season_id=season.id, franchise_id=team.id,
                    kind=LEDGER_TRADE_CASH, amount_lakh=delta,
                    balance_after=int(team.purse_remaining_lakh),
                    note=note[:200], by_tg_id=by_tg_id))
        trade.status = STATUS_REVERSED
        trade.reason = "The auction was restarted"
        trade.announced_at = datetime.utcnow()
    for trade in (session.query(AuctionTrade)
                  .filter(AuctionTrade.season_id == season.id,
                          AuctionTrade.status.in_(LIVE_STATUSES)).all()):
        trade.status = STATUS_CANCELLED
        trade.reason = "The auction was restarted"
    session.flush()
    return len(rows)


def admin_trade(session, season, team_a, team_b, a_lots, b_lots, cash_lakh=0,
                *, by_tg_id=None, by_web=False, note=None, now=None):
    """A trade the bot admin puts through directly (the website's form).

    Every squad rule still applies; the window does not, because the admin is
    the person who decides when the window is.
    """
    if not by_web and not is_bot_admin(by_tg_id):
        raise AuctionError("Only the bot owner or a bot admin can force a trade.")
    now = now or datetime.utcnow()
    if team_a is None or team_b is None or team_a.id == team_b.id:
        raise AuctionError("Pick two different franchises.")
    phase = trade_phase(session, season, now)[0] or PHASE_POST
    trade = AuctionTrade(season_id=season.id, team_a_id=team_a.id,
                         team_b_id=team_b.id,
                         lots_a_json=_dump([l.id for l in a_lots]),
                         lots_b_json=_dump([l.id for l in b_lots]),
                         cash_lakh=int(cash_lakh or 0), phase=phase,
                         status=STATUS_PENDING, opened_by_tg_id=by_tg_id,
                         note=(note or None), by_admin=True,
                         created_at=now, updated_at=now)
    session.add(trade)
    session.flush()
    trade.approved_by_tg_id = by_tg_id
    return execute(session, trade, by_tg_id=by_tg_id, now=now, by_admin=True,
                   check_window=False)


# ──────────────────────────────────────────────────────────────────────
# The published league
# ──────────────────────────────────────────────────────────────────────

def _league_team(session, season, team):
    if not season.league_id or team is None:
        return None
    return (session.query(ChallengeTeam)
            .filter(ChallengeTeam.league_id == season.league_id,
                    ChallengeTeam.name == (team.name or "")[:120]).first())


def sync_league(session, season, moves):
    """Carry a trade into the published Challenge League, if there is one.

    The ``ChallengePlayer`` row **moves** to its new team rather than being
    re-created there: tournament stats are keyed on that row's id
    (``TournamentPlayerStats.roster_id``), so a traded batter keeps his runs.
    A saved "last XI" for either team that names a player who has left is
    dropped, so the picker never offers somebody who is no longer there.
    Returns how many rows moved.
    """
    if not season.league_id or not season.published_at or not moves:
        return 0
    from services import player_query
    moved = 0
    touched = set()
    for lot, src, dst in moves:
        src_ct = _league_team(session, season, src)
        dst_ct = _league_team(session, season, dst)
        if dst_ct is None:
            continue
        key = (lot.name or "")[:150]

        def row_on(ct):
            # Name AND card: two editions of one player share a name.
            query = (session.query(ChallengePlayer)
                     .filter(ChallengePlayer.team_id == ct.id,
                             ChallengePlayer.name == key))
            if lot.player_id:
                query = query.filter(or_(
                    ChallengePlayer.source_player_id == lot.player_id,
                    ChallengePlayer.source_player_id.is_(None)))
            return query.first()
        cp = row_on(src_ct) if src_ct is not None else None
        already = row_on(dst_ct)
        if cp is not None and already is not None and already.id != cp.id:
            session.delete(already)
            session.flush()
        if cp is None:
            cp = already
        if cp is None:
            cp = ChallengePlayer(team_id=dst_ct.id, name=key)
            session.add(cp)
        cp.team_id = dst_ct.id
        cp.source_player_id = lot.player_id
        cp.is_overseas = bool(lot.is_overseas)
        cp.details_json = player_query.challenge_details_json(
            lot, is_overseas=bool(lot.is_overseas),
            source_player_id=lot.player_id,
            extra={"auction_price_lakh": lot.sold_price_lakh,
                   "auction_lot_no": lot.lot_no,
                   "acquisition": "trade",
                   "traded_from": src.name if src is not None else None})
        moved += 1
        touched.update(ct.id for ct in (src_ct, dst_ct) if ct is not None)
    session.flush()
    if touched:
        try:
            from models import UserTeamLastXI
            (session.query(UserTeamLastXI)
             .filter(UserTeamLastXI.team_id.in_(list(touched)))
             .delete(synchronize_session=False))
            session.flush()
        except Exception:
            logger.exception("trade: clearing saved XIs failed (non-fatal)")
    return moved


# ──────────────────────────────────────────────────────────────────────
# The trade block
# ──────────────────────────────────────────────────────────────────────

def block_add(session, season, team, lot, *, note=None, by_tg_id=None):
    if lot.status != A.LOT_SOLD or lot.sold_to_id != team.id:
        raise AuctionError(f"{lot.name} is not on {team.name}'s squad.")
    row = (session.query(AuctionTradeBlock)
           .filter(AuctionTradeBlock.season_id == season.id,
                   AuctionTradeBlock.lot_id == lot.id).first())
    if row is None:
        row = AuctionTradeBlock(season_id=season.id, franchise_id=team.id,
                                lot_id=lot.id, by_tg_id=by_tg_id)
        session.add(row)
    row.asking_note = (note or "").strip()[:120] or None
    session.flush()
    return row


def block_remove(session, season, team, lot):
    gone = (session.query(AuctionTradeBlock)
            .filter(AuctionTradeBlock.season_id == season.id,
                    AuctionTradeBlock.franchise_id == team.id,
                    AuctionTradeBlock.lot_id == lot.id)
            .delete(synchronize_session=False))
    session.flush()
    if not gone:
        raise AuctionError(f"{lot.name} is not on the trade block.")
    return gone


def block_list(session, season):
    """``[(row, lot, franchise)]`` for every player still on the block."""
    out = []
    rows = (session.query(AuctionTradeBlock)
            .filter(AuctionTradeBlock.season_id == season.id)
            .order_by(AuctionTradeBlock.id.asc()).all())
    for row in rows:
        lot = session.query(AuctionLot).filter(AuctionLot.id == row.lot_id).first()
        team = franchise(session, row.franchise_id)
        if lot is None or team is None or lot.sold_to_id != team.id:
            session.delete(row)
            continue
        out.append((row, lot, team))
    session.flush()
    return out


# ──────────────────────────────────────────────────────────────────────
# Fairness — daylight, not a rule
# ──────────────────────────────────────────────────────────────────────

def fairness(session, trade):
    """Both sides' OVR and money, and a one-line verdict on who it favours.

    It refuses nothing. A lopsided trade is allowed; it is just never quiet.
    """
    a_lots = lots_for(session, trade, "a")
    b_lots = lots_for(session, trade, "b")
    team_a = team_on(session, trade, "a")
    team_b = team_on(session, trade, "b")
    season = trade.season
    a_ovr = sum(int(l.rating or 0) for l in a_lots)
    b_ovr = sum(int(l.rating or 0) for l in b_lots)
    a_best = max([int(l.rating or 0) for l in a_lots] or [0])
    b_best = max([int(l.rating or 0) for l in b_lots] or [0])
    a_delta, b_delta = purse_delta(session, trade)
    # Talent A gains, weighted toward the best player in the deal — one 95 is
    # worth more than two 80s, which a plain sum would not say.
    talent = (b_ovr - a_ovr) + 2 * (b_best - a_best)
    name_a = team_a.name if team_a else "Team A"
    name_b = team_b.name if team_b else "Team B"
    if abs(talent) <= 4:
        verdict = "⚖️ An even trade on talent"
    elif talent > 0:
        verdict = f"📈 Favours {name_a} on talent"
    else:
        verdict = f"📈 Favours {name_b} on talent"
    if a_delta:
        richer = name_a if a_delta > 0 else name_b
        verdict += f" · {richer} bank {_money(season, abs(a_delta))}"
    return {"a_ovr": a_ovr, "b_ovr": b_ovr, "a_delta": a_delta,
            "b_delta": b_delta, "talent": talent, "verdict": verdict}


# ──────────────────────────────────────────────────────────────────────
# Rendering — HTML, escaped
# ──────────────────────────────────────────────────────────────────────

ROLE_SHORT = {"Batsman": "BAT", "Bowler": "BOWL", "All-rounder": "AR",
              "All Rounder": "AR", "Wicket Keeper": "WK"}


def lot_line(season, lot, *, ticked=None):
    mark = "" if ticked is None else ("☑️ " if ticked else "▫️ ")
    flag = " ✈️" if lot.is_overseas else ""
    role = ROLE_SHORT.get(lot.category, (lot.category or "")[:4].upper())
    return (f"{mark}{_e(lot.name)} — {role} {int(lot.rating or 0)}{flag} · "
            f"{_money(season, lot_price(lot))}")


def _side_block(season, team, lots):
    if not lots:
        return f"<b>{_e(team.name)}</b> send: <i>no players</i>"
    lines = [f"<b>{_e(team.name)}</b> send:"]
    lines += [f"  • {lot_line(season, lot)}" for lot in lots]
    return "\n".join(lines)


def _cash_line(season, trade, team_a, team_b):
    cash = int(trade.cash_lakh or 0)
    if not cash:
        return "💵 No cash on top"
    payer, payee = (team_a, team_b) if cash > 0 else (team_b, team_a)
    return (f"💵 <b>{_e(payer.name)}</b> add {_money(season, abs(cash))} "
            f"cash to {_e(payee.name)}")


def _purse_lines(session, season, trade, team_a, team_b):
    a_delta, b_delta = purse_delta(session, trade)
    out = []
    for team, delta in ((team_a, a_delta), (team_b, b_delta)):
        now_ = int(team.purse_remaining_lakh or 0)
        out.append(f"💰 {_e(team.name)}: {_money(season, now_)} → "
                   f"<b>{_money(season, now_ + delta)}</b> ({_signed_money(season, delta)})")
    return out


def render_offer(session, season, trade, *, viewer_side=None):
    team_a = team_on(session, trade, "a")
    team_b = team_on(session, trade, "b")
    a_lots = lots_for(session, trade, "a")
    b_lots = lots_for(session, trade, "b")
    title = "TRADE OFFER" if trade.status != STATUS_BUILDING else "TRADE BUILDER"
    lines = [f"🔁 <b>{title}</b> #{trade.id} · "
             f"{PHASE_LABEL.get(trade.phase, 'Trade')} window"]
    if trade.parent_trade_id:
        lines.append(f"<i>A counter-offer to trade #{trade.parent_trade_id}</i>")
    lines += ["", _side_block(season, team_a, a_lots), "",
              _side_block(season, team_b, b_lots), "",
              _cash_line(season, trade, team_a, team_b)]
    if trade.status in LIVE_STATUSES:
        lines += _purse_lines(session, season, trade, team_a, team_b)
    if a_lots or b_lots:
        lines.append(fairness(session, trade)["verdict"])
    if trade.note:
        lines.append(f"📝 {_e(trade.note)}")
    lines.append("")
    if trade.status == STATUS_BUILDING:
        lines.append(f"✏️ <b>{_e(team_a.name)}</b> is building this offer — tick "
                     f"players on either squad, set the cash, then 📤 Send.")
    elif trade.status == STATUS_OFFERED:
        lines.append(f"⏳ Waiting for <b>{_e(team_b.name)}</b> — ✅ Accept, "
                     f"❌ Reject or 🔁 Counter.")
    elif trade.status == STATUS_PENDING:
        lines.append("⏳ Both franchises agreed. Waiting for the <b>bot "
                     "admin</b> to ✅ Approve or 🚫 Veto.")
    else:
        lines.append(STATUS_LABEL.get(trade.status, trade.status)
                     + (f" — {_e(trade.reason)}" if trade.reason else ""))
    if trade.status in (STATUS_BUILDING, STATUS_OFFERED) and trade.expires_at:
        left = int((trade.expires_at - datetime.utcnow()).total_seconds())
        if left > 0:
            lines.append(f"⌛ Expires in {max(1, left // 60)} min")
    return "\n".join(lines)


def trade_headline(session, season, trade):
    """The one-paragraph announcement (fits an AuctionEvent headline)."""
    snap = {}
    try:
        snap = json.loads(trade.snapshot_json or "{}")
    except (TypeError, ValueError):
        snap = {}
    team_a = team_on(session, trade, "a")
    team_b = team_on(session, trade, "b")

    def names(rows):
        return ", ".join(_e(r["name"]) for r in rows) or "cash"
    text = (f"🔁 <b>TRADE</b> — {_e(team_a.name)} get "
            f"{names(snap.get('b', []))}; {_e(team_b.name)} get "
            f"{names(snap.get('a', []))}.")
    return text[:300]


def render_done(session, season, trade):
    """The full "TRADE COMPLETED" card the room is shown."""
    snap = {}
    try:
        snap = json.loads(trade.snapshot_json or "{}")
    except (TypeError, ValueError):
        snap = {}
    team_a = team_on(session, trade, "a")
    team_b = team_on(session, trade, "b")
    if trade.status == STATUS_VETOED:
        return (f"🚫 <b>TRADE VETOED</b> #{trade.id}\n"
                f"{_e(team_a.name)} ⇄ {_e(team_b.name)} will not go through — "
                f"the bot admin vetoed it"
                + (f": {_e(trade.reason)}" if trade.reason else ".")
                + "\n\n" + render_offer(session, season, trade).split("\n", 1)[-1])
    if trade.status == STATUS_REVERSED:
        return (f"↩️ <b>TRADE UNDONE</b> #{trade.id}\n"
                f"{_e(team_a.name)} ⇄ {_e(team_b.name)}: every player and every "
                f"rupee is back where it was.")
    if trade.status == STATUS_PENDING:
        return ("⏳ <b>TRADE AGREED</b> — waiting for the bot admin\n\n"
                + render_offer(session, season, trade))

    def block(team, rows):
        if not rows:
            return f"<b>{_e(team.name)}</b> receive: <i>no players</i>"
        out = [f"<b>{_e(team.name)}</b> receive:"]
        for r in rows:
            role = ROLE_SHORT.get(r.get("category"), (r.get("category") or "")[:4].upper())
            out.append(f"  ➕ {_e(r['name'])} — {role} {r['rating']}"
                       f"{' ✈️' if r.get('overseas') else ''} · "
                       f"{_money(season, r['price'])}")
        return "\n".join(out)

    lines = [f"🔁 <b>TRADE COMPLETED</b> #{trade.id} · "
             f"{PHASE_LABEL.get(trade.phase, 'Trade')} window"
             + (" · by the bot admin" if trade.by_admin else ""),
             "", block(team_a, snap.get("b", [])), "",
             block(team_b, snap.get("a", [])), ""]
    cash = int(snap.get("cash") or 0)
    if cash:
        payer, payee = (team_a, team_b) if cash > 0 else (team_b, team_a)
        lines.append(f"💵 {_e(payer.name)} paid {_money(season, abs(cash))} "
                     f"cash to {_e(payee.name)}")
    for team, key, delta_key in ((team_a, "a_purse", "a_delta"),
                                 (team_b, "b_purse", "b_delta")):
        before, after = (snap.get(key) or [0, 0])[:2]
        lines.append(f"💰 {_e(team.name)}: {_money(season, before)} → "
                     f"<b>{_money(season, after)}</b> "
                     f"({_signed_money(season, snap.get(delta_key, 0))})")
    if snap.get("verdict"):
        lines.append(_e(snap["verdict"]))
    if trade.note:
        lines.append(f"📝 {_e(trade.note)}")
    return "\n".join(lines)


def render_log(session, season, limit=10):
    lines = ["🔁 <b>Trades</b> — " + _e(season.name), window_line(session, season)]
    live = live_trades(session, season)
    if live:
        lines += ["", "<b>Open</b>"]
        for trade in live:
            a = team_on(session, trade, "a")
            b = team_on(session, trade, "b")
            lines.append(f"#{trade.id} {_e(a.name)} → {_e(b.name)} · "
                         f"{STATUS_LABEL.get(trade.status, trade.status)}")
    done = completed_trades(session, season, limit=limit)
    lines += ["", "<b>Completed</b>"]
    if not done:
        lines.append("<i>No trades yet.</i>")
    for trade in done:
        lines.append(_log_line(session, season, trade))
    return "\n".join(lines)


def _log_line(session, season, trade):
    snap = {}
    try:
        snap = json.loads(trade.snapshot_json or "{}")
    except (TypeError, ValueError):
        pass
    a = team_on(session, trade, "a")
    b = team_on(session, trade, "b")
    a_names = ", ".join(_e(r["name"]) for r in snap.get("a", [])) or "—"
    b_names = ", ".join(_e(r["name"]) for r in snap.get("b", [])) or "—"
    cash = int(snap.get("cash") or 0)
    tail = f" + {_money(season, abs(cash))} cash" if cash else ""
    mark = " ↩️ undone" if trade.status == STATUS_REVERSED else ""
    when = trade.completed_at.strftime("%d %b") if trade.completed_at else ""
    return (f"#{trade.id} {when} · {PHASE_LABEL.get(trade.phase, '')}: "
            f"<b>{_e(a.name)}</b> send {a_names} ⇄ <b>{_e(b.name)}</b> send "
            f"{b_names}{tail}{mark}")


def render_block(session, season):
    rows = block_list(session, season)
    lines = ["🏷 <b>Trade block</b> — players their franchise will listen to "
             "offers for", ""]
    if not rows:
        lines.append("<i>Nobody is on the block. An owner lists a player with "
                     "/atradeblock add &lt;player&gt;.</i>")
    by_team = {}
    for row, lot, team in rows:
        by_team.setdefault(team.name, []).append((row, lot))
    for name, items in by_team.items():
        lines.append(f"<b>{_e(name)}</b>")
        for row, lot in items:
            lines.append(f"  • {lot_line(season, lot)}"
                         + (f" — <i>{_e(row.asking_note)}</i>" if row.asking_note else ""))
    lines += ["", "Make an offer with <code>/atrade &lt;franchise&gt;</code>."]
    return "\n".join(lines)


def render_rules(session, season):
    rules = trade_rules(season)

    def onoff(v):
        return "✅ on" if v else "❌ off"
    cap = rules["max_trades_per_team"]
    cash_cap = rules["max_cash_lakh"]
    lines = [
        "🔁 <b>Trade rules</b> — " + _e(season.name),
        window_line(session, season), "",
        f"Pre-auction trades: {onoff(rules['pre_auction'])}",
        f"Mid-auction trades: {onoff(rules['mid_auction'])}",
        f"Post-auction trades: {onoff(rules['post_auction'])}",
        f"Mid-season trades: {onoff(rules['mid_season'])}",
        f"Bot admin approval: {onoff(rules['require_approval'])}",
        f"Cash in trades: {onoff(rules['allow_cash'])}"
        + (f" (max {_money(season, cash_cap)})" if cash_cap else ""),
        f"Players per side: up to {rules['max_players_per_side']}",
        "Trades per franchise per window: "
        + (str(cap) if cap else "no limit"),
    ]
    if season.trade_deadline_at or season.trade_deadline_matches:
        bits = []
        if season.trade_deadline_at:
            bits.append(f"{season.trade_deadline_at:%d %b %Y %H:%M} UTC")
        if season.trade_deadline_matches:
            bits.append(f"after {season.trade_deadline_matches} league matches")
        lines.append("Mid-season deadline: " + " or ".join(bits))
    lines += ["",
              "<b>How the money works (IPL rule)</b>: the team that takes a "
              "player pays his auction price, the team that lets him go gets "
              "it back, and any cash moves on top. A player moves once per "
              "window, and the squad limit, overseas cap, role and rating "
              "rules all still apply."]
    return "\n".join(lines)
