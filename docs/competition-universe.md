# Competition Universe delivery and rollout

This release connects the existing Live Events and Challenge League stores. Source, competition instance and fixture identify each result. Challenge League archival with saved stats preserves verified rating inputs while rankings are enabled. A Live Event room remains separate from its published template. A Challenge League controller is the verified captain who played the match. Honour and prize-share attribution still follow the existing award rules.

## Delivery by prompt

| Prompt | Implementation | Remaining limits |
| --- | --- | --- |
| 01 AI Manager Personalities | Ten versioned profiles, persistent per-franchise assignment for new opted-in Live Events rooms, seeded automatic assignment or fixed franchise mapping, bounded tactical weighting, legal selection, and opponent scouting. The human-versus-AI path applies a prior to the existing captain policy. AI-only fixtures retain their existing phase policy with bounded sampling. | Challenge League's official Approach fixtures require two human seats. Its administrative score simulator is outside AI manager assignment. Calibrated here with synthetic AI-only matches. Larger samples and human-versus-AI staging are pending. |
| 02 World Rankings | Verified PvP Elo across the two sources, separate source and format views, historical exclusion counts, provisional rules, stable ties, personal changes, Solo record, and idempotent replay after a correction. | Older Challenge League results lacking controller evidence and matches with handover attribution gaps stay unrated. Rate anomalies are logged as signals. Dedicated moderation and a full combined non-Elo manager leaderboard are pending. Replays stop and report a failure above 20,000 candidate fixtures. |
| 03 Tournament Analytics | Source-authorized fixture and scorecard views, underlying standings, batting/bowling figures, phase and approach summaries when recorded, condition breakdowns, team, format, participation, stage and date filters, and player CSV. Includes MiniApp drill-down, a phase rate chart and Telegram summary. | Deep partnership, batting-position, pace/spin and bowler-pair analysis, the full tactics matrix, saved-archive analytics and incremental caching are pending. Coverage varies by fixture. Direct reads enforce 2,500 fixture and 150 displayed/exported player limits. |
| 04 Competition Universe | Unified directory, personal next actions, source/status/format filters, progress and deadlines, and links back to the existing room and tournament flows. Private Live Event rooms are available to members. | A cross-source calendar, deeper player profile and historical archive discovery remain separate or pending. |
| 05 Smart Admin Automation | Authenticated operations dashboard with source statuses, lobby and fixture incidents, Live Event wallet exposure and date windows, rankings and collection reconciliation, and audited draft rotation with preview, explicit approval, overlap information and retries. Links to the existing Event Studio preview and generation tools. | Automatic publication is disabled. Unified actual-vs-forecast analytics, production difficulty recommendations, broad incident diagnostics and combined recap/archive tooling are pending. |
| 06 Event Collection Book | Verified champion and runner-up trophies, participation from official evidence, separate season-edition records, source/mode/format/year filters, user-private progress, admin family mapping and retirement, and reconciliation that retains superseded rows. Reads locked Challenge League honour and prize-share evidence. | Full title, milestone and cosmetic rule catalogue, artwork/share cards, broader history recovery and chunked backfill are pending. Older Live Event snapshots may lack a verifiable runner-up key. The Book retains previously verified Challenge League trophies in saved archives. Participation requires one verified match and a completed instance. Scans have a 15,000-record cap. |

## Flags and configuration

All feature flags default off except the two source selectors, which default on behind the master flag.

| Environment variable | Effect |
| --- | --- |
| `COMPETITION_UNIVERSE` | Competition directory, signed API and operations page |
| `COMPETITION_UNIVERSE_LIVE` | Include Live Events in shared surfaces |
| `COMPETITION_UNIVERSE_CHALLENGE` | Include Challenge League in shared surfaces |
| `COMPETITION_UNIVERSE_RANKINGS` | Capture controller evidence and refresh Elo when the corresponding source is enabled |
| `COMPETITION_UNIVERSE_ANALYTICS` | Enable analytics and record new Challenge League summaries |
| `COMPETITION_UNIVERSE_COLLECTION` | Enable Collection Book and reconciliation |
| `COMPETITION_UNIVERSE_ROTATION` | Enable approved, draft-only rotation |
| `EVENT_STUDIO_AI_PERSONALITIES` | Enable AI manager configuration at publication, independently of shared views |

