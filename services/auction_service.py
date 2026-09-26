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
    AuctionAdmin, AuctionBid, AuctionEvent, AuctionFranchise,
    AuctionLedgerEntry, AuctionLot, AuctionRetentionOffer, AuctionSeason,
    ChallengeLeague, ChallengeMode, ChallengePlayer, ChallengeTeam,
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

# The statuses an auction is still *happening* in. ``setup`` is one of them on
# purpose: retention, the expansion picks and the whole pool exist before a
# single lot opens, and a franchise planning against them is looking at a live
# thing, not at history.
STATUS_ACTIVE = (STATUS_SETUP, STATUS_LIVE, STATUS_PAUSED)

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
LEDGER_DRAFT = "draft"           # an expansion side's pre-auction pick
# A short squad topped up for free from the unsold pile at the very end. A
# zero-amount row, kept so the ledger still records every signing.
LEDGER_AUTOFILL = "autofill"

# ── Money ─────────────────────────────────────────────────────────────
LAKH_PER_CRORE = 100
# Defaults, in lakh. A ₹100 Cr purse and a ₹20 L floor are the proposal's own
# numbers; every one of them is editable per season.
DEFAULT_OPENING_PURSE_LAKH = 10_000
DEFAULT_MIN_BASE_PRICE_LAKH = 20

# The four roles a card can carry, in the order the setup page lists them. It
# is ``config.CATEGORIES`` and ``draft_service._CATEGORIES``, spelled the same
# way, because a role rule is matched against ``AuctionLot.category`` — which is
# copied verbatim from the catalogue — and a fifth spelling of "All-rounder"
# would be a rule that silently never fires.
SQUAD_ROLES = ("Batsman", "Bowler", "All-rounder", "Wicket Keeper")

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
# A pre-auction pick by a side with no previous squad. Its own kind and not a
# retention, because a retention is a franchise KEEPING somebody — an
# expansion pick is a franchise taking somebody nobody kept, and the two
# answer different questions about how a squad was assembled.
ACQ_DRAFTED = "drafted"
# Handed over for nothing when the auction finished with a franchise still
# under the minimum squad and players left unsold. Not a bid, not a
# retention: its own kind, so a squad can say how it was completed.
ACQ_AUTOFILL = "autofill"

# Signed before the auction opened: no bid, no clock, no room watching. What
# these have in common is what ``pool_counts`` needs — they are not auction
# progress, and folding them into the board's "N/M resolved" line would have
# it announce a figure before the first lot ever opened.
ACQ_PRE_AUCTION = (ACQ_RETAINED, ACQ_DRAFTED)

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


def going_stage_for(left, season=None):
    """0 running · 1 going once / 1st warning · 2 going twice / 2nd warning.

    A pure function of the seconds remaining (and, on a staged clock, the
    season's two warning points), deliberately: it is what the sweeper decides
    the room's next message from, and a test should not need a fake bot to
    pin it.
    """
    if left is None:
        return 0
    staged = staged_clock(season) if season is not None else None
    once, twice = ((staged[2], staged[3]) if staged
                   else (GOING_ONCE_AT, GOING_TWICE_AT))
    if left <= twice:
        return 2
    if left <= once:
        return 1
    return 0


# ── The staged clock: /atimer 60 40 20 10 5 ──────────────────────────
#
# One lot, four parts. It opens with 60s. A bid with less than 40s left puts
# the clock back to 40 — as often as it takes, which is what replaces
# anti-snipe. At 20s the room gets its 1st warning ("Selling X to Team for
# ₹…"), at 10s the 2nd, and the last 5 are counted down. Because the reset
# point is always above the 1st warning, no lot can ever sell without both
# warnings having been said first.

STAGED_DEFAULTS = (60, 40, 20, 10, 5)


def staged_clock(season):
    """``(open, reset, warn1, warn2, countdown)``, or None on the classic clock."""
    reset = _as_int(getattr(season, "reset_seconds", 0), 0)
    if reset <= 0:
        return None
    return (max(5, _as_int(getattr(season, "bid_seconds", 60), 60)), reset,
            _as_int(getattr(season, "warn1_seconds", 0), 0),
            _as_int(getattr(season, "warn2_seconds", 0), 0),
            countdown_seconds(season))


def apply_staged_defaults(season):
    """Put a brand-new auction on the staged clock (60 40 20 10 5)."""
    (season.bid_seconds, season.reset_seconds, season.warn1_seconds,
     season.warn2_seconds, season.countdown_seconds) = STAGED_DEFAULTS
    return season


def validate_staged(open_s, reset, warn1, warn2, count):
    """Refuse a staged clock whose parts are out of order, naming the rule."""
    if not 10 <= open_s <= 600:
        raise AuctionError("The opening clock runs between 10 and 600 seconds.")
    if not (open_s >= reset > warn1 > warn2 > count >= 0 and warn2 >= 1):
        raise AuctionError(
            "The parts must go down: open ≥ reset > 1st warning > 2nd warning "
            "> countdown — e.g. <code>/atimer 60 40 20 10 5</code>.")
    if count > COUNTDOWN_MAX:
        raise AuctionError(f"The final countdown runs up to {COUNTDOWN_MAX} "
                           f"seconds — or off.")


def staged_summary(season):
    """The staged clock in one line, for /atimer, the board and /arules."""
    staged = staged_clock(season)
    if staged is None:
        return None
    open_s, reset, warn1, warn2, count = staged
    return (f"{open_s}s · a bid under {reset}s resets to {reset}s · warnings at "
            f"{warn1}s & {warn2}s · "
            + (f"count {count}" if count else "no final count"))


# ──────────────────────────────────────────────────────────────────────
# The rule ladders
# ──────────────────────────────────────────────────────────────────────

def base_price_rules(season):
    """The base-price ladder as explicit rating **ranges**, highest band first.

    Each row is ``{"min_rating": lo, "max_rating": hi, "base_lakh": n}``, and
    ``max_rating`` of ``None`` is an open top — "97 and up".

    A ladder saved before ranges existed carries only ``min_rating``, and its
    upper bound was implicit: each band ran up to just below the band above
    it. Those rows are given exactly that bound here, so a season saved the
    old way keeps precisely the prices it had, ``DEFAULT_BASE_PRICE_RULES``
    needs no rewriting, and nothing downstream has to know which shape a row
    came from. That is the whole reason the normalising lives here rather than
    in the two callers.

    A range typed backwards (``92-96``) is read as the slip it is rather than
    as a band that matches nothing — the same call
    ``parse_rating_range`` already makes for ``/apool 90 - 85``.
    """
    rules = _loads(getattr(season, "base_price_rules_json", None), [])
    cleaned = []
    for row in rules if isinstance(rules, list) else []:
        if not isinstance(row, dict):
            continue
        base = _as_int(row.get("base_lakh"), -1)
        if base < 0:
            continue
        low = max(0, _as_int(row.get("min_rating"), 0))
        raw_high = row.get("max_rating")
        high = (_as_int(raw_high, 0) if raw_high not in (None, "")
                else None)
        if high is not None and high < low:
            low, high = high, low
        cleaned.append({"min_rating": low, "max_rating": high,
                        "base_lakh": base})
    if not cleaned:
        cleaned = [{"min_rating": row["min_rating"], "max_rating": None,
                    "base_lakh": row["base_lakh"]}
                   for row in DEFAULT_BASE_PRICE_RULES]
    # Highest band first, because the first band a rating fits is the one that
    # wins; an open top sorts above a closed band that starts in the same
    # place, since it is the wider of the two.
    cleaned.sort(key=lambda r: (r["min_rating"],
                                10 ** 6 if r["max_rating"] is None
                                else r["max_rating"]),
                 reverse=True)
    above = None
    for row in cleaned:
        if row["max_rating"] is None and above is not None:
            # The old shape's implicit ceiling: up to just under the band above.
            row["max_rating"] = above - 1
        above = row["min_rating"]
    return cleaned


def base_price_for(season, rating):
    """The base price a card of this rating starts at, in lakh.

    Called **once per lot, at pool-build time**, and the answer is stamped onto
    ``AuctionLot.base_price_lakh``. Editing the ladder afterwards is therefore
    safe by construction: it cannot move the price of a lot that has already
    gone on the block, and it cannot move a price mid-auction.

    First band the rating fits wins, so two bands that overlap are decided by
    the more expensive one rather than refused — an overlap is somebody
    narrowing a rung, not a mistake worth blocking a pool build over. A rating
    **no** band covers falls back to the floor, and
    ``base_price_gaps`` is what the setup page uses to say so before it costs
    anybody a lot priced at ₹20 L by accident.
    """
    rating = _as_int(rating, 0)
    for row in base_price_rules(season):
        high = row["max_rating"]
        if rating >= row["min_rating"] and (high is None or rating <= high):
            return max(1, row["base_lakh"])
    return max(1, DEFAULT_MIN_BASE_PRICE_LAKH)


def base_price_gaps(season, low=1, high=100):
    """Runs of rating in ``low..high`` that no band covers, as ``(lo, hi)``.

    The ladder used to be gapless by construction — every row was "this rating
    and up", so the bottom row caught everything below it. Ranges can leave a
    hole, and a hole is silent: those cards are simply built at
    ``DEFAULT_MIN_BASE_PRICE_LAKH`` and nobody finds out until the pool is
    already priced. So the setup page asks this and says which ratings are
    uncovered, before the build rather than after.
    """
    covered = set()
    for row in base_price_rules(season):
        top = row["max_rating"]
        top = high if top is None else min(high, top)
        for rating in range(max(low, row["min_rating"]), top + 1):
            covered.add(rating)
    gaps, run = [], None
    for rating in range(low, high + 1):
        if rating in covered:
            if run is not None:
                gaps.append((run, rating - 1))
                run = None
        elif run is None:
            run = rating
    if run is not None:
        gaps.append((run, high))
    return gaps


def render_rating_band(row):
    """One ladder rung the way it is typed and read: ``96-92``, or ``97+``."""
    low, high = row["min_rating"], row["max_rating"]
    if high is None:
        return f"{low}+"
    if high == low:
        return str(low)
    return f"{high}-{low}"


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


def parse_increment_rules(text):
    """``2:10L, 5:20L, 10:25L, 50L`` as a ladder. Raises ``AuctionError``.

    Each comma-separated band is ``<under>:<step>`` — "while the standing
    price is under this, the least raise is that" — and one band may be a bare
    ``<step>``, which is "and everything above". Amounts read the way ``/bid``
    reads them: a bare number is crore, lakh needs an ``L``. A single bare
    step (``/aincrement 25L``) is a flat ladder.

    A ladder with no catch-all gets one at its top step, so a price above the
    last ceiling never falls off the end.
    """
    rules, catch_all = [], None
    for band in (text or "").split(","):
        band = band.strip()
        if not band:
            continue
        for sep in ("->", "→", "=", ":"):
            if sep in band:
                upto_text, step_text = [p.strip() for p in band.split(sep, 1)]
                break
        else:
            upto_text, step_text = None, band
        try:
            step = parse_amount(step_text)
            upto = parse_amount(upto_text) if upto_text else 0
        except AuctionError:
            raise AuctionError(f"“{band}” is not a band. Write "
                               f"<under>:<step>, e.g. 2:10L means under ₹2 Cr "
                               f"the least raise is ₹10 L.")
        if not upto:
            if catch_all is not None:
                raise AuctionError("Only one band can be the catch-all "
                                   "(a step with no ceiling).")
            catch_all = step
            continue
        if step >= upto:
            raise AuctionError(f"A {render_money(step)} step under "
                               f"{render_money(upto)} would jump past the "
                               f"band in one raise — did you mean "
                               f"{step_text}L?")
        rules.append({"upto_lakh": upto, "step_lakh": step})

    ceilings = [row["upto_lakh"] for row in rules]
    if len(ceilings) != len(set(ceilings)):
        raise AuctionError("Two bands share a ceiling — give each its own.")
    rules.sort(key=lambda row: row["upto_lakh"])
    if catch_all is None:
        if not rules:
            raise AuctionError("Give at least one band, e.g. /aincrement "
                               "2:10L, 5:20L, 10:25L, 50L")
        catch_all = rules[-1]["step_lakh"]
    rules.append({"upto_lakh": 0, "step_lakh": catch_all})
    return rules


def set_increment_rules(session, season, rules, *, by_tg_id=None,
                        quiet=False):
    """Save the bid ladder. ``None`` (or empty) restores the default one.

    Safe mid-auction by construction: ``next_min_bid`` reads the ladder on
    every bid rather than stamping it on the lot, so the change applies from
    the next raise and never moves a bid already made. Said out loud unless
    ``quiet`` — a room whose least raise changes under it needs to hear so.
    """
    if season.status == STATUS_CANCELLED:
        raise AuctionError("This auction was cancelled.")
    season.bid_increment_rules_json = _dumps(
        rules if rules else DEFAULT_INCREMENT_RULES)
    session.flush()
    if not quiet:
        log_event(session, season, "increments_changed",
                  "📈 <b>Bid increments changed</b> — "
                  + render_increment_rules(season, joiner=" · "),
                  by_tg_id=by_tg_id, by_admin=True)
    return increment_rules(season)


def render_increment_rules(season, *, joiner="\n"):
    """The ladder the way the room reads it: ``under ₹2 Cr → +₹10 L``."""
    symbol = getattr(season, "currency_label", None) or "₹"
    lines = []
    for row in increment_rules(season):
        where = (f"under {render_money(row['upto_lakh'], symbol)}"
                 if row["upto_lakh"] > 0 else "above that")
        lines.append(f"{where} → +{render_money(row['step_lakh'], symbol)}")
    return joiner.join(lines)


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


# Every rule a season carries — the whole of "how this competition is played",
# and exactly what a next season should inherit. Named explicitly rather than
# derived by copying every column, because the interesting question is which
# columns are NOT here: status, the bound chat, the published league and the
# board cursors are all state from a *run*, and a clone is not a run.
#
# ``tests/test_auction_season.py`` checks this list against the model, so a new
# rule column added later fails a test instead of being silently left behind.
SEASON_RULE_FIELDS = (
    # The clock
    "bid_seconds", "snipe_window_seconds", "snipe_extend_seconds",
    "max_extensions",
    # Money
    "opening_purse_lakh", "currency_label", "min_base_price_lakh",
    "base_price_rules_json", "bid_increment_rules_json",
    # The squad
    "min_squad_size", "max_squad_size", "role_minimums_json",
    "role_maximums_json", "home_country", "max_overseas",
    # Retention
    "max_retentions", "min_retentions", "retention_max_spend_lakh",
    "retention_min_rating", "retention_max_rating",
    "retention_categories_json", "retention_price_rules_json",
    "retention_mode", "retention_slots_json", "retention_rules_json",
    # Right To Match
    "rtm_enabled", "rtm_per_team", "rtm_window_seconds", "rtm_extra_lakh",
    # Expansion teams
    "expansion_picks",
    # Whether unsold players get their accelerated round on their own
    "auto_accelerated",
    # Whether the group runs auction commands only while the auction is live.
    # A room that turned the lock off for one season meant it about the room,
    # not about that season.
    "focus_mode",
    # Whether a bidder may name their own number, or only take the next step.
    "direct_bids",
    # How long the hammer countdown runs before a lot is sold or passed.
    "countdown_seconds",
    # The quiet gap every franchise waits after a bid.
    "bid_gap_seconds",
    # The staged clock: reset point and the two warnings.
    "reset_seconds", "warn1_seconds", "warn2_seconds",
)

# What a franchise takes with it into the next season: who it is and who runs
# it. Everything else — the purse, the squad, the cards — is this season's
# result, and starts again.
FRANCHISE_CARRY_FIELDS = ("short_name", "city", "logo_url", "owner_tg_id",
                          "owner_name", "co_owner_ids_json", "sort_order")


def clone_season(session, source, name, *, chat_id=None, by_tg_id=None):
    """Start the next season from this one. Returns the new season.

    Every rule is copied and every franchise carried over with its owners, so
    "the same competition, next year" is one action rather than an evening of
    re-typing. What it does *not* copy is as deliberate:

    * **The pool.** The proposal's own flow is RETAIN, then BUILD POOL — by the
      time a season ends, last season's players are sitting on squads and the
      catalogue has moved on. The pool builder already skips retained players.
    * **The bound group.** ``bind_chat`` refuses to steal another auction's
      chat, and it is right to: the old season's pinned board is still in that
      group answering to an auction that would no longer exist. An admin runs
      /abind when the old season is finished. ``chat_id`` is here for the case
      where the group is already free and the caller knows it.
    * **Anything from the run** — status, the published league, the board
      cursors, the retention lock.

    The one thing it wires up by itself is ``previous_league_id``, pointed at
    the league the source published. That is the single most forgettable step
    in setting up a season, and forgetting it does not fail loudly: retention
    and Right To Match simply find nobody.
    """
    if source is None:
        raise AuctionError("There is no auction to clone.")

    season = create_season(session, name,
                           **{f: getattr(source, f) for f in SEASON_RULE_FIELDS})
    season.previous_season_id = source.id
    session.flush()

    if chat_id is not None:
        bind_chat(session, season, chat_id)

    for old in franchises(session, source.id):
        create_franchise(
            session, season, old.name, quiet=True,
            purse_total_lakh=season.opening_purse_lakh,
            carried_from_id=old.id,
            rtm_cards_total=_as_int(season.rtm_per_team, 0),
            **{f: getattr(old, f) for f in FRANCHISE_CARRY_FIELDS})

    # Flushed before anything counts the rows just written: this session is
    # autoflush=False, and ``link_previous_season`` reads the franchises back
    # through a query to match them to last season's teams.
    session.flush()

    followed = 0
    if source.league_id:
        followed = link_previous_season(session, season, source.league_id)

    field = franchises(session, season.id)
    if source.league_id:
        tail = (f" Following {_e(source.name)}'s published league, so "
                f"retention and Right To Match already know who held whom.")
    else:
        tail = (f" {_e(source.name)} was never published, so there is no "
                f"record of last season's squads to follow — publish it, then "
                f"link the league on this season's setup page.")
    log_event(session, season, "season_cloned",
              f"🌱 <b>{_e(season.name)}</b> starts from {_e(source.name)} — "
              f"{len(field)} franchises and every rule carried over.{tail}",
              by_tg_id=by_tg_id, by_admin=True,
              detail={"from_season_id": source.id,
                      "franchises": len(field),
                      "lots_followed": followed})
    return season


