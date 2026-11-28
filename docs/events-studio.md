# CMU Event Studio

## Checkout and integration

The clean workspace initially checked out `89e4e87`. GitHub main had advanced by 81 commits to `f207918`. Events Studio was ported to and verified against that newer base. `admin.py` serves the authenticated Flask panel and Telegram MiniApp. The legacy `/events` screen in that panel configures economy multipliers. `website.py` serves the franchise site.

- The bot uses `bot.py` and the Telegram JobQueue. The MiniApp loads `templates/webapp.html` and authenticates API requests with `admin._webapp_auth`.
- The database uses SQLAlchemy sessions from `database.get_session` and additive table creation in `database.init_db`. The deployment path supports PostgreSQL and development supports SQLite.
- Core cricket runs through `services.cipl_match.simulate_over` and `engine.ball_outcome.calculate_outcome`. Shared XI rules and bot captain policies are in `services.xi_rules` and `services.bot_captain`.
- The ordinary Approach path includes dramatic scenario scripting, chase steering, scoring floors and ceilings, pressure-dependent finishing adjustments, and tournament result overrides. Official play needs an explicit opt-in execution path that excludes those controls.
- ODI uses the existing `services.odi_approach` adapter and the measured reference validated by `engine.realism.odi`. Published ODIs pin the tuning settings, engine version and reference hash.
- Ordinary match finishing grants standard rewards and activity through the existing handlers and `services.match_rewards`. Events settle their wallet rewards independently. Eligible Community XP uses the shared hooks and XP ledger.

## Implementation plan and risks

First register separate tables and a validated revisioned draft format. Build deterministic schedule, objective and reward generation before connecting publish. Freeze team snapshots and bound liability at publication. Then wire personal campaigns to a server-owned Approach state, with idempotent launches, versioned over commits and immutable results. Add bounded Moment continuations after full matches work. Expose the same service in Telegram and the MiniApp, then verify deadlines, wallet claims, recovery and ordinary game regressions.

The main risks are concurrent starts and finalization, randomness changes on restart, incomplete bowling quotas, stale generation, and wallet settlement during external activity. Serialize writes with PostgreSQL row locks or SQLite's writer lock, pin the match policy and private seed, version each committed over, validate fixture dependencies and legal bowler choices, and settle amounts and receipts in one database transaction.

## Author and publish

1. Open **Event Studio** in the authenticated admin navigation or visit `/events/studio`.
2. Create a draft with a new slug. Pick the opening and closing times from the calendar and clock. A local time that crosses a daylight-saving boundary needs an offset-aware ISO timestamp instead: enter it in the full configuration JSON and that field becomes a text box showing it.
3. Choose a Challenge League at the top of the draft and select **Copy teams and squads**. It fills Team details with each active team's name, short name, logo and colours and Squads with its master card IDs, and takes the league's overseas rules with it. Team details and Squads are two views of the same team list, as Challenge Data separates them; teams and squad CSV can be imported instead, and a full configuration JSON can be imported or duplicated when creating a draft. Active master card IDs are the source of all ratings. Resolve missing cards through the existing player editor.
4. Choose tracks, schedule, objectives and a per-asset budget. For a Moment or standalone boss, enable just the `tournament_team` track and author two teams with the playable team first. Every JSON field carries a collapsed example of its shape.
5. Select **Auto Build Event** for schedule → objectives → rewards. Review the preview and select **Apply preview**. Each run records its generator version and digests. Fields edited after generation are preserved by default. Set fixture `locks`, reward `reward_locks` or `objectives_locked` to keep them even when replacing unlocked edits.
6. Validate the draft, save any edits and publish the saved revision. Publication snapshots the legal XI, eligible squads, engine identifier, ground reference, objectives and reward policy. Draft copies omit the published snapshot and campaign records.

Stadium Data (`/conditions`) groups its grounds into one collapsible per country, so an edit to a country's grounds does not mean scanning the whole list. The country is free text on a ground, with a list of the spellings already in use; spellings are folded when grouping, so `UAE` and `United Arab Emirates` stay one country.

Team JSON accepts the Challenge Data field names, for example:

