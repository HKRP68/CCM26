# CMU Event Studio

For new Live Events with Solo and Multiplayer registration, tournament objectives, and a claimed-star reward road, see [Live Events 2.0 staging and migration](event-operations.md#live-events-20-staging-and-migration). The fixture-objective workflow below describes legacy campaigns and standalone challenges.

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

A draft is laid out as ten steps, and the nav along the top is also the progress
indicator: it marks the step you are on, the ones you have visited, and any step holding
a field the browser refused. Nothing is published until the last step.

1. Open **Event Studio** in the authenticated admin navigation or visit `/events/studio`, and select **Create New Event** with a new slug. A draft can also start as a duplicate of an existing event or from a pasted JSON export.
2. **Event & league.** Choose the event type, match format, banner and description. Pick a Challenge League and select **Import teams and squads**: it fills the team table with each active team's name, short name, logo and colours and each squad's master card IDs, and takes the league's overseas rules with it. Edit any of it afterwards — this is the draft's own copy and never writes back to Challenge Data. Add a player by typing a name: the picker searches active master cards and stores the card ID, which remains the source of every rating. Challenge Data holds no ground for a team, so a home stadium is typed here rather than imported. Teams and squad CSV can still be imported, and both lists are editable as JSON under the same step.
3. **Dates.** Pick the start and end from the calendar and clock, with the timezone they are read in, and the reward claim expiry. The step reports how long the event runs. A local time that crosses a daylight-saving boundary needs an offset-aware ISO timestamp instead: enter it in the full configuration on the Advanced step and that field becomes a text box showing it. There is no registration cutoff to author — see below.
4. **Schedule.** Choose a schedule type — round robin, 1.5 round robin, group stage, knockout, double elimination, bilateral series, survival ladder or a manual schedule — and only the settings that type reads are shown. Choose a playoff bracket: no playoffs, top 2 direct final, top 4 semi-finals, top 4 IPL style, top 6 knockout, top 8 quarter-finals, or custom, where you author the rows yourself. Set the round interval, match deadline duration, grace, minimum rest between matches, playing days, blackout dates and maximum fixtures per day.
5. Select **Generate Schedule**. It writes fixture keys, round numbers, opening times, deadlines, stadiums, pitches and weather, and shows the result in five views: an editable fixture table, a calendar, round by round, team by team, and the playoff bracket. The step says whether the whole schedule fits inside the event window. **Shuffle Fixtures** re-seeds which teams meet when. **Auto Fix Schedule** tightens the round interval and then the match window until the fixtures fit — it never changes the event's own dates, and says so plainly when no interval fits. **Lock** on a row pins it against the next generation.
6. **Participation.** Three independent checkboxes — Solo, Team (multiplayer) and Own Roster — over the two lists the configuration stores. Set the maximum participants, registration window, minimum human managers and early start.
7. **Match rules, Objectives, Rewards, Features.** AI difficulty, Impact Player, AI manager personalities, the points table, the objective table with **Auto Generate Objectives**, the Free and Diamond reward tables with **Auto Generate Rewards**, Event Pass milestones, and the per-event feature toggles. For a Moment or standalone boss, enable just the `tournament_team` track and author two teams with the playable team first.
8. **Advanced** holds the whole configuration as JSON, the per-fixture overrides, and a collapsed example for every JSON field. It is the one source of truth: every control on every step reads and writes it, so an export is exactly what the steps produced. Select **Auto Build Event** at the top for schedule → objectives → rewards in one run; each records its generator version and digests, fields edited after generation are preserved by default, and fixture `locks`, reward `reward_locks` or `objectives_locked` keep them even when replacing unlocked edits.
9. **Review.** A summary of the event, teams, fixtures, duration, modes, objectives, maximum stars and maximum reward expenditure. **Validate Event** runs the publish checks; **Publish Event** publishes the last *saved* revision, so save first. Publication snapshots the legal XI, eligible squads, engine identifier, ground reference, objectives and reward policy. Draft copies omit the published snapshot and campaign records.

### Schedule types and playoff brackets

A playoff slot is never a team: it is a reference the official final standings resolve
(`rank:1`, `group_rank:a:1`) or an earlier playoff fixture does (`winner:qualifier`,
`loser:qualifier`), so qualified teams advance from results rather than anyone
re-authoring the bracket. The brackets live in one table in
`services/event_studio_schedule.py`:

| Bracket | Teams | Fixtures |
| --- | --- | --- |
| Top 2 — direct final | 2 | Final |
| Top 4 — semifinals | 4 | Two semi-finals, final |
| Top 4 — IPL style | 4 | Qualifier 1, Eliminator, Qualifier 2, final |
| Top 6 — knockout | 6 | Two eliminators, two semi-finals, final |
| Top 8 — quarterfinals | 8 | Four quarter-finals, two semi-finals, final |
| Custom | — | None generated; your own rows stand |

The two halves combine either way round. An automatic league takes custom playoffs:
choose **Custom playoffs** and author the rows, and generating the league keeps them like
any other manual fixture. A custom league takes an automatic bracket: choose the
**Manual schedule** type with a bracket, and your authored league rows keep their own
keys, slots, timings and grounds while only the bracket is written and timed, from the
first date clear of the league.

A 1.5 round robin plays every pair once, then a balanced subset again with home and away
reversed, so team totals differ by at most one game. Leave **League games per team** empty
for the default halfway point between one and two round robins, or set it to the average
you want.

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

Minimum rest between matches (`schedule.min_rest_minutes`) applies to every schedule type, and defaults to 0 so a draft or published revision saved before it existed schedules and validates exactly as it did. The Weekend Championship template keeps its own `rest_minutes`, which packs rounds into a two- or three-day window; where both are set, the larger applies.

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
