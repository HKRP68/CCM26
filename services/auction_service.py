"""Franchise Auction — a season's squads, bought lot by lot in a live room.

The Tournament Draft (``services/draft_service.py``) already answers "how does
a Challenge League get its squads?" one way: an admin uploads a pick order and
owners take turns. An auction answers it the other way — every franchise starts
with a purse, every player has a base price, and the room decides what each one
is worth. The two share a great deal of shape (a bound group, franchises with
owners and co-owners, a restart-safe clock, a one-way publish into a real
``ChallengeLeague``) and almost nothing of their rules.

What is genuinely new here is money:

  • a **purse** per franchise, and a **ledger** where every movement of it is a
    row — the auction's audit trail and the thing a dispute is settled from;
  • a **base price** per lot, resolved from a rating ladder at pool-build time
    and then frozen, so editing the ladder never moves a price a lot already
    went on the block at;
  • a **bidding loop** with an anti-snipe rule, where two people typing at once
    is the normal case rather than the edge case.

**Amounts are integer lakh, everywhere.** ₹1.5 Cr is ``150``; ₹100 Cr is
``10_000``. There is no float in any column, any comparison, or any arithmetic
below — the only decimal in the feature lives inside ``parse_amount`` for the
half-second it takes to turn what somebody typed into lakh. It is a value unit
of its own with no relationship to the coin economy in ``config.BUY_VALUES``.

Contract, matching ``services.draft_service``: every function takes the session
first, **nothing here commits**, and every refusal is an ``AuctionError``
carrying **plain text** (never HTML) written for whoever triggered it.

Two things here do produce HTML, and both escape what they interpolate: the
``render_*`` helpers at the bottom, and the ``headline`` on every
``AuctionEvent`` — because a headline *is* the sentence the group is sent, and
writing it once at the point the thing happened is what stops the announcement
and the website's log describing the same event differently.
"""

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import escape

from sqlalchemy import and_, case, func, or_

from models import (
    AuctionBid, AuctionEvent, AuctionFranchise, AuctionLedgerEntry, AuctionLot,
    AuctionSeason, ChallengeLeague, ChallengeMode, ChallengePlayer,
    ChallengeTeam,
)
from services import player_query

logger = logging.getLogger(__name__)


class AuctionError(Exception):
    """A refusal written for whoever typed the command.

    Plain text, never HTML — it quotes player and franchise names, which are
    user-supplied. Callers escape it once, when they render it.
    """


# ── Lifecycle ─────────────────────────────────────────────────────────
STATUS_SETUP = "setup"
STATUS_LIVE = "live"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

_STATUS_LABEL = {
    STATUS_SETUP: "📝 Setup",
    STATUS_LIVE: "🟢 Live",
    STATUS_PAUSED: "⏸ Paused",
    STATUS_COMPLETED: "🏁 Completed",
    STATUS_CANCELLED: "🚫 Cancelled",
}

# ── Lot states ────────────────────────────────────────────────────────
LOT_QUEUED = "queued"
LOT_ON_BLOCK = "on_block"
LOT_SOLD = "sold"
LOT_UNSOLD = "unsold"
LOT_WITHDRAWN = "withdrawn"
# A lot mid-Right-To-Match: neither on the block nor sold. Unlike a retention —
# which rides on ``sold`` precisely because every reader of ``sold`` wants it —
# this one has to be its own status, because almost every reader wants the
# opposite: no third-party bids, no sale on the clock, a different board.
#
# ``current_lot`` deliberately returns a lot in this state, which is what makes
# the six existing ``status != LOT_ON_BLOCK`` guards (extend_timer, validate_bid,
# sell_lot, pass_lot, undo_last_bid, resolve_expired) refuse during an RTM
# window for free, with messages that are already right.
LOT_RTM_OFFERED = "rtm_offered"

# A lot in one of these has left the pool for good unless an admin re-lists it.
LOT_RESOLVED = (LOT_SOLD, LOT_UNSOLD, LOT_WITHDRAWN)
# The lot is in front of the room, one way or another.
LOT_LIVE = (LOT_ON_BLOCK, LOT_RTM_OFFERED)

# The three questions an RTM asks, in order. Each has its own window on
# ``AuctionLot.deadline_at``, and each times out to the SAFE default —
# decline, stand, decline — so a silent owner can never wedge an auction.
RTM_INTENT = "intent"            # the holder: do you want to exercise it?
RTM_FINAL_OFFER = "final_offer"  # the top bidder: one more raise, or stand?
RTM_DECISION = "decision"        # the holder: match that number, or let it go?

# ── Ledger kinds ──────────────────────────────────────────────────────
LEDGER_OPENING = "opening"
LEDGER_PURCHASE = "purchase"
LEDGER_REFUND = "refund"
LEDGER_CORRECTION = "correction"
LEDGER_RETENTION = "retention"   # phase 2
LEDGER_RTM = "rtm"               # phase 2

# ── Money ─────────────────────────────────────────────────────────────
LAKH_PER_CRORE = 100
# Defaults, in lakh. A ₹100 Cr purse and a ₹20 L floor are the proposal's own
# numbers; every one of them is editable per season.
DEFAULT_OPENING_PURSE_LAKH = 10_000
DEFAULT_MIN_BASE_PRICE_LAKH = 20

# Highest band first; the first band whose ``min_rating`` the card meets wins.
# These are the proposal's example ladder, converted to lakh.
DEFAULT_BASE_PRICE_RULES = [
    {"min_rating": 97, "base_lakh": 400},
    {"min_rating": 95, "base_lakh": 300},
    {"min_rating": 93, "base_lakh": 200},
    {"min_rating": 90, "base_lakh": 150},
    {"min_rating": 88, "base_lakh": 100},
    {"min_rating": 0, "base_lakh": 20},
]

# How a lot came to be on a squad. ``auction`` is the default and the only
# value phase 1 ever wrote; ``retained`` is phase 2a. Kept as a column beside
# ``status`` rather than as a status of its own, so every reader that asks
# "is this player on a squad?" (squad, overseas_count, role_counts,
# publish_to_league) keeps working untouched — see docs/franchise-auction.md.
ACQ_AUCTION = "auction"
ACQ_RETAINED = "retained"
ACQ_RTM = "rtm"          # phase 2b

# The retention slab ladder: what the Nth retention costs. Descending, the way
# every real retention ladder is. This only PRE-FILLS the price; the caps are
# what refuse. Real IPL's 2025 numbers, in lakh.
DEFAULT_RETENTION_PRICE_RULES = [
    {"slab": 1, "price_lakh": 1800},
    {"slab": 2, "price_lakh": 1400},
    {"slab": 3, "price_lakh": 1100},
]

# The bid ladder: the smallest legal raise at a given standing price. Real
# auctions step up as the price climbs, because ₹10 L on top of ₹18 Cr is
# noise and forty of them is a room falling asleep.
DEFAULT_INCREMENT_RULES = [
    {"upto_lakh": 200, "step_lakh": 10},
    {"upto_lakh": 500, "step_lakh": 20},
    {"upto_lakh": 1000, "step_lakh": 25},
    {"upto_lakh": 0, "step_lakh": 50},      # 0 = "and everything above"
]


# ──────────────────────────────────────────────────────────────────────
# Small parsers and renderers
# ──────────────────────────────────────────────────────────────────────

def _loads(raw, fallback):
    """A ``*_json`` column as Python, falling back rather than raising.

    A hand-edited JSON column should degrade to the default, not take a live
    auction down mid-lot.
    """
    if not raw:
        return fallback
    try:
        value = json.loads(raw)
    except Exception:
        logger.warning("auction: unreadable JSON column, using fallback")
        return fallback
    if type(value) is not type(fallback):
        return fallback
    return value


def _dumps(value):
    return json.dumps(value, separators=(",", ":"))


def _as_int(raw, default=0):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def render_money(lakh, symbol="₹"):
    """Lakh as the room says it out loud: ``₹1.5 Cr``, ``₹75 L``, ``₹12.25 Cr``.

    Under a crore it stays in lakh, because "₹0.75 Cr" is nobody's speech. At
    or above, it is crore with the trailing zeros trimmed — ``150`` is
    "₹1.5 Cr", not "₹1.50 Cr", and ``10000`` is "₹100 Cr", not "₹100.00 Cr".
    """
    if lakh is None:
        return "—"
    lakh = int(lakh)
    sign = "-" if lakh < 0 else ""
    lakh = abs(lakh)
    if lakh < LAKH_PER_CRORE:
        return f"{sign}{symbol}{lakh} L"
    whole, part = divmod(lakh, LAKH_PER_CRORE)
    if part == 0:
        return f"{sign}{symbol}{whole} Cr"
    text = f"{part:02d}".rstrip("0")
    return f"{sign}{symbol}{whole}.{text} Cr"


def parse_amount(raw):
    """What somebody typed, as integer lakh. Raises ``AuctionError`` on junk.

    **A bare number is crore**, because that is how the room talks and how the
    proposal writes it: ``/bid 15`` is ₹15 Cr against a ₹100 Cr purse, not
    ₹15 L against a lot whose base price is already ₹1 Cr. Lakh needs saying
    out loud — ``75L``, ``75 lakh`` — and every prompt the bot prints shows the
    next minimum in the same form, so the commonest action is typing back the
    number just shown.

    Accepted: ``15``, ``15.25``, ``1.5cr``, ``₹1.5 Cr``, ``75L``, ``75 lakh``,
    ``1,50``-style separators.
    """
    text = str(raw or "").strip().lower()
    for junk in ("₹", "rs.", "rs", ",", "inr"):
        text = text.replace(junk, "")
    text = text.strip()
    if not text:
        raise AuctionError("Say how much — for example /bid 15 (₹15 Cr) or "
                           "/bid 75L (₹75 lakh).")

    unit = LAKH_PER_CRORE
    for suffix, multiplier in (("crore", LAKH_PER_CRORE), ("cr", LAKH_PER_CRORE),
                               ("lakh", 1), ("lac", 1), ("l", 1)):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            unit = multiplier
            break

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        raise AuctionError(f"“{str(raw).strip()}” is not an amount. Try /bid 15 "
                           f"for ₹15 Cr, or /bid 75L for ₹75 lakh.")
    if value <= 0:
        raise AuctionError("A bid has to be more than nothing.")

    lakh = value * unit
    if lakh != lakh.to_integral_value():
        raise AuctionError(f"{render_money(int(lakh))} is finer than a lakh — "
                           f"bids move in whole lakh. Try "
                           f"{render_money(int(lakh))} rounded off.")
    lakh = int(lakh)
    # A typo of "1500" meaning ₹15 Cr reads as ₹1,500 Cr. Nothing legitimate is
    # anywhere near it, and refusing beats letting it hit the purse check with
    # a number nobody meant.
    if lakh > 1_000_000:
        raise AuctionError("That is not a real amount.")
    return lakh


def status_label(season):
    return _STATUS_LABEL.get(season.status, season.status or "?")


