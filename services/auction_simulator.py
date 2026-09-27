"""``/afinish`` — end an auction early by simulating the rest of it.

An auction with sixty lots left and a room that has run out of evening has
two options today: grind through every lot, or cancel and lose the squads.
This is the third: every lot still to come is put through a **simulated
bidding war**, the squads are completed inside every rule the live auction
enforces, and the result is committed in one transaction. The room then
watches it happen — one lot per message, a few seconds apart — the way it
would have watched the real thing.

**Every signing is one the live auction would have allowed.** A franchise only
bids when ``validate_bid`` would have let it: a free squad slot, the overseas
cap, the role and rating rules (``check_role_rules``), no second card of a
cricketer it already holds, and never above ``max_bid_now`` — the reachability
rule that keeps enough purse back to fill the minimum squad. Money moves the
way ``sell_lot`` moves it: purse and squad size on the franchise, one ledger
row per sale, so ``reconcile_purses`` stays clean.

**How a franchise values a player.** Base price, scaled up steeply with
rating; scaled by need (below the minimum squad, a role still owed and a
rating rule still owed all push the number up; a squad that has its minimum
only buys the odd good-value extra); capped by a per-slot budget so nobody
blows the purse on the first lot; and jittered by a random generator seeded
from the season and lot, so the same auction simulates the same way twice.

**The order of play** is the live auction's own: whatever is on the block
resolves first (a standing bid is honoured at its price), then the queue in
lot order, then one accelerated round of the unsold, then the existing free
top-up (``autofill_short_squads``) for any squad the pool could still not
complete. The season then completes exactly as ``complete_if_done`` would
complete it.

**Nothing here talks to Telegram.** Every step writes an ``AuctionEvent``
whose kind starts with ``sim_``; the scheduler drains those at one message per
``SIM_MESSAGE_GAP`` seconds, which is what makes the playback slow, and what
lets it survive a redeploy halfway through.
"""

import random
from datetime import datetime

from models import AuctionEvent, AuctionLot
from services import auction_service as A
from services.auction_service import AuctionError

SIM_PREFIX = "sim_"
QUICK_BATCH = 5
# Seconds between two playback messages; the scheduler reads it from here so
# the preview's time estimate and the real pace can never disagree. Telegram
# allows a group about twenty messages a minute.
SIM_MESSAGE_GAP = 3.0

ACCEL_LABEL = "⚡ Accelerated round"
BID_LIMIT = 400          # a bidding war that long is a bug, not an auction


# ──────────────────────────────────────────────────────────────────────
# Preview
# ──────────────────────────────────────────────────────────────────────

def _check_can_finish(session, season):
    if season.status in (A.STATUS_COMPLETED, A.STATUS_CANCELLED):
        raise AuctionError(f"This auction is already {season.status}.")
    if not A.franchises(session, season.id):
        raise AuctionError("No franchises yet — add some before finishing.")


def _remaining_lots(session, season):
    return (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season.id,
                    AuctionLot.status.in_((A.LOT_QUEUED,) + A.LOT_LIVE))
            .order_by(AuctionLot.lot_no.asc()).all())


