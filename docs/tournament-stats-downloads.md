# Tournament player statistics downloads

Open a competition at `/stats/<series-id>`, scroll to **Player statistics**, and
choose **All seasons** or an individual season under **Seasons to download**.
The current website season is selected initially. Both downloads include every
recorded player in that scope, independent of the on-screen player search.

- **Download Excel** produces a real `.xlsx` workbook with **Summary**, **Player
  Stats**, **Season Breakdown**, and **Stats Guide** tabs. Player Stats contains
  one row per identified player/card version; Season Breakdown splits these
  totals by season. Filters and frozen player/version columns make wide sheets
  easier to use. Numbers remain numeric; overs are explicitly labelled text.
- **Download CSV** produces UTF-8 CSV compatible with Excel and Google Sheets.
  **CSV rows** selects combined player/version totals or one row per season.

All 56 columns are documented in Stats Guide: player identity, current overall
and batting/bowling ratings, role, country, teams, seasons, appearances, full
batting and bowling totals and rates, milestone timings, hat tricks, verified
fielding, Player of the Match awards, and source coverage.

Current squad overrides take precedence over catalogue ratings. Historical
ratings are not stored in these scorecards. Recorded card IDs keep different
versions separate; ambiguous identities remain separate by season/team.

Only completed, non-simulated scorecard evidence is used. Seasons with no player
scorecards can still export their saved aggregate totals. Missing historical
fields remain blank, and unknown denominators propagate to calculated rates.
Live innings are excluded. Fielding needs complete verified delivery coverage
and an unambiguous player/team identity. Three-wicket hauls count spells with
three or four wickets, with five or more counted separately.

The read-only endpoint is
`/stats/<series-id>/players/download?season=all&format=xlsx`.
CSV additionally accepts `layout=totals` (default) or `layout=seasons`.
Only published, active competitions and their own linked seasons are accessible.
Downloads contain public cricket fields, with no user/chat IDs, private match
settings or raw event streams. Spreadsheet text cannot execute formulas.
