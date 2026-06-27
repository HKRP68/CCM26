# CIPL Match — Playing Guide (Approach Mode)

A practical guide to the over-by-over **Approach** game used by `/cipl` and the
admin-created Challenge League / League Battle matches. It explains how a match
flows, what every approach does, and — the part most people want — **which
approach works against which approach**.

> This mode is played **in the Telegram chat**. The Mini App link in each over
> message is a **read-only** scorecard / commentary view — you cannot bat, bowl,
> pick players, or use an Impact Player from it.

---

## 1. How a match flows

1. Host clicks **Start Match** → normal **toss** runs (guest calls heads/tails,
   winner elects bat or bowl) using the existing coin-toss animation.
2. Batting order is **already fixed** from Playing XI selection, so no mid-match
   batsman picking — new batsmen walk in automatically when a wicket falls.
3. Each over/set repeats this loop:
   1. **Bowling captain picks the bowler** (only bowlers & all-rounders shown).
   2. Bowling captain picks a **Bowling Approach**.
   3. Batting captain then picks a **Batting Approach**.
   4. The whole over is simulated ball-by-ball as one atomic unit.
   5. Over summary is shown; the next over begins (previous messages are cleared).
4. **Auto-pick on timeout (90s):** idle bowling approach → *Balanced* (idle
   bowler pick → top-rated eligible bowler); idle batting approach → *Balanced*.
   No forfeit — the match keeps going.

### Bowler rotation rules
| Format | Balls/unit | Total | Powerplay | Bowler quota | Consecutive |
|--------|-----------|-------|-----------|--------------|-------------|
| **T20** | 6 (over) | 120 | 6 overs (36 balls) | `ceil(overs/5)` overs | never 2 overs in a row |
| **The Hundred** | 5 (set) | 100 | 5 sets (25 balls) | 4 sets (20 balls) | may bowl **2** sets in a row, not 3 |

---

## 2. The approaches

The result of every ball still respects player ratings, pitch, ground, match
phase, momentum, pressure and scenario logic. An **approach only tilts the odds**
— it scales the raw outcome weights right before the delivery is sampled.

### Batting approaches
| | Approach | What it does |
|---|----------|--------------|
| 🛡 | **Defensive** | Survive. More dots/singles, far fewer boundaries, **lowest wicket risk**. For weak batsmen or tough bowling. |
| 🔁 | **Rotate Strike** | Strike rotation. More 1s & 2s, low wicket risk, few boundaries. Best in the **middle overs**. |
| ⚖ | **Balanced** | Neutral default (no tilt at all). Medium everything. |
| 🚀 | **Aggressive** | Controlled attack. More 4s & 6s, **higher wicket risk**. Best for **powerplay / chase pressure**. |
| 💥 | **Ultra Attack / Slog** | Full risk. Very high six chance, **very high wicket chance**. Best for **death overs / last-over chase**. |

### Bowling approaches
| | Approach | What it does |
|---|----------|--------------|
| 🛡 | **Defensive** | Stop runs. Fewest boundaries, more dots/singles, low wicket chance. Best when **defending a total**. |
| ⚖ | **Balanced** | Neutral default. Medium wickets, medium runs. |
| ☠️ | **Mixed** | Death-overs mode. Suppresses sixes **more when the bowler is highly rated** (≥80 → ×0.55; ≥65 → ×0.70; weak bowlers leak, ×0.90). |
| 🔥 | **Aggressive** | Attack for wickets. **Highest wicket chance** but concedes more boundaries. Best vs **new batsmen / tailenders**. |
| 🌀 | **Variation** | Slower balls, cutters, deception. Forces mistimed shots; good vs aggressive batting. Medium wickets, suppresses boundaries. |

---

## 3. Which approach works against which? (the matchup matrix)

These tables are computed from the actual outcome multipliers in
`engine/approach_modifiers.py`, using a representative base over and a
bowler rated **70**. Read **batting approach (rows) vs bowling approach (columns)**.

### Expected RUNS per ball — lower is better for the bowler
| BAT ↓ \ BOWL → | 🛡 Defensive | ⚖ Balanced | ☠️ Mixed | 🔥 Aggressive | 🌀 Variation |
|---|---|---|---|---|---|
| 🛡 Defensive | 0.94 | 1.24 | 1.09 | 1.32 | 1.13 |
| 🔁 Rotate | 1.24 | 1.58 | 1.42 | 1.67 | 1.46 |
| ⚖ Balanced | 1.36 | 1.92 | 1.65 | 2.06 | 1.72 |
| 🚀 Aggressive | 1.63 | 2.29 | 1.98 | 2.44 | 2.06 |
| 💥 Ultra | 1.92 | 2.70 | 2.33 | 2.86 | 2.43 |

