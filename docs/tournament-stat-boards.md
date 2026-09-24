# Stat boards that answer the next question

`/tournamentstats` ranks ten categories and used to print one number per player:

```
🥇 Sai Sudharsan · PBKS — 383
```

383 says who is top and nothing else. Whether that is 383 off 144 balls in
thirteen matches or 383 off 400 in thirty is the whole of what anyone wants to
know next, and the board could not say — even though `stat_leaders` had already
loaded every one of those columns and was throwing them away.

Each ranked entry is two lines now: who, then what.

```
🥇 Sai Sudharsan (PBKS)
     — 🏃 383 runs (144b · SR 265.97) · 📊 13 M

🥇 Jasprit Bumrah (MI)
     — 🎯 24 wkts (18.0 ov · Econ 6.42 · Best 4/27) · 📊 13 M
```

The number keeps its emoji and gains its unit, the figures behind it ride in the
bracket, and the match count closes the line — so a season reads as a season and
an afternoon reads as an afternoon.

## What each board carries

| Board | Ranked on | In the bracket | Matches |
| --- | --- | --- | --- |
| 🏏 Most Runs | runs | balls, strike rate | yes |
| 🎯 Most Wickets | wickets | overs, economy, best figure | yes |
| 6️⃣ Most Sixes / 4️⃣ Most Fours | sixes / fours | runs, strike rate | yes |
| ⭐ Highest Score | the knock | balls, that innings' strike rate | no |
| 📊 Batting Average | average | runs, dismissals | yes |
| 💥 Best Figure | the spell | — | no |
| ⚡ Best Strike Rate | strike rate | runs, balls | yes |
| 🛡️ Best Economy | economy | wickets, overs | yes |
| 🏅 MVP | impact points | runs, wickets, wins, awards | yes |

Two rules run through that table.

**A batting board never shows economy and a bowling board never shows a strike
rate.** The figures are chosen per board rather than printed wholesale, because
a column that does not bear on the ranking is noise between the reader and the
one that does.

**A per-innings board carries no match count.** Highest Score ranks a single
knock and Best Figure a single spell — "how many matches" has one answer, and
printing it would be answering a question nobody asked. Best Figure carries
nothing at all: there is nothing behind a figure but the figure, so the entry
stays on one line rather than growing a blank second one.

## Overs are cricket overs

`_overs(110)` is `18.2` — eighteen overs and two balls — never `18.33`. Balls
divided by six is a decimal nobody in cricket reads, and an economy rate printed
next to a wrong over count is worse than no over count.

`_figure` guards the other sentinel: `best_bowl_runs` is seeded at `-1` rather
than `0` precisely because 0 runs is a real (and excellent) figure, so a board
that checked for falsiness would hide the best spell in the tournament.

## Two renderers, one row

`_leaders_for` returns rows built by `_leader(...)`, carrying both shapes the
two renderers want:

* `extras` — what the HTML board prints in the bracket
* `cells` — `(header, value)` pairs the rich table gives their own columns

A table has headers, which is the one place a figure can be labelled without
repeating the label on every row, so the bracket becomes `BALLS | SR | M`
columns there. Both come off the same row, so the two cannot drift into showing
different things. The headers are taken from the first row that has any, so
every row lays out the same way even when one of them is missing a figure.

Rows used to be plain `(name, team, value)` tuples. `_unpack` still accepts
one — it simply has nothing extra to show — so anything building a row outside
this module keeps working.

## The same board elsewhere

`/mytours` → 📈 Tour Stats reads the same way (`handlers/tours._leader_lines`),
off figures `tour_service.get_tour_stats` now sums in the same `GROUP BY` that
was already running. The same board in two places should not be read two
different ways.

## Where the pieces live

| File | Role |
| --- | --- |
| `handlers/tournament.py` | `_leaders_for`, `_rank_line`, `_rank_cells` |
| `services/tournament_service.py` | `stat_leaders` — the rows and the ranking |
| `services/tour_service.py` | `get_tour_stats` — the tour twin |
| `handlers/tours.py` | `_leader_lines`, `_bat_detail`, `_bowl_detail` |
| `tests/test_tournament_stat_boards.py` | the figures, both renderers, the legacy row |
