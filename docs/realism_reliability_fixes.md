# Cricket Mechanics & Reliability (Prompt 04)

All fixes sit behind the realism-first feature flag (`SIM_REALISM_ENABLED`).
With the flag off the engine is byte-identical to before — the original engine
stays as an immediate rollback. No player-database field was added, removed or
changed; the simulation still consumes only Name, Batting style, Batting
rating, Bowling style, Bowling rating and Overall rating.

| # | Concern | Fix | Status |
|---|---------|-----|--------|
| 1 | Delivery accounting | Byes tracked as their own extra in realism; innings total reconciles to `Σ batter runs + extras_total` | Done |
| 2 | Bowler identity | Quota / fatigue / last-bowler lock keyed on a stable id, not the name | Done |
| 3 | Deterministic simulation | One seeded RNG per match; seed + engine version kept as metadata | Done |
| 4 | Individual innings realism | Early-innings vulnerability, settling, milestone risk and acceleration — all from live match state, mean-neutral | Done |
| 5 | Fielding realism | Drops / misfields / run-outs scaled by a single team-level `fielding_quality`, no per-fielder attribute | Done |
| 6–7 | Tactical-AI parity & fairness reports | — | Pending |

## Fix 1 — Delivery accounting

**Before.** Extras were a three-key dict (`wides`, `noballs`, `legbyes`); byes
were folded into leg-byes, and the runs a batter did not strike (byes off a
wide, byes down the leg side) were added to the team total but not always to the
extras line. An innings total could therefore fail to equal
`Σ batter runs + extras` — e.g. 148 all-told against a 147 reconciliation.

**After (realism).** Byes are a tracked extra of their own, a wide carries its
penalty *plus* any byes run off it, and a no-ball's byes are attributed
correctly. The invariant now holds every innings:

```
innings.runs == Σ batter.runs + extras_total
extras_total == wides + noballs + legbyes + byes
```

Verified green across both innings of six seeded matches
(`DeliveryAccountingTests.test_realism_innings_reconciles_and_tracks_byes`).
Legacy keeps the historical three-key dict and its historical accounting — the
very under-count realism corrects — so legacy mode asserts *shape*, not
reconciliation (`test_legacy_keeps_historical_three_key_extras`).

## Fix 2 — Bowler identity

**Before.** `BowlerManager` keyed quota, fatigue and the no-consecutive lock on
the bowler's **name**. Two players in one XI who share a name shared a single
quota counter (one could in effect bowl eight overs) and a single fatigue tally.

**After.** A static `_key(bowler)` returns a stable id —
`roster_id → player_id → id → name → id(obj)` — and all per-bowler state is
keyed on it. A `_resolve()` helper plus a `name → key` map keep the legacy
string API working for uniquely-named line-ups (a colliding name falls back to
the name, and the hot paths now pass the dict, which is never ambiguous).
`quota_summary` was also re-pointed at the id so its rows report real counts.

Verified: two `Khan`s with `roster_id` 1 and 2 keep separate quotas and
separate consecutive-over locks; the legacy name API still resolves unique
names (`BowlerIdentityTests`).

## Fix 3 — Deterministic simulation

**Goal.** A dedicated seeded RNG per match; fielding, extras, wicket types and
every other random outcome on one reproducible stream; seed + engine version
preserved for debugging; no global random-state interference between concurrent
matches.

**Design.**

* `engine/sim/rng.py` — `SimRng` (already SHA-256-seeded for cross-process
  stability) gained a `random.choices`-compatible `choices()`, making it a
  drop-in for the `random`-module subset the engine draws from. An
  `ENGINE_VERSION` marker is stored with each match's seed.
* `engine/ball_outcome.calculate_outcome(..., rng=None)` — the single delivery
  funnel now takes an optional `rng`. All 11 of its draws route through it;
  `rng=None` (every legacy caller) falls back to the global `random` module, so
  legacy is byte-identical.
* `services/sim_match.py` — in realism mode `simulate_match(..., seed=None)`
  builds one `SimRng` and shares it across the toss, both innings, the ball
  loop and the fielding picks. The chosen seed and `ENGINE_VERSION` are returned
  as `rng_seed` / `engine_version` (both `None` in legacy mode).
* `services/cipl_match.py` — the match seed is persisted in the (JSON-serialised)
  state; each delivery derives an independent stream `SimRng(seed).derive(
  innings, tick)` off a monotonic per-delivery counter (so a re-bowled
  wide/no-ball never reuses a stream). Old states self-heal a seed on first use.