def season_from_league(session, league, name, *, template=None, chat_id=None,
                       by_tg_id=None):
    """Start an auction for the season after the one this league just played.

    ``clone_season`` needs a previous *auction*. This one needs only a league,
    which is what a finished tournament actually leaves behind — and that
    league may never have come from an auction at all: it can be hand-built,
    or come out of a draft. Without this there is no way in from there, and
    the one thing that matters most, ``previous_league_id``, has to be wired
    by hand or the new season's retention and Right To Match find nobody.

    ``template`` is an earlier season to take the rules from. With none, the
    season takes the ordinary defaults and the admin sets them up.

    The franchises come out **ownerless**, because a ``ChallengeTeam`` has no
    owner to carry — the league only knows a team's name and its badge. That
    is not a hole: ``start`` refuses an auction with an ownerless franchise,
    by name, so it cannot be forgotten quietly.
    """
    if league is None:
        raise AuctionError("That league no longer exists.")

    settings = ({f: getattr(template, f) for f in SEASON_RULE_FIELDS}
                if template is not None else {})
    season = create_season(session, name, **settings)
    if template is not None:
        season.previous_season_id = template.id
    session.flush()

    if chat_id is not None:
        bind_chat(session, season, chat_id)

    teams = (session.query(ChallengeTeam)
             .filter(ChallengeTeam.league_id == league.id)
             .order_by(ChallengeTeam.sort_order.asc(),
                       ChallengeTeam.name.asc()).all())
    for team in teams:
        create_franchise(session, season, team.name, quiet=True,
                         short_name=team.short_name, logo_url=team.logo_url,
                         sort_order=team.sort_order,
                         rtm_cards_total=_as_int(season.rtm_per_team, 0))

    # Flushed before ``link_previous_season`` reads the franchises back
    # through a query: this session is autoflush=False.
    session.flush()
    link_previous_season(session, season, league.id)

    field = franchises(session, season.id)
    log_event(session, season, "season_from_league",
              f"🌱 <b>{_e(season.name)}</b> follows {_e(league.name)} — "
              f"{len(field)} franchises carried over from its teams. "
              f"They have no owners yet; set those before starting.",
              by_tg_id=by_tg_id, by_admin=True,
              detail={"league_id": league.id, "franchises": len(field)})
    return season


def season_following_league(session, league_id):
    """The auction already following this league, or None.

    So a second "next season" press links to the one that exists rather than
    quietly building a duplicate field of franchises beside it.
    """
    if not league_id:
        return None
    return (session.query(AuctionSeason)
            .filter(AuctionSeason.previous_league_id == int(league_id))
            .order_by(AuctionSeason.id.asc()).first())


def season_chain(session, season):
    """The seasons behind this one, nearest first. Empty for a first season."""
    chain, seen = [], set()
    current = season
    while current is not None and getattr(current, "previous_season_id", None):
        previous_id = int(current.previous_season_id)
        # A cycle cannot happen through clone_season, but a hand-edited row is
        # not worth hanging the page over.
        if previous_id in seen:
            break
        seen.add(previous_id)
        current = (session.query(AuctionSeason)
                   .filter(AuctionSeason.id == previous_id).first())
        if current is None:
            break
        chain.append(current)
    return chain


def season_for_chat(session, chat_id):
    """The auction bound to this group, or None. ``chat_id`` is unique."""
    if chat_id is None:
        return None
    return (session.query(AuctionSeason)
            .filter(AuctionSeason.chat_id == int(chat_id)).first())


def bind_chat(session, season, chat_id):
    """Bind an auction to one group, taking it from a finished one if need be.

    A *running* auction keeps its group: its pinned board is in there and the
    room is bidding into it, so binding over the top would leave that board
    answering for an auction nobody can reach. That much was always true.

    A **finished** one does not. The obvious next thing a group does after an
    auction is run the next season in the same group, and the old binding is
    then pure history — ``season_for_chat`` resolves one chat to one auction,
    so leaving it in place blocks the group forever. It used to, and the
    refusal even said "cancel or finish it first" when finishing it was exactly
    what had already happened.
    """
    chat_id = int(chat_id)
    other = season_for_chat(session, chat_id)
    if other is not None and other.id != season.id:
        if other.status not in (STATUS_COMPLETED, STATUS_CANCELLED):
            raise AuctionError(f"This group is already running “{other.name}”. "
                               f"Finish or cancel it first.")
        # Released, not shared: two auctions on one chat_id would make every
        # command in the group ambiguous.
        other.chat_id = None
        _focus_changed(other, session)
        session.flush()
        log_event(session, season, "chat_rebound",
                  f"📌 This group has moved on from {_e(other.name)} to "
                  f"<b>{_e(season.name)}</b>.", by_admin=True)
    season.chat_id = chat_id
    _focus_changed(season, session)
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


def seasons_for_actor(session, tg_id):
    """Every auction still running that this user owns or co-owns a team in.

    Newest first. Only the ``STATUS_ACTIVE`` ones: a completed auction is
    history, and offering it as "your auction" would answer a question about
    this season with last season's purse.

    Co-ownership lives in a JSON column, so the match is made in Python rather
    than in SQL. What keeps that honest is the status filter — the handful of
    auctions actually running at once, not every auction the bot has ever run.
    """
    if tg_id is None:
        return []
    rows = (session.query(AuctionFranchise, AuctionSeason)
            .join(AuctionSeason, AuctionFranchise.season_id == AuctionSeason.id)
            .filter(AuctionSeason.status.in_(STATUS_ACTIVE))
            .order_by(AuctionSeason.id.desc()).all())
    found, seen = [], set()
    for franchise, season in rows:
        if season.id in seen or not may_bid_for(franchise, tg_id):
            continue
        seen.add(season.id)
        found.append(season)
    return found


def season_for_actor(session, tg_id):
    """The one auction this user has a team in — what a DM read resolves to.

    Two is ambiguous rather than wrong, so it names them instead of picking one
    — the same call ``_find_franchise`` makes for a name that could be either
    of two franchises. Reads only: ``/bid`` never comes through here, because a
    bid nobody in the room saw is how a price gets disputed.
    """
    found = seasons_for_actor(session, tg_id)
    if not found:
        return None
    if len(found) > 1:
        raise AuctionError(
            "You have a team in " + ", ".join(s.name for s in found[:5])
            + " — ask in that auction's group, where the command knows which "
              "one you mean.")
    return found[0]


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


def create_franchise(session, season, name, *, quiet=False, **fields):
    """Add a franchise, and open its purse with the ledger's first row.

    ``quiet`` suppresses the per-franchise announcement, for a caller adding a
    whole field at once — ten "X joined" lines are ten messages for the sweeper
    to post where one summary would do. The ledger row is written either way:
    that one is the record, not the announcement.
    """
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
    if not quiet:
        log_event(session, season, "franchise_added",
                  f"🏛 {_e(name)} joined with "
                  f"{render_money(purse, season.currency_label)}.",
                  franchise=franchise)
    return franchise


# ──────────────────────────────────────────────────────────────────────
# The field, as a file
#
# A franchise field is the one part of an auction that is typed by hand, is the
# same across seasons and leagues, and is worth having somewhere other than in
# this database — twelve sides with owners, co-owners, cities and purses is
# twenty minutes of form-filling to reproduce, and every minute of it is a
# chance to mistype a Telegram id. So it exports as JSON and imports back.
#
# **What is exported is what an admin typed, never what the auction did.** The
# purse *remaining*, the squad size, the retained count, the RTM cards used and
# every lot are results — importing them would write a franchise whose caches
# disagree with its own ledger, which is precisely the drift
# ``reconcile_purses`` exists to catch. ``purse_total_lakh`` is in, because it
# is a rule; ``purse_remaining_lakh`` is out, because it is a score. The export
# carries the results too, under ``"squad"``, but only so the file reads as a
# record of a season — the importer ignores the key entirely.
# ──────────────────────────────────────────────────────────────────────

# What a franchise file carries, and what an import will write. Deliberately
# not derived from the model's columns: a column added later is far more likely
# to be a cache or a result than something an admin types, and an importer that
# picked new columns up automatically would write those too.
FRANCHISE_FILE_FIELDS = ("name", "short_name", "city", "owner_name",
                         "owner_tg_id", "logo_url", "sort_order")

FRANCHISE_FILE_VERSION = 1


def export_franchises(session, season):
    """The whole field as a plain dict, ready for ``json.dumps``.

    Ordered by ``franchises()``, which is the order the setup page and the
    expansion draft both run in, so a file re-imported into a fresh season
    rebuilds the same order rather than whatever the ids happen to be.
    """
    field = franchises(session, season.id)
    rows = []
    for franchise in field:
        row = {key: getattr(franchise, key, None)
               for key in FRANCHISE_FILE_FIELDS}
        row["purse_total_lakh"] = int(franchise.purse_total_lakh or 0)
        row["co_owner_tg_ids"] = co_owner_ids(franchise)
        row["rtm_cards_total"] = int(franchise.rtm_cards_total or 0)
        row["draft_picks_total"] = int(franchise.draft_picks_total or 0)
        # Read-only, and ignored on the way back in — see the note above.
        row["squad"] = [
            {"name": lot.name, "rating": lot.rating, "role": lot.category,
             "country": lot.country, "price_lakh": int(lot.sold_price_lakh or 0),
             "acquisition": lot.acquisition}
            for lot in squad(session, franchise.id)]
        rows.append(row)
    return {
        "format": "franchise-auction/franchises",
        "version": FRANCHISE_FILE_VERSION,
        "season": season.name,
        "currency_label": season.currency_label or "₹",
        "opening_purse_lakh": int(season.opening_purse_lakh or 0),
        "exported_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "franchises": rows,
    }


def _franchise_rows(payload):
    """The list of franchises inside whatever shape somebody uploaded.

    A file this exported, the ``franchises`` list out of one, or a bare list —
    all three are the same intent, and refusing two of them would only teach an
    admin to edit the file before uploading it.
    """
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("franchises")
        if rows is None:
            raise AuctionError(
                "That file has no “franchises” list. Export one from this page "
                "to see the shape an import expects.")
    else:
        raise AuctionError("A franchise file is a JSON object or a JSON list.")
    if not isinstance(rows, list) or not rows:
        raise AuctionError("That file lists no franchises.")
    cleaned = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise AuctionError(f"Franchise {index} in that file is not an "
                               f"object — every entry needs at least a name.")
        name = str(row.get("name") or "").strip()
        if not name:
            raise AuctionError(f"Franchise {index} in that file has no name.")
        cleaned.append((name, row))
    seen = {}
    for name, _row in cleaned:
        key = name.lower()
        if key in seen:
            raise AuctionError(f"“{name}” is listed twice in that file.")
        seen[key] = True
    return cleaned


def import_franchises(session, season, payload, *, replace=False,
                      by_tg_id=None):
    """Write a franchise file into this season. Returns ``(added, updated, removed)``.

    **Matched by name, case aside**, which is what makes the same file safe to
    upload twice: the second upload updates the field it created rather than
    refusing it or doubling it. A franchise already in the season keeps its
    purse, its squad and its ledger — only the typed fields move — because an
    import is an edit to who the sides are, not a reset of what they have done.

    The one exception is ``purse_total_lakh``. Changing a franchise's *total*
    after it has spent anything would leave the total and the ledger describing
    different auctions, so on an existing franchise it is applied through
    ``correct_purse`` — a real correction row for the difference — rather than
    written over the column. A franchise that has spent nothing therefore ends
    up exactly where a fresh one would, and one mid-auction gets an audited
    adjustment instead of a silent one.

    ``replace`` removes franchises the file does not mention, through
    ``remove_franchise`` so their players and purses go back where they belong.
    Off by default: an import is far more often "here is the field" than "and
    nobody else", and a flag that deletes is a flag that has to be asked for.
    """
    rows = _franchise_rows(payload)
    existing = {(f.name or "").strip().lower(): f
                for f in franchises(session, season.id)}
    added = updated = 0
    touched = set()

    for order, (name, row) in enumerate(rows, start=1):
        key = name.lower()
        franchise = existing.get(key)
        purse = row.get("purse_total_lakh")
        if purse is None:
            purse = season.opening_purse_lakh or DEFAULT_OPENING_PURSE_LAKH
        purse = max(0, _as_int(purse, 0))

        if franchise is None:
            franchise = create_franchise(session, season, name, quiet=True,
                                         purse_total_lakh=purse)
            existing[key] = franchise
            added += 1
        else:
            updated += 1
            if purse != int(franchise.purse_total_lakh or 0):
                # Through the ledger, never over the column — see the docstring.
                correct_purse(session, season, franchise,
                              purse - int(franchise.purse_total_lakh or 0),
                              note=f"Imported purse: "
                                   f"{render_money(purse, season.currency_label)}",
                              by_tg_id=by_tg_id)
                franchise.purse_total_lakh = purse
        touched.add(franchise.id)

        # Only keys the file actually carries are written, so a hand-trimmed
        # file that lists names and purses alone does not blank out the cities
        # and owners already recorded here.
        for field in FRANCHISE_FILE_FIELDS:
            if field == "name" or field not in row:
                continue
            value = row.get(field)
            if field == "owner_tg_id":
                franchise.owner_tg_id = _as_int(value, 0) or None
            elif field == "sort_order":
                franchise.sort_order = _as_int(value, 0)
            else:
                text = str(value).strip() if value is not None else ""
                setattr(franchise, field, text[:500] or None)
        if "sort_order" not in row:
            # The file's own order, which is the order an admin arranged it in.
            franchise.sort_order = order
        if "co_owner_tg_ids" in row or "co_owners" in row:
            set_co_owners(session, franchise,
                          row.get("co_owner_tg_ids") or row.get("co_owners") or [])
        if "rtm_cards_total" in row:
            franchise.rtm_cards_total = max(0, _as_int(row["rtm_cards_total"], 0))
        if "draft_picks_total" in row:
            franchise.draft_picks_total = max(0, _as_int(row["draft_picks_total"], 0))

    removed = []
    if replace:
        for franchise in franchises(session, season.id):
            if franchise.id in touched:
                continue
            name = franchise.name
            if int(franchise.squad_size or 0) > 0:
                # The same path /aremoveteam takes: players back into the pool
                # and the purse shared out, all on the ledger. A plain delete
                # would strand both.
                remove_franchise(session, season, franchise, by_tg_id=by_tg_id)
            else:
                session.delete(franchise)
            removed.append(name)

    session.flush()
    log_event(session, season, "franchises_imported",
              f"🏛 The field was imported from a file: {added} added, "
              f"{updated} updated"
              + (f", {len(removed)} removed" if removed else "") + ".",
              by_tg_id=by_tg_id, by_admin=True)
    return added, updated, removed


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


def set_opening_purse(session, season, purse_lakh, *, by_tg_id=None,
                     apply_to_field=True, quiet=False):
    """Change the season's opening purse — and every franchise's with it.

    The opening purse used to be read once, by ``create_franchise``, and never
    again: editing it on the setup page moved a number that only the *next*
    franchise would ever see, so an admin who set the field up at ₹100 Cr and
    then decided on ₹120 Cr had twelve purses to re-type by hand — and no sign
    that anything had been missed until somebody was refused mid-lot.

    So the field moves with it. Each franchise is brought to exactly the new
    purse: its total is set, its remaining is moved by the same delta, and the
    move is written as a ``correction`` ledger row, which is what keeps
    ``SUM(ledger) == purse_remaining_lakh`` true and leaves the reason on the
    record. Spending is untouched — a franchise that has bought ₹30 Cr of
    players out of ₹100 Cr keeps its squad and lands on ₹90 Cr of a ₹120 Cr
    purse, not on ₹120 Cr.

    A cut that would overdraw somebody is refused **by name** rather than
    clamped at zero: "₹40 Cr, but Mumbai has already spent ₹55 Cr" is a
    decision for the admin (sell somebody, or pick another number), and a
    silent clamp would leave one franchise on a different purse from the rest
    with nothing saying so.

    Saving the same number again does nothing at all, which is what protects a
    franchise deliberately given a purse of its own: the field follows this
    number when this number moves, not on every save of the page it lives on.

    ``apply_to_field=False`` keeps the old behaviour — the default for new
    franchises only — for a caller that wants it.
    """
    new_purse = _as_int(purse_lakh, None)
    if new_purse is None or new_purse < 0:
        raise AuctionError("A purse cannot be negative.")
    old_purse = _as_int(season.opening_purse_lakh, 0)
    season.opening_purse_lakh = new_purse
    if not apply_to_field or new_purse == old_purse:
        # Unchanged is not "apply it again": a franchise deliberately set to a
        # different purse on its own row would otherwise be dragged back to the
        # season's every time somebody saved the anti-snipe numbers. The field
        # moves when the number moves, and only then.
        return []

    field = franchises(session, season.id)
    symbol = season.currency_label or "₹"
    # Everything is checked before anything is written: a half-applied purse
    # change is the one outcome worse than a refused one.
    for franchise in field:
        delta = new_purse - _as_int(franchise.purse_total_lakh, 0)
        if delta >= 0:
            continue
        after = _as_int(franchise.purse_remaining_lakh, 0) + delta
        if after < 0:
            spent = (_as_int(franchise.purse_total_lakh, 0)
                     - _as_int(franchise.purse_remaining_lakh, 0))
            raise AuctionError(
                f"{franchise.name} has already spent "
                f"{render_money(spent, symbol)}, so a "
                f"{render_money(new_purse, symbol)} purse would put it "
                f"{render_money(-after, symbol)} in the red. Sell somebody "
                f"first, or set that franchise's own purse.")

    changed = []
    for franchise in field:
        delta = new_purse - _as_int(franchise.purse_total_lakh, 0)
        if delta == 0:
            continue
        # The move is ONE conditional statement, the same device ``sell_lot``
        # debits with — and for the same reason, which is not hypothetical
        # here: this can be saved from the website while the room is bidding,
        # so a read-then-write would read a balance, have a sale debit it, and
        # write the pre-sale number back over the top. Money the franchise had
        # already spent would reappear, and its ledger would stop agreeing with
        # its purse. The non-negative guard rides in the WHERE for the same
        # reason: the loop above is a courtesy that says WHY in words, this is
        # what actually refuses, and it cannot be overtaken.
        moved = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == franchise.id,
                         AuctionFranchise.purse_remaining_lakh + delta >= 0)
                 .update({"purse_total_lakh": new_purse,
                          "purse_remaining_lakh":
                              AuctionFranchise.purse_remaining_lakh + delta},
                         synchronize_session=False))
        session.flush()
        session.expire(franchise)
        if not moved:
            raise AuctionError(
                f"{franchise.name}'s purse moved while this was being saved, "
                f"and a {render_money(new_purse, symbol)} purse would now put "
                f"it in the red. Nothing has been changed — try again.")
        # Refreshed first: ``_ledger`` stamps ``balance_after`` from the row,
        # and the row the ORM is holding is the one before that UPDATE.
        _ledger(session, franchise, LEDGER_CORRECTION, delta,
                note=(f"Opening purse {render_money(old_purse, symbol)} → "
                      f"{render_money(new_purse, symbol)}"),
                by_tg_id=by_tg_id)
        changed.append(franchise)

    if changed and not quiet:
        log_event(session, season, "opening_purse",
                  f"💰 Every franchise now has a purse of "
                  f"<b>{render_money(new_purse, symbol)}</b> — what has been "
                  f"spent already stays spent.",
                  by_tg_id=by_tg_id, by_admin=True)
    return changed


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


def role_maximums(season):
    """``{"Bowler": 8}`` — the roles that have a ceiling, and what it is.

    **Sparse on purpose.** A role missing from this map has no ceiling; a role
    mapped to ``0`` has a ceiling of none-at-all, which is a real rule an admin
    may want ("no specialist keepers in this pool"). A dense map — one entry per
    role, blank meaning "no rule" — cannot hold both answers, which is why
    ``set_role_rules`` below takes ``None`` for "no rule" and writes nothing.
    """
    raw = _loads(getattr(season, "role_maximums_json", None), {})
    out = {}
    for key, value in (raw or {}).items():
        number = _as_int(value, -1)
        if number >= 0:
            out[str(key)] = number
    return out


