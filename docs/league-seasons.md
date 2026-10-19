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

The page links directly to three entries in **Event GIF Management**:

* **New Record** (`new_record`) — a mark is surpassed or first established.
* **Record Equalled** (`record_equalled`) — a new joint holder joins a record.
* **Record Holder** (`record_holder`) — requested with the Holder GIF button.

Use the existing GIF upload, URL, enable/disable, replace and weighted selection
controls. GIF choices are shared across competitions. Captions can use
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
match, so an innings extending its own record does not spam the group. Claims
are persisted before delivery: a failed Telegram send is logged and not retried.
Scorecard edits rebuild the displayed boards on the next read; already delivered
messages are not automatically retracted.

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
Exact fastest milestones, fielding, partnerships and phase splits require
additional event evidence and are explicitly identified as unavailable on the
page. This implementation does not infer them from final innings totals.

`LeagueRecordConfig` and `LeagueRecordAlert` are new tables created by the
normal `init_db` / `create_all` startup path; no existing columns are altered.
