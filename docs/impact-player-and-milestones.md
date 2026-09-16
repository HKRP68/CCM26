# Impact Player & Milestone Messages

Two features for the **over-by-over "Approach" simulation** — the engine behind
both `/letsplay` and every Challenge League mode (`/cipl`, `/cm`, `/cdraft` and
the admin-created league aliases).

`handlers/letsplay.py` builds the same `cipl_approach` state and hands straight
off to `handlers.cipl_play._prompt_bowler`, so everything below is shared by
both modes by construction. There is no LetsPlay-specific copy.

---

## Impact Player

One substitution per side per match, IPL-style: a squad member from outside the
Playing XI comes on for someone in it.

### Where it lives

| Rule | Where |
|---|---|
| Shared, mode-agnostic swap mechanics | `services/impact_player.py` |
| Mini App (ball-by-ball) entry points | `services/match_webapp_service.py` — `get_impact_player_options` / `use_impact_player`, aliasing the shared helpers |
| Over-by-over entry points | `services/impact_player.py` — `cipl_options` / `cipl_use` |
| Legal window | `impact_player.cipl_break_label` |
| Batting position | `impact_player.cipl_batting_slots` / `insert_into_batting_order` |
| Innings-break order rebuild | `impact_player.rebuild_batting_order`, called from `cipl_match.end_first_innings` |
| Substitute pool (bench) | `state["bat_bench"]` / `state["bowl_bench"]`, filled at launch |
| Chat UI | `handlers/cipl_play.py` — `cipl_imp*` callbacks, `_impact_button`, `/impact` |
| Where the button rides | `_with_view_match` → `_view_and_impact_rows` — every prompt, beside View Match |
| Marking a substitute | `impact_player.is_impact` / `display_name` / `IMPACT_SUFFIX` |
| Button ownership | `services/button_access.py` — `cipl_imp_` in `SHARED_CALLBACK_PREFIXES`, the picker prefixes in `OWNER_RULES` |
| AI captain | `services/bot_captain.py` — `pick_impact_swap` |

### The window

Legal whenever `next_action` is one of `A_PICK_CIPL_BOWLER`,
`A_PICK_BOWL_APPROACH` or `A_PICK_BAT_APPROACH` — i.e. any point before the
over is simulated. An over runs in one call, so there is no mid-over moment to
protect, and **there is no "after wicket" window** in this mode (unlike the Mini
App, whose wording promises one).

The innings break needs no special case: `_innings_break` posts its card, sets
`next_action = A_PICK_CIPL_BOWLER` and calls `_prompt_bowler` immediately, so
the break *is* the bowler pick before the first over of the chase.

### Who may be replaced

* **Batting side** — anyone active except the two batters at the crease. That
  is the real rule, and it is also what keeps `striker_idx` / `non_striker_idx`
  pointing at someone still on the field.
* **Bowling side** — anyone active except the bowler already handed the ball for
  this over. Swapping at the bowler-pick step instead lets the substitute be
  chosen to bowl straight away, which sidesteps re-checking quota and the
  no-back-to-back rule mid-over.

### The batting-position rule

`batting_order` is a list; `striker_idx`, `non_striker_idx` and
`next_batsman_idx` are positional indices into it, and batters are promoted in
list order (`cipl_match.simulate_over`).

**The invariant that makes this safe:** `striker_idx` and `non_striker_idx` are
always below `next_batsman_idx` — the order starts `0, 1, 2`, and every wicket
does `striker_idx = next_batsman_idx; next_batsman_idx += 1`. So any insert or
removal at an index `>= next_batsman_idx` cannot move a player who is batting or
has already batted, and **no index needs fixing up afterwards**. Never touch the
list below `next_batsman_idx`.

Legal slots are therefore exactly `next_batsman_idx … len(batting_order)`, which
is what the third picker step offers. Two cases behave differently:

* outgoing **has not batted** — their slot is freed, and the substitute takes it
  unless another is chosen;
* outgoing is **already dismissed** — they must stay in the order or the
  scorecard loses their innings, so the substitute is *inserted* at the chosen
  slot. Appending (what the Mini App does) would bat every impact substitute at
  number 12, which is the whole reason the captain gets to choose.

A side that swaps while **bowling** has no batting order yet. The choice is
stored on `impact_players.usage[uid]["bat_position"]` and applied when
`end_first_innings` builds one.

### Where the button lives

`_with_view_match` appends View Match to **every** prompt message (it is what
`_new_action_message` and `_edit_action_message` both run), so the Impact button
goes there too, on the same row. That puts it on the bowler prompt and both
approach prompts — the whole window in which a swap is legal — rather than only
on the over summary, which is deleted the moment the next over starts.

