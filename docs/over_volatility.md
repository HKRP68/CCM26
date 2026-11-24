# Realistic over-to-over variation (Historical Cricket Intelligence, Feature 2)

A zero-mean, temporary, bounded per-over random effect that makes the simulated
spread of runs-per-over match modern T20 **without** changing the average over
score or predetermining any over's result.

## Why (the empirical finding)

Measured over 4 330 simulated overs (realism engine, Even pitch) the current
engine was only *mildly* too uniform, not broken:

| metric | CCM26 (before) | modern-T20 target |
|--------|---------------:|------------------:|
| runs/over SD | 4.87 | 5.0–5.5 |
| low over (≤2) | 5.9% | ~7.5% |
| explosive (≥18) | 6.2% | ~6% |
| maiden (0) | 0.25% | ~1.8% |

So Part D's effect is introduced **only** to close the SD / low-over gap —
deliberately small, per the brief's "use this effect only when it improves
held-out accuracy."

## The model (provably zero-mean)

`engine/realism/over_volatility.py`. Each over draws one standard normal `z`
(seeded match RNG). The over's *target* conditional run-mean is

```
target = base_mean + sigma * z          # LINEAR in z
```

and the six run outcomes (Dot..Six) are tilted to hit it exactly with the
existing max-entropy solver (`engine/realism/tilt.solve_mean`). Because the
target is linear and `E[z] = 0`, the expectation over overs is exactly
`base_mean`: the innings total is unchanged, only the variance grows. A small
symmetric wicket coupling (`exp(-wicket_coupling * z)`) lets a tight over cluster
the odd extra wicket. Bounded (`|z| ≤ 2.5`), renormalised, and it never revives a
zero-probability outcome. No player attribute is introduced.

Config (`config/realism.json` → `over_volatility`, defaults in
`engine/realism/config.py`): `sigma=0.34`, `bound=2.5`, `wicket_coupling=0.06`.

## Files

* `engine/realism/over_volatility.py` — the effect (new).
* `engine/realism/config.py` — `over_volatility_enabled()`,
  `over_volatility_config()`, defaults.
* `services/sim_match.py`, `services/cipl_match.py` — integration (behind the flag).
* `data/simulation_reference/v1/t20_over_volatility.json` — reference targets +
  fitted params + provenance (new).
* `tools/build_realism_reference.py` — `cricsheet --over-stats` emits the
  over-volatility reference from real counts.
* `tools/compare_over_volatility.py` — the benchmark (new).
* `tests/test_over_volatility.py`, `tests/test_over_volatility_integration.py` (new).

## Data preparation

The reference is lightweight (one small JSON), never ball-by-ball at runtime.
`python -m tools.build_realism_reference cricsheet --zip <t20s_json.zip>
--over-stats` walks Cricsheet overs and writes `t20_over_volatility.json` with
the measured runs/over mean+SD, maiden/low/explosive/2-wicket rates and sample
size. In this build the committed file carries published-aggregate targets
because Cricsheet is not reachable from the sandbox (the proxy blocks
cricsheet.org); the rebuild command above regenerates exact counts offline.

## Integration points

The effect is applied as the **last weight shape** before the single
authoritative sample, on top of the realism distribution — never a second
sample. It engages only when realism is live and `SIM_REALISM_OVER_VOL=1`:

* `/sim`: one `OverVolatility` per innings, drawn from an independent child of
  the match RNG (so toggling it never shifts the ball-outcome stream); the
  per-over `z` is drawn at over start and applied via a hook composed before the
  Balanced Drama overlay.
* `/cipl`: the per-over `z` is derived from `(rng_seed, innings, over)`, so it is
  reproducible and identical across a pause/resume of the same over, and
  composed onto the existing hook chain.

## Realism & Balanced-Drama compatibility

Over-volatility is a *realism-layer* effect (independent over-to-over shocks),
distinct from the Balanced Drama *momentum* overlay (correlated continuity), so
the two model different things and are not the same mechanism counted twice
(Part D). It is calibrated on the /sim realism path (which has no GSME momentum).
In /cipl it stacks with the existing GSME momentum, which already adds some
variance, so the combined over-SD there may run slightly higher — operators
should re-measure in /cipl before a wide rollout (see Limitations). Normal
Balanced matches keep no predetermined winner: the effect is zero-mean and never
targets a particular over score.

## Benchmark (measured, not claimed)

`tools/compare_over_volatility.py --n 100 --pitch Even` (3 591 overs/arm):

| metric | OFF | ON | real target |
|--------|----:|---:|------------:|
| runs/over SD | 4.84 | **5.10** | 5.2 |
| low-over (≤2) | 6.3% | 7.0% | 7.5% |
| explosive (≥18) | 6.0% | 6.7% | 6.0% |
| 2+ wkt over | 4.9% | 5.1% | 6.0% |
| mean 1st-inn total | 181.8 | 179.9 | — |

The SD moves into the historical band and the tails fatten, while the mean total
shifts by −1.9 runs (≈1%, within one standard error of OFF) — i.e. zero-mean.

## Tests

`tests/test_over_volatility.py` (unit): disabled/zero-z pass-through, positive z
lifts scoring and negative z cuts it, **zero-mean over many overs**, adds
over-variance, bounded + normalised, zero outcome not revived, reproducible.
`tests/test_over_volatility_integration.py`: over-spread widens without moving
the mean, reproducible from a seed, default-off is untouched, no player data
touched.

## Known limitations

* Maiden overs stay rarer than history (~0.4% vs ~1.8%). True maidens come from
  bowling skill and pressure (death yorkers, a weak batter against a strong
  bowler), not a symmetric over shock; forcing them up with a larger `sigma`
  would overshoot the SD and the explosive-over rate. The per-ball rating /
  death-over model is the right home for that and is left to a later pass.
* The `/cipl` interaction with GSME momentum is not separately re-calibrated here.
* The committed reference uses published-aggregate targets, not sandbox-counted
  Cricsheet deliveries (proxy-blocked); rebuild offline for exact figures.

## Activation & rollback

Enable: `SIM_REALISM_ENABLED=1 SIM_REALISM_OVER_VOL=1`. Roll back instantly by
unsetting `SIM_REALISM_OVER_VOL` (default off) — the effect becomes a pure
pass-through and the engine is byte-identical to realism without it. It has no
effect unless realism is actually driving the deliveries.
