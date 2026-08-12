# Lets Play Tournaments

A **Lets Play Tournament** is the Challenge League tournament with people instead
of franchises. An admin enters the field by **Telegram id**; every fixture is an
ordinary `/letsplay` match — both sides field their own roster, with their own
cards and traits, over 20 overs — and every result feeds a points table with net
run rate, per-player leaderboards and an optional playoff bracket.

It is deliberately *not* a second tournament engine. The only thing that is new
is the answer to "who are the two teams": a `TournamentTeam` row carries a
`user_tg_id` instead of a `challenge_team_id`. Everything downstream — standings,
NRR, the round-robin generator, the knockout bracket, the stat leaderboards, the
admin dashboard — keys off `TournamentTeam.id` and neither knows nor cares which
kind of team is behind it.

---

## Running one

The whole competition can be run from chat, or from the admin website, or both —
they operate on the same rows.

### From chat (bot admins only)

```
/lptnew Summer Smash | double | top4 | 10
/lptadd 123456789
/lptadd 987654321 | Mumbai Mavericks
/lptschedule
/lptstart
```

| Command | What it does |
| --- | --- |
| `/lptadmin` | The admin reference, printed in chat |
| `/lptnew <name> [\| format [\| playoffs [\| max]]]` | Create and make active. `format` = `single` (default) / `double`. `playoffs` = `none` (default) / `top4` / `playoffs` / `knockout`. Stays in **draft** |
| `/lptadd <telegram_id> [\| Team Name]` | Enter a player. A reply or `@username` also works, but the id is the documented way — it works for someone who has never messaged the bot |
| `/lptremove <telegram_id>` | Take a player out |
| `/lptrename <telegram_id> \| New Name` | Rename their team |
| `/lptsync` | Fill in real names for players who have since run `/debut` |
| `/lptschedule [single\|double]` | Generate the round-robin fixture list |
| `/lptknockout [top4\|playoffs\|knockout]` | Seed the playoff bracket from the final table |
| `/lptstart` `/lptpause` `/lptresume` `/lptcomplete` `/lptcancel` | Lifecycle |
| `/lptreset` | Wipe every result, keeping the field and the schedule |
| `/lptlist` `/lptuse <id>` `/lptdelete <id>` | Switch between tournaments |

### From the admin website

**Tournament Panel → 🏏 Lets Play Tournaments** creates them. After that, the
tournament reuses the shared pages: **Players & setup** (the Manage page, which
shows a Telegram-id entry panel instead of the Challenge League team dropdown),
**Schedule**, and **Overview** (points table, leaderboards, per-match scorecards).

---

## Playing and following it

| Command | Who | What |
| --- | --- | --- |
| `/lptour` | players | Reply to your opponent (or `/lptour @user`) to play your fixture |
| `/lpt` | anyone | The tournament's front page, with buttons for table / fixtures / teams |
| `/lptable` | anyone | Full points table (P W L T · Pts · NRR) |
| `/lptfixtures` | anyone | The whole schedule, with the viewer's own remaining matches pulled out on top |
| `/lptteams` | anyone | The field, printed as plain text so a squad list never pings everyone in it |
| `/lptstats` | anyone | Top-10 leaderboards (runs, wickets, sixes, fours, HS, average, best figure, SR, economy) |

These are listed in the **group** slash menu only, alongside `/cltour` — a
fixture is two people in a chat, and the readouts describe a competition that
chat is following. They still *run* in DM; the private menu is at Telegram's
100-command ceiling.

`/lptour` is a thin gate in front of `/letsplay`. It checks the tournament rules,
then hands `letsplay_handler` a `tour_ctx` and gets out of the way — so the
invitation, the pitch pick, the XI cards, the toss, the over-by-over Approach
match, the scorecard, the Super Over and the Mini App view are all the code that
already existed, with a tournament badge on the cards.

---

## The rules the bot enforces

Checked by `lp_tournament_service.check_pair`, once when `/lptour` is typed and
again at launch (the tournament can be paused, or the fixture taken by another
chat, while two players are picking a pitch):

1. **A tournament must be running.** A draft, paused, completed or cancelled
   tournament starts nothing, and says which it is.
2. **Both players must be entered.** The refusal names whichever side is missing.
3. **The pairing must have a fixture left** — but only once a schedule exists.
   Leave the schedule ungenerated for a *free-play* tournament, where any two
   entrants may meet any number of times and every result still counts.
4. **Both sides need a legal Playing XI**, inherited from `/letsplay`, checked
   before the invitation is even sent.

On top of that:

