# Tournament Draft

A **draft** is how a Challenge League gets its squads. Until now an admin typed
every player into every team by hand on the Challenge Data page; a draft lets the
team owners do it themselves, live, pick by pick, in a Telegram group — from a
player pool and a pick order the admin uploads as a spreadsheet.

It is deliberately *not* a second competition engine. The draft ends by writing
its squads into a real `ChallengeLeague`, at which point every downstream thing —
the `/cipl` over-by-over match, the Playing XI picker, the points table, NRR, the
playoff bracket — is code that already existed and neither knows nor cares that a
draft produced the teams.

---

## Running one

### Setting it up (the website)

**Admin → Tournament Panel → 🎯 Player Drafts → Create a draft**, then open it.

**Player pool.** Upload an `.xlsx` or `.csv`, or paste the rows. Columns are
matched **by header name**, so their order does not matter:

```text
name, rating, tier, icon_eligible, gender, indian_status, category, country,
bat_hand, bowl_hand, bowl_style, bat_rating, bowl_rating
```

* `tier` must be on the draft's ladder (default `Platinum, Gold, Silver, Bronze`).
  Anything else is reported as a skipped row rather than quietly becoming a fifth tier.
* `country` decides **home or overseas**: a player is home when their country
  is the draft's `home_country` (settings, or `/dhome`), overseas otherwise.
  Spellings fold — `India`, `IND`, `Ind.` and `Indian` are one country.
* `indian_status` is only a *fallback*, for a row whose country is missing or
  unreadable (`Unknown`, blank). It accepts `Overseas` / `Domestic`, a country
  name, or `1` / `0`. A row with neither is counted as home.
  Sheets that carry both are fine: the country column wins, which is what keeps
  an India-centric sheet correct when it is re-used for an England draft.
* `icon_eligible` accepts `1` / `0` / `yes` / `no`.
* `category` is folded onto the engine's four roles — `wk`, `keeper`,
  `all rounder`, `bat`, `bowl` all land where you'd expect.
* A pool name that matches a card in the master catalogue is **linked** to it,
  which is what lets the bot post that card's image when the player is picked.
  The page reports how many did not match; those picks post as text.

Re-uploading is safe: a player already in the pool is updated in place, so a
corrected sheet can simply be uploaded again.

**Draft order.** Same deal — `.xlsx`, `.csv` or pasted:

```text
Round No, Pick Number, Tier, Team Name, Owner Name, Owner Tag ID
```

Teams are created from this sheet as they first appear, so it is the only file
that needs preparing: the franchise, its owner and the owner's Telegram id travel
with the picks. The running order is rebuilt from Round No and Pick Number, so
the rows can be in any order in the file.

**Teams & owners.** Add co-owners (extra Telegram ids allowed to pick) and upload
a team logo.

### Running it (the group)

```text
/dbind 3          ← in the group the draft should live in
/dtimer 15
/dstart
```