def preview(session, season):
    """What ``/afinish go`` would do, without doing any of it."""
    _check_can_finish(session, season)
    remaining = _remaining_lots(session, season)
    unsold = A.unsold(session, season.id)
    minimum = A._as_int(season.min_squad_size, 0)
    teams = []
    for franchise in A.franchises(session, season.id):
        size = int(franchise.squad_size or 0)
        teams.append({
            "name": franchise.name,
            "size": size,
            "short": max(0, minimum - size),
            "purse": int(franchise.purse_remaining_lakh or 0),
            "max_bid": max(0, A.max_bid_now(season, franchise)),
        })
    messages = len(remaining) + len(teams) + 3
    return {"remaining": len(remaining), "unsold": len(unsold),
            "teams": teams, "est_seconds": int(messages * (SIM_MESSAGE_GAP + 1)),
            "est_seconds_quick": int((len(remaining) // QUICK_BATCH + len(teams) + 4)
                                     * (SIM_MESSAGE_GAP + 1))}


# ──────────────────────────────────────────────────────────────────────
# The playback log
# ──────────────────────────────────────────────────────────────────────

class _Narrator:
    """Turns the simulation into ``sim_*`` events, one per message.

    In quick mode lot lines are buffered and written ``QUICK_BATCH`` at a
    time; a set header or a squad completing flushes the batch first, so the
    order the room reads is still the order things happened in.
    """

    def __init__(self, session, season, *, quick=False, by_tg_id=None):
        self.session = session
        self.season = season
        self.quick = quick
        self.by_tg_id = by_tg_id
        self.batch = []

    def say(self, kind, headline, detail=None, *, lot=None, franchise=None):
        self.flush()
        return A.log_event(self.session, self.season, SIM_PREFIX + kind,
                           headline, lot=lot, franchise=franchise,
                           detail=detail, by_tg_id=self.by_tg_id,
                           by_admin=True)

    def lot(self, entry, lot):
        if not self.quick:
            A.log_event(self.session, self.season, SIM_PREFIX + "lot",
                        _lot_headline(self.season, entry), lot=lot,
                        detail=entry, by_tg_id=self.by_tg_id, by_admin=True)
            return
        self.batch.append(entry)
        if len(self.batch) >= QUICK_BATCH:
            self.flush()

    def flush(self):
        if not self.batch:
            return
        entries, self.batch = self.batch, []
        A.log_event(self.session, self.season, SIM_PREFIX + "batch",
                    f"🔨 {len(entries)} lots: " + "; ".join(
                        _lot_headline(self.season, e) for e in entries),
                    detail={"lots": entries}, by_tg_id=self.by_tg_id,
                    by_admin=True)


def _lot_headline(season, entry):
    """The plain one-liner a lot entry falls back to (and the website shows)."""
    name = A._e(entry["name"])
    if entry["outcome"] == "unsold":
        return f"❌ #{entry['lot_no']} {name} — unsold"
    money = A.render_money(entry["price"], season.currency_label)
    verb = "RTM" if entry["outcome"] == "rtm" else "SOLD"
    return f"🔨 #{entry['lot_no']} {name} — {verb} to {A._e(entry['team'])} {money}"


# ──────────────────────────────────────────────────────────────────────
# The bidding model
# ──────────────────────────────────────────────────────────────────────

class _Team:
    """What the simulation tracks about one franchise between lots."""

    def __init__(self, session, franchise):
        self.f = franchise
        self.names = {(lot.name or "").strip().lower()
                      for lot in A.squad(session, franchise.id)}
        self.overseas = A.overseas_count(session, franchise.id)
        self.done_announced = False


def _star(lot):
    """0 for a 70-rated player, 1 for a 100: how much of a star he is."""
    rating = int(lot.rating or 0)
    return max(0.0, min(1.0, (rating - 70) / 30.0))


def _eligible(session, season, team, lot, price):
    """Could this franchise legally sign this lot at ``price``?"""
    f = team.f
    if int(f.squad_size or 0) + 1 > A._as_int(season.max_squad_size, 0):
        return False
    if (lot.name or "").strip().lower() in team.names:
        return False
    if lot.is_overseas and team.overseas + 1 > max(0, A._as_int(season.max_overseas, 0)):
        return False
    if price > int(f.purse_remaining_lakh or 0) or price > A.max_bid_now(season, f):
        return False
    try:
        A.check_role_rules(session, season, f, lot)
    except AuctionError:
        return False
    return True


def _valuation(session, season, team, lot, rng, *, accelerated=False):
    """The most this franchise would pay for this player, or 0 for "not
    interested". Never above what it can legally bid."""
    f = team.f
    base = max(1, int(lot.base_price_lakh or 1))
    size = int(f.squad_size or 0)
    minimum = A._as_int(season.min_squad_size, 0)
    maximum = A._as_int(season.max_squad_size, 0)
    short = max(0, minimum - size)
    star = _star(lot)

    if short == 0:
        # A complete squad only buys the odd good-value extra — the better the
        # player, the likelier — and never in the accelerated round's leftovers
        # unless it is still a decent one.
        want = 0.1 + 0.85 * star
        # A free slot is worth saving for someone who improves the side: an
        # extra has to be at least as good as the squad's middle player.
        ratings = sorted(A.squad_ratings(session, f.id))
        if ratings and int(lot.rating or 0) < ratings[len(ratings) // 2]:
            return 0
        if accelerated:
            want *= 0.5
        if rng.random() > want:
            return 0

    value = base * (1.0 + 4.0 * star * star)
    if short:
        value *= 1.15
    owed, _ = A.role_shortfall(session, season, f)
    if lot.category in owed:
        value *= 1.3
    rules = A.rating_rules(season)
    if rules and lot.rating is not None:
        ratings = A.squad_ratings(session, f.id)
        for rule in rules:
            if (int(lot.rating) <= rule["max_rating"]
                    and sum(1 for r in ratings if r <= rule["max_rating"])
                    < rule["min_players"]):
                value *= 1.3
                break
    if accelerated:
        value = base * (1.0 + star)

    # A budget per slot still to fill, so the first star does not take the
    # whole purse: a star may take a few slots' worth, a squad filler one.
    purse = int(f.purse_remaining_lakh or 0)
    planned = max(1, short + (1 if size < maximum else 0))
    budget = purse / planned * (1.0 + 3.0 * star)
    value = min(value, budget) * rng.uniform(0.8, 1.25)

    ceiling = min(purse, A.max_bid_now(season, f))
    value = min(int(value), ceiling)
    if short and ceiling >= base:
        # A squad that still needs players never lets a player it can afford
        # go for nothing: it will always pay the base price.
        value = max(value, base)
    return value if value >= base else 0


def _bidding_war(session, season, teams, lot, rng, *, accelerated=False):
    """``(winner, price, bids, trail)`` — or ``(None, None, 0, [])`` if nobody
    wanted him. The price climbs the live auction's own increment ladder."""
    base = max(1, int(lot.base_price_lakh or 1))
    values = {}
    for team in teams:
        if not _eligible(session, season, team, lot, base):
            continue
        value = _valuation(session, season, team, lot, rng,
                           accelerated=accelerated)
        if value >= base:
            values[team.f.id] = (team, value)
    if not values:
        return None, None, 0, []

    leader, price, bids, trail = None, None, 0, []
    while bids < BID_LIMIT:
        step = base if price is None else price + A.increment_for(season, price)
        contenders = [team for team, value in values.values()
                      if team is not leader and value >= step
                      and step <= A.max_bid_now(season, team.f)]
        if not contenders:
            break
        leader = rng.choice(contenders)
        price = step
        bids += 1
        trail.append([leader.f.short_name or leader.f.name, price])
    return leader, price, bids, trail[-4:]


def _rtm_match(session, season, teams_by_id, lot, winner, price, rng):
    """The previous franchise's Right To Match, simulated: it matches when it
    has a card, can legally pay, and rates the player at that price."""
    if not A.rtm_configured(season) or not lot.previous_franchise_id:
        return None, None
    holder = teams_by_id.get(lot.previous_franchise_id)
    if holder is None or holder is winner or A.rtm_cards_left(holder.f) <= 0:
        return None, None
    cost = price + max(0, A._as_int(getattr(season, "rtm_extra_lakh", 0), 0))
    if not _eligible(session, season, holder, lot, cost):
        return None, None
    # Matching is a decision about a player you know: the better he is, the
    # likelier, and never for someone the franchise would not have bid on.
    if rng.random() > 0.35 + 0.6 * _star(lot):
        return None, None
    return holder, cost


# ──────────────────────────────────────────────────────────────────────
# Moving players and money
# ──────────────────────────────────────────────────────────────────────

def _award(session, season, team, lot, price, *, now, bids=0, rtm=False,
           by_tg_id=None):
    """Sign ``lot`` to ``team`` for ``price`` — the writes ``sell_lot`` makes."""
    f = team.f
    price = int(price)
    lot.status = A.LOT_SOLD
    lot.sold_to_id = f.id
    lot.sold_price_lakh = price
    lot.sold_at = now
    lot.deadline_at = None
    lot.going_stage = 0
    lot.rtm_stage = None
    lot.current_bid_lakh = price
    lot.bid_count = max(int(lot.bid_count or 0), int(bids or 0))
    if lot.opened_at is None:
        lot.opened_at = now
    if rtm:
        lot.acquisition = A.ACQ_RTM
        lot.rtm_matched_by_id = f.id
        f.rtm_cards_used = int(f.rtm_cards_used or 0) + 1
    else:
        lot.current_bidder_id = f.id
    f.purse_remaining_lakh = int(f.purse_remaining_lakh or 0) - price
    f.squad_size = int(f.squad_size or 0) + 1
    team.names.add((lot.name or "").strip().lower())
    if lot.is_overseas:
        team.overseas += 1
    session.flush()
    A._ledger(session, f, A.LEDGER_RTM if rtm else A.LEDGER_PURCHASE, -price,
              lot=lot, note=(f"Right To Match: {lot.name}" if rtm
                             else f"Lot {lot.lot_no}: {lot.name}"),
              by_tg_id=by_tg_id)


def _entry(season, lot, outcome, *, team=None, price=None, bids=0, trail=None,
           rtm_from=None, index=0, total=0):
    return {"lot_no": lot.lot_no, "name": lot.name, "rating": lot.rating,
            "role": lot.category, "overseas": bool(lot.is_overseas),
            "base": int(lot.base_price_lakh or 0), "outcome": outcome,
            "team": team.f.name if team is not None else None,
            "price": price, "bids": bids, "trail": trail or [],
            "rtm_from": rtm_from, "index": index, "total": total}


def _settle_the_block(session, season, teams_by_id, narrator, now, by_tg_id):
    """Whatever the room was looking at resolves first.

    A standing bid is the room's own decision and is honoured at its price —
    including one sitting under an unanswered Right To Match, which goes to
    the bidder the way a timed-out window does. A lot nobody has bid on goes
    back to the head of the queue and is simulated like the rest.
    """
    lot = A.current_lot(session, season)
    if lot is None:
        return 0
    bidder = teams_by_id.get(lot.current_bidder_id)
    if bidder is None:
        lot.status = A.LOT_QUEUED
        lot.deadline_at = None
        lot.going_stage = 0
        lot.current_bid_lakh = None
        lot.current_bidder_id = None
        lot.rtm_stage = None
        lot.rtm_base_bid_lakh = None
        session.flush()
        return 0
    price = int(lot.current_bid_lakh or lot.base_price_lakh or 0)
    _award(session, season, bidder, lot, price, now=now, by_tg_id=by_tg_id)
    entry = _entry(season, lot, "sold", team=bidder, price=price,
                   bids=int(lot.bid_count or 0),
                   trail=[[bidder.f.short_name or bidder.f.name, price]])
    entry["standing"] = True
    narrator.lot(entry, lot)
    return 1


def _check_team_done(season, team, narrator):
    minimum = A._as_int(season.min_squad_size, 0)
    if (minimum and not team.done_announced
            and int(team.f.squad_size or 0) >= minimum):
        team.done_announced = True
        narrator.say("team_done",
                     f"✅ {A._e(team.f.name)} have their minimum squad "
                     f"({int(team.f.squad_size or 0)}/{minimum}).",
                     {"team": team.f.name, "size": int(team.f.squad_size or 0),
                      "min": minimum, "max": A._as_int(season.max_squad_size, 0),
                      "purse": int(team.f.purse_remaining_lakh or 0)},
                     franchise=team.f)


def _run_lot(session, season, teams, teams_by_id, lot, rng, narrator, now,
             by_tg_id, *, index, total, accelerated=False):
    winner, price, bids, trail = _bidding_war(session, season, teams, lot, rng,
                                              accelerated=accelerated)
    if winner is None:
        lot.status = A.LOT_UNSOLD
        lot.deadline_at = None
        lot.going_stage = 0
        lot.times_unsold = int(lot.times_unsold or 0) + 1
        session.flush()
        narrator.lot(_entry(season, lot, "unsold", index=index, total=total),
                     lot)
        return None
    holder, cost = (_rtm_match(session, season, teams_by_id, lot, winner,
                               price, rng) if not accelerated else (None, None))
    if holder is not None:
        _award(session, season, holder, lot, cost, now=now, bids=bids, rtm=True,
               by_tg_id=by_tg_id)
        lot.current_bidder_id = winner.f.id
        narrator.lot(_entry(season, lot, "rtm", team=holder, price=cost,
                            bids=bids, trail=trail, rtm_from=winner.f.name,
                            index=index, total=total), lot)
        return holder
    _award(session, season, winner, lot, price, now=now, bids=bids,
           by_tg_id=by_tg_id)
    narrator.lot(_entry(season, lot, "sold", team=winner, price=price,
                        bids=bids, trail=trail, index=index, total=total), lot)
    return winner


# ──────────────────────────────────────────────────────────────────────
# The whole thing
# ──────────────────────────────────────────────────────────────────────

def _prepare_setup(session, season, now, by_tg_id):
    """A season that never started gets the parts of ``start`` that matter
    to a finished auction: retention closed, RTM cards dealt, the opening
    order remembered (so ``/arestart`` still has something to go back to)."""
    if not _remaining_lots(session, season) and not A.unsold(session, season.id):
        raise AuctionError("The pool is empty — build it before finishing.")
    A.lock_retention(session, season, now=now, by_tg_id=by_tg_id, quiet=True)
    A.sync_rtm_cards(session, season)
    A._snapshot_opening_order(session, season)


def simulate_finish(session, season, *, by_tg_id=None, quick=False, seed=None,
                    now=None):
    """Simulate every remaining lot, complete the squads, finish the auction.

    Returns a summary dict. Raises ``AuctionError`` for an auction that is
    already over. The caller commits.
    """
    now = now or datetime.utcnow()
    _check_can_finish(session, season)
    session.flush()
    if season.status == A.STATUS_SETUP:
        _prepare_setup(session, season, now, by_tg_id)
    seed = int(seed if seed is not None else season.id * 7919 + len(
        _remaining_lots(session, season)))
    field = A.franchises(session, season.id)
    teams = [_Team(session, f) for f in field]
    teams_by_id = {team.f.id: team for team in teams}
    minimum = A._as_int(season.min_squad_size, 0)
    for team in teams:
        team.done_announced = minimum > 0 and int(team.f.squad_size or 0) >= minimum

    narrator = _Narrator(session, season, quick=quick, by_tg_id=by_tg_id)
    remaining_before = len(_remaining_lots(session, season))
    narrator.say("intro",
                 f"⏩ <b>{A._e(season.name)}</b> is being fast-forwarded — "
                 f"{remaining_before} lots left, simulated.",
                 {"season": season.name, "lots": remaining_before,
                  "teams": [{"name": t.f.name,
                             "size": int(t.f.squad_size or 0),
                             "purse": int(t.f.purse_remaining_lakh or 0)}
                            for t in teams],
                  "min": minimum, "max": A._as_int(season.max_squad_size, 0),
                  "quick": bool(quick)})

    stats = {"sold": 0, "rtm": 0, "accel_sold": 0}
    stats["sold"] += _settle_the_block(session, season, teams_by_id, narrator,
                                       now, by_tg_id)
    season.current_lot_id = None

    queue = (session.query(AuctionLot)
             .filter(AuctionLot.season_id == season.id,
                     AuctionLot.status == A.LOT_QUEUED)
             .order_by(AuctionLot.lot_no.asc()).all())
    current_set = None
    for index, lot in enumerate(queue, 1):
        label = A.set_label(lot)
        if label != current_set:
            current_set = label
            left_in_set = sum(1 for other in queue[index - 1:]
                              if A.set_label(other) == label)
            narrator.say("set", f"📦 Set: <b>{A._e(label)}</b> — "
                                f"{left_in_set} players.",
                         {"set": label, "count": left_in_set})
        rng = random.Random(seed ^ (lot.id * 2654435761))
        winner = _run_lot(session, season, teams, teams_by_id, lot, rng,
                          narrator, now, by_tg_id, index=index,
                          total=len(queue))
        if winner is not None:
            stats["rtm" if lot.acquisition == A.ACQ_RTM else "sold"] += 1
            _check_team_done(season, winner, narrator)

    # One more go for everyone nobody bought — the accelerated round the live
    # auction would have run — before anything is handed out for free.
    leftovers = A.unsold(session, season.id)
    if leftovers and any(int(t.f.squad_size or 0) < A._as_int(season.max_squad_size, 0)
                         for t in teams):
        narrator.say("accel", f"⚡ <b>Accelerated round</b> — {len(leftovers)} "
                              f"unsold players, one more time at base price.",
                     {"count": len(leftovers)})
        for index, lot in enumerate(leftovers, 1):
            rng = random.Random(seed ^ (lot.id * 40503) ^ 0x5EED)
            winner = _run_lot(session, season, teams, teams_by_id, lot, rng,
                              narrator, now, by_tg_id, index=index,
                              total=len(leftovers), accelerated=True)
            if winner is not None:
                stats["accel_sold"] += 1
                _check_team_done(season, winner, narrator)
    season.accelerated_done = 1
    narrator.flush()

    # Whoever the pool still could not complete is topped up for free from
    # what is left — the live auction's own last step.
    given = A.autofill_short_squads(session, season, now=now)
    for team in teams:
        _check_team_done(season, team, narrator)

    # Every squad, as it will be published.
    for team in teams:
        narrator.say("team", f"🏏 {A._e(team.f.name)} — "
                             f"{int(team.f.squad_size or 0)} players, "
                             f"{A.render_money(team.f.purse_remaining_lakh, season.currency_label)} left.",
                     _team_detail(session, season, team.f), franchise=team.f)

    # Completed exactly as ``complete_if_done`` completes a season.
    season.status = A.STATUS_COMPLETED
    season.current_lot_id = None
    A._focus_changed(season, session)
    A._auction_wrap_news(session, season)
    A.log_event(session, season, "season_completed",
                "🏁 Every lot is resolved — the auction is complete. "
                "An admin can publish the squads now.",
                by_tg_id=by_tg_id, by_admin=True)
    summary = _summary(session, season, teams, stats, len(given))
    # The last word is a sim event, so the scheduler keeps draining this
    # (now completed) season until the room has heard all of it.
    narrator.say("outro", "🏁 Simulation complete — every squad is final. "
                          "Publish with /apublish.", summary)
    session.flush()
    return summary


def _team_detail(session, season, franchise):
    players = []
    for lot in A.squad(session, franchise.id):
        players.append({"name": lot.name, "rating": lot.rating,
                        "role": lot.category, "overseas": bool(lot.is_overseas),
                        "price": int(lot.sold_price_lakh or 0),
                        "how": lot.acquisition or A.ACQ_AUCTION})
    owed, _ = A.role_shortfall(session, season, franchise)
    return {"team": franchise.name, "size": int(franchise.squad_size or 0),
            "min": A._as_int(season.min_squad_size, 0),
            "max": A._as_int(season.max_squad_size, 0),
            "overseas": sum(1 for p in players if p["overseas"]),
            "max_overseas": A._as_int(season.max_overseas, 0),
            "purse": int(franchise.purse_remaining_lakh or 0),
            "owed": owed, "players": players}


def _summary(session, season, teams, stats, autofilled):
    buys = (session.query(AuctionLot)
            .filter(AuctionLot.season_id == season.id,
                    AuctionLot.status == A.LOT_SOLD,
                    AuctionLot.sold_price_lakh.isnot(None),
                    AuctionLot.sold_price_lakh > 0)
            .order_by(AuctionLot.sold_price_lakh.desc()).limit(3).all())
    names = {t.f.id: t.f.name for t in teams}
    minimum = A._as_int(season.min_squad_size, 0)
    return {
        "sold": stats["sold"], "rtm": stats["rtm"],
        "accel_sold": stats["accel_sold"], "autofilled": autofilled,
        "unsold": len(A.unsold(session, season.id)),
        "top_buys": [{"name": lot.name, "team": names.get(lot.sold_to_id, "?"),
                      "price": int(lot.sold_price_lakh or 0)} for lot in buys],
        "teams": [{"name": t.f.name, "size": int(t.f.squad_size or 0),
                   "purse": int(t.f.purse_remaining_lakh or 0),
                   "spent": int(t.f.purse_total_lakh or 0)
                   - int(t.f.purse_remaining_lakh or 0),
                   "complete": int(t.f.squad_size or 0) >= minimum}
                  for t in teams],
    }


# ──────────────────────────────────────────────────────────────────────
# Playback control
# ──────────────────────────────────────────────────────────────────────

def playback_pending(session, season):
    """How many ``sim_*`` messages the room has still to be shown."""
    return int(session.query(AuctionEvent)
               .filter(AuctionEvent.season_id == season.id,
                       AuctionEvent.id > int(season.announced_event_id or 0),
                       AuctionEvent.kind.like(SIM_PREFIX + "%"))
               .count())


def mute_playback(session, season):
    """Skip straight to the final summary of a simulation still playing."""
    outro = (session.query(AuctionEvent)
             .filter(AuctionEvent.season_id == season.id,
                     AuctionEvent.id > int(season.announced_event_id or 0),
                     AuctionEvent.kind == SIM_PREFIX + "outro")
             .order_by(AuctionEvent.id.desc()).first())
    if outro is None:
        raise AuctionError("No simulation is playing back right now.")
    skipped = playback_pending(session, season) - 1
    before = (session.query(AuctionEvent.id)
              .filter(AuctionEvent.season_id == season.id,
                      AuctionEvent.id < outro.id)
              .order_by(AuctionEvent.id.desc()).first())
    season.announced_event_id = before[0] if before else outro.id - 1
    session.flush()
    return max(0, skipped)

