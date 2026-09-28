# Playing /lpbot and /ciplbot from the Mini App

Practice matches against the AI captain (`/lpbot`, `/ciplbot` and every
`/c<league>bot` alias) can be played over by over from the Mini App as well as
from the chat, in any mix. Human-vs-human over-by-over matches (`/cipl`,
`/letsplay`) are still played in the chat, and the Mini App stays a read-only
board for them.

## The over, in the Mini App

The chat's "🎮 Play in Mini App" button opens the Arena on the match.

| Your side | You pick | Then |
|---|---|---|
| Bowling | a bowler (overs left, 🧤 part-timers flagged) → a **bowling plan** | the bot picks how to bat, the over is bowled |
| Batting | a **batting plan** (the bot's bowler is shown; its plan is hidden) | the over is bowled |

Each approach card carries a one-line description and a 1–5 risk meter. The
sheet also shows the phase (powerplay / middle / death), the bot's captaincy
style and difficulty, the chase chance in a chase, and a warning once you have
played the same plan for `REPEAT_FROM - 1` overs in a row, just before the
opposition starts reading it.

When the over is bowled, it plays back ball by ball: chips, commentary, the
usual sounds and GIFs, and a **Skip** button. It ends on a summary card: runs and
wickets, your plan vs the bot's (revealed only now), the special combination
and its flavour line, and the bowler's figures.

## New batsman, mid-over

In a bot match, when **your** batsman is out and there is a real choice (the
innings goes on and at least two batsmen are waiting), the over stops after
that ball. You pick who walks in, from the Mini App sheet or from the chat
buttons (`cipl_newbat_<mid>_<rid>`), and the rest of the over is bowled. If
nobody picks within `CIPL_TIMEOUT_SECONDS`, the next batsman in the batting
order walks in and play continues. Nobody is fined and the match is not closed.

When the bot bats, it sends its batsmen in batting order without a pause.
Human-vs-human matches keep the fixed batting order too.

## How it is wired

* `services/cipl_match.py`: with `simulate_over(state, pause_on_wicket=True)`
  the over parks itself in `state["over_in_progress"]` on a wicket and returns
  `{"paused": True, ...}`. `bring_in_batsman(state, roster_id)` moves the chosen
  player up to the next batting slot. The next `simulate_over` call resumes at
  the following ball and returns the summary of the whole over. A resume that
  somehow skipped the pick sends in the next batsman rather than letting the
  dismissed one bat.
* `handlers/cipl_play.py`: `submit_pick(context, mid, actor_tg, kind, value,
  on_accept)` is the single entry point for every pick (`bowler`,
  `bowl_approach`, `bat_approach`, `new_batsman`), used by the chat callbacks
  and the Mini App alike. The new action `PICK_CIPL_NEW_BATSMAN` has its own
  prompt, reminder, timeout (auto-pick) and `/rcl` resume. Impact Player swaps
  stay between overs.
* `services/bot_bridge.py`: the Flask Mini App runs in a thread of the bot
  process. `submit_and_wait_for_accept` runs `submit_pick` on the bot's event
  loop, and the request returns as soon as the pick is validated. The rest (the
  bot's reply, the over) lands on the next poll. `bot.py` hands the PTB
  Application over through `admin.set_bot_for_admin`.
* `POST /api/match/action` with `type` = `cipl_bowler {rosterId}`,
  `cipl_bowl_approach {key}`, `cipl_bat_approach {key}` or
  `cipl_new_batsman {rosterId}`.
* `services/match_webapp_service.py`: `is_approach_match` (every ball-by-ball
  mutator refuses these) vs `is_view_only_match` (approach matches that are
  *not* against the bot).
* `services/crickidex_arena.py`: `approachMode` plus an `approach` block with
  the options, `overBowlers`, `incomingBatsmen`, `overInProgress`, `lastOver`
  and `hints`. The turn states are `selecting_over_bowler`, `bowling_approach`,
  `batting_approach` and `selecting_wicket_batsman`.
* `static/cricket/approach.js`: the sheets and the over playback.
