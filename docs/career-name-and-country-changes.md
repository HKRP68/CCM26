# Career Player name & country changes

A Career Player is created once and kept for good, so the identity on it — the
name and the flag — is the one thing owners come back and ask to change. This is
how that is sold, and how a name somebody types is reviewed before it goes on a
card.

Everything lives in `services/career_change_service.py`. The bot surface is
`handlers/career_change.py` (`/cmuchange`, plus the ✏️ button under the card
`/cmucareer` sends), the Mini App surface is the **Name & country** panel on the
Career screen, and the website surface is **🎖 Career Players → ✏️ Name changes**.

## The price ladder

| Change | Cost |
| --- | --- |
| 1st | **free** |
| 2nd | 300 💎 |
| 3rd | 500 💎 |
| 4th | 750 💎 |
| 5th | 1000 💎 |
| … | +250 💎 each |

All four numbers (`career_change_price_2`, `career_change_price_3`,
`career_change_price_step`, and the free first change) are website-tunable on the
Name changes page. `Player.career_changes_used` is the counter the rung is read
off, and it starts at 0 for every card — including the ones created before this
shipped, so those owners still have their free change.

**One change buys both halves.** A submission may carry a new name, a new
country, or both, and it costs a single rung either way. Nobody pays twice to fix
an identity they picked in one sitting.

**The paid rungs are gated.** Nothing above the free change is sold unless the
website has `career_paid_changes_open` switched on — that is the switch for
opening paid changes up when you announce them. The free first change always
works, open or shut.

**Admins can gift a change.** *🎁 Free change* on the Career Players page adds to
`Player.career_free_changes`. A granted change costs nothing, works while paid
changes are shut, and **never advances the ladder** — it is the way to say "try
again on us" after refusing a name, without opening paid changes for everybody.

## Two kinds of name

| Where the name came from | What happens |
| --- | --- |
| 🎲 **rolled from the pool** (`source="pool"`) | applied immediately |
| ✍️ **typed by the owner** (`source="custom"`) | queued for the website |
| 🌍 country only | applied immediately |

Pool names come out of the admin-curated `career_name_pool` — the same table the
`/cmucareer` wizard deals from — so they are already vetted and there is nothing
to review. A name the owner typed is not, so it goes to the queue.

`source` arrives from the client, and it decides whether a name is reviewed, so
`name_is_from_pool` checks the claim against the pool of the country the card is
moving to rather than believing it. A name that is not really in that pool is
treated as typed, whatever it was submitted as — which is also why switching
country after rolling a name clears the roll on both the bot and the Mini App:
that name belongs to the pool it came from.

**While a typed name waits, nothing on the card moves.** Not the name, and not
the country submitted alongside it. A card is never half-changed: the owner keeps
playing under the identity they had until an admin approves the new one, at which
point both halves land together.

Two gates sit in front of a typed name before it ever reaches the queue:

* the same rules `services.career_service.validate_career_name` enforces —
  letters, spaces, apostrophes, hyphens and full stops only, 2–48 characters,
  and never a name another player already carries
* the website's **blocklist**, matched against the name with everything but
  letters and digits stripped out, so `S.l u-r` cannot walk past `slur`

`career_custom_names_open` turns typed names off entirely (pool rolls keep
working), and `career_custom_names_need_approval` makes a typed name land
instantly if you would rather not review them at all.

## Money, and getting it back

Gems are taken **when the request is made**, not when it is approved. That is
what keeps the queue honest — a speculative name costs exactly what a real one
does.

Rejecting or withdrawing a request refunds **every gem and gives the rung back**,
so a refused name costs the owner nothing and does not use up a change: their
next attempt is priced at the same rung they just paid. `CareerChangeRequest`
carries the receipt that makes this exact — `gems_charged`, `change_index` and
`used_free_grant` are everything the refund needs.

The rung is only handed back when it is still the last one used, so a change made
in between (a granted free one, say) can never be un-charged by a later refund.

Three things trigger a refund:

* an admin **rejects** the name (optionally with a reason, which is DM'd to the
  owner)
* the owner **withdraws** it from `/cmuchange` or the Mini App
* the career card is **deleted** while the name sits in the queue — approving
  then closes the request and refunds rather than stranding the owner

## Review queue

`/career/changes` on the website lists the queue and the recent decisions. Each
pending row shows who asked, what the card currently reads, what it cost them,
and whether the name trips a blocklist word that was added *after* they submitted
it (approving overrides the blocklist on purpose — the admin is overruling it).

Approving re-checks the name for a collision first: the queue is not instant, and
another card may have taken it in the meantime. A clash leaves the request
**pending** rather than failing it, so the admin can reject-and-refund on purpose
rather than losing the request to a race.

Either decision DMs the owner — the new name, or the reason it was refused and
the refund.

## Where the pieces are

| Piece | File |
| --- | --- |
| Pricing, gating, submission, review | `services/career_change_service.py` |
| Bot: `/cmuchange`, ✏️ button, typed-name capture | `handlers/career_change.py` |
| Mini App panel + API | `templates/webapp.html`, `admin.py` (`/api/webapp/career/change/*`) |
| Website queue + settings | `admin.py` (`/career/changes`), `templates/admin_career_changes.html` |
| Request rows, counters, settings columns | `models.py` (`CareerChangeRequest`, `Player`, `GameConfig`) |
| Tests | `tests/test_career_change.py` |

The typed name arrives as an ordinary chat message, caught by a handler in its
own PTB group (8) that returns on a dictionary miss for every user who has not
asked to type one — so it can never starve the other text handlers.