def format_clock(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def seconds_left(lot, now=None):
    """Seconds remaining on a lot, or None when no clock is running."""
    if lot is None or lot.deadline_at is None:
        return None
    return (lot.deadline_at - (now or datetime.utcnow())).total_seconds()


# The board says two different things near the end, so this is a counter
# rather than a flag, and a bid resets it to 0.
GOING_ONCE_AT = 10
GOING_TWICE_AT = 5


def going_stage_for(left):
    """0 running · 1 going once · 2 going twice, from the seconds remaining.

    A pure function of one number, deliberately: it is what the sweeper decides
    the room's next message from, and a test should not need a fake bot to
    pin it.
    """
    if left is None:
        return 0
    if left <= GOING_TWICE_AT:
        return 2
    if left <= GOING_ONCE_AT:
        return 1
    return 0


# ──────────────────────────────────────────────────────────────────────
# The rule ladders
# ──────────────────────────────────────────────────────────────────────

def base_price_rules(season):
    rules = _loads(getattr(season, "base_price_rules_json", None), [])
    cleaned = []
    for row in rules if isinstance(rules, list) else []:
        if not isinstance(row, dict):
            continue
        base = _as_int(row.get("base_lakh"), -1)
        if base < 0:
            continue
        cleaned.append({"min_rating": _as_int(row.get("min_rating"), 0),
                        "base_lakh": base})
    if not cleaned:
        return [dict(row) for row in DEFAULT_BASE_PRICE_RULES]
    cleaned.sort(key=lambda r: r["min_rating"], reverse=True)
    return cleaned


def base_price_for(season, rating):
    """The base price a card of this rating starts at, in lakh.

    Called **once per lot, at pool-build time**, and the answer is stamped onto
    ``AuctionLot.base_price_lakh``. Editing the ladder afterwards is therefore
    safe by construction: it cannot move the price of a lot that has already
    gone on the block, and it cannot move a price mid-auction.
    """
    rating = _as_int(rating, 0)
    for row in base_price_rules(season):
        if rating >= row["min_rating"]:
            return max(1, row["base_lakh"])
    return max(1, DEFAULT_MIN_BASE_PRICE_LAKH)


def increment_rules(season):
    """The bid ladder, cheapest band first and the catch-all last.

    Sorted here rather than trusting the stored order: the bands are read in
    sequence and the first one the standing price fits decides the step, so a
    ladder saved out of order would quietly hand a ₹50 L raise to a lot sitting
    at ₹30 L. An ``upto_lakh`` of 0 means "and everything above", which is why
    it sorts to the end rather than to the front.
    """
    rules = _loads(getattr(season, "bid_increment_rules_json", None), [])
    cleaned = []
    for row in rules if isinstance(rules, list) else []:
        if not isinstance(row, dict):
            continue
        step = _as_int(row.get("step_lakh"), 0)
        if step > 0:
            cleaned.append({"upto_lakh": _as_int(row.get("upto_lakh"), 0),
                            "step_lakh": step})
    if not cleaned:
        cleaned = [dict(row) for row in DEFAULT_INCREMENT_RULES]
    cleaned.sort(key=lambda r: (r["upto_lakh"] <= 0, r["upto_lakh"]))
    return cleaned


def increment_for(season, standing_lakh):
    """The smallest legal raise on top of ``standing_lakh``."""
    standing = max(0, _as_int(standing_lakh, 0))
    catch_all = None
    for row in increment_rules(season):
        ceiling = row["upto_lakh"]
        if ceiling <= 0:
            catch_all = row["step_lakh"]
            continue
        if standing < ceiling:
            return row["step_lakh"]
    if catch_all:
        return catch_all
    return max(1, increment_rules(season)[-1]["step_lakh"])


def next_min_bid(season, lot):
    """The smallest amount that would be a legal bid on this lot right now.

    Printed everywhere — on the board, in the refusal, on the quick-bid button
    — because the commonest action in an auction is "raise it by the minimum"
    and nobody should have to do that arithmetic against a 30-second clock.
    """
    if lot.current_bid_lakh is None:
        return int(lot.base_price_lakh or 1)
    return int(lot.current_bid_lakh) + increment_for(season, lot.current_bid_lakh)


# ──────────────────────────────────────────────────────────────────────
# Seasons
# ──────────────────────────────────────────────────────────────────────

def create_season(session, name, **settings):
    """A new auction in ``setup``. Nothing is bound and no pool exists yet."""
    name = (name or "").strip()
    if not name:
        raise AuctionError("An auction needs a name.")
    season = AuctionSeason(name=name[:120], status=STATUS_SETUP)
    season.base_price_rules_json = _dumps(DEFAULT_BASE_PRICE_RULES)
    season.bid_increment_rules_json = _dumps(DEFAULT_INCREMENT_RULES)
    for key, value in (settings or {}).items():
        if hasattr(season, key):
            setattr(season, key, value)
    session.add(season)
    session.flush()
    return season


def season_for_chat(session, chat_id):
    """The auction bound to this group, or None. ``chat_id`` is unique."""
    if chat_id is None:
        return None
    return (session.query(AuctionSeason)
            .filter(AuctionSeason.chat_id == int(chat_id)).first())


def bind_chat(session, season, chat_id):
    """Bind an auction to one group. Refuses to steal another auction's chat."""
    chat_id = int(chat_id)
    other = season_for_chat(session, chat_id)
    if other is not None and other.id != season.id:
        raise AuctionError(f"This group is already running “{other.name}”. "
                           f"Cancel or finish it first.")
    season.chat_id = chat_id
    return season


def franchises(session, season_id):
    return (session.query(AuctionFranchise)
            .filter(AuctionFranchise.season_id == season_id)
            .order_by(AuctionFranchise.sort_order.asc(),
                      AuctionFranchise.name.asc()).all())


def co_owner_ids(franchise):
    return [int(x) for x in _loads(getattr(franchise, "co_owner_ids_json", None), [])
            if str(x).lstrip("-").isdigit()]


def may_bid_for(franchise, tg_id):
    """True when this Telegram user owns or co-owns the franchise.

    Deliberately *not* widened to bot admins, the same call ``may_pick_for``
    makes for ``/pick``: an admin who has to act for an owner who is not in the
    room uses the console's "bid as franchise" form, which stamps the event as
    an admin's and announces it as one. A bid that reads as the owner's own
    choice when it was not is how a result gets disputed.
    """
    if tg_id is None or franchise is None:
        return False
    tg_id = int(tg_id)
    return tg_id == (franchise.owner_tg_id or 0) or tg_id in co_owner_ids(franchise)


def franchise_for_actor(session, season_id, tg_id):
    """The franchise this user may bid for, or None."""
    for franchise in franchises(session, season_id):
        if may_bid_for(franchise, tg_id):
            return franchise
    return None


def set_co_owners(session, franchise, tg_ids):
    """Replace the co-owner list wholesale — what the website's form posts.

    Blanks, duplicates, non-numbers and the owner's own id are all dropped, so
    a pasted box of ids needs no cleaning first.
    """
    cleaned, seen = [], set()
    for raw in tg_ids or []:
        value = _as_int(raw, 0)
        if value > 0 and value != (franchise.owner_tg_id or 0) and value not in seen:
            seen.add(value)
            cleaned.append(value)
    franchise.co_owner_ids_json = _dumps(cleaned)
    return cleaned


def add_co_owner(session, franchise, tg_id):
    tg_id = _as_int(tg_id, 0)
    if tg_id <= 0:
        raise AuctionError("A co-owner is a positive Telegram user id.")
    if tg_id == (franchise.owner_tg_id or 0):
        raise AuctionError("That id already owns this franchise.")
    ids = co_owner_ids(franchise)
    if tg_id in ids:
        raise AuctionError("That id already co-owns this franchise.")
    ids.append(tg_id)
    franchise.co_owner_ids_json = _dumps(ids)
    return ids


def create_franchise(session, season, name, **fields):
    """Add a franchise, and open its purse with the ledger's first row."""
    name = (name or "").strip()
    if not name:
        raise AuctionError("A franchise needs a name.")
    clash = (session.query(AuctionFranchise)
             .filter(AuctionFranchise.season_id == season.id,
                     func.lower(AuctionFranchise.name) == name.lower()).first())
    if clash is not None:
        raise AuctionError(f"“{name}” is already in this auction.")

    purse = _as_int(fields.pop("purse_total_lakh", None),
                    season.opening_purse_lakh or DEFAULT_OPENING_PURSE_LAKH)
    if purse < 0:
        raise AuctionError("A purse cannot be negative.")

    franchise = AuctionFranchise(season_id=season.id, name=name[:120],
                                 purse_total_lakh=purse,
                                 purse_remaining_lakh=purse, squad_size=0)
    for key, value in (fields or {}).items():
        if hasattr(franchise, key):
            setattr(franchise, key, value)
    session.add(franchise)
    session.flush()

    # The opening purse is itself a ledger row, which is what makes
    # SUM(ledger) == purse_remaining_lakh true from the very first moment
    # rather than only after the first purchase.
    _ledger(session, franchise, LEDGER_OPENING, purse, note="Opening purse")
    log_event(session, season, "franchise_added",
              f"🏛 {_e(name)} joined with {render_money(purse, season.currency_label)}.",
              franchise=franchise)
    return franchise


# ──────────────────────────────────────────────────────────────────────
# The purse ledger
# ──────────────────────────────────────────────────────────────────────

def _ledger(session, franchise, kind, amount_lakh, *, lot=None, note=None,
            by_tg_id=None):
    """Append one ledger row. ``amount_lakh`` is signed: spends are negative.

    This does **not** move the cache — every caller that changes
    ``purse_remaining_lakh`` does so with its own conditional UPDATE in the
    same transaction, and passes the resulting balance here. Writing the cache
    from two places is exactly how the two drift.
    """
    entry = AuctionLedgerEntry(
        season_id=franchise.season_id, franchise_id=franchise.id, kind=kind,
        amount_lakh=int(amount_lakh),
        balance_after=int(franchise.purse_remaining_lakh or 0),
        lot_id=(lot.id if lot is not None else None),
        player_name=(lot.name if lot is not None else None),
        note=(note or None), by_tg_id=by_tg_id)
    session.add(entry)
    return entry


def ledger(session, franchise_id, limit=None):
    query = (session.query(AuctionLedgerEntry)
             .filter(AuctionLedgerEntry.franchise_id == franchise_id)
             .order_by(AuctionLedgerEntry.id.desc()))
    return query.limit(limit).all() if limit else query.all()


def ledger_total(session, franchise_id):
    """What the ledger says the purse is. The authority the cache answers to."""
    total = (session.query(func.coalesce(func.sum(AuctionLedgerEntry.amount_lakh), 0))
             .filter(AuctionLedgerEntry.franchise_id == franchise_id).scalar())
    return int(total or 0)


def reconcile_purses(session, season, *, repair=False, by_tg_id=None):
    """Compare every franchise's cached purse against its ledger.

    Returns ``[(franchise, cached, summed, delta)]`` for the ones that
    disagree — empty when all is well. With ``repair=True`` it writes a
    ``correction`` row bringing the ledger up to the cache rather than
    overwriting the cache, because the cache is what the auction actually
    charged people and the ledger is the record of why; silently rewriting
    either one destroys the evidence of whichever went wrong.
    """
    drift = []
    for franchise in franchises(session, season.id):
        cached = int(franchise.purse_remaining_lakh or 0)
        summed = ledger_total(session, franchise.id)
        if cached == summed:
            continue
        drift.append((franchise, cached, summed, cached - summed))
        if repair:
            _ledger(session, franchise, LEDGER_CORRECTION, cached - summed,
                    note="Reconciliation: ledger brought up to the live purse",
                    by_tg_id=by_tg_id)
    return drift


def correct_purse(session, season, franchise, amount_lakh, *, note=None,
                  by_tg_id=None):
    """An admin's hand on a purse — the escape hatch for anything unforeseen.

    Signed, like every ledger row: a positive number is a grant, a negative one
    takes money back. It is a first-class ledger kind rather than a quiet
    column edit precisely because it is the one operation with no rule behind
    it, so it had better be the most visible.
    """
    amount = _as_int(amount_lakh, 0)
    if amount == 0:
        raise AuctionError("A correction of nothing is not a correction.")
    new_balance = int(franchise.purse_remaining_lakh or 0) + amount
    if new_balance < 0:
        raise AuctionError(
            f"That would leave {franchise.name} at "
            f"{render_money(new_balance, season.currency_label)}. A purse "
            f"cannot go below zero.")
    franchise.purse_remaining_lakh = new_balance
    _ledger(session, franchise, LEDGER_CORRECTION, amount, note=note,
            by_tg_id=by_tg_id)
    log_event(session, season, "purse_corrected",
              f"🧾 {_e(franchise.name)}'s purse adjusted by "
              f"{render_money(amount, season.currency_label)} — now "
              f"{render_money(new_balance, season.currency_label)}.",
              franchise=franchise, by_tg_id=by_tg_id, by_admin=True)
    return new_balance


# ──────────────────────────────────────────────────────────────────────
# Squads, and what a franchise can still afford
# ──────────────────────────────────────────────────────────────────────

def squad(session, franchise_id):
    """Every lot this franchise holds, best first."""
    return (session.query(AuctionLot)
            .filter(AuctionLot.sold_to_id == franchise_id,
                    AuctionLot.status == LOT_SOLD)
            .order_by(AuctionLot.rating.desc(), AuctionLot.name.asc()).all())


def overseas_count(session, franchise_id):
    return int(session.query(func.count(AuctionLot.id))
               .filter(AuctionLot.sold_to_id == franchise_id,
                       AuctionLot.status == LOT_SOLD,
                       AuctionLot.is_overseas.is_(True)).scalar() or 0)


def role_counts(session, franchise_id):
    rows = (session.query(AuctionLot.category, func.count(AuctionLot.id))
            .filter(AuctionLot.sold_to_id == franchise_id,
                    AuctionLot.status == LOT_SOLD)
            .group_by(AuctionLot.category).all())
    return {row[0]: int(row[1]) for row in rows}


def role_minimums(season):
    raw = _loads(getattr(season, "role_minimums_json", None), {})
    return {str(k): _as_int(v, 0) for k, v in (raw or {}).items()
            if _as_int(v, 0) > 0}


def max_bid_now(season, franchise, *, winning_this_lot=True):
    """The most this franchise may legally bid on the lot in front of it.

    This is the **reachability rule**, in money. The draft refuses a pick while
    there is still a slot left to fix the problem with, never afterwards
    (``docs/player-draft.md``); the auction's version is that a bid is refused
    when winning it would leave the franchise unable to fill its remaining
    minimum squad slots at base price.

        slots_after = max(0, min_squad - (squad_size + 1))
        reserve     = slots_after * min_base_price_lakh
        max_bid     = purse_remaining - reserve

    Two properties are load-bearing:

    * once ``squad_size + 1`` reaches the minimum, ``reserve`` is 0 and a
      franchise may spend its last rupee. That is the auction's endgame and it
      must not be blocked by a rule about slots that no longer exist.
    * ``min_base_price_lakh`` is the pool's cheapest base price, **stamped at
      pool-build time**, not "the cheapest lot still available". The tighter
      version is marginally more correct and much worse to play against: the
      ceiling would move every time some *other* franchise bought a cheap
      player, for reasons invisible to the person steering by it.

    The number is printed on the board, in ``/apurse`` and on the console,
    because a rule you only meet when you have broken it is one you cannot plan
    around.
    """
    remaining = int(franchise.purse_remaining_lakh or 0)
    size = int(franchise.squad_size or 0) + (1 if winning_this_lot else 0)
    minimum = int(season.min_squad_size or 0)
    floor = max(0, int(season.min_base_price_lakh or 0))
    slots_after = max(0, minimum - size)
    return remaining - (slots_after * floor)


# ──────────────────────────────────────────────────────────────────────
# Retention
#
# Before a single lot opens, a franchise keeps some of the players it already
# has, at a price, and that spend comes off the top of its purse. The whole of
# it happens while the season is still in ``setup``.
#
# **A retained player is an ordinary sold lot** — ``status = LOT_SOLD`` with
# ``acquisition = ACQ_RETAINED``. Not a status of its own, because ``squad``,
# ``overseas_count``, ``role_counts`` and ``publish_to_league`` all key on
# ``LOT_SOLD`` and then need no change at all: a retained player is in the
# squad, counts against the overseas and squad caps, and reaches the published
# league, for free.
#
# **Retention creates the lot from the catalogue**, rather than needing the
# pool built first — which is the order the proposal asks for. Because
# ``add_players_to_pool`` skips any player who already has a lot in the season,
# building the pool afterwards excludes retained players automatically.
# ──────────────────────────────────────────────────────────────────────

def retention_price_rules(season):
    """The slab ladder, cheapest slab number first."""
    rules = _loads(getattr(season, "retention_price_rules_json", None), [])
    cleaned = []
    for row in rules if isinstance(rules, list) else []:
        if not isinstance(row, dict):
            continue
        price = _as_int(row.get("price_lakh"), -1)
        slab = _as_int(row.get("slab"), 0)
        if price >= 0 and slab > 0:
            cleaned.append({"slab": slab, "price_lakh": price})
    if not cleaned:
        return [dict(row) for row in DEFAULT_RETENTION_PRICE_RULES]
    cleaned.sort(key=lambda r: r["slab"])
    return cleaned


def retention_price_for(season, nth):
    """What the ``nth`` retention (1-based) costs, per the ladder.

    A ladder shorter than ``max_retentions`` is not an error — past its end the
    last slab repeats, which is the behaviour an admin who typed three rungs
    and allowed four retentions obviously meant.

    This only decides what the form and the command **default to**. Nothing
    enforces it: ``retain`` takes the price it is given and refuses on the caps
    instead. Making the slab binding would only invite juggling the retention
    order to dodge the expensive rungs, and an admin who sees the number before
    committing does not need protecting from it.
    """
    rules = retention_price_rules(season)
    nth = max(1, _as_int(nth, 1))
    for row in rules:
        if row["slab"] == nth:
            return row["price_lakh"]
    return rules[-1]["price_lakh"]


def retention_categories(season):
    """Roles a retained player may have. Empty means no restriction."""
    raw = _loads(getattr(season, "retention_categories_json", None), [])
    return [str(v).strip() for v in (raw or []) if str(v).strip()]


def retained(session, franchise_id):
    """Every player this franchise has retained, best first."""
    return (session.query(AuctionLot)
            .filter(AuctionLot.sold_to_id == franchise_id,
                    AuctionLot.status == LOT_SOLD,
                    AuctionLot.acquisition == ACQ_RETAINED)
            .order_by(AuctionLot.rating.desc(), AuctionLot.name.asc()).all())


def retention_spent(session, franchise_id):
    """What a franchise has spent retaining, in lakh.

    Derived rather than cached. The purse column is a cache for a reason that
    does not apply here — it has to be writable by a conditional UPDATE — and a
    second cache is only a second thing to drift.
    """
    total = (session.query(func.coalesce(func.sum(AuctionLot.sold_price_lakh), 0))
             .filter(AuctionLot.sold_to_id == franchise_id,
                     AuctionLot.status == LOT_SOLD,
                     AuctionLot.acquisition == ACQ_RETAINED).scalar())
    return int(total or 0)


def retention_configured(season):
    return _as_int(getattr(season, "max_retentions", 0), 0) > 0


def retention_locked(season):
    return getattr(season, "retention_locked_at", None) is not None


def retention_seconds_left(season, now=None):
    """Seconds until the retention deadline, or None when there is not one.

    Negative once it has passed, so a caller can tell "closes in 3h" from
    "closed 2 days ago" without asking twice.
    """
    deadline = getattr(season, "retention_deadline_at", None)
    if deadline is None:
        return None
    return (deadline - (now or datetime.utcnow())).total_seconds()


def retention_open(season, now=None):
    """Whether a retention would be accepted right now, ignoring the caps."""
    if season.status != STATUS_SETUP or retention_locked(season):
        return False
    left = retention_seconds_left(season, now)
    return left is None or left > 0


def lock_retention(session, season, *, now=None, by_tg_id=None, quiet=False):
    """Shut the retention window. Idempotent."""
    if retention_locked(season):
        return season
    season.retention_locked_at = now or datetime.utcnow()
    if not quiet:
        log_event(session, season, "retention_locked",
                  "🔒 Retention is closed. Every squad is now what it will "
                  "take into the auction.",
                  by_tg_id=by_tg_id, by_admin=True)
    return season


# ── Who held whom last season ────────────────────────────────────────

def previous_squad_map(session, season, league_id=None):
    """``{player_id: AuctionFranchise}`` for the league this season follows.

    Matching is by ``source_player_id``, never by name: two cricketers sharing
    a name would be quietly mis-assigned, and that is the one mistake a
    retention picker — and later, Right To Match — must not make. The franchise
    is matched to the old team by name, which is the only link there is.

    Used by the retention picker (before any lot exists) and by
    ``link_previous_season`` (after the pool is built), so the two can never
    disagree about who held whom.
    """
    league_id = league_id or getattr(season, "previous_league_id", None)
    if not league_id:
        return {}
    rows = (session.query(ChallengePlayer.source_player_id, ChallengeTeam.name)
            .join(ChallengeTeam, ChallengePlayer.team_id == ChallengeTeam.id)
            .filter(ChallengeTeam.league_id == int(league_id),
                    ChallengePlayer.source_player_id.isnot(None)).all())
    if not rows:
        return {}
    by_name = {(f.name or "").strip().lower(): f
               for f in franchises(session, season.id)}
    mapping = {}
    for source_id, team_name in rows:
        franchise = by_name.get((team_name or "").strip().lower())
        if franchise is not None:
            mapping[source_id] = franchise
    return mapping


# ── Retaining ────────────────────────────────────────────────────────

def _retention_lot(session, season, player):
    """The lot a retention will use — converting a queued one, or a new row.

    An admin who built the pool first must not hit a wall, so a player already
    sitting in the queue is converted in place rather than refused. Anything
    further along than ``queued`` is somebody else's business and is refused by
    name.
    """
    lot = (session.query(AuctionLot)
           .filter(AuctionLot.season_id == season.id,
                   AuctionLot.player_id == player.id).first())
    if lot is None:
        home = (season.home_country or "").strip().lower()
        lot = AuctionLot(
            season_id=season.id, player_id=player.id,
            name=(player.name or "")[:150], rating=player.rating or 0,
            category=player.category or "Batsman",
            country=player.country or "Unknown",
            is_overseas=bool(home and (player.country or "").strip().lower() != home),
            version=player.version, bat_hand=player.bat_hand or "Right",
            bowl_hand=player.bowl_hand or "Right",
            bowl_style=player.bowl_style or "Medium Pacer",
            bat_rating=player.bat_rating or 0,
            bowl_rating=player.bowl_rating or 0,
            lot_no=_next_lot_no(session, season.id),
            base_price_lakh=base_price_for(season, player.rating),
            status=LOT_QUEUED)
        session.add(lot)
        session.flush()
        return lot
    if lot.status == LOT_QUEUED:
        return lot
    if lot.acquisition == ACQ_RETAINED and lot.status == LOT_SOLD:
        holder = (session.query(AuctionFranchise)
                  .filter(AuctionFranchise.id == lot.sold_to_id).first())
        raise AuctionError(f"{lot.name} has already been retained by "
                           f"{holder.name if holder else 'another franchise'}.")
    raise AuctionError(f"{lot.name} is already {lot.status} in this auction and "
                       f"cannot be retained.")


def retain(session, season, franchise, player, price_lakh=None, *,
           now=None, by_tg_id=None):
    """Keep a player for a franchise, at a price, out of its purse.

    Every refusal names the number that would have worked. The order matters:
    the window first (there is no point pricing a retention that cannot happen
    at all), then the caps on how many and how much, then the squad rules, and
    finally reachability — the same rule, and the same call, that refuses a
    bid which would leave a franchise unable to fill its minimum squad.
    """
    now = now or datetime.utcnow()
    symbol = season.currency_label or "₹"

    if season.status != STATUS_SETUP:
        raise AuctionError("Retention happens before the auction opens — this "
                           "one is " + str(season.status) + ".")
    if retention_locked(season):
        raise AuctionError("Retention is closed for this auction.")
    left = retention_seconds_left(season, now)
    if left is not None and left <= 0:
        raise AuctionError("The retention deadline has passed.")
    if not retention_configured(season):
        raise AuctionError("This auction allows no retentions — set a maximum "
                           "first.")

    count = int(franchise.retained_count or 0)
    if count + 1 > _as_int(season.max_retentions, 0):
        raise AuctionError(f"{franchise.name} has already retained {count}, "
                           f"which is the maximum.")

    price = (retention_price_for(season, count + 1) if price_lakh is None
             else _as_int(price_lakh, -1))
    if price < 0:
        raise AuctionError("A retention price cannot be negative.")

    cap = getattr(season, "retention_max_spend_lakh", None)
    if cap is not None:
        spent = retention_spent(session, franchise.id)
        if spent + price > int(cap):
            raise AuctionError(
                f"{franchise.name} has spent "
                f"{render_money(spent, symbol)} of a "
                f"{render_money(int(cap), symbol)} retention budget — "
                f"{render_money(price, symbol)} would go "
                f"{render_money(spent + price - int(cap), symbol)} over.")

    low = getattr(season, "retention_min_rating", None)
    high = getattr(season, "retention_max_rating", None)
    rating = _as_int(player.rating, 0)
    if low is not None and rating < int(low):
        raise AuctionError(f"{player.name} is rated {rating}; this auction "
                           f"only allows retaining {int(low)} and above.")
    if high is not None and rating > int(high):
        raise AuctionError(f"{player.name} is rated {rating}; this auction "
                           f"only allows retaining {int(high)} and below.")
    allowed = retention_categories(season)
    if allowed and (player.category or "") not in allowed:
        raise AuctionError(f"{player.name} is a {player.category}; this "
                           f"auction only allows retaining "
                           f"{', '.join(allowed)}.")

    size = int(franchise.squad_size or 0)
    if size + 1 > _as_int(season.max_squad_size, 0):
        raise AuctionError(f"{franchise.name} already has {size} players, "
                           f"which is the squad limit.")

    lot = _retention_lot(session, season, player)

    if lot.is_overseas:
        overseas_cap = max(0, _as_int(season.max_overseas, 0))
        if overseas_count(session, franchise.id) + 1 > overseas_cap:
            raise AuctionError(f"{franchise.name} is already at the overseas "
                               f"limit of {overseas_cap}.")

    remaining = int(franchise.purse_remaining_lakh or 0)
    if price > remaining:
        raise AuctionError(f"{franchise.name} has "
                           f"{render_money(remaining, symbol)} left — "
                           f"{render_money(price, symbol)} is more than the "
                           f"purse.")
    ceiling = max_bid_now(season, franchise)
    if price > ceiling:
        slots_after = max(0, _as_int(season.min_squad_size, 0) - (size + 1))
        reserve = slots_after * max(0, _as_int(season.min_base_price_lakh, 0))
        raise AuctionError(
            f"{render_money(price, symbol)} would leave {franchise.name} "
            f"unable to fill its squad: {slots_after} more players need "
            f"{render_money(reserve, symbol)} held back. The most it can "
            f"retain this player for is {render_money(max(0, ceiling), symbol)}.")

    # Every write below is one transaction, and the debit is conditional for
    # the same reason a sale's is: if the purse moved under the validation a
    # few milliseconds ago, the whole retention rolls back rather than half
    # happening.
    debited = (session.query(AuctionFranchise)
               .filter(AuctionFranchise.id == franchise.id,
                       AuctionFranchise.purse_remaining_lakh >= price,
                       AuctionFranchise.squad_size < _as_int(season.max_squad_size, 0))
               .update({"purse_remaining_lakh":
                        AuctionFranchise.purse_remaining_lakh - price,
                        "squad_size": AuctionFranchise.squad_size + 1,
                        "retained_count": AuctionFranchise.retained_count + 1},
                       synchronize_session=False))
    session.flush()
    session.expire_all()
    franchise = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == franchise.id).first())
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
    if not debited:
        raise AuctionError(f"{franchise.name} can no longer pay "
                           f"{render_money(price, symbol)} — nothing has been "
                           f"retained.")

    lot.status = LOT_SOLD
    lot.acquisition = ACQ_RETAINED
    lot.sold_to_id = franchise.id
    lot.sold_price_lakh = price
    lot.sold_at = now
    lot.deadline_at = None
    lot.going_stage = 0
    # A retained player was, by definition, held by this franchise. Recording
    # it keeps the Right To Match input complete even for players who will
    # never be RTM'd, and costs nothing.
    if lot.previous_franchise_id is None:
        lot.previous_franchise_id = franchise.id

    # Flushed before anything reads it back: this session is autoflush=False,
    # and ``retention_spent`` / ``retained`` / ``squad`` all find a retention by
    # QUERYING for ``status`` and ``acquisition``. Left pending, the next
    # retention would price itself against a budget that had not noticed this
    # one — the bug class that bit phase 1 three times.
    session.flush()

    _ledger(session, franchise, LEDGER_RETENTION, -price, lot=lot,
            note=f"Retained: {lot.name}", by_tg_id=by_tg_id)
    log_event(session, season, "retained",
              f"🔒 <b>{_e(franchise.name)}</b> retain {_e(lot.name)} "
              f"({lot.rating} OVR) for "
              f"{render_money(price, symbol)}.",
              lot=lot, franchise=franchise, by_tg_id=by_tg_id, by_admin=True,
              detail={"price_lakh": price, "slab": count + 1})
    return lot


