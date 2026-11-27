# ODI Approach experiment

The release gate is closed. **CMU_ODI_APPROACH_ENABLED** defaults to **0**. The experiment supports **/letsplay ODI @user**, **/letsplay 50**, **/lpbot ODI app** and **/lpbot 50 chat**. Reply to a player's message when omitting their username. **/lpbot** carries the chosen format through the difficulty prompt and remembers the chat or Mini App preference.

Turning off the flag closes match entry. Existing ODI matches use their saved tuning, engine version, reference version and reference SHA-256. Keep the reference version on disk while any save uses it. If a saved reference is unavailable, restore the pinned file and use **/rcl** to continue. A future tuning or data release should use a new version and retain the old settings reader for unfinished matches.

## Data and attribution

The uploaded **icc_mens_cricket_world_cup_male_json.zip** contains matches from 2003 through 2023. The importer filters on international men's ODI metadata and selects the 2019 and 2023 tournaments. The manifest records the archive hash, exclusions and coverage.

| Population | Matches | Physical deliveries | Legal balls |
| --- | ---: | ---: | ---: |
| 2019 fit | 33 | 18,722 | 18,230 |
| 2023 holdout | 37 | 20,186 | 19,641 |

The build excludes four adjusted or reduced matches, one incomplete match, 190 out-of-window files and two Super Over innings. Ordinary all-outs and completed chases remain in the sample. The archive omits Afghanistan men's matches under the source's withholding policy. Different grounds, teams and eras limit the 2019-to-2023 comparison.

