# Standalone Franchise Auction website

`website.py` serves a separate browser website at `/` and `/franchise`, with
Telegram Login Widget sign-in. It uses the same database and auction services
as the Telegram bot and Mini App. Existing seasons, owners, co-owners, player
pools, bids, squads and purse ledgers appear after login; no copy or reset of
auction data is needed.

The website provides live auctions and RTM, squads and budgets, player pools,
retentions and negotiation, draft/expansion picks, trades, rules, season setup,
imports/exports, and publishing squads to Challenge League through the shared
auction workspace. See [the auction guide](miniapp-auction.md) for detailed
rules and operations. It does not move unrelated card-collecting games or
replace the existing password-protected administration panel. Publishing
squads uses the existing Challenge League integration; a separate tournament
management and scoreboard website is outside this migration.

## Access

Sign in with the same Telegram account that owns a franchise or has been
appointed as an Auction Manager. Owners/co-owners can bid and answer RTM for
their assigned franchise and view its auction information. Auction Managers
manage retentions, draft picks, trades, purses and setup using the existing
permissions. Spectators opening a season link must pass a Telegram group
membership check. A season/group in a URL is a navigation hint, never a grant
of access.

Telegram browser sessions do not confer password-admin access. Manager and
franchise permissions are read from the database on each auction request, so
removing a manager or owner takes effect without waiting for logout.

## Render deployment

The repository's default `Procfile` and `Dockerfile` continue to run `bot.py`.
Keep that existing Render service and create a **second Web Service** from the
same repository for the standalone website. The included
[`render.franchise.yaml`](../render.franchise.yaml) declares only this new
Python web service; it creates no database and does not replace the bot's start
command. The current Python backend cannot run unchanged on Sites/Cloudflare
Workers.

In Render, select **New → Blueprint**, connect this repository and set the
Blueprint file path to `render.franchise.yaml`. Supply the requested
`DATABASE_URL`, `BOT_TOKEN`, `BOT_USERNAME` and explicit `BOT_ADMIN_IDS` values from the existing bot.
Render generates a separate `WEBSITE_SECRET` for the website. Review the new
service before creating it; adding this file to the repository alone does not
deploy a service.

The Blueprint defaults to Render's **Free** plan and provisions no paid
resources. Free web services can spin down when idle; an always-on plan is
recommended for running live auctions. Choose the appropriate instance plan in
Render before the competition.

If creating the service manually, use these settings:

| Render field | Value |
| --- | --- |
| Service type | Web Service |
| Name | `ccm26-franchise` (or another available name) |
| Repository/branch | The same code release as the existing bot |
| Runtime | Python 3 |
| Root directory | Repository root; leave blank |
| Build command | `pip install -r requirements.txt` |
| Start command | `gunicorn --bind 0.0.0.0:$PORT --worker-class gthread --workers 2 --threads 32 --timeout 60 website:app` |
| Health check path | `/` |
| Python version environment | `PYTHON_VERSION=3.11.9` |

The manually selected **Python** runtime uses this start command even though
the repository also contains a Dockerfile. Do not change the existing bot
service to `website:app`; the bot still owns clocks and announcements.

1. Back up the production database. Test with a staging database and bot before
   changing production links.
2. Install `requirements.txt` on the new web service. Set its environment:

   | Variable | Value |
   | --- | --- |
   | `DATABASE_URL` | The same shared PostgreSQL database used by the bot; use its Render internal URL for services in the same region |
   | `BOT_TOKEN` | The existing bot's token, stored as a server secret |
   | `BOT_USERNAME` | The corresponding bot username without `@` |
   | `BOT_ADMIN_IDS` | The same explicit administrator Telegram IDs as the bot; copy `OWNER_IDS`/`BOT_OWNER_IDS` too if configured |
   | `WEBSITE_SECRET` | Render-generated secret from the Blueprint, or a new stable random secret of at least 32 characters |
   | `WEBSITE_HTTPS` | `true` for production secure cookies |
   | `PYTHON_VERSION` | `3.11.9` |
   | `PORT` | Assigned by Render automatically; do not override it |

   Keep `WEBSITE_SECRET` separate from `ADMIN_SECRET`. Generate it using your
   provider's secret generator or `python -c "import secrets; print(secrets.token_hex(32))"`
   in your own terminal, then save it directly in the provider's secret store.
   Never embed bot tokens or session secrets in frontend files.
3. On an initial installation or when this checkout requires schema updates,
   run `python website.py init-db` once against the backed-up shared database
   using the same release as the bot. Importing the app does not initialize or
   migrate the database. Existing up-to-date installations do not need to
   create a new database.
4. Start the web service:

   ```sh
   gunicorn --bind 0.0.0.0:$PORT --worker-class gthread --workers 2 --threads 32 --timeout 60 website:app
   ```

   Render gives the service an HTTPS `https://<assigned-name>.onrender.com`
   address. Use the URL actually shown in the service dashboard; a custom
   domain can be added later. Keep bot and web releases compatible with the
   same database schema.
5. In **@BotFather**, use `/setdomain`, select the existing bot and register the
   new website domain (for example `<assigned-name>.onrender.com`) for Telegram
   login. Telegram must be able to display its Login Widget in visitors'
   browsers. Keep the production site on HTTPS.