def set_role_rules(session, season, minimums, maximums):
    """Save both ends of the role rule together, because they constrain each other.

    ``minimums`` and ``maximums`` are ``{role: count or None}``; ``None`` — and,
    for a minimum, ``0`` — means "no rule about this role". They are saved in one
    call rather than two because every check worth making is between them: a
    minimum above its own maximum is unsatisfiable, and minimums that add up to
    more than the squad cap are a squad nobody can ever legally finish. Both are
    refused here, at the one moment an admin can still fix them, rather than
    surfacing as a bid refusal in the middle of a live lot.
    """
    lows, highs = {}, {}
    for role, value in (minimums or {}).items():
        number = _as_int(value, 0)
        if number > 0:
            lows[str(role)] = number
    for role, value in (maximums or {}).items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        number = _as_int(value, -1)
        if number >= 0:
            highs[str(role)] = number

    for role, low in sorted(lows.items()):
        high = highs.get(role)
        if high is not None and high < low:
            raise AuctionError(
                f"{role}: the minimum ({low}) is above the maximum ({high}). "
                f"A squad cannot satisfy both.")

    cap = max(0, _as_int(season.max_squad_size, 0))
    owed = sum(lows.values())
    if cap and owed > cap:
        raise AuctionError(
            f"The role minimums add up to {owed} players, which is more than "
            f"the squad limit of {cap} — no squad could ever be legal. Raise "
            f"the maximum squad size or lower a minimum.")

    # A ceiling cannot be so low that the rest of the roles cannot fill the
    # squad's MINIMUM size. Only checked when every role is capped, because a
    # role with no ceiling can always take up the slack.
    roles_with_a_ceiling = set(highs)
    floor = max(0, _as_int(season.min_squad_size, 0))
    if floor and roles_with_a_ceiling >= set(SQUAD_ROLES):
        room = sum(highs.get(role, 0) for role in SQUAD_ROLES)
        if room < floor:
            raise AuctionError(
                f"The role maximums add up to {room} players, which is fewer "
                f"than the minimum squad size of {floor} — no squad could ever "
                f"be legal. Raise a maximum, or lower the minimum squad size.")

    season.role_minimums_json = _dumps(lows) if lows else None
    season.role_maximums_json = _dumps(highs) if highs else None
    session.flush()
    return lows, highs


def role_rule_line(season):
    """"Bowler 4-8, Wicket Keeper 1+" — both ends of the rule in one string.

    Empty when no role has a rule at all, so every caller can print it behind a
    plain truth test rather than each deciding what "no rules" looks like.
    """
    lows, highs = role_minimums(season), role_maximums(season)
    # The four known roles in the page's own order, then anything else a rule
    # names — a catalogue that grows a fifth role must not drop out of the line.
    extra = sorted((set(lows) | set(highs)) - set(SQUAD_ROLES))
    parts = []
    for role in SQUAD_ROLES + tuple(extra):
        low, high = lows.get(role), highs.get(role)
        if low is None and high is None:
            continue
        if low and high is not None:
            parts.append(f"{role} {low}-{high}")
        elif high is not None:
            parts.append(f"{role} up to {high}")
        else:
            parts.append(f"{role} {low}+")
    return ", ".join(parts)


def role_shortfall(session, season, franchise):
    """``(owed, over)`` — the roles this squad still needs, and any it has overrun.

    ``over`` should always be empty while the rules are enforced; it is reported
    anyway because a rule tightened mid-auction, or a franchise inheriting a
    squad from a removed one, can land a squad on the wrong side of a ceiling
    that nothing it does next can fix. A number an admin can see is a number an
    admin can correct.
    """
    counts = role_counts(session, franchise.id)
    owed = {role: need - counts.get(role, 0)
            for role, need in role_minimums(season).items()
            if counts.get(role, 0) < need}
    over = {role: counts.get(role, 0) - cap
            for role, cap in role_maximums(season).items()
            if counts.get(role, 0) > cap}
    return owed, over


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
    retention picker — and later, Right To Match — must not make.

    The *franchise* side has two routes. A season cloned from another carries
    ``carried_from_id`` on every franchise, which is an exact link and survives
    a franchise being renamed between seasons. Failing that we fall back to
    matching last season's team name to this season's franchise name, which is
    all a hand-linked season has — and which loses every holder, in silence,
    the moment somebody renames a side.

    Used by the retention picker (before any lot exists) and by
    ``link_previous_season`` (after the pool is built), so the two can never
    disagree about who held whom.
    """
    league_id = league_id or getattr(season, "previous_league_id", None)
    if not league_id:
        return {}
    links = previous_team_links(session, season, league_id)
    if not links:
        return {}
    by_team = {team.id: franchise for franchise, team in links.values()}
    rows = (session.query(ChallengePlayer.source_player_id, ChallengePlayer.team_id)
            .filter(ChallengePlayer.team_id.in_(list(by_team)),
                    ChallengePlayer.source_player_id.isnot(None)).all())
    return {source_id: by_team[team_id] for source_id, team_id in rows
            if team_id in by_team}


# Words a side's name carries or drops between seasons without becoming a
# different side: "Mumbai FC" is "Mumbai", "The Kochi XI" is "Kochi".
_TEAM_NOISE = {"the", "fc", "xi", "team", "club", "cc", "cricket"}


def _team_key(name):
    """A team name reduced to what identifies it, for matching across seasons."""
    import re
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in _TEAM_NOISE)


def league_teams(session, league_id):
    if not league_id:
        return []
    return (session.query(ChallengeTeam)
            .filter(ChallengeTeam.league_id == int(league_id))
            .order_by(ChallengeTeam.sort_order.asc(),
                      ChallengeTeam.name.asc()).all())


def previous_team_links(session, season, league_id=None):
    """``{franchise_id: (franchise, ChallengeTeam)}`` — who was who last season.

    Resolved in order of how much each route can be trusted:

    1. **The stored link**, ``previous_team_id``. Set by an admin with
       /aprevteam or the setup page, or pinned automatically when the league is
       linked. It always wins, which is what makes a rename harmless: once a
       franchise is tied to last season's team by id, neither side's name
       matters again.
    2. **The carried link** a cloned season has — ``carried_from_id`` to last
       season's franchise, whose team ``publish_to_league`` named after it.
    3. **The name**, forgivingly: case, punctuation and filler words such as
       FC / XI / Team are ignored, and ``short_name`` counts. Accepted only
       when it is unique both ways — one franchise, one team — because a guess
       between two would hand a squad to the wrong side in silence.

    A team is never given to two franchises.
    """
    league_id = league_id or getattr(season, "previous_league_id", None)
    teams = league_teams(session, league_id)
    if not teams:
        return {}
    by_id = {t.id: t for t in teams}
    here = franchises(session, season.id)
    links, claimed = {}, set()

    for franchise in here:
        team = by_id.get(getattr(franchise, "previous_team_id", None) or 0)
        if team is not None and team.id not in claimed:
            links[franchise.id] = (franchise, team)
            claimed.add(team.id)

    carried = {f.carried_from_id: f for f in here
               if f.carried_from_id and f.id not in links}
    if carried and getattr(season, "previous_season_id", None):
        team_by_exact = {}
        for team in teams:
            team_by_exact.setdefault((team.name or "").strip().lower(), []).append(team)
        for was in franchises(session, int(season.previous_season_id)):
            franchise = carried.get(was.id)
            found = team_by_exact.get((was.name or "").strip().lower(), [])
            found = [t for t in found if t.id not in claimed]
            if franchise is not None and len(found) == 1:
                links[franchise.id] = (franchise, found[0])
                claimed.add(found[0].id)

    def keys(*names):
        return {k for k in (_team_key(n) for n in names if n) if k}

    free_teams = [t for t in teams if t.id not in claimed]
    free_here = [f for f in here if f.id not in links]
    team_keys = {t.id: keys(t.name, t.short_name) for t in free_teams}
    here_keys = {f.id: keys(f.name, f.short_name) for f in free_here}
    for franchise in free_here:
        matches = [t for t in free_teams if team_keys[t.id] & here_keys[franchise.id]]
        if len(matches) != 1:
            continue
        team = matches[0]
        rivals = [f for f in free_here if team_keys[team.id] & here_keys[f.id]]
        if len(rivals) == 1 and team.id not in claimed:
            links[franchise.id] = (franchise, team)
            claimed.add(team.id)
    return links


def pin_previous_teams(session, season):
    """Store every link that resolves, so a later rename cannot undo it.

    Returns ``(linked, unlinked franchises, unclaimed teams)`` for the report.
    """
    links = previous_team_links(session, season)
    for franchise, team in links.values():
        franchise.previous_team_id = team.id
    session.flush()
    here = franchises(session, season.id)
    claimed = {team.id for _f, team in links.values()}
    return (links,
            [f for f in here if f.id not in links],
            [t for t in league_teams(session, season.previous_league_id)
             if t.id not in claimed])


def set_previous_team(session, season, franchise, team_name):
    """Tie a franchise to last season's team by hand — or ``none`` to clear.

    For the side that changed its name: "Kochi" this season was "Kochi
    Tuskers" last season, and no name rule should be trusted to know that.
    """
    if not season.previous_league_id:
        raise AuctionError("Link last season's league first — /aprevious "
                           "<league>.")
    text = (team_name or "").strip()
    if text.lower() in ("none", "clear", "-", "off"):
        franchise.previous_team_id = None
        session.flush()
        return None
    teams = league_teams(session, season.previous_league_id)
    wanted = text.lower()
    team = None
    if text.isdigit():
        team = next((t for t in teams if t.id == int(text)), None)
    if team is None:
        exact = [t for t in teams if (t.name or "").strip().lower() == wanted
                 or (t.short_name or "").strip().lower() == wanted]
        loose = [t for t in teams if _team_key(text)
                 and _team_key(text) in (_team_key(t.name), _team_key(t.short_name))]
        partial = [t for t in teams if wanted and wanted in (t.name or "").lower()]
        for pool in (exact, loose, partial):
            if len(pool) == 1:
                team = pool[0]
                break
            if len(pool) > 1:
                raise AuctionError("That could be " + ", ".join(
                    t.name for t in pool[:6]) + " — type more of the name.")
    if team is None:
        raise AuctionError(f"No team called “{text}” in last season's league. "
                           f"Its teams: " + ", ".join(t.name for t in teams[:20]))
    for other in franchises(session, season.id):
        if other.id != franchise.id and other.previous_team_id == team.id:
            raise AuctionError(f"{team.name} is already linked to "
                               f"{other.name}. Clear that first with "
                               f"/aprevteam {other.name} | none.")
    # A link another franchise only has by name gives way to an explicit one.
    franchise.previous_team_id = team.id
    session.flush()
    return team


# ── Retaining ────────────────────────────────────────────────────────

def _signing_lot(session, season, player, *, verb="retained"):
    """The lot a pre-auction signing will use — a queued one, or a new row.

    Shared by retention and by an expansion pick, which are the same operation
    with different eligibility (see ``_sign_before_auction``). An admin who
    built the pool first must not hit a wall, so a player already sitting in
    the queue is converted in place rather than refused. Anything further
    along than ``queued`` is somebody else's business and is refused **by
    name** — which is what stops a new side picking a player another franchise
    has already kept, the whole point of drafting from the un-retained pool.
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
    if lot.status == LOT_SOLD and lot.acquisition in (ACQ_RETAINED, ACQ_DRAFTED):
        holder = (session.query(AuctionFranchise)
                  .filter(AuctionFranchise.id == lot.sold_to_id).first())
        how = "retained" if lot.acquisition == ACQ_RETAINED else "drafted"
        raise AuctionError(f"{lot.name} has already been {how} by "
                           f"{holder.name if holder else 'another franchise'}.")
    raise AuctionError(f"{lot.name} is already {lot.status} in this auction and "
                       f"cannot be {verb}.")


def _sign_before_auction(session, season, franchise, player, price, *,
                         kind, counter, ledger_kind, verb, note,
                         event_kind, mark, word, detail=None,
                         now=None, by_tg_id=None):
    """Put a player on a squad before the auction opens, for a price.

    The shared body of **retention** and an **expansion pick**, which are the
    same operation with different eligibility (see the table in
    ``docs/franchise-auction.md``). Each caller does its own window and quota
    checks first, then hands the price here; everything from the squad cap
    down — the lot claim, the overseas cap, the purse, the reachability rule,
    the conditional debit, the ledger row and the announcement — is identical
    and lives once.

    ``counter`` is the column on the franchise that the signing spends:
    ``retained_count`` for a retention, ``draft_picks_used`` for a pick. It is
    incremented inside the same conditional UPDATE as the debit, so a purse
    that moved underneath the validation rolls the whole signing back rather
    than leaving a counter that moved and a player who did not.
    """
    now = now or datetime.utcnow()
    symbol = season.currency_label or "₹"

    size = int(franchise.squad_size or 0)
    if size + 1 > _as_int(season.max_squad_size, 0):
        raise AuctionError(f"{franchise.name} already has {size} players, "
                           f"which is the squad limit.")

    lot = _signing_lot(session, season, player, verb=f"{verb}ed")

    if lot.is_overseas:
        overseas_cap = max(0, _as_int(season.max_overseas, 0))
        if overseas_count(session, franchise.id) + 1 > overseas_cap:
            raise AuctionError(f"{franchise.name} is already at the overseas "
                               f"limit of {overseas_cap}.")

    # The role ceiling, not the whole rule: see ``check_role_ceiling``. A
    # franchise that could retain past a cap would take an illegal squad into
    # an auction that then refuses every bid which might fix it.
    check_role_ceiling(session, season, franchise, lot.category)

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
            f"{verb} this player for is {render_money(max(0, ceiling), symbol)}.")

    # Every write below is one transaction, and the debit is conditional for
    # the same reason a sale's is: if the purse moved under the validation a
    # few milliseconds ago, the whole signing rolls back rather than half
    # happening.
    column = getattr(AuctionFranchise, counter)
    debited = (session.query(AuctionFranchise)
               .filter(AuctionFranchise.id == franchise.id,
                       AuctionFranchise.purse_remaining_lakh >= price,
                       AuctionFranchise.squad_size < _as_int(season.max_squad_size, 0))
               .update({"purse_remaining_lakh":
                        AuctionFranchise.purse_remaining_lakh - price,
                        "squad_size": AuctionFranchise.squad_size + 1,
                        counter: column + 1},
                       synchronize_session=False))
    session.flush()
    session.expire_all()
    franchise = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == franchise.id).first())
    lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
    if not debited:
        raise AuctionError(f"{franchise.name} can no longer pay "
                           f"{render_money(price, symbol)} — nothing has been "
                           f"signed.")

    lot.status = LOT_SOLD
    lot.acquisition = kind
    lot.sold_to_id = franchise.id
    lot.sold_price_lakh = price
    lot.sold_at = now
    lot.deadline_at = None
    lot.going_stage = 0
    # A RETAINED player was, by definition, held by this franchise; recording
    # it keeps the Right To Match input complete even for players who will
    # never be RTM'd, and costs nothing. A DRAFTED one was not — an expansion
    # side is taking somebody nobody kept — so the field is left alone and
    # whoever really held him last season stays on the record.
    if kind == ACQ_RETAINED and lot.previous_franchise_id is None:
        lot.previous_franchise_id = franchise.id

    # Flushed before anything reads it back: this session is autoflush=False,
    # and ``retention_spent`` / ``retained`` / ``drafted`` / ``squad`` all find
    # a signing by QUERYING for ``status`` and ``acquisition``. Left pending,
    # the next one would price itself against a budget that had not noticed
    # this one — the bug class that bit phase 1 three times.
    session.flush()

    _ledger(session, franchise, ledger_kind, -price, lot=lot, note=note,
            by_tg_id=by_tg_id)
    log_event(session, season, event_kind,
              f"{mark} <b>{_e(franchise.name)}</b> {word} {_e(lot.name)} "
              f"({lot.rating} OVR) for {render_money(price, symbol)}.",
              lot=lot, franchise=franchise, by_tg_id=by_tg_id, by_admin=True,
              detail={"price_lakh": price, **(detail or {})})
    return lot


def retain(session, season, franchise, player, price_lakh=None, *,
           now=None, by_tg_id=None, slot_key=None, talk_rounds=None):
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

    # Dynamic retention prices by SLOT, not by order: the player fills the
    # slot his rating reaches and nothing goes under its floor. Checked here
    # rather than only in the negotiation so /aretainforce and the setup
    # page cannot step round the structure either.
    from services import retention_negotiation as RN
    dynamic = RN.is_dynamic(season)
    if dynamic:
        if price_lakh is None:
            slot = (RN.find_slot(season, slot_key)[1] if slot_key
                    else RN.slot_for(session, season, franchise, player.rating))
            price_lakh = slot["floor_lakh"]
        price = _as_int(price_lakh, -1)
        if price < 0:
            raise AuctionError("A retention price cannot be negative.")
        slot_key = RN.check_signing(session, season, franchise, player, price,
                                    slot_key)
    else:
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

    detail = {"slab": count + 1}
    if dynamic:
        detail = {"slot": slot_key}
        if talk_rounds:
            detail["rounds"] = int(talk_rounds)
    lot = _sign_before_auction(
        session, season, franchise, player, price,
        kind=ACQ_RETAINED, counter="retained_count",
        ledger_kind=LEDGER_RETENTION, verb="retain",
        note=f"Retained: {player.name}",
        event_kind="retained", mark="🔒", word="retain",
        detail=detail, now=now, by_tg_id=by_tg_id)
    if dynamic:
        lot.retention_slot = slot_key
        session.flush()
    return lot


# ── Expansion picks ──────────────────────────────────────────────────
#
# A side joining a league that already exists has no previous squad, so it
# retains nobody and would walk into the auction with an empty list while
# everyone else arrives with three players kept. The IPL solved this in 2022
# by letting Gujarat and Lucknow each take three players out of the pool
# nobody had retained, before the mega auction opened. This is that.
#
# Deliberately NOT built on the ``PlayerDraft`` feature, which owns the whole
# ``d*`` command namespace: that is a season object in its own right, and
# using it would mean standing a second season up beside the auction and
# reconciling two purses, two pools and two publish paths for the sake of a
# handful of picks. The money, the caps and the ledger a pick needs are all
# already here, on the franchise doing the picking.


def expansion_configured(season):
    return _as_int(getattr(season, "expansion_picks", 0), 0) > 0


def picks_left(franchise):
    if franchise is None:
        return 0
    return max(0, int(franchise.draft_picks_total or 0)
               - int(franchise.draft_picks_used or 0))


def drafted(session, franchise_id):
    """Every player this franchise took as an expansion pick, best first."""
    return (session.query(AuctionLot)
            .filter(AuctionLot.sold_to_id == franchise_id,
                    AuctionLot.status == LOT_SOLD,
                    AuctionLot.acquisition == ACQ_DRAFTED)
            .order_by(AuctionLot.rating.desc(), AuctionLot.name.asc()).all())


def pick_spent(session, franchise_id):
    """What a franchise has spent on picks, in lakh. Derived, not cached."""
    total = (session.query(func.coalesce(func.sum(AuctionLot.sold_price_lakh), 0))
             .filter(AuctionLot.sold_to_id == franchise_id,
                     AuctionLot.status == LOT_SOLD,
                     AuctionLot.acquisition == ACQ_DRAFTED).scalar())
    return int(total or 0)


