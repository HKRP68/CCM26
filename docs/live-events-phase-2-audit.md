# Live Events Phase 2 audit

Baseline is CCM26 at `f783d57` with the Phase 1 working-tree changes. Status refers to the runtime path. The previous suite recorded 655 tests and 105 subtests passing, including canonical Events, reward, CIPL and RCPL tests. PostgreSQL concurrency and Chromium were exercised separately. These results do not establish coverage for missing features.

| Prompt | Starting status | Requirement audit |
|---|---|---|
| 01 Progressive Objectives | Partial | **Implemented:** canonical completed-result contributions, PvP both sides, explicit and idempotent star claims, typed metric allowlist, per-team fixture counts, rate filters and single-target generation, both clients. **Partial:** flat weighted objectives can represent each threshold independently, but grouping, editing, importing and generating real ladders are absent from the active Studio path. The standalone ladder files are unused by the runtime. **Missing:** audited canonical adjustments, milestone-level generator locks and comparable completion estimates in the live editor. |
| 02 Event Pass | Partial | **Implemented:** claimed-star thresholds, independent Free/Diamond bundles, eligibility-time tier, deadline, wallet liability, locks, claims, wallet receipts and both clients. **Missing:** pack and player assets, inventory liability, audited inventory grants and their reveal paths. |
| 03 Franchise Objectives | Missing, previously excluded | **Missing:** franchise variants, parity/tier comparisons, franchise-specific generation and editing. The previous request excluded implementation. |
| 04 Underdog Bonus | Missing, previously excluded | **Missing:** pinned strength band bonus, objective-star adjustment, cap and accounting. Underdog Championship is a separate schedule template. |
| 05 Dressing Room | Partial | **Implemented:** completed-result appearances, neutral morale, recent form, XI overlap, workload, captain appearance counts, displays. **Partial:** informational leader/rotation cues and no persistent historical fan/reputation totals. **Missing:** bounded fixture effects and calibration. |
| 06 Lobby 2.0 | Partial | **Implemented:** canonical reservations, visibility, expiring hashed invite, readiness, consent-based transfer, AI fill, host controls, versioned polling, retries and seat/start race coverage. **Partial:** host can transfer or cancel, with no configurable automatic transfer. **Missing:** configurable automatic full/start policies, richer seat notifications and some recovery tests. |
| 07 News Centre | Partial | **Implemented:** deterministic canonical reports, centuries, five-fors and broken batting records, stable IDs, both clients. **Missing:** other requested article types, admin feed, publication/delivery auditing and graphics. |
| 08 Manager Profile | Partial | **Implemented:** separate capped XP and fixed ranks, match/claim/completion/title sources, Solo/Multi and format splits, zero-sum same-band Elo, provisional samples, private profile. **Missing:** configurable XP weights/ranks, collusion review, adjustment workflow and player-facing event history details. |
| 09 Qualification | Partial | **Implemented:** canonical standings, conservative points guarantees, small schedule enumeration, seeded cached neutral estimates and uncertainty. **Partial:** group/rank slot support with unsupported structures hidden. **Missing:** personalized scenario selection, calibrated pitch/strength priors and detailed predictor diagnostics. |
| 10 Honour Board | Partial | **Implemented:** immutable final snapshots, winner, shared run and wicket awards, rates, tally records and human trophy archive. **Missing:** runner-up, captain, MVP, final player, team of tournament, graphic jobs and Collection Book link. |
| 11 Formats | Partial | **Implemented:** Weekend Championship, Nations/Legends/Rivalry templates, and Solo Survival/Road to Final. **Partial:** template squad restrictions are authored using the existing roster rules. **Missing:** Mystery, Double Elimination, Champions, Underdog, World Tour and Dynasty. |
| 12 Group Engagement | Partial | **Implemented:** hosted-group start/cancel/completion and PvP deadline notices, durable dedupe/lease and per-group limits, Community XP with existing caps. **Missing:** join digests, qualification and performance notices, per-group subscriptions/mutes and admin targeted retries. |
| 13 Telegram/MiniApp | Partial | **Implemented:** shared signed API, aliases, authorized joined-event picker, private claims, opaque callback owners, stale lobby refresh, responsive Event Hub. **Partial:** unsupported optional views and limited history navigation. **Missing:** comprehensive integration coverage for the new Phase 2 features. |

## Phase 2 completion report

Prompt 03 and Prompt 04 remain excluded under the original rollout scope. Their absence does not indicate a failing implementation. Underdog Championship uses authored eligibility and does not change a manager's stars.

