# Live Event upgrades

See the [Phase 2 audit and completion report](live-events-phase-2-audit.md) for the current formats, inventory rewards, gameplay settings, and verification.

These upgrades use the Events Studio and shared Solo and Multiplayer fixtures from CCM26 main. Configure the features in an unpublished tournament-scoped Live Event. Existing published revisions keep their original policy.

## Publish a new event

1. Keep the existing Events Studio rollout flags for the formats and modes you intend to publish. Multiplayer requires `EVENT_STUDIO_MULTIPLAYER=1`; Multiplayer ODI also requires the qualified ODI adapter and `EVENT_STUDIO_MULTIPLAYER_ODI=1`.
2. Deploy the bot and authenticated admin app together. Their existing database initialization adds the lobby policy, team performance, manager ledger, honour snapshot, group notice and qualification job tables. Back up the database using the existing deployment procedure.
3. In Events Studio, add fixed squads and generate fixtures. Select the tournament features you want. For an Event Pass, set the human participation cap, wallet budget, match-reward toggle and claim window. Generate rewards or author milestone bundles in the editor. Validate and review the liability before publishing.
4. Check the player preview and try one official fixture, a star claim and a wallet claim. Enable group notices only for events intended to post to their hosting group. The existing bot worker processes qualification estimates, notices and Community XP.

Example fragment for a draft. The values are illustrative and require configured objectives, capacity, schedule and budget.

```json
{
  "objective_scope": "tournament",
  "competition": {
    "event_pass": true,
    "match_rewards": false,
    "lobby_v2": true,
    "dressing_room": true,
    "news": true,
    "manager_profile": true,
    "competitive_rating": false,
    "manager_xp_cap": 500,
    "qualification": true,
    "predictor_samples": 400,
    "honour_board": true,
    "group_engagement": false
  },
  "milestones": [
    {"id": "bronze", "stars": 4, "free": {"coins": 200}, "diamond": {"coins": 400}, "locked": true}
  ]
}
```

Wallet entitlements unlock after explicit star claims. A Diamond bundle replaces the Free bundle for that milestone. Tier and expiry are captured at unlock and retained by the entitlement. Auto Rewards preserves authored milestones by default and preserves locked milestones during explicit regeneration. Event Pass supports coins, gems and Quest Points. Phase 2 adds existing packs and specific or version-filtered random player cards. Optional milestones may depend on playoff opportunities.

## Player routes

- `/event`, `/hotevents`, `/eventjoin [code]`, `/eventroom` and `/eventlobby [code]` open published events and lobbies.
- `/myevent`, `/myevents`, `/eventplay [match ID]`, `/eventfixtures`, `/eventtable` and `/eventstats` open joined events and official matches.
- `/eventobjectives`, `/eventclaim`, `/eventpass` and `/eventrewards` open objectives and rewards. Make claims in private chat or the MiniApp.
- `/eventqualification`, `/eventnews`, `/eventhistory`, `/eventawards`, `/trophies`, and `/manager` open enabled event views. Use `/eventhelp` for the main routes.

The signed MiniApp uses the existing Events Hub API. Lobby listing and actions use `POST /api/webapp/events-hub/lobby`. Event creation and claims use `POST /api/webapp/events-hub/competitions/<event_id>`. Event details and paginated news use `POST /api/webapp/events-hub/<event_id>`.

## Operating limits

- Dressing Room workload uses balls faced and deliveries bowled with a 24-hour half-life. Phase 2 adds opt-in bounded ratings for Solo fixtures. Multiplayer stays informational.
- Manager XP is separate from Community XP. The opt-in Elo ladder groups matches by format and by the ten-point OVR band of the published default XIs. Only same-band official PvP matches can affect rating.
- Qualification guarantees use conservative points bounds and the canonical table for completed leagues. Optional estimates use a neutral model. Future points-and-wins ties use random order as an approximation for unknown NRR.
- The news feed projects stable articles from official results. The Honour Board snapshots finalists, champions and player awards. The worker generates approved award graphics.
- Schedule templates available in this phase are Weekend Championship, Nations Cup, Legends League, Rivalry Cup and Road to Final. The last four reuse canonical schedules and explicitly authored squads. Weekend Championship validates its rest and event window. Phase 2 adds Mystery, Champions League eligibility, Double Elimination, World Tour, Dynasty, Underdog Championship and the Survival League template.
- Group notices cover lobby start and cancellation, eligible PvP deadline reminders and tournament completion. Telegram can deliver a duplicate if it accepts a send and its acknowledgement is lost. Retry limits and per-group rate limits apply.

## Files and schema

| Area | Files |
| --- | --- |
| Storage | `models_events.py` adds `studio_lobby_policies`, `studio_team_performance`, `studio_manager_entries`, `studio_honour_snapshots`, `studio_group_notices` and `studio_qualification_jobs`. |
| Event configuration and settlement | `services/event_studio.py`, `services/event_studio_rules.py`, `services/event_studio_competition.py`, `services/event_studio_pass.py` and `services/event_studio_schedule.py`. |
| Projections and delivery | `services/event_studio_insights.py`, `services/event_studio_manager.py`, `services/event_studio_predictor.py`, `services/event_studio_group.py` and `services/event_studio_worker.py`. |
| Authenticated admin and MiniApp | `services/event_studio_admin.py`, `templates/admin_event_studio.html`, `static/js/event_studio_admin.js` and `static/js/events_hub.js`. |
| Telegram | `bot.py`, `handlers/event_competitions.py`, `handlers/event_studio.py` and `services/command_catalog.py`. |
| Tests | `tests/test_live_event_expansion.py` and `tests/test_bot_menu_commands.py`. |

## Rollback

Leave the new tables and wallet receipts in place. Turn off the applicable feature settings on new drafts. Published policies are pinned. Keep the upgraded claim path running until issued Event Pass entitlements have expired before reverting the application code, or pause new enrollments and continue serving claims from the upgraded deployment. A database downgrade is unnecessary.
