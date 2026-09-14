# Trait Rating Boost: what an equipped trait is worth on the card

Design note for the rating half of the trait system.

Reference implementation: `services/trait_rating_service.py` (the reckoning),
`config.py` (the ladder and its ceilings), `services/player_stats_service.py`
(the fair-match gate it is kept out of), `handlers/letsplay.py` (the Playing XI
card), `handlers/cipl_play.py` (the live board), `handlers/traits.py`
(`/traitboost`).
Tests: `tests/test_trait_rating_boost.py` (the reckoning),
`tests/test_letsplay_trait_boost_card.py` (the cards).

---

## 1. The problem

A trait has always changed the *simulation* and never the *number on the card*.
`services/trait_engine.py` nudges the per-ball probability weights, which is
real and which decides matches — but it is invisible until the match is already
being played.

So the moment a trait is being weighed up is the one moment it does not show.
A captain who spent 3,050 gems taking a trait to Lv.5 opened the Playing XI
card before the toss and read the same **Team Overall** as the day they bought
it. Two squads that differ by a fully kitted XI of Lv.5 traits looked identical
on the only screen that compares them.

## 2. The ladder

Every equipped trait adds rating points, doubling with each level:

| Level | Boost |
|---|---:|
| Lv.1 | +0.2 |
| Lv.2 | +0.4 |
| Lv.3 | +0.8 |
| Lv.4 | +1.6 |
| Lv.5 | +3.2 |

The doubling is the point, not decoration. A linear ladder would make Lv.5 five
times Lv.1, and the correct build would always be five Lv.1 traits — levelling
would be a worse deal than buying, at every rung. Doubling makes one Lv.5 worth
more than four Lv.1s (3.2 > 0.8), so *levelling a trait you already own competes
with buying another one*. That trade is the decision the trait economy exists to
create, and rarity already handles the other side of it: an Elite costs eight
times a Common to acquire but the same gems to level
([traits-and-market-stock.md](./traits-and-market-stock.md) §3.1).

## 3. Two ceilings

**Per card.** Traits on one player stack with the ball engine's own diminishing
returns — `TRAIT_STACK_WEIGHTS` (×1.0, ×0.7, ×0.5), strongest level first — and
then cap at `TRAIT_RATING_BONUS_MAX_PER_PLAYER` (6.0). Without the cap, three
Lv.5 traits on one card stack to 7.04, which moves a single player further than
the gap between a good XI and a great one.

Strongest-first ordering is not cosmetic: order the stack by whatever the
database returns and a player's effective squad depends on their row order, so
two captains with identical squads read different Team Overalls. It is the same
bug `trait_engine.apply_traits` fixed on its own side, fixed the same way.

**Per XI.** Team Overall is the *average* of eleven cards, so the boost is the
average of the eleven cards' boosts, capped at
`TRAIT_RATING_BONUS_MAX_PER_TEAM` (5.0).

That the team boost is an average and not a sum is what keeps it honest. One
Lv.5 trait lifts an XI by 3.2/11 ≈ 0.3, not by 3.2. Moving the number takes
kitting out the squad — which is exactly what the squad-wide budget of 18
equipped traits (`TRAIT_MAX_PER_SQUAD`) is there to be spent on.

The 5.0 team ceiling is deliberately **below** `STATS_FAIRNESS_OVR_GAP` (10), so
no amount of trait investment can on its own open a gap wide enough to look like
stat farming. `tests/test_trait_rating_boost.py` pins that relationship rather
than the two numbers, so re-tuning either one cannot quietly break it.

## 4. What the boost does NOT touch

**The ball engine.** Traits already move the simulation once, per delivery,
through `trait_engine.apply_traits`. Feeding the boost into the ratings that
engine reads would pay the same trait out twice — once as a probability nudge
and again as a higher effective rating driving the same probabilities. The boost
is a *card* number: the visible, comparable measure of a squad's trait
investment.

