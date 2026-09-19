# Team logos, and the approval they go through

`/setteamlogo` lets a manager put their own crest on their team. That image is
then drawn on every scorecard of every match their team plays, in front of
everyone in the chat — so it is not applied on upload. It is held until a bot
admin approves it from their DM, and a rejection tells the owner why.

## The flow

```
/setteamlogo (DM)  →  validate  →  TeamLogoRequest(pending)  →  DM every admin
                                                                     │
                                      ┌──────────────────────────────┴───────┐
                                  ✅ Approve                             ❌ Reject
                                      │                                      │
                          copied onto users.team_logo_*            reason picked or typed
                                      │                                      │
                             "your logo is approved"            "not approved — <reason>"
```

Nothing about the team changes while a request is pending. Whatever crest the
team has today — or none — keeps rendering until an admin decides. That is the
same rule `CareerChangeRequest` follows for career-player names, and it exists
so a queue backlog never leaks an unreviewed image onto a card.

## Commands

| Command | Who | What |
| --- | --- | --- |
| `/setteamlogo` | anyone, DM only | Send an image with the command, reply to one, or send the command and then the image. |
| `/setteamlogo remove` | anyone | Clear an approved crest. Needs no review — removing shows nobody anything. |
| `/logoqueue` | bot admins | List what is waiting and re-send any review card. The recovery path when a DM was missed or the bot restarted. |
| `/logounhold <telegram id>` | bot admins | Lift a consecutive-rejection hold. |
| `/previewsummary` | bot admins | Render a summary card from canned data, through the real delivery path, with your own crest on innings 1. |

`/setteamlogo` is deliberately **not** in the slash menu: both player scopes sit
at Telegram's 100-command ceiling, so publishing it would cost an existing
command its entry. It is named in `/help` and in `/howto` beside `/teamname`,
and `/teamname` offers it to anyone who has not set one.

## Team colour

`/setteamcolour #aa001b` (also `/teamcolour`, and the `color` spelling) sets the
colour a team wears on every scorecard — its header bar, crest panel and table
divider. Unlike the crest it applies **immediately**: a hex code carries nothing
to moderate, and the card picks readable text for whatever is chosen
(`match_summary_card._readable_on`).

### What the colour does *not* touch

The batters' and bowlers' names, and every number beside them, are drawn in
plain ink (`match_summary_card.VALUE_INK`) whatever the team picked. The
reference poster tints a not-out score in the team's colour, and this is a
deliberate departure from it: once teams choose their own colours, a tinted
number reads as saying something about the *team* rather than about the
innings — and the trailing `*` is what marks a not-out anyway, so the colour
was only repeating it. `_draw_rows` takes no colour argument at all, which is
what keeps this from drifting back.

Two colours on the card are fixed rather than ink: the gold of an award and the
green of an Impact substitute. Both mark something no other element shows.

Text drawn *on* a team-coloured bar — the crest initials, the team name, the
overs meta and the score — is the one exception, and it is not "team-coloured
text": `_readable_on` picks black or white per bar so the text stays legible
whatever the team chose. Forcing those to black would make them vanish on any
dark colour.

It takes `#aa001b`, a bare `aa001b`, three-digit `#abc`, or one of the names in
`handlers/team.COLOUR_NAMES` (`crimson`, `navy`, `gold`, …). `/setteamcolour`
alone shows the current colour; `remove` clears it. The confirmation renders a
**live swatch** — the real bar, crest panel and a sample row — because a hex
code tells nobody what their scorecard will look like.

Two teams can choose the same colour, and two identical innings blocks read as a
rendering fault. `card_identity.separate_colours` keeps innings 1 exactly as
chosen and pushes innings 2 away from it — lightening a dark clash, darkening a
light one — falling back to the admin default only when it cannot separate them
at all. Distinct colours are never touched.

Admins can set or clear it from a user's page on the website
(`/users/<id>/team_colour`).

## What an upload has to be

Checked in `services/team_logo_service.normalise_image`, reusing the limits the
website's custom card art has used for a long time
(`services/player_image_service.py`):

* **PNG only**, at most **5 MB** — a PNG is the only common format that can
  carry a transparent background, and a crest without one draws as a square
  tile sitting on the team's colour instead of on it
* at least **200×200**
* no wilder than **2:1** — the crest panel is tall-ish, and anything longer
  renders as a sliver
* stored re-encoded as a PNG, fitted to **512 px**

The re-encode is the point, not a side effect: it drops EXIF, animation frames
and anything else the decoder found interesting, so what reaches the card
renderer is a plain PNG.

### Teaching, not just refusing

A refusal that only states the rule teaches nobody anything, so the guidance
lives in `TRANSPARENCY_HELP` and surfaces in four places:

1. **A non-PNG** is refused with why, plus how to get one — a background
   remover, a phone's *Share → Save as*, or Canva/Figma's transparent export.
2. **A photo rather than a file.** Telegram re-encodes anything sent as a
   *photo* into JPEG, which strips the alpha channel — so a compressed send can
   never be a usable crest however the file started. `_photo_from` returns the
   *kind* alongside the file so this is caught before the download, and the
   reply explains the attachment menu. The await slot stays armed so they can
   just try again.
