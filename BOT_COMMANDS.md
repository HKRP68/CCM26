# 🏏 Bot Command — Cricket Simulator (CMU)

The complete command reference for the Telegram bot in this repository.
Every command below is registered in [`bot.py`](bot.py); the short aliases are
the exact ones the bot answers to.

> **New here?** Read [Start the bot](#-start-the-bot) first — you need to join
> the official **channel** and **group** before the bot is much use to you.

---

## Contents

| # | Section |
|---|---------|
| 0 | [Start the bot](#-start-the-bot) |
| 1 | [How to read this file](#1-how-to-read-this-file) |
| 2 | [Account & daily rewards](#2-account--daily-rewards) |
| 3 | [Squad & roster](#3-squad--roster) |
| 4 | [Playing XI & batting order](#4-playing-xi--batting-order) |
| 5 | [Market, shop & economy](#5-market-shop--economy) |
| 6 | [Traits](#6-traits) |
| 7 | [Stats, profile & leaderboards](#7-stats-profile--leaderboards) |
| 8 | [Play a match](#8-play-a-match) |
| 9 | [Challenge Leagues](#9-challenge-leagues) |
| 10 | [Lets Play Tournament](#10-lets-play-tournament) |
| 11 | [Challenge League Tournament](#11-challenge-league-tournament) |
| 12 | [Tournament Draft](#12-tournament-draft) |
| 13 | [Franchise Auction](#13-franchise-auction) |
| 14 | [Fantasy league](#14-fantasy-league) |
| 15 | [Mini games & social games](#15-mini-games--social-games) |
| 16 | [Quests, achievements & referrals](#16-quests-achievements--referrals) |
| 17 | [Help & support](#17-help--support) |
| 18 | [Mini App](#18-mini-app) |
| — | [Appendix A — Usage examples, command by command](#appendix-a--usage-examples-command-by-command) |
| — | [Appendix B — Admin & owner commands](#appendix-b--admin--owner-commands) |
| — | [Appendix C — Where each command works](#appendix-c--where-each-command-works) |
| — | [Appendix D — Alias index (A→Z)](#appendix-d--alias-index-az) |
| — | [Appendix E — Cooldowns & limits](#appendix-e--cooldowns--limits) |

---

## 🚀 Start the bot

**You need to join the channel and the group.** The bot is built around a
community: matches, tournaments, drafts and auctions all happen in the official
group, and card drops, downtime notices and event announcements are posted in
the official channel. Both links are printed at the bottom of every `/start`
message (see `format_branding_html` in
[`services/referral_service.py`](services/referral_service.py)).

### First-time setup, in order

| Step | What you do | Where |
|------|-------------|-------|
| 1 | **Join the official channel** — announcements, events, downtime notices | Link at the bottom of `/start` |
| 2 | **Join the official group** — this is where matches, tournaments, drafts and auctions are played | Link at the bottom of `/start` |
| 3 | Open a **private chat (DM)** with the bot and send `/start` | Bot DM |
| 4 | Send `/debut` to create your account and receive your starting squad | Bot DM |
| 5 | Send `/claim`, `/daily` and `/gspin` to top up your first cards and coins | Bot DM |
| 6 | Set up your side: `/autobuild` → `/setcaptain` → `/setbo` → `/teamname` | Bot DM |
| 7 | Go to the group and play: `/letsplay @user`, `/cipl` (reply to someone), or `/lpbot` against the AI | Group |

```
/start        →  welcome + the channel and group links
/debut        →  create your account, get your starter XI
/claim        →  first hourly player + coins
/autobuild    →  best available XI picked for you
/letsplay @friend   →  your first 20-over match (in the group)
```

**Tip:** add the bot to your own group and it works there too. `/ewm` turns on
welcome messages for that chat, `/dwm` turns them off (group admins only).

> **Members-only (Rookie) mode.** If the operator has turned Rookie mode on,
> `/start` says so and most commands need a membership — see `/membership`.

---

## 1. How to read this file

* **Aliases** — every alias is listed. `/myroster /mr /roster` all do the same thing.
* **`<required>`** — an argument you must supply. **`[optional]`** — you may leave it out.
* **`|`** — a literal pipe character the bot uses to separate fields, e.g. `/dadd Mumbai | Virat Kohli`.
* 💬 **DM only** — runs in a private chat with the bot. In a group it replies with a deep link instead of the answer.
* 👥 **Group only** — refuses to run in a private chat; it needs a second player or a group audience.
* 🔒 **Admin** — restricted to bot admins/owner. Full list in [Appendix B](#appendix-b--admin--owner-commands).
* **Reply/tag** — several match commands target an opponent by *replying to their message* or *tagging* them.

---

## 2. Account & daily rewards

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/start` | `/s` | Welcome message, command overview, channel & group links |
| `/debut` | `/d` | Create your account and receive a starting squad |
| `/claim` | `/c` | Claim your hourly player + coin reward |
| `/daily` | `/dl` | Daily reward — grows with your login streak |
| `/gspin` | `/gs` | Lucky Card Pick — pick one of five cards, win a reward |
| `/cmumysterybox` | — | Open your subscriber Mystery Box 🎁 |
| `/cmuweekly` | — | Claim your weekly guaranteed 85+ card (Platinum/Diamond) 🏆 |
| `/cmuchest` | — | Open your recurring coin chests (Platinum/Diamond) 🪙 |
| `/membership` | `/member` `/mysub` `/subscription` `/plans` | Your membership, its perks, and the price list 💳 |
| `/redeem` | `/code` | Redeem a reward code |
| `/notifications` | `/notify` `/notif` | Turn reminder notifications on or off |
| `/cmuundo` | — | Undo your latest eligible action |

## 3. Squad & roster

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/myroster` | `/mr` `/roster` | View your player roster |
| `/playerinfo` | `/pi` `/info` | Full details for one player |
| `/searchpl` | `/search` `/sp` | Search for a player by name |
| `/searchovr` | `/so` | Search players by overall rating |
| `/buypl` | `/buy` `/b` | Buy a player at market price |
| `/releasepl` | `/release` `/rel` | Release one player (by name or roster position) for coins |
| `/releasemultiple` | `/relm` `/rm` | Release a range of roster positions at once |
| `/trade` | `/tr` | Trade players with another user |
| `/owners` | `/ownedby` `/whoowns` | Who owns this player in this group 👥 |
| `/teamname` | `/tn` | Set your team name |
| `/setteamlogo` | `/teamlogo` | Set your team crest (DM; needs admin approval) |
| `/cmucareer` | `/career` | Create and train your own Career Player 🎖 |
| `/cmuchange` | `/careerchange` | Change your Career Player's name or country ✏️ |

## 4. Playing XI & batting order

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/playingxi` | `/pxi` `/xi` | View or manage your playing XI |
| `/ximage` | `/xiimg` `/xipic` | Your Playing XI rendered as an image |
| `/autobuild` | `/ab` `/best11` | Auto-pick your best legal XI and reorder the roster |
| `/swapplayers` | `/swappl` `/swap` | Swap two players' positions |
| `/setbo` 💬 | `/sbo` `/battingorder` `/bo` | View or change your batting order |
| `/setcaptain` | `/captain` `/cap` | Set your team captain |
| `/cmuchem` | `/cmuchemistry` `/chem` `/chemistry` | Check your XI's Team Chemistry 🧪 |
| `/chemhelp` | `/chemguide` | How Team Chemistry works 📖 |
| `/change` | — | Change your XI / batting order during match setup |

## 5. Market, shop & economy

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/purse` | `/p` | Check your balance |
| `/coins2gems` | `/c2g` | Convert coins into gems (1000 coins = 1 gem) |
| `/cmushop` | — | Browse the CMU shop 🛍️ |
| `/playermarket` | `/pmarket` `/market` | Browse the player market |
| `/buypack` | `/packs` `/shop` | Browse and buy card packs |
| `/openpack` | `/open` | Open a pack from your inventory |

## 6. Traits

All trait inventory commands answer in **DM**; `/traitlist` works anywhere.

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/traits` 💬 | `/tt` | Your traits and inventory |
| `/traitboost` 💬 | `/tboost` | What your equipped traits add to your XI's Team Overall ⚡ |
| `/traitlist` | `/tlist` `/traitcatalogue` | Browse every trait in the game, its effect and its price |
| `/traitshop` 💬 | `/tshop` | The daily trait shop |
| `/traitapply` 💬 | `/tapply` | Apply a trait to a player |
| `/traitupgrade` 💬 | `/tup` | Level up a trait |
| `/traitreplace` 💬 | `/trep` | Replace a trait on a player |
| `/removetrait` 💬 | `/rtrait` | Remove a trait (back to inventory) |
| `/selltrait` 💬 | `/tsell` `/straits` | Sell an inventory trait for gems |
| `/tradetrait` | `/ttrade` `/trtrade` | Swap a trait with another user, same level both ways |

## 7. Stats, profile & leaderboards

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/myprofile` | `/profile` `/me` | Your profile |
| `/stats` 💬 | `/st` | A player's game statistics |
| `/gstats` 💬 | `/globalstats` | Global stats — every owner, every match type 🌍 |
| `/statscl` 💬 | — | Any Challenge League player's stats |
| `/statstour` | — | A player's stats in the active tournament |
| `/tournamentstats` | — | Tournament stat leaderboards (top 25, ranks 11+ behind a tap) |
| `/cmuleaderboard` | `/leaderboard` `/lb` `/top` | The global leaderboard |
| `/h2h` | `/headtohead` | Head-to-head record vs another player |
| `/rank` | `/myrank` `/elo` | Your ranked rating, division and ladder position 📈 |
| `/ranked` | `/ladder` `/rladder` | This season's ranked ladder |
| `/rivalry` | `/rivalries` `/rival` | Your rivalries, or the series with one player ⚔️ |
| `/halloffame` | `/hof` `/records` | All-time records — top scores, best figures, streaks 🏛️ |
| `/lastmatch` | `/lm` | Your last completed match |
| `/lastscorecard` | `/lsc` `/scorecard` | Re-send this chat's last scorecard images |
| `/recentmatches` 💬 | `/recent` `/matches` | Your recent matches |
| `/pitchstats` | `/pstats` `/ps` | What each pitch actually does — runs/over, wickets/over, win % |
| `/botstatus` | `/bstatus` `/ping` | Bot ping, uptime and live health |

## 8. Play a match

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/letsplay` | `/lp` | 20-over match with your own roster — reply or tag to invite |
| `/playmatch` | `/pm` `/match` | Challenge another user to a match |
| `/cm` | — | Two-wicket challenge match |
| `/cdraft` 👥 | `/challengedraft` | Challenge Draft — both captains build an XI pick by pick, then play |
| `/sim` | `/simmatch` | Simulate a full match instantly |
| `/wpm` | — | Match lobby up to 20 overs (Mini App) — tag/reply to invite |
| `/vsbot` | `/vsb` | Play a bot opponent in chat |
| `/wpmbot` | `/wpmb` | Play a bot opponent in the Mini App |
| `/lpbot` | `/lpb` `/letsplaybot` | Unranked Lets Play practice vs the bot |
| `/ciplbot` | `/ciplb` `/challengeiplbot` | Unranked league practice vs the bot |
| `/predict` 👥 | `/pred` | Back a side of the live match in this group with coins 🔮 |
| `/botvsbot` | `/bvb` | Configure a bot-versus-bot match |
| `/botmatch` | `/spectate` | Spectate a bot-versus-bot match |
| `/pbo` | `/bowlout` | Start a standalone player bowl-out |
| `/impact` | `/ip` | Open the Impact Player picker mid-match |
| `/resume` | `/r` | Resume your active match if the buttons disappear |
| `/rcl` | `/resumecl` | Resume a stuck Challenge League match |
| `/endmatch` | `/em` | Request to end your active match (a fine applies) |
| `/matchinfo` | `/mi` | Live match info — striker, bowler, score, target |
| `/clearmatches` | `/clearmatch` | Clear stuck matches in this chat (players in the match, or an admin) |
| `/cltour` | `/cltours` | Challenge League Tour — best-of series vs a friend |
| `/cmtours` | `/createtour` | Create a tournament |
| `/mytours` | `/tours` | View your tournaments |

### Ranked ladder, rivalries, predictions & highlights

* **Ranked ladder** — every completed match between two players (Lets Play,
  CIPL / Challenge League, `/playmatch`, `/wpm`, Super Over and bowl-out
  finishes) moves both captains' skill rating. Beating a stronger opponent pays
  more than beating a weaker one; a tie pulls the two ratings together. The
  first 10 matches are placement matches with bigger swings. Divisions:
  🥉 Bronze · 🥈 Silver 1050 · 🥇 Gold 1150 · 🔷 Platinum 1250 · 💎 Diamond 1350
  · 👑 Legend 1450. The ladder runs on the monthly season: at month end everyone
  with 5+ ranked matches is paid by division (Silver 1,000 coins → Legend
  20,000 coins + 10 💎), and ratings reset halfway back to 1000. The same pair
  can only move each other's rating 3 times a UTC day. Matches vs the bot and
  "won't count" mismatches are never rated.
* **Rivalries** — after 5 meetings, two players become a named rivalry. The
  result card then shows the series score after every match, each rivalry win
  pays +250 coins, and the series is played in rounds of 5: the side that wins
  more of a round gets +3,000 coins and +2 💎. Old matches count, so a pair with
  history is a rivalry straight away.
* **Spectator predictions** — `/predict` in the group while a player-vs-player
  match is live. Anyone except the two players can back a side with 100, 500,
  1K or 5K coins, once per match, until the innings break. Winners share the
  whole pool by stake, plus a 10% bonus on their own stake. If nobody backed
  the winner, or the match ends tied or unfinished, everyone is refunded.
* **Match highlights** — after every Lets Play / Challenge League match, the
  chat gets a short reel of the 3–5 biggest moments (wickets of set batters,
  fifties and hundreds, bowling hauls, sixes, big overs, maidens and swings in
  the chase), plus the *moment of the match*.
* **Weather and dew during the match** — Lets Play and Challenge League matches
  now have conditions that change as the match goes on: cloud rolls in or
  clears, the wind can pick up, and on a night match the dew *builds* — it
  arrives late in the first innings or during the chase, up to the level the
  Pitch Report forecast. Each change is announced in the over summary. There is
  **no rain**: nothing stops play, shortens an innings or sets a DLS target.

## 9. Challenge Leagues

League commands are **dynamic**: every active league answers to both
`/c<league>` and `/challenge<league>`. Reply to the person you want to play.

| Command | What it does |
|---------|--------------|
| `/cipl` · `/challengeIPL` | Start an IPL challenge |
| `/cbbl` · `/challengeBBL` | Start a BBL challenge |
| `/cint` · `/challengeINT` | Start an international challenge |
| `/c<league>` | Any extra league an admin has added on the website answers the same way |

## 10. Lets Play Tournament

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/lptour` | `/lptplay` | Play your tournament fixture (counts as the official result) 🏆 |
| `/lpt` | `/lptournament` | Tournament front page — table, fixtures, teams 🏆 |
| `/lptable` | `/lptpoints` | Points table (P W L T · Pts · NRR) |
| `/lptfixtures` | `/lptfix` | Full fixture list, with *your* next matches pulled out |
| `/lptteams` | — | Who is in the tournament |
| `/lptstats` | — | Leaderboards — runs, wickets, sixes, average, economy (top 25) |

Admin commands for running one: [Appendix B](#appendix-b--admin--owner-commands).

## 11. Challenge League Tournament

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/ctour` | `/ctournament` | Tournament front page |
| `/cttable` | `/ctpoints` | Points table |
| `/ctfixtures` | `/ctfix` | The schedule — played matches struck through |
| `/ctteams` | — | The participating teams and who owns them |
| `/ctinjuries` | `/ctinjury` | Who is ruled out injured, and for how many more matches 🚑 |
| `/clsd` | `/clschedule` `/ctsd` | One team's schedule: standing, form, next matches, results |
| `/teamtourstats` | `/teamstour` `/myteamstats` `/tts` | A team's tournament by the numbers |
| `/mvp` | `/tourmvp` `/ctmvp` | Most Valuable Player — batting + bowling + wins + POTM |

## 12. Tournament Draft

A draft is a group event; these run in the chat the draft is bound to.

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/pick` 👥 | `/pk` `/dpick` | Make your pick when you're on the clock 🎯 |
| `/dboard` 👥 | `/draftboard` | The live draft board |
| `/dsquad` 👥 | `/myteam` | A team's drafted squad |
| `/dqueue` 👥 | `/dq` | Your auto-pick wishlist — used if your clock runs out |
| `/dsearch` 👥 | `/dfind` `/dpool` | Browse the pool: who is still available |
| `/dtrade` 👥 | `/dswap` | Trade players with another franchise once the draft is done |
| `/dtrades` 👥 | `/dtradelog` | The trade log |
| `/dtradecancel` 👥 | `/dtradex` | Cancel a trade offer you made |

## 13. Franchise Auction

An auction is a group event; these run in the chat the auction is bound to.

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/bid` 👥 | `/bd` | Bid for the player on the block — bare `/bid` is the next minimum |
| `/aboard` 👥 | `/auctionboard` | The live auction board, with quick-bid buttons |
| `/apurse` 👥 | `/apurses` | Every purse, or one franchise's squad |
| `/artm` 👥 | `/rtm` | Answer a Right To Match on your former player |
| `/ainfo` 👥 | `/amenu` | Where the auction stands, with a button for every view below |
| `/asets [page]` 👥 | — | Every set in running order, numbered — tap one for its first few players, or press its number for the whole set |
| `/anextset` 👥 | — | The next set's players |
| `/anextplayer` 👥 | `/anextplayers` | Who comes to the block next |
| `/asquad` 👥 | `/amysquad` | Your squad (or name a franchise), with purse and max bid |
| `/asoldlist` 👥 | — | Every player sold, set by set |
| `/aunsoldlist` 👥 | — | The ⚡ Unsold / Accelerated set |

Every new player is announced with his **player card** and a fresh **pinned
board**; bids are announced as one short line per burst.

## 14. Fantasy league

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/fantasy` | `/fl` | Open the weekly fantasy cricket league |
| `/myfantasy` | `/mfl` | Your fantasy squad and points |
| `/fantasyleaderboard` | `/flb` | Fantasy league leaderboard |
| `/fantasystats` | `/fstats` | Top fantasy scorers this week |
| `/fantasyguide` | `/fguide` | Fantasy rules and commands |

## 15. Mini games & social games

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/catch` | — | Catch coins using your purse |
| `/bal` | — | Your catch-game balance |
| `/lucky7` | — | Bet on two dice summing below / above / exactly 7 |
| `/powerplay` | `/pp` | Crash game — cash out before the multiplier crashes |
| `/score21` | `/s21` | Blackjack against the dealer |
| `/unscramble` 👥 | `/u` | Create an Unscramble Player lobby |
| `/ju` 👥 | — | Join the Unscramble lobby |
| `/su` 👥 | — | Start the Unscramble game (host only) |
| `/eu` 👥 | — | Leave the Unscramble lobby |
| `/cu` 👥 | — | Cancel the Unscramble lobby (host only) |
| `/wordchase` 👥 | `/wc` | Host a word-guessing game |
| `/endchase` 👥 | `/ewc` | End the running Word Chase (host only) |
| `/bluff` 👥 | — | Cricket trivia bluff duel — reply to your opponent |
| `/mole` 👥 | — | Mole Hunt social deduction game |
| `/cartel` 👥 | — | Cricket Cartel multi-role deduction game |

## 16. Quests, achievements & referrals

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/myquest` | `/mq` `/quests` | View and claim daily + monthly quest rewards |
| `/achievements` | `/ach` `/badges` | Your achievements |
| `/invite` | `/ref` `/refer` `/share` | Invite friends and view referrals |

## 17. Help & support

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/howto` | `/help` `/guide` | The interactive help guide |
| `/matchhelp` | `/mhelp` `/howtoplay` | How to play Lets Play & CIPL — the Approach game |
| `/chemhelp` | `/chemguide` | How Team Chemistry works |
| `/fantasyguide` | `/fguide` | How fantasy works |
| `/pitchstats` | `/pstats` `/ps` | What each pitch does to the numbers |
| `/report` | — | Send feedback or report an issue to the admins |
| `/feedback` | `/fb` | Send feedback or a bug report |
| `/ewm` 👥 | — | Enable welcome messages for this chat (group admins) |
| `/dwm` 👥 | — | Disable welcome messages for this chat (group admins) |

## 18. Mini App

| Command | Aliases | What it does |
|---------|---------|--------------|
| `/app` | — | Open the Cricket Simulator Mini App |
| `/ipl160` | `/16o` `/iplsim` | Open the 16-0 IPL season simulator (Mini App) |
| `/wpm` | — | Match lobby up to 20 overs in the Mini App |
| `/wpmbot` | `/wpmb` | Play a bot in the Mini App |

---

# Appendix A — Usage examples, command by command

Everything in `<angle brackets>` is yours to fill in. Everything in
`[square brackets]` is optional.

## A1 · Account & rewards

| Usage | Example |
|-------|---------|
| `/start` | `/start` |
| `/debut` | `/debut` |
| `/claim` | `/claim` |
| `/daily` | `/daily` |
| `/gspin` | `/gspin` |
| `/redeem <CODE>` | `/redeem WELCOME100` |
| `/coins2gems <coins>` | `/coins2gems 5000` |
| `/membership` | `/membership` |
| `/notifications` | `/notifications` |

## A2 · Squad & roster

| Usage | Example |
|-------|---------|
| `/myroster` | `/myroster` |
| `/playerinfo <player name>` | `/playerinfo Virat Kohli` |
| `/searchpl <player name>` | `/searchpl kohli` |
| `/searchovr <rating>` | `/searchovr 88` |
| `/buypl <player name>` | `/buypl Jasprit Bumrah` |
| `/releasepl <name>` · `/releasepl <position>` | `/releasepl 14` |
| `/releasemultiple <from> <to>` | `/releasemultiple 20 30` |
| `/trade @username` | `/trade @rahul` |
| `/owners <player name>` | `/owners Virat Kohli` |
| `/teamname <name>` | `/teamname Delhi Dynamos` |
| `/cmucareer` | `/cmucareer` |
| `/cmuchange` | `/cmuchange` |

## A3 · Playing XI

| Usage | Example |
|-------|---------|
| `/playingxi` | `/playingxi` |
| `/ximage` | `/ximage` |
| `/autobuild` | `/autobuild` |
| `/swap <pos1> <pos2>` | `/swap 3 7` |
| `/setbo <from> <to>` | `/sbo 2 11` |
| `/setcaptain <player name>` | `/setcaptain Rohit Sharma` |
| `/cmuchem` | `/cmuchem` |
| `/change <out> <in>` | `/change 2 13` |

## A4 · Traits

| Usage | Example |
|-------|---------|
| `/traits` | `/traits` |
| `/traitlist [category]` | `/traitlist batting` |
| `/traitshop` | `/traitshop` |
| `/traitapply` | `/traitapply` |
| `/traitupgrade` | `/traitupgrade` |
| `/traitreplace` | `/traitreplace` |
| `/removetrait` | `/removetrait` |
| `/selltrait` | `/selltrait` |
| `/tradetrait @username` | `/tradetrait @rahul` |

## A5 · Stats

| Usage | Example |
|-------|---------|
| `/stats <player name>` | `/stats Virat Kohli` |
| `/gstats <player name>` | `/gstats Virat Kohli` |
| `/statscl <player name>` | `/statscl Virat Kohli` |
| `/statstour <player name>` | `/statstour Virat Kohli` |
| `/h2h @username` | `/h2h @rahul` |
| `/rank [@username]` | `/rank` |
| `/ranked` | `/ranked` |
| `/rivalry [@username]` | `/rivalry @rahul` |
| `/halloffame [bat\|bowl\|team\|career]` | `/hof bowl` |
| `/pitchstats [pitch\|approaches] [lp\|cl]` | `/pitchstats green lp` |
| `/lastscorecard` | `/lastscorecard` |

## A6 · Matches

| Usage | Example |
|-------|---------|
| `/letsplay @username` *(or reply)* | `/letsplay @rahul` |
| `/predict [match id]` *(in the group, while a match is live)* | `/predict` |
| `/playmatch @username` | `/playmatch @rahul` |
| `/cm @username` | `/cm @rahul` |
| `/cdraft` | `/cdraft` |
| `/sim [T10\|T20\|<overs 1-20>]` | `/sim T10` |
| `/wpm <overs 1-20> [@user]` | `/wpm 10 @rahul` |
| `/vsbot <overs 1-20>` *(default 5)* | `/vsbot 10` |
| `/wpmbot <overs 1-20>` | `/wpmbot 20` |
| `/lpbot` | `/lpbot` |
| `/ciplbot [league]` | `/ciplbot ipl` |
| `/pbo @username` | `/pbo @rahul` |
| `/cipl` *(reply to a user)* | `/cipl` |
| `/matchinfo` | `/matchinfo` |
| `/resume` · `/rcl` | `/rcl` |
| `/endmatch` | `/endmatch` |

## A7 · Tournaments

| Usage | Example |
|-------|---------|
| `/lptour @username` *(or reply)* | `/lptour @rahul` |
| `/lpt` · `/lptable` · `/lptfixtures` | `/lptable` |
| `/ctour` · `/cttable` · `/ctfixtures` | `/cttable` |
| `/clsd <team name>` | `/clsd Mumbai Indians` |
| `/teamtourstats [team name]` | `/teamtourstats Mumbai Indians` |
| `/mvp` | `/mvp` |
| `/cmtours @user2` | `/cmtours @rahul` |
| `/cltour @username` | `/cltour @rahul` |

## A8 · Tournament Draft

| Usage | Example |
|-------|---------|
| `/pick <player name>` | `/pick Virat Kohli` |
| `/dboard` | `/dboard` |
| `/dsquad [team]` | `/dsquad Mumbai` |
| `/dqueue <player name>` | `/dqueue Virat Kohli` |
| `/dsearch <filters>` | `/dsearch platinum bowler available` |
| `/dtrade <team>` | `/dtrade Mumbai` |
| `/dtrades` | `/dtrades` |

## A9 · Franchise Auction

| Usage | Example |
|-------|---------|
| `/bid [amount]` | `/bid 15` · `/bid 75L` · `/bid` |
| `/aboard` | `/aboard` |
| `/apurse [franchise]` | `/apurse Mumbai` |
| `/artm yes\|no` | `/artm yes` |
| `/asquad [franchise]` | `/asquad` · `/asquad Chennai` |
| `/anextplayer [n]` | `/anextplayer 10` |

## A10 · Games

| Usage | Example |
|-------|---------|
| `/catch <bet> <height>` | `/catch 500 3` |
| `/lucky7 <bet>` | `/lucky7 200` |
| `/powerplay <bet> <target>` | `/powerplay 500 2.5` |
| `/score21 <bet>` | `/score21 300` |
| `/unscramble` → `/ju` → `/su` | `/unscramble` |
| `/wordchase` → `/endchase` | `/wordchase` |
| `/bluff` *(reply to your opponent)* | `/bluff` |
| `/mole` · `/cartel` | `/mole` |

## A11 · Help

| Usage | Example |
|-------|---------|
| `/howto` | `/howto` |
| `/matchhelp [topic]` | `/matchhelp approach` |
| `/report <your message>` | `/report scorecard image missing` |
| `/feedback <your message>` | `/feedback please add T10 tours` |

---

# Appendix B — Admin & owner commands

🔒 These are published only into the DMs of the Telegram IDs in
`config.ADMIN_IDS`; players never see them in the slash menu.

## B1 · Moderation & content

| Usage | What it does |
|-------|--------------|
| `/logoqueue` | Review team logos waiting for approval |
| `/logounhold <telegram id>` | Let a held user send a team logo again |
| `/previewsummary` | Render a sample match summary card |
| `/setcardid <player name> \| <file_id>` | Pin a Telegram photo as a player's card |
| `/setmilestone` | Set in-match milestone messages and media |
| `/grant <tier> <telegram_id>` | Grant a subscription tier (e.g. `/grant Upgrade Diamond 12345`) |
| `/clearmatches` | Clear every stuck match in this chat |
| `/removematch @user` | Pull one user out of their active match |
| `/testwpm [match_id]` | Mini App match diagnostic |
| `/cdraftset` | Read or change the `/cdraft` rating band and allowed editions |
| `/tourallow <telegram_id>` | Allow a user to create tours |
| `/tourblock <telegram_id>` | Block a user from creating tours |
| `/tourallowlist` | Show the tournament-command allowlist |

## B2 · Broadcast

| Usage | What it does |
|-------|--------------|
| `/frwd` *(reply to a message)* | Copy the replied message to every active chat. Registered **only** when `FORWARD_ONLY_MODE=true`, where it is the single command the bot still answers — an admin-only broadcast doorway while gameplay is paused |
| `/frwd_grp` *(reply)* | Forward to all active groups |
| `/frwd_prvt` *(reply)* | Forward to all active private chats |

## B3 · Tournament Draft

| Usage | What it does |
|-------|--------------|
| `/dadmin` | The Tournament Draft reference card |
| `/dnew <draft name>` | Create a draft and bind it to this group — `/dnew Summer Mega Draft` |
| `/dbind <id>` | Bind an existing draft to this group |
| `/dstart` · `/dpause` · `/dresume` | Start, pause and resume the draft clock |
| `/dtimer <minutes>` | Minutes allowed per pick — `/dtimer 15` |
| `/dhome <country>` · `/dhome sync` | Set the home country and re-flag the pool — `/dhome England` |
| `/dpin on\|off` | Auto-pin the latest pick |
| `/dco <team> \| <telegram id>` | Add a co-owner who may pick — `/dco Mumbai \| 123456789` |
| `/dskip` | Resolve the pick on the clock now |
| `/dautopick` | Grant the team on the clock a random pick of its tier |
| `/dundo` | Roll the last pick back |
| `/dadd <team> \| <player>` | Put a player on a squad — `/dadd Mumbai \| Virat Kohli` |
| `/ddrop <player>` | Send a drafted player back to the pool |
| `/dtradelock on\|off` | Close or reopen the trade window |
| `/dpublish` | Publish drafted squads as a Challenge League |
| `/dcancel` | Cancel the draft |

## B4 · Franchise Auction

| Usage | What it does |
|-------|--------------|
| `/adminhelp` · `/auction` | Every Franchise Auction admin command, section by section |
| `/anew <name>` | Create an auction and bind it here — `/anew Season 2` |
| `/abind <name>` | Bind an existing auction to this group |
| `/astart` · `/apause` · `/aresume` | Start, pause and resume the auction clock |
| `/anext` | Put the next lot on the block |
| `/aextend [seconds]` | Add seconds to the lot on the block |
| `/asold` · `/aunsold` | Sell at the standing bid, or pass the lot |
| `/aunsold <lot no \| player>, …` | Send those players unsold — `/aunsold 67, 88, 89, 53`. Only players on no squad and with no standing bid go; the rest are named with the reason |
| `/aforce <player \| lot no>` (`/aforcenext`) | That player next — `/aforce Tilak Varma`. Opens at once if the auction is live and nothing is on the block, otherwise straight after the current lot. A withdrawn or unsold player is brought back on the way |
| `/aundobid` | Void the standing bid |
| `/awithdraw <player name>` | Pull a player out of the auction |
| `/areinstate <player \| lot no>, …` (`/aunwithdraw`) | `/awithdraw`'s opposite — a withdrawn (or unsold) player goes back to the end of the queue |
| `/aincrement 2:10L, 5:20L, 10:25L, 50L` (`/aincrements`) | Bid increments: under ₹2 Cr the least raise is ₹10 L, and so on; one amount alone is a flat step, `reset` restores the default, bare reads it back. Also on the website's setup page |
| `/atimer <seconds>` | Seconds allowed per lot — `/atimer 45` |
| `/asnipe <window> <extend> <max>` | Anti-snipe window, extension and cap — `/asnipe 10 10 5` |
| `/afocus on\|off` | Focus mode: while the auction is live or paused, this group answers auction commands and nothing else (default **on**). Bare `/afocus` reads it back |
| `/adirect on\|off` | Direct bids: may a bidder type their own amount (`/bid 12`), or only take the next step? Default **on**. Off still allows bare `/bid` and the board's buttons |
| `/agrant <franchise> \| <amount>` | Correct a franchise purse |
| `/aco <franchise> \| <telegram id>` | Add a co-owner who may bid |
| `/aretain <franchise> \| <player> \| [price]` | Retain a player — `/aretain Mumbai \| Virat Kohli \| 18` |
| `/aunretain <player name>` | Release a retained player into the pool |
| `/aretlock [on]` · `/aretention` | Retention state, and close the window |
| `/artmset <cards> [seconds] [max]` · `/artmset off` | Right To Match rules — `/artmset 2 45 200` |
| `/artmcards <franchise> <cards>` | One franchise's own RTM count — `/artmcards Mumbai 3` |
| `/artmforce yes\|no` | Answer an open RTM on the franchise's behalf |
| `/artmundo <player>` | Undo a match — card and money both back |
| `/aaccel [go]` | Accelerated round — re-list everything unsold |
| `/aclone <name>` | Start the next season from this one — `/aclone Season 3` |
| `/apublish` | Publish bought squads as a Challenge League |
| `/acancel` | Cancel the auction |
| `/apool <min>-<max> [\| set] [\| all]` | Add every card in a rating range to the pool as one set — `/apool 85-90 \| Marquee` |
| `/anextset <set \| 80-85>` | Make a set, or every queued player in a rating range, come next |
| `/asetorder A, B, C` | Order the whole queue by set — the numbers `/asets` shows are the order it runs in, and the website's Sets card does the same with ↑ / ↓ |
| `/aaccelmode on\|off` | The automatic ⚡ Accelerated round for unsold players (default on) |
| `/aretainforce <franchise> \| <player> \| [price]` | Retain at once, without the franchise's Accept |
| `/aoffers` · `/aretcancel <player>` | Retention offers waiting, and withdraw one |
| `/acall [message]` | Tag every owner and co-owner |
| `/aremoveteam <franchise> [\| confirm]` | Remove a team: players back in the pool, its purse shared equally |
| `/aadminadd <id \| @user \| reply>` | Bot admins only: make someone an auction admin |
| `/aadminremove <id \| @user \| reply>` · `/aadmins` | Remove one, or list them |

`/aretain` now **offers** the retention: the franchise's owner or a co-owner
presses ✅ Accept (nobody else can), and only then is the player kept.
**Auction admins** may use every command in this table and no other admin
command in the bot.

**Focus mode.** While an auction bound to a group is live or paused, that group
answers **only** auction commands — `/bid`, `/aboard`, `/apurse`, `/ainfo`,
`/asquad` and the rest — and refuses everything else with one line pointing at
a DM, where the whole rest of the bot still works. Talking is never blocked,
non-auction buttons are refused the same way, admins are never locked out, and
an auction still in setup locks nothing. `/afocus off` turns it off for that
auction; the ⚙️ Settings fold on the auction's page has the same switch.

**One pair of hands per franchise.** Whoever bids first for a franchise on a lot
holds that lot: a second owner or co-owner of the same side is refused by name
until the next lot, because two of them bidding one player is one franchise
raising its own price. An admin bidding from the console is exempt.

**Every auction card has a ❌ Close**, and belongs to whoever asked for it — the
`/ainfo` menu and the 🗂 Sets card can only be driven by the person who sent the
command, and an admin command's answer closes the same way. The pinned board is
the exception on both counts: its quick-bid and RTM buttons are the room's, and
it cannot be closed. Nor are one-line refusals or a pending retention offer
(answer it, or withdraw it with `/aretcancel`).

**Changing the opening purse on the setup page now moves every franchise** to
the new purse — what has already been spent stays spent, and a cut that would
overdraw somebody is refused by name.

## B5 · Lets Play Tournament

| Usage | What it does |
|-------|--------------|
| `/lptadmin` | The Lets Play tournament reference card |
| `/lptnew <name> [\| format \| playoffs \| max teams]` | `/lptnew Summer Smash \| double \| top4 \| 10` |
| `/lptadd <telegram_id> [\| Team Name]` | Enter a player in the tournament |
| `/lptremove <telegram_id>` | Take a player out |
| `/lptrename <telegram_id> \| New Name` | Rename a participant's team |
| `/lptsync` | Resolve placeholder names from Telegram accounts |
| `/lptschedule [single\|double]` | Generate the round-robin fixture list |
| `/lptknockout [top4\|playoffs\|knockout]` | Seed the playoff bracket from the table |
| `/lptstart` · `/lptpause` · `/lptresume` | Run the tournament |
| `/lptcomplete` · `/lptcancel` | Finish or cancel it |
| `/lptreset` | Clear every result, keeping teams and fixtures |
| `/lptlist` · `/lptuse <id>` · `/lptdelete <id>` | List, switch and delete tournaments |

## B6 · Running tournament (points & fixtures)

| Usage | What it does |
|-------|--------------|
| `/tpoints <TEAM> \| <±N> \| <reason>` | Dock or award points — `/tpoints MI \| +2 \| walkover` |
| `/tpointsclear <TEAM>` | Clear a team's points adjustment |
| `/taddmatch <match number>` · `/addmatch` *(reply to a scorecard)* | Record a fixture played off the bot |
| `/tfixsync` | Un-stick fixtures still showing as live |
| `/remindmatch [team] [vs team] [force]` | Nudge two teams to play their pending fixture |

---

# Appendix C — Where each command works

The bot publishes three separate slash menus (private, group, and admin DMs),
because Telegram caps each scope at 100 commands.

### 💬 DM only

In a group these post a one-line deep link instead of the answer
(`services/dm_only.py`):

`/stats` · `/gstats` · `/statscl` · `/setbo` · `/recentmatches` · `/traits` ·
`/traitboost` · `/traitshop` · `/traitapply` · `/traitupgrade` ·
`/traitreplace` · `/removetrait` · `/selltrait`

### 👥 Group only

These refuse to run in a private chat — they need a second player or a group
audience:

`/bluff` · `/cartel` · `/mole` · `/wordchase` · `/endchase` · `/cltour` ·
`/unscramble` · `/ju` · `/eu` · `/su` · `/cu` · `/ewm` · `/dwm` ·
`/lptour` · `/lpt` · `/lptable` · `/lptfixtures` · `/lptteams` · `/lptstats` ·
`/pick` · `/dboard` · `/dsquad` · `/dqueue` · `/dsearch` · `/cdraft` ·
`/bid` · `/aboard` · `/apurse` · `/artm` · `/predict`

### 🔑 Private-chat entry points

Deep links, typed-reply flows and long rules pages, listed only in the DM menu:

`/start` · `/debut` · `/redeem` · `/feedback` · `/notifications` · `/invite` ·
`/chemhelp` · `/fantasyguide` · `/myfantasy` · `/fantasyleaderboard` ·
`/fantasystats` · `/cmuchange`

### Not in any slash menu

These work from the keyboard but are deliberately unpublished, because both
player menus sit at Telegram's 100-command ceiling: `/pitchstats`, `/dtrade`,
`/dtrades`, `/bid`, `/aboard`, `/apurse`, and the auction views `/ainfo`,
`/asets`, `/anextset`, `/anextplayer`, `/asquad`, `/asoldlist`,
`/aunsoldlist` (all behind `/ainfo`'s buttons), and `/rank`, `/ranked`,
`/rivalry`, `/predict` and `/halloffame` (all in `/help`).

### Disabled

`/wsp`, `/wspbot` and `/wspb` (auto-simulated "watch mode") are turned off.
The handlers are still in the tree and old `wsp` matches remain viewable in the
admin panel, but the commands are not registered.

---

# Appendix D — Alias index (A→Z)

| Alias | Canonical command |
|-------|-------------------|
| `/16o` | `/ipl160` |
| `/ab` | `/autobuild` |
| `/ach` | `/achievements` |
| `/addmatch` | `/taddmatch` 🔒 |
| `/arelease` | `/aunretain` 🔒 |
| `/arelistall` | `/aaccel` 🔒 |
| `/auctionboard` | `/aboard` |
| `/ahelp` | `/adminhelp` 🔒 |
| `/acallteams` | `/acall` 🔒 |
| `/amenu` | `/ainfo` |
| `/amysquad` | `/asquad` |
| `/anextplayers` | `/anextplayer` |
| `/aunpause` | `/aresume` 🔒 |
| `/apurses` | `/apurse` |
| `/anextseason` | `/aclone` 🔒 |
| `/aretention` | `/aretlock` 🔒 |
| `/artmrules` | `/artmset` 🔒 |
| `/b` | `/buypl` |
| `/badges` | `/achievements` |
| `/battingorder`, `/bo` | `/setbo` |
| `/bd` | `/bid` |
| `/best11` | `/autobuild` |
| `/bowlout` | `/pbo` |
| `/bstatus`, `/ping` | `/botstatus` |
| `/buy` | `/buypl` |
| `/bvb` | `/botvsbot` |
| `/c` | `/claim` |
| `/c2g` | `/coins2gems` |
| `/cap`, `/captain` | `/setcaptain` |
| `/career` | `/cmucareer` |
| `/careerchange` | `/cmuchange` |
| `/challengedraft` | `/cdraft` |
| `/challengeiplbot`, `/ciplb` | `/ciplbot` |
| `/chem`, `/chemistry`, `/cmuchemistry` | `/cmuchem` |
| `/chemguide` | `/chemhelp` |
| `/clearmatch` | `/clearmatches` |
| `/clschedule`, `/ctsd` | `/clsd` |
| `/cltours` | `/cltour` |
| `/createtour` | `/cmtours` |
| `/code` | `/redeem` |
| `/ctmvp`, `/tourmvp` | `/mvp` |
| `/ctfix` | `/ctfixtures` |
| `/ctinjury` | `/ctinjuries` |
| `/ctournament` | `/ctour` |
| `/ctpoints` | `/cttable` |
| `/d` | `/debut` |
| `/dauto` | `/dautopick` 🔒 |
| `/dcountry` | `/dhome` 🔒 |
| `/dfind`, `/dpool` | `/dsearch` |
| `/dl` | `/daily` |
| `/dpick`, `/pk` | `/pick` |
| `/dq` | `/dqueue` |
| `/draftboard` | `/dboard` |
| `/drelease` | `/ddrop` 🔒 |
| `/dsign` | `/dadd` 🔒 |
| `/dswap` | `/dtrade` |
| `/dtradelog` | `/dtrades` |
| `/dtradex` | `/dtradecancel` |
| `/dunpause` | `/dresume` 🔒 |
| `/elo`, `/myrank` | `/rank` |
| `/em` | `/endmatch` |
| `/ewc` | `/endchase` |
| `/fb` | `/feedback` |
| `/fguide` | `/fantasyguide` |
| `/fl` | `/fantasy` |
| `/flb` | `/fantasyleaderboard` |
| `/fstats` | `/fantasystats` |
| `/globalstats` | `/gstats` |
| `/gs` | `/gspin` |
| `/guide`, `/help` | `/howto` |
| `/headtohead` | `/h2h` |
| `/hof`, `/records` | `/halloffame` |
| `/howtoplay`, `/mhelp` | `/matchhelp` |
| `/info`, `/pi` | `/playerinfo` |
| `/ip` | `/impact` |
| `/iplsim` | `/ipl160` |
| `/kickmatch`, `/rmatch` | `/removematch` 🔒 |
| `/ladder`, `/rladder` | `/ranked` |
| `/lb`, `/top`, `/leaderboard` | `/cmuleaderboard` |
| `/letsplaybot`, `/lpb` | `/lpbot` |
| `/lm` | `/lastmatch` |
| `/lp` | `/letsplay` |
| `/lptfix` | `/lptfixtures` |
| `/lptplay` | `/lptour` |
| `/lptpoints` | `/lptable` |
| `/lptournament` | `/lpt` |
| `/lptaddteam` | `/lptadd` 🔒 |
| `/lptremoveteam` | `/lptremove` 🔒 |
| `/lptunpause` | `/lptresume` 🔒 |
| `/lsc`, `/scorecard` | `/lastscorecard` |
| `/market`, `/pmarket` | `/playermarket` |
| `/match` | `/playmatch` |
| `/matches`, `/recent` | `/recentmatches` |
| `/matchreminder`, `/nudge`, `/remindmatches` | `/remindmatch` 🔒 |
| `/me`, `/profile` | `/myprofile` |
| `/member`, `/mysub`, `/plans`, `/subscription` | `/membership` |
| `/mfl` | `/myfantasy` |
| `/mi` | `/matchinfo` |
| `/mq`, `/quests` | `/myquest` |
| `/mr`, `/roster` | `/myroster` |
| `/myteam` | `/dsquad` |
| `/myteamstats`, `/teamstour`, `/tts` | `/teamtourstats` |
| `/open` | `/openpack` |
| `/ownedby`, `/whoowns` | `/owners` |
| `/p` | `/purse` |
| `/packs`, `/shop` | `/buypack` |
| `/pm` | `/playmatch` |
| `/pp` | `/powerplay` |
| `/pred` | `/predict` |
| `/ps`, `/pstats` | `/pitchstats` |
| `/pxi`, `/xi` | `/playingxi` |
| `/r` | `/resume` |
| `/ref`, `/refer`, `/share` | `/invite` |
| `/rel`, `/release` | `/releasepl` |
| `/relm`, `/rm` | `/releasemultiple` |
| `/resumecl` | `/rcl` |
| `/rival`, `/rivalries` | `/rivalry` |
| `/rtm` | `/artm` |
| `/rtrait` | `/removetrait` |
| `/s` | `/start` |
| `/s21` | `/score21` |
| `/sbo` | `/setbo` |
| `/search`, `/sp` | `/searchpl` |
| `/simmatch` | `/sim` |
| `/sms` | `/setmilestone` 🔒 |
| `/so` | `/searchovr` |
| `/spectate` | `/botmatch` |
| `/st` | `/stats` |
| `/straits`, `/tsell` | `/selltrait` |
| `/swap`, `/swappl` | `/swapplayers` |
| `/tapply` | `/traitapply` |
| `/tboost` | `/traitboost` |
| `/teamlogo` | `/setteamlogo` |
| `/tfixheal` | `/tfixsync` 🔒 |
| `/tlist`, `/traitcatalogue` | `/traitlist` |
| `/tn` | `/teamname` |
| `/tours` | `/mytours` |
| `/tpts` | `/tpoints` 🔒 |
| `/tptsclear` | `/tpointsclear` 🔒 |
| `/tr` | `/trade` |
| `/trep` | `/traitreplace` |
| `/trtrade`, `/ttrade` | `/tradetrait` |
| `/tshop` | `/traitshop` |
| `/tt` | `/traits` |
| `/tup` | `/traitupgrade` |
| `/u` | `/unscramble` |
| `/vsb` | `/vsbot` |
| `/wc` | `/wordchase` |
| `/wpmb` | `/wpmbot` |
| `/xiimg`, `/xipic` | `/ximage` |

---

# Appendix E — Cooldowns & limits

| Thing | Value |
|-------|-------|
| `/claim` | Hourly (faster on paid tiers) |
| `/daily` | Every 24h, grows with your streak |
| `/gspin` | Every 8h |
| `/cmuweekly` | Every 7 days (Platinum / Diamond) |
| `/sim`, `/wpm`, `/wpmbot`, `/vsbot`, `/letsplay` | Up to 20 overs |
| `/vsbot` default | 5 overs |
| Coins → gems | 1000 coins = 1 gem |
| Challenge League setup timeout | 5 minutes per turn, with a reminder at 4:30 (`CL_SELECT_WINDOW_SECONDS`) |
| Slash menu size | 100 commands per scope (Telegram's ceiling) |
| Ranked: same pair | 3 rated matches per UTC day |
| `/predict` stake | 100 / 500 / 1,000 / 5,000 coins, one prediction per match, closes at the innings break |

Cooldowns and rewards for many commands are configurable by the operator in the
admin website (`BotCommand` / `CommandReward` rows), so an individual
deployment may differ from the defaults above.

---

*Related docs:* [`docs/`](docs/) holds the design notes behind most of these
systems — e.g. [`docs/franchise-auction.md`](docs/franchise-auction.md),
[`docs/player-draft.md`](docs/player-draft.md),
[`docs/team-chemistry.md`](docs/team-chemistry.md) and
[`docs/pitch-types.md`](docs/pitch-types.md).
