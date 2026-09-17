# Franchise Auction

The Tournament Draft answers "how does a Challenge League get its squads?" one
way: an admin uploads a pick order and the owners take turns.
[`docs/player-draft.md`](player-draft.md) ends with what it could not do —

> **Auction mode** — purse, bids, RTM cards. The pool and team tables would
> carry it; the bidding loop is the new part.

This is that. Franchises start with a purse, every player has a base price, and
the room decides what each one is worth. It ends the same way a draft does, by
writing real `ChallengeTeam` / `ChallengePlayer` rows, so everything downstream
— the `/cipl` match, the XI picker, the points table, the playoff bracket — is
code that already existed and neither knows nor cares that an auction produced
the squads.

---

## Money is a value, not coins

Every amount is an **integer count of lakh**, and every such column is suffixed
`_lakh`. ₹1.5 Cr is `150`; ₹100 Cr is `10_000`. There is no float in any
column, comparison or sum — the only decimal in the whole feature lives inside
`parse_amount` for as long as it takes to turn what somebody typed into lakh.

It is a **unit of its own with no relationship to the coin economy**. A
franchise purse cannot buy a card in `/playermarket`, and a player's coins are
worth nothing at an auction. Nothing in `config.BUY_VALUES` is consulted.

**A bare number is crore**, because that is how the room talks and how the
proposal writes it: `/bid 15` is ₹15 Cr against a ₹100 Cr purse. Lakh has to be
said out loud — `/bid 75L`, `/bid 75 lakh`. Every prompt prints the next
minimum in the same form, so the commonest action is typing back the number the
bot just showed, and `tests/test_auction_bidding.py` pins that
`parse(render(n)) == n` for every lakh up to ₹20 Cr — a display rule and an
input rule that drifted apart would charge somebody a hundred times what the
board said.

---

## Running one

### Setting it up (the website)

**Admin → Tournament Panel → 🔨 Franchise Auctions → Create an auction**, then
open it.

**Settings** — the group chat id, seconds per lot, the opening purse, squad
minimum and maximum, home country and the overseas cap, and the three
anti-snipe numbers.

**Base prices by rating** — a ladder, highest band first; a card takes the first
band its rating reaches.

> The price is **stamped onto each lot when the lot is built**, not looked up
> when it opens. Editing the ladder afterwards cannot move a price that a lot
> already went on the block at, and cannot move one mid-auction. A single
> player is overridden from the pool table, while the lot is still queued.

**Franchises** — name, city, logo, owner and co-owners, and a purse that
defaults to the season's. The owner is a **Telegram id, not an account here**,
for the reason `DraftTeam` and `TournamentTeam` both use one: an admin builds
the field from a list, and some of those people have never messaged the bot.
Owner and co-owners are equals for every check `/bid` makes.

**Auction pool** — filter the global catalogue by name, rating band, country,
role, batting hand, bowling style and card edition; preview; then add the
ticked players or everything the filter matches. Career cards and inactive rows
are excluded before you see them.

> Adding is **idempotent per player**. A unique `(season_id, player_id)` means
> re-running the builder with a corrected filter adds what is new and leaves
> what is already there exactly as it is, including anything already sold. A
> pool builder you cannot safely run twice is one nobody dares run once.

### Running it (the group)

```text
/anew Season 2      ← in the group the auction should live in
/atimer 30
/asnipe 10 10 5
/astart
```

| Command | Who | What |
| --- | --- | --- |
| `/bid [amount]` (`/bd`) | owner + co-owners | Bid on the lot on the block. **Bare `/bid` bids the next minimum** — the commonest action, and the one form that cannot be fat-fingered with ten seconds on the clock |
| `/aboard` | anyone | A personal copy of the live board, with quick-bid buttons |
| `/apurse [franchise]` | anyone | Every purse, max bid and squad count — or one franchise's squad |

Admin: `/auction` (the reference card), `/anew`, `/abind`, `/astart`,
`/apause`, `/aresume`, `/anext`, `/aextend`, `/asold`, `/aunsold`,
`/aundobid`, `/awithdraw`, `/atimer`, `/asnipe`, `/agrant`, `/aco`,
`/apublish`, `/acancel`.

