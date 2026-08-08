# Squad size, the squad-wide trait budget, and trait refresh timing

Design note for the 25 → 19 squad rebalance, the new squad-wide trait cap, and
the website-editable trait market schedule.

Reference implementation: `config.py` (the constants),
`services/trait_service.py` (counting and enforcement),
`services/roster_service.py` (`trim_roster_to_cap`),
`handlers/release.py` (`_do_release`, the release choke point),
`services/global_market.py` (refresh scheduling),
`migrate_squad_and_trait_limits.py` (the one-time downsizing).
Tests: `tests/test_roster_cap.py`, `tests/test_squad_trait_cap.py`,
`tests/test_squad_downsize_migration.py`, `tests/test_trait_refresh_schedule.py`.

---

## 1. Summary

Three changes, plus the data migration the first two force:

1. **The squad cap is 19, down from 25** (`config.MAX_ROSTER`), Career Player
   included.
2. **A squad-wide trait budget of 18** (`config.TRAIT_MAX_PER_SQUAD`) sits on
   top of the existing per-card rules. The **Career Player is exempt** and keeps
   its own 3 slots, so a fully kitted account holds **18 + 3 = 21** equipped
   traits.
3. **The trait market refreshes on its own interval + start hour**, editable
   from the admin website — "every 12 hours starting 12 AM" means 12 AM and
   12 PM IST, every day.

---

## 2. Squad cap: 19

### 2.1 Why one constant, and why that mattered here

`config.MAX_ROSTER` was always meant to be the single source of truth, but six
modules shadowed it with a literal `25` — two of them *inside* a function, so
they were invisible to a grep for module constants. Lowering the constant alone
would have left the free pack, the reward packs, the gspin reward, the undo
handler and two Mini App endpoints still admitting cards up to the old limit.

Every such site now reads the constant. `tests/test_roster_cap.py` scans the
source tree for `MAX_ROSTER = 25` and `roster_count >= 25` patterns so the
shadowing cannot come back.

Two consequences that are easy to miss:

- **`admin.py` used `< 25` as an `order_position` sentinel**, not as a cap. It
  decides whether a newly bought card appends at `max_position + 1` or falls
  back to 99. Left at 25 it would have kept handing out positions 20–25 that no
  longer exist in a 19-card squad.
- **The "Full Squad" achievement required 25 cards** and became permanently
  unachievable. Its threshold now tracks `MAX_ROSTER`. Its *key* deliberately
  stays `roster_25` — that string is stored in `user_achievements`, and renaming
  it would orphan every row already unlocked.

### 2.2 The Career Player counts

The cap includes the Career Player. That is the pre-existing behaviour
(`services/career_service.py` refuses to create one at a full roster) and it is
unchanged — a captain holds 18 ordinary cards plus their career card.

---

## 3. The squad-wide trait budget

### 3.1 Why

Trait limits used to be purely per roster slot — 3 per card, 2 of a category,
1 Elite. Nothing stopped a captain from equipping every card they owned, so the
only real constraint was gems. With a 25-card squad that was 75 trait slots to
fill, and "which cards get traits" was not a decision anyone had to make.

18 is exactly the number of ordinary cards (19 minus the Career Player). The
budget therefore stretches to **one trait on every ordinary card** — a squad
does not have to leave anyone bare. What it cannot do is stack: the per-card
limit is 3, so a squad has room for 54 traits' worth of slots against a budget
of 18. Giving a key card a second or third trait means another card gives one
up. That trade is the decision the cap exists to create, and it is why the
number sits at the card count rather than well above it.

### 3.2 Why the Career Player is exempt

The career card is the one card a captain builds rather than acquires. Making
its 3 slots compete with the squad budget would mean every career upgrade came
out of the outfield, which reads as a penalty for engaging with the mode. It
gets its own allowance instead.

Mechanically the exemption lives in one place —
`trait_service._squad_traits_query` filters out `Player.is_career` — and
everything else (`count_squad_traits`, `squad_trait_budget`, the enforcement
block, the migration trim) is built on it, so the rule cannot drift between
surfaces. The filter is NULL-safe (`is_career == False OR is_career IS NULL`)
because rows predating the career feature have NULL there, exactly as
`services/player_service.not_career` handles it.

### 3.3 Where it is enforced, and where it deliberately is not

- **`apply_trait_to_player`** — refused once the non-career squad holds 18.
  Checked *after* the per-card rules, so a captain hitting both limits gets the
  more specific message first.
- **`replace_trait_on_player`** — **no check**. A replace overwrites a
  `PlayerTrait` row in place, so the count is unchanged; a captain sitting
  exactly on the cap can still reshape their squad. There is a comment saying
  so at the call site, because this looks like an omission and isn't.
