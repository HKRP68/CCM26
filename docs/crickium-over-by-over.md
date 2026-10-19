# CK versus CCM26: over-by-over simulation

## Scope and conclusion

This compares Crickium's `engines/play_runtime.py`, `strategy_engine.py` and
`tactics_engine.py` (the `/play` family), with its separate Mini App tactical
engine and AI as supporting references, against CCM26's `services/cipl_match.py`,
`engine/ball_outcome.py`, `engine/approach_modifiers.py`, `services/bot_tactics.py`
and `services/bot_captain.py` (`/letsplay`, `/cipl`, `/lpbot`, `/ciplbot`).

Both engines resolve individual deliveries under the plans selected for an
over. They do not simply draw an over's total from a random range. CK is not
demonstrably better overall: it has a useful per-batter confidence signal, while
CCM26 already has a broader set of contextual interactions and an AI solver
that models all 25 approach combinations.

| Area | CK's over-by-over engine | CCM26 |
|---|---|---|
| Batting/bowling plans | Five mindsets, five underlying bowling shapes | Five batting and five bowling approaches, contextual 5×5 interactions |
| Named spin plans | Ten delivery names alias the five tactical shapes | Bowling style already affects wickets, pitch fit, handedness and contextual approach rules; ball-by-ball games also have named deliveries |
| Ratings and settling | Level-gap transfers, balls-faced adaptation, confidence | Rating duel, absolute quality, new-batter vulnerability, runs/balls-faced settling |
| Recent personal confidence | Changes after each dot, run or boundary | Added by this change; previously only score-based settling, team momentum and boundary streaks |
| Pitch and phase | Pitch/phase tables, equivalent T20 phases in short Mini App games | Pitch/phase tables, pitch wear, format-aware phases and environmental modifiers |
| Momentum and repeated plans | Confidence and boundary streaks; rarity penalties after big overs | Team momentum/pressure, per-batter streaks, repetition counters, mind games, partnership and bowler fatigue |
| Wicket clusters | Fourth wicket in an over becomes impossible | Strong bounded damping; rare fourth wickets remain possible |
| Scoring after a big start | Abrupt reductions at 8/12/15/18 runs in the over | Existing bounded score/contest layers; this change adds no per-over score cap |
| Chasing | Score-environment and RPO ceilings; standard `_apply_match_state` is a no-op, separate Mini App pressure layer | Required-rate pressure, wickets/resources, pitch-specific baselines and calibrated closing-ball chase controller |
| AI | Candidate outcome evaluation and difficulty profiles | Full over run/wicket distributions, resource valuation, zero-sum mixed strategies, opponent memory and captain personas |
| Extras/free hits | Present; standard runtime resets its free-hit flag on any result other than a no-ball | Wides preserve free hits; legal run-outs remain possible |

## Direct probability probes

Using CK's actual `resolve_weights`, on an Even pitch in over 10, Variation
against Neutral, equal level 75, confidence 60 and 20 balls faced:

| Runs already in this over | Expected runs next delivery | Four-or-six probability |
|---:|---:|---:|
| 0 or 7 | 1.449 | 9.28% |
| 8 or 11 | 1.066 | 5.18% |
| 12 | 0.835 | 2.83% |
| 15 | 0.686 | 1.41% |
| 18 | 0.556 | 0.28% |

These are conditional probabilities for one fixed scenario, not measured match
averages. They show why CK's overs can look more tightly controlled: scoring is
actively suppressed after a successful start. It does not establish realism.

A separate CK probe on Green, over 4, Swinging against Ultra Aggressive, equal
level 75, confidence 20 and two balls faced gives next-wicket chances of 5.595%,
4.580%, 1.667% and **0%** after zero, one, two and three wickets in the over.
CCM26's existing cluster guard keeps its wicket weight positive after three.

The named spin deliveries do not represent ten additional independent
probability models in CK: its `SPINNER_TACTIC_ARCHETYPE` maps them onto the same
five bowling shapes. There is no reason to copy the labels as a physics upgrade.

## Implemented: per-batter recent confidence

`engine/batter_confidence.py` adds a small, reversible execution signal to the
actual over loop and to the AI's projected over. Each batter starts at 50. A
dot lowers confidence; successful rotation and boundaries lift it. The update
decays old events, so it can fall during a dry spell even after a long innings.
It changes no player rating, pitch factor or permanent settling bonus.

