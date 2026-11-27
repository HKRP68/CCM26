# Cricket Moments and Boss Battles

In Event Studio choose **Cricket Moment** or **Boss Battle**. Bosses can use a full T20 or ODI match or a Moment. The first of the two configured teams is the player's side. The first official success is the challenge reward source. A later attempt can improve best stars and reach an additional milestone.

Moments support a bounded batting or bowling segment, and chase or defense challenges from a simulated score. Choose a prefix seed, start over and play window. The Studio derives a checkpoint using the shared ball engine and rejects an innings that ended before the checkpoint. The prefix includes the actual score, active batters and bowling quotas. ODI checkpoints use the 50-over adapter and its saved workload and RNG state.

Two examples for use with your own eligible squads:

- **Batting sprint.** Set `goal_type` to `segment`, `side` to `bat`, `start_over` to 16, `overs` to 4 and `goal` to 35. Preview the proposed seed and adjust the checkpoint if its innings ended early.
- **Death-over defense.** Set `goal_type` to `chase`, `side` to `bowl`, `start_over` to 16 and `overs` to 4. The generated first-innings score establishes the target. The defense succeeds when actual play produces a defending win within the window.

Players see the starting score, actual continuation progress and remaining legal balls. A Moment attempt consumes an official slot when it launches. Attempts can resume during grace. The scorecard includes the prefix. Event player stats count only the bat runs, balls and bowling figures earned after the checkpoint. The Studio can schedule up to 12 daily or weekly editions of an approved template. Each edition has its own deterministic checkpoint seed, published window and attempt history. Validation checks each edition and the combined maximum payout against the series budget. Assign a chapter name, position and an earlier published Moment slug to create a chapter path. The server checks the required best-star result before enrollment. Repeat successes do not erase the best attempt. Boss Battles can display an opt-in milestone for distinct players who have defeated the boss. Practice replays remain pending.

## Recorded checkpoints

Paste a JSON object into **Recorded scorecard checkpoint** to use supplied figures. For a first-innings Moment, provide `current`. For a second-innings Moment also supply `first_innings` and `target`. The target must equal the completed first-innings total plus one. XI slots are 1–11 in the published batting and bowling XIs for that innings. A null checkpoint selects the deterministic prefix generator.

Each scorecard supplies:

- `runs`, `wickets`, `extras`, `crease` in striker and non-striker order, and `partnership` with `runs` and legal `balls`.
- `batting` rows for batters who have appeared, with `slot`, `runs`, `balls`, `fours`, `sixes`, `dots`, `dismissal` and `bowler`. Use an empty dismissal for a batter still in and null for a dismissal without bowler credit. List dismissed batters in batting appearance order.
- `bowling` rows with `slot`, `runs` conceded, legal `balls`, `wickets`, `dots` and `maidens`.
- Chronological `overs` rows with `bowler`, legal `balls`, `runs`, `wickets`, `credited_wickets` and `maidens`. The active innings must start on an over boundary.

Validation checks totals, crease positions, credited wickets, quotas and the previous bowler. Choose whether extra objectives count from the first live delivery or include preset figures. The primary goal always measures the live continuation.
