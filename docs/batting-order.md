# The batting order you save with `/sbo`

A player arranges their line-up once and every match bats it. This note records
where that order lives, which two states it can be in, and what each mode does
with it.

## Where the order lives

The batting order **is** the roster's `order_position`:

| `order_position` | meaning |
|---|---|
| 1–11 | the Playing XI, in batting order |
| 12+ | the bench |

`User.batting_order_set_at` records whether the player arranged it themselves.
That single timestamp is the whole flag — it is set by `/setbo`, `/sbo`, the
Mini App XI reorder (`POST /api/webapp/xi/reorder`) and the Mini App bench swap
(`POST /api/webapp/xi/swap_bench`), and cleared by `/sbo auto` and `/autobuild`
(which pick an XI by rating, which is not a line-up anybody chose).

## The two states

`services/batting_order_service.py` is the only place that decides which one
applies. Every mode asks it; none of them work it out for themselves.

**Saved order** — `batting_order_set_at` is set.
Roster slots 1-11 are used verbatim. Slots 1 and 2 open, and a wicket brings in
the next player down. No mode asks the player to pick openers or a new batsman,
because they already answered that question.

**No saved order** — the flag is `NULL`.
Nobody ever chose a line-up, so the XI is sorted by batting rating (high → low),
ties broken on overall rating and then name. Modes with an opener picker still
show it. This is the "🤖 Auto order" the `/sbo` card names, and `/sbo auto`
returns a player to it.

## `order_locked`

`ordered_xi_dicts()` stamps `order_locked` on every engine player dict it
returns. The marker is a plain bool, so it survives the JSON round-trip through
`match_state`, the innings swap (which swaps whole XI lists), and the engine
adapters in `sim_match._adapt_player` (which copy with `{**p, …}`). Any code
holding only an XI can therefore still tell a saved line-up from a generated
one, with no extra DB read mid-match — which is what the opener lock and the
mid-innings walk-in rely on.

## What each mode does

| Mode | XI source | With a saved order |
|---|---|---|
| `/playmatch` (chat) | `handlers.match._gxi` | openers locked to 1 & 2; a wicket walks in the next player down |
| `/vsbot` (chat) | `handlers.match._gxi` | same; the bot's own XI is unaffected |
| `/wpm`, `/cm` (Mini App) | `match_webapp_service._xi` | openers locked at init and again at the innings break; no "pick next batsman" screen |
| `/wsp` (auto) | `match_webapp_service._xi` | already opened with slots 1 & 2 |
| `/letsplay`, `/lpbot` | `letsplay._xi_bench_for_side` | already used the saved order; no batsman prompts exist in this flow |
| `/sim` | `sim.\_xi_from_roster` | `sim_match.simulate_innings` skips its rating sort |
| Quick Match | `quick_match_service.get_user_xi` | `position` is renumbered to the batting position it weights by |
| Tour matches | route into `/wpm` | as `/wpm` |
| `/cipl`, Challenge League | `ChallengePlayer` draft picks | not applicable — those squads are drafted per competition, and selection order is already the batting order |

## Deliberate exclusions

* **The Super Over** still nominates its three batters by batting rating
  (`match_dynamics.super_over_batsmen`). A super over is three batters out of
  eleven, not a line-up, and the interactive version already lets the captain
  name them.
* **`/swapplayers`** numbers by the `/pxi` *display* order (batsmen → keepers →
  all-rounders → pacers → spinners), which is not the batting order. It still
  works, but for a player with a saved order it now says so and points at
  `/sbo`, which addresses batting slots 1-11 directly.
* **Per-match opener picks** are gone only for players who saved an order. A
  player who wants to choose openers every match can keep the auto order — that
  is exactly what `/sbo auto` restores.
