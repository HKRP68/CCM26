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

```
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
three minutes like every other paginated list. A manager with no `@username` is
rendered as a `tg://user?id=` mention, so every name in the list is tappable —
the list exists to be acted on.

Every edition of a card counts as owning the player, and the edition each owner
holds is named, because Base and Gold are not the same trade.

### In a DM

There is no group to scope to, so the DM view leads with global rarity and then
tallies the groups the bot has seen you in:

```
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
group costs a few hundred writes a day, not one per message. The throttle key is
only marked once the write actually lands, so a user who debuts *after* their
first message is picked up on their next one rather than being suppressed for
six hours.

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

## Menu scoping

Telegram caps a slash menu at 100 commands **per scope**, and the group scope
was already at 100. `/gstats` and `/owners` are both group-first, so room was
made by moving the two rules pages — `/chemhelp` and `/fantasyguide` — into
`PRIVATE_ONLY_COMMANDS`. They still run anywhere; they are simply not listed in
the group menu, and their parent commands (`/cmuchem`, `/fantasy`) now both
print a pointer to them.
