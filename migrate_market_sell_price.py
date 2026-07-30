"""One-time migration: retire the Player Market's baked-in blanket discount.

Background: the Player Market used to bake a blanket discount into
``final_price`` when it listed a slot — 5% off ``base_price`` for the global
market, 10% off for the legacy per-user market. The membership tiers now own the
discount entirely: base price IS the sell price, free/Bronze/Silver members pay
it in full, and only Platinum (5%) and Diamond (10%) pay less, applied
per-buyer in ``subscription_service.market_price``.

``final_price`` keeps its meaning as the admin-editable "Sell" column on the
markets page, so a slot left at the old discounted value would read as a
store-wide sale that every free user gets. This resets those rows.

Scope: a row is only reset when its ``final_price`` is EXACTLY what the old
listing code would have written (``int(base_price * 0.95)`` for the global
market, ``int(base_price * 0.9)`` for the per-user one). Any other value is an
admin's deliberate sale price and is left untouched — the new pricing model
honours those, applying the membership discount on top. A hand-set sale that
happens to land exactly on the old formula is indistinguishable from a legacy
row and will be reset; re-enter it on the markets page if that happens.

Only PLAYER market rows are considered — the trait market's ``discount_pct``
sales are a separate, deliberate mechanic.

The daily reroll writes the new prices too, so the global market would
self-heal within a day; run this to fix the live market immediately.

Usage:
    python migrate_market_sell_price.py          # apply
    python migrate_market_sell_price.py --dry-run # report only, change nothing

Idempotent: re-running after a successful pass updates nothing (no-op).
"""

import logging
import sys

from database import SessionLocal
from models import GlobalPlayerMarket, PlayerMarket

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("market_sell_price")

# (model, label, legacy sell price the old listing code wrote). The lambdas
# reproduce the original expressions exactly — including float truncation — so
# the comparison matches what actually landed in the column.
LEGACY_PRICING = (
    (GlobalPlayerMarket, "global player market",
     lambda base: int(base * (1 - 5 / 100))),      # was: flat 5% off
    (PlayerMarket, "legacy per-user player market",
     lambda base: int(base * (1 - 0.10))),         # was: flat 10% off
)


def main(dry_run=False):
    session = SessionLocal()
    try:
        total_reset = 0
        total_kept = 0
        for model, label, legacy_price in LEGACY_PRICING:
            rows = (session.query(model)
                    .filter(model.final_price != model.base_price).all())
            reset, kept = 0, 0
            for row in rows:
                if row.final_price == legacy_price(row.base_price):
                    if not dry_run:
                        row.final_price = row.base_price
                    reset += 1
                else:
                    # An admin's own sale price — the new model honours it.
                    kept += 1
                    log.info("  %s slot %s: keeping admin sale price %s "
                             "(base %s).", label, row.slot_index,
                             row.final_price, row.base_price)
            log.info("%s: %s legacy slot(s) reset to base price, "
                     "%s admin sale(s) kept.", label, reset, kept)
            total_reset += reset
            total_kept += kept

        if dry_run:
            session.rollback()
            log.info("Dry run — nothing written. Would reset %s slot(s) and "
                     "keep %s admin sale(s).", total_reset, total_kept)
        else:
            session.commit()
            log.info("Done — %s slot(s) now sell at their base price, "
                     "%s admin sale(s) left as-is.", total_reset, total_kept)
    finally:
        session.close()


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