- **`upgrade_player_trait` / `upgrade_inventory_trait`** — unaffected, levels
  don't change the count.
- **The admin trait grant** (`/users/<id>/traits/grant`) grants to *inventory*,
  not equipped, so it is uncapped by construction.

### 3.4 Surfacing it

`squad_trait_budget()` returns `{used, max, full}` and feeds every surface:
the `/traits` list header, the `/traitapply` player picker (which also marks the
career card 🎖), and the Mini App player sheet via a `squad_trait_slots` key on
`/api/webapp/player/detail`. The Mini App labels the career card's slots
"outside the squad limit" rather than showing a counter, which would be
actively misleading there.

---

## 4. Downsizing existing accounts

Two entry points, one implementation — `services/squad_downsize_service.py`:

- **The website.** Maintenance → **Apply Squad & Trait Limits**. This is the
  normal way to run it.
- **`migrate_squad_and_trait_limits.py`.** A thin CLI over the same service, for
  a deploy-time sweep or when the panel isn't reachable.

They share the service precisely so the two can never disagree about what
"apply the caps" means.

### 4.1 Refunds are at BUY price, not sell price

Ordinary releases pay `get_sell_value` — deliberately well under the buy price,
because selling is meant to be a loss. A forced release is not a sale: the
captain did not choose to part with the card. So the migration refunds
`get_buy_value` for cards and `trait_buy_value(level, rarity)` for traits, the
full amount each cost to acquire.

This is why `_do_release` grew a `value_fn` parameter rather than the migration
deleting rows itself — that function is the one choke point that cancels pending
trades, nulls FK pointers on historical ones, returns equipped traits to
inventory, clears `captain_roster_id` and renumbers positions. Reimplementing
any of that in a migration script would have been a bug waiting to happen.

It also grew `record_undo=False`. A forced release must not leave a 60-second
`/cmuundo` record, or the captain could restore the cards and land straight back
over the cap that forced the release.

### 4.2 Selection rules

- **Cards** — lowest rating first; ties broken by most-recently-acquired, then
  highest id, so of two identical cards the older one is kept. The Career Player
  is filtered out of the candidate list entirely (not merely sorted last):
  `_do_release` refuses career cards, and one slipping into the batch would
  abort the whole release.
- **Traits** — lowest level first, then lowest rarity, then lowest id. The id
  tie-break is not decoration: level ties constantly, and without a stable
  final key the same input could produce different removals across runs.

### 4.3 Ordering matters

Cards are trimmed **before** traits. Releasing a card un-equips its traits into
inventory, which usually brings the squad under 18 on its own — so running the
trait pass first would destroy traits that the roster pass was about to hand
back intact. On a typical 25-card account with a trait on every card, the roster
pass alone is enough and nothing is refunded in gems.

### 4.4 The preview is the real thing, rolled back

`preview_downsize` does not estimate. It runs the actual downsizing and then
rolls it back, so the numbers it reports are exactly what an apply produces.
That matters because of §4.3: counting over-cap traits up front *overstates*
the gem refund, since releasing cards frees most of those traits into inventory
instead. The earlier hand-rolled `--dry-run` had that bug — it reported six
traits on an account where only one was really refunded.

This is also what makes the button's confirm step honest: the figure on screen
is the figure you get.

**One account per transaction, rolled back immediately** — not one transaction
across the whole scan. Two reasons:

- A failing account can't abort the run. It is recorded in `failed` and the
  walk continues, matching `run_downsize`. Otherwise a dry run could die on an
  account the real run would simply skip.
- No long-held write locks on `user_roster`, `player_traits` and `users`, which
  on a large database would block live gameplay for the length of the scan.

SAVEPOINTs are the textbook tool for the first point, and were tried. They do
not work here: under pysqlite a released savepoint **survived the outer
rollback**, so the preview silently persisted. The test suite runs on SQLite, so
that is not a theoretical concern. A plain rollback per account behaves the same
on both backends.

### 4.5 Operational shape

Only over-cap accounts are visited. `find_over_cap_user_ids` gets them with two
`GROUP BY … HAVING` queries rather than walking every user and asking each one,
which is a query per user on a table where the over-cap accounts are a small
minority.

Each user commits separately inside its own try/except, so one bad row skips
that user instead of aborting the run, and the accounts already fixed stay
fixed. A second run is a no-op — everyone is at or under both caps — so it is
safe to re-run after fixing whatever caused a skip. The website reports skipped
users and tells you to press Preview and Apply again.

Between users the session is expunged to bound memory on a large user table.
That detaches **every** object in the session, so `run_downsize` and
`preview_downsize` are only safe to hand a session the caller owns for the
duration — both entry points do. Re-query anything you still need afterwards.

