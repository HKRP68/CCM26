# Realism Engine v1

A data-driven ball-outcome model for CCM26, calibrated to real cricket and
wired into `/cipl` and `/sim` behind feature flags. **It is off by default.**
A deployment that changes nothing keeps the engine CCM26 has always had,
delivery for delivery.

- [What it is](#what-it-is)
- [Quick start](#quick-start)
- [Data sources and licensing](#data-sources-and-licensing)
- [How the model works](#how-the-model-works)
- [Calibration](#calibration)
- [Configuration reference](#configuration-reference)
- [Which modes are supported](#which-modes-are-supported)
- [Rollback](#rollback)
- [Known limitations](#known-limitations)

---

## What it is

Every delivery in `/cipl`, `/sim` and `/letsplay` goes through
`engine.ball_outcome.calculate_outcome`. That function builds a weight for each
of eight outcomes — `Dot Single Double Three Four Six Wicket Extras` —
normalises them and samples one.

The realism engine replaces how those weights are *built*. Instead of a
hand-set per-pitch scoring matrix pushed around by phase boosts and a per-ball
rating duel, it starts from a distribution fitted to real cricket for this
format, era, phase, innings and wickets-lost, then applies a calibrated rating
curve and bounded condition adjustments.

Everything downstream of the weights is untouched: wicket-type selection,
extras-type selection, free-hit rules, the fielding drop and misfield
mechanics, the result dict, the scorecard, match state, commentary and every
Telegram command.

### What it does not do

The brief's hard constraint is that the player system must not change, and it
has not:

- `data/players.json` is byte-identical. No field added, no rating edited.
- No per-player statistics, form, confidence, technique, pace or aggression
  are stored anywhere, ever.
- The model reads exactly five things off a card: batting style, batting
  rating, bowling style, bowling rating and name (for logs only).
  `overall_rating` is **deliberately not read** — it is derived from the
  batting and bowling numbers, so using it would count the same ability twice.
- Two cards with the same ratings and styles have identical ability
  distributions in identical situations. This is structural, not a promise:
  those five values are the only per-player inputs to
  `engine.realism.outcome_distribution.build`.
- Fatigue, pressure, balls faced, batter runs and batting position are read
  from live match state and die with the innings.
- No migration. Nothing writes to the roster.

`tests/test_realism_reference.py` asserts all of this, including that the
reference files contain no player's name.

---

## Quick start

```bash
# See what the shipped reference contains and where it came from
python -m tools.build_realism_reference coverage --version v1

# Watch the new model without letting it decide anything
SIM_REALISM_SHADOW_MODE=1 python -m tools.compare_realism --shadow --n 40

# Compare old vs new vs real
python -m tools.compare_realism --n 150

# Turn it on
export SIM_REALISM_ENABLED=1

# Turn it off again
unset SIM_REALISM_ENABLED
```

### Optional dependencies

None. The engine uses only the standard library plus what CCM26 already
installs. The reference files are plain JSON — 43 KB for v1 (31 KB T20, 11 KB
ODI, 1 KB manifest) — read once and cached. **No network access is ever made
during a simulated ball**: the only file read is the reference, and only on
first use.

Building a reference from the Cricsheet archive needs the archive downloaded
(see below); the bot never downloads anything.

---

## Data sources and licensing

### Cricsheet (ODbL 1.0)

Ball-by-ball data comes from [Cricsheet](https://cricsheet.org/), published
under the **Open Data Commons Open Database Licence (ODbL) 1.0**.

Reference files derived from it are a **Derivative Database** under that
licence. If you redistribute them you must:

1. attribute Cricsheet as the source,
2. license the derived database under ODbL 1.0,
3. include or link a copy of the licence,
4. keep the database open — no DRM or technical restriction on access.

The ODbL covers the *database*. CCM26's own code is unaffected by it.
`data/simulation_reference/*/manifest.json` carries the notice in machine-
readable form, and `tests/test_realism_reference.py` fails if it is removed.

### Cricket-Intelligence-Lab (MIT)

[HalcyonVector/Cricket-Intelligence-Lab](https://github.com/HalcyonVector/Cricket-Intelligence-Lab)
is MIT licensed. `tools/build_realism_reference.py` follows its approach to
Cricsheet's JSON layout — iterate `innings[] → overs[] → deliveries[]`, read
`runs.batter`, `extras.{wides,noballs,byes,legbyes}` and `wickets[]`, take
competition and date from `info`, and count into a context key. **No code was
copied verbatim**; the CCM26 version is written for the eight-outcome
vocabulary this engine uses. The debt is to the data layout and the idea, and
it is credited in the manifest.

### What the shipped v1 reference actually is

This matters, so it is stated plainly rather than buried.

**`cricsheet.org` was not reachable from the environment this was built in.**
No ball-by-ball deliveries were counted. The shipped v1 reference was built in
`anchors` mode, from two clearly separated ingredients:

| Part | Source | Status |
|---|---|---|
| Scoring level (runs per ball faced) | `static/ipl16/ipl_master_calibrated.csv` | **Measured** |
| Dismissal level (wickets per ball faced) | same | **Measured** |
| Extras level (wides + no-balls per legal ball) | same, derived | **Measured** (approximation) |
| Outcome shape (dot/1/2/3/4/6 split) | published aggregate T20 shares | **Prior** |
| Phase / innings / wickets structure | published aggregate phase splits | **Prior** |
| ODI reference, all of it | published aggregate ODI figures | **Prior** |

`static/ipl16/ipl_master_calibrated.csv` is real IPL player-season data already
committed to this repository, carrying Cricsheet player-registry ids. Summed
across every player it gives competition-level, player-independent aggregates
for three eras:

| Era | Runs/ball | RPO | Balls/wicket | Extras RPO | Balls faced |
|---|---|---|---|---|---|
| 2008-2013 | 1.2291 | 7.37 | 19.55 | 0.267 | 91,253 |
| 2014-2021 | 1.3200 | 7.92 | 19.88 | 0.244 | 109,619 |
| 2022-2026 | 1.4690 | 8.81 | 18.90 | 0.327 | 83,758 |

Those are measurements. The *shape* — how a given run rate splits between
dots, ones, fours and sixes — cannot be recovered from season aggregates, so
it comes from published aggregate shares (the same figures this repository
already carries in `tools/realism_audit.REAL_BALLS`) and is then **fitted** to
the measured level by maximum-entropy exponential tilting: among all
distributions with the prior's shape, the one whose expected runs match the
measured anchor exactly.

Every node built this way is tagged `count_kind: "pseudo"`, its pseudo-count
magnitude is a *confidence weight* and not a sample size, and the file's
`provenance` block states the limitation. Nothing presents a prior as a
measurement. `tests/test_realism_reference.py` enforces that.

**To get a genuinely ball-by-ball reference**, download the archive and run:

```bash
python -m tools.build_realism_reference cricsheet \
    --zip ~/cricsheet/ipl_json.zip \
    --zip ~/cricsheet/t20s_json.zip \
    --format T20 --holdout-from 2024 \
    --out data/simulation_reference/v2
export SIM_REALISM_REFERENCE_VERSION=v2
```

Every node is then `count_kind: "observed"` with a real sample size, the
competition and era splits are real, the chase-situation hierarchy level gets
populated (it cannot be in `anchors` mode), and `--holdout-from` keeps a
period out of the fit so `tools/compare_realism.py` can score the model on
data it never saw.

---

## How the model works

```
engine/realism/
  context.py                BallContext — everything the model may look at
  styles.py                 card style strings → pace/spin + handedness bucket
  baselines.py              reference loading, validation, smoothing, coverage
  rating_curve.py           bat_rating vs bowl_rating → bounded multipliers
  conditions.py             pitch, dew, tactics, fatigue, pressure, chase
  outcome_distribution.py   composes all of it into one normalised distribution
  integration.py            the seam with engine/ball_outcome
  shadow.py                 shadow-mode divergence recorder
  config.py                 env flags + config/realism.json
```

A delivery is built in five stages, **each applied exactly once**:

1. **Baseline** — the real-data distribution for this context.
2. **Rating curve** — `bat_rating` against `bowl_rating`.
3. **Conditions** — pitch, weather/dew, tactics, fatigue, batter settling,
   batter milestone, chase, style match-up. Bounded individually and jointly.
4. **Downstream gain** — a measured correction (see below).
5. **Normalise**, clamp the wicket share into its band, re-normalise.

### The two batter-state families

Both are keyed on live match state, both are **mean-neutral by
construction**, and between them they decide how an innings' runs are
distributed across batters rather than how many there are:

- **Settling** (`balls_faced`) — a new batter's first three balls are their
  most dangerous, a batter 11-24 balls in is the safest they will ever be,
  and past 45 balls they are swinging at everything. Band shares are
  measured, not assumed.
- **Milestone** (`batter_runs`) — dismissal risk rises with the score a
  batter has actually made: ×1.25 past fifty, ×1.9 past seventy-five, ×2.2
  past a hundred.

Neither stores anything. `balls_faced` and `batter_runs` are read from the
innings in progress and discarded with it, exactly as §1 of the brief allows,
and two cards with the same ratings on the same score are treated identically.

The milestone family is **wicket-only**, and the version that was not is
instructive. Raising a set batter's *scoring* rate as well is a feedback
loop: scoring faster moves the batter into a higher band, which raises their
scoring rate again. Mean-neutrality is computed against the measured band
shares, and the loop shifts those shares upward, so the invariant quietly
stops holding — measured, first-innings totals went from 183.5 to 187.9 and
the centuries the family was added to curb did not move at all.

### The scoreboard has to be passed in

`calculate_outcome` takes an optional `match_context` dict —
`{"runs", "wickets", "balls_bowled", "target"}` — which `/cipl` and `/sim`
now supply. It is not decoration. The game-state vector the function already
receives (`engine.game_state_engine.compute_game_state_vector`) carries
derived indices — `rr_ratio`, `required_aggression`, `wickets_in_hand`,
`rrr` — and **not** the score, the wickets or the target. Reading
`game_state["score"]` or `["target"]` therefore returns 0 and None with no
error at all, which pinned every delivery to the `w0_2` baseline node and
switched the entire chase layer off silently. With the scoreboard wired
through, all three phases, both innings and all three wickets buckets are
reached. `match_context` defaults to None and is ignored by every legacy
path, so no existing caller is affected.

The result is guaranteed finite, non-negative, summing to 1 within 1e-9, over
exactly the eight outcomes the engine already understands.
`tests/test_realism_outcomes.py` checks this across more than a thousand
contexts plus a set of deliberately absurd ones.

**No random number is drawn anywhere in the model.** That is what makes shadow
mode free.

### Hierarchical smoothing

Nodes form a tree and each level is smoothed into its parent exactly as the
brief specifies:

```
P(outcome | context) = (count + alpha * P_parent(outcome)) / (total + alpha)

format → competition/era → phase → innings → wickets → chase band
```

A node with 50 deliveries behind it is dominated by its parent; one with
50,000 stands on its own; nothing between needs a special case. `alpha` rises
with depth (250 → 1200) because deeper nodes are thinner. A node the lookup
cannot find simply stops the descent at its parent, which is what the
hierarchy is for — and the `cricsheet` builder drops any node under 200
deliveries for exactly that reason.

Phase boundaries are a *fraction* of the innings (0-30% / 30-80% / 80-100%)
rather than hardcoded over numbers, so T20 maps to overs 0-5 / 6-15 / 16-19 —
the existing engine's split — and a 100-ball innings gets a correctly
proportioned powerplay and death without a per-format table.

### The rating curve

```
advantage = tanh((bat_rating - bowl_rating - gap_anchor) / scale)
mult      = exp(gap_elasticity[outcome] * advantage
                + level_elasticity[outcome] * batter_level) / normaliser
```

Three ideas:

- **Gap term** — the relative contest. `tanh` is steep through the realistic
  range and saturates smoothly, so a big edge is a big edge without being a
  certainty. `scale` is 17.0, matching `engine/rating_duel.GAP_SCALE`.
- **Level term** — absolute class, independent of the gap. Two tail-enders at
  parity produce dots and edges, not sixes. This is not double-counting the
  rating: the gap term is the duel, the level term is the standard of cricket.
- **Extras term** — a wide is a bowler error, not a contest, so extras scale
  with the bowler's class alone.

Then bounded: no single factor exceeds 2.75x, and the normalised wicket share
is clamped into `[1.2%, 14%]`. The floor is the important half — **a
99-rated batter facing a 30-rated bowler still has at least a 1.2% chance of
being dismissed on any ball**, about once every 83 balls. Nobody is immortal.

### The chase is measured against its own par

`conditions.chase_multipliers` compares the required rate to the rate the
chase *started* at, so the ratio is exactly 1.0 on the first ball of any
chase and only moves as the side gets ahead of or behind its own target. The
obvious alternative — compare the asking rate to the rate achieved so far,
with some "typical" value standing in before any balls are bowled — reads
ball one of a 186-chase as already behind, and then applies a
behind-the-asking-rate penalty for the whole innings. Measured, that cost
chases 8 percentage points of success.

The penalty for falling behind is also deliberately milder than it first
was. A chase that drops behind takes more risk, loses a wicket and is then
further behind, so a strong behind-penalty compounds into a spiral: at
1.14x/1.42x on the wicket weight, chases came home 36% of the time.

This is the only place the engine looks at the target, and it looks at it as
a number a batting side would genuinely react to. It cannot see, and cannot
steer toward, a result.

### Where the legacy layers went

In realism mode some existing layers are bypassed, because leaving them on
would apply the same influence twice:

| Layer | Realism mode | Why |
|---|---|---|
| Hand-set pitch scoring matrix | **bypassed** | replaced by the fitted baseline |
| Powerplay / death / 2nd-innings-death boosts, death surge | **bypassed** | phase is the baseline's own hierarchy level |
| `T20_WICKET_REALISM_MULT`, `T20_DUEL_RECAL` | **bypassed** | these drag the old matrix toward the real wicket rate, which is now a measured anchor |
| Legacy pitch-wear model | **bypassed** | `conditions.pitch_multipliers` carries wear |
| Per-ball rating duel | **bypassed** | `rating_curve` carries rating influence, and the duel would spend two RNG draws |
| Game-state momentum engine (GSME) | **kept** | ball history is information the baseline cannot have |
| Pressure engine | **kept** | same |
| Approach modifiers | **kept** | `/cipl` has a UI for them; the realism *tactics* family switches itself off when they are active, so only one applies |
| `/letsplay` trait hook | **kept** | traits are a separate feature |
| Free hit, fielding drop/misfield, wicket & extras type | **kept** | not modelled by the realism layer |

Each bypass is a flag in `config/realism.json`, so the double count can be
measured rather than argued about.

### No scripted outcomes

The realism model cannot see a desired result. It reads the target only as a
*required rate* — a number a batting side would genuinely react to — and has
no access to `_planned_winner_user_id`, `_planned_super_overs` or any
scenario script. `tests/test_realism_integration.py` asserts that passing a
planned winner into the context changes nothing, and that the last ball of a
one-run chase keeps a real chance of a dot and a real chance of a wicket.

`engine/scenario_engine.py` is **not** deleted — it still serves exhibition
and admin-controlled matches. It is simply a separate, explicitly selected
layer. A tournament configured with `_planned_super_overs` or
`_planned_winner_user_id` is still steered by
`services/tournament_match_settings.py`, and **such a match is not a pure
realism simulation** even with `SIM_REALISM_ENABLED=1`. That is preserved for
backward compatibility and documented here rather than removed.

---

## Calibration

Two correction factors are **measured, not chosen**. Both exist for a real
reason discovered during calibration, and both are reproducible with one
command.

### 1. `rating_curve.normaliser` — per phase

`exp()` is convex, so by Jensen's inequality the *mean* of
`exp(k * advantage)` across an XI exceeds 1 even when the mean advantage is
zero. A real side bats its #9, #10 and #11 against good bowlers. Uncorrected,
the curve lifted the innings wicket rate from the measured 5.3% anchor to 6.4%
before any other layer ran.

It is **per phase** because the rating distribution is correlated with phase:
a side's best batters bat in the powerplay and its bowlers bat at the death.
Measured, the curve's mean boundary multiplier is 1.26x/1.38x (four/six) in
the powerplay and 0.85x/0.82x at the death. A single flat divisor left the
curve unbiased overall but biased *within* each phase — powerplay scoring came
out 23% high and first-innings totals reached 198 against a real figure near
176. Since the baseline is already conditioned on phase, and its phase profile
was measured from cricket in which the good batters also batted early, the
divisor has to be conditioned on phase too.

Related and also measured: the curve's neutral point. The reference describes
real professional T20, and the ball-weighted mean CCM26 match-up is
**bat 62.1 against bowl 73.5** — a gap of **-11.3**, because every XI bats its
bowlers but no XI bowls its batters. `gap_anchor` and `level_anchor` are set
there. Anchoring at the obvious-looking 75/75 put the origin two-thirds of a
standard deviation from where the data lives.

### 2. `integration.downstream_gain` — per phase

The baseline is an *innings average*, so it already contains the average of
everything the kept layers react to. Those layers carry genuine per-ball
information, but their net effect over an innings is not 1.0 — they were
calibrated against a different, lower base. Measured, they multiply the wicket
weight by about 1.26x, and the misfield mechanic systematically shifts
dot→single→double.

It is **per phase** for the same reason the normaliser is: those layers do
not bite equally everywhere. The momentum engine, the pressure engine and
aggressive captaincy all bite hardest at the death, and a single global
factor measured 12.8 runs an over there against a baseline of 11.0 while the
powerplay was already correct.

A related fix is not a factor but an invariant:
`conditions.wicket_pressure_multipliers` is **mean-neutral by construction**.
A layer that only ever raises the wicket chance for a new batter adds the
new-batter effect a second time, on top of an average that already included
it. So a new batter is more vulnerable *and* a set batter is correspondingly
less so, with the share weights that make the mean exactly 1.0. Without this
the engine produced 8.4 wickets an innings against a 6.3 anchor.

### Re-measuring

Order matters — the curve first, then the gain:

```bash
python -m tools.compare_realism --calibrate-curve --n 120   # iterate to ~1.0000
python -m tools.compare_realism --calibrate-gain  --n 120   # iterate to ~1.00
```

Each prints the values to paste into `config/realism.json`. Re-run after any
change to the kept layers, the reference data, or the elasticities. `Three` is
0.5% of deliveries, so its ratio is dominated by sampling noise — do not chase
it.

### Reading a shadow-mode report

```
shadow mode: 2832 deliveries
phase        balls      TV   d_runs  biggest shifts
powerplay      876  0.2519  +0.0272  Dot +0.0806  Single -0.0755  Wicket -0.0309
middle        1463  0.2298  +0.0729  Dot -0.0407  Single +0.0272  Extras +0.0219
death          493  0.2689  -0.0303  Single +0.0675  Four -0.0396  Dot -0.0314
overall       2832  0.2435  +0.0408  Wicket -0.0174  Extras +0.0166  Six +0.0070
```

`TV` is the total-variation distance between the two distributions and
`d_runs` is the difference in expected runs per ball.

One caveat worth stating, because it is easy to over-read: **shadow mode
measures the per-ball divergence in legacy contexts, not the difference the
two engines would produce over a match.** The legacy engine is still deciding
every outcome, so the score, the wickets and the batters' states that the
realism distribution is computed against are the ones legacy produced. The
compounding — a higher scoring rate putting more balls in high-scoring
contexts — does not appear. Here the shadow delta of +0.04 runs a ball
suggests about +5 runs an innings, while the two engines actually differ by
nearly 40. Use shadow mode to find *where* the models disagree and to catch a
distribution that has gone wrong; use `--n` benchmarking for what a match
would look like.

### Benchmarking

```bash
python -m tools.compare_realism --n 150 --json bench.json   # old vs new vs real
python -m tools.compare_realism --ratings --n 60            # rating sensitivity
python -m tools.compare_realism --shadow  --n 40            # divergence report
python -m tools.realism_audit --pitch Even --n 100          # existing texture audit
```

`--n` is matches per arm, two innings each. The brief asks for 10,000+ innings
in the main validation suite: that is `--n 5000`, which takes hours, so the
default is small and the number is a knob rather than a promise.

### Measured results

Every figure below is a **`/cipl`** measurement — `/cipl` is the primary
pilot and the one the correction factors are calibrated against. `/sim` shares
the model but keeps fewer of the layers those factors compensate for, so it
runs below its anchor; limitation 12 has its numbers.

`python -m tools.compare_realism --n 250 --seed 1009` — 500 matches per arm,
500 first innings per arm, ~57,000 legal balls per arm, pitch Even, the
`ipl_modern` baseline. These are measurements from this repository, not
estimates:

| metric | OLD | REALISM | baseline | real | source |
|---|---|---|---|---|---|
| mean 1st-inn total | 148.3 | **185.1** | – | 179.2 | measured-derived |
| median 1st-inn total | 148.0 | 187.5 | – | – | |
| stdev 1st-inn total | 35.5 | 28.0 | – | – | |
| 1st inn under 120 % | 19.2 | 2.0 | – | – | |
| 1st inn over 200 % | 8.8 | 30.0 | – | – | |
| dot % | 37.18 | **32.21** | 32.59 | 35.50 | prior all-T20 |
| single % | 34.28 | 33.79 | 32.29 | 33.00 | prior all-T20 |
| double % | 7.48 | 7.04 | 6.78 | 6.50 | prior all-T20 |
| three % | 0.47 | 0.50 | 0.56 | 0.50 | prior all-T20 |
| four % | 10.65 | **13.32** | 13.63 | 11.50 | prior all-T20 |
| six % | 4.35 | **7.22** | 7.41 | 5.50 | prior all-T20 |
| wicket % | 5.28 | **5.32** | 5.29 | 5.29 | measured |
| wide+nb % of legal | 1.75 | **2.81** | 2.71 | – | |
| wickets / innings | 6.02 | **6.17** | – | 6.35 | measured-derived |
| extras runs / innings | 3.65 | 6.25 | – | 10.13 | measured-derived |
| powerplay rpo | 7.70 | 9.11 | 8.65 | 8.80 | prior |
| middle rpo | 6.73 | 8.47 | 7.93 | 8.00 | prior |
| death rpo | 8.90 | **11.25** | 10.96 | 11.00 | prior |
| 50s / innings | 0.786 | 1.134 | – | 0.750 | prior |
| 100s / innings | 0.066 | 0.098 | – | 0.035 | prior |
| median economy | 7.25 | **9.00** | – | 9.14 | measured-derived |
| chase success % | 52.8 | 42.4 | – | 50.0 | prior |
| close match (≤10) % | 66.0 | 57.2 | – | 22.0 | prior |

Where the realism engine lands on its target and the old engine does not:

- **Dismissal rate is exact.** 5.32% against a measured 5.29%; 6.17 wickets an
  innings against 6.35.
- **First-innings totals.** 185.1 against a measured-derived 179.2 (+3.3%),
  where the legacy engine posts 148.3 (-17%).
- **Boundaries.** Sixes 7.22% against the baseline's 7.41%, where legacy
  managed 4.35% — the legacy engine was producing barely half the boundary
  hitting of the era it was meant to represent.
- **Extras.** 2.81% of legal balls against a baseline 2.71%; legacy managed
  1.75%, roughly a third of the real rate.
- **Phase shape.** Death overs at 11.25 rpo against a real ~11.0; legacy 8.90.
- **Economy.** Median 9.00 against a measured-derived 9.14; legacy 7.25.

And where it does not — stated plainly, with the rest in
[Known limitations](#known-limitations):

- **Chase success 42.4% against ~50%**, where legacy sits at 52.8%. This is
  the one headline metric on which the new engine is worse.
- **Centuries 0.098 an innings against ~0.034**, where legacy manages 0.066.
- **Close matches 57.2% against ~22%.** Both engines are wrong here; legacy
  is worse at 66%.
- **Variance is narrower** (stdev 28.0 against legacy's 35.5) and sub-120
  totals are rarer than reality (2.0% against a real 5-8%). Collapses need
  wicket *clustering*, which the per-ball model does not generate on its own.

So: every per-ball distributional metric and the headline total are on target,
and the residuals are concentrated in match-shape outcomes that depend on
layers above the ball model.

**What the "real" column is and is not.** Scoring, dismissal and extras levels
are the measured IPL anchors. The ball-split shares and the softer gameplay
figures are published aggregate priors. A row where the realism engine matches
the *shape* prior is evidence that the engine implements its prior faithfully —
not independent evidence about cricket. Proper held-out validation needs the
Cricsheet archive and `--holdout-from`; without it `compare_realism` says so
instead of printing a number it cannot justify.

---

## Configuration reference

### Feature flags (environment, like `config.py`)

| Variable | Default | Meaning |
|---|---|---|
| `SIM_REALISM_ENABLED` | `0` | let the realism model decide live outcomes |
| `SIM_REALISM_SHADOW_MODE` | `0` | compute it, record it, but keep the legacy outcome |
| `SIM_REALISM_REFERENCE_VERSION` | `v1` | which `data/simulation_reference/<v>/` to read |
| `SIM_REALISM_FORMATS` | `T20,The100` | formats the model may drive |
| `SIM_REALISM_BASELINE` | *(auto)* | force a competition/era, e.g. `ipl_2008_2013` |

If both `ENABLED` and `SHADOW_MODE` are set, **shadow wins**. Measuring is the
safe reading of an ambiguous configuration, and a half-rolled-out deployment
must never silently switch matches over.

### Tuning (`config/realism.json`, overlaid by `config/realism.local.json`)

Same convention as `config/sim_engine.json`. Anything omitted falls back to
`engine/realism/config.py: DEFAULTS`.

| Key | Default | Meaning |
|---|---|---|
| `rating_curve.scale` | 17.0 | tanh scale; matches `rating_duel.GAP_SCALE` |
| `rating_curve.strength` | 1.0 | global dial on how much ratings matter |
| `rating_curve.gap_anchor` | -11.33 | **measured** neutral gap |
| `rating_curve.level_anchor` | 62.14 | **measured** mean batting rating |
| `rating_curve.bowl_level_anchor` | 73.47 | **measured** mean bowling rating |
| `rating_curve.gap_elasticity` | per outcome | response to the rating gap |
| `rating_curve.level_elasticity` | per outcome | response to absolute class |
| `rating_curve.max_multiplier` | 2.75 | cap on any single rating factor |
| `rating_curve.min_wicket_share` | 0.012 | nobody is un-dismissable |
| `rating_curve.max_wicket_share` | 0.140 | safety valve, not a shaper |
| `rating_curve.normaliser` | **measured**, per phase | keeps the curve level-neutral |
| `conditions.bound` | 1.60 | per-family cap |
| `conditions.total_bound` | 2.20 | joint cap on all families |
| `conditions.*_strength` | 1.0 | per-family dial; `0` disables cleanly |
| `smoothing.alpha.*` | 250 → 1200 | smoothing strength per hierarchy level |
| `integration.keep_legacy_*` | `false` | re-enable a bypassed layer to study it |
| `integration.downstream_gain` | **measured** | compensates for the kept layers |
| `fallback.legacy_on_missing_reference` | `true` | fail safe to the old engine |

---

## Which modes are supported

| Mode | Command | Status |
|---|---|---|
| CIPL T20 | `/cipl` | **uses the realism model** when enabled |
| Short-format sim | `/sim` (T20, custom overs) | **uses the realism model** when enabled |
| Let's Play | `/letsplay` | **uses the realism model** when enabled (shares `calculate_outcome`) |
| The Hundred | `The100` | wired and allow-listed; phases scale correctly, but **no Hundred-specific reference data** — it reads the T20 reference |
| ODI / List A (50 over) | `/sim` long, ListA | **legacy engine.** Reference ships but is prior-only and not allow-listed |
| Test | long-form | **legacy engine.** No reference exists |
| `/wpm`, `/wpmbot` | `services/probability_engine.py` | **legacy engine** — a separate per-ball model that does not call `calculate_outcome` |
| `/cm`, quick match | `services/match_engine.py` | **legacy engine** — same reason |
| Super over | `engine/super_over_outcome.py` | **legacy engine** |

All three pilot modes share one funnel, `calculate_outcome`, which is why
wiring it there reached them together without touching a command handler.

### What integrating the remaining modes needs

- **`/wpm`, `/cm`, super over** — `services/probability_engine.py` is a
  different model: it resolves a bowler/variation/length against a batter shot
  from `data/probability_matrix.csv`, with its own vocabulary. Integrating it
  means mapping that shot-level matrix onto the eight-outcome distribution, or
  giving the realism engine a shot-conditioned layer. Cricsheet records no
  shot or length, so the second option would need a data source this project
  does not have. The honest route is the first: use the realism distribution
  as the marginal and keep the existing matrix as the conditional shape.
- **ODI / List A** — needs a measured ODI anchor. The in-repo CSV is IPL only.
  Build an ODI reference from `odis_json.zip`, validate the 50-over phase
  structure (ODI has three field-restriction phases, not two), then add
  `ListA` to `SIM_REALISM_FORMATS`.
- **Test** — needs a session/over-age model the v1 hierarchy does not have,
  plus a declaration and follow-on-aware context. Not started.
- **The Hundred** — needs its own reference. 100-ball cricket has a different
  powerplay shape and a five/ten-ball over structure; reading the T20
  reference is a reasonable approximation, not a validated one.

None of these is claimed as integrated. `engine.realism.integration
.supported_formats()` reports the live truth at runtime.

---

## Rollback

Immediate, no deploy, no data change:

```bash
unset SIM_REALISM_ENABLED
unset SIM_REALISM_SHADOW_MODE
```

The next delivery uses the legacy engine. Nothing persisted differs between
the two modes: the realism path writes no extra match state and adds only an
additive `result["realism"]` provenance key that every existing consumer
ignores.

Partial rollbacks:

- **Keep it on but soften it** — lower `rating_curve.strength`, or set a
  `conditions.*_strength` to 0.
- **Pin an older reference** — `SIM_REALISM_REFERENCE_VERSION=v1`.
- **Restrict to one format** — `SIM_REALISM_FORMATS=T20`.
- **Delete the reference** — `data/simulation_reference/` can simply be
  removed. Every mode reverts to the legacy engine with one `WARNING` log
  line. There is no code path that substitutes invented probabilities for a
  missing reference; `tests/test_realism_reference.py::SafeFallbackTests`
  covers missing files, corrupt JSON, negative counts and parent cycles.

---

## Known limitations

1. **The shipped v1 reference counts no deliveries.** `cricsheet.org` was not
   reachable when it was built. The scoring, dismissal and extras *levels* are
   measured from real IPL aggregates in this repository; the *shape* and the
   phase/innings/wickets structure are documented priors. Rebuild with the
   `cricsheet` mode for observed counts. This is the single biggest limitation
   and everything else follows from it.
2. **No held-out validation.** Scoring the model on data it never saw needs
   ball-by-ball deliveries and `--holdout-from`. `compare_realism` reports the
   absence rather than inventing a figure.
3. **The chase hierarchy level is unpopulated.** `anchors` mode cannot build
   it, so chase situations currently resolve to their wickets-bucket parent and
   the chase effect comes entirely from `conditions.chase_multipliers`.
4. **The ODI reference is prior-only** and not enabled. It is shipped so the
   plumbing is testable, not because it is validated.
5. **Style effects are deliberately coarse.** Cricsheet records bowler and
   batter but no line, length, seam or turn, and published handedness splits
   are a point or two of strike rate. So `styles.py` collapses to pace/spin
   and four handedness buckets, and `conditions.style_multipliers` applies a
   small documented nudge. The engine does not claim a detailed handedness
   model, and `tests/test_realism_rating_curve.py` asserts that handedness
   alone moves no outcome by more than 2 percentage points.
6. **Weather is not in the data at all.** Cricsheet has none. The dew model is
   CCM26's existing one, re-expressed as a bounded family. Its direction is
   well evidenced; its magnitude is not measured here.
7. **Venue effects are not modelled.** The brief allows them "where
   adequately supported by data"; per-venue samples in the in-repo aggregates
   are not adequate, so v1 has no venue layer. Pitch type is used instead, and
   its *ordering* between surfaces is CCM26's existing calibrated model, not a
   measurement — only the overall level is fitted.
8. **`batter_runs` is not an input.** A set batter is modelled through
   `balls_faced` only. The legacy engine's runs-based confidence curve is a
   form proxy the realism model deliberately omits; the baseline already
   contains the average effect of set and unset batters.
9. **Close finishes are too common in both engines.** The benchmark measures
   roughly 55-60% of matches inside 10 runs against a real figure near 22%.
   This is pre-existing — the legacy engine measures worse, at about 66% — and
   is driven by layers outside the ball model (`services/match_drama.py`'s
   contest and ceiling hooks, which exist to stop runaways). Fixing it means
   recalibrating those hooks, which would change the legacy engine too, so it
   is out of scope for v1 and recorded here as confirmed and unaddressed.

10. **Chase success is below the real figure.** Measured at 42.4%
    against a real ~50%, where the legacy engine sits at 52.8%. This is the
    one headline metric on which the realism engine is *worse* than the one
    it replaces, so it is worth being precise about what is and is not known:

    - It is **not** the chase family. Switching that family off entirely
      (`conditions.chase_strength = 0`) makes chases *worse*, not better —
      35% against 44% — so the family is helping.
    - It **was** partly an unmeasured second-innings prior. A reference built
      with `INNINGS_PRIOR` at (0.985, 1.03) — chasing sides scoring slightly
      slower and losing slightly more wickets, which sounds harmless — cost
      about ten percentage points, because a 186 target sits where a four-run
      shift in the mean moves the win probability a long way. The v1
      reference is now neutral between innings, which is also the honest
      position: the in-repo aggregates cannot separate the two innings, so
      asserting a difference was never supportable.
    - What remains is most likely the first-innings level, which runs a few
      percent above the measured anchor and makes every target harder.

    A reference built from real ball-by-ball data would populate the innings
    and chase-band hierarchy levels with observed counts and should settle
    this properly.

11. **Centuries are still too frequent.** 0.098 an innings against a real
    modern-IPL figure near 0.034, with the legacy engine at 0.066. The team
    total is right and its distribution across batters is not quite. The
    milestone family brought this down from 0.21 and the measurements that
    drove it are in `conditions._MILESTONE_BANDS`; closing the rest would
    need a risk term that knows the batter's intent, which this data cannot
    support.
12. **The downstream gain is calibrated against the `/cipl` pipeline.** Every
    number in [Measured results](#measured-results) is a `/cipl` measurement,
    because `/cipl` is the primary pilot and `tools/compare_realism.py` drives
    it. `/sim` keeps *fewer* of the layers the gain compensates for — no
    approach modifiers, none of `services/match_drama.py`'s hooks — so the
    same gain over-corrects there and `/sim` lands below the anchor:

    | `/sim` first innings | legacy | realism | anchor |
    |---|---|---|---|
    | 20 overs | 125.6 (6.28 rpo) | 143.8 (7.19 rpo) | 8.81 rpo |
    | 10 overs | 62.9 (6.29 rpo) | 79.9 (7.99 rpo) | — |

    Still a clear improvement on legacy, and the 10-over row shows the phase
    model working — a 10-over innings is mostly powerplay and death, so it
    *should* score faster per over than a 20-over one, and under legacy the
    two were indistinguishable at 6.28 and 6.29. But `/sim` is not on its
    anchor, and closing that needs a per-pipeline gain, which means a
    `--pipeline sim` arm in `compare_realism` that v1 does not have. Until
    then, treat `/cipl` as the calibrated mode and `/sim` as improved but
    uncalibrated.

13. **The downstream gain is a correction, not an explanation.** It cancels
    the net level shift of the kept layers rather than fixing those layers'
    own calibration. The cleaner fix is to recalibrate GSME and the pressure
    engine against the new base; the gain makes the pilot measurable without
    destabilising modes still on the legacy path.
14. **Pseudo-count magnitudes are confidence weights, not sample sizes.** They
    are labelled `count_kind: "pseudo"` everywhere, but a reader who ignores
    that label will misread `n`.
