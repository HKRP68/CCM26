# Player pricing: 55% resale and the elite signing bonus

Two rules govern what a player card is worth, and both live in `config.py`
so nothing downstream has to restate them.

## 1. A card sells for 55% of its buy price

There used to be two hand-maintained columns per rating — a buy price and a
sell price — and they drifted. Re-tuning a price meant remembering to re-tune
its resale, and the return rate wandered between roughly 26% and 45%
depending on where you looked in the table.

Now there is one column:

```python
SELL_VALUE_PCT = 55

BUY_VALUES = {100: 6_435_000, 99: 5_840_000, ...}

def sell_value_of(buy_value):        # rounded half-up
    return (int(buy_value) * SELL_VALUE_PCT + 50) // 100
```

`get_sell_value(rating)` is `sell_value_of(get_buy_value(rating))`, so the
rate is identical at every rating: **buy at 100, sell at 55**. Change a price
and its resale follows automatically; change `SELL_VALUE_PCT` and the whole
economy moves at once.

Selling is still a loss at every rating — that is the property the release,
overflow, giveaway and roster-value paths depend on, and
`tests/test_player_pricing_and_gem_bonus.py` asserts it for the full table.

`BUY_SELL` (the old `rating -> (buy, sell)` mapping) is kept as a derived view
so older callers that unpack the pair keep working. It is never edited by
hand; its sell column is always the 55% figure.

### The market can't be priced below resale

Releasing a card pays `get_sell_value` no matter what its owner paid for it,
and player-market stock is unlimited by default. A slot priced under that
figure is therefore a coin printer — buy it, release it, buy it again. Raising
resale to 55% widened that window considerably (the old sell column sat nearer
29% of the buy price), so the market now floors every price at
`config.market_price_floor(rating)` — one coin above what releasing pays.

The floor applies **after** the membership discount, not before: 10% off a
price that only just cleared resale would dip straight back under it. It bites
only on deep admin sales — an ordinary listing sits at 100% of catalog, so a
Diamond member's 10% still lands far above the floor and the perk is untouched.

`global_market.player_slot_prices(user, slot, player)` returns the
`(list price, this buyer's price)` pair with both floored, and every surface
that shows or charges a market price goes through it — bot `/playermarket`,
the Mini App market list, and both buy endpoints — so a buyer is always charged
the price they were quoted. When the floor bites, `buy_player` logs a warning
naming the slot; the admin markets page still shows the number the admin typed.

The trait market has the same rule for the same reason — see
`roll_trait_discount` and the `TRAIT_SELL_*` notes in `config.py`.

## 2. The Elite Signing Bonus — a limited-time offer

While the offer is running, signing a card at or above its rating threshold
rebates a slice of the coins spent, paid in gems. At the default terms — **96+
at 0.1%**:

| OVR | Buy | Gems back |
|----:|----:|----------:|
| 100 | 6,435,000 | 6,435 |
|  97 | 4,450,000 | 4,450 |
|  96 | 3,800,000 | 3,800 |
|  95 | 3,340,000 | — |

It is an **offer, not a permanent rule**. An admin runs it from
**Economy → 💎 Elite Signing Bonus**:

| Control | Meaning |
|---|---|
| Offer is open | The switch. Off = nobody is paid, and nothing advertises it. |
| Minimum rating | Cheapest card that qualifies. 96 means "above 95". |
| Rate, % of price paid | Stored as basis points, so payouts stay exact integers. |
| Starts (IST) | Blank = right away. Set it and the offer waits, showing as *Scheduled*. |
| Ends (IST) | Blank = until you close it. Set it and the offer closes itself. |
| Close Now | One click, no date editing. Submits even mid-edit (`formnovalidate`). |

The page shows a LIVE / SCHEDULED / CLOSED badge, the time remaining, and a
worked example at the current terms — deliberately the *cheapest* qualifying
card, so the number on screen is the smallest bonus those terms hand out.

### Reading it

```python
services.buy_bonus.current_offer(session) -> Offer
    .active         # switch AND window already applied
    .min_rating .bps .percent
    .seconds_left   # None when open-ended
    .scheduled      # configured, waiting for its start time
    .gems_for(rating, price_paid=None)
```

`current_offer` is the only correct way to ask. It reads **fresh** rather than
through `config_service`'s process-local cache, because the admin website and
the bot usually run as separate processes and opening or closing an offer has
to take effect on the next buy without a restart — pass the session you already
hold and it costs one single-row query.