| Prompt | Phase 2 status and completed requirements | Changed files and migration | Admin configuration and clients | Remaining limitations |
|---|---|---|---|---|
| 01 | **Partial.** Versioned Bronze-to-Diamond ladders now settle against the official contribution ledger. Independent entitlements and multi-threshold claims use the existing receipts. Auto generation honors league opportunities, star limits, stage count, presets, and locked stages. | `event_studio_objectives.py`, `event_objective_rules.py`, `event_objective_generator.py`, `event_studio.py`, `event_studio_rules.py`, `event_studio_competition.py`. No new tables. | `objective_ladders`, `ladder_generator`, Auto Objectives, `/eventobjectives`, signed event detail and claims. | Stage editing is JSON-based. A measured strength-band dataset and audited correction workflow are pending. |
| 02 | **Implemented for wallet, packs and player cards.** Exact and version-filtered random cards, existing pack types, separate tier bundles, publish-time card pools, deterministic draws, capacity checks, budget preview, Auto Rewards, and atomic item receipts. | `event_studio_items.py`, `event_studio_pass.py`, `event_studio_competition.py`, `event_studio_admin.py`, `models_events.py`. Adds `studio_reward_items`. | `pass_inventory`, `free_items`, `diamond_items`, `/eventpass`, `/eventclaim`, `/openpack`, signed competition claims and existing pack screen. | Pack definitions retain existing live catalog behavior on opening. Claims blocked by capacity or by an unavailable catalog item remain unclaimed. Cosmetics and trophies are separate from inventory claims. |
| 03 | **Missing, excluded.** No Phase 2 changes. | None. | None. | Franchise-specific objectives remain outside the requested rollout. |
| 04 | **Missing, excluded.** No Phase 2 changes. | None. | None. | Underdog star bonuses remain outside the requested rollout. |
| 05 | **Implemented for opt-in Solo, partial against original management spec.** Published cap and weights drive bounded engine copy adjustments for morale, unity, form, workload and captain impact. Added per-instance reputation and fan metrics. | `event_studio_dressing.py`, `event_studio_insights.py`, `event_studio_competition.py`, `event_studio.py`. Uses `studio_team_performance`. | `competition.dressing_room`, `management_cap`, `management_weights`, Telegram Squad and MiniApp Dressing Room. | Multi is informational. The smoke sample is not production calibration. Long-term franchise aggregation and detailed rotation suggestions are pending. |
| 06 | **Partial.** Preserves prior authorization, ready/consented transfer, reservation, expiry and AI fill. Full-room auto-start is independently configurable. Mystery uses consent-based seats. | `event_studio_competition.py`, `event_studio.py`, `handlers/event_competitions.py`, `events_hub.js`. No new tables. | `competition.auto_start_full`, `/eventroom`, `/eventjoin`, signed lobby actions. | Configurable host-departure policy and complete lobby reminder/digest coverage are pending. |
| 07 | **Partial.** Official reports, last-over close finishes, major upsets, centuries, five-fors, legal-ball hat-tricks, batting and bowling records, streaks, qualification/elimination, final lineups, and champion stories. Shared deterministic story IDs and admin feed. | `event_studio_cricket.py`, `event_studio_insights.py`, `event_studio_competition.py`, `event_studio_admin.py`. Canonical result JSON gains optional news facts. | `competition.news`, `news_upset_gap`, `/eventnews`, News in event detail, authenticated Studio feed. | The feed is projected from durable results. Persisted editorial/correction workflows, all article filters, and automatic graphic delivery for each article are pending. |
| 08 | **Partial.** Pinned per-source XP weights now complement capped XP, career statistics, and same-band PvP Elo. | `event_studio_manager.py`, `event_studio.py`, `event_studio_competition.py`. Uses `studio_manager_entries`. | `manager_xp_weights`, `manager_xp_cap`, `rating_k`, `/manager`, Manager Profile in event detail. | Global rank thresholds remain versioned constants. Collusion review, rating adjustment/reset workflow and leaderboard diagnostics are pending. |
| 09 | **Partial.** Kept canonical points bounds, small-schedule proof and revision-keyed advisory estimator. News uses final qualification status. Both clients show what winning all remaining league matches would mean for the manager's playoff status. | `event_studio_insights.py`, `event_studio_predictor.py`. Uses `studio_qualification_jobs`. | `competition.qualification`, `predictor_samples`, `/eventqualification`, signed event detail. | Predictions for unsupported brackets, calibrated distributions and interactive custom scenario editing are pending. |
| 10 | **Implemented for the requested awards, partial against original ceremony spec.** Champion, runner-up, Orange/Purple Caps, tournament and final MVP, winning captain, boundaries and rate awards, Team of the Tournament. Reuses Challenge League impact scoring, XI roles and graphic frame. Added recoverable award graphics and the Events Hub Collection Book. | `event_studio_insights.py`, `event_studio_award_media.py`, `tournament_ceremony.py`, `event_studio_worker.py`, `event_studio_admin.py`, `models_events.py`. Adds `studio_award_media`. | `competition.honour_board`, `/eventhistory`, `/eventawards`, `/trophies`, signed `POST /api/webapp/events-hub/honours/<event_id>`, authenticated award PNGs. | Gifts are not attached to player awards. Fastest milestone awards, superseding correction snapshots, and full automated Telegram photo ceremonies are pending. A complete XI graphic needs eleven available cards. |
| 11 | **Partial against original format specification.** Existing Weekend, Nations, Legends, Rivalry, and Survival/Road to the Final are reused. Added seeded Mystery enrollment, upper/lower double-elimination brackets with optional reset, Champions-only/author-invited entry, Underdog templates, Dynasty links and country-series World Tour. | `event_studio_schedule.py`, `event_studio_brackets.py`, `event_studio_progress.py`, `event_studio_competition.py`, `event_studio_manager.py`. Adds no tables. Conditions, teams and seed are pinned in published revisions. | `schedule.template`, `grand_final_reset`, `previous_event_id`, `tour`, Auto Schedule, `/eventjoin`, `/eventfixtures`, signed competition/lobby actions. | Survival/Road to the Final stay Solo. Dynasty validates franchise identity and shows the manager’s linked editions with verified wins and titles. Rosters, wallet and gameplay effects do not carry across editions. Complex graph editing and exhaustive format/eligibility recovery tests are pending. |
| 12 | **Partial.** Existing group notice queue now includes verified landmark match highlights and a durable league qualification digest. Existing Community XP remains separate. | `event_studio_competition.py`, `event_studio_group.py`, `event_studio_worker.py`. Uses `studio_group_notices`, `studio_competition_xp`. | `competition.group_engagement`, existing `notifications` and `community` options. | Group subscriptions, mutes, joins/digests, admin targeted retries, and notification preference tooling are pending. A transport acknowledgement loss can still result in retry delivery. |
| 13 | **Partial.** Mystery consent, item receipt and existing pack navigation, News, source fixtures, awards graphic endpoints, Trophy Collection Book, and World Tour series integrated in existing clients. | `handlers/event_competitions.py`, `services/event_studio_admin.py`, `static/js/events_hub.js`, `templates/admin_event_studio.html`, `tests/test_live_events_phase2.py`. No new routing middleware. | Existing Event Studio and signed Event Hub APIs. New award image routes as above. | No live Telegram deployment or live-device test. Advanced bracket graphical interaction and full end-to-end tests of every config combination are pending. |