def unretain(session, season, franchise, lot, *, by_tg_id=None):
    """Release a retained player back into the auction pool.

    A money operation: the purse is credited back with a ``refund`` row so the
    ledger still adds up, and both counters come down. The lot is returned to
    the queue at its base price rather than deleted — the player is available
    again, which is the point.
    """
    if lot is None or lot.acquisition != ACQ_RETAINED or lot.status != LOT_SOLD:
        raise AuctionError(f"{lot.name if lot else 'That player'} is not "
                           f"retained.")
    if retention_locked(season):
        raise AuctionError("Retention is closed — a retained player cannot be "
                           "released now.")
    if season.status != STATUS_SETUP:
        raise AuctionError("The auction has already opened.")

    price = int(lot.sold_price_lakh or 0)
    (session.query(AuctionFranchise)
     .filter(AuctionFranchise.id == franchise.id)
     .update({"purse_remaining_lakh":
              AuctionFranchise.purse_remaining_lakh + price,
              "squad_size": case((AuctionFranchise.squad_size > 0,
                                  AuctionFranchise.squad_size - 1), else_=0),
              "retained_count": case((AuctionFranchise.retained_count > 0,
                                      AuctionFranchise.retained_count - 1),
                                     else_=0)},
             synchronize_session=False))
    session.flush()
    session.expire_all()
    franchise = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == franchise.id).first())
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()

    lot.status = LOT_QUEUED
    lot.acquisition = ACQ_AUCTION
    lot.sold_to_id = None
    lot.sold_price_lakh = None
    lot.sold_at = None

    session.flush()      # see retain() — autoflush is off

    _ledger(session, franchise, LEDGER_REFUND, price, lot=lot,
            note=f"Retention released: {lot.name}", by_tg_id=by_tg_id)
    log_event(session, season, "retention_released",
              f"🔓 <b>{_e(franchise.name)}</b> release {_e(lot.name)} — "
              f"{render_money(price, season.currency_label)} back in the purse, "
              f"and the player goes into the auction.",
              lot=lot, franchise=franchise, by_tg_id=by_tg_id, by_admin=True)
    return lot


# ──────────────────────────────────────────────────────────────────────
# The event log — and the queue the group is announced from
# ──────────────────────────────────────────────────────────────────────

def log_event(session, season, kind, headline, *, lot=None, franchise=None,
              detail=None, by_tg_id=None, by_admin=False):
    """Record what happened, in the words the room will be told it in.

    Every mutating function here writes exactly one of these, and that is the
    whole protocol between the website and the group: the Flask admin panel
    runs in a thread of the bot's process and must never touch Telegram, so it
    writes rows, writes an event, commits, and returns. The sweeper on the PTB
    loop announces everything past ``season.announced_event_id``. Nothing but
    the database crosses the boundary, and the room's message order is the id
    order of this table regardless of which surface produced it.
    """
    event = AuctionEvent(
        season_id=season.id, kind=kind, headline=(headline or "")[:300],
        lot_id=(lot.id if lot is not None else None),
        franchise_id=(franchise.id if franchise is not None else None),
        detail_json=(_dumps(detail) if detail else None),
        by_tg_id=by_tg_id, by_admin=bool(by_admin))
    session.add(event)
    return event


def pending_events(session, season, limit=20):
    """Events the group has not been told about yet, oldest first."""
    return (session.query(AuctionEvent)
            .filter(AuctionEvent.season_id == season.id,
                    AuctionEvent.id > int(season.announced_event_id or 0))
            .order_by(AuctionEvent.id.asc()).limit(limit).all())


def recent_events(session, season_id, limit=15):
    return (session.query(AuctionEvent)
            .filter(AuctionEvent.season_id == season_id)
            .order_by(AuctionEvent.id.desc()).limit(limit).all())


# ──────────────────────────────────────────────────────────────────────
# Building the pool
# ──────────────────────────────────────────────────────────────────────

def lots(session, season_id, status=None):
    query = session.query(AuctionLot).filter(AuctionLot.season_id == season_id)
    if status:
        query = query.filter(AuctionLot.status == status)
    return query.order_by(AuctionLot.lot_no.asc()).all()


