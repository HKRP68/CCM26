# Conditions Engine v4

Weather, time of day, stadiums, ball aging and a thriller dial — for the live
`/letsplay` and CIPL matches, and as a standalone T20 / ODI / Test simulator.
Every number lives in **`config/sim_engine.json`**; nothing needs a code change
to rebalance.

| What | Where |
|---|---|
| All tuning multipliers | `config/sim_engine.json` |
| Your local overrides (only the keys you change) | `config/sim_engine.local.json`, or the file in `SIM_ENGINE_CONFIG` |
| Stadium database (editable) | `data/stadiums.json` |
| The rules, one module each | `engine/sim/` — `pitch`, `weather`, `time_of_day`, `stadium`, `ball_aging`, `situation`, `drama`, `rain_dls` |
| Composing them / the ball outcome | `engine/sim/factors.py`, `engine/sim/outcome.py` |
| Standalone match loop, commentary, summary | `engine/sim/match.py`, `commentary.py`, `report.py` |
| Live hook for /letsplay + CIPL | `engine/sim/hook.py`, called from `services/cipl_match.simulate_over` |
| Play a match | `python -m tools.sim_match` |
| Balance sweep (standalone) | `python -m tools.sim_calibration` |
| Balance sweep (live engine) | `python -m tools.pitch_calibration --conditions` |
| Tests | `tests/test_sim_engine.py` |

## Playing a match

```
python -m tools.sim_match --format T20 --stadium Wankhede --seed 7
python -m tools.sim_match --format ODI --stadium "Eden Gardens" --session Afternoon --daynight --seed 42
python -m tools.sim_match --format Test --stadium Chepauk --pitch Dusty --seed 3 --summary-only
python -m tools.sim_match --team1 my_xi.json --team2 England --cloud 85 --humidity 88 --json out.json
```

Same arguments and the same `--seed` give the same match, ball for ball
(`SimRng` hashes the seed with SHA-256 and hands each subsystem — weather, rain,
drama, every over — its own stream, so a change to one never reshuffles the
others). Teams are countries from `data/players.json` or a JSON file
`{"name": ..., "players": [engine player dicts]}`; any attribute you put on a
player (`technique`, `swing`, `is_death_specialist`, ...) overrides the derived one.

