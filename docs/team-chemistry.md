# Team Chemistry

Design note for the Playing XI chemistry system. Covers an evaluation of the
proposed pair-linking rules, the problems that evaluation surfaced, and the
recommended system that replaces it.

Reference implementation: `services/chemistry.py`. Tests: `tests/test_chemistry.py`.
Every number in this document is produced by that module, not by hand.

---

## 1. Summary

Total chemistry is **100**, split **80 country / 20 special cards**, as briefed.

The proposed scoring — *every same-country pair is a link, each link is worth 5,
cap at 80* — does not survive evaluation. Pair counting grows quadratically while
the cap does not, so the system overshoots its own ceiling so hard that chemistry
stops being a decision, and the cheapest route to a perfect score turns out to be
stacking one nation and ignoring the rest.

The recommendation keeps the brief's structure (100 total, 80/20 split, max 7 per
country, the "blocks of countrymen" mental model) and replaces the pair formula
with a **concave block table**, plus an **Icon rule** that keeps legends from
smaller nations playable.

---

## 2. Evaluating the proposed system

`Links = N × (N−1) ÷ 2` per country, 5 points per link, capped at 80.

| Combo | Links | Raw points | Final | Reaches 80? | Points wasted |
|---|---:|---:|---:|:---:|---:|
| 7-4 | 27 | 135 | 80 | yes | **55** |
| 7-3-1 | 24 | 120 | 80 | yes | 40 |
| 7-2-2 | 23 | 115 | 80 | yes | 35 |
| 6-3-2 | 19 | 95 | 80 | yes | 15 |
| 5-4-2 | 17 | 85 | 80 | yes | 5 |
| 5-3-3 | 16 | 80 | 80 | yes | 0 |
| 4-4-3 | 15 | 75 | 75 | **no** | 0 |
| 3-3-3-2 | 10 | 50 | 50 | **no** | 0 |

An XI of 11 with a 7-per-country ceiling has **49** possible shapes. **15** of
them reach 80. The threshold is 16 links; the maximum available is 27.

### 2.1 Four structural problems

**a. The cap is reached so early that chemistry stops mattering.** A 7-4 squad
generates 135 points against an 80 ceiling. Those 55 surplus points are slack:
the manager can swap almost any card for any other and the displayed chemistry
never moves. A stat that reads 80 no matter what you do is not a mechanic, it is
a decoration.

**b. The cheapest path to a perfect score is the least diverse one.** This is the
serious one. Under pair counting, **7-1-1-1-1 scores a full 80** — seven Indians
and four completely unconnected cards. A system introduced to reward squad
cohesion instead pays you to stack one nation and treat the other four slots as
free. The rule intended to produce variety produces the opposite.

**c. Diversity is not merely disfavoured, it is disqualified.** No shape whose
largest block is 4 or smaller can reach 80 — the arithmetic makes it impossible.
4-4-3, a genuinely well-balanced three-nation squad, is capped at 75 forever,
while 7-2-2 gets a perfect score. Every competitive squad is therefore forced to
contain a block of at least 5.

**d. The "at least 2 different countries" rule can never fire.** With 11 players
and a maximum of 7 from one country, a second country is already forced by
arithmetic. The rule is unreachable, so it is UI noise — a constraint players
must read and can never violate.

---

## 3. Recommended system

### 3.1 Country chemistry (0–80)

Score each national **block** from a table instead of counting pairs:

| Players from one country | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Chemistry** | 0 | 8 | 18 | 30 | 40 | 46 | 50 |
| *marginal gain* | — | +8 | +10 | **+12** | +10 | +6 | +4 |

