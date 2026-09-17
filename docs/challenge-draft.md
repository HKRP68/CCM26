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
| Slots 1–11 | both | Each slot offers two cards. The captain on the clock takes one; the other goes straight to their opponent. Either captain can **❌ Cancel Draft**. |
| Pitch | host | The usual surface picker, with the Pitch Report. |
| Playing XI | both | The eleven drafted cards, with **✅ Use draft order** for one tap. |
| Toss → match | both | Identical to `/cipl` from here — coin, elect, over by over. |

A captain has **60 seconds** a pick. Miss it and the bot takes the stronger card
for you and moves on; miss three in a row and the draft is called off. The
pick clock is the only clock during the draft — the normal setup timers take
back over at the pitch step.

Every slot card also carries **❌ Cancel Draft**, so two captains who want out
don't have to wait for the clock to run them out.

### Who can press what

A draft happens in a busy group, so every button is checked twice: the global
guard in `services/button_access.py` decides whether a callback is *personal*
(only the user whose command posted it may press) or *shared*, and a shared
button's handler then authorises the press itself against the draft state.
All of `/cdraft`'s buttons are shared, because every one of them is pressed by
somebody other than the person whose tap posted the card — and the handlers do
the real check:

| Button | Who may press it |
| --- | --- |
| 🙋 **Join Draft** | anyone in the chat — or only the player you replied to, when `/cdraft` was a reply. Never the host. |
| ❌ **Cancel** (lobby) | the host only |
| ❌ **Cancel Draft** (mid-draft) | either captain — not the rest of the group |
| 🅰️ / 🅱️ **pick** | only the captain on the clock, and only for the slot on the clock |

Anyone else gets told whose turn it is, rather than a bare refusal. The same
holds through the pitch, Playing XI and toss steps, which are the Challenge
League's own shared buttons.

> This is the one part of the mode that is easy to get wrong, because it is
> split across two files. A button whose prefix is missing from
> `SHARED_CALLBACK_PREFIXES` is silently unpressable by the other captain — the
> bug that made the first release of `/cdraft` impossible to start.
> `tests/test_cdraft.py::ButtonOwnershipTests` walks the keyboards the flow
> actually builds and fails if any of them is not shared, so a new button
> cannot ship with that hole.

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

The **roles** are fixed. The **ratings are not**: every slot draws its own
target at random from the admin's band, so no slot is pinned to either end and
no two drafts come out the same shape.

```text
a draft at 84–99
Slot   1   2   3   4   5   6   7   8   9  10  11
OVR   91  84  97  88  99  85  93  86  95  89  90

the next one
OVR   85  86  86  95  89  93  92  90  85  89  97
```

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
bowlers last) would put every bowler in the game below every batsman whenever
the ratings happened to trend downward, for both sides. Spreading the roles out
keeps that bias from being built into the mode.

### The pick order is a snake

```text
Slot   1  2  3  4  5  6  7  8  9 10 11
Picks  H  G  G  H  H  G  G  H  H  G  G
```

Strict alternation would give the host every odd slot — six picks, with the
advantage stacked on the earliest slots. The snake splits it **5 to the host,
6 to the guest**: the host's compensation is picking first, and the guest's is
the last pick.

### Fairness, concretely

* Both cards in a pair share a role and sit within the **pair spread** (±1 by
  default) of each other, so no slot can hand one captain the better player.
  Random ratings do not change this: a slot's rating is drawn once and applies
  to both cards, so whatever it draws, it draws for both captains at once.
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

### A tie goes to a Super Over

Nothing in `/cdraft` implements this, and nothing should. A drafted match
finishes through `handlers/cipl_play.py` → `_complete_match` like every other
over-by-over match, and the tie branch there sends any non-bot match to
`handlers/super_over.py`. A Challenge Draft always has two humans (the command
refuses a private chat and waits for someone to join), so it is never the bot
match that branch excludes.

The Super Over reads each side's eleven off `state['bat_xi']` /
`state['bowl_xi']`, which by then are engine player dicts like any other — so a
`DraftCard` is indistinguishable from a `ChallengePlayer` there, exactly as it
is everywhere else. The squad being exactly eleven is the only thing worth
checking, because selection needs three batters and a bowler out of it on every
replay; `tests/test_cdraft_super_over.py` plays real drafted squads through a
tie to a winner over several deals to pin that.

The lobby card says so, so captains know a level score will not simply be
declared a draw.

Two things are deliberately absent, both because there is no league behind the
squad: **XI memory** (`_resolve_team_id` returns `None`, and every drafted squad
is a one-off anyway) and the **overseas rule** (no home country, so the limits
stay at 0/11). Team Chemistry does not apply either — the same call already made
for league squads, for the same reason: these are not cards anyone collected.

### Teams, and what the match card says

A draft has no franchises, so each captain's team is named after them:
**"Shanka Draft XI"**, built from their Telegram first name (their handle if
they have no first name). The live match header needs a short code, and left to
itself `_team_short_code` would reduce that to the initials `SDX` — so the draft
supplies its own, `SHANKA`, through the `team_codes` map the draft dict already
carries. A league builds that same map with the very resolver the header would
have used, so this changes nothing for `/cipl`.

The pinned match card names the competition rather than always saying "IPL"
(`handlers/cipl_play._competition_line`):

```text
🏆 WHATEVER 🆚 SHANKA
🎯 Challenge Draft
━━━━━━━━━━━━━━━
🏟️ Sheikh Zayed Cricket Stadium • 20 overs
```

