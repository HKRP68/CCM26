# CMU Franchise Draft 2.0 — operator guide

This extends `services/franchise_draft.py`; it does not create another draft
engine or player inventory. The same `AuctionSeason`, `AuctionFranchise`,
`AuctionLot`, catalogue IDs, ownership, ledger and League Season publication are
used by auctions and drafts. Auction workflows retain their existing behavior.

## Prepare and launch

1. From a completed tournament choose **Existing leagues → Draft**; Auction
   remains available as the other acquisition mode. Tournament management shows
   the selected mode. Existing squads and budgets do not copy automatically.
2. Set rounds and franchise owners. `/drounds 15` previews a 15-place squad for
   each team. New franchises can join during setup. Acquisition mode/order
   cannot be silently switched after activity begins.
3. Preview equal purses with `/dpurse all | 100cr`, or individual balances with
   `/dpurse North | 120cr`. Explicit adjustments can account for carryover
   permitted by competition rules; old balances are never assumed.
4. Import a CSV or use `/dpooladd ids 12,34,56` and
   `/dpooladd 80-96 | BAT | Base`. Category and version are optional; `*` means
   any value. Catalogue records are identified by their database IDs. Withdraw
   eligible pool cards with `/dpoolremove #12 #34`; adding their catalogue IDs
   again restores eligibility without creating another card.
5. Send `/dprices` with a complete rating table. `96:23cr`, `96 : 23 Cr` and
   `96=23.5cr` are accepted. `/dcustom Joe Root:25.5cr` overrides a rating price;
   ambiguous names list exact versions/IDs. `/dcustom remove #12` restores the
   rating price. Price previews show the full table, player counts, original
   prices, custom targets and final prices. Signed prices never change.
6. Set the cyclic initial order and the existing auction category/rating rules.
   No overseas restriction applies. The first team moves to the end each round;
   this is not a snake draft. The full order uses three-round pages with
   **Earlier/Later rounds** buttons, including later rotations.
7. Configure any permitted retention and the timeout policy below. `/dpreview`
   checks readiness. `/dstart` opens a preview; confirm it to launch and announce
   the first team. Cancelling does not start the draft.

All money is stored as integer lakh and displayed in Crores. Values must resolve
to whole lakh, for example `25.5cr` or `0.01cr`; finer values are refused instead
of rounded. No floating-point money calculation is used.

## Retained contracts and slots

Retention defaults to zero. `/dretention 2` previews a cap of two retained
contracts per team. Then `/dretain North | p123 | 10cr` previews keeping the
exact previous-season player record. `p123` is catalogue player ID 123; `#123`
is pool lot ID 123. The franchise must hold that player's previous-season rights.

Each retained contract is signed and charged through the existing squad and
ledger, occupies a squad place, and reduces the team's acquisition allowance.
No squad can exceed the round count. Full teams receive no further acquisition
turn. `/dunretain North | #12` previews a full refund before launch. Retention
changes are locked after launch so existing turn history cannot be corrupted.

## Picking and dashboards

The current owner or co-owner can use `/Dpick Joe Root`, `/dpick #12` or
`/dpick p123`. `/dsearch Root`, available-player buttons and the native player
pool offer exact-version pick buttons. Confirmations show version, country,
OVR/BAT/BOWL, price, current/resulting purse and squad places.

Only the user who requested a signing may confirm it. Both competing owner and
co-owner quotes may exist, but the revision lock permits only one signing for
the turn. Confirmation checks current membership, availability, price, timer,
budget and squad rules in the same transaction as the debit and assignment.
Cancel spends nothing and keeps the turn. Expired, stale, forwarded or duplicate
controls cannot sign or debit. Owners cannot pick on another franchise's turn.

The edited board shows round, scheduled pick, completed selections, retained
contracts, pending places, player count, current/next team, squads and purses.
Shared read buttons open menus belonging to the reader. `/dhistory` pages through
original signing prices, contracts, skipped turns and administrator audit events,
including events whose picks were later reversed. Confirmed signings and draft
completion receive their own announcements and reliable user-ID mentions.

## Timer policies

`/dclock 30s | pause` or `/dclock 5m | skip` opens an approval preview.

| Policy | On expiry |
| --- | --- |
| `pause` | Pause and preserve the acquisition place |
| `admin` | Pause for an authorized administrator resolution |
| `skip` | Defer the place to the existing FIFO catch-up queue |
| `auto` | Sign the highest-rated eligible, affordable player; stable pool-ID order breaks ties |

New drafts pause by default; automatic selection/skipping needs approval.
Existing drafts preserve their prior behavior after upgrade. `0` disables the
clock. Durations up to 24 hours are accepted. `/dtimer 10` remains available and
changes duration without changing the policy. Pause preserves remaining time;
the next team gets a fresh full window. Restart recovery resolves one expired
turn rather than skipping every team that would have timed out during downtime.

Automatic picks run the same validation and atomic signing as manual picks and
are logged as **System**, not as a manual owner confirmation. If no legal choice
exists, the draft pauses without spending purse or losing a slot.

## Previews, corrections and recovery

Prices, rounds, purse changes, pool changes, retention, start/resume and undo
commands open durable administrator previews. They expire after ten minutes.
Confirmation checks the current administrator role, opener ID, season, revision,
pool, rules, ownership and financial snapshot. No change occurs before approval;
cancelled, stale or repeated approvals cannot apply the action twice.

After launch, normal prices stay locked even while paused or after undo. Pause
and supply a recorded reason for a correction:

```text
/dcorrect prices | Correct price sheet | 96:23cr
/dcorrect custom | Fix exact version price | #12:25.5cr
/dcorrect custom | Remove override | remove #12
/dcorrect rounds | One extra place per team | 16
/dcorrect purse | Approved carryover | North | 120cr
/dcorrect pooladd | Replacement players | ids 12,34
/dcorrect poolremove | Player withdrew | #12
/dcorrect undo | Wrong card selected |
/dcorrect clock | Revised pick window | 60s | pause
/dcorrect resolve | Owner unavailable | skip
```

Each applied correction records the actual administrator, reason, request and
before/after preview. Undo reverses the **latest** committed turn, refunds the
original price and restores its player, counters, queue and acquisition place.
To correct an earlier turn, reverse intervening turns in order; subsequent picks
are never silently rewritten. Admin resolution can explicitly defer a paused
turn, preserving its slot in the catch-up queue. Owners cannot use admin overrides.

## Readiness and publication

Readiness checks owners, unique player IDs, resolved prices, pool size, available
places, budgets, role minima/maxima, rating restrictions and distinct-player
supply. Minimum-cost flow checks role/rating intersections and aggregate supply;
an exact, bounded search also checks distinct-player combinations in the final
three slots. Mandatory-category budget reserves prevent spending the money
needed for remaining roles. Complex endgames exceeding the safety bound require
administrator review rather than an unchecked signing.

After all places and catch-up turns are complete, final rules are validated and
the bot prepares a summary. `/dpublish` uses the existing season/league pipeline,
with no subsequent auction required. Publishing locks changes to finished
squads. Telegram send/edit failures are retried after database commit and cannot
undo a completed signing or double-charge a retry.

Only one administrator-action table and two nullable fields on the existing
draft configuration were added. Startup creates/migrates these without replacing
seasons or squads. `/draftcommands` lists all 48 commands/aliases, including the
new controls; the native draft room also offers their guided forms.
