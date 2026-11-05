# Trait balance audit — 5 November 2026

The batting and bowling shares remain **50% each**. This pass fixes role
leakage and upgrade plateaus, and records a repeatable match audit rather than
assuming that equal probability weights imply equal match value.

## Changes assessed

- Every one of the 43 catalogue effects has explicit action roles. Batting
  effects cannot help or hurt a bowler, and bowling effects cannot increase
  their owner's dismissal risk while batting. Four shared traits keep both
  role-specific branches.
- Only active, allowed traits consume diminishing-return slots. Unknown,
  wrong-role, inactive and failed effects cannot weaken another active trait.
- The combined absolute strength uses the smooth ceiling
  `25 × S / (25 + S)` before the role's half share. This stays below 25%,
  preserves effect directions and tradeoffs, and keeps upgrades effective
  after the old hard cap would have flattened them.
- The calibrated chase controller reads actual trait levels and strongest-first
  stack weights. It previously assigned the same clutch bonus to Lv.1 and Lv.5.
- Activation summaries use the same role rules as the probability engine.

The four focused traits were assessed individually. Their characteristic
tradeoffs and activation windows remain intact; their actual strengths change
through the smooth curve and role enforcement. These are the raw weight changes
on a delivery where the trait activates, after the 50% share and before outcome
normalization:

| Trait | Outcome | Lv.4 | Lv.5 | Review decision |
|---|---|---:|---:|---|
| Wicket Hunter | Wicket | +5.556% | +6.490% | Keep the always-on wicket focus; prevent self-harm when batting. |
| Anchor | Wicket | −5.000% | −5.745% | Keep survival plus the six-hitting tradeoff; restrict both to batting. |
| Finisher | Six | +4.545% | +5.153% | Keep the final-three-over window; restore progression in both probability and chase layers. |
| Death Specialist | Six | −4.545% | −5.153% | Keep death-over boundary suppression and wicket support; restrict both to bowling. |

The smooth curve reduces some previously clipped maximum effects. Its benefit
is continuous, bounded progression rather than a flat upgrade. No rarity,
price, inventory or displayed card-rating bonus is changed.

## Match audit method

Command:

```sh
python -m tools.trait_balance --n 100 --pitches Flat Even Green --levels 1 4 5 \
  --output /tmp/trait-balance.json --csv /tmp/trait-balance.csv
```

This runs **6,600 complete T20 matches**, with seed 20261105. Each row has
100 matches, grouped into 50 seed pairs. In each pair A bats first once and
second once against an identical-rated neutral XI. Team A alone carries the
tested loadout. The no-trait controls are reused across levels and produce
50% win share by the mirrored construction. Ties count as half a win.

The four main profiles are neither, five batting traits, five bowling traits
and both loadouts. The four focused profiles equip five copies of one named
trait on eligible cards. The combined profile has ten traits and therefore
has a larger investment than either single-role profile.

The harness uses the production LetsPlay over loop, innings transition,
legal quotas and calibrated chase controller. Both teams use balanced
approaches and rotate the five regular bowlers evenly. Conditions use baked-in
defaults; commentary is disabled. No database or live match setting is written.
A pilot using a greedy bowling order exhausted quotas early and was excluded
from the recorded comparison.

## Recorded results

Win share with approximate 95% intervals; each cell represents 100 matches.

| Pitch | No traits | Batting only | Bowling only | Both |
|---|---:|---:|---:|---:|
| Flat | 50.0% (50.0–50.0) | 49.0% (41.2–56.8) | 54.5% (48.4–60.6) | 47.5% (37.8–57.2) |
| Even | 50.0% (50.0–50.0) | 47.5% (40.6–54.4) | 46.5% (38.8–54.2) | 45.0% (37.9–52.1) |
| Green | 50.0% (50.0–50.0) | 45.5% (39.3–51.7) | 53.5% (46.1–60.9) | 53.5% (44.9–62.1) |

| Pitch | Anchor | Finisher | Wicket Hunter | Death Specialist |
|---|---:|---:|---:|---:|
| Flat | 46.0% (39.2–52.8) | 51.5% (48.0–55.0) | 55.0% (48.0–62.0) | 50.5% (47.5–53.5) |
| Even | 57.0% (50.7–63.3) | 50.0% (46.0–54.0) | 53.5% (46.6–60.4) | 48.0% (45.3–50.7) |
| Green | 50.5% (42.3–58.7) | 48.0% (45.3–50.7) | 48.0% (40.6–55.4) | 51.5% (47.0–56.0) |

Correct-role activation rates for the focused Lv.5 profiles, pooled across the three pitches:

| Trait | Activated deliveries | Opportunities | Activation rate |
|---|---:|---:|---:|
| Anchor | 30,832 | 30,832 | 100.0% |
| Finisher | 2,861 | 30,476 | 9.4% |
| Wicket Hunter | 35,697 | 35,697 | 100.0% |
| Death Specialist | 4,809 | 35,784 | 13.4% |

**Wrong-role activations: 0 across the full audit.** The phase specialists
activate much less often than the always-on traits, so their average match
impact should be interpreted alongside their activation opportunities.

The single-role loadout intervals overlap on all three surfaces. These samples
do not support a further role-wide buff or nerf. Anchor shows a positive effect
on Even in this seed set; a positive result against a trait-free control is an
expected benefit of equipping traits, not a reason on its own to nerf one.
The raw handler coefficients and phase windows are therefore retained while
the role, progression and clutch-level fixes are applied.

## Interpretation and validation

The committed [CSV](trait-balance-audit.csv) contains every pitch, level and
profile, including runs, wickets, ties, activation counts, wrong-role
activations and confidence bounds. The table above reports selected Lv.5 win
shares; the CSV also records Lv.1 and Lv.4.

Intervals are approximate 95% intervals across seed pairs, specific to each
row, without a correction for multiple comparisons. Small observed win-rate
differences remain exploratory. This audit does not establish equal value for
every trait or force identical teams to have equal results in every match.
Further coefficient changes should use larger, independent samples and other
approach or conditions presets, rather than fitting these particular seeds.

Validation covers all 43 effects in their supported roles, wrong-role
suppression, every active effect's Lv.4-to-Lv.5 progression, upgrades in legal
three-category stacks, inactive stack slots, matching opposing effects,
scale-independent match hooks, level-aware chase bonuses and reproducible
mirrored match audits. Existing trait, chase, bot and match-flow suites also
remain part of the checks.