No new environment variables. Existing `EVENT_STUDIO_PUBLISH`, format, Live Events, Multiplayer and Multiplayer ODI gates still apply. The two new tables are additive and are created by the existing database initializer. Existing snapshots, balances, inventory and receipts are retained.

### Configuration examples

```json
{
  "competition": {
    "event_pass": true,
    "dressing_room": true,
    "management_cap": 1,
    "news": true,
    "honour_board": true
  },
  "pass_inventory": {"packs": 400, "cards": 400},
  "milestones": [
    {
      "id": "weekend_chest",
      "stars": 4,
      "free_items": [{"kind": "pack", "pack_id": 1}],
      "diamond_items": [
        {"kind": "pack", "pack_id": 1},
        {"kind": "random_player", "min_rating": 82, "max_rating": 86, "version": "Base"}
      ]
    }
  ]
}
```

Set both `schedule.template` and `schedule.preset` to `"double_elimination"` for the two-loss bracket. Use `grand_final_reset: true` to reserve the conditional fixture. Set `template: "world_tour"` with `preset: "series"` and `tour: [{"country":"India","games":3},{"country":"Australia","games":3}]` for a bilateral tour. Scores and the champion follow the count of won series, then the ordinary competition tiebreaks. World Tour requires a Stadium Data venue in each country.

For inventory grants, an author supplies existing pack or card IDs. Maximum liability multiplies the larger track grant at every threshold by the event-wide manager capacity. Full roster and pack limits leave the affected entitlement unclaimed. Pack opening uses the existing inventory, pack score and reveal path.

### Deployment and rollback