def pool_counts(session, season_id):
    """How the pool stands, with **retentions counted separately**.

    ``sold`` and ``total`` are auction figures — a retained player is a sold
    lot (see the retention section) but is not something the auction did, and
    folding them together would have the board announce "3/20 lots resolved"
    before the first lot ever opened, and the admin list count retentions as
    purchases. ``retained`` is its own key, and ``total`` counts only what the
    auction actually has to get through.
    """
    rows = (session.query(AuctionLot.status, AuctionLot.acquisition,
                          func.count(AuctionLot.id))
            .filter(AuctionLot.season_id == season_id)
            .group_by(AuctionLot.status, AuctionLot.acquisition).all())
    counts, retained_lots = {}, 0
    for status, acquisition, n in rows:
        n = int(n)
        # Only a RETENTION is kept out of the auction figures. A Right To
        # Match is auction progress — the room watched the clock run and money
        # moved — so folding it in here would stall the board's "N/M resolved"
        # line forever and credit a retention nobody made.
        if status == LOT_SOLD and acquisition == ACQ_RETAINED:
            retained_lots += n
            continue
        counts[status] = counts.get(status, 0) + n
    counts["retained"] = retained_lots
    counts["total"] = sum(v for k, v in counts.items() if k != "retained")
    return counts


def _next_lot_no(session, season_id):
    top = (session.query(func.coalesce(func.max(AuctionLot.lot_no), 0))
           .filter(AuctionLot.season_id == season_id).scalar())
    return int(top or 0) + 1


def add_players_to_pool(session, season, players, *, set_name=None):
    """Put master cards into the auction as lots. Returns ``(added, skipped)``.

    **Idempotent per player**, which is the point: a unique
    ``(season_id, player_id)`` means re-running the pool builder with a
    corrected filter adds what is new and silently leaves what is already there
    — including anything already sold. A pool builder you cannot safely run
    twice is one nobody dares run once.

    The player columns are copied, not referenced. The catalogue moves under a
    season — a card is re-rated, retired, deactivated — and a finished auction
    has to stay readable afterwards; ``player_id`` keeps the link for the card
    image.
    """
    if season.status not in (STATUS_SETUP, STATUS_PAUSED):
        raise AuctionError("Pause the auction before changing its pool.")

    existing = {row[0] for row in session.query(AuctionLot.player_id)
                .filter(AuctionLot.season_id == season.id,
                        AuctionLot.player_id.isnot(None)).all()}
    home = (season.home_country or "").strip().lower()
    lot_no = _next_lot_no(session, season.id)
    added = skipped = 0

    for player in players:
        if player is None or player.id in existing:
            skipped += 1
            continue
        existing.add(player.id)
        session.add(AuctionLot(
            season_id=season.id, player_id=player.id,
            name=(player.name or "")[:150], rating=player.rating or 0,
            category=player.category or "Batsman",
            country=player.country or "Unknown",
            is_overseas=bool(home and (player.country or "").strip().lower() != home),
            version=player.version, bat_hand=player.bat_hand or "Right",
            bowl_hand=player.bowl_hand or "Right",
            bowl_style=player.bowl_style or "Medium Pacer",
            bat_rating=player.bat_rating or 0, bowl_rating=player.bowl_rating or 0,
            set_name=(set_name or None), lot_no=lot_no,
            base_price_lakh=base_price_for(season, player.rating),
            status=LOT_QUEUED))
        lot_no += 1
        added += 1

    session.flush()
    if added:
        _restamp_price_floor(session, season)
    return added, skipped


def _restamp_price_floor(session, season):
    """Re-read the pool's cheapest base price into ``min_base_price_lakh``.

    Done on every pool change and never again — the reachability rule holds
    this number still on purpose (see ``max_bid_now``), so it is stamped while
    the pool is being built and left alone once the auction is live.
    """
    floor = (session.query(func.min(AuctionLot.base_price_lakh))
             .filter(AuctionLot.season_id == season.id).scalar())
    if floor:
        season.min_base_price_lakh = int(floor)


def remove_lot(session, season, lot):
    """Take a player back out of the pool. Only one nobody has bought."""
    if lot.status == LOT_SOLD:
        raise AuctionError(f"{lot.name} has been sold. Undo the sale first.")
    if lot.status == LOT_ON_BLOCK:
        raise AuctionError(f"{lot.name} is on the block right now.")
    session.delete(lot)
    session.flush()
    _restamp_price_floor(session, season)


def set_base_price(session, season, lot, price_lakh):
    """The per-player override the proposal asks for, while the lot is queued."""
    price = _as_int(price_lakh, 0)
    if price <= 0:
        raise AuctionError("A base price has to be more than nothing.")
    if lot.status != LOT_QUEUED:
        raise AuctionError(f"{lot.name} has already left the queue — a base "
                           f"price can only be set before the lot opens.")
    lot.base_price_lakh = price
    session.flush()
    _restamp_price_floor(session, season)
    return lot


def relist(session, season, lot):
    """Send an unsold player back to the tail of the queue for another round."""
    if lot.status != LOT_UNSOLD:
        raise AuctionError(f"Only an unsold player can be re-listed — "
                           f"{lot.name} is {lot.status}.")
    lot.status = LOT_QUEUED
    lot.lot_no = _next_lot_no(session, season.id)
    lot.current_bid_lakh = None
    lot.current_bidder_id = None
    lot.deadline_at = None
    lot.going_stage = 0
    lot.extensions_used = 0
    lot.rtm_stage = None
    lot.rtm_base_bid_lakh = None
    log_event(session, season, "lot_relisted",
              f"↩️ {_e(lot.name)} goes back into the pool for another round.",
              lot=lot)
    return lot


def link_previous_season(session, season, league_id):
    """Stamp who held each pooled player in a league, for phase 2's RTM.

    Nothing in phase 1 reads ``previous_franchise_id``; it is filled now
    because who held a player last season gets *harder* to recover as time
    passes, not easier, and it is the entire input to Right To Match. Matching
    is by ``source_player_id`` — a name match would quietly mis-assign two
    cricketers who share a name, which is the one mistake RTM must not make.
    """
    if not league_id:
        return 0
    if season.status not in (STATUS_SETUP, STATUS_PAUSED):
        raise AuctionError("Pause the auction before changing which league it "
                           "follows — it decides who holds a Right To Match, "
                           "and one may be open right now.")
    # Remembered, not just used: the retention picker needs to know whose
    # players were whose BEFORE any lot exists, so it cannot re-derive this
    # from the lots the way this function does.
    season.previous_league_id = int(league_id)
    mapping = previous_squad_map(session, season, league_id)
    if not mapping:
        return 0
    stamped = 0
    for lot in lots(session, season.id):
        franchise = mapping.get(lot.player_id)
        if franchise is not None:
            lot.previous_franchise_id = franchise.id
            stamped += 1
    return stamped


# ──────────────────────────────────────────────────────────────────────
# The lot on the block
# ──────────────────────────────────────────────────────────────────────

def current_lot(session, season):
    """The lot on the block, read from the lots themselves.

    ``AuctionSeason.current_lot_id`` is a pointer, not the truth: the lot's own
    ``status`` is. Deriving the answer here means a pointer left stale by a
    crash between two commits heals itself on the next read instead of
    stranding the auction — the same call ``draft_service.current_pick`` makes,
    and for the same reason.

    **A lot mid-RTM counts as the current lot.** It is still the one thing the
    room is looking at, and saying so here is what makes the rest of the
    service treat an RTM window correctly without being told: ``open_lot``
    will not start the next lot over the top of one, ``complete_if_done`` will
    not finish the auction in the middle of one, and every function that
    already guards on ``status != LOT_ON_BLOCK`` refuses for the right reason.
    """
    return (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season.id,
                    AuctionLot.status.in_(LOT_LIVE))
            .order_by(AuctionLot.lot_no.asc()).first())


def next_queued(session, season_id):
    return (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season_id,
                    AuctionLot.status == LOT_QUEUED)
            .order_by(AuctionLot.lot_no.asc()).first())


def open_lot(session, season, lot, *, now=None):
    """Put one queued lot on the block and start its clock."""
    now = now or datetime.utcnow()
    if season.status != STATUS_LIVE:
        raise AuctionError("The auction is not running.")
    standing = current_lot(session, season)
    if standing is not None:
        raise AuctionError(f"{standing.name} is still on the block — sell or "
                           f"pass that lot first.")

    claimed = (session.query(AuctionLot)
               .filter(AuctionLot.id == lot.id,
                       AuctionLot.season_id == season.id,
                       AuctionLot.status == LOT_QUEUED)
               .update({"status": LOT_ON_BLOCK,
                        "deadline_at": now + timedelta(seconds=max(5, int(season.bid_seconds or 30))),
                        "going_stage": 0, "extensions_used": 0,
                        "current_bid_lakh": None, "current_bidder_id": None,
                        "opened_at": now}, synchronize_session=False))
    if not claimed:
        raise AuctionError(f"{lot.name} is no longer waiting in the queue.")
    season.current_lot_id = lot.id
    session.flush()
    session.expire_all()
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
    log_event(session, season, "lot_opened",
              f"🔨 Lot {lot.lot_no}: {_e(lot.name)} ({lot.rating} OVR, "
              f"{_e(lot.category)}) — base "
              f"{render_money(lot.base_price_lakh, season.currency_label)}.",
              lot=lot)
    return lot


def open_next_lot(session, season, *, now=None):
    """Open the next queued lot, or finish the auction when none is left."""
    upcoming = next_queued(session, season.id)
    if upcoming is None:
        return complete_if_done(session, season)
    return open_lot(session, season, upcoming, now=now)


def complete_if_done(session, season):
    """Finish the auction once nothing is queued and nothing is on the block.

    Flushes first, and that is not a detail: this session is autoflush=False,
    and every caller reaches here having *just* changed the status of the lot
    it is asking about. Without the flush the query below reads the lot's old
    ``on_block`` from the database, decides the auction is still running, and
    the last lot of an auction leaves it live forever — which is exactly what
    happens when an admin resolves the final lot from the website, where the
    commit comes after this call rather than before it.
    """
    session.flush()
    if current_lot(session, season) is not None:
        return None
    if next_queued(session, season.id) is not None:
        return None
    if season.status in (STATUS_COMPLETED, STATUS_CANCELLED):
        return None
    season.status = STATUS_COMPLETED
    season.current_lot_id = None
    log_event(session, season, "season_completed",
              "🏁 Every lot is resolved — the auction is complete. "
              "An admin can publish the squads now.")
    return None


# ──────────────────────────────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────────────────────────────

def start(session, season, *, now=None, by_tg_id=None):
    """Open the auction. Refuses anything that would strand it halfway."""
    if season.status == STATUS_LIVE:
        raise AuctionError("This auction is already running.")
    if season.status in (STATUS_COMPLETED, STATUS_CANCELLED):
        raise AuctionError(f"This auction is {season.status}.")
    if not season.chat_id:
        raise AuctionError("Bind the auction to a group first with /abind.")

    field = franchises(session, season.id)
    if not field:
        raise AuctionError("No franchises yet — add some before starting.")
    # A franchise nobody can bid for is a franchise that sits out the whole
    # auction and then has no squad. Say so now, by name, rather than letting
    # the room find out on its first lot.
    ownerless = [f.name for f in field if not f.owner_tg_id]
    if ownerless:
        raise AuctionError("These franchises have no owner Telegram id, so "
                           "nobody could bid for them: " + ", ".join(ownerless))
    # An auction paused on its last lot has nothing queued behind it. This
    # preflight is about *opening* one with nothing to sell — applied to a
    # resume it would strand the very lot the room is waiting on, and there
    # would be no way back: building the pool cannot reopen a closed window.
    # Flushed first, because an admin who withdraws a lot and resumes in one
    # request would otherwise be answered from what is still on disk.
    session.flush()
    standing = current_lot(session, season)
    if standing is None and next_queued(session, season.id) is None:
        raise AuctionError("The pool is empty — build it before starting.")

    # A franchise under the retention minimum can only be fixed *before* the
    # auction opens, so this is the last moment it can usefully be said — and
    # it is said by name, because "someone is short" is not actionable.
    # Flushed first: an admin who retains and starts in one request would
    # otherwise be checked against what is still on disk.
    if retention_configured(season) and _as_int(season.min_retentions, 0) > 0:
        session.flush()
        minimum = _as_int(season.min_retentions, 0)
        short = [f"{f.name} ({int(f.retained_count or 0)})" for f in field
                 if int(f.retained_count or 0) < minimum]
        if short:
            raise AuctionError(
                f"These franchises have retained fewer than the minimum of "
                f"{minimum}: " + ", ".join(short))

    # Opening the auction closes retention rather than leaving the window
    # ajar. Quietly — the "under way" announcement below already says the
    # squads are what they are.
    if season.status == STATUS_SETUP:
        lock_retention(session, season, now=now, by_tg_id=by_tg_id, quiet=True)

    resuming = season.status == STATUS_PAUSED
    season.status = STATUS_LIVE
    log_event(session, season,
              "season_resumed" if resuming else "season_started",
              ("▶️ The auction is back on." if resuming
               else f"🎬 <b>{_e(season.name)}</b> is under way — "
                    f"{len(field)} franchises, "
                    f"{pool_counts(session, season.id).get(LOT_QUEUED, 0)} lots."),
              by_tg_id=by_tg_id, by_admin=True)

    if standing is not None:
        # Resuming onto the lot that was on the block when we paused. A FULL
        # clock, not whatever fraction was left: the room has been arguing and
        # nobody was watching a frozen countdown, and four seconds on resume is
        # worse than no pause at all. The announcement says so out loud.
        return restart_clock(session, season, standing, now=now)
    return open_next_lot(session, season, now=now)


def restart_clock(session, season, lot, *, now=None):
    """A fresh full clock on whatever question the lot is actually asking.

    A lot paused mid-Right-To-Match comes back with an RTM window, not a
    lot-length one: the room is answering a yes/no, not running an auction.
    """
    now = now or datetime.utcnow()
    seconds = (max(5, _as_int(getattr(season, "rtm_window_seconds", 30), 30))
               if lot.status == LOT_RTM_OFFERED
               else max(5, int(season.bid_seconds or 30)))
    lot.deadline_at = now + timedelta(seconds=seconds)
    lot.going_stage = 0
    season.current_lot_id = lot.id
    return lot


def pause(session, season, *, by_tg_id=None):
    """Stop the clock. The deadline is dropped, not frozen.

    Freezing it would mean storing a remaining-seconds figure and adding it
    back on resume — and any path that forgets to would hammer the lot the
    instant the auction came back. Dropping it makes "paused" a state the
    sweeper simply cannot act on, which is the same call
    ``draft_service.pause`` makes.
    """
    if season.status != STATUS_LIVE:
        raise AuctionError("The auction is not running.")
    season.status = STATUS_PAUSED
    lot = current_lot(session, season)
    if lot is not None:
        lot.deadline_at = None
        lot.going_stage = 0
    log_event(session, season, "season_paused",
              "⏸ The auction is paused. Bids are closed until it resumes.",
              lot=lot, by_tg_id=by_tg_id, by_admin=True)
    return lot


