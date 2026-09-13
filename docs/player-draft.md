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
| `/dboard` | anyone | The live board — who's up, time left, recent picks, plus buttons for the order, the remaining pool and the field |
| `/dsquad [team]` | anyone | A squad by tier, with slot progress and the overseas count |
| `/dqueue <player>` | owner + co-owners | Your wishlist. If your clock runs out the bot picks from it first |

Admin: `/dadmin` (the reference card), `/dnew`, `/dbind`, `/dstart`, `/dpause`,
`/dresume`, `/dtimer`, `/dhome`, `/dco`, `/dskip`, `/dundo`, `/dcancel`,
`/dpublish`.

Owner only: `/dautopick` — see *Granting a pick* below.

### Finishing

`/dpublish` (or the button on the draft page) writes the squads into a Challenge
League. Publishing again re-syncs the same league rather than creating a second
one, so a correction made with `/dundo` can just be republished.

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
                 overseas cap, role minimums, and the league it published into
DraftTeam        a franchise: name, logo, owner_tg_id, co-owners, pick queue
DraftPlayer      one pool entry, scoped to the draft. picked_by_team_id NULL
                 = available. source_player_id links to a real card, for the image
DraftPick        one slot in the order AND the record of the pick that filled it
```

The pool is its own table rather than a view over `players` because the uploaded
sheet carries `tier`, `icon_eligible`, `gender` and the country the overseas rule
is decided on, none of which the master catalogue models — and because a draft's pool is a curated list for one
competition, not an edit to the global card database.

The order sheet and the result log are the same rows on purpose: *"R1 P3 is
Mumbai's Platinum slot"* and *"R1 P3 was Bumrah"* are the same fact at two points
in time, and splitting them would let the two drift apart.

New tables only, so `create_all` builds them — there is no `_try_add` migration,
because nothing was added to an existing table.

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
| `services/draft_scheduler.py` | The restart-safe pick clock, and the announcements it and `/pick` both use |
| `services/xlsx_reader.py` | Stdlib `.xlsx` reader |
| `handlers/draft.py` | `/pick` and every other draft command, plus the `dr_` callbacks |
| `models.py` | `PlayerDraft`, `DraftTeam`, `DraftPlayer`, `DraftPick` |
| `admin.py` | `/drafts` and `/drafts/<id>` |
| `templates/admin_drafts.html`, `templates/admin_draft_detail.html` | The two admin pages |
| `migrate_draft_home_country.py` | One-off sweep: re-flags every existing pool's home/overseas players from their country |
| `tests/test_player_draft.py` | 113 tests over importing, the home country, the ceiling, permissions, the caps, picking, granting, the clock, undo, publishing and rendering |

---

## Not built (yet)

* **A snake-order generator** — `/dsnake 4` would build the whole order sheet from
  the team list instead of a hundred hand-typed rows.
* **Pick trading** between teams mid-draft.
* **Auction mode** — purse, bids, RTM cards. The pool and team tables would carry
  it; the bidding loop is the new part.
* **Icon and gender quotas.** `icon_eligible` and `gender` are imported and shown
  but not enforced; turning them into rules is a few lines in `validate_pick`.
* **A Mini App draft board** — a live web view, reusing the `/webapp` plumbing.
  `static/ipl16/draft.html` is a client-side-only draft UI already in the repo
  that could be re-pointed at real data.
