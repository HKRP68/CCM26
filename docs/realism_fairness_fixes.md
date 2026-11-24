# Fairness & Simulation Fixes (Prompt 03)

A pass over the realism engine to remove or correct structural biases that
could make identical cards behave differently, make outcomes overly
predictable, or quietly favour one side. Same feature flag as the rest of the
realism engine (`SIM_REALISM_ENABLED`), same immediate rollback. Every
correction is **behind the flag**: with it off, the engine behaves exactly as
it did before.

The guiding rule, taken from the prompt: *do not assume a hardcoded heuristic
is defective merely because it is hardcoded — measure its effect before
removing it.* Two of the ten items (the 12% cap, the 1.0-multiplier skill
model) turned out to be fine on measurement and were retained with that
evidence recorded.

| # | Concern | Outcome |
|---|---|---|
| 1 | ID-derived hidden skills (pace/spin, aggression, form, death-spec) | gated off in realism; only pace/spin reached the ball model and was already off |
| 2 | Overlapping rating-ratio / gap-steepen / tier systems | one model (`rating_curve`); legacy layers structurally bypassed |
| 3 | Forced outcomes / dramatic-finish steering / score convergence | off in realism (Prompt 2 hooks + scenario bypass), verified |
| 4 | Default toss favours home | **fixed** — seeded coin spin |
| 5 | 12% wicket cap + CIPL consecutive-wicket suppression | cap **evaluated and retained**; throttle **removed** in realism (measured) |
| 6 | Artificial batting-position skill penalty | **removed** in realism; order effects via match state |
| 7 | Pitch-wear inconsistent across innings | **fixed** — match-long wear in `/sim` too |
| 8 | No per-delivery probability diagnostics | **added** — opt-in, off by default |
| 9 | Tactical AI distributions miscalibrated to legacy engine | **fixed** — bot reasons over the realism distribution |
| 10 | Format phase boundaries defined in several places | **centralised** — one source for the realism fractions |

