# Rookie mode — the members-only switch

Rookie mode turns the whole bot into a paid product with one toggle. While it
is **OFF** (the default, and what every existing deployment wakes up with)
nothing changes. While it is **ON**:

* using the bot at all requires an **active membership of at least 🐣 Rookie**
  (₹15 / 30 days) — every command, every button, and the entire Mini App;
* a player without one can still walk through the front door: `/debut` creates
  the account, and a short list of doorway commands keeps working so they can
  find out what a membership costs and reach a human;
* **every higher tier already includes it** — Bronze, Silver, Platinum and
  Diamond all outrank Rookie, so nobody has to buy Rookie *as well*;
* admins on the bypass list are never locked out.

The switch lives on **Website → Maintenance → 🐣 Rookie Mode**, next to
maintenance mode, and the two are independent:

| | Maintenance mode | Rookie mode |
|---|---|---|
| Purpose | the bot is *paused* | the bot is *members-only* |
| Who gets through | nobody (except bypass admins) | members of any tier |
| In-flight matches | protected — callbacks and live-match APIs stay open | not applicable: a non-member could not have started one |
| Message | "back shortly", with an ETA | the membership upsell, with the price list |

## What a locked-out player can still do

`services/rookie_gate.FREE_COMMANDS` — kept deliberately short, and pinned by
`tests/test_rookie_gate.py` to commands `bot.py` actually registers, so a
renamed alias can't quietly lock the front door:

| Command | Why it stays open |
|---|---|
| `/debut` `/d` | creating an account is the way in — it must be free |
| `/start` `/s` | the welcome message, which says the bot is members-only |
| `/membership` `/plans` … | status, perks and prices: what the upsell points at |
| `/howto` `/help` `/guide` | reading about the game is not playing it |
| `/feedback` `/fb` | a locked-out player must be able to reach the admins |
| `/redeem` `/code` | `/debut` itself asks for a referral code right after |
| `/botstatus` `/ping` | answers "is the bot down, or am I locked out?" |

Two buttons stay tappable for the same reason (`FREE_CALLBACK_PREFIXES`): the
**Skip** button on that referral prompt, and the inert filler button.

A brand-new player typing their referral code as plain text is let through too,
so the gate never eats the answer to a question the bot just asked — but only
under exactly the conditions the handler that consumes it requires: the
`awaiting_referral_code` flag is set, the chat is a DM, **and** the text is
shaped like a code. The flag survives until a code is redeemed or Skip is
tapped, so a wider exemption would hand anyone who answers with neither a
permanent pass into every text-driven handler (WordChase, Bluff, XI
quick-select). `tests/test_rookie_gate.py` pins the gate's code pattern against
`handlers.redeem`'s, so the two copies of that rule cannot drift.

Everything else is stopped. Plain (non-command) messages are stopped
**silently**, so a locked-down bot doesn't reply to every line of group chatter.

The gate only looks at things a player *did*: messages and button taps. Telegram
service messages (someone joined or left a chat) and non-message updates (the
bot being added to a group, `my_chat_member`) pass straight through — otherwise
turning Rookie mode on would silence the group welcome that tells a brand-new
player to run `/debut`, and break chat tracking with it.

## How it is enforced

Two gates, one rule set (`services/rookie_gate.py` holds the rules and touches
no database; both callers resolve the user and pass the answer in):

1. **Bot** — a `TypeHandler` middleware in `bot.py`, handler group `-10`. It
   runs *after* the maintenance (`-30`) and ban (`-20`) middleware, so "the bot
   is down" and "you are banned" still win over "you need a membership", and
   before every command handler (group `0`+). PTB runs only the first matching
   handler per group, so each middleware has a group of its own; the numbers are
   spaced by ten to leave room for the next one.
2. **Mini App** — a Flask `before_request` hook in `admin.py`
   (`_block_miniapp_without_membership`), which covers every path under the Mini
   App prefixes — including endpoints added later — and answers
   `403 {"error": "rookie_required"}`. The Mini App and the fantasy picker both
   turn that into a full-screen 🔒 lock, the same way they handle maintenance.
   Ad-network postbacks (`/api/ads/reward`, `/api/adsgram/reward`) stay open:
   they are an external service reporting a finished ad, not a player using the
   app, and dropping one destroys a reward that was already earned.

Both paths cost nothing while the mode is off — the config flag is checked
first, and the membership lookup only happens once that check passes.

### Freshness

The bot caches each user's membership for `ROOKIE_CACHE_TTL_SECONDS`
(default 30) so a button tap doesn't pay a database round trip. A membership
activated on the website therefore unlocks the bot within about half a minute,
with no restart. Expiry needs no job at all: `get_tier()` compares
`subscription_expires_at` on every read, so a lapsed membership locks the bot
again by itself.

## Admin controls

Website → Maintenance page, 🐣 Rookie Mode panel:

* **Enable / Disable** — the switch. The confirm dialog spells out what
  non-members lose.
* **Custom lock message** — replaces the default upsell (Telegram HTML), in the
  bot *and* in the Mini App lock screen. Left blank, the default quotes the live
  price list out of `config.SUBSCRIPTION_TIERS` and points players at
  `/membership`. Both front ends render it through the same
  escape-then-allow-`<b>`/`<i>`/`<br>` path, so an admin-written message can
  never inject markup. (The short plain-text `rookie_required_alert()` is still
  what a Telegram callback answer shows — those alerts render text, not HTML.)
* **Member count** — "X of Y registered players hold an active membership",
  counted exactly the way the gate counts it (an expired tier is not a
  membership), so the number answers "how many am I about to lock out?" before
  the switch is flipped.

Stored on `game_config` as `rookie_mode` / `rookie_message`; the migration in
`database.py` adds both columns with `rookie_mode` defaulting to **FALSE**.
Saving invalidates the config cache, so a separate bot process picks the change
up on the next update rather than on the next restart.

## `/membership`

The command the upsell tells players to send, and the reason it is a doorway
command. It shows their tier, days left, the perks that tier actually unlocks,
and the plans they can step up into — all rendered from
`config.SUBSCRIPTION_TIERS`, so a re-priced or retuned tier shows up with no
code change. A lapsed member is told **Expired**, not "Free", so a grant that
ran out doesn't read like it never happened.
