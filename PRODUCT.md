# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- Cricket players build squads, choose a playing XI, play simulated matches, and track results and progression through the Telegram bot and Mini App.
- Team managers and franchise owners build competition squads, manage budgets, and take part in drafts, auctions, and trades.
- Tournament organizers, auction managers, and admins set up competitions, run auction events, and publish results. Eligible spectators can follow franchise seasons.

## Product Purpose

CCM26 is a Telegram-centered cricket simulator and team-management game. Players can build a roster over time and put their selection and tactics to work in simulated matches. Organized competitions give managers a way to assemble squads, manage fixtures, and follow standings and player statistics.

## Positioning

The standalone Franchise Auction website and the Telegram surfaces use the same auction services and data. Managers can use the same Telegram identity to continue with existing seasons, squads, bids, and purse balances across those surfaces. Organizers can publish auction squads into Challenge League.

## Operating Context

- Players use Telegram commands and the Mini App for account and squad management, matches, quests, rewards, and competition activity. The Mini App prioritizes resuming an active match and showing daily quest progress.
- Franchise participants use live bidding, right to match (RTM), retentions, draft picks, and trades. Auction managers handle setup and league operations. The browser workspace uses Telegram sign-in.
- The password-protected admin panel provides operational tools for competitions, content, and game configuration.

## Capabilities and Constraints

- Preserve existing auction records when users move between Telegram and the browser workspace.
- Owners, co-owners, and assigned team managers can bid and respond to RTM for their franchise. Auction managers have separate operational permissions. Spectator access depends on Telegram group membership.
- A season link helps users navigate to the auction. Access is checked against their current identity and permissions.
- Telegram browser sessions for franchise auctions and password-admin sessions use separate authentication.

## Evidence on Hand

- [Bot command reference](BOT_COMMANDS.md) documents current player, organizer, and administrator workflows.
- [Match types guide](docs/Match-Types-Guide.md) covers match and tournament formats.
- [Mini App UX plan](docs/miniapp-ux-plan.md) documents the daily player loop and interaction priorities.
- [Franchise website guide](docs/franchise-website.md) documents shared data, access rules, and browser workflows.

## Product Principles

- Make it easy for players to resume an active match and understand their next available action.
- Keep auction state consistent across surfaces so managers can change devices or interfaces without losing their place.
- Make live competition actions clear to the roles authorized to take them.

## Accessibility & Inclusion

Show Mini App progress numerically as well as visually so players can understand progress without relying on color.
