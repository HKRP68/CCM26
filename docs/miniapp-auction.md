# Franchise auction in the Mini App

The Mini App and Telegram operate the **same auction**, not separate copies.
They share lots, bids, ownership, purses, retention, draft turns, RTM stages,
trades, the ledger and the restart-safe scheduler. Existing Telegram commands
continue to work. No database migration or separate hosting service is needed.

Player-pool creation, imports, franchise setup and all other Mini App administration
are restricted to verified **bot admins**. Players browse the prepared player list, manage
their own franchise and bid; they are never asked to build the pool. The Admin
tab is hidden for players and the API independently checks every admin action.
Delegated auction-admin status alone does not grant Mini App admin access;
existing delegated Telegram command permissions are unchanged.

## Open and switch

- Open **Franchise Auction** from the Mini App home screen.
- Tap **Arena ↗** for the optional presentation inspired by the reference screens.
  **Return to current UI** closes it and restores the existing auction interface.
- The Telegram live board and `/ainfo` menu have **Switch to Miniapp** buttons.
  A group button opens `auction_<seasonId>_c<chatId>`, preserving the auction
  and its group. Private Mini App URLs can use `#auction_<seasonId>`.
- **Switch to Tg** returns to the pinned auction board in its Telegram group.
- An auction selector supports parallel competitions and completed seasons.
- Owners can open an auction directly even without a card-collecting `/debut`.

The existing deployment must have `WEBAPP_URL` pointing at the Mini App's
public HTTPS URL (normally `https://<host>/webapp`), plus `BOT_USERNAME` and
the BotFather-configured `MINIAPP_NAME` for group links. Redeploy/restart the
existing bot/web service to load the new routes and keyboards. The bot's
existing auction scheduler must keep running to resolve clocks and announce
events. This change does not deploy the service or reconfigure BotFather.

## Screens

### Optional Auction Arena

The existing Mini App screens remain the default. The Arena uses separate,
scoped styles in a full-screen dialog, with **Squads / Arena / Logs** navigation.
It includes a dark player stage, uploaded portraits (falling back to player card
artwork), role and country badges,
four tiles showing stored card ratings/edition, a bid button docked above the
navigation, current and next bid, the shared
server countdown, team purse/squad/overseas counts, and Right To Match controls.
No real-world career statistics or participant-presence counts are invented.

Sold/unsold result cards come from committed auction events. Teams expand to
show acquisitions and budget details; Logs includes live activity and current
player bid history. Background updates pause when hidden, preserve typed custom
bids, reject stale lot/stage/price contexts, and disable bidding when disconnected
or after the server deadline. The scheduler remains responsible for resolution.

Only a verified bot admin sees the **Bot admin panel** button. Opening the panel
checks the signed Telegram identity on the server, and every action independently
requires bot-admin access. Owners, co-owners, spectators and delegated auction
admins cannot gain access by sending `admin: true` or `bot_admin: true`.
The panel provides start/resume, pause, next player, sell/unsold, clock extension,
undo bid, admin bidding and RTM controls. Team buttons open the existing player
and purse workspace; **Open full control room** provides season setup and rules.

### Existing auction screens

| Screen | Available features |
| --- | --- |
| Live room | Player card, current and next bid, quick bid, custom bid when allowed, server-timed countdown, all three RTM stages including final raise/stand, purse and legal maximum, squad warnings, bid log, upcoming players and room activity |
| Player pool | Name search, role/set/status filters, pagination, running set order, sold/unsold results and admin lot controls |
| Teams & purses | Every squad, acquisitions, overseas counts, RTM cards, purse, legal bid ceiling and squad warnings; the owner's purse ledger and bid history |
| Retention & draft | Previous-squad candidates, classic/range retention, admin offers and owner answers, dynamic negotiation/counters/withdrawals, owner release, token-checked retention review and finalization, readiness, eligible expansion players, private shortlist, pick and skip |
| Trades | Build offers, select players from both sides, signed cash, notes, send/counter/accept/reject/withdraw, public trade block and completed trades; bot-admin approval/veto/reversal |
| Rulebook | Purse, squad/role/rating rules, base prices, increment ladder, clock, anti-snipe, retention bands and RTM rules |
| Admin | Create/clone seasons, bind groups, add/edit/import owners and co-owners, link/carry previous squads, build/search/import/export pools and sets, reorder sets, configure rules, retention/draft/RTM, live auction controls, purse corrections, trade rules/windows, simulation/playback, publication, restart/cancel and auction-admin access |

The control room walks an admin through group, franchises, rules, player pool,
preseason, live operations, trades and completion. Complex rule tables use JSON
editors; common purse, squad, clock, ownership and player-pool settings have
labelled forms. Monetary inputs use **integer lakh**: `100` means ₹1 Cr.

## Manage team players and purses

In the Mini App, open **Teams & purses → Manage a team**. On the website, open
**Franchise Auctions → your auction → Franchises → Manage players & purse**.
Team names on the website's live console also open this workspace.

