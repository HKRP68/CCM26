# `/owners` and `/gstats`

Two commands that answer questions about a *cricketer* rather than about your
own copy of him:

* **`/owners <name>`** — who is holding this card, this group first. The
  question every trade starts with.
* **`/gstats <name>`** — his career record summed over every owner and every
  match type. `/stats` stays personal.

Aliases: `/ownedby`, `/whoowns`; `/globalstats`.

## `/owners`

### In a group

```text
👥 Who owns Rohit Sharma 🇮🇳
⭐ 92 OVR · Batsman

🏠 Cric Masters Fan Club
Owned by 19 of 46 known members (41%)

1. @sam — Gold ⭐94 · 3mo
2. Ravi — Base ⭐92 · 12d
…

🌍 Globally: 412 owners — 1 in every 13 managers
🎴 Editions: Base 380 · Gold 32

🤝 Offer a swap with /trade @user
```

Ten owners a page, `⬅️ Prev` / `Next ➡️` beyond that, buttons expiring after
three minutes like every other paginated list.

**Names are printed, not tagged.** `services/display_name.manager_name` renders
a handle as plain `sam` — no `@`, and no `tg://user` link for managers without
one. A card owned by thirty people used to fire thirty notifications every time
somebody idly checked who had it, which made the command something groups asked
to have turned off. Anything that genuinely needs to ping somebody (a match
invite, a trade offer) builds its own mention and is explicit about it.

Every edition of a card counts as owning the player, and the edition each owner
holds is named, because Base and Gold are not the same trade.

### In a DM

There is no group to scope to, so the DM view leads with global rarity and then
tallies the groups the bot has seen you in:

```text
🌍 Globally: 412 owners — 1 in every 13 managers
🎴 Editions: Base 380 · Gold 32

🏠 Your groups
• Cric Masters Fan Club — 19 owners
• CMU Traders — 3 owners
```

It never names members of another group — a tally is public information about a
group you are in, a member list is not something to hand out in a DM.

## Where "members of this group" comes from

Telegram gives a bot **no way to list a group's members**. There is no API for
it, so the bot has to learn membership from what it does see:

`services/chat_tracker.record_chat_member`, called from the same `group=-3`
middleware as `record_chat`, writes a `chat_members` row (`chat_id`, `user_id`,
`is_active`) the first time a **debuted** user is seen in a group. Undebuted
users own nothing, so they are skipped.

| signal | effect |
|---|---|
| any update from a group | upsert the sender, throttled |
| `new_chat_members` | record immediately, no throttle |
| `left_chat_member` | flip `is_active` off |

**Throttling.** One write per member per chat per `MEMBER_THROTTLE_SECONDS`
(6 hours), tracked in an in-memory dict — deliberately far coarser than the
10-minute `bot_chats` throttle, because membership barely changes. A 200-strong
group costs a few hundred writes a day, not one per message.

**Off the event loop.** The middleware calls `record_chat_member_async`, which
splits the work in two: `_plan_member_write` decides what to write (attribute
reads and one dict lookup, so the overwhelming majority of updates return
immediately), and only when there *is* something to write does
`_write_member_plan` run — in a worker thread, so the database round trip can't
stall every update queued behind it.

Planning **reserves** the sender's throttle slot before returning, which is what
makes the off-loop write safe: two messages arriving back-to-back cannot both
queue a write for the same member and race on the unique `(chat_id, user_id)`
index. `_release_throttle` hands the slot back when the write turns out to be a
no-op or fails, so a user who debuts *after* their first message is picked up on
their next one rather than being suppressed for six hours.

**This is best effort, and the wording says so.** A member who has not spoken
since the bot joined is invisible to us, which is why the group line always
reads "of N *known* members" and never claims to be the group's real size.
With Telegram's group privacy mode on, the bot only receives commands and
replies aimed at it, so "seen" narrows to "has used the bot here" — which is
still exactly the population that can own a card and trade it.

## `/gstats`

`player_game_stats` holds one row per (owner, card) and is written by every
match mode there is — `/playmatch`, `/letsplay`, `/wpm`, `/cm`, `/vsbot`,
Challenge League, tours, super overs. Summing a player's rows is therefore his
record across the whole game, in all match types, which is exactly what the
feedback asked for.

`services/global_stats_service.py` does the aggregate in one grouped query, plus
two small ordered lookups for the records:

* **Counting columns are summed** — innings, runs, wickets, 50s, 100s, POTM…
* **Records are the best of the bests** — highest score is the best individual
  innings anyone played (a not-out wins the tie, so `99*` never prints as `99`);
  best bowling is the most wickets, then the fewest runs at that haul.