Pass `extras=False` to either sender for a message that is not a prompt anyone
acts on. The bot's "hands the ball to X" note does this: it is informational and
transient, and the human's approach prompt right behind it carries the button.
`tests/test_bot_match_flow.py` pins that.

`/impact` opens step 1 of the picker directly, through the same
`_send_impact_step1` helper the button uses, so the two entry points cannot
drift. It takes the match lock and re-reads state inside it, so it can never act
on a half-finished over.

> **The bug this replaced:** `impact_handler` bound the `(match_id, state)` tuple
> from `_find_cipl_match_in_chat` to one name. A non-empty tuple is truthy, so
> the guard passed and the lookup then used a match id that cannot exist —
> `/impact` answered "No live over-by-over match in this chat" *every time, in
> every chat*. The callbacks were tested; the command was not.
> `tests/test_impact_command.py` now covers it.

### One swap per team, for the whole match

The record is `state["impact_players"]["usage"][str(user_id)]` — inside the match
state, keyed by user id, seeded for both innings' team ids by `usage_for`. Every
reload path preserves it:

- JSON persistence — keys are `str(user_id)` precisely so the round-trip is lossless;
- `end_first_innings` never touches `impact_players`;
- `cipl_resume` / `_resume_locked` only re-read and re-render, so `/rcl` cannot refill it;
- every confirm runs inside `get_match_lock(mid)`, so two fast taps serialise.

`OneUseSurvivesEverythingTests` in `tests/test_cipl_impact_player.py` drives each
of those for real rather than asserting on the dict.

### Button ownership

Two messages, two different rules — and getting either backwards is a
live-match bug.

The **🔄 entry button** rides on the over summary and the innings-break card.
Those are sent while handling *one* captain's approach tap, so
`services/button_access` registers the message to that captain — which would
lock the other captain out of their own substitution. `cipl_imp_` is therefore
listed in `SHARED_CALLBACK_PREFIXES`, exactly like `cipl_bowler_` /
`cipl_bowlapp_` / `cipl_batapp_`, and `_impact_guard` checks the clicker
against `bat_user_tg` / `bowl_user_tg` and hands each captain their own
options. Both captains press the same button and each gets their own picker.

The **picker itself** is personal: it lists one captain's squad and spends
their one irreversible swap. Its buttons carry the owner's Telegram id in the
callback data (`_imp_cb` / `_imp_parse`, via `button_access.tag_owner`), which
makes the lock stateless — a restart empties the owner registry, and without
the tag every picker in flight would fall open to the other captain. The four
picker prefixes also have `OWNER_RULES` entries, so pressing someone else's
picker says how to get your own rather than the generic refusal.

Note `"cipl_impo_"` does **not** start with `"cipl_imp_"` (the character after
`imp` is `o`, not `_`), so the shared entry prefix cannot accidentally
un-protect the picker. There is a test pinning that.

### Bookkeeping that is easy to get wrong

`apply_to_identity_list` keeps the outgoing player in the XI list marked
`active: False` and **appends** the substitute, because scorecard and career
stat persistence resolve stat rows through those lists — replacing in place
orphans existing stats. The XI list therefore holds 12 entries after a swap, so
everything that reads it for *who is on the field* filters through
`impact_player.active_players`:

* `cipl_match.eligible_bowlers` — a replaced bowler must never bowl again;
* `cipl_match._pick_fielder` — and must never appear on a `c … b …` line;
* `cipl_match._fielding_quality` — fielding is the eleven's, not the twelve's;
* `cipl_match.end_first_innings` — see above.

Things that read the XI for *who contributed* (`_summary_rows`,
`_innings_scorecard`) deliberately keep both, filtered by balls faced/bowled.

### Marking a substitute on the scorecards

A substitute carries `impact_replacement: True`. `cipl_use` stamps that on the
dict **before** filing it, so it lands on `batting_order` as well as the XI list
— every text scorecard renders from the order, not the XI, so a flag that only
reached the XI would show up nowhere.

| Surface | How it shows | Where |
|---|---|---|
| Telegram text | `-IP` after the name | `_bat_line` / `_compact_bat_line` → `impact_player.display_name` |
| Summary card image | the whole row turns **green** | `match_summary_card._draw_rows`, flag carried by `_normalise_batters` / `_normalise_bowlers` |
| HTML analysis + Mini App scorecard tab | `-IP` after the name | `match_webapp_service.build_scorecard`, which feeds both |
| Mini App scorecard rows | a green `-IP` tag | `app.js:impactTag` + `.tb-row-impact` |

The image rows are 4-tuples `(name, value1, value2, is_impact)`; `_draw_rows`
still accepts the old 3-tuple shape so an un-updated caller degrades to "no
tint" rather than an IndexError mid-render.