`/bid`, `/aboard` and `/apurse` are **not** in the group slash menu. Both
player scopes sit exactly at Telegram's 100-command ceiling and `_clamped`
drops the tail rather than letting `setMyCommands` reject the whole call, so
publishing three auction commands would cost three existing player commands
their entry. It is the same call `/dtrade` and `/dtrades` already made, and the
room is told about them where it matters instead: the `/auction` card, the
board's own footer, and `/help`. The eighteen admin commands are in
`ADMIN_MENU_COMMANDS`, which is exempt from the clamp and published only into
admin DMs, so they cost nobody anything.

### Running it (the website)

**🔨 Console** is the same auction, driven with buttons: start, pause, next
player, extend, mark sold, mark unsold, undo the last bid, withdraw, and a
**bid-for-a-franchise** form for an owner whose phone has died. That last one
is stamped `by_admin` and announced in the room as an admin's bid — a bid that
reads as the owner's own choice when it was not is how a result gets disputed.

The console polls an HTML fragment every two seconds and ticks the countdown
client-side off a `data-deadline` attribute, so the number moves smoothly
without a request per second. It renders *the same partial* the full page
includes, so there is one copy of the markup; and it stops auto-refreshing when
a response comes back redirected, because an expired admin session answers with
the login page and painting that into the console would look exactly like the
auction having vanished. All three are `admin_live_matches`'s calls, made again.

### Finishing

`/apublish` (or the button) writes one `ChallengeTeam` per franchise and one
`ChallengePlayer` per bought lot. Publishing again re-syncs the same league
rather than creating a second one, so a correction can simply be republished.

---

## Retention

Before the auction opens, a franchise keeps some of the players it already has,
at a price, and that spend comes off the top of its purse. All of it happens
while the season is still in **setup**.

```text
/aretlock                              the state of it, and every franchise's keeps
/aretain Mumbai | Virat Kohli          at the ladder's next slab
/aretain Mumbai | Virat Kohli | 12     at a price you choose
/aunretain Virat Kohli                 back into the pool, purse refunded
/aretlock on                           close the window early
```

…or the **🔒 Retention** card on the auction's setup page, which is where the
player search and the purse table live.

### A retained player is an ordinary sold lot

`status = sold`, `acquisition = retained`. Not a status of its own — because
`squad`, `overseas_count`, `role_counts` and `publish_to_league` all key on
`sold` and then need *no change at all*. A retained player is in the squad,
counts against the overseas and squad caps, and reaches the published league
(carrying `acquisition` on its `details_json`), for free.

**Retention creates the lot from the catalogue**, so it does not need the pool
built first — which is the order the proposal asks for. Because the pool
builder skips any player who already has a lot, building the pool afterwards
leaves retained players out automatically. Build it the other way round and a
player already sitting in the queue is converted in place rather than refused:
an admin who did it backwards should not hit a wall.

Two places had to be taught the difference, because "sold" alone is no longer
the whole story:

* **`pool_counts`** reports `retained` separately, and its `sold` and `total`
  are *auction* figures. Otherwise the board would announce "3/20 lots
  resolved" before the first lot ever opened.
* **`undo_sale` refuses a retention** and names `/aunretain`. Undoing one there
  would refund through the wrong ledger kind, leave `retained_count` standing,
  and put somebody nobody bid for on the block.

### The ladder pre-fills; the caps refuse

The slab ladder — 1st retention ₹18 Cr, 2nd ₹14 Cr, 3rd ₹11 Cr — decides what
the form and the command **default to** for a franchise's next keep, and past
its end the last slab repeats. It is not binding: any price can be typed over
it. Making the slab binding would only invite juggling the retention order to
dodge the expensive rungs, and an admin who sees the number before committing
does not need protecting from it.

What actually refuses, each naming the number that would have worked: the
window, the **count** (`max_retentions`), the **budget**
(`retention_max_spend_lakh`), the purse, the squad and overseas caps, the
optional **rating band and role restriction**, and **reachability**.

That last one is the same rule bidding uses, and the same call —
`max_bid_now`. A franchise cannot retain its way into being unable to fill its
minimum squad at base price, and the rule stops applying the moment the minimum
is met. Because retention moves the purse and the squad through the same path a
purchase does, the bidding rule picks up exactly where retention left off.

> One wrinkle worth knowing: `min_base_price_lakh` is stamped when the *pool*
> is built, so a retention computes its reserve against the default floor and
> the pool build re-stamps it afterwards. Not a correctness problem — the
> bidding rule re-reads the column — but the ceiling shown during retention can
> move once the pool lands.

`min_retentions` is enforced **at `start()`**, by name, not at retention time:
nothing a retention *does* can fix a franchise being under the minimum, so
refusing earlier would be a complaint nobody could act on.

