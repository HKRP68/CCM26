# ODI Approach experiment

The ODI experiment is available by default. **CMU_ODI_APPROACH_ENABLED** defaults to **1**. The experiment supports **/letsplay ODI @user**, **/letsplay 50**, **/lpbot ODI app** and **/lpbot 50 chat**. Reply to a player's message when omitting their username. **/lpbot** carries the chosen format through the difficulty prompt and remembers the chat or Mini App preference.

## Any length from 21 to 50 overs

ODI rules apply to any innings of **21 to 50 overs**, not only the full fifty. The over count is the only switch: **services/match_formats.py** holds the range (**CUSTOM_OVERS_MIN** 1, **MEDIUM_OVERS_MIN** 21, **ODI_OVERS** and **CUSTOM_OVERS_MAX** 50) and **ball_format_for_overs** maps a count to **ODI** or **T20**. A command never decides the engine from the spelling of its argument, so **/lp 30**, **/lp t30** and **/lp 30overs** all start the same 30-over ODI, and 1 to 20 overs stay on the short-format realism engine exactly as before.

Entry points: **/letsplay**, **/lp**, **/lpbot**, **/lpmulti**, and every Challenge League command — **/c&lt;league&gt; 50 @user** or a reply. A league command resolves its opponent by reply, **@username**, numeric Telegram id or a tapped no-username mention. A league with a fixed non-T20 format does not take a custom length, and tournament, CL Tour and The Hundred matches always play their full length. **/sim** keeps its own 20-over custom cap and its **ODI** keyword.

Everything the model reads scales from the innings length rather than a constant. The phase boundaries are the ODI split as fractions, **ODI_POWERPLAY_END** 0.20 and **ODI_DEATH_START** 0.80 in **engine/realism/phases.py**; at 300 balls they land on ball 60 and ball 240, the split the measured reference filed its deliveries under, so a full ODI is unchanged. **services/odi_approach.py** reads **innings_overs**, **innings_balls** and **phase_windows** from the state for the delivery context, pitch wear, required rate, pressure input, par curve, chase forecast, bowling horizon and death reserve. The per-bowler quota is already a fifth of the innings — ten overs at fifty, six at thirty — and part-timers stay capped at two. **valid_xi** takes the length and asks for that much legal bowling capacity.

A paired-seed regression compared 24 innings of 50-over ODI play before and after the change: every over's bowler, phase, running score and the full batting and bowling figures were identical. Across 30 paired matches at each of 21, 30, 35, 45 and 50 overs, every innings completed, no bowler exceeded quota or bowled consecutive overs, and first-innings means ran 130.4, 183.1, 211.2, 254.8 and 278.2 — 6.21 down to 5.56 runs an over, the shorter games scoring faster per over as they do in the real game.

**matches.ball_format** is written as **ODI** for every such match, including Challenge League ones, so the career split below holds after the live state is gone.

Set **CMU_ODI_APPROACH_ENABLED=0** to pause new ODI matches. Existing ODI matches use their saved tuning, engine version, reference version and reference SHA-256. Keep the reference version on disk while any save uses it. If a saved reference is unavailable, restore the pinned file and use **/rcl** to continue. A future tuning or data release should use a new version and retain the old settings reader for unfinished matches.

## The drinks interval

Halfway through an ODI innings — after over 25 of fifty, or **ceil(overs / 2) - 1** rounded to the innings length — play stops and both captains commit to a plan for the rest of it. The batting side chooses **Consolidate**, **Build** or **Accelerate**; the bowling side chooses **Squeeze**, **Rotate** or **Strike**. The bowling captain is asked first and their pick is held back, exactly as the per-over flow does it, so neither captain reads the other; both are revealed together with the match-up named. The pair then applies a bounded multiplier to runs and to dismissal risk on every remaining delivery of the innings, through **services/drinks_break.py** and one factor in **engine/realism/odi.py**'s approach distribution.

Three properties are held by **tests/test_drinks_break.py** rather than claimed in a comment. The grid is **exactly mean-neutral on both axes**: the tables are written as cricket, and read literally they added about 7% to dismissal risk across their nine cells, which would have pulled every ODI total down the moment the interval existed and moved the innings off the anchor the measured reference was calibrated to. Simulation agrees with the correction — across the nine choices the mean is +0.4 runs and +0.00 wickets against no interval at all, with a spread of about 8 runs and 0.7 wickets by choice. **No plan on either side is the best answer to everything**, at any price of a wicket from 0.2 to 1.0, which is the same non-dominance property the per-over frontier is held to; each bowling plan answers exactly one batting plan, so squeeze beats acceleration, rotate beats building and strike beats blocking. And **SESSION_STRENGTH** is the single dial for how heavy the interval is, with the clamps as rails no cell reaches — which is what lets the normalisation be exact rather than approximate.

The AI captain calls it from the score, the wickets standing and the chase. An unanswered interval is called the way the AI would call it: nobody is fined or forfeited for a bonus decision. An innings with no recorded plan behaves exactly as it did before the interval existed, so matches already in flight, every T20 and The Hundred are untouched. The interval is asked in the chat on both surfaces, so a Mini App match does not wait for an app release to use it; the Arena shows the plans in force.

## Rain and revised targets

