# Live scores and public records

The read-only stats website lives in the existing Flask application:

- `/stats` lists active competitions with linked League Seasons.
- `/stats/<series_id>` opens all linked seasons by default, including archived records.
- `?season=all` shows all seasons; `?season=<season_id>` selects a season.

The site includes live scorecards and commentary, fixtures/results, a newsroom,
team dashboards, player profiles, a tournament centre, a Statsguru-style search,
all record categories, and ground/pitch analysis.
The admin records page links to its public counterpart. Inactive competitions
are not published. Private live matches are excluded, and responses contain only
public cricket facts: no private captain selections, user identifiers, chat
identifiers, raw roster identifiers, administrative winner settings, or raw
match state. Public squad names and OVR/BAT/BOWL ratings are visible.

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

All automatic polling loops pause in background tabs, prevent overlapping requests, abort
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

## Opening the portal

Use `/cricinfo`, `/statsweb`, or `/recordsweb` in Telegram. Each opens a button to
`/stats` using the configured `WEBAPP_URL` host. The command does no historical
work and can be used in a group or private chat. Tournament match messages also
carry the Live scores & stats button.

## Portal sections and data

- **Fixtures and results**: upcoming/live/completed filters, round, stage, venue,
  pitch and match links. Match times remain unavailable when no date is recorded.
- **Newsroom**: published CMU articles/interviews plus factual match victories,
  active injury reports and completed transfers where recorded. Publishing and
  moderation stay in the existing News admin screens; unpublished content is hidden.
- **Tournament centre**: points tables, playoffs, tied Orange/Purple Cap leaders
  and season selection. Synthetic deadline simulations are labelled on fixtures.
- **Team dashboards**: registered squads and ratings, recorded manager, recent
  form, fixtures, team records and injuries. A playing captain is not inferred
  from the manager. Archived appearances are distinguished from a full roster.
- **Player profiles**: OVR/BAT/BOWL, selected-season and career totals, recent
  performances/form, personal records and opponent/pitch/venue splits. Squad
  members can open a profile before their first recorded appearance. Same-name
  versions have unknown ratings unless the recorded identity is unambiguous.
- **Stats explorer**: combine player, season, team, opponent, pitch, venue and
  stage filters; switch batting/bowling and page through results. Strike rate,
  average and economy use pooled denominators, rather than averaging match rates.
- **Records and grounds**: search every available record category and view all
  joint holders. Ground profiles include team/player results, scoring averages,
  successful chases, pitch splits and validated phase performance.

Missing historical denominators or surface data remain unavailable, rather than
being counted as zero. Phase analysis only uses saved and validated deliveries.
No interviews, transfer reports, results, or ratings are invented for an empty
section. News articles can cover transfers outside the linked auction system.

The public API adds read-only `/api/stats/<series_id>/portal`, `/explorer`,
`/player?name=...`, `/team?name=...` and `/venue?name=...`. All accept the existing
`season` query. `/api/stats/news/<article_id>` serves published articles without
changing view counts. Historical responses have a bounded 20-second cache,
completion signatures and committed-correction invalidation. They are computed
on website demand and never during a ball, over or scorecard delivery.

## Season archive, comparisons and artwork

All linked seasons are listed in the season archive, including preserved and
aggregate-only seasons. Each has a direct page and record-book link, honours,
cap leaders and data coverage. All-time is the default; the season selector
still scopes the live feed, tables, player records and queries to one season.

The head-to-head centre supports team vs team and player vs player. Team views
include actual meetings, wins, ties, no-results and per-season breakdowns.
Player comparisons include batting/bowling figures, milestone counts, season
breakdowns and direct batter/bowler encounters when saved deliveries exist.
Unknown historical meetings and delivery data are not inferred from totals.
Comparisons can be swapped and shared as URLs with their chosen scope.

Player profiles show every retained season and the complete supported batting
and bowling statistics. Missing historical milestone/dot/award data stays
unavailable. Career totals include preserved aggregate-only seasons, while
match-specific filters continue to require match evidence.

Available team logos and collectible player cards appear on match cards,
tables, squads, profiles and comparisons. Unique catalogue identities use the
same custom/template/tier card renderer as the bot, with its existing caches.
Rendering happens only on website image requests and excludes remote artwork
downloads. Cards use contained frames so text and borders are not cropped;
large cards open their full image in a new tab. Unknown identities or unavailable
artwork get a neutral card placeholder; missing logos get initials. Media loads
lazily and is cached independently from the three-second live feed.

New read-only endpoints: `/api/stats/<series_id>/seasons` and
`/api/stats/<series_id>/compare?mode=team|player&left=...&right=...&season=all|ID`.
The archive always lists all seasons, even while another page section is scoped
to one season. Historical calculations remain on the website request path.

## Full record leaderboards and premium appearance

Every record category title and its **View all rankings** button opens the full
ranked record book, not just its leading holders. Users can search by player,
team, season or venue, switch seasons within the leaderboard, page through
results and copy a direct ranking link.
Exact tied ranks and the configured batting/bowling qualification rules are
retained. Partnership entries link to their match rather than pretending the
partnership is an individual player. Career aggregates have no invented match.

`GET /api/stats/<series_id>/leaderboard?category=runs&season=all&page=1&limit=25&q=`
is read-only. The expensive underlying record books are cached once per scope
and reused across categories, pages and searches. Unknown categories are refused.

The portal defaults to a glossy dark design, with a persistent light-mode toggle
available on the landing page and every competition page. The cricket landing
scene uses lightweight CSS ball/stadium animation. Opening and per-panel loading
states never wait artificially or block user interaction. Reduced-motion users
receive static artwork and immediate transitions. No external animation or
font dependency is required.
