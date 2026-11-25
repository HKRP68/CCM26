# CMU ELITE Mini App 3.0

## Integrated build

The upgrade was integrated on top of `57fcf50`. Main had already added the Mini App blueprint, durable economy quotes, Community Arena, World T20 Championship changes and archived Super Over commentary. The integrated build keeps those contracts and adds the Elite layout, navigation and recovery improvements.

| Surface | Implementation |
| --- | --- |
| Shell | Home, Squad, Play, Compete and Rewards, a desktop sidebar, browser and Telegram back navigation, appearance and haptic preferences, and shared safe-area tokens. Explore still holds settings, wallet, traits, news and community access. |
| Clubhouse | Obsidian Gold, original cricket artwork, club identity, scoped record, recent match, balances and existing reward and quest panels. The primary action follows live Arena match, active Quick Match, eligible timed fixture, XI setup and Play. |
| Play | Quick Match restores the current phase after browser reload and checks the authoritative state when a submission response is uncertain. Existing AI caps, toss, tactics, rewards and limits continue to apply. |
| Match Centre | Sticky score and chase equation, read-only spectator view, separate Scorecard, Analysis and Info tabs, and recorded progression and overs. The stored result supplies the winner. The existing dark scorecard and detailed archived Super Over innings, turning points and delivery commentary remain available. |
| Squad | Market, catalog and captain tools remain reachable from Squad. The catalog supports variant filters, rating sort, pagination, list/card views and comparison. XI keeps explicit Save and captain and vice-captain markers, with named keyboard move controls. |
| Compete | Existing tournament registration, fixtures, standings and archives, plus an eligible fixture preview and ranked and season access. Franchise Auction and Draft open the configured standalone franchise site or the existing Telegram bot. |
| Rewards and Community | The Rewards hub links to the existing claims, pack history and shop. Pack confirms and reveals use the durable quote workflow on main. The separate Community Arena and XP page is linked from Explore. |

## Implementation and compatibility

- `templates/webapp.html` remains the dashboard and coordinator. `miniapp_blueprint.css` contains shell and catalog presentation, `miniapp_tokens.css` shares the theme palette with `/cricket`, and `miniapp_theme.js` retains `cmu-appearance` and honors the old Arena preference as a fallback. Existing asset and API endpoints for the standalone franchise and Community Arena are unchanged.
- `/api/webapp/init` adds `quick_match_active` and an eligible `tournament.my_fixture`. The deadline is calculated from the current released round or batch. The UI displays the tournament's existing IST label and opens the fixture list.
- Quick Match `quota` includes the account's current server snapshot and the behavior for a recently completed result. `start` accepts `resume_existing`. `phase` accepts the expected match/innings/phase tuple and rejects stale actions without advancing the match. Previous client request fields remain valid.
- `/api/webapp/players` adds optional `version` and `sort` alongside existing numeric or all-results paging. `countries` adds the available variants. Catalog prices and actions still go through the existing economy reviews.
- `GET /api/match` retains the existing state and action vocabulary and adds recorded venue, wicket limit, known extras and wicket falls. The result view displays coins only when the match state records the current viewer's award. Archived scorecards keep their original data and methods.
- Bot commands, signed Telegram auth, quote transactions, match simulation, ratings, approach rules, prices, reward amounts, memberships and tournament rules keep the same server authority.

| Destination | Route and link map |
| --- | --- |
| Home | `home`, `profile`, `wallet`, `explore`, `appearance`, notifications, `news_<id>`, `poll_<id>` and `/webapp/community` |
| Squad | `roster`, `xi`, `browse`, `market`, `captains`, `traits`, `playerdetail` and existing aliases |
| Play | `matchcentre`, `match`, `career`, match and scorecard deep links, and `/cricket?match_id=…` |
| Compete | `compete`, `tournament`, `leaderboard`, `season`, franchise website/Telegram handoff and legacy auction launch hints |
| Rewards | `rewards`, `daily`, `spin`, `freepack`, `packs`, `quests`, `achievements` and `invite` |

## Verification and rollout

The integrated checkout passed 195 focused tests and 248 subtests. The Community Arena suite passed its 26 tests in a separate run. Combining that suite with legacy suites that reload the model modules produced eight ambiguous-column failures. The browser smoke covered 320, 390, 768 and 1280 CSS pixels, fixture navigation, browser back, catalog comparison, Community Arena entry, Quick Match resume, chase analysis, spectator mode and the archived Super Over.

The older checkout passed 322 focused tests and 278 subtests. Its isolated 390×844 benchmark at 4× CPU slowdown and 1.6 Mbps download measured 5.18s median LCP, 0.061 layout shift and 79,113 initially requested local JS/CSS bytes. These figures predate integration with main and should not be treated as measurements of the integrated layout.

Use a fresh temporary SQLite database and Flask testing mode to reproduce the browser run. Development authentication is disabled unless testing mode and `WEBAPP_DEV_MODE=1` are both enabled.

```sh
CCM26_CAPTURE_DB=$(mktemp /tmp/ccm26-capture.XXXXXX.db)
export DATABASE_URL="sqlite:///$CCM26_CAPTURE_DB"
export BOT_TOKEN=123456789:LOCAL_VISUAL_CAPTURE_ONLY
export WEBAPP_DEV_MODE=1
export WEBAPP_URL=
python - <<'PY'
from admin import app
from database import get_session, init_db
from models import User

init_db()
with get_session() as db:
    db.add(User(telegram_id=30001, first_name="Jordan", team_name="Harbour Cricket Club"))
    db.commit()
app.config.update(TESTING=True)
app.run(host="127.0.0.1", port=5088, use_reloader=False)
PY
```

Open `http://127.0.0.1:5088/webapp?dev=30001`. Use Explore to change appearance. The shared workspace screenshots labeled `main-` are from the integrated build. The earlier captures and benchmark reflect the starting checkout and initial implementation.

The Elite change has no schema migration of its own. Ship its Python, Jinja, CSS, JS and artwork files together. To roll it back while preserving the preceding changes in main, revert the Elite integration commit as one revision.

Quick Match resume is limited to the existing process-local retention window. Real Telegram WebViews, live ads, real auction bids and production network performance require staging verification. Additional lineups, campaign mechanics and historical analysis depend on existing server data and contracts.
