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

Tournament approach matches can now pause for a mid-match injury. Each team has
one 5% roll per match (not per over), scheduled between completed overs while
play can still continue. A batter who has faced a delivery or a bowler who has
just bowled may be assessed unfit. An incident is generated only when a fit,
unused like-for-like reserve can keep a legal XI.

The affected captain chooses their **one concussion/medical substitute per team
per match** from the chat buttons or Mini App. `/concussion` (`/medicalsub`)
reopens a deleted prompt. The match stays paused until the decision is saved;
normal picks, Impact swaps and stale buttons cannot advance it. The bot and the
normal turn timeout select the closest legal reserve automatically, without a
fine or forfeit. An undelivered prompt is retried. Save, restart and `/rcl`
preserve the outstanding medical choice and the used allowance.

Only unused, fit squad members qualify. They must match role, overseas status
and bowling style for bowlers/all-rounders, remain within ten points of both
ratings, and preserve **exactly eleven active players** and all applicable XI
rules: keeper, bowling options, overseas limits, rating requirements, roster
role limits and no duplicate versions of a player. Current tournament injuries
are rechecked before the choice is accepted. If every legal reserve becomes
unavailable during the pause, play remains paused for an admin to restore an
eligible reserve; the system never fields an illegal XI.

The injured player's recorded figures are retained and they cannot return to
bat, bowl, field or play a Super Over. A batter retires hurt without a wicket;
the replacement takes the same batting position and strike. A bowler's
replacement shares the remaining bowling quota and inherits the consecutive
spell restriction. The replacement appears with `-CS` on live scorecards and
the medical event is saved with tournament statistics. Impact Player retains
its own separate allowance, including when already used.

This is a simulation medical-replacement rule, including bowling injuries,
rather than a claim to reproduce formal concussion regulations. The existing
opt-in post-match injury/layoff system remains separate: a medical substitution
rules the player out for this match and does not itself create a multi-match
layoff. Casual games and standalone Super Overs do not roll medical incidents;
`concussion_enabled=False` disables new incidents on a match state.

Suggested next features: a live win-probability graph, tournament rivalry cards,
qualification scenarios, and a post-match turning-point recap.
