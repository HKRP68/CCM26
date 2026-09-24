# Rich text messages (Bot API 10.1)

`/pxi` used to fake a table. Names, ratings and a flag went into one padded
line inside a `<blockquote>`:

```python
f"{circled(serial)} {small_caps(player.name)}  {flag}  {stats}"
```

Telegram renders that in a proportional font, so the numbers only line up when
every name happens to be the same width. They never are, and the bench had the
same problem one `<blockquote expandable>` down.

Bot API 10.1 added real tables. They are not a `parse_mode`: instead of an HTML
string, `sendRichMessage` takes a tree of typed blocks.

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

## Where it is used

| Surface | Rendering |
| --- | --- |
| `/pxi`, `/xi`, `/playingxi` | one `table` block — the four section names (batsmen, keeper, all-rounders, bowlers) are full-width header rows inside it |
| the `📋 View Bench` button | a collapsed `details` block, replacing `<blockquote expandable>` |
| the Franchise Auction's pinned board, `/aboard` | headings, a lot `table`, and the purses as a striped `table` — edited in place through `auction_rich.edit`, which treats "not modified" as success and latches rich board edits off for the process if Telegram refuses one |
| `/asquad`, `/apurse`, `/asets`, `/anextset`, `/anextplayer`, `/asoldlist`, `/aunsoldlist`, `/ainfo` | tables, a numbered `list`, and one collapsed `details` per set |
| `/adminhelp` | one `details` per section, each a two-column `table`, and a `pre` block of examples |
| `/tournamentstats`, `/lptstats` | the Top-10 board as a ranked `table` — rank, player, team and the value in its own named column (`RUNS`, `WKTS`, `ECON` …); MVP keeps its `footer` pointing at `/mvp` |
| `/statstour` | batting and bowling as two `table`s per player; a second player matching the same name goes behind a `details` |
| `/ctour`, `/cttable`, `/ctfixtures`, `/ctteams`, `/ctinjuries` | `services/cl_tournament_rich.py` — the points table and the fixture list as native tables, the champion as a `pullquote`, the points-adjustment footnote as a `details` holding a `list` |
| `/clsd`, `/teamtourstats` | one team's card: standing and form as paragraphs, the fixtures as a `table`, the results behind a `details`, the batting/bowling splits as two-column tables |
| `/lpt`, `/lptable`, `/lptfixtures`, `/lptteams` | the same shapes for the Lets Play tournament (`services/lp_tournament_rich.py`), with a Telegram id where the Challenge League has an owner |
| the delivery and shot prompts, and the ball that follows | `services/match_rich.py` — the score as a `table`, the crease and the bowler's figures as tables, the headline (`SIX!`, `WICKET!`) as a `pullquote`, the commentary as a `blockquote`, the timeline still `code` so every ball keeps its width; the player whose turn it is is a real `tg://user` mention node |
| the Mini App's `MATCH READY` card and its live scorecard broadcast | `services/match_broadcast.py` — the match facts and both XIs as tables, the chase as a `pullquote` |
| the Challenge League XI picker, and the XI it confirms | `handlers/challenge.py` — the selection rules as a real `checklist`, the confirmed XI through `match_rich.playing_xi_blocks` (a numbered `table`, bench behind a `details`) |
| what `/claim` leads to — retain, release, replace, and the 60s auto-decide | `handlers/claim.py` — the card's attributes as a `table` behind a `details`, replacing `<blockquote expandable>` |
| `/stats` | the career record as a batting `table` and a bowling one; with rich messages on, the card image leads with the short caption and the record follows as its own message |
| the scorecard of last resort, when every image fails | `services/scorecard_delivery.py` — batting, bowling and the result as native tables, the winner as a `pullquote` |

Everything else in the bot still sends HTML, and nothing forces that to change:
the two are ordinary sends to different endpoints. The `/claim`, `/gstats` and
scorecard cards are photos, and a caption is not something `sendRichMessage`
replaces — for those only the messages around the photo are rich.

## Both renderers stay alive

`handlers/lineup.py` keeps its HTML renderers (`format_xi_text`,
`format_bench_text`) next to the block builders (`build_xi_blocks`,
`build_bench_details`). That is deliberate, not a migration half-done:

* python-telegram-bot is still on Bot API **10.0** and ships no
  `send_rich_message`, so the payload goes through `Bot._post` — the same
  private hop every PTB method uses, which keeps the rate limiter, the base URL
  and the error parsing. There is no typed interface to migrate onto yet.
* A self-hosted Bot API server below 10.1 has no such method at all. It answers
  404, which PTB surfaces as `InvalidToken`.

So `services/rich_message.py` takes the HTML as a `fallback_text` argument and
sends it whenever the rich call is refused. A missing method latches rich sends
off for the rest of the process, so an old server costs one refused call rather
than one per `/pxi`. A refused *payload* does not latch — that would be this
bot's own bug, and it should keep showing up in the logs.

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
`footnote`, added for the surfaces in the table above. The auction's module is
`services/auction_rich.py`; its `send` is wider than `send_rich_message` in one
way — any failure of the rich attempt, not only a `TelegramError`, falls through
to HTML — because an auction announcement must never be lost to a renderer bug.

## Two conveniences the later surfaces are built on

A command handler holds a `Message` and a button handler holds a
`CallbackQuery`; neither holds the `(bot, chat_id)` pair `send_rich_message`
takes, and both already render their own HTML. So `rich_message` grew two
wrappers, and a surface normally calls one of them rather than the raw senders:

* `reply_rich(message, blocks, fallback_text, reply_markup=…)` — answers a
  command. An HTML fallback too long for one send goes out in parts, buttons on
  the last, which is why `blocks=None` is a legitimate call: a long list is now
  split rather than refused even where there is no block rendering.
* `edit_rich(query, blocks, fallback_text, reply_markup=…)` — redraws a card
  behind a button, and reads Telegram's "not modified" as *already drawn*
  rather than as a failure. Tapping the tab you are already on is the common
  case for every hub card here.

## What a builder does when it cannot build

Every builder added for these surfaces returns `None` rather than raising —
`match_rich._safe`, `cl_tournament_rich.render_blocks`, the `try/except` in each
`claim` builder. `None` means "send the HTML", which the senders already do for
empty blocks. The rule is the same one behind the fallback itself: a bug in a
renderer may cost the *rendering*, never the message. A live match must not
stall because a chemistry badge raised.

Blocks the API offers that this bot does not use yet: `collage` and `slideshow`
for grouped media, `mathematical_expression`, `map`, and `sendRichMessageDraft`
for streaming a message as it is built — a fit for live commentary, which
currently edits one message over and over.