### The window

There is no `retention` season status. Retention happens while the season is in
`setup`, and `retention_locked_at` says whether the window is shut — a status
would have to be threaded through the pool guard, the sweeper's filter, every
status pill and `start()`'s resume path for nothing a timestamp does not
already give. There is no `retention_enabled` boolean either: `max_retentions >
0` carries the same fact, and a non-nullable boolean added to a populated table
reads back NULL-as-falsy on every existing row.

The **deadline is enforced lazily** — `retain()` refuses once it has passed.
Nothing sweeps during retention (the clock job only looks at live seasons), so
there is nothing to close the window with and nothing that needs one. What that
costs is visibility, so the setup page and `/aretlock` both carry a live
"closes in 3h 20m" / "closed 2 days ago" line. A deadline nobody can see until
it refuses them is the failure mode here.

**Starting the auction closes retention**, quietly — the "under way"
announcement already says the squads are what they are.

### Who held whom last season

`AuctionSeason.previous_league_id` remembers the league this season follows, and
`previous_squad_map` turns it into `{player_id: franchise}` — matched by
`source_player_id`, never by name, because two cricketers sharing a name would
be quietly mis-assigned and that is the one mistake retention (and later RTM)
must not make.

The retention picker uses it to sort each franchise's own former players to the
top and badge them ⭐, and **warns rather than refuses** when somebody retains a
player they did not hold. An admin untangling a mess has to be able to put a
player anywhere; the warning is the brake, not a gate. The same map is what
`link_previous_season` stamps onto lots once the pool exists, so the picker and
the stamper can never disagree.

### The purse, read from both ends

`purse_total_lakh` is where a franchise started, `retention_spent` is the sum of
what it kept, and `purse_remaining_lakh` is what it takes into the auction. That
is the proposal's Starting Purse / Retention Spent / Auction Purse table, and
the tests assert the identity holds after every retention and every release.
Retention spend is **derived, not cached** — the purse column is a cache for a
reason that does not apply here, and a second cache is only a second thing to
drift.

---

## How the website talks to the group

**The Flask admin panel never touches Telegram.** It runs in a thread of the
bot's process, and reaching across that boundary is the bug this design exists
to avoid.

Instead every mutating call — from a command or from a web form — writes its
rows plus exactly one **`AuctionEvent`**, and commits. The bot's sweeper drains
everything past `AuctionSeason.announced_event_id` and says it out loud, in id
order. Nothing but the database crosses over, the room's message order is the
id order of one table whichever surface produced it, and "Mark Sold" in the
browser and `/asold` in the group are literally the same function call.

The event log had to exist anyway — the proposal asks for a permanent one — so
this costs nothing.

---

## The clock

`services/auction_scheduler.py` sweeps every **two** seconds, not the draft's
fifteen. A pick clock runs for fifteen minutes, so landing fifteen seconds late
costs nothing; a lot runs for thirty seconds, and at the draft's interval an
auction would sell up to half a lot-length late and refuse bids by a rule the
room cannot see. The cost is one indexed statement that returns nothing at all
while no auction is live.

**The deadline is authoritative; the sweeper only announces.** `place_bid`
carries `deadline_at > now` inside its own conditional `UPDATE`, so a bid
arriving after the true deadline is refused *by the command*, before the job has
run. The sweeper never decides whether a bid was in time — only when the room is
told. One consequence falls out for free: a process that was down for ten
minutes resolves correctly on its first tick back, because every bid in the
table is still valid and the lot simply sells to the standing top bid.

**The clock lives on the lot, not the season** — `AuctionLot.deadline_at`,
`.going_stage`, `.extensions_used`. That is what lets the anti-snipe extension
be applied *in the same statement as the bid that earned it*. Applied
separately, two bidders on the same tick both read the deadline, both decide
they are inside the window, and the clock goes out twice for one bid.

### What reaches Telegram, and when

**No per-second countdown, and no reply to a successful bid.** Forty bids inside
one lot would be forty messages into a room that is already reading a live
board.

* **New messages** go out for discrete events only — a lot opening, a sale, a
  pass, a pause. That is two or three per lot: roughly 300 across a 100-lot
  auction, well inside a group's limit.
* **Everything else is an edit of one pinned board.** `/bid` does not edit it;
  the sweeper notices `lot.bid_count != season.board_rendered_bid_count` and
  performs **at most one edit per two-second tick**, so ten bids inside one tick
  cost one edit.
