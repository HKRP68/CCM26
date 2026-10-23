Tournament results automatically publish to the destinations configured with
`/tchannels` after the result is committed. Posts include the scores, result,
Player of the Match when available, and the match link, followed by:

> 📢 Join our Official Channel: @cricmasterultra
> 💬 Join our Official Group: @cmugames

Both handles stay in the caption when a long Super Over history moves onto the
scorecard image. Existing delivery retries and duplicate protection still apply.

For tournament fixtures using the existing planned Super Over setting, both
sides now play under the ball engine's attacking death-over conditions with a
1.45 multiplier on boundary weights. This
raises expected scoring, while deliveries, shots, ratings, extras, dots and
wickets still matter. There is no minimum score or selected winner. Ordinary
Super Overs retain their existing balance. The existing two-Super-Over setting
still conditions the first Super Over to finish level.

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
