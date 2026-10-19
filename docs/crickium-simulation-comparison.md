# Crickium (`ck.zip`) compared with CCM26

The uploaded archive contains `Crickium-main`. This comparison reads its
`engines/tactics_engine.py`, `miniapp_tactics_engine.py`, `probability_engine.py`,
`shot_engine.py`, `playso_probability.py` and match runtimes against CCM26's
live engines.

For the focused over-by-over comparison and the subsequent confidence upgrade,
see [Over-by-over comparison](crickium-over-by-over.md). The precision shot
controls below apply to ball-by-ball games.

## Is its simulation better?

Crickium offers finer manual technique controls. CCM26 has a broader conditions
model and more formats. Reading the source establishes feature differences;
it does not establish which engine produces more realistic matches. That would
require equivalent XIs, tactics and conditions, and statistical comparison
against real cricket.

| Simulation feature | Crickium | CCM26 before this change |
|---|---|---|
| Batter intent versus bowling plan | Five mindsets, tactical matrix | Five approaches, tactical matrix, repetition, contextual interactions |
| Ratings and settling in | Level gap, confidence and balls faced | Rating duel, absolute ratings, settling, set-batter curve, form |
| Pitch and innings phases | Pitch/phase weights, short-format scaling | Pitch/phase weights, format-specific phases and pitch wear |
| Delivery variations | Named pace/off-spin/leg-spin deliveries | Already present in ball-by-ball games, including hand-specific options |
| Bowler chooses line | Explicit off/middle/leg | Missing |
| Spinner chooses length | Present in the detailed shot mode | Spinner delivery had no separate length choice |
| Batter chooses footwork | Front/back/advance | Missing |
| Batter chooses trajectory and intent separately | Ground/lofted and defensive/neutral/attacking | Intent mainly embodied in the selected shot |
| Detailed shot repertoire | Drives, cuts, sweeps, hooks, scoops, etc. | Eight Mini App buttons; additional profiles already existed in the calculator |
| Shot suitability against the delivery | Explicit length/footwork/shot match quality | Shot modifiers were independent of the delivery's length |
| Wickets, extras, free hits | Present | Already present, with fielding errors and scorecard accounting |
| Weather, dew, clock, stadium, ball age | These inspected engines primarily use pitch/phase tables | Dedicated modules for each, plus ground-specific modifiers |
| Rain/DLS, ODI and Test simulation | Inspected tactical engines target short matches | Standalone conditions simulator supports ODI/Test, rain/DLS, follow-on, declarations and time lost |
| Reproducible simulation | Mini App accepts an RNG | Standalone simulator has seeded subsystem streams |
| AI difficulty | Four profiles and decision strength | Existing AI levels, captain personas, matchup solver and bowler management |

## Implemented

The ball-by-ball Mini App now offers optional **Batting technique** and
**Bowling line & spin length** panels. The eight familiar shot buttons remain;
**More shots** adds On Drive, Off Drive, Hook, Square Cut, Late Cut, Reverse
Sweep, Slog Sweep, Paddle, Upper Cut, Switch Hit and Leg Glance using their
existing probability profiles. Nineteen shots are available altogether.

* Front-foot drives benefit against full balls; back-foot cuts and pulls fit
  short balls. Advancing to a short ball or yorker carries a penalty.
* Line affects shot suitability and wide risk. It is relative to the batter,
  so the existing handedness advantage is counted only once.
* Ground strokes favour rotation and safety. Lofted strokes offer more sixes
  at greater wicket risk. Intent independently changes aggression.
* Spinners can choose full, good or short length. The existing length weights
  and the new footwork matchup both consume that actual length.
* Feedback records the last shot's good/neutral/poor matchup and explains
  footwork/trajectory effects. Feedback describes the matchup, not a promise
  that a good shot will succeed.

The original implementation lives in `engine/shot_matchup.py`. Its bounded
weight adjustments preserve non-extra probability mass, so a batter's footwork
cannot manufacture wides or no-balls. Existing rating, pitch, form, confidence,
traits, free-hit and scoring rules remain in the shared calculator.

All controls default to **Auto**, which preserves the previous calculation and
random stream exactly. Technique selections visible in the browser are sent
with each shot; server-side plans are consumed once, including on wides and
no-balls, and cannot carry into a subsequent AI/autoplay delivery. Existing
chat shot indices and AI shot choices remain stable. No database migration is
needed. The controls apply to ball-by-ball games; over-by-over Approach matches
continue using their own captaincy decisions.

## API

`POST /api/match/action` keeps its existing envelope and adds optional keys:

```json
{"type":"delivery","action":{"delivery":"Inswing Full","speed":"normal","line":"off"}}
```

For a spinner, `action.spinLength` can be `full`, `good` or `short`.
`action.line` can be `off`, `middle` or `leg`.

```json
{"type":"shot","action":{"shot":"Off Drive","footwork":"front","stroke":"ground","intent":"balanced"}}
```

Footwork: `front`, `back`, `advance`. Stroke: `ground`, `lofted`.
Intent: `defensive`, `balanced`, `attacking`. Omit any optional choice, send
`null`, or use `auto` to keep its previous behaviour. Invalid inputs, wrong
roles, wrong turns and precision actions in Approach matches are rejected.
Existing authentication, match IDs and user identity fields are still required.

Arena snapshots include `shotOptions`, `currentLine`, `currentSpinLength` and
`lastBall.shotMatchup`.

## Other differences and tradeoffs

Crickium also has contextual uploaded probability profiles and upgrade-specific
probability bonuses. Those are separate administration/progression systems;
this change uses CCM26's existing shot profiles and traits rather than adding
a second progression model. The archive's scoop-specific shot families have
no dedicated CCM26 profile and remain a repertoire difference.

Several Crickium tactical guardrails prescribe the shape of a match: its
realism caps set wicket probability to zero after three wickets in an over,
limit run-outs per innings, and impose second-innings scoring ceilings. Those
would suppress legitimate cricket outcomes and need a separate balance
decision, so they were not adopted as simulation improvements.

## Verification

`tests/test_shot_matchup.py` covers unchanged default results and RNG state,
shot/footwork/line relationships, scoring-risk tradeoffs, valid finite weights,
probability conservation, unchanged batting-controlled extras, invalid API
input, role guards, full API-to-calculator scoring, spin-length propagation,
one-delivery cleanup and free-hit dismissal rules.

Run it with:

```
python -m pytest tests/test_shot_matchup.py tests/test_cric_mechanics.py \
  tests/test_approach_miniapp_play.py tests/test_cipl_approach.py \
  tests/test_wpm_refinements.py tests/test_scorecard_fidelity.py \
  tests/test_sim_engine.py tests/test_rating_rules.py \
  tests/test_match_dynamics.py tests/test_pitch_registry.py -q
```

The new precision coefficients are modest initial tuning, not values fitted to
real shot-tracking data. There is no claim of measured improvement in realism.

Validation: 306 distinct Python tests passed across the selected suites. An
offline Playwright check also passed ten checks covering the shot grids,
disabled controls, preserved selections, feedback, request payloads and
spin-versus-pace controls. The controls were visible at mobile width without
horizontal overflow. The async tests require local socket access for event-loop
wakeups; the restricted command sandbox caused stalls that also reproduced on
the unchanged baseline, and the same tests passed with that access available.

Changes are implemented in the source workspace; this work does not deploy or
restart the running bot.
