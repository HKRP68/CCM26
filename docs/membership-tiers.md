# Membership tiers

Four paid tiers, granted manually by an admin (there is no self-serve payment).
Everything below is configured in **`config.SUBSCRIPTION_TIERS`** and read
through `services/subscription_service.py` — no tier name is hard-coded in
feature code, so adding or retuning a tier is a config change.

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
| Cooldown reduction | −5 min/hr | −10 min/hr | −20 min/hr | −30 min/hr |
| `/autobuild` + `/wpmbot` | ✅ | ✅ | ✅ | ✅ |
| Mini App Autoplay | — | ✅ | ✅ | ✅ |

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