def cancel(session, season, *, by_tg_id=None):
    if season.status in (STATUS_COMPLETED, STATUS_CANCELLED):
        raise AuctionError(f"This auction is already {season.status}.")
    lot = current_lot(session, season)
    if lot is not None:
        lot.status = LOT_QUEUED
        lot.deadline_at = None
        lot.going_stage = 0
        lot.current_bid_lakh = None
        lot.current_bidder_id = None
        lot.rtm_stage = None
        lot.rtm_base_bid_lakh = None
    season.status = STATUS_CANCELLED
    season.current_lot_id = None
    log_event(session, season, "season_cancelled",
              "🚫 The auction has been cancelled.",
              by_tg_id=by_tg_id, by_admin=True)
    return season


def set_timer(session, season, seconds):
    seconds = _as_int(seconds, 0)
    if seconds < 5 or seconds > 600:
        raise AuctionError("A lot timer runs between 5 and 600 seconds.")
    season.bid_seconds = seconds
    return seconds


def set_anti_snipe(session, season, window, extend, max_extensions):
    window = _as_int(window, 0)
    extend = _as_int(extend, 0)
    cap = _as_int(max_extensions, 0)
    if window < 0 or window > 120:
        raise AuctionError("The anti-snipe window runs between 0 and 120 "
                           "seconds. 0 turns it off.")
    if extend < 1 or extend > 300:
        raise AuctionError("An extension runs between 1 and 300 seconds.")
    if cap < 0 or cap > 100:
        raise AuctionError("Allow between 0 and 100 extensions.")
    season.snipe_window_seconds = window
    season.snipe_extend_seconds = extend
    season.max_extensions = cap
    return (window, extend, cap)


def extend_timer(session, season, lot, seconds=None, *, now=None,
                 by_tg_id=None):
    """An admin's hand on the clock.

    Deliberately does **not** spend an anti-snipe extension: that counter is
    the room's budget against a bidding war that never ends, and an admin
    adding time because the connection dropped is not a snipe.
    """
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_ON_BLOCK:
        raise AuctionError("Nothing is on the block.")
    if season.status != STATUS_LIVE:
        raise AuctionError("The auction is not running.")
    seconds = _as_int(seconds, 0) or max(5, int(season.bid_seconds or 30))
    if seconds < 1 or seconds > 600:
        raise AuctionError("Add between 1 and 600 seconds.")
    base = lot.deadline_at if lot.deadline_at and lot.deadline_at > now else now
    lot.deadline_at = base + timedelta(seconds=seconds)
    lot.going_stage = 0
    log_event(session, season, "timer_extended",
              f"⏱ {seconds}s added to {_e(lot.name)}.",
              lot=lot, by_tg_id=by_tg_id, by_admin=True)
    return lot


# ──────────────────────────────────────────────────────────────────────
# Bidding
# ──────────────────────────────────────────────────────────────────────

def validate_bid(session, season, lot, franchise, amount_lakh, *, now=None):
    """Everything that can refuse a bid, checked in the order that reads best.

    Refusals name the number. An auction runs on a thirty-second clock and
    "that bid is invalid" costs somebody the lot; every message below says what
    would have worked instead.
    """
    now = now or datetime.utcnow()
    amount = int(amount_lakh)
    symbol = season.currency_label or "₹"

    if season.status == STATUS_PAUSED:
        raise AuctionError("The auction is paused — bidding is closed until an "
                           "admin resumes it.")
    if season.status != STATUS_LIVE:
        raise AuctionError("No auction is running here.")
    # The one case where a bid is legal on a lot that is NOT on the block: the
    # standing top bidder making their one final offer inside an RTM window.
    final_offer = (lot is not None
                   and lot.status == LOT_RTM_OFFERED
                   and lot.rtm_stage == RTM_FINAL_OFFER
                   and lot.current_bidder_id == franchise.id)
    if not final_offer and (lot is None or lot.status != LOT_ON_BLOCK):
        if lot is not None and lot.status == LOT_RTM_OFFERED:
            raise AuctionError(f"{lot.name} is under a Right To Match — only "
                               f"the top bidder can raise, and only once.")
        raise AuctionError("Nothing is on the block right now.")
    left = seconds_left(lot, now)
    if left is None or left <= 0:
        raise AuctionError(f"The clock has run out on {lot.name}.")

    # Bidding against yourself is not a tactic — except when the rule asks you
    # to. Gated on the stage AND the identity, so it opens for exactly one
    # franchise at exactly one moment.
    if lot.current_bidder_id == franchise.id and not final_offer:
        raise AuctionError(f"You already hold the top bid at "
                           f"{render_money(lot.current_bid_lakh, symbol)}. "
                           f"Bidding against yourself is not a tactic.")

    minimum = next_min_bid(season, lot)
    if amount < minimum:
        if lot.current_bid_lakh is None:
            raise AuctionError(f"{lot.name}'s base price is "
                               f"{render_money(minimum, symbol)} — a first bid "
                               f"cannot be under it.")
        raise AuctionError(f"The bid stands at "
                           f"{render_money(lot.current_bid_lakh, symbol)}. "
                           f"The next legal bid is "
                           f"{render_money(minimum, symbol)}.")

    size = int(franchise.squad_size or 0)
    if size + 1 > int(season.max_squad_size or 0):
        raise AuctionError(f"{franchise.name} already has {size} players, "
                           f"which is the squad limit.")

    remaining = int(franchise.purse_remaining_lakh or 0)
    if amount > remaining:
        raise AuctionError(f"{franchise.name} has "
                           f"{render_money(remaining, symbol)} left — "
                           f"{render_money(amount, symbol)} is more than the "
                           f"purse.")

    # Reachability, in money: winning this lot must not leave the franchise
    # unable to fill its remaining minimum slots at base price. Checked while
    # there is still a slot to fix the problem with, never afterwards.
    ceiling = max_bid_now(season, franchise)
    if amount > ceiling:
        slots_after = max(0, int(season.min_squad_size or 0) - (size + 1))
        reserve = slots_after * max(0, int(season.min_base_price_lakh or 0))
        if ceiling < minimum:
            raise AuctionError(
                f"{franchise.name} cannot afford this lot and still fill its "
                f"squad: {slots_after} more players need "
                f"{render_money(reserve, symbol)} held back, and only "
                f"{render_money(remaining, symbol)} is left. An admin can "
                f"adjust the purse with /agrant.")
        raise AuctionError(
            f"{render_money(amount, symbol)} would leave {franchise.name} "
            f"short: {slots_after} more players still to buy need "
            f"{render_money(reserve, symbol)} held back. Your ceiling on this "
            f"lot is {render_money(ceiling, symbol)}.")

    if lot.is_overseas:
        # A literal cap: a typed 0 is a real rule — *no* overseas players —
        # not an absence of one, which is the convention every other overseas
        # limit in this codebase already follows.
        cap = max(0, _as_int(season.max_overseas, 0))
        if overseas_count(session, franchise.id) + 1 > cap:
            raise AuctionError(f"{franchise.name} is already at the overseas "
                               f"limit of {cap}.")

    _check_role_reachability(session, season, franchise, lot)
    return amount


def _check_role_reachability(session, season, franchise, lot):
    """Refuse a buy that would make a role minimum unreachable.

    Same shape as the draft's rule and for the same reason: a squad that owes a
    keeper with one slot left must spend that slot on a keeper, and the refusal
    has to land *at* that bid rather than as a complaint about a finished squad
    nobody can act on. Off by default — ``role_minimums_json`` is empty until
    an admin sets it.
    """
    minimums = role_minimums(season)
    if not minimums:
        return
    counts = role_counts(session, franchise.id)
    # Buying this player fills one slot; count what the squad would then owe.
    counts = dict(counts)
    counts[lot.category] = counts.get(lot.category, 0) + 1
    owed = sum(max(0, need - counts.get(role, 0)) for role, need in minimums.items())
    slots_left = int(season.max_squad_size or 0) - (int(franchise.squad_size or 0) + 1)
    if owed > slots_left:
        short = [role for role, need in minimums.items()
                 if counts.get(role, 0) < need]
        raise AuctionError(
            f"{franchise.name} would have no room left for "
            f"{', '.join(sorted(short))} — {owed} still needed with only "
            f"{max(0, slots_left)} squad slots to spare.")


def place_bid(session, season, lot, franchise, amount_lakh, *, now=None,
              by_tg_id=None, source="tg", by_admin=False):
    """Take a bid, or explain why not. Returns the refreshed lot.

    **The claim is one statement.** Two co-owners typing on the same tick, or a
    bid racing the website's Mark Sold, are the normal case here rather than
    the edge case, and a read-then-write would lose one of them. The UPDATE
    below carries every precondition in its own WHERE and reports how many rows
    it changed: one means this bid won, zero means something moved underneath
    it and we re-read to say *which* thing. It is the device
    ``draft_service.make_pick`` uses to claim a player and a slot, and it
    behaves identically on SQLite and Postgres — no ``SELECT … FOR UPDATE``,
    which SQLite does not have.

    **The anti-snipe extension rides inside the same statement.** Applying it
    separately would mean two bidders both reading the deadline, both deciding
    they were inside the window, and both extending — the clock going out
    twice for one bid.
    """
    now = now or datetime.utcnow()
    amount = validate_bid(session, season, lot, franchise, amount_lakh, now=now)

    # Re-derived here rather than returned from validate_bid: the claim below
    # has to know which status it is claiming against, and a validator that
    # started handing back control flow would be a validator doing two jobs.
    final_offer = (lot.status == LOT_RTM_OFFERED
                   and lot.rtm_stage == RTM_FINAL_OFFER
                   and lot.current_bidder_id == franchise.id)

    window = max(0, int(season.snipe_window_seconds or 0))
    extend = max(1, int(season.snipe_extend_seconds or 10))
    cap = max(0, int(season.max_extensions or 0))
    snipe_cutoff = now + timedelta(seconds=window)
    extended_to = now + timedelta(seconds=extend)

    # True exactly when this bid earns an extension: inside the window, and the
    # budget is not spent. Referenced twice below; SQL evaluates every SET
    # expression against the row's OLD values, so both see the same answer.
    sniping = (and_(AuctionLot.deadline_at <= snipe_cutoff,
                    AuctionLot.extensions_used < cap)
               if window and cap and not final_offer else None)

    values = {
        "current_bid_lakh": amount,
        "current_bidder_id": franchise.id,
        "bid_count": AuctionLot.bid_count + 1,
        "going_stage": 0,
    }
    if sniping is not None:
        values["deadline_at"] = case((sniping, extended_to),
                                     else_=AuctionLot.deadline_at)
        values["extensions_used"] = (AuctionLot.extensions_used
                                     + case((sniping, 1), else_=0))

    claimed = (session.query(AuctionLot)
               .filter(AuctionLot.id == lot.id,
                       AuctionLot.season_id == season.id,
                       AuctionLot.status == (LOT_RTM_OFFERED if final_offer
                                             else LOT_ON_BLOCK),
                       AuctionLot.deadline_at > now,
                       # During an RTM the deadline alone is NOT sufficient:
                       # moving to the decision stage sets a new, later one, so
                       # a late final offer would otherwise land against the
                       # clock the holder is answering on.
                       *([AuctionLot.rtm_stage == RTM_FINAL_OFFER]
                         if final_offer else []),
                       or_(AuctionLot.current_bid_lakh.is_(None),
                           AuctionLot.current_bid_lakh < amount),
                       *([] if final_offer else
                         [or_(AuctionLot.current_bidder_id.is_(None),
                              AuctionLot.current_bidder_id != franchise.id)]))
               .update(values, synchronize_session=False))

    # The UPDATE bypassed the identity map, so every loaded row may be stale.
    # Flush BEFORE expiring: this session is autoflush=False and expiring first
    # would throw away pending work instead of writing it.
    session.flush()
    session.expire_all()
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()

    if not claimed:
        # Re-read only on the losing path, and say which thing moved — "invalid
        # bid" against a running clock is useless to the person who typed it.
        raise _why_the_bid_lost(season, lot, franchise, amount, now)

    session.add(AuctionBid(season_id=season.id, lot_id=lot.id,
                           franchise_id=franchise.id, amount_lakh=amount,
                           by_tg_id=by_tg_id, source=(source or "tg")[:10]))
    # Flushed rather than left pending: this session is autoflush=False, and
    # ``undo_last_bid`` finds the standing bid by QUERYING this table. Without
    # the flush, an undo in the same transaction as the bid it is undoing would
    # report that there is nothing to undo.
    session.flush()
    # An admin entering a bid for an owner who is not in the room says so on
    # the record. A bid that reads as the owner's own choice when it was not is
    # how a result gets disputed afterwards.
    log_event(session, season, "bid",
              f"💰 {_e(franchise.name)} bids "
              f"{render_money(amount, season.currency_label)} for {_e(lot.name)}"
              + (" <i>(entered by an admin)</i>." if by_admin else "."),
              lot=lot, franchise=franchise, by_tg_id=by_tg_id,
              by_admin=by_admin, detail={"amount_lakh": amount})
    # "One more chance to increase" means one. A landed final offer closes the
    # raise window there and then and puts the number to the RTM holder,
    # rather than leaving the clock running on a bidder who has had their go.
    if final_offer:
        lot = rtm_to_decision(session, season, lot, now=now)
    return lot


def _why_the_bid_lost(season, lot, franchise, amount, now):
    symbol = season.currency_label or "₹"
    if lot is None:
        return AuctionError("That lot has gone.")
    if lot.status == LOT_SOLD:
        return AuctionError(f"{lot.name} has just been sold for "
                            f"{render_money(lot.sold_price_lakh, symbol)}.")
    if lot.status != LOT_ON_BLOCK:
        return AuctionError(f"{lot.name} is no longer on the block.")
    if lot.deadline_at is not None and lot.deadline_at <= now:
        return AuctionError(f"The clock ran out on {lot.name} as that arrived.")
    if lot.current_bidder_id == franchise.id:
        return AuctionError("You already hold the top bid.")
    if lot.current_bid_lakh is not None and lot.current_bid_lakh >= amount:
        return AuctionError(
            f"Someone got there first — the bid is now "
            f"{render_money(lot.current_bid_lakh, symbol)}. The next legal bid "
            f"is {render_money(next_min_bid(season, lot), symbol)}.")
    return AuctionError("That bid did not land — try again.")


# ──────────────────────────────────────────────────────────────────────
# Resolving a lot
# ──────────────────────────────────────────────────────────────────────

