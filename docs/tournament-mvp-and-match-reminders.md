# Tournament MVP & match reminders

Two things a running tournament was missing: a board that ranks a whole
tournament rather than one column of it, and any way at all to tell the two
people holding up a fixture that they are holding up a fixture.

| Command | What it does | Who may use it |
| --- | --- | --- |
| `/mvp` (`/tourmvp`, `/ctmvp`) | Most Valuable Player, Overall / Batting / Bowling | anyone |
| `/tournamentstats` → 🏅 MVP | the same board as a tab on the stats card | anyone |
| `/remindmatch [team] [vs team] [force]` | nudge two teams to play what they owe | bot admins; team owners for their own team |

---

## 1. Most Valuable Player

### The problem with every other board

`/tournamentstats` has nine categories and every one of them ranks a single
column. Most Runs finds the batsman who batted the most. Most Wickets finds the
bowler who bowled the most. Neither of them — nor any combination of them — can
see the all-rounder who won three matches with 40-odd and two wickets a time,
because he is 11th on one board and 9th on another.

MVP is the board that sees him. One number, for the whole tournament.

### The currency

The unit is the **impact point**, and it is deliberately the same unit the live
match card already awards Player of the Match with (`handlers/match.py::_calc_potm`).
A tournament's MVP table is therefore just every match's POTM race added up —
which means a player who keeps *narrowly losing* that race still climbs, and the
number is one people already have an intuition for.

| | Scoring |
| --- | --- |
| 🏏 Batting | runs, +1 a four, +2 a six, +5/+10 for a strike rate of 130/150, +15/+30 for a fifty/hundred |
| 🎯 Bowling | +25 a wicket, ±2 per over for each run/over either side of an 8.00 economy, +15/+30 for a 3-for/5-for |
| 🏆 Result | +10 for each match your team won (+5 for a tie) |
| ⭐ Award | +25 for each match you were the best player on the field |

The card prints this rubric under every table, generated from the constants
themselves (`services/tournament_mvp.py::rubric_lines`) — so it can never drift
out of step with the scoring, and a leaderboard nobody can explain never happens.

### Three properties worth knowing

* **It is scored per innings, not per aggregate.** Three fifties are three
  milestone bonuses; one 150 is one hundred's worth. Scoring off the aggregated
  `TournamentPlayerStats` row would silently merge them and reward the wrong
  thing — consistency *and* impact are the point.
* **A bad day can't go negative.** An expensive spell that still took three
  wickets is a contribution. Bowling points floor at zero per spell, so one
  mauling does not drag a whole tournament backwards.
* **Nothing is stored.** The table is rebuilt from `TournamentMatch.scorecard_json`
  every time it is opened, so deleting or correcting a match moves the MVP table
  exactly as it moves the points table. A scorecard that won't parse is skipped
  rather than allowed to take the board down.

### Who won which match

