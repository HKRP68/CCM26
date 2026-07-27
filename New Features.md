In /cipl and Other same.(League Battle / Challenge League )

After the Host Clicks match starts, the toss should happen exactly like the current system.

Use existing Toss Like 
After the toss:

For the batting team, no extra batsman selection is required because the batting order is already selected during Playing XI selection.

For the bowling team, show only bowlers and all-rounders for bowler selection over by over.

The match simulation will run over by over.

For every over, both batting and bowling users must select their approach.

Flow:

1. Bowling user selects the bowler for the over.
2. After selecting the bowler, the bowling user sees Bowling Approach buttons.
3. Bowling user selects one bowling approach.
4. After bowling approach is selected, the batting user sees Batting Approach buttons.
5. Batting user selects one batting approach.
6. The over is simulated ball by ball using both selected approaches.
7. After the over ends, repeat the same flow for the next over.
8 When the New Over starts Delete the Message of Previous overs 
9 In Message There should be a Link of Miniapp Where this Match Scorecard, commantry Teams and Other can Be checked

Batting Approaches:

1. 🛡 Defensive
   Used when the batsman wants to survive.

Effect:
Low wicket chance
Low boundary chance
More dots and singles
Best for weak batsmen or tough bowling conditions

Example:
The batsman plays carefully, defends good balls, and rotates strike only when safe.

2. 🔁 Rotate Strike
   Safe and balanced batting approach.

Effect:
More 1s and 2s
Low wicket risk
Few boundaries
Best for middle overs

Example:
The batsman focuses on finding gaps, taking quick singles, and keeping the scoreboard moving.

3. ⚖ Balanced
   Normal cricket approach.

Effect:
Equal chance of singles, boundaries, and dots
Medium wicket risk
Good default AI mode

Example:
The batsman plays according to the ball, attacks loose deliveries, and respects good bowling.

4. 🚀 Aggressive
   Controlled attacking approach.

Effect:
Higher chance of 4s and 6s
Higher wicket risk
Best for powerplay or chase pressure situations

Example:
The batsman looks to dominate the bowler and punish loose balls.

5. 💥 Ultra Attack / Slog
   Full-risk attacking mode.

Effect:
Very high six-hitting chance
Very high wicket chance
Best for death overs or last-over chase situations

Example:
The batsman goes all out and tries to clear the boundary almost every ball.

Bowling Approaches:

1. 🛡 Defensive Bowling
   Bowler focuses on stopping runs.

Effect:
Low boundary chance
Low wicket chance
More singles and dots
Best when defending a total

Example:
The bowler keeps it tight, bowls safer lines, and avoids giving boundary balls.

2. ⚖ Balanced Bowling
   Normal smart bowling approach.

Effect:
Medium wicket chance
Medium run chance
Good default AI mode

Example:
The bowler mixes line, length, and variations according to the batsman’s weakness.

3. ☠️ Mixed Bowling
   Special mode for last overs.

Effect:
More yorkers, slower balls, and smart variations
Reduces six chance if bowler rating is high
Low-rated bowlers can leak many runs

Example:
The bowler tries to outsmart the batsman with yorkers, slower balls, and surprise deliveries.

4. 🔥 Aggressive Bowling
   Bowler attacks for wickets.

Effect:
Higher wicket chance
Higher boundary chance
Best against new batsmen or tailenders

Example:
The bowler attacks the stumps, uses bouncers, yorkers, and risky wicket-taking deliveries.

5. 🌀 Variation Bowling
   Bowler uses slower balls, cutters, spin tricks, and deception.

Effect:
Good against aggressive batsmen
Can force mistimed shots
Medium wicket chance

Example:
The bowler changes pace and uses variations to make the batsman play false shots.

Simulation Engine Requirements:

Use the existing engine from "CricketSimulation.zip".

Engine structure:

engine/

- match.py
  Ball-by-ball match driver.

- ball_outcome.py
  Probabilistic outcome model.

- game_state_engine.py
  Momentum engine, par-score curve, and match state analysis.

- pressure_engine.py
  Psychological pressure and clutch situation handling.

- scenario_engine.py
  Dramatic finish and special scenario handling.

- commentary_engine.py
  Ball-by-ball commentary generation.

- stats_service.py
  Match stats persistence and aggregation.

The simulation must use:

