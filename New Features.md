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