Once an account's downsizing has committed, nothing downstream may mark it
failed. Totals accounting and the per-user log line sit outside the
transactional `try`, and the audit-log write on the website is wrapped
separately: a failure there warns that the log is missing, rather than
reporting a bare error for releases and refunds that already happened.

### 4.6 The Apply button is gated on Preview

Apply carries a token that only Preview issues, held in the Flask session and
cleared *before* the work starts. Three things follow:

- A bare POST — a bookmarked URL, a double submit, a re-sent form — cannot
  release anybody's cards, because it has no token.
- The token is single-use, so a double submit cannot apply twice even while the
  first request is still in flight.
- You cannot reach Apply without having seen the numbers first.

Every apply writes an `AdminLog` row with the totals, since this is the one
action that rewrites every squad on the service at once.

### 4.7 Affected captains are DMed

A captain who opens the bot to find six cards missing and their gem balance
changed deserves better than working it out themselves. So every account the
button actually changed gets a Telegram DM: the new caps, the Career Player
exemption, an itemised list of what was released and what each refunded, and an
explicit line that their **trait inventory was not touched** — which is true,
because only *equipped* traits are ever candidates.

Three properties are worth stating, because each is a deliberate choice:

- **Only committed work is announced.** The messages are collected through
  `run_downsize`'s `on_user_done`, which fires after that account's commit. A
  user whose downsizing was rolled back is never told it happened.
- **Messaging cannot undo a refund.** `run_downsize` runs the callback inside
  its post-commit reporting block, where an exception is logged rather than
  counted as a failed account.
- **One thread, paced.** `_tg_send_batch_async` walks the whole list in a single
  daemon thread with a 50 ms gap (~20/s, under Telegram's ~30/s ceiling). The
  per-message thread of `_tg_send_async` is fine for one Mini App notification
  and wrong for an action that can touch every account on the service. The
  admin's request returns immediately; delivery outlives it, so the flash
  reports how many were *queued*, not how many arrived.

`format_downsize_dm` lives in the service and takes a plain result dict, so the
wording is testable without a database and without Telegram.

The CLI does **not** DM anyone — a shell run is silent. If the players should
hear about it, use the button.

---

## 5. Trait market refresh timing

### 5.1 Why it moved off the player market's hour

Both shared markets used to reroll once daily at
`GameConfig.market_refresh_hour_ist` — one setting, two markets. Traits want to
turn over more often than player cards, and there was no way to say so.

### 5.2 Interval + start hour

Two new `GameConfig` columns, `trait_market_refresh_interval_hours` and
`trait_market_refresh_start_hour_ist`. Anchors are
`(start + k × interval) mod 24`, so 12 h from 12 AM is `[0, 12]` — midnight and
noon, on the same clock hours forever.

**Only divisors of 24 are offered** (`TRAIT_MARKET_REFRESH_INTERVALS`). An
interval like 5 h does not tile a day: the refresh times would drift round the
clock and "12 AM and 12 PM" would stop being true after the first day. A stored
value outside the list is snapped by `clamp_refresh_interval`, which breaks ties
toward the *shorter* interval so a mis-set value errs toward refreshing more
often rather than less.

### 5.3 The anchor search is relative to the last refresh

`_next_refresh_utc` searches anchors around `last_refresh_utc`, not around now.
That is what makes `_is_due` mean "has an anchor passed since we last ran?" — a
market that has not rerolled for a week reports the anchor it first missed and
fires immediately. Anchoring the search on *now* looks equivalent and quietly
breaks the stale case.

The player market calls the same function without an interval, so it keeps its
single daily anchor and is entirely unaffected.

### 5.4 Back-compat

`trait_market_refresh_start_hour_ist` is nullable. A database that has been
migrated but whose settings have not been re-saved has NULL there, and
`_trait_schedule` falls back to `market_refresh_hour_ist` — such an install
keeps exactly the refresh time it already had rather than silently jumping to
midnight.

### 5.5 The legacy per-user shop

`trait_service.refresh_shop` (the old per-user 5-slot shop, which `/traitshop`
no longer uses) hardcoded a 24-hour window. It now reads the configured
interval, so it cannot disagree with the setting if anything ever calls it
again.

---

## 6. Admin UI

`/markets` → **Market Settings**. The player market keeps its single
"daily refresh time" select; the trait market gets an interval select and a
start-hour select, with the resulting schedule rendered underneath
("🔁 Trait market currently refreshes at 12:00 AM, 12:00 PM IST"). The preview
is computed server-side by `get_trait_refresh_schedule`, from the same helpers
`ensure_trait_market_fresh` uses — so the page cannot show a schedule the bot
won't follow, and it needs no JavaScript.
