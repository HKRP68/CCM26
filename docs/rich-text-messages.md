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

Everything else in the bot still sends HTML, and nothing forces that to change:
the two are ordinary sends to different endpoints.

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
3. Send with `rich_message.send_rich_message(...)`, passing the HTML rendering
   as `fallback_text`.
4. Test that both renderings agree on anything the user types back.

`services/rich_message.py` now also has `pre`, `pullquote`, `list_block`,
`strikethrough` and `mention` (a `url` node pointing at `tg://user?id=…`),
first used by the Franchise Auction. The auction's module is
`services/auction_rich.py`; its `send` is wider than `send_rich_message` in one
way — any failure of the rich attempt, not only a `TelegramError`, falls through
to HTML — because an auction announcement must never be lost to a renderer bug.

Blocks the API offers that this bot does not use yet: checkbox lists,
`collage` and `slideshow` for grouped media, `mathematical_expression`, `map`,
and `sendRichMessageDraft` for streaming a message as it is built — a fit for
live commentary, which currently edits one message over and over.
