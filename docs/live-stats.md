# Live scores and public records

The read-only stats website lives in the existing Flask application:

- `/stats` lists active competitions with linked League Seasons.
- `/stats/<series_id>` opens the latest season's scores and statistics.
- `?season=all` shows all seasons; `?season=<season_id>` selects a season.

The site includes a live score centre with batting and bowling figures, confirmed
player statistics, record holders, a points table, and recent match results.
The admin records page links to its public counterpart. Inactive competitions
are not published. Private live matches are excluded, and responses contain only
cricket statistics: no captain selections, user identifiers, chat identifiers,
roster details, or raw match state.

## Refresh and performance

`GET /api/stats/<series_id>/live` reads active match snapshots and refreshes every
3 seconds while the page is visible. It never rebuilds historical record boards.
Scores change as match state advances after a ball or over; the existing full
match link provides the detailed match view. In-progress figures are clearly
separate from confirmed season/career totals.

`GET /api/stats/<series_id>/records` supplies confirmed player totals, records,
standings, and results. The page refreshes these every 30 seconds. Historical
calculations use a separate, bounded 20-second cache and a build lock for each
competition/season, so a large record book cannot block live polls or unrelated
competitions. A small source aggregate invalidates the cache when a match result
is added, removed, or its team totals change. When a live match ends, the page
requests confirmed records immediately rather than waiting for its next poll.
`services.live_records.invalidate_after_commit(session)` defers explicit
invalidation for player-only administrative corrections until the outer
transaction commits. Nested savepoint commits do not expose an uncommitted
correction through the cache.

Both polling loops pause in background tabs, prevent overlapping requests, abort
unresponsive requests after 10 seconds, and reconnect on returning to the page.
Neither endpoint accepts writes. There is no statistics rebuild or network
publication on the live delivery path and no separate database or deployment.

## Validation

`python -m pytest tests/test_live_records.py -q` covers unauthenticated read-only
pages, inactive competition and private-match filtering, live updates without
history scans, information boundaries, season access, cache reuse and completion
invalidation, and five-ball set progress.

## Match-end delivery

The result is sent as soon as the terminal score is saved. Summary rendering and
accounting run independently in workers, so standings, rewards, quests and
historical record reconciliation do not gate the summary photo. Reward amounts
and tournament news are added only after their transaction finishes. Super Over
results use the same priority; the main summary precedes the secondary card.

Initial summaries use available disk/database artwork without restoring missing
images from Telegram. The replay payload is retained, and `/lastscorecard` can
restore missing art when it redraws. Cosmetic deletions, collectible cards,
record alerts and channel copies follow the important match-chat artifacts.
Fresh results do not rebuild record snapshots for earlier matches; corrections,
replays and backdated additions still update affected historical snapshots.

Telegram transport latency and server-imposed flood-control waits still apply.
This removes application work from the critical delivery path rather than
promising that a remote service will always respond instantly.