**Changing `app.js` or `style.css` means bumping the `?v=` query on both tags in
`static/cricket/index.html`** — Telegram's WebView otherwise pins the old file.

---

## Milestone messages

An admin-configurable message and/or image/GIF fired when something notable
happens mid-match.

### Where it lives

| Rule | Where |
|---|---|
| Detection (diff before/after an over) | `services/milestones.py` |
| Dispatch | `handlers/cipl_play.py` — `_fire_milestones_async`, called from `_run_over` and `_complete_match` |
| Sending, caption rendering, send-method choice | `services/event_media_service.py` — `fire_event_media`, `render_caption`, `_send_kind` |
| Configurable events | `event_media_service.EVENT_KEYS` |
| Storage | `models.EventMedia` (`caption` column; migration in `database._migrate_add_columns`) |
| Web admin | `admin.py` `/media` + `/media/<event_key>`, `templates/admin_media_detail.html` |
| Telegram admin | `handlers/setmilestone.py` — `/setmilestone` |

### What fires

`fifty`, `century`, `three_fer`, `five_fer`, `hattrick`, `partnership_50`,
`partnership_100`, `team_100`, `team_150`, `team_200`, `match_won`,
`impact_player`.

Milestones are **crossings**, not equalities: a batter going 48 → 52 gets their
fifty. Each fires once, and one over can carry several at once.

Hat-tricks need no tracking here — `cipl_match.simulate_over` already calls
`match_engine.note_bowler_ball`, which maintains `wkt_streak` and sets
`hattrick` on the bowler's stat row (run-outs excluded). This finally makes the
`hattrick` event key reachable; it had been configurable in the admin UI for a
long time without any live loop emitting it.

### Captions

Placeholders are substituted with `str.replace`, **never `str.format`** —
admin-written text may contain stray braces, and `format()` would raise inside a
live match. This mirrors `services/commentary_service._render`, which does it
for the same reason. Unknown placeholders are left as written.

Available fields: `event_media_service.CAPTION_FIELDS`.

A row may carry media, a caption, or both. Caption with no media is valid and
goes out as a plain message, so an admin can set a milestone message without
finding an image.

Milestones are dispatched with `cooldown=False`: the 8-second anti-spam window
exists for 6-6-6 overs, and two batters reaching fifty in the same over must
both be announced. They are posted on a background task so a slow upload never
delays the next bowler prompt — the same pattern as
`handlers/match.py:_fire_event_media_async` for the ball-by-ball mode.

---

## Verifying a change here

```bash
python -m pytest tests/test_cipl_impact_player.py tests/test_milestones.py \
                 tests/test_event_media_caption.py tests/test_impact_player.py -q
python -m pytest tests/ -q          # full suite
```

* `tests/test_cipl_impact_player.py` — the window, the outgoing rules, every
  legal batting slot leaving the crease indices untouched, the dismissed-batter
  insert, one swap per team surviving the innings swap, the replaced bowler
  being unable to bowl or field, and the innings-break rebuild.
* `tests/test_milestones.py` — crossings, fire-once, several in one over, a
  fallen partnership, and the innings-boundary guard.
* `tests/test_event_media_caption.py` — caption substitution (including stray
  braces), send-method selection, backwards compatibility for caption-less rows,
  and that every key the detector emits has an admin UI entry.
* `tests/test_impact_button_ownership.py` — that the other captain is not locked
  out of the shared 🔄 button, that the picker stays personal even with an empty
  registry, and that the callback data round-trips inside Telegram's 64-byte cap.
* `tests/test_impact_command.py` — `/impact` opens the picker at every approach
  step, each captain gets their own side, and the button sits beside View Match
  on every prompt.
* `tests/test_impact_scorecard_marking.py` — the impact flag survives the card's
  tuple flattening, and the card still renders.
* `tests/test_impact_player.py` — the pre-existing Mini App suite; it must stay
  green, since `match_webapp_service` now aliases the shared helpers.

End to end: run `/lpbot` (fastest full over-by-over match), confirm the Impact
Player button appears on the over summary, that a batter at the crease is
refused, that the substitute walks out at the chosen slot, and that a configured
`fifty` or `team_150` message fires. Then bowl first in a second match and use
the swap in innings 1, to exercise the innings-break rebuild.

## Known gap

`/impact` is registered but does not appear in the slash menu: the player-facing
menu is already at Telegram's hard 100-command ceiling (see
`bot.py:BOT_MENU_COMMANDS` and `tests/test_bot_menu_commands.py`), which ~29
other commands already sit outside. `/setmilestone` is published in the admin
menu, which is allowed to overflow.