6. Keep the original `bot.py` worker running against the same database. It owns
   the auction scheduler and Telegram announcements; the standalone website
   intentionally starts no second scheduler. A browser countdown does not
   settle a lot. For separate hosts, two local SQLite files are two different
   databases and will not synchronize.
7. Verify a real owner login, manager login, access refusal for an unrelated
   account, retention/draft tools and a complete staging auction including its
   scheduled sale before switching links.
8. Once verification passes, set
   `FRANCHISE_WEBSITE_URL=https://<assigned-name>.onrender.com/franchise` on
   the **existing bot service** in Render's Environment settings and restart
   that service. New auction-board and `/ainfo` buttons open the website with
   the selected season and group. They are plain URL buttons in private chats
   and groups. Existing messages may retain their old buttons until refreshed
   or reposted.

Render services have separate ephemeral filesystems, even when they share a
database. Existing player portraits can restore from the shared database's
asset store, and custom card images can restore from their stored Telegram
file IDs with the same bot token. Check representative uploaded artwork on the
new service before switching links. Older images that exist only on the bot's
local disk need to be uploaded/persisted through those durable stores first;
sharing `DATABASE_URL` does not copy arbitrary files between services.

Leave the existing `WEBAPP_URL` and Mini App configuration in place for other
bot features. If `FRANCHISE_WEBSITE_URL` is empty or invalid, auction buttons
keep their existing Mini App behavior. Only HTTPS URLs without login credentials
or fragments are accepted. Existing query parameters are preserved; season and
group hints are replaced with the selected auction's values.

To roll back the doorway, clear `FRANCHISE_WEBSITE_URL` on the bot and restart
it. No auction data rollback is required: both interfaces use the same records.
Keep the bot scheduler running during either rollout or rollback.

## Local development and checks

Use a disposable local database and development bot. Set `WEBSITE_SECRET` to a
random local value and `WEBSITE_HTTPS=false` only for local HTTP testing, then:

```sh
python website.py init-db
python website.py serve
```

Open `http://localhost:5001/`. Public layout and session handling can be checked
locally; a complete real Telegram Login Widget flow requires a domain configured
in BotFather. There is no development identity bypass on the standalone website.

In a development test environment, install `pytest` in addition to the runtime
requirements, then run:

```sh
python -m pytest -q tests/test_franchise_website.py
python -m unittest discover -s tests -p 'test_franchise_website_links.py'
python -m unittest discover -s tests -p 'test_auction_webapp.py'
```

## Login and request protection

The server verifies the Login Widget's HMAC signature using the bot token,
rejects stale/future payloads and stores the verified identity in a signed,
HttpOnly, SameSite browser cookie for up to 24 hours. Login payloads must be
fresh (within five minutes). Browser-authenticated auction requests, login and
logout require the session's random `X-CSRF-Token`. Successful login/logout
rotate it. Sessions carry identity, not permanent permissions.

An explicitly supplied Mini App authorization header is verified independently;
an invalid header cannot fall back to a browser cookie. `DEV_` identities cannot
access the shared auction. Browser login does not set the password-admin flag.

## Live updates and visible roles

The auction room and dashboard subscribe to authenticated server-sent change
notifications. A lightweight database probe checks for committed changes every
500 ms, so actions made by the bot or another web worker reach the same room.
Actual arrival time includes database and network latency; zero delay is not a
guarantee. Full viewer-specific payloads are fetched after changes and periodically
to keep bidding state fresh. Heartbeats alone never renew stale bidding data.

Streams last at most 25 seconds, reconnect automatically, and release their
database session between probes. Hidden or offline tabs suspend connections.
The website uses one stream for its active screen. If streaming is unavailable
or full, the live room polls every second and tools/dashboard refresh every two
seconds. Live connection status, the last confirmed update, and measured request
round-trip time are shown in the room.

The Render command uses two `gthread` workers with 32 threads each.
`AUCTION_STREAM_MAX_CLIENTS=12` caps streams per worker, leaving threads for bids
and sign-ins. Use the new start command when updating an existing website
service. Without an explicit setting, the cap defaults to two so an existing
four-thread service keeps capacity for ordinary requests during an upgrade.
Keep this cap below the configured thread count. The normal bot
start command remains `python bot.py`. An always-on Render instance avoids
free-tier cold starts during live auctions; no instance plan is changed by code.

Bot Admin, Auction Manager, Team Owner, Team Co-owner and Spectator badges are
derived from the verified Telegram identity and current permissions. Multiple
roles appear together. Bot Admin includes auction management, so it is shown
instead of a redundant Auction Manager badge. Being a team owner or Telegram
group administrator does not grant management access. Role removal updates
the next authorized payload; every action still rechecks its permissions.

A committed bid is acknowledged immediately. Controls remain busy until the
server state is reread, and uncertain requests are never automatically submitted
again. While typing a custom bid, current price and leader still update. A new
player, RTM stage, pause or role change replaces obsolete controls. Typed amounts
are never automatically raised.

## Suggested next additions

The website already reuses the existing purse warnings, legal bid maximums,
stale-action checks and squad-result downloads. Useful future additions are:

- An owner watchlist with optional outbid notifications.
- A display-only spectator board for an auction venue.
- Shortlists filtered by remaining squad, role, rating and purse requirements.
- A side-by-side squad comparison by roles, ratings and remaining budget.

Automated maximum bids would need explicit competition rules and shared backend
support before they could work consistently in Telegram and the website.
