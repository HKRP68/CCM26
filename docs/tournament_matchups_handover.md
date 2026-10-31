# Season matchups and tournament captain handover

## Commands

- `/tvs [#tournamentID] Batter | Bowler`: deliveries recorded in one tournament.
- `/svs [#tournamentID] Batter | Bowler`: deliveries across that tournament and the seasons linked to its Total Season Stats. With automatic links, this includes the same competition's tournaments and saved seasons. With multiple active tournaments, specify `#ID`.
- `/frivalry [#tournamentID] Team A | Team B` or `/frh2h`: franchise rivalry across the same current and linked seasons. For example, `/frivalry MI | CSK` when those short names are configured and unique.
- `/handover @manager` or `/transfermatch @manager`: offer control of your live tournament team. A Telegram user ID or a reply to the receiving manager's message also works.

For example: `/svs Virat Kohli | Jasprit Bumrah`. When names are shared by teams, use `Virat Kohli @ Alpha`. A player's records for the same team merge across rebuilt seasonal rosters; records for different teams require choosing the team.

Matchup figures use completed main innings only. Older scorecards and manual results do not contain delivery encounters. `/svs` reports how many selected seasons have encounter data; it does not reconstruct missing history. Deleting a tournament with **keep stats** now preserves recorded encounters alongside its player totals. Existing saved seasons without encounters remain unavailable for matchups.

## Handover behavior

The playing captain must be a registered owner or co-owner of the team. The receiver must also be a registered manager of that same team, have a bot account, and be free of other live matches. The receiver taps **Accept** within 60 seconds. They can **Decline**, and the sender can **Cancel**. Requests survive bot restarts and can be answered only once. Team membership and availability are checked again on acceptance.

The current captain keeps control until acceptance. Acceptance transfers pick permissions in chat and the Mini App. It preserves the score, XI, selected approaches, fixture, original result/stat identities and remaining turn time. The new captain uses the current buttons. The previous captain's pick permissions end immediately. Control follows the team through innings changes, pause/continue, and any subsequent Super Over. A captain can offer control back using the same command. New handovers are unavailable during an active Super Over or while the match is saved rather than live.

Accepted handovers are recorded in the match state with the team, sender, receiver, time, innings and over. An inactivity fine is charged to the current controlling captain. A request does not pause or restart either team's clock.

## Franchise rivalry

The rivalry card shows completed meetings, each franchise's wins, separate ties and no-results, recorded Super Over wins, decided-match win percentages, highest main-innings totals against each other, the latest eight seasons' breakdown, and the last five meetings in date order. All selected seasons contribute to the overall totals. League and playoff results both count. Simulated results are included and marked; a real replay replaces its simulated fixture without counting twice.

Franchises are matched by full name, ignoring case and extra spaces. Team IDs, squads and captains may change between seasons. Configured short names and unique partial full names work as search terms; ambiguous matches require the full name. A franchise rename is treated as a different franchise until an explicit alias feature is added.

Only completed fixture evidence counts. Highest totals exclude unfinished no-result innings; unavailable scores show `—`. Super Over wins require an explicit stored marker and are already included in overall wins. Equal main-innings scores alone do not establish a Super Over win.

Deleting a tournament with **keep stats** now saves its completed result history and team names, preserving linked rivalries after deletion. Existing archives with player totals alone cannot reconstruct head-to-head results; the card reports available versus selected seasons. Deleting without keeping stats, or removing a saved season, removes those results from the rivalry.

## Suggested next features

1. **Phase filters for `/svs`**: powerplay, middle and death overs, with balls faced shown beside every rate. This makes the matchup useful for choosing when to bring on a bowler.
2. **Recent form beside career totals**: compare the last five recorded matches with all linked seasons, so a small recent sample is visible.
3. **Franchise rename aliases**: explicitly connect an old and new franchise name across seasons while keeping unrelated teams separate. The basic rivalry card is available now through `/frivalry`.
4. **Co-owner availability**: managers opt in as available for the next fixture; the bot suggests those managers in the handover flow.
5. **Captain timeline**: expose the existing accepted-handover audit beside the match scorecard, showing who controlled which innings and overs.

Phase filters and recent form need additional per-phase/per-match encounter evidence; they should not infer those figures from existing aggregate rows.