def sell_lot(session, season, lot, *, now=None, by_tg_id=None, by_admin=False):
    """Sell the lot on the block to whoever holds the top bid.

    Every write below is conditional, and they are one transaction. The sale
    reads ``current_bidder_id`` and ``current_bid_lakh`` *inside* the same
    UPDATE that closes the lot, so a bid landing at the same instant either
    lands first (and is what gets sold) or is refused by the bid's own
    ``status = 'on_block'`` guard. There is no read-modify-write anywhere in
    the pair, so there is no ordering in which the price and the buyer can
    disagree.

    If the debit then fails — a purse that moved under a validation from a few
    milliseconds ago — the whole transaction rolls back *including the sale*,
    and the lot is still on the block. It cannot half-happen.
    """
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_ON_BLOCK:
        raise AuctionError("Nothing is on the block.")
    if lot.current_bidder_id is None:
        raise AuctionError(f"Nobody has bid for {lot.name} — pass the lot "
                           f"instead (/aunsold).")

    closed = (session.query(AuctionLot)
              .filter(AuctionLot.id == lot.id,
                      AuctionLot.status == LOT_ON_BLOCK,
                      AuctionLot.current_bidder_id.isnot(None))
              .update({"status": LOT_SOLD,
                       "sold_to_id": AuctionLot.current_bidder_id,
                       "sold_price_lakh": AuctionLot.current_bid_lakh,
                       "sold_at": now, "deadline_at": None, "going_stage": 0},
                      synchronize_session=False))
    session.flush()
    session.expire_all()
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
    if not closed:
        raise AuctionError(f"{lot.name} has just been resolved by someone else.")

    price = int(lot.sold_price_lakh or 0)
    buyer = (session.query(AuctionFranchise)
             .filter(AuctionFranchise.id == lot.sold_to_id).first())
    if buyer is None:
        raise AuctionError("The winning franchise has gone — cannot complete "
                           "the sale.")

    debited = (session.query(AuctionFranchise)
               .filter(AuctionFranchise.id == buyer.id,
                       AuctionFranchise.purse_remaining_lakh >= price,
                       AuctionFranchise.squad_size < int(season.max_squad_size or 0))
               .update({"purse_remaining_lakh":
                        AuctionFranchise.purse_remaining_lakh - price,
                        "squad_size": AuctionFranchise.squad_size + 1},
                       synchronize_session=False))
    session.flush()
    session.expire_all()
    buyer = (session.query(AuctionFranchise)
             .filter(AuctionFranchise.id == buyer.id).first())
    if not debited:
        raise AuctionError(
            f"{buyer.name} can no longer pay "
            f"{render_money(price, season.currency_label)} — the sale has been "
            f"rolled back and {lot.name} is still on the block.")

    _ledger(session, buyer, LEDGER_PURCHASE, -price, lot=lot,
            note=f"Lot {lot.lot_no}: {lot.name}", by_tg_id=by_tg_id)
    log_event(session, season, "lot_sold",
              f"🔨 <b>SOLD</b> — {_e(lot.name)} to <b>{_e(buyer.name)}</b> for "
              f"{render_money(price, season.currency_label)}.",
              lot=lot, franchise=buyer, by_tg_id=by_tg_id, by_admin=by_admin,
              detail={"price_lakh": price})
    season.current_lot_id = None
    complete_if_done(session, season)
    return lot


def pass_lot(session, season, lot, *, by_tg_id=None, by_admin=False):
    """Mark the lot on the block unsold.

    **Refused while a bid stands.** An admin passing a lot somebody has bid for
    would silently void a franchise's winning bid with nothing on the record;
    undoing the bid first leaves a ``bid_undone`` row with their name on it,
    which is the difference between a correction and a disappearance.
    """
    if lot is None or lot.status != LOT_ON_BLOCK:
        raise AuctionError("Nothing is on the block.")
    if lot.current_bidder_id is not None:
        raise AuctionError(
            f"{lot.name} has a standing bid of "
            f"{render_money(lot.current_bid_lakh, season.currency_label)}. "
            f"Sell it, or undo the bid first with /aundobid.")

    lot.status = LOT_UNSOLD
    lot.deadline_at = None
    lot.going_stage = 0
    lot.times_unsold = int(lot.times_unsold or 0) + 1
    season.current_lot_id = None
    log_event(session, season, "lot_unsold",
              f"❌ <b>UNSOLD</b> — nobody bid for {_e(lot.name)}.",
              lot=lot, by_tg_id=by_tg_id, by_admin=by_admin)
    complete_if_done(session, season)
    return lot


def withdraw_lot(session, season, lot, *, by_tg_id=None):
    """Pull a player out of this auction entirely."""
    if lot is None:
        raise AuctionError("No such lot.")
    if lot.status == LOT_SOLD:
        raise AuctionError(f"{lot.name} has been sold — undo the sale first.")
    was_on_block = lot.status in LOT_LIVE
    lot.status = LOT_WITHDRAWN
    lot.deadline_at = None
    lot.going_stage = 0
    lot.current_bid_lakh = None
    lot.current_bidder_id = None
    # A lot pulled mid-RTM takes no card with it — none was spent — but it must
    # not keep a stage, or a re-listed lot would come back mid-question.
    lot.rtm_stage = None
    lot.rtm_base_bid_lakh = None
    if was_on_block:
        season.current_lot_id = None
    log_event(session, season, "lot_withdrawn",
              f"🚫 {_e(lot.name)} has been withdrawn from the auction.",
              lot=lot, by_tg_id=by_tg_id, by_admin=True)
    if was_on_block:
        complete_if_done(session, season)
    return lot


def undo_last_bid(session, season, lot, *, now=None, by_tg_id=None):
    """Void the standing bid and fall back to the one under it.

    A price operation, not a money one: nothing has been paid yet, so nothing
    is refunded. The bid row is marked void rather than deleted — it is part of
    why the price moved and the room watched it happen, and voiding is
    idempotent where a delete-and-retry is not.

    The clock is floored at one full extension afterwards, because the bid
    being undone may well be the one that bought the last extension; without
    the floor, an undo with two seconds left hands the lot to the previous
    bidder before anybody can react.
    """
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_ON_BLOCK:
        raise AuctionError("Nothing is on the block.")
    top = (session.query(AuctionBid)
           .filter(AuctionBid.lot_id == lot.id, AuctionBid.is_void.is_(False))
           .order_by(AuctionBid.id.desc()).first())
    if top is None:
        raise AuctionError(f"There are no bids on {lot.name} to undo.")

    top.is_void = True
    top.voided_by_tg_id = by_tg_id
    # Flushed before the next query for the same reason ``place_bid`` flushes
    # its bid row: this session is autoflush=False, so an un-flushed void flag
    # is invisible to the SELECT below — which would then hand back the very
    # bid we are undoing as the one to fall back to.
    session.flush()
    previous = (session.query(AuctionBid)
                .filter(AuctionBid.lot_id == lot.id,
                        AuctionBid.is_void.is_(False))
                .order_by(AuctionBid.id.desc()).first())
    lot.current_bid_lakh = previous.amount_lakh if previous else None
    lot.current_bidder_id = previous.franchise_id if previous else None
    lot.bid_count = max(0, int(lot.bid_count or 1) - 1)
    lot.going_stage = 0
    floor = now + timedelta(seconds=max(1, int(season.snipe_extend_seconds or 10)))
    if lot.deadline_at is None or lot.deadline_at < floor:
        lot.deadline_at = floor

    bidder = (session.query(AuctionFranchise)
              .filter(AuctionFranchise.id == top.franchise_id).first())
    restored = (render_money(lot.current_bid_lakh, season.currency_label)
                if lot.current_bid_lakh is not None else "no bid")
    log_event(session, season, "bid_undone",
              f"↩️ {_e(bidder.name) if bidder else 'A'} bid of "
              f"{render_money(top.amount_lakh, season.currency_label)} on "
              f"{_e(lot.name)} was undone — back to {restored}.",
              lot=lot, franchise=bidder, by_tg_id=by_tg_id, by_admin=True)
    return lot


def undo_sale(session, season, lot, *, now=None, by_tg_id=None):
    """Put a sold lot back on the block and refund the buyer.

    A separate operation from undoing a bid, because this one moves money: the
    purse is credited back with a ``refund`` row so the ledger still adds up,
    and the winning bid is voided so the lot does not immediately re-sell at
    the same number to the same franchise.

    Refused once the season has been published — republishing would be needed
    to make the league agree again, and doing that silently is how a squad
    people are already playing with changes underneath them.
    """
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_SOLD:
        raise AuctionError(f"{lot.name if lot else 'That lot'} is not sold.")
    # A retained player is also a sold lot, and undoing one here would refund
    # through the wrong ledger kind, leave ``retained_count`` standing, and put
    # somebody nobody bid for on the block.
    if lot.acquisition == ACQ_RTM:
        raise AuctionError(f"{lot.name} was kept with a Right To Match, not "
                           f"bought. Undo the match instead — it gives the "
                           f"card back as well as the money.")
    if (lot.acquisition or ACQ_AUCTION) != ACQ_AUCTION:
        raise AuctionError(f"{lot.name} was retained, not bought. Release the "
                           f"retention instead (/aunretain).")
    if season.published_at is not None:
        raise AuctionError("This auction has been published. Undo the sale "
                           "after re-publishing, or correct the squad in the "
                           "league itself.")

    # Checked BEFORE anything moves: this lot is going back on the block, and
    # only one lot may be there at a time.
    standing = current_lot(session, season)
    if standing is not None and standing.id != lot.id:
        raise AuctionError(f"{standing.name} is on the block — finish that lot "
                           f"before undoing a sale.")

    price = int(lot.sold_price_lakh or 0)
    buyer = (session.query(AuctionFranchise)
             .filter(AuctionFranchise.id == lot.sold_to_id).first())
    if buyer is not None:
        (session.query(AuctionFranchise)
         .filter(AuctionFranchise.id == buyer.id)
         .update({"purse_remaining_lakh":
                  AuctionFranchise.purse_remaining_lakh + price,
                  "squad_size": case((AuctionFranchise.squad_size > 0,
                                      AuctionFranchise.squad_size - 1),
                                     else_=0)},
                 synchronize_session=False))
        session.flush()
        session.expire_all()
        buyer = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == buyer.id).first())
        lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
        _ledger(session, buyer, LEDGER_REFUND, price, lot=lot,
                note=f"Sale undone: {lot.name}", by_tg_id=by_tg_id)

    winning = (session.query(AuctionBid)
               .filter(AuctionBid.lot_id == lot.id, AuctionBid.is_void.is_(False))
               .order_by(AuctionBid.id.desc()).first())
    if winning is not None:
        winning.is_void = True
        winning.voided_by_tg_id = by_tg_id
        session.flush()      # see undo_last_bid — autoflush is off
    previous = (session.query(AuctionBid)
                .filter(AuctionBid.lot_id == lot.id, AuctionBid.is_void.is_(False))
                .order_by(AuctionBid.id.desc()).first())

    lot.status = LOT_ON_BLOCK
    lot.sold_to_id = None
    lot.sold_price_lakh = None
    lot.sold_at = None
    lot.current_bid_lakh = previous.amount_lakh if previous else None
    lot.current_bidder_id = previous.franchise_id if previous else None
    lot.going_stage = 0
    lot.extensions_used = 0
    if season.status == STATUS_COMPLETED:
        season.status = STATUS_LIVE
    lot.deadline_at = now + timedelta(seconds=max(5, int(season.bid_seconds or 30)))
    season.current_lot_id = lot.id
    log_event(session, season, "sale_undone",
              f"↩️ The sale of {_e(lot.name)} to "
              f"{_e(buyer.name) if buyer else 'a franchise'} for "
              f"{render_money(price, season.currency_label)} was undone — "
              f"the lot is back on the block.",
              lot=lot, franchise=buyer, by_tg_id=by_tg_id, by_admin=True)
    return lot


def resolve_expired(session, season, lot, *, now=None):
    """What the clock does when a lot's time runs out. Returns the outcome.

    ``('sold', lot)`` when a bid stands, ``('unsold', lot)`` when none does.
    Pure of Telegram on purpose: this is the decision, and the scheduler is
    only the thing that says it out loud.
    """
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_ON_BLOCK:
        return (None, lot)
    if lot.current_bidder_id is not None:
        # Before the hammer: does anybody hold a Right To Match on him? If so
        # the lot does not sell yet — it goes to the holder to answer.
        holder, blocked = rtm_available(session, season, lot)
        if holder is not None:
            return ("rtm", offer_rtm(session, season, lot, now=now,
                                     holder=holder))
        if blocked:
            # A card existed and something else stopped it. Worth saying;
            # "no RTM was available" on every other lot would be noise.
            log_event(session, season, "rtm_unavailable",
                      f"🪪 No Right To Match on {_e(lot.name)} — {blocked}.",
                      lot=lot)
        return ("sold", sell_lot(session, season, lot, now=now))
    return ("unsold", pass_lot(session, season, lot))


# ──────────────────────────────────────────────────────────────────────
# Right To Match
#
# The IPL 2025 rule, in full, and it is the only three-party thing in this
# feature:
#
#   The clock expires with RCB top at ₹6 Cr. Ashwin's old franchise, RR, is
#   asked first whether it wants to exercise its RTM. If RR say yes, RCB get
#   ONE more raise — say to ₹9 Cr. RR then match at ₹9 Cr, or let him go.
#
# Three questions, three parties, three windows on the same ``deadline_at``
# with ``rtm_stage`` saying which one is open. **Every stage times out to the
# safe default** — decline, stand, decline — so a franchise nobody is running
# can never wedge an auction, the same call ``pass_lot`` makes for a lot
# nobody bid on.
#
# The price is the FINAL bid, not the one that triggered the offer. That is the
# whole point of the rule: exercising an RTM does not cap what the player
# costs, it only decides who ends up with him.
# ──────────────────────────────────────────────────────────────────────

def rtm_configured(season):
    return bool(getattr(season, "rtm_enabled", False))


def rtm_cards_left(franchise):
    if franchise is None:
        return 0
    return max(0, int(franchise.rtm_cards_total or 0)
               - int(franchise.rtm_cards_used or 0))


def set_rtm_rules(session, season, *, enabled=None, per_team=None,
                  window_seconds=None, extra_lakh=None):
    """Save the Right To Match rules and deal the cards out.

    Card counts live on the franchise so an admin can hand one side an extra,
    but the common case is "everybody gets N" — so saving the rules deals them
    out to anyone who has not spent one yet. A franchise mid-auction that has
    already used a card keeps whatever it has left, because taking a card back
    from underneath a live auction is a way to lose one.
    """
    if enabled is not None:
        season.rtm_enabled = bool(enabled)
    if per_team is not None:
        season.rtm_per_team = max(0, _as_int(per_team, 0))
    if window_seconds is not None:
        season.rtm_window_seconds = max(5, _as_int(window_seconds, 30))
    if extra_lakh is not None:
        season.rtm_extra_lakh = max(0, _as_int(extra_lakh, 0))
    for franchise in franchises(session, season.id):
        if int(franchise.rtm_cards_used or 0) == 0:
            franchise.rtm_cards_total = _as_int(season.rtm_per_team, 0)
    session.flush()
    return season


