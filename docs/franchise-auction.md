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
| `/apurse [franchise]` | anyone | Every purse, max bid and squad count (and RTM cards left) — or one franchise's squad |
| `/artm yes\|no` | the holder's owner + co-owners | Answer an open Right To Match. One verb for both questions it asks |
| `/ainfo` | anyone | Where the auction stands, and one button per view below |
| `/asets` · `/anextset` · `/anextplayer` | anyone | Every set and its state, the next set's players, who is up next |
| `/asquad [franchise]` | anyone | Your own squad (or any franchise's) as a table, with purse and max bid |
| `/asoldlist` · `/aunsoldlist` | anyone | Everyone sold, set by set; the ⚡ Unsold / Accelerated set |

Admin: `/adminhelp` (the reference card, also `/auction`), `/anew`, `/abind`, `/astart`,
`/apause`, `/aresume`, `/anext`, `/aextend`, `/asold`, `/aunsold`,
`/aundobid`, `/awithdraw`, `/atimer`, `/asnipe`, `/agrant`, `/aco`,
`/apublish`, `/acancel`, plus retention's `/aretlock`, `/aretain`,
`/aunretain`, RTM's `/artmset`, `/artmcards`, `/artmforce`, `/artmundo`, the
accelerated round's `/aaccel`, `/aclone` for the next season, and the
expansion picks' `/apick`, `/apicks`, `/apickset`, `/apickskip` and
`/apickundo`, and the room features below: `/apool`, `/anextset <set>`,
`/asetorder`, `/aaccelmode`, `/aretainforce`, `/aoffers`, `/aretcancel`,
`/acall`, `/aremoveteam`, and — for bot admins only — `/aadminadd`,
`/aadminremove`, `/aadmins`.

`/bid`, `/artm`, `/aboard`, `/apurse` and the team views are **not** in the group slash menu. Both
player scopes sit exactly at Telegram's 100-command ceiling and `_clamped`
drops the tail rather than letting `setMyCommands` reject the whole call, so
publishing them would cost that many existing player commands their entry. It
is the same call `/dtrade` and `/dtrades` already made, and the room is told
about them where it matters instead: the `/auction` card, the board's own
footer, `/help`, and — for `/artm` — the prompt that asks the question, which
names the owner and carries buttons. The admin commands are in
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

### The franchise accepts

`/aretain` does not sign anybody. It posts an **offer** — player, franchise,
price, which slab it is — with ✅ Accept / ❌ Decline buttons, and only that
franchise's **owner or a co-owner** can press them. Not another owner, and
deliberately not a bot admin, for the reason `may_bid_for` gives: this spends
the franchise's money, and the button exists so that they chose to.

Accepting runs the ordinary `retain()` at the offered price, so every cap
below still refuses — and says why in the button's alert — at the moment of
acceptance, against the purse as it is then. An offer with no price takes the
ladder's slab **when it is accepted**. One offer waits per player — held by a partial unique index on pending
offers, not just a check, so two admins on the same tick cannot both create
one — and an offer whose card fails to post is withdrawn at once; `/aoffers`
lists them and `/aretcancel` withdraws one. They live in
`auction_retention_offers`, and nothing is announced until one is accepted —
the `retained` event is the announcement.

`/aretainforce` (and the setup page's retention form) is the old instant path,
kept as the admin's override for an owner who agreed in person.

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

## Right To Match

The franchise that held a player last season gets one chance to keep him at
whatever the room decides he is worth. **The rule is IPL 2025's, in full** —
not a straight match, but *intent → one final offer → decision*:

> Ashwin is on the block and RCB have bid ₹6 Cr. Rajasthan Royals, who held him
> last season, are asked first: do you want to use an RTM? If they say yes, RCB
> get **one more bid**. If RCB raise to ₹9 Cr, RR may then match at ₹9 Cr — the
> *final* number, not the one that triggered the window.

```text
/artmset 2                  two cards each, 30s a window, no premium
/artmset 2 45 2             …a 45s window, and ₹2 Cr on top of the final bid
/artmcards Mumbai 3         one franchise's own count
/artm yes | /artm no        the holder answers — both questions, one verb
/artmforce yes|no           an admin answers for them
/artmundo Ashwin            money and card both back, lot on the block
```

…or the **🪪 Right To Match** card on the setup page and the console's stage
buttons. Off by default: `rtm_enabled` is `False` and `rtm_per_team` is 0, so
an auction nobody configured never offers one.

### Three windows on one clock

```text
on_block ──(clock expires, a bid stands, RTM is available)──▶ rtm_offered
    stage = intent      the holder: exercise it at all?
        ├─ no / times out ──────────────────────────▶ sold to the bidder at ₹6 Cr
        └─ yes ─▶ stage = final_offer
                     the top bidder: one raise, or stand
                        └────────────▶ stage = decision
                     the holder: match the final number?
                        ├─ yes ─────────────────────▶ sold to the holder (`rtm`, a card spent)
                        └─ no / times out ──────────▶ sold to the bidder at the final number
```

**Every stage times out to the safe default** — decline, stand, decline — the
one that changes nothing about who was winning. An owner whose phone is in
their pocket cannot wedge an auction; that is the same call `pass_lot` makes
for a lot nobody bid on. `rtm_stage` says which window is open and
`deadline_at` carries whichever it is, so the sweeper reads the two together
or not at all.

**Anti-snipe does not apply.** Sniping is a contest between bidders; an RTM
window is one franchise answering one question, and extending it only lets
somebody stall. The lot's `extensions_used` budget is untouched.

### `rtm_offered` is a status, where `retained` was not

Retention rides on `sold` because every reader of `sold` wanted it. A lot under
RTM is the opposite: it is neither on the block nor sold, and the difference
matters to nearly every reader. So it gets a status — and then **one line does
almost all of the work**:

```python
def current_lot(session, season):
    ... AuctionLot.status.in_(LOT_LIVE)   # (on_block, rtm_offered)
```

Widening `current_lot` makes six functions that already guard on
`status != LOT_ON_BLOCK` refuse during an RTM window *for free*, each with a
message that was already right: `extend_timer`, `validate_bid`, `sell_lot`,
`pass_lot`, `undo_last_bid`, `resolve_expired`. Two more follow without being
told — `open_lot` will not open the next lot over an open window, and
`complete_if_done` will not finish the auction mid-RTM. Of the 33 places that
read `LOT_ON_BLOCK` or `current_lot()`, that left four needing code.

### The one rule that is deliberately suspended

`validate_bid` refuses the franchise that already holds the top bid — bidding
against yourself is not a tactic. A final offer is exactly that, so the guard
lifts, gated on **both** `rtm_stage == 'final_offer'` **and** the bidder being
the standing top bidder. A third franchise is refused for the ordinary reason.
The minimum increment still applies: a raise that is not a raise is not a
final offer.

This is also the one place the deadline stops being the whole story. Advancing
to `decision` writes a *new, later* `deadline_at`, so a final offer arriving a
moment late would still satisfy `deadline_at > now` and land as a raise against
a question already being answered. The claim carries the stage as well:

```python
.filter(AuctionLot.id == lot.id,
        AuctionLot.deadline_at > now,
        *([AuctionLot.rtm_stage == RTM_FINAL_OFFER] if final_offer else []))
```

The buttons carry it too — `au_rtm_{lot_id}_{stage}_{yes|no}` — so a stale
prompt still on somebody's screen cannot answer the *next* question.

### The card, the money and the price

The price is the **final** bid plus `rtm_extra_lakh`, which is 0 by default, so
what ships is the pure IPL rule. A match spends a card, sells the lot with
`acquisition = rtm`, sets `rtm_matched_by_id`, and writes a **`LEDGER_RTM`**
row rather than `LEDGER_PURCHASE` — so the ledger still says how a squad was
assembled, and `/apurse` shows cards left beside the purse.

The card and the debit move in the same statement-pair as the sale: a debit
that cannot go through takes the card back with it. **A holder who can afford
the ₹6 Cr but not the raised ₹9 Cr is told so at the decision prompt**, before
they can tap into a refusal, and the card is not spent — they never got to use
it. `undo_rtm` returns the card as well as the money, which is the whole point
of it being a separate operation: an undone match that quietly ate one leaves a
franchise silently poorer for an admin's slip.

Eligibility is checked once, at the moment the clock expires — a card in hand, a
previous franchise that is not already the top bidder, the purse, the same
reachability rule bidding and retention use, and the squad and overseas caps.
Fail any of it and the lot simply sells. The room hears *why* only when a card
existed and something else blocked it; otherwise it is noise.

---

## The accelerated round

At the end of a long auction a pile of players has gone unsold and the room
wants another go at them. `/aaccel` puts **every** unsold player back into the
queue at once, in the order they were first offered.

```text
/aaccel        who would come back, and what it would cost nobody
/aaccel go     do it
```

…or the console's ⚡ card, whose tick-boxes bring back a chosen few. Same
service call either way, so the two surfaces cannot drift.

**It also happens on its own.** When the queue runs dry with players unsold,
`complete_if_done` re-lists them once as the **⚡ Accelerated** set and the
auction carries on live, rather than finishing. `accelerated_done` makes it
once only — a manual `/aaccel go` counts as the round — and whoever the room
passes on twice is what the auto-fill below hands out. `/aaccelmode off` turns
the automatic round off for an auction (it is carried into a cloned season).

**They come back at the same base price.** A second chance at the same player
is not a discount: dropping the floor would quietly re-price every lot the room
had already judged, and an admin who actually wants that has the lot's own base
price on the setup page.

**A completed auction re-opens `paused`, never live.** The last lot resolving
is what finishes an auction, so "who is left?" is only answerable once it has —
which makes a completed season the normal place to call this from. Coming back
paused means nothing goes on the block until an admin presses Start: a clock
running in a room that has already drifted off sells a player to whoever still
has their phone out.

**One announcement, not forty.** The event log is the announcement queue, so a
row per player would be forty messages for the sweeper to post one at a time
into a room that just wants to get on with it.

A re-listed lot comes back clean — and `extensions_used` is the one that
matters. A standing bid cannot survive (`pass_lot` refuses while one stands),
but a lot bid up into the snipe window *spends* extensions, and if that bid is
then undone and the lot passed, the spend outlives it. Left alone, the second
outing would quietly run to a shorter clock than the first.

---

## Sets

A lot's `set_name` groups the pool — "Marquee", "85-90 OVR", "⚡ Accelerated".
The queue is still plain ascending `lot_no`; choosing which set comes next is a
**renumbering**. `bring_forward` gives the chosen lots the next numbers after
every number the season has used, in their own order, and the rest of the queue
follows them — so no two rows can meet on the `(season_id, lot_no)` unique
index mid-flush, and `next_queued` never has to learn what a set is.

```text
/apool 85-90 | Marquee      every base card rated 85–90, best first, as one set
/apool 70-79                the set is named "70-79 OVR"
/anextset Marquee           that set comes next (the lot on the block finishes first)
/anextset 88-92             every queued player rated 88–92 comes next
/asetorder Marquee, Bowlers the whole queue, set by set; unnamed sets keep their place
```

`list_sets` reads every set's state back — ✅ done, 🔨 live, ⏭ next, ⏳ queued —
for `/asets`, `/ainfo`, the 🗂 Sets card and the lot card, which names the set its
player came from. `/apool` takes base cards only unless told `| all`: two editions
of one cricketer in a pool is a squad with the same man twice.

### Set No

A set's **Set No** is the order it runs in: 1 goes to the block first, then 2.
It is `list_sets`'s 1-based position and **nothing stores it**. The queue's order
already lives in `lot_no`, so a column would be a second copy of the same fact,
free to drift from it — and read back this way two sets can never hold one
number, a reorder renumbers for free, and a set the website never created (the
⚡ Accelerated relist, a 🔁 Released set, `/apool`) is numbered with nothing to
backfill. Anything that wants the number asks `list_sets`; nothing counts sets
for itself.

```text
set_positions(session, season, [(name, no), …])   the numbers typed on a page
move_set(session, season, name, ±1)               one place earlier or later
```

Both refuse before moving anything: a number given twice, or one belonging to a
set that has already run, is named back with the set that holds it. The typed
number is a **rank**, so 1, 5, 9 orders the same as 1, 2, 3 — they are sorted and
the queue renumbered, then `list_sets` reads the real number back. Both resolve a
name **exactly** (`_set_exact`, not `_match_set`): a page echoes back names it
rendered, and a set that finished while it sat open must be refused rather than
matched on a substring to a different set.

`_requeue` numbers above the season's high-water mark so no two rows meet on the
`(season_id, lot_no)` unique index mid-flush — which costs a range per reorder,
and `lot_no` is on screen as the lot's `#`. So when **no lot has left the queue
yet** it compacts back to `1..N` in a second pass. The test is per-lot, not
`season.status`: retention sells lots while the season is still in setup.

Every event is announced to the room (`auction_scheduler.SILENT_KINDS` is empty,
and `start` does not skip what queued up before it), so the reorder calls take
`quiet=`. The **setup page is quiet** — it is where an admin nudges the order half
a dozen times, and those nudges would otherwise save themselves up and land in
the group the moment the auction starts. The **live console is not**: the room is
watching, and the order the remaining sets run in is news.

One wart, left alone deliberately: a set whose lots are *all* sold before the
auction starts (retention, expansion picks) has `queued == 0`, so `order()` files
it with the done sets and it is numbered ahead of the queue — a "Retained" set
reading as Set 1 before a ball is bowled.

### The two surfaces

`templates/_auction_sets_card.html` is one card rendered by **both** the setup
page and the live console, so a fix to either is a fix to both. It is one form
around the whole table — a `<form>` is not valid inside `<table>`/`<tr>`, and a
per-row form nested in an outer one is dropped by the browser, which would make ↑
silently submit "Save order" — and its row buttons name themselves and carry
their row's index as the value, the only way one click can say both *what* and
*which set*. Done and live rows render their number as plain text with no hidden
input, so they cannot be submitted at all. On the console it sits **outside
`#au-panel`**: that fragment is re-fetched every couple of seconds and would wipe
a half-typed Set No.

`/asets` is paged. A pool is routinely dozens of sets of dozens of players, so
the card never tries to print all of it: every set on the page is a `details`
collapsible (an expandable blockquote in the HTML twin) holding its first few
players, a button per set opens that set in full — itself paged — by **editing**
the message rather than posting another one, and the page buttons walk the sets.
Sets are addressed in callback data by `set_no`, never by name: names carry emoji,
spaces and commas, Telegram caps callback data at 64 bytes, and `list_sets` maps
the number back. A number that no longer exists is refused, because opening
whichever set now holds it is the one failure nobody can notice. `AR.edit` takes
`html_text` for this card — without it a chat where rich text is off gets no edit
at all, which is a button that does nothing.

---

## What the room sees

* **Every player is a new message** — his card, captioned with the lot, the
  set, the base price and the bid to type — and a **fresh board**, which is the
  message that gets **pinned**. `AuctionSeason.board_lot_id` is how the sweeper
  tells a board whose lot has moved on; it is replaced, not edited.
* Inside a lot the board is **edited** as before, and now carries the
  quick-bid buttons itself.
* **Bids are announced**, small: every bid inside one sweep is folded into one
  line (`💸 Mumbai ₹2.2 Cr → Chennai ₹2.4 Cr leads · Kohli`), and consecutive
  bid lines are at least `BID_MESSAGE_GAP` (4s) apart — a bid that lands inside
  the gap waits for the next tick instead of being dropped, and never holds up
  anything queued behind it. Telegram allows a group about twenty messages a
  minute; a line per two-second tick would be thirty.
* SOLD, UNSOLD and RETAINED are announced in richer HTML (a quoted result, the
  buyer's purse and squad after it); everything else still reads its stored
  `headline`.

## Removing a franchise

`/aremoveteam Delhi` previews; `/aremoveteam Delhi | confirm` (or the setup
page's Remove button, whatever the squad) does it:

* every player the side holds — bought, retained, matched, picked — goes back
  into the pool at the tail, as the set **🔁 Released – Delhi**, with no Right
  To Match attached (the side that held him is gone);
* its **whole opening purse** is shared equally among the franchises left, the
  odd lakh one each to the first few by sort order, as a `correction` row on
  each ledger — so every purse still equals its ledger;
* its bids, ledger, open retention offers and the franchise row are deleted
  **explicitly**, because SQLite only honours `ON DELETE CASCADE` with a pragma
  this project does not set.

The bids from the round that sold those players are **voided** and their bid
counts reset, so an undo in the new round cannot fall back to an old-round bid.
If the auction had already finished, releasing players re-opens it **paused**,
as `relist_all` does — left completed, they could never be sold, and a publish
would quietly leave them out of the league.

Refused while it holds the standing bid (undo it first, so the room sees who
dropped out), while a Right To Match is being asked of it, once published, and
for the last franchise standing.

## Short squads at the end

When the queue is done for good — and only once an accelerated round has run,
automatic or `/aaccel go`, so with `/aaccelmode off` and no manual round the
admin's choice to leave players unsold stands — `autofill_short_squads` runs
just before the auction is marked complete: round-robin, the smallest squad first, each
franchise under `min_squad_size` takes the best-rated unsold player that fits —
the squad cap, the overseas cap, owed roles first when role minimums are set,
and never a second card of a cricketer it already has (a published league keys
its players by name, so two Kohlis on one squad would collapse into one row).
It is **free**: `acquisition = autofill`, `sold_price_lakh = 0`, and a
zero-amount `autofill` ledger row records the signing without moving the purse.
One `autofill` event lists who went where. `undo_sale` refuses an auto-filled
player — there was no sale.

## Auction admins

A bot admin appoints them — `/aadminadd` with a Telegram id, an `@username`
the bot knows, or as a reply — into `auction_admins`. `is_auction_admin` is
`is_admin` **or** a row there, and the auction handlers' `_require_admin` is
the only gate that reads it: an auction admin runs every auction admin command
and nothing else in the bot, because every other admin check still reads
`services.admin_ids.is_admin`. They cannot appoint each other, and the website
console stays behind the web login. `/adminhelp` shows each of them the card;
only bot admins see the appointing section.

`/acall [message]` tags every owner and co-owner, grouped by franchise, as
HTML mentions (the form Telegram notifies from), split under the 4096-character
limit.

---

## The next season

A finished season **is** the saved configuration — so there is no template
table, just `/aclone`:

```text
/aclone Season 3     every rule, every franchise, every owner
```

…or **🌱 Next season** on the setup page, which lands you on the new auction
rather than the one you just left.

What carries: the whole clock, the purse, both price ladders, the squad and
overseas caps, every retention rule, every RTM rule — and the field, with its
owners and co-owners, which is the tedious part. Purses go back to the opening
purse (with their opening ledger row, so `ledger_total == purse_remaining_lakh`
from the first moment), squads and cards start again.

**The one thing it wires up by itself** is `previous_league_id`, pointed at the
league the source published. That is the most forgettable step in setting a
season up and it does not fail loudly when missed — retention and Right To
Match simply find nobody. An unpublished source has nothing to point at, and
both surfaces say so rather than leaving it quiet.

`SEASON_RULE_FIELDS` names the rules explicitly rather than copying every
column, because the interesting question is which columns are *not* there —
status, the bound chat, the published league and the board cursors are state
from a **run**, and a clone is not a run. A test checks that list against the
model, so a rule column added later fails rather than being silently left on
its default.

**What is not carried**, and why:

* **The pool.** §14's flow is RETAIN, then BUILD POOL. By the end of a season
  its players are on squads and the catalogue has moved on, so the next season
  builds fresh — and the pool builder already skips retained players.
* **The group.** A *running* auction keeps its chat: its pinned board is in
  there and the room is bidding into it. A **finished** one hands it over on
  the next `/abind`, releasing its own binding as it goes, because
  `season_for_chat` resolves one chat to one auction and two would make every
  command in the group ambiguous. (That used to be refused outright, with a
  message reading "cancel or finish it first" when finishing it was exactly
  what had happened — so the group was blocked forever.)

### Renaming a franchise between seasons

`previous_squad_map` matched last season's **team name** to this season's
**franchise name**, and its own docstring called that "the only link there is".
It was: rebrand a side between seasons and every Right To Match and every
retention candidate vanished, with no error — just an empty map.

A cloned franchise carries `carried_from_id`, which is a real link. The lookup
now resolves last season's team name to last season's *franchise* (safe, because
`publish_to_league` names teams after franchises in that same season) and then
hops to this season's through the stored id. The name match stays as the
fallback, because a season linked to a league by hand has nothing else.

---

## Expansion teams

A side joining a league that already exists has no previous squad, so it
retains nobody — and would walk into the auction with an empty list while
everyone else arrives holding three players. The IPL solved this in 2022:
Gujarat and Lucknow each took three players out of the pool **nobody had
retained**, before the mega auction opened.

### Getting there from a finished tournament

`clone_season` needs a previous *auction*. What actually finishes is a
**tournament**, and the league it played on may never have come from an
auction at all. So a completed Challenge League tournament's admin page offers
**🏆 Next season**, which calls `season_from_league`: a franchise per team, and
`previous_league_id` wired to that league — the step that decides who holds a
Right To Match on whom, and the one easiest to forget by hand.

Only a **Challenge League** tournament, and only a **completed** one. A Lets
Play tournament's teams are Telegram users playing their own rosters, so there
is no league of squads to auction. A second press links to the season that
already follows that league rather than quietly building a rival field.

The new franchises arrive **ownerless** — a `ChallengeTeam` has no owner to
carry. That is not a hole: `start()` already refuses an ownerless franchise by
name, so it cannot be missed quietly.

### The picks

```text
/apickset 3                       three picks to every new side
/apickset Lucknow | 2             one side's own number
/apick Gujarat | Hardik Pandya    at the ladder price
/apick Gujarat | Hardik | 15      at yours
/apicks                           the order, whose turn, what is taken
/apickskip                        pass — the turn is spent either way
/apickundo Hardik Pandya          money and pick both back
```

…or the **🆕 Expansion picks** card on the setup page, which shares the
retention search box — one resolver, so a pick and a retention can never
disagree about which card they mean.

**Who is new is derived, not ticked.** A side is an expansion side exactly
when the league this season follows records nobody as theirs — the same
question `previous_squad_map` already answers for retention and RTM. One source
of truth beats a checkbox somebody has to remember, and a first season (where
every side is new) correctly gets none.

**The order snakes** — 1, 2, 2, 1, 1, 2 — so no seat takes the best player
available every round. It is derived from `draft_picks_used` and `sort_order`,
not stored in a cursor: a cursor is one more thing to fall out of step with the
picks it describes. A skip spends the turn, which is the point — a side that
does not want its pick must not be able to stall the order by never taking it.

**Retention closes first.** A pick comes out of the players nobody kept, so a
season that allows retentions refuses picks until retention is locked, naming
`/aretlock on`. One switch, not a second window with its own deadline.

### A pick is a retention with a different eligibility rule

That is the whole design. Both acquire a player before the auction, at a price,
out of the purse. They differ in three places:

| | retention | expansion pick |
|---|---|---|
| who is eligible | anyone (yours is *warned*, not required) | anyone **not already signed** this season |
| counts against | `retained_count` / `max_retentions` | `draft_picks_used` / `draft_picks_total` |
| called | `ACQ_RETAINED`, `LEDGER_RETENTION` | `ACQ_DRAFTED`, `LEDGER_DRAFT` |

Everything else — the squad cap, the lot claim, the overseas cap, the purse,
the reachability rule, the conditional debit, the ledger row and the
announcement — is identical, and lives once in `_sign_before_auction`. `retain`
is a thin caller over it with its signature unchanged.

One reader had to be told about the new kind, and it is the one that bit this
feature before: **`pool_counts`** keeps picks out of the auction figures and
counts them apart from retentions. Nobody bid and no clock ran, so folding them
in would have the board announce lots resolved before the first one opened —
exactly the bug retention shipped in phase 2a. A pick also does **not** rewrite
`previous_franchise_id`: a retention records the holder because it *is* one,
but a pick takes somebody nobody kept, so the real record survives.

### The pool builder stamps who held whom

`link_previous_season` can only stamp lots that already exist, and the natural
order — the one `/aclone` and both pages tell an admin to work in — is to
follow a league first and build the pool after. Every lot came out unstamped
and Right To Match found nobody, in silence. `add_players_to_pool` now stamps
as it creates, so the order cannot matter.

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

**No per-second countdown, and no reply to a successful bid.** The bidder's
own acknowledgement is still a **reaction on their message**, and a *refused*
bid always answers, saying what would have worked instead.

* **New messages** go out for discrete events — a lot opening (the player's
  card, then a fresh pinned board), a sale, a pass, a pause — and **one short
  line per burst of bids**, at most one every `BID_MESSAGE_GAP` seconds (see
  *What the room sees*). Roughly five messages a lot.
* **Everything else is an edit of the lot's pinned board.** `/bid` does not
  edit it; the sweeper notices `lot.bid_count != season.board_rendered_bid_count`
  and performs **at most one edit per two-second tick**, so ten bids inside one
  tick cost one edit.

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
                    retention rules and ladder, the RTM rules, the league and
                    the season it follows, the pinned board and the
                    announcement cursor
AuctionFranchise    a franchise: name, city, logo, owner + co-owners, purse,
                    its Right To Match cards (total and used), and the
                    franchise it continues from last season
AuctionLot          one player — the lot that goes on the block AND its result,
                    retained and matched players included (``acquisition``
                    tells the three apart), plus the open RTM stage
AuctionBid          every bid, losing and voided ones included
AuctionLedgerEntry  every movement of a purse, signed, with the balance after
AuctionEvent        the permanent log, and the queue the group is announced from
AuctionRetentionOffer  a retention waiting on the franchise's Accept
AuctionAdmin        someone a bot admin trusted to run auctions
```

Six new tables, so `create_all` builds them. Everything the first release
shipped needed **no `_try_add` lines**; retention and Right To Match were added
afterwards and theirs sit beside each other in `_migrate_add_columns`, all
nullable or defaulted. All six tables are in `database.init_db`'s explicit
import list.

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
| `services/auction_scheduler.py` | The two-second sweeper, the pinned board (fresh per lot), the lot card, the bid-line coalescing, the hardened edit, and the event drain |
| `services/auction_rich.py` | Every auction surface as a Bot API 10.1 rich message beside its HTML twin — board, squad, purses, sets, sold/unsold, `/ainfo`, `/adminhelp` — plus the richer announcements and the shared keyboards |
| `services/player_query.py` | The one master-player filter, and the one `details_json` |
| `handlers/auction.py` | `/bid`, `/artm` and every other command, plus the `au_bid_` and `au_rtm_` buttons |
| `models.py` | The six tables |
| `admin.py` | `/auctions`, `/auctions/<id>`, `/auctions/<id>/console` and its polled panel |
| `templates/admin_auctions.html`, `admin_auction_detail.html`, `admin_auction_console.html`, `_auction_console_panel.html` | The pages |
| `static/css/admin_auction.css` | Their stylesheet, via `{% block head_extra %}` |
| `migrate_auction_purse_reconcile.py` | Re-sums every ledger, `--dry-run` first; the work is the service's, shared with the admin button |
| `tests/test_franchise_auction.py` | The pool, base prices, the ledger, reachability, the overseas cap, and publishing |
| `tests/test_auction_bidding.py` | The lifecycle, bidding, two-session concurrency, the clock, anti-snipe, undo, permissions, the commands, the board, and the accelerated round |
| `tests/test_auction_retention.py` | The ladder, the money, every cap, the window, what retention does to the pool and the board, publishing a retained player, and the commands |
| `tests/test_auction_rtm.py` | The proposal's own Ashwin example end to end, every eligibility gate, all three timeouts, the self-raise suspension from both sides, the card, `undo_rtm`, the two-session races, and the autoflush shapes |
| `tests/test_auction_expansion.py` | Starting a season from a league, who counts as an expansion side, the snake order as a sequence, every cap a pick obeys, and the rule that a player somebody kept cannot be picked |
| `tests/test_auction_features.py` | Sets and the queue order, removing a franchise and its purse split, the automatic accelerated round, the free auto-fill and its caps, retention offers and who may answer them, auction admins, `/acall`, every team view, the rich builders, and what the sweeper sends per lot and per burst of bids |
| `tests/test_auction_season.py` | Cloning: every rule carried (and a guard against the rule list falling behind the model), the field and its owners, the purses and their ledger, what is deliberately left behind, the group handover, and the rename that used to lose every holder |

---

## Not built (yet)

* **A Mini App auction board.** The board is a Telegram message today; the
  `/webapp` plumbing would serve a live web view of the same data.

