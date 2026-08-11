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

## 2. Buying above 95 pays gems back

Signing a card rated **above 95** (i.e. 96+) rebates **0.1% of the coins
spent**, paid in gems:

| OVR | Buy | Gems back |
|----:|----:|----------:|
| 100 | 6,435,000 | 6,435 |
|  97 | 4,450,000 | 4,450 |
|  96 | 3,800,000 | 3,800 |
|  95 | 3,340,000 | — |

```python
GEM_BONUS_MIN_RATING = 96   # "above 95"
GEM_BONUS_BPS = 10          # 10 basis points = 0.1%
get_buy_gem_bonus(rating, price_paid=None)
```

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