def set_rtm_cards(session, season, franchise, cards):
    """Give one franchise its own number of cards, never below what it spent."""
    franchise.rtm_cards_total = max(int(franchise.rtm_cards_used or 0),
                                    _as_int(cards, 0))
    session.flush()
    return franchise


def rtm_price(season, lot):
    """What matching would cost: the standing bid, plus any configured premium.

    The premium defaults to zero, so what ships is the pure IPL rule — an RTM
    costs exactly what the room decided the player was worth.
    """
    return (int(lot.current_bid_lakh or 0)
            + max(0, _as_int(getattr(season, "rtm_extra_lakh", 0), 0)))


def owner_ping(franchise):
    """The franchise's name, wired to a Telegram mention of its owner.

    Used only where somebody has seconds to answer. A 30-second Right To
    Match window that arrives as an unremarkable line of text in a busy
    group is a window nobody opens — and every one of them times out into a
    decision the owner never got to make.
    """
    if franchise is None:
        return "?"
    label = _e(franchise.name)
    tg_id = int(franchise.owner_tg_id or 0)
    if tg_id <= 0:
        return label
    return f'<a href="tg://user?id={tg_id}">{label}</a>'


def rtm_holder(session, lot):
    """The franchise that held this player last season, or None."""
    if lot is None or not lot.previous_franchise_id:
        return None
    return (session.query(AuctionFranchise)
            .filter(AuctionFranchise.id == lot.previous_franchise_id).first())


def rtm_available(session, season, lot):
    """``(holder, reason)`` — who may exercise an RTM on this lot, and if
    nobody, why not.

    The reason is returned rather than logged here because it is only worth
    saying out loud in one case: a franchise *had* a card and still could not
    use it. Announcing "no RTM was available" on every lot in a season where
    RTM is off would be pure noise.
    """
    if not rtm_configured(season):
        return (None, None)
    if lot is None or lot.current_bidder_id is None:
        return (None, None)
    if (lot.acquisition or ACQ_AUCTION) != ACQ_AUCTION:
        return (None, None)
    holder = rtm_holder(session, lot)
    if holder is None:
        return (None, None)
    if holder.id == lot.current_bidder_id:
        # They are already winning him. There is nothing to match.
        return (None, None)
    if rtm_cards_left(holder) <= 0:
        return (None, None)

    # From here on a card genuinely exists, so a refusal is worth saying.
    price = rtm_price(season, lot)
    if int(holder.squad_size or 0) + 1 > _as_int(season.max_squad_size, 0):
        return (None, f"{holder.name} has no squad room left")
    if lot.is_overseas:
        cap = max(0, _as_int(season.max_overseas, 0))
        if overseas_count(session, holder.id) + 1 > cap:
            return (None, f"{holder.name} is at the overseas limit")
    if price > int(holder.purse_remaining_lakh or 0):
        return (None, f"{holder.name} cannot afford "
                      f"{render_money(price, season.currency_label)}")
    if price > max_bid_now(season, holder):
        return (None, f"{holder.name} could not fill its squad afterwards")
    return (holder, None)


def offer_rtm(session, season, lot, *, now=None, holder=None):
    """Open the RTM window instead of selling the lot.

    Conditional on the lot still being on the block, so an admin's Mark Sold
    racing the sweeper cannot leave the lot both sold and offered.
    """
    now = now or datetime.utcnow()
    holder = holder or rtm_available(session, season, lot)[0]
    if holder is None:
        raise AuctionError("No RTM is available on this lot.")

    window = max(5, _as_int(getattr(season, "rtm_window_seconds", 30), 30))
    claimed = (session.query(AuctionLot)
               .filter(AuctionLot.id == lot.id,
                       AuctionLot.status == LOT_ON_BLOCK)
               .update({"status": LOT_RTM_OFFERED,
                        "rtm_stage": RTM_INTENT,
                        "rtm_base_bid_lakh": lot.current_bid_lakh,
                        "rtm_offered_at": now,
                        "deadline_at": now + timedelta(seconds=window),
                        "going_stage": 0}, synchronize_session=False))
    session.flush()
    session.expire_all()
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
    if not claimed:
        raise AuctionError(f"{lot.name} has just been resolved by someone else.")

    bidder = (session.query(AuctionFranchise)
              .filter(AuctionFranchise.id == lot.current_bidder_id).first())
    log_event(session, season, "rtm_offered",
              f"🪪 <b>Right To Match</b> — {_e(lot.name)} is going to "
              f"{_e(bidder.name) if bidder else '?'} for "
              f"{render_money(lot.current_bid_lakh, season.currency_label)}.\n"
              f"<b>{owner_ping(holder)}</b>, do you want to use an RTM? "
              f"({rtm_cards_left(holder)} left) — "
              f"<code>/artm yes</code> or <code>/artm no</code>",
              lot=lot, franchise=holder,
              detail={"stage": RTM_INTENT, "bid_lakh": lot.current_bid_lakh})
    return lot


def _rtm_to_bidder(session, season, lot, *, now=None, reason="", by_tg_id=None):
    """End the RTM and let the standing top bidder have the player.

    Puts the lot back on the block for exactly as long as it takes ``sell_lot``
    to close it, rather than growing a second copy of the money path. One
    implementation of the debit, the ledger row and the event is the whole
    reason the purse and its ledger have never disagreed.
    """
    now = now or datetime.utcnow()
    lot.status = LOT_ON_BLOCK
    lot.rtm_stage = None
    session.flush()
    sold = sell_lot(session, season, lot, now=now, by_tg_id=by_tg_id)
    if reason:
        log_event(session, season, "rtm_declined", reason, lot=sold,
                  by_tg_id=by_tg_id)
    return sold


def rtm_intent(session, season, lot, franchise, wants, *, now=None,
               by_tg_id=None):
    """The holder's first answer: do you want to exercise it at all?

    Yes opens the top bidder's one raise. No sells the lot where it stood —
    and spends no card, because nothing was used.
    """
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_RTM_OFFERED or lot.rtm_stage != RTM_INTENT:
        raise AuctionError("There is no Right To Match to answer right now.")
    holder = rtm_holder(session, lot)
    if holder is None or franchise is None or holder.id != franchise.id:
        whose = holder.name if holder else "the previous franchise"
        raise AuctionError(f"Only {whose} can answer this Right To Match.")

    if not wants:
        return _rtm_to_bidder(
            session, season, lot, now=now, by_tg_id=by_tg_id,
            reason=f"🪪 {_e(holder.name)} pass on their Right To Match.")

    window = max(5, _as_int(getattr(season, "rtm_window_seconds", 30), 30))
    advanced = (session.query(AuctionLot)
                .filter(AuctionLot.id == lot.id,
                        AuctionLot.status == LOT_RTM_OFFERED,
                        AuctionLot.rtm_stage == RTM_INTENT)
                .update({"rtm_stage": RTM_FINAL_OFFER,
                         "deadline_at": now + timedelta(seconds=window)},
                        synchronize_session=False))
    session.flush()
    session.expire_all()
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
    if not advanced:
        raise AuctionError("That Right To Match has already been answered.")

    bidder = (session.query(AuctionFranchise)
              .filter(AuctionFranchise.id == lot.current_bidder_id).first())
    log_event(session, season, "rtm_intent",
              f"🪪 <b>{_e(holder.name)}</b> will use their Right To Match on "
              f"{_e(lot.name)}.\n"
              f"<b>{owner_ping(bidder)}</b> — one final bid, or "
              f"stand at {render_money(lot.current_bid_lakh, season.currency_label)}. "
              f"<code>/bid &lt;amount&gt;</code> to raise.",
              lot=lot, franchise=holder, by_tg_id=by_tg_id,
              detail={"stage": RTM_FINAL_OFFER})
    return lot


def rtm_to_decision(session, season, lot, *, now=None):
    """Close the top bidder's window and put the number to the holder."""
    now = now or datetime.utcnow()
    if lot is None or lot.rtm_stage != RTM_FINAL_OFFER:
        return lot
    window = max(5, _as_int(getattr(season, "rtm_window_seconds", 30), 30))
    (session.query(AuctionLot)
     .filter(AuctionLot.id == lot.id,
             AuctionLot.status == LOT_RTM_OFFERED,
             AuctionLot.rtm_stage == RTM_FINAL_OFFER)
     .update({"rtm_stage": RTM_DECISION,
              "deadline_at": now + timedelta(seconds=window)},
             synchronize_session=False))
    session.flush()
    session.expire_all()
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()

    holder = rtm_holder(session, lot)
    price = rtm_price(season, lot)
    raised = int(lot.current_bid_lakh or 0) > int(lot.rtm_base_bid_lakh or 0)
    afford = holder is not None and price <= max_bid_now(season, holder) \
        and price <= int(holder.purse_remaining_lakh or 0)
    body = (f"🪪 Final price on {_e(lot.name)}: "
            f"<b>{render_money(price, season.currency_label)}</b>"
            + (" — raised from "
               f"{render_money(lot.rtm_base_bid_lakh, season.currency_label)}."
               if raised else ", unchanged."))
    if afford:
        body += (f"\n<b>{owner_ping(holder)}</b> — match it? "
                 f"<code>/artm yes</code> or <code>/artm no</code>")
    else:
        # Say it before they tap into a refusal. The card is not spent either
        # way: they never got to use it.
        body += (f"\n<b>{_e(holder.name) if holder else '?'}</b> cannot afford "
                 f"that and still fill a squad — the Right To Match lapses.")
    log_event(session, season, "rtm_final_offer", body, lot=lot,
              franchise=holder, detail={"stage": RTM_DECISION,
                                        "price_lakh": price})
    return lot


def rtm_decide(session, season, lot, franchise, matching, *, now=None,
               by_tg_id=None):
    """The holder's second answer: match the final number, or let him go."""
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_RTM_OFFERED or lot.rtm_stage != RTM_DECISION:
        raise AuctionError("There is no Right To Match to answer right now.")
    holder = rtm_holder(session, lot)
    if holder is None or franchise is None or holder.id != franchise.id:
        whose = holder.name if holder else "the previous franchise"
        raise AuctionError(f"Only {whose} can answer this Right To Match.")

    if not matching:
        return _rtm_to_bidder(
            session, season, lot, now=now, by_tg_id=by_tg_id,
            reason=f"🪪 {_e(holder.name)} let {_e(lot.name)} go.")

    symbol = season.currency_label or "₹"
    price = rtm_price(season, lot)
    if price > int(holder.purse_remaining_lakh or 0):
        raise AuctionError(f"{holder.name} has "
                           f"{render_money(holder.purse_remaining_lakh, symbol)} "
                           f"— {render_money(price, symbol)} is more than the "
                           f"purse.")
    if price > max_bid_now(season, holder):
        raise AuctionError(f"{render_money(price, symbol)} would leave "
                           f"{holder.name} unable to fill its squad.")
    if rtm_cards_left(holder) <= 0:
        raise AuctionError(f"{holder.name} has no Right To Match left.")

    claimed = (session.query(AuctionLot)
               .filter(AuctionLot.id == lot.id,
                       AuctionLot.status == LOT_RTM_OFFERED,
                       AuctionLot.rtm_stage == RTM_DECISION)
               .update({"status": LOT_SOLD, "rtm_stage": None,
                        "acquisition": ACQ_RTM,
                        "sold_to_id": holder.id, "sold_price_lakh": price,
                        "rtm_matched_by_id": holder.id,
                        "sold_at": now, "deadline_at": None},
                       synchronize_session=False))
    session.flush()
    session.expire_all()
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
    if not claimed:
        raise AuctionError("That Right To Match has already been answered.")

    # The card and the money move in the SAME statement-pair as the sale, so a
    # debit that cannot go through takes the card back with it rather than
    # leaving a franchise quietly one poorer.
    debited = (session.query(AuctionFranchise)
               .filter(AuctionFranchise.id == holder.id,
                       AuctionFranchise.purse_remaining_lakh >= price,
                       AuctionFranchise.squad_size < _as_int(season.max_squad_size, 0),
                       AuctionFranchise.rtm_cards_used < AuctionFranchise.rtm_cards_total)
               .update({"purse_remaining_lakh":
                        AuctionFranchise.purse_remaining_lakh - price,
                        "squad_size": AuctionFranchise.squad_size + 1,
                        "rtm_cards_used": AuctionFranchise.rtm_cards_used + 1},
                       synchronize_session=False))
    session.flush()
    session.expire_all()
    holder = (session.query(AuctionFranchise)
              .filter(AuctionFranchise.id == holder.id).first())
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
    if not debited:
        raise AuctionError(f"{holder.name} can no longer pay "
                           f"{render_money(price, symbol)} — the match has been "
                           f"rolled back.")

    _ledger(session, holder, LEDGER_RTM, -price, lot=lot,
            note=f"Right To Match: {lot.name}", by_tg_id=by_tg_id)
    log_event(session, season, "rtm_matched",
              f"🪪 <b>MATCHED</b> — {_e(holder.name)} keep {_e(lot.name)} for "
              f"{render_money(price, symbol)}. "
              f"({rtm_cards_left(holder)} RTM left)",
              lot=lot, franchise=holder, by_tg_id=by_tg_id,
              detail={"price_lakh": price})
    season.current_lot_id = None
    complete_if_done(session, season)
    return lot


def resolve_rtm_stage(session, season, lot, *, now=None):
    """What the clock does when an RTM window runs out. Returns the outcome.

    Every default here is the *safe* one — the one that changes nothing about
    who was winning — because the alternative is an auction that stops dead
    when somebody's phone is in their pocket.
    """
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_RTM_OFFERED:
        return (None, lot)
    holder = rtm_holder(session, lot)
    name = _e(holder.name) if holder else "The previous franchise"

    if lot.rtm_stage == RTM_INTENT:
        return ("declined", _rtm_to_bidder(
            session, season, lot, now=now,
            reason=f"🪪 {name} did not answer in time — no Right To Match."))
    if lot.rtm_stage == RTM_FINAL_OFFER:
        # Standing pat is a real answer, and the commonest one.
        return ("stood", rtm_to_decision(session, season, lot, now=now))
    if lot.rtm_stage == RTM_DECISION:
        return ("declined", _rtm_to_bidder(
            session, season, lot, now=now,
            reason=f"🪪 {name} did not match in time."))
    return (None, lot)


