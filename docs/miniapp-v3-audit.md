# CMU ELITE Mini App 3.0 audit

## Main integration

The first audit below describes the starting checkout at `89e4e87`. The upgrade was then integrated on `57fcf50`, which contains a newer Mini App blueprint, Community Arena and XP, World T20 Championship updates, durable wallet and pack quotes, and archived Super Over commentary. The integrated code preserves those screens and APIs. See the implementation report for the current route map.

## Starting point

The local `work` checkout renders the collecting game from `GET /webapp` in `admin.py` and `templates/webapp.html`. This branch has no `PRODUCT.md`, `DESIGN.md`, `.impeccable`, `_miniapp_home.html`, `_miniapp_icons.html`, `webapp_community.html`, or standalone overview/theme CSS or JS listed in the master brief. `website.py` serves the standalone franchise site and registers shared auction routes. It does not render the collecting game homepage.

Live match links and the home resume action lead to `GET /cricket`, backed by `static/cricket/index.html`, `app.js`, `approach.js`, `live-batting.js` and `style.css`. The inline live renderer in `webapp.html` is legacy. The shared scorecard drawer in that template is active for direct scorecard and tournament links. The bot also issues a dedicated tournament-only link and a standalone franchise link. Their independent auth and entry paths matter for users without a collecting-game account.

## Active surfaces and contracts

| Surface | Existing routes and API |
| --- | --- |
| Clubhouse and debut | `/webapp`, `POST /api/webapp/init`. Server supplies account balances, match record, eligible resume data, debut status, reward quotas, season data, news and tournament summary. |
| Profile, wallet and gift/match history | `profile`, `wallet`, `matchcentre`, `rewards`; `/api/webapp/profile`, `/profile/photo` and `/api/webapp/clubhouse/{wallet,matches,rewards}` plus scoped gift and giveaway actions. |
| Squad and player catalog | `roster`, `xi`, `market`, `browse`, `playerdetail`; `/api/webapp/{roster,xi,market,players,search,player-detail}` plus existing player and resource actions. |
| Play | `match` for phase-based Quick Match, `career` for Career Player progression. `/api/webapp/quickmatch/{quota,start,phase,abandon}` and `/api/webapp/career`. Group-launched matches live in `/cricket` and use authenticated `/api/match` state/mutations. |
| Competitions | `tournament`, `auction`, `franchisedraft`, `leaderboard`, `season`. Directory/fixtures/table/team/stats/scorecard use `/api/webapp/tournament` and its subpaths. Auction and franchise draft use separate existing modules and role-checked routes. |
| Rewards | `daily`, `spin`, `freepack`, `packs`, `quests`, `achievements`, `invite`. Existing init/quota/ad confirmation/receipt flows own eligibility and reward delivery. |
| Community | Existing news, poll, suggestion, official links and `clubs`. Profile and season progression exist. No Community XP or group minigame feed was found in the collecting-game endpoints. |

## UI and compatibility risks

The old shell uses a narrow single-column canvas, six primary destinations and a large grid of equally weighted shortcuts. The template defines the router and most styles inline. Feature modules add more surface, radius and spacing rules after that cascade. The Arena has its own palette, font, storage and match lifecycle. An inline live renderer duplicates parts of its presentation but is bypassed by normal live links. The existing roster, player profile, gift, wallet, tournament and auction code already covers meaningful workflows and can remain the source for those screens.

Preserve signed Telegram initData, group origin forwarding, maintenance and debut gates, auction-only launch, tournament-only launch, match query parameters, player ownership, archived scorecards, action concurrency and existing server-side reward reconciliation. Keep real chart series tied to recorded events. The tournament summary lists active competitions. The full fixture payload gives team ownership, release status and next scheduled fixture, but lacks a per-fixture deadline and authorization to start from this surface. A preview can link to the directory without claiming play eligibility.

## Reference review and migration

Crickium public frontend at `708ff37` was reviewed in its static directory. Its stable loading placeholders, progression and rewards grouping, separate match analysis and transparent lineup states informed the interaction audit. CMU keeps its own visual assets, components and server semantics. No reference code or assets were imported.

Use a shared semantic palette and appearance controller. Keep the existing route IDs and feature controllers under one tab shell, add focused destination hubs, and move the Clubhouse to a single contextual action with real competition and reward summaries. Apply Arena work at `/cricket`, where match status, tactical sheets, scorecards and the event feed already live. Validate with an isolated SQLite fixture, browser captures and existing tests before release. See the implementation report for QA results and remaining contract gaps.

## Baseline evidence

Chromium captured the authenticated layout through a temporary local Flask test instance. At 390px, the free-pack popup interrupts the opening view and covers the primary flow. At 1280px, the same experience stays inside a 480px column with large unused margins. Captures of the authenticated example are in the workspace's shared `ccm26-v3` directory.

The full pytest run started before editing and reported 8,008 passed, 74 failed, 22 errors, 2 skipped and 3,330 passing subtests. This run overlapped implementation, so use it as the initial repository health check rather than an immutable baseline. The two focused failures observed during implementation involved outdated Node test fixtures for tournament and authentication source extraction. Both checks pass with the final scripts.
