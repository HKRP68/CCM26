"""One-time squad + trait downsizing migration for EXISTING databases.

Background: the squad cap was cut from 25 to 19 (``config.MAX_ROSTER``) and a
squad-wide trait budget was introduced (``config.TRAIT_MAX_PER_SQUAD`` = 18,
with the Career Player's own 3 slots sitting outside it). Both limits are
enforced on every acquisition path, but nothing retroactively shrinks a squad
that was already over them — an existing captain would simply be stuck above
the cap, unable to buy or claim anything, with no way to know why.

This script brings such accounts back in line:

  • Cards — the lowest-rated cards are released until the squad is at
    MAX_ROSTER. Each one is refunded at its FULL BUY PRICE
    (``config.get_buy_value``), not the lossy sell price: the captain did not
    choose to sell, so they should not eat the buy/sell spread. Traits equipped
    on a released card go back to the owner's trait inventory, as they do for
    any other release. The Career Player is never released.

  • Traits — once the roster is down, any remaining equipped traits beyond
    TRAIT_MAX_PER_SQUAD are removed lowest-level first (then lowest rarity, then
    lowest id) and refunded in full gems (``config.trait_buy_value``). The
    Career Player's traits are exempt and never touched.

The two passes run in that order on purpose: releasing cards un-equips their
traits into inventory, which often brings the squad under the trait cap on its
own, so fewer traits have to be refunded.

Usage:
    python migrate_squad_and_trait_limits.py [--dry-run]

Idempotent: a second run is a no-op because every user is already at or under
both caps. Each user is committed separately and wrapped in its own
try/except, so one bad row can never abort the whole run.
"""

import argparse
import logging

from config import MAX_ROSTER, TRAIT_MAX_PER_SQUAD
from database import SessionLocal
from models import User
from services.roster_service import trim_roster_to_cap, get_roster_count
from services.trait_service import trim_squad_traits_to_cap, count_squad_traits

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("squad_trait_limits")


def migrate_user(session, user, dry_run=False):
    """Bring one user under both caps. Returns a summary dict."""
    summary = {"cards": 0, "coins": 0, "traits": 0, "gems": 0}

    over_cards = get_roster_count(session, user.id) - MAX_ROSTER
    over_traits = count_squad_traits(session, user.id) - TRAIT_MAX_PER_SQUAD
    if over_cards <= 0 and over_traits <= 0:
        return summary

    if dry_run:
        # Report intent without touching anything. The trait number is the
        # pre-release count, so it is an upper bound: releasing cards frees
        # traits into inventory and usually leaves fewer to refund.
        summary["cards"] = max(0, over_cards)
        summary["traits"] = max(0, over_traits)
        return summary

    if over_cards > 0:
        roster_result = trim_roster_to_cap(session, user)
        summary["cards"] = roster_result["count"]
        summary["coins"] = roster_result["coins"]

    trait_result = trim_squad_traits_to_cap(session, user)
    summary["traits"] = trait_result["count"]
    summary["gems"] = trait_result["gems"]
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    log.info("Squad cap: %s   Squad trait cap: %s%s",
             MAX_ROSTER, TRAIT_MAX_PER_SQUAD,
             "   [DRY RUN — nothing will be written]" if args.dry_run else "")

    session = SessionLocal()
    totals = {"users": 0, "cards": 0, "coins": 0, "traits": 0, "gems": 0}
    failed = 0
    try:
        user_ids = [row[0] for row in session.query(User.id).all()]
        log.info("Scanning %s users…", len(user_ids))

        for user_id in user_ids:
            user = session.query(User).get(user_id)
            if not user:
                continue
            try:
                result = migrate_user(session, user, dry_run=args.dry_run)
                if not (result["cards"] or result["traits"]):
                    session.rollback()
                    continue

                if args.dry_run:
                    session.rollback()
                else:
                    session.commit()

                totals["users"] += 1
                for key in ("cards", "coins", "traits", "gems"):
                    totals[key] += result[key]
                log.info(
                    "  user %s (tg %s): -%s cards (+%s coins), "
                    "-%s traits (+%s gems)",
                    user.id, getattr(user, "telegram_id", "?"),
                    result["cards"], f"{result['coins']:,}",
                    result["traits"], f"{result['gems']:,}",
                )
            except Exception:
                session.rollback()
                failed += 1
                log.exception("  user %s FAILED — skipped", user_id)
            finally:
                # commit() expires objects but leaves them in the identity map.
                # Without this the one long-lived Session accumulates every
                # User, UserRoster, Player and PlayerTrait touched by the whole
                # run, which matters on a large user table.
                session.expunge_all()
    finally:
        session.close()

    log.info("─" * 60)
    log.info("Users adjusted : %s", totals["users"])
    log.info("Cards released : %s  (refunded %s coins)",
             totals["cards"], f"{totals['coins']:,}")
    log.info("Traits refunded: %s  (refunded %s gems)",
             totals["traits"], f"{totals['gems']:,}")
    if failed:
        log.warning("Users skipped after an error: %s — re-run to retry", failed)
    log.info("Done.")


if __name__ == "__main__":
    main()