It **fails open** to the baked-in rate (`config.GEM_BONUS_MIN_RATING` /
`GEM_BONUS_BPS`): a database hiccup should not silently withdraw a bonus
players have been promised. `config.get_buy_gem_bonus` remains pure arithmetic
— it answers "what would this rate pay?", never "is the offer running?".

The `game_config` defaults reproduce exactly what shipped hard-coded (on, 96+,
0.1%, no window), so deploying this changes nothing until an admin touches it.

> `config_service.save_config` skips `None` values so partial forms don't wipe
> fields they don't send. Nullable settings therefore need
> `allow_null=("gem_bonus_starts_at", "gem_bonus_ends_at")` — without it,
> clearing a date field silently keeps the old date.

### It is sized on coins paid, not on the list price

`price_paid` defaults to the card's list value, but every buy path passes the
real charge. That matters where the buyer pays less than list — a Platinum or
Diamond market discount, or an admin's sale price — because a rebate pegged to
the list price would let a discounted slot mint gems the sale never charged
for. Pay less, get less.

### Where it is applied

All four buy paths route through `services/buy_bonus.py`, so the credit, the
`buy_gem_bonus` audit row and the wording can't drift apart:

| Path | Entry point |
|---|---|
| Bot `/buypl` | `handlers/buy.py` → `buypl_confirm_callback` |
| Bot `/playermarket` | `services/global_market.py` → `buy_player` |
| Mini App direct buy | `admin.py` → `webapp_buy` |
| Mini App market buy | `admin.py` → `webapp_market_buy` |

`buy_player` now returns `(ok, message_or_name, gem_bonus)` — the third value
is the rebate that was credited.

The bonus is credited on the caller's transaction, before its `commit()`, so a
rolled-back buy never leaves gems behind.

An in-flight buy reads the offer once and passes it to both the award and the
receipt, so a purchase can't be paid from one offer and described by another if
an admin closes it mid-transaction.

### Where players see it — and stop seeing it

Everything below renders from the same `Offer`, so closing the offer takes the
advertising down with it in the same moment:

* **Bot** — a "💎 Elite Signing Bonus" line on the player card (`/buypl`) and on
  the market screen (`/playermarket`), sized on what *that* buyer will pay, with
  `· ends in 3h 20m` when the offer has an end time. Receipts show the gems
  credited plus the new balance.
* **Mini App** — a banner across the top of the market stating the offer's terms
  and its clock, a note under the buy button, a `+N 💎` tag on qualifying market
  rows, and the gems in the success toast. Served by `gem_bonus`,
  `gem_bonus_ends_in` and `gem_bonus_offer` on the player-detail, market and buy
  payloads.
* **`/howto` → Economy** — one bullet describing the running offer and its
  countdown, rendered per request by `_signing_bonus_bullet()` and **omitted
  entirely** while the offer is closed. (The rest of the tab is a module-level
  constant; the bullet is substituted into a `{signing_bonus}` placeholder at
  render time, since a tutorial built at import time can't know today's terms.)

### Undo takes the gems back

`/cmuundo` reverses a buy, so it must reverse the rebate too — otherwise
buy-then-undo is a gem printer. `undo_service.record_buy` stores `gem_bonus` in
the undo payload and `handlers/undo.py` deducts it on reversal.

If the buyer has already spent those gems, the undo is **refused** rather than
clamped at zero, the same way an undone release is refused when the coins are
gone. The undo record is left in place, so it still works if they top up
before the 60-second window closes.

### Market buys are not undoable, and the rebate depends on that

Only the two direct-buy paths write an undo record. A market buy also moves the
slot's stock — `purchased_count`, plus a `MarketPurchase` audit row — and the
undo handler knows nothing about either, so reversing one would permanently eat
a copy of a limited run.

`webapp_market_buy` used to call `record_buy(db, user.id, player.id, price)`,
which always raised `TypeError` against `record_buy`'s keyword-only signature
and had the exception swallowed — so market buys have never been undoable in
practice. That dead call is removed rather than repaired: the gem rebate is
only safe to credit on these paths *because* nothing can reverse the purchase
and leave the gems behind. Making market buys undoable is a real feature, and
it needs the stock accounting first.

### Where players see it

* Bot player card (`/buypl`, `/playermarket`) — an "Elite Signing Bonus" line
  on the card, before they commit, sized on what *they* will pay.
* Purchase receipts — the gems credited, plus the new gem balance.
* Mini App — a note under the buy button, a `+N 💎` tag on elite market rows,
  and the gems in the success toast (`gem_bonus` on the player-detail, market
  and buy API payloads).
* `/howto` → Economy — both rules, generated from the config constants.
