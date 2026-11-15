# Rich text messages (Bot API 10.4)

`/pxi` used to fake a table. Names, ratings and a flag went into one padded
line inside a `<blockquote>`:

```python
f"{circled(serial)} {small_caps(player.name)}  {flag}  {stats}"
```

Telegram renders that in a proportional font, so the numbers only line up when
every name happens to be the same width. They never are, and the bench had the
same problem one `<blockquote expandable>` down.

Telegram's current [Bot API](https://core.telegram.org/bots/api) supports real
tables, expandable sections and native buttons. Rich messages are not a
`parse_mode`: `sendRichMessage` accepts exactly one of `blocks`, rich `html`,
or rich `markdown`. This bot normally sends typed blocks.

```python
{"chat_id": 42, "rich_message": {"blocks": [
    {"type": "heading", "text": "👑 @tester's PLAYING XI", "size": 3},
    {"type": "table", "is_bordered": True, "cells": [[
        {"align": "center", "valign": "middle", "text": "1"},
        {"align": "left",   "valign": "middle", "text": "ᴏᴘᴇɴᴇʀ ᴏɴᴇ  🇮🇳"},
        {"align": "right",  "valign": "middle",
         "text": {"type": "bold", "text": "80"}},
    ]]},
]}}
```

Telegram aligns the columns, so the layout no longer depends on how long a
player's name is.

## Bot-wide delivery

`services/rich_message_defaults.py` is installed by the main bot, the franchise
bot, admin DMs and website broadcasts. It promotes ordinary `send_message`,
`reply_text`, `reply_html`, callback edits and scheduled job messages through
the shared native rich transport. This includes match setup, delivery/shot
prompts, claims, daily rewards, player searches, roster and
shop pages, trades, tournaments, help, errors and administrator screens.
Standalone end-of-over summaries deliberately retain the original formatted
HTML through `sendMessage`, using the task-local `legacy_delivery()` bypass.
Their controls and deletion tracking are preserved; other messages still use
rich delivery. WSP autoplay summaries use the same exception.
Final-over delivery reveals also bypass rich delivery. They show one score and
target, the delivery result, the over timeline and the current requirement.
Extras explain that no legal ball was used; terminal deliveries show the win,
tie or successful defence. Per-delivery saves and message cleanup are unchanged.

Existing HTML becomes native rich HTML. Plain text becomes a native paragraph;
names containing `<`, `>` or `&` remain literal text. Explicit Telegram entities
preserve their formatting, including UTF-16 offsets. Specialized tables and
scorecards keep their existing block builders. Legacy Markdown is preserved
through its original formatter because its syntax differs from rich Markdown.

The automatic path preserves inline keyboards, reply targets, forum topics,
private parameters, content protection, the SDK rate limiter and caller timeouts.
Inline keyboards remain removable by existing completion/timeout handlers.
Personal native buttons register with the same owner guard; shared match
controls remain available to both captains and retain their backend checks.
An explicit rich helper's compatibility send is never promoted a second time.

`services/rich_message_compat.py` bridges the installed SDK: rich message
responses and incoming callback/webhook messages expose readable `text` and
`text_html`, with mentions and formatting. It preserves the original native
message without mutating Telegram updates. Existing photo/video/file delivery
keeps its media and formatted captions. `/claim` now embeds the photo and player
details in one rich message. Retain, release, replacement and the 60-second
auto-decision edit that message and preserve its photo. Older photo cards are
updated through their captions; a failed edit never sends a duplicate receipt.

## Alignment and match controls

`services/rich_layout.py` turns consecutive label/value facts into native tables
across ordinary replies, edits and website notifications. It preserves inline
formatting and mentions, including expandable fact sections. Text labels stay
left-aligned and numeric, currency, score and squad-count cells align right.
Complex HTML and code retain their original rendering rather than being guessed
into columns. Existing table builders share the same numeric-cell defaults.

`/sbo` uses explicit position/player/BAT/role columns and an expandable numbered
bench. Over summaries keep the old timeline, score, batter/bowler figures,
momentum and match notes in one ordinary message. Milestone
cards show known facts only and retain administrator wording in their full
announcement. Configured milestone media can be embedded in the same rich card.

Approach choices and Manage Batting use the separate inline keyboard below the
message. Shared send/edit helpers preserve that placement by default. Native
embedded buttons remain an explicit opt-in for a surface that needs them.

