# 🏏 CMU Cricket — The Complete Match & Tournament Guide

Every way to play a match, every tour and tournament, and exactly how to start
each one — written for players. Get this file any time from the bot with
`/matchguide` (Markdown) or `/matchguide pdf` (PDF), or read just one part in
chat with `/matchguide <topic>` — e.g. `/matchguide letsplay`.

> **Legend.** `<required>` is yours to fill in, `[optional]` may be left out.
> 👥 = group only · 💬 = DM (private chat with the bot) only.
> **Reply** = send the command as a *reply* to your opponent's message.
> **Ranked** = moves your ladder rating (see [Ranked ladder](#-ranked-ladder-rivalries--rewards)).

---

## 📑 Contents

| # | Part | Topic key |
|---|------|-----------|
| 1 | [Getting started](#-1-getting-started) | `start` |
| 2 | [Which match should I play?](#-2-which-match-should-i-play) | `choose` |
| 3 | [How a match plays](#-3-how-a-match-plays) | `howtoplay` |
| 4 | [Lets Play](#-4-lets-play) | `letsplay` |
| 5 | [Lets Play Multi](#-5-lets-play-multi) | `lpmulti` |
| 6 | [Challenge Leagues — CIPL, BBL, INT…](#-6-challenge-leagues--cipl-bbl-int) | `cipl` |
| 7 | [Multi League & The Hundred](#-7-multi-league--the-hundred) | `cmulti` |
| 8 | [Challenge Draft](#-8-challenge-draft) | `cdraft` |
| 9 | [Play Match — ball by ball](#-9-play-match--ball-by-ball) | `playmatch` |
| 10 | [Two-wicket Challenge](#-10-two-wicket-challenge) | `cm` |
| 11 | [Mini App match lobby](#-11-mini-app-match-lobby) | `wpm` |
| 12 | [Instant Sim — T10, T20, ODI, Test](#-12-instant-sim--t10-t20-odi-test) | `sim` |
| 13 | [Play against the bot](#-13-play-against-the-bot) | `bot` |
| 14 | [Watch bot vs bot](#-14-watch-bot-vs-bot) | `botvsbot` |
| 15 | [Super Over & Bowl-out](#-15-super-over--bowl-out) | `superover` |
| 16 | [Challenge League Tour (series)](#-16-challenge-league-tour-series) | `cltour` |
| 17 | [Friendly Tours](#-17-friendly-tours) | `tours` |
| 18 | [Lets Play Tournament](#-18-lets-play-tournament) | `lpt` |
| 19 | [Challenge League Tournament](#-19-challenge-league-tournament) | `ctour` |
| 20 | [Tournament Draft](#-20-tournament-draft) | `draft` |
| 21 | [Franchise Auction](#-21-franchise-auction) | `auction` |
| 22 | [Auction League — solo career](#-22-auction-league--solo-career) | `rcpl` |
| 23 | [League Seasons](#-23-league-seasons) | `seasons` |
| 24 | [Fantasy League](#-24-fantasy-league) | `fantasy` |
| 25 | [Ranked ladder, rivalries & rewards](#-25-ranked-ladder-rivalries--rewards) | `ranked` |
| 26 | [During a match — pause, resume, fix](#-26-during-a-match--pause-resume-fix) | `controls` |
| 27 | [Troubleshooting & FAQ](#-27-troubleshooting--faq) | `faq` |

---

## 🚀 1. Getting started
<!-- topic: start begin newbie setup -->

The bot is built around a community. Matches, tournaments, drafts and auctions
happen in the **official group**; announcements, events and downtime notices go
out in the **official channel**. Both links are at the bottom of every `/start`
message.

### Your first ten minutes

| Step | Do this | Where |
|------|---------|-------|
| 1 | Join the **official channel** | link under `/start` |
| 2 | Join the **official group** | link under `/start` |
| 3 | Open a private chat with the bot and send `/start` | DM |
| 4 | `/debut` — create your account and get your starting squad | DM |
| 5 | `/claim`, `/daily`, `/gspin` — your first extra cards and coins | DM |
| 6 | `/autobuild` — the bot picks your best legal Playing XI | DM |
| 7 | `/setcaptain <player>` — choose your captain | DM |
| 8 | `/setbo` — check or change your batting order | DM |
| 9 | `/teamname <name>` — name your side | DM |
| 10 | Play! `/lpbot` to practise, then `/letsplay @friend` in the group | DM / group |

```text
/start
/debut
/claim
/autobuild
/setcaptain Virat Kohli
/teamname Delhi Dynamos
/lpbot                ← a practice match against the AI
/letsplay @rahul      ← your first real match (in the group)
```

### Good to know before you play

* **You need a legal Playing XI.** Most matches check your top 11 before the
  invite is even sent. `/playingxi` shows it, `/autobuild` fixes it,
  `/ximage` draws it, `/cmuchem` shows its Team Chemistry.
* **New-player journey.** After `/debut` you get a checklist (join the group,
  `/claim`, `/daily`, play a match, open a pack, win a match). Each step pays
  coins/gems the first time; finishing them all pays a bonus. `/start` shows
  your progress.
* **Members-only (Rookie) mode / Official GC.** If the operator has switched
  these on, `/start` tells you; see `/membership` and tap **✅ I've joined**
  after joining the group.
* **Help in chat:** `/howto` (the whole bot), `/matchhelp` (the Approach game,
  12 pages), `/tourhelp` (every tournament command), `/matchguide` (this file).

---

## 🧭 2. Which match should I play?
<!-- topic: choose which compare modes types -->

| I want to… | Play | Squad used | Overs | Ranked | Rewards |
|-----------|------|------------|-------|--------|---------|
| Beat a friend with **my own cards** | `/letsplay` | your roster + traits | 20 (or 1–20) | ✅ (5+ overs) | ✅ |
| Practise my roster vs the AI | `/lpbot` | your roster | 20 | ❌ | ❌ |
| My roster vs a **real franchise** | `/lpmulti` | yours vs a league team | 20 (or 1–20) | ❌ | ❌ |
| Play with **franchise squads** (IPL, BBL…) | `/cipl` `/cbbl` `/cint` `/c<league>` | league teams, no traits | 20 (or 1–20) | ✅ | ✅ |
| Two **different** leagues, or The Hundred | `/cmulti` | league teams | 20 / 1–20 / 100 balls | ✅ | ✅ |
| Practise league cricket vs the AI | `/ciplbot` | league teams | 20 | ❌ | ❌ |
| **Draft** both XIs pick by pick | `/cdraft` 👥 | drafted | 20 | ✅ | ✅ |
| Control **every ball** (delivery + shot) | `/playmatch` | your roster | 1–20 (guest picks) | ✅ | ✅ |
| A quick 2-wicket shoot-out | `/cm` | your roster | up to 2 | — | ✅ |
| Play inside the **Mini App** | `/wpm`, `/wpmbot` | your roster | 1–20 | `/wpm` ✅ | ✅ |
| Result **instantly**, no clicking | `/sim` | your roster | T10/T20/ODI/Test/1–20 | ❌ | ❌ |
| A quick game vs a bot team | `/vsbot` | your roster | 1–20 (default 5) | ❌ | ❌ |
| Just **watch** | `/botvsbot`, `/botmatch` | bot teams | — | ❌ | ❌ |
| A **best-of series** vs one friend | `/cltour` | fixed league teams | 20 | ✅ | ✅ |
| A **competition** with a table & playoffs | `/lptour`, `/ctour` | roster / franchise | 20 | ✅ | ✅ |
| Run a franchise for a **whole season**, solo | `/rcpl` 💬 | auctioned squad | 20 | ❌ | season prizes |

**Short version:** new → `/lpbot`. With a friend → `/letsplay`. Franchise
cricket → `/cipl`. In a hurry → `/sim`. Competitive → tournaments (§18–19).

---

## 🎮 3. How a match plays
<!-- topic: howtoplay approach over engine rules -->

Lets Play, Challenge League (`/cipl`…), `/cmulti`, `/cdraft`, `/lpmulti`,
`/lpbot`, `/ciplbot`, CL Tour and tournament fixtures all use the same
**over-by-over Approach game**. `/playmatch`, `/wpm` and `/cm` are
ball-by-ball instead (§9–11).

### Setup

1. **Invite** — the guest gets Accept / Deny (30 seconds).
2. **Pitch** — the host picks one (Flat, Hard, Even, Bouncy, Dry, Green, Dusty…).
   Tournament fixtures have their pitch fixed in advance.
3. **Playing XI** — shown for both sides. Batting order is set automatically by
   batting rating; the host can tweak with `/change <out> <in>`.
4. **Traits vote** (Lets Play only) — both captains say whether traits play.
   Yes + yes = on, no + no = off, a split defers to the lower-rated XI.
5. **Toss** — the guest calls heads/tails, the winner bats or bowls.

### Every over

1. The bowling captain picks a **bowler** (max 4 overs each in a T20, never
   two in a row).
2. …then a hidden **bowling plan**: 🛡 Defensive · ⚖ Balanced · ☠️ Mixed ·
   🔥 Aggressive · 🌀 Variation.
3. The batting captain picks a hidden **batting intent**: 🛡 Defensive ·
   🔁 Rotate Strike · ⚖ Balanced · 🚀 Aggressive · 💥 Ultra Attack.
4. Both are revealed and the over is bowled ball by ball.

### What decides the over

* **Counters.** Guess the plan right and you get +8% runs; walk into a plan
  your intent can't answer and the bowler gets +10% wicket chance. Best replies:
  vs Defensive → Ultra, vs Mixed → Rotate, vs Aggressive → Aggressive,
  vs Variation → Balanced.
* **Don't be predictable.** The same pick 4 overs running gets read (worse up
  to 6); one different over resets it.
* **Phases.** Powerplay 1–6 (attack pays), Middle 7–16 (rotate; Ultra is the
  worst pick), Death 17–20 (Ultra is worth most, never block).
* **Pitch.** Each surface has a par score and changes for the 2nd innings — read
  the toss tip. `/pitchstats` shows real numbers per pitch.
* **Momentum & pressure** (−2…+2) — boundaries and partnerships build it;
  wickets, maidens and a climbing required rate pull it down.
* **Weather & dew** — cloud and wind change during the match; on a night match
  dew builds late in the match. There is no rain and no DLS.
* **Traits** (Lets Play only), **Team Chemistry** (`/chemhelp`) and **form**
  (last matches nudge ratings).

### Impact Player

One substitution per side per match, IPL-style: before any over, tap the
Impact button or send `/impact` to bring a bench player on for someone in the
XI. Overseas ✈️ subs only while your XI is under the overseas cap.

### After the match

Batting & bowling scorecards after each innings, a summary card, a **match
analysis HTML file** (phase splits, worms, win probability, every over's
approach duel), and a highlights reel of the 3–5 biggest moments. Lost a card?
`/lastscorecard`. A tie goes to a **Super Over** (§15).

> Deep dive with the live numbers: `/matchhelp` (or `/matchhelp approach`).

---

## 🤝 4. Lets Play
<!-- topic: letsplay lp friendly roster -->

**Your own collected squad, traits and all, against another player's.** The
flagship mode.

**Start it** (in the group):

```text
/letsplay @rahul          ← 20 overs
/lp 5 @rahul              ← a 5-over friendly (any 1–20)
/letsplay                 ← as a reply to their message
```

**Rules**
* Both players need a legal Playing XI — checked before the invite goes out.
* Default 20 overs; custom 1–20 for friendlies. Tournament fixtures are always 20.
* Traits are live (after the traits vote).
* Ranked when 5+ overs; pays coins, gems, quest progress and career stats.
  Under 5 overs still pays and counts for rivalries, but isn't ranked.

**Tips** — `/setbo` saves your order so every match reuses it. `/playapp` moves
the match into the Mini App so your picks stay private.

**Related** — `/lpbot` (practice vs AI), `/lptour` (tournament fixture),
`/lpmulti` (vs a franchise).

---

## 🔀 5. Lets Play Multi
<!-- topic: lpmulti lpm -->

**Your own roster vs a real Challenge League franchise** (e.g. your XI vs
Mumbai Indians).

```text
/lpmulti AI              ← you pick League → Team; the AI captains it
/lpm AI 10 app           ← 10 overs, played in the Mini App
/lpmulti @rahul          ← Rahul accepts, picks League → Team and captains it
```

* The franchise fields its best legal XI automatically; the rest of its squad
  is the Impact Player bench.
* From the pitch onwards it is the normal Lets Play flow (`/change`, toss…).
* Leagues offered are the ones admins tick **Include in Multi**.
* **Always unranked** — no stats, POTM, coins, gems or W/L.

---

## 🏟 6. Challenge Leagues — CIPL, BBL, INT…
<!-- topic: cipl cbbl cint challenge league franchise ipl bbl -->

**Franchise cricket with fixed squads** — IPL, BBL, Internationals, and any
league an admin adds. No traits: it's pure ratings and captaincy.

**Start it** — reply to your opponent's message in the group:

```text
/cipl          ← IPL challenge
/cbbl          ← BBL
/cint          ← International
/cipl 6        ← a 6-over friendly (1–20)
/challengeIPL  ← long form, same thing
```

**Flow:** host picks a team → guest picks a team → host picks the pitch → both
pick a Playing XI (numbered squad list, tap or type numbers) → toss → overs.
Each setup turn has **5 minutes** (reminder at 4:30); nobody is fined for a
challenge that times out before the match starts.

**Rules** — the league's own XI rules apply (keeper, five bowling options,
overseas cap). Ranked, pays rewards. Any admin league answers `/c<league>` and
`/challenge<league>`; add `bot` (e.g. `/ciplbot`) to play the AI.

---

## 🌐 7. Multi League & The Hundred
<!-- topic: cmulti multi hundred 100b -->

**Each side picks its own league, then a team** — e.g. MI vs Sydney Sixers.

```text
/cmulti          ← 20 overs (reply to your opponent)
/cmulti 10       ← 10 overs
/cmulti 100B     ← The Hundred: 100 balls a side
/cipl multi      ← the long form still works
```

The Hundred is always 100 balls — don't add an over count with it. Hundred
leagues, tournaments and CL Tour matches always play full length.

---

## 🃏 8. Challenge Draft
<!-- topic: cdraft draft-challenge picks -->

**Both captains build an XI pick by pick, then play.** 👥 Group only.

```text
/cdraft        ← open it; a second player joins
```

* The bot deals **11 slots**. Each slot offers two cards of the **same role
  within a point of OVR** — the captain on the clock takes one and the other
  goes to the opponent.
* Career cards are never dealt.
* After 11 slots it is an ordinary Challenge League match: pitch, XI screen,
  toss, overs, Super Over on a tie.

---

## 🎯 9. Play Match — ball by ball
<!-- topic: playmatch pm ballbyball -->

**Full control of every delivery.** The bowler picks a delivery (and length for
pacers), the batter picks a shot, every ball.

```text
/playmatch @rahul     ← or /pm, /match
```

* Uses your roster and Playing XI. After accepting, the guest picks the overs (1–20).
* Each action has a timer: a warning at 45s, a forfeit at 90s (with a fine).
* `/endmatch` ends it early — a fine scaled by balls bowled is **burned**, not
  paid to your opponent.
* Ranked and rewarded (coins per over for winner and loser).

---

## ⚡ 10. Two-wicket Challenge
<!-- topic: cm twowicket -->

A fast shoot-out: **2 wickets per innings, up to 2 overs**, in the Mini App
match flow.

```text
/cm @rahul       ← or reply to them with /cm
```

Both XIs must be legal; only one challenge can wait in a chat at a time.

---

## 📱 11. Mini App match lobby
<!-- topic: wpm miniapp app -->

**A match played inside the Cricket Simulator Mini App.**

```text
/wpm 10            ← an open 10-over lobby anyone can join
/wpm 10 @rahul     ← invite one player directly
/wpmbot 20         ← vs a bot team, in the Mini App
/app               ← just open the Mini App
```

* 1–20 overs. `/wpmbot` has an autoplay toggle to let the AI play your side.
* Any over-by-over match can also move to the Mini App mid-game with `/playapp`
  (and back with `/playchat`).

---

## 🖥 12. Instant Sim — T10, T20, ODI, Test
<!-- topic: sim simulate t10 odi test -->

**The whole match is simulated at once** — scorecard, winner and a ball-by-ball
commentary file.

```text
/sim            ← T20 vs an auto-generated Sim XI
/sim T10
/sim 12         ← custom 1–20 overs
/sim ODI        ← 50 overs
/sim Test       ← five days: sessions, new balls, follow-ons, declarations, draws
/sim            ← as a reply to a player: your XI vs theirs
```

| Format | Overs | Max per bowler | Powerplay | Death from |
|--------|-------|----------------|-----------|------------|
| T10 | 10 | 2 | 1–3 | 8 |
| T20 | 20 | 4 | 1–6 | 16 |
| ODI | 50 | 10 | 1–10 | 41 |
| Test | 5 days | — | — | — |

* Uses your saved batting order (`/setbo`); only bowlers and all-rounders bowl.
* **Cooldown: 4 hours** (shorter on paid tiers). Replying to a player is a
  challenge: it uses **both** players' cooldowns, so you can't challenge someone
  who is still on cooldown. A rejected command never costs you a sim.

---

## 🤖 13. Play against the bot
<!-- topic: bot lpbot ciplbot vsbot wpmbot ai practice -->

| Command | What it is |
|---------|-----------|
| `/lpbot [chat\|app]` | **Lets Play vs the AI captain** — your roster, 20 overs, Approach game. Pick your new batsman after each wicket |
| `/ciplbot [league] [chat\|app]` | **Challenge League vs the AI** — you pick team, pitch and XI; the bot takes another team and fields its best XI. `/ciplbot bbl app` |
| `/vsbot <overs>` | Your XI vs a pre-built bot team, in chat (1–20, default 5) |
| `/wpmbot <overs>` | The same in the Mini App |
| `/lpmulti AI` | Your roster vs an AI-captained franchise (§5) |

```text
/lpbot          ← remembers chat/app from last time
/lpbot app
/ciplbot ipl chat
/vsbot 10
```

`/lpbot` and `/ciplbot` are **unranked practice**: no stats, POTM, coins, gems,
W/L or streak — and walking away mid-over costs nothing. The bot's picks stay
hidden, just like a human's.

---

## 👀 14. Watch bot vs bot
<!-- topic: botvsbot botmatch spectate watch -->

```text
/botvsbot      ← pick two bot teams and watch them play
/botmatch      ← spectate a bot-vs-bot match
```

Pure entertainment: no rewards, no stats.

---

## 🔥 15. Super Over & Bowl-out
<!-- topic: superover tie bowlout pbo -->

### Super Over (automatic on a tie)

In `/letsplay`, `/cipl`/`/c<league>` and `/cdraft`, a tie goes to a Super Over
played like a 1-over `/playmatch` — delivery and shot picks every ball.

* Each side: max **6 legal balls or 2 wickets**, whichever first.
* Wides/no-balls add runs and are re-bowled; byes/leg byes are legal balls.
* The side that batted **second** bats first in the Super Over.
* Still level after countback? Another Super Over — players already used are spent.

### Bowl-out

A tie-breaker and a standalone game: each side picks 5 bowlers, 10 deliveries
are bowled at the stumps, most hits wins; level → sudden death, one ball each.

```text
/pbo @rahul      ← a standalone player bowl-out (or /bowlout)
```

---

## 🏆 16. Challenge League Tour (series)
<!-- topic: cltour series bestof -->

**A best-of-3, 5 or 7 series between two players** with fixed franchise teams.

```text
/cltour @rahul     ← create (or reply)
/cltour            ← show your tour and the "Play Match N" button
```

1. Host picks a league → host picks a team → guest picks a team.
2. Host picks the length: **3, 5 or 7** matches → guest accepts.
3. Each match is a normal `/cipl` match (pitch, XIs, toss, overs), always full
   length. Only the next unplayed match can be started.

---

## 🧳 17. Friendly Tours
<!-- topic: tours cmtours mytours -->

**A personal multi-match tour against one player.**

```text
/cmtours @rahul    ← create: matches (3–6) → overs (5/8/10/15/20) → invite
/mytours           ← your tours: play the next match, info, stats
```

---

## 🎖 18. Lets Play Tournament
<!-- topic: lpt lptour letsplay-tournament -->

**A league of players** — each fixture is a normal Lets Play match with your
own roster, feeding a points table with NRR, leaderboards and optional playoffs.

**How to join:** a bot admin enters you by Telegram id. Ask in the group.

**Play your fixture** (reply to your opponent, or tag them):

```text
/lptour @rahul
```

**Follow it**

| Command | Shows |
|---------|-------|
| `/lpt` | Front page — table, fixtures, teams |
| `/lptable` | Points table (P W L T · Pts · NRR) |
| `/lptfixtures` | This round's fixtures, with your next matches on top |
| `/lptteams` | Who is in it |
| `/lptstats` | Leaderboards — runs, wickets, sixes, average, economy |

**Rules the bot enforces**
1. The tournament must be **running** (not draft/paused/finished).
2. Both players must be **entered**.
3. Your pairing must have a fixture in the **open round** — rounds open one at a
   time. (A tournament with no schedule is *free play*: anyone vs anyone.)
4. Both need a legal Playing XI.
5. Fixtures are always 20 overs. The fixture is claimed at the toss, so a
   denied invite costs nothing.

Formats: single or double round-robin; playoffs none / top 4 / IPL playoffs /
knockout.

---

## 🏅 19. Challenge League Tournament
<!-- topic: ctour tournament cltournament franchise-tournament tipl -->

**A franchise tournament** — teams are owned (and co-owned) by players, squads
come from the league (or from a draft/auction), every fixture is a `/cipl`
match.

**Play your fixture** — use your league's tournament command, as a reply to the
opposing owner, e.g. `/tipl`. The pitch is fixed per fixture (home ground if
set). Owners: `/thome <team> | <stadium>` sets your home ground.

**Follow it**

| Command | Shows |
|---------|-------|
| `/ctour` | Front page |
| `/cttable` | Points table |
| `/ctfixtures` | Schedule, one round at a time |
| `/ctteams` · `/teamowner` | Teams and their owners |
| `/clsd <team>` | One team's schedule, form and results |
| `/teamtourstats [team]` | A team's tournament by the numbers |
| `/ctinjuries` | Who is injured and for how many matches |
| `/tournamentstats` · `/statstour <player>` | Stat boards · one player |
| `/mvp` | Most Valuable Player table |
| `/tteam` | Team of the Tournament |
| `/tbracket` | Playoff bracket picture |

**Several at once.** Each league can run its own tournament in parallel, with
its own commands — for `/tipl`: `/tiplinfo`, `/tipltable`, `/tiplfixtures`,
`/tiplteams`, `/tiplinjuries`, `/tiplstats`, `/tiplmvp`, `/tiplplayer <name>`.
The general commands list them all when more than one is running.

`/tourhelp` puts every tournament command on one card.

---

## 📝 20. Tournament Draft
<!-- topic: draft tournament-draft dpick -->

**How franchise owners build their squads, live, pick by pick**, from a player
pool the admin uploads. 👥 Runs in the group it is bound to.

| Command | What it does |
|---------|--------------|
| `/pick <player>` | Make your pick when you're on the clock |
| `/dboard` | Live board — who's up, time left, recent picks |
| `/dsearch [filters]` | Browse the pool — 🟢 available / 🔴 taken |
| `/dsquad [team]` | A squad by tier, slot progress, overseas count |
| `/dqueue <player>` | Your wishlist — used if your clock runs out |
| `/dtrade <team>` · `/dtrades` · `/dtradecancel` | Trade after the draft · the log · cancel your offer |

**Rules:** a slot's tier is a *ceiling* (a Platinum slot can take a Gold
player, but the slot is spent); the overseas cap refuses a pick that breaks it;
role minimums (keeper, bowlers) are enforced while you still have slots to obey
them. When done, the admin publishes the squads into a Challenge League and the
tournament (§19) begins.

---

## 🔨 21. Franchise Auction
<!-- topic: auction franchise-auction bid ipl-auction -->

**IPL-style auction in the group**: every franchise has a purse, players come up
in sets, the room bids.

```text
/bid           ← bid the next minimum (the safest, fastest bid)
/bid 15        ← ₹15 Cr      /bid 75L ← ₹75 lakh
.bid           ← dot shortcut (also .purse .squad .board .lb .mybids)
```

| Command | What it does |
|---------|--------------|
| `/aboard` | Live board with quick-bid buttons |
| `/apurse [franchise]` | Purses, max bids, squad counts, RTM cards |
| `/asquad [franchise]` | A squad with purse and max bid |
| `/ainfo` · `/arules` | Where it stands · every number it runs by |
| `/asets` · `/anextset` · `/anextplayer` | Sets and who is next |
| `/asoldlist` · `/aunsoldlist` · `/aretained` | Sold, unsold, retained |
| `/aleaderboard` · `/amybids` | Spend ranking · your bid history |
| `/artm yes\|no` | Answer a Right To Match on your former player |
| `/atrade <team>` · `/atradehelp` | IPL-style trades, before/during/after |

**The clock (default):** a lot opens with 60s; a bid with under 40s left puts
it back to 40s; ⚠️ 1st warning at 20s, ⚠️⚠️ 2nd at 10s, then a 5-4-3-2-1 count
and SOLD/UNSOLD. Retention and RTM work like the real IPL. Read-only commands
also answer in DM. Full guides: `docs/Franchise-Auction-Guide-Users.pdf`.

---

## 💼 22. Auction League — solo career
<!-- topic: rcpl auction-league solo career al -->

**Run one franchise for a whole IPL-style season, alone, in DM** — the AI runs
everyone else. 💬

```text
/rcpl            ← the hub: start, continue, or see the last career
/rcpl IPL        ← straight to that league's teams
/rcpl help       ← the full guide (/alhelp)
```

1. **Pick your franchise**, AI retentions on/off, AI difficulty (Easy/Normal/Hard).
   Entry fee **100 💎** per season.
2. **Retention** — keep up to 3 (₹18 / 14 / 10 Cr). Sides that keep ≤ 2 get an RTM card.
3. **Auction** — purse ₹120 Cr; sets Marquee → rating bands by role → Emerging →
   Accelerated. Each AI raise: **Bid**, **🚀 Jump** or **Pass** (20s). ⚡ Fast,
   ⏭ Skip player (`/skipplayer`), ⏸ Pause (`/alpause`/`/alresume`),
   `/simset [n]`, `/simtolast`.
4. **Squad rules** — ≥ 4 BAT, 4 BOWL, 1 WK, 2 AR; ≤ 18 players, ≤ 8 overseas.
5. **Trade window** — AI offers + up to 2 swaps of your own (`/altrade`).
6. **Season** — round robin + IPL playoffs. `/alplay` plays your next match
   (ball by ball, 20 overs; AI matches are simmed). `/altable`, `/alfixtures`,
   `/alstats`, `/alsquad`, `/alpurse`, `/alcard`.
7. **Career** — Start Season N+1; ratings move with performance; `/alhof` is the
   Hall of Fame. `/alquit` discards (fee not refunded).

**Prizes per season:** champions 25,000 coins + 15 💎, runners-up 10,000 + 5 💎,
playoffs 4,000 coins (once per season, max one paid season per 48 h, not if you
conceded a match). Matches themselves are unranked.

---

## 📚 23. League Seasons
<!-- topic: seasons league-seasons history champions -->

A competition (e.g. "IPL") played season after season, with a history of
champions. Each new season starts by mega auction, mini auction or manual teams.

```text
/seasons                    ← every season: champion, runner-up, caps
/seasons IPL                ← one competition
/seasons Virat Kohli        ← one player, season by season
```

---

## 🧮 24. Fantasy League
<!-- topic: fantasy fl -->

A weekly fantasy cricket league: pick a squad in the Mini App, score points from
real in-bot performances.

| Command | What it does |
|---------|--------------|
| `/fantasy` | Open the league and the squad picker |
| `/myfantasy` | Your squad and points |
| `/fantasyleaderboard` | The leaderboard |
| `/fantasystats` | Top scorers this week |
| `/fantasyguide` | The rules |

---

## 📈 25. Ranked ladder, rivalries & rewards
<!-- topic: ranked rank rivalry rewards elo ladder -->

### Ranked ladder

Every completed match between two players (Lets Play, CIPL/Challenge League,
`/playmatch`, `/wpm`, Super Over and bowl-out finishes) moves both ratings.
Beating a stronger player pays more.

| Division | Rating |
|----------|--------|
| 🥉 Bronze | below 1050 |
| 🥈 Silver | 1050 |
| 🥇 Gold | 1150 |
| 🔷 Platinum | 1250 |
| 💎 Diamond | 1350 |
| 👑 Legend | 1450 |

* First 10 matches are placements (bigger swings).
* Monthly season: with 5+ ranked matches you're paid by division (Silver 1,000
  coins → Legend 20,000 coins + 10 💎); ratings then reset halfway to 1000.
* The same pair can move each other's rating 3 times per UTC day.
* Never rated: bot matches, `/lpmulti`, under-5-over friendlies, "won't count"
  mismatches.

`/rank` · `/ranked` · `/rivalry @user` · `/h2h @user`

### Rivalries

After 5 meetings two players become a named rivalry: series score on every
result card, +250 coins per rivalry win, and rounds of 5 (winner of a round:
+3,000 coins + 2 💎). Bonuses count 5+ over matches, up to 3 per pair per day.

### Match rewards

Winners and losers earn coins per over played (defaults 300 / 150, operator
configurable), plus gems, quest progress (`/myquest`), achievements and career
stats. Unranked practice modes pay nothing.

---

## 🛠 26. During a match — pause, resume, fix
<!-- topic: controls pause resume continue saved endmatch -->

| Command | What it does |
|---------|--------------|
| `/matchinfo` | Striker, bowler, score, target |
| `/impact` | Open the Impact Player picker |
| `/change <out> <in>` | Swap a player in your XI before the toss |
| `/pause` | Save a Lets Play / Challenge League match exactly where it is ⏸️ |
| `/continue <MatchId>` | Carry it on from that ball, in any group (other captain taps ✅ Ready) |
| `/saved` | Your paused, cleared and timed-out matches |
| `/resume` · `/rcl [MatchId]` | Buttons disappeared? Bring the match back |
| `/playapp` · `/playchat` | Move the match to the Mini App / back to chat |
| `/endmatch` | Ask to end your match (a fine applies) |
| `/clearmatches` | Clear a stuck match in this chat (it is saved first) |
| `/lastscorecard` | Re-send the last scorecards in this chat |

---

## ❓ 27. Troubleshooting & FAQ
<!-- topic: faq help trouble stuck problems -->

**"Your XI is not valid."** Run `/autobuild`, then check `/playingxi`. You need
a keeper and enough bowling options.

**The buttons vanished.** `/resume` (or `/rcl`, or `/rcl <MatchId>`).

**The chat is stuck with an old match.** `/clearmatches` — a Lets Play /
Challenge League match is saved first, so `/continue` it later.

**"There is already a challenge waiting in this chat."** One lobby per chat:
finish or let it expire, or play in another group.

**My opponent has no @username.** Reply to their message with the command.

**My tournament fixture won't start.** The tournament may be paused, your
fixture may be in a round that hasn't opened, or one of you isn't entered —
the bot's reply says which. Check `/lptfixtures` or `/ctfixtures`.

**Why didn't my rating change?** Bot matches, `/lpmulti`, under-5-over matches
and the 4th+ match of the day between the same pair are not rated.

**`/sim` says I'm on cooldown.** It's every 4 hours (less on paid tiers).

**Something else?** `/report <message>` or `/feedback <message>` reaches the
admins.

---

*Commands and numbers here are the defaults; an operator can change cooldowns
and rewards, and the bot's own replies (`/arules`, `/rcpl help`, `/matchhelp`)
always show what is live. Get this guide again with `/matchguide`.*