A headline bug surfaced *by* the item-8 diagnostics is described under
[The approach model was dead in live matches](#the-approach-model-was-dead-in-live-matches).

---

## 1. ID-derived hidden skills

The realism ball model reads only the five card fields, so two identical cards
are identical by construction. The one place a player's *identity* leaked into
a delivery was `engine/approach_modifiers.batting_matchup_delta`, whose
fallback hashes a roster id (or name) into a ±6 vs-pace/vs-spin swing. That is
gated off in realism mode (Prompt 2, hardened here with a call-site test).

The generated **aggression, form and death-bowling** attributes
(`engine/sim/player_adapter.py`, seeded by `stable_unit(pid, …)`) feed only
the SimCricketX engine (`/sim` long, the conditions engine), which the realism
model does not drive — so they never reach a realism-LIVE delivery. A test
asserts two identical cards produce identical realism distributions regardless
of any extra derived fields on the dict.

## 2. Single ability model

In realism LIVE mode the entire legacy weight loop — the multiplicative skill
model (`_skill_multiplier` with its rating-ratio, level and gap-steepening
exponents), the per-ball `rating_duel` (gap curve + class tiers), and the
`T20_DUEL_RECAL` recalibration — is replaced wholesale by
`raw_weights = dict(realism_dist)`. `engine/realism/rating_curve.py` is the one
calibrated model. The duel is asserted never to run in realism mode (it would
both double-count and spend RNG).

## 4. The toss no longer favours home

`services/sim_match.py` defaulted an unspecified toss to the home team:

```python
if toss_winner not in teams_by_name:
    toss_winner = home_name        # a permanent free edge to whoever is named first
```

The toss decides who bats on the pitch's `ideal_toss`, so this was a standing
structural advantage. In realism mode it is now a coin spin drawn from the
match RNG — fair, and still reproducible under a seed. Legacy mode keeps the
home default so its behaviour is byte-identical.

## 5. The 12% cap and the consecutive-wicket throttle

**Measured first.** Over ~9,500 realism deliveries:

- The **12% normalised wicket-share cap** binds on ~5% of balls, and the
  pre-cap 95th/99th percentile share sits right at it. The value matches the
  real-world ceiling on sustained per-ball dismissal (~12-13%). It is
  *retained*, now as the named constant `T20_WICKET_SHARE_CAP`, documented as
  evaluated. It sits below the realism distribution's own 0.14 clamp, so the
  two do not double-count — the looser clamp only binds where this cap is
  disabled (free hits, ListA).

- CIPL's **consecutive-wicket throttle** (`{1:0.50, 2:0.22, 3:0.08}`) was
  built to stop the *legacy* engine's unbounded momentum/collapse layers
  stacking a 4-5 wicket over. The realism per-ball wicket is already bounded
  and calibrated, and measured, the throttle does the opposite of its name:

  | | 2+ wicket overs |
  |---|---|
  | throttled (current) | 3.3% |
  | unthrottled (realism now) | 5.8% |
  | Poisson(0.33) | 4.4% |
  | real T20 | ~6-8% |

  Real cricket clusters wickets *more* than a Poisson process (collapses); the
  throttle flattened them to *below* Poisson. So it is **off in realism mode**,
  and the unthrottled distribution produces realistic clustering with no
  runaway (four-wicket overs ~0.1%). Legacy mode keeps the throttle.

## 6. No batting-position skill penalty

`_POS_BATTING_MULT` scales a card's rating by batting position, down to 0.75
for a number 11 — an artificial skill penalty, since a tail-ender is not a
*worse player* than a top-order bat, they bat lower because the order put them
there. In realism mode the rating is passed to the engine **before** that
penalty; batting-order effects are kept, but through match state (wickets in
hand, balls faced, and the win-probability captain's resource model), not by
secretly lowering the card. Verified: a number 1 and a number 11 with the same
card get the same effective rating in realism mode. Legacy keeps the curve.

## 7. Pitch wear is match-long

`/cipl` already used `engine.pitch_state.carry_wear`, which spans innings 1
across 0.0-0.5 and innings 2 across 0.5-1.0 — a T20 pitch is bowled on for 40
overs, and the chase does not get a fresh square. `/sim` instead reset wear to
zero each innings. In realism mode `/sim` now uses the same match-long,
innings-length-keyed model, so wear is consistent across innings and correct
for any format.

## 8. Per-delivery diagnostics

`engine/realism/diagnostics.py`. Turn on with `SIM_REALISM_DIAGNOSTICS=1`.
Off by default and free when off (one env read on the hot path). When on, each
delivery records: the context, the baseline it started from, each modifier
stage as per-outcome multipliers (rating curve, conditions, approach,
downstream gain), the distribution after the realism model, the final
pre-sample distribution, and the outcome that was drawn. Records go to a
bounded in-process ring buffer and are attached to `result["realism"]
["diagnostics"]` for single-delivery inspection. `format_record` renders one:

```
delivery: T20 inn2 over 18  150/3  88 vs 74 on Even (death)
  node=T20/ipl_modern/death/inn2/w3_5 n=264 kind=pseudo
  outcome              Dot   Single   Double    Three     Four      Six   Wicket   Extras
  baseline          0.2590   0.2691   0.0680   0.0050   0.1519   0.1350   0.0712   0.0408
  realism_out       0.1189   0.3187   0.0647   0.0059   0.2345   0.2184   0.0195   0.0194
  final_pre_sample  0.1189   0.3187   0.0647   0.0059   0.2345   0.2184   0.0195   0.0194
  xrating_curve      0.587    1.087    1.262    1.125    2.062    2.623    0.546    0.909
  xconditions        0.960    1.008    1.033    1.016    1.067    1.085    0.656    1.000
  xdownstream_gain   0.856    1.135    0.768    1.085    0.737    0.597    0.803    0.550
  sampled: Single
```

### The approach model was dead in live matches

The diagnostics earned their place immediately. They showed
`final_pre_sample == realism_out` with no approach multiplier on an over where
both captains had made explicit picks. Tracing it: `calculate_outcome` passed
`apply_tactics = (both approaches are None)` to the realism build, which is
**False** for every over with real picks — i.e. every `/cipl` over — so
`engine.realism.approaches` never ran in a live match. The Prompt 2 approach
model was correct in its unit tests (which call `build()` directly) but was
dead code in play. Fixed: the realism approach model now always runs in
realism mode, and the legacy approach layer stays reduced to its allowlist
(predictability + chemistry), so there is no double count. A diagnostics test
asserts an Ultra pick lifts the six multiplier on a live over.

## 9. The tactical AI reasons over the realism distribution

`services/bot_tactics.ball_probabilities` was a compact rating-gap stand-in
fitted to the *legacy* engine (the `EDGE_*` coefficients). In realism mode the
live engine is the realism model, so the bot was optimising against the wrong
probabilities. Now, when realism is live, the bot's per-ball distribution is
the realism engine's own baseline-plus-conditions distribution for the live
context, and the approach shaping uses `engine.realism.approaches` — the same
priced trade-off the over will run. The existing zero-sum solve and
win-probability chase/first-innings value logic are preserved unchanged; only
the distribution they reason over is corrected. Crude check: the bot's per-ball
wicket estimate is now ~5% (the real rate), not the legacy stand-in's ~2%.

## 10. One source for the phase boundaries

The realism phase split (powerplay < 30% < middle < 80% < death) was
duplicated in `engine/realism/context.py` (live phase) and
`tools/build_realism_reference.py` (phase at build time). If those drifted, a
delivery would be filed under one node and looked up under another, silently
reading the wrong distribution. Both now import `engine/realism/phases.py`, the
single source. The legacy over-based boundaries in `engine/format_config` are
unchanged and cross-referenced.

---

## Measured before / after

`/cipl`, realism mode, measured mix. The combined effect of removing the
position penalty (tail-enders keep their card), removing the throttle (more
realistic clustering, +~9% wickets), and switching the approach model on in
live play — all of which shift the level, so the downstream gain and the
rating-curve normaliser were re-measured after these changes.

`python -m tools.compare_realism --n 70 --seed 8080`:

| metric | OLD | REALISM | baseline | real |
|---|---|---|---|---|
| mean 1st-inn total | 150.3 | **179.1** | – | 179.2 |
| dot % | 36.67 | **32.34** | 32.59 | 35.50 |
| single % | 34.78 | 34.40 | 32.29 | 33.00 |
| four % | 10.58 | 11.44 | 13.63 | 11.50 |
| six % | 4.50 | 6.93 | 7.41 | 5.50 |
| wicket % | 5.42 | **5.45** | 5.29 | 5.29 |
| wickets / innings | 6.16 | **6.29** | – | 6.35 |
| powerplay rpo | 7.58 | 8.81 | – | 8.80 |

The dismissal rate and the innings total are both on anchor after
recalibration. The **wickets-per-over clustering** is the headline behavioural
change from removing the throttle:

| 2+ wicket overs | value |
|---|---|
| throttled (before) | 3.3% |
| unthrottled realism (now) | ~6% |
| Poisson(0.33) | 4.4% |
| real T20 | ~6-8% |

Real cricket clusters wickets more than a Poisson process; the throttle had
flattened them below it. Four-wicket overs stay at ~0.1%, so there is no
runaway cascade.

One performance note: fixing the dead approach model (it now runs on every
delivery) made the iterative tilt solver a hot path. It is memoised per
(baseline node, approach pair), which the measurements confirmed takes the
cost from ~34 ms/delivery to ~0.3 ms after a sub-second warm-up — a match is
a handful of distinct keys.

## Configuration

| control | default | meaning |
|---|---|---|
| `SIM_REALISM_DIAGNOSTICS` | off | record per-delivery probability diagnostics |
| `engine/ball_outcome.T20_WICKET_SHARE_CAP` | 0.12 | evaluated real-world per-ball dismissal ceiling |

All other fixes ride the existing `SIM_REALISM_ENABLED` flag and roll back with
it.

## Rollback

`unset SIM_REALISM_ENABLED`. Every correction here is gated on the realism flag
(the toss, the position penalty, the throttle, the pitch-wear model, the AI
distribution source) or is itself opt-in (diagnostics). Legacy behaviour is
byte-identical with the flag off.
