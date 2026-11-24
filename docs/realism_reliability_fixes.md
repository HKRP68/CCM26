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
| 4–5 | Individual innings & fielding realism | — | Pending |
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

### Residuals (documented, bounded)

* **Super overs** run on the separate legacy `services.probability_engine`, not
  the realism funnel, and stay on the global `random` module. They fire only on
  a tie and do not affect the main match stream.
* **cipl narrative helpers** (per-innings variance draw, scenario arming) remain
  on the global module. They are per-innings, not per-delivery, and low-volume;
  the dominant per-ball draws (outcome, fielding, extras, wicket type) are all
  on the derived per-delivery stream.
