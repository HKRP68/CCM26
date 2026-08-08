"""One-time squad + trait downsizing sweep for EXISTING databases.

The squad cap was cut from 25 to 19 (``config.MAX_ROSTER``) and a squad-wide
trait budget was introduced (``config.TRAIT_MAX_PER_SQUAD`` = 18, with the
Career Player's own 3 slots sitting outside it). Both limits are enforced on
every acquisition path, but nothing retroactively shrinks a squad that was
already over them — such a captain is simply stuck above the cap, unable to buy
or claim anything, with no way to know why.

The work itself lives in ``services/squad_downsize_service.py``, which the
**Apply squad limits** button on the admin Maintenance page also uses, so the
CLI and the website can never drift apart. See that module for what happens to
an account and why the refunds are at buy price.

Usage:
    python migrate_squad_and_trait_limits.py [--dry-run]

Idempotent: a second run is a no-op because every user is already at or under
both caps. Each user is committed separately and wrapped in its own
try/except, so one bad row can never abort the whole run.

NOTE: this CLI does **not** notify anyone. The Apply button DMs every affected
captain a summary of what left their squad; a shell run is silent. If the
players should hear about it, use the button.
"""

import argparse
import logging

from config import MAX_ROSTER, TRAIT_MAX_PER_SQUAD
from database import SessionLocal
from services.squad_downsize_service import preview_downsize, run_downsize

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("squad_trait_limits")


def main():
    """Walk every over-cap user, bring them back in line, report totals."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    log.info("Squad cap: %s   Squad trait cap: %s%s",
             MAX_ROSTER, TRAIT_MAX_PER_SQUAD,
             "   [DRY RUN — nothing will be written]" if args.dry_run else "")

    session = SessionLocal()
    try:
        if args.dry_run:
            # The preview runs the real downsizing and rolls it back, so these
            # are the exact numbers an apply would produce.
            totals = preview_downsize(session)
        else:
            totals = run_downsize(session, log=log)
    finally:
        session.close()

    log.info("─" * 60)
    log.info("Users adjusted : %s", totals["users"])
    log.info("Cards released : %s  (refunded %s coins)",
             totals["cards"], f"{totals['coins']:,}")
    log.info("Traits refunded: %s  (refunded %s gems)",
             totals["traits"], f"{totals['gems']:,}")
    if totals["failed"]:
        log.warning("Users skipped after an error: %s — re-run to retry",
                    totals["failed"])
    log.info("Done.")


if __name__ == "__main__":
    main()
