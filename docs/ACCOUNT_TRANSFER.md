# Recover a Telegram game account

Open **Admin → Users → Transfer Account**, or use **Transfer Account** on a
player's detail page to prefill their old Telegram user ID.

1. Verify independently that the claimant owns the old account and controls the
   new Telegram account. Enter numeric Telegram IDs, not internal game IDs or
   usernames. Record the verification method or support ticket (10–200 characters).
2. Preview the account, squad count, coins, gems, quest points and subscription.
3. Check the ownership verification box, retype the destination ID and select
   **Confirm Transfer** within 10 minutes. A changed account requires a new preview.
4. Ask the player to send `/start` from the new account to refresh their Telegram
   name and username. They keep the original game account and do not need to debut
   again unless that account was already marked as needing debut.

The destination must be unused: existing game accounts are never merged or
replaced. Tell the player to wait before `/debut`. Conflicting team memberships,
saved lineups or control records cause the whole transfer to roll back.

Recovery preserves the original `users.id`, so its squad, career player, currency,
traits, packs, quests, achievements, subscriptions, match records and other linked
data stay attached. Direct Telegram ID ownership and eligibility records are
updated too, including tournament/draft/auction teams, co-owners, franchise
managers, saved XIs, solo auction saves, ad history, giveaway entries, prize
recipients and match-control restrictions. Existing bans, cooldowns, subscription
expiry and restrictions remain. Administrative permissions and historical action
authorship are not transferred; review those separately.

A completed transfer creates an **Audit Log** receipt with the old/new IDs,
verification note, squad size, coins, gems, timestamp and admin IP, and an account
activity entry. These are committed with the ownership update in one transaction.
The transfer page displays the latest 25 receipts. Invalid or repeated confirmation
submissions cannot repeat a completed transfer. No database migration is required.

Deleting a Telegram account does not delete its game account. If the game's data
was deleted/reset separately, restore an appropriate database backup before
attempting recovery. This feature cannot recreate data that no longer exists.
