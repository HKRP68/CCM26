# Scorecards that stay gettable

Every innings ends with two cards (batting, bowling) and every match with a
third (the summary). Groups reported two separate complaints about them:

* **"the scorecard didn't come"** — sometimes one card arrived, sometimes
  neither, with nothing in the chat to say so
* **"can we get it back?"** — no, and there was no way to

Both came from the same design. The cards were rendered off live match state
and sent once:

```python
png = generate_batting_scorecard(...)   # CPU-bound PIL render
await bot.send_photo(chat_id, png)      # one attempt, no retry
```

`match_state` is cleaned up shortly after a match ends, so the moment that send
failed the card was unrecoverable — there was nothing left to render from.

## What changed

The order is inverted. Values first, pixels second.

1. **Persist.** The render-ready values for each card are written to
   `match_scorecard_images` **before** anything is drawn. A render bug, a
   missing font, a chat that rejects photos — none of them can now cost the
   data.
2. **Render independently.** The cards of one delivery are still drawn
   concurrently — they are CPU-bound PIL work on worker threads, and drawing
   them one after another would make other live matches wait — but each render
   now returns `None` instead of raising. The batting and bowling renders used
   to share one `try` and one bare `asyncio.gather`, so a failure in either
   discarded both. The *sends* still go out in order, so the chat reads
   batting-then-bowling as it always has.
3. **Retry the send.** `send_photo_with_retry` honours Telegram flood control
   (`RetryAfter`), retries a `TimedOut`/`NetworkError`, and stops immediately
   on `Forbidden` — no number of retries fixes a chat the bot was kicked from.
4. **Cache the `file_id`.** Telegram hands one back for every photo it accepts.
   Re-sending by `file_id` costs no render and no upload; a rejected one is
   cleared so the next request redraws rather than failing identically forever.
5. **Fall back to text.** If *no* image reaches the chat, the numbers are
   posted as text — the batting card's score and dismissals, the bowling
   figures, and the result. Built from the same payloads the images are drawn
   from, so it cannot disagree with them. A group is never left with silence.

The old fire-and-forget JSON snapshot to the Telegram storage channel is still
taken, but it now runs *after* the chat has its cards rather than in front of
them.

## What is deliberately *not* in a payload

The teams' colours and logos, the Player of the Match's portrait, and the
Scorecard Designer's text settings. All of them are re-read live in
`render_card`, so a card redrawn next month follows the theme the admins have
now and the crest the team has now, rather than the ones in force when the
match was played. See `docs/team-logo-approval.md` for where a crest comes
from; the portrait is the admin-uploaded card art the bot already holds
(`services/player_image_service`), found via the payload's `potm_player_id`.

`/previewsummary` (bot admins) renders a card from canned data through this
same path, which is the quick way to see the effect of a Scorecard Designer
change without playing a match.

## Branding reaches every mode, not just this one

All of the above is resolved by **`services/card_identity.py`**, not by this
module. That matters because not every mode goes through `render_card`: `/cipl`
and the Challenge League, the Super Over and `/sim` all import a generator and
call it directly, and while the resolution lived here those modes silently drew
cards with no crest, no colour and no photo.

They now each call `card_identity.summary_visuals(...)` and spread the result
into their generator call. Identity resolves in this order, first hit wins:

1. **user id** — `users.team_logo_asset_key` / `users.team_colour`
2. **`ChallengeTeam`** — its `logo_url` / `primary_color`, since CIPL sides are
   franchises rather than user teams
3. **team name** — the `users` lookup keyed on the name

A user id is preferred wherever one is in scope, because names are not reliable
keys: `/sim` can pass `"🤖 Sim XI"`, `"@someone"` or `"Someone's XI"`, and
`/cipl` labels carry a `" (Bot)"` suffix. Each mode resolves from the clean
name and the owning id, never from the display label.

The POTM portrait comes from `services/player_portrait_service` — cut-outs with
a transparent background, and a **global fallback PNG**, which is what makes a
photo appear in every mode rather than only for players with art of their own.
It is deliberately *not* `player_image_service.get_custom_image_bytes`: that
returns the full collectible card, borders and all, which is what the strip used
to paste in.

## The Player of the Match showcase

