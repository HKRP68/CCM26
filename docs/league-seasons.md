# League Seasons

A competition — "IPL", "CMU Tournament" — played season after season, the way
the IPL is: a history of champions, and at the end of each season the next one
starts by **🔨 mega auction**, **🔁 mini auction** or **✍️ manual teams**.

Everything a season needs already existed: squads in a Challenge League,
matches in a Challenge League tournament, money in a Franchise Auction, a
deleted tournament's stats in a saved season. What did not exist was the thread
through them. That is all this feature is — two tables and one service,
`services/league_season_service.py`, that drive the machinery already there.

---

## Running one (the website)

**Admin → Tournament Panel → 🏟 League Seasons.**

1. **Create a competition** — a name and a short name.
2. **➕ Add a season** — any Challenge League tournament becomes Season N
   (finished or running). So does a **📦 saved season** (a deleted
   tournament's stats), and a season from before the bot can be **recorded by
   hand** with just its champion and runner-up. Add the old ones first:
   "add a tournament and its previous season" is exactly this card, used twice.
3. The **current season** card shows where it stands —
   ① Squads → ② Tournament → ③ Champion — and the one button that moves it on.
4. When the current season is **completed**, **🌱 Next season** offers the
   three ways to make Season N+1:

| | What happens | Built from |
|---|---|---|
| 🔨 **Mega auction** | Squads dissolve. Each franchise keeps up to *N* (default 6 — the IPL 2025 rule: retentions and Right To Match together, RTM cards = spots not used), purses reset, everyone else goes back into the pool | `clone_season` / `season_from_league`, the retention rules, RTM |
| 🔁 **Mini auction** | Every franchise **keeps its whole squad at what each player cost last season**; the purse is the cap (default: last purse + ₹5 Cr) **minus** that. Owners and the admin release players until the window closes; only the released and the new are auctioned. No RTM | `auction_service.carry_previous_squads`, `retain`, `unretain` |
| ✍️ **Manual teams** | Squads typed as Challenge Data text — `Team \| Player \| Version` — pre-filled with last season's | the Challenge Data importer |

5. After the auction: **📤 Publish squads** (the same call as the auction
   page's Publish and `/apublish` — publishing from either of those is picked
   up too), then **🏟 Create the tournament**.
6. **🏁 Complete season** when the final is done (the Tournaments page's
   Complete works just as well).

### The release board (mini auctions)

`/league-seasons/<id>/seasons/<sid>/release` — every franchise's kept squad,
its price and purse, with a tick per player. Releasing puts the player back in
the pool under **🔁 Released** and his price back in the purse (the ordinary
`unretain`, so the ledger still adds up). The deadline and **🔒 Close the
window** are here too; starting the auction closes it anyway.

### Creating next season's tournament

Copies every **rule** column of last season's tournament (`TOURNAMENT_RULE_FIELDS`
— format, overs, points, overseas, rating rules, owner rule, fixture mode,
pitches, injuries, deadlines, tiebreak, stadiums, prizes, announce chat), enters
every team of the new league with its **owner** — whoever bought the squad
(draft or auction), else whoever owned the same side last season, with its home
ground and pitches — and builds the fixtures the way the Tournaments page would.
A test classifies every `Tournament` column as rule or run state, so a column
added later fails instead of silently starting next season on its default.

**Moving the commands.** With the box ticked (the default), the league's
`/command`, tournament command, fixtures command, view commands and short code
**move** to the new season's league and the old league is deactivated, so
`/cipl` and friends play the new season and the old squads stop being offered.
Moved, not copied: the bot only reads active leagues, but the Challenge Data
page refuses a view command another league holds — inactive ones included — so
a copy would be thrown away the next time anybody saved the new league. The old
tournament still answers through its `command_snapshot`. Activating the new
tournament deactivates last season's (a completed tournament otherwise stays
active by design).

---

## In Telegram

**`/seasons`** — for everyone, in a group or a DM.

