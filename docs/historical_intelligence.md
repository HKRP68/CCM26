# Historical Cricket Intelligence Upgrade — deliverables

Two prioritised features, both integrated into the common outcome engine behind
feature flags, preserving the six player fields and Balanced Drama.

## Feature 1 — Context-aware ball probabilities

This is the **realism engine** (`engine/realism/`), built and benchmarked in the
earlier realism work and extended since. It is a historical-data-calibrated
conditional probability model with hierarchical smoothing

```
P(outcome | context) = (observed + alpha * parent_P) / (context_count + alpha)
```

and a format → competition/era → phase → innings → wickets → chase fallback.

### Context coverage

| context the brief asks for | where it lives |
|----------------------------|----------------|
| format | separate reference per format (`t20_baselines.json`, `odi_baselines.json`); only enabled formats drive live |
| innings | `inn1` / `inn2` baseline nodes |
| match phase / small over bands | `powerplay` / `middle` / `death` nodes (finer over bands available in `cricsheet` build mode) |
| over number | the phase the over falls in, from `balls_bowled` / `innings_balls` in `BallContext` |
| wickets lost | `w0_2` / `w3_5` / `w6_plus` nodes + `conditions.wicket_pressure_multipliers` |
| balls remaining | phase fraction + the chase layer |
| chase situation | `conditions.chase_multipliers` from the live required rate (rrr-band baseline nodes are populated in `cricsheet` mode; runtime-computed in the anchors reference) |

Player ratings (`rating_curve`), batting/bowling approaches
(`engine/realism/approaches.py`) and pitch/weather (`conditions`) are applied as
**separate, de-overlapped** layers — the realism de-overlap work removed the
double-counting between them, so a context, a rating and an approach each move
the distribution once.

### Data pipeline

`tools/build_realism_reference.py`:
* `cricsheet` — walks real Cricsheet JSON and *counts* deliveries into the
  context hierarchy (handles legal-ball vs attempted-ball, wides/no-balls/byes/
  leg-byes, run-outs, combined run+wicket balls, a per-year holdout split).
* `anchors` — builds from committed IPL aggregates when raw deliveries are not
  shipped (the committed reference, so the bot never loads millions of balls).

The committed reference is small and versioned under
`data/simulation_reference/v1/`.

### Validation

`tools/compare_realism.py` scores old engine vs realism vs real anchors.
Representative realism-arm result (Even pitch): wicket 5.44% (measured anchor
5.29%), dot/single/four/six shares near the modern-IPL targets, wickets/innings
6.29 (anchor 6.35). It also has a held-out mode
(`build_realism_reference cricsheet --holdout-from <year>`) to score on unseen
real deliveries. `tests/test_realism_reference.py` asserts the six player fields
and the reference schema are unchanged.

### Flags

`SIM_REALISM_ENABLED` (live), `SIM_REALISM_SHADOW_MODE` (compute-and-compare,
never changes a match), `SIM_REALISM_FORMATS`, plus legacy mode when unset.

## Feature 2 — Realistic over-to-over variation

`engine/realism/over_volatility.py` — see **docs/over_volatility.md** for the
full write-up. In short: a zero-mean, bounded per-over effect that widens the
runs-per-over spread to match modern T20 (measured SD 4.84 → 5.10 into the
5.0–5.5 band) without changing the innings mean (−1% shift, within one SE) or
predetermining any over. Behind `SIM_REALISM_OVER_VOL`, integrated into `/sim`
and `/cipl`, distinct from the Balanced Drama momentum overlay so the two are
not counted twice.

## Final deliverables checklist

1. **Files** — Feature 1: `engine/realism/*` + `tools/build_realism_reference.py`
   + `data/simulation_reference/v1/*` (pre-existing, confirmed). Feature 2:
   `engine/realism/over_volatility.py`, `config/realism.json` +
   `engine/realism/config.py`, `services/sim_match.py`,
   `services/cipl_match.py`, `data/simulation_reference/v1/t20_over_volatility.json`,
   `tools/compare_over_volatility.py`, `tools/build_realism_reference.py`
   (`--over-stats`), `tests/test_over_volatility*.py`, docs.
2. **Data prep + reference** — the ETL above; lightweight versioned JSON.
3. **Statistical model** — hierarchical smoothed conditional distributions
   (Feature 1); zero-mean max-entropy-tilted per-over effect (Feature 2).
4. **Integration points** — the shared `calculate_outcome` funnel used by `/sim`
   and `/cipl`, via the realism distribution and the weight-hook chain.
5. **Realism & Balanced Drama compatibility** — both features are additive,
   flag-gated, mean-respecting; Balanced Drama keeps no predetermined winners;
   `engine/scenario_engine.py` untouched.
6. **Tests** — `tests/test_realism_*`, `tests/test_over_volatility*`,
   `tests/test_drama_*`, `tests/test_sim_match.py`, `tests/test_cipl_*`.
7. **Benchmarks** — `tools/compare_realism.py`, `tools/compare_over_volatility.py`,
   `tools/fairness_report.py`, `tools/drama_balance_report.py`; actual numbers in
   the docs and commit messages.
8. **Known limitations** — maiden-over rate still below history; `/cipl`
   over-volatility not separately re-calibrated against GSME momentum; the
   committed over-volatility reference uses published-aggregate targets because
   Cricsheet is unreachable from the build sandbox (proxy-blocked) — the ETL
   regenerates exact counts offline.
9. **Activation & rollback** — `SIM_REALISM_ENABLED=1` (Feature 1),
   `SIM_REALISM_OVER_VOL=1` (Feature 2); unset either to roll back instantly to
   byte-identical prior behaviour.
10. **Branch** — `claude/ccm26-realism-engine-v1-dql40t`.
