# Saved matches: pause, continue, and private plans in the Mini App

Challenge League (`/cipl`, `/c<league>`, tournament fixtures) and Lets Play
matches no longer have to be finished in one sitting, and a clear or a
time-out no longer throws away the overs already played.

| Command | Aliases | What it does |
|---|---|---|
| `/pause` | `/pausematch` | A captain saves the live match in this chat and stops the clock. The chat is free again. |
| `/continue [MatchId]` | `/unpause` | Picks a saved match up from the exact ball it stopped on, in any group. Against a human the other captain taps ✅ Ready first; a bot match resumes at once. With no id: the one saved match in this chat (or your only one), else the list. |
| `/saved` | `/savedmatches` | Your saved matches, each with ▶️ Continue and 🗑 Discard. |
| `/playapp` | `/appmode` | Moves the live match to the Mini App. Each captain picks privately; neither side's plans are shown. |
| `/playchat` | `/chatmode` | Moves it back to the chat buttons. |

## When a match is saved

| How it stopped | Saved as | Match row | Tournament fixture |
|---|---|---|---|
| `/pause` | ⏸ Paused | `paused` (end reason `paused`) | stays reserved for it |
| `/clearmatches`, `/removematch` | 🧹 Cleared | `completed`, as before | handed back, re-reserved on continue |
| A tournament fixture's **first** idle time-out | ⏱ Timed out | `paused` (end reason `auto_ended`) — no fine, no winner | stays reserved |
| A practice match vs the bot going idle | ⏱ Timed out | `abandoned`, as before | — |

A second idle time-out in the same tournament match forfeits it as before.
Friendlies and CL Tour games still forfeit on the first one: a CL Tour forfeit
decides the series game. A finished match, or one whose result is already paid
out, is never saved.

## Continuing

`/continue` revives the **same** `Match` row, so the result is recorded
exactly as if the match had never stopped. Before it does:

* the chat must have no live match, and neither captain may be in one;
* a human-vs-human match must be continued in a group;
* the tournament must not be over, and its fixture must still be the match's:
  a fixture that was played or taken by another match since makes the save
  unplayable (it is voided, and says why). A fixture a clear handed back is
  reserved again (`scheduled` → `live`), and a CL Tour slot likewise
  (`pending` → `playing`).

Only one continue can win a save (`saved` → `resuming` is a compare-and-swap).
Discarding a save closes its match as `abandoned` and hands a fixture / tour
slot it still held back to the schedule.

## Private plans

`play_mode` is now a property of any over-by-over match, not only bot matches.
With `play_mode = "app"` between two humans:

* the chat carries no pick buttons (as in an app-mode bot match), just the
  "📱 pick X in the Mini App" line, the over summaries and the result;
* the over summary in the chat omits the combination, its flavour, the
  carry-over note and the predictability warning, and says
  "🔒 Plans stay private";
* the Arena's `lastOver` carries no approaches at all — the Mini App's over
  summary shows neither plan (not even a "🔒 Hidden" placeholder) to anyone;
* the raw `/api/match` state never carries plan keys
  (`match_webapp_service.strip_hidden_plans`) — this applies to every
  over-by-over match.

## Where it lives

* `models.SavedMatch` (`saved_matches`, created by `create_all`).
* `services/saved_match_service.py` — the blocking DB side: snapshot, park,
  claim, revive, discard.
* `handlers/cipl_pause.py` — the commands, the `svm_*` buttons (shared
  prefix; every press is checked against the two captains),
  `autosave_idle_match` and `snapshot_before_clear`.
* `handlers/cipl_play.py` — `_app_mode` / `_private_plans`, and the timeout
  that saves instead of forfeiting.
* `handlers/match.py` — `/clearmatches` and `/removematch` save first.