* bare: the competition's card (or a picker when there are several) — every
  season newest first: a running one's stage ("🔁 Mini auction · Release window
  open (closes in 2d)"), a finished one's 🏆 champion, 🥈 runner-up, 🟠 Orange
  and 🟣 Purple Caps; buttons for each season and **📊 All-time**;
* a season: champion, runner-up, caps, MVP, its top five batters and bowlers;
* all-time: runs, wickets, sixes, highest score, best figures, average,
  **titles** — every season added up;
* `/seasons <player>` or `/seasons <competition> | <player>`: that player season
  by season, and the total.

The buttons answer only the person who asked (`ls_<view>_<series>_<arg>_<opener>`,
the `/tournamentstats` way). Not in the slash menu — both player scopes sit at
Telegram's 100-command ceiling — so it is on `/tourhelp` and in `/commands`.

**`/arelease`** — an owner's (or co-owner's) own release, while the retention /
release window is open. `/arelease Gill` by name, or bare for a list of buttons
(each asks before it releases). Works in a DM too, before the group is even
bound. An auction admin's `/aunretain <player>` is unchanged.

---

## How a season knows where it stands

**Derived, never stored.** `season_stage` reads the tournament (scheduled /
in play / completed / cancelled), else a saved season (completed), else the
league (squads ready), else the auction (window open, pool and start, live,
publish), else typed honours (completed). A season moved along from the bot or
another page is therefore never out of date.

`sync_season` runs on every page view and `/seasons` and picks up what happened
elsewhere — a publish from `/apublish`, a tournament created on the Tournaments
page (linked when exactly one unclaimed tournament sits on the season's league)
— writing only when something changed.

**Honours are the one thing copied in** (`honours_json`): the tournament's
`TournamentHonour` rows, else its final, else — for a league-only tournament —
the top two of the table and the stat leaders. A deleted tournament takes its
final with it, so the season keeps the snapshot and follows the tournament to
its saved season (`season_archive.delete_tournament` calls `forget_tournament`).

**Total Season Stats.** Each season plays on its own league, and `/statstour`'s
default is "every season in the same league" — which would drop the earlier
ones. So each season's tournament has the competition's earlier seasons
**merged** into its links (`link_season_stats`); a link an admin set by hand
survives.

---

## The mini auction, in the auction's own terms

A mini auction is retention with the cap at the squad size and the price fixed
by last season instead of a slab. `carry_previous_squads`:

* takes each franchise's last-season team (`previous_team_links`, by id where
  it can), each player at `details_json.auction_price_lakh` — what
  `publish_to_league` stamps — or, for a hand-made league or a free auto-fill
  (stamped at 0), the base-price ladder;
* keeps the **dearest first**, so a purse that cannot hold the whole squad
  loses the cheap end rather than a star;
* runs each `retain` in a **savepoint** — `_sign_before_auction` writes the lot
  before its checks, and a refusal must not leave a half-made lot for the pool
  builder to skip — and puts every refusal (purse, squad, overseas, role or
  rating cap) in the pool under **🔁 Released**, reported by name; so are the
  players of a last-season team no franchise continues;
* is **quiet per player** (`retain(..., quiet=True)`) and says one
  `squads_carried` line per franchise. Setup-time events are replayed into the
  group when the auction goes live, five a tick, and two hundred "🔒 retain"
  lines would bury the first lot.

The preset sets every retention knob rather than inheriting it — classic mode
(a season cloned from a dynamic-retention year would otherwise price every
player by slot), no spend cap, no rating or category limit, RTM off — but keeps
the squad rules (role maximums, rating rules), which also govern the auction.

`/arules` and `/aretlock` read "Up to 25 per franchise" in a mini auction: that
is the squad size, and it is what the cap is.

---

## Data model

