# Balanced Drama Simulation Engine v1 (Codex Prompt 05)

A bounded drama overlay that sits **on top of** the realism engine. Guiding
principle: *every match should be capable of becoming a thriller, but no match
should be forced to become one.* It never samples its own replacement outcome
and never predetermines a competitive result — it shapes the existing outcome
weights, inside one shared budget, immediately before the single authoritative
sample.

No player-database field was added, removed or changed. `data/players.json`
stays byte-identical (asserted in `tests/test_drama_integration.py` and
`tests/test_realism_reference.py`); the six permitted fields are the only player
inputs. Every drama input is live, match-only state.

## Presets (§4)

Resolved **once at match creation** and recorded in the match, so flipping a
global setting cannot alter an ongoing match.

| Preset | Overlay | Scripted finales | Use |
|--------|---------|------------------|-----|
| `REALISTIC` | none | no | realism engine only |
| `BALANCED` | momentum + clutch + rare + tension + highlights, one shared budget | **no** | the target competitive preset |
| `CINEMATIC` | wider budget, higher volatility | yes (ScenarioEngine) | explicitly designated exhibition / challenge / story modes only |

Switches: `SIM_DRAMA_PRESET` (REALISTIC default) selects the policy;
`SIM_DRAMA_ENABLED` is the master override of the selected preset's own
`enabled` flag. Both default off, so a deployment that sets nothing keeps the
legacy / realism behaviour. Config lives in `config/drama_presets.json`,
validated and clamped on load so an invalid file can never crash a live match.

## Module structure (§5)

```
engine/drama/
  config.py      preset loading, validation, env switches
  context.py     DramaContext (match-state snapshot) + win-probability
  tension.py     Tension Index 0-100 (presentation only)
  momentum.py    bounded rolling-window momentum
  clutch.py      short-lived Clutch Focus event
  events.py      rare volatility + highlight detection
  modifiers.py   the single shared drama budget
  engine.py      DramaEngine orchestrator (serialisable state)
config/drama_presets.json
services/drama_runtime.py   glue between the match loops and the engine
tools/drama_calibration.py  activation / budget measurement
tools/drama_balance_report.py  legacy vs balanced comparison
```

Reused rather than duplicated: `engine/realism/win_probability.py` (tension and
clutch closeness), the realism outcome distribution and the existing
`calculate_outcome` / `weight_hook` funnel (the overlay is just the last hook),
and `engine/sim/rng.py` (a deterministic child stream per match).

## Exact algorithms

**Tension (§7)** — presentation only, never feeds an outcome. Second innings:
`C = 4p(1-p)`, `U = clamp(1 - balls_left/innings_balls, 0, 1)`, `S = 1` for a
knockout, `recent_swing` = largest |win-prob change| over the last 6 legal
balls; `tension = 100·clamp(C·(0.30+0.55U) + 0.10S + 0.05·recent_swing, 0, 1)`.
First innings uses a separate intensity from scoring volatility and recent
wickets (not a win probability).

**Momentum (§8)** — rolling 12-legal-ball residual
`(R-ER-4(W-EW))/sqrt(max(VR+16VW,4))`, clamped to ±3, then
`momentum_next = clamp(0.80·prev + 0.20·tanh(residual/2), -0.75, +0.75)`.
Updated **after** the delivery. Small per-outcome log adjustments
(Six +0.050·m, Dot −0.035·m, …) feed the shared budget.

**Clutch Focus (§9)** — eligible in the last 18 legal balls or a high-pressure
state, never once decided; `activation = min(cap, 0.03 + 0.06·4p(1-p))` per
eligible over (a lower low-pressure value in the first innings); symmetric
batting/bowling coin flip; ≤1 per side per innings; ≤6 legal balls. Batting
clutch Four×1.08 Six×1.12 Dot×0.96 Wicket×1.08; bowling clutch Dot×1.08
Wicket×1.10 Four×0.94 Six×0.92.

**Rare volatility (§10)** — 0.015 per eligible over, ≤1 per innings, never with
a clutch event, ≤1 over, a small counterattack or bowling-surge nudge.

