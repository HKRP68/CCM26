# Mini App — Tournament tab

A **🏟️ Tourney** button appears in the Mini App's bottom row **only while a
tournament is live** (`Tournament.is_active`). When nothing is live the button
stays hidden, and a player already on the screen is sent back to Home.

A Challenge League tournament and a Let's Play tournament can be live at the
same time. When both are, a chip row at the top of the screen switches between
them.

## What it shows

| Sub-tab | Contents |
|---|---|
| **Overview** | Your team's position, live matches, next fixture, latest 3 results, leaders (runs, wickets, highest score, best figures), MVP leader, and the top 4 of the table |
| **Table** | Points table: P / W / L / NR / NRR / Pts, last-5 form, your team highlighted, and a qualification line from the knockout type (`top4_sf` and `ipl_playoffs` → 4; `groups_top2_sf` → 2 per group; `groups_top4_qf` → 4 per group). Groups with separate tables get one table each. Manual points adjustments are footnoted. |
| **Matches** | **Results** (newest first) and **Remaining** fixtures (grouped by stage, with a NEXT badge, venue and pitch). Live matches are pinned on top. Tap a result to open its scorecard. |
| **Stats** | Most runs, most wickets, highest score, best figures, most sixes, most fours, most 50s and 100s, best strike rate, best economy, best average |
| **MVP** | The impact-points race, with bat, bowl, result and Player of the Match points |
| **Injuries** | Players currently ruled out. Only shown when injuries are enabled. |

Scorecards open in the shared bottom-sheet drawer:

- A bot-played match serves its full `MatchScorecard`, or the live state while it is in play.
- A result that was entered by hand or imported falls back to the per-player lines in `TournamentMatch.scorecard_json`. These have no dismissal details and are labelled as a summary.

## Endpoints (`admin.py`, served by `services/tournament_webapp.py`)

| Endpoint | Body | Returns |
|---|---|---|
| `POST /api/webapp/init` | — | adds `tournament: {live, tournaments:[{id, name, kind, kind_label}]}` |
| `POST /api/webapp/tournament` | `{tournament_id?}` | header, `table`, `group_tables`, `fixtures`, `injuries`, `my_team_ids` |
| `POST /api/webapp/tournament/stats` | `{tournament_id}` | `boards[]` and `mvp[]` |
| `POST /api/webapp/tournament/match` | `{tournament_id, fixture_id}` | a scorecard in the drawer's `innings` shape |

An id that names a tournament which is not active is refused. The main endpoint heals the aggregates (`recompute_tournament`, `heal_live_fixtures`) at most once every `_TOURNAMENT_RECOMPUTE_TTL`, the same throttle the admin dashboard uses.

Tests: `tests/test_webapp_tournament.py`.
