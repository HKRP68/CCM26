# Realism-first approaches, pitches and conditions

An extension of [Realism Engine v1](realism_engine_v1.md) that recalibrates
the tactical layer: the five batting approaches, the five bowling approaches,
the eight pitch types, and the weather, dew, wear, fatigue, momentum and
pressure effects around them.

Like the rest of the realism engine it is **off by default** and shares the
same switch. `SIM_REALISM_ENABLED=1` turns all of it on; unsetting it reverts
to the engine CCM26 has always had, approach tables included.

- [What changed](#what-changed)
- [Approaches as priced trade-offs](#approaches-as-priced-trade-offs)
- [What stopped being applied twice](#what-stopped-being-applied-twice)
- [Pitches and conditions](#pitches-and-conditions)
- [No identity-derived ability](#no-identity-derived-ability)
- [A captain who plays to win](#a-captain-who-plays-to-win)
- [No result steering](#no-result-steering)
- [Calibration and tests](#calibration-and-tests)
- [Configuration](#configuration)
- [Rollback](#rollback)
- [Limitations](#limitations)

---

## What changed

Nothing user-facing. Every approach keeps its key, emoji, label, blurb, risk
number and button position; every pitch type keeps its name and its place in
the registry; no player field was added, removed or edited.
`tests/test_realism_approaches.py::PreservedControlsTests` asserts the exact
lists and labels.

| | legacy | realism mode |
|---|---|---|
| Approach effect | hand-set 5×5 grid of per-outcome multipliers plus a counter matrix | a monotone trade-off frontier tilted against the measured baseline, plus bounded counters |
| Pitch effect on approaches | `_PITCH_RULES` table | the baseline's own pitch family (applied once) |
| Phase effect on approaches | `_PHASE_RULES` table | the baseline's phase hierarchy level (applied once) |
| Momentum / fatigue in the approach layer | `MOMENTUM_*`, `FATIGUE` | the game-state engine and `conditions.fatigue_multipliers` |
| Read / out-think mind game | `_READS`, `OUTTHINK_BONUS` | the counters |
| Hidden vs-pace / vs-spin ability | derived from the player's **id or name** | **removed** — ratings and styles only |
| First-innings captain objective | expected runs − wicket cost | win probability |
| Par-band brake, contest tightening, pitch-character, clutch, mix-up | on | **off** |

---

## Approaches as priced trade-offs

`engine/realism/approaches.py`. An approach is three numbers, not a grid:

```
runs            expected runs off the bat, relative
boundary_share  how much of that comes in fours and sixes
wicket          dismissal probability, relative
```

`engine/realism/tilt.py` turns the first two into per-outcome multipliers by
**maximum-entropy tilting of the baseline run shape** — the same machinery
that fitted the reference data, with a second parameter added:

```
p(r) ∝ prior(r) · exp(θ·r + φ·[r is a boundary])
```

θ sets the expected runs, φ sets the boundary share. Two knobs, because one
cannot express Rotate Strike: *more singles, fewer boundaries, a similar run
rate* is a two-dimensional move. (It is also what lets a powerplay be
high-dot **and** high-boundary at once, which is why the baseline builder
needed the same family.)

Tilting the **baseline for the delivery's own context** is what makes the
approach layer phase-aware without a phase table. Ultra Attack in the
powerplay and Ultra Attack at the death are the same intent applied to two
different shapes, and come out differently because the shapes differ — not
because a table says so.

### The frontier

The endpoints are cricket, not round numbers: a batter playing purely for
survival scores at ~0.63 of the par rate and is dismissed at ~0.60 of it;
all-out attack scores at ~1.42 and is dismissed at ~1.79. The middle three
interpolate geometrically. **Scoring more costs dismissal risk by
construction**, and `_validate()` fails at import if the ladder is ever
edited so that it does not.

Bowling is not a single ladder — Variation is not "more attacking
Defensive" — so each plan is stated on its own, with the trade-off axis
being **runs conceded against wickets taken**, asserted as a strict pairwise
non-dominance check over all five plans.

That check earned its place. The first version had Variation conceding
*fewer* runs and taking *more* wickets than Balanced, which made Balanced
strictly dominated. No amount of reading the table showed it; the calibration
tool found it in one run — `balanced` was the best reply in **0 of 600**
measured cells, a button nobody should ever press.

### Mean-neutrality

The multipliers are normalised so the **mix-weighted mean is 1.0**, where the
mix is how often captains actually pick each approach (from `/pitchstats`:
Ultra 32%, Aggressive 22%, Balanced 18%, Rotate 18%, Defensive 7%).

This is the same invariant the batter-state families use, and for the same
reason. The baseline is measured from real cricket in which most captains
attack most of the time. Treating *Balanced* as the neutral point would add
the attacking premium a second time on top of a baseline that already
contains it, and every innings total would drift with whatever mix the
simulation happened to play. Normalising on the mix means the innings lands
on its measured anchor whatever the captains choose, and the approach layer
decides only how the runs are distributed and what they cost.

A second, residual correction is then **measured at runtime**, per baseline
node, because the tilt is nonlinear and the counters are not mix-neutral
either: normalising the tables leaves the realised 25-cell mean 3.3% low on
runs and 7.4% high on wickets. It is not a config constant on purpose — it is
a pure function of the baseline and the tables, so computing it where it is
used means it cannot go stale when either changes. Measured drift after
correction is under 1.1% on every node.

### No dominant choice

A monotone frontier has no dominant point only if the opponent's choice
changes which point is best, so `COUNTERS` carries bounded interaction terms
— the cricket in which the same plan is a match-winner against one intent and
a gift against another. Magnitudes are capped at ±45% and asserted; counters
break ties, they do not decide matches.

Measured over 8 pitches × 3 phases × 5 wicket costs = 600 best-reply cells
per side:

| batting | best reply in | | bowling | best reply in |
|---|---|---|---|---|
| defensive | 14.0% | | defensive | 1.2% |
| rotate | 6.8% | | balanced | 6.5% |
| balanced | 10.8% | | mixed | 28.0% |
| aggressive | 12.7% | | variation | 36.0% |
| ultra | 55.7% | | aggressive | 28.3% |

Every approach is the right call somewhere; none is the right call
everywhere. Ultra's 55.7% is not a balance failure — it is a modern T20
roster against a sweep that includes cheap-wicket situations, and it matches
the 32% real pick rate once you account for Ultra also being the *fallback*
best reply when nothing else fits. At a wicket cost of 5 — the last over with
the shed full — all-out attack **should** be the answer to everything, and
the tool excludes that end of the sweep from its pass/fail for exactly that
reason.

Ranking needs a value function, because raw runs always favours Ultra and raw
wickets always favours Aggressive bowling. The one used is the game's own:
runs minus the wicket cost, swept from 5 runs (last over, wickets spare) to
25 (150/6 with overs left).

---

## What stopped being applied twice

Requirement 3. In realism mode `engine/approach_modifiers.py` runs an
**allowlist** of its situational layers, not a denylist, so a layer added
there later is off until someone decides it does not double-count:

```json
"legacy_approach_layers": ["predictability", "chemistry"]
```

| layer | realism mode | why |
|---|---|---|
| base 5×5 tables, counter matrix, special combos | **off** | replaced by the frontier |
| `_PITCH_RULES` | **off** | the baseline has a pitch family |
| `_PHASE_RULES` | **off** | phase is a baseline hierarchy level |
| `_READS` / `OUTTHINK_BONUS` | **off** | the counters model the same thing |
| momentum | **off** | the game-state engine is kept and does this |
| fatigue | **off** | `conditions.fatigue_multipliers` |
| carry | **off** | belongs to the special combos, which are off |
| **predictability** | **on** | how readable a captain has become over several overs — nothing else models it |
| **chemistry** | **on** | how settled a batting pair is — ditto |

`tests/test_realism_approaches.py::NoOverlapTests` asserts the allowlist, that
every name in it is a real layer, that the suppressed ones are genuinely
inert, and that legacy mode still applies all of them.

---

## Pitches and conditions

Requirement 7: a pitch changes probabilities and never enforces a total.

The surface enters once, through `conditions.pitch_multipliers`, as a bounded
per-outcome family — run factor and wicket factor, with wear giving spin more
than pace. The *ordering* of surfaces is CCM26's existing calibrated model
(`PITCH_RUN_FACTOR` / `PITCH_WICKET_FACTOR`); only the overall level is fitted
to real data. That is stated plainly because it is the honest division: which
way a Dusty pitch plays is the game's design, how much scoring happens on it
is measured.

What is **gone** in realism mode is the par-band enforcement. The legacy
`make_score_ceiling_hook` damps boundaries and lifts wickets once an innings
projects past the top of the pitch's par band, blending the ball's weights
toward a "sane ball" by up to 75%. That is a predetermined total, not a
probability, and it is off.

Requirement 8: weather, dew and wear are bounded and not invented.
**Cricsheet records no weather at all**, so there is no empirical coefficient
to be had, and none is claimed. The dew model is CCM26's existing one
re-expressed as a bounded family: its direction is well evidenced (a night
chase skids and the grip goes), its magnitude is not measured here, and it
applies only to the second innings of a day/night match. Every family is
capped individually at 1.60× and jointly at 2.20×, so conditions colour a
delivery and cannot replace the distribution.

---

## No identity-derived ability

Requirement 9, and the most important fix in this change.

`engine/approach_modifiers.batting_matchup_delta` adjusts a batter's rating
against pace or spin. If the dict carries an explicit `vs_pace`/`vs_spin`
profile it uses it — that is data. If it does not, the legacy fallback
**hashes the player's roster id, or failing that their name**, into a ±6
rating swing with a matching weakness on the other side.

That is a hidden permanent ability difference derived from *who a player is*
rather than from anything on their card, and it means two cards with
identical ratings and styles are not the same player. It also ran **before**
the rating reached the realism rating curve, so until this change it was
leaking into the realism engine and quietly breaking the contract the v1 docs
claim.

In realism mode it returns 0.0. Explicit profiles are still honoured in both
modes. Measured:

```
legacy  (allow_identity=True):  -0.578   +5.445     two identical 75-rated cards
realism (allow_identity=False):  0.0      0.0
```

Nothing in either mode reads or writes a stored player field.

---

## A captain who plays to win

Requirement 10. `services/bot_tactics.py` already evaluated a **chase**
correctly — the value of an over is the win probability of the position it
leaves behind, which is the only objective that knows 6 an over is a triumph
at 30 off 60 and a disaster at 30 off 12. Batting **first** it maximised
expected runs minus a wicket penalty.

`engine/realism/win_probability.py` supplies the missing half:

```
P(win | first innings ends on T) = 1 − P(chase of T+1 succeeds)
```

reusing the chase model the bot already trusts, so the two halves of a match
cannot disagree about the same scoreboard (asserted by a test). Mid-innings
the total is a distribution, so the model projects the innings forward and
integrates win probability over it by five-point quadrature.

The shape is the point. Measured:

| position | P(win) | per run | per wicket | runs a wicket costs |
|---|---|---|---|---|
| 10/0 off 108 left | 0.379 | +0.0155 | +0.0691 | **4.5** |
| 50/2 off 72 | 0.104 | +0.0074 | +0.0207 | 2.8 |
| 100/4 off 42 | 0.107 | +0.0081 | +0.0135 | 1.7 |
| 150/5 off 18 | 0.287 | +0.0173 | +0.0131 | 0.8 |
| 170/6 off 6 | 0.348 | +0.0199 | +0.0051 | **0.3** |

A wicket costs 4.5 runs' worth of win probability in the second over and 0.3
in the last. The linear objective it replaces valued the 180th run of an
innings exactly as highly as the 80th. The model's 50/50 total is 185, which
is where the measured mean first-innings total sits — a consistency check
rather than a coincidence.

It changes decisions. At 110/4 after 14 overs the legacy objective favours
Balanced (0.90); the win-probability objective picks Ultra (1.00), because
Balanced projects to ~160 and loses.

The objective is used **only when the realism ball model is actually deciding
deliveries** (`integration.live_for`), so the captain is never optimising
against a different engine than the one bowling at it.

---

## No result steering

Requirement 11. Five hooks are off in realism mode, each individually
switchable and each still the default outside it:

| hook | what it did |
|---|---|
| `score_ceiling` | pulled a projecting innings back toward the pitch's par band |
| `contest` | tightened the bowling when a side was running away |
| `pitch_character` | re-asserted a surface's reputation on top of its modelled effect |
| `clutch_finale` | sharpened the last over of a chase |
| `wicket_mixup` | converted the odd dismissal into a run out |

Measured over 12 matches on a Flat pitch: legacy calls them 1,414 /
1,261 / 1,414 / 1,414 times; realism mode calls none of them.

They are **not deleted**. `engine/scenario_engine.py` and
`services/match_drama.py` remain intact for exhibition and
admin-controlled matches, and a tournament armed with
`_planned_super_overs` or `_planned_winner_user_id` is still steered by
`services/tournament_match_settings.py`. Such a match is not a pure realism
simulation even with the flag on — same caveat as v1, restated here because
this change is what makes the rest of it honest.

---

## Measured results

`python -m tools.compare_realism --n 120 --seed 8080` — 240 matches per arm,
240 first innings per arm, ~27,000 legal balls per arm, pitch Even,
`ipl_modern` baseline, with both arms playing the measured `/pitchstats`
approach mix.

| metric | OLD | REALISM | baseline | real |
|---|---|---|---|---|
| mean 1st-inn total | 144.0 | **185.2** | – | 179.2 |
| stdev 1st-inn total | 36.8 | 32.5 | – | – |
| 1st inn under 120 % | 22.5 | 3.3 | – | – |
| dot % | 37.82 | **31.92** | 32.59 | 35.50 |
| four % | 9.85 | **12.91** | 13.63 | 11.50 |
| six % | 4.21 | **7.33** | 7.41 | 5.50 |
| wicket % | 5.67 | **5.14** | 5.29 | 5.29 |
| wide+nb % of legal | 1.78 | **2.89** | 2.71 | – |
| wickets / innings | 6.39 | 5.94 | – | 6.35 |
| powerplay rpo | 7.31 | 9.18 | 8.65 | 8.80 |
| middle rpo | 6.52 | 8.46 | 7.93 | 8.00 |
| death rpo | 8.84 | **11.16** | 10.96 | 11.00 |
| median economy | 6.75 | **9.00** | – | 9.14 |
| **chase success %** | 49.2 | **47.5** | – | 50.0 |
| 100s / innings | 0.042 | 0.104 | – | 0.035 |
| close match (≤10) % | 57.5 | 60.0 | – | 22.0 |

The headline change from v1 is **chase success**: 42.4% → 47.5%, against a
real ~50% and a legacy 49.2%. That was the one metric on which v1 was worse
than the engine it replaced, and it is now essentially on target. Removing
the result-steering hooks is most of it — the clutch amplifier and the
contest tightening were both working against the chasing side — and the
win-probability captain is the rest.

Everything else holds: the per-ball distribution still tracks the baseline,
the dismissal rate is on its measured anchor, and the phase profile is
intact. Centuries (0.104 against ~0.035) and close finishes (60% against
~22%) remain as recorded in
[v1's limitations](realism_engine_v1.md#known-limitations).

### Recalibration was required, and why

The approach change moved the innings level, which is worth stating because
it is the kind of thing that silently invalidates a calibration. The v1
`downstream_gain` was measured with the **legacy** approach tables applying
downstream, and those tables are not mean-neutral — `ultra` scaled Wicket by
1.80 and `aggressive` by 1.45, so with a mix that is 54% those two they were
a large net wicket inflation that the gain was quietly cancelling. Replacing
them with a mean-neutral family left the gain over-correcting: measured, the
powerplay wicket rate fell to 2.69% against a 4.63% baseline.

A second mismatch surfaced at the same time. `tools/compare_realism.py` was
drawing approaches from `realism_audit`'s hand-set weights (balanced 39%,
ultra 17%) while the model is mean-normalised on the measured `/pitchstats`
mix (ultra 32%, balanced 18%). Benchmarking a model normalised on one mix
against a different one measures the mismatch, not the model, so the
benchmark now plays the measured mix.

Re-measuring after both fixes brought every phase back inside 7%.

---

## Calibration and tests

```bash
# the 25 cells on one pitch and phase, four ways
python -m tools.calibrate_approaches matrix --pitch Green --phase powerplay

# every pitch x phase x wicket-cost cell: is anything dominant or useless?
python -m tools.calibrate_approaches dominance

# does the approach layer move the innings level? (it must not)
python -m tools.calibrate_approaches neutrality

# real innings per pairing, through the engine
python -m tools.calibrate_approaches live --n 30
```

`dominance` exits non-zero if any approach is never the best reply or is best
everywhere. `neutrality` exits non-zero if drift exceeds 2% on any node.

`tests/test_realism_approaches.py` covers all 25 pairings on all 8 pitches in
all 3 phases (600 distributions), the trade-off monotonicity, pairwise
non-dominance, the counter structure, mean-neutrality, the overlap allowlist,
identity-derived ability, pitch probabilism, condition bounds, rating
fairness, the win-probability shape, result-steering, and that extras,
wickets and free hits still behave for every pairing.

---

## Configuration

Added under `conditions` and `integration` in `config/realism.json`
(defaults in `engine/realism/config.py`):

| key | default | meaning |
|---|---|---|
| `conditions.approach_strength` | 1.0 | dial on the whole approach model; 0 is a clean no-op |
| `conditions.approach_bound` | 2.60 | cap on any single approach multiplier |
| `integration.legacy_approach_layers` | `["predictability","chemistry"]` | allowlist of legacy situational layers |
| `integration.drama_hooks.*` | all `false` | re-enable an individual steering hook |
| `integration.identity_derived_ability` | `false` | allow hidden ability from a player's id |

---

## Rollback

Same switch as v1, and it covers all of this:

```bash
unset SIM_REALISM_ENABLED
```

The legacy approach tables, pitch rules, phase rules, mind game, momentum,
fatigue, identity-derived profiles, drama hooks and the expected-runs captain
objective all come straight back. Nothing persisted differs between the two
modes. Partial rollbacks: `approach_strength: 0` neutralises the new approach
model while keeping the rest of realism mode; adding a layer name to
`legacy_approach_layers` restores it; setting a `drama_hooks` entry to `true`
restores that hook.

---

## Limitations

1. **Bowling plans are nearly collinear at realistic magnitudes.** A
   containment plan saving a tenth of the runs for a sixth of the wickets and
   an attacking one buying a quarter more wickets for a seventh more runs
   put the five plans almost on a straight line, and a captain maximising a
   linear value picks an end of it. Which is how bowling actually works — the
   plan is chosen for the batter in front of you — so the frontier sets the
   magnitudes and the counters do the deciding. It does mean bowling
   `defensive` is the best reply in only 1.2% of cells.
2. **The counters are cricket, not measurement.** Cricsheet records the
   bowler and the batter, not the plan either of them chose, so no
   approach-versus-approach effect in this engine is empirical. The
   magnitudes are bounded and the *consequences* are tested (nobody
   dominant, everybody useful); the individual numbers are judgement.
3. **The captain-choice mix is from `/pitchstats`, not from real cricket.**
   Mean-neutrality is therefore relative to how CCM26 captains pick, which is
   the right reference for keeping *this* engine on its anchor but is not a
   measured feature of real T20.
4. **Weather and dew remain unmeasured**, as in v1. Direction evidenced,
   magnitude inherited.
5. **Pitch ordering is CCM26's model, not data.** Only the level is fitted.
   Per-venue samples in the in-repo aggregates are not adequate for a venue
   layer, so there is none.
6. **The win-probability model is coarse.** A normal approximation on the run
   rate with a wicket-survival table, deciding between five buttons. It is a
   large improvement on a linear function of runs and is not a simulator.
7. **`/sim` does not use approaches at all** — it has no approach UI — so
   this change reaches `/cipl` and `/letsplay`. `/sim` still gets the v1 ball
   model, the de-overlapped conditions and the identity fix.
8. **Ultra Attack is still the most frequently correct batting call** (55.7%
   of cells). That is defensible for modern T20 and it is not dominant, but
   it is not an even spread either.
