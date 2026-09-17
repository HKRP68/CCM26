"""Check every Franchise Auction purse against its own ledger, and repair it.

``auction_franchises.purse_remaining_lakh`` is a **cache** over
``auction_ledger``: the ledger is the record of why a franchise has the money it
has, and the column is what the auction actually charges against. They are
written in the same transaction and should never disagree — but a cache that is
only checked by the code that maintains it is a cache nobody can prove.

So this walks every auction and re-sums. The work itself lives in
``services/auction_service.reconcile_purses``, which the **🔍 Reconcile
purses** button on the auction's setup page also calls, so the CLI and the
website can never drift apart.

**A repair writes a `correction` row; it never overwrites either side.** The
cache is what people were actually charged and the ledger is the record of why,
and silently rewriting either one destroys the evidence of whichever went
wrong. What you get instead is a ledger that adds up again and a row saying so.

Usage:
    python migrate_auction_purse_reconcile.py [--dry-run] [--season-id N]

Idempotent: a second run reports nothing, because the first one made the two
agree. Each auction is committed separately and wrapped in its own
try/except, so one bad row can never abort the whole run.
"""

import argparse
import logging

from database import SessionLocal
from models import AuctionSeason
from services import auction_service as auction

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("auction_purse_reconcile")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report the drift and change nothing")
    parser.add_argument("--season-id", type=int, default=None,
                        help="only this auction (default: every one)")
    args = parser.parse_args()

    session = SessionLocal()
    seasons_checked = drifted = repaired = 0
    try:
        query = session.query(AuctionSeason).order_by(AuctionSeason.id)
        if args.season_id:
            query = query.filter(AuctionSeason.id == args.season_id)

        for season in query.all():
            seasons_checked += 1
            try:
                drift = auction.reconcile_purses(session, season,
                                                 repair=not args.dry_run)
                if not drift:
                    log.info("#%s %-30s ok", season.id, season.name)
                    session.rollback()
                    continue
                drifted += len(drift)
                for franchise, cached, summed, delta in drift:
                    log.warning(
                        "#%s %-30s %-20s purse %s · ledger %s · out by %s",
                        season.id, season.name, franchise.name,
                        auction.render_money(cached),
                        auction.render_money(summed),
                        auction.render_money(delta))
                if args.dry_run:
                    session.rollback()
                else:
                    session.commit()
                    repaired += len(drift)
            except Exception:
                session.rollback()
                log.exception("#%s failed", season.id)
    finally:
        session.close()

    log.info("")
    log.info("%s auction(s) checked · %s purse(s) out of step · %s repaired%s",
             seasons_checked, drifted, repaired,
             " (dry run — nothing written)" if args.dry_run else "")


if __name__ == "__main__":
    main()