```json
{"teams":[{"team_name":"Harbour XI","short_name":"HBR","primary_color":"#1d4ed8"}]}
```

A squad import replaces the listed teams' squads after preview. Its CSV headers and one illustrative row are:

```csv
TEAM NAME,PLAYER NAME,VERSION
Harbour XI,Example Player,Base
```

For a repeatable eight-team test cup select `round_robin` with two legs and `top4` playoffs. The generator produces 56 league fixtures and four playoff fixtures. Use a closing date that accommodates the final grace period. Group events use a mapping such as `{"a":["team-1","team-2"],"b":["team-3","team-4"]}` and `qualify_per_group` in the schedule settings. The survival ladder uses the first team as the playable side.

Anyone may join an event while it is open; there is no enrollment limit and no enrollment deadline to author. Joining closes on its own: a Live Event takes players until its first fixture deadline, because a campaign's fixtures are written when someone joins and carry the published deadlines, so a later joiner would be handed a fixture they could never play; a Moment or Boss Battle has no fixtures and takes players until it closes. `max_users`, shown under Rewards as players covered by the budget, is only the figure each reward bundle is scaled against and liability is reported for.

Conditions are per fixture, not per event. Generating a schedule draws each fixture's `pitch`, `venue` and `weather` — the pitch from the surfaces the format's pinned reference models, the venue from Stadium Data, narrowed by `schedule.venue_countries` when that is set, and the weather from the pitch report's list. The draw is reproducible from the draft itself, so regenerating an unchanged draft gives the identical grounds; changing `schedule.seed` re-rolls every one. A standalone Moment or Boss Battle has no fixtures, so its one ground is drawn onto the event. Editing a fixture's ground by hand keeps it on the next run, as with any other authored field, unless **Replace unlocked manual edits** is selected.

A manual fixture row contains `key`, `home`, `away`, `round`, `stage`, `opens_at`, `deadline`, `grace` and optional `source`, `locks`, `pitch`, `venue` and `weather`. With the `manual` preset a whole tournament can be authored this way, each fixture carrying its own ground. Fixture overrides use the fixture key. They can set `pitch`, `weather`, `venue`, `difficulty`, `objectives` or `rewards`, and an override beats the fixture row's own ground. A fixture may use different objectives with the same maximum stars. Bracket slots accept earlier `winner:<fixture>`, `loser:<fixture>`, `rank:<number>` and `group_rank:<group>:<number>` dependencies. JSON exports contain only versioned design inputs.


## CIPL match integration

An official match uses a durable `event-<id>` link. After toss and launch, the stored checkpoint contains the enrollment, fixture or attempt, published revision, squad keys, format, chosen difficulty, reward tier and deadline. Shared CIPL validation handles bowler, bowling approach, batting approach and incoming-batter choices. Teams alternate innings via the standard CIPL transition. CIPL Arena handles playback, delivery chips, match effects, chase comparison and the scorecard. The Events panel shows objectives, stars, points and reward eligibility.

Set **AI difficulty override** to Easy, Normal, Hard or Hard++. A fixture can override the event's setting. The result model is shared across difficulties. Hard++ changes the AI's choice of legal XI and its tactical planning. T20 uses the ordinary CIPL probability model selected at publication. A deployment using the measured T20 model pins its baseline and tuning in the event revision. ODI pins the existing 50-over model. Official play disables scenario scripting and result steering. Impact Player is opt-in for full T20 fixtures.

## Delivery status

- **Studio.** Drafts, Challenge Data copy and imports, generation previews, publishing, manual overrides, activity, deadline extensions and audit.
- **Official play.** Personal tournaments, T20 and ODI, resume across Telegram and the MiniApp, immutable terminal results, knockout and survival progression.
- **Challenges.** Bounded segments and chases from recorded or generated checkpoints, Boss Battles, attempt limits and daily or weekly editions.
- **Progression.** Objectives, standings, player statistics, track leaderboards, trophies, wallet claims, optional Community XP and fixture reminders.
- **Rollout.** All publishing switches default off. Own Roster requires a separate Hard++ evaluation switch. The operations runbook records measurements and known limits.
