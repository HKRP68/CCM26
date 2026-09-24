# Pitch types

The eight surfaces, what each one does, and — more usefully — which of the
things the bot used to believe about them were not true.

Every fact about *which* surfaces exist and *what each one is* now lives in
`engine/pitch_registry.py`. The numbers live where they already did:

| Layer | What it owns |
|---|---|
| `engine/pitch_registry.py` | Which surfaces exist, and their cricket identity |
| `config/ground_conditions.yaml` (+ `_defaults`) | The ball-by-ball model: scoring matrix, who takes the wickets, par band, chase grid |
| `engine/pitch_state.py` | How the surface changes between innings |
| `engine/approach_modifiers.py` | Which captaincy calls the surface rewards |
| `services/probability_engine.py` | The same cricket for the manual `/cric` engine |

`tests/test_pitch_registry.py` fails if any of those gains, loses, or disagrees
about a surface.

## The eight

Ordered rankest bowling surface → deadest road. Par bands are the first-innings
median from `config/ground_conditions.yaml`, measured with
`python -m tools.pitch_calibration`.

| Pitch | Modelled on | Par | Favours | Toss (day) |
|---|---|---|---|---|
| **Dusty** | Chepauk / Kanpur — a raging, bare turner | 152–165 | Spin | bat |
| **Green** | Wellington / Headingley — a grassed-up seaming top | 165–178 | Pace | bowl |
| **Dry** | Abu Dhabi / Kandy — slow, low, gripping | 170–182 | Spin | bat |
| **Bouncy** | Perth / the Wanderers — steep, true bounce | 180–192 | Pace | bowl |
| **Even** | a neutral international T20 square | 190–202 | Balanced | bat |
| **Hard** | a modern drop-in with real carry | 212–226 | Batting | bat |
| **Flat** | Wankhede / Chinnaswamy — a genuine road | 225–240 | Batting | bowl |
| **Dead** | Sharjah in its shirtfront years | 242–258 | Batting | bowl |

`Dead` is fully configured but is not offered when a host picks a pitch: a
guaranteed 250 is a novelty, not a contest.

Under lights the day column does not apply — dew in the second innings makes
bowling first correct on every surface, which is why `correct_toss_choice_dn`
exists rather than being a copy.

## Three separate questions

A surface is not one number. Reading a profile means reading three:

**How many runs is a ball worth here** — the scoring matrix's run buckets. This
is the surface's stroke-making character, and the shape matters as much as the
total. A turner and a green top are both low-scoring, but for opposite reasons:
on the turner a single is always on and a boundary almost never is (the highest
share of ones in the game, the lowest share of boundaries); on the green top you
cannot work it around at all, so what runs there are arrive in fours.

**How often does a wicket fall** — the matrix's `Wicket` bucket, which now
carries the aggregate and rises monotonically as the surface gets ranker.

**Who takes those wickets** — `wicket_factors`, by bowling type. This is where
a surface's identity actually lives, and the table is deliberately opinionated:

* **Green** pays the *fast-medium* swing bowler (1.55) more than the express
  quick (1.42). A green top is a swing bowler's pitch first.
* **Bouncy** inverts that: express pace (1.58) well clear of everything else,
  and the one surface where a wrist spinner's bounce (0.90) beats a finger
  spinner's turn (0.72).
* **Dusty** pays every spinner (1.46–1.62) and the quicks almost nothing —
  except that the *medium* pacer (0.82) does better than the express one
  (0.68), because cutters into the footmarks are the only pace plan that works.
* **Flat** and **Dead** pay nobody, with wrist spin the last thing that still
  buys a wicket.

Every surface prices all seven bowling types the engine knows, so nobody lands
on `default`.

## What was wrong

This is the part worth keeping. Each of these shipped.

### The engines did not agree on which pitches exist

Six tables, four different answers. `services/match_constants.PITCH_TYPES`
offered seven surfaces; `services/probability_engine.PITCH_MODS` knew a
*different* seven — it had `Dead`, which is not selectable, and had no `Bouncy`,
which is. Its lookup ended `else "Flat"`, so a captain who picked the bounciest
deck in the game got the flattest road in the table. `data/ground_conditions.yaml`
was a stale five-surface copy of the config, with its own contradictory numbers,
and it was what `/sim` read.

### The green top was the easiest pitch in the game to bat on

`run_factor` reads like a run multiplier. It is not: `ball_outcome` multiplies it
into every *non-wicket* bucket and then normalises, so the only thing it changes
is how often a wicket falls. Green carried 2.10 — the highest of all eight,
above `Dead`'s 1.52 — which meant the seaming green top shed wickets faster than
a shirtfront. Measured over the attack, before this change:

```
        pace W/120   spin W/120
Dead        1.70        1.70
Flat        2.46        2.46
Hard        2.93        2.55
Green       3.14        1.70   ← the seamer's pitch, 4th of 8 for a quick
Even        3.14        3.11
Dry         3.39        4.90
Bouncy      3.76        2.73
Dusty       4.24        5.16   ← a raging turner, best pitch in the game for pace
```

