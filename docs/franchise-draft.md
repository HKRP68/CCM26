# CMU Franchise Draft 2.0

The upgrade extends this existing draft engine, models, player pool, ledger and
season workflow. Start with the [Draft 2.0 operator guide](franchise-draft-2.md)
for configuration previews, retention, seconds-based timers and audited recovery.

The dedicated Franchise bot supports a fixed-price draft alongside auctions.
Use **/auction → New draft**, or **Existing leagues → Draft** to continue a
competition with its franchises and owners. A linked League Season continues
in the same competition, with previous tournament ownership recovered. Check
the team assignments before starting.

The draft uses the existing player catalogue, CSV importer, franchise roles,
squad rules, purse ledger and league publishing. It is a full season draft;
the auction's existing pre-auction expansion picks remain separate.

## Draft Admins and all commands

Draft Admin is a separate role from Auction Manager. Bot Owners and Bot Admins
can manage both modes. An appointed Draft Admin can manage drafts without
owning a franchise or receiving auction/global administration rights.
Existing Auction Managers need a Draft Admin appointment to operate drafts.

```text
/dadminadd 123456 Draft organiser
/dadminremove 123456
/dadmins
/dadmin
/draftcommands
```

Only Bot Owners and Bot Admins can appoint or remove Draft Admins. The optional
name in `/dadminadd` is for the directory. `/dadmins` lists the appointments;
`/dadmin` opens draft controls. The bot's private workspace also has appointment
buttons. Role changes are checked again on each command, button and form reply.
A Draft Admin cannot pick or skip for a team unless they also own or co-own it.

`/draftcommands` sends **all 48 draft commands and aliases together**, with
examples and role requirements. `/dcommands`, `/dcmds` and `/dhelp` open the
same complete reference. Its button sends every section even when Telegram
needs two messages. All draft commands are also registered in the slash-command
menu; `/commands draft` remains searchable.

| Purpose | Commands |
| --- | --- |
| Administration | `/dadmin`, `/dadminadd`, `/dadminremove`, `/dadmins` |
| Complete reference | `/draftcommands`, `/dcommands`, `/dcmds`, `/dhelp` |
| Create and bind | `/dnew`, `/dsetup`, `/dbind` |
| Teams and money | `/dteam`, `/downer`, `/dco`, `/dpurse` |
| Pool and fixed prices | `/dpooladd`, `/dprices`, `/dcustom` |
| Order and rounds | `/dorder`, `/drounds`, `/daddrounds` |
| Role and rating rules | `/drolemin`, `/drolemax`, `/dratingrule` |
| Live draft | `/dtimer`, `/dstart`, `/dpause`, `/dresume`, `/dpick`, `/dskip` |
| Recovery and completion | `/dundo`, `/dcancel`, `/dpublish` |
| Public views | `/draft`, `/dboard`, `/dpool`, `/dsquad`, `/dskips`, `/drules` |

Examples:

```text
/downer Team 1 | 123456
/dco Team 1 | 234567,345678
/dpurse Team 1 | 120cr
/dpooladd 80-96
/drolemin BAT=4,BOWL=4,AR=2,WK=1
/drolemax BAT=8,BOWL=8,AR=6,WK=3
/dratingrule 83=4
```

Owner, co-owner, role, rating and purse edits require setup or pause. Role and
rating commands replace their table; `clear` removes it. `/dpurse` without an
amount shows purses to everyone. `/dpublish` requires all regular and catch-up
picks to finish. `/dcancel` requires the exact draft name, preserving squads
and financial history. Shared `/importplayers` and guided settings recognise
Draft Admin authority only for drafts.

## Setup

1. Create the draft in the guided menu, entering a name, round count and
   opening purse. Alternatively, `/dnew Premier Season 2` creates a draft
   with 15 rounds and the default purse. In a group it binds there; in private,
   use `/dbind <draft ID>` in the intended group afterwards.
