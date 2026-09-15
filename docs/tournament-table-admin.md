# Running the table: adjustments, imported matches, and stuck fixtures

Three things a tournament needs once it is actually running, and one bug that
made the third of them unavoidable.

* **A match that is over must never read as live.** A Super-Over-decided
  tournament match was never recorded at all, so its fixture sat on `live`
  forever and the dashboard reported a finished match as in progress.
* **Points are sometimes set by hand.** Real competitions dock points for a
  slow over rate and award them for a walkover. There was no way to do either.
* **Not every match is played on the bot.** A match played on a stream or in a
  hall could only be entered as two scorelines, which left every batting and
  bowling leaderboard untouched.

---

## 1. The Super Over bug, and why a fixture got stuck

A tied tournament match does not finish where an ordinary one does: it returns
early and starts a Super Over, and `handlers/super_over.py::_finalize` is the
only place its result can be recorded.

That code read `so["main"]`. `so["main"]` is the two-line scoreline built for
the "MATCH TIED!" announcement — team names, runs, wickets, overs. It carries no
`tournament_id`, no `match_id` and no per-player stats. The live match state is
`so["main_state"]`, which is what every other block in `_finalize` already read.

So `record_tournament_match` was called with a dict whose `tournament_id` was
missing, returned `None` on its first line, and **every Super-Over-decided
tournament match was silently dropped**. The visible symptom is the fixture,
because a fixture's life is:

```
scheduled ──reserve_fixture──▶ live ──record_tournament_match──▶ completed
```

Nothing recorded means nothing ever flipped it off `live`, and the points table,
the net run rate and the leaderboards never saw the match either. Two Super
Overs or one made no difference — the fix is reading the right dict.

### Making it structural

A one-word fix is not much of a guarantee, so the same failure mode is now
closed off three more ways.

**A fixture knows which match is playing it.** `reserve_fixture` claims a
fixture before the `Match` row exists, so nothing on the fixture pointed at the
game. `league_schedule_service.bind_fixture_match` now writes the match id in
the same transaction that creates the match (`handlers/cipl_play.py`,
`handlers/letsplay.py`). `record_tournament_match`'s idempotency check moved to
"a **completed** row already carries this match id", so binding early doesn't
make a live fixture look like a recorded result.

**Recording nothing hands the fixture back.** Both completion paths — the Super
Over decider and the ordinary one — now call `release_fixture` when
`record_tournament_match` returns `None`. The fixture goes back to `scheduled`,
where it can be replayed or entered by hand, instead of sitting on `live`.

**Stuck fixtures are swept up.** `league_schedule_service.heal_live_fixtures`
reverts any fixture on `live` whose bound match is gone or has reached a
terminal status (`completed`, `abandoned`, `expired`, `cancelled`, `forfeit`),
and any unbound fixture that has been live longer than a grace period — three
hours by default. It keys off the terminal list rather than off "not currently
active" so that an unrecognised status is left alone: a fixture whose match is
still being played is never touched, however long it has been running, and a
completed fixture is never touched at all. It runs from the hourly cleanup job,
from the admin dashboard alongside its throttled recompute, and on demand via
`/tfixsync`.

**Bonus, from the same area:** recording now fills the fixture the match
actually *reserved* (`state["reserved_fixture_id"]`) rather than the earliest
open fixture for that pair. In a double round-robin a pair has two legs, and
playing the second one first used to write the result onto the first.

---

## 2. Points adjustments

`TournamentTeam.points` is **derived**: `recompute_standings` zeroes it and
rebuilds it from the recorded matches every time a result is added or removed.
Anything typed straight into it disappears at the next result.

So an adjustment is its own column, and the rebuild re-applies it:

| column | meaning |
| --- | --- |
| `points_adjust` | points added on top of what the results earned (`-2`, `+4`) |
| `points_adjust_note` | why, shown wherever the adjustment is |

```
points = (results) + points_adjust      ← recomputed, every time
```

`tournament_service.adjust_points(session, team_id, delta, set_to=, note=)`
takes a delta (two `-2`s end at `-4`) or an absolute `set_to`, bounded to
`±MAX_POINTS_ADJUST` so a typo like `-200` is refused rather than silently
rewriting the table. Setting it to `0` clears the note too.

**From the chat**

```
/tpoints Mumbai Indians | -2 | slow over rate
/tpoints MI | +2 | walkover
/tpoints #7 CSK | =0                  clear it
/tpoints                              list every adjustment in force
/tpointsclear CSK
```

**From the admin site** — open a row in the points table on the tournament
dashboard. The summary line shows the adjustment and its reason when one
applies, so the number in the table is never unexplained.

**In the bot's table** — an adjusted team is starred (`Alpha*`) and a footnote
under the table names the adjustment and its reason. A points column nobody can
account for reads as a bug, so it is never shown without the explanation.

---

## 3. Importing a written scorecard

`/taddmatch <match number>`, sent as a **reply** to a text file (or to a plain
message) holding the scorecard. The match number is the one on `/ctfixtures` or
`/lptfixtures`.