def undo_rtm(session, season, lot, *, now=None, by_tg_id=None):
    """Undo a matched RTM: refund the purse, **give the card back**, re-open.

    Returning the card is the whole point. An undone match that quietly ate one
    leaves a franchise permanently poorer for an admin's slip, and nothing on
    the record to say why.
    """
    now = now or datetime.utcnow()
    if lot is None or lot.status != LOT_SOLD or lot.acquisition != ACQ_RTM:
        raise AuctionError(f"{lot.name if lot else 'That lot'} was not a "
                           f"Right To Match.")
    if season.published_at is not None:
        raise AuctionError("This auction has been published. Undo the match "
                           "after re-publishing, or correct the squad in the "
                           "league itself.")
    standing = current_lot(session, season)
    if standing is not None and standing.id != lot.id:
        raise AuctionError(f"{standing.name} is in front of the room — finish "
                           f"that lot first.")

    price = int(lot.sold_price_lakh or 0)
    holder = (session.query(AuctionFranchise)
              .filter(AuctionFranchise.id == lot.sold_to_id).first())
    if holder is not None:
        (session.query(AuctionFranchise)
         .filter(AuctionFranchise.id == holder.id)
         .update({"purse_remaining_lakh":
                  AuctionFranchise.purse_remaining_lakh + price,
                  "squad_size": case((AuctionFranchise.squad_size > 0,
                                      AuctionFranchise.squad_size - 1), else_=0),
                  "rtm_cards_used": case((AuctionFranchise.rtm_cards_used > 0,
                                          AuctionFranchise.rtm_cards_used - 1),
                                         else_=0)},
                 synchronize_session=False))
        session.flush()
        session.expire_all()
        holder = (session.query(AuctionFranchise)
                  .filter(AuctionFranchise.id == holder.id).first())
        lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
        _ledger(session, holder, LEDGER_REFUND, price, lot=lot,
                note=f"Right To Match undone: {lot.name}", by_tg_id=by_tg_id)

    lot.status = LOT_ON_BLOCK
    lot.acquisition = ACQ_AUCTION
    lot.rtm_matched_by_id = None
    lot.rtm_offered_at = None
    lot.rtm_base_bid_lakh = None
    lot.rtm_stage = None
    lot.sold_to_id = None
    lot.sold_price_lakh = None
    lot.sold_at = None
    if season.status == STATUS_COMPLETED:
        season.status = STATUS_LIVE
    lot.deadline_at = now + timedelta(seconds=max(5, _as_int(season.bid_seconds, 30)))
    season.current_lot_id = lot.id
    session.flush()
    log_event(session, season, "rtm_undone",
              f"↩️ The Right To Match on {_e(lot.name)} was undone — "
              f"{render_money(price, season.currency_label)} back in "
              f"{_e(holder.name) if holder else 'the'} purse, and the card "
              f"returned. The lot is back on the block.",
              lot=lot, franchise=holder, by_tg_id=by_tg_id, by_admin=True)
    return lot


# ──────────────────────────────────────────────────────────────────────
# Publishing
# ──────────────────────────────────────────────────────────────────────

def _short_code(name):
    letters = "".join(ch for ch in (name or "") if ch.isalnum())
    return (letters[:4].upper() or "TEAM")


def _ensure_default_mode(session):
    """The Challenge Mode a published league hangs off.

    Mirrors ``admin._ensure_default_challenge_mode``; a service cannot import
    the Flask admin module, which builds an app object at import time.
    """
    mode = (session.query(ChallengeMode)
            .order_by(ChallengeMode.sort_order, ChallengeMode.id).first())
    if mode:
        return mode
    mode = ChallengeMode(name="League Battles",
                         description="Default league battle data", sort_order=0)
    session.add(mode)
    session.flush()
    return mode


def publish_to_league(session, season, *, league_name=None):
    """Write the bought squads into a Challenge League. Returns the league.

    Idempotent: publishing twice re-syncs the same league rather than creating a
    second one, so a correction can simply be republished.

    The ``details_json`` blob goes through ``player_query.challenge_details_json``
    — the one key set ``services.cipl_match.cp_to_player_dict`` reads. The match
    engine takes every rating and handedness from that blob, *not* from the
    master ``players`` row, so a second hand-written copy of those keys would
    mean a bought squad plays with the wrong numbers and nothing says so.
    """
    if season.status != STATUS_COMPLETED:
        raise AuctionError("Finish the auction first — only a completed "
                           "auction can be published.")
    bought = (session.query(AuctionLot)
              .filter(AuctionLot.season_id == season.id,
                      AuctionLot.status == LOT_SOLD,
                      AuctionLot.sold_to_id.isnot(None)).all())
    if not bought:
        raise AuctionError("Nothing was bought, so there is nothing to publish.")

    league = None
    if season.league_id:
        league = (session.query(ChallengeLeague)
                  .filter(ChallengeLeague.id == season.league_id).first())
    if league is None:
        mode = _ensure_default_mode(session)
        league = ChallengeLeague(
            mode_id=mode.id,
            name=(league_name or season.name or "Auction League")[:120],
            short_code=_short_code(season.name), is_active=True)
        session.add(league)
        session.flush()
    league.home_country = season.home_country or None
    league.max_overseas = max(0, min(11, int(season.max_overseas or 11)))

    by_franchise = {}
    for lot in bought:
        by_franchise.setdefault(lot.sold_to_id, []).append(lot)

    for franchise in franchises(session, season.id):
        team = (session.query(ChallengeTeam)
                .filter(ChallengeTeam.league_id == league.id,
                        ChallengeTeam.name == (franchise.name or "")[:120]).first())
        if team is None:
            team = ChallengeTeam(league_id=league.id,
                                 name=(franchise.name or "")[:120],
                                 short_name=franchise.short_name,
                                 sort_order=franchise.sort_order)
            session.add(team)
            session.flush()
        team.logo_url = franchise.logo_url or team.logo_url

        existing = {(cp.name or "").lower(): cp for cp in
                    session.query(ChallengePlayer)
                    .filter(ChallengePlayer.team_id == team.id).all()}
        squad_lots = sorted(by_franchise.get(franchise.id, []),
                            key=lambda l: (-(l.rating or 0), l.name or ""))
        for lot in squad_lots:
            key = (lot.name or "").lower()
            player = existing.get(key)
            if player is None:
                player = ChallengePlayer(team_id=team.id,
                                         name=(lot.name or "")[:150])
                session.add(player)
            player.source_player_id = lot.player_id
            player.is_overseas = bool(lot.is_overseas)
            player.details_json = player_query.challenge_details_json(
                lot, is_overseas=bool(lot.is_overseas),
                source_player_id=lot.player_id,
                # Ignored by every existing reader, and it is what lets a
                # published squad still say what each player cost.
                extra={"auction_price_lakh": lot.sold_price_lakh,
                       "auction_lot_no": lot.lot_no,
                       # How this player was got. Ignored by every existing
                       # reader, and it is what lets a published squad still
                       # say who was retained rather than bought.
                       "acquisition": lot.acquisition or ACQ_AUCTION})

    season.league_id = league.id
    season.published_at = datetime.utcnow()
    log_event(session, season, "published",
              f"📤 Squads published to the “{_e(league.name)}” Challenge League.",
              by_admin=True)
    return league


# ──────────────────────────────────────────────────────────────────────
# Rendering — the only place here that produces HTML
# ──────────────────────────────────────────────────────────────────────

def _e(value):
    return escape(str(value or ""))


def render_lot_line(season, lot):
    symbol = season.currency_label or "₹"
    mark = "✈️" if lot.is_overseas else "🏠"
    return (f"{mark} <b>{_e(lot.name)}</b> · {lot.rating} OVR · "
            f"{_e(lot.category)} · base {render_money(lot.base_price_lakh, symbol)}")


def render_board(session, season, lot=None, *, now=None):
    """The pinned board: the lot on the block, the price, and every purse.

    This one message is edited in place for the whole of a lot rather than
    re-sent, which is what keeps a thirty-second auction from costing the room
    thirty messages.
    """
    now = now or datetime.utcnow()
    symbol = season.currency_label or "₹"
    lot = lot if lot is not None else current_lot(session, season)
    counts = pool_counts(session, season.id)
    done = counts.get(LOT_SOLD, 0) + counts.get(LOT_UNSOLD, 0)

    head = [f"🏟 <b>{_e(season.name)}</b> — {status_label(season)}"]

    if lot is None:
        head.append("\n<i>No lot is on the block.</i>")
    else:
        left = seconds_left(lot, now)
        head.append(f"\n🔨 <b>Lot {lot.lot_no}</b> · {render_lot_line(season, lot)}")
        if lot.current_bid_lakh is None:
            head.append(f"💰 No bids yet — opening at "
                        f"<b>{render_money(lot.base_price_lakh, symbol)}</b>")
        else:
            bidder = (session.query(AuctionFranchise)
                      .filter(AuctionFranchise.id == lot.current_bidder_id).first())
            head.append(f"💰 <b>{render_money(lot.current_bid_lakh, symbol)}</b> — "
                        f"{_e(bidder.name if bidder else '?')}")
        # Only invite a bid when one would actually be accepted. During the
        # intent and decision windows nobody may bid at all, and during the
        # final offer only one franchise may.
        if lot.status != LOT_RTM_OFFERED or lot.rtm_stage == RTM_FINAL_OFFER:
            head.append(f"➡️ Next bid: <code>/bid "
                        f"{_bid_hint(next_min_bid(season, lot))}</code> "
                        f"({render_money(next_min_bid(season, lot), symbol)})")
        if lot.status == LOT_RTM_OFFERED:
            holder = rtm_holder(session, lot)
            who = _e(holder.name) if holder else "the previous franchise"
            price = render_money(rtm_price(season, lot), symbol)
            if lot.rtm_stage == RTM_INTENT:
                head.append(f"🪪 <b>Right To Match</b> — {who}, use it? "
                            f"<code>/artm yes</code> / <code>/artm no</code>")
            elif lot.rtm_stage == RTM_FINAL_OFFER:
                bidder = (session.query(AuctionFranchise)
                          .filter(AuctionFranchise.id == lot.current_bidder_id)
                          .first())
                head.append(f"🪪 <b>Right To Match</b> — {who} will use it. "
                            f"{_e(bidder.name) if bidder else '?'}: one final "
                            f"raise, or stand.")
            elif lot.rtm_stage == RTM_DECISION:
                head.append(f"🪪 <b>Right To Match</b> — {who}, match at "
                            f"{price}? <code>/artm yes</code> / "
                            f"<code>/artm no</code>")
        if season.status == STATUS_PAUSED:
            head.append("⏸ <b>Paused</b> — bidding is closed.")
        elif left is not None:
            # "Going once / twice" is the auctioneer calling a contest. An RTM
            # window is one franchise answering one question, so it counts
            # down without the patter.
            stage = "" if lot.status == LOT_RTM_OFFERED else {
                1: " · <b>going once</b>",
                2: " · <b>GOING TWICE</b>"}.get(going_stage_for(left), "")
            head.append(f"⏳ {format_clock(left)} left{stage}")
            if lot.extensions_used and season.max_extensions:
                remaining = int(season.max_extensions) - int(lot.extensions_used)
                head.append(f"🛡 Anti-snipe: {lot.extensions_used} used"
                            + (" · <b>final extension</b>" if remaining <= 0
                               else f" · {remaining} left"))

    head.append(f"\n📊 <b>{done}/{counts.get('total', 0)}</b> lots resolved"
                + (f" · 🔒 {counts['retained']} retained"
                   if counts.get("retained") else ""))
    head.append("\n<b>Purses</b>")
    for franchise in franchises(session, season.id):
        ceiling = max_bid_now(season, franchise)
        kept = int(franchise.retained_count or 0)
        head.append(
            f"· {_e(franchise.name)} — "
            f"{render_money(franchise.purse_remaining_lakh, symbol)} · "
            f"{franchise.squad_size}/{season.max_squad_size}"
            + (f" (🔒{kept})" if kept else "")
            + f" · max bid {render_money(max(0, ceiling), symbol)}")
    return "\n".join(head)


def _bid_hint(lakh):
    """The amount as a player would type it back — crore unless it is under one."""
    lakh = int(lakh or 0)
    if lakh < LAKH_PER_CRORE:
        return f"{lakh}L"
    whole, part = divmod(lakh, LAKH_PER_CRORE)
    return str(whole) if part == 0 else f"{whole}.{f'{part:02d}'.rstrip('0')}"


def render_purses(session, season):
    """Every purse — and, where there are retentions, the proposal's own
    Starting / Retention Spent / Auction Purse breakdown, which is just the
    ledger read from one end to the other."""
    symbol = season.currency_label or "₹"
    lines = [f"💼 <b>{_e(season.name)}</b> — purses"]
    for franchise in franchises(session, season.id):
        kept = retention_spent(session, franchise.id)
        block = [f"\n<b>{_e(franchise.name)}</b>"]
        if kept:
            block.append(
                f"  🏦 {render_money(franchise.purse_total_lakh, symbol)} "
                f"start · 🔒 {render_money(kept, symbol)} retained "
                f"({int(franchise.retained_count or 0)})")
        block.append(
            f"  💰 {render_money(franchise.purse_remaining_lakh, symbol)}"
            + (" to spend" if kept else
               f" of {render_money(franchise.purse_total_lakh, symbol)}"))
        block.append(
            f"  👥 {franchise.squad_size}/{season.max_squad_size} · "
            f"✈️ {overseas_count(session, franchise.id)}/{season.max_overseas}")
        block.append(
            f"  🎯 Max bid "
            f"{render_money(max(0, max_bid_now(season, franchise)), symbol)}")
        if rtm_configured(season):
            block.append(f"  🪪 {rtm_cards_left(franchise)} Right To Match "
                         f"of {int(franchise.rtm_cards_total or 0)}")
        lines.append("\n".join(block))
    return "\n".join(lines)


def render_squad(session, season, franchise):
    symbol = season.currency_label or "₹"
    rows = squad(session, franchise.id)
    spent = sum(int(lot.sold_price_lakh or 0) for lot in rows)
    kept = retention_spent(session, franchise.id)
    matched = sum(int(lot.sold_price_lakh or 0) for lot in rows
                  if lot.acquisition == ACQ_RTM)
    lines = [f"👥 <b>{_e(franchise.name)}</b> — {len(rows)}"
             f"/{season.max_squad_size} players",
             f"💰 {render_money(franchise.purse_remaining_lakh, symbol)} left · "
             f"spent {render_money(spent, symbol)}"
             + (f" (🔒 {render_money(kept, symbol)} retained · 🔨 "
                f"{render_money(spent - kept, symbol)} at auction)" if kept else "")]
    if matched:
        lines.append(f"🪪 {render_money(matched, symbol)} of that was matched "
                     f"back at auction prices.")
    if not rows:
        lines.append("\n<i>Nobody signed yet.</i>")
    for lot in rows:
        mark = "✈️" if lot.is_overseas else "🏠"
        # A matched player is not a retained one: he went to the block, the
        # room set his price, and a card was spent to keep him. Squashing the
        # two into one padlock loses the whole story.
        how = {ACQ_RETAINED: " 🔒", ACQ_RTM: " 🪪"}.get(
            lot.acquisition or ACQ_AUCTION, "")
        lines.append(f"{mark} {_e(lot.name)} · {lot.rating} · "
                     f"{_e(lot.category)} — "
                     f"{render_money(lot.sold_price_lakh, symbol)}{how}")
    return "\n".join(lines)