def expansion_franchises(session, season):
    """The sides with no previous squad — the ones a pick is for.

    Derived, not flagged. A franchise is an expansion side exactly when the
    league this season follows records nobody as theirs, which is the same
    question ``previous_squad_map`` already answers for retention and Right To
    Match. One source of truth beats a checkbox an admin has to remember.

    With no previous league at all every side is new — a first season — and
    picks make no sense, so this comes back empty rather than handing the
    whole field free players.
    """
    if not getattr(season, "previous_league_id", None):
        return []
    held = previous_squad_map(session, season)
    have_squads = {f.id for f in held.values()}
    return [f for f in franchises(session, season.id)
            if f.id not in have_squads]


def deal_expansion_picks(session, season):
    """Give every expansion side the season's allowance. Returns them.

    Run when the allowance is set rather than at pick time, so an admin sees
    who is getting what before anybody picks — and a side that has already
    used one keeps what it has left, the same call ``set_rtm_rules`` makes
    about Right To Match cards.
    """
    allowance = _as_int(getattr(season, "expansion_picks", 0), 0)
    new_sides = expansion_franchises(session, season)
    for franchise in new_sides:
        if int(franchise.draft_picks_used or 0) == 0:
            franchise.draft_picks_total = allowance
    session.flush()
    return new_sides


def pick_order(session, season):
    """The sides holding picks, in seat order."""
    return [f for f in franchises(session, season.id)
            if int(f.draft_picks_total or 0) > 0]


def pick_schedule(session, season):
    """The whole running order, one entry per pick.

    **A snake**: round one runs down the seats, round two back up them, and so
    on, so the side picking last in one round picks first in the next. Straight
    repetition would hand the first seat the best player available in every
    round, which is the entire reason a draft snakes.

    Built whole rather than stepped through, because "whose turn is it" and
    "what is the running order" are then the same fact read two ways, and a
    schedule you can print is a schedule an owner can plan against.
    """
    order = pick_order(session, season)
    if not order:
        return []
    rounds = max(int(f.draft_picks_total or 0) for f in order)
    schedule = []
    for rnd in range(rounds):
        seats = order if rnd % 2 == 0 else list(reversed(order))
        for franchise in seats:
            if int(franchise.draft_picks_total or 0) > rnd:
                schedule.append(franchise)
    return schedule


def pick_turn(session, season):
    """Whose pick it is, or ``None`` once every allowance is spent.

    Derived from what has been used, not from a stored cursor: a cursor is one
    more thing to fall out of step with the picks it describes, and there is
    nothing here it would buy. Because a turn can only be spent in order — by
    picking or by being skipped, both of which bump ``draft_picks_used`` — the
    number already spent *is* the index into the schedule.
    """
    schedule = pick_schedule(session, season)
    made = sum(int(f.draft_picks_used or 0) for f in pick_order(session, season))
    return schedule[made] if made < len(schedule) else None


def _pick_window(session, season):
    """Refuse a pick that cannot happen at all, and say which reason."""
    if season.status != STATUS_SETUP:
        raise AuctionError("Expansion picks happen before the auction opens — "
                           "this one is " + str(season.status) + ".")
    # Picks come out of the pool nobody retained, so retention has to be over
    # first. Reusing retention's own lock rather than inventing a second
    # window with its own deadline: one switch, one thing to explain.
    if retention_configured(season) and not retention_locked(season):
        raise AuctionError("Retention is still open. A pick comes out of the "
                           "players nobody kept, so close retention first "
                           "with /aretlock on.")


def draft_pick(session, season, franchise, player, price_lakh=None, *,
               now=None, by_tg_id=None):
    """An expansion side signs a player before the auction, at a price.

    The same operation as a retention with a different eligibility rule, so it
    runs through the same body — see ``_sign_before_auction``. What is its own
    here is the turn, the allowance, and that the player must not already have
    been signed by anybody, which ``_signing_lot`` enforces and names.
    """
    now = now or datetime.utcnow()
    _pick_window(session, season)

    used = int(franchise.draft_picks_used or 0)
    if picks_left(franchise) <= 0:
        raise AuctionError(f"{franchise.name} has used all "
                           f"{int(franchise.draft_picks_total or 0)} of its "
                           f"picks.")

    turn = pick_turn(session, season)
    if turn is not None and turn.id != franchise.id:
        raise AuctionError(f"It is {turn.name}'s pick, not {franchise.name}'s.")

    price = (retention_price_for(season, used + 1) if price_lakh is None
             else _as_int(price_lakh, -1))
    if price < 0:
        raise AuctionError("A pick price cannot be negative.")

    return _sign_before_auction(
        session, season, franchise, player, price,
        kind=ACQ_DRAFTED, counter="draft_picks_used",
        ledger_kind=LEDGER_DRAFT, verb="draft",
        note=f"Expansion pick: {player.name}",
        event_kind="drafted", mark="🆕", word="draft",
        detail={"pick": used + 1}, now=now, by_tg_id=by_tg_id)


def skip_pick(session, season, franchise, *, by_tg_id=None):
    """Burn a turn without signing anybody.

    The allowance is spent either way, which is the point: a side that does
    not want its pick must not be able to stall the order by never taking it.
    """
    _pick_window(session, season)
    if picks_left(franchise) <= 0:
        raise AuctionError(f"{franchise.name} has no picks left to skip.")
    turn = pick_turn(session, season)
    if turn is not None and turn.id != franchise.id:
        raise AuctionError(f"It is {turn.name}'s pick, not {franchise.name}'s.")

    franchise.draft_picks_used = int(franchise.draft_picks_used or 0) + 1
    # Flushed before the announcement reads the turn back: this session is
    # autoflush=False and ``pick_turn`` QUERIES the franchises.
    session.flush()
    following = pick_turn(session, season)
    log_event(session, season, "pick_skipped",
              f"⏭ <b>{_e(franchise.name)}</b> pass on an expansion pick."
              + (f" Over to {_e(following.name)}." if following is not None
                 else " That is every pick used."),
              franchise=franchise, by_tg_id=by_tg_id, by_admin=True)
    return franchise


def undo_pick(session, season, lot, *, by_tg_id=None):
    """Put a picked player back in the pool, money and turn both returned.

    Returning the pick is the whole reason this is not ``undo_sale``: an undone
    pick that quietly ate one leaves a side a player short for somebody else's
    slip.
    """
    if lot is None or lot.acquisition != ACQ_DRAFTED or lot.status != LOT_SOLD:
        raise AuctionError(f"{lot.name if lot else 'That player'} was not an "
                           f"expansion pick.")
    if season.status not in (STATUS_SETUP, STATUS_PAUSED):
        raise AuctionError("Undo a pick before the auction opens — this one "
                           "is " + str(season.status) + ".")

    franchise = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == lot.sold_to_id).first())
    price = int(lot.sold_price_lakh or 0)

    if franchise is not None:
        (session.query(AuctionFranchise)
         .filter(AuctionFranchise.id == franchise.id)
         # ``case`` rather than a two-argument ``max``: that spelling is
         # SQLite's, and Postgres calls it ``greatest``. The same call
         # ``unretain`` makes, for the same reason.
         .update({"purse_remaining_lakh":
                  AuctionFranchise.purse_remaining_lakh + price,
                  "squad_size": case((AuctionFranchise.squad_size > 0,
                                      AuctionFranchise.squad_size - 1), else_=0),
                  "draft_picks_used":
                      case((AuctionFranchise.draft_picks_used > 0,
                            AuctionFranchise.draft_picks_used - 1), else_=0)},
                 synchronize_session=False))
        session.flush()
        session.expire_all()
        franchise = (session.query(AuctionFranchise)
                     .filter(AuctionFranchise.id == franchise.id).first())
        lot = session.query(AuctionLot).filter(AuctionLot.id == lot.id).first()
        _ledger(session, franchise, LEDGER_REFUND, price, lot=lot,
                note=f"Pick undone: {lot.name}", by_tg_id=by_tg_id)

    lot.status = LOT_QUEUED
    lot.acquisition = ACQ_AUCTION
    lot.sold_to_id = None
    lot.sold_price_lakh = None
    lot.sold_at = None
    session.flush()

    log_event(session, season, "pick_undone",
              f"↩️ The expansion pick on {_e(lot.name)} is undone — "
              f"{render_money(price, season.currency_label)} back in "
              f"{_e(franchise.name) if franchise else 'the'} purse, and the "
              f"pick returned. He is in the pool again.",
              lot=lot, franchise=franchise, by_tg_id=by_tg_id, by_admin=True)
    return lot


def set_expansion_picks(session, season, count):
    """Set the season's allowance and deal it out."""
    season.expansion_picks = max(0, _as_int(count, 0))
    session.flush()
    return deal_expansion_picks(session, season)


def set_franchise_picks(session, season, franchise, count):
    """One side's own allowance, never below what it has already used."""
    franchise.draft_picks_total = max(int(franchise.draft_picks_used or 0),
                                      _as_int(count, 0))
    session.flush()
    return franchise


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
    lot.retention_slot = None

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
    """How the pool stands, with **pre-auction signings counted separately**.

    ``sold`` and ``total`` are auction figures. A retained player — and an
    expansion side's pick — is a sold lot (see the retention section) but is
    not something the auction did, and folding them in would have the board
    announce "3/20 lots resolved" before the first lot ever opened, and the
    admin list count them as purchases. They get their own keys, and ``total``
    counts only what the auction actually has to get through.

    ``retained`` and ``drafted`` are kept apart from each other too: they are
    different facts about how a squad was assembled, and a league that adds a
    team wants to see which is which.
    """
    rows = (session.query(AuctionLot.status, AuctionLot.acquisition,
                          func.count(AuctionLot.id))
            .filter(AuctionLot.season_id == season_id)
            .group_by(AuctionLot.status, AuctionLot.acquisition).all())
    counts, before = {}, {ACQ_RETAINED: 0, ACQ_DRAFTED: 0}
    for status, acquisition, n in rows:
        n = int(n)
        # A Right To Match is deliberately NOT here: the room watched a clock
        # run and money moved, so it is auction progress.
        if status == LOT_SOLD and acquisition in ACQ_PRE_AUCTION:
            before[acquisition] += n
            continue
        counts[status] = counts.get(status, 0) + n
    aside = ("retained", "drafted")
    counts["total"] = sum(v for k, v in counts.items() if k not in aside)
    counts["retained"] = before[ACQ_RETAINED]
    counts["drafted"] = before[ACQ_DRAFTED]
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

    # Who held each of these last season, stamped as the lot is created.
    #
    # ``link_previous_season`` can only stamp lots that already exist, and the
    # natural order is the other way round: follow a league first, build the
    # pool after — which is exactly what /aclone and the setup page tell an
    # admin to do. Left to that, every lot came out unstamped and Right To
    # Match found nobody, in silence. Done here it cannot depend on the order
    # at all.
    held = (previous_squad_map(session, season)
            if getattr(season, "previous_league_id", None) else {})

    for player in players:
        if player is None or player.id in existing:
            skipped += 1
            continue
        existing.add(player.id)
        was = held.get(player.id)
        session.add(AuctionLot(
            season_id=season.id, player_id=player.id,
            previous_franchise_id=was.id if was is not None else None,
            name=(player.name or "")[:150], rating=player.rating or 0,
            category=player.category or "Batsman",
            country=player.country or "Unknown",
            is_overseas=bool(home and (player.country or "").strip().lower() != home),
            version=player.version, bat_hand=player.bat_hand or "Right",
            bowl_hand=player.bowl_hand or "Right",
            bowl_style=player.bowl_style or "Medium Pacer",
            bat_rating=player.bat_rating or 0, bowl_rating=player.bowl_rating or 0,
            # Clipped here rather than by each caller: the column is 40 and the
            # name is echoed back into pages and buttons that then match it
            # exactly, so it has to be cut once, on the way in, and never again.
            set_name=((set_name or "").strip()[:40] or None), lot_no=lot_no,
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


def unsold(session, season_id):
    """Every player the room passed on, in the order they were offered."""
    return (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season_id,
                    AuctionLot.status == LOT_UNSOLD)
            .order_by(AuctionLot.lot_no.asc()).all())


def relist_all(session, season, *, lot_ids=None, by_tg_id=None,
               set_name=None, automatic=False):
    """The accelerated round: every unsold player back into the queue at once.

    Re-listing one at a time is the same work done N times, and by the time it
    matters — the end of a long auction, with a room waiting — N is usually
    most of what went unsold in the first hour. Returns the lots re-listed.

    They keep their original order and their base price. An accelerated round
    is a second chance at the same player, not a discount: dropping the floor
    would quietly re-price every lot the room had already judged, and an admin
    who wants that has /alotprice.

    ``lot_ids`` narrows it to a chosen few, which is what the console's
    tick-boxes send — the same call either way, so the two surfaces cannot
    drift.

    ``set_name`` files every re-listed player under one set — the automatic
    round uses the ⚡ Accelerated set, so ``/asets`` shows it as a set of its
    own. ``automatic`` is the queue running dry by itself (see
    ``complete_if_done``) rather than an admin asking: it words the
    announcement for a room that did not see it coming.
    """
    if season.status == STATUS_CANCELLED:
        raise AuctionError("This auction was cancelled.")

    rows = unsold(session, season.id)
    if not rows:
        raise AuctionError("Nothing went unsold — there is nothing to re-list.")
    if lot_ids is not None:
        wanted = {int(i) for i in lot_ids}
        rows = [lot for lot in rows if lot.id in wanted]
        if not rows:
            # Said apart from the case above on purpose: "nothing went unsold"
            # when a dozen players did is the kind of wrong answer that sends
            # an admin looking for a bug in the wrong place.
            raise AuctionError("None of those are unsold in this auction.")

    next_no = _next_lot_no(session, season.id)
    for offset, lot in enumerate(rows):
        lot.status = LOT_QUEUED
        lot.lot_no = next_no + offset
        if set_name:
            lot.set_name = set_name[:40]
        lot.current_bid_lakh = None
        lot.current_bidder_id = None
        lot.deadline_at = None
        lot.going_stage = 0
        lot.extensions_used = 0
        lot.rtm_stage = None
        lot.rtm_base_bid_lakh = None

    # A completed auction is the normal place to call this from — the last lot
    # resolving is what finishes it, and "who is left" is only answerable once
    # it has. It comes back PAUSED rather than live: nobody is watching yet,
    # and starting a clock on a lot in an empty room is how a player goes for
    # his base price to the one franchise still looking at their phone.
    reopened = season.status == STATUS_COMPLETED
    if reopened:
        season.status = STATUS_PAUSED
        season.current_lot_id = None
    # One accelerated round is what the automatic path promises; an admin who
    # runs one by hand has had it, so the queue running dry afterwards
    # finishes the auction instead of starting another.
    season.accelerated_done = 1

    # Flushed before the announcement counts anything: this session is
    # autoflush=False and pool_counts queries the very rows just changed.
    session.flush()

    note = (" The auction is open again, paused — /astart when the room is "
            "ready." if reopened else "")
    published = (" Squads were already published, so re-publish when this "
                 "round is done." if season.published_at is not None else "")
    if automatic:
        headline = (f"⚡ <b>Accelerated round</b> — the main pool is done. "
                    f"{len(rows)} unsold "
                    f"{'player comes' if len(rows) == 1 else 'players come'} "
                    f"back as the <b>{_e(set_name or ACCELERATED_SET)}</b> set, "
                    f"at the same base price.")
    else:
        headline = (f"⚡ <b>Accelerated round</b> — {len(rows)} unsold "
                    f"{'player goes' if len(rows) == 1 else 'players go'} back "
                    f"into the pool.{note}{published}")
    log_event(session, season, "relist_all", headline,
              by_tg_id=by_tg_id, by_admin=not automatic,
              detail={"count": len(rows),
                      "lot_ids": [lot.id for lot in rows]})
    return rows


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
    if season.previous_league_id != int(league_id):
        # A different league: last season's team ids mean nothing any more.
        for franchise in franchises(session, season.id):
            franchise.previous_team_id = None
        session.flush()
    season.previous_league_id = int(league_id)
    pin_previous_teams(session, season)
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
                        "last_bid_at": None,
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
              f"{render_money(lot.base_price_lakh, season.currency_label)} · "
              f"{_e(set_label(lot))}.",
              lot=lot, detail={"set": set_label(lot)})
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
    # The main pool is done but players went unsold: they get one more go, as
    # the Accelerated set, before anything is final. Once only — the players
    # the room passes on twice are what the auto-fill below hands out.
    if (_as_int(getattr(season, "auto_accelerated", 1), 1)
            and not _as_int(getattr(season, "accelerated_done", 0), 0)
            and unsold(session, season.id)):
        relist_all(session, season, set_name=ACCELERATED_SET, automatic=True)
        session.flush()
        return None
    # Squads still under the minimum are topped up for free from whatever is
    # left unsold, inside the squad and overseas caps — but only once the
    # unsold players have had their second round. With the automatic round
    # switched off and no /aaccel run, the admin has chosen to leave them
    # unsold, and handing them out anyway would overrule that.
    if _as_int(getattr(season, "accelerated_done", 0), 0):
        autofill_short_squads(session, season)
    season.status = STATUS_COMPLETED
    season.current_lot_id = None
    _focus_changed(season, session)
    _auction_wrap_news(session, season)
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
        _snapshot_opening_order(session, season)

    resuming = season.status == STATUS_PAUSED
    season.status = STATUS_LIVE
    _focus_changed(season, session)
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
    _focus_changed(season, session)
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
    _focus_changed(season, session)
    log_event(session, season, "season_cancelled",
              "🚫 The auction has been cancelled.",
              by_tg_id=by_tg_id, by_admin=True)
    return season


def _snapshot_opening_order(session, season):
    """Remember the queue as it stands when the auction first opens.

    What ``restart_auction`` puts back. Taken once, on the first start: a
    resume is not an opening, and by then the accelerated round may already
    have renumbered and re-filed the players it relisted.
    """
    session.flush()
    rows = (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season.id,
                    AuctionLot.status.in_((LOT_QUEUED,) + LOT_LIVE))
            .order_by(AuctionLot.lot_no.asc()).all())
    season.opening_order_json = _dumps([[lot.id, lot.set_name]
                                        for lot in rows])


