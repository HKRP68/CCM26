# Tournament records and presentations

- Records stay silent during play. Once the scorecard is committed, the group receives one `match-<id>-records.md` document containing the match recap and every broken or equalled season/all-time record. Corrected scorecards update that document. Existing live-watch notifications are no longer delivered.
- `/seasons record [competition]` downloads the latest season's current holders, including joint holders and rate qualification minimums. The season and record cards also have a Records file button and an opener-only Close button.
- League fixtures and each playoff stage appear in separate sections in Telegram, the tournament Mini App and the admin overview.
- League fixtures repeat **four Night, two Day** in fixture order; playoffs are always Night. The period drives actual weather/dew conditions. Free-play matches keep their existing random conditions.
- **Tournament → Manage → Playoff pitches and stadiums** sets neutral playoff conditions in advance. Empty selections inherit the tournament defaults. Changes apply to unstarted playoffs, leaving played/live fixtures alone.
- **Tournament → Schedule → Match finish** lets website admins choose normal play, one Super Over or two Super Overs for an unstarted fixture. This control is never included in public payloads or announcements. Scoring is steered before deliveries are recorded, so the scorecard and result agree. A two-Super-Over setting ties the first Super Over before opening the second; subsequent unresolved ties retain the normal replay rules. The feature applies to matches played through Challenge League and Lets Play, including their Arena play flows. Manual result entry and deadline simulations retain their explicit result controls.

## Awards ceremony

Prizes continue to pay once when the final is decided. Presentations wait for an admin to run `/awardceremony`, or `/awardceremony #<tournament-id>` when choosing a finished tournament. Each award is presented in a separate photo message, two seconds apart, with the winner, team, contribution and actual prize. If Telegram fails, running the command again resumes the remaining awards without paying prizes again.

Set team ceremony photos on **Tournament → Manage → participating team → Save team photo** using an HTTPS image URL. Without one, the team's logo/crest is used. Players use their generated player cards with their team and tournament contribution; imported players without master artwork get a named award card.

Awards include Champion, Runner-up, Orange Cap, Purple Cap, MVP and Emerging Player. Configure the new prize on Manage or with `/tprize emerging <coins> [gems]`.

**Emerging Player rule:** highest tournament MVP points among players rated **83 or below**, with at least half the appearances of the most-used player (rounded up). Lower rating breaks equal MVP points. Players with unknown ratings are ineligible. This rewards performance from lower-rated players without imposing IPL age rules on the game's roster.