The strip's first two columns always carry the two numbers that describe a
cricket performance — `runs(balls)` and `wickets/runs-conceded`. An all-rounder
gets both; the showcase used to branch to one discipline and throw the other
half away even when the payload held it. The last three columns adapt, so a
bowler is never shown FOURS and SIXES and a batter is never shown ECONOMY:

| The player | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- |
| batted **and** bowled | BATTING | BOWLING | S/R | OVERS | ECON |
| batted only | BATTING | BOWLING | FOURS | SIXES | S/R |
| bowled only | BATTING | BOWLING | OVERS | ECON | DOTS |

Payload numbers always win. What is missing is recovered from the `potm_stats`
display string, which is all some callers send and all that archived rows have,
so `_potm_metrics` parses every format in use:

| Caller | String | |
| --- | --- | --- |
| `/playmatch` | `🏏 52(31) \| 🎳 4/27 (4)` | both halves, overs from the bare bracket |
| `/cipl` | `52(31) \| 4/27 (4)` | both halves |
| `/sim` | `52 runs, 3 wkts` | words |
| Arena / `/wpm` / `/vsbot` | `12 runs • 2 wickets` | words — but this path now also digs the raw numbers out of the arena stat tables (`admin.py:_arena_potm_numbers`), so it fills properly |
| Scorecard Designer preview | `4/25 (4 OVERS)` | figure plus an overs word |

One parsing rule is load-bearing and has a test of its own: the batting pattern
carries a lookbehind so the trailing `(4)` of `4/27 (4)` is not read as "27 runs
off 4 balls".

## `/lastscorecard`

Aliases: `/lsc`, `/scorecard`.

```
/lastscorecard          the last match played in this chat
/lastscorecard 1234     that match by number (also accepts #1234)
```

It replays every archived card of the match in the original order — batting 1,
bowling 1, batting 2, bowling 2, summary — from the cached `file_id` where
there is one and from a fresh render where there is not. That second path is
the repair: a card that never arrived live has a row, so it can still be drawn.

* **In a group** it reads that group's last match. This is the intended use:
  the cards belong to the chat they were played in.
* **In a DM** there is no group history, so it answers with the caller's own
  last match.
* **A match id from another chat** is refused unless the caller played in it
  (or is a configured bot admin). Someone's group match is not readable out of
  a stranger's DM.
* **A second request while one is in flight** is turned away. Five images is a
  lot of upload, and two overlapping replays double the flood-control pressure
  that loses cards in the first place.

When some of the cards it is about to rebuild were never delivered in the first
place, it says so ("Rebuilding 2 cards this chat never received…") rather than
pretending it is just re-posting.

The command is not in the slash menu: both player scopes sit at Telegram's
100-command ceiling, so an entry would cost an existing command its slot (see
`tests/test_bot_menu_commands.py`). It is named under every match result, in
`/matchhelp after` and in `/howto`, which is where someone whose card went
missing is actually looking.

## Coverage

| Mode | Archived |
|---|---|
| `/playmatch`, `/cric`, `/vsbot`, tours, bowl-out, watch-mode autoplay | yes — all funnel through `handlers.match._send_innings_scorecards` |
| Mini App `/wpm`, `/cm`, `/wpmbot` | yes — `admin._build_innings_cards` and `_build_match_summary_image` record before rendering |

Matches played before this landed have no rows and cannot be replayed; the
command says so rather than failing silently.

## Styling

A payload stores the card's **data** only — teams, rows, fall of wickets,
extras. The admin-tunable accent colour and text settings are re-read live in
`scorecard_delivery.render_card`, so a card redrawn next season follows the
theme the admins have configured *then*, not the one in force when the match
was played.

Payload keys the renderer no longer accepts are dropped rather than passed
through. Archived rows outlive the code that drew them, and one stale key in
`generate(**payload)` is a `TypeError` that would retire every old row at once.

## Where the pieces live

| File | Role |
|---|---|
| `services/scorecard_delivery.py` | persistence, rendering, retrying sends, text fallback |
| `models.MatchScorecardImage` | one row per card: payload, caption, `file_id`, delivered flag |
| `handlers/match.py` | `_send_innings_scorecards`, `_replay_stored_scorecards`, `lastscorecard_handler` |
| `tests/test_scorecard_delivery.py` | the service, against a throwaway sqlite file |
| `tests/test_lastscorecard_command.py` | the command's chat rules and recovery paths |
