# /pitchstats

What the surfaces actually did, in matches people played.

Three things already describe a pitch, and none of them answered the question a
captain has at the toss:

| Source | Answers |
|---|---|
| `/matchhelp pitches` | what each surface is **designed** to do |
| `tools/pitch_calibration` | what the **engine** produces playing itself |
| **`/pitchstats`** | what happened when **people** played on it |

The third is the one that can disagree with the other two, and the command is
built so that disagreement is visible rather than buried.

## Using it

```
/pitchstats              every surface, one block each
/pitchstats Green        one surface in detail
/pitchstats approaches   every intent and plan, rated
/pitchstats Flat lp      …narrowed to Lets Play
```

Aliases: `/pstats`, `/ps`. Buttons on the card switch surface, switch view,
filter to **Lets Play** or **Challenge League**, and toggle **tournaments only**.

## What counts

Only the two modes that play the full Approach game between two people:

* **`/letsplay`**, including Lets Play Tournament fixtures
* **Challenge League** — `/cipl` and `/c<league>`, including tournaments and
  Challenge League Tours

Excluded, deliberately:

* **Practice matches against the AI captain** (`/lpbot`, `/ciplbot`). They are
  unranked, and the bot's approach picks would drown the human record the
  approach table exists to describe. The `matches` row of a bot match is
  indistinguishable from a human one — the only thing that tells them apart is
  `state["is_bot_match"]`, which is why the recorder reads the live state rather
  than the match type.
* Every other mode: `/cric`, `/playmatch`, `/wpm`, `/vsbot`, bot-vs-bot.
* Matches abandoned, force-ended or cleared. Those never reach a completion
  path, so they never reach the recorder.

## The numbers

**Everything is rated per six balls**, not per over bowled. A chase won in 17.2
overs lasted 104 balls, and dividing its runs by the 20-over match length would
make every successful chase look as though it slowed the pitch down. It also
means The Hundred's five-ball sets count for what they are.

| Stat | Read as |
|---|---|
| Runs/over | both innings' runs ÷ balls × 6 |
| Wickets/over | both innings' wickets ÷ balls × 6 |
| 1st inns avg | mean first-innings score, with high and low |
| Bat 1st / Bat 2nd win % | share of **decided** matches; ties are counted and shown separately |
| By phase | the same two rates split powerplay / middle / death |
| Toss | what winners elected, against the surface's own recommendation |
| Approaches | every intent and plan's run and wicket rate, plus the pairings |

A **tie** is excluded from the win split rather than assigned to a side — the
main innings genuinely had no winner. A tie broken by a Super Over is recorded
as a win for whoever won it, but the Super Over's own over is *not* added to the
surface's run rate: it is a separate contest at a length no pitch has a par for.

### The par-band verdict

Each surface's measured first-innings average is printed against the par band it
was designed for in `config/ground_conditions.yaml`, marked ✅ in band,
🔻 under or 🔺 over. A surface sitting outside its band is either mis-tuned or
being played in a way the design did not anticipate, and this is the only place
in the bot that can tell you which surfaces those are.

### The toss record

The surface's recommended call comes from `engine.pitch_registry` — the same one
the Pitch Report card shows a captain before they choose. The card reports how
often winners followed it, and the win rate when they did against when they did
not, so the recommendation is falsifiable rather than just asserted.

### Approaches

Rated per six balls over every over that intent or plan was used, sliceable by
surface and by phase. The **pairings** block is the only view that can say a
pick was good *against a specific reply*, which is the whole point of a
simultaneous-move game — and the lines are named for the over they produced
("most productive", "cheapest") rather than credited to one captain, because
both picks make the cell.

A pairing is 1 of 25 cells, so pairings under 8 overs are withheld. Printing
"Ultra vs Variation: 24.0 rpo" off two overs is worse than silence.

## Where it lives

| File | Role |
|---|---|
| `models.PitchMatchStat` | one row per finished match — balls per innings, phase splits, result, toss |
| `models.PitchApproachStat` | rolling totals per (pitch, mode, phase, intent, plan) |
| `services/pitch_stats.py` | the recorder and every query |
| `handlers/pitchstats.py` | the command and its card |

The recorder is called from `handlers/cipl_play._complete_match` (the shared
finish for `/letsplay`, `/cipl` and `/c<league>`) and from the Super Over's
finalise, in both cases while the live state still exists — `cleanup_state`
takes it away immediately afterwards, and the two things this needs live only
there:

* how many **balls** each innings lasted (`matches` stores runs and wickets, not
  balls);
* the **approach duel**, the over-by-over record of which intent met which plan.

It is idempotent on `match_id` and never raises: a stats write must not be able
to cost a player their result.

## Note on the slash menu

`/pitchstats` is **not** in `BOT_MENU_COMMANDS`. The private-chat menu is at
Telegram's 100-command ceiling exactly, and `bot._clamped` drops the *tail* of
the list on overflow — so publishing this one would silently unpublish
`/fantasyguide` instead. The command works from the keyboard either way and is
advertised in `/start`'s command list and by `/matchhelp pitches`. To give it a
menu slot, retire an entry from that list rather than growing it.