A fast bowler took more wickets on a raging turner than on a green top or a
bouncy deck. A spinner on a green top was exactly as harmless as on a
shirtfront. The calibration harness had been reporting the consequence for a
while without anyone reading it that way: Green had the *lowest* capitulation
rate (12%) and the *highest* chase-win rate (75%) of any surface.

`run_factor` is now a plain batting-friendliness index ordered with the par
bands (0.86 → 1.22), the aggregate wicket rate moved into the matrix where a
reader looks for it, and the pace/spin split is carried by `wicket_factors`:

```
        pace W/120   spin W/120
Dusty       2.96        6.06
Green       5.15        2.40
Dry         3.22        4.91
Bouncy      3.82        2.39
Even        3.06        3.04
Hard        2.86        2.46
Flat        2.45        2.33
Dead        1.66        1.76
```

The rescale fixes the super-over engine for free. `super_over_outcome` blends
`run_factor` against player skill additively (`0.4 * skill + 0.6 * pitch`), so a
2.10 made a super over on a green top a run-fest; near 1.0 the blend is on the
same scale as the wicket branch it sits beside.

### Par had moved and half the bot had not been told

Unified T20 Engine v3.0 recalibrated par (`docs/unified-t20-engine-v3.md`,
section 5A). `engine/format_config.py` still carried the numbers from *before*
that pass — `target_scores` was, line for line, the "Was" column of that doc's
own table. So the engine produced ~168 on Green while every consumer of par —
GSME, the pressure engine, the DLS curve — judged it against 123, and read every
Green innings as forty-five runs ahead of par from the first over.

`engine/game_state_engine.py` was worse in a quieter way. Its dispatch read
`if fmt is not None and fmt.name != "T20"`, so a T20 match *never* used the
FormatConfig it was handed and always fell back to a local five-surface table.
Dusty, Bouncy and Even had no row in it, so all three were judged against Hard —
a side 140/4 after 16 on a turner read as badly behind par.

Both now derive from one place, and the T20 branch uses its own config.

### The bot told you to bat and to bowl on the same pitch

`services/pitch_report._PITCH_TOSS` said `Flat: "bat"`.
`engine/format_config.correct_toss_choice` said `Flat: "bowl"`. Both were shown
to players. Every toss table is now built from `pitch_registry.toss_call`.

### Surfaces that never wore, and boosts that missed

`ball_outcome._apply_pitch_wear` covered Dry, Green, Hard, Flat and Dead — so
the raging turner did not deteriorate at all. The ListA wear model had the same
gap, plus `LISTA_RUN_FACTORS` and `LISTA_WICKET_PITCH_MULT` were missing Dusty,
Bouncy and Even entirely: a 50-over match on a turner defaulted to Hard's wicket
rate and a run factor of 1.0, which sat *above* the neutral Even (0.92).
`LISTA_RUN_FACTORS` also had Flat (1.21) above Dead (1.18) while Dead's own
target sat twenty runs above Flat's.

The death-overs boundary boost chose between a batting and a bowling multiplier
with `if pitch in ("Flat", "Dead", "Hard") ... else:  # Green or Dry`. The
comment was wrong: that `else` also swallowed Dusty, Bouncy and Even. It is now
`pitch_registry.favours(pitch) == "Batting"`.

### Chase grids priced on totals that no longer meant anything

Section 9's chase grid is keyed on the absolute first-innings total, and it was
never rebased when section 5A moved par. The 50% crossings ended up like this:

| Pitch | Par | Grid crossed 50% at | Off by |
|---|---|---|---|
| Green | 171 | ~218 | +47 |
| Dusty | 158 | ~200 | +42 |
| Dry | 176 | ~200 | +24 |
| Bouncy | 186 | ~210 | +24 |
| Even | 196 | ~213 | +17 |

So a side chasing 190 on a raging turner — thirty runs above par — was handed a
58% baseline, and that baseline is live: `cipl_match` feeds it into
`_chase_baseline_effects`, which tilts the whole chase. Each grid is now a
logistic in runs *relative to that pitch's own par*, so the crossing sits at par
on every surface, and the win% at par is the surface's innings-2 character —
44% on a turner whose footmarks deepen, 57% on a green top whose grass burns
off. That is deliberately the same story `engine/pitch_state.py` tells.

**The engine still chases more freely than the grid asks**, by roughly 10-15
points at and above par. That is the pre-existing residual
`docs/unified-t20-engine-v3.md` describes under "Where the doc disagrees with
itself" and `CHASE_INTRINSIC_BIAS` exists to absorb; it is a property of the
Fighting Match rule, not of any pitch, and it was not chased further here.

## Calibration

`python -m tools.pitch_calibration --n 300`, before and after. Par is the
primary target, and every sweep surface now sits at or inside its spec band —
Green and Hard were below theirs before:

