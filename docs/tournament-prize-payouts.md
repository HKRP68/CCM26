# Tournament prize payouts

Tournament prizes increase actual account coins and gems. Each recipient gets a
separate payout record; the account credit, payment status, balance receipt and
numeric `tournament_prize` activity entry commit together. An award announcement
alone is not a payment.

## Default owner and co-owner split

Every prize earned by a team follows that team's split, including Champion,
Runner-up, Orange Cap, Purple Cap, MVP and the other player/captain awards. The
award still honours its winning player or captain; the money is shared by the
team's saved management group. A match controller does not take the whole
prize merely because their user ID appears in the scorecard.

The default is an equal split among the primary owner and all distinct
co-owners. With one owner and one co-owner the split is 50/50:

| Award earned by the same team | Prize | Owner | Co-owner |
| --- | ---: | ---: | ---: |
| Champion | 60,000 coins | 30,000 | 30,000 |
| Orange Cap | 30,000 coins | 15,000 | 15,000 |
| **Total** | **90,000 coins** | **45,000** | **45,000** |

A solo owner receives 100%. Lets Play teams use their participating user's
identity. Telegram IDs are deduplicated so promoting a co-owner to owner does
not create an extra share. Coins and gems are divided separately as whole
units. The team's awards being planned together form one pool; deterministic
largest-remainder allocation divides that combined total before recording each
award's recipient rows. Thus multiple odd-sized awards do not repeatedly give
all spare units to the owner. The exact configured total is preserved; ties
follow the saved recipient order. Later awards account for existing shares when
dividing the new amount, so rounding stays fair across the full pool. Existing
entitlements are never reallocated when a new award is configured later.

## Custom percentages and locking

Before the tournament starts, admins can set a team's custom prize percentages
under **Manage → Prize sharing**. Choose **Custom percentages** and **Save prize
split**, or return to **Equal shares (default)**. Agree custom percentages with
the recipients before saving. Every recipient must be a current owner/co-owner,
percentages must be positive with at most two decimal places, and the total must
be exactly 100%.

Starting/activating the tournament saves the recipient IDs and split weights.
The first match/payout also freezes any missing snapshot for tournaments created
by older versions. Once locked, later owner/co-owner edits do not transfer prize
rights or change percentages. New season teams start with a fresh equal default;
last season's locked shares and receipts stay with last season. Resetting a
pre-award tournament can clear its lock, but a reset never unlocks existing
payment entitlements or remints an awarded prize.

## Per-recipient receipts and payment states

One honour is retained for each award, with separate payout rows for its
recipients. Those rows save their Telegram IDs and exact coin/gem entitlements.
Changing prize settings, ownership or scorecard controllers later cannot resize
or redirect an existing payout. The honour records the total actually credited,
while its prize snapshot records the original entitlement.

**Manage → Prize payouts** shows the award, team, recipient, coins, gems and
status, with a status filter and **Retry pending and failed payouts** action:

- **Paid:** the recipient's account was credited. The receipt includes payment
  time, account ID, and coin/gem balances immediately after that credit.
- **Pending:** the share is saved but its intended recipient has no account,
  or the award has no assigned owner yet. Other registered recipients can receive
  their shares immediately; a missing recipient's share is not redistributed.
- **Failed:** a payment attempt failed. The row retains a bounded failure reason
  and can be retried after the underlying issue is resolved.

Recipients can open **`/myprofile` → Prize receipts** in their private chat to
see their awards, payment states, amounts, balances after payment and dates,
with pagination for older receipts.
Opening this view also retries their pending/failed shares. A recipient can view
only their own receipts.

Ceremony captions show confirmed credits separately from pending shares. A
completed presentation does not prevent a later payment retry, and retrying a
payment does not require reposting the award.

## Recovery

1. Open **Manage → Prize payouts** and check its saved recipient IDs and
   payment states. Locked recipients cannot be replaced by editing team owners.
2. Have an unregistered recipient run `/debut`. Registration automatically
   retries the exact shares saved for that Telegram ID.
3. Registered recipients can open **`/myprofile` → Prize receipts** to retry
   their shares. Admins can use **Retry pending and failed payouts**. Saving
   prize settings or opening `/awardceremony #<tournament-id>` also checks unpaid
   prizes without repeating successful credits.
4. Verify the payout receipt, the recipient's account balances and the numeric
   coin/gem changes in activity history.

Existing prizes marked paid by older versions are never paid again when the
per-recipient ledger is introduced. Their legacy receipts are backfilled as
paid using the original recipient, amount and award time. A historical balance
after payment cannot be reconstructed and is left blank. A legacy honour's
nonzero paid coin/gem amounts are its existing payment receipt. New entitlements
are created only for unpaid prizes, and subsequently frozen, so deployment cannot duplicate historical
awards. Setting a prize for a category that previously had none can create its
first entitlement after the final; it cannot top up an entitlement already
created or paid.