def restart_auction(session, season, *, now=None, by_tg_id=None):
    """Start the whole auction again from its first player.

    Every player who came through the auction — bought, matched with a Right
    To Match, handed out by the auto-fill, passed unsold, on the block, or
    still waiting — goes back into the queue in the order the auction opened
    with, with no bids on him. Every franchise gets back what it spent on
    them, its squad count and its RTM cards; the accelerated round is owed
    again. What was settled *before* the auction is left alone: retained
    players and expansion picks stay signed, and a withdrawn player stays
    withdrawn (``/areinstate`` brings one back).

    The auction comes back LIVE with the first player on the block — that is
    the command. Refused once squads are published: the league is already
    playing with them.

    Returns ``(first lot or None, number of players returned to the pool)``.
    """
    now = now or datetime.utcnow()
    if season.status == STATUS_SETUP:
        raise AuctionError("The auction has not started yet — /astart opens "
                           "it from the first player.")
    if season.published_at is not None:
        raise AuctionError("This auction's squads have been published — the "
                           "league is playing with them, so it cannot be "
                           "restarted.")
    if not season.chat_id:
        raise AuctionError("Bind the auction to a group first with /abind.")
    session.flush()

    rows = (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season.id,
                    AuctionLot.status != LOT_WITHDRAWN)
            .all())
    rows = [lot for lot in rows
            if not (lot.status == LOT_SOLD
                    and (lot.acquisition or ACQ_AUCTION) in ACQ_PRE_AUCTION)]
    if not rows:
        raise AuctionError("There is nobody in the pool to restart with.")

    # ── Money, squads and cards back ──────────────────────────────────
    field = {f.id: f for f in franchises(session, season.id)}
    refunds = {}
    for lot in rows:
        if lot.status != LOT_SOLD or lot.sold_to_id not in field:
            continue
        back = refunds.setdefault(lot.sold_to_id, [0, 0, 0])
        back[0] += int(lot.sold_price_lakh or 0)
        back[1] += 1
        if lot.acquisition == ACQ_RTM:
            back[2] += 1
    for franchise_id, (money, players, cards) in refunds.items():
        franchise = field[franchise_id]
        franchise.purse_remaining_lakh = (int(franchise.purse_remaining_lakh or 0)
                                          + money)
        franchise.squad_size = max(0, int(franchise.squad_size or 0) - players)
        franchise.rtm_cards_used = max(0, int(franchise.rtm_cards_used or 0)
                                       - cards)
        _ledger(session, franchise, LEDGER_REFUND, money,
                note=f"Auction restarted — {players} "
                     f"{'player' if players == 1 else 'players'} back in the "
                     f"pool", by_tg_id=by_tg_id)

    # ── Every bid on them void ────────────────────────────────────────
    ids = [lot.id for lot in rows]
    (session.query(AuctionBid)
     .filter(AuctionBid.lot_id.in_(ids), AuctionBid.is_void.is_(False))
     .update({"is_void": True, "voided_by_tg_id": by_tg_id},
             synchronize_session=False))

    # ── The order the auction opened with ─────────────────────────────
    opening = _loads(getattr(season, "opening_order_json", None), [])
    by_id = {lot.id: lot for lot in rows}
    ordered, seen = [], set()
    for entry in opening:
        try:
            lot_id, set_name = int(entry[0]), entry[1]
        except (TypeError, ValueError, IndexError):
            continue
        lot = by_id.get(lot_id)
        if lot is None or lot_id in seen:
            continue
        lot.set_name = set_name
        ordered.append(lot)
        seen.add(lot_id)
    ordered += sorted((lot for lot in rows if lot.id not in seen),
                      key=lambda lot: lot.lot_no)

    # The same lot numbers, handed out again in that order. Parked on
    # negatives first: ``(season_id, lot_no)`` is unique, and swapping two
    # numbers in place would collide halfway through.
    numbers = sorted(lot.lot_no for lot in ordered)
    for offset, lot in enumerate(ordered):
        lot.lot_no = -(offset + 1)
    session.flush()
    for lot, number in zip(ordered, numbers):
        lot.lot_no = number
        lot.status = LOT_QUEUED
        lot.deadline_at = None
        lot.going_stage = 0
        lot.extensions_used = 0
        lot.current_bid_lakh = None
        lot.current_bidder_id = None
        lot.bid_count = 0
        lot.opened_at = None
        lot.times_unsold = 0
        lot.sold_to_id = None
        lot.sold_price_lakh = None
        lot.sold_at = None
        lot.acquisition = ACQ_AUCTION
        lot.rtm_stage = None
        lot.rtm_base_bid_lakh = None
        lot.rtm_offered_at = None
        lot.rtm_matched_by_id = None
        lot.last_bid_at = None

    season.accelerated_done = 0
    season.current_lot_id = None
    season.board_lot_id = None
    season.board_rendered_bid_count = 0
    season.status = STATUS_LIVE
    _focus_changed(season, session)
    session.flush()
    log_event(session, season, "season_restarted",
              f"🔄 <b>{_e(season.name)}</b> is starting again from the first "
              f"player — {len(ordered)} "
              f"{'player is' if len(ordered) == 1 else 'players are'} back in "
              f"the pool, and every franchise has its purse back.",
              by_tg_id=by_tg_id, by_admin=True,
              detail={"players": len(ordered),
                      "refunds": {str(k): v[0] for k, v in refunds.items()}})
    return (open_next_lot(session, season, now=now), len(ordered))


COUNTDOWN_MAX = 10
BID_GAP_MAX = 10


def bid_gap_seconds(season):
    """The quiet gap after every bid — 0 when it is off, which is the default.

    Off by default because a room-wide gap is the opposite of an auction: two
    franchises going for the same player at the same moment is the whole
    point, and a gap turns the second of them away. Spam is stopped per
    PERSON instead (``services/auction_antispam``), which never holds one team
    back because another one just bid. ``/abidgap N`` still turns it on.
    """
    return max(0, min(BID_GAP_MAX,
                      _as_int(getattr(season, "bid_gap_seconds", 0), 0)))


def set_bid_gap(session, season, seconds):
    raw = str(seconds or "").strip().lower()
    value = 0 if raw in ("off", "0", "no", "none") else _as_int(raw, -1)
    if value < 0 or value > BID_GAP_MAX:
        raise AuctionError(f"The gap after a bid runs between 1 and "
                           f"{BID_GAP_MAX} seconds — or off.")
    season.bid_gap_seconds = value
    return value


class BidTooSoon(AuctionError):
    """A bid inside the quiet gap after the last one."""


class PriceMoved(AuctionError):
    """The bid was under the next legal bid — someone else got there first.

    Its own class so a bare ``/bid`` (which means "the next minimum", whatever
    that is by now) can simply try again at the fresh minimum, while a bid that
    named a number or a button that carried one is never raised on the
    bidder's behalf.
    """


def bid_holder_message(session, season, lot):
    """Who holds the lot right now — what a bid inside the gap is told."""
    team = None
    if lot is not None and lot.current_bidder_id is not None:
        team = (session.query(AuctionFranchise)
                .filter(AuctionFranchise.id == lot.current_bidder_id).first())
    lines = ["⏳ Current bid holder",
             f"Player: {lot.name if lot is not None else '?'}",
             f"Team: {team.name if team is not None else '?'}"]
    if lot is not None and lot.current_bid_lakh is not None:
        lines.append(f"Bid: {render_money(lot.current_bid_lakh, season.currency_label)}")
    lines.append("Bid again in a moment.")
    return "\n".join(lines)


def _in_bid_gap(season, lot, franchise, now, *, by_admin=False):
    """True while the last bid on this lot is younger than the gap.

    The franchise already holding the lot is left to ``validate_bid``, whose
    "you already hold the top bid" is the more useful answer. An admin entering
    a bid is correcting the room, not racing it.
    """
    gap = bid_gap_seconds(season)
    if (not gap or by_admin or lot is None or lot.status != LOT_ON_BLOCK
            or lot.last_bid_at is None
            or lot.current_bidder_id == franchise.id):
        return False
    return now < lot.last_bid_at + timedelta(seconds=gap)


def countdown_seconds(season):
    """How long the hammer countdown runs — 0 when it is off."""
    return max(0, min(COUNTDOWN_MAX,
                      _as_int(getattr(season, "countdown_seconds", 3), 3)))


def set_countdown(session, season, seconds):
    raw = str(seconds or "").strip().lower()
    value = 0 if raw in ("off", "0", "no", "none") else _as_int(raw, -1)
    if value < 0 or value > COUNTDOWN_MAX:
        raise AuctionError(f"The countdown runs between 1 and {COUNTDOWN_MAX} "
                           f"seconds — or off.")
    staged = staged_clock(season)
    if staged is not None and value >= staged[3]:
        raise AuctionError(f"On the staged clock the countdown must be under "
                           f"the 2nd warning ({staged[3]}s).")
    season.countdown_seconds = value
    return value


def _focus_changed(season, session=None):
    """Drop the focus-mode gate's cached answer for this auction's group.

    The gate caches "is this chat locked?" for a few seconds so it can be asked
    on every command in every group without a query each time. Every call that
    moves an auction's status or its focus switch calls this, so /astart,
    /apause and /afocus land in the room at once rather than at the end of a
    TTL.

    Dropped **twice** when a session is given: now, and again when that session
    commits. The second is what closes the window between the two — a command
    arriving in those milliseconds would otherwise read the state this call is
    in the middle of changing and cache it for a full TTL. A rolled-back
    transaction simply never fires the second one, and has already invalidated
    the entry it might have dirtied.

    Missing a call site only delays a change by one window, so this is allowed
    to fail quietly rather than take a sale down with it.
    """
    chat_id = getattr(season, "chat_id", None)
    try:
        from services.auction_focus import invalidate
        invalidate(chat_id)
        if session is not None:
            from sqlalchemy import event

            @event.listens_for(session, "after_commit", once=True)
            def _drop_it_again(_session):      # pragma: no cover - trivial
                invalidate(chat_id)
    except Exception:
        logger.debug("auction focus invalidation failed", exc_info=True)


def focus_mode_on(season):
    """True when this auction locks its group to auction commands while it runs.

    The rule itself lives in ``services/auction_focus.py``; this is the reader
    every card and every template goes through, so nobody has to remember that
    the column is an integer or that NULL means ON.
    """
    from services.auction_focus import focus_mode_on as _on
    return _on(season)


def set_focus_mode(session, season, on, *, by_tg_id=None, quiet=False):
    """Turn focus mode on or off, and tell the room which way it went.

    Announced rather than silent: it changes what every command in the group
    does, and a room that finds out by being refused has been told the hard
    way. ``quiet`` is for the website, which says so on the page instead.
    """
    on = bool(on)
    was = focus_mode_on(season)
    season.focus_mode = 1 if on else 0
    _focus_changed(season, session)
    if not quiet and was != on:
        log_event(session, season, "focus_mode",
                  ("🔒 Auction focus is <b>on</b>: while this auction is "
                   "running, only auction commands work in this group. "
                   "Everything else still works in a DM with me."
                   if on else
                   "🔓 Auction focus is <b>off</b>: every other command works "
                   "in this group again, auction or no auction."),
                  by_tg_id=by_tg_id, by_admin=True)
    return on


def direct_bids_on(season):
    """True when a bidder may name their own number (``/bid 12``).

    OFF means the ladder and nothing else: bare ``/bid`` takes the next
    minimum, the board's quick-bid buttons offer the same steps, and a typed
    amount is refused. An integer column read the way ``focus_mode`` is, NULL
    included, so an auction written before the switch existed keeps the
    behaviour it has always had.
    """
    if season is None:
        return True
    raw = getattr(season, "direct_bids", 1)
    if raw is None:
        return True
    try:
        return bool(int(raw))
    except (TypeError, ValueError):
        return bool(raw)


def set_direct_bids(session, season, on, *, by_tg_id=None, quiet=False):
    """Turn typed bid amounts on or off, and tell the room which way it went.

    Announced rather than silent, for focus mode's reason: it changes what
    ``/bid 12`` does for everybody in the room, and a franchise that finds out
    by being refused on a thirty-second clock has been told the hard way.
    """
    on = bool(on)
    was = direct_bids_on(season)
    season.direct_bids = 1 if on else 0
    if not quiet and was != on:
        log_event(session, season, "direct_bids",
                  ("🔢 Direct bids are <b>on</b>: name your own number with "
                   "<code>/bid 12</code>, or send bare <code>/bid</code> for "
                   "the next minimum."
                   if on else
                   "🪜 Direct bids are <b>off</b>: every raise is one step. "
                   "Send bare <code>/bid</code> — or tap the board — to bid "
                   "the next minimum. A typed amount will be refused."),
                  by_tg_id=by_tg_id, by_admin=True)
    return on


def set_timer(session, season, seconds):
    """``/atimer``: one number (the classic clock's length), five (the staged
    clock: open, reset, 1st warning, 2nd warning, final count — the last may
    be ``off``), or ``classic`` to go back. Returns the opening seconds."""
    raw = str(seconds if seconds is not None else "").replace(",", " ").split()
    if len(raw) == 1 and raw[0].lower() in ("classic", "off", "legacy"):
        season.reset_seconds = 0
        season.warn1_seconds = 0
        season.warn2_seconds = 0
        return int(season.bid_seconds or 30)
    if len(raw) in (4, 5):
        count_raw = raw[4].lower() if len(raw) == 5 else str(countdown_seconds(season))
        count = 0 if count_raw in ("off", "no", "none") else _as_int(count_raw, -1)
        parts = [_as_int(x, -1) for x in raw[:4]]
        if min(parts) < 0 or count < 0:
            raise AuctionError("Every part is a number of seconds — e.g. "
                               "<code>/atimer 60 40 20 10 5</code>.")
        open_s, reset, warn1, warn2 = parts
        validate_staged(open_s, reset, warn1, warn2, count)
        (season.bid_seconds, season.reset_seconds, season.warn1_seconds,
         season.warn2_seconds, season.countdown_seconds) = (
            open_s, reset, warn1, warn2, count)
        return open_s
    if len(raw) != 1:
        raise AuctionError("Use <code>/atimer 45</code>, or the staged clock "
                           "<code>/atimer 60 40 20 10 5</code>.")
    seconds = _as_int(raw[0], 0)
    if seconds < 5 or seconds > 600:
        raise AuctionError("A lot timer runs between 5 and 600 seconds.")
    staged = staged_clock(season)
    if staged is not None:
        validate_staged(seconds, *staged[1:])
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

def lot_bidder(session, lot_id, franchise_id):
    """The Telegram id that claimed this franchise's bidding on this lot.

    The first person from the franchise to bid on a lot holds it for that lot;
    ``None`` while nobody has bid yet. Read off ``auction_bids`` rather than
    stored on a column, which gives two properties for free: it resets by
    itself when the next lot opens, and ``/aundobid`` — which voids a bid
    rather than deleting it — releases the claim when the last of that
    franchise's bids on the lot goes.
    """
    row = (session.query(AuctionBid.by_tg_id)
           .filter(AuctionBid.lot_id == lot_id,
                   AuctionBid.franchise_id == franchise_id,
                   AuctionBid.is_void.is_(False),
                   AuctionBid.by_tg_id.isnot(None))
           .order_by(AuctionBid.id.asc()).first())
    return int(row[0]) if row and row[0] is not None else None


def person_name(session, tg_id):
    """A name for a Telegram id, for a refusal that has to name somebody.

    Falls back to the id itself: "222 is bidding for Mumbai" is still an
    answer the other co-owner can act on, where "somebody" is not.
    """
    if tg_id is None:
        return "somebody"
    try:
        from models import User
        user = (session.query(User)
                .filter(User.telegram_id == int(tg_id)).first())
    except Exception:
        logger.debug("bidder name lookup failed", exc_info=True)
        user = None
    if user is not None:
        name = (user.first_name or "").strip() or (user.username or "").strip()
        if name:
            return name
    return str(tg_id)


