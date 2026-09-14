# Tournament injuries

An opt-in cricket injury system for a Challenge League Tournament. When it is on,
finishing a match can leave a player carrying a knock that rules them out of
their team's **next few tournament matches** — they cannot be picked in the XI
until they are fit again.

Off by default, and scoped to one tournament: an injured card is fit everywhere
else in the bot, including in another tournament running at the same time.

| Setting | Column | Default |
| --- | --- | --- |
| On / off | `tournaments.injuries_enabled` | off |
| Chance, per team per match | `tournaments.injury_chance` | 12% |
| Hardest possible layoff | `tournaments.injury_max_matches` | 3 matches |

---

## How an injury is decided

The victim is drawn from the players who **actually did the work** in the match
just played, weighted by workload — balls bowled count double, because bowling is
the repetitive high-load action that really breaks people down. Someone who
neither batted nor bowled is never picked, so a bench-warmer can't pull a
hamstring.

What they were doing then decides the *kind* of injury, so the news reads like
something that happened in that match:

| They were… | Injuries drawn from |
| --- | --- |
| Bowling | Side Strain, Shoulder Niggle, Ankle Roll, Hamstring Tightness, Back Spasm, Split Webbing |
| Batting | Hamstring Tear, Concussion, Blow to the Hand, Cramp, Groin Strain, Bruised Ribs |
| Fielding | Dislocated Finger, Shoulder Knock, Corked Thigh, Twisted Knee |

Severity is a three-rung ladder, weighted hard towards the minor end — a
tournament where every knock is a three-match layoff stops being cricket and
starts being attrition:

| Severity | Matches out | Weight |
| --- | --- | --- |
| Niggle | 1 | 55% |
| Strain | 2 | 30% |
| Serious | 3 (up to the cap) | 15% |

Each injury type also has its own ceiling, so a Cramp is never a three-match
layoff and a Concussion can be. Raise `injury_max_matches` to 4 or 5 and the top
rung stretches to it; the other two never do.

## Rules that hold no matter what the dice say

* **A team is never injured below a fieldable XI.** If a knock would leave fewer
  than 11 fit players in the squad, it is not applied. The tournament has to stay
  playable.
* **Nobody is injured twice over.** An already-injured player is not in the pool.
* **A player hurt today does not have today counted as served.** The count-down
  runs before the roll, so a two-match injury really costs two matches.
* **Only Challenge League tournaments generate injuries.** A Lets Play
  tournament's "team" is a person playing their own roster, which this does not
  model.
* **A failed roll never costs a result.** Everything is wrapped: if the injury
  step raises, the match is still recorded, the points table still updates, and
  the error goes to the log.

Both steps run inside the match-recording transaction, which is idempotent on
`match_id` — so a replayed finalize cannot serve a match twice or injure a second
player.

---

## What it looks like in play

**After the match**, outside the collapsed recap — a player being ruled out is
the thing both captains most need to see:

```text
🚑 Injury news
❌ Bumrah (MI) — pulled up in his delivery stride. Side Strain — out for 2 matches.
✅ Rahul (LSG) is fit again.
```

**At selection**, the injured are removed from the squad list entirely — the
picker's numbering, the typed quick-select and the bot's XI builder all work off
one list, so removing them there is what makes "ruled out" mean ruled out. The
prompt says who is missing, so a shorter list is never a mystery:

```text
🚑 Unavailable (injured):
• Bumrah — Side Strain, out 2 more matches
```

**Any time**, with `/ctinjuries` (or the 🚑 Injuries tab on `/ctour`, which only
appears when the tournament has the system on):

```text
🚑 Summer Trophy — Injury List

Alpha 👈 yours
  ❌ Bumrah — Side Strain · out 2 more matches

Bravo
  ❌ Rahul — Concussion · out 1 more match
```

### If a squad runs out of fit players

The generator refuses to cause this, so it only happens when an admin rules
somebody out by hand. The XI picker then says so plainly rather than letting a
captain hunt for an eleventh name:

> 🚑 Alpha only has 9 fit players — 6 are injured. An admin needs to clear an
> injury or add to the squad before this match can be played.

---

## Admin

On the tournament's **Manage** page:

* the three settings above, in Settings;
* a **🚑 Injuries** section listing everyone currently out, with **Declare fit**
  per player and **Clear all injuries**;
* **Rule out a player** — pick a team, a player from its squad, a name for the
  injury and a number of matches. It replaces any injury that player is already
  carrying rather than stacking a second one on top, and is capped by the
  tournament's own ceiling.

Resetting a tournament empties the treatment room along with the results — a
replayed tournament must not start with players hurt in matches that no longer
exist.

---

## Tests

`tests/test_tournament_injuries.py` — the switch, the ladder and its ceiling, the
count-down landing exactly on time, the fieldable-XI and no-double-injury
guards, the workload-weighted victim pool, reset, and the squad the Playing XI
picker is actually handed.
