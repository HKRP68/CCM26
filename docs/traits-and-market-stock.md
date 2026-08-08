# Traits: the catalogue, rarity, and unlimited market stock

Design note for the trait expansion and the shared-market stock change.

Reference implementation: `services/trait_service.py` (the catalogue),
`services/trait_engine.py` (what each trait does to a delivery),
`services/global_market.py` (stock and pricing), `config.py` (the economy).
Tests: `tests/test_trait_catalogue.py`, `tests/test_market_unlimited_stock.py`,
`tests/test_trait_seed_and_market_stock.py`, plus the pre-existing
`tests/test_trait_market.py` which still pins the Common-tier price tables
unchanged.

---

## 1. Summary

Three things changed together, because each one is what makes the next one
work:

1. **Stock in the shared markets is unlimited.** `quantity = 0` means "never
   sells out", and that is the default for every player card and every trait
   listed. A limited run is still available by setting a positive quantity.
2. **The catalogue went from 14 traits to 43**, across seven categories, with
   the ten Elite traits as the top tier.
3. **Traits carry a rarity**, which decides how often the market lists one and
   what it costs — because a catalogue of 43 traits where all of them cost 150
   gems and appear equally often has no shape to it.

The one-per-player Elite rule (`TRAIT_MAX_ELITE_PER_PLAYER`) exists because
without it a captain stacks Master Blaster + Run Machine + GOAT Instinct on one
card and the other 33 traits stop being decisions.

> **Since this note was written**, a squad-wide budget of 18 equipped traits
> (`TRAIT_MAX_PER_SQUAD`) was added on top of the per-card rules, with the
> Career Player exempt and carrying its own 3 slots; and the trait market moved
> off the player market's daily hour onto its own interval + start hour, set
> from the website. See [`squad-and-trait-limits.md`](./squad-and-trait-limits.md).

---

## 2. Unlimited stock

### 2.1 Why

The old market listed **one copy of each player card** and ten of each trait.
That makes the daily refresh a race: the captain who opens `/playermarket`
first gets the card, and the one who is 2,000 coins short of it that morning
never gets a second chance at it. Nothing in the game's economy needs that
scarcity — the market charges the standard `/buy` price, so an extra sale is
not an extra discount, and per-captain ownership is already limited by the rule
that nobody may own two copies of the same card.

So the shop is a shop now. What is listed today is buyable by everyone, all
day, at the same price.

### 2.2 The convention

`quantity = 0` (`services.global_market.UNLIMITED`) means unlimited. Every
caller asks `is_sold_out(slot)` rather than comparing `purchased_count` to
`quantity` inline — the inline comparison is exactly the one that reads an
unlimited slot as sold out from the moment it is listed, and it was duplicated
in nine places before this change.

| Helper | Answers |
|---|---|
| `is_unlimited(slot)` | can this be bought without end? |
| `is_sold_out(slot)` | should the Buy button be dead? |
| `stock_left(slot)` | copies remaining, or `None` when unlimited |
| `stock_label(slot)` | `"∞"` or `"3 left"` |

`purchased_count` is still incremented on an unlimited slot. It no longer gates
anything, but it is the only record of which listings people actually want, and
the admin market page reads it.

### 2.3 What did *not* change

A limited run still behaves exactly as before, race conditions included. The
atomic `UPDATE … WHERE purchased_count < quantity` is still there — it is only
skipped when the slot is unlimited, where there is no race to lose. Losing that
guard on limited runs would have been a silent regression, so
`tests/test_market_unlimited_stock.py` drives both halves.

Existing databases are converted once, by the `market_unlimited_stock`
migration in `database.py`. Trait rows already sitting at their old ten-copy cap
are included deliberately: they were only capped because stock existed, and
leaving them sold out would keep dead listings on the shelf.

---

## 3. Rarity

| Tier | Weight | Lv.1 price | Meaning |
|---|---:|---:|---|
| ⚪ Common | 50 | 150 💎 | a straightforward, always-on edge |
| 🔵 Rare | 30 | 300 💎 | conditional, but the condition comes up often |
| 🟣 Epic | 15 | 600 💎 | situational, strong when it lands |
| ⭐ Elite | 5 | 1,200 💎 | very rare; **one per player** |

Weights matter more than they look. With ten Elite traits in a 43-trait
catalogue, a flat `random.sample` would put an Elite in a 5-slot shop about a
quarter of the time — and with unlimited stock, whatever is listed is buyable by
everyone all day. `pick_traits_by_rarity` does a weighted draw instead.

### 3.1 What rarity scales, and what it doesn't

Rarity multiplies the **Lv.1 price** and, with it, resale and the swap fee.
It does **not** scale upgrade costs: levelling an Elite trait costs the same
gems as levelling a Common one.

That is a deliberate split. Rarity is a barrier to *getting* a trait, not a tax
on using it — scaling both would put a Lv.5 Elite at 24,000 gems, which nobody
would ever build, and the trait would exist only on paper.

Every pricing function takes an optional `rarity` that defaults to Common, so
every number in the pre-existing economy is unchanged to the gem:

```
                  Lv.1    Lv.2    Lv.3    Lv.4    Lv.5
Common  buy        150     350     750   1,550   3,050
        sell       119     319     719   1,519   3,019
Elite   buy      1,200   1,400   1,800   2,600   4,100
        sell       959   1,159   1,559   2,359   3,859
```

