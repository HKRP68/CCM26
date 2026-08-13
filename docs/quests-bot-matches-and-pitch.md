# Quests: AI matches, the daily cap, pitch and match-length quests

What moves a quest bar, which quests are guaranteed to show up each day, and
the operational notes for all of it.

## AI matches complete quests again

`/wpmbot` and `/vsbot` — the two AI modes that pay coins, gems and a Win/Loss
record — now feed match-end quests. Previously every bot match was refused by
`services.quest_service.match_counts_for_quests`, so a player with nobody online
could win ten matches and watch "Play 3 matches today" sit at 0/3.

The anti-farming concern that closed the door originally is still real: the AI is
always available and never refuses. So the door is open with a limit rather than
wide open.

**The daily cap.** Each user may feed quests from the first
`BOT_QUEST_MATCH_DAILY_CAP` AI matches per UTC day (default **3**, settable via
the environment variable of the same name; `0` disables AI quest progress
entirely). The counter lives on the user row —

| column | meaning |
| --- | --- |
| `users.bot_quest_matches_today` | AI matches that fed quests today |
| `users.bot_quest_matches_date` | the UTC date those belong to (`YYYY-MM-DD`) |

— and resets lazily on the first bot match of a new day, the same pattern Quick
Match's daily limit uses. Both columns are added by
`database._migrate_add_columns` on boot; there is nothing to run by hand.

A bot match past the cap still pays out in full. It just stops moving quest bars.

**What still doesn't count.** `/lpbot` and `/ciplbot` are explicitly *unranked
practice* — `handlers.cipl_play.mark_bot_match` sets `stats_disabled` and
`unranked`, and they already pay no coins, gems, W/L or streak. They stay
refused, alongside spectator matches, bot-vs-bot matches, and matches the
fair-match gate voided for a 10+ Team Overall gap.

**Two event keys came back.** `vsbot_played` and `vsbot_won` were retired when
bot matches stopped counting; they now fire again, from AI matches only, and are
selectable on the admin quest form. A quest on either key can never be completed
in a PvP game — and its `target_count` must be at or under the daily cap, or it
cannot be completed at all.

## Pitch quests

`track_user_match_quests` now fires one event per finished match naming the
surface it was played on, so a quest can ask for "play 3 matches on a Green
pitch today".

The key is `pitch_<surface lowercased>`, built by
`services.quest_service.pitch_event_key` from the match state's own
`pitch_type`. The surfaces come from `services.match_constants.PITCH_TYPES`, so
adding a pitch there adds its quest key with no second list to update:

`pitch_dry` · `pitch_dusty` · `pitch_hard` · `pitch_even` · `pitch_flat` ·
`pitch_green` · `pitch_bouncy`

The pitch is randomised per match (or chosen by the host in Challenge League), so
these quests ask a player to keep playing until the surface comes up rather than
to grind a stat — which is why the seeded targets are 2–3, not 10.

## Match-length quests

The host picks the length when they open a lobby (`/wpm <overs>`, 1–20), so a
quest can ask for a *format* rather than a stat: "play 3 matches of 15 overs or
more today".

`track_user_match_quests` reads the match's own `overs` off the state and fires
one event per threshold that length **clears** — the keys are a ladder, not a
partition:

| length played | events fired |
| --- | --- |
| 20 overs (120 balls) | `overs_10`, `overs_15`, `overs_20` |
| 15–19 overs | `overs_10`, `overs_15` |
| The Hundred (100 balls) | `overs_10`, `overs_15` |
| 10–14 overs | `overs_10` |
| under 10 | none |

Clearing rather than matching is deliberate. If `overs_10` meant *exactly* ten,
a player who chose the longest format would watch "play 2 matches of 10+ overs"
sit at 0/2 all day, which reads as a bug from the other side of the screen.

**The comparison is in balls, not in `overs`.** `overs` does not mean the same
thing in every format: Challenge League's The Hundred stores an innings as 20
*sets of five*, so a 100-ball Hundred match carries `overs == 20` exactly as a
120-ball T20 does. Taken at face value that let a Hundred game complete "Full
Twenty" twenty balls short. `match_balls_per_unit` reads the ball count for the
state's format straight out of `services.cipl_match`, so there is one
definition of how long a format is rather than a copy here that can drift — and
a Hundred match now clears 10 and 15, which by length it genuinely does, but
not 20.

The thresholds live in `services.quest_service.MATCH_LENGTH_THRESHOLDS`, and
`match_length_event_keys` builds the keys from them — adding a threshold there
adds its key everywhere, with no second list to update. A state whose `overs` is
missing or unreadable clears nothing rather than falling back to a default, so a
mode that forgets to record its own length can never hand out a 20-over quest
for free.

The three seeded quests are **Double Header** (2 × 10+ overs, tier 1), **Long
Format** (3 × 15+ overs, tier 2) and **Full Twenty** (4 × 20 overs, tier 3).
Target and length climb together on purpose — the three are one difficulty
ladder, and the tier prices each accordingly, so Full Twenty is the family's
heaviest ask rather than its lightest. Four full T20s is a real session; if that
proves too steep in practice, the target is the cheapest thing to tune, editable
per quest on the admin quest form with no deploy.

## Both families are guaranteed a slot every day

Pitch and match-length quests are only worth holding if they turn up often
enough to plan a session around. Left in the ordinary random draw, a player saw
a pitch quest roughly one day in five and a length quest less often still — not
a theme, just an occasional curiosity.

The reference point is **Ad Enthusiast**, the pinned "watch 5 ads today" quest
`database.py` seeds — it is on every card, every day. Pitch and match-length
quests now show up just as reliably, but a pin was the wrong tool for it: a pin
is per *quest*, so pinning the pitch family would deal all seven surfaces to
everybody every day. A reserved slot per family gives the same reliability and
still rotates which surface and which format the day asks for.