The second input was **ppai22/cwc-2019-ball-by-ball-dataset** at **09b89840d639d9ca676eebb7a59467aaf4808cef**. The audit examined 24,713 ICC commentary rows across 45 files and found 32 unique matches with the structured 2019 sample. The CSV fields are ball notation, run marker and commentary. Their missing match metadata and ambiguous events prevent verified additions to the measured input. The audit preserves marker frequencies and identifies the positions and markers skipped during fingerprint matching. [ICC commentary source](https://github.com/ppai22/cwc-2019-ball-by-ball-dataset) · [Cross-check](../data/simulation_reference/odi_wc_experiment/odi_2019_commentary_crosscheck.json)

The eight outcome categories describe physical deliveries. Extras take precedence, then dismissals, then runs off the bat. The importer separately tracks legal balls, actual runs, dismissals, balls faced and scoring boundaries. Wickets and types of runs on extras are captured in the conditional extras table. Wides and no-balls leave the legal-ball count unchanged. The training report includes all measured hierarchy nodes, five-over windows, batter balls-faced bands, innings distributions, rates and counts. It tracks the result for each chase observed in a required-rate band. A chase can appear in several bands. Pace and spin are model features based on CCM26 card styles. The reference lacks reliable bowling-style metadata.

Published probability nodes have integer observed counts. The runtime smooths phase, innings, wickets and chase counts toward their parent with weights of 60, 120, 220 and 1,450, respectively. Sparse nodes fall back to their nearest populated ancestor. The prior at **v1/odi_baselines.json** remains available for legacy diagnostics. The experiment uses its own reference version and has 37 observed nodes with **intl_odi_modern** as the training competition key. [Training report](../data/simulation_reference/odi_wc_experiment/odi_training_report.json) · [Reference manifest](../data/simulation_reference/odi_wc_experiment/manifest.json)

Contains information from Cricsheet, made available under the Open Data Commons Attribution License 1.0 specified in the supplied archive. The raw datasets stay outside the repository. [Cricsheet](https://cricsheet.org/) · [Data license](../data/simulation_reference/odi_wc_experiment/CRICSHEET-LICENSE.txt)

SimCricketX source was reviewed at **111a2f6cfddef0d13db6e71a3d3230ac7cc15202**, including its format catalog and settings, List A ground matrices, outcome model, pressure engine, bowler manager, phase-based selection and List A regression tests. Its configured phase rates were 5.8, 4.8 and 8.5, with a Hard par curve ending at 290. Green, Dry, Hard, Flat and Dead targets were 220, 230, 285, 320 and 340. CCM26 already contains the List A rules and pressure engine. The ODI adapter explicitly reuses those mechanics. Its outcome counts come exclusively from Cricsheet. CCM26 adds recovery-aware workload, a feasibility bound, a short planning horizon and an independent probability model. [SimCricketX snapshot](https://github.com/Manishrdy/SimCricketX/tree/111a2f6cfddef0d13db6e71a3d3230ac7cc15202) · [Apache 2.0 license](licenses/SimCricketX-Apache-2.0.txt)

## Runtime and migration

The ball loop and AI use the same ODI context and per-delivery weights. The ODI identity uses 50 overs, with phases at overs 1–10, 11–40 and 41–50. The legal allocation checks quotas after every potential choice and accounts for the previous bowler. Specialists can bowl ten overs and part-timers can bowl two. An infeasible state pauses for recovery. ODI ties finish level, and Impact Player is unavailable.

The baseline and its ODI rating curve own rating, pitch, wear and new-ball effects. The ODI context supplies confidence, partnership chemistry, momentum and the List A pressure response once. The existing trait hook applies the saved card traits once. ODI fatigue applies bounded weights from completed overs, with pace and spin recovering at different rates. The approach frontier reallocates probability between dots, rotation and boundaries, then applies the existing counter topology with ODI-specific coefficients and a five-over repeat grace. Those tactical coefficients are game design parameters. Cricsheet lacks Approach labels.

The chase forecast estimates remaining runs from phase rates and wickets available. The AI applies the same forecast to approximate the over's value using the delivery probabilities. It also uses role-specific priors and previously completed overs. Projection and win-chance displays remain advisory. The over loop samples each delivery from a match-local stream and adjudicates extras before advancing its legal-ball count. Short-format scoring floors, outcome steering and scripted scenarios are outside this path.

Database startup adds nullable **matches.ball_format** and creates **odi_player_innings** via the existing migration and table creation routines. Eligible competitive ODI innings are written idempotently to the ODI table. Existing matches retain their format conventions. Practice remains unranked. The scorecard archive retains the ODI label and totals after live-state cleanup.

## Reproduce

The development build used the match ID hash modulo five, with fold zero held out from the 2019 fit. The remaining 27 matches built the fold reference and six matches were scored separately. The final experiment used all 33 eligible 2019 matches. The tuning was frozen before 2023 was scored.

~~~sh
python -m tools.build_realism_reference cricsheet \
  --zip "$ODI_CRICSHEET_ZIP" --format ODI --min-year 2019 --max-year 2023 \
  --holdout-from 2023 --over-stats \
  --icc-commentary "$ICC_2019_CLONE/Commentary Dump" \
  --icc-commit 09b89840d639d9ca676eebb7a59467aaf4808cef \
  --out data/simulation_reference/odi_wc_experiment

python -m tools.calibrate_odi_approach --n 1000 --legacy-n 120 --seed 261923 --holdout \
  --out data/simulation_reference/odi_wc_experiment/odi_calibration_report.json

python -m tools.measure_odi_approach \
  --out data/simulation_reference/odi_wc_experiment/odi_interactive_measurements.json
~~~

For the development build, use **--max-year 2019 --development-fold 0** and run the calibration tool with **--development** in place of **--holdout**. The raw 2023 deliverables stay outside the fit. A subsequent release following a failed holdout requires fresh independent data.

The holdout report contains counts and phase distributions for 2023. The calibration report records seeds, input hashes, outcomes, run rates, odds comparisons, wickets, rarity, quotas and match completion. Per-delivery intervals describe outcome sampling variability. Match-level standard errors show the spread in innings means. [Holdout report](../data/simulation_reference/odi_wc_experiment/odi_holdout_report.json) · [Calibration report](../data/simulation_reference/odi_wc_experiment/odi_calibration_report.json)

Interactive measurements use the real Telegram turn handlers with a mock transport and local SQLite. In a full 100-over match the transport received 199 sends, 300 edits and 198 deletions in either play mode, excluding setup, milestone and final-result posts. Each surface wrote 700 checkpoints and finished with about 400 KiB of persisted state. The seeded simulation report records the time spent in the per-over engine. End-to-end Telegram and production database latency remains unmeasured. [Interactive measurements](../data/simulation_reference/odi_wc_experiment/odi_interactive_measurements.json)

The tactics sweep found different best replies for all five bowling plans. Ultra Attack was the best response in 118 of 135 sampled batting payoffs. The frequency is a balance concern for future validation even though other batting intents won in some contexts.

## Release gate

All six ODI arms completed 1,000 matches each. The legacy arms completed 120 matches per format using Balanced plans and the first legal bowler. The harness recorded zero unfinished innings, bowling-legality breaches or scorecard accounting errors across all eight arms.

| ODI arm | Matches | First innings mean | Second innings mean |
| --- | ---: | ---: | ---: |
| Hard, Balanced | 1,000 | 277.4 | 249.5 |
| Hard, adaptive | 1,000 | 282.4 | 248.2 |
| Hard, +14 rating side | 1,000 | 285.1 | 217.4 |
| Green | 1,000 | 246.0 | 210.9 |
| Dead | 1,000 | 335.5 | 303.4 |
| Cycling tactics | 1,000 | 277.6 | 247.6 |

The +14 rating side won 94.1% of its matches. The Hard adaptive arm produced five 400-plus first innings. The ODI engine's p95 over simulation times ranged from 2.7 to 10.0 ms during the shared-workspace run. These timings measure the over engine separately from AI decisions, network transport and persistence.

The neutral holdout simulation missed the provisional limits for powerplay run rate, death-over dot and single shares, and mean chase score. Its first innings averaged 277.4 versus 297.6 in the holdout. The second innings averaged 249.5 versus 224.4. The simulated powerplay ran at 4.96 runs per over, 0.63 below holdout. Death-over dot share differed by 5.59 percentage points. The 2019 powerplay averaged 4.89 runs per over and the 2019 chase averaged 246.4, both closer to the neutral simulation than to the holdout. Death overs also shifted toward more dots and fewer singles in 2023. These population differences help explain the direction of the errors. The experiment remains disabled by default, with no release manifest.

## Verification commands

~~~sh
python -m pytest -q \
  tests/test_odi_approach.py tests/test_custom_overs_and_roster_full.py \
  tests/test_cipl_approach.py tests/test_approach_miniapp_play.py \
  tests/test_bot_tactics.py tests/test_realism_ai_parity.py \
  tests/test_realism_reference.py tests/test_realism_deterministic.py \
  tests/test_bot_match_unranked.py tests/test_bot_match_flow.py \
  tests/test_letsplay_toss_first.py tests/test_bot_difficulty.py \
  tests/test_final_over_drama.py tests/test_database_startup.py \
  tests/test_scorecard_fidelity.py tests/test_score_projection.py

python -m pytest -q

node --check static/cricket/app.js
node --check static/cricket/approach.js
~~~

The focused command passed 356 tests. A paired-seed regression compared 40 T20 and 40 Hundred matches with base commit **8255f792a0e395b7a569fbdca83944dd204de564**. Both comparisons matched across innings totals, batting and bowling statistics, and the over logs. The checks used seeds 0–39 with Balanced approaches.

The separate conditions-simulator and recovery regression run passed 111 tests and two subtests.

~~~sh
python -m pytest -q tests/test_sim_long.py tests/test_sim_handler.py tests/test_sim_match.py \
  tests/test_over_limit_resume.py tests/test_impact_xi_rules.py tests/test_cricket_realism.py
~~~

In isolated comparisons, ten failing cases from the existing suite also failed at the base commit. These included auction and menu coverage, card rendering, the league XI picker, archived coaching, match handover and streak settlement.