| Command | Who | What |
| --- | --- | --- |
| `/pick <player>` (`/pk`) | owner + co-owners | Make the pick that is on the clock |
| `/dboard` | anyone | The live board — who's up, time left, recent picks, plus buttons for the order, the remaining pool and the field (the buttons are [yours alone](#whose-buttons-are-whose)) |
| `/dsearch [filters]` | anyone | Browse the pool — 🟢 available / 🔴 taken, with filter buttons ([yours alone](#whose-buttons-are-whose)) |
| `/dsquad [team]` | anyone | A squad by tier, with slot progress and the overseas count |
| `/dqueue <player>` | owner + co-owners | Your wishlist. If your clock runs out the bot picks from it first |
| `/dtrade <team>` | owner + co-owners | **After the draft:** swap players with another franchise — [any player for any player](#trading-after-the-draft--dtrade) |
| `/dtrades` | anyone | Every squad change since the draft — trades and admin edits |

Admin: `/dadmin` (the reference card), `/dnew`, `/dbind`, `/dstart`, `/dpause`,
`/dresume`, `/dtimer`, `/dhome`, `/dpin`, `/dco`, `/dskip`, `/dundo`,
`/dcancel`, `/dpublish`, `/dtradelock`, and
[`/dadd` / `/ddrop`](#editing-a-squad-by-hand--dadd-and-ddrop).

`/dtrade` and `/dtrades` are **not** in the group slash menu: that list sits
exactly at Telegram's 100-command ceiling and publishing them would push two
existing player commands off the end. The draft group is told about them where
it matters instead — the "draft complete" announcement, the footer of
`/dsquad`, `/dadmin`, and the completed-trade card's own pointer to `/dtrades`.

Owner only: `/dautopick` — see *Granting a pick* below.

### Finishing

`/dpublish` (or the button on the draft page) writes the squads into a Challenge
League. Publishing again re-syncs the same league rather than creating a second
one, so a correction made with `/dundo` can just be republished.

### Trading after the draft — `/dtrade`

The last slot is filled and somebody is looking at three keepers and no death
bowler. `/dtrade` is how they fix it, and the point of it is a rule it **does
not** have.

```text
/dtrade Chennai      open a trade with that franchise
/dtrades             every trade that has been done
/dtradecancel        call off your team's open offer
/dtradelock on|off   admin: close or reopen the window
```

The flow is `/trade`'s — one message in the group, each side ticks its players
in turn, both owners confirm — and the buttons work the same way. What is
different is everything underneath.

**There is no rating rule.** `/trade` insists the two cards have the *exact
same OVR*, because it moves cards between two collections and neither captain
may gain value. A franchise is not a collection: the whole reason to trade is
that one squad has a spare keeper and the other has a spare quick, and "the
numbers must match" makes that trade impossible. A 97 for a 74 is a legal
`/dtrade`.

What replaces the rating rule is **daylight**, not a cheaper restriction. The
offer card prints both sides' OVR totals and names the side the deal favours
before either owner taps:

```text
4-for-4 · 97 OVR ⇄ 74 OVR
📈 Chennai Kings gains +23 OVR — heavily in their favour.
   There is no rating rule here: if both owners tap, it stands.
```

…and the whole thing lands in `/dtrades` permanently. A lopsided trade is
allowed; a *quiet* one is not.

**What still holds.** "Any player for any player" is only a good rule if the
squad it produces is still a squad the draft could have built, so every rule the
draft enforced on every pick is re-checked against the squad the trade *would*
produce — on both sides, before anybody moves:

* **The counts match.** A squad's size is its slot count in the order sheet, so
  a trade is N-for-N. Two-for-two and three-for-three are ordinary trades — one
  thing `/trade` can never do — but two-for-one would leave a franchise a player
  short of a legal XI.
* **The tier quota holds.** A slot takes its own tier or below, which is the
  rule that caps a team's Platinum count at its number of Platinum slots; a
  trade must not be the way around it. The check is *Hall's condition on the
  ladder*: walking from the top, the best **k** tiers of players must fit in the
  best **k** tiers of slots. That is also what keeps it fair in the other
  direction — a Platinum slot spent on a Silver player is still a Platinum slot,
  so the team that picked below its ceiling can trade back up into it.
* **The overseas cap holds**, and **role minimums hold**.
* **Never worse than it already was.** A squad that is already short — a slot the
  clock passed, a minimum an admin set late — may still trade; it just may not
  make the shortfall deeper. Every check compares the after against the before
  rather than against perfection, because an absolute check would freeze exactly
  the franchises that most need the trade window out of it.

**Who may trade:** the Owner Tag ID and co-owners, the same people `/pick`
lets act for a team. One open offer per team at a time, and an offer expires
after 30 minutes.

**An offer is a database row, not a dict in memory.** `/trade` keeps its
in-flight state in `context.bot_data` and loses every open trade to a redeploy.
A draft's trade window is open for days on a host that restarts often, so a
half-built `/dtrade` is a `DraftTrade` row: the buttons still work afterwards,
and a co-owner can pick up where the owner left off. It is the same call the
[pick clock](#the-clock) makes.

**A published draft follows the trade.** If `/dpublish` has already run, the
matching `ChallengePlayer` rows move between `ChallengeTeam`s as part of the
swap — otherwise `/dsquad` and the squads people actually play with would
silently disagree. Republishing could not fix that on its own: `publish_to_league`
adds and updates, so the player would arrive at the new team and *stay at the
old one as well*. Admins close the window with `/dtradelock on` before a match
day, so a team sheet cannot change under a live fixture.

**Pick rows are never rewritten**, by a trade or by an admin edit. *"R1 P3 was
Mumbai's Platinum slot, and Mumbai spent it on Bumrah"* stays true however many
times Bumrah changes hands afterwards —
and it is also where `tier_slots` reads a team's quota from, which has to keep
describing the order sheet rather than the current squad. The consequence is
that `/dundo` refuses to reach *through* a trade: undoing a pick whose player
has since moved would take him off a squad that never made it, so it says so and
names the team holding him. It is also why `/dtrades` exists: with the pick rows
frozen, that log is the only record of what happened to a squad after the draft.

### Browsing the pool — `/dsearch`

`/dboard → 📦 Available` lists the best of what is left. `/dsearch` answers the
other question, the one owners ask all evening: *is he still there, and if not,
who took him.*

```text
/dsearch                     the whole pool, and what is left per tier
/dsearch gold bowler         a tier and a role
/dsearch platinum available  only what can still be picked
/dsearch overseas wk         only the overseas keepers
/dsearch kohli               one player — his card, and who holds him
/dsearch australia           a country, because that is a question too
```

The words go in **any order** and anything the parser doesn't recognise is
treated as the name to look for — an owner three seconds before their pick types
what they mean, not a query language. Tiers match on a three-letter prefix
(`plat`), roles on the usual spellings (`wk`, `keeper`, `ar`, `bat`).

Every row carries **🟢 available** or **🔴 taken**, the tier badge, the rating,
the player's **own country flag** (a column of ✈️ says only "not from here";
🇦🇫 🇦🇺 🏴󠁧󠁢󠁥󠁮󠁧󠁿 says who they are, which is what somebody watching the overseas cap
is reading for) and, for a player who has gone, the team that holds him.
Available players sort first, then by tier and rating.

The buttons under it re-filter and page **in place** — tier, role, 🟢/🔴, home
vs. overseas, and ◀️ ▶️ when the list is longer than a page — so nobody retypes
the command to turn a page. They belong to **whoever ran the command**, not to
the room (see [Whose buttons are whose](#whose-buttons-are-whose) below): a
browser that re-filters under you because somebody else pressed a tier is worse
than sending `/dsearch` again, and the command is free. The filters and the
owner both ride in the callback data, so the message keeps working hours
later. A search that matches exactly one name skips the list and shows that
player's card instead, including whether he fits the slot currently on the
clock.

### The pinned pick

Every pick — typed, auto-picked by the clock, `/dskip`, `/dautopick`, or a
passed slot — is **pinned** in the draft group, and the previous pick's pin is
dropped. A draft runs for hours and scrolls fast; the pin is how somebody
arriving late sees where it is without reading back through the room. Exactly
one message is pinned at a time, so the pin always answers *what just happened*.

Pinning is silent (no ping per pick) and needs the bot to have the group's
**Pin Messages** right — without it the pick still lands and a warning is
logged, because a draft must never stop over a pin. `/dundo` and `/dcancel`
drop the pin, and `/dpin off` turns the whole thing off for a group that would
rather keep its own pin (`/dpin` on its own reports which it is).

---

## The rules the bot enforces

**A slot's tier is a ceiling, not a requirement.** A Platinum slot accepts
Platinum, Gold, Silver or Bronze. Picking below the slot's tier is allowed and
simply spends the slot — no refund, no extra pick. The announcement says so out
loud (*"⚠️ Platinum slot used on a Gold player"*), because it is irreversible and
noticing three picks later is worse than being told now.

One useful consequence: **a team's tier quota enforces itself.** A Platinum
player fits *only* a Platinum slot, so a team with two Platinum slots can never
end up holding three Platinum players. That is why there is no separate per-tier
cap to configure — the ceiling already is one.

**The overseas cap** (`max_overseas`, with `home_country` deciding who counts)
refuses the pick that would break it, naming the limit.

**Who counts as overseas is the pool's own country column**, compared with the
draft's `home_country` — not a label. A pool uploaded without an
`indian_status` column used to flag *every* player as home, which meant a squad
of eleven "Indians" from four countries and a cap that never refused anything.

Changing the home country re-flags the whole pool, from the draft page or with
`/dhome England` in the group. `/dhome` on its own reports the split and says
how many rows disagree with their country; `/dhome sync` re-flags without
changing the country, which is how a draft **already running** gets corrected —
its pool cannot be re-uploaded, because replacing it is refused once picking has
started. `migrate_draft_home_country.py` does the same sweep across every draft
in the database (`--dry-run` first). Players whose country reads as nothing
(`Unknown`, blank) are left exactly as they are, so a flag fixed by hand
survives; squads already picked keep their players, and the recount applies to
the picks still to come.

**Role minimums are checked as reachability, not as a finished squad.** If a
squad owes a keeper and has one slot left, the non-keeper pick is refused *at
that pick* — while there is still a slot to obey the rule with. Checking the
finished squad instead would produce a complaint nobody can act on. An
all-rounder counts towards a bowler minimum, because the XI rules already treat
them that way and refusing here would be a rule the squad sheet never stated.

### What `/dsquad` says about the rules

Every rule above gets a line on the squad readout, whether or not the squad is
currently breaking it — a rule you only see once you have broken it is one you
cannot plan around, and `🎯 Bowler 3/4` three picks out is worth far more than
the same line once there is nothing left to do about it.

```text
⚠️ Squad rules: 2 broken
👥 Squad 3/3
🏏 Batsman 2
🎯 Bowler 1/2 ⚠️
⚡ All-rounder 0
🧤 Wicket Keeper 0/1 ⚠️
   all-rounders count toward the bowler minimum
🇮🇳 Home 2 · ✈️ Overseas 1/1
```

**Two marks, because there are two different things to say.** `⚠️` means
*broken*: a rule no further pick can satisfy. `⏳` means *short but still
reachable* — the ordinary state of every squad in round one, and not a fault.
One symbol for both would either cry wolf at every team all evening or say
nothing at all until it was too late to act.

A row with no minimum is a **bare count**, not `2/0`, which reads like a
failure when it is not a rule at all. A tier over its own slot count is **not**
marked on its own: a spare Platinum slot houses a second Gold player quite
legally, so only the prefix the ladder actually overflows at gets the mark,
with the reason spelled out underneath it.

The count in the header is the length of `squad_problems`, and a row carries
`⚠️` if and only if that list contains it — both come from one
`draft_service.squad_health`, which is also what `/dtrade` checks a trade
against and what `/dadd` prints back. A rule cannot end up marked on the
readout and missing from a refusal.

### Editing a squad by hand — `/dadd` and `/ddrop`

```text
/dadd Mumbai | Virat Kohli    put a player on a squad (moves him if
                              another team has him)
/ddrop Virat Kohli            send a player back to the pool
```

Admin only, and they work at any point — a mis-typed name in round two is the
commonest reason to need them, and waiting for the draft to finish is not a fix.

**They enforce nothing.** Every other route into a squad is gated: `/pick` obeys
the tier ceiling, the overseas cap and the role minimums; `/dtrade` re-checks
all three against the squad the trade would produce. These two obey none of it,
on purpose — an admin untangling a mess has to be able to pass *through* an
illegal squad to reach a legal one (drop the extra keeper, then add the quick),
and a gate that refuses the first half makes the tool useless at the one moment
it is needed.

What stands in for the gate is the report. Each edit:

* is **announced in the draft group** — a squad nobody remembers agreeing to is
  exactly how a league ends up disputed, so the room seeing it is the brake;
* is written to **`DraftSquadEdit`** and shown in `/dtrades`, with the player,
  both teams and the admin who typed it;
* prints the **full rule state of every squad it touched**, so the one person
  who can undo it sees what they have just broken — or fixed — without running
  `/dsquad` twice.

`/dadd` is one verb for *add* and *move* because from the admin's side they are
the same instruction — "this player belongs to that team now" — and making them
choose a command based on a state they may not have checked is a way to get the
wrong one. `/ddrop` takes no team argument: a player is on exactly one squad, so
naming it would only be a second thing to get wrong.

A published league keeps up with both. `sync_league` reconciles all three
directions — moved, added, and dropped — so a player signed after `/dpublish`
gets a `ChallengePlayer` row (carrying the same `details_json` contract
publishing writes, not a second copy that can drift from it) and a released one
loses theirs. A league player whose name is **not in this draft's pool** is left
strictly alone: an admin may have added them by hand on the Challenge Data page,
and this is a reconcile against the draft, not a claim to own every row.

**Only the Owner Tag ID and co-owners may pick**, and only for the team actually
on the clock. Bot admins are deliberately *not* included: an admin who needs to
unstick a draft uses `/dskip`, which is recorded as an auto-pick — because that
is what it is, and it must never read as the owner's own choice.

**A player can only be taken once.** Both the player and the slot are claimed
with a conditional `UPDATE`, so two co-owners typing `/pick` on the same tick
cannot both win; the loser is told the player has just gone.

**An ambiguous name is never guessed.** `/pick Kohli` with two Kohlis in the pool
posts buttons rather than choosing, because guessing wrong costs a team its pick
and needs an admin to undo. More than five matches asks for more of the name.

**`/pick` only works in the bound group.** A draft is a public event the room has
to be able to see and argue with; a pick made quietly in a DM is how an order
gets disputed.

### Whose buttons are whose

**Every `dr_` draft button belongs to the person who ran the command that posted
it.** Someone else pressing it is told so, and told what to send instead — "🎯
That board belongs to whoever sent /dboard. Send /dboard for your own copy."

(`/dtrade`'s `dt_` buttons are the deliberate exception, and the opposite case:
a trade the second franchise cannot touch is not a trade. They are listed as
shared in `services.button_access`, and every press is authorised in
`handlers/draft_trade.py` against the team the presser owns **and** the step the
offer is actually on — the stronger check anyway, since it survives a restart
and lets a co-owner take over mid-offer. `/trade`'s `t1p_`/`t2p_` buttons work
the same way for the same reason.)

This is not about secrecy: everything the buttons reach is public draft state,
and the commands are free. It is about a busy room, and the three keyboards earn
the rule in three different ways.

The **pool browser** re-filters *in place*, so a second pair of hands on it
genuinely fights the first — you are halfway through Platinum bowlers when
somebody else flips it to 🔴 Taken, and neither of you gets to read anything
three seconds before a pick. The **board's tabs** post a fresh message each
press, so a stranger driving yours drops an unasked-for readout into the one
chat that has to stay legible while picks are landing. The **`/pick`
disambiguation buttons** are locked for a harder reason than either: a pick is
irreversible and needs an admin to undo, so the hands that may finish it are the
hands that typed the name.

A copy per reader costs one command and nothing else, which is why the answer to
a refusal is the command itself rather than an apology.

The lock is **stateless**. Each button carries its owner's Telegram id inside
its own callback data (`dr_view_u<id>_pool`, `dr_srch_u<id>~…`,
`dr_pick_u<id>_<pick>_<player>`), which is why it still holds hours later and
across a bot restart — the generic owner guard in `services.button_access`
remembers senders in memory only, and a draft board late in an evening is
exactly the message people are still pressing when a deploy lands. Buttons
posted *before* this shipped carry no id and fall back to that guard, so
nothing already on screen is bricked.

None of it replaces authorisation. `dr_pick_` is still re-checked against the
team on the clock on every press, so owning the message is necessary, not
sufficient — the rules above about who may pick are unchanged, and the same is
true of every `dt_` press.

---

## The clock

Every pick gets `pick_seconds` (default 15 minutes, `/dtimer`), with a warning
ping `warn_seconds` before it expires. When it runs out the bot picks:

1. **The owner's queue** — the first `/dqueue` entry that is still legal. Being
   asleep should not mean losing the player you already said you wanted.
2. **Otherwise the middle of the band** — among the legal players in the slot's
   tier, the one closest to that set's *mean* rating. Not the best of the tier
   (which would reward going quiet) and not random (which cannot be explained
   afterwards, and would happily hand a squad an illegal player).
3. If that tier is exhausted it steps **down** the ladder, which the ceiling rule
   already permits.
4. If nothing legal is left anywhere the slot is **passed**, not retried — one
   impossible slot must not wedge a hundred-pick draft.

Auto-pick runs `validate_pick` on every candidate, so the clock can never do
something an owner would have been refused for.

### Granting a pick — `/dautopick`

`/dautopick` (**bot owner only**, one slot per command) resolves the pick on the
clock right now with a **random** legal player of the slot's allotted tier — for
a squad whose owner simply isn't in the room, where waiting out a 15-minute
clock every round is the only alternative.

It differs from `/dskip` in exactly two ways, and both are the point:

* **Who may run it.** `/dskip` unsticks a draft with the clock's own rule, so
  every bot admin has it. `/dautopick` hands a team a player *nobody chose*, so
  it sits with the owner.
* **How the player is chosen.** Randomly, from what is legal in the tier — and
  the queue is ignored, because this is for the owner who said nothing. Running
  the clock's deterministic middle-of-the-band over every absent team hands them
  all the same shape of squad, pick after pick.

Everything else is identical: the tier ceiling, the overseas cap and the role
minimums are all checked (it draws only from players `validate_pick` accepts),
the ladder is stepped down when the tier is empty, a slot with nothing legal is
passed rather than retried, and it is recorded and announced as an **auto-pick**
— the board must never read as the owner's own choice.

**The clock is a column, not a job.** The deadline lives in
`player_drafts.pick_deadline_at` and `services/draft_scheduler.py` reconciles it
every 15 seconds. The host redeploys often, and an in-process `run_once` does not
survive that — which for an hours-long draft would mean every team stranded on
the clock forever. Same call, and the same sweeper shape, as
`services/giveaway_scheduler.py`.

---

## Data model

```text
PlayerDraft      the draft: status, bound chat_id, pick clock, tier ladder,
                 overseas cap, role minimums, the pinned pick, and the league
                 it published into
DraftTeam        a franchise: name, logo, owner_tg_id, co-owners, pick queue
DraftPlayer      one pool entry, scoped to the draft. picked_by_team_id NULL
                 = available. source_player_id links to a real card, for the image
DraftPick        one slot in the order AND the record of the pick that filled it
DraftTrade       one /dtrade: the half-built offer, then the record of the swap
DraftSquadEdit   one admin override: a player added, moved or sent to the pool
```

The pool is its own table rather than a view over `players` because the uploaded
sheet carries `tier`, `icon_eligible`, `gender` and the country the overseas rule
is decided on, none of which the master catalogue models — and because a draft's pool is a curated list for one
competition, not an edit to the global card database.

The order sheet and the result log are the same rows on purpose: *"R1 P3 is
Mumbai's Platinum slot"* and *"R1 P3 was Bumrah"* are the same fact at two points
in time, and splitting them would let the two drift apart.

`DraftTrade` is where the whole in-flight offer lives — which side has ticked
which players, who has confirmed — rather than in process memory, so a redeploy
mid-offer costs nothing. The two id lists are kept after completion, which is
what makes the table the `/dtrades` log: since pick rows are never rewritten,
this is the only place a squad change *after* the draft is on the record.

`DraftSquadEdit` is a separate table from `DraftTrade` because it is a
different fact. A trade is two consenting franchises and needs an offer, two
selections and two confirmations; an admin edit is one row with no state machine
at all. Sharing one table would mean a `kind` column plus three columns that
mean different things depending on it. `from_team_id` and `to_team_id` are NULL
for the pool, which is what makes the three actions one shape: NULL → team is an
add, team → NULL a release, team → team a move.

The draft tables are new tables, so `create_all` builds them. The columns that
arrived later — `player_drafts.pinned_message_id` and `.pin_picks` for the
auto-pinned latest pick, and `.trades_open` for the trade window — do have
`_try_add` lines in `database.py`, because an existing database already has the
table. `trades_open` is also backfilled to `TRUE`, because it is non-nullable in
the model and a `NULL` reads back falsy — which would quietly close the window
on every draft that finished before the column existed.

### What publishing writes

One `ChallengeTeam` per `DraftTeam` and one `ChallengePlayer` per pick, with
`is_overseas = not is_indian` and a `details_json` blob whose keys match exactly
what `admin._challenge_player_details_from_source` writes — because
`services.cipl_match.cp_to_player_dict`, not the master `players` row, is where
the match engine reads every rating and handedness from. `tier`, `icon_eligible`
and `gender` ride along after those; every existing reader ignores them.
`tests/test_player_draft.py` runs a published row back through
`cp_to_player_dict`, so a drifted key set fails the suite rather than silently
giving a drafted squad the wrong numbers.

---

## Reading a spreadsheet with no spreadsheet library

`admin._build_players_xlsx` already *writes* `.xlsx` as a zip of hand-built XML,
specifically so the project needs no Excel dependency.
`services/xlsx_reader.py` is the mirror image — `zipfile` plus
`xml.etree`, resolving shared and inline strings — so an admin can upload the
same file the site let them download, and `requirements.txt` gains nothing.

---

## Files

| File | Role |
| --- | --- |
| `services/draft_service.py` | The pool, the order, the ceiling rule, the clock, validation, auto-pick, the renderers, and publishing |
| `services/draft_trade_service.py` | `/dtrade`: the offer, the swap, `/dadd` and `/ddrop`, and the league re-sync |
| `services/draft_scheduler.py` | The restart-safe pick clock, and the announcements it and `/pick` both use |
| `services/xlsx_reader.py` | Stdlib `.xlsx` reader |
| `handlers/draft.py` | `/pick` and every other draft command, plus the `dr_` callbacks |
| `handlers/draft_trade.py` | `/dtrade`, `/dtrades`, `/dtradecancel`, `/dtradelock`, and the `dt_` callbacks |
| `models.py` | `PlayerDraft`, `DraftTeam`, `DraftPlayer`, `DraftPick`, `DraftTrade`, `DraftSquadEdit` |
| `admin.py` | `/drafts` and `/drafts/<id>` |
| `templates/admin_drafts.html`, `templates/admin_draft_detail.html` | The two admin pages |
| `migrate_draft_home_country.py` | One-off sweep: re-flags every existing pool's home/overseas players from their country |
| `tests/test_player_draft.py` | 168 tests over importing, the home country, the ceiling, permissions, the caps, picking, granting, searching, pinning, the clock, undo, publishing, the squad-rules readout, `/dadd` / `/ddrop`, and rendering |
| `tests/test_draft_trade.py` | 83 tests over the trade window, the missing rating rule, the squad rules that replace it, the tier fit, permissions, restart-safety, the admin overrides, the league re-sync and the commands |

---

## Not built (yet)

* **A snake-order generator** — `/dsnake 4` would build the whole order sheet from
  the team list instead of a hundred hand-typed rows.
* **Pick trading** between teams *mid-draft*. `/dtrade` moves players once the
  draft is over; trading the slots themselves, while the clock is running, is a
  different feature — a pending `DraftPick` would have to change hands, and the
  reachability checks that make role minimums enforceable are written against a
  running order that does not move.
* **Auction mode** — purse, bids, RTM cards. The pool and team tables would carry
  it; the bidding loop is the new part.
* **Icon and gender quotas.** `icon_eligible` and `gender` are imported and shown
  but not enforced; turning them into rules is a few lines in `validate_pick`.
* **A Mini App draft board** — a live web view, reusing the `/webapp` plumbing.
  `static/ipl16/draft.html` is a client-side-only draft UI already in the repo
  that could be re-pointed at real data.
