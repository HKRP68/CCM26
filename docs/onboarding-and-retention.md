# Onboarding & retention

Most players who leave do it in their **first session**. /start used to print
about 80 commands in one message, and /debut handed out a squad and then went
quiet. This change set targets that first session, adds a way to win back
players who drift away, and gives the website numbers that show whether it
works.

| Piece | Where | Default |
|---|---|---|
| Rich `/start` card + `/commands` | `handlers/onboarding.py`, `services/onboarding_rich.py`, `services/command_catalog.py` | always on |
| Forced Official GC join | `services/gc_gate.py`, middleware `_gc_check` in `bot.py` (group −12) | **OFF** |
| New-player journey | `services/onboarding_service.py` | ON |
| Comeback DMs | `services/comeback_service.py` (hourly job) | ON |
| Cooldown-ready pings (improved) | `services/cooldown_notifier.py` | on (existing) |
| D1/D7/D30 cohorts + funnel | `services/retention_stats.py`, dashboard | always on |

All switches are on **Website → Maintenance** (the *📢 Force Official GC Join*
and *🔁 Retention* panels).

## `/start`

A bare `/start` answers with one of three rich cards (Bot API 10.1
`sendRichMessage`, with an HTML fallback on older servers):

* **No account yet:** a short pitch and three steps (join the Official GC,
  `/debut`, first match), with *Join GC*, *✅ I've joined* and *Start — /debut*
  buttons. The GC step is ticked when the visitor is already a member.
* **Mid-journey:** their journey checklist (below).
* **Everyone else:** a status card: coins, gems, squad, record, login
  streak, and **what's ready to collect right now** (daily, card pick, claim,
  free pack). This uses the same readiness rule as the DM pings.

Every deep-link payload (`/start ref…`, `debut`, `cmd_…`, Mini App screens…)
behaves exactly as before. The old command list lives on at **`/commands`**,
grouped into collapsible sections.

## Forced Official GC join

With the switch ON (and `official_group_id` set under Settings), every command
and button is locked until the player is a member of the Official GC.

* Still open: `/start` `/debut` `/help` `/howto` `/guide` `/feedback`
  `/redeem` `/botstatus` `/commands`, plus the referral code /debut asks for.
* The prompt carries **🔗 Join Official GC** and **✅ I've joined**. The
  button re-checks on the spot and unlocks immediately.
* Membership is cached per user: 10 min for members (`GC_CACHE_TTL_SECONDS`),
  30 s for non-members. Joining the GC (seen by the group's own join message)
  clears the cache at once.
* Anything said *inside* the Official GC passes. Admins on the bypass list are
  never locked. Group chatter is dropped silently; only commands and taps get
  the prompt.
* **The bot must be an admin of the Official GC.** If Telegram can't answer
  the membership lookup, the gate **fails open** rather than locking the bot.

Order of the gates: forward-only → maintenance → ban → **Official GC** →
Rookie → auction focus.

## New-player journey

Stamped on by `/debut` (`User.onboarding_started_at`). Accounts that predate
it are never shown the checklist.

| Step | Completed by | Reward |
|---|---|---|
| Join the Official GC *(only if a GC is configured)* | ✅ I've joined, or joining the group | 300 🪙 |
| Claim your first player | `claim` quest event | 200 🪙 |
| Collect your daily reward | `daily` | 200 🪙 |
| Play your first match | `match_played` / `vsbot_played` / `quick_match_played` | 500 🪙 + 5 💎 |
| Open a pack | `free_pack_opened` / `pack_open` | 300 🪙 |
| Win a match | `match_won` / `vsbot_won` / `quick_match_won` | 500 🪙 + 10 💎 |
| **Finish them all** | | **1,000 🪙 + 25 💎** |

Steps are driven by the quest event stream: `quest_service.track_event`
forwards every event to `onboarding_service.on_event`, so no gameplay handler
needed a new hook. A completed step pays once (on the caller's transaction)
and queues a "✅ step done, next up…" card, which a 20-second job delivers.

## Comeback DMs

An hourly job finds players quiet for **1 / 3 / 7 / 14 days** (latest of
`last_seen_at`, `last_match_date`, `created_at`) and DMs each tier once per
absence with a reward behind a **🎁 Claim** button. Coming back resets the
ladder.

* Skips: `/notifications` off, banned, `dm_blocked` (set when Telegram says
  the bot was blocked, cleared when they talk to the bot again), anyone quiet
  for more than `COMEBACK_MAX_INACTIVE_DAYS` (30).
* Never two nudges within 36 h. Quiet hours (23:00–07:00 IST) skip the tick.
* Rewards are editable as JSON on the Retention panel, e.g.
  `{"1": {"coins": 300, "gems": 0}, "7": {"coins": 1500, "gems": 10, "enabled": false}}`.
* Testing: `COMEBACK_TIER_UNIT=minutes` makes the tiers minutes instead of
  days, and `COMEBACK_INTERVAL_SECONDS` shortens the tick.

`User.last_seen_at` is written by a middleware (group −45) at most once an
hour per user.

## Cooldown-ready pings

The existing notifier now sends **one DM per user** listing everything that
became ready (rather than up to four pings), adds a Mini App button for the free
pack, warns when a 2+ day **login streak** is about to lapse, and stops
messaging users who blocked the bot.

## Dashboard: cohorts & funnel

* **New-player retention:** players grouped by their `/debut` day (IST). D1,
  D7 and D30 are the share with *any* activity exactly that many days later. The
  KPI tiles are size-weighted averages, and cells show `·` until that day has passed.
* **Funnel (45 days):** opened `/start` (`start_visits`, which includes people
  with no account) → ran `/debut` → joined the GC → first match → finished the
  journey → came back on day 1.
