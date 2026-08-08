# Approach Interaction System

How the over-by-over Approach game (`/cipl`, `/c<league>`, `/letsplay`) turns two
hidden picks into a delivery. This is the implementation note for the CIPL /
LETSPLAY *Approach Interaction System* design; it records what was built, what
was deliberately built differently, and where each rule lives in the code.

Everything here happens in `engine/approach_modifiers.py`, which scales the
outcome weights `engine.ball_outcome.calculate_outcome` has already built from
ratings, pitch, pressure, momentum and scenario logic. The approach layer never
decides a ball — it *tilts* one.

## The over

1. The bowling captain picks a bowler, then a bowling plan.
2. The batting captain picks a batting intent, without seeing the plan.
3. `services.cipl_match.simulate_over` builds one **context** for the over
   (`approach_context`) and bowls every ball of it under that context.

## The layers

`approach_multipliers(bat, bowl, bowler_rating, context)` is the single source of
truth for what a match-up does. Both the live over and the AI captain's payoff
matrix (`services.bot_tactics.payoff_matrix`) go through it, so the bot is always
reasoning about the over the player is about to face.

| # | Layer | Depends on | Table |
|---|-------|-----------|-------|
| 1 | Batting intent | the pick | `_BATTING_MULT` |
| 2 | Bowling plan | the pick | `_BOWLING_MULT` |
| 3 | Counter matrix | both picks | `_COUNTER_MULT` |
| 4 | Special combination | both picks | `SPECIAL_COMBOS` |
| 5 | Pitch × approach | context | `_PITCH_RULES` |
| 6 | Phase × approach | context | `_PHASE_RULES` |
| 7 | The mind game | context | `_READS`, `READ_BONUS`, `OUTTHINK_BONUS` |
| 8 | Momentum / fatigue / chemistry | context | `MOMENTUM_*`, `FATIGUE`, `CHEMISTRY_*` |
| 9 | Last over's carry-over | context | `combo_carry` |

Layers 1-4 depend only on the two picks and always apply. Layers 5-9 need a
context; without one they are inert, so a caller that knows nothing about where
the over is being bowled still gets a coherent 5×5 game.

### 3 — the counter matrix

The interaction matrix in the design doc (five run/wicket bands per cell) is
implemented as *relative* terms rather than absolute per-over numbers, because
the engine's own calibration decides the absolute level. What is preserved is the
matrix's shape: runs and wicket risk both rise monotonically with batting
aggression, and the per-plan ordering inside each row holds. Its per-cell prose
lives in `MATRIX_FLAVOUR`.

The tables were balanced by measurement, not by feel — see the long note above
`_BATTING_MULT` — and `tests/test_cipl_approach.py` asserts the property that
matters: no approach is the right answer everywhere, and none is dominated
everywhere. `SituationalBalanceTests` re-runs that bar with layers 5-9 switched
on, so a new pitch or phase rule cannot quietly hand one intent every column.

### 4 — special combinations

Eight named match-ups, with the doc's mechanical twist attached. Two are
implemented in spirit rather than literally:

* **The Shootout** (Ultra vs Aggressive) — "runs are doubled" would put totals on
  the board no batting card could explain, and would ignore the wickets that fell
  inside the over. It is modelled as the volatility the rule is describing:
  boundaries and wickets both spike, the safe outcomes drain.
* **The Gladiator Over** (Aggressive vs Aggressive) — its 18% wicket cap sits
  above the engine's own 12% cap on the normalised per-ball wicket share, which
  binds first. What survives is the part that bites: the boundary surge and a
  mild damp on the wicket.

Four combinations set up the *next* over instead of changing this one. That is
`combo_carry`: it is resolved once the over's result is known, stored on the
state as `approach_carry`, applied to the following over through the context, and
cleared at the innings break.

| Combination | Fires when | Next over |
|---|---|---|
| The Blockathon (Def / Def) | fewer than 4 runs | batting side scores more freely |
| The Survival (Def / Aggressive) | no wicket fell | wicket chance drops |
| The Purist (Bal / Bal) | exactly 8 runs | a small edge to both sides |

### 5 — pitch

Only the **approach-conditional** half of the doc's pitch table is implemented:
hitting pays on Hard and Bouncy, nudging pays on Dry, spin strangles the
accumulators on Dusty, seam suppresses the front-foot intents on Green through
the powerplay, and a Flat deck blunts the attacking bowling plans.

The rest of that table — "spinners get +5% wicket on a dusty pitch" — is *already*
in the engine, in `PITCH_WICKET_FACTOR` and `config/ground_conditions.yaml`.
Implementing it again here would apply the pitch twice.

### 6 — phase

`cipl_match.approach_phase` is the one definition of powerplay / middle / death,
used by the engine and by the bot (`bot_tactics.phase` delegates to it) so the
bot never plans for a phase boundary the over is not bowled in. The Hundred's
powerplay is five sets, not six, and short formats scale both windows down so a
5-over game still has all three phases.

Ultra Attack is worth most at the death and Defensive is worth least; the
accumulator gains in the middle overs and loses value trying to hit spin off the
square; the powerplay's field restrictions cost the bowling side wickets.

### 7 — the mind game

Neither captain sees the other's pick, which is what makes the over a real
simultaneous-move game. Two rules price that:

