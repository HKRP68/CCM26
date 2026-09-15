# Challenge Draft — `/cdraft`

Two players. One command. Eleven picks each way. Then a Challenge League match.

`/cdraft` is the Challenge League without the Challenge League: no franchise, no
admin-loaded squad, no cards you had to own first. Both XIs are built live, pick
by pick, out of the master card catalogue — and the way the cards are dealt makes
the two sides provably symmetrical, so the result is a test of selection and
captaincy rather than of who has the better collection.

Career Players (`/cmucareer`) are never dealt. The pool is
`services/player_cache.py`, which already applies `player_service.not_career`.

---

## Playing one

```text
/cdraft                 in a group — anyone can tap Join
/cdraft  (as a reply)   only the player you replied to can join
```

| Step | Who | What happens |
| --- | --- | --- |
| Lobby | anyone | A second player taps **🙋 Join Draft**. The host can **❌ Cancel** until then. |
| Slots 1–11 | both | Each slot offers two cards. The captain on the clock takes one; the other goes straight to their opponent. |
| Pitch | host | The usual surface picker, with the Pitch Report. |
| Playing XI | both | The eleven drafted cards, with **✅ Use draft order** for one tap. |
| Toss → match | both | Identical to `/cipl` from here — coin, elect, over by over. |

A captain has **60 seconds** a pick. Miss it and the bot takes the stronger card
for you and moves on; miss three in a row and the draft is called off. The
pick clock is the only clock during the draft — the normal setup timers take
back over at the pitch step.

---

## What a slot offers

Every slot carries a **role** and a **target rating**, and deals two cards that
share that role and sit within **1 OVR** of each other:

```text
🎯 CHALLENGE DRAFT · Slot 4/11
═════════════════════════════
🌟 All-rounder · ~85 OVR

🅰️ R Jadeja
     85 OVR · BAT 82 · BWL 86 · India

🅱️ B Stokes
     85 OVR · BAT 84 · BWL 83 · England

⏱ @alice — pick one. The other goes to @bob.
```

Because both cards share the role, *both* squads gain that role — whoever picks.
That is the whole trick, and it is what "the Playing XI rules apply in the draft"
means here.

### The template

| Slot | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Role | Bat | Bowl | Bat | All | Bowl | Bat | WK | Bowl | Bat | All | Bowl |
| ~OVR | 88 | 87 | 86 | 85 | 84 | 83 | 82 | 81 | 80 | 79 | 78 |

Four batsmen, one keeper, two all-rounders, four bowlers — **4/1/2/4**, the same
shape the AI opponent's XI is built to (`services.bot_xi_builder.LPBOT_SHAPE`),
and legal under both of the game's rulebooks:

* **Challenge League XI** (`xi_rules.validate_challenge_xi`) — 11 players, at
  least one keeper, at least five bowling options. This gives six.
* **Roster XI** (`xi_rules.validate_roster_xi`) — 3–5 batsmen, 3–5 bowlers, 1–2
  keepers, 1–3 all-rounders. At exactly two all-rounders the "third all-rounder
  must bowl worse than every pure bowler" clause never comes into play, so a
  legal XI never depends on how the pool's ratings happen to fall.

An illegal XI is therefore **unreachable**, not merely rejected. A slot's role is
never swapped for another to fill it: if the catalogue cannot supply two
different keepers, `/cdraft` says so and refuses to start rather than deal a
squad that could not take the field.

**Why the roles are interleaved.** Blocking them (four batsmen first, four
bowlers last) would put every bowler in the game ~10 OVR below every batsman, in
every draft, for both sides. Spreading them down the ladder keeps the
marquee-first feel without building that bias into the mode.

### The pick order is a snake

```text
Slot   1  2  3  4  5  6  7  8  9 10 11
Picks  H  G  G  H  H  G  G  H  H  G  G
```

Strict alternation would give the host every odd slot — six picks, with the
advantage stacked on the best cards at the top of the ladder. The snake splits it
**5 to the host, 6 to the guest**: the host's compensation is picking first, and
the guest's is the last pick.

### Fairness, concretely

* Both cards in a pair share a role and sit within `CDRAFT_PAIR_SPREAD` OVR, so
  no slot can hand one captain the better player.
* Both squads finish on the same 4/1/2/4 shape.
* Total squad OVR therefore lands within a point a slot of each other — in
  practice, a handful of points across all eleven.
* No cricketer is dealt twice in a draft. De-duplication is by **name**, not id,
  so two versions of the same card (Base, Gold, Icon…) can never both appear.

---

## How it plugs into the existing game

The object that carries a Challenge League match through setup is the *draft
dict* in `context.bot_data`. `/cdraft` builds one of exactly that shape with two
extra keys — `mode: "cdraft"` and the `cdraft` state — so from the pitch step
onward every existing callback drives it unchanged: `cl_pitch_`, `cl_xi_`,
`cl_pick_`, `cl_confirm_`, `cl_start_`, `cipl_coin_`, `cipl_toss_`.

Exactly two functions need to know the squad was drafted rather than loaded:

| Function | What changes |
| --- | --- |
| `handlers/challenge.py` → `_query_team_players` | Returns the drafted cards instead of a `ChallengeTeam`'s `ChallengePlayer` rows. |
| `handlers/cipl_play.py` → `build_xi_from_draft` | Resolves the confirmed XI against the draft instead of querying the database. |

Everything above them — the XI picker and its numbered quick-select, the XI
rulebook, `cipl_match.cp_to_player_dict`, the scorecard, POTM, stats — is
untouched, because a drafted card wears a `ChallengePlayer`'s clothes:
`services.cdraft_service.DraftCard` exposes the same `id` / `name` /
`details_json` / `is_overseas` surface. There are **no throwaway database rows**.

`DraftCard.id` is the real `Player.id`, so the engine's `roster_id` and
`player_id` both land on the master card and Player-of-the-Match and global
player stats are recorded against it like any other match.

Two things are deliberately absent, both because there is no league behind the
squad: **XI memory** (`_resolve_team_id` returns `None`, and every drafted squad
is a one-off anyway) and the **overseas rule** (no home country, so the limits
stay at 0/11). Team Chemistry does not apply either — the same call already made
for league squads, for the same reason: these are not cards anyone collected.

### Files

| File | Role |
| --- | --- |
| `services/cdraft_service.py` | The draft: the slot template, the snake, dealing the pairs, applying a pick, the `DraftCard` shim. No Telegram, no SQLAlchemy — unit-tested on its own, like `services/xi_rules.py`. |
| `handlers/cdraft.py` | The Telegram surface: the lobby, the slot cards, the pick buttons, the pick clock. |
| `tests/test_cdraft.py` | Both — including `test_every_draft_produces_two_legal_xis`, which is the mode's central claim checked over many random drafts against both rulebooks. |

---

## Settings

All optional; the defaults are what the mode runs on.

| Variable | Default | What it does |
| --- | --- | --- |
| `CDRAFT_RATING_TOP` | `88` | Target OVR of slot 1. |
| `CDRAFT_RATING_BOTTOM` | `78` | Target OVR of slot 11. |
| `CDRAFT_PAIR_SPREAD` | `1` | Max OVR gap between the two cards in one slot. |
| `CDRAFT_PICK_SECONDS` | `60` | Seconds per pick before the bot picks for you. |
| `CDRAFT_MAX_AUTO_PICKS` | `3` | Lapsed picks in a row that end the draft. |

If a rating band is thin the search widens from the target until it finds a pair,
so a small catalogue still produces a full draft — just closer together on the
ladder than the settings ask for.