The result bonus needs to know which side a scorecard line was on. The line
knows its player and its team *name*; the match row knows which team **id** won.
Names alone are fragile — a franchise renamed mid-tournament stops matching — so
the user id is tried first (`host_user_id` batted first, `target_user_id` batted
second, which is exactly how the fixture's two team ids were filled in) and the
name is the fallback.

The id only decides when it is **one of the two the fixture knows about**. An
imported or hand-corrected scorecard can carry a user the fixture never saw, and
a line like that is not "the losing side" by default — the team name gets its
say instead. A line that neither can place earns no result bonus at all, rather
than being guessed onto a side.

Player of the Match follows the live card's own eligibility rule: the award goes
to the winning side, and a losing player only enters the race with 50+ points
behind them.

### The card

```text
🏅 Summer Trophy — Most Valuable Player
🏅 Overall · the whole tournament, one number

🥇 Hardik Pandya · Mumbai Indians — 641 pts
      🎮 8 · 🏏 289 (172b, SR 168.0) · 🎯 11w in 28.0 ov (Econ 7.10) · ⭐ 3 POTM
🥈 Jasprit Bumrah · Mumbai Indians — 588 pts
      🎮 8 · 🎯 19w in 32.0 ov (Econ 6.02) · ⭐ 2 POTM

How Hardik Pandya got there
🏏 341 batting · 🎯 205 bowling · 🏆 70 results · ⭐ 75 awards

Scoring
🏏 runs · +1 a four · +2 a six · +5/10 SR 130/150 · +15/30 a 50/100
🎯 +25 a wicket · economy vs 8.00 (×2 an over) · +15/30 a 3/5-for
🏆 +10 a win (+5 a tie) · ⭐ +25 a Player of the Match
```

Three tabs — 🏅 Overall, 🏏 Batting, 🎯 Bowling — are the same points ranked on
their own column, so "who batted best" and "who bowled best" are one tap apart
from "who mattered most". Only the halves a player actually played appear on
their line: a specialist bowler's row carries no empty batting stat.

`/mvp` reads the live Challenge League tournament first and the Lets Play one
after, so it answers in a chat running either. The 🏅 MVP tab on
`/tournamentstats` and `/lptstats` is the same table, served from
`tournament_service.stat_leaders` — and so is the **Most Valuable Player** widget
that now leads the Tournament Leaders grid on the admin dashboard, ahead of the
nine boards that each rank one column.

---

## 2. Match reminders — `/remindmatch`

### What actually stalls a tournament

Not a bug, and not anybody being difficult: two people each assume the other
will start the match, and neither has opened the fixture list today. Every
follow-along command the bot had — `/ctfixtures`, `/clsd` — only talks to
whoever already typed it, which is never the person holding things up.

`/remindmatch` is the message that goes the other way.

```text
🏏 PENDING MATCH — Summer Trophy

🟠 Sunrisers Hyderabad  vs  🩷 Rajasthan Royals
M14 · 🏠 Sunrisers Hyderabad · 🌱 Dusty

🟠 @the_boss_returns  🤝 @cookinmeth
🩷 @someone_else

👉 You run Sunrisers Hyderabad in this one.

⏳ You still have 1 league match left against each other.
🎮 Play it now — reply to your opponent and use /cipl
📅 Full schedule: /clsd SRH · /clsd RR
```

### Where it goes

Two places, on one command:

* **the group it was run in** — so the chat can see the fixture is outstanding
  and who owes it;
* **the DMs of every owner and co-owner of both sides** — because the person
  holding things up is by definition not reading the group.

The 👉 line only appears in the DM. A co-owner opening a card full of franchise
names needs telling instantly that this one is on them.

### Usage

```
/remindmatch                  everything still unplayed
/remindmatch SRH              one team's remaining fixtures
/remindmatch SRH vs RR        just that pair
/remindmatch SRH force        ignore the cooldown (bot admins)
```

Team names are matched exactly as `/clsd` matches them — case, `short_name`,
initials, an unambiguous prefix — and `vs` (or `v`, `against`) separates two of
them, because franchise names have spaces in them and that is how people write
it anyway. A trailing `force` is read off first so it can never be mistaken for
part of a name.

### Nothing fires without a preview

The first reply is always a **preview**: which pairs would be chased, who would
be tagged, how many people that is, and what is being held back by the cooldown
— with **📣 Send reminders** and **❌ Cancel** under it, usable only by whoever
opened it. Tagging a dozen people is not a thing to do by typo, and a preview
costs one tap.

The Send button **re-resolves the plan from the database** rather than restoring
it from the card. A fixture played between the preview and the tap drops out
instead of getting a reminder to go and play a match that is already done.

Afterwards the preview becomes a delivery report: pairs chased, DMs delivered,
and how many could not be reached (someone who has never started a chat with the
bot, or has blocked it — the bot cannot DM them, and that is worth saying rather
than silently counting as sent).

### The four things that keep it from becoming spam

This is the feature that turns into noise everybody mutes if nobody thinks about
it, so most of the design is restraint:

1. **A 12-hour cooldown per fixture.** Every send is written to
   `TournamentMatchReminder`; a fixture reminded inside `COOLDOWN` is held back,
   and the preview *says* it is being held back rather than quietly sending less
   than it was asked to. A pair is quiet only while **every** fixture in it is
   quiet — a newly scheduled second leg is worth a nudge even if the first was
   mentioned this morning. Bot admins can override with `force`; a team owner
   cannot, because their own cooldown is not negotiable.
2. **One message per person.** Someone who co-owns two stalled teams gets one DM
   carrying both fixtures, not two DMs. Both legs of a double round-robin pair go
   into one card, because two teams owing each other two matches is one
   conversation.
3. **A cap, and a roundup.** At most 12 pairs are chased in one run, and at most
   three full cards go into one DM (the rest are named, and pointed at `/clsd`).
   In the group, one pair posts as its own full card; several post as a single
   roundup line-per-pair — eight cards in a row is a flood, and a flood is
   scrolled past.
4. **It only ever names people who are in the fixture.** No @everyone, no
   broadcast to the league.

### Who may send one

| | Scope |
| --- | --- |
| Bot admin | any fixture in the tournament |
| Team owner or **co-owner** | that team's own fixtures |
| Everybody else | nothing — the bot will not ping people on their say-so |

A co-owner is the owner's equal here, as everywhere else in the tournament. The
person chasing their own outstanding fixture is exactly the person with a reason
to, which is why the command isn't admin-only.

### Team badges

The two sides need telling apart at a glance in a chat full of text. A Challenge
League team already carries its franchise's `primary_color`, so the badge is the
coloured circle nearest to it — SRH comes out 🟠, RR 🩷 — with nobody entering an
emoji anywhere. A team with no colour (every Lets Play team, for one) gets a
circle derived from its name: arbitrary, but the same every time, which is all a
badge has to be.

### What it leaves alone

* matches already **completed**, and matches already **live** — reminding people
  to start a match they are in the middle of is noise;
* knockout slots that still read "Winner of Qualifier 1" — there is nobody to
  remind yet;
* teams removed from the tournament mid-schedule.

---

## Where the code lives

| File | What |
| --- | --- |
| `services/tournament_mvp.py` | the impact-point engine and the MVP table |
| `handlers/tournament_mvp.py` | `/mvp` and its three tabs |
| `handlers/tournament.py` | the 🏅 MVP category on `/tournamentstats` and `/lptstats` |
| `templates/admin_tournament_dashboard.html` | the MVP widget on the admin dashboard |
| `services/tournament_service.py` | `match_scorecards`, `player_identity`, `stat_leaders["mvp"]` |
| `services/match_reminder_service.py` | finding pending fixtures, contacts, badges, the cards, the cooldown |
| `handlers/match_reminders.py` | `/remindmatch`: permissions, preview, group post, DM fan-out, report |
| `models.py` | `TournamentMatchReminder` — the cooldown's memory and the audit trail |

## Tests

* `tests/test_tournament_mvp.py` — the rubric's arithmetic, the all-rounder
  beating the bigger scorer, per-innings milestones, the POTM eligibility rule,
  and the table moving when a match is deleted.
* `tests/test_match_reminders.py` — what gets chased and what is left alone, who
  is reached (owners *and* co-owners, once each), the card, pair grouping, the
  cooldown and its override, and who is allowed to send one at all.
