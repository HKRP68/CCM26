Tournament results automatically publish to the destinations configured with
`/tchannels` after the result is committed. Posts include the scores, result,
Player of the Match when available, and the match link, followed by:

> 📢 Join our Official Channel: @cricmasterultra
> 💬 Join our Official Group: @cmugames

Both handles stay in the caption when a long Super Over history moves onto the
scorecard image. Existing delivery retries and duplicate protection still apply.

The website's Tournament → Schedule page now has a **100% Win** selector beside
each unstarted fixture's **Match finish** setting. Choose either participating
team, or **Normal** to clear the selection. The selected side wins the simulated
match on the final legal delivery: a successful chase, or a defence by 1–3 runs.
The setting follows the team regardless of who bats first or which registered
captain plays it. Replacing the selected team in a fixture clears its setting.

It also works alongside one or two configured Super Overs: regulation ends
level, and the selected side wins the deciding Super Over. Deliveries are
conditioned before scoring, so the scorecards, player figures and standings
agree with what was played. Administrative settings remain on the website and
are filtered from live and archived participant API responses. Manual results
and forfeits continue to use their existing result rules.

Completed approach matches announce the winner before stats, rewards and
archives finish processing, then update that message with rewards and
tournament news. The final delivery has no additional suspense pause. The
Mini App's open tournament tab refreshes every five seconds while visible and
online, including stats/MVP when a fixture changes.

The existing tournament Super Over setting remains available. All Super Overs
now share attacking final-over conditions and bounded chase pressure, using the
same ball model for ordinary and configured matches. Ratings, shots, deliveries,
dots and wickets still matter; there is no special tournament scoring boost.
The existing two-Super-Over setting still conditions the first round to finish
level, while its deliveries use the shared engine.

No-balls can produce fours or sixes, a bowled/caught dismissal that is overturned,
or a rare run out while attempting a run. Completed runs and the no-ball penalty
count separately. A no-ball awards a free hit, which survives intervening wides
and no-balls; only a run out can dismiss a batter on it. Bowler figures exclude
run outs. A faced no-ball counts as a batter's ball but never as a legal delivery.
The auto-simulation follows the same extra-ball and strike-rotation rules.

Live commentary calls out these incidents and late chase equations. A short
turning-point recap uses actual deliveries from the Super Over, including
reprieves, wickets, big hits and the winning delivery.

Tournament comparisons:

- `/th2h [#ID] @username`: the caller's tournament record against another captain.
  Replying to their message also works. Uses the recorded fixture winner, so a
  Super Over win counts as a win despite level main-innings scores.
- `/tvs [#ID] Virat Kohli | Jasprit Bumrah`: batter-versus-bowler runs, balls
  faced, dismissals, dots, boundaries, strike rate and average. For ambiguous
  player names, use `Virat Kohli @ Team name`. Only completed tournament matches
  count, and Super Over figures stay separate from the main-innings statistics.

`#ID` selects a specific tournament, including past tournaments; omit it when
only one tournament is active. Ball-level comparisons are recorded from this
update onward in both Challenge League and Lets Play tournament matches through
their shared approach engine. Older scorecards and manual/deadline results
cannot supply batter/bowler encounters. Captain records use existing results
where both participating user IDs were recorded.

Tournament approach matches have a single 0.5% roll for an in-match concussion
incident. A medical assessment is announced between overs, while play can still
continue. The incident requires a batter currently at the crease who has faced
a delivery and a suitable unused reserve. The substitute must match the role,
overseas status and bowling style for bowlers/all-rounders, stay within ten
points of both ratings, and satisfy the XI's rating rules. Without a suitable
reserve, no substitution is generated. Thus the actual incidence is below 0.5%.

The injured batter retires hurt without a wicket, keeps their recorded figures,
and cannot return to field, bowl or bat in a Super Over. The substitute appears
with `-CS` on live scorecards. The Impact Player allowance is unchanged, including
when already used. This is an IPL-inspired simulation mechanic, not a complete
implementation of IPL medical regulations. It does not create a multi-match
injury layoff; the existing post-match injury system remains separate.

Suggested next features: a live win-probability graph, tournament rivalry cards,
qualification scenarios, and a post-match turning-point recap.