Pressure Engine
Momentum Engine
Scenario Engine for dramatic finishes
Ground Conditions
Game Mode
Pitch Profile
Phase Boost
Blending Weight
Batting Approach
Bowling Approach
Player Ratings
Match Situation
Required Run Rate
Wickets Remaining
Overs Remaining

The final outcome of every ball should be calculated using a blended simulation model.

The outcome should depend on:

Batsman rating
Bowler rating
Batting approach
Bowling approach
Pitch profile
Ground condition
Match phase
Momentum
Pressure
Scenario state
Game mode

The system should not generate random results only. Every ball outcome must feel realistic, cricket-based, and situation-aware.

Example over flow:

Bowling Team User selects: Jasprit Bumrah
Bot shows Bowling Approach buttons:

🛡 Defensive
⚖ Balanced
☠️ Mixed
🔥 Aggressive
🌀 Variation

Bowling user selects: ☠️ Mixed

Now batting user sees Batting Approach buttons:

🛡 Defensive
🔁 Rotate Strike
⚖ Balanced
🚀 Aggressive
💥 Ultra Attack / Slog

Batting user selects: 🚀 Aggressive

Now the engine simulates the complete over ball by ball using:

Bumrah’s bowling rating
Current batsman rating
Selected bowling approach
Selected batting approach
Pitch profile
Ground condition
Pressure
Momentum
Match phase

After the over ends, show:

Over summary
Runs scored
Wickets lost
Current batsmen stats
Bowler over stats
Updated score
Required run rate if chasing
Momentum shift
Short commentary summary

Then continue to the next over with bowler selection again.

---

## Implementation Suggestions (added during build)

These are the engineering decisions made while implementing the feature. They
fill gaps the spec left open and keep the new mode isolated from existing flows.

1. **Scope** — The over-by-over Approach flow is wired into `/cipl` and every
   admin-created Challenge League command (the `challenge_league_handler` path).
   `/cm` and `/playmatch` (`/wpm`) are intentionally left unchanged.

2. **Toss first, then play in chat** — After the host clicks *Start Match*, the
   guest calls heads/tails and the winner elects bat/bowl using the existing
   `run_coin_toss` animation. Only then does the over-by-over flow begin, played
   entirely in the Telegram chat. The Mini App link in each over message is a
   **read-only** scorecard/commentary view.

3. **No mid-over batsman selection** — Because the batting order is fixed during
   Playing XI selection, new batsmen come in automatically (in order) when a
   wicket falls, so an over is simulated as one atomic unit.

4. **Approach effects as weight multipliers** — Approaches scale the engine's raw
   outcome weights right before the delivery is sampled (`engine/approach_modifiers.py`),
   so results still respect ratings, pitch, momentum, pressure and scenario logic.
   Canonical keys: batting `defensive/rotate/balanced/aggressive/ultra`; bowling
   `defensive/balanced/mixed/aggressive/variation`. `Balanced` is neutral.
   *Mixed* bowling suppresses sixes more when the bowler is highly rated.

5. **Auto-pick on timeout** — If a captain doesn't pick within 90s, the match
   continues automatically: the bowling captain's idle approach defaults to
   *Balanced* (and an idle bowler pick defaults to the top-rated eligible bowler),
   the batting captain's idle approach defaults to *Balanced*. No forfeit.

6. **Per-over message cleanup** — Each over uses one editable action message plus
   one summary message; both are deleted when the next over begins.

7. **Bowler quota** — A bowler may bowl at most `ceil(overs / 5)` overs and never
   two overs in a row, matching standard limited-overs rules.

8. **Reuse over rewrite** — Stats persistence reuses `player_stats_service`,
   the SimCricketX engine (`engine/`) and `services/sim_match` adapters are reused
   rather than duplicated, and match state reuses the existing `match_state` store.
---

## Approach counters and the AI captain (2026 update)

### Why the approach tables changed

The five batting and five bowling approaches above were each scaled by their own
fixed multipliers, applied independently. That made the over *separable*: an
approach shifted the odds the same way whatever the other captain chose, and a
separable game always collapses to a single right answer for each side.

Simulated against the live engine (500 overs per cell, four pitches, seven
rating match-ups), that is exactly what had happened:

* **Ultra Attack** out-scored every other batting approach against **every**
  bowling plan — 244 runs an innings against 200 for the next best.
* **Defensive** out-contained every other bowling plan against **every** batting
  approach.