**services/rain_interruption.py** lets weather shorten an ODI-rules match. Short formats are untouched, as are bot practice and Super Overs. The arithmetic is not new: **engine/dls.py** already holds the Duckworth-Lewis resource curve and **engine/sim/rain_dls.py** already turns it into targets for **/sim ODI**, so the interactive match and the auto-sim price a stoppage identically.

A first-innings stoppage shortens both innings and the target is the resource-scaled par set at the break; a second-innings stoppage shortens the chase and revises the target by **engine.dls.revised_target**. Each stoppage's resource slice is banked when it happens, so two showers in one innings add up rather than the second overwriting the first. The innings length lives in **state["overs"]**, which is what **cipl_match.total_balls** reads, so a stoppage is one write and the innings end, the chase maths, the phases, the par curve, the bowling quota and every surface follow from it; **state["scheduled_overs"]** keeps the length the match was scheduled for.

Rain never washes a match out: each side keeps at least **MIN_OVERS_PER_SIDE**, nothing is cut below what is already bowled, and a shower that cannot fit costs nothing. The powerplay and the last over are left alone. At most two stoppages per innings. The roll is drawn from a match-local stream derived from the match seed, so a replay rains in the same places, and both the roll and **apply** clamp the loss — the public door was writing a negative innings length before it did.

The per-over chances are deliberately tiny because they compound over about ninety eligible overs: a first draft at 0.016 for a damp sky interrupted 45 of 60 matches. Measured over 120 matches per weather at a neutral ground, the rate is 0% under a clear, hot or windy sky, 1.7% in humidity, 4.2% under cloud and 5.8% after earlier rain; at the wettest sky and the wettest ground it is 15.8%. Over 200 wet matches none breached the over floor, the bowling quota or the innings length, and every revised target was sane. **CMU_RAIN_ENABLED=0** pauses it.

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

## Separate ODI career stats

An innings of 21 overs or more is recorded in **odi_player_innings** and never in **player_game_stats**, so a long-format hundred cannot flatter a short-format strike rate and a T20 spell cannot flatter an ODI economy. The table is one row per match, innings, discipline and player holding the raw live-state figures as JSON, which keeps the record auditable: nothing printed is a counter that can drift from the innings it came from.

**services/odi_stats_service.py** is the read side. **fold_batting** and **fold_bowling** sum the per-innings blobs; **career** returns one player's record for one owner, or for every owner when **user_id** is omitted; **squad_careers** folds a whole roster in one query. The derived figures — batting and bowling average, strike rate, economy, highest score with its not-out star, best bowling, overs — return **None** rather than dividing by zero, and **record_block** renders the two-column layout **/stats** uses. The module never writes.

**/odistats &lt;player&gt;** shows the caller's own card's ODI career; **/odistats &lt;player&gt; global** sums every owner's innings for that card, as **/gstats** does for the short formats. Every edition of a re-released card folds into one record, and the command is registered as **/odistats**, **/odistat** and **/statsodi**. Unranked practice and bot matches earn no ODI innings, which is the rule the short formats already follow.

The record is also on the cards a player already opens, which is where most readers will find it: **/stats** carries a **🥇 ODI record** button that flips the same message to the long-format record and back, and **/gstats** carries a **🥇 ODI** view beside its **👥 Owners** one, summed across every owner and following whichever edition is paged to. Neither ODI command is in the slash menus — both player menus were already at Telegram's 100-command ceiling, and an over-long scope is rejected whole, so publishing them would have cost every player their menu.

**/odirecords** (**/odilb**) holds the leaderboards: most runs, most wickets, highest score, best bowling, batting average, economy and batting strike rate, seven boards behind one row of buttons. The rate boards carry a qualifying bar so one good night cannot outrank a career, and the folded table is cached for a minute. The Hall of Fame gains **Most ODI runs by one card** and **Most ODI wickets by one card**: they are appended rather than merged into the short-format boards, because 300 ODI runs and 300 T20 runs are not the same achievement.

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

The neutral holdout simulation missed the provisional limits for powerplay run rate, death-over dot and single shares, and mean chase score. Its first innings averaged 277.4 versus 297.6 in the holdout. The second innings averaged 249.5 versus 224.4. The simulated powerplay ran at 4.96 runs per over, 0.63 below holdout. Death-over dot share differed by 5.59 percentage points. The 2019 powerplay averaged 4.89 runs per over and the 2019 chase averaged 246.4, both closer to the neutral simulation than to the holdout. Death overs also shifted toward more dots and fewer singles in 2023. These population differences help explain the direction of the errors. The holdout gate failed. Match entry was enabled for experimental play on request, and the reference remains under the experiment manifest.

## Verification commands

~~~sh
python -m pytest -q \
  tests/test_odi_approach.py tests/test_long_format_matches.py \
  tests/test_custom_overs_and_roster_full.py \
  tests/test_challenge_league_commands.py tests/test_dm_only_commands.py \
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

In isolated comparisons, eleven failing cases from the existing suite also failed at the base commit. These included auction and menu coverage, card rendering, the league XI picker, archived coaching, match handover, saved batting order and streak settlement.

The full repository run collected the 8,560 existing tests while integration was in progress. It reported 8,396 passed, 110 failed, 52 errors and two skipped. The setup errors included missing model imports in the World T20 and undo fixtures. Isolated focused reruns used the final implementation and included the new ODI tests. Examples of the full-run outcomes were reproduced separately at the base commit. The full regression gate also failed. The runner was stopped after reporting results because post-test cleanup remained active.
