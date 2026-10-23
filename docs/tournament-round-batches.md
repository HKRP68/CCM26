# Round pitches and flexible releases

Choose **Shared per round — balanced rotation** under **Tournaments → Manage →
Pitch**. Every league/group match with the same positive round number shares a
pitch, across all groups. The generator spreads surfaces evenly across rounds.
Manually added round-0 fixtures and playoff conditions remain independent.

On **Schedule**, each round shows Locked, Open, In Progress or Completed, its
completed-match count and pitch. A whole-round selector edits the shared pitch.
Once any fixture in a round is live or completed, its pitch is frozen. Re-rolling
unplayed pitches preserves the entire started round. Switching into or away from
the round pitch rule is refused once league play starts.

## Opening rounds

| Mode | Behavior | Bot example |
| --- | --- | --- |
| Automatic batches | Open 1–3; wait for every match in those rounds; open 4–6 | `/tfixturemode batch 3` |
| Manual batches | Open an initial batch; wait for the admin to release more | `/tfixturemode manual 3` |
| Scheduled batches | Release another batch at an interval while active | `/tfixturemode scheduled 3 48h` |
| Rolling window | Advance the window when the earliest unfinished round ends | `/tfixturemode 3` |
| Single round | Open the next round only after this one finishes | `/tfixturemode round` |
| All at once | Show and allow every fixture | `/tfixturemode all` |

Use `/tfixturemode next 2` or **Schedule → Open more rounds now** to add exactly
two additional rounds. An explicit opening from rolling mode switches to manual
release. In automatic batch mode, an explicit opening extends the current batch;
the next batch waits for all released rounds. Change the batch size at any time:
already released rounds stay available and subsequent releases use the new size.
Add `#id` before the arguments to select a tournament when several are active.

Scheduled releases run on the existing tournament watch job, so they take effect
on its next tick. Configuring a draft tournament starts the interval when the
watch first sees it active. Restarting an active tournament catches up missed
intervals once. The admin schedule displays the next release in the browser's
local timezone. If the tournament is paused, the watch leaves it alone; resuming
catches up elapsed intervals.

Fixtures may finish in any order within the released range. Match selection,
reservation, simulation, Telegram fixture lists and the Mini App use the same
release boundary. Dependent knockout fixtures still require both opponents to
be known; batch controls do not bypass bracket advancement.

## Shared deadlines

Existing `/tdeadline 48h`, `/tdeadline +12h` and `/tdeadline off` commands work for
batches. The deadline covers all released unfinished league rounds and stays
fixed when an individual round finishes. The next batch starts a new clock.
Manual batches that are complete have no deadline until more rounds are opened.
The existing reminders and admin alerts include pending matches across the batch.
Deadlines and scheduled openings never simulate or settle a fixture.

## Compatibility and rollout

`database.init_db()` adds the release mode, boundary, interval and next-release
columns through the existing idempotent migration mechanism. Existing rows default
to their rolling window; all-at-once tournaments keep their existing behavior.
The new creation form defaults to batches of three and a shared pitch per round.
Restart the admin and bot processes after deployment to apply the schema additions
and run the updated watch job. No external scheduler is needed.