**Shared budget (§11)** — `combined = momentum + clutch + rare`, clamped to
±ln(1.15), applied as `weight·exp(combined)` then renormalised. Two safety
limits per delivery: |ΔP(wicket)| ≤ 0.012 and |Δexpected runs| ≤ 0.08; a breach
reduces the whole overlay's strength smoothly (one scalar) until it complies. A
zero-probability outcome is never revived; a free hit gets no positive wicket
adjustment.

## Processing order (§12)

Base distribution → ratings → pitch/weather → approaches → legitimate
game-state effects → **Balanced Drama overlay (one budget)** → validation &
normalisation → one sample → update state → detect highlights. The overlay is
the last `weight_hook`, so it never double-counts an earlier layer, and there is
exactly one authoritative outcome per delivery.

## Supported modes (§24 Phase 6, honestly scoped)

* **/sim (`services/sim_match.py`)** — **fully integrated.** The overlay drives
  momentum, clutch, rare events, tension and highlights; the preset, drama
  engine version and detected highlights are recorded in the result. sim has no
  GSME momentum layer, so the drama engine is the single momentum owner here
  (no double-count, §18). The drama RNG is an independent child of the match
  RNG, so toggling drama never shifts the ball-outcome stream (§20).
* **/cipl (`services/cipl_match.py`)** — the chosen **preset and drama engine
  version are recorded** in match state at creation (§19/§20). CIPL keeps its
  existing single momentum mechanism (GSME); the per-ball drama overlay is not
  yet wired into its interactive, serialised ball loop, so it is **not claimed
  as integrated** (§24: "Do not falsely claim support for modes that have not
  been integrated"). This is the documented next increment.

The ScenarioEngine (`engine/scenario_engine.py`) is untouched and remains
available for Cinematic / explicit special modes. Balanced never calls
`get_override_outcome()` or `get_scenario_bias()`; in realism-live mode (which
Balanced builds on) the chase-steering paths are already disabled (Prompt 03),
so no scripted path leaks into a Balanced competitive match.

## Measured behaviour

`tools/drama_balance_report.py --n 50 --pitch Even`:

| metric | REALISTIC | BALANCED |
|--------|----------:|---------:|
| mean 1st-inn total | 148.9 | 150.7 |
| stdev 1st-inn total | 41.8 | 42.1 |
| 50s / innings | 0.70 | 0.72 |
| 100s / innings | 0.06 | 0.04 |
| highlights / innings | 0.00 | 3.88 |
| mean final tension | 0.0 | 45.6 |
| stronger-side win % (+12 rating) | 98 | 98 |

Balanced adds drama (≈4 highlights and a live tension read per innings) and a
little volatility **without** inflating average totals or wicket rates (§22) and
**without** breaking rating monotonicity (§19). `tools/drama_calibration.py
--n 40`: clutch activates only in genuinely close, undecided finishes
(≈0.01–0.06 per eligible over), rare volatility ≈0.19/innings (0.015/over),
|momentum| mean 0.28 / max 0.75 (bounded), and the budget's safety limit binds
on only ~1.7% of deliveries. These are initial balancing values, not
empirically-optimal ones (§21).

## Tests (§23)

`tests/test_drama_{tension,momentum,clutch,events,modifiers,fairness,integration}.py`
prove: tension stays 0–100 and never changes an outcome; momentum decays and
stays bounded; clutch obeys the per-side/per-innings and duration limits and is
symmetric; rare events cannot accumulate; the budget's caps bind and never
revive a zero outcome; Balanced is reproducible from a seed, varies across
seeds, does not inflate scoring, and touches no player data; and an inactive
preset is a pure pass-through (legacy byte-identical).

## Enabling and rollback

Enable Balanced: `SIM_REALISM_ENABLED=1 SIM_DRAMA_ENABLED=1
SIM_DRAMA_PRESET=BALANCED`. Roll back instantly by unsetting `SIM_DRAMA_ENABLED`
(or `SIM_DRAMA_PRESET=REALISTIC`): the overlay goes inert and the path is
byte-identical to the realism engine. The legacy engine is preserved whenever
Balanced is disabled or its config is unavailable.

### Known limitations

* CIPL overlay integration is pending (preset is recorded only), as above.
* Super overs run on the separate legacy `probability_engine` and are outside
  the drama/realism stream (a documented Prompt 04 residual).
* The config numbers are initial balancing values; a large-n calibration pass
  (the two tools above) should inform the final values before wider rollout.