2. Add franchises, owners and co-owners through **Teams & roles**. A Team
   Manager role alone does not grant permission to draft players.
   New franchises can join alongside teams carried from the previous season:
   `/dteam New Team | 123456 | 234567,345678` adds a team with its owner and
   optional co-owners, a full opening purse and one pick per round. Add teams
   before starting; a previously saved order places the new team at its end.
3. Add players with `/importplayers` and the existing CSV format, or build
   rating sets through **Player pool**. `/sampleplayers` downloads an example.
4. Set draft prices, order, squad rules, purse and `/dtimer 10` before `/dstart`.

To use an existing empty auction setup as a draft, select it in `/auction`
and run `/dsetup 15`. Converting an auction requires both Auction Manager and
Draft Admin authority (Bot Owners/Admins have both). A setup with existing
signings or bids cannot change mode.

## Prices

Paste one rating and price per line:

```text
/dprices
96:23cr
95:21cr
94:19.5cr
80:1cr
```

Every player of a rating has that price. This command replaces the rating
table, so include all ratings needed by the available pool. Missing prices
are reported before the draft starts. Explicit `cr`, `crore`, `L` and `lakh`
units are accepted; money is stored as integer lakh, avoiding floating point
rounding. A price must resolve to whole lakh.

Custom player prices take priority:

```text
/dcustom
Joe Root:25.5cr
Ben Stokes:24cr
```

Custom updates preserve other custom prices. Use an exact player name or the
pool's `#ID` if multiple cards share a name. Existing signings keep their
agreed price when tables change. After launch, pause and use `/dcorrect` with a
reason; ordinary price edits stay locked even while paused or after undo.
The CSV's auction base prices do not set draft signing prices.

## Rounds and order

`/dorder Team 1 | Team 2 | Team 3 | Team 4 | Team 5` sets round 1's order.
Team IDs, one team per line, and `/dorder random` also work. Without a custom
order, the bot uses the franchise display order.

| Round | Order |
| --- | --- |
| 1 | Team 1 → Team 2 → Team 3 → Team 4 → Team 5 |
| 2 | Team 2 → Team 3 → Team 4 → Team 5 → Team 1 |
| 3 | Team 3 → Team 4 → Team 5 → Team 1 → Team 2 |

Each team gets one signing per round. The squad size equals the round count.
Use `/drounds 15` to set the total or `/daddrounds 2` to add rounds. Rounds
can be added during setup, while paused, or after completion before publishing.
After the first pick, the team order is locked and rounds can only increase.
After launch, round changes require a reasoned `/dcorrect rounds` preview.
Adding rounds to a completed draft reopens it paused; review and confirm `/dresume`.

## Pick time, skips and catch-up rounds

`/dtimer 10` gives each team ten minutes to finish its turn. Any whole-minute
value from 1 to 1440 is accepted, including 5, 8, 10 and 15. The default is
10 minutes; `/dtimer 0` disables automatic timeouts. Change the timer during
setup or while paused. The draft controls also include common timer buttons.

Each confirmed pick or skip starts a fresh full window for the next team,
whose owner and co-owners are mentioned with the UTC deadline. Opening a pick
confirmation does not stop the timer: the signing must be confirmed before
the deadline. An expired confirmation cannot charge the purse.

The current team's owner or co-owner may use `/dskip` or **Skip my turn**.
The turn advances immediately without signing a player or spending purse.
On expiry, the bot follows the explicitly approved `/dclock` policy. New drafts
pause by default; automatic skipping and automatic selection need administrator
approval. Duration-only `/dtimer` changes preserve the approved policy. Managers
and spectators cannot use an owner's skip control; administrator resolution is
an explicitly reasoned correction with a preview.

All skipped picks are saved in the order they occurred. After the scheduled
rounds, a **Skipped-pick round** plays that queue. For example, if Team 3,
Team 1 and Team 4 skip, their catch-up order is 3 → 1 → 4. If Team 3 skips
again, the order becomes 1 → 4 → 3. A team may have multiple deferred picks
from different rounds. Each deferred pick keeps its original round and
receives a fresh timer when called. `/dskips` shows the queue with navigation
buttons. Deferred picks fill the original squad slots and do not increase
the squad limit or deduct purse until a player is actually signed.

