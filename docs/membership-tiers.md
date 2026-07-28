# Membership tiers

Four paid tiers, granted manually by an admin (there is no self-serve payment).
Everything below is configured in **`config.SUBSCRIPTION_TIERS`** and read
through `services/subscription_service.py` — no server-side feature code, admin
page or upsell message hard-codes a tier name, so adding or retuning a tier is a
config change. (The one exception is the Arena client's pre-flight Autoplay
alert in `static/cricket/app.js`, which has no access to the config; the
server's own 403 message is built from `tiers_with_perk("autoplay")`.)

Declaration order in `SUBSCRIPTION_TIERS` **is** the rank
(`bronze < silver < platinum < diamond`); `tier_rank()` reads it.

## What each tier gets

| Perk | 🥉 Bronze | 🥈 Silver | 🏆 Platinum | 💎 Diamond |
|---|---|---|---|---|
| Price / 30 days | ₹19 | ₹59 | ₹99 | ₹149 |
| Instant coins | 29,000 | 49,000 | 1,000,000 | 2,000,000 |
| Instant gems | 150 | 499 | 1,000 | 1,500 |
| Instant quest points | — | 499 | 1,000 | 1,500 |
| Instant packs | — | Star Pack | Legend Pack | Legend Pack + Ultimate Legend Pack |
| `/cmumysterybox` | every 15 days | every 8 days | every 4 days | every 2 days |
| `/cmuweekly` (85+ card, 7-day cd) | — | — | ✅ | ✅ |
| `/cmuchest` coin chests | — | — | 3 per 10 days (60k–99k) | 5 per 7 days (70k–110k) |
| Player Market discount | — | — | 5% | 10% |
| Mini App daily login reward | 1× | 1× | 1× | **2×** |
| Cooldown reduction | — | −5 min/hr | −10 min/hr | −15 min/hr |
| `/autobuild` + `/wpmbot` | ✅ | ✅ | ✅ | ✅ |
| Mini App Autoplay | — | ✅ | ✅ | ✅ |

### Command cooldowns

`cooldown_reduction_min_per_hour` shaves N minutes off every hour of a
command's base cooldown (`×(1 − N/60)`), so one number drives every command.
Bronze deliberately runs on the free-user timers — the entry tier buys rewards
and premium commands, not speed.

| Command | Base (free & 🥉 Bronze) | 🥈 Silver | 🏆 Platinum | 💎 Diamond |
|---|---|---|---|---|
| `/claim` | 1h | 55m | 50m | 45m |
| `/gspin` (Mini App) | 8h | 7h 20m | 6h 40m | 6h |
| `/daily` (Mini App) | 24h | 22h | 20h | 18h |

Base values live in `config.py` (`CLAIM_COOLDOWN`, `GSPIN_COOLDOWN`,
`DAILY_COOLDOWN`). An admin override saved on the website
(`BotCommand.cooldown_seconds`) replaces the base value, and the tier reduction
still applies on top — so retuning a base cooldown from the admin panel keeps
the ladder proportional.

A tier is active only while `subscription_expires_at` is in the future.
`get_tier()` checks that on every read, so an expired tier behaves exactly like
a free account with no background job involved.

## Upgrades

Upgrading keeps the member's **remaining paid time** and credits a top-up
bundle — never a second full subscription. The bundle is roughly **half the raw
instant difference** between the two tiers, plus whichever signature packs the
source tier never granted.

| Upgrade | Coins | Gems | QP | Packs |
|---|---|---|---|---|
| 🥉 Bronze → 🥈 Silver | 10,000 | 175 | 250 | Star Pack |
| 🥉 Bronze → 🏆 Platinum | 485,000 | 425 | 500 | Legend Pack |
| 🥉 Bronze → 💎 Diamond | 985,000 | 675 | 750 | Legend Pack, Ultimate Legend Pack |
| 🥈 Silver → 🏆 Platinum | 451,000 | 251 | 251 | Legend Pack |
| 🥈 Silver → 💎 Diamond | 975,000 | 500 | 500 | Legend Pack, Ultimate Legend Pack |
| 🏆 Platinum → 💎 Diamond | 500,000 | 250 | 250 | Ultimate Legend Pack |

The halves are picked so that **hopping never pays more than going direct**
(Bronze → Silver → Diamond credits the same coins as Bronze → Diamond), which
`tests/test_membership_tiers.py` pins.

Downgrades are not an upgrade path: `subscription_service.upgrade()` raises
`ValueError` for a same or lower target. To move a member down, deactivate and
activate the lower tier.

## Granting

**Website** — user detail page → one activation button per tier, plus an
upgrade button for every tier above the member's current one. Both are rendered
from `SUBSCRIPTION_TIERS`, so a new tier appears with no template change.

**Telegram (owner only)** — `/grant`:

```
/grant Bronze <telegram_id>            activate Bronze
/grant Diamond <telegram_id>           activate Diamond
/grant Silver2Diamond <telegram_id>    upgrade → Diamond
/grant Platinum2Diamond <telegram_id>  upgrade → Diamond
/grant Upgrade Diamond <telegram_id>   upgrade the member's active tier → Diamond
```

Tier names, single-letter shorthands (`b`/`s`/`p`/`d`, e.g. `p2d`) and the
`->`/`to`/`2` separators are all accepted. Only the *target* matters — the
source is always the member's live tier.

## Where the perks are enforced

| Perk | Enforced in |
|---|---|
| Instant / upgrade bundles | `subscription_service.grant_instant_rewards`, `grant_upgrade_rewards` |
| Mystery Box cadence | `handlers/cmumysterybox.py` via `mysterybox_cooldown_seconds` |
| Weekly card, coin chests | `handlers/premium_drops.py` via `has_weekly_card`, `coin_chest_config` |
| Market discount | `subscription_service.market_price` (bot `/playermarket`, Mini App market API, `services/global_market.buy_player`) |
| Daily login multiplier | `services/login_streak_service.claim_login_reward` via `daily_login_multiplier` |
| Command cooldowns | `services/command_config_service.get_user_cooldown` via `cooldown_seconds` |
| `/autobuild`, `/wpmbot` | `handlers/lineup.py`, `handlers/wpmbot.py` via `has_premium_commands` |
| Autoplay | see below |

## Autoplay is locked for free users

Autoplay (handing your side to the AI in the Arena) is a paid perk from Silver
up. It is gated in three places so a free account can't reach it:

1. **State** — `crickidex_arena.serialize_match_state` reports
   `autoplay.premium`, and the Arena renders the pill as `🔒 PRO`. The client
   flag defaults to *locked*, so a missing field never unlocks anything.
2. **Toggle** — `POST /api/match/autoplay-status` returns `403
   premium_required` when a free user tries to switch it ON. Switching OFF is
   always allowed, so a lapsed subscriber is never stuck with it on.
3. **Execution** — `POST /api/match/autoplay` returns `403 premium_required`,
   so even a hand-crafted request cannot make the AI play a ball.

Entitlement is re-checked on every request, so a subscription that lapses
mid-match stops Autoplay on the next tick.