def validate_bid(session, season, lot, franchise, amount_lakh, *, now=None,
                 by_tg_id=None, by_admin=False):
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

    # One pair of hands per franchise per lot. Two co-owners bidding the same
    # player is not two tactics, it is one franchise racing itself up its own
    # price — and the loser of that race is always the franchise. Whoever bids
    # first holds this lot; the next lot is open to either of them again.
    # ``by_admin`` is exempt: an admin bidding from the console is acting FOR
    # the franchise, and the announcement says so.
    if by_tg_id is not None and not by_admin:
        holder = lot_bidder(session, lot.id, franchise.id)
        if holder is not None and holder != int(by_tg_id):
            raise AuctionError(
                f"{person_name(session, holder)} is bidding for "
                f"{franchise.name} on this lot — only one of you at a time, "
                f"or you end up raising each other. The next lot is open to "
                f"either of you.")

    minimum = next_min_bid(season, lot)
    if amount < minimum:
        if lot.current_bid_lakh is None:
            raise AuctionError(f"{lot.name}'s base price is "
                               f"{render_money(minimum, symbol)} — a first bid "
                               f"cannot be under it.")
        raise PriceMoved(f"The bid stands at "
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

    check_role_rules(session, season, franchise, lot)
    return amount


def check_role_rules(session, season, franchise, lot):
    """Refuse a buy that breaks either end of the role rule.

    **The two ends are enforced differently, and they have to be.** A *maximum*
    is a fact about the squad in front of you — an eighth bowler in a
    seven-bowler squad is over the line the moment it is bought, so the refusal
    is a plain count. A *minimum* is a promise about a squad that does not exist
    yet, so it is enforced as **reachability**, the same shape as the draft's
    rule and for the same reason: a squad that owes a keeper with one slot left
    must spend that slot on a keeper, and the refusal has to land *at* that bid
    rather than as a complaint about a finished squad nobody can act on.

    A minimum's reachability count deliberately reads the ceilings too: a slot
    is only free to fix a shortfall if some role that is still short can
    actually take it.

    Both are off by default — the two JSON columns are empty until an admin sets
    them, and then this is the only place a bid meets either.
    """
    counts = dict(role_counts(session, franchise.id))
    role = lot.category
    check_role_ceiling(session, season, franchise, role, counts=counts)

    minimums = role_minimums(season)
    if not minimums:
        return
    # Buying this player fills one slot; count what the squad would then owe.
    counts[role] = counts.get(role, 0) + 1
    owed = sum(max(0, need - counts.get(name, 0))
               for name, need in minimums.items())
    slots_left = int(season.max_squad_size or 0) - (int(franchise.squad_size or 0) + 1)
    if owed > slots_left:
        short = [name for name, need in minimums.items()
                 if counts.get(name, 0) < need]
        raise AuctionError(
            f"{franchise.name} would have no room left for "
            f"{', '.join(sorted(short))} — {owed} still needed with only "
            f"{max(0, slots_left)} squad slots to spare.")


def check_role_ceiling(session, season, franchise, role, *, counts=None):
    """Refuse one more player of ``role`` when the squad is already at its cap.

    Split out of ``check_role_rules`` because the two ends of the rule apply at
    different moments. A retention or an expansion pick happens before the
    auction opens, when a minimum is still trivially reachable — every slot is
    empty — so checking reachability there would only ever fire on a
    misconfiguration the setup page already refuses. A ceiling is different: a
    franchise can retain its way past one, and nothing later can undo it.
    """
    caps = role_maximums(season)
    cap = caps.get(role)
    if cap is None:
        return
    if counts is None:
        counts = role_counts(session, franchise.id)
    have = counts.get(role, 0)
    if have + 1 <= cap:
        return
    if cap == 0:
        # A typed 0 is a real rule — "no specialist keepers in this auction" —
        # and needs its own sentence, because "already has 0, which is the
        # limit of 0" reads as a bug.
        raise AuctionError(f"{franchise.name} may not take a {role} at all — "
                           f"this auction caps the role at 0.")
    raise AuctionError(
        f"{franchise.name} already has {have} "
        f"{role}{'' if have == 1 else 's'}, which is this auction's limit "
        f"of {cap}.")


# The old private name, kept because the reachability half is what it always
# did and callers outside this module may still reach for it.
_check_role_reachability = check_role_rules


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
    if _in_bid_gap(season, lot, franchise, now, by_admin=by_admin):
        raise BidTooSoon(bid_holder_message(session, season, lot))
    amount = validate_bid(session, season, lot, franchise, amount_lakh, now=now,
                          by_tg_id=by_tg_id, by_admin=by_admin)

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
    staged = staged_clock(season)
    sniping = (and_(AuctionLot.deadline_at <= snipe_cutoff,
                    AuctionLot.extensions_used < cap)
               if window and cap and not final_offer and staged is None
               else None)
    # The staged clock's reset: a bid with less than ``reset`` seconds left
    # puts the clock back to ``reset``, however many times it takes. It rides
    # in the same statement as the claim for the same reason anti-snipe does.
    reset_at = (now + timedelta(seconds=staged[1])
                if staged is not None and not final_offer else None)
    resets = bool(reset_at is not None and lot.deadline_at is not None
                  and lot.deadline_at < reset_at)

    # The quiet gap after a bid: nobody may answer it for ``gap`` seconds, so
    # a bid must leave the room at least that long (plus one) to answer it —
    # otherwise a bid two seconds from the end could never be topped.
    gap = 0 if (final_offer or by_admin) else bid_gap_seconds(season)
    answerable = now + timedelta(seconds=gap + 1) if gap else None

    values = {
        "current_bid_lakh": amount,
        "current_bidder_id": franchise.id,
        "bid_count": AuctionLot.bid_count + 1,
        "going_stage": 0,
        "last_bid_at": now,
    }
    if sniping is not None:
        values["deadline_at"] = case(
            (sniping, max(extended_to, answerable or extended_to)),
            *([(AuctionLot.deadline_at < answerable, answerable)]
              if answerable else []),
            else_=AuctionLot.deadline_at)
        values["extensions_used"] = (AuctionLot.extensions_used
                                     + case((sniping, 1), else_=0))
    elif reset_at is not None:
        floor = max(reset_at, answerable or reset_at)
        values["deadline_at"] = case(
            (AuctionLot.deadline_at < floor, floor),
            else_=AuctionLot.deadline_at)
    elif answerable is not None:
        values["deadline_at"] = case(
            (AuctionLot.deadline_at < answerable, answerable),
            else_=AuctionLot.deadline_at)

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
                              AuctionLot.current_bidder_id != franchise.id)]),
                       *([or_(AuctionLot.last_bid_at.is_(None),
                              AuctionLot.last_bid_at
                              <= now - timedelta(seconds=gap))]
                         if gap else []))
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
        if gap and _in_bid_gap(season, lot, franchise, now):
            raise BidTooSoon(bid_holder_message(session, season, lot))
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
              by_admin=by_admin,
              detail=({"amount_lakh": amount, "reset_to": staged[1]}
                      if resets else {"amount_lakh": amount}))
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
        return PriceMoved(
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
    _record_buy_news(session, season, lot, buyer, price)
    season.current_lot_id = None
    complete_if_done(session, season)
    return lot


# A "record" over the auction's first couple of lots is no record at all.
RECORD_NEWS_MIN_PRIOR_SALES = 3


def _record_buy_news(session, season, lot, buyer, price):
    """CMU News when a sale beats every earlier price in this auction."""
    try:
        prior = (session.query(func.count(AuctionLot.id),
                               func.max(AuctionLot.sold_price_lakh))
                 .filter(AuctionLot.season_id == season.id,
                         AuctionLot.status == LOT_SOLD,
                         AuctionLot.id != lot.id).one())
        count, best = int(prior[0] or 0), int(prior[1] or 0)
        if count < RECORD_NEWS_MIN_PRIOR_SALES or price <= best:
            return
        from services.news_service import auto_story
        money = render_money(price, season.currency_label)
        auto_story(
            session, "auction_record", f"auction_record:{season.id}:{lot.id}",
            f"💰 Record buy! {buyer.name} sign {lot.name} for {money}",
            f"{buyer.name} have smashed the {season.name} auction record, "
            f"signing {lot.name} for {money}.\n\n"
            f"The previous highest price was "
            f"{render_money(best, season.currency_label)}.",
            kicker="Record buy")
    except Exception:
        logger.exception("record-buy news failed for lot %s", getattr(lot, "id", None))


def _auction_wrap_news(session, season):
    """CMU News when an auction finishes: the top buys and the big spender."""
    try:
        from services.news_service import auto_story
        lots = (session.query(AuctionLot)
                .filter(AuctionLot.season_id == season.id,
                        AuctionLot.status == LOT_SOLD,
                        AuctionLot.sold_price_lakh.isnot(None))
                .order_by(AuctionLot.sold_price_lakh.desc()).all())
        if not lots:
            return
        names = {f.id: f.name for f in session.query(AuctionFranchise)
                 .filter(AuctionFranchise.season_id == season.id)}
        spend = {}
        for sold in lots:
            spend[sold.sold_to_id] = (spend.get(sold.sold_to_id, 0)
                                      + int(sold.sold_price_lakh or 0))
        cur = season.currency_label
        top = "\n".join(
            f"{i}. {sold.name} → {names.get(sold.sold_to_id, 'a franchise')} "
            f"({render_money(sold.sold_price_lakh, cur)})"
            for i, sold in enumerate(lots[:3], 1))
        big_id = max(spend, key=spend.get)
        body = (f"The {season.name} auction is done — {len(lots)} players signed.\n\n"
                f"Top buys:\n{top}\n\n"
                f"Biggest spender: {names.get(big_id, 'a franchise')} "
                f"({render_money(spend[big_id], cur)}).")
        auto_story(session, "auction_complete", f"auction_done:{season.id}",
                   f"🔨 {season.name} auction wrap: {lots[0].name} goes for "
                   f"{render_money(lots[0].sold_price_lakh, cur)}",
                   body, kicker="Auction wrap")
    except Exception:
        logger.exception("auction wrap news failed for season %s", season.id)


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


def reinstate_lot(session, season, lot, *, by_tg_id=None, quiet=False):
    """``/awithdraw``'s opposite: put a withdrawn player back in the queue.

    An unsold player is accepted too — it is the same "back into the pool"
    either way, and an admin should not have to remember which of the two
    commands a player needs. He goes to the **tail** of the queue, with every
    trace of his last time on the block cleared; ``force_next`` is how he goes
    to the front instead.

    A completed auction comes back **paused**, for the reason ``relist_all``
    gives: a clock starting in an empty room sells to whoever is still looking.
    """
    if lot is None:
        raise AuctionError("No such lot.")
    if season.status == STATUS_CANCELLED:
        raise AuctionError("This auction was cancelled.")
    if lot.status == LOT_QUEUED:
        raise AuctionError(f"{lot.name} is already waiting in the queue.")
    if lot.status in LOT_LIVE:
        raise AuctionError(f"{lot.name} is on the block right now.")
    if lot.status == LOT_SOLD:
        raise AuctionError(f"{lot.name} has been sold — undo the sale first.")

    was = lot.status
    lot.status = LOT_QUEUED
    lot.lot_no = _next_lot_no(session, season.id)
    lot.current_bid_lakh = None
    lot.current_bidder_id = None
    lot.deadline_at = None
    lot.going_stage = 0
    lot.extensions_used = 0
    lot.rtm_stage = None
    lot.rtm_base_bid_lakh = None
    reopened = season.status == STATUS_COMPLETED
    if reopened:
        season.status = STATUS_PAUSED
        season.current_lot_id = None
    session.flush()
    if not quiet:
        note = (" The auction is open again, paused — /astart when the room "
                "is ready." if reopened else "")
        verb = "withdrawn" if was == LOT_WITHDRAWN else "unsold"
        log_event(session, season, "lot_reinstated",
                  f"↩️ {_e(lot.name)} ({verb}) is back in the auction pool."
                  f"{note}",
                  lot=lot, by_tg_id=by_tg_id, by_admin=True,
                  detail={"was": was})
    return lot


def force_next(session, season, lot, *, now=None, by_tg_id=None):
    """Make this player the very next lot — and open him now if nothing is up.

    Returns ``(lot, opened)``. ``opened`` is True when the auction was live
    with an empty block, so he went straight under the hammer; otherwise he
    waits at the front of the queue and opens the moment the current lot
    resolves (or when the auction starts).

    A withdrawn or unsold player is reinstated on the way, so "force Tilak
    Verma" works whatever happened to him earlier. A sold one is refused: he
    is on a squad, and forcing him would sell him twice.
    """
    if lot is None:
        raise AuctionError("No such lot.")
    if lot.status in LOT_LIVE:
        raise AuctionError(f"{lot.name} is already on the block.")
    if lot.status == LOT_SOLD:
        raise AuctionError(f"{lot.name} has been sold — undo the sale first.")
    if lot.status != LOT_QUEUED:
        reinstate_lot(session, season, lot, by_tg_id=by_tg_id, quiet=True)
    _require_reorderable(season)
    _requeue(session, season, [lot])

    opened = (season.status == STATUS_LIVE
              and current_lot(session, season) is None)
    if opened:
        log_event(session, season, "lot_forced",
                  f"⏩ The auctioneer calls <b>{_e(lot.name)}</b> now.",
                  lot=lot, by_tg_id=by_tg_id, by_admin=True,
                  detail={"opened": True})
        lot = open_lot(session, season, lot, now=now)
    else:
        standing = current_lot(session, season)
        after = (f" — straight after {_e(standing.name)}"
                 if standing is not None else "")
        log_event(session, season, "lot_forced",
                  f"⏩ Next up: <b>{_e(lot.name)}</b>{after}.",
                  lot=lot, by_tg_id=by_tg_id, by_admin=True,
                  detail={"opened": False})
    return lot, opened


def mark_unsold(session, season, picked, *, by_tg_id=None):
    """``/aunsold 67, 88, 89`` — send a batch of players straight to unsold.

    Returns ``(done, skipped)``: the lots that went unsold, and
    ``(lot, reason)`` pairs for the ones that did not, so the admin is told
    about each player by name rather than the whole batch failing on one.

    Only a player **nobody has bought and nobody is bidding on** can go:

    * queued (yet to come) — marked unsold without ever opening;
    * on the block with no bid — passed, exactly as bare ``/aunsold`` does;
    * on the block with a standing bid, mid Right To Match, sold, already
      unsold or withdrawn — left alone, with the reason.

    The lot on the block is passed **last**, after the queued ones, so the
    single ``complete_if_done`` it triggers sees the whole batch at once.
    """
    if season.status in (STATUS_COMPLETED, STATUS_CANCELLED):
        raise AuctionError(f"This auction is {season.status}.")
    done, skipped, live = [], [], None
    seen = set()
    for lot in picked:
        if lot is None or lot.id in seen:
            continue
        seen.add(lot.id)
        if lot.status == LOT_QUEUED:
            # Conditional, like open_lot's own claim: if the sweeper opened
            # this lot after it was read, the write matches nothing and the
            # lot stays with the room instead of being marked unsold under it.
            claimed = (session.query(AuctionLot)
                       .filter(AuctionLot.id == lot.id,
                               AuctionLot.status == LOT_QUEUED)
                       .update({"status": LOT_UNSOLD, "deadline_at": None,
                                "going_stage": 0},
                               synchronize_session=False))
            if claimed:
                session.refresh(lot)
                done.append(lot)
            else:
                skipped.append((lot, "has just gone on the block"))
        elif lot.status == LOT_ON_BLOCK:
            if lot.current_bidder_id is not None:
                skipped.append((lot, "has a standing bid — /aundobid first"))
            else:
                live = lot
        elif lot.status == LOT_RTM_OFFERED:
            skipped.append((lot, "is in a Right To Match"))
        elif lot.status == LOT_SOLD:
            buyer = getattr(lot, "sold_to", None)
            skipped.append((lot, "is already in " + (buyer.name if buyer
                                                     else "a squad")))
        elif lot.status == LOT_UNSOLD:
            skipped.append((lot, "is already unsold"))
        else:
            skipped.append((lot, "was withdrawn — /areinstate first"))

    session.flush()
    if done:
        names = ", ".join(_e(lot.name) for lot in done)
        log_event(session, season, "lots_unsold",
                  f"❌ <b>UNSOLD</b> by the auctioneer, before going under "
                  f"the hammer: {names}.",
                  by_tg_id=by_tg_id, by_admin=True,
                  detail={"lot_ids": [lot.id for lot in done]})
    if live is not None:
        pass_lot(session, season, live, by_tg_id=by_tg_id, by_admin=True)
        done.append(live)
    elif done and season.status == STATUS_LIVE:
        # The queue may just have run dry under a live auction with an empty
        # block; this is what finishes it (or starts the accelerated round).
        # Never before then: in setup or paused it would finish an auction
        # that has not run — start/resume reach complete_if_done themselves.
        complete_if_done(session, season)
    return done, skipped


def find_lots(session, season, text):
    """Lots named in ``67, 88, 89`` or ``Tilak Verma, 53``. Returns ``(lots, misses)``.

    A bare number is the **lot number** — the ``#`` the board, the console and
    ``/anextset`` print. Anything else is a name, matched the way ``/awithdraw``
    matches one: exact first, then a unique substring. A token that matches
    nothing, or more than one player, is returned in ``misses`` with the reason
    rather than raising, so one typo does not throw away the rest of a list.
    """
    tokens = []
    for chunk in (text or "").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        words = chunk.split()
        if all(w.lstrip("#").isdigit() for w in words):
            tokens.extend(words)
        else:
            tokens.append(chunk)

    rows = (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season.id).all())
    by_no = {int(lot.lot_no): lot for lot in rows if lot.lot_no is not None}
    found, misses = [], []
    for token in tokens:
        bare = token.lstrip("#")
        if bare.isdigit():
            lot = by_no.get(int(bare))
            if lot is None:
                misses.append((token, "no lot has that number"))
            else:
                found.append(lot)
            continue
        wanted = token.lower()
        exact = [lot for lot in rows if (lot.name or "").lower() == wanted]
        hits = exact or [lot for lot in rows
                         if wanted in (lot.name or "").lower()]
        if len(hits) == 1 or exact:
            found.append(hits[0])
        elif hits:
            misses.append((token, "could be " +
                           ", ".join(lot.name for lot in hits[:5])))
        else:
            misses.append((token, "is not in this auction's pool"))
    return found, misses


def find_one_lot(session, season, text):
    """Exactly one lot, by number or name, or an ``AuctionError`` saying why."""
    found, misses = find_lots(session, season, text)
    if misses:
        token, why = misses[0]
        raise AuctionError(f"“{token}” {why}.")
    if len(found) != 1:
        raise AuctionError("Name one player (or one lot number).")
    return found[0]


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
    # The bid the gap was counting from is gone; the room may answer now.
    lot.last_bid_at = None
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
    if lot.acquisition == ACQ_DRAFTED:
        raise AuctionError(f"{lot.name} was an expansion pick, not bought. "
                           f"Undo the pick instead — it gives the pick back "
                           f"as well as the money.")
    if lot.acquisition == ACQ_AUTOFILL:
        raise AuctionError(f"{lot.name} was handed to a short squad at the "
                           f"end, not bought — there is no sale to undo.")
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


def person_tag(session, tg_id, fallback="Owner"):
    """A clickable mention of one person — ``@username`` when we know it."""
    if not tg_id:
        return _e(fallback or "Owner")
    from services.draft_scheduler import mention
    return mention(session, tg_id, fallback)


def owner_tag(session, franchise, *, by_tg_id=None):
    """``<b>Team</b> (👤 @owner)`` — the franchise, with its owner tagged.

    Tagged so the person actually gets the notification in a busy group: the
    SOLD card, the warnings and "outbid" all name somebody who needs to know.
    ``by_tg_id`` — the person who placed the bid — is tagged too when it was a
    co-owner rather than the owner. An unowned franchise is just its name.
    """
    if franchise is None:
        return "<b>?</b>"
    label = f"<b>{_e(franchise.name)}</b>"
    owner = int(franchise.owner_tg_id or 0)
    people = []
    if owner > 0:
        people.append(person_tag(session, owner, franchise.owner_name or franchise.name))
    if by_tg_id and int(by_tg_id) != owner:
        people.append("bid by " + person_tag(session, int(by_tg_id), "co-owner"))
    return f"{label} (👤 {' · '.join(people)})" if people else label


def winning_bidder(session, lot):
    """Who typed the winning bid on a sold lot, or None."""
    if lot is None or not lot.sold_to_id:
        return None
    row = (session.query(AuctionBid)
           .filter(AuctionBid.lot_id == lot.id,
                   AuctionBid.franchise_id == lot.sold_to_id,
                   AuctionBid.is_void.is_(False))
           .order_by(AuctionBid.amount_lakh.desc(), AuctionBid.id.desc())
           .first())
    return int(row.by_tg_id) if row is not None and row.by_tg_id else None


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
    _record_buy_news(session, season, lot, holder, price)
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
    session.flush()
    # The squads belong to the people who bought them. A tournament already
    # built on this league gets its unclaimed teams' owners and co-owners
    # filled in now (an admin's own assignment always wins); one built later
    # inherits them on its own via tournament_service.league_owner_for_team.
    try:
        from models import Tournament
        from services import tournament_service
        for tour in (session.query(Tournament)
                     .filter(Tournament.league_id == league.id).all()):
            tournament_service.sync_owners_from_draft(session, tour.id)
    except Exception:
        logger.exception("auction publish: owner sync failed (non-fatal)")
    log_event(session, season, "published",
              f"📤 Squads published to the “{_e(league.name)}” Challenge League.",
              by_admin=True)
    return league


# ──────────────────────────────────────────────────────────────────────
# Sets — which group of players comes to the block next
#
# ``AuctionLot.set_name`` groups the pool ("Marquee", "85-90 OVR", …) and the
# queue is still simply ascending ``lot_no``. Choosing which set comes next is
# therefore a renumbering: the chosen lots get the next numbers after
# everything that exists, in their existing relative order, and the rest of
# the queue follows them. Nothing else in the feature has to learn what a set
# is — ``next_queued`` keeps reading the lowest number.
# ──────────────────────────────────────────────────────────────────────

# Lots nobody filed under a set.
DEFAULT_SET = "Main pool"
# Where the automatic accelerated round files the players it brings back.
ACCELERATED_SET = "⚡ Accelerated"
# How /asets and /aunsoldlist label players passed on and not yet re-listed.
UNSOLD_SET = "⚡ Unsold / Accelerated"
# A removed franchise's players go back into the pool under this.
RELEASED_SET_PREFIX = "🔁 Released"


def set_label(lot):
    return (lot.set_name or "").strip() or DEFAULT_SET


def parse_rating_range(text):
    """``"85-90"`` / ``"85 to 90"`` / ``"85+"`` / ``"85"`` → ``(low, high)``.

    Returns None when the text is not a rating range at all, so a caller can
    fall back to treating it as a set name. A range typed backwards is a slip,
    not a request for nobody.
    """
    import re
    raw = (text or "").strip().lower().replace("ovr", "").strip()
    if not raw:
        return None
    match = re.fullmatch(r"(\d{1,3})\s*(?:-|–|to)\s*(\d{1,3})", raw)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        return (min(low, high), max(low, high))
    match = re.fullmatch(r"(\d{1,3})\s*\+", raw)
    if match:
        return (int(match.group(1)), 999)
    if re.fullmatch(r"\d{1,3}", raw):
        return (int(raw), int(raw))
    return None