```text
LeagueSeries   league_series    a competition: name, short name, description, logo, active
LeagueSeason   league_seasons   one season: number, name, how its squads were made
                                (mega | mini | manual | existing), and the ids of its
                                league, auction, tournament and saved season, plus
                                the honours snapshot
```

The links are **plain ids, not foreign keys** — the `StatsSeason` call, and one
more reason: SQLite neither enforces `SET NULL` nor promises not to reuse a
deleted row's id. So every delete path clears them (`forget_tournament`,
`forget_league` from Challenge Data's delete, `forget_auction` from the auction
delete, `forget_saved_season`), and every reader treats a missing row as
unlinked. Two new tables, so `create_all` builds them; no `_try_add` lines.

## Files

| File | Role |
|---|---|
| `services/league_season_service.py` | Competitions, seasons, stage, honours, sync, the three next-season paths, publishing, the command hand-over, next season's tournament, stats |
| `services/auction_service.py` | `retain(quiet=)`, `carry_previous_squads`, `release_by_owner`, `unretain(by_admin=, set_name=)` |
| `services/season_archive.py` | Deleting a tournament or saved season keeps its League Season |
| `admin.py` | `/league-seasons`, `/league-seasons/<id>`, the release board, manual teams |
| `templates/admin_league_seasons.html`, `admin_league_series.html`, `admin_league_season_release.html`, `admin_league_season_manual.html`, `static/css/admin_league_seasons.css` | The pages |
| `handlers/league_seasons.py` | `/seasons` and its `ls_` buttons |
| `handlers/auction.py` | `/arelease` for owners and its `au_rel_` buttons |
| `tests/test_league_seasons.py` | The service: attach, stage, honours after delete, mini, mega, owner release, hand-over, tournament rules guard, stats |
| `tests/test_league_seasons_page.py` | The pages over HTTP |
| `tests/test_league_seasons_command.py` | `/arelease` and `/seasons` |

## Not built (yet)

* `/seasons` has no slash-menu slot — giving it one means retiring another entry.
* A mini auction's released list is announced player by player (the ordinary
  `retention_released` line), not as one list per team.

## Season Records & Stats Centre

Open **League Seasons → a competition → Open records centre**. Select a season
or **All-time**, then filter by stage or team. The page includes honours,
scorecard totals, a full-season standings table, player figures, joint record
holders, match evidence and a comparison with another season. The comparison
season is unfiltered and shows match counts and team runs per match alongside
totals. Standings use the existing official table, including simulations and
points adjustments; performance record boards exclude simulated fixtures.

### Editable minimum balls

Under **Qualifications & GIFs**, edit **Minimum batting balls** and **Minimum
bowling balls** and save. Values must be whole numbers from 0 to 100,000; zero
removes the qualification minimum. Undefined rates (zero balls or dismissals)
remain undefined. Batting minimum applies to average and strike rate; bowling
minimum applies to economy, average and bowling strike rate. Unqualified players
remain in the player table and count-based records. Existing tournament minimums
are the initial values and are updated when saved, so the tournament's own
qualified boards and next season's inherited rules use the same minimums.
All-time rates use the latest season's settings, applied to competition totals.

### GIFs and Telegram

The primary control links to **Default Record GIF** (`record_default`), shared
by all players and record events. Three legacy fallback entries remain in
**Event GIF Management**:

* **New Record** (`new_record`) — a mark is surpassed or first established.
* **Record Equalled** (`record_equalled`) — a new joint holder joins a record.
* **Record Holder** (`record_holder`) — requested with the Holder GIF button.

Use the existing GIF upload, URL, enable/disable, replace and weighted selection
controls. The shared default applies across competitions. Captions can use
`{player}`, `{record}`, `{value}`, `{previous}`, `{season}`, `{scope}` and
`{match}`. For a holder request there is no previous value or triggering match;
leave those placeholders out of that caption.

In Telegram, **/seasons → a season → Season records**, or **/seasons → Records**
for all-time. Choose a category, then **Holder GIF** to celebrate its current
holder(s). Buttons retain the existing opener-only access check.