Output: one line of commentary per ball, scorecards with fall of wickets, a
wagon wheel, and a summary with the key turning points, **what the conditions
did** ("The dew after 6pm killed the spinners' grip (spin x0.70) — active 7.2
overs"), and a 0–10 impact rating for every player.

## How a ball is decided

```
fs  = factors.compose(conditions, ball, bowler)      # pitch, grass, weather, clock, ground, ball age
sit = situation.evaluate(...)                        # pressure, momentum, settling in, tail, choke, death
help        = skill-weighted mean of the channels this bowler can use   (damped)
bowlerEdge  = (0.45 + 0.55·skill) · (0.8 + 0.4·accuracy) · help · sit.bowler_edge
shotQuality = (0.45 + 0.55·(technique+matchup)/2) · form · battingEase · batSpeed · sit.shot_quality
ratio       = bowlerEdge / shotQuality
Wicket × ratio^1.25 · pace|spinWicketChance · sit.wicket · aggression
Four/Six × (1/ratio)^1.1 · aggression · carry / six
Dot × ratio^0.5            Extras × (1/accuracy)^0.6
→ clamp, renormalise the format's base distribution, one draw
→ a 1/2/3 into a gap goes for four when shotPower > 100 − outfieldSpeed
→ a catch goes down with P(baseDropChance + dew + drama)
```

## The spec, rule by rule

| Spec | Config key | Notes |
|---|---|---|
| 7 pitches × 9 multipliers | `pitches` | `Dead` kept too (not selectable in the bot). Clamped 0.1–3.0. |
| Morning swing ×1.4, seam ×1.2 (+ if cloud > 60) | `timeOfDay.morning` | Session is read from the clock *at the delivery*, not the start time. |
| Afternoon heat: crack + spin +0.1/session | `timeOfDay.afternoon` | |
| Evening/night dew: shine restored, spin grip ×0.7, drops +15% | `timeOfDay.eveningNight` | Dew needs the clock past `dewFromHour` (18:00) and ramps in over an hour. |
| Pink ball swing ×1.3, first 15 overs | `timeOfDay.pinkBall` | Day/night Tests use a pink ball and the `TestDN` session clock. |
| Test decay by `crackRate`; days 4–5 Dry/Dusty spin wicket ×2.0 | `pitchDecay`, `timeOfDay.test` | |
| swing = 0.6 + cloud/100 × 1.2 | `weather.swingBase`, `swingCloudScale` | Plus a small humidity term (`swingHumidityScale`); weather drifts every over. |
| Heat > 35 °C: stamina ×1.5, crack ×1.3 | `weather.heat` | |
| Wind > 25 km/h: spin drift ×1.3, six +10% downwind | `weather.wind` | The spinner bowling *into* the wind gets drift; the batter then hits downwind. |
| Humidity > 80%: swing window +5 overs | `weather.humidity` | |
| Rain → lost overs, DLS; Test → time lost. Very rare | `weather.rain` | ~0.6% of T20s, ~1.5% of ODIs at rain chance 50. Standalone only (see below). |
| Boundary bands 1.5 / 1.2 / 1.0 / 0.75 | `stadium.sixProbBands` | Averaged over the four regions. Altitude and wind shorten the *effective* boundary first. |
| Outfield 1/2/3 → 4 when shotPower > 100 − speed | `stadium.outfield` | See "Where the spec was not taken literally". |
| dewFactor > 60 after 6 pm | `stadium.dew.threshold` | One dew rule: at or above the threshold it is full dew; below, it scales down. |
| Altitude > 800 m: carry +8%, swing ×0.85, six +10% | `stadium.altitude` | +4% more per 1000 m above, so Dharamsala (1300 m) reads +12%. |
| Ball aging bands, reverse swing, soft ball | `ballAging` | ODI: one ball per end. Test: new ball every 80 overs. |
| Pressure RRR > 8 (T20): aggression ×1.4, wicket ×1.6 | `situation.pressure` | Ramped, see below. |
| Momentum +5%, new batter ×0.8 for 5 balls, tail ×0.5 below 8 | `situation.*` | |
| Choke: target < 50, 5+ wickets in hand | `situation.choke` | |
| Death overs: specialist ×1.3, yorker accuracy +20%, max aggression | `situation.death` | T20 16–20, ODI 45–50. |
| dramaSlider, event chance = slider/1000 per over | `drama` | Dropped catch, direct hit, stumping, huge over, DRS overturn, rain scare, crunch misfield. |
| Formats | `formats` | T20, ODI (two balls, DLS), Test (5 days × 3 sessions, 90 overs/day, follow-on 200 — optional, declarations). |

## Where the spec was not taken literally, and why

**Pitch help is centred (`pitchCentering`).** Raw, the table's help values
compound with everything else: a Green top's seam ×1.8 meets the new ball's
×1.4, the morning's ×1.2 and the cloud, and the side batting on it scored 63.
So each pitch's bowling help is divided by its best bowling option (at strength
0.6): the help values decide *who* takes the wickets — quicks on Green, spinners
on Dusty — and `battingEase` decides *how many runs* the surface is worth.
Turn it off with `"enabled": false` if you want raw help to move totals too.

**Damping (`outcome.damping`).** Channels are compressed before they meet the
players (help^0.35, ease^0.6, wicket chance^0.6, six^0.8), with per-format
overrides because a Test innings compounds (wickets end innings, so small edges
become huge totals). Lower = conditions matter less.

**battingEase is tuned, not typed.** The seven values were fitted with
`tools/sim_calibration` so the standalone T20 medians land on the same par bands
the live engine is calibrated to (`docs/pitch-types.md`). They read as a ranking,
Dusty 0.58 → Flat 1.40.

**Pressure ramps.** ×1.6 wickets the moment the rate ticks past 8 would make
every 170+ chase a collapse from ball one. The multipliers ramp from RRR 8 to
full strength at RRR 12 (`rampPerRun` 0.25); set `rampPerRun` to 100 for a step.