So a player who simply picked Ultra Attack and Defensive every over was playing
optimally, and eight of the ten options were decoration. It is also why the bots
felt easy: they varied their plans, and varying was strictly worse.

### What replaced it

Two changes, both in `engine/approach_modifiers.py`:

1. **Risk is priced.** Aggressive and Ultra buy their boundaries with a
   genuinely higher wicket rate, so slogging from ball one runs out of batting
   before it runs out of overs.
2. **A counter matrix** (`_COUNTER_MULT`) multiplies on top of both tables, keyed
   by the *pair* of choices. This is the layer that makes the over
   rock-paper-scissors — the same plan wins one match-up and loses another:

   | If they bat…    | …the best bowling plan is | If they bowl…  | …the best batting intent is |
   |-----------------|---------------------------|----------------|------------------------------|
   | Defensive       | 🔥 Aggressive              | 🛡 Defensive    | 💥 Ultra Attack              |
   | Rotate Strike   | 🛡 Defensive               | ⚖ Balanced     | 💥 Ultra Attack              |
   | Balanced        | ☠️ Mixed                   | ☠️ Mixed        | 🔁 Rotate Strike             |
   | Aggressive      | 🌀 Variation               | 🔥 Aggressive   | 🚀 Aggressive                |
   | Ultra Attack    | 🌀 Variation               | 🌀 Variation    | ⚖ Balanced                   |

   Two options stay deliberately "safe but not optimal" on runs alone, which is
   the role they play rather than a bug: **Balanced bowling** is the neutral
   baseline, and **Defensive batting** is a survival tool whose value (protecting
   the last wicket, seeing off a spell) a runs-scored yardstick cannot show.

`tests/test_cipl_approach.py` asserts the no-dominant-approach property directly,
so a future retune that brings back a single right answer fails the suite.

### The AI captain (/lpbot, /ciplbot)

`services/bot_tactics.py` is the bot's brain. Each time it must pick an approach
it rebuilds the ball-outcome odds for the coming over, runs all 25 match-ups
through the engine's own modifiers, scores each on **win probability** (chasing)
or **runs minus the real price of the wickets risked** (batting first), and
solves the resulting 5×5 zero-sum game by regret matching. It plays the Nash
mixed strategy — the mix no fixed counter-strategy beats — pulled toward a best
response when the player's own picks start showing a pattern.

The ball model is fitted, not guessed: its coefficients come from a
least-squares fit to ~170 situations sampled out of `cipl_match.simulate_over`,
and it tracks the real engine to about 14% RMS across run rates from 2.9 to 20
an over.

### Difficulty

`/lpbot` and `/ciplbot` open with a difficulty prompt (🟢 Easy / 🟡 Normal /
🔴 Hard); the choice is remembered per player and shown on the match card next to
the bot's captaincy persona. Difficulty scales **how well the bot captains** —
how far the solver leads, how wide its mix stays, how hard it presses a read on
you, and how often it simply misjudges an over. It never changes the squad, the
engine or the rules: an Easy bot plays under the same bowler quotas and the same
laws as a Hard one.

### Scorecard attribution (2026 update)

* **The bot's Approach is no longer published.** The over summary used to print
  "🤖 Bot's plan: 🌀 Variation" after every over, on the reasoning that there was
  no opponent to keep it from. But the player *is* the opponent: a bot whose
  every pick is printed is a bot whose mix can be written down over a few matches
  and countered, which defeats the point of solving for an unexploitable one. Its
  plans are now hidden exactly as a human captain's are. What the bot has
  *noticed* is still said out loud ("it has spotted three straight Ultra Attack
  overs") — a warning is not a plan, and it keeps the mind game visible.
* **A bot-run side is named on the summary card** — `RCB (Bot)`. In a league bot
  match the bot fields a real franchise, so without it "RCB won by 8 wickets"
  reads exactly like a result against a human. Applied at render time only; the
  plain team name stays the key for POTM and result logic.
* **The archived text scorecard credits the captains** — `RCB (@alice) vs CSK
  (@bob)`, with a bot side credited as `(Bot)`. `MatchNo<id>.txt` is the durable
  record, and two different players fielding RCB previously produced identical
  files. The archive also now carries the pitch, the ground and the Player of the
  Match, which the on-screen card had and the file did not. An `@` is only
  prefixed when the stored value could actually be a handle — a first name is
  left as plain text rather than dressed up as one.