* A bidder's own acknowledgement is a **reaction on their own message** — zero
  messages. A *refused* bid always answers, and always says what would have
  worked instead; on a thirty-second clock, "invalid bid" is useless.

Every edit goes through a hardened helper modelled on
`services.match_broadcast.reveal_toss_result`: `RetryAfter` is honoured, and a
`BadRequest` reading "not modified" is treated as **success** — two bids in one
tick can render byte-identical text. It is caught before the `NetworkError`
clause, because PTB's `BadRequest` subclasses it. A failed edit never rolls
anything back; the bid is already committed.

---

## Two people typing at once

Two co-owners bidding on the same tick, and an admin pressing **Mark Sold**
while a bid lands, are the same problem: two writers, two sessions, two threads,
sharing nothing but the database. The draft's answer is a conditional `UPDATE`
so exactly one of two simultaneous `/pick`s wins. The auction's is the same
device, three times over.

**The bid claim** carries every precondition in its own `WHERE` — the lot is
still on the block, the deadline has not passed, the amount beats the standing
bid, and the bidder is not already top — and applies the anti-snipe extension in
the same statement with a `CASE`. One row changed means this bid won; zero means
something moved underneath it, and only then is the row re-read, to say *which*
thing moved. The `AuctionBid` row is inserted after the claim succeeds, so a bid
that did not take leaves no trace and `MAX(id)` for a lot is always the standing
bid — which is what "undo the last bid" restores from.

**The sale** reads `current_bidder_id` and `current_bid_lakh` *inside* the same
`UPDATE` that closes the lot, so both orderings are correct with no lost update:
bid-then-sell sells at the higher price, sell-then-bid refuses the bid by
`status = 'on_block'`.

**The debit** is `WHERE purse_remaining_lakh >= :price AND squad_size < :max`.
Zero rows means a purse moved under a validation from a few milliseconds ago,
and the whole transaction rolls back *including the sale* — the lot is still on
the block. It cannot half-happen.

All three are single statements, so they behave identically on SQLite (dev) and
Postgres (prod). There is no `SELECT … FOR UPDATE`, which SQLite does not have.

> **A dev-only caveat.** `database.py` sets no busy timeout for SQLite, so under
> Flask-thread/PTB-loop write contention a dev database can raise
> `database is locked`. Production is Postgres, where the row lock serialises
> correctly.

---

## The purse: a ledger, and a cache that answers to it

The proposal asks for a transaction-based purse ledger, and `auction_ledger` is
it: every `opening`, `purchase`, `refund` and `correction` is a signed row
carrying the balance at the moment it was written. The opening purse is itself a
row, which is what makes

```text
SUM(auction_ledger.amount_lakh) == auction_franchises.purse_remaining_lakh
```

true from the very first moment rather than only after the first sale. The test
suites assert it **after every single mutation**, not once at the end — a cache
that is only right at the end is a cache that was wrong in the middle of
somebody's auction.

**Why there is a cached column at all**, given the ledger is the truth: not for
speed. A season has ten franchises and a few hundred rows; `SUM()` costs
nothing. It is for **atomicity** — you cannot write a safe conditional `UPDATE`
against an aggregate on both backends, and the read-then-write alternative has a
window in it, which is precisely where the website's "Mark Sold" lives. So the
column is written only ever in the same transaction as its ledger row, and
`reconcile_purses` re-sums and reports any drift. Repairing writes a
`correction` row rather than overwriting either side: the cache is what the
auction actually charged people and the ledger is the record of why, and
silently rewriting either destroys the evidence of whichever went wrong. The
setup page shows the drift and the button, and
`migrate_auction_purse_reconcile.py` runs the same function from the shell.

**A bid is deliberately not a ledger row.** Nothing moves when you bid — you are
outbid twenty seconds later and nothing happened. The purse is debited exactly
once, when a lot sells. A ledger that recorded bids would need a matching
reversal for almost every row and would turn "undo the last bid" into a money
operation. The losing bids are kept in `auction_bids`, which is the stronger
audit anyway: it is the only place the *price's* history lives.

---

## What refuses a bid, and why it says so

In order, each with its own message naming the number that would have worked:

1. the auction is live (a paused auction says so), a lot is on the block, and
   the clock has not run out;
2. the bidder is not already the top bidder;
3. the amount clears the base price, or the standing bid plus the increment;
4. the squad cap;
5. the purse;
6. **reachability**;
7. the overseas cap, and role minimums as reachability.