Official tournament matches in the over-by-over flow check provisional records
following each saved over. Rate and successful-chase records are evaluated after
the committed match result, including regulation performances in matches decided
by a Super Over. Casual matches do not contribute. A per-season checkbox disables
automatic celebrations; browsing records and requesting a holder GIF still work.

Alerts combine season/all-time achievements for the same category and holder,
then group the same holder's achievements into one message and GIF. Each
holder/category/scope/break-or-equal combination celebrates at most once per
match, so an innings extending its own record does not spam the group. Alert
claims and publications are durable; see Corrections and delivery guarantees
below for retries, source revisions and retractions.

### Evidence and supported records

Supported boards: runs, highest individual score, batting strike rate and
average, fours, sixes, fifties (50–99), hundreds, wickets, best bowling figures,
economy, bowling average and strike rate, 3+ and 5+ wicket hauls, highest team
total and highest successful chase. Rate comparisons use exact fractions for
ties; formatting to two decimals never creates or breaks a joint rank.

Match-based records link to a read-only scorecard within the records centre.
When deleting a tournament **with stats kept**, the season preserves completed
non-simulated scorecards and the standings before the source is removed.
Deleting its saved season clears this preserved evidence too. Unlinking a
League Season removes its record settings and alert claims while leaving the
underlying tournament, league and auction intact.

Older imported aggregate-only seasons retain supported totals and best figures,
labelled **Season aggregate**. They do not gain fabricated match links, fifties,
hundreds or hauls. Partial milestone counts are omitted from mixed-history
all-time boards when an aggregate-only season contributes to that player.
Fielding, partnerships and phase splits use saved delivery evidence in new
matches. Imported history remains explicitly unknown where it is absent;
these figures are never inferred from final innings totals.

`LeagueRecordConfig` and `LeagueRecordAlert` are new tables created by the
normal `init_db` / `create_all` startup path; no existing columns are altered.


### Exact fastest 50 / 100 / 150 (new matches)

Every scoring delivery captures `balls_to_50`, `balls_to_100` and
`balls_to_150` the first time a batter crosses the corresponding score. These
are balls faced, not legal balls bowled. Wides do not count; a faced no-ball
counts for the batter while remaining an illegal delivery for the bowler.
The fields are retained in match-state JSON, the tournament scorecard, the
Mini App scorecard and preserved season evidence. No new database columns or
ball-by-ball history table are needed.

Fastest fifty, hundred and 150 boards use the lowest recorded ball count;
equal counts share ranks. Minimum-ball qualifications do not apply to these
milestones. New records and equal records use the existing live-over alerts
and configured record GIFs. The count is captured on the actual delivery,
even though the over-by-over flow announces it after the saved over.

Older innings without these fields remain excluded. Resuming an older live
innings records future crossings only, without guessing already-passed ones.

## Record centre: watches, profiles, advanced evidence and amendments

### One default GIF for all players

**Records centre → Qualifications & GIFs → Default record GIF for all players**
opens `/media/record_default`. Upload or replace the shared animation there.
It is used for new records, joint holders and requested holder celebrations,
for every player and competition. No player-specific GIF mappings are stored.
Existing `new_record`, `record_equalled` and `record_holder` clips remain fallback
choices only when the default sends no media. Captions additionally support
`{previous_holder}` and `{scorecard}`. Alerts include old holders, scoped old/new
values, opposition and a scorecard link when `WEBAPP_URL` is configured.

### Record watch and automatic recaps

The automatic post-match **Records file** covers all linked seasons of the
competition, including archived seasons. It contains a match summary, only
all-time record changes (with previous holders), every current all-seasons
record holder, and the top three career runs/wickets ranks including ties.
Match-based holders include season and source-match details where available;
aggregate-only history is labelled. The file is a snapshot through the triggering
match, so later matches and seasons cannot change its historical cutoff. Rate
minimums come from the latest season included in that snapshot. Correcting an
earlier season also revises affected later files in place. The group receives
one document after the official result commits; automatic live watches and
celebration messages remain silent. On-demand record browsing is still available.