Pause freezes the time remaining; resume restores that balance. Changing
the timer while paused explicitly gives the restored turn the new full
window. Deadlines and queues survive restarts. After an outage, only the
current expired turn is skipped; the next team gets a full window from
recovery, so multiple teams are not skipped immediately for downtime.

## Purses and squad rules

Each franchise has its own purse. **Season & purse → Opening purse** changes
the default and existing teams; **Teams & roles → a team → Adjust purse**
allows a different balance for one franchise and records the reason.
These existing guided money inputs use whole lakh: `10000` means ₹100 Cr.

Configure the four role minimums and maximums under **Squad rules**:
`BAT=4,BOWL=4,ALR=2,WK=1`. The existing rating rules also apply; for example,
`83=4` requires at least four players rated 83 or below in each finished squad.
There is **no overseas limit**. Squad size is managed through draft rounds.

Start checks validate owners, pool size, all available player prices, role and
rating supply, and the ability to afford the remaining picks. Picks enforce
purse, role ceilings, remaining slots, rating minimums and reserves for the
remaining squad. A pick cannot exhaust the pool's last players needed to meet
other teams' role or rating minimums.

## Picking

The bot announces the current team and mentions its owner and co-owners.
Only that team's current owner or co-owner can request:

```text
/dpick Joe Root
```

The rich confirmation shows the player, team, exact signing price, purse
after signing and resulting squad size. **✅ Sign** commits the signing;
**❌ Cancel** leaves the purse and squad untouched. The person who requested
the confirmation must answer it. Anyone else is refused. A co-owner can
request their own confirmation.

Each confirmed pick updates the squad and purse atomically, records a ledger
entry, advances the turn, updates the pinned draft board and announces the
next team's owners. Repeated or stale confirmations cannot debit twice.
Confirmations expire after ten minutes and become stale when the draft changes.

`/draft` shows the board; `/dpool` lists available players with prices;
`/dsquad Team name` shows a squad and purse; `/drules` shows the rulebook.
Navigation buttons belong to their opener. Manager buttons recheck current
authority, and all pick confirmations must happen in the bound group.

## Recovery and completion

Managers can `/dpause` and `/dresume`. `/dundo` reverses the most recent
turn while paused (or immediately after completion). A signing is refunded
and returned to the pool; a skip or timeout restores the previous turn and
queue without changing purse. Undo leaves the draft paused and gives the
restored turn a full window when resumed. Published squads are locked. The turn journal,
confirmation state, turn, purses and announcement cursor survive restarts.
Telegram delivery runs after committed picks; failed sends are retried.

After all scheduled and catch-up picks, publish with `/dpublish` or **Publish squads**. The normal
League Season tournament creation and previous-season statistics continue to
work. Ordinary auction bidding, timers, simulated finishing, expansion picks,
manual roster edits and pre-publication trades cannot change a draft's squads.

All draft messages use rich text. The live board highlights the team on the
clock, tags its owner/co-owners and displays the UTC deadline. Native tables
line up squad sizes, purses, player ratings and signing prices. Confirmations
show the exact charge, purse after signing and resulting squad size; signing
receipts show the next team. Round orders and long squads use collapsible
sections. Admin panels, rules, price lists, player imports and the full command
guide use the same headings and formatting.

Every card also has an escaped HTML rendering for Telegram servers without
rich-message support. Long lists and command guides split into consecutive
messages with the buttons on the final message. The pinned board stays compact.
Announcements save their display values with the turn, keeping the original
purse, squad count and next-team mentions when delivery retries later. Button
ownership and current-role checks apply in both formats. `/draftcommands` sends
the full reference; `/commands draft` provides a searchable guide.