Deploy the bot worker and web process together and run the current database initialization. Validate drafts in Event Studio before publishing. Use a new unpublished test event for each new schedule, inventory policy, or Solo rating cap. Keep the approved runtime serving already-published templates and claims. To stop rollout, disable new drafts and pause enrollment as needed. Retain additive tables and use the Phase 2 claim path until issued items have been claimed or their deadlines pass.

### Tests executed

- Broad SQLite/default regression command included Events Studio, Live Events, RCPL, CIPL, ODI, rewards, packs, Challenge League awards, Community Arena and Telegram menu tests. It passed **838 tests and 105 subtests**.

  ```bash
  .venv/bin/python -m pytest -q tests/test_event_*.py tests/test_live_event_expansion.py tests/test_live_events_phase2.py tests/test_rcpl_multiplayer_flow.py tests/test_auction_league*.py tests/test_cipl*.py tests/test_odi_approach.py tests/test_*reward*.py tests/test_*pack*.py tests/test_tournament_award*.py tests/test_community_arena.py tests/test_bot_menu_commands.py
  ```

- PostgreSQL 16 verification passed **14 tests**, covering concurrent inventory claims, shared lobby and claim races, notice leasing, bracket reset paths, durable awards, World Tour and Solo adapter launches.

  ```bash
  CCM26_EVENT_TEST_POSTGRES=<disposable-postgresql-url> .venv/bin/python -m pytest -q tests/test_live_events_phase2.py tests/test_event_competitions.py tests/test_live_event_expansion.py -k 'phase2 or race or lease'
  ```

- The Phase 2 and authenticated Telegram/API subset passed **15 tests**, including the two-process SQLite restart reconstruction, signed wallet and item claims, unauthorized art access rejection, authenticated Studio graphics, and the MiniApp pack-navigation flow at 390 px and 1280 px.

  ```bash
  .venv/bin/python -m pytest -q tests/test_live_events_phase2.py tests/test_event_competitions.py -k 'phase2 or authenticated or telegram'
  ```

- A paired engine smoke test ran twelve seeds in each format using the same XIs and toss order before and after the Solo cap. In T20 the favoured side went from 8 to 9 wins and the mean total went from 308.00 to 299.33 runs. In ODI it went from 5 to 6 wins, with one tie under each policy, and the mean total went from 536.58 to 538.08 runs. The histories were synthetic. The sample verifies that both adapters apply the small adjustment and gives no estimate of production fairness.

- The final Phase 2 file passed **14 tests**, including Dynasty linked history and empty initial stars. A final targeted PostgreSQL 16 run passed the Dynasty and inventory-claim regressions (**2 tests**).

  ```bash
  .venv/bin/python -m pytest -q tests/test_live_events_phase2.py
  CCM26_EVENT_TEST_POSTGRES=<disposable-postgresql-url> .venv/bin/python -m pytest -q tests/test_live_events_phase2.py -k 'dynasty or reward_claim'
  ```

| Prompt | Verification coverage |
|---|---|
| 01 | Official fixture crossing independent thresholds, claim receipts, versioned Auto Objectives and World Tour fixture counts. |
| 02 | Two concurrent requests on SQLite and PostgreSQL, exact and random versioned card grants, full roster rollback, real pack opening, inventory cap, signed claims, both Chromium widths. |
| 03 | Excluded from implementation and testing. |
| 04 | Excluded from implementation and testing. |
| 05 | Production launch path in T20 and ODI, pinned engine-copy differences, cap and printed-rating assertions, seeded sensitivity smoke test. |
| 06 | Existing seat/start races, host consent, private access, and Mystery non-repeatable seat allocation in the same lobby. |
| 07 | Canonical engine output, match report equality for both human participants, clipping of unsupported data, and final awards/news integration. Separate exhaustive golden coverage for all article types is pending. |
| 08 | Existing capped XP, same-band zero-sum rating and idempotent claims. |
| 09 | Cached estimate reproducibility and invalidation, canonical standings, conservative scenario and responsive display paths. |
| 10 | Both double-elimination final paths, snapshot award results, PNG worker recovery, authenticated Flask and admin access, and existing tournament award regressions. |
| 11 | Odd-team two-loss progression, conditional reset and skipped reset, Mystery authorization, published country-series venues, Dynasty history without carried stars. |
| 12 | Existing notice lease races and Community XP suites, plus official result side effects. |
| 13 | Local Telegram handlers, command limits, signed Flask endpoints, actual two-process recovery and Chromium at 390 px and 1280 px. |
