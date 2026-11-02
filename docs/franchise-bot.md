# Dedicated Franchise Auction bot

`python franchise_bot.py` starts **Franchise Auction HQ**, a separate Telegram
bot using your **existing database** and a new BotFather token. It
registers auction, retention, draft, RTM and trade handlers, the guided control
menus and the restart-safe auction scheduler. No website, login widget,
collecting-game account, `/debut` or coin economy is part of this bot.

The application uses the existing auction rules engine so native commands,
button actions, budgets, roster limits and deadlines stay consistent. The
original application's `Procfile`, Docker command and bot service are not the
entry point for this deployment.

## What is better in this bot

- `/auction` is a role-aware workspace with a setup checklist and clear next
  steps, rather than requiring the organiser to remember every command.
- Native buttons open the live room, squads, purses, player pool, retention,
  draft, trades, complete settings and season controls.
- Common settings accept short replies. Role limits, price bands, rating
  requirements and ranges accept readable table formats; custom negotiation
  tables also accept JSON. Every guided money field uses **integer lakh**:
  `100` means ₹1 Cr. The traditional `/bid` command keeps its existing format:
  `/bid 1.5` is ₹1.5 Cr and `/bid 75L` is ₹75 lakh.
- Sensitive manager controls and unfinished forms stay in private chats.
  Bidding buttons stay shared in the auction group. Menus, selected auction
  IDs, roles and pending actions are rechecked against current authority.
- A person can be Bot Owner, Bot Admin, appointed Auction Manager, Team Owner,
  Co-owner and Team Manager at the same time. Team roles apply only to their
  assigned franchises. Only Bot Owners/Admins appoint global Auction Managers.
- Existing auctions, players, franchise assignments, previous squads, retention
  rights and published leagues are available directly, without copying rows.
- **Existing leagues → Next season** carries the league's franchises and
  previous-squad rights into a new auction. **Link previous league** and
  **Previous team** handle existing auctions and renamed franchises.
- CSV import adds to the shared player catalogue and auction pool together.
  Conflicting rows are rejected; repeat imports do not duplicate players.
- Confirmations bind live manager actions to the displayed lot and bid state.
  Concurrent wizard replies cannot submit the same purse change twice.
- Auctions resume from persisted deadlines after a restart. Telegram responses
  do not undo committed bids; existing message throttles protect the group
  against flooding. Exact delivery speed still depends on Telegram and hosting.

## Connect the new bot to the existing database

1. In **@BotFather**, use `/newbot` and choose a new name and username. Store
   that new token directly in the new Render service's environment.
2. Copy the existing bot's PostgreSQL connection URL into `DATABASE_URL` on the
   new Render service. Both services connect to the same database. No export,
   reset or new database is required. An explicit `FRANCHISE_DATABASE_URL`
   overrides `DATABASE_URL` if you already configured it; replace or clear an
   old override so it does not keep pointing at another database.
3. Configure the owner IDs before startup. The owner does not need an existing
   game account. Your Telegram ID can be obtained from your current bot's ID
   command or a Telegram user-ID utility. `/id` in the new bot also shows the
   current user and group IDs after it starts.

| Environment variable | Purpose |
| --- | --- |
| `FRANCHISE_BOT_TOKEN` | New BotFather bot token; required |
| `DATABASE_URL` | Existing bot's database connection URL; required unless overridden |
| `FRANCHISE_DATABASE_URL` | Optional database URL override; blank uses `DATABASE_URL` |
| `FRANCHISE_OWNER_IDS` | Explicit owner Telegram IDs; required |
| `FRANCHISE_ADMIN_IDS` | Optional global administrator IDs |
| `FRANCHISE_BOT_USERNAME` | Optional username; Telegram supplies it at startup |
| `PORT` | Render web-service port for health checks only |
| `FRANCHISE_HEALTH_PORT` | Optional local health port; leave unset for worker mode |
| `FRANCHISE_SOLO_LEAGUE` | Keep `false` for the focused group auction bot |

The dedicated runtime maps only its configured token, database and identities
into the shared services. It clears the former website/Mini App links and
legacy admin aliases. It accepts the shared database but refuses the old bot
token if present in `BOT_TOKEN`: each bot needs its own Telegram identity.
The new bot runs in its own process. Appointed Auction Managers and franchise
roles already stored in the database continue to apply; configure Bot Owner
and Bot Admin environment IDs explicitly for the new service.

## Hand auctions over from the original bot

Only one service should run the auction clock on this shared database:

1. If an auction is live, use `/apause` in its current bot before the handoff.
2. On the **original Render bot service**, set `AUCTION_BOT_ENABLED=false` and
   `FRANCHISE_AUCTION_BOT_USERNAME` to the new bot's username without `@`.
   Redeploy that service and wait for it to finish before starting the new bot.
3. The original bot now skips the auction scheduler and auction focus lock.
   Its auction commands and old auction buttons cannot change records. Private
   or explicitly addressed auction commands point to the new bot; bare group
   bids are ignored so the two bots do not both reply. Other games keep running.
4. Add the new bot to the same auction group, grant pin-message permission,
   and open `/auction` privately. Choose the existing auction and verify its
   squads, purse, retention and settings before resuming in **Control room**.
   The existing group binding is reused. The scheduler replaces the former
   bot's board when it cannot edit it; use the new bot's menus and bid buttons.