Merge the following fields into an existing **draft** shared Live Event config. The team keys must exist in the draft. Use the usual Event Studio flags for publication, supported format and Multiplayer.

```json
{
  "objective_scope": "tournament",
  "competition": {
    "modes": ["solo", "multi"],
    "lobby_v2": true,
    "world_rankings": true,
    "analytics": true
  },
  "ai_managers": {
    "enabled": true,
    "blend": 0.12,
    "allowlist": ["aggressor", "wall", "architect"],
    "franchises": { "team-2": "wall" }
  }
}
```

Live Event Elo requires an opted-in revision, a public Multiplayer lobby and an officially completed human-human match. Challenge League Elo uses actual CIPL match seats at finalization rather than current ownership. The initial policy uses Elo 1500 with K 32 during provisional play and K 24 subsequently. Public ranks require 10 verified matches and three different opponents. T20 and the qualified ODI are rated independently as well as in the combined board.

Set `competition.analytics` to collect additional summaries in new Live Event fixtures. Existing participant scorecards remain available where the underlying data supports them. An empty AI allowlist uses the ten registered profiles. Set a franchise mapping to override its automatic assignment. Publication pins the settings; room start pins the identities for the AI-managed franchises. Scout usage is based on recorded, completed overs.

Collection catalogue and edition links are configured at **Competition Operations → Collection catalogue**. Select an existing published Live Event or Challenge League as the family, then map related published editions explicitly by ID if they use a different source ID. Two editions of one family remain two trophies. Retirement leaves earned records in place.

## Entry points

- Telegram directory `/competitions` or `/competition`, rankings `/worldrank` or `/worldrankings`, and personal book `/collection`.
- Telegram analytics `/ctanalytics <tournament ID>` and `/eventanalytics <room ID>`. The MiniApp links directly from the competition card. Collection and private room analytics require a private Telegram chat.
- MiniApp **Compete → Competition Universe**, **Profile → Collection Book**, and **Analytics** from the Challenge League and Live Event hubs. Deep links include `#universe`, `#worldrank` and `#collection`.
- Signed MiniApp `POST /api/webapp/competition-universe` uses the existing Telegram MiniApp authentication. Actions are `directory`, `rankings`, `analytics`, `csv` and `collection`.
- Admin `GET /competitions/operations` and CSRF-protected `POST /competitions/operations`. Continue using `/events/studio` to edit and publish Live Events.

```json
{"action":"analytics","source":"live","instance":42,"format":"T20","participation":"all"}
```

## Database and deployment

The additive SQLAlchemy migration runs through `database.init_db()`. It creates `studio_ai_managers`, `competition_controller_evidence`, `competition_archived_results`, `competition_projections`, `competition_ratings`, `competition_rating_events`, `competition_catalogue`, `competition_catalogue_links`, `competition_collectibles` and `competition_rotations`. Canonical Live Event result details and Challenge League scorecards can carry versioned derived telemetry in existing JSON fields. The original fixture, wallet and receipt schemas remain intact.

1. Take the usual database backup and run `python -c 'from database import init_db; init_db()'` against staging. Check that a second run keeps the original rows and creates no duplicate tables or indexes.
2. Enable the master flag and desired sources on staging. Enable analytics, rankings, Collection Book and rotation independently. Use Operations to dry-run rankings and collection before writing either projection.
3. Keep the existing Event Studio worker running. It refreshes projections at approximately five-minute intervals and handles approved rotation jobs in small batches. Rotations prepare drafts. Publish using the existing Event Studio workflow.
4. Qualify AI manager settings with representative test squads before enabling the independent AI publication flag. Publish a new revision or event through normal controls; existing revisions and active rooms retain their pins.
5. Roll back by turning off only the affected feature flags, or the master flag to remove the shared surfaces and stop their background work. Turning off `EVENT_STUDIO_AI_PERSONALITIES` stops future opted-in publication. Keep the additive tables and pinned profiles so existing matches can resume. Queued rotations and projection history remain available when rollout resumes.