def range_set_name(low, high):
    if high >= 999:
        return f"{low}+ OVR"
    if low == high:
        return f"{low} OVR"
    return f"{low}-{high} OVR"


def add_rating_range_to_pool(session, season, low, high, *, set_name=None,
                             editions=False):
    """Every catalogue card rated ``low``–``high`` into the pool, as one set.

    Best first, which is the order the room expects a set to be read out in.
    ``editions`` includes special editions of a cricketer as well as his base
    card; off by default, because two cards of one man in the same pool is a
    squad with the same player twice. Returns ``(added, skipped, set_name)``.
    """
    filters = {"rating_min": low, "rating_max": high}
    if not editions:
        filters["version_mode"] = "base"
    query = player_query.master_player_query(session, filters)
    players = player_query.ordered(query).all()
    if not players:
        raise AuctionError(f"No active card is rated {range_set_name(low, high)}.")
    name = (set_name or "").strip() or range_set_name(low, high)
    added, skipped = add_players_to_pool(session, season, players,
                                         set_name=name[:40])
    return added, skipped, name[:40]


def list_sets(session, season):
    """Every set in the order the room will meet it, with its counts.

    Each entry is a dict: ``name``, ``set_no`` (its place in this list, which is
    the number everything else calls its "Set No"), ``queued``, ``sold``,
    ``unsold``, ``withdrawn``, ``total`` and ``state`` — ``done`` (nothing left in it),
    ``live`` (the lot on the block is from it), ``next`` (the first set still
    waiting after the live one) or ``queued``. Sets that are finished come
    first, in the order they ran; the live one; then the queue, in queue order.
    """
    rows = lots(session, season.id)
    live = current_lot(session, season)
    live_name = set_label(live) if live is not None else None
    sets = {}
    for lot in rows:
        name = set_label(lot)
        entry = sets.setdefault(name, {
            "name": name, "queued": 0, "sold": 0, "unsold": 0, "withdrawn": 0,
            "total": 0, "first_queued": None, "first_any": lot.lot_no})
        entry["total"] += 1
        entry["first_any"] = min(entry["first_any"], lot.lot_no)
        if lot.status == LOT_QUEUED:
            entry["queued"] += 1
            if entry["first_queued"] is None or lot.lot_no < entry["first_queued"]:
                entry["first_queued"] = lot.lot_no
        elif lot.status == LOT_SOLD:
            entry["sold"] += 1
        elif lot.status == LOT_UNSOLD:
            entry["unsold"] += 1
        elif lot.status == LOT_WITHDRAWN:
            entry["withdrawn"] += 1

    def order(entry):
        if entry["name"] == live_name:
            return (1, 0)
        if entry["queued"]:
            return (2, entry["first_queued"])
        return (0, entry["first_any"])

    ordered_sets = sorted(sets.values(), key=order)
    next_marked = False
    for index, entry in enumerate(ordered_sets):
        # ``set_no`` is this list's 1-based position — the number the room and
        # the website both call "Set 3". It is deliberately NOT stored: the
        # queue's order already lives in ``lot_no``, so a column would be a
        # second copy of the same fact, free to drift from it. Read back this
        # way two sets can never hold one number, and a reorder renumbers for
        # free. Anything that wants the number must ask here rather than count
        # sets itself.
        entry["set_no"] = index + 1
        if entry["name"] == live_name:
            entry["state"] = "live"
        elif entry["queued"] and not next_marked:
            entry["state"] = "next"
            next_marked = True
        elif entry["queued"]:
            entry["state"] = "queued"
        else:
            entry["state"] = "done"
    return ordered_sets


def queued_sets(session, season):
    """The sets still waiting — the ones a reorder can actually move."""
    return [entry for entry in list_sets(session, season) if entry["queued"]]


def next_set(session, season):
    """The first set still waiting behind the live one, or None."""
    for entry in list_sets(session, season):
        if entry["state"] == "next":
            return entry
    return None


def queued_lots(session, season, *, set_name=None, limit=None):
    query = (session.query(AuctionLot)
             .filter(AuctionLot.season_id == season.id,
                     AuctionLot.status == LOT_QUEUED)
             .order_by(AuctionLot.lot_no.asc()))
    rows = query.all()
    if set_name is not None:
        rows = [lot for lot in rows if set_label(lot) == set_name]
    return rows[:limit] if limit else rows


def _match_set(session, season, text):
    """``(label, queued lots)`` for what somebody typed: a set or a range.

    A rating range picks queued players by rating whatever set they are in; a
    name matches a set exactly, then by a unique prefix. It never guesses
    between two.
    """
    queued = queued_lots(session, season)
    if not queued:
        raise AuctionError("Nothing is waiting in the queue.")
    # An exact set name wins over reading the text as a range: "85-90 OVR" is
    # the name /apool gives a set, and it must move THAT set, not every queued
    # player rated 85-90 wherever they sit.
    typed = (text or "").strip().lower()
    for name in dict.fromkeys(set_label(lot) for lot in queued):
        if name.lower() == typed:
            return name, [lot for lot in queued if set_label(lot) == name]
    band = parse_rating_range(text)
    if band is not None:
        low, high = band
        picked = [lot for lot in queued if low <= int(lot.rating or 0) <= high]
        if not picked:
            raise AuctionError(f"Nobody rated {range_set_name(low, high)} is "
                               f"waiting in the queue.")
        return range_set_name(low, high), picked
    wanted = (text or "").strip().lower()
    if wanted in ("unsold", "accelerated", "accel"):
        wanted = ACCELERATED_SET.lower()
    names = []
    for lot in queued:
        name = set_label(lot)
        if name not in names:
            names.append(name)
    exact = [n for n in names if n.lower() == wanted]
    if not exact:
        exact = [n for n in names if n.lower().startswith(wanted)]
    if not exact:
        exact = [n for n in names if wanted and wanted in n.lower()]
    if not exact:
        raise AuctionError(f"No set called “{text}” is waiting. Queued sets: "
                           + ", ".join(names[:8]))
    if len(exact) > 1:
        raise AuctionError("That could be " + ", ".join(exact[:5])
                           + " — type more of the name.")
    name = exact[0]
    return name, [lot for lot in queued if set_label(lot) == name]


def _set_exact(session, season, name):
    """``(label, queued lots)`` for a set named *exactly*, case aside.

    What a page or a button submits is a name it has just been shown, so it
    needs none of ``_match_set``'s prefix and substring fallbacks — and must not
    have them. A set that finished while the page was open would otherwise match
    a *different* set on a substring and get reordered instead of refused, which
    is the one failure an admin has no way to notice.
    """
    queued = queued_lots(session, season)
    wanted = (name or "").strip().lower()
    if not wanted:
        raise AuctionError("Name the set to move.")
    picked = [lot for lot in queued if set_label(lot).lower() == wanted]
    if not picked:
        raise AuctionError(f"No set called “{name}” has anyone waiting — "
                           f"it may have finished. Reload and try again.")
    return set_label(picked[0]), picked


def _require_reorderable(season):
    """Reordering the queue is fine while live; it is not once it is over.

    Deliberately looser than ``add_players_to_pool``'s setup-or-paused guard:
    that one exists because adding lots restamps ``min_base_price_lakh``, which
    ``max_bid_now``'s reachability rule needs held still once bidding starts.
    Reordering touches no price, and reordering a live queue is the whole point
    of the console's Sets card.
    """
    if season.status in (STATUS_COMPLETED, STATUS_CANCELLED):
        raise AuctionError(f"This auction is {season.status}.")


def _requeue(session, season, front):
    """Renumber the queue so ``front`` comes next, everything else after it.

    New numbers are all above every number the season has ever used, so no
    two rows can meet on the ``(season_id, lot_no)`` unique index mid-flush.

    That high-water-mark trick costs a number range per reorder, and the numbers
    are on screen: three reorders while building a twenty-lot pool would leave
    it numbered 61-80 on the setup page, the console and ``/anextset``. So when
    **nothing in the season has left the queue yet** the numbers are compacted
    back to ``1..N`` in a second pass. The test is per-lot rather than
    ``season.status`` on purpose — retention sells lots while the season is
    still in setup, and renumbering around a sold lot is what the first pass
    exists to avoid.
    """
    queued = queued_lots(session, season)
    chosen = {lot.id for lot in front}
    order = list(front) + [lot for lot in queued if lot.id not in chosen]
    base = _next_lot_no(session, season.id)
    for offset, lot in enumerate(order):
        lot.lot_no = base + offset
    session.flush()

    untouched = (session.query(func.count(AuctionLot.id))
                 .filter(AuctionLot.season_id == season.id,
                         AuctionLot.status != LOT_QUEUED).scalar())
    if not untouched:
        # Safe in one pass: everything sits above the old high-water mark, so
        # 1..N is unoccupied and no two rows can collide on the way down.
        for offset, lot in enumerate(order):
            lot.lot_no = offset + 1
        session.flush()
    return order


def bring_forward(session, season, text, *, by_tg_id=None, quiet=False):
    """Make a set — or every queued player in a rating range — come next.

    Returns ``(label, lots moved)``. The lot on the block is untouched: this
    reorders what is waiting, so the chosen set opens as soon as the current
    lot resolves.

    ``quiet`` skips the event, and therefore the announcement — see
    :func:`set_order` for why the setup page needs that and the console does not.
    """
    _require_reorderable(season)
    label, picked = _match_set(session, season, text)
    _requeue(session, season, picked)
    if not quiet:
        log_event(session, season, "set_queued",
                  f"⏭ Next up: the <b>{_e(label)}</b> set — {len(picked)} "
                  f"{'player' if len(picked) == 1 else 'players'}.",
                  by_tg_id=by_tg_id, by_admin=True,
                  detail={"set": label, "count": len(picked)})
    return label, picked


def set_order(session, season, names, *, by_tg_id=None, exact=False,
              quiet=False):
    """Order the whole queue by set: ``names`` first, in that order.

    Any queued set not named keeps its place behind them. Returns the labels
    in the order they will run.

    ``exact`` matches each name exactly instead of through ``_match_set``'s
    range / prefix / substring ladder. A caller echoing back names it rendered
    itself — the website's Sets card, its ↑/↓ buttons — wants that; somebody
    typing ``/asetorder`` does not.

    ``quiet`` skips the event. Every event is announced to the auction's group
    (``auction_scheduler.SILENT_KINDS`` is empty and ``start`` does not skip
    what queued up before it), so a page where an admin nudges the order half a
    dozen times while *setting the season up* would post half a dozen messages —
    or, before the auction has started, save them all up and dump them into the
    room the moment it does. The setup page is therefore quiet and the live
    console, where the room is watching and a reorder is news, is not.
    """
    _require_reorderable(season)
    names = [n.strip() for n in names or [] if n and n.strip()]
    if not names:
        raise AuctionError("Name the sets in the order they should run, "
                           "separated by commas.")
    resolve = _set_exact if exact else _match_set
    front, labels, seen = [], [], set()
    for text in names:
        label, picked = resolve(session, season, text)
        fresh = [lot for lot in picked if lot.id not in seen]
        seen.update(lot.id for lot in fresh)
        front.extend(fresh)
        labels.append(label)
    _requeue(session, season, front)
    if not quiet:
        log_event(session, season, "set_order",
                  "🗂 Set order: " + " → ".join(f"<b>{_e(l)}</b>" for l in labels),
                  by_tg_id=by_tg_id, by_admin=True, detail={"sets": labels})
    return labels


def set_positions(session, season, pairs, *, by_tg_id=None, quiet=False):
    """``[(set name, Set No)]`` typed on a page → the queue in that order.

    Set No *is* the running order, so two sets cannot hold one number: a repeat
    is refused naming both, and so is a number belonging to a set that has
    already run — that set's players are gone from the queue and nothing can be
    reordered into their place. Every check runs **before** a single lot moves,
    so a refusal leaves the queue exactly as it was.

    The typed number is a *rank*, not a stored value: the sets are sorted by it
    and the queue renumbered, then ``list_sets`` reads the real Set No back from
    the new order. Typing 2, 40, 41 therefore means the same as typing 1, 2, 3.
    """
    _require_reorderable(season)
    entries = list_sets(session, season)
    ran = {entry["set_no"]: entry["name"] for entry in entries
           if entry["state"] in ("done", "live")}
    queued = {entry["name"].lower(): entry["name"]
              for entry in entries if entry["queued"]}

    wanted = []
    taken = {}
    for raw_name, raw_no in pairs or []:
        name = (raw_name or "").strip()
        if not name:
            continue
        actual = queued.get(name.lower())
        if actual is None:
            raise AuctionError(f"“{name}” has nobody waiting — it may have "
                               f"finished. Reload and try again.")
        number = _as_int(raw_no, 0)
        if number < 1:
            raise AuctionError(f"Give {actual} a Set No of 1 or more.")
        if number in taken:
            raise AuctionError(f"Set No {number} is already {taken[number]}'s — "
                               f"give {actual} a different number.")
        if number in ran:
            raise AuctionError(f"Set No {number} is {ran[number]}, which has "
                               f"already run — give {actual} a number after it.")
        taken[number] = actual
        wanted.append((number, actual))

    if not wanted:
        raise AuctionError("Nothing to reorder — give at least one set a Set No.")
    wanted.sort()
    return set_order(session, season, [name for _, name in wanted],
                     by_tg_id=by_tg_id, exact=True, quiet=quiet)


def delete_set(session, season, name, *, by_tg_id=None, quiet=False):
    """Take a whole set out of the pool — every player in it still waiting.

    Named *exactly*, through ``_set_exact``, for the reason that helper exists:
    what a page submits is a name it has just been shown, and a set that
    finished while the page sat open must be refused rather than matched onto a
    different one on a substring. Deleting the wrong forty players is not a
    mistake anybody notices in time.

    **Only queued lots go.** A set whose players have been sold, passed on or
    withdrawn is a record of what happened, and this is a pool edit, not an
    undo — ``undo_sale`` is what puts a sold player back. So a half-run set
    loses what is still waiting and keeps what already happened, and the set
    stays in ``list_sets`` as a finished one. Returns ``(label, removed)``.
    """
    _require_reorderable(season)
    if season.status not in (STATUS_SETUP, STATUS_PAUSED):
        raise AuctionError("Pause the auction before changing its pool.")
    label, picked = _set_exact(session, season, name)
    live = current_lot(session, season)
    for lot in picked:
        # ``_set_exact`` only returns queued lots, so this can only fire on a
        # lot that opened between the query and here. Cheap, and the
        # alternative is deleting the row the console is counting down.
        if live is not None and lot.id == live.id:
            raise AuctionError(f"{lot.name} is on the block right now — the "
                               f"set cannot be deleted under a live lot.")
        session.delete(lot)
    session.flush()
    _restamp_price_floor(session, season)
    if not quiet:
        log_event(session, season, "set_delete",
                  f"🗑 <b>{_e(label)}</b> was taken out of the pool — "
                  f"{len(picked)} player{'' if len(picked) == 1 else 's'} "
                  f"who had not gone on the block yet.",
                  by_tg_id=by_tg_id, by_admin=True)
    return label, len(picked)


def clear_pool(session, season, *, by_tg_id=None, quiet=False):
    """Empty the pool of everything still waiting, whatever set it is in.

    The same rule as ``delete_set`` and for the same reason: sold, unsold and
    withdrawn lots are the auction's record and stay. A season in ``setup``
    with nothing sold therefore comes out genuinely empty, which is what
    "start the pool again" means; one mid-auction comes out with its history
    intact and its queue gone. Returns how many were removed.
    """
    _require_reorderable(season)
    if season.status not in (STATUS_SETUP, STATUS_PAUSED):
        raise AuctionError("Pause the auction before changing its pool.")
    queued = queued_lots(session, season)
    if not queued:
        raise AuctionError("Nothing is waiting in the pool.")
    for lot in queued:
        session.delete(lot)
    session.flush()
    _restamp_price_floor(session, season)
    if not quiet:
        log_event(session, season, "pool_clear",
                  f"🗑 The pool was emptied — {len(queued)} player"
                  f"{'' if len(queued) == 1 else 's'} who had not gone on the "
                  f"block yet. Everything already sold or passed on stays on "
                  f"the record.",
                  by_tg_id=by_tg_id, by_admin=True)
    return len(queued)


def move_set(session, season, name, delta, *, by_tg_id=None, quiet=False):
    """Move a queued set one place earlier (``-1``) or later (``+1``).

    Returns the labels in their new order, or None when the set is already at
    that end of the queue — a no-op rather than an error, because a ↑ on the
    first set is a misclick, not something to shout about.
    """
    _require_reorderable(season)
    order = [entry["name"] for entry in queued_sets(session, season)]
    wanted = (name or "").strip().lower()
    index = next((i for i, n in enumerate(order) if n.lower() == wanted), None)
    if index is None:
        raise AuctionError(f"No set called “{name}” has anyone waiting — "
                           f"it may have finished. Reload and try again.")
    target = index + (1 if _as_int(delta, 0) > 0 else -1)
    if not 0 <= target < len(order):
        return None
    order[index], order[target] = order[target], order[index]
    return set_order(session, season, order, by_tg_id=by_tg_id, exact=True,
                     quiet=quiet)


def sold_lots(session, season_id):
    """Every player on a squad, in the order they were signed."""
    return (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season_id,
                    AuctionLot.status == LOT_SOLD)
            .order_by(AuctionLot.sold_at.asc(), AuctionLot.lot_no.asc()).all())


def acquisition_mark(lot):
    """The mark after a squad line, with its leading space — or nothing."""
    mark = acquisition_icon(lot)
    return f" {mark}" if mark else ""


def acquisition_icon(lot):
    return {ACQ_RETAINED: "🔒", ACQ_RTM: "🪪", ACQ_DRAFTED: "🆕",
            ACQ_AUTOFILL: "🎁"}.get(lot.acquisition or ACQ_AUCTION, "")


# ──────────────────────────────────────────────────────────────────────
# Removing a franchise
# ──────────────────────────────────────────────────────────────────────

def removal_preview(session, season, franchise):
    """What removing this franchise would do, without doing it."""
    others = [f for f in franchises(session, season.id) if f.id != franchise.id]
    pot = int(franchise.purse_total_lakh or 0)
    share = pot // len(others) if others else 0
    return {"players": squad(session, franchise.id), "others": others,
            "pot": pot, "share": share}


