# The Trait Vote: do both captains want traits in this match?

Design note for the pre-toss traits question in `/wpm` and `/letsplay`.

Reference implementation: `services/trait_vote_service.py` (the rule and the
wording), `handlers/match.py` (`/wpm`: the lobby prompt, and `_ball_traits` —
the gate every delivery passes), `handlers/letsplay.py` (`/letsplay`: the prompt
between accept and pitch, and the XI built without traits at launch),
`services/match_webapp_service.py` (`traits_enabled` on the live state and in the
Mini App snapshot), `config.py` (the four settings).
Tests: `tests/test_trait_vote.py`.

---

## 1. Summary

Traits are equipped days before a match and paid for in gems, so a head-to-head
between two squads is also a match between two trait investments. Not every
player wants that contest: a captain who has spent nothing on traits came for a
match between the cards, and a captain who has spent a fortune came for the
opposite. Both are reasonable, and neither of them is the game's to decide.

So it is put to the two of them, once, before the toss:

| Host | Guest | Result |
|---|---|---|
| Yes | Yes | traits **ON** — what every match did before this existed |
| No | No | traits **OFF** — cards only |
| Yes | No (or the reverse) | the **lower-rated XI's** answer stands |

Nothing is unequipped, sold, or refunded. A match played without traits is a
match; the traits are back for the next one.

---

## 2. The split, and why the underdog decides

A split vote is the only part of this that needed designing — the other two rows
of the table write themselves.

A coin flip settles a split too, and it was the first thing tried on paper. The
problem is that it tells the losing captain nothing. They tapped a button, a
dice roll disagreed with them, and there is no answer to "why?" beyond "bad
luck" — which is precisely the answer a player who just lost a match to a trait
they voted against will not accept.

**The weaker XI decides** instead, on Team Overall *with* the ⚡ Trait Boost
included. That rule has three properties a coin flip does not:

* it is **deterministic** — the same two squads always get the same answer, so
  nobody has to play the vote twice to see what it does;
* it is **readable before you vote** — both Team Overalls are printed on the
  prompt, so each captain knows who holds the casting answer while they are
  still deciding;
* it points the game in the right direction — the side with **less to gain** from
  traits is the side that says whether they are used. A stacked squad cannot
  force its investment onto an opponent who does not want to face it, and,
  symmetrically, an underdog who *wants* traits (a single well-levelled Finisher
  is the cheapest way to trouble a better XI) gets them.

The boost is the right measure of strength here precisely because the boost is
the thing being argued about: it is what the trait-heavy squad is bringing, so it
belongs in the number that decides who is bringing more.

Two XIs rated **dead level** have no underdog to defer to. That falls back to
`TRAIT_VOTE_DEFAULT`, which is "yes" — the status quo.

Operators who disagree with any of this have `TRAIT_VOTE_SPLIT_RULE`:
`"underdog"` (default), `"traits_on"`, `"traits_off"`, or `"random"`.

---

## 3. The secret ballot

Answers are **hidden until both are in**. The prompt shows only that a captain
has answered (`✅ locked in`), never what they said.

Shown live, the second captain would not be voting on the match — they would be
voting on the answer already on the screen, and the vote would collapse into
"whoever taps second wins", since a No always beats a Yes under the split rule
when you know the Yes is already there. Hidden, both answers are about the match.

A captain gets **one** answer, and only the two captains may answer at all —
everyone else in the chat gets told so.

> One consequence worth stating: both prompts are posted while handling *one*
> captain's tap, so `cric_traits:` and `lp_traits_` are registered in
> `services/button_access.py` as shared callback prefixes. Without that, the
> owner guard would hand the buttons to whoever triggered the message and the
> other captain could never answer. `tests/test_trait_vote.py` pins it.

---

## 4. What "OFF" turns off

Both halves of the trait system, for both sides:

| | with traits | without |
|---|---|---|
| ball engine (`services.trait_engine`) | nudges every delivery | never consulted |
| ⚡ Trait Boost on the cards | added to Team Overall | not added |
| trait ownership, levels, the market | — | *unchanged* |

Turning off the engine but leaving the boost on the card would be worse than not
offering the vote at all: the trait-heavy squad would keep a rating lead, on the
scoreboard and in the ratings the engine reads, in a match explicitly agreed to
be played without traits.

The two modes reach that from opposite directions, because their XIs are built
differently:

* **`/wpm`** looks traits up per player, per match, from the database
  (`_get_roster_traits`). One gate — `handlers.match._ball_traits` — sits on the
  single path every delivery takes (the chat flow, the Mini App, and the vsbot
  auto-play all call `_calc`), and returns nothing when the flag is off. It does
  not even reach the database.
* **`/letsplay`** carries traits inline on its engine dicts, so they are simply
  never attached: `_xi_to_engine(..., with_traits=False)` for a human side and
  `trait_vote_service.strip_traits` for the `/lpbot` XI. Everything downstream —
  the ball loop, the ⚡ OVR board line, the Super Over that decides a tie — reads
  the same eleven dicts, so they all agree with no second flag to keep in sync.

The live state carries `traits_enabled: False` either way. It is only ever
written as `False`: an absent flag means traits are in play, which is what every
state written before this feature says, and what every mode that never asks
means. The Mini App snapshot exposes the same boolean so the board can badge it.

---

## 5. When the question is *not* asked

The shortest match setup is the one that does not ask a question with one
possible answer. There is no prompt when:

* **neither squad has a single trait equipped** — there is nothing to switch off;
* **it is an official fixture** (a Lets Play Tournament fixture, or a `/tour`
  match): two captains must not be able to play a fixture under rules the pair
  before them did not, or the standings compare results from two different games;
* **it is a practice match against the bot** (`/lpbot`, `/wpmbot`) — nothing is at
  stake and there is no second captain to negotiate with;
* **the operator switched it off** (`TRAIT_VOTE_ENABLED = False`).

In every one of those cases the match plays with traits, exactly as it did before
the vote existed.

---

## 6. Timing out

Each captain has `TRAIT_VOTE_TIMEOUT` seconds (45), and the prompt says so. A
missing answer counts as `TRAIT_VOTE_DEFAULT` ("yes") and the setup moves on —
a lobby that stalls on this question is a lobby nobody is playing in.

Counting silence as *yes* is deliberate and load-bearing: it means no one can
turn traits off by refusing to answer. The only way to a no-traits match is for
someone to actually say no.

---

## 7. Settings

| Setting | Default | What it does |
|---|---|---|
| `TRAIT_VOTE_ENABLED` | `True` | offer the vote at all |
| `TRAIT_VOTE_TIMEOUT` | `45` | seconds before it settles without an answer |
| `TRAIT_VOTE_DEFAULT` | `"yes"` | what a missing answer counts as |
| `TRAIT_VOTE_SPLIT_RULE` | `"underdog"` | how a one-Yes-one-No vote is settled |

---

## 8. Things deliberately left out

* **No per-captain standing preference** ("always play without traits"). It would
  make the common case one tap shorter and the uncommon case impossible to
  reason about — a captain would be voting by a setting they configured weeks
  ago, against an opponent they have not seen. If it is added later, it belongs
  as a *pre-filled* answer on the prompt, not as a silent vote.
* **No mid-match switch.** The vote closes before the toss. Traits that are in
  are in for all twenty overs.
* **Nothing for `/cm` and the Challenge League.** Those XIs are league squads
  handed to both captains, not personal rosters — they carry no traits to vote
  about.