## Verification

Paired synthetic engine calibration used the same fixtures and seeds for each comparison, four surfaces and three rating-gap bands. The run completed 12 fixture pairs per profile and format, with 24 shared baseline fixtures and 240 profiled fixtures. There were no ineligible bowling selections. T20 profiles won 4–6 of 12 fixtures versus 4 of 12 for the baseline. ODI profiles and baseline each won 7 of 12. Several profiles shared results on these seeds. The JSONL output includes Wilson intervals, recorded approach counts and phase denominators. Host load affected absolute timings, and the sample is too small to qualify a balance change.

```sh
python -m tools.calibrate_competition_ai --pairs 12
```

A synthetic SQLite tournament with 600 completed fixtures and up to 22 scorecard lines per fixture used four SQL SELECTs per authorized analytics request. Median latency over eight single-reader samples was 77.52 ms (maximum 95.71 ms). Twelve requests across four concurrent readers measured 449 ms median and 736.22 ms maximum in the same workspace. These measurements cover the service read only.

## Files

- Models and schema registration in `models_universe.py`, `models_events.py` and `database.py`.
- Domain logic in `services/competition_ai.py`, `competition_sources.py`, `competition_rankings.py`, `competition_analytics.py`, `competition_collection.py`, `competition_universe.py` and `competition_operations.py`.
- Existing integration points in `services/event_studio.py`, `event_studio_admin.py`, `event_studio_competition.py`, `event_studio_cricket.py`, `event_studio_worker.py`, `bot_captain.py`, `tournament_service.py`, `tournament_webapp.py`, `season_archive.py` and `command_catalog.py`.
- Telegram and web views in `bot.py`, `handlers/competition_universe.py`, `handlers/event_competitions.py`, `templates/webapp.html`, `templates/admin_event_studio.html`, `templates/admin_competition_operations.html`, `static/js/competition_universe.js` and `static/js/events_hub.js`.
- Verification in `tests/test_competition_universe.py`, `tests/test_event_competitions.py` and `tools/calibrate_competition_ai.py`.

### Regression commands

```sh
python -m pytest -q tests/test_competition_universe.py tests/test_tournament_duels.py
python -m pytest -q tests/test_competition_universe.py -k miniapp
CCM26_EVENT_TEST_POSTGRES="$TEST_POSTGRES_DSN" python -m pytest -q \
  tests/test_competition_universe.py tests/test_event_competitions.py \
  tests/test_event_objective_foundation.py tests/test_live_events_phase2.py -k 'not miniapp'
CCM26_EVENT_TEST_POSTGRES="$TEST_POSTGRES_DSN" python -m pytest -q \
  tests/test_competition_universe.py \
  tests/test_event_competitions.py::test_migration_rerun_keeps_legacy_and_shared_rows -k 'not miniapp'
python -m pytest -q tests/test_event_*.py tests/test_live_event_expansion.py \
  tests/test_live_events_phase2.py tests/test_rcpl_multiplayer_flow.py \
  tests/test_auction_league*.py tests/test_cipl*.py tests/test_odi_approach.py \
  tests/test_tournament*.py tests/test_bot_menu_commands.py
node --check static/js/competition_universe.js
node --check static/js/events_hub.js
git diff --check
```

The focused SQLite run passed 25 tests, and a separate Competition Universe run passed 15. Playwright passed at 390px and 1280px. The signed-route test starts two separate application processes against one file-backed SQLite database and verifies private access, CSV export, Telegram rendering and admin CSRF enforcement. The PostgreSQL 16 Events and Universe regression run passed 50 tests with six browser cases deselected. The final focused PostgreSQL rerun passed 14 tests with two browser cases deselected. The ranking concurrency test starts two refresh workers against the same rows, and the Events regressions include concurrent claims.

The broad regression run passed 1,213 tests and 129 subtests with one failing tournament-duels season archive assertion. The same assertion failed in a separate broad run of the untouched `f783d57` checkout. The assertion passed independently and in the focused tournament-duels suite. No production Telegram messages were sent and no service was deployed.