### Reachability, in money

The draft refuses a pick "while there is still a slot left to fix the problem
with, never afterwards". The money version:

```text
slots_after = max(0, min_squad - (squad_size + 1))
reserve     = slots_after * min_base_price_lakh
max_bid     = purse_remaining - reserve
```

Two properties are load-bearing.

**Once the minimum is met the rule stops applying**, and a franchise may spend
its last rupee. That is the auction's endgame, and blocking it with a rule about
slots that no longer exist would be a bug rather than a safeguard.

**`min_base_price_lakh` is the pool's cheapest base price, stamped when the pool
is built** — not "the cheapest lot still available". The tighter version is
marginally more correct and much worse to play against: the ceiling would move
every time some *other* franchise bought a cheap player, for reasons invisible
to the person steering by it.

`max_bid_now()` is printed on the board, in `/apurse`, on the console and beside
every franchise on the setup page, for the reason `/dsquad` prints every squad
rule whether or not it is being broken: *a rule you only see once you have
broken it is one you cannot plan around.*

---

## Undo, and what it refuses to reach through

| | |
| --- | --- |
| **Undo the last bid** | A price operation. The bid is marked `is_void` — never deleted, because it is part of why the price moved and the room watched it happen, and voiding is idempotent where a delete-and-retry is not. The clock is floored at one full extension afterwards: the undone bid may well be the one that bought the last extension, and without the floor an undo with two seconds left hands the lot to the previous bidder before anybody can react. |
| **Undo a sale** | A money operation, and a separate one. The purse is credited back with a `refund` row so the ledger still adds up, the winning bid is voided so the lot does not instantly re-sell at the same number to the same franchise, and the lot goes back on the block with a fresh clock. **Refused once the auction has been published** — making the league agree again needs a republish, and doing that silently is how a squad people are already playing with changes underneath them. |
| **Mark unsold** | **Refused while a bid stands.** Passing a lot somebody has bid for would void a franchise's winning bid with nothing on the record; undoing the bid first leaves a row with the admin's name on it. That is the difference between a correction and a disappearance. |
| **Re-list** | An unsold player goes back to the tail of the queue **on the same row**, with `times_unsold` incremented. One row per player per auction is what keeps the unique index that makes `/bid`'s name lookup unambiguous. |

---

## Who may bid

Only a franchise's **owner and its co-owners**, and only in the bound group.

**Bot admins deliberately cannot bid**, exactly as they cannot `/pick` in a
draft. An admin who has to act for an owner who is not in the room uses the
console's bid-for-a-franchise form, which records and announces it as an
admin's bid. Everything else in the feature widens to admins; this one narrows,
because it is the only one where acting for somebody else could be mistaken for
them acting.

`/bid` in a DM is refused. An auction is a public event the room has to be able
to see and argue with, and a bid made quietly is how a price gets disputed.

### The quick-bid buttons

`au_bid_` is a **shared** callback prefix, not an owner-locked one — it rides on
the one pinned board every franchise is watching, and a board the second
franchise cannot press is not an auction. It is the same call `dt_` makes for
`/dtrade`. Every press is authorised against the franchise the presser actually
owns, and the exact price is baked into the callback data, so a button pressed
after the price has moved is refused with "the price has moved" rather than
quietly bidding a number nobody meant.

---

## Data model

```text
AuctionSeason       the auction: status, bound chat, the lot timer, anti-snipe,
                    the purse default, squad rules, the base-price ladder, the
                    retention rules and ladder, the league it follows, the
                    pinned board and the announcement cursor
AuctionFranchise    a franchise: name, city, logo, owner + co-owners, purse
AuctionLot          one player — the lot that goes on the block AND its result,
                    retained players included (``acquisition`` tells them apart)
AuctionBid          every bid, losing and voided ones included
AuctionLedgerEntry  every movement of a purse, signed, with the balance after
AuctionEvent        the permanent log, and the queue the group is announced from
```

Six new tables, so `create_all` builds them and there are **no `_try_add`
lines** — but all six are in `database.init_db`'s explicit import list anyway.

**`AuctionLot` is one table, not two.** The pool and the result log are the same
rows on purpose, the call `DraftPick` already makes: *"Bumrah is lot 4 at a base
of ₹2 Cr"* and *"lot 4 went to Mumbai for ₹12.25 Cr"* are the same fact at two
points in time. Two tables would need an unenforceable "exactly one result per
lot" invariant and a `LEFT JOIN` on every dashboard query.