**Country Chemistry = min(80, sum of every block's value).**

The marginal value of the Nth countryman rises to a peak at the **4th** and then
falls away. Three consequences, all deliberate:

- **A national core of four is the sweet spot** — which is how cricket squads
  genuinely read: a top order, a pace battery, a spin pairing.
- **One country tops out at 50**, i.e. 62% of the 80. No single nation can carry
  an XI, so every competitive squad needs at least two real blocks. This is what
  the dead "2 countries minimum" rule was trying and failing to do — enforced by
  the curve instead of by a constraint.
- **Concentration past 5 is actively poor value.** The 6th and 7th countrymen
  return +6 and +4, less than the 2nd player of a fresh nation (+8).

### 3.2 The same combinations, rescored

| Combo | Proposed system | **Recommended** | Change |
|---|---:|---:|---:|
| 7-4 | 80 | **80** | — |
| 6-5 | 80 | **80** | — |
| 5-5-1 | 80 | **80** | — |
| 5-4-2 | 80 | **78** | −2 |
| 4-4-3 | 75 | **78** | **+3** |
| 5-3-3 | 80 | **76** | −4 |
| 6-3-2 | 80 | **72** | −8 |
| 7-3-1 | 80 | **68** | −12 |
| 7-2-2 | 80 | **66** | −14 |
| 3-3-3-2 | 50 | **62** | +12 |
| **7-1-1-1-1** | **80** | **50** | **−30** |

The headline result: **4-4-3 (78) now beats 7-3-1 (68)**, and the stack-plus-filler
exploit collapses from 80 to 50. Only three shapes reach a full 80 (7-4, 6-5,
5-5-1) and **seven of the 49 shapes land within 5 points of it**. Diversity costs
about 2 points — enough to be a real trade-off, small enough that players build a
squad they like rather than copying one meta build.

### 3.3 The Icon rule

A concave table still pays a lone countryman nothing, which would quietly delete
every legend from a smaller cricket nation. A single Brian Lara, Richard Hadlee or
Mahela Jayawardene in an XI would be pure chemistry dead weight.

> **An Icon counts as two players of its country when sizing the block** — but
> still only one against the 7-per-country squad limit, and block value is still
> never read past 7.

| Squad | Without Icon rule | With Icon rule |
|---|---:|---:|
| Lara alone (WI) | 0 | **8** |
| Lara + 1 West Indian | 8 | **18** |
| Lara + 2 West Indians | 18 | **30** |
| Icon inside an existing 7-block | 50 | 50 (*no change*) |

Because the table is steep at the bottom and flat at the top, the bonus lands
exactly on the tail and is **worth nothing to the meta**. It is self-balancing:
it cannot be stacked, because the block it would inflate is already at its
ceiling. Lara stops being a luxury and becomes the efficient way to open a second
front.

### 3.4 Special chemistry (0–20)

Two components, deliberately weighted towards **variety over quantity**:

```
Variety :  3 points per distinct special type present   (max 5 types → 15)
Depth   :  1 point  per special card in the XI          (max 5 cards →  5)
Special Chemistry = min(20, variety + depth)
```

Types, in prestige order: **Icon, TOTY, Prime, Legend, Event**. (Event is the
catch-all for named drops — World Cup, IPL, Ashes, Star, Gold — so a new edition
scores the day it ships, with no config change.)

| XI composition | Special chemistry |
|---|---:|
| All base cards | 0 |
| One Legend | 4 |
| **Eleven Icons** | **8** |
| **One each of Icon / TOTY / Prime / Legend / Event** | **20** |

This is the part that keeps the system from being pay-to-win. Under a naive
"points per rare card" rule, the 20 goes to whoever owns the most Icons — the
scoreboard just re-reports wallet size. Here **the whale with eleven Icons scores
8 and the collector with five varied cards scores 20**, and demand is pushed
across the whole special-card market instead of piling onto the top tier.

### 3.5 What chemistry does in a match

Chemistry is a **tie-breaker, not a substitute for card quality**:

```
bonus % = 3.0 × (chemistry / 100)      →  0% at 0 chem, 3% at 100 chem
```

Two rules matter more than the number:

- **It is a bonus band, never a penalty.** A 0-chemistry XI plays exactly at its
  raw ratings. New players fielding whatever they pulled are not taxed; they are
  simply not yet earning the bonus. A penalty model would make the first week
  feel broken.
- **3% is small on purpose.** Chemistry should decide close matches between
  similar squads, never let a weak squad beat a strong one. If chemistry can
  overturn a rating gap, it stops being a fun optimisation and becomes a
  mandatory tax on squad-building.

Wiring `chemistry_bonus_pct()` into `services/probability_engine.py` alongside the
existing trait deltas is the follow-up step; the calculator is engine-agnostic
and ships standalone.

---

## 4. Analysis

### 4.1 Does it create diverse squads?

Yes, and measurably so. Under the proposed system every 80-point squad required a
block of 5+ and 7-1-1-1-1 was optimal. Under the recommendation, three different
shapes reach 80, seven land within 5 points, and a three-nation 4-4-3 outscores a
seven-stack. Concentration past the 5th countryman is worse value than opening a
new nation, so the curve does the work that a hard rule could not.

### 4.2 Does it stop everyone using the same countries?

Partly — and the remaining pressure is a **catalogue** problem, not a formula
problem. Checked against the live pool (`data/players.json`, 1,329 cards, 23
nations after alias folding):

| Nation | Cards | | Nation | Cards |
|---|---:|---|---|---:|
| India | 293 | | Ireland | 49 |
| Australia | 180 | | Scotland | 48 |
| Netherlands | 97 | | Zimbabwe | 24 |
| Sri Lanka | 92 | | Namibia / Nepal | 23 each |
| South Africa | 84 | | Bangladesh | **14** |
| New Zealand | 83 | | Hong Kong | 10 |
| West Indies | 76 | | Oman | 6 |
| Afghanistan | 73 | | Papua New Guinea | 5 |
| England | 56 | | United States | **2** |
| Pakistan | 55 | | Trinidad and Tobago | **1** |

**21 of 23 nations can field a block of 4**, so the sweet spot is reachable
almost everywhere — the curve is not the bottleneck. But India alone is 22% of
the pool, so Indian blocks will still be the *easiest* to assemble simply because
players own more Indian cards. That is a drop-rate lever, not a chemistry lever:
if you want fewer Indian cores, weight packs, not the formula.

Bangladesh at 14 cards is the notable gap — a full Test nation that is harder to
build a core from than Namibia. Worth a catalogue pass.

### 4.3 Impact on the transfer market

The proposed system would have been actively bad for the economy. Quadratic
scaling concentrates all demand on whichever nations have the deepest card pools;
prices for Indian and Australian cards inflate, and cards from the other ~19
nations fall to the floor price with no buyers. In a collectible game that means
roughly 40% of every pack opened feels like nothing, which is a retention problem
long before it is a balance problem.

The recommendation spreads demand three ways:

- **A block of 4 is the sweet spot**, so the 4th Sri Lankan is worth more (+12)
  than the 7th Indian (+4). Mid-tier nations gain real, permanent bid support.
- **The 62% ceiling per nation** guarantees that every squad is shopping in at
  least two national markets at all times.
- **Variety-weighted special chemistry** creates demand for *cheap* special cards.
  A player holding four Icons still needs a Prime and a TOTY, and the cheapest
  card of a missing type is worth more to them than a fifth Icon. That puts a
  price floor under the entire special-card catalogue rather than just the top.

Net effect: more liquid two-sided markets, fewer dead cards, and rarity that
still commands a premium on playing strength without also monopolising chemistry.

### 4.4 Do legends from smaller nations stay valuable?

Under the proposed system, no — and this was its worst outcome. A lone Lara in a
7-3-1 contributes exactly 0 chemistry, so he is strictly worse than a mediocre
card from your main block. Every legend from West Indies, Sri Lanka, New Zealand
and Zimbabwe becomes a trophy you cannot field.

Under the recommendation they are fine, via two independent routes:

1. **The Icon rule** makes a lone Icon worth 8 and a 3-man core worth 30 — the
   same as a 4-man block of anyone else. Lara *is* the fourth West Indian.
2. **Variety-weighted special chemistry** means an Icon is often the missing type
   in your set, worth more than yet another card of a type you already hold.

The live pool already supports this: West Indies has 11 special cards, more than
England (8) or Pakistan (9). The cards to build around exist.

### 4.5 Exploits and balance issues

| # | Exploit | Status |
|---|---|---|
| 1 | **7-1-1-1-1 scores a perfect 80** under pair counting | **Closed** — scores 50. The curve pays nothing for singletons. |
| 2 | **Cap slack** — 55 wasted points make swaps free at 7-4 | **Closed** — max raw is 80 + 20, so every swap moves the number. |
| 3 | **Icon stacking** — buy Icons to inflate a big block | **Closed by construction** — block value is never read past 7, so an Icon in a 6/7-block adds 0. |
| 4 | **Pay-to-win specials** — buy the 20 with rare cards | **Closed** — eleven Icons score 8; variety, not spend, drives the score. |
| 5 | **Short-XI farming** — field only well-connected cards | **Closed** — a short XI is pro-rated by `n/11` and floored, so it can never outscore a full one (pinned by test). |
| 6 | **Split blocks from spelling variants** | **Closed** — `COUNTRY_ALIASES` folds them (see 5.2). |
| 7 | Deep-pool nations are easier to build cores from | **Open, by design** — a drop-rate lever, not a formula one (see 4.2). |
| 8 | Multi-national eligibility (e.g. a player who represented two nations) | **Open** — one country per card today. Flagged for the catalogue, not the formula. |

The residual risk worth watching is **item 7**: chemistry is fair, but card
*supply* is not, and supply is where any remaining pressure toward Indian cores
now lives. That is the right place for it — it is tunable per pack, per season,
without ever touching the scoring rules.

---

## 5. Edge cases

### 5.1 Handled by the calculator

| Case | Behaviour |
|---|---|
| Short XI (forfeit, incomplete lineup) | Scored on the players present, then pro-rated by `n/11` and floored. Never outscores a full XI. |
| Empty XI | Returns 0, no exception. |
| Missing / blank country | Folded into one `"Unknown"` block and surfaced in the breakdown so an admin can spot bad rows — rather than silently scoring 0. |
| 8+ from one country | `validate_country_rule()` rejects it; scoring still caps the block at 7 so a bad squad cannot crash a match. |
| Icon inside a maxed block | Adds 0 — effective size is clamped to 7. |
| Two versions of one cricketer | Already blocked upstream by `validate_roster_xi()` in `services/xi_rules.py`. |

### 5.2 Two live data problems this surfaced

Both were found by running the calculator against `data/players.json`, and both
are handled in code:

**Version strings carry a "card" suffix.** The live pool uses `"Base card"`,
`"Legend card"`, `"Star Card"` — not `"Base"`/`"Legend"`. A naive exact match on
`"base"` sends all **1,228 base cards** into the Event catch-all, handing every
squad free special chemistry. The matcher strips the noise suffix first.

**Ireland is spelled two ways.** The pool carries both `"Ireland Republic"` (46
cards) and `"Republic of Ireland"` (3). Untreated, those score as two separate
nations and split a player's block — costing them real chemistry for a
data-entry difference they cannot see or fix. `COUNTRY_ALIASES` folds them, along
with the other variants likely to appear (`UAE`, `USA`, `Windies`, `Holland`, …).

*Recommendation:* also fix the source rows so the admin UI shows one Ireland.
The alias map should be a safety net, not the only thing holding the data together.

---

## 6. Rollout and balancing notes

**1. The catalogue only reaches 11 of the 20 special points today.** The live pool
contains just two non-base editions (Legend, Star). Maximum reachable special
chemistry is therefore `3+3 variety + 5 depth = 11/20`, so a realistic strong
squad today scores **89**, not 100. Before launch, either ship Icon / TOTY / Prime
editions, or temporarily rescale variety to `5 points × 3 available types` so the
20 is honestly achievable. **Do not launch a visible 0–100 stat whose top 9 points
are unreachable** — players will read it as broken.

**2. Ship the table, not the formula.** Casual players will not compute a curve.
The XI screen should show the block table as a small chart, and each block as
`🇮🇳 India ×4 — 30`. The formula belongs in this document; the table belongs in
the app.

**3. Show the next step, not just the score.** The highest-value UI element is a
single hint: *"+12 — add a 4th South African."* That teaches the entire curve
without a tutorial, because the sweet spot becomes visible exactly when it is
actionable.

**4. Drop the "minimum 2 countries" rule.** It cannot fire (§2.1d). One hard rule
survives: **max 7 from one country**.

**5. Season-tune the catalogue, never the curve.** If one nation dominates the
meta, adjust pack weighting and which nations get new editions. Re-tuning the
block table invalidates every squad players have built, which is the fastest way
to lose a competitive playerbase.

**6. Suggested telemetry.** Track the distribution of squad shapes, median
chemistry, and the win-rate delta between the 90+ and 60− bands. If high-chemistry
squads win materially more than ~3% above baseline, the bonus is too strong. If
one shape exceeds ~40% usage, the curve has an unintended peak.

---

## 7. Reference

`services/chemistry.py` — pure standard library, no Telegram or SQLAlchemy
imports, matching the house style of `services/xi_rules.py` so the match engine,
Mini App, bot XI builder and tests can all share it.

```python
from services import chemistry

report = chemistry.calculate_chemistry(players)   # any objects with .country/.version
report["total"]    # 0-100
report["country"]  # 0-80
report["special"]  # 0-20
report["shape"]    # "7-4"
report["blocks"]   # per-country breakdown for the UI
```

`tests/test_chemistry.py` — 29 tests pinning the curve, the published
combination table, the Icon rule, variety-over-quantity, the closed exploits and
the live-data edge cases.