- **Add a player:** search the active master catalogue, select a card, enter its
  signing price in lakh (`0` is free), and give a reason. The player joins the
  squad and the price is charged through the existing ledger. Cards outside
  the pool are added automatically. Signed or unavailable cards cannot be added.
- **Remove a player:** select a squad member and provide a reason. Their full
  signing price is refunded and they return to the end of the auction queue.
  Retention releases and draft removals preserve the existing counters and
  window rules; removing an RTM signing returns the used card. Completed trades
  must be reversed before a traded player can be removed.
- **Change the purse:** choose **Add funds**, **Deduct funds** or **Set balance**.
  Review the before/after balance and record the reason. The server rejects stale
  balances, negative purses and reductions that cannot cover a standing winning
  bid and its minimum-squad reserve.

Player changes require **setup or paused** status with the current player
resolved. Published and cancelled auctions are locked in this workspace. Squad,
overseas, role, rating and minimum-purse reserves also apply to admin signings;
another version of a player already on that squad cannot be added. Requests are
transactional and repeated signings/removals cannot debit or refund twice.

The enhanced interface includes team search, squad progress, total remaining
purses, team warnings, a selected team's recent ledger, and visible live controls.
The Admin tab includes a setup checklist and quick links to common tasks. Its
checklist is a quick review; starting still runs the shared backend validation.

Suggested auction routine: review warnings and the setup checklist, run bidding
from the live desk, pause and resolve the current player before squad corrections,
then check the ledger before resuming. Future useful additions are a downloadable
auction results sheet and an optional display-only board for the venue.

## Improvements included

- **Legal maximum and budget warnings** show why a franchise cannot bid before
  it taps. Required minimum squad slots remain reserved by the shared service.
- **Outbid feedback** uses an in-app notice and Telegram haptic feedback while
  the live room is open. It does not send additional DMs.
- **Stale-button protection** binds actions to a lot, RTM stage and displayed
  bid count. The app never raises a bid automatically after somebody else bids.
- **Connection handling** disables bidding when live data is delayed, pauses
  polling when hidden, refreshes on reconnect and asks the user to check the
  resulting state after an uncertain request. Typing a bid is preserved during
  background refreshes.
- **Retention review and trade versions** prevent an old review or an edited
  trade from being accepted using stale buttons.
- **Responsive Telegram bidding** allows an immediate `/bid` after being
  outbid, with duplicate-attempt feedback and database work kept off the bot's
  event loop. A failed reaction or expired callback cannot undo a committed bid.

Useful future additions are a general auction watchlist with optional outbid
push notifications, and squad comparison by role/rating. Automated maximum bids
would require an explicit competition rule and shared backend support so they
behave consistently with Telegram bidding.

## API and permissions

All endpoints are POSTs under `/api/webapp/auction` and use the app's existing
Telegram `initData` authentication. Read and action responses are not cached.

| Endpoint | Purpose |
| --- | --- |
| `/api/webapp/auction` | Auction selector and the requested `view` (`arena`, `live`, `pool`, `teams`, `preseason`, `eligible`, `trades`, `rules`, `admin`) |
| `/api/webapp/auction/action` | Named owner or `admin_` action, committed once after validation |
| `/api/webapp/auction/catalogue` | Admin search of active master player cards |
| `/api/webapp/auction/export` | Admin JSON export of sets or franchises |

Owners/co-owners can act only for their assigned franchise. Spectators require
an affirmative group-membership check, cached for 60 seconds; an unsigned
launch hint never grants access. Bot-admin rights are checked on every Mini App
admin read, catalogue/export request and action. Negotiation MAPs, other teams' shortlists and purse
ledgers, and unsent outgoing trade builders are not served to other owners.

Successful mutations write the existing events and ledger rows. The existing
bot scheduler announces them in Telegram; the HTTP request never calls a bot
send method. Polling the live room every two seconds reads the server deadline;
the browser countdown does not decide when a lot sells.

Preseason/trade views also persist the shared service's materialized previous
squad rights as queued lots, so subsequent release/trade buttons have stable
lot IDs. Materializing rights does not sign a player or charge a purse.

## Validation

`tests/test_auction_webapp.py` exercises signed requests against a temporary
SQLite database: mixed Telegram/Mini App bids, concurrent bids, stale buttons,
expired clocks, owner/co-owner/spectator/admin gates, retention and negotiation,
expansion turns and private shortlists, the three-stage RTM, trade consent and
approval, import/export, setup and switching links. Existing auction suites
continue to exercise the shared rules and scheduler. Mobile and desktop browser
checks cover navigation, search, bidding and the admin screen.

`tests/test_auction_miniapp_ui.py` runs the JavaScript controller in Node to
check empty-room startup and player/admin separation without browser packages.
`tests/test_auction_arena_ui.py` checks the optional Arena's bidding context,
server-checked admin entry, navigation, result cards and restoration of the
original screen and scroll settings. Signed API tests also cover delegated
auction admins being denied Mini App administration and shared Arena results.
`tests/test_auction_room_ux.py` also verifies rapid rebidding, duplicate feedback,
event-loop responsiveness during slow database work and acknowledgement failures.
