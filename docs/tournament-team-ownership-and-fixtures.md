# Tournament team ownership, fixed pitches & the fixture card

Three rules a Challenge League Tournament — including one published from a
Tournament Draft — can now enforce, plus the read-only card players follow it
with. Every one of them is **opt-in**: a tournament that doesn't turn them on
behaves exactly as it did before.

| Rule | Column | Default |
| --- | --- | --- |
| A team may only be played by the people who run it | `tournaments.enforce_team_owner` | off |
| Each fixture fixes the pitch it is played on | `tournaments.pitch_mode` | `host` (the host picks) |
| Overseas-in-XI limits for this tournament | `tournaments.min_overseas` / `max_overseas` | inherit the league |

---

## 1. One team, one owner — and any number of co-owners

`TournamentTeam` carries `owner_tg_id` + `owner_name`, plus
`co_owner_ids_json`: a JSON list of extra **Telegram ids** who run the same
team. Telegram ids, for the same reason a Lets Play participant is one: an admin
assigns them from a list, and some of those people have never run `/debut`.

**The owner and its co-owners are equals** for every check the tournament makes
— any of them may pick the team, play its fixtures, and see its remaining
matches under "Your next matches". The owner is distinguished only by being the
name shown on the team. This mirrors `DraftTeam`, which has had co-owners since
the draft's own `/pick` flow.

With **Team ownership** ticked on the tournament's Manage page:

* the tournament command refuses anyone who neither owns nor co-owns a team in
  the field, and refuses a challenge whose *opponent* has none either — checked
  before a picker is even opened, so nobody finds out one tap later;
* the team picker only shows the teams that player may field;
* the pick itself is re-checked when it arrives, so a stale card or a replayed
  button can't get around the hidden keyboard.

A team **nobody runs stays open to everybody**. That is deliberate: a
half-assigned tournament must never lock its own players out of the teams an
admin simply hasn't got to yet. "Nobody runs it" means no owner *and* no
co-owners — so clearing a team's owner leaves its co-owners in charge rather
than handing the team back to the whole chat, and promoting a co-owner to owner
drops them from the co-owner list instead of listing them twice.

Admins set co-owners from the same row as the owner on the Manage page: a
comma-separated box of Telegram ids. Blanks, duplicates, non-numbers and the
owner's own id are all ignored, so the raw box can be pasted straight in.

### Draft tournaments get their owners for free

A Challenge League published from a Tournament Draft already knows who runs each
franchise — `DraftTeam.owner_tg_id` and `DraftTeam.co_owner_ids_json`. Adding a
team to a tournament inherits both, and **👤 Inherit owners from the draft** on
the Manage page back-fills a tournament that was built before them. It never
overwrites an assignment made by hand — a team that already has an owner *or* a
co-owner is left alone entirely.

---

## 2. The pitch is a property of the fixture

`TournamentMatch` carries `pitch_type`, `home_team_id` and `venue`. What fills
them is the tournament's `pitch_mode`:

| Mode | What the generator stamps | What the host sees |
| --- | --- | --- |
| `host` | home side only | the pitch picker, as before |
| `fixture` | a random surface per fixture | the pitch, announced — no picker |
| `home` | the **home team's** `home_pitch`, random where none is declared | the same |

In the two fixed modes setup skips the pitch step entirely: the surface is
announced with its full Pitch Report and the draft goes straight to Playing XI
selection. The guest keeps their **❌ Deny Match** button — it moves onto the
announcement card, which is where it used to live.

The fixture stays the authority right up to the toss: when the match reserves
its fixture, the surface is re-read from the row actually reserved (and the
weather conditions rebuilt with it), so an admin edit between setup and the toss
can't leave a match played on a pitch the fixture didn't fix.

**What is never rewritten:** a match that has been played. `assign_fixture_venues`
only touches fixtures still `scheduled`; a completed one keeps the surface it was
played on, and `set_fixture_pitch` refuses it outright.

Admins edit both per fixture on the **Schedule** page (🏠 home side, 🌱 pitch),
or re-roll every unplayed pitch at once with **Re-roll pitches**.

### Home teams

The round-robin generator makes slot 1 the home side. The circle method already
mirrors the slots in a double round-robin, so each pair gets one home leg each
with no extra bookkeeping. Swapping a team out of a fixture moves the home flag
with it, so `home_team_id` is always one of the two sides — or `NULL` for a
neutral venue.

---

## 3. Overseas in the XI, per tournament

`Tournament.min_overseas` / `max_overseas` override
`ChallengeLeague.min_overseas` / `max_overseas` for that tournament's matches.
Each bound inherits **independently**, and `NULL` is the only value that means
"inherit":

* an **empty box** on the admin form → inherit the league's rule;
* a typed **`0`** → a real rule, *no overseas players at all*.

The XI picker's live rule checkbox reads the tournament's numbers whenever the
match is a tournament match.

---

## 4. What players see

Four read-only commands, open to anyone, all showing the **active** Challenge
League Tournament:

| Command | Shows |
| --- | --- |
| `/ctour` (`/ctournament`) | the hub — overview, with tabs for the rest |
| `/cttable` (`/ctpoints`) | points table with net run rate |
| `/ctfixtures` (`/ctfix`) | the schedule |
| `/ctteams` | the field, who owns each team, and how many co-owners (🤝) |
| `/ctinjuries` (`/ctinjury`) | the treatment room — see [tournament-injuries.md](tournament-injuries.md) |

A league can publish its **own alias** for the hub:
`ChallengeLeague.fixtures_command` (e.g. `/iplfixtures`), set next to the
tournament command on the league's admin page. Unlike the tournament command —
which *starts* a match and is gated to approved players — the fixtures command
is read-only and ungated.

These are deliberately **not** in Telegram's slash menu: both menu scopes
are at Telegram's 100-commands-per-scope ceiling, and pushing one over silently
truncates somebody else's command off the tail. They are advertised in `/help`,
from the hub's own buttons, and by whatever alias a league sets.

### The fixture card

```text
🗓️ Summer Trophy — Fixtures

Your next matches
M3  ⚪ Alpha 🏠 vs Delta · 🌱 Dusty

Full schedule
M1  ✅ A̶l̶p̶h̶a̶ ̶v̶s̶ ̶B̶r̶a̶v̶o̶ — Alpha won by 12 runs
M2  🔴 Charlie 🏠 vs Delta — in progress
M3  ⚪ Alpha 🏠 vs Delta · 🌱 Dusty

🏠 home side · 🌱 the pitch this match must be played on · struck-through matches are done.
```

A completed fixture is **struck through** and carries its result; a live one is
flagged in progress; an unplayed one shows the surface it is pinned to. When the
viewer owns *or co-owns* a team, their own remaining fixtures are pulled out on
top — the thing a team owner actually opens this for.

---

## Tests

* `tests/test_tournament_rules.py` — the rules: ownership and co-ownership,
  draft inheritance, the three pitch modes, overseas inheritance, and the
  rendered card.
* `tests/test_tournament_team_lock.py` — the bot flow: the picker refusing a
  team that isn't yours, and the fixture's surface being found from team names.