| Pitch | Spec par band | Measured | Was |
|---|---|---|---|
| Flat | 225–240 | 243 | 228 |
| Hard | 212–226 | 227 | 211 |
| Even | 190–202 | 195 | 201 |
| Bouncy | 180–192 | 188 | 180 |
| Dry | 170–182 | 178 | 178 |
| Green | 165–178 | 173 | 164 |
| Dusty | 152–165 | 159 | 162 |

Flat (243) and Hard (227) land a few runs above their band tops, inside the
harness's ±10 par tolerance; both were a few runs below before.

`Dead` has no row for the same reason it has never had one: it is not in
`SPEC_PITCHES`, so nothing measures it.

### What the sweep still reports, and why

The overall check count went from 105/112 to 100/112, and the whole difference
is on two surfaces:

* **Flat gained three** (13/16 → 16/16), all of them chase bands the rebased
  grid now prices correctly.
* **Green lost five and Bouncy three**, on their capitulation rate and their
  above-par chase bands.

Green is the honest cost of the fix. The old Green produced an 18% capitulation
rate and a 26-run median defended margin — the lowest of any surface — because
nobody ever got out on it. It is now a boom-or-bust surface by design, and a
boom-or-bust surface produces some one-sided matches, which is exactly what the
Fighting Match rule counts. The alternative is a green top where a seamer is no
more dangerous than on a road, which is where this started.

The above-par chase bands are the pre-existing engine residual described above:
`_chase_baseline_effects` is a bounded nudge and cannot pull the engine all the
way down to a grid centred on par. Capitulation was already over the harness's
30% cap on four of seven surfaces before any of this.

## Adding a surface

1. Add a `PitchProfile` to `engine/pitch_registry.py` in par order.
2. Add its profile to **both** `config/ground_conditions.yaml` and
   `config/ground_conditions_defaults.yaml` — scoring matrix, all seven
   `wicket_factors`, `run_factor`, `scoring_dynamics`, `chase_win_pct`.
3. Add its rows to `engine/pitch_state.INNINGS2_EVOLUTION` / `EVOLUTION_NOTES`,
   `engine/approach_modifiers._PITCH_RULES`, `services/probability_engine`'s
   `PITCH_MODS` / `PITCH_BOWLER_SYNERGY` / `_pitch_wear_mods`, and the T20 and
   ListA tables in `engine/format_config` and `engine/ball_outcome`.
4. Run `pytest tests/test_pitch_registry.py` — it names every table you missed.
5. Run `python -m tools.pitch_calibration --pitch <name> --n 300` and tune the
   matrix until par lands in its band.

## Toss and wicket rebalance (Dusty, Green, Dry, Bouncy, Flat)

`/pitchstats` showed the toss deciding matches on five surfaces: batting first
won 58% on Dusty and 60% on Flat, and only 37% on Green, 41% on Dry and 32% on
Bouncy. Each pitch was also losing 0.31-0.36 wickets an over. Three layers
caused the split, and each one was changed:

* **Innings-2 drift.** `_apply_pitch_wear` and `pitch_state.INNINGS2_EVOLUTION`
  both pushed the same way on each surface: the turners got much harder for
  the chase, while the green top and the bouncy deck got much easier. Both
  layers are now small, so each pitch keeps its flavour without handing the
  match to one side.
* **Chase grids.** They are still a logistic around each pitch's own par, but
  gentler (scale 40 rather than ~25), so a total 20 runs off par no longer
  swings the steer by 20 points. The grid no longer bakes in any surface
  character; a per-pitch offset of a few points absorbs the engine's residual.
* **Wickets.** Lower `Wicket` weights in the scoring matrices (the weight
  moves to dots, not boundaries) and smaller extreme `wicket_factors`. A new
  `phase_boosts.death_overs.wicket_boost_by_pitch` softens the death on these
  five pitches only. Par bands, `format_config` par factors, target scores
  and RRR baselines moved to match.

Even and Hard are deliberately left as they were.

`python -m tools.pitch_calibration --mix real` plays each over with approaches
drawn from what real captains pick, and it now reports bat-first win % and
wickets per over. Measured at n=1000 per pitch:

| Pitch | Bat-first win % (balanced / real mix) | Wickets/over, real mix (was) |
|---|---|---|
| Dusty | 53 / 51 | 0.316 (0.363) |
| Green | 50 / 48 | 0.340 (0.352) |
| Dry | 54 / 53 | 0.323 (0.387) |
| Bouncy | 50 / 51 | 0.291 (0.329) |
| Flat | 50 / 51 | 0.225 (0.233) |

Green's wicket cut is the smallest. The bot captain's approach solver
(`services/bot_tactics`) reads Green's matrix directly, and cutting its base
wicket rate further leaves Balanced batting and Aggressive bowling with no
situation where they are the right call (`tests/test_cipl_approach.py`).