3. **A fully opaque PNG** is accepted and queued, but the uploader is warned it
   will tile, and the admin's review caption carries the same note — a Telegram
   preview renders transparency on white either way, so the reviewer cannot see
   it for themselves.
4. **A preset rejection reason**, "🪟 Needs a transparent background", whose
   message is the same how-to. One tap, and the owner gets an answer rather
   than a dead end.

## Rejection reasons

Tapping ❌ Reject swaps the keyboard for a grid of one-tap reasons — not your
artwork · offensive/NSFW · too low quality · ads or links · wrong shape — plus
**✍️ Write my own**, which prompts for free text and sends it to the owner
verbatim. The reason is stored on the request as `review_note`, so the queue
is its own audit trail.

The reason keys ride in `callback_data`, which Telegram caps at 64 bytes, so
they are short and stable. Renaming one changes what an in-flight button means.

## Where the bytes live

In `stored_assets`, through `services/asset_store.py`, under the
`data/team_logos` durable root. The host filesystem is rebuilt on every deploy,
so the on-disk copy is only a cache that `asset_store.ensure` heals on a read
miss. Telegram's own `file_id` is kept alongside, so `/purse` and the DMs can
re-send the image without a render.

Each submission gets its own key (`<user id>-<timestamp>-<random>.png`). It has
to be unique per *submission*, not per user: while a new crest is in review the
previously approved one is still being drawn on cards, and a shared key would
let unreviewed bytes overwrite it. A timestamp alone is not enough — two
uploads in the same second collide.

## How a crest reaches a card

Resolved at **render time**, not stored in the scorecard payload:

```python
# services/scorecard_delivery.py
_summary_style(inn1_team, inn2_team)   →  inn1_logo_png / inn2_logo_png
_live_style(is_first_innings, team)    →  team_logo_png
```

This is the same call the module already made for the admin accent colours, and
for the same reason: a match redrawn by `/lastscorecard` months later should
carry the crest the team has *now*, not the one it had when the match was
played. A team with no approved crest falls back to its initials on the summary
card, and to the CMU mark on the batting and bowling cards.

The lookup goes through the **team name**, because that is what the stored
payloads have held since long before crests existed. Two users with the same
team name is possible and rare; the most recently approved crest wins, which at
least stays stable rather than alternating. Results are cached for five minutes
and invalidated on every approve, reject and removal, so a decision shows up
immediately rather than within the minute.

## Two things to know if you are changing this

**The review keyboard is exempt from the button owner-lock.** It is sent to the
admins while handling the *uploader's* update, so `services/button_access.py`
would otherwise pin it to the uploader and answer every admin with "this button
is not for you". `tlogo:` is listed in `SHARED_CALLBACK_PREFIXES`, and
`handlers/team_logo.py` authorises each press with `is_admin()` instead. The
owner's own Withdraw button checks the request's `telegram_id`.

**Every admin gets their own copy of the review card.** Whoever presses first
wins: the service refuses a second decision because the row is no longer
pending, and the other copies are edited to say who decided. The message ids
for that tidy-up live in `bot_data`, so a restart loses them — the buttons still
work, they just stop being cleaned up.

## Submission limits

Every upload is a DM an admin has to action, so one user must not be able to
fill the queue on their own. Three limits, all in `team_logo_service`:

| Limit | Default | Why |
| --- | --- | --- |
| `DAILY_SUBMISSION_CAP` | 5 per 24h | Counts *submissions*, not decisions — withdrawing does not buy another go. |
| `REJECT_COOLDOWN_SECONDS` | 1 hour | A resubmission straight after a rejection is usually the same image lightly edited. The wait is also when the reason gets read. |
| `CONSECUTIVE_REJECT_LIMIT` | 3 | Three refusals in a row with nothing approved between pauses uploads until an admin lifts it. |

They are checked before the image is decoded, so a throttled user does not pay
for a validation pass they cannot use, and the message says which limit was hit
and when it clears.

A hold is lifted with `/logounhold <telegram id>` — deliberately a command
rather than an automatic expiry, because a hold means an admin decided three
times and a human should decide to undo that. The id is on the review card.
`clear_hold` marks the streak `cancelled` rather than rewriting what was
decided, so the audit trail survives.

## Sessions: do not reach for a second one

`_store` and `_forget` write `stored_assets` through the **caller's** session,
not through `asset_store.put` / `asset_store.drop`. Those open their own, and
everything here runs inside the caller's open transaction. A second pooled
connection writing while the first is mid-transaction is the pool-exhaustion
trap `services/player_image_service` documents at length; on SQLite it fails
outright, and because the failure is swallowed the row survives as an orphan —
the bytes of a rejected logo would outlive the rejection.

Writing through the caller's session also means the bytes and the request row
land together or not at all.

For the same reason every read here calls `_flush(session)` first:
`SessionLocal` is built with `autoflush=False`, so a status this service just
set is *not* visible to a later filter on that status until something flushes.