So the daily deal now reserves a slot for each family:

```text
daily card = 3 random  +  1 pitch quest  +  1 match-length quest  (+ any pins)
```

The two are drawn **on top of** the random three, not out of them, so nobody
loses general quests to make room — the same arrangement
`CAREER_QUESTS_PER_USER` already uses for career weeklies, for the same reason:
a category that has to appear cannot be made to compete for the general slots.
Note that this does mean two more quests' worth of rewards per player per day.

Which surface and which format the day asks for still rotates, and yesterday's
picks go to the back of the queue, so the same pitch quest rarely lands two days
running. The reserved families are removed from the random pool entirely, so the
draw can never spend a general slot on a second pitch quest.

The families are declared in one place —
`services.quest_service.DAILY_GUARANTEED_BUCKETS` — as `(family, event keys,
slots)`. Some consequences worth knowing:

* **Pinning still works, and doesn't double up — at deal time.** A pitch quest
  an admin pins (`always_assign`) *fills* the pitch slot instead of arriving
  next to it, so a card dealt after the pin carries that pitch quest rather
  than two.

  The exception is a pin added *mid-day*: everybody who already opened their
  card that day ends up holding two, their morning draw plus the pin. The
  top-up only ever adds. Trimming instead would mean unassigning a quest the
  player may already have progress on, and skipping the pin for those users
  would break what a pin means everywhere else ("every user gets this one,
  now"). It is one bonus quest for one day; the next daily deal puts them back
  on one per family.
* **An empty family costs nobody a slot.** Deactivate every pitch quest and
  players simply get their random three and a length quest.
* **A mid-day rollout reaches everyone the same day.** The daily deal normally
  happens once, on a user's first quest read of the day.
  `_top_up_daily_guaranteed` fills a family a user is missing on their *next*
  read, so shipping this at noon does not leave the whole player base waiting
  until 00:00 UTC.

Weekly and monthly deals are untouched.

## Rolling the new quests out

The seven pitch quests, three match-length quests and two AI quests are in
`seed_quests_v3.py`. They are **not** live until that script is run against the
database:

```bash
python seed_quests_v3.py --dry-run   # report what would change
python seed_quests_v3.py             # replace the daily/monthly catalogue
```

The seeder is idempotent and self-cleaning: a quest already present by name is
updated in place, and any other active daily/monthly quest is deactivated.
Pinned (`always_assign`) and Career Player quests are never touched.

Until it runs, everything above is inert — the events fire, but no quest is
listening for them, and a guaranteed slot with nothing active to draw from
simply stays empty.

## Every quest, and the ball loop behind it

Two reports — "the dot ball quest never completes" and "the progress bar isn't
working" — turned out to be three separate things. Worth stating the shape of
the problem, because the first two look identical from the player's side and
neither is a fault in the quest catalogue.

A quest bar moves only if **all three** of these hold:

1. the quest's `event_key` is one something in the game fires
   (`services.quest_service`, or a handler like `/claim`);
2. the event's underlying figure is recorded by the ball loop of the mode the
   match was played in; and
3. the match itself counts — `match_counts_for_quests`, plus the AI daily cap.

(1) has been guarded for a while: `CatalogueTests` in
`tests/test_quest_counting.py` fails if any seeded quest sits on an event key
nothing fires, and no quest in `seed_quests_v3.py` is `manual`. Nothing in the
shipped catalogue is untriggered.

(2) was where the two live bugs were. Three interactive loops score balls, and
they have to agree on every per-player figure `track_user_match_quests` reads:

| loop | modes |
| --- | --- |
| `handlers/match.py` | in-chat `/wpm`, `/playmatch`, `/vsbot` |
| `services/match_webapp_service.py` | the Mini App |
| `services/cipl_match.py` | `/letsplay`, Challenge League, CL Tour |

**Dot balls.** `cipl_match` recorded every figure the tracker reads except
`dots` — so "Bowl 30 dot balls today" and the career `career_dot_balls` quests
sat on 0 for a whole /letsplay or Challenge League match, however many dots were
bowled. The bowling scorecard's dots column was blank there for the same reason.
All three loops now credit a dot to the bowler *and* the batter on a no-run
legal delivery, including a wicket off a no-run ball.

That gap survived because the career audit
(`tests/test_career_weekly_catalogue.py`) only scanned two of the three loops:
`cipl_match` was excluded back when /letsplay fired no quest events at all, and
was never added when it was wired into the shared tracker. It is in the list
now, so the next missing stat fails a test instead of a quest.

**Powerplay and death overs.** `_phase_runs` scores a phase off the per-over run
list: innings 1 from `inn1_over_runs`, innings 2 from the live `over_runs`. The
Mini App and Challenge League archive that list at the innings break; the in-chat
loop never did, and never cleared it either. One list therefore held both
innings, so "Score 45+ in the first 6 overs" read **nothing at all** for the side
batting first and the *opponent's* opening overs for the side batting second.
The in-chat break now archives and resets it, which also fixes the Manhattan
chart and the Arena over-by-over graph for in-chat matches.

(3) is unchanged, and is explained on the `/myquest` panel itself — unranked
practice, a voided Team Overall mismatch and the AI daily cap are all reasons a
finished match legitimately moves nothing.

## The progress bar

`/myquest` draws a ten-segment bar and truncated to whole tenths, so anything
under 10% drew an *empty* bar: the first few dots of a 30-dot quest looked
exactly like a quest whose trigger never fires — the same picture as the bug
above, which is why the two were reported together. A part-filled segment still
rounds down (99% must not read as finished), but any progress at all now lights
at least one segment. The Mini App draws a percentage-width bar and was never
affected.