**The player columns are a snapshot, not a view** over `players`, the same
reason `DraftPlayer` is its own table: the catalogue moves — a card is re-rated,
retired, deactivated — and a finished season has to stay readable afterwards.
`player_id` keeps the link, for the card image and for publishing.

**`AuctionEvent` is separate from `AuctionBid`** for the reason `DraftSquadEdit`
is separate from `DraftTrade`: a bid has a franchise, an amount and a lot,
always, and each of them means something. An event has a kind and a headline and
its other columns are meaningful only for some kinds. One table would leave
`amount_lakh` NULL on two thirds of the rows and turn "the bid history for this
lot" into a filtered scan over the room's whole narrative.

**Base prices are a JSON column, not a seventh table** — the call
`PlayerDraft.tier_order_json` makes. A handful of bands, edited as a whole,
never queried by. What is load-bearing is not where they live but that the
answer is *stamped onto the lot*.

### One `details_json`, one filter

Two things were on their way to a third copy each, and both fail silently.
`services/player_query.py` now holds both:

* **`master_player_query`** — the union of `admin._challenge_filters` (which
  matched ratings by exact equality and could not filter by edition) and
  `admin.players_list`'s range and version filters, with `not_career()` and
  `is_active` applied once.
* **`challenge_details_json`** — the key set
  `services.cipl_match.cp_to_player_dict` reads. The match engine takes every
  rating and handedness from that blob, *not* from the master `players` row, so
  a drifted copy means a squad plays with the wrong numbers and nothing says so.
  `admin._challenge_player_details_from_source` now delegates to it, and
  `tests/test_franchise_auction.py` runs a published row back through
  `cp_to_player_dict` as the belt to that brace.

---

## Files

| File | Role |
| --- | --- |
| `services/auction_service.py` | The rules: the pool, base prices, the ledger, validation, the bidding claim, the lot lifecycle, publishing, the renderers. Session-first, **commits nothing**, raises `AuctionError` in plain text; only `render_*` emits HTML. Every clock decision is a pure function taking an injected `now` |
| `services/auction_scheduler.py` | The two-second sweeper, the pinned board, the hardened edit, and the event drain |
| `services/player_query.py` | The one master-player filter, and the one `details_json` |
| `handlers/auction.py` | `/bid` and every other command, plus the `au_bid_` buttons |
| `models.py` | The six tables |
| `admin.py` | `/auctions`, `/auctions/<id>`, `/auctions/<id>/console` and its polled panel |
| `templates/admin_auctions.html`, `admin_auction_detail.html`, `admin_auction_console.html`, `_auction_console_panel.html` | The pages |
| `static/css/admin_auction.css` | Their stylesheet, via `{% block head_extra %}` |
| `migrate_auction_purse_reconcile.py` | Re-sums every ledger, `--dry-run` first; the work is the service's, shared with the admin button |
| `tests/test_franchise_auction.py` | The pool, base prices, the ledger, reachability, the overseas cap, and publishing |
| `tests/test_auction_bidding.py` | The lifecycle, bidding, two-session concurrency, the clock, anti-snipe, undo, permissions, the commands and the board |
| `tests/test_auction_retention.py` | The ladder, the money, every cap, the window, what retention does to the pool and the board, publishing a retained player, and the commands |

---

## Not built (yet)

* **Right To Match.** `rtm_enabled`, `rtm_cards_total` / `rtm_cards_used`,
  `AuctionLot.rtm_offered_at` / `rtm_matched_by_id`, and the `LEDGER_RTM` kind
  are all in place and unused. `rtm_enabled` defaults `False` and no branch
  tests it. Its two inputs are already here and already working —
  `previous_squad_map` and `AuctionLot.previous_franchise_id` — because who
  held a player last season only gets harder to recover as time passes. What
  RTM still needs is a price mode (`bid`, `bid + N`), a per-franchise card
  count, and the hard part: a two-party confirmation under a clock, wedged
  between the final bid and the sale committing. `DraftTrade` is the precedent
  for keeping that offer in a row rather than in process memory.
* **Season templates** — saving a finished configuration and starting the next
  season from it.
* **A Mini App auction board.** The board is a Telegram message today; the
  `/webapp` plumbing would serve a live web view of the same data.
* **Accelerated rounds** as a first-class thing. Re-listing an unsold player is
  one button at a time; a "re-list everything unsold" sweep is a small addition.