The season page shows players approaching 500-run / 25-wicket multiples and
run/wicket lead changes. Legacy watch and recap settings remain stored for
compatibility; the automatic match flow publishes the single records document.

The match summary includes the result, leading batting and bowling performances
and exact run milestones. Configured Mini App links open the source match.

### New delivery evidence

Every new applied delivery records the batter/bowler, batting pair, team runs,
batter runs/balls, bowler-conceded runs, legality, wicket, phase and explicit
fielder/keeper attribution. Direct-hit and assisted run-outs are credited only
when supplied by the scoring outcome; otherwise they are unclassified. Caught
and bowled credits the bowler; stumpings use the selected keeper. Regulation
innings remain separate from Super Overs.

The stream is versioned JSON in live state, the final Mini App scorecard, and
`record_detail` on the first real tournament player line. It is preserved with
the season archive. The existing array of player lines stays compatible with
all aggregate rebuilds. There is no fabricated metadata player and no new
per-delivery database table.

Partnerships include extras, contributions, wicket number, legal-ball counts,
exact 50/100 crossing balls and the unfinished-stand marker. By-wicket leaders
are shown separately. Partnership records require an innings captured from its
start. Phase tables show recorded batting runs/strike rate and bowling
wickets/economy. Full T20 windows are 1–6, 7–15 and 16–20; The Hundred uses first
25, middle 55 and last 20 legal balls; shortened games use scaled windows.
Field catches, keeper catches, stumpings and run-out involvements stay separate.
Missing history is not reconstructed. Coverage is displayed on the page.

If an aggregate scorecard correction disagrees with a complete delivery stream,
advanced records are withheld with a diagnostic until the source event evidence
is corrected. The raw events remain downloadable from the match scorecard.

### Player pages, honours and sharing

Click a player in the records centre for career totals, season splits, last five
scored performances, personal bests, awards and stored record history. An existing
catalogue photo is used when present. `/seasons <player>` also shows recent form,
current all-time records and awards.

**Season honours** preserves champions, caps, official awards, highlights,
standings, playoff scorecards and record announcements. The suggested statistical
XI is explicitly labelled, scored as runs + 20 × wickets, with 1 keeper,
4 batters, 2 all-rounders and 4 bowlers; unfilled roles stay vacant. An admin may
publish eleven distinct season players with explicit selection criteria as the
**Official Best XI**. That selection is kept in the honours archive and shown in
Telegram season cards too. Existing team logos appear in standings when available.

Every populated record card has a **Download record card** PNG action. It uses
the current board, includes joint holders and can be shared independently. Access
follows the admin page's login requirement. The cards/compact leaderboard toggle
is remembered in the browser and works on mobile; wide tables scroll locally.

### Corrections and delivery guarantees

`LeagueRecordPublication` stores text, original Telegram message ID, revision,
source/policy fingerprint and pending/sending/sent state. Scorecard rebuilds,
result removals and qualification changes queue revisions inside the same DB
transaction. After commit, the bot's `league_record_publications` job (every
30 seconds) edits affected messages rather than publishing a fresh duplicate.
If Telegram reports that the original can no longer be edited, an amendment is
posted and its new message ID retained. Removed/ineligible source matches produce
explicit retractions. First official completion anchors live announcements and
does not mistake still-running matches for removed scorecards.

Failed sends remain pending; stale sending claims recover after five minutes.
As with any external API, an ambiguous network failure after Telegram accepted a
new message can produce a retry duplicate. Known successful sends and amendments
are deduplicated persistently. Amendments never replay the celebration GIF.
`LeagueRecordOptions` and `LeagueRecordPublication` are new startup-created
tables; their rows are removed when their League Season is explicitly unlinked.
