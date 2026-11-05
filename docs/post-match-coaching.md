# Post-match coaching

Completed matches now show a short coaching summary in Telegram, the Mini App
result screen and the downloadable CIPL / LetsPlay analysis report. The report
places coaching first, with up to two turning points and one next-match tip
for each captain. Archived Arena results can rebuild it from their saved state.

`services.match_coaching.build_post_match_coaching` reads recorded match facts
without changing state, ratings, rewards, results or simulation probabilities.
There is no external model call or new database table.

Turning points rank recorded chase-probability changes, first-innings momentum
changes, wicket clusters and unusual scoring overs. A single innings/over can
appear once. Adjacent probability records are compared; missing probabilities
are never treated as zero. Scores or wickets that were not recorded are omitted.

Each captain gets the highest-priority supported tip:

1. Promote an equipped finisher who faced no balls when the innings reached
   their activation window. This requires explicit zero-ball, not-out stats;
   it is skipped for inactive players, disabled traits and early finishes.
   Ice Finisher is considered only in a chase.
2. Reserve a death specialist's quota when the over log proves it was entirely
   spent before the final three overs or sets.
3. Change a batting intent or bowling plan after a recorded consecutive streak
   reaches the engine's predictability threshold.
4. Look for singles when recorded batting stats show sustained dot-ball pressure.
5. Practice rotating strike through the quietest recorded two-over spell.

The rules support six-ball overs and The Hundred's five-ball sets. Player and
team names are escaped in Telegram and the offline report; the browser uses
text nodes. Coaching delivery failures are logged without interrupting match
completion. Missing evidence produces fewer points or no coaching, rather than
a guessed explanation of why a team won or lost.

Validation covers factual ranking, distinct turning points, early finishes,
trait votes, missing stats, inactive players, innings and captain identity,
duplicate team names, legacy trait caches, death-bowling quotas in both formats,
HTML escaping, Telegram limits, result payloads, file delivery and browser
rendering/clearing of the coaching card.
