# Events Studio operations

## Staging and database

Run the repository's existing database bootstrap against a backup or disposable staging database. It registers nine additive tables prefixed `studio_event`. The CIPL addendum adds `studio_event_enrollments.match_setup` for pending tosses and `studio_event_games.next_pick_at` with an index for batter and innings-break timers. The bot JobQueue sweeps durable game deadlines and event lifecycle. Both services must share the same database and flags. PostgreSQL locks each official game or wallet row during mutation. SQLite uses its writer lock.

The example environment has all switches off. Enable `EVENT_STUDIO_PUBLISH`, the format flag `EVENT_STUDIO_T20` or `EVENT_STUDIO_ODI`, and the required mode flags in staging. ODI also uses the existing `CMU_ODI_APPROACH_ENABLED` launch setting. Mode flags are `EVENT_STUDIO_LIVE`, `EVENT_STUDIO_MOMENTS` and `EVENT_STUDIO_BOSSES`. Removing a launch flag blocks new publishing, registration and starts. Active games and issued claims remain recoverable. Keep `EVENT_STUDIO_HARD_PLUS` off for rollout. It permits publishing when a track or fixture uses Hard++. An event that overrides all effective difficulties to a different level can publish without it.

The bootstrap leaves all legacy rows and columns intact. For a code rollback, disable new starts and preserve the added tables. Retain the release and its ground reference for pinned games and earned claims. Use the Events export for draft transfer. Keep database backups for recovering official state, audit rows and receipts.

## Deadlines and incidents

Official starts require server time strictly before the deadline. A match that started in time can complete at or before the grace cutoff. An unresolved unstarted fixture forfeits after its start deadline. A started fixture forfeits after grace. The same row lock governs expiry and each accepted pick. A winning terminal delivery retains its result when the action was accepted before cutoff. Scheduler retries use a separate clock from the gameplay cutoff. A technical disconnect leaves the same match available for recovery. A player pause leaves the tournament cutoff in effect. An incoming-batter timeout uses `CIPL_TIMEOUT_SECONDS` and sends in the next batter in the saved order.

**Pause official play** records the reason and freezes the event. **Resume** shifts remaining fixtures, enrollment cutoff, and overall close by the paused interval. Published rules and completed results remain in the existing revision. Archive a completed event after play ends. Earned receipts remain accessible. Use the Studio activity panel and worker log to inspect stuck fixtures and reward counts. **Retry failed jobs** clears the fixture backoff without changing its deadline or result. **Extend a personal fixture** moves an unresolved deadline within its event and round windows and records the reason and both timestamps.

Reconcile a reward using the immutable entitlement's enrollment, source, tier, recorded assets, claim time and activity ledger reference. Existing activity-log archival may move the activity record out of the live table. The entitlement stays the permanent receipt. Avoid altering official results, deadlines or ledger balances by hand. A verified execution failure keeps the last checkpoint and records the error type. Resolve an unresolved game only after reviewing its incident. For a Live Event, the operation records a no-result. For a standalone challenge, it records a technical void and restores the attempt allowance. Cancel an event with a reason and explicit confirmation to close unresolved games. This is a free-entry feature. Completed results and existing claims survive cancellation. Compensating financial corrections remain pending.

Fixture reminder delivery is opt-in per event and uses the account's existing notification preferences and quiet-hours policy. The default authored offsets are six hours and one hour before the fixture's start deadline. Each notification has a durable lease and receipt. Transport errors retry up to three times, and a lost Telegram acknowledgement can result in a repeated notification. Finished and already-started fixtures do not send start reminders.

## Qualification evidence

The final main-based smoke calibration used synthetic XIs with matched seeds and surfaces. The T20 smoke covered balanced and aggressive opposition strategies. The ODI smoke also included a defensive strategy. Each policy recorded zero consecutive-over or quota violations. The sample did not establish an improvement for Hard++; the Own Roster publication switch stays off by default.

| Format and policy | Paired matches | AI wins | Guest wins | Over p50 | Over p95 |
|---|---:|---:|---:|---:|---:|
| T20 Hard | 18 | 17 | 1 | 58.01 ms | 180.51 ms |
| T20 Hard++ | 18 | 15 | 3 | 71.47 ms | 290.94 ms |
| ODI Hard | 54 | 38 | 16 | 2.30 ms | 6.79 ms |
| ODI Hard++ | 54 | 38 | 16 | 2.13 ms | 3.77 ms |

