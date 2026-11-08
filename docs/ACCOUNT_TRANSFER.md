# Recover a Telegram game account

Open **Admin → Users → Transfer Account**, or use **Transfer Account** on a
player's detail page to prefill their old Telegram user ID.

1. Verify independently that the claimant owns the old account and controls the
   new Telegram account. Enter numeric Telegram IDs, not internal game IDs or
   usernames. Record the verification method or support ticket (10–200 characters).
2. Preview the old account, squad count, coins, gems, quest points and subscription.
   If the new ID already has an account, review its current balances and squad too.
3. Check the ownership verification box, retype the destination ID and select
   **Confirm Transfer** within 10 minutes. A changed account requires a new preview.
   For an existing destination, also check **Replace the new ID's current game
   account**. This extra confirmation is required on the server as well.
4. Ask the player to send `/start` from the new account to refresh their Telegram
   name and username. They keep the original game account and do not need to debut
   again unless that account was already marked as needing debut.

Existing destination accounts are supported. The old account replaces the new
ID's active account, including squad, money and progress. Squads and balances are
never combined. The displaced account retains its original internal user ID and
complete linked data under a reserved, negative internal identity. It is disabled,
notifications are switched off, and it cannot be accessed through Telegram. The
receipt links to the archived account for admin review and manual support recovery.
This is an archive of the displaced account, not an automatic rollback interface.
Neither live account changes during preview; changes to either account before
confirmation require a fresh preview.

Recovery preserves the original `users.id`, so its squad, career player, currency,
traits, packs, quests, achievements, subscriptions, match records and other linked
data stay attached. Direct Telegram ID ownership and eligibility records are
updated too, including tournament/draft/auction teams, co-owners, franchise
managers, saved XIs, solo auction saves, ad history, giveaway entries, prize
recipients and match-control restrictions. Existing bans, cooldowns, subscription
expiry and restrictions remain. A destination account ban also remains in force
on the recovered account. Archived accounts are excluded from season rankings
and season prizes. Archived tournament prize shares remain with their archived
accounts, preserving receipt reversals and preventing accidental duplicate rewards.
Administrative permissions and historical action
authorship are not transferred; review those separately.

A completed transfer creates an **Audit Log** receipt with the old/new IDs,
verification note, squad size, coins, gems, timestamp and admin IP, and an account
activity entry. These are committed with the ownership update in one transaction.
The transfer page displays the latest 25 receipts. Invalid or repeated confirmation
submissions cannot repeat a completed transfer. Replacement also creates an
`account_transfer_archive` audit entry. Archiving, identity changes, linked record
updates and both receipts share one transaction; any failure rolls them all back.
No database migration is required.

Deleting a Telegram account does not delete its game account. If the game's data
was deleted/reset separately, restore an appropriate database backup before
attempting recovery. This feature cannot recreate data that no longer exists.