**The anti stat-farming gate.** `is_stat_farming_mismatch` compares the printed
card ratings, not the boosted ones. That gate exists to catch a strong XI
farming career stats off a deliberately weak one — usually an alt account. A
trait is bought with gems and equipped days in advance; counting it would mean a
captain who did exactly what the trait economy asked of them could silently lose
career stats for it, and the fix they would reach for is *unequipping their
traits*, which is the opposite of the behaviour the feature is trying to reward.

An operator who wants the boost inside the gap sets
`TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS = True`; every gate reads
`trait_rating_service.fairness_overall()` rather than deciding for itself, so
the switch means one thing everywhere.

## 5. Where it shows

### The Playing XI card (before the toss)

The card each side confirms before the toss now prints the boost twice: once per
card, and once for the team.

```text
👤 Ana (Host)
 1. R. Sharma (88 ⚡+3.2) 🔥⚓
 2. V. Kohli (89 ⚡+0.4) 🎯
 3. S. Iyer (84)
 …

━━━━━━━━━━━━━━━━━━━
📊 Team Overall: Host 84 vs Guest 83 — stats will count. ✅
⚡ Trait Boost: Host 84 → 85.2 (+1.2) · Guest 83 → 83.0 (+0.0)
⚡ Trait Boost is what your equipped traits add to the team card. It does not
change the gap above — traits never cost you career stats.
```

Both lines are there on purpose, and they answer different questions. The gap
decides whether career stats count; the boost is what the traits add to the team
about to be fielded. The footnote only appears when there *is* a boost to
explain — a squad with no traits sees exactly the card it always saw, down to
the per-player line, which keeps `format_player_rating` printing a bare `84`
rather than `84 ⚡+0.0`.

A side effect worth naming: the human XIs on that card never showed their trait
badges at all — `_row_fields` returned an empty trait list for every database
row, so only the /lpbot opponent's inline traits rendered. Loading the traits for
the boost fixed that in passing, in one batched query per card rather than one
per player, because the card is rebuilt on every `/change`.

### `/traitboost`

The same reckoning for your own XI, on demand, card by card — so "is levelling
this trait worth 1,500 gems?" can be answered before the gems are spent rather
than after the toss. It lists each card's traits and boost, the XI's Team
Overall before and after, the ladder, and how many of the eleven are carrying
nothing.

DM-only like the rest of the inventory commands: it reads out your whole squad.

### The live board

The board that carries every over of a `/letsplay` match gets an `⚡ OVR` line
next to the `🧪 CHEM` one — the same two numbers from the XI card, carried into
the match:

```text
🧪 CHEM  MI 🟩 88  ·  CSK 🟨 74
⚡ OVR  MI 84 → 85.2  ·  CSK 83 → 83.4
```

Scoped exactly like the chemistry line beside it, and for the same reason: only
a side fielding its own roster has traits at all. A Challenge League squad is a
league roster handed to both captains, so there is nothing to boost and the line
does not render. Neither does it render when neither side has a trait equipped —
an all-zero line is noise on an already dense board.

It is computed from the two XIs rather than read from the state keys below, so a
match that was already in flight when this shipped shows the line too. The boost
is a pure function of the eleven cards and their traits, so there is nothing to
migrate.

### Live match state

`/letsplay` stamps `bat_team_trait_boost` / `bowl_team_trait_boost` and the
matching `*_team_ovr_eff` onto the match state alongside — never instead of —
`bat_team_ovr` / `bowl_team_ovr`. The existing keys stay the printed-card
average because the fair-match warning quotes them next to the gap they are
measured from; a card that wants the boosted figure reads the new ones. Both XIs
already carry their traits inline, so this costs no extra query.

## 6. Tuning it

Everything lives in `config.py`:

| Constant | Meaning |
|---|---|
| `TRAIT_RATING_BONUS` | the level → points ladder |
| `TRAIT_RATING_BONUS_MAX_PER_PLAYER` | ceiling on one card |
| `TRAIT_RATING_BONUS_MAX_PER_TEAM` | ceiling on one XI |
| `TRAIT_RATING_BONUS_COUNTS_FOR_FAIRNESS` | is the boost inside the stat gap? |

Stacking reuses `TRAIT_STACK_WEIGHTS`, so re-tuning diminishing returns moves
both halves of the trait system together rather than letting the card and the
simulation drift apart.