### WICKET % per ball — higher is better for the bowler
| BAT ↓ \ BOWL → | 🛡 Defensive | ⚖ Balanced | ☠️ Mixed | 🔥 Aggressive | 🌀 Variation |
|---|---|---|---|---|---|
| 🛡 Defensive | 1.9% | 2.5% | 2.7% | 3.3% | 3.1% |
| 🔁 Rotate | 2.3% | 2.9% | 3.2% | 3.6% | 3.6% |
| ⚖ Balanced | 3.4% | 4.1% | 4.6% | 5.0% | 5.2% |
| 🚀 Aggressive | 4.1% | 4.6% | 5.4% | 5.6% | 6.0% |
| 💥 Ultra | 4.8% | 5.0% | 6.1% | 6.0% | 6.6% |

**How to read it:** 🔥 Aggressive bowling always takes the most wickets but also
leaks the most runs. 🛡 Defensive bowling always leaks the fewest runs but takes
the fewest wickets. ☠️ Mixed and 🌀 Variation sit in between — they keep boundaries
(and sixes) down while still threatening wickets, which is why they shine against
big-hitting batting.

---

## 4. Recommended counters

### If you're **bowling**, pick by what the batter is doing
| Batter is playing… | Best counter | Why |
|--------------------|--------------|-----|
| 🛡 Defensive / 🔁 Rotate | 🔥 **Aggressive** | Runs are already low, so trade nothing to buy the most wickets (3.3–3.6% vs ~2%). |
| ⚖ Balanced | ⚖ **Balanced** or 🌀 **Variation** | Match it, or use Variation to shave boundaries without giving up many wickets. |
| 🚀 Aggressive | 🌀 **Variation** or ☠️ **Mixed** | Cuts boundary runs (2.06 vs 2.44 for Aggressive bowling) while keeping wicket pressure high. |
| 💥 Ultra / slog | ☠️ **Mixed** (with a **high-rated** bowler) or 🌀 **Variation** | Mixed throttles sixes hardest when the bowler is rated ≥80; Variation forces the mistimed shot. Avoid Aggressive bowling here — it feeds the slog. |
| Defending a small total | 🛡 **Defensive** | Lowest runs conceded across the board. |

### If you're **batting**, pick by the situation
| Situation | Best pick | Why |
|-----------|-----------|-----|
| Powerplay | 🚀 **Aggressive** | Fielding restrictions reward boundaries; risk is acceptable early. |
| Middle overs / rebuild | 🔁 **Rotate Strike** | Keeps the rate ticking with minimal wicket risk. |
| Lost quick wickets / tough bowling | 🛡 **Defensive** | Lowest wicket chance in the game — survive, then accelerate. |
| Death overs / last-over chase | 💥 **Ultra Attack** | Highest six output; accept the wicket risk because balls > wickets now. |
| No strong read | ⚖ **Balanced** | Neutral and safe; also the auto-pick on timeout. |

### The core trade-off
- **More aggression = more runs *and* more wickets** on both sides.
- The bowler's job is to find the approach that keeps **runs** down without
  giving up **wickets**, and the death-overs answer to a slogger is **Mixed**
  (rating-gated six suppression) or **Variation**, *not* Aggressive bowling.
- 🔥 Aggressive bowling is a **wicket weapon**, best spent against cautious or
  set-in batting where the extra runs cost you little.

---

## 5. Other useful info

- **Engine reuse:** outcomes come from the SimCricketX engine (`engine/`) — ball
  outcome model, momentum / par-score, pressure, and (in 2nd-innings T20 chases)
  the dramatic-finish **Scenario Engine**. Approaches are a thin multiplier layer
  on top (`engine/approach_modifiers.py`), so ratings and conditions always matter.
- **Commentary:** uses the full SimCricketX commentary engine (rich, varied
  lines), not terse templates.
- **Per-over summary** shows: runs, wickets, current batsmen, bowler figures,
  updated score, required run rate (if chasing), momentum shift, and a short
  commentary recap.
- **Eligibility:** only Bowlers and All-rounders can bowl; the previous over's
  bowler is excluded; quota and consecutive-over rules above are enforced.
- **Scope:** this Approach flow powers `/cipl` and admin Challenge League matches
  only — `/cm` and `/playmatch` (`/wpm`) are unchanged.

*Matchup numbers are derived from the multiplier tables in
`engine/approach_modifiers.py`; absolute values shift with the actual base
weights of a given delivery, but the **relative ordering between approaches
holds**.*