def remove_franchise(session, season, franchise, *, by_tg_id=None):
    """Take a franchise out of the auction, mid-way or before it starts.

    * Every player it holds — bought, retained, matched or picked — goes back
      into the pool at the end of the queue, in a set of its own, at his base
      price. None of them carries a Right To Match: the side that held him no
      longer exists.
    * Its whole **opening purse** is shared equally among the franchises that
      remain (any odd lakh go one each to the first few, by sort order), as a
      ``correction`` row on each ledger — so every purse still equals its
      ledger, and the reason sits beside the money.
    * Its bids, ledger, open retention offers and the franchise itself are
      deleted, explicitly rather than by relying on ``ON DELETE CASCADE``,
      which SQLite only honours with a pragma this project does not set.

    If the auction had already finished, releasing players re-opens it
    **paused**, the way ``relist_all`` does, so they can still be sold.

    Refused while it holds the standing bid on the block (undo the bid first,
    so the room sees who drops out), while a Right To Match is being asked of
    it, once squads are published, and for the last franchise standing.
    Returns ``(released lots, {franchise: share})``.
    """
    if season.status == STATUS_CANCELLED:
        raise AuctionError("This auction was cancelled.")
    if season.published_at is not None:
        raise AuctionError("This auction has been published — its squads are "
                           "already a league. Remove the team there instead.")
    field = franchises(session, season.id)
    others = [f for f in field if f.id != franchise.id]
    if not others:
        raise AuctionError(f"{franchise.name} is the only franchise left — "
                           f"cancel the auction instead.")
    live = current_lot(session, season)
    if live is not None and live.current_bidder_id == franchise.id:
        raise AuctionError(
            f"{franchise.name} holds the standing bid on {live.name}. Undo it "
            f"first with /aundobid, then remove the franchise.")
    if (live is not None and live.status == LOT_RTM_OFFERED
            and live.previous_franchise_id == franchise.id):
        raise AuctionError(f"{franchise.name} is being asked a Right To Match "
                           f"on {live.name}. Settle it first (/artmforce).")

    symbol = season.currency_label or "₹"
    name = franchise.name
    held = squad(session, franchise.id)
    label = f"{RELEASED_SET_PREFIX} – {name}"[:40]
    base = _next_lot_no(session, season.id)
    for offset, lot in enumerate(held):
        lot.status = LOT_QUEUED
        lot.lot_no = base + offset
        lot.set_name = label
        lot.acquisition = ACQ_AUCTION
        lot.sold_to_id = None
        lot.sold_price_lakh = None
        lot.sold_at = None
        lot.current_bid_lakh = None
        lot.current_bidder_id = None
        lot.deadline_at = None
        lot.going_stage = 0
        lot.extensions_used = 0
        lot.rtm_stage = None
        lot.rtm_base_bid_lakh = None
        lot.rtm_matched_by_id = None
        lot.bid_count = 0
    # A released player starts his new round clean. The bids from the round
    # that sold him are voided — never deleted, they are why his old price
    # was what it was — or an undo in the new round would fall back to one of
    # them and hand him to a side that has not bid for him this time.
    if held:
        (session.query(AuctionBid)
         .filter(AuctionBid.lot_id.in_([lot.id for lot in held]),
                 AuctionBid.is_void.is_(False))
         .update({"is_void": True, "voided_by_tg_id": by_tg_id},
                 synchronize_session=False))
        # A finished auction with players back in the queue is not finished:
        # it re-opens PAUSED, as ``relist_all`` does, so /astart can sell them
        # — left completed, the sweeper ignores it, start() refuses it, and a
        # publish would silently leave them out of the league.
        if season.status == STATUS_COMPLETED:
            season.status = STATUS_PAUSED
            season.current_lot_id = None
    # Nobody may exercise a Right To Match on behalf of a side that is gone.
    (session.query(AuctionLot)
     .filter(AuctionLot.season_id == season.id,
             AuctionLot.previous_franchise_id == franchise.id)
     .update({"previous_franchise_id": None}, synchronize_session=False))
    (session.query(AuctionLot)
     .filter(AuctionLot.season_id == season.id,
             AuctionLot.current_bidder_id == franchise.id)
     .update({"current_bidder_id": None}, synchronize_session=False))
    session.flush()

    # The purse, shared out. One conditional-free UPDATE per franchise: these
    # only ever ADD money, so there is no floor for a race to break.
    pot = int(franchise.purse_total_lakh or 0)
    share, odd = divmod(pot, len(others))
    shares = {}
    for index, other in enumerate(others):
        amount = share + (1 if index < odd else 0)
        if amount <= 0:
            continue
        (session.query(AuctionFranchise)
         .filter(AuctionFranchise.id == other.id)
         .update({"purse_total_lakh": AuctionFranchise.purse_total_lakh + amount,
                  "purse_remaining_lakh":
                  AuctionFranchise.purse_remaining_lakh + amount},
                 synchronize_session=False))
        shares[other.id] = amount
    session.flush()
    session.expire_all()
    for other in others:
        amount = shares.get(other.id)
        if not amount:
            continue
        fresh = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == other.id).first())
        _ledger(session, fresh, LEDGER_CORRECTION, amount,
                note=f"Redistributed from {name}", by_tg_id=by_tg_id)

    # Explicit, because SQLite ignores ON DELETE without a pragma.
    session.query(AuctionBid).filter(
        AuctionBid.franchise_id == franchise.id).delete(synchronize_session=False)
    session.query(AuctionLedgerEntry).filter(
        AuctionLedgerEntry.franchise_id == franchise.id).delete(
            synchronize_session=False)
    session.query(AuctionRetentionOffer).filter(
        AuctionRetentionOffer.franchise_id == franchise.id).delete(
            synchronize_session=False)
    session.query(AuctionEvent).filter(
        AuctionEvent.franchise_id == franchise.id).update(
            {"franchise_id": None}, synchronize_session=False)
    session.query(AuctionFranchise).filter(
        AuctionFranchise.id == franchise.id).delete(synchronize_session=False)
    session.flush()
    session.expire_all()

    each = render_money(share, symbol)
    log_event(session, season, "franchise_removed",
              f"🚪 <b>{_e(name)}</b> have left the auction. "
              f"{len(held)} {'player goes' if len(held) == 1 else 'players go'} "
              f"back into the pool, and their "
              f"{render_money(pot, symbol)} purse is shared out — "
              f"{each} to each of the {len(others)} franchises left.",
              by_tg_id=by_tg_id, by_admin=True,
              detail={"name": name, "released": len(held), "pot_lakh": pot,
                      "shares": {str(k): v for k, v in shares.items()}})
    released = (session.query(AuctionLot)
                .filter(AuctionLot.id.in_([lot.id for lot in held]))
                .order_by(AuctionLot.lot_no.asc()).all()) if held else []
    return released, shares


# ──────────────────────────────────────────────────────────────────────
# Topping up short squads at the end
# ──────────────────────────────────────────────────────────────────────

def autofill_short_squads(session, season, *, now=None):
    """Hand unsold players, free, to franchises still under the minimum squad.

    Runs once the queue is empty for good, and only after an accelerated
    round has run (automatic or ``/aaccel go``) — so it only ever deals in
    players the whole room has already passed on twice.
    Round-robin, the smallest squad choosing first, each taking the
    best-rated player left that fits: the squad cap, the overseas cap, never
    a second card of a cricketer the squad already has (a published league
    keys its players by name, so the two would collapse into one row), and —
    when role minimums are set — a role the squad still owes before anything
    else. Stops when nobody is short or nothing left fits.

    Free, because the rule is "given to that team": a zero-amount ledger row
    records the signing, and the purse is not touched. Returns
    ``[(franchise name, lot)]`` in the order they were handed out.
    """
    now = now or datetime.utcnow()
    minimum = _as_int(season.min_squad_size, 0)
    maximum = _as_int(season.max_squad_size, 0)
    if minimum <= 0:
        return []
    pool = (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season.id,
                    AuctionLot.status == LOT_UNSOLD)
            .order_by(AuctionLot.rating.desc(), AuctionLot.lot_no.asc()).all())
    if not pool:
        return []
    field = franchises(session, season.id)
    overseas_cap = max(0, _as_int(season.max_overseas, 0))
    overseas = {f.id: overseas_count(session, f.id) for f in field}
    roles = {f.id: role_counts(session, f.id) for f in field}
    names = {f.id: {(lot.name or "").strip().lower()
                    for lot in squad(session, f.id)} for f in field}
    needs = role_minimums(season)
    caps = role_maximums(season)
    given = []

    while pool:
        short = sorted((f for f in field
                        if int(f.squad_size or 0) < minimum
                        and int(f.squad_size or 0) < maximum),
                       key=lambda f: (int(f.squad_size or 0), f.sort_order or 0,
                                      f.name or ""))
        if not short:
            break
        progressed = False
        for franchise in short:
            owed = {role for role, need in needs.items()
                    if roles[franchise.id].get(role, 0) < need}

            def fits(lot):
                if (lot.name or "").strip().lower() in names[franchise.id]:
                    return False
                if lot.is_overseas and overseas[franchise.id] + 1 > overseas_cap:
                    return False
                # A free player is still a player: handing a squad its ninth
                # bowler under an eight-bowler cap would build a squad the
                # auction itself would have refused to sell.
                cap = caps.get(lot.category)
                return not (cap is not None
                            and roles[franchise.id].get(lot.category, 0) + 1 > cap)

            candidates = [lot for lot in pool if fits(lot)]
            if owed:
                candidates.sort(key=lambda lot: 0 if lot.category in owed else 1)
            if not candidates:
                continue
            lot = candidates[0]
            pool.remove(lot)
            lot.status = LOT_SOLD
            lot.acquisition = ACQ_AUTOFILL
            lot.sold_to_id = franchise.id
            lot.sold_price_lakh = 0
            lot.sold_at = now
            lot.current_bid_lakh = None
            lot.current_bidder_id = None
            franchise.squad_size = int(franchise.squad_size or 0) + 1
            names[franchise.id].add((lot.name or "").strip().lower())
            if lot.is_overseas:
                overseas[franchise.id] += 1
            roles[franchise.id][lot.category] = roles[franchise.id].get(lot.category, 0) + 1
            session.flush()
            _ledger(session, franchise, LEDGER_AUTOFILL, 0, lot=lot,
                    note=f"Auto-filled: {lot.name}")
            given.append((franchise.name, lot))
            progressed = True
        if not progressed:
            break

    if given:
        by_team = {}
        for team, lot in given:
            by_team.setdefault(team, []).append(lot.name)
        summary = "; ".join(f"<b>{_e(team)}</b> ← {len(names)}"
                            for team, names in by_team.items())
        log_event(session, season, "autofill",
                  f"🎁 Short squads topped up from the unsold players, free: "
                  f"{summary}."[:300],
                  detail={team: names for team, names in by_team.items()})
    session.flush()
    return given


# ──────────────────────────────────────────────────────────────────────
# Retention offers — the admin proposes, the franchise accepts
# ──────────────────────────────────────────────────────────────────────

OFFER_PENDING = "pending"
OFFER_ACCEPTED = "accepted"
OFFER_DECLINED = "declined"
OFFER_CANCELLED = "cancelled"


def offer_retention(session, season, franchise, player, price_lakh=None, *,
                    by_tg_id=None, chat_id=None):
    """Propose keeping ``player`` for ``franchise``. Nothing is signed yet.

    The cheap refusals — the window, the count, a player already kept or
    already offered — are checked now so an admin is not left waiting on an
    offer that could never be accepted. Everything that depends on money is
    checked again, for real, by ``retain()`` when the franchise presses
    Accept, because the purse may well have moved in between.
    """
    if season.status != STATUS_SETUP:
        raise AuctionError("Retention happens before the auction opens — this "
                           "one is " + str(season.status) + ".")
    if retention_locked(season):
        raise AuctionError("Retention is closed for this auction.")
    left = retention_seconds_left(season)
    if left is not None and left <= 0:
        raise AuctionError("The retention deadline has passed.")
    if not retention_configured(season):
        raise AuctionError("This auction allows no retentions — set a maximum "
                           "first.")
    from services import retention_negotiation as RN
    if RN.is_dynamic(season):
        raise AuctionError("This auction uses dynamic retention — the "
                           "franchise's owner negotiates with /retain "
                           "<player>. /aretainforce still signs at once.")
    if int(franchise.retained_count or 0) >= _as_int(season.max_retentions, 0):
        raise AuctionError(f"{franchise.name} has already retained "
                           f"{int(franchise.retained_count or 0)}, which is "
                           f"the maximum.")
    lot = (session.query(AuctionLot)
           .filter(AuctionLot.season_id == season.id,
                   AuctionLot.player_id == player.id).first())
    if lot is not None and lot.status != LOT_QUEUED:
        raise AuctionError(f"{lot.name} is already {lot.status} in this "
                           f"auction.")
    clash = (session.query(AuctionRetentionOffer)
             .filter(AuctionRetentionOffer.season_id == season.id,
                     AuctionRetentionOffer.player_id == player.id,
                     AuctionRetentionOffer.status == OFFER_PENDING).first())
    if clash is not None:
        raise AuctionError(f"{player.name} already has a retention offer "
                           f"waiting. Withdraw it with /aretcancel first.")
    price = None if price_lakh is None else _as_int(price_lakh, -1)
    if price is not None and price < 0:
        raise AuctionError("A retention price cannot be negative.")
    offer = AuctionRetentionOffer(
        season_id=season.id, franchise_id=franchise.id, player_id=player.id,
        player_name=(player.name or "")[:150], price_lakh=price,
        status=OFFER_PENDING, offered_by_tg_id=by_tg_id, chat_id=chat_id)
    # The check above reads; the partial unique index on pending offers is
    # what actually holds when two admins offer the same player on one tick.
    # A savepoint keeps the loser's session usable for the refusal.
    from sqlalchemy.exc import IntegrityError
    try:
        with session.begin_nested():
            session.add(offer)
            session.flush()
    except IntegrityError:
        raise AuctionError(f"{player.name} already has a retention offer "
                           f"waiting. Withdraw it with /aretcancel first.")
    return offer


def offer_price(season, franchise, offer):
    """What accepting this offer would cost right now."""
    if offer.price_lakh is not None:
        return int(offer.price_lakh)
    return retention_price_for(season, int(franchise.retained_count or 0) + 1)


def retention_offer(session, offer_id):
    return (session.query(AuctionRetentionOffer)
            .filter(AuctionRetentionOffer.id == int(offer_id)).first())


def pending_retention_offers(session, season_id):
    return (session.query(AuctionRetentionOffer)
            .filter(AuctionRetentionOffer.season_id == season_id,
                    AuctionRetentionOffer.status == OFFER_PENDING)
            .order_by(AuctionRetentionOffer.id.asc()).all())


def answer_retention_offer(session, season, offer, tg_id, accept, *, now=None):
    """The franchise's answer. Returns the retained lot, or None on decline.

    Only the franchise the offer names may answer — its owner or a co-owner,
    and deliberately *not* a bot admin, for the reason ``may_bid_for`` gives:
    this spends the franchise's money, and the whole point of the button is
    that they chose to.
    """
    if offer is None or offer.season_id != season.id:
        raise AuctionError("That offer is not from this auction.")
    if offer.status != OFFER_PENDING:
        raise AuctionError(f"That offer has already been {offer.status}.")
    franchise = (session.query(AuctionFranchise)
                 .filter(AuctionFranchise.id == offer.franchise_id).first())
    if franchise is None:
        raise AuctionError("That franchise is no longer in the auction.")
    if not may_bid_for(franchise, tg_id):
        raise AuctionError(f"Only {franchise.name}'s owner or a co-owner can "
                           f"answer this.")
    lot = None
    if accept:
        from models import Player
        player = session.query(Player).filter(Player.id == offer.player_id).first()
        if player is None:
            raise AuctionError(f"{offer.player_name} is no longer in the "
                               f"catalogue.")
        lot = retain(session, season, franchise, player, offer.price_lakh,
                     now=now, by_tg_id=tg_id)
        # What was actually paid, so the card never re-prices an accepted
        # offer off the NEXT slab of the ladder.
        offer.price_lakh = int(lot.sold_price_lakh or 0)
    offer.status = OFFER_ACCEPTED if accept else OFFER_DECLINED
    offer.answered_by_tg_id = int(tg_id) if tg_id else None
    offer.answered_at = now or datetime.utcnow()
    session.flush()
    return lot


def cancel_retention_offer(session, season, offer):
    if offer is None or offer.season_id != season.id:
        raise AuctionError("That offer is not from this auction.")
    if offer.status != OFFER_PENDING:
        raise AuctionError(f"That offer has already been {offer.status}.")
    offer.status = OFFER_CANCELLED
    offer.answered_at = datetime.utcnow()
    session.flush()
    return offer


# ──────────────────────────────────────────────────────────────────────
# Auction admins
# ──────────────────────────────────────────────────────────────────────

def is_auction_admin(session, tg_id):
    """A bot admin, or somebody a bot admin made an auction admin.

    The second kind may run every auction admin command and nothing else in
    the bot: every other admin gate reads ``services.admin_ids.is_admin``,
    which never sees the ``auction_admins`` table.
    """
    from services.admin_ids import is_admin
    if tg_id is None:
        return False
    if is_admin(int(tg_id)):
        return True
    try:
        return (session.query(AuctionAdmin.id)
                .filter(AuctionAdmin.tg_id == int(tg_id)).first()) is not None
    except Exception:
        logger.debug("auction admin lookup failed", exc_info=True)
        return False


def auction_admins(session):
    return session.query(AuctionAdmin).order_by(AuctionAdmin.id.asc()).all()


def add_auction_admin(session, tg_id, *, name=None, by_tg_id=None):
    tg_id = _as_int(tg_id, 0)
    if tg_id <= 0:
        raise AuctionError("An auction admin is a positive Telegram user id.")
    existing = (session.query(AuctionAdmin)
                .filter(AuctionAdmin.tg_id == tg_id).first())
    if existing is not None:
        raise AuctionError(f"{existing.name or tg_id} is already an auction "
                           f"admin.")
    row = AuctionAdmin(tg_id=tg_id, name=(name or None) and name[:120],
                       added_by_tg_id=by_tg_id)
    session.add(row)
    session.flush()
    return row


def remove_auction_admin(session, tg_id):
    tg_id = _as_int(tg_id, 0)
    row = (session.query(AuctionAdmin)
           .filter(AuctionAdmin.tg_id == tg_id).first())
    if row is None:
        raise AuctionError(f"{tg_id} is not an auction admin.")
    session.delete(row)
    session.flush()
    return row


def owner_ids(franchise):
    """The owner and every co-owner — everyone a /acall should reach."""
    ids = []
    if franchise.owner_tg_id:
        ids.append(int(franchise.owner_tg_id))
    for tg_id in co_owner_ids(franchise):
        if tg_id not in ids:
            ids.append(tg_id)
    return ids


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
            staged = staged_clock(season)
            words = ({1: " · <b>⚠️ 1st warning</b>",
                      2: " · <b>⚠️⚠️ 2nd warning</b>"} if staged else
                     {1: " · <b>going once</b>", 2: " · <b>GOING TWICE</b>"})
            stage = "" if lot.status == LOT_RTM_OFFERED else words.get(
                going_stage_for(left, season), "")
            head.append(f"⏳ {format_clock(left)} left{stage}")
            if staged and lot.status == LOT_ON_BLOCK:
                head.append(f"🔄 A bid under {staged[1]}s puts the clock back "
                            f"to {staged[1]}s")
            if lot.extensions_used and season.max_extensions and not staged:
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
             f"💰 {render_money(franchise.purse_remaining_lakh, symbol)} left "
             f"of {render_money(franchise.purse_total_lakh, symbol)} · "
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
        # Four different ways onto a squad, four marks. A matched player is
        # not a retained one — he went to the block, the room set his price,
        # and a card was spent to keep him — and an expansion pick is neither:
        # nobody kept him and nobody bid. Squashing any of them together loses
        # the story of how the squad was built.
        how = acquisition_mark(lot)
        lines.append(f"{mark} {_e(lot.name)} · {lot.rating} · "
                     f"{_e(lot.category)} — "
                     f"{render_money(lot.sold_price_lakh, symbol)}{how}")
    return "\n".join(lines)