| Match | Line |
| --- | --- |
| `/cipl`, `/cbbl`, any league | `⚡ High-Voltage {league} Battle ⚡` |
| Tournament | `🏆 {tournament name}` |
| CL Tour | `🏆 {league} Tour · Match {n}/{m}` |
| `/cdraft` | `🎯 Challenge Draft` |

What the line needs travels on the match state, set in `begin_cipl_match`:
`league_name`, `tournament_name`, `cl_tour_*` and `mode_name`. Before this only
`league_key` reached the state, and only for a bot match — which is why every
card, in every league and every tournament, announced itself as IPL.

### Files

| File | Role |
| --- | --- |
| `services/cdraft_service.py` | The draft: the slot template, the snake, dealing the pairs, applying a pick, the `DraftCard` shim. No Telegram, no SQLAlchemy — unit-tested on its own, like `services/xi_rules.py`. |
| `handlers/cdraft.py` | The Telegram surface: the lobby, the slot cards, the pick buttons, the pick clock. |
| `handlers/cdraft_admin.py` | `/cdraftset` — the pool settings from a DM. |
| `admin.py` → `admin_match_settings` | The same settings on the website, plus the `_cdraft_*` helpers that build the version tick list and the feasibility warning. |
| `tests/test_cdraft.py` | The draft — including `test_every_draft_produces_two_legal_xis`, the mode's central claim checked over many random drafts against both rulebooks. |
| `tests/test_cdraft_admin.py` | The settings — `/cdraftset`, and source-level checks on the route, the template and the config plumbing. |
| `tests/test_cdraft_super_over.py` | A tied drafted match — the handoff out of `_complete_match`, and the Super Over played out on real drafted squads. |
| `tests/test_cipl_announcement.py` | The match card's competition line, for all four kinds of match — including that a league's wording is byte-identical to what it always was. |

---

## The pool — what an admin controls

Two settings decide which cards a draft may deal. Both live in `GameConfig`, so
the website and the bot always agree, and both are editable either way.

### On the website

**Admin → 🏏 Match Gameplay → 🎯 Challenge Draft pool**

* **Lowest / Highest rating** — the band every slot draws its own rating from.
  Not a ladder: no slot is pinned to either end.
* **Pair spread** — how far apart the two cards offered in one slot may be on
  OVR. Keep it small; at 0 a slot always offers two players on the same rating.
* **Allowed versions** — a checkbox per edition that exists in your `players`
  table, with a card count beside each. **Tick nothing and every edition is
  allowed**, including any you add later; tick some and a draft deals nothing
  else.

Saving runs a feasibility check and warns you — by role — if the settings can't
fill all eleven slots, so you find out before a group does.

### From Telegram

```text
/cdraftset                        the current pool, and whether it can be dealt
/cdraftset min 80                 the band's floor
/cdraftset max 92                 the band's ceiling
/cdraftset range 80 92            both at once
/cdraftset spread 1               ± OVR between the two cards in a slot
/cdraftset versions Base, Legend  only these editions may be dealt
/cdraftset versions all           clear the list — every edition allowed
/cdraftset reset                  back to the defaults
```

Admin-only. Edition names are matched against the catalogue case-insensitively;
a name that isn't there is rejected with the valid list rather than stored — a
typo that silently emptied the pool would only surface as a draft nobody can
start. With no arguments it prints the current pool plus how many *different*
cricketers each role has against how many it needs (8 batsmen, 8 bowlers,
4 all-rounders, 2 keepers — two per slot, since a pair is two different players).

### Rating gives, versions don't

The two settings are not equally binding, and the difference is deliberate:

* A **rating** that has no pair for some role looks elsewhere **inside the
  band** first, a point at a time, and only leaves the band once every rating
  in it has been tried. So a thin rating borrows from the rest of the pool you
  allowed, and breaks your range only when the whole of it has nothing.
  (The band always comes from your settings, never from whichever ratings the
  eleven slots happened to draw.)
* A **version** you untick is *never* dealt. If the allowed editions genuinely
  can't supply a role, `/cdraft` refuses and names the role, rather than
  reaching for a card you excluded.

Both squads are dealt from the same pool, so narrowing it never favours one
captain.

### Environment fallbacks

The `CDRAFT_*` variables are the defaults used when nothing is stored, so an
install that never opens the admin panel behaves exactly as it always has. The
stored settings win wherever they are set.

| Variable | Default | What it does |
| --- | --- | --- |
| `CDRAFT_RATING_TOP` | `88` | The band's ceiling, until set on the website. |
| `CDRAFT_RATING_BOTTOM` | `78` | The band's floor, until set on the website. |
| `CDRAFT_PAIR_SPREAD` | `1` | Max OVR gap in one slot, until set on the website. |
| `CDRAFT_PICK_SECONDS` | `60` | Seconds per pick before the bot picks for you. |
| `CDRAFT_MAX_AUTO_PICKS` | `3` | Lapsed picks in a row that end the draft. |

Adding a setting here is the repo's usual four steps, and all four are pinned by
`tests/test_cdraft_admin.py::ConfigPlumbingTests`: the column on
`models.GameConfig`, the key in `services/config_service.DEFAULTS` (`save_config`
silently ignores keys that aren't in it), the `_try_add("game_config", …)` line
in `database._migrate_add_columns`, and the form field.
