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

`/setteamlogo` is deliberately **not** in the slash menu: both player scopes sit
at Telegram's 100-command ceiling, so publishing it would cost an existing
command its entry. It is named in `/help` and in `/howto` beside `/teamname`,
and `/teamname` offers it to anyone who has not set one.

## What an upload has to be

Checked in `services/team_logo_service.normalise_image`, reusing the limits the
website's custom card art has used for a long time
(`services/player_image_service.py`):

* PNG, JPG, WEBP, GIF or BMP, at most **5 MB**
* at least **200×200**
* no wilder than **2:1** — the crest panel is tall-ish, and anything longer
  renders as a sliver
* stored re-encoded as a PNG, fitted to **512 px**

The re-encode is the point, not a side effect: it drops EXIF, animation frames
and anything else the decoder found interesting, so what reaches the card
renderer is a plain PNG.

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

## What this does not do

There is no rate limit. A rejected user can resubmit immediately, and every
upload is a DM an admin has to action. If the queue becomes a burden, the
obvious knobs are a cooldown after a rejection, a daily cap per user, and an
auto-hold after repeated rejections.