The `AUCTION_BOT_ENABLED` switch defaults to `true` on the original service.
It requires a Render environment change and restart there; changing the new
service does not disable the old service remotely. To roll back, pause the
auction, stop the dedicated service, re-enable the original service and resume.

## Render deployment

Deploy a **new service** from this repository with the **Python** runtime:

| Render setting | Value |
| --- | --- |
| Build command | `pip install -r requirements.txt` |
| Start command | `python franchise_bot.py` |
| Python version | `3.11.9` |
| Health check for a Web Service | `/healthz` |
| Environment | Shared `DATABASE_URL` and the dedicated `FRANCHISE_*` variables above |

The included [`render.franchise-bot.yaml`](../render.franchise-bot.yaml) can
create a new Python Web Service through **New → Blueprint**. Choose that file
as its Blueprint path. It creates no database. Configure the original service's
handoff switch separately as described above. Its HTTP server serves only a
small readiness endpoint; there is no browser application. Readiness becomes successful only after Telegram
initialization and the auction scheduler are ready.

For live competitions, an **always-on Background Worker or paid Web Service**
is recommended. A Render Free Web Service can spin down when it receives no
inbound HTTP traffic, even while the bot is polling Telegram. The Blueprint
uses Free so applying its source does not select a paid plan; choose the
appropriate always-on instance yourself before a live auction. With a
Background Worker, no HTTP port or health path is required.

Run exactly **one process for the new token**. Do not launch multiple workers,
Gunicorn processes or replicas of this long-polling bot. A Telegram polling
conflict stops the extra bot process. Startup ensures the existing application's
schema is current and starts only the auction scheduler, rather than the original
games and jobs.

## From retention to published squads

1. Open the new bot privately and use `/start` or `/auction`.
2. Choose an **existing auction** to continue it. For a new season, choose
   **Existing leagues → Next season** to carry last season's franchises and
   squad rights over, or **New auction** to start with an empty field. You can
   also create one with `/anew Premier Season 1` in the competition group.
3. Add the bot to the auction group and grant administrator permission to pin
   its live board. Use `/id` there to obtain the negative chat ID. In private
   controls choose **Bind group**, or use `/bind auction_id` in the target group.
4. In **Teams & roles**, add each franchise and assign its owner. Co-owners
   and Team Managers are optional separate lists. `/id` on a reply identifies
   that Telegram user when available.
5. Open **Retention**. If this auction is not yet linked, use **Link previous
   league** with an ID shown under **Existing leagues**. **Previous team** on
   each franchise handles renamed teams. Negotiate with `/retain`, review
   offers and finalize with `/retainlock` or the retention controls. Set and
   complete expansion picks before starting the auction.
6. In **Player pool → Build rating set**, use players already in the shared
   catalogue. Import is optional: `/sampleplayers` downloads an example CSV;
   replace its rows, use `/importplayers Main` in the group, and upload the CSV
   **as a reply to the import prompt** so Telegram privacy mode delivers it.
7. Open **All settings**. Review purse, squad/overseas/role/rating rules,
   opening/reset/countdown clocks, bid increments, anti-snipe and RTM.
8. Review the checklist, then choose **Start / resume** in the Control room.
   Team users bid in the group using shared buttons or `/bid`.
9. Manage trades in **Trades** before, during or after the auction as allowed
   by its rules. At completion, open **Next season & finish → Publish squads**.
   Squads publish into the same Challenge League tables used by the original
   bot for competitions. Re-publishing updates the same league. **Clone next
   season** carries rules, owners and published squad rights into the next cycle.

Private selection is navigation only: an old auction ID or forwarded menu never
grants access. Auction Managers can work on any selected franchise; team users
can read their assigned competitions, bid and answer RTM for their franchises.
Spectators use the bound group. `/adminhelp` and the existing `/a...` commands
remain available for detailed auction operations and imports/exports.

## Player CSV

The [example CSV](franchise-players-example.csv) contains fictional example
rows, not a ready-made player database. Required headers are:

```csv
name,rating,role,country
Example Batter,85,BAT,India
```

Optional headers: `version`, `base_lakh`, `bat_rating`, `bowl_rating`, `overseas`.
Roles accept `BAT`, `BOWL`, `AR`, `WK` and their full names. All monetary values
are integer lakh. Blank base prices follow the auction's price rules. Overseas
status follows the auction's home country. A matching catalogue entry is reused;
conflicting rating/role definitions are refused. Import is transactional, limited
to 2 MB and 2,000 rows, and available only to current Auction Managers during
setup or an allowed paused state.

## Local development and verification

Install `requirements.txt`. Copy
[`franchise-bot.env.example`](../franchise-bot.env.example) to a separate local
`franchise-bot.env`, fill its dedicated settings, and run:

```sh
FRANCHISE_ENV_FILE=franchise-bot.env python franchise_bot.py
```

Use a development bot and disposable database for testing. No Telegram or
production database credentials are needed for the automated checks:

```sh
python -m pytest -q tests/test_franchise_bot_runtime.py tests/test_franchise_bot_ui.py tests/test_franchise_player_import.py
```

The earlier website source remains available in repository history and files;
this dedicated entry point never starts it. Switching from that website to the
new bot does not require copying old auction records: both bot services read
the same database, with the dedicated bot responsible for the auction clock.