```
Innings 1: Mumbai Indians 187/5 (20)
Batting
Rohit Sharma 62* (41) 6x4 2x6
Ishan Kishan 45 (30) 4x4 1x6 c Dhoni b Jadeja
Suryakumar Yadav, 40, 20, 3, 2, out
Bowling
Deepak Chahar 4-0-31-1
Ravindra Jadeja 4-0-28-2

Innings 2: Chennai Super Kings 180/8 (20)
Batting
Ruturaj Gaikwad 55 (38) 5x4 1x6 b Bumrah
MS Dhoni 30 (12) 1 3 not out
Bowling
Jasprit Bumrah 4-0-24-3

Result: Mumbai Indians won by 7 runs
```

The **Bowling** list inside an innings is the *fielding* side's bowlers, exactly
as a printed scorecard reads — they are credited to the other team.

### The grammar

Deliberately forgiving, because a person types this.

* **Batting** — `Name runs (balls)`, optionally `6x4` / `2x6` (or two bare
  numbers) and a dismissal. Also `Name, runs, balls, fours, sixes, out`, comma
  or pipe separated. `runs (balls)` is what tells a name from a figure, so the
  parentheses are required in the compact form.
* **Bowling** — `Name O-M-R-W`, `Name O M R W`, or the comma form. Maidens are
  read and discarded; there is no column for them.
* **Not out** — `not out`, `n.o.`, or a trailing `*` on the runs. Anything else
  counts as out: assuming not-out would quietly inflate batting averages.
* **Innings header** — `Innings 1:` / `[2nd Innings]` / `1st Innings`, or the
  label on its own line above the scoreline. An **unlabelled** header must show
  wickets (`187/5`), because `Bravo 151 (20)` is indistinguishable from a batter
  who faced 20 balls.
* Overs may be `(20)`, `in 20 overs` or `19.3` in the usual cricket notation.
  `Extras`, `Total`, `Fall of wickets`, `Did not bat` and similar lines are
  skipped, so a whole card can be pasted without editing it down.
* A line that is meant to be a player but can't be read stops the import and
  names itself, rather than dropping that player silently.

### What happens

1. The card is parsed (`parse_scorecard` — pure, no database).
2. `plan_import` resolves it against the fixture: which team is which (full
   name, short name or initials), which squad member each player name is, and
   who the figures belong to. Every guess becomes a warning on the preview.
3. The preview is posted with **✅ Record it** / **✖️ Cancel**. Nothing is
   written until it is confirmed, and the card is re-parsed and re-planned on
   confirmation in case the fixture changed underneath it.
4. `record_import` fills the fixture exactly as a bot-played match would —
   scoreline, winner, per-player scorecard — then rebuilds the standings and
   the player aggregates from every recorded match.

Details worth knowing:

* **Player identity.** Names are matched back to the team's `ChallengePlayer`
  roster (full name, then surname), which is the identity a bot-played match
  writes. That is what lets an imported innings and a simulated one land on the
  *same* leaderboard row. A name that isn't in the squad still counts, keyed by
  the name itself, and the preview says so.
* **Whose stats.** A Lets Play team *is* a user. A Challenge League team's
  figures go to the franchise's owner (then a co-owner). With nobody behind the
  team the match records, but its player rows don't — `TournamentPlayerStats`
  requires a user, and the preview warns.
* **Which side batted first.** `recompute_standings` treats `team1` as the side
  that batted first, so a card whose first innings is the fixture's `team2`
  swaps the two slots. Without it the net run rate is credited backwards.
* **The winner.** A `Result:` line naming one of the two sides wins outright —
  that is how a Super Over or an awarded match is recorded. If it disagrees with
  the scores, the preview says so and records the line. Otherwise the runs
  decide, and level scores with no line is a tie.
* **Refusals.** An already-completed fixture (remove its result first), a
  fixture still genuinely being played, a team that isn't in the fixture, or an
  innings longer than the tournament's over limit.
* **Undo.** An imported match is a recorded match. Remove it from the admin
  dashboard and the standings and leaderboards rebuild without it.

`/taddmatch` heals stale live fixtures for the tournament before it looks the
fixture up, so the very matches that got stuck by the Super Over bug are the
ones it can rescue.

---

## Where the code is

| what | where |
| --- | --- |
| the Super Over fix + fixture release | `handlers/super_over.py::_finalize` |
| binding a fixture to its match | `handlers/cipl_play.py`, `handlers/letsplay.py` |
| `bind_fixture_match`, `heal_live_fixtures`, `release_fixture` | `services/league_schedule_service.py` |
| `adjust_points`, `points_adjust_footnote`, reserved-fixture recording | `services/tournament_service.py` |
| the scorecard grammar and the import | `services/scorecard_import.py` |
| the chat commands | `handlers/tournament_admin.py` |
| the dashboard form and route | `templates/admin_tournament_dashboard.html`, `admin.py` |
| tests | `tests/test_scorecard_import.py`, `tests/test_tournament_table_admin.py`, `tests/test_super_over.py` |