`services/rich_media.py` sends documented photo/animation/video blocks and real
multipart attachments through PTB. Cached file IDs are reused on edits; newly
returned rich photo IDs remain available to existing card caches. A refused
native media format falls back to one legacy photo card, without dropping the
image into a separate text receipt. See [Telegram rich media blocks](https://core.telegram.org/bots/api#inputrichblockphoto).

## Where it is used

| Surface | Rendering |
| --- | --- |
| `/buypack`, pack details and purchase receipts | `services/pack_messages.py` shares prices and contents between HTML and rich cards. Currency balances use tables, version filters are explicit, daily availability is visible, and receipts show the actual currency paid plus `/openpack`. |
| `/openpack` inventory and results | The inventory uses its own numbered pack table. The final receipt separates main and bonus pulls with version, rating and actual roster placement for each card, then shows roster/unopened counts and the next command. Overflow is explicitly marked as not added. |
| trade confirmation and completion | `services/trade_messages.py` keeps each captain’s offered/received card, fee and status visible until both confirm. Completion shows what each captain received, the fee paid and any traits returned. |
| chat daily reward receipts | `services/daily_messages.py` shows base/streak reward totals, current balances, player placement and milestone progress. The next claim uses the configured cooldown and a native localized time; HTML fallback shows the remaining duration. Overflow retains its Release/Replace controls. |
| pinned match-start announcements | `match_rich.match_start_blocks` shows teams, venue, format, pitch and batting first, keeps competition/conditions/chemistry notes and puts the command guide in collapsed details. View Match and Manage batting stay below the card. |
| match highlights | `highlights.reel_blocks` features the moment of the match, followed by the same selected moments in match order; The Hundred uses set labels. |
| `/pxi`, `/xi`, `/playingxi` | one `table` block — the four section names (batsmen, keeper, all-rounders, bowlers) are full-width header rows inside it |
| the `📋 View Bench` button | a collapsed `details` block, replacing `<blockquote expandable>` |
| the Franchise Auction's pinned board, `/aboard` | headings, a lot `table`, and the purses as a striped `table` — edited in place through `auction_rich.edit`, which treats "not modified" as success and shares the recoverable native transport |
| `/asquad`, `/apurse`, `/asets`, `/anextset`, `/anextplayer`, `/asoldlist`, `/aunsoldlist`, `/ainfo` | tables, a numbered `list`, and one collapsed `details` per set |
| `/adminhelp` | one `details` per section, each a two-column `table`, and a `pre` block of examples |
| `/tournamentstats`, `/lptstats` | the board as a ranked `table` — rank, player, team and the value in its own named column (`RUNS`, `WKTS`, `ECON` …). Ten places open, ranks 11–25 in a collapsed `details`; MVP keeps its `footer` pointing at `/mvp` |
| `/mvp` | the same split, with each player's season under their name |
| `/statstour` | batting and bowling as two `table`s per player; a second player matching the same name goes behind a `details` |
| `/ctour`, `/cttable`, `/ctfixtures`, `/ctteams`, `/ctinjuries` | `services/cl_tournament_rich.py` — the points table and the fixture list as native tables, the champion as a `pullquote`, the points-adjustment footnote as a `details` holding a `list`. The schedule shows twenty fixtures open and the rest in a collapsed `details`; nothing is dropped |
| `/clsd`, `/teamtourstats` | one team's card: standing and form as paragraphs, the fixtures as a `table`, the results behind a `details`, the batting/bowling splits as two-column tables |
| `/lpt`, `/lptable`, `/lptfixtures`, `/lptteams` | the same shapes for the Lets Play tournament (`services/lp_tournament_rich.py`), with a Telegram id where the Challenge League has an owner |
| the delivery and shot prompts, and the ball that follows | `services/match_rich.py` — the score as a `table`, the crease and the bowler's figures as tables, the headline (`SIX!`, `WICKET!`) as a `pullquote`, the commentary as a `blockquote`, the timeline still `code` so every ball keeps its width; the player whose turn it is is a real `tg://user` mention node |
| the Challenge League / `/letsplay` over board — the bowler picker and both approach prompts | `handlers/cipl_play._approach_card_blocks` — both totals and the batters (R, B, 4s, 6s, SR) as tables, the chase as a `pullquote` with target/RRR/CRR on the line under it, the bowler's figures as a table captioned with the last over, CHEM and OVR as one match-up table, the last over's commentary behind a collapsed `details`, and the captain as a `tg://user` mention |
| **Manage batting** on a live scorecard | The full-width keyboard entry beneath the message uses `lb_n_<match id>` in native and fallback delivery. It also appears on pinned chat-match start cards. Opening the menu keeps the plan picker and its deadline intact. |
| the Mini App's `MATCH READY` card and its live scorecard broadcast | `services/match_broadcast.py` — the match facts and both XIs as tables, the chase as a `pullquote` |
| the Challenge League XI picker, and the XI it confirms | `handlers/challenge.py` — the selection rules as a real `checklist`, the confirmed XI through `match_rich.playing_xi_blocks` (a numbered `table`, bench behind a `details`) |
| the Challenge League's team picker, pitch menu, pitch confirmation and "the battle is set" recap | `services/challenge_rich.py` — the two sides as one table so captains and franchises line up in columns, the surfaces as a table of what each does to a match, the Pitch Report as a `blockquote`, and a bot opponent's XI behind a `details` so the Select XI buttons stay on screen. The team picker usually lives in a league banner's caption, which `sendRichMessage` cannot replace, so only its text-only path is rich |
| `/cltour` | `handlers/cl_tour._cl_tour_blocks` — the two sides as a table, the series scoreline as a `pullquote`, and each match's state as a row |
| the Lets Play traits vote | `services/trait_vote_service.prompt_blocks` / `result_blocks` — the two XIs and who has answered as one table, the outcome rules as another, the verdict as a `pullquote` |
| every `/letsplay` and `/lpbot` setup card — invitation, pitch, both Playing XIs, the toss, and what `/change` takes | `services/letsplay_rich.py` — the invitation's facts as a table, each XI as a numbered `table` with its rating in a right-aligned column of its own and its bench behind a `details`, and the Team Overall verdict as a `pullquote` when the gap is wide enough to cost both sides their career stats. The toss reveal goes through `_reveal_rich`, which raises on a refused edit so `reveal_toss_result`'s retry keeps meaning what it says |
| `/claim` and its retain, release, replace and 60s auto-decide outcomes | `handlers/claim.py` — one embedded-media message contains the photo, rating table, player facts, version, manager mention and timer explanation. Its outcomes edit the same message. Removable controls stay below the decision card. |
| `/stats` | the career record as a batting `table` and a bowling one; with rich messages on, the card image leads with the short caption and the record follows as its own message |
| the scorecard of last resort, when every image fails | `services/scorecard_delivery.py` — batting, bowling and the result as native tables, the winner as a `pullquote` |
| all Franchise season draft messages | `services/franchise_draft_rich.py` — purse, player and rule tables; the current team and confirmed player as `pullquote` blocks; round order and squad lists in `details`; command guides as grouped tables. Separate inline controls reuse the existing authorized callbacks; deadlines use localized `date_time` nodes. API 10.4 private rich replies explain rejected button actions to the clicker. Event cards snapshot purse/squad/next-owner values before delivery. Long HTML fallbacks split without dropping rows. |

The shared helpers send native rich HTML when no block builder is available;
ordinary bot text sends receive the same native delivery automatically. Image
cards keep their images and formatted captions, with native rich details where
the flow provides a separate text card.

Synchronous news, Mini App notifications and direct website sends share
`services/telegram_rich_http.py`. It preserves HTML, keyboards and privacy,
retries a rejected native format through the original styled message, and
caches missing endpoints temporarily per token. Network failures and flood
limits are returned to the worker without issuing a second message.

## Both renderers stay alive

`handlers/lineup.py` keeps its HTML renderers (`format_xi_text`,
`format_bench_text`) next to the block builders (`build_xi_blocks`,
`build_bench_details`). That is deliberate, not a migration half-done:

* python-telegram-bot is still on Bot API **10.0** and ships no
  `send_rich_message`, so the payload goes through `Bot._post` — the same
  private hop every PTB method uses, which keeps the rate limiter, the base URL
  and the error parsing. There is no typed interface to migrate onto yet.
* An older self-hosted Bot API server may have no such method. It can answer
  404, which PTB surfaces as `InvalidToken`.

`services/rich_message.py` takes the existing HTML as `fallback_text`. A rejected
block payload first retries native rich HTML, generated safely by
`services/rich_message_html.py`, preserving tables, details and button rows.
Only if native rendering is unavailable does it use the formatted `sendMessage`
or `editMessageText` path. User names and callback attributes remain escaped.

Missing-method responses are cached for five minutes per bot and operation,
then retried automatically. A failed edit cannot disable rich sends, and one
bot cannot disable another. Network failures have a five-second backoff;
native attempts have an eight-second ceiling. Payload validation failures do
not switch off later rich cards. A private ephemeral reply never retries as a
public group message: old servers instead use the existing private popup.

Because both renderers are live, they must agree on anything a user acts on.
They share `_xi_sections` for the category split, and
`tests/test_lineup_rich_text.py` reads both renderings and compares the display
numbering — the number typed at `/swap` and `/release` has to be the same one
whichever version a user is looking at.
`tests/test_rich_text_surfaces.py` does the same for everything added since: a
leaderboard's rank order against `_render`, a batting order against
`_challenge_xi_confirmed_text`, a live board's score against
`build_live_scorecard`. It also checks the two things a block tree fails
silently on — a cell missing `align`/`valign`, and a tree that will not
serialise — and that every builder answers `None` rather than raising.

The one intentional difference: the table uses plain digits where the HTML uses
`bold_digits`. Stylised digits are not monospaced, and they fight the column
alignment that is the whole point of the table.

## Switching it off

`RICH_TEXT=0` (config `RICH_TEXT_ENABLED`). Default on. Turning it off is
always safe — every surface has its HTML path — and saves one refused call per
render when pointing at an older Bot API server.

## Adding another surface

1. Write a `build_*_blocks` next to the existing HTML renderer; keep whatever
   logic decides *content* shared between them.
2. Build blocks with the helpers in `services/rich_message.py` rather than raw
   dicts — `cell()` always emits `align` and `valign`, which the API requires.
3. Send with `rich_message.reply_rich(...)` from a command, `edit_rich(...)`
   from a button, or `send_rich_message(...)` where you hold a bot and a chat
   id — passing the HTML rendering as the fallback in every case.
4. Catch your own exceptions and return `None`, so a renderer bug costs the
   rendering rather than the message.
5. Test that both renderings agree on anything the user types back.

`services/rich_message.py` now also has `pre`, `pullquote`, `list_block`,
`strikethrough` and `mention` (a `url` node pointing at `tg://user?id=…`),
first used by the Franchise Auction, plus `blockquote`, `checklist` and
`footnote`, added for the surfaces in the table above. The current contract uses
`credit` for quotations, a separate `expandable_blockquote`, checkbox flags on
individual list items, and reference/link pairs for notes. Saved event blocks
are normalized on delivery without changing the audit snapshot. Both auction
and draft senders use the same transport and recovery.

## Two conveniences the later surfaces are built on

A command handler holds a `Message` and a button handler holds a
`CallbackQuery`; neither holds the `(bot, chat_id)` pair `send_rich_message`
takes, and both already render their own HTML. So `rich_message` grew two
wrappers, and a surface normally calls one of them rather than the raw senders:

* `reply_rich(message, blocks, fallback_text, reply_markup=…)` — answers a
  command. An HTML fallback too long for one send goes out in parts, buttons on
  the last, which is why `blocks=None` is a legitimate call: a long list is now
    split rather than refused even where there is no block rendering. Without
    blocks, the helper first attempts the existing HTML through `sendRichMessage`.
* `edit_rich(query, blocks, fallback_text, reply_markup=…)` — redraws a card
  behind a button, and reads Telegram's "not modified" as *already drawn*
  rather than as a failure. Tapping the tab you are already on is the common
  case for every hub card here. A long card gets a native edit first; an older
  server receives all fallback parts as replies, rather than a truncated list.

## What a builder does when it cannot build

Every builder added for these surfaces returns `None` rather than raising —
`match_rich._safe`, `cl_tournament_rich.render_blocks`, the `try/except` in each
`claim` builder. `None` means "send the HTML", preferably as native rich HTML.
The rule is the same one behind the fallback itself: a bug in a
renderer may cost the *rendering*, never the message. A live match must not
stall because a chemistry badge raised.

Blocks the API offers that this bot does not use yet: `collage` and `slideshow`
for grouped media, `mathematical_expression`, `map`, and `sendRichMessageDraft`
for streaming a message as it is built — a fit for live commentary, which
currently edits one message over and over.