The effect ramps over the first six balls faced and is bounded to a 2.4%
boundary-weight nudge before probability conservation. Non-extra mass is
preserved; batting confidence cannot change the weight of Extras. The model
does not consume another random draw. A new batter starts independently,
confidence survives JSON saves and mid-over pauses, and the chase starts with
fresh confidence while first-innings values remain archived.

The AI uses the same signal and includes it in its cache key. The Mini App
batter cards show **In rhythm**, **Steady** or **Tied down**. These labels reveal
public performance state, with no opponent plan attached.

This is an original bounded adaptation of the useful concept, not a copy of
CK's accumulated score bonus or its hard probability caps.

## Calibration and remaining limitations

The same existing harness ran **350 matches per pitch on all seven pitches**,
before and after the change (4,900 matches total, seed 12345, Balanced plans).

| Pitch | First-innings median before | After |
|---|---:|---:|
| Flat | 234 | 233 |
| Hard | 222 | 224 |
| Even | 199 | 194 |
| Bouncy | 191 | 195 |
| Dry | 187 | 191 |
| Green | 175 | 177 |
| Dusty | 164 | 164 |

All seven stay inside the harness's existing par tolerances. Global all-out
sub-100 frequency is 0.49% before / 0.41% after (limit 2%); 270+ frequency is
0.10% / 0.12% (limit 0.3%); one-sided matches are 10.3% / 9.5% (limit 15%).

**The full calibration harness does not pass cleanly.** Before the change,
Bouncy, Dry and Green each fail one chase-band check. Afterward Flat, Bouncy,
Dry, Green and Dusty have chase-band misses; Dry also has a toss-balance miss
(61% batting-first wins against a 50±8% tolerance). The changed number of misses
is not proof of a regression or proof of noise. A small probability change
reshuffles a long seeded sweep, so a separate per-match seeded ablation is
used to assess the change's effect. The chase-band targets need further
calibration; this work does not silently relax them or claim every balance
check passed.

The follow-up ablation uses 100 match pairs on each pitch (1,400 matches),
resetting the seed for each pair and disabling only the confidence multiplier
for the control. Mean first-innings changes were Flat +2.03, Hard +0.35, Even
+0.45, Bouncy +0.40, Dry −0.30, Green +1.72 and Dusty +1.30 runs. Each pitch's
approximate paired 95% interval includes zero. Chase-win changes ranged from
−8 to +2 percentage points, also with intervals spanning zero; this sample is
too small to rule out modest chase effects. It does not explain away the
remaining balance warnings or establish improved realism.

The existing realism audit also ran 150 matches before and after (300 innings
per run, mixed approaches). Afterward: fours 10.76% of legal balls, sixes 4.78%,
wickets 5.24%, pure maidens 0.23% of full overs. Remaining issues include
centuries at 0.130 per innings versus the audit's approximate 0.035 reference,
and run-outs at 12.34% of dismissals versus approximately 9%. The audit's median
146 combines first innings and chases under mixed plans; it is not comparable
to the Balanced first-innings par of 194 above.

These results support feature compatibility and bounded changes, not a claim
that one engine has been proved more realistic than the other.

## Regression checks

469 selected Python tests passed, with one pre-existing opt-in slow chase-grid
test skipped. Fifteen tests specifically cover recent confidence, per-batter
separation, probability conservation, exact no-op for neutral/old saves,
in-over updates, extras, wicket pauses, JSON restore, innings reset, The Hundred,
AI projection/cache behaviour and the nonzero fourth-wicket probability.
An offline browser check passed six checks for independent batter labels,
unchanged score text, legacy/missing batter handling and mobile overflow.

```
python -m pytest tests/test_over_confidence.py tests/test_bot_tactics.py \
  tests/test_cipl_approach.py tests/test_cricket_realism.py \
  tests/test_bot_captain.py tests/test_pitch_state_and_momentum.py \
  tests/test_chase_model.py tests/test_chase_chance.py -q
python -m pytest tests/test_approach_miniapp_play.py \
  tests/test_pitch_calibration.py tests/test_sim_engine.py \
  tests/test_shot_matchup.py -q
python -m tools.pitch_calibration --n 350
python -m tools.realism_audit --pitch Even --n 150
```

The asyncio match-flow tests need local socket access for their event-loop
wakeups. Changes are in source; the running bot has not been restarted or
deployed.