An earlier 108-pair T20 run on the older workspace base found 96 AI wins under each policy. It used the prototype before the port removed identity-derived matchup adjustments. Use `python -m tools.calibrate_event_studio --format T20 --pairs 18` or `--format ODI --pairs 54` to repeat the smoke runs. Broader adversary coverage and the proposed production calibration remain pending.

The final PostgreSQL 16 load test used a disposable local schema with 500 enrollments and 60 fixtures per campaign. It completed 24 AI simulations across 110 durable batches of up to eight overs and ran 20 competing claims for one synthetic earned entitlement. The claims produced one credit and one ledger receipt.

| Measured operation | p50 | p95 | SQL statements p50 / p95 |
|---|---:|---:|---:|
| Enrollment, four workers | 58.96 ms | 105.32 ms | 8 / 8 |
| AI simulation batch, eight workers | 266.38 ms | 568.16 ms | 6 / 8 |
| Claim against the same wallet | 25.16 ms | 156.08 ms | 4 / 4 |

An earlier run that simulated whole AI matches in one transaction had a 41.18 s p95. The worker now persists each batch, yields to other due fixtures and prioritizes personal deadlines. The final figure measures batch latency. Time to finish every AI result in a campaign still depends on the worker cadence and competing jobs.

Super Overs, claim-expiry and grace notices, and community-goal reward grants remain pending. The Community XP outbox supports eligible completed play and the Boss Battle meter counts distinct official winners.

The focused repository run passed 413 tests and 42 subtests. The PostgreSQL and ODI run passed 93 tests, with Events Studio transactions exercising an isolated PostgreSQL 16 schema. Chromium passed the admin draft validation and publishing flow and the player enrollment, keyboard play and scorecard flow at 390 px and 1440 px. The whole-repository run on the updated base did not complete.

## CIPL gameplay addendum verification

Events and ordinary CIPL share the pick validator, over engine and Arena viewer. Event transitions and finalization lock the official fixture checkpoint. Official results write to the event ledger and progression tables.

```bash
.venv/bin/python -m pytest -q tests/test_event_studio.py tests/test_cipl*.py tests/test_bot_captain.py tests/test_bot_tactics.py tests/test_approach_miniapp_play.py tests/test_odi_approach.py tests/test_live_batting*.py tests/test_realism*.py
CCM26_EVENT_TEST_POSTGRES=postgresql+psycopg2://localhost/test_db .venv/bin/python -m pytest -q tests/test_event_studio.py tests/test_odi_approach.py
.venv/bin/python -m tools.calibrate_event_studio --pairs 18
```

The first command passed 752 tests and 22 subtests. The PostgreSQL and ODI command passed 103 tests using disposable PostgreSQL 16 schemas. The additive migration passed twice against a simulated pre-addendum schema on SQLite and PostgreSQL. Chromium verified the shared batting and bowling controls at 390 px and 1440 px, Arena playback, pause and reload, and the Events Hub toss, quick XI, captain selection and confirmed forfeit flow. Telegram rendering and pick tests used local fixtures.

The stage-based paired T20 smoke produced 18 AI wins under Hard and 17 under Hard++, with zero bowling allocation violations. Hard++ remains gated for staging. Published competition rules settle T20 and ODI ties as ties. The existing interactive Super Over flow supports two human captains. Event Super Overs and practice replays remain pending.

The combined CIPL, CIPLbot, AI, Approach, MiniApp, ODI, realism, tournament and reward regression completed with 1,652 passes and 112 subtest passes. Its nine failures also reproduced on unchanged `f207918`. The affected tests cover the bot command menu, Clubhouse fixture visibility, tournament duels and presentation, and admin rewards and prize retries.

```bash
mapfile -t tests < <(rg --files tests | rg '/test_((cipl|approach|bot_captain|bot_tactics|odi_approach|live_batting|lp_tournament|league_tournament|parallel_tournaments|super_over_miniapp|webapp_tournament|tournament|miniapp|reward|ad_reward|admin_bulk_rewards|login_reward|event_studio|bot_menu_commands|realism).*)\.py$' | sort)
.venv/bin/python -m pytest -q --tb=short "${tests[@]}"
```