* **Read the game.** Each batting intent is *built* to beat specific plans
  (`_READS`, from the design doc's "Best against"). Picking the right intent for
  the plan you turn out to be facing is worth +8% runs.
* **Outthink the batter.** Every other cell is the same coin the other way up —
  the batter walked into a plan their intent has no answer to — and is worth
  +10% wicket chance to the bowler. The doc's own example, Aggressive
  premeditating into Variations, is one of these.

Pricing both directions is deliberate: a read bonus with no matching penalty is
a standing gift to the batting side, since ten of the twenty-five cells would
pay out every over and none would cost anything. As priced, the layer is net
bowler-favourable — switching it off entirely *adds* about 10 runs an innings.

### 8 — momentum, fatigue, chemistry

* **Momentum** — 12+ off the previous over and the batting side keeps the
  ascendancy; a bowler who struck in their *own* previous over is more likely to
  strike again. (A run-out at the other end is not a bowler's rhythm, so the
  bowling side of this reads `last_over_wickets`, credited to the bowler.)
* **Fatigue** — a bowler's third over of the innings is where the legs go, and
  the fourth is worse. The engine never lets a T20 bowler send down back-to-back
  overs, so "consecutive" is read as how deep into their own spell the over is —
  which is the quantity the rule is really about, and the one that makes a
  captain think about who is left.
* **Chemistry** — a pair who have batted four overs together run better between
  the wickets. Reset, like the partnership itself, by a wicket.

## What was deliberately *not* built

**Player rating modifiers** (the Elite / Good / Average / Below Avg / Poor
ladder). `engine.rating_duel` plus the level and tier layers in
`engine.ball_outcome` already scale boundaries, dots and wickets by both players'
absolute ratings *and* by the gap between them — far more finely than a
five-rung ladder. Adding the ladder on top would apply class twice and unpick the
innings-total calibration the whole engine is tuned to. The one place a rating
still meets an *approach* directly is Mixed bowling, whose six suppression scales
with the bowler's rating (`_mixed_six_mult`).

## Reading the design doc's numbers

The doc is written in per-over units; this layer scales per-ball outcome weights.
The translation lives in one place:

* `_runs(delta)` — one run per over is `_RUN_UNIT` (12%) of scoring weight,
  spread across the run outcomes by `_RUN_SPREAD` (boundaries move most, singles
  barely move, dots move the other way). The landing is deliberately about half
  strength — a nominal +1 is worth ~0.55 real runs — because these rules stack,
  and at 1:1 apiece a death Ultra on a Hard pitch with momentum, chemistry and a
  correct read would routinely go for 20+.
* `_wkt(pct)` — percentage lines are read as *relative* nudges to the wicket
  weight, not absolute additions to a probability. Absolute addition would be far
  more violent than intended: two points on a 4% per-ball wicket weight is a 50%
  jump.

Measured against the live engine (4,000 overs per cell, 70-vs-70, Even pitch,
over 9), the resulting per-over means are lower-scoring and more wicket-heavy
than the doc's illustrative bands — because they inherit this engine's
calibration, where a T20 innings loses 6-8 wickets rather than the ~1.5 the
doc's own per-over wicket percentages would produce. The *ordering* the doc
describes is reproduced across all 25 cells.

## What it costs the calibration

The situational layers are not free: several of the doc's advanced mechanics
(fatigue, chemistry, batting momentum) only ever point one way, so the system
does move the par score. Measured over 80 innings per pitch with approaches
picked uniformly at random — the worst case, since a real captain picks on
merit — against the same innings without the layers:

| Pitch | Before | After |
|---|---|---|
| Hard | 209.8 | 231.4 |
| Even | 198.2 | 205.7 |
| Flat | 220.7 | 227.3 |
| Green | 166.8 | 178.8 |
| Dry | 178.2 | 176.2 |
| Dusty | 162.7 | 170.7 |
| Bouncy | 195.4 | 192.2 |

About +5% on average, and the pitches separate more than they did — which is the
pitch layer doing its job. Hard moves most (+10%) because the doc's Hard row is
its most one-sided: two intents gain two runs, one loses one, and the
compensating half of that row ("spinners get -5% wicket") is engine-side and so
not repeated here. `_RUN_UNIT` is the knob if the par score needs pulling back,
though it only accounts for about a third of the shift — halving it recovers
roughly 6 of the 20 runs on Hard.

## Flavour in chat

The over summary names the special combination (or the match-up's one-line
character) — **but not in bot matches**. The 5×5 flavour table is a lookup, so
printing "The Chess Match" after the over is printing both picks, and the bot's
picks are hidden on purpose: a bot whose plans are published is a bot whose mix
can be written down and countered. Between two humans the reveal is symmetric —
each captain gives up exactly what they learn — so it stays on. The switch is the
`_is_bot_match` check in `handlers/cipl_play._render_over_summary`.

## Where things live

| Thing | File |
|---|---|
| Tables, context, combinations | `engine/approach_modifiers.py` |
| Applying the weights to a ball | `engine/ball_outcome.py` (`approach_context` argument) |
| Building the context, carry-overs | `services/cipl_match.py` (`approach_context`, `approach_phase`) |
| The AI captain seeing the same over | `services/bot_tactics.py` (`payoff_matrix`) |
| Chat rendering | `handlers/cipl_play.py` (`_render_over_summary`) |
| Tests | `tests/test_cipl_approach.py` |
