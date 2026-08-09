# Does it look like cricket?

A separate question from "is the total right", and one nothing in this repo was
asking. `tools/pitch_calibration.py` guards the team score; a side can post a
perfectly par 200 while the scorecard underneath it is nonsense. This note
records what an audit of the *texture* found, what was fixed, and what is still
off.

Run it yourself:

```
python -m tools.realism_audit --pitch Even --n 100
```

It prints the engine's figures beside real T20 ones — every ball outcome, every
dismissal mode, maidens, individual scores, strike rate by batting position,
bowling economy. The reference numbers are approximate and vary by ground and
season, so a flagged row means "look at this", not "this is broken".

## What was wrong

### Dismissals were not cricket

The starting mix, measured over 160 innings:

| | engine | real T20 |
|---|---|---|
| Caught | 35.8% | ~57% |
| Bowled | 22.2% | ~19% |
| **LBW** | **21.8%** | **~6.5%** |
| Stumped | 12.0% | ~5% |
| Run Out | 8.2% | ~9% |

Every third wicket was an LBW. Pace bowlers were credited with a 4% stumping
rate — a stumping off a quick is a freak, not a mode — and catching, which is the
signature T20 dismissal, was a minority outcome. This is the most visible error
in the whole engine, because it is printed on every scorecard line.

The table is now three layers: the bowler's style, the phase of the innings, and
what the batter was trying to do. A wicket in the last four overs against a
batter on Ultra Attack is a catch 78% of the time; the same bowler to a
defensive batter in the powerplay bowls them or traps them in front 51% of the
time. **Run Out is deliberately identical in every row** — it is a fielding
accident between the wickets, and the old table's 8%-for-pace against
12%-for-spin was modelling a relationship that does not exist.

Measured after: Caught 61.7, Bowled 16.9, Run Out 9.0, LBW 7.4, Stumped 5.0.

### Twos outnumbered fours on every pitch

| | engine | real T20 |
|---|---|---|
| Fours | 7.7% of balls | ~11.5% |
| Twos | 11.0% | ~6.5% |

All eight pitch profiles had the two nearly inverted — ratios from 0.85 to 1.45
twos per four, against a real ~0.57. Fours are the currency of T20; a scorecard
where batters are running twos instead of finding the rope does not read like
the format. Fixed by transferring 35% of each profile's `Double` weight to
`Four`, which is exactly sum-preserving, then restoring par with `run_factor`.

Measured after: fours 11.4%, twos 8.1%.

### The batter who got in ran away with it

Centuries in **20% of innings**, against a real rate near 3.5%, and openers
striking at 173 against a real ~135.

The cause was the "graduated confidence" curve in `compute_weighted_prob`: past
fifty, a batter was permanently 20% better than their rating, and the bonus was
worth exactly as much at 90 as at 50. The ListA branch of the same code had
already been flattened — its comment says, in as many words, *"keeps this curve
flatter to reduce opener snowballing"* — and T20 had simply been left steep.

**The thing to know before touching it again:** the curve is amplified. It tops
out at +12% on *effective batting*, but the weighting function is steeply
non-linear, so it arrives as roughly **+50% on the boundary weight** — about a
3.5× multiplier on whatever the constant reads. Trimming 1.20 to 1.12 moved par
by twenty runs. `tests/test_cricket_realism.py` pins that ratio so the next
person finds it as a test failure rather than as a mystery.

### A batter tied down did nothing about it

Nothing modelled the most ordinary situation in cricket: four dots, and the
batter has to do something. Dots clustered — an intent holds for a whole over
and the pressure layers reinforce it — with no counter-pressure.

`DOT_PRESSURE` in `services/cipl_match.py` is that counter. From three
consecutive dots the batter starts looking for the boundary, and by five they
are taking a real risk: the dot weight drops to 40% and the wicket weight rises
18%. It has to cut both ways, or being becalmed would just be free runs.

## What is still off, and why it is left alone

* **Centuries are ~4× too common** (0.136 per innings against ~0.035). Halved
  from 0.20, and the residual is a fat right tail on individual scores rather
  than the confidence curve, which now plateaus. Flattening further would start
  taking away the real advantage of being set, and would need another full
  recalibration pass; it is worth doing deliberately, not as a side effect.
* **Openers strike at ~155 against a real ~135.** Same root, same argument.
* **Sixes run slightly light** (4.8% against 5.5%).

## A measurement trap, recorded so it is not rediscovered

The first audit reported maidens at four times the real rate. It was wrong —
it was counting pure maidens and wicket maidens together. A wicket ball *is* a
dot ball, and the new batter arrives with a settling penalty, so wicket maidens
are several times more common than pure ones. Split apart, the engine bowls
about 0.4% pure maidens and 0.7% wicket maidens, against real figures near 0.15%
and 0.7%. The wicket-maiden rate is right; pure maidens run a little rich, which
is the honest remaining figure rather than the four-times-worse one the combined
count implied.

(The denominator matters too, and got its own correction: an over is counted as
six legal balls, not as one call of ``simulate_over``. A chase finishing mid-over
produces a part-over that can never be a maiden, and counting it whole quietly
understated both rates.)

`tools/realism_audit.py` reports the two separately for exactly this reason, and
says so in its docstring.

## Where things live

| Thing | File |
|---|---|
| Dismissal model (style × phase × intent) | `engine/ball_outcome.py` (`_DISMISSAL_BASE`, `_get_wicket_type_by_bowling`) |
| Set-batter confidence curve | `engine/ball_outcome.py` (`compute_weighted_prob`) |
| Dot-ball pressure | `services/cipl_match.py` (`DOT_PRESSURE`, `_make_dot_pressure_hook`) |
| Boundary balance per pitch | `config/ground_conditions*.yaml` (`scoring_matrix`) |
| The audit | `tools/realism_audit.py` |
| Regression guards | `tests/test_cricket_realism.py` |

## Calibration state after this work

Par sits inside its Section 5A band on all seven measured pitches, and the
global Sub-100 rate is 0.92% against the doc's "<2% globally". Measured with
`python -m tools.pitch_calibration --n 350`:

| Pitch | par | band | | Pitch | par | band |
|---|---|---|---|---|---|---|
| Flat | 227 | 225–240 | | Dry | 172 | 170–182 |
| Hard | 213 | 212–226 | | Green | 167 | 165–178 |
| Even | 199 | 190–202 | | Dusty | 159 | 152–165 |
| Bouncy | 184 | 180–192 | | | | |

These are Monte-Carlo medians, so read them with a couple of runs' tolerance:
at `--n 300` the same build put Hard and Green a run *below* their bands, and at
`--n 350` both sit inside. A pitch drifting one or two runs across a band edge
between runs is noise, not a regression — which is why the harness carries a
`par_pad` rather than testing the edge exactly. Judge a real change at `--n 300`
or more.

Anyone changing the ball model should re-run **both** harnesses — the totals can
stay perfect while the cricket underneath them stops making sense, which is the
whole reason the second one exists.