The invariant that matters is unchanged and now holds at every tier: **resale
always returns less than the cheapest the trait could have been acquired for.**
Break it and the shop becomes a gem tap — buy the discounted slot, sell it
straight back, repeat.

### 3.2 A bug that mattered more after this change

The shared trait market rolled its sale percentage from `[0, 0, 10, 15, 25]`,
but resale is priced against `TRAIT_DISCOUNT_RANGE`, whose deepest discount is
20%. A 25%-off Lv.1 trait therefore cost 112 gems and sold back for 119.

That was survivable when stock ran out after ten copies. Unlimited stock turns
it into an unbounded gem printer, so `roll_trait_discount()` now derives the
roll from the range resale was priced for. `tests/test_market_unlimited_stock.py`
pins it.

### 3.3 Trading

`/tradetrait` now requires the same **rarity** as well as the same level. Same
level is not the same value once rarity exists: a Lv.2 Elite cost 1,400 gems to
build and a Lv.2 Common 350, so a cross-tier swap would be the trait economy's
free-money trade.

---

## 4. The catalogue

43 traits in seven categories. Two categories are new (Awareness, Special) and
Elite is its own category so `/traits` groups it the way players think about it.

| Category | Traits |
|---|---|
| Batting | Finisher, Power Hitter, Anchor, Fast Starter, Clutch Player, **Spin Basher**, **Pace Destroyer**, **Late Bloomer**, **Power Surge**, **Pinch Hitter** |
| Bowling | Death Specialist, Wicket Hunter, Dot Ball Specialist, Powerplay King, Yorker Specialist, **Spell Builder**, **Partnership Breaker**, **Tail-End Hunter**, **Middle-Over Squeeze**, **Economy Machine** |
| Fielding | Safe Hands, Sniper Arm, **Boundary Rider**, **Livewire** |
| Mental | Consistency King, Momentum Player, **Confidence Player**, **Comeback King**, **Ice Veins** |
| Awareness | **Pitch Reader**, **Strike Rotator**, **Gap Finder** |
| Special | **Giant Killer** |
| Elite | **Master Blaster, Run Machine, Ice Finisher, Bowling Wizard, Magic Spell, Unplayable, Golden Arm, Big Fish Hunter, Nightmare Matchup, GOAT Instinct** |

Six of those (Pinch Hitter, Middle-Over Squeeze, Economy Machine, Boundary
Rider, Livewire, Ice Veins) were added on top of the brief to fill gaps the rest
of the list left: Fielding had two traits, no bowling trait covered the middle
overs between Powerplay King and Death Specialist, and nothing let a batter
lower their risk under a high required rate.

`/traitlist` shows the whole catalogue in-game, optionally filtered by category
(`/traitlist bowling`). Without it the catalogue is invisible — the shop only
ever shows five slots, and nobody can plan for a trait they have never seen.

---

## 5. How a trait fires

`services/trait_engine.py` maps each `effect_key` to a handler taking
`(ctx, x, role)` and returning `(probability_key, delta)` pairs. `x` is the
level's strength in percentage points; `role` is which side the trait sits on.

### 5.1 The context contract

**Every ctx key is optional.** A handler reads with `.get()` and a default, and
returns `[]` rather than raising when it isn't told what it needs.

This is what lets a new trait ship without touching every ball loop. Three
loops feed this engine — `services/cipl_match.py` (/letsplay, /cipl),
`handlers/match.py` (/playmatch) and `handlers/super_over.py` — and they know
different amounts about the match. A trait that needs something a loop can't
supply stays quiet there instead of breaking it.

The full key list is in the module docstring. The interesting additions are the
ones that make matchup traits possible at all: `is_spin`, `bat_position`,
`partnership_runs`, `boundary_streak`, `bowler_balls` / `bowler_runs`,
`bat_hand` / `bowl_hand`, and both effective ratings.

### 5.2 Two engine fixes this needed

**Singles and doubles were being discarded.** `services/cipl_match.py` mapped
trait deltas onto the engine's raw weights through a four-entry table — Six,
Four, Wicket, Dot. Any delta on a `1` or a `2` was computed and thrown away,
which would have made Strike Rotator, Gap Finder and Boundary Rider look
equipped and do nothing in /letsplay. The map now carries Single and Double too.

**Stacking is level-ordered.** `TRAIT_STACK_WEIGHTS` applies diminishing
returns by slot (×1.0, ×0.7, ×0.5), and the engine used to index it by whatever
order the traits arrived in — i.e. by database row order. A player's effective
squad therefore depended on the order their rows came back. Traits are now
sorted by level descending first, so the Lv.5 trait always takes the
full-strength slot.

### 5.3 Randomness

Two traits are deliberately probabilistic:

- **Magic Spell** ("occasionally produces an exceptional over") must be an
  *over*, not scattered balls, so it hashes over + innings + bowler into a roll
  that is stable for the whole over and carries no state.
- **GOAT Instinct** is a per-ball 25% roll, gated on the match actually being
  on the line (high required rate, last two overs, or 7+ down).

---

## 6. Seeding

`database._seed_traits` now **upserts**, keyed on `effect_key` and falling back
to the name. Editing a description or re-tuning a rarity in
`services/trait_service.py` reaches live databases on the next boot instead of
only new ones.

Two fields are never written: `is_active` and `base_price`. Those belong to the
admin, who can disable a trait or pin its price from the website without the
next deploy silently undoing it. `tests/test_trait_seed_and_market_stock.py`
pins that, along with the rename case (a renamed trait updates in place rather
than inserting a duplicate).