* **Rates are recomputed from the sums** — `runs / times_out`, never the mean of
  the owners' averages, which would weight a manager with two innings the same
  as one with two hundred.

The card also names the managers with the most runs and most wickets using him
(a natural person to trade with), and splits the totals per edition when the
card has variants.

### The edition browser

A cricketer with variants is several cards, and one merged block hid that. The
`/gstats` message is a small browser instead:

```text
🌍 GLOBAL STATS — Virat Kohli 🇮🇳
⭐ 93 OVR · Batsman
All match types · all editions · 412 owners with a record
Edition 1/3

  ◀ Prev      1/3      Next ▶
          👥 Owners
           ❌ Close
```

* **Page 1 is the combined record** — every edition summed, which is what
  `/gstats` has always printed. Pages 2..N are one edition each, and the
  per-edition breakdown only appears on page 1, where it isn't a restatement of
  the block directly above it.
* **Paging swaps the card art too**, via `edit_message_media`, so the Gold page
  shows the Gold card. A cached Telegram `file_id` makes that free; a failed
  media edit falls back to editing the caption alone rather than costing the
  player the page they asked for.
* **`👥 Owners` flips the same message** to the `/owners` answer for whatever
  edition is open (`handlers.owners.render_owners_summary`), with `◀ Back to
  stats` returning to the page you left. Nothing new is posted.
* **Only the manager who ran the command can drive it.** The owner's Telegram
  id is baked into every `callback_data`, and a stranger's tap gets "Not your
  stats — run /gstats yourself!". Buttons expire after five minutes.

A card with a single edition has one page and no navigation — just the stats and
the two buttons.

## Stat commands answer in DM

`/gstats`, `/stats`, `/statscl`, `/sbo`, `/recentmatches` and every trait
command answer in a DM only. In a group they post one notice with a deep-link
button and nothing else:

```text
📩 Global player stats now answers in DM
/gstats was filling groups with stat walls, so it runs one-to-one with me
instead.

Tap below — I'll run /gstats Virat Kohli there for you.

            [ 📩 Open in DM ]
```

`services/dm_only.py` owns the whole mechanism:

* **`DM_ONLY_COMMANDS`** is the single list. `bot.py` wraps each handler in
  `dm_only(name, handler)` at registration — one readable list rather than a
  guard pasted into a dozen handler modules — and folds the same names into
  `DM_ONLY_MENU_COMMANDS` so the group slash menu never offers a command that
  only redirects when tapped.
* **The link replays the command.** The payload is `cmd_<command>` plus the
  arguments as base64url (`build_start_payload`), which keeps it inside
  Telegram's 64-character `/start` limit and its `A-Za-z0-9_-` charset.
  `bot._replay_dm_only_command` decodes it, puts the arguments back into
  `context.args` and dispatches — so the DM opens already showing Kohli's card
  rather than a usage line. An over-long name degrades to the bare command
  instead of producing a link Telegram refuses. Only names in
  `DM_ONLY_COMMANDS` are dispatchable: the payload is user-supplied and must
  not be able to name an arbitrary handler.
* **The notice can't become the new spam.** It is throttled per
  (chat, user, command) for a minute and deletes itself after one, so holding
  down `/stats` posts one notice, not twelve.

`/tradetrait` and `/owners` deliberately stay in the group: a trade needs both
managers in the same room, and naming the people around you is the whole point
of `/owners`.

## Admin registration

Both commands are seeded into the `bot_commands` catalog (`database.py`,
idempotent) under the `utility` category with no cooldown and no reward, so they
appear in the website's command manager and can be switched off there. Each
handler checks `is_command_enabled` first and fails open when there is no row,
matching `/claim`, `/daily`, `/sim` and the rest of the catalog.

## Menu scoping

Telegram caps a slash menu at 100 commands **per scope**, and the group scope
was already at 100. `/gstats` and `/owners` are both group-first, so room was
made by moving the two rules pages — `/chemhelp` and `/fantasyguide` — into
`PRIVATE_ONLY_COMMANDS`. They still run anywhere; they are simply not listed in
the group menu, and their parent commands (`/cmuchem`, `/fantasy`) now both
print a pointer to them.

Moving the stat readouts to DM took a further twelve entries out of the group
menu, so that scope now has real headroom. The two lists stay separate on
purpose: `PRIVATE_ONLY_COMMANDS` is only about what a menu advertises, while
`DM_ONLY_MENU_COMMANDS` mirrors handlers that genuinely behave differently in a
group. `tests/test_bot_menu_commands.py` pins both directions — no DM-only
command in a group menu, and every one of them still offered in DMs.