**Outfield conversion applies to shots into the gap.** Read literally —
shotPower uniform, outfield 85 — 85% of all singles would become fours. So only
a share of 1s/2s/3s are gap shots (`candidateShare`: 3% of singles, 12% of
twos, 45% of threes), and the formula decides which of *those* beat the fielder.

**Situation and drama rules are standalone-only.** The live engine already has
its own pressure engine, momentum/MPI, clutch finale, last-over drama and chase
controller, calibrated together. Adding a second copy of each would count them
twice; the live hook applies only the *environment*.

**No rain in live matches.** `services/weather_drift` deliberately never
produces rain in /letsplay, and a stoppage needs a UI for shortened innings the
bot does not have. Rain, DLS and time lost are fully modelled in the standalone
simulator.

## The live hook

`services/cipl_match.simulate_over` asks `engine.sim.hook.make_conditions_hook`
for a weight hook. It maps the Pitch Report's strings ("Overcast", humidity
"High", dew "Heavy", "Night (7:30 PM)", outfield "Fast", grass, pitch age) and
the venue name to the numeric model, then applies the **delta** between those
conditions and neutral ones (`meta.neutral_reference`) *on the same pitch*.
The pitch, the phase, pressure and momentum cancel out of that ratio, so the
calibrated par bands hold. The delta is blended by `meta.live_strength` (0.6)
and clamped to `meta.live_bounds` (0.8–1.25). When it is on, it replaces the old
string-table hook in `services/pitch_report`, which described the same weather.

Switch it per format with `meta.enabled_live`. A match without conditions, or
any failure, falls back to the old hook.

The post-match analysis file gains a **What the conditions did** section from
the hook's tally (`state["sim_influences"]`).

Examples of the live delta (striker 80, bowler 85):

| Situation | Wicket | Four | Six |
|---|---|---|---|
| Wankhede, heavy night dew, spinner, chase over 15 | 0.80 | 1.16 | 1.25 |
| Lord's, overcast morning, green top, quick, over 2 | 1.19 | 0.87 | 0.91 |
| MCG, hot afternoon, quick, over 10 | 0.96 | 1.05 | 0.96 |
| Chinnaswamy (920 m, short), spinner, over 10 | 1.00 | 1.07 | 1.25 |

## Measured

Standalone, `python -m tools.sim_calibration` (first-innings means):

| Pitch | T20 (n=150) | ODI (n=60) | Test (n=20) |
|---|---|---|---|
| Dusty | 170 | 266 | 246 |
| Green | 174 | 259 | 322 |
| Dry | 180 | 286 | 286 |
| Bouncy | 188 | 276 | 343 |
| Even | 195 | 298 | 332 |
| Hard | 219 | 323 | 365 |
| Flat | 234 | 364 | 407 |

Live engine, `python -m tools.pitch_calibration --n 250` (first-innings median):

| Pitch | Spec band | No conditions | Old env hook | v4 hook |
|---|---|---|---|---|
| Flat | 230–246 | 242 | 239 | 238 |
| Hard | 212–226 | 228 | 218 | 224 |
| Even | 190–202 | 196 | 192 | 200 |
| Bouncy | 186–198 | 202 | 196 | 200 |
| Dry | 180–194 | 192 | 190 | 195 |
| Green | 174–188 | 179 | 170 | 172 |
| Dusty | 160–173 | 166 | 162 | 171 |

All three runs fail one to three chase-band rows per sweep, and different rows
each time — the harness's documented Monte-Carlo noise at n=250, not the hook.
Sub-100 all-outs stay at 0.3%.

Known gaps: Tests finish inside five days more often than real ones do (draws
are rare), and the CLI's country XIs are picked by rating from
`data/players.json`, which mixes men's and women's cards — pass a team file for
a specific XI.

## Balancing workflow

1. Copy the keys you want to change into `config/sim_engine.local.json`.
2. `python -m tools.sim_calibration --format T20 --n 150` (and ODI/Test).
3. For the live game: `python -m tools.pitch_calibration --n 250 --conditions`.
4. `python -m pytest tests/test_sim_engine.py -q`.

The bot reads the config once per process; restart it (or call
`engine.sim.config.reload()`) after editing.