**Verified** (`tests/test_realism_deterministic.py`):

* same seed replays a match identically;
* different seeds diverge;
* churning 5 000 global-`random` draws between two seeded runs leaves the result
  unchanged — i.e. a concurrent match cannot perturb this one;
* the auto-seed is recorded and replays identically;
* legacy: the seed is ignored with the flag off, and `rng_seed` /
  `engine_version` are `None`.

The isolation test (global churn → identical result) also confirms that in
realism-live mode the funnel is the sole source of per-delivery randomness.

## Fix 4 — Individual innings realism

A batter's innings is shaped **only** by live match state — balls faced and
runs scored this innings — never by a stored or hidden player attribute. Two
cards with the same ratings on the same score and ball count are treated
identically. Each layer is **mean-neutral by construction** (`_solve_neutral_band`
pins the share-weighted mean of every column to 1.0), so shaping an individual
innings never moves the team total off its measured anchor.

* **Early-innings vulnerability & settling** — `conditions.wicket_pressure_multipliers`,
  keyed on balls faced: a new batter is more likely to be dismissed and scores
  slower; a set batter correspondingly less so. The curve is mean-neutral over
  the measured balls-faced band shares (the 7+-down excess is carried as a
  separate, separately-neutralised tail factor).
* **Milestone risk (the century cap)** — `conditions.milestone_multipliers`,
  keyed on runs scored. **Wicket-only**: it raises dismissal risk as a batter
  climbs (1.0 → 2.20 across the bands) but leaves every scoring row at 1.0, so
  there is no scoring→higher-band→more-scoring feedback loop. This is what stops
  a set batter running away to a hundred without touching the team total.
* **Acceleration & strike rotation** — carried by the settling curve (keyed on
  balls faced, a quantity nothing in the model can inflate) and by the phase
  baseline, which already encodes the real single/double/boundary split per
  phase. No extra layer or attribute is needed.

Invariants verified in `tests/test_realism_individual_fielding.py`: new batter
more vulnerable than a set one; settling share-weighted wicket mean ≈ 1.0;
milestone risk monotone in runs, scoring rows untouched, share-weighted wicket
mean = 1.0 (±0.005).

**Measured distribution** (`compare_realism --n 60 --pitch Even`, realism arm,
120 innings):

| metric | realism | reference |
|--------|--------:|----------:|
| 50s / innings | 1.09 | 0.75 (prior) |
| 100s / innings | 0.083 | 0.035 (prior) |
| median individual score | 12.0 | — |
| wickets / innings | 6.29 | 6.35 (measured) |

The mechanism is in place and mean-neutral (wickets/innings sits on its anchor,
confirming the team total is undisturbed). The century **conversion** still runs
above the real reference in this high-scoring test configuration (strong XIs,
Even pitch); because the milestone family is mean-neutral, tightening it would
curb centuries without moving the total, but that recalibration needs a
large-n run (n ≥ 300 for stable century estimates) and is flagged for a
dedicated calibration pass rather than tuned here on a noisy n=60 sample.

## Fix 5 — Fielding realism

Drops, misfields and run-outs are a **team-level** effect. The only fielding
input to `calculate_outcome` is the scalar `fielding_quality` (plus the pressure
`drop_mult` / `misfield_mult` the chasing-pressure layer raises); there is no
per-fielder attribute anywhere, in keeping with the database constraint. A
weaker side drops more, and a side under pressure drops more.

Measured drop rate (share of all deliveries converted from a catch to runs,
strong bowler vs weak batter so chances are frequent):

| team fielding_quality | drop rate | under 2× pressure |
|----------------------:|----------:|------------------:|
| 30 (poor) | 0.90% | — |
| 60 (average) | 0.65% | 0.92% |
| 95 (elite) | 0.30% | — |

Per *chance*, that is a ~16% drop for a poor side down to ~4% for an elite one —
realistic, and monotone in quality. With no `fielding_quality` supplied the drop
path is inert (the engine never invents a fallback attribute). Verified in
`FieldingRealismTests`.

### Residuals (documented, bounded)

* **Super overs** run on the separate legacy `services.probability_engine`, not
  the realism funnel, and stay on the global `random` module. They fire only on
  a tie and do not affect the main match stream.
* **cipl narrative helpers** (per-innings variance draw, scenario arming) remain
  on the global module. They are per-innings, not per-delivery, and low-volume;
  the dominant per-ball draws (outcome, fielding, extras, wicket type) are all
  on the derived per-delivery stream.
