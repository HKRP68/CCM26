# Live Matches (admin)

The ops view at `/live-matches`. Covers what the page shows, the three columns
added to `matches` to make it possible, and how rows written before those
columns existed are handled.

Implementation: `services/match_outcome.py`, the `admin_live_matches` /
`admin_live_matches_panel` routes in `admin.py`, and
`templates/admin_live_matches.html` + `templates/_live_match_cards.html`.
Tests: `tests/test_match_outcome.py`.

---

## 1. What the page shows

**Live**, in two groups:

- **In play** — `playing`, `active`, `in_progress`. Ball-by-ball is running, so
  the card carries the score, the innings, the target and the ball counter.
- **Setting up** — `pending`, `accepted`, `toss`, `selecting`. An invite is
  waiting, or the toss/openers are still being decided. No ball bowled yet.

The old page only queried `active` and `pending`, so a `/cric` match sitting at
the toss and a `/vsbot` match in `in_progress` were both invisible. The
dashboard's Live Matches tile counts the same two sets, so its number and this
page's now agree.

The live half auto-refreshes every 15s from `/live-matches/panel`, which
re-renders `_live_match_cards.html` on its own — one copy of the card markup,
rather than a JSON feed plus a client-side renderer that has to be kept in step
with it. "Last ball" ticks up between polls client-side. Polling pauses while
the tab is hidden, and stops if the admin session has expired (the fetch is
redirected to the login page, which would otherwise be pasted into the panel
and read as "all matches vanished").

**Completed** — every match that is no longer playable, as a table:

| Match No | Host | Guest | Match Type | Winner | Status | Ended |

searchable by match id or player name, filterable by mode, and filterable by
end reason through the tally chips. The chip counts are computed against the
search but *not* against the reason filter, so picking one still shows what
else is in there.

## 2. Why three new columns

Every terminal path but the cleanup job wrote `status = "completed"`. A
completed row with no `winner_id` could equally be a tie, a forfeit, an
`/endmatch`, a `/clearmatches` sweep or an admin force-end — the information
was never recorded anywhere.

`matches.end_reason` records it at the point it happens:

The reason answers **what stopped this match being playable**, not whether
there was a winner — the Winner column already answers that. So a timeout
forfeit is *Automatically ended* even though it names a winner, and a Mini App
quit is *Ended by user* even though it does too.

| Value | Label | Written by |
| --- | --- | --- |
| `completed` | Completed | played out to a result — incl. tie, super over, bowl-out |
| `ended_by_user` | Ended by user | `/endmatch`, the Mini App forfeit button |
| `cleared_by_user` | Cleared by user | `/clearmatches`, `/removematch` |
| `ended_by_admin` | Ended by admin | the admin panel's force-end |
| `auto_ended` | Automatically ended | idle-player timeout forfeits, the stuck-match cleanup job, lazy expiry of stale pre-play rows, orphaned CIPL rows |

`abandon_match` and `handle_match_termination` each serve both a deliberate
quit and the inactivity sweep; they split on their `reason` argument.

`matches.ended_by_id` records *who*, for the two reasons a person causes.

`matches.match_type` records the mode the match was started in — `cric`,
`playmatch`, `wsp`, `challenge`, `letsplay`, `cipl`, `vsbot`, `wspbot`,
`wpmbot`, `botmatch`, `botvsbot` — set at each `Match(...)` creation site.
Super over and bowl-out reuse the parent match's row and so keep its type.

`end_reason` is bookkeeping only: `mark_end()` deliberately leaves `status` and
`completed_at` to the caller, because "completed" vs "abandoned" means
different things per mode and other queries filter on it. Adding the call next
to an existing assignment changes nothing else.

## 3. Old rows

Both columns are NULL on everything that finished before they landed.
`derive_end_reason()` reconstructs what it can and reports whether it had to:

- a dead status (`abandoned`, `expired`, `cancelled`) → **Automatically ended**
- `completed` with a winner, or a result-shaped margin (`tie`, `super over`,
  `bowl-out`, `forfeit`) → **Completed**
- `completed` with neither → **Unrecorded**

That last case is the honest answer, not a gap. `/endmatch` and
`/clearmatches` left byte-identical rows behind, so picking either one would be
inventing a fact. Match type falls back the same way, to `PvP` / `vs AI` /
`AI vs AI` based on which sides are the bot user.

Anything reconstructed is marked in the table — `~` on the mode pill, `*` on
the status — so a guess never reads as a recorded fact.

`end_reason_filter()` is the SQL mirror of `derive_end_reason()`, so clicking
"Completed 12" selects the same twelve rows the table just labelled completed
rather than only the ones written since the column shipped. The two are written
in different languages, so `tests/test_match_outcome.py` runs the SQL against
SQLite and compares it to the Python answer row for row, and checks the buckets
partition the table exactly.

## 4. Migration

`match_type`, `end_reason` and `ended_by_id` are added by
`_migrate_add_columns()` in `database.py`; the two lookup indexes are created
by an isolated migration step, because `create_all()` never adds an index to a
table that already exists. Backfill is not attempted — the information the old
rows would need was never written down.