- **A scheduled tournament that is under way can no longer be reshaped.** A
  generated schedule *plus* a recorded result is the point of no return:
  regenerating would throw away real results, and leaving the old fixture list in
  place while the field changes would silently corrupt the table. So adding is
  refused until `/lptreset`. Either condition alone is recoverable — an unplayed
  schedule is simply cleared and rebuilt when the field changes, and a *free-play*
  tournament has no fixture list to invalidate, so a latecomer can be waved in at
  any time and starts on zero.
- **Removal is stricter than entry.** Any result at all blocks it, free-play or
  not: the matches that player has already played would be orphaned, and everyone
  who beat them would quietly lose those points.
- **A fixture is claimed at the toss, not at the invitation.** The reservation
  (`scheduled → live`) happens in the same transaction as the `Match` row, so two
  chats can't play the same fixture and a denied or expired invite costs nothing.
  A forfeit, an abandoned launch or a `/clearmatches` hands it back
  (`live → scheduled`) so the pairing stays playable.
- **A tournament fixture is never stat-farming.** `/letsplay` flags a lopsided
  pairing so no career stats or prize money are recorded. An admin decided who
  plays whom here, so a mismatch is just a hard draw — flagging it would strip a
  real result of its rewards.
- **One entry per person**, and a `max_teams` cap that actually holds.
- **Negative ids are refused**: those are groups and channels, not people.

---

## Two families, side by side

`Tournament.kind` is the discriminator: `"challenge"` (default) or `"letsplay"`.
Rows written before the column existed read as `"challenge"` — every tournament
that predates this feature was a Challenge League one.

The single-active rule is **per kind**, so one Challenge League tournament and
one Lets Play tournament can be live at the same time and neither activation
takes the other off the air:

```python
tournament_service.get_active_tournament(session)                       # CIPL
tournament_service.get_active_tournament(session, kind=KIND_LETSPLAY)   # Lets Play
```

`get_active_tournament` defaults to `KIND_CHALLENGE`, so every pre-existing
caller — the Challenge League tournament command, `/tournamentstats`,
`/statstour` — keeps seeing exactly the tournament it always did.

---

## How a result is recorded

The completion path is shared with `/cipl` (`handlers/cipl_play.py`), which calls
`tournament_service.record_tournament_match` whenever the match state carries a
`tournament_id`. The two kinds differ only in how the state names its teams:

| State key | Kind | Meaning |
| --- | --- | --- |
| `tournament_team_by_user` | challenge | `{db user id: ChallengeTeam.id}`, resolved to a `TournamentTeam` via the league |
| `tournament_tteam_by_user` | letsplay | `{db user id: TournamentTeam.id}` directly — a Lets Play team *is* a user, so there is nothing to resolve through |

When the direct map is present it wins, and a row belonging to a different
tournament is rejected rather than credited. A practice match against the bot
clears both maps (`cipl_play.mark_bot_match`), so `/lpbot` can never touch a real
table.

---

## Data model

```
Tournament
  kind              "challenge" | "letsplay"     (NULL reads as "challenge")
  league_id         NULL for letsplay
  command_snapshot  "/lptour" for letsplay
  overs             always 20 for letsplay

TournamentTeam
  challenge_team_id set for challenge, NULL for letsplay
  user_tg_id        set for letsplay,  NULL for challenge   ← the new identity
```

Two unique indexes cover `(tournament_id, challenge_team_id)` and
`(tournament_id, user_tg_id)`. Both are partial in effect rather than in DDL:
SQL treats NULLs as distinct, so each index simply ignores the other kind's rows.

The migration in `database.py` adds `tournaments.kind` and
`tournament_teams.user_tg_id` in place, plus the new unique index — `create_all`
never adds an index to a table that already exists, so without that an existing
database would let the same user be entered twice.

`user_tg_id` is the identity rather than `users.id` on purpose: an admin builds
the draw from a list of Telegram ids, and the people in it may not have accounts
yet. Such a participant is named `Player <id>` until they register, at which
point `/lptsync` (or the **Refresh names** button) resolves it. A name an admin
typed is never overwritten.

---

## Files

| File | Role |
| --- | --- |
| `services/lp_tournament_service.py` | Participants by Telegram id, eligibility, fixture reservation, the chat renderers |
| `services/tournament_service.py` | Kind-awareness: `_kind_filter`, `tournament_kind`, per-kind activation, Lets Play team resolution when recording |
| `handlers/lp_tournament.py` | Every `/lpt*` command and its callbacks |
| `handlers/letsplay.py` | Accepts a `tour_ctx`, re-checks it at launch, reserves the fixture and tags the match state |
| `admin.py` | `/lptournaments` list + create; the `add_lp_team` / `rename_lp_team` / `sync_lp_names` actions on the shared Manage page |
| `templates/admin_lp_tournaments.html` | The list + create page |
| `tests/test_lp_tournament.py` | 66 tests over participants, kind isolation, recording, eligibility, reservation, brackets, rendering and `/lptour` |
