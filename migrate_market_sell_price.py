"""One-time migration: make every market slot's sell price equal its base price.

Background: the Player Market used to bake a blanket discount into
``final_price`` when it listed a slot — 5% off ``base_price`` for the global
market, 10% off for the legacy per-user market. The membership tiers now own the
discount entirely: base price IS the sell price, free/Bronze/Silver members pay
it in full, and only Platinum (5%) and Diamond (10%) pay less, applied
per-buyer in ``subscription_service.market_price``.

``final_price`` keeps its meaning as the admin-editable "Sell" column on the
markets page, so a slot left at the old discounted value would read as a
store-wide sale that every free user gets. This normalises the rows that are
already listed. (The daily reroll writes the new prices too, so it would
self-heal within a day; run this to fix the live market immediately.)

Only PLAYER market rows are touched — the trait market's ``discount_pct``
sales are a separate, deliberate mechanic and are left alone.

Usage:
    python migrate_market_sell_price.py

Idempotent: re-running after a successful pass updates nothing (no-op).
"""

import logging

from database import SessionLocal
from models import GlobalPlayerMarket, PlayerMarket

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("market_sell_price")


def main():
    session = SessionLocal()
    try:
        total = 0
        for model, label in ((GlobalPlayerMarket, "global player market"),
                             (PlayerMarket, "legacy per-user player market")):
            updated = (session.query(model)
                       .filter(model.final_price != model.base_price)
                       .update({model.final_price: model.base_price},
                               synchronize_session=False))
            log.info("%s: reset sell price on %s slot(s).", label, updated)
            total += updated
        session.commit()
        log.info("Done — %s slot(s) now sell at their base price.", total)
    finally:
        session.close()


if __name__ == "__main__":
    main()